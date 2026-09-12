"""Signed single-file worker artifacts (Swarm Phase 2 gate).

Non-negotiable invariant under test: no artifact activates without digest
pinning and signature verification, and the rule is identical across
runtime profiles. Unsigned or tampered artifacts fail closed — these tests
flip real bytes and re-verify.
"""

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from conch.fleet.artifacts import (
    ArtifactError,
    allowed_signers_line,
    build_worker_artifact,
    generate_signing_key,
    parse_manifest,
    sha256_file,
    sign_manifest,
    verify_artifact,
    write_manifest,
)
from conch.swarm.protocol import canonical_json

HAVE_PYGMENTS = importlib.util.find_spec("pygments") is not None
HAVE_SSH_KEYGEN = shutil.which("ssh-keygen") is not None

ROOT = Path(__file__).resolve().parents[1]


def _build_packages():
    return ("conch", "pygments") if HAVE_PYGMENTS else ("conch",)


class ArtifactCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


class TestReproducibleBuild(ArtifactCase):
    def test_two_builds_are_byte_identical(self):
        a = self.root / "a.pyz"
        b = self.root / "b.pyz"
        manifest_a = build_worker_artifact(a, packages=_build_packages())
        manifest_b = build_worker_artifact(b, packages=_build_packages())
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(
            manifest_a["artifact"]["sha256"], manifest_b["artifact"]["sha256"]
        )

    def test_artifact_is_a_zipapp_bundling_conch(self):
        out = self.root / "conch-worker.pyz"
        build_worker_artifact(out, packages=_build_packages())
        import zipapp

        self.assertEqual(zipapp.get_interpreter(out), "/usr/bin/env python3")
        with zipfile.ZipFile(out) as archive:
            names = set(archive.namelist())
        self.assertIn("__main__.py", names)
        self.assertIn("conch/__init__.py", names)
        self.assertIn("conch/fleet/artifacts.py", names)
        if HAVE_PYGMENTS:
            self.assertIn("pygments/__init__.py", names)
        self.assertFalse(any("__pycache__" in name for name in names))
        self.assertFalse(any(name.endswith(".pyc") for name in names))
        mode = out.stat().st_mode
        self.assertTrue(mode & 0o111, "artifact must be executable")

    def test_artifact_entry_runs_the_worker_entrypoint(self):
        out = self.root / "conch-worker.pyz"
        build_worker_artifact(out, packages=_build_packages())
        proc = subprocess.run(
            [sys.executable, str(out), "--version"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn(b"conch-worker", proc.stdout)

    def test_manifest_matches_the_bytes_and_is_canonical(self):
        out = self.root / "w.pyz"
        manifest = build_worker_artifact(out, packages=_build_packages())
        self.assertEqual(manifest["artifact"]["sha256"], sha256_file(out))
        self.assertEqual(manifest["artifact"]["size"], out.stat().st_size)
        path = write_manifest(out, manifest)
        text = path.read_text()
        self.assertEqual(text, canonical_json(manifest) + "\n")
        parsed = parse_manifest(path)
        self.assertEqual(parsed["artifact"], manifest["artifact"])

    def test_unimportable_package_fails_closed(self):
        with self.assertRaises(ArtifactError):
            build_worker_artifact(
                self.root / "x.pyz",
                packages=("definitely_not_a_real_package_xyz",),
            )


class TestManifestValidation(ArtifactCase):
    def _manifest_file(self, mutate=None):
        out = self.root / "w.pyz"
        manifest = build_worker_artifact(out, packages=("conch",))
        if mutate:
            mutate(manifest)
        path = self.root / "m.json"
        path.write_text(canonical_json(manifest) + "\n")
        return out, path

    def test_unknown_format_fails_closed(self):
        _, path = self._manifest_file(
            lambda m: m.__setitem__("format", "evil-format")
        )
        with self.assertRaises(ArtifactError):
            parse_manifest(path)

    def test_newer_manifest_version_fails_closed(self):
        _, path = self._manifest_file(
            lambda m: m.__setitem__("manifest_version", 99)
        )
        with self.assertRaises(ArtifactError):
            parse_manifest(path)

    def test_malformed_digest_fails_closed(self):
        _, path = self._manifest_file(
            lambda m: m["artifact"].__setitem__("sha256", "short")
        )
        with self.assertRaises(ArtifactError):
            parse_manifest(path)


@unittest.skipUnless(HAVE_SSH_KEYGEN, "ssh-keygen not on PATH")
class TestSshsigSignAndVerify(ArtifactCase):
    """The signing chain end to end with real OpenSSH."""

    def setUp(self):
        super().setUp()
        self.key, self.pub = generate_signing_key(self.root / "keys")
        self.signers = self.root / "allowed_signers"
        self.signers.write_text(
            allowed_signers_line("fleet@test", self.pub)
        )
        self.artifact = self.root / "conch-worker.pyz"
        manifest = build_worker_artifact(self.artifact, packages=("conch",))
        self.manifest_path = write_manifest(self.artifact, manifest)
        self.sig = sign_manifest(self.manifest_path, self.key)

    def test_valid_signature_verifies_and_pins_digest(self):
        manifest = verify_artifact(
            self.artifact, self.manifest_path, self.sig, self.signers
        )
        self.assertEqual(manifest["verified_principal"], "fleet@test")
        self.assertEqual(
            manifest["artifact"]["sha256"], sha256_file(self.artifact)
        )

    def test_tampered_artifact_fails_closed(self):
        raw = bytearray(self.artifact.read_bytes())
        raw[len(raw) // 2] ^= 0xFF  # flip one byte in the middle
        self.artifact.write_bytes(bytes(raw))
        with self.assertRaises(ArtifactError) as ctx:
            verify_artifact(
                self.artifact, self.manifest_path, self.sig, self.signers
            )
        self.assertIn("digest", str(ctx.exception))

    def test_truncated_artifact_fails_closed(self):
        raw = self.artifact.read_bytes()
        self.artifact.write_bytes(raw[:-64])
        with self.assertRaises(ArtifactError) as ctx:
            verify_artifact(
                self.artifact, self.manifest_path, self.sig, self.signers
            )
        self.assertIn("size", str(ctx.exception))

    def test_tampered_manifest_fails_signature(self):
        text = self.manifest_path.read_text()
        self.manifest_path.write_text(text.replace('"size":', '"size" :'))
        with self.assertRaises(ArtifactError):
            verify_artifact(
                self.artifact, self.manifest_path, self.sig, self.signers
            )

    def test_missing_signature_fails_closed(self):
        with self.assertRaises(ArtifactError):
            verify_artifact(
                self.artifact, self.manifest_path,
                self.root / "no-such.sig", self.signers,
            )

    def test_unknown_signer_fails_closed(self):
        other_key, other_pub = generate_signing_key(
            self.root / "other-keys", name="other"
        )
        signers = self.root / "other_signers"
        signers.write_text(allowed_signers_line("other@test", other_pub))
        with self.assertRaises(ArtifactError):
            verify_artifact(
                self.artifact, self.manifest_path, self.sig, signers
            )

    def test_signature_over_different_manifest_fails(self):
        other = self.root / "other.pyz"
        manifest = build_worker_artifact(
            other, packages=("conch",), main_py="print('other')\n"
        )
        other_manifest = write_manifest(other, manifest)
        with self.assertRaises(ArtifactError):
            verify_artifact(
                other, other_manifest, self.sig, self.signers
            )

    def test_digest_swap_between_signed_manifests_fails(self):
        """A validly signed manifest cannot bless different bytes."""
        other = self.root / "other.pyz"
        build_worker_artifact(
            other, packages=("conch",), main_py="print('other')\n"
        )
        with self.assertRaises(ArtifactError) as ctx:
            verify_artifact(
                other, self.manifest_path, self.sig, self.signers
            )
        message = str(ctx.exception)
        self.assertTrue("digest" in message or "size" in message, message)


class TestBuildScriptAndDockerfile(unittest.TestCase):
    def test_build_script_exists_and_uses_the_library(self):
        script = ROOT / "tools" / "build_worker_artifact.py"
        text = script.read_text()
        self.assertIn("build_worker_artifact", text)
        self.assertIn("sign_manifest", text)

    def test_dockerfile_has_constrained_worker_target(self):
        text = (ROOT / "Dockerfile").read_text()
        self.assertIn("FROM runtime AS worker", text)
        self.assertIn('ENTRYPOINT ["conch-worker"]', text)
        # The worker target inherits runtime's non-root USER conch and
        # never reintroduces root.
        worker_block = text.split("FROM runtime AS worker", 1)[1]
        worker_block = worker_block.split("FROM", 1)[0]
        self.assertNotIn("USER root", worker_block)


if __name__ == "__main__":
    unittest.main()
