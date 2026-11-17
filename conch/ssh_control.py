"""Validated OpenSSH ControlMaster lifecycle and command construction."""

from __future__ import annotations

import atexit
import hashlib
import ipaddress
import os
import re
import secrets
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

_USER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HOST_CHARS_RE = re.compile(r"[A-Za-z0-9._-]+")
_UNSAFE_CREDENTIAL_COMMANDS = (
    re.compile(r"(^|[\s;|&])sshpass([\s;|&]|$)", re.IGNORECASE),
    re.compile(
        r"(^|[\s;|&])sudo(?:\s+\S+)*\s+"
        r"(?:-[^-\s]*[AS]\S*|--(?:stdin|askpass)(?:=\S*)?)(?:\s|$)",
    ),
    re.compile(
        r"(^|[\s;|&])(SSH_ASKPASS|SUDO_ASKPASS|SSHPASS)\s*=",
        re.IGNORECASE,
    ),
    re.compile(
        r"(^|\s)--(?:password|passphrase)(?:=\S*|\s|$)",
        re.IGNORECASE,
    ),
)


class SSHValidationError(ValueError):
    pass


def validate_ssh_user(user: str) -> str:
    value = str(user or "").strip()
    if not value:
        return ""
    if not _USER_RE.fullmatch(value) or value.startswith("-"):
        raise SSHValidationError(
            "invalid SSH user (use letters, digits, '.', '_' or '-')"
        )
    return value


def validate_ssh_host(host: str) -> str:
    value = str(host or "").strip()
    if not value or len(value) > 253:
        raise SSHValidationError("invalid SSH host")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not _HOST_CHARS_RE.fullmatch(value) or value.startswith(("-", ".")):
        raise SSHValidationError(
            "invalid SSH host or alias (options and whitespace are not allowed)"
        )
    if value.endswith("."):
        labels = value[:-1].split(".")
    else:
        labels = value.split(".")
    if not labels or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        for label in labels
    ):
        raise SSHValidationError("invalid SSH host or alias")
    return value


def validate_ssh_port(port) -> int | None:
    if port in (None, ""):
        return None
    if isinstance(port, bool):
        raise SSHValidationError("invalid SSH port")
    try:
        value = int(port)
    except (TypeError, ValueError):
        raise SSHValidationError("invalid SSH port")
    if value < 1 or value > 65535:
        raise SSHValidationError("SSH port must be between 1 and 65535")
    return value


def validate_remote_command(command: str, *, allow_empty: bool = False) -> str:
    value = str(command or "")
    if "\x00" in value or len(value) > 131072:
        raise SSHValidationError("invalid command")
    if not value.strip() and not allow_empty:
        raise SSHValidationError("empty command")
    if any(pattern.search(value) for pattern in _UNSAFE_CREDENTIAL_COMMANDS):
        raise SSHValidationError(
            "refused insecure credential forwarding; use the interactive "
            "terminal handoff and enter credentials only at the program prompt"
        )
    return value


def parse_ssh_target(value: str, port=None) -> SSHTarget:
    target = str(value or "").strip()
    if target.count("@") > 1:
        raise SSHValidationError("invalid SSH target")
    if "@" in target:
        user, host = target.split("@", 1)
    else:
        user, host = "", target
    return SSHTarget(
        host=validate_ssh_host(host),
        user=validate_ssh_user(user),
        port=validate_ssh_port(port),
    )


def merge_ssh_target(host: str, user: str = "", port=None) -> SSHTarget:
    """Build a target accepting ``user@host`` in the host field."""

    parsed = parse_ssh_target(host, port)
    user = validate_ssh_user(user)
    if parsed.user and user and parsed.user != user:
        raise SSHValidationError(
            f"conflicting SSH users: '{parsed.user}' in host "
            f"and '{user}' in user"
        )
    return SSHTarget(
        host=parsed.host, user=parsed.user or user, port=parsed.port
    )


@dataclass(frozen=True)
class SSHTarget:
    host: str
    user: str = ""
    port: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "host", validate_ssh_host(self.host))
        object.__setattr__(self, "user", validate_ssh_user(self.user))
        object.__setattr__(self, "port", validate_ssh_port(self.port))

    @property
    def identity(self) -> str:
        user = f"{self.user}@" if self.user else ""
        port = f":{self.port}" if self.port is not None else ""
        return f"{user}{self.host}{port}"


def default_ssh_runtime_dir() -> Path:
    runtime = str(os.environ.get("XDG_RUNTIME_DIR", "") or "").strip()
    if runtime:
        return Path(runtime).expanduser() / "conch" / "ssh"
    state = Path(
        os.environ.get(
            "XDG_STATE_HOME", str(Path.home() / ".local" / "state")
        )
    ).expanduser()
    return state / "conch" / "runtime" / "ssh"


class SSHControlManager:
    """Own only the ControlMaster sockets created during this process."""

    def __init__(
        self,
        runtime_dir: Path | None = None,
        persist_seconds: int = 600,
    ):
        try:
            persist = int(persist_seconds)
        except (TypeError, ValueError):
            persist = 600
        self.persist_seconds = max(1, min(persist, 86400))
        self.runtime_dir = Path(runtime_dir or default_ssh_runtime_dir())
        self._instance_id = secrets.token_hex(6)
        self._control_paths: dict[str, Path] = {}
        self._owned_targets: dict[str, SSHTarget] = {}
        self._targets: dict[str, SSHTarget] = {}
        self._lost_targets: dict[str, SSHTarget] = {}
        self._active_identity = ""
        self._closed = False
        atexit.register(self.cleanup_all)

    def _ensure_runtime_dir(self) -> Path:
        if self.runtime_dir.is_symlink():
            raise OSError("SSH runtime directory must not be a symlink")
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.runtime_dir.is_symlink():
            raise OSError("SSH runtime directory must not be a symlink")
        info = self.runtime_dir.stat()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise OSError("SSH runtime directory is not owned by this user")
        os.chmod(self.runtime_dir, 0o700)
        return self.runtime_dir

    def control_path(self, target: SSHTarget) -> Path:
        directory = self._ensure_runtime_dir()
        existing = self._control_paths.get(target.identity)
        if existing is not None:
            return existing
        digest = hashlib.sha256(target.identity.encode("utf-8")).hexdigest()[:20]
        path = directory / f"cm-{self._instance_id}-{digest}"
        self._control_paths[target.identity] = path
        return path

    def _reserve_control_path(self, target: SSHTarget) -> Path:
        path = self.control_path(target)
        if target.identity in self._owned_targets:
            return path
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise OSError(f"cannot inspect SSH ControlPath: {exc}") from exc
        else:
            raise OSError(
                "refusing to replace an existing SSH ControlPath"
            )
        self._owned_targets[target.identity] = target
        return path

    @staticmethod
    def _target_options(target: SSHTarget) -> list[str]:
        options: list[str] = []
        if target.port is not None:
            options.extend(["-p", str(target.port)])
        if target.user:
            options.extend(["-l", target.user])
        return options

    def connect_argv(self, target: SSHTarget) -> list[str]:
        control_path = self._reserve_control_path(target)
        return [
            "ssh",
            "-M",
            "-S",
            str(control_path),
            "-o",
            "ControlMaster=yes",
            "-o",
            f"ControlPersist={self.persist_seconds}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-N",
            "-f",
            *self._target_options(target),
            "--",
            target.host,
        ]

    def check_argv(self, target: SSHTarget) -> list[str]:
        return [
            "ssh",
            "-S",
            str(self.control_path(target)),
            "-O",
            "check",
            "-o",
            "BatchMode=yes",
            *self._target_options(target),
            "--",
            target.host,
        ]

    def exec_argv(
        self, target: SSHTarget, command: str, *, tty: bool = False
    ) -> list[str]:
        command = validate_remote_command(command, allow_empty=tty)
        argv = [
            "ssh",
            "-S",
            str(self.control_path(target)),
            "-o",
            "ControlMaster=no",
        ]
        if tty:
            argv.append("-tt")
        else:
            argv.extend(["-T", "-o", "BatchMode=yes"])
        argv.extend(self._target_options(target))
        argv.extend(["--", target.host])
        if command:
            argv.append(command)
        return argv

    def disconnect_argv(self, target: SSHTarget) -> list[str]:
        return [
            "ssh",
            "-S",
            str(self.control_path(target)),
            "-O",
            "exit",
            "-o",
            "BatchMode=yes",
            *self._target_options(target),
            "--",
            target.host,
        ]

    def remember(self, target: SSHTarget) -> None:
        self._targets[target.identity] = target
        self._lost_targets.pop(target.identity, None)
        self._active_identity = target.identity

    def resolve(
        self,
        *,
        host: str = "",
        user: str = "",
        port=None,
    ) -> SSHTarget:
        if host:
            target = merge_ssh_target(host, user, port)
            if target.user:
                return target
            return self._resolve_host_only(target)
        if user or port not in (None, ""):
            raise SSHValidationError("host is required with user or port")
        target = self._targets.get(self._active_identity)
        if target is None:
            raise SSHValidationError(
                "no active SSH connection; connect with user and host first"
            )
        return target

    def _resolve_host_only(self, wanted: SSHTarget) -> SSHTarget:
        """Match a host-only request to the unique live connection there.

        Connections register under their full identity (``user@host[:port]``),
        so a bare host must not be keyed literally: probe the registered
        targets for this host and adopt the single live one regardless of
        user. Two live users on one host is ambiguous and must be spelled out.
        """

        candidates: dict[str, SSHTarget] = {}
        for registry in (self._owned_targets, self._targets):
            for target in registry.values():
                if target.host != wanted.host:
                    continue
                if wanted.port is not None and target.port != wanted.port:
                    continue
                candidates[target.identity] = target
        active = [
            target
            for target in candidates.values()
            if self.is_connected(target)
        ]
        if len(active) == 1:
            return active[0]
        if len(active) > 1:
            identities = ", ".join(
                sorted(target.identity for target in active)
            )
            raise SSHValidationError(
                f"multiple active SSH connections match host "
                f"{wanted.identity}: {identities}; specify the user"
            )
        if not candidates:
            # Nothing registered any more; a recently lost master for this
            # host still resolves so callers keep reporting the loss
            # honestly instead of inventing a never-connected identity.
            for target in self._lost_targets.values():
                if target.host == wanted.host and (
                    wanted.port is None or target.port == wanted.port
                ):
                    candidates[target.identity] = target
        if len(candidates) == 1:
            return next(iter(candidates.values()))
        return wanted

    def _socket_is_owned(self, path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError:
            return False
        if not stat.S_ISSOCK(info.st_mode):
            return False
        return not hasattr(os, "getuid") or info.st_uid == os.getuid()

    def is_known(self, target: SSHTarget) -> bool:
        """Was this exact identity registered (owned or remembered)?"""

        return (
            target.identity in self._owned_targets
            or target.identity in self._targets
        )

    def was_lost(self, target: SSHTarget) -> bool:
        """Did a previously active master for this identity stop answering?"""

        return target.identity in self._lost_targets

    def _purge_stale(self, target: SSHTarget) -> None:
        """Forget a dead master so a reconnect can reserve a fresh path."""

        self._lost_targets[target.identity] = target
        self._remove_owned_socket(target)
        self._owned_targets.pop(target.identity, None)
        self._control_paths.pop(target.identity, None)
        self._targets.pop(target.identity, None)
        if self._active_identity == target.identity:
            self._active_identity = next(iter(self._targets), "")

    def is_connected(self, target: SSHTarget) -> bool:
        """True only when the control socket answers a real ``-O check``.

        A dead or missing master is purged from the registry so its state is
        never reported as active again and a reconnect starts clean.
        """

        if target.identity not in self._owned_targets:
            return False
        path = self.control_path(target)
        if not self._socket_is_owned(path):
            self._purge_stale(target)
            return False
        try:
            proc = subprocess.run(
                self.check_argv(target),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # Transient local failure: stay honest ("not connected") but do
            # not destroy state that may still be alive.
            return False
        connected = proc.returncode == 0
        if connected:
            self.remember(target)
        else:
            self._purge_stale(target)
        return connected

    def connected_targets(self) -> list[SSHTarget]:
        connected = []
        for target in list(self._targets.values()):
            if self.is_connected(target):
                connected.append(target)
        return connected

    def _remove_owned_socket(self, target: SSHTarget) -> None:
        if target.identity not in self._owned_targets:
            return
        path = self.control_path(target)
        if self._socket_is_owned(path):
            try:
                path.unlink()
            except OSError:
                pass

    def disconnect(self, target: SSHTarget, timeout: int = 5) -> bool:
        owned = target.identity in self._owned_targets
        socket_exists = owned and self._socket_is_owned(
            self.control_path(target)
        )
        success = False
        if socket_exists:
            try:
                proc = subprocess.run(
                    self.disconnect_argv(target),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=max(1, int(timeout)),
                    check=False,
                )
                success = proc.returncode == 0
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        self._remove_owned_socket(target)
        self._owned_targets.pop(target.identity, None)
        self._control_paths.pop(target.identity, None)
        self._targets.pop(target.identity, None)
        self._lost_targets.pop(target.identity, None)
        if self._active_identity == target.identity:
            self._active_identity = next(iter(self._targets), "")
        return success

    def cleanup_all(self) -> None:
        if self._closed:
            return
        self._closed = True
        for target in list(self._owned_targets.values()):
            self.disconnect(target, timeout=3)

    def close(self) -> None:
        self.cleanup_all()
        atexit.unregister(self.cleanup_all)
