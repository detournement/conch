"""conch-hostctl: the on-host fleet control utility (Swarm Phase 2).

This module is deliberately **stdlib-only and self-contained**: the
enrollment bootstrap streams this single file to a bare trusted host and
runs it with the system ``python3`` before any conch install exists, so it
must never import the rest of conch (or anything third-party). The
installed ``conch-hostctl`` console script runs this exact module.

Responsibilities on the host:

- ``install-verify``   checksum-verified self-install (atomic rename)
- ``probe``            host capability report (arch, python, systemd,
                       docker, disk, cgroups, GPU, local model endpoints)
- ``artifact-put/get/has``  content-addressed blob store (atomic, idempotent)
- ``trust-install``    install the allowed-signers trust anchor
- ``deploy``           fail-closed sshsig+digest verification, stage release
- ``activate``         atomic current-revision swap (previous retained)
- ``rollback``         swap back to the previous revision
- ``worker-start/stop/status``  supervise the worker under a runtime
                       profile: hardened user-level systemd unit (Linux
                       default), plain supervised process (hosts without
                       systemd, e.g. macOS dev), or docker (strict
                       isolation, digest-pinned image)
- ``unit-text``        print the hardened systemd unit for review/install
- ``rpc``              relay one bounded JSON request line from stdin to
                       the worker supervisor socket and echo the response

Every mutating operation takes the host deployment lock and records an
idempotent operation receipt: repeating an operation with the same
``--op-id`` returns the recorded receipt without re-effecting; repeating a
deploy of already-staged content is a no-op receipt. Commands print exactly
one JSON object to stdout (the controller drives this over SSH stdio);
secret bytes never appear in arguments, outputs, receipts, or logs.
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

HOSTCTL_VERSION = 1

#: sshsig namespace — must match conch.fleet.artifacts.SSHSIG_NAMESPACE.
SSHSIG_NAMESPACE = "conch-fleet-artifact"

MANIFEST_FORMAT = "conch-fleet-manifest"
MANIFEST_VERSION = 1

#: Hard bound on one RPC line in either direction (matches the swarm
#: protocol wire bound).
MAX_RPC_BYTES = 1024 * 1024

#: Hard bound on artifact-put payloads.
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class HostctlError(Exception):
    """Fail closed: the operation must not proceed."""


# ---------------------------------------------------------------------------
# Paths / state layout
# ---------------------------------------------------------------------------

def fleet_home(override: str = "") -> Path:
    if override:
        return Path(override).expanduser()
    env = os.environ.get("CONCH_FLEET_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(state).expanduser() if state else Path.home() / ".local/state"
    return root / "conch-fleet"


def _ensure_dir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def _check_name(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not _NAME_RE.fullmatch(value):
        raise HostctlError(
            f"invalid {label} {value!r} (letters, digits, ., _, - only)"
        )
    return value


def _check_digest(value: str) -> str:
    value = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(value):
        raise HostctlError(f"invalid sha256 digest {value!r}")
    return value


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o600):
    _ensure_dir(path.parent)
    tmp = path.with_name(path.name + f".part-{os.getpid()}")
    with open(tmp, "wb") as handle:
        handle.write(payload)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, data, mode: int = 0o600):
    _atomic_write_bytes(
        path, (json.dumps(data, sort_keys=True) + "\n").encode("utf-8"), mode
    )


class Host:
    """One host's fleet state rooted at the fleet home directory."""

    def __init__(self, home: Path):
        self.home = Path(home)

    @property
    def store_dir(self) -> Path:
        return self.home / "store" / "sha256"

    @property
    def keys_dir(self) -> Path:
        return self.home / "keys"

    @property
    def allowed_signers(self) -> Path:
        return self.keys_dir / "allowed_signers"

    @property
    def receipts_dir(self) -> Path:
        return self.home / "receipts"

    @property
    def lock_path(self) -> Path:
        return self.home / "locks" / "deploy.lock"

    def worker_dir(self, worker: str) -> Path:
        return self.home / "workers" / _check_name(worker, "worker name")

    def worker_json(self, worker: str) -> Path:
        return self.worker_dir(worker) / "worker.json"

    def store_path(self, digest: str) -> Path:
        return self.store_dir / _check_digest(digest)

    # -- receipts (idempotent operations) ---------------------------------

    def receipt_path(self, op_id: str) -> Path:
        return self.receipts_dir / (
            _check_name(op_id, "op id") + ".json"
        )

    def load_receipt(self, op_id: str):
        path = self.receipt_path(op_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise HostctlError(f"corrupt receipt for op {op_id!r}")

    def record_receipt(self, op_id: str, receipt: dict) -> dict:
        receipt = dict(receipt)
        receipt.setdefault("op_id", op_id)
        receipt.setdefault("recorded_at", time.time())
        _atomic_write_json(self.receipt_path(op_id), receipt)
        return receipt

    # -- worker record -----------------------------------------------------

    def load_worker(self, worker: str) -> dict:
        path = self.worker_json(worker)
        if not path.is_file():
            return {
                "worker": worker, "profile": "", "current": "",
                "staged": "", "revisions": [], "config_digest": "",
                "python": "", "image_ids": {}, "updated_at": 0.0,
            }
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise HostctlError(f"corrupt worker record for {worker!r}")

    def save_worker(self, worker: str, record: dict):
        record = dict(record)
        record["worker"] = worker
        record["updated_at"] = time.time()
        _ensure_dir(self.worker_dir(worker))
        _atomic_write_json(self.worker_json(worker), record)


class _DeployLock:
    """The remote deployment lock: one mutating fleet operation at a time
    on this host. flock-based, so a crashed holder releases on exit."""

    def __init__(self, host: Host, timeout: float = 30.0):
        self.path = host.lock_path
        self.timeout = float(timeout)
        self._handle = None

    def __enter__(self):
        import fcntl

        _ensure_dir(self.path.parent)
        handle = open(self.path, "a+")
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    handle.close()
                    raise
                if time.time() >= deadline:
                    handle.close()
                    raise HostctlError(
                        "deployment lock is held by another operation —"
                        " refusing to proceed concurrently"
                    )
                time.sleep(0.1)
        self._handle = handle
        return self

    def __exit__(self, *exc):
        if self._handle is not None:
            import fcntl

            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._handle.close()
            self._handle = None


# ---------------------------------------------------------------------------
# sshsig verification (fail closed; mirrors conch.fleet.artifacts by design
# — this file must stand alone on a bare host)
# ---------------------------------------------------------------------------

def _run(argv, stdin_bytes: bytes = b"", timeout: float = 30.0):
    try:
        return subprocess.run(
            argv, input=stdin_bytes, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except OSError as exc:
        raise HostctlError(f"cannot run {argv[0]}: {exc}")
    except subprocess.SubprocessError as exc:
        raise HostctlError(f"{argv[0]} failed: {exc}")


def verify_signed_manifest(manifest_bytes: bytes, signature_path,
                           allowed_signers_path) -> dict:
    """Verify the sshsig then parse the manifest. Any failure is fatal."""
    signers = Path(allowed_signers_path)
    if not signers.is_file():
        raise HostctlError(
            f"no allowed-signers trust anchor at {signers} — install one"
            " with trust-install before deploying"
        )
    if not Path(signature_path).is_file():
        raise HostctlError(f"missing signature {signature_path}")
    found = _run([
        "ssh-keygen", "-Y", "find-principals", "-s", str(signature_path),
        "-f", str(signers),
    ])
    if found.returncode != 0:
        raise HostctlError(
            "signature matches no trusted principal — refusing: "
            + found.stderr.decode("utf-8", "replace").strip()
        )
    principal = found.stdout.decode("utf-8", "replace").strip().splitlines()
    principal = principal[0].strip() if principal else ""
    if not principal:
        raise HostctlError("signature matches no trusted principal")
    verified = _run(
        [
            "ssh-keygen", "-Y", "verify", "-f", str(signers),
            "-I", principal, "-n", SSHSIG_NAMESPACE,
            "-s", str(signature_path),
        ],
        stdin_bytes=manifest_bytes,
    )
    if verified.returncode != 0:
        raise HostctlError(
            "manifest signature verification failed — refusing: "
            + verified.stderr.decode("utf-8", "replace").strip()
        )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except ValueError as exc:
        raise HostctlError(f"signed manifest is not valid JSON: {exc}")
    if not isinstance(manifest, dict):
        raise HostctlError("signed manifest is not a JSON object")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise HostctlError(
            f"unknown manifest format {manifest.get('format')!r} — refusing"
        )
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise HostctlError(
            f"unsupported manifest version"
            f" {manifest.get('manifest_version')!r} — refusing"
        )
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict) or not isinstance(
        artifact.get("sha256"), str
    ):
        raise HostctlError("manifest carries no artifact digest — refusing")
    manifest["verified_principal"] = principal
    return manifest


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_install_verify(host: Host, args) -> dict:
    source = Path(args.source) if args.source else Path(
        os.path.abspath(__file__)
    )
    if not source.is_file():
        raise HostctlError(f"install source {source} does not exist")
    expected = _check_digest(args.digest)
    actual = sha256_file(source)
    if actual != expected:
        try:
            if args.source == "":
                # Never leave an unverified bootstrap lying around.
                source.unlink()
        except OSError:
            pass
        raise HostctlError(
            f"hostctl digest mismatch: expected {expected}, got {actual}"
            " — refusing install"
        )
    dest = Path(args.dest) if args.dest else host.home / "hostctl.py"
    _ensure_dir(dest.parent)
    if source.resolve() != dest.resolve():
        tmp = dest.with_name(dest.name + f".part-{os.getpid()}")
        shutil.copyfile(source, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
    else:
        os.chmod(dest, 0o755)
    for sub in ("store/sha256", "keys", "receipts", "locks", "workers"):
        _ensure_dir(host.home / sub)
    return {
        "ok": True, "op": "install-verify", "digest": expected,
        "installed": str(dest), "home": str(host.home),
        "hostctl_version": HOSTCTL_VERSION,
    }


def _login_user() -> str:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        try:
            import pwd

            user = pwd.getpwuid(os.getuid()).pw_name
        except (ImportError, KeyError, OSError):
            user = ""
    return user


def _linger_state() -> str:
    """loginctl linger for the current user: "yes" / "no" / "unknown".

    Without linger the user systemd manager is torn down when the last
    login session ends, taking every user-scope worker (systemd *and*
    process profile) with it — the field report's silent-death mode.
    """
    loginctl = shutil.which("loginctl")
    user = _login_user()
    if not loginctl or not user:
        return "unknown"
    try:
        out = _run(
            [loginctl, "show-user", user, "--property=Linger"], timeout=10
        )
    except HostctlError:
        return "unknown"
    if out.returncode != 0:
        return "unknown"
    value = out.stdout.decode("utf-8", "replace").strip()
    if value == "Linger=yes":
        return "yes"
    if value == "Linger=no":
        return "no"
    return "unknown"


def _probe_systemd() -> dict:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {"present": False}
    out = _run([systemctl, "--version"], timeout=10)
    version = 0
    features = []
    if out.returncode == 0:
        lines = out.stdout.decode("utf-8", "replace").splitlines()
        if lines:
            parts = lines[0].split()
            for part in parts:
                if part.isdigit():
                    version = int(part)
                    break
        if len(lines) > 1:
            features = [
                token for token in lines[1].split()
                if token.startswith(("+", "-"))
            ]
    sandboxing = version >= 232
    return {
        "present": True, "version": version, "features": features,
        "sandboxing": sandboxing,
        "user_manager": bool(os.environ.get("XDG_RUNTIME_DIR")),
        "linger": _linger_state(),
    }


def _probe_docker() -> dict:
    docker = shutil.which(os.environ.get("CONCH_FLEET_DOCKER", "docker"))
    if not docker:
        return {"present": False}
    out = _run(
        [docker, "version", "--format", "{{.Server.Version}}"], timeout=10
    )
    if out.returncode != 0:
        return {"present": True, "server": ""}
    return {
        "present": True,
        "server": out.stdout.decode("utf-8", "replace").strip(),
    }


def _probe_gpu() -> dict:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return {"present": False, "gpus": []}
    out = _run(
        [smi, "--query-gpu=name,memory.total", "--format=csv,noheader"],
        timeout=10,
    )
    if out.returncode != 0:
        return {"present": False, "gpus": []}
    gpus = [
        line.strip() for line in
        out.stdout.decode("utf-8", "replace").splitlines() if line.strip()
    ]
    return {"present": bool(gpus), "gpus": gpus}


def _probe_model_endpoints() -> list:
    import urllib.error
    import urllib.request

    endpoints = []
    probes = (
        ("ollama", "http://127.0.0.1:11434/api/tags"),
        ("llama.cpp", "http://127.0.0.1:8080/v1/models"),
    )
    for kind, url in probes:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                body = json.loads(response.read(65536).decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            continue
        models = []
        if isinstance(body, dict):
            for item in (body.get("models") or body.get("data") or []):
                if isinstance(item, dict):
                    name = item.get("name") or item.get("model") or (
                        item.get("id")
                    )
                    if name:
                        models.append(str(name))
        endpoints.append({
            "kind": kind, "url": url, "models": models[:16],
            "model_count": len(models),
        })
    return endpoints


def _probe_cgroups() -> str:
    if Path("/sys/fs/cgroup/cgroup.controllers").exists():
        return "v2"
    if Path("/sys/fs/cgroup/memory").exists():
        return "v1"
    return "none"


def cmd_probe(host: Host, args) -> dict:
    uname = os.uname()
    try:
        usage = shutil.disk_usage(str(host.home if host.home.exists()
                                      else Path.home()))
        disk = {"total": usage.total, "free": usage.free}
    except OSError:
        disk = {"total": 0, "free": 0}
    os_release = ""
    release_path = Path("/etc/os-release")
    if release_path.is_file():
        for line in release_path.read_text(errors="replace").splitlines():
            if line.startswith("PRETTY_NAME="):
                os_release = line.split("=", 1)[1].strip().strip('"')
                break
    elif sys.platform == "darwin":
        import platform

        os_release = f"macOS {platform.mac_ver()[0]}"
    return {
        "ok": True, "op": "probe",
        "hostctl_version": HOSTCTL_VERSION,
        "os": uname.sysname.lower(),
        "os_release": os_release,
        "arch": uname.machine,
        "hostname": uname.nodename,
        "python": {
            "executable": sys.executable,
            "version": ".".join(str(v) for v in sys.version_info[:3]),
        },
        "systemd": _probe_systemd(),
        "docker": _probe_docker(),
        "disk": disk,
        "cgroups": _probe_cgroups(),
        "gpu": _probe_gpu(),
        "model_endpoints": _probe_model_endpoints(),
        "profiles": _supported_profiles(),
        "home": str(host.home),
    }


def _supported_profiles() -> list:
    profiles = ["process"]
    if shutil.which("systemctl"):
        profiles.insert(0, "systemd")
    if shutil.which(os.environ.get("CONCH_FLEET_DOCKER", "docker")):
        profiles.append("docker")
    return profiles


def cmd_artifact_put(host: Host, args) -> dict:
    expected = _check_digest(args.digest)
    dest = host.store_path(expected)
    if dest.is_file() and sha256_file(dest) == expected:
        # Drain stdin so the SSH pipe closes cleanly, but change nothing.
        while sys.stdin.buffer.read(1024 * 1024):
            pass
        return {"ok": True, "op": "artifact-put", "digest": expected,
                "size": dest.stat().st_size, "duplicate": True}
    _ensure_dir(dest.parent)
    tmp = dest.with_name(dest.name + f".part-{os.getpid()}")
    hasher = hashlib.sha256()
    total = 0
    with open(tmp, "wb") as handle:
        while True:
            chunk = sys.stdin.buffer.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARTIFACT_BYTES:
                handle.close()
                tmp.unlink()
                raise HostctlError("artifact exceeds the size bound")
            hasher.update(chunk)
            handle.write(chunk)
    actual = hasher.hexdigest()
    if actual != expected:
        tmp.unlink()
        raise HostctlError(
            f"artifact digest mismatch: expected {expected}, got {actual}"
            " — nothing stored"
        )
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    return {"ok": True, "op": "artifact-put", "digest": expected,
            "size": total, "duplicate": False}


def cmd_artifact_get(host: Host, args) -> None:
    path = host.store_path(args.digest)
    if not path.is_file():
        raise HostctlError(f"artifact {args.digest} is not in the store")
    with open(path, "rb") as handle:
        shutil.copyfileobj(handle, sys.stdout.buffer)
    sys.stdout.buffer.flush()


def cmd_artifact_has(host: Host, args) -> dict:
    digest = _check_digest(args.digest)
    path = host.store_path(digest)
    present = path.is_file() and sha256_file(path) == digest
    return {"ok": True, "op": "artifact-has", "digest": digest,
            "present": present}


#: OpenSSH key type token in an allowed-signers entry (ssh-ed25519,
#: rsa-sha2-512, ecdsa-sha2-nistp256, sk-ssh-ed25519@openssh.com, ...).
_KEY_TYPE_RE = re.compile(r"^(?:sk-)?(?:ssh|ecdsa|rsa)-[A-Za-z0-9@.-]+$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def _bad_signer_line(number: int, line: str, why: str) -> HostctlError:
    snippet = line if len(line) <= 100 else line[:97] + "..."
    return HostctlError(
        f"allowed_signers line {number} is not a valid OpenSSH"
        f" allowed-signers entry ({why}): {snippet!r} — refusing the"
        " whole install"
    )


def _validate_signer_line(line: str, number: int) -> None:
    try:
        tokens = shlex.split(line, comments=False, posix=True)
    except ValueError as exc:
        raise _bad_signer_line(number, line, f"unbalanced quoting: {exc}")
    if len(tokens) < 3:
        raise _bad_signer_line(
            number, line,
            "expected principal, optional options, key type, and key"
        )
    key_index = 0
    for index, token in enumerate(tokens[1:], start=1):
        if _KEY_TYPE_RE.fullmatch(token):
            key_index = index
            break
        if token == "cert-authority" or "=" in token:
            continue  # an options token (namespaces=..., valid-after=...)
        raise _bad_signer_line(
            number, line, f"unexpected token {token!r} before the key type"
        )
    if not key_index:
        raise _bad_signer_line(number, line, "no OpenSSH key type found")
    if key_index + 1 >= len(tokens):
        raise _bad_signer_line(number, line, "missing base64 key material")
    key_type, key_b64 = tokens[key_index], tokens[key_index + 1]
    if not _BASE64_RE.fullmatch(key_b64) or len(key_b64) % 4:
        raise _bad_signer_line(
            number, line, "key material is not valid base64"
        )
    try:
        blob = base64.b64decode(key_b64, validate=True)
    except ValueError:
        raise _bad_signer_line(
            number, line, "key material is not valid base64"
        )
    # The wire blob embeds its own type (4-byte length + string); it must
    # agree with the declared type token.
    if len(blob) < 4:
        raise _bad_signer_line(number, line, "key blob is truncated")
    type_len = int.from_bytes(blob[:4], "big")
    embedded = blob[4:4 + type_len].decode("utf-8", "replace")
    if type_len <= 0 or embedded != key_type:
        raise _bad_signer_line(
            number, line,
            f"key blob type {embedded!r} does not match declared type"
            f" {key_type!r}"
        )


def validate_allowed_signers(payload: bytes) -> int:
    """Fail closed unless every non-comment, non-blank line parses as an
    OpenSSH allowed-signers entry (principal, optional options, key type,
    base64 key — ssh-keygen(1) ALLOWED SIGNERS). Returns the entry count.

    Field regression (2026-09-14): redirecting the artifact builder's
    stdout used to prepend build-summary lines to the trust anchor;
    installing such a file silently poisons every later deploy. The bad
    line is named so the operator can see exactly what leaked in.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostctlError(f"allowed_signers is not valid UTF-8: {exc}")
    entries = 0
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        _validate_signer_line(stripped, number)
        entries += 1
    if not entries:
        raise HostctlError(
            "allowed_signers carries no signer entries — refusing"
        )
    return entries


def cmd_trust_install(host: Host, args) -> dict:
    payload = sys.stdin.buffer.read(1024 * 1024)
    if not payload.strip():
        raise HostctlError("trust-install expects allowed-signers on stdin")
    entries = validate_allowed_signers(payload)
    with _DeployLock(host):
        if args.op_id:
            existing = host.load_receipt(args.op_id)
            if existing is not None:
                return dict(existing, duplicate=True)
        _atomic_write_bytes(host.allowed_signers, payload)
        receipt = {
            "ok": True, "op": "trust-install",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "path": str(host.allowed_signers),
            "entries": entries,
        }
        if args.op_id:
            receipt = host.record_receipt(args.op_id, receipt)
    return receipt


def cmd_image_load(host: Host, args) -> dict:
    digest = _check_digest(args.digest)
    path = host.store_path(digest)
    if not path.is_file() or sha256_file(path) != digest:
        raise HostctlError(
            f"image archive {digest} is not in the store (put it first)"
        )
    docker = os.environ.get("CONCH_FLEET_DOCKER", "docker")
    with _DeployLock(host):
        if args.op_id:
            existing = host.load_receipt(args.op_id)
            if existing is not None:
                return dict(existing, duplicate=True)
        out = _run([docker, "load", "-i", str(path)], timeout=300)
        if out.returncode != 0:
            raise HostctlError(
                "docker load failed: "
                + out.stderr.decode("utf-8", "replace").strip()
            )
        text = out.stdout.decode("utf-8", "replace")
        image_ref = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("Loaded image: "):
                image_ref = line.split(": ", 1)[1]
            elif line.startswith("Loaded image ID: "):
                image_ref = line.split(": ", 1)[1]
        image_id = ""
        if image_ref:
            inspect = _run(
                [docker, "image", "inspect", "--format", "{{.Id}}",
                 image_ref],
                timeout=30,
            )
            if inspect.returncode == 0:
                image_id = inspect.stdout.decode(
                    "utf-8", "replace"
                ).strip()
        receipt = {
            "ok": True, "op": "image-load", "digest": digest,
            "image_ref": image_ref, "image_id": image_id,
        }
        if args.op_id:
            receipt = host.record_receipt(args.op_id, receipt)
    return receipt


def cmd_deploy(host: Host, args) -> dict:
    artifact_digest = _check_digest(args.artifact_digest)
    manifest_digest = _check_digest(args.manifest_digest)
    signature_digest = _check_digest(args.signature_digest)
    worker = _check_name(args.worker, "worker name")
    profile = args.profile or "process"
    if profile not in ("systemd", "process", "docker"):
        raise HostctlError(f"unknown runtime profile {profile!r}")
    with _DeployLock(host):
        existing = host.load_receipt(args.op_id)
        if existing is not None:
            return dict(existing, duplicate=True)
        manifest_path = host.store_path(manifest_digest)
        signature_path = host.store_path(signature_digest)
        artifact_path = host.store_path(artifact_digest)
        for path, label in ((manifest_path, "manifest"),
                            (signature_path, "signature"),
                            (artifact_path, "artifact")):
            if not path.is_file():
                raise HostctlError(
                    f"{label} blob is not in the store — put it first"
                )
        signers = Path(args.allowed_signers) if args.allowed_signers else (
            host.allowed_signers
        )
        manifest_bytes = manifest_path.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != manifest_digest:
            raise HostctlError("manifest blob corrupt in store — refusing")
        manifest = verify_signed_manifest(
            manifest_bytes, signature_path, signers
        )
        pinned = manifest["artifact"]["sha256"]
        if pinned != artifact_digest:
            raise HostctlError(
                f"manifest pins digest {pinned}, deploy asked for"
                f" {artifact_digest} — refusing"
            )
        actual = sha256_file(artifact_path)
        if actual != artifact_digest:
            raise HostctlError(
                f"stored artifact digest mismatch ({actual}) — refusing"
            )
        expected_size = manifest["artifact"].get("size")
        if isinstance(expected_size, int) and not isinstance(
            expected_size, bool
        ):
            if artifact_path.stat().st_size != expected_size:
                raise HostctlError("stored artifact size mismatch — refusing")
        record = host.load_worker(worker)
        release_dir = _ensure_dir(host.worker_dir(worker) / "releases")
        release_path = release_dir / f"{artifact_digest}.pyz"
        already = (
            release_path.is_file()
            and sha256_file(release_path) == artifact_digest
        )
        if not already:
            tmp = release_path.with_name(
                release_path.name + f".part-{os.getpid()}"
            )
            shutil.copyfile(artifact_path, tmp)
            os.chmod(tmp, 0o755)
            os.replace(tmp, release_path)
        noop = already and record.get("staged") == artifact_digest
        record["staged"] = artifact_digest
        record["profile"] = profile
        if args.config_digest:
            record["config_digest"] = _check_digest(args.config_digest)
        if args.image_id:
            record.setdefault("image_ids", {})[artifact_digest] = (
                args.image_id
            )
        host.save_worker(worker, record)
        receipt = host.record_receipt(args.op_id, {
            "ok": True, "op": "deploy", "worker": worker,
            "digest": artifact_digest, "profile": profile,
            "verified_principal": manifest["verified_principal"],
            "staged": True, "noop": noop,
        })
    return receipt


def cmd_activate(host: Host, args) -> dict:
    worker = _check_name(args.worker, "worker name")
    digest = _check_digest(args.digest)
    with _DeployLock(host):
        existing = host.load_receipt(args.op_id)
        if existing is not None:
            return dict(existing, duplicate=True)
        record = host.load_worker(worker)
        release = host.worker_dir(worker) / "releases" / f"{digest}.pyz"
        if not release.is_file() or sha256_file(release) != digest:
            raise HostctlError(
                f"revision {digest} is not a verified staged release —"
                " deploy it first"
            )
        if record.get("current") == digest:
            receipt = host.record_receipt(args.op_id, {
                "ok": True, "op": "activate", "worker": worker,
                "digest": digest, "noop": True,
                "previous": record.get("current", ""),
            })
            return receipt
        previous = record.get("current", "")
        record["revisions"] = (record.get("revisions") or []) + [{
            "digest": digest, "activated_at": time.time(),
            "op_id": args.op_id, "previous": previous,
        }]
        record["current"] = digest
        host.save_worker(worker, record)
        restarted = False
        if _worker_running(host, worker, record):
            _stop_worker(host, worker, record)
            _start_worker(host, worker, record, args)
            restarted = True
        receipt = host.record_receipt(args.op_id, {
            "ok": True, "op": "activate", "worker": worker,
            "digest": digest, "previous": previous, "noop": False,
            "restarted": restarted,
        })
    return receipt


def cmd_rollback(host: Host, args) -> dict:
    worker = _check_name(args.worker, "worker name")
    with _DeployLock(host):
        existing = host.load_receipt(args.op_id)
        if existing is not None:
            return dict(existing, duplicate=True)
        record = host.load_worker(worker)
        revisions = record.get("revisions") or []
        if not revisions:
            raise HostctlError(f"worker {worker!r} has no revision history")
        current = record.get("current", "")
        previous = revisions[-1].get("previous", "")
        if not previous:
            raise HostctlError(
                f"worker {worker!r} has no previous revision to roll back to"
            )
        release = host.worker_dir(worker) / "releases" / f"{previous}.pyz"
        if not release.is_file() or sha256_file(release) != previous:
            raise HostctlError(
                f"previous revision {previous} is no longer verifiable —"
                " refusing rollback"
            )
        record["revisions"] = revisions + [{
            "digest": previous, "activated_at": time.time(),
            "op_id": args.op_id, "previous": current, "rollback": True,
        }]
        record["current"] = previous
        host.save_worker(worker, record)
        restarted = False
        if _worker_running(host, worker, record):
            _stop_worker(host, worker, record)
            _start_worker(host, worker, record, args)
            restarted = True
        receipt = host.record_receipt(args.op_id, {
            "ok": True, "op": "rollback", "worker": worker,
            "digest": previous, "rolled_back_from": current,
            "restarted": restarted,
        })
    return receipt


# ---------------------------------------------------------------------------
# Worker lifecycle per runtime profile
# ---------------------------------------------------------------------------

def _pidfile(host: Host, worker: str) -> Path:
    return host.worker_dir(worker) / "run" / "worker.pid"


def _container_name(worker: str) -> str:
    return f"conch-worker-{worker}"


def _unit_name(worker: str) -> str:
    return f"conch-worker-{worker}.service"


def _read_pid(host: Host, worker: str) -> int:
    path = _pidfile(host, worker)
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _worker_running(host: Host, worker: str, record: dict) -> bool:
    profile = record.get("profile") or "process"
    if profile == "process":
        return _pid_alive(_read_pid(host, worker))
    if profile == "systemd":
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return False
        out = _run(
            [systemctl, "--user", "is-active", _unit_name(worker)],
            timeout=10,
        )
        return out.stdout.decode("utf-8", "replace").strip() == "active"
    if profile == "docker":
        docker = os.environ.get("CONCH_FLEET_DOCKER", "docker")
        out = _run(
            [docker, "inspect", "--format", "{{.State.Running}}",
             _container_name(worker)],
            timeout=15,
        )
        return out.stdout.decode("utf-8", "replace").strip() == "true"
    return False


def _tail_lines(path: Path, count: int = 10) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(lines.splitlines()[-count:]).strip()


def _systemd_unit_state(systemctl: str, unit: str) -> "tuple":
    out = _run(
        [systemctl, "--user", "show", unit,
         "--property=ActiveState,SubState", "--value"],
        timeout=10,
    )
    lines = out.stdout.decode("utf-8", "replace").splitlines()
    active = lines[0].strip() if lines else ""
    sub = lines[1].strip() if len(lines) > 1 else ""
    return active, sub


def _unit_failure_detail(systemctl: str, unit: str) -> str:
    status = _run(
        [systemctl, "--user", "status", unit, "--no-pager", "-l"],
        timeout=10,
    )
    detail = status.stdout.decode("utf-8", "replace").strip()
    parts = [f"systemctl status: {detail}" if detail else
             "systemctl status: (no output)"]
    journalctl = shutil.which("journalctl")
    if journalctl:
        journal = _run(
            [journalctl, "--user", "-u", unit, "-n", "10", "--no-pager"],
            timeout=10,
        )
        text = journal.stdout.decode("utf-8", "replace").strip()
        if text:
            parts.append(f"last journal lines: {text}")
    return "; ".join(parts)


def _await_unit_running(systemctl: str, unit: str) -> None:
    """Block until the unit reaches active (running), fail closed if it
    enters failed or does not settle within the window.

    Field regression (2026-09-14): ``enable --now`` returns 0 even for a
    unit that immediately dies into a restart loop, so worker-start used
    to report ok for a broken deploy. The failure detail carries the
    systemctl status and the last journal lines so the caller can see
    *why* without a manual journalctl dig.
    """
    timeout = float(os.environ.get("CONCH_FLEET_START_TIMEOUT", "5.0"))
    deadline = time.monotonic() + timeout
    while True:
        active, sub = _systemd_unit_state(systemctl, unit)
        if active == "active" and sub == "running":
            return
        if active == "failed" or time.monotonic() >= deadline:
            raise HostctlError(
                f"unit {unit} did not reach active (running) — state is"
                f" {active or 'unknown'} ({sub or 'unknown'});"
                f" {_unit_failure_detail(systemctl, unit)}"
            )
        time.sleep(0.2)


def systemd_unit_text(host: Host, worker: str, record: dict, *,
                      python: str = "", memory_max: str = "2G",
                      cpu_quota: str = "100%", tasks_max: int = 256,
                      ip_allow: str = "localhost") -> str:
    """The hardened user-level systemd unit (Linux default profile).

    ProtectHome=tmpfs hides the rest of the home directory while BindPaths
    re-exposes only the worker directory (worker state lives under the
    unprivileged user's home; user units cannot write /var/lib). The
    network is denied by default except localhost so a worker can reach
    local model endpoints but nothing else; pass a wider allow-list only
    for workers that genuinely need one.

    The unit is only ever installed **user scope** (``systemctl --user``
    throughout this module), so every directive must be valid under an
    unprivileged user manager. Capability directives
    (CapabilityBoundingSet / AmbientCapabilities) are deliberately absent:
    a user manager cannot manipulate capabilities — spawning fails with
    ``status=218/CAPABILITIES`` — and an unprivileged process cannot gain
    capabilities in the first place, so they add no hardening here. If a
    system-scope unit is ever emitted, thread a ``scope`` parameter
    through and reinstate them for ``system`` only. ProtectHostname is
    likewise omitted: user managers ignore it with a warning on hosts
    that prohibit unprivileged UTS namespaces, and the kernel already
    denies hostname changes to unprivileged processes. The remaining
    sandbox directives (ProtectSystem/ProtectHome/PrivateTmp/Protect*,
    seccomp filters, IPAddress*) are all user-scope-valid on systemd with
    unprivileged user namespaces, as is the cgroup resource block on
    cgroups v2.
    """
    worker_dir = host.worker_dir(worker)
    current = record.get("current", "")
    if not current:
        raise HostctlError(
            f"worker {worker!r} has no activated revision — activate first"
        )
    release = worker_dir / "releases" / f"{current}.pyz"
    interpreter = python or record.get("python") or "python3"
    ip_lines = "IPAddressDeny=any\n"
    for item in [part.strip() for part in ip_allow.split(",") if part.strip()]:
        ip_lines += f"IPAddressAllow={item}\n"
    return f"""[Unit]
Description=Conch fleet worker {worker} (revision {current[:16]})
Documentation=https://github.com/detournement/conch
After=network.target

[Service]
Type=simple
ExecStart={interpreter} {release} --home {worker_dir}
WorkingDirectory={worker_dir}
Restart=on-failure
RestartSec=5
UMask=0077

# Hardening: the worker is replaceable compute with no business
# credentials; it may touch only its own directory and localhost.
# This is a user-scope unit: capability directives are deliberately
# absent (an unprivileged user manager cannot drop or grant
# capabilities — both EPERM at spawn — and an unprivileged process
# cannot gain them anyway), and ProtectHostname is omitted (ignored
# with a warning under user managers; the kernel already denies
# hostname changes to unprivileged processes).
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=tmpfs
BindPaths={worker_dir}
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectProc=invisible
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
{ip_lines}
# Resource caps: a runaway worker degrades, it does not take the host.
MemoryMax={memory_max}
CPUQuota={cpu_quota}
TasksMax={tasks_max}
LimitNOFILE=1024
LimitCORE=0

[Install]
WantedBy=default.target
"""


def _start_worker(host: Host, worker: str, record: dict, args) -> dict:
    profile = record.get("profile") or "process"
    current = record.get("current", "")
    if not current:
        raise HostctlError(
            f"worker {worker!r} has no activated revision — activate first"
        )
    worker_dir = host.worker_dir(worker)
    _ensure_dir(worker_dir / "run")
    _ensure_dir(worker_dir / "logs")
    _ensure_dir(worker_dir / "state")
    if profile == "process":
        release = worker_dir / "releases" / f"{current}.pyz"
        if not release.is_file():
            raise HostctlError(f"release {current} missing — deploy first")
        interpreter = (
            getattr(args, "python", "") or record.get("python") or "python3"
        )
        record["python"] = interpreter
        log_path = worker_dir / "logs" / "worker.log"
        with open(log_path, "ab") as log_handle:
            proc = subprocess.Popen(
                [interpreter, str(release), "--home", str(worker_dir)],
                stdin=subprocess.DEVNULL, stdout=log_handle,
                stderr=log_handle, start_new_session=True,
                cwd=str(worker_dir),
            )
        _atomic_write_bytes(
            _pidfile(host, worker), f"{proc.pid}\n".encode("ascii")
        )
        # A worker that dies at spawn (bad interpreter, broken release)
        # must not be reported as started.
        settle = float(os.environ.get("CONCH_FLEET_SPAWN_SETTLE", "1.0"))
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            status = proc.poll()
            if status is not None:
                tail = _tail_lines(log_path)
                raise HostctlError(
                    f"worker process exited immediately (rc={status})"
                    + (f" — last log lines: {tail}" if tail else "")
                )
            time.sleep(0.1)
        return {"profile": profile, "pid": proc.pid}
    if profile == "systemd":
        systemctl = shutil.which("systemctl")
        if not systemctl:
            raise HostctlError(
                "systemd profile selected but systemctl is not available"
                " on this host — use the process profile"
            )
        unit_dir = Path(
            os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        ) / "systemd" / "user"
        _ensure_dir(unit_dir, mode=0o755)
        unit_path = unit_dir / _unit_name(worker)
        _atomic_write_bytes(
            unit_path,
            systemd_unit_text(
                host, worker, record,
                python=getattr(args, "python", "") or "",
                memory_max=getattr(args, "memory_max", "2G"),
                cpu_quota=getattr(args, "cpu_quota", "100%"),
                tasks_max=int(getattr(args, "tasks_max", 256)),
                ip_allow=getattr(args, "ip_allow", "localhost"),
            ).encode("utf-8"),
            mode=0o644,
        )
        for argv in (
            [systemctl, "--user", "daemon-reload"],
            [systemctl, "--user", "enable", "--now", _unit_name(worker)],
        ):
            out = _run(argv, timeout=30)
            if out.returncode != 0:
                raise HostctlError(
                    f"{' '.join(argv[1:])} failed: "
                    + out.stderr.decode("utf-8", "replace").strip()
                )
        # enable --now returns 0 even for a unit dying into a restart
        # loop: confirm it actually reached active (running).
        _await_unit_running(systemctl, _unit_name(worker))
        return {"profile": profile, "unit": str(unit_path)}
    if profile == "docker":
        docker = os.environ.get("CONCH_FLEET_DOCKER", "docker")
        image_id = (record.get("image_ids") or {}).get(current, "")
        if not image_id:
            raise HostctlError(
                f"no docker image is recorded for revision {current} —"
                " image-load it and deploy with --image-id"
            )
        name = _container_name(worker)
        _run([docker, "rm", "-f", name], timeout=60)
        argv = [
            docker, "run", "-d", "--name", name,
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", str(int(getattr(args, "tasks_max", 256))),
            "--memory", str(getattr(args, "memory_max", "2G")).lower(),
            "--restart", "on-failure",
            "--tmpfs", "/tmp",
            "-v", f"{worker_dir / 'state'}:/worker",
            image_id,
            "--home", "/worker",
        ]
        out = _run(argv, timeout=120)
        if out.returncode != 0:
            raise HostctlError(
                "docker run failed: "
                + out.stderr.decode("utf-8", "replace").strip()
            )
        return {
            "profile": profile, "container": name, "image_id": image_id,
        }
    raise HostctlError(f"unknown runtime profile {profile!r}")


def _stop_worker(host: Host, worker: str, record: dict) -> dict:
    profile = record.get("profile") or "process"
    if profile == "process":
        pid = _read_pid(host, worker)
        if not _pid_alive(pid):
            return {"profile": profile, "stopped": False, "pid": pid}
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 10
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        if _pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            _pidfile(host, worker).unlink()
        except OSError:
            pass
        return {"profile": profile, "stopped": True, "pid": pid}
    if profile == "systemd":
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return {"profile": profile, "stopped": False}
        out = _run(
            [systemctl, "--user", "stop", _unit_name(worker)], timeout=30
        )
        return {"profile": profile, "stopped": out.returncode == 0}
    if profile == "docker":
        docker = os.environ.get("CONCH_FLEET_DOCKER", "docker")
        out = _run(
            [docker, "stop", "-t", "10", _container_name(worker)],
            timeout=60,
        )
        return {"profile": profile, "stopped": out.returncode == 0}
    raise HostctlError(f"unknown runtime profile {profile!r}")


def cmd_worker_start(host: Host, args) -> dict:
    worker = _check_name(args.worker, "worker name")
    record = host.load_worker(worker)
    if args.profile:
        record["profile"] = args.profile
    if _worker_running(host, worker, record):
        return {"ok": True, "op": "worker-start", "worker": worker,
                "already_running": True,
                "profile": record.get("profile") or "process"}
    profile = record.get("profile") or "process"
    linger = ""
    if profile in ("systemd", "process"):
        # Without linger, the user manager (and this worker) dies with
        # the last login session — the "keeps running when the terminal
        # closes" promise silently breaks (field report 2026-09-14).
        linger = _linger_state()
        if linger == "no" and not getattr(args, "force", False):
            user = _login_user() or "<user>"
            raise HostctlError(
                f"user linger is OFF (loginctl Linger=no): a user-scope"
                f" {profile} worker dies as soon as the login session"
                f" ends. Run `loginctl enable-linger {user}` on this host"
                " first (needs sudo or polkit authorization — do it in"
                " the interactive enrollment flow), or pass --force to"
                " start a worker that will not survive logout"
            )
    detail = _start_worker(host, worker, record, args)
    host.save_worker(worker, record)
    result = {"ok": True, "op": "worker-start", "worker": worker,
              "already_running": False}
    if linger:
        result["linger"] = linger
    if linger == "no":
        result["warning"] = (
            "linger is off — this worker dies at logout; enable with"
            f" `loginctl enable-linger {_login_user() or '<user>'}`"
        )
    return dict(result, **detail)


def cmd_worker_stop(host: Host, args) -> dict:
    worker = _check_name(args.worker, "worker name")
    record = host.load_worker(worker)
    detail = _stop_worker(host, worker, record)
    return dict({"ok": True, "op": "worker-stop", "worker": worker}, **detail)


def cmd_worker_status(host: Host, args) -> dict:
    worker = _check_name(args.worker, "worker name")
    record = host.load_worker(worker)
    running = _worker_running(host, worker, record)
    return {
        "ok": True, "op": "worker-status", "worker": worker,
        "running": running,
        "profile": record.get("profile") or "",
        "current": record.get("current", ""),
        "staged": record.get("staged", ""),
        "config_digest": record.get("config_digest", ""),
        "revisions": len(record.get("revisions") or []),
        "pid": _read_pid(host, worker),
    }


def cmd_unit_text(host: Host, args) -> None:
    worker = _check_name(args.worker, "worker name")
    record = host.load_worker(worker)
    sys.stdout.write(systemd_unit_text(
        host, worker, record,
        python=args.python or "",
        memory_max=args.memory_max, cpu_quota=args.cpu_quota,
        tasks_max=args.tasks_max, ip_allow=args.ip_allow,
    ))


def cmd_receipt(host: Host, args) -> dict:
    receipt = host.load_receipt(args.op_id)
    if receipt is None:
        raise HostctlError(f"no receipt recorded for op {args.op_id!r}")
    return receipt


# ---------------------------------------------------------------------------
# RPC relay: controller → (ssh) → hostctl rpc → worker supervisor socket
# ---------------------------------------------------------------------------

def _read_line_bounded(stream, bound: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = stream.read(1)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > bound:
            raise HostctlError("rpc request exceeds the size bound")
        if chunk == b"\n":
            break
    return b"".join(chunks)


def cmd_rpc(host: Host, args) -> None:
    worker = _check_name(args.worker, "worker name")
    request = _read_line_bounded(sys.stdin.buffer, MAX_RPC_BYTES)
    if not request.strip():
        raise HostctlError("rpc expects one JSON request line on stdin")
    record = host.load_worker(worker)
    if (record.get("profile") or "") == "docker":
        docker = os.environ.get("CONCH_FLEET_DOCKER", "docker")
        proc = _run(
            [docker, "exec", "-i", _container_name(worker),
             "conch-worker", "--home", "/worker", "--relay"],
            stdin_bytes=request, timeout=float(args.timeout),
        )
        if proc.returncode != 0:
            raise HostctlError(
                "docker rpc relay failed: "
                + proc.stderr.decode("utf-8", "replace").strip()
            )
        response = proc.stdout
    else:
        socket_path = host.worker_dir(worker) / "run" / "supervisor.sock"
        if not socket_path.exists():
            raise HostctlError(
                f"worker {worker!r} supervisor socket is not present —"
                " is the worker running?"
            )
        info = socket_path.lstat()
        if not stat.S_ISSOCK(info.st_mode):
            raise HostctlError("supervisor socket path is not a socket")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(float(args.timeout))
        try:
            client.connect(str(socket_path))
            client.sendall(request)
            chunks = []
            total = 0
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RPC_BYTES:
                    raise HostctlError(
                        "rpc response exceeds the size bound"
                    )
                if chunk.endswith(b"\n"):
                    break
        except socket.timeout:
            raise HostctlError("rpc timed out waiting for the worker")
        except OSError as exc:
            raise HostctlError(f"rpc transport failed: {exc}")
        finally:
            client.close()
        response = b"".join(chunks)
    if not response.strip():
        raise HostctlError("worker closed the connection without replying")
    sys.stdout.buffer.write(response)
    if not response.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _version_string() -> str:
    try:
        from conch import __version__

        return f"conch-hostctl {__version__} (hostctl {HOSTCTL_VERSION})"
    except ImportError:  # standalone single-file bootstrap on a bare host
        return f"conch-hostctl standalone (hostctl {HOSTCTL_VERSION})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conch-hostctl",
        description=(
            "Conch fleet host control: install/verify, capability probe,"
            " content-addressed artifact store, fail-closed signed deploys,"
            " activate/rollback with receipts, worker supervision under"
            " systemd/process/docker profiles, and the worker RPC relay."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=_version_string(),
    )
    parser.add_argument(
        "--home", default="", metavar="DIR",
        help="Fleet home directory (default: $CONCH_FLEET_HOME or"
             " ~/.local/state/conch-fleet).",
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("install-verify",
                       help="Verify this file's digest and install it.")
    p.add_argument("--digest", required=True)
    p.add_argument("--dest", default="")
    p.add_argument("--source", default="")

    sub.add_parser("probe", help="Report host capabilities as JSON.")

    p = sub.add_parser("artifact-put",
                       help="Store stdin bytes content-addressed by digest.")
    p.add_argument("--digest", required=True)

    p = sub.add_parser("artifact-get",
                       help="Write a stored blob to stdout.")
    p.add_argument("--digest", required=True)

    p = sub.add_parser("artifact-has", help="Check whether a blob is stored.")
    p.add_argument("--digest", required=True)

    p = sub.add_parser("trust-install",
                       help="Install the allowed-signers trust anchor.")
    p.add_argument("--op-id", default="")

    p = sub.add_parser("image-load",
                       help="docker load a stored image archive.")
    p.add_argument("--digest", required=True)
    p.add_argument("--op-id", default="")

    p = sub.add_parser("deploy",
                       help="Verify signature+digest and stage a release.")
    p.add_argument("--worker", required=True)
    p.add_argument("--op-id", required=True)
    p.add_argument("--artifact-digest", required=True)
    p.add_argument("--manifest-digest", required=True)
    p.add_argument("--signature-digest", required=True)
    p.add_argument("--profile", default="")
    p.add_argument("--config-digest", default="")
    p.add_argument("--image-id", default="")
    p.add_argument("--allowed-signers", default="")

    p = sub.add_parser("activate", help="Swap the current revision.")
    p.add_argument("--worker", required=True)
    p.add_argument("--op-id", required=True)
    p.add_argument("--digest", required=True)
    p.add_argument("--python", default="")
    p.add_argument("--memory-max", default="2G")
    p.add_argument("--cpu-quota", default="100%")
    p.add_argument("--tasks-max", type=int, default=256)
    p.add_argument("--ip-allow", default="localhost")

    p = sub.add_parser("rollback",
                       help="Swap back to the previous revision.")
    p.add_argument("--worker", required=True)
    p.add_argument("--op-id", required=True)
    p.add_argument("--python", default="")
    p.add_argument("--memory-max", default="2G")
    p.add_argument("--cpu-quota", default="100%")
    p.add_argument("--tasks-max", type=int, default=256)
    p.add_argument("--ip-allow", default="localhost")

    p = sub.add_parser("worker-start", help="Start the worker supervisor.")
    p.add_argument("--worker", required=True)
    p.add_argument("--profile", default="",
                   choices=["", "systemd", "process", "docker"])
    p.add_argument("--force", action="store_true",
                   help="Start even when loginctl linger is off (the"
                        " worker will die at logout).")
    p.add_argument("--python", default="")
    p.add_argument("--memory-max", default="2G")
    p.add_argument("--cpu-quota", default="100%")
    p.add_argument("--tasks-max", type=int, default=256)
    p.add_argument("--ip-allow", default="localhost")

    p = sub.add_parser("worker-stop", help="Stop the worker supervisor.")
    p.add_argument("--worker", required=True)

    p = sub.add_parser("worker-status", help="Report worker status JSON.")
    p.add_argument("--worker", required=True)

    p = sub.add_parser("unit-text",
                       help="Print the hardened systemd unit text.")
    p.add_argument("--worker", required=True)
    p.add_argument("--python", default="")
    p.add_argument("--memory-max", default="2G")
    p.add_argument("--cpu-quota", default="100%")
    p.add_argument("--tasks-max", type=int, default=256)
    p.add_argument("--ip-allow", default="localhost")

    p = sub.add_parser("receipt", help="Fetch a recorded operation receipt.")
    p.add_argument("--op-id", required=True)

    p = sub.add_parser("rpc",
                       help="Relay one JSON request line to the worker.")
    p.add_argument("--worker", required=True)
    p.add_argument("--timeout", type=float, default=30.0)

    return parser


_COMMANDS = {
    "install-verify": cmd_install_verify,
    "probe": cmd_probe,
    "artifact-put": cmd_artifact_put,
    "artifact-has": cmd_artifact_has,
    "trust-install": cmd_trust_install,
    "image-load": cmd_image_load,
    "deploy": cmd_deploy,
    "activate": cmd_activate,
    "rollback": cmd_rollback,
    "worker-start": cmd_worker_start,
    "worker-stop": cmd_worker_stop,
    "worker-status": cmd_worker_status,
    "receipt": cmd_receipt,
}

_RAW_COMMANDS = {
    "artifact-get": cmd_artifact_get,
    "unit-text": cmd_unit_text,
    "rpc": cmd_rpc,
}


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 2
    host = Host(fleet_home(args.home))
    try:
        if args.command in _RAW_COMMANDS:
            _RAW_COMMANDS[args.command](host, args)
            return 0
        result = _COMMANDS[args.command](host, args)
        print(json.dumps(result, sort_keys=True))
        return 0
    except HostctlError as exc:
        print(json.dumps({
            "ok": False, "error": str(exc), "op": args.command,
        }, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
