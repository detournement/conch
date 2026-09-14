"""conch-hostctl: the on-host fleet control utility (Swarm Phase 2 gates).

Covered here: checksum-verified self-install, capability probe on this
host, the content-addressed artifact store (atomic, idempotent), the
fail-closed signed deploy chain, idempotent deploy/activate/rollback
receipts (repeated deployment is a no-op), the deployment lock, worker
lifecycle under the process profile with a real supervised subprocess,
hardened systemd unit validation (textual everywhere, plus a real
systemd-analyze verify on hosts that have it), and the bounded RPC relay.
"""

import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from conch.fleet import hostctl
from conch.fleet.artifacts import (
    allowed_signers_line,
    generate_signing_key,
    sha256_file,
    sign_manifest,
    write_manifest,
)

HAVE_SSH_KEYGEN = shutil.which("ssh-keygen") is not None

#: A tiny long-running stand-in for the worker supervisor: enough to prove
#: process-profile supervision (start/status/stop, process-group kill).
STUB_WORKER_MAIN = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
    "sys.stderr.write('stub worker started: %r\\n' % (sys.argv[1:],))\n"
    "sys.stderr.flush()\n"
    "while True:\n"
    "    time.sleep(0.2)\n"
)


def run_hostctl(argv, home, stdin_bytes=b""):
    """Drive hostctl exactly as SSH would: a real subprocess over pipes."""
    env = dict(os.environ)
    env["CONCH_FLEET_HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, "-m", "conch.fleet.hostctl", *argv],
        input=stdin_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, timeout=90, cwd=str(Path(__file__).resolve().parents[1]),
    )
    return proc


def hostctl_json(argv, home, stdin_bytes=b"", expect_rc=0):
    proc = run_hostctl(argv, home, stdin_bytes)
    if expect_rc is not None:
        assert proc.returncode == expect_rc, (
            f"rc={proc.returncode} stdout={proc.stdout!r}"
            f" stderr={proc.stderr!r}"
        )
    return json.loads(proc.stdout.decode("utf-8"))


class HostctlCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "fleet"
        patcher = patch.dict(os.environ, {
            "CONCH_FLEET_HOME": str(self.home),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host = hostctl.Host(self.home)


class TestInstallVerify(HostctlCase):
    def test_bootstrap_stream_and_install_verify(self):
        """The real enrollment shape: stream the single file, verify, move."""
        source_bytes = Path(hostctl.__file__).read_bytes()
        digest = hashlib.sha256(source_bytes).hexdigest()
        staged = self.root / "hostctl.py.new"
        staged.write_bytes(source_bytes)
        proc = subprocess.run(
            [sys.executable, str(staged), "--home", str(self.home),
             "install-verify", "--digest", digest,
             "--source", str(staged),
             "--dest", str(self.home / "hostctl.py")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        result = json.loads(proc.stdout.decode())
        self.assertTrue(result["ok"])
        installed = self.home / "hostctl.py"
        self.assertTrue(installed.is_file())
        self.assertEqual(sha256_file(installed), digest)
        self.assertTrue(installed.stat().st_mode & 0o111)
        # The state skeleton exists with private permissions.
        for sub in ("store/sha256", "keys", "receipts", "locks", "workers"):
            self.assertTrue((self.home / sub).is_dir())

    def test_digest_mismatch_refuses_and_removes_nothing_verified(self):
        staged = self.root / "hostctl.py.new"
        staged.write_bytes(b"print('evil')\n")
        result = hostctl_json(
            ["install-verify", "--digest", "0" * 64,
             "--source", str(staged)],
            self.home, expect_rc=1,
        )
        self.assertFalse(result["ok"])
        self.assertIn("mismatch", result["error"])
        self.assertFalse((self.home / "hostctl.py").exists())


class TestProbe(HostctlCase):
    def test_probe_reports_this_host(self):
        result = hostctl_json(["probe"], self.home)
        self.assertTrue(result["ok"])
        self.assertIn(result["os"], ("darwin", "linux"))
        self.assertTrue(result["arch"])
        self.assertTrue(result["python"]["version"].startswith("3."))
        self.assertIn("systemd", result)
        self.assertIn("docker", result)
        self.assertIn("gpu", result)
        self.assertIn("model_endpoints", result)
        self.assertGreater(result["disk"]["total"], 0)
        self.assertIn("process", result["profiles"])
        if sys.platform == "darwin":
            self.assertFalse(result["systemd"]["present"])
            self.assertNotIn("systemd", result["profiles"])

    def test_probe_never_carries_environment_values(self):
        canary = "PROBE-CANARY-0f9b"
        with patch.dict(os.environ, {"SUPER_SECRET_TOKEN": canary}):
            result = hostctl_json(["probe"], self.home)
        self.assertNotIn(canary, json.dumps(result))


class TestArtifactStore(HostctlCase):
    def test_put_get_has_roundtrip_atomic_and_idempotent(self):
        payload = os.urandom(300000)
        digest = hashlib.sha256(payload).hexdigest()
        first = hostctl_json(
            ["artifact-put", "--digest", digest], self.home, payload
        )
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["size"], len(payload))
        again = hostctl_json(
            ["artifact-put", "--digest", digest], self.home, payload
        )
        self.assertTrue(again["duplicate"])
        has = hostctl_json(["artifact-has", "--digest", digest], self.home)
        self.assertTrue(has["present"])
        proc = run_hostctl(["artifact-get", "--digest", digest], self.home)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, payload)
        # No .part debris left behind.
        parts = list((self.home / "store" / "sha256").glob("*.part*"))
        self.assertEqual(parts, [])

    def test_wrong_digest_stores_nothing(self):
        payload = b"not the advertised bytes"
        result = hostctl_json(
            ["artifact-put", "--digest", "a" * 64], self.home, payload,
            expect_rc=1,
        )
        self.assertFalse(result["ok"])
        self.assertIn("mismatch", result["error"])
        store = self.home / "store" / "sha256"
        self.assertEqual(
            [p for p in store.glob("*") if p.is_file()], [],
        )

    def test_malformed_digest_rejected(self):
        result = hostctl_json(
            ["artifact-has", "--digest", "../escape"], self.home,
            expect_rc=1,
        )
        self.assertIn("invalid sha256", result["error"])


def _stub_artifact(root: Path, main_py: str = STUB_WORKER_MAIN):
    """Build+sign a stub artifact (conch-only pyz with a stub __main__)."""
    from conch.fleet.artifacts import build_worker_artifact

    artifact = root / "stub-worker.pyz"
    manifest = build_worker_artifact(
        artifact, packages=("conch",), main_py=main_py
    )
    manifest_path = write_manifest(artifact, manifest)
    return artifact, manifest_path


@unittest.skipUnless(HAVE_SSH_KEYGEN, "ssh-keygen not on PATH")
class SignedDeployCase(HostctlCase):
    """Common scaffolding: a signed stub release in the host store."""

    def setUp(self):
        super().setUp()
        self.key, self.pub = generate_signing_key(self.root / "keys")
        signers = allowed_signers_line("fleet@test", self.pub)
        result = hostctl_json(
            ["trust-install", "--op-id", "trust-1"], self.home,
            signers.encode(),
        )
        self.assertTrue(result["ok"])
        self.artifact, self.manifest_path = _stub_artifact(self.root)
        self.sig_path = sign_manifest(self.manifest_path, self.key)
        self.digests = {}
        for label, path in (("artifact", self.artifact),
                            ("manifest", self.manifest_path),
                            ("signature", self.sig_path)):
            digest = sha256_file(path)
            self.digests[label] = digest
            hostctl_json(
                ["artifact-put", "--digest", digest], self.home,
                path.read_bytes(),
            )

    def deploy(self, op_id="deploy-1", worker="w1", expect_rc=0,
               **overrides):
        argv = [
            "deploy", "--worker", worker, "--op-id", op_id,
            "--artifact-digest",
            overrides.get("artifact_digest", self.digests["artifact"]),
            "--manifest-digest",
            overrides.get("manifest_digest", self.digests["manifest"]),
            "--signature-digest",
            overrides.get("signature_digest", self.digests["signature"]),
            "--profile", overrides.get("profile", "process"),
        ]
        return hostctl_json(argv, self.home, expect_rc=expect_rc)


class TestSignedDeployChain(SignedDeployCase):
    def test_deploy_verifies_and_stages(self):
        receipt = self.deploy()
        self.assertTrue(receipt["ok"])
        self.assertTrue(receipt["staged"])
        self.assertFalse(receipt["noop"])
        self.assertEqual(receipt["verified_principal"], "fleet@test")
        release = (self.home / "workers" / "w1" / "releases"
                   / f"{self.digests['artifact']}.pyz")
        self.assertTrue(release.is_file())
        self.assertEqual(sha256_file(release), self.digests["artifact"])

    def test_repeated_deploy_same_op_returns_recorded_receipt(self):
        first = self.deploy(op_id="deploy-dup")
        second = self.deploy(op_id="deploy-dup")
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(first["digest"], second["digest"])

    def test_repeated_deploy_same_content_is_noop(self):
        self.deploy(op_id="deploy-a")
        second = self.deploy(op_id="deploy-b")
        self.assertTrue(second["noop"])

    def test_unsigned_deploy_fails_closed(self):
        """No signature blob in the store → nothing stages."""
        result = self.deploy(
            op_id="deploy-nosig", signature_digest="b" * 64, expect_rc=1,
        )
        self.assertFalse(result["ok"])
        self.assertFalse(
            (self.home / "workers" / "w1" / "releases").exists()
        )

    def test_tampered_artifact_fails_closed(self):
        raw = bytearray(self.artifact.read_bytes())
        raw[len(raw) // 3] ^= 0xFF
        tampered = bytes(raw)
        tampered_digest = hashlib.sha256(tampered).hexdigest()
        hostctl_json(
            ["artifact-put", "--digest", tampered_digest], self.home,
            tampered,
        )
        result = self.deploy(
            op_id="deploy-tampered", artifact_digest=tampered_digest,
            expect_rc=1,
        )
        self.assertFalse(result["ok"])
        self.assertIn("pins digest", result["error"])

    def test_manifest_signed_by_untrusted_key_fails_closed(self):
        other_key, other_pub = generate_signing_key(
            self.root / "otherkeys", name="other"
        )
        # A genuinely rogue signature over the identical manifest bytes,
        # produced at a separate path so the trusted .sig is untouched.
        rogue_manifest = self.root / "rogue" / self.manifest_path.name
        rogue_manifest.parent.mkdir(parents=True)
        rogue_manifest.write_bytes(self.manifest_path.read_bytes())
        rogue_sig = sign_manifest(rogue_manifest, other_key)
        self.assertNotEqual(
            rogue_sig.read_bytes(), self.sig_path.read_bytes()
        )
        rogue_digest = sha256_file(rogue_sig)
        hostctl_json(
            ["artifact-put", "--digest", rogue_digest], self.home,
            rogue_sig.read_bytes(),
        )
        result = self.deploy(
            op_id="deploy-rogue", signature_digest=rogue_digest,
            expect_rc=1,
        )
        self.assertIn("principal", result["error"])

    def test_activate_requires_staged_release(self):
        result = hostctl_json(
            ["activate", "--worker", "w1", "--op-id", "act-none",
             "--digest", "c" * 64],
            self.home, expect_rc=1,
        )
        self.assertIn("not a verified staged release", result["error"])


class TestActivateRollbackReceipts(SignedDeployCase):
    def setUp(self):
        super().setUp()
        # A second, different signed release for upgrade/rollback drills.
        self.artifact2, manifest2 = _stub_artifact(
            self.root / "v2",
            main_py=STUB_WORKER_MAIN + "# v2\n",
        )
        self.sig2 = sign_manifest(manifest2, self.key)
        self.digests2 = {}
        for label, path in (("artifact", self.artifact2),
                            ("manifest", manifest2),
                            ("signature", self.sig2)):
            digest = sha256_file(path)
            self.digests2[label] = digest
            hostctl_json(
                ["artifact-put", "--digest", digest], self.home,
                path.read_bytes(),
            )

    def _deploy_and_activate_both(self):
        self.deploy(op_id="d1")
        hostctl_json(
            ["activate", "--worker", "w1", "--op-id", "a1",
             "--digest", self.digests["artifact"]],
            self.home,
        )
        hostctl_json(
            ["deploy", "--worker", "w1", "--op-id", "d2",
             "--artifact-digest", self.digests2["artifact"],
             "--manifest-digest", self.digests2["manifest"],
             "--signature-digest", self.digests2["signature"],
             "--profile", "process"],
            self.home,
        )
        hostctl_json(
            ["activate", "--worker", "w1", "--op-id", "a2",
             "--digest", self.digests2["artifact"]],
            self.home,
        )

    def test_activate_swaps_and_previous_is_retained(self):
        self._deploy_and_activate_both()
        status = hostctl_json(
            ["worker-status", "--worker", "w1"], self.home
        )
        self.assertEqual(status["current"], self.digests2["artifact"])
        old_release = (self.home / "workers" / "w1" / "releases"
                       / f"{self.digests['artifact']}.pyz")
        self.assertTrue(old_release.is_file(), "previous artifact retained")

    def test_activate_is_idempotent(self):
        self.deploy(op_id="d1")
        first = hostctl_json(
            ["activate", "--worker", "w1", "--op-id", "a1",
             "--digest", self.digests["artifact"]], self.home,
        )
        self.assertFalse(first["noop"])
        dup = hostctl_json(
            ["activate", "--worker", "w1", "--op-id", "a1",
             "--digest", self.digests["artifact"]], self.home,
        )
        self.assertTrue(dup.get("duplicate"))
        noop = hostctl_json(
            ["activate", "--worker", "w1", "--op-id", "a1-retry",
             "--digest", self.digests["artifact"]], self.home,
        )
        self.assertTrue(noop["noop"])

    def test_rollback_restores_previous_revision(self):
        self._deploy_and_activate_both()
        receipt = hostctl_json(
            ["rollback", "--worker", "w1", "--op-id", "rb1"], self.home
        )
        self.assertEqual(receipt["digest"], self.digests["artifact"])
        self.assertEqual(
            receipt["rolled_back_from"], self.digests2["artifact"]
        )
        status = hostctl_json(
            ["worker-status", "--worker", "w1"], self.home
        )
        self.assertEqual(status["current"], self.digests["artifact"])
        dup = hostctl_json(
            ["rollback", "--worker", "w1", "--op-id", "rb1"], self.home
        )
        self.assertTrue(dup.get("duplicate"))
        self.assertEqual(dup["digest"], receipt["digest"])

    def test_rollback_without_history_refuses(self):
        result = hostctl_json(
            ["rollback", "--worker", "fresh", "--op-id", "rb-none"],
            self.home, expect_rc=1,
        )
        self.assertIn("no revision history", result["error"])

    def test_receipt_lookup(self):
        self.deploy(op_id="d-lookup")
        receipt = hostctl_json(
            ["receipt", "--op-id", "d-lookup"], self.home
        )
        self.assertEqual(receipt["op"], "deploy")

    def test_deploy_lock_serializes_operations(self):
        host = hostctl.Host(self.home)
        (self.home / "locks").mkdir(parents=True, exist_ok=True)
        with hostctl._DeployLock(host):
            result = self.deploy(op_id="d-locked", expect_rc=1)
        self.assertIn("lock", result["error"])
        # Lock released → the same deploy succeeds.
        after = self.deploy(op_id="d-after-lock")
        self.assertTrue(after["ok"])


class TestProcessProfileLifecycle(SignedDeployCase):
    """Real supervised subprocess under the process profile."""

    def setUp(self):
        super().setUp()
        self.deploy(op_id="d1")
        hostctl_json(
            ["activate", "--worker", "w1", "--op-id", "a1",
             "--digest", self.digests["artifact"]], self.home,
        )

    def _stop(self):
        hostctl_json(["worker-stop", "--worker", "w1"], self.home)

    def test_start_status_stop_roundtrip(self):
        started = hostctl_json(
            ["worker-start", "--worker", "w1", "--profile", "process",
             "--python", sys.executable],
            self.home,
        )
        self.addCleanup(self._stop)
        self.assertFalse(started["already_running"])
        self.assertGreater(started["pid"], 0)
        status = hostctl_json(["worker-status", "--worker", "w1"], self.home)
        self.assertTrue(status["running"])
        self.assertEqual(status["profile"], "process")
        again = hostctl_json(
            ["worker-start", "--worker", "w1", "--profile", "process",
             "--python", sys.executable],
            self.home,
        )
        self.assertTrue(again["already_running"])
        stopped = hostctl_json(["worker-stop", "--worker", "w1"], self.home)
        self.assertTrue(stopped["stopped"])
        deadline = time.time() + 5
        while time.time() < deadline:
            status = hostctl_json(
                ["worker-status", "--worker", "w1"], self.home
            )
            if not status["running"]:
                break
            time.sleep(0.1)
        self.assertFalse(status["running"])

    def test_stop_kills_the_whole_process_group(self):
        """A worker that ignores SIGTERM (and its children) still dies."""
        stubborn = (
            "import signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "child = subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(600)'])\n"
            "with open('child.pid', 'w') as fh:\n"
            "    fh.write(str(child.pid))\n"
            "while True:\n"
            "    time.sleep(0.2)\n"
        )
        artifact, manifest_path = _stub_artifact(
            self.root / "stubborn", main_py=stubborn
        )
        sig = sign_manifest(manifest_path, self.key)
        digests = {}
        for label, path in (("artifact", artifact),
                            ("manifest", manifest_path),
                            ("signature", sig)):
            digest = sha256_file(path)
            digests[label] = digest
            hostctl_json(["artifact-put", "--digest", digest], self.home,
                         path.read_bytes())
        hostctl_json(
            ["deploy", "--worker", "w2", "--op-id", "d-stubborn",
             "--artifact-digest", digests["artifact"],
             "--manifest-digest", digests["manifest"],
             "--signature-digest", digests["signature"],
             "--profile", "process"], self.home,
        )
        hostctl_json(
            ["activate", "--worker", "w2", "--op-id", "a-stubborn",
             "--digest", digests["artifact"]], self.home,
        )
        started = hostctl_json(
            ["worker-start", "--worker", "w2", "--profile", "process",
             "--python", sys.executable],
            self.home,
        )
        pid = started["pid"]
        child_pid_file = (self.home / "workers" / "w2" / "child.pid")
        deadline = time.time() + 10
        child_pid = 0
        while time.time() < deadline and not child_pid:
            try:
                child_pid = int(child_pid_file.read_text().strip())
            except (OSError, ValueError):
                time.sleep(0.1)
        self.assertGreater(child_pid, 0, "stub child never started")
        hostctl_json(["worker-stop", "--worker", "w2"], self.home)
        deadline = time.time() + 10
        while time.time() < deadline:
            if not hostctl._pid_alive(pid) and not hostctl._pid_alive(
                child_pid
            ):
                break
            time.sleep(0.1)
        self.assertFalse(hostctl._pid_alive(pid), "worker survived stop")
        self.assertFalse(
            hostctl._pid_alive(child_pid),
            "worker child escaped the process-group kill",
        )

    def test_rollback_drill_under_process_profile(self):
        """Deploy v2, activate (running worker restarts), roll back —
        the supervised process ends up running the previous revision."""
        hostctl_json(
            ["worker-start", "--worker", "w1", "--profile", "process",
             "--python", sys.executable], self.home,
        )
        self.addCleanup(self._stop)
        artifact2, manifest2 = _stub_artifact(
            self.root / "v2", main_py=STUB_WORKER_MAIN + "# v2\n"
        )
        sig2 = sign_manifest(manifest2, self.key)
        digests2 = {}
        for label, path in (("artifact", artifact2),
                            ("manifest", manifest2),
                            ("signature", sig2)):
            digest = sha256_file(path)
            digests2[label] = digest
            hostctl_json(["artifact-put", "--digest", digest], self.home,
                         path.read_bytes())
        hostctl_json(
            ["deploy", "--worker", "w1", "--op-id", "d2",
             "--artifact-digest", digests2["artifact"],
             "--manifest-digest", digests2["manifest"],
             "--signature-digest", digests2["signature"],
             "--profile", "process"], self.home,
        )
        upgraded = hostctl_json(
            ["activate", "--worker", "w1", "--op-id", "a2",
             "--digest", digests2["artifact"], "--python", sys.executable],
            self.home,
        )
        self.assertTrue(upgraded["restarted"])
        status = hostctl_json(["worker-status", "--worker", "w1"], self.home)
        self.assertTrue(status["running"])
        self.assertEqual(status["current"], digests2["artifact"])
        rolled = hostctl_json(
            ["rollback", "--worker", "w1", "--op-id", "rb1",
             "--python", sys.executable], self.home,
        )
        self.assertTrue(rolled["restarted"])
        status = hostctl_json(["worker-status", "--worker", "w1"], self.home)
        self.assertTrue(status["running"])
        self.assertEqual(status["current"], self.digests["artifact"])


class TestSystemdUnitText(SignedDeployCase):
    """Textual validation everywhere; when systemd-analyze exists on the
    test host the rendered unit is additionally verified for real
    (macOS lacks systemd-analyze, so that check skips cleanly there)."""

    def _unit_text(self):
        self.deploy(op_id="d1", profile="systemd")
        hostctl_json(
            ["activate", "--worker", "w1", "--op-id", "a1",
             "--digest", self.digests["artifact"]], self.home,
        )
        proc = run_hostctl(
            ["unit-text", "--worker", "w1", "--memory-max", "1G",
             "--cpu-quota", "50%", "--tasks-max", "128"],
            self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        return proc.stdout.decode()

    def test_unit_contains_every_required_hardening_directive(self):
        text = self._unit_text()
        for directive in (
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "PrivateTmp=yes",
            "ProtectHome=tmpfs",
            "ProtectKernelTunables=yes",
            "ProtectKernelModules=yes",
            "ProtectControlGroups=yes",
            "RestrictSUIDSGID=yes",
            "LockPersonality=yes",
            "SystemCallFilter=@system-service",
            "IPAddressDeny=any",
            "IPAddressAllow=localhost",
            "MemoryMax=1G",
            "CPUQuota=50%",
            "TasksMax=128",
            "LimitNOFILE=1024",
            "Restart=on-failure",
            "UMask=0077",
        ):
            self.assertIn(directive, text, f"missing {directive}")
        self.assertIn(f"BindPaths={self.home / 'workers' / 'w1'}", text)
        self.assertIn(self.digests["artifact"][:16], text)
        # Structure: exactly the three standard sections.
        self.assertIn("[Unit]", text)
        self.assertIn("[Service]", text)
        self.assertIn("[Install]", text)

    def test_unit_omits_system_scope_only_directives(self):
        """Field regression (2026-09-14): the unit is only ever installed
        user-scope, where capability manipulation always EPERMs
        (status=218/CAPABILITIES) — the directives must be absent, not
        empty — and ProtectHostname is ignored with a warning."""
        text = self._unit_text()
        for directive in (
            "CapabilityBoundingSet",
            "AmbientCapabilities",
            "ProtectHostname",
        ):
            self.assertNotRegex(
                text, rf"(?m)^\s*{directive}\s*=",
                f"{directive}= must not appear in a user-scope unit",
            )

    @unittest.skipUnless(
        shutil.which("systemd-analyze"),
        "systemd-analyze not on this host (e.g. macOS)",
    )
    def test_unit_passes_systemd_analyze_verify_user_scope(self):
        text = self._unit_text()
        unit_path = self.root / "conch-worker-w1.service"
        unit_path.write_text(text)
        proc = subprocess.run(
            ["systemd-analyze", "verify", "--user", str(unit_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"systemd-analyze verify --user failed:\n"
            f"{proc.stdout.decode()}\n{proc.stderr.decode()}",
        )

    def test_unit_text_refuses_without_activated_revision(self):
        proc = run_hostctl(["unit-text", "--worker", "ghost"], self.home)
        self.assertNotEqual(proc.returncode, 0)

    def test_systemd_start_refuses_on_hosts_without_systemctl(self):
        if shutil.which("systemctl"):
            self.skipTest("host has systemctl")
        self.deploy(op_id="d1", profile="systemd")
        hostctl_json(
            ["activate", "--worker", "w1", "--op-id", "a1",
             "--digest", self.digests["artifact"]], self.home,
        )
        result = hostctl_json(
            ["worker-start", "--worker", "w1", "--profile", "systemd"],
            self.home, expect_rc=1,
        )
        self.assertIn("systemctl", result["error"])
        self.assertIn("process profile", result["error"])


@unittest.skipUnless(HAVE_SSH_KEYGEN, "ssh-keygen not on PATH")
class TestTrustInstallValidation(HostctlCase):
    """trust-install fails closed on anything that is not a valid OpenSSH
    allowed-signers file, naming the offending line — a malformed anchor
    silently poisons every later deploy (field report 2026-09-14)."""

    def setUp(self):
        super().setUp()
        _, self.pub = generate_signing_key(self.root / "keys")
        self.good_line = allowed_signers_line("fleet@thom", self.pub)

    def test_valid_anchor_installs_with_entry_count(self):
        payload = (
            "# trust anchor for the conch fleet\n"
            "\n"
            + self.good_line
        ).encode()
        result = hostctl_json(
            ["trust-install", "--op-id", "t-valid"], self.home, payload
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 1)
        self.assertEqual(
            (self.home / "keys" / "allowed_signers").read_bytes(), payload
        )

    def test_build_summary_prefix_refuses_naming_the_bad_line(self):
        """The exact malformed file from the field report: five build
        summary lines redirected into the anchor before the signer line."""
        payload = (
            "artifact: dist/conch-worker.pyz\n"
            "sha256:   13b49c61bbd83259fcbae6bc3e4645d548362f1ea1aa2be9"
            "92e235adcb3f688c\n"
            "size:     1747825\n"
            "manifest: dist/conch-worker.pyz.manifest.json\n"
            "signature: dist/conch-worker.pyz.manifest.json.sig\n"
            + self.good_line
        ).encode()
        result = hostctl_json(
            ["trust-install", "--op-id", "t-summary"], self.home, payload,
            expect_rc=1,
        )
        self.assertFalse(result["ok"])
        self.assertIn("line 1", result["error"])
        self.assertIn("artifact: dist/conch-worker.pyz", result["error"])
        self.assertFalse((self.home / "keys" / "allowed_signers").exists())

    def test_garbage_key_material_refuses(self):
        principal, options, key_type, _ = self.good_line.split(None, 3)
        payload = (
            f"{principal} {options} {key_type} not!base64!!\n".encode()
        )
        result = hostctl_json(
            ["trust-install", "--op-id", "t-b64"], self.home, payload,
            expect_rc=1,
        )
        self.assertIn("base64", result["error"])
        self.assertFalse((self.home / "keys" / "allowed_signers").exists())

    def test_key_blob_type_mismatch_refuses(self):
        """A blob whose embedded type disagrees with the declared token
        is not the key it claims to be."""
        line = self.good_line.replace("ssh-ed25519", "ssh-rsa", 1)
        self.assertNotEqual(line, self.good_line)
        result = hostctl_json(
            ["trust-install", "--op-id", "t-type"], self.home,
            line.encode(), expect_rc=1,
        )
        self.assertIn("does not match declared type", result["error"])

    def test_comments_only_anchor_refuses(self):
        result = hostctl_json(
            ["trust-install", "--op-id", "t-empty"], self.home,
            b"# nothing here\n", expect_rc=1,
        )
        self.assertIn("no signer entries", result["error"])

    def test_documented_build_command_output_installs_verbatim(self):
        """README flow end to end: the build tool's stdout (redirected as
        `> allowed_signers`) must be exactly what trust-install accepts."""
        key_dir = self.root / "buildkeys"
        key, _ = generate_signing_key(key_dir)
        repo_root = Path(hostctl.__file__).resolve().parents[2]
        proc = subprocess.run(
            [sys.executable,
             str(repo_root / "tools" / "build_worker_artifact.py"),
             "--out", str(self.root / "dist" / "conch-worker.pyz"),
             "--packages", "conch",
             "--sign-key", str(key),
             "--principal", "fleet@you", "--emit-allowed-signers"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
            cwd=str(repo_root),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        result = hostctl_json(
            ["trust-install", "--op-id", "t-readme"], self.home,
            proc.stdout,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 1)


class TestRpcRelay(HostctlCase):
    def _serve_once(self, socket_path, reply: bytes, capture: dict):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)
        server.settimeout(10)

        def serve():
            try:
                conn, _ = server.accept()
                conn.settimeout(10)
                chunks = []
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if chunk.endswith(b"\n"):
                        break
                capture["request"] = b"".join(chunks)
                conn.sendall(reply)
                conn.close()
            finally:
                server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return thread

    def test_rpc_relays_one_bounded_line_each_way(self):
        run_dir = self.home / "workers" / "w1" / "run"
        run_dir.mkdir(parents=True)
        socket_path = run_dir / "supervisor.sock"
        capture = {}
        thread = self._serve_once(
            socket_path, b'{"ok": true, "result": 42}\n', capture
        )
        request = b'{"v": 1, "op": "worker.status", "args": {}}\n'
        proc = run_hostctl(["rpc", "--worker", "w1"], self.home, request)
        thread.join(timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(capture["request"], request)
        self.assertEqual(proc.stdout, b'{"ok": true, "result": 42}\n')

    def test_rpc_without_worker_socket_fails_cleanly(self):
        result = hostctl_json(
            ["rpc", "--worker", "ghost"], self.home,
            b'{"v": 1, "op": "x"}\n', expect_rc=1,
        )
        self.assertIn("socket", result["error"])

    def test_oversized_rpc_request_rejected(self):
        run_dir = self.home / "workers" / "w1" / "run"
        run_dir.mkdir(parents=True)
        (run_dir / "supervisor.sock").write_bytes(b"")  # placeholder file
        huge = b"x" * (hostctl.MAX_RPC_BYTES + 10) + b"\n"
        result = hostctl_json(
            ["rpc", "--worker", "w1"], self.home, huge, expect_rc=1,
        )
        self.assertIn("size bound", result["error"])


class TestHostctlIsStandalone(unittest.TestCase):
    def test_module_imports_only_stdlib(self):
        """The bootstrap contract: hostctl.py must run on a bare host with
        nothing but python3 — no conch imports, no third-party imports."""
        import ast

        source = Path(hostctl.__file__).read_text()
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    offenders.append(f"relative import: {ast.dump(node)}")
                    continue
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root in ("conch", "pygments"):
                    # `from conch import __version__` is allowed ONLY in
                    # the guarded version helper (ImportError-tolerant).
                    if "conch" == root and "_version_string" in source:
                        continue
                    offenders.append(name)
        self.assertEqual(offenders, [])

    def test_runs_as_a_bare_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            solo = Path(tmp) / "hostctl.py"
            solo.write_bytes(Path(hostctl.__file__).read_bytes())
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith("PYTHON")
            }
            env["CONCH_FLEET_HOME"] = str(Path(tmp) / "fleet")
            proc = subprocess.run(
                [sys.executable, str(solo), "probe"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=tmp, env=env, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            result = json.loads(proc.stdout.decode())
            self.assertTrue(result["ok"])


def _docker_image_id(tag):
    proc = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    return proc.stdout.decode().strip() if proc.returncode == 0 else ""


@unittest.skipUnless(
    os.environ.get("CONCH_FLEET_DOCKER_DRILL") == "1"
    and shutil.which("docker")
    and _docker_image_id("conch-worker:rollback-v1")
    and _docker_image_id("conch-worker:rollback-v2"),
    "docker drill opt-in (set CONCH_FLEET_DOCKER_DRILL=1 and build "
    "conch-worker:rollback-v1/v2)",
)
class TestDockerProfileRollbackDrill(HostctlCase):
    """Live rollback drill under the docker runtime profile (Docker
    Desktop). CI-skipped: opt in with CONCH_FLEET_DOCKER_DRILL=1 after
    building the two worker images. The process-profile drill in
    TestProcessProfileLifecycle always runs."""

    def _put_signed(self, key, main_py, worker="drillw"):
        from conch.fleet.artifacts import (
            build_worker_artifact,
            sign_manifest,
            write_manifest,
        )

        art = self.root / f"{main_py[:4]}.pyz"
        manifest = build_worker_artifact(art, packages=("conch",),
                                         main_py=main_py)
        manifest_path = write_manifest(art, manifest)
        sig = sign_manifest(manifest_path, key)
        digests = {}
        for label, path in (("artifact", art), ("manifest", manifest_path),
                            ("signature", sig)):
            digest = sha256_file(path)
            digests[label] = digest
            hostctl_json(["artifact-put", "--digest", digest], self.home,
                         path.read_bytes())
        return digests

    def test_docker_rollback_restores_previous_image(self):
        from conch.fleet.artifacts import (
            allowed_signers_line,
            generate_signing_key,
        )

        key, pub = generate_signing_key(self.root / "keys")
        hostctl_json(["trust-install", "--op-id", "t1"], self.home,
                     allowed_signers_line("drill@fleet", pub).encode())
        id1 = _docker_image_id("conch-worker:rollback-v1")
        id2 = _docker_image_id("conch-worker:rollback-v2")
        d1 = self._put_signed(key, "print('v1')\n")
        hostctl_json(["deploy", "--worker", "drillw", "--op-id", "d1",
                      "--artifact-digest", d1["artifact"],
                      "--manifest-digest", d1["manifest"],
                      "--signature-digest", d1["signature"],
                      "--profile", "docker", "--image-id", id1], self.home)
        hostctl_json(["activate", "--worker", "drillw", "--op-id", "a1",
                      "--digest", d1["artifact"]], self.home)
        started = hostctl_json(
            ["worker-start", "--worker", "drillw", "--profile", "docker"],
            self.home,
        )
        self.addCleanup(lambda: subprocess.run(
            ["docker", "rm", "-f", "conch-worker-drillw"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        self.assertEqual(started["image_id"], id1)
        time.sleep(3)
        status = hostctl_json(["worker-status", "--worker", "drillw"],
                              self.home)
        self.assertTrue(status["running"])
        d2 = self._put_signed(key, "print('v2')\n")
        hostctl_json(["deploy", "--worker", "drillw", "--op-id", "d2",
                      "--artifact-digest", d2["artifact"],
                      "--manifest-digest", d2["manifest"],
                      "--signature-digest", d2["signature"],
                      "--profile", "docker", "--image-id", id2], self.home)
        hostctl_json(["activate", "--worker", "drillw", "--op-id", "a2",
                      "--digest", d2["artifact"]], self.home)
        rb = hostctl_json(["rollback", "--worker", "drillw", "--op-id",
                           "rb1"], self.home)
        self.assertTrue(rb["restarted"])
        time.sleep(2)
        running = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}",
             "conch-worker-drillw"],
            stdout=subprocess.PIPE,
        ).stdout.decode().strip()
        self.assertEqual(running, id1, "rollback did not restore v1 image")


class TestHostctlInProcess(HostctlCase):
    """The console-script surface stays wired to the same module."""

    def test_entrypoint_dispatches_to_fleet_hostctl(self):
        from conch.entrypoints import hostctl_main

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = hostctl_main(["--home", str(self.home), "probe"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
