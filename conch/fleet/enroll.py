"""Trusted-host enrollment (Swarm Phase 2), built on ssh_control.py.

Enrollment turns an operator-controlled SSH host into a fleet worker:

1. **Interactive first-enroll** with strict host-key verification. The
   operator opens the connection through the existing secure-terminal
   path (Conch never stores the password; the human authenticates at the
   real prompt) so the host key is learned under human supervision.
2. **Bootstrap hostctl**: stream the single-file ``conch/fleet/hostctl.py``
   and ``install-verify`` it against its sha256 — a bare host needs only
   ``python3``.
3. **BatchMode probe**: run ``conch-hostctl probe`` under
   ``BatchMode=yes``. A host is recorded ``autonomy_capable`` only when
   restart-safe key-based auth works — i.e. the non-interactive probe
   succeeds without a prompt. Password-only hosts enroll but cannot run
   unattended.
4. **Trust anchor + registry**: install the allowed-signers file and
   enroll the worker in the :class:`FleetRegistry` (PENDING), recording
   its observed capabilities and supported runtime profiles.

Never uses sshpass, stored passwords, agent forwarding, or automated
sudo (``ssh_control.validate_remote_command`` refuses those shapes). No
secrets ever appear in argv, receipts, events, or logs.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..ssh_control import SSHControlManager, SSHTarget
from .registry import FleetRegistry

RunnerResult = Tuple[int, bytes, bytes]
Runner = Callable[[list, bytes, float], RunnerResult]


class EnrollmentError(Exception):
    pass


def hostctl_source_path() -> Path:
    from . import hostctl

    return Path(hostctl.__file__).resolve()


def hostctl_digest() -> str:
    return hashlib.sha256(hostctl_source_path().read_bytes()).hexdigest()


def _default_runner(argv: list, stdin_bytes: bytes,
                    timeout: float) -> RunnerResult:
    try:
        proc = subprocess.run(
            argv, input=stdin_bytes, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise EnrollmentError("remote command timed out")
    except OSError as exc:
        raise EnrollmentError(f"cannot launch ssh: {exc}")
    return proc.returncode, proc.stdout, proc.stderr


class Enroller:
    """Drives enrollment against one host. I/O goes through a ``runner`` so
    the flow is testable without a reachable SSH host; production uses the
    default subprocess runner over OpenSSH ControlMaster sessions."""

    def __init__(self, registry: FleetRegistry, *,
                 manager: Optional[SSHControlManager] = None,
                 hostctl_remote_dir: str = "~/.local/state/conch-fleet",
                 runner: Optional[Runner] = None,
                 confirm: Optional[Callable[[str], bool]] = None):
        self.registry = registry
        self.manager = manager or SSHControlManager()
        self.remote_dir = hostctl_remote_dir
        self._runner = runner or _default_runner
        self._confirm = confirm

    # -- remote steps ---------------------------------------------------------

    def _exec(self, target: SSHTarget, command: str,
              stdin_bytes: bytes = b"", *, timeout: float = 60.0
              ) -> RunnerResult:
        """Run one remote command over the managed session (BatchMode,
        multiplexed over the ControlMaster when one is up)."""
        argv = self.manager.exec_argv(target, command, tty=False)
        return self._runner(argv, stdin_bytes, timeout)

    def _autonomy_argv(self, target: SSHTarget) -> list:
        """A fresh, non-multiplexed BatchMode connection: succeeds only with
        restart-safe key-based auth (no password, no reused master)."""
        options: List[str] = ["ssh", "-o", "BatchMode=yes",
                              "-o", "ControlMaster=no",
                              "-o", "ControlPath=none"]
        if target.port is not None:
            options += ["-p", str(target.port)]
        if target.user:
            options += ["-l", target.user]
        options += ["--", target.host, "true"]
        return options

    def check_autonomy(self, target: SSHTarget) -> bool:
        rc, _out, _err = self._runner(
            self._autonomy_argv(target), b"", 20.0
        )
        return rc == 0

    def probe(self, target: SSHTarget, *,
              hostctl_cmd: str = "") -> Tuple[bool, Dict]:
        """Run ``conch-hostctl probe``. Returns (ok, probe_dict)."""
        cmd = (hostctl_cmd or "conch-hostctl") + " probe"
        rc, stdout, stderr = self._exec(target, cmd)
        if rc != 0:
            return False, {
                "error": stderr.decode("utf-8", "replace").strip()
                or f"probe exited {rc}",
            }
        try:
            data = json.loads(stdout.decode("utf-8", "replace"))
        except ValueError as exc:
            return False, {"error": f"probe output not JSON: {exc}"}
        return bool(data.get("ok")), data

    def bootstrap_hostctl(self, target: SSHTarget) -> Dict:
        """Stream the single-file hostctl and install-verify it. Returns the
        install receipt dict."""
        digest = hostctl_digest()
        source = hostctl_source_path().read_bytes()
        staged = f"{self.remote_dir}/hostctl.py.new"
        installed = f"{self.remote_dir}/hostctl.py"
        # 1. mkdir + write the streamed bytes to the staging path via a
        #    here-safe cat (bytes on stdin, never argv).
        mkdir = f"mkdir -p {self.remote_dir} && cat > {staged}"
        rc, _out, err = self._exec(target, mkdir, stdin_bytes=source)
        if rc != 0:
            raise EnrollmentError(
                "failed to stage hostctl: "
                + err.decode("utf-8", "replace").strip()
            )
        # 2. verify + install through the freshly staged file itself.
        verify = (
            f"python3 {staged} --home {self.remote_dir} install-verify"
            f" --digest {digest} --source {staged} --dest {installed}"
        )
        rc, out, err = self._exec(target, verify)
        if rc != 0:
            raise EnrollmentError(
                "hostctl install-verify failed (digest mismatch or missing"
                " python3): "
                + (err.decode("utf-8", "replace").strip()
                   or out.decode("utf-8", "replace").strip())
            )
        try:
            receipt = json.loads(out.decode("utf-8", "replace"))
        except ValueError as exc:
            raise EnrollmentError(f"install-verify output not JSON: {exc}")
        if not receipt.get("ok"):
            raise EnrollmentError(
                f"install-verify refused: {receipt.get('error')}"
            )
        return receipt

    def install_trust_anchor(self, target: SSHTarget,
                             allowed_signers_bytes: bytes,
                             hostctl_cmd: str, op_id: str) -> Dict:
        cmd = f"{hostctl_cmd} trust-install --op-id {op_id}"
        rc, out, err = self._exec(
            target, cmd, stdin_bytes=allowed_signers_bytes
        )
        if rc != 0:
            raise EnrollmentError(
                "trust-install failed: "
                + err.decode("utf-8", "replace").strip()
            )
        return json.loads(out.decode("utf-8", "replace"))

    # -- orchestration --------------------------------------------------------

    def enroll(self, name: str, target: SSHTarget, *,
               trust_level: int = 1, data_ceiling: str = "internal",
               labels: Optional[Dict] = None, resource_group: str = "",
               max_concurrency: int = 1,
               allowed_signers_bytes: bytes = b"",
               interactive_first: bool = True) -> Dict:
        """Full enrollment. Returns a receipt with the worker id, the probe,
        and whether the host is autonomy-capable.

        ``interactive_first`` records that the operator opened the first
        connection under supervision (strict host-key learning); the actual
        secure-terminal handoff is the caller's responsibility (it must not
        be captured). This method then bootstraps and probes non-interactively.
        """
        existing = self.registry.find(name)
        if existing is not None:
            raise EnrollmentError(
                f"a worker named {name!r} is already enrolled"
                f" ({existing['worker_id']})"
            )
        if interactive_first and self._confirm is not None:
            if not self._confirm(
                f"Confirm the host key for {target.identity} was verified"
                " interactively (strict host-key) before enrolling?"
            ):
                raise EnrollmentError(
                    "enrollment aborted: host key not confirmed"
                )
        install_receipt = self.bootstrap_hostctl(target)
        installed = install_receipt["installed"]
        hostctl_cmd = f"python3 {installed} --home {self.remote_dir}"
        probe_ok, probe = self.probe(target, hostctl_cmd=hostctl_cmd)
        if not probe_ok:
            raise EnrollmentError(
                f"probe failed on {target.identity}: {probe.get('error')}"
            )
        # Autonomy-capable ONLY if a fresh, restart-safe key-based BatchMode
        # connection works — a password-only host still enrolls, but never
        # runs unattended.
        autonomy_ok = self.check_autonomy(target)
        profiles = probe.get("profiles") or []
        capabilities = {
            key: probe[key] for key in (
                "os", "os_release", "arch", "python", "systemd", "docker",
                "disk", "cgroups", "gpu", "model_endpoints", "hostname",
            ) if key in probe
        }
        runtime_profile = profiles[0] if profiles else ""
        worker_id = self.registry.enroll(
            name, target.host, ssh_user=target.user, ssh_port=target.port,
            trust_level=trust_level, data_ceiling=data_ceiling,
            labels=labels, capabilities=capabilities, profiles=profiles,
            runtime_profile=runtime_profile, resource_group=resource_group,
            max_concurrency=max_concurrency,
            autonomy_capable=bool(autonomy_ok),
        )
        trust_receipt = None
        if allowed_signers_bytes:
            trust_receipt = self.install_trust_anchor(
                target, allowed_signers_bytes, hostctl_cmd,
                op_id=f"trust-{worker_id}",
            )
        return {
            "worker_id": worker_id,
            "autonomy_capable": bool(autonomy_ok),
            "profiles": profiles,
            "runtime_profile": runtime_profile,
            "install": install_receipt,
            "trust": trust_receipt,
            "probe": probe,
        }


def build_ssh_target(host: str, user: str = "",
                     port: Optional[int] = None) -> SSHTarget:
    return SSHTarget(host=host, user=user, port=port)


__all__ = [
    "Enroller", "EnrollmentError", "hostctl_digest",
    "hostctl_source_path", "build_ssh_target",
]
