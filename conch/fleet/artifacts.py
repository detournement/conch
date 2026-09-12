"""Reproducible, signed single-file worker artifacts (Swarm Phase 2).

Every deployment is a signed single-file Conch build. This module produces
the ``.pyz`` (zipapp format: shebang + zip archive) bundling the ``conch``
package plus its one runtime dependency (``pygments``), a canonical-JSON
sha256 digest manifest, and OpenSSH ``sshsig`` signatures over the manifest
(``ssh-keygen -Y sign`` / ``-Y verify`` with an allowed-signers file).
OpenSSH is a guaranteed host prerequisite, so signing adds no Python
dependencies.

Verification is mandatory and fail-closed: a missing or invalid signature,
an unknown manifest format/version, or a digest/size mismatch raises
:class:`ArtifactError` and the artifact must not activate. The identical
rule applies to the optional OCI profile (digest-pinned images); this
module is the single specification of delivery/verification semantics.

Reproducibility: the archive is written with sorted entry names, a fixed
timestamp, fixed permissions, and a fixed compression level, so building
twice from the same source tree in the same environment yields
byte-identical artifacts and therefore identical digests.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import os
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ..swarm.protocol import PROTOCOL_VERSION, canonical_json

ARTIFACT_MANIFEST_FORMAT = "conch-fleet-manifest"
ARTIFACT_MANIFEST_VERSION = 1

#: sshsig namespace: signatures are only valid for this exact purpose.
SSHSIG_NAMESPACE = "conch-fleet-artifact"

#: Fixed zip entry timestamp (the zip epoch) for reproducible archives.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: The zipapp entry point: the official worker entrypoint. It stays gated
#: exactly like the installed console script.
_MAIN_PY = (
    "import sys\n"
    "from conch.entrypoints import worker_main\n"
    "sys.exit(worker_main())\n"
)

_SHEBANG = b"#!/usr/bin/env python3\n"

#: Source file suffixes bundled into the artifact. Compiled caches and
#: metadata never ship.
_BUNDLED_SUFFIXES = (".py", ".txt", ".cfg", ".json", ".md")
_EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


class ArtifactError(Exception):
    """Artifact build/signing/verification failed. Always fail closed."""


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _package_files(package: str) -> List[Tuple[str, Path]]:
    """(archive name, source path) pairs for one importable package."""
    try:
        module = importlib.import_module(package)
    except ImportError as exc:
        raise ArtifactError(
            f"cannot bundle package {package!r}: not importable in the"
            f" build environment ({exc})"
        )
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise ArtifactError(
            f"cannot bundle package {package!r}: no __file__ (builtin?)"
        )
    root = Path(module_file).resolve().parent
    entries: List[Tuple[str, Path]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix not in _BUNDLED_SUFFIXES:
            continue
        arcname = f"{package}/{path.relative_to(root).as_posix()}"
        entries.append((arcname, path))
    if not entries:
        raise ArtifactError(f"package {package!r} produced no files")
    return entries


def build_worker_artifact(
    out_path,
    packages: Iterable[str] = ("conch", "pygments"),
    main_py: str = _MAIN_PY,
) -> Dict[str, object]:
    """Build the reproducible worker ``.pyz``; return its manifest dict.

    The archive is zipapp format: a ``#!/usr/bin/env python3`` shebang
    followed by a zip whose ``__main__.py`` dispatches to the official
    ``conch-worker`` entrypoint.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    entries: List[Tuple[str, bytes]] = [
        ("__main__.py", main_py.encode("utf-8"))
    ]
    for package in packages:
        for arcname, source in _package_files(package):
            entries.append((arcname, source.read_bytes()))
    entries.sort(key=lambda pair: pair[0])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for arcname, payload in entries:
            info = zipfile.ZipInfo(arcname, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload, compresslevel=9)
    tmp_path = out_path.with_name(out_path.name + ".part")
    with open(tmp_path, "wb") as handle:
        handle.write(_SHEBANG)
        handle.write(buffer.getvalue())
    os.chmod(tmp_path, 0o755)
    os.replace(tmp_path, out_path)
    return build_manifest(out_path)


def build_manifest(artifact_path) -> Dict[str, object]:
    artifact_path = Path(artifact_path)
    if not artifact_path.is_file():
        raise ArtifactError(f"artifact {artifact_path} does not exist")
    from .. import __version__

    return {
        "format": ARTIFACT_MANIFEST_FORMAT,
        "manifest_version": ARTIFACT_MANIFEST_VERSION,
        "artifact": {
            "name": artifact_path.name,
            "sha256": sha256_file(artifact_path),
            "size": artifact_path.stat().st_size,
        },
        "conch_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
    }


def write_manifest(artifact_path, manifest: Optional[Dict[str, object]] = None,
                   out_path=None) -> Path:
    """Write the canonical-JSON manifest next to the artifact."""
    artifact_path = Path(artifact_path)
    manifest = manifest or build_manifest(artifact_path)
    out_path = Path(out_path) if out_path else (
        artifact_path.with_name(artifact_path.name + ".manifest.json")
    )
    tmp_path = out_path.with_name(out_path.name + ".part")
    tmp_path.write_text(canonical_json(manifest) + "\n", encoding="ascii")
    os.replace(tmp_path, out_path)
    return out_path


# ---------------------------------------------------------------------------
# sshsig signing / verification (OpenSSH ssh-keygen -Y)
# ---------------------------------------------------------------------------

def _run_ssh_keygen(argv: List[str], stdin_bytes: bytes = b"") -> str:
    try:
        proc = subprocess.run(
            argv, input=stdin_bytes, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, check=False,
        )
    except OSError as exc:
        raise ArtifactError(f"ssh-keygen unavailable: {exc}")
    except subprocess.SubprocessError as exc:
        raise ArtifactError(f"ssh-keygen failed: {exc}")
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise ArtifactError(
            f"ssh-keygen {argv[1]} failed (rc={proc.returncode}): {detail}"
        )
    return proc.stdout.decode("utf-8", "replace")


def sign_manifest(manifest_path, key_path) -> Path:
    """Sign the manifest file with ``ssh-keygen -Y sign``; returns the
    ``.sig`` path. The private key never leaves the OpenSSH process."""
    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        raise ArtifactError(f"manifest {manifest_path} does not exist")
    _run_ssh_keygen([
        "ssh-keygen", "-Y", "sign", "-f", str(key_path),
        "-n", SSHSIG_NAMESPACE, str(manifest_path),
    ])
    sig_path = manifest_path.with_name(manifest_path.name + ".sig")
    if not sig_path.is_file():
        raise ArtifactError("ssh-keygen reported success but wrote no .sig")
    return sig_path


def find_signature_principal(signature_path, allowed_signers_path) -> str:
    """The allowed-signers principal that produced this signature, or fail."""
    out = _run_ssh_keygen([
        "ssh-keygen", "-Y", "find-principals",
        "-s", str(signature_path), "-f", str(allowed_signers_path),
    ])
    principal = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not principal:
        raise ArtifactError(
            "signature matches no principal in the allowed-signers file"
        )
    return principal


def verify_manifest_signature(manifest_path, signature_path,
                              allowed_signers_path) -> str:
    """Verify the sshsig over the manifest bytes; returns the principal.

    Fail closed: missing files, unknown signer, wrong namespace, or any
    ssh-keygen failure raises :class:`ArtifactError`.
    """
    manifest_path = Path(manifest_path)
    signature_path = Path(signature_path)
    for path, label in ((manifest_path, "manifest"),
                        (signature_path, "signature"),
                        (Path(allowed_signers_path), "allowed-signers")):
        if not Path(path).is_file():
            raise ArtifactError(f"{label} file {path} does not exist")
    principal = find_signature_principal(signature_path, allowed_signers_path)
    _run_ssh_keygen(
        [
            "ssh-keygen", "-Y", "verify", "-f", str(allowed_signers_path),
            "-I", principal, "-n", SSHSIG_NAMESPACE,
            "-s", str(signature_path),
        ],
        stdin_bytes=manifest_path.read_bytes(),
    )
    return principal


def parse_manifest(manifest_path) -> Dict[str, object]:
    """Parse and validate a manifest. Unknown format/version fails closed."""
    import json

    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactError(f"unreadable manifest {manifest_path}: {exc}")
    if not isinstance(data, dict):
        raise ArtifactError("manifest is not a JSON object")
    if data.get("format") != ARTIFACT_MANIFEST_FORMAT:
        raise ArtifactError(
            f"unknown manifest format {data.get('format')!r} — failing closed"
        )
    if data.get("manifest_version") != ARTIFACT_MANIFEST_VERSION:
        raise ArtifactError(
            f"unsupported manifest version {data.get('manifest_version')!r}"
            f" (supported: {ARTIFACT_MANIFEST_VERSION}) — failing closed"
        )
    artifact = data.get("artifact")
    if not isinstance(artifact, dict):
        raise ArtifactError("manifest carries no artifact object")
    digest = artifact.get("sha256")
    size = artifact.get("size")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ArtifactError("manifest artifact.sha256 is malformed")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ArtifactError("manifest artifact.size is malformed")
    return data


def verify_artifact(artifact_path, manifest_path, signature_path,
                    allowed_signers_path) -> Dict[str, object]:
    """Full fail-closed verification: signature, manifest, digest, size.

    Returns the validated manifest. Any failure raises
    :class:`ArtifactError`; a failed artifact must never activate.
    """
    principal = verify_manifest_signature(
        manifest_path, signature_path, allowed_signers_path
    )
    manifest = parse_manifest(manifest_path)
    artifact_path = Path(artifact_path)
    if not artifact_path.is_file():
        raise ArtifactError(f"artifact {artifact_path} does not exist")
    artifact = manifest["artifact"]
    actual_size = artifact_path.stat().st_size
    if actual_size != artifact["size"]:
        raise ArtifactError(
            f"artifact size mismatch: manifest says {artifact['size']},"
            f" file is {actual_size} — refusing"
        )
    actual_digest = sha256_file(artifact_path)
    if actual_digest != artifact["sha256"]:
        raise ArtifactError(
            "artifact digest mismatch — the bytes are not the signed"
            f" build (expected {artifact['sha256']}, got {actual_digest})"
        )
    manifest["verified_principal"] = principal
    return manifest


# ---------------------------------------------------------------------------
# Key management helpers (dev/test convenience; production keys are the
# operator's responsibility)
# ---------------------------------------------------------------------------

def generate_signing_key(directory, name: str = "conch-fleet-signing",
                         comment: str = "conch-fleet") -> Tuple[Path, Path]:
    """Generate an ed25519 signing keypair; returns (private, public)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key_path = directory / name
    if key_path.exists():
        raise ArtifactError(f"refusing to overwrite existing key {key_path}")
    _run_ssh_keygen([
        "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment,
        "-f", str(key_path),
    ])
    return key_path, key_path.with_name(name + ".pub")


def allowed_signers_line(principal: str, public_key_path) -> str:
    """One allowed-signers line binding a principal to a public key,
    restricted to the artifact namespace."""
    pub = Path(public_key_path).read_text(encoding="utf-8").strip()
    parts = pub.split()
    if len(parts) < 2:
        raise ArtifactError("malformed public key file")
    key_material = " ".join(parts[:2])
    return (
        f"{principal} namespaces=\"{SSHSIG_NAMESPACE}\" {key_material}\n"
    )
