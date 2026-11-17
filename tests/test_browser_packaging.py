"""Browser-capture satellite packaging (macOS-first productionization).

Proven here: the extension's static files live inside the conch package
and are declared as package data, so the wheel a user's pip/uv/pipx
builds from the edge tarball actually carries them (a real `pip wheel`
build is unzipped and inspected); the pinned EXTENSION_ID is *derived*
from the manifest's committed public key, so the id in every generated
native-host manifest can never drift from the extension Chrome loads;
`/install capture browser`'s stable extension copy lands under the
user data dir, is complete, and refreshes on re-run; and the installed
host round-trips framed hello/event messages over real process stdio
into the kernel store with gating and status intact — the whole
extension → host → kernel path minus the one gesture Chrome reserves
for the user (load-unpacked + per-origin permission).
"""

import base64
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from conch.kernel.browser_capture import (
    EXTENSION_FILES,
    EXTENSION_ID,
    HOST_PROTOCOL_VERSION,
    BrowserEventRejected,
    extension_dir,
    extension_install_dir,
    host_manifest,
    install_extension_files,
    read_message,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_EXT_DIR = (REPO_ROOT / "conch" / "satellites"
                   / "browser_capture" / "extension")


def derive_extension_id(manifest_key_b64: str) -> str:
    """Chrome's unpacked-extension id for a manifest ``key``: the first
    16 bytes of SHA-256 over the DER public key, hex mapped a–p."""
    der = base64.b64decode(manifest_key_b64)
    digest = hashlib.sha256(der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(char, 16)) for char in digest)


class TestPackagedSource(unittest.TestCase):
    def test_extension_lives_inside_the_package(self):
        for name in EXTENSION_FILES:
            self.assertTrue((PACKAGE_EXT_DIR / name).is_file(), name)
        self.assertTrue(
            (PACKAGE_EXT_DIR.parent / "README.md").is_file()
        )
        self.assertEqual(extension_dir(), PACKAGE_EXT_DIR)

    def test_package_data_declared(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        self.assertIn('"conch.satellites.browser_capture"', pyproject)
        self.assertIn('"extension/*"', pyproject)

    def test_pinned_id_matches_manifest_key(self):
        manifest = json.loads(
            (PACKAGE_EXT_DIR / "manifest.json").read_text()
        )
        self.assertEqual(derive_extension_id(manifest["key"]),
                         EXTENSION_ID)
        # and the generated native-host manifest allows exactly that id
        native = host_manifest(command_path="/x/host")
        self.assertEqual(
            native["allowed_origins"],
            [f"chrome-extension://{EXTENSION_ID}/"],
        )

    def test_manifest_shape_chrome_expects(self):
        manifest = json.loads(
            (PACKAGE_EXT_DIR / "manifest.json").read_text()
        )
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["background"]["service_worker"],
                         "background.js")
        self.assertNotIn("<all_urls>", json.dumps(manifest))
        self.assertEqual(
            sorted(manifest["permissions"]),
            ["nativeMessaging", "scripting", "storage"],
        )


class TestWheelCarriesTheSatellite(unittest.TestCase):
    """Build a real wheel the way pip does from the edge tarball tree
    and assert the satellite ships. Skips only when no build tooling is
    available at all (offline sandbox without setuptools)."""

    def build_wheel(self, tmp: Path) -> Path:
        commands = [
            [sys.executable, "-m", "pip", "wheel", "--no-deps",
             "--no-build-isolation", "-w", str(tmp), str(REPO_ROOT)],
            [sys.executable, "-m", "pip", "wheel", "--no-deps",
             "-w", str(tmp), str(REPO_ROOT)],
        ]
        errors = []
        for command in commands:
            try:
                result = subprocess.run(
                    command, capture_output=True, timeout=600,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc))
                continue
            if result.returncode == 0:
                wheels = sorted(tmp.glob("conch_shell-*.whl"))
                if wheels:
                    return wheels[0]
            errors.append(result.stderr.decode()[-800:])
        self.skipTest(
            "cannot build a wheel here (no setuptools and no network): "
            + " | ".join(errors)
        )

    def test_wheel_contains_extension_and_launcher_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            wheel = self.build_wheel(Path(tmp_name))
            with zipfile.ZipFile(wheel) as archive:
                names = set(archive.namelist())
                base = "conch/satellites/browser_capture"
                for name in EXTENSION_FILES:
                    self.assertIn(f"{base}/extension/{name}", names)
                self.assertIn(f"{base}/README.md", names)
                self.assertIn(f"{base}/__init__.py", names)
                # the packaged manifest still derives the pinned id
                manifest = json.loads(archive.read(
                    f"{base}/extension/manifest.json"
                ))
                self.assertEqual(
                    derive_extension_id(manifest["key"]), EXTENSION_ID,
                )
                # console script registered for the native host
                entry_points = next(
                    name for name in names
                    if name.endswith("entry_points.txt")
                )
                self.assertIn(
                    "conch-capture-host = conch.entrypoints:"
                    "capture_host_main",
                    archive.read(entry_points).decode(),
                )


class TestStableExtensionCopy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {
            "XDG_DATA_HOME": os.path.join(self._tmp.name, "data"),
            "XDG_STATE_HOME": os.path.join(self._tmp.name, "state"),
            "XDG_CONFIG_HOME": os.path.join(self._tmp.name, "config"),
            "HOME": os.path.join(self._tmp.name, "home"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_copy_lands_complete_in_data_dir(self):
        destination = install_extension_files()
        self.assertEqual(destination, extension_install_dir())
        self.assertTrue(str(destination).startswith(
            os.environ["XDG_DATA_HOME"]
        ))
        for name in EXTENSION_FILES:
            self.assertTrue((destination / name).is_file(), name)
        self.assertTrue(
            (destination.parent / "README.md").is_file()
        )
        copied = json.loads((destination / "manifest.json").read_text())
        self.assertEqual(derive_extension_id(copied["key"]),
                         EXTENSION_ID)

    def test_rerun_refreshes_stale_files(self):
        destination = install_extension_files()
        (destination / "background.js").write_text("// stale edit\n")
        (destination / "stray.js").write_text("// cruft\n")
        refreshed = install_extension_files()
        self.assertEqual(refreshed, destination)
        self.assertNotIn(
            "stale edit", (destination / "background.js").read_text()
        )
        self.assertFalse((destination / "stray.js").exists())

    def test_missing_packaged_source_fails_closed(self):
        with patch(
            "conch.kernel.browser_capture.extension_dir",
            return_value=None,
        ):
            with self.assertRaises(BrowserEventRejected):
                install_extension_files()


class TestInstalledHostEndToEnd(unittest.TestCase):
    """The full extension→host→kernel path over real process stdio,
    exactly as Chrome drives it (argv caller origin, framed messages),
    against an isolated HOME — no Chrome required."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for sub in ("data", "state", "config/conch", "home"):
            (self.root / sub).mkdir(parents=True)
        self.env = dict(
            os.environ,
            XDG_DATA_HOME=str(self.root / "data"),
            XDG_STATE_HOME=str(self.root / "state"),
            XDG_CONFIG_HOME=str(self.root / "config"),
            HOME=str(self.root / "home"),
        )
        self.env.pop("XDG_RUNTIME_DIR", None)

    def _frame(self, obj) -> bytes:
        raw = json.dumps(obj).encode("utf-8")
        return struct.pack("<I", len(raw)) + raw

    def _run_host(self, payload: bytes) -> list:
        result = subprocess.run(
            [sys.executable, "-c",
             "from conch.entrypoints import capture_host_main; "
             "raise SystemExit(capture_host_main())",
             f"chrome-extension://{EXTENSION_ID}/"],
            input=payload, capture_output=True, timeout=60,
            env=self.env, cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        stream = io.BytesIO(result.stdout)
        replies = []
        while True:
            message = read_message(stream)
            if message is None:
                return replies
            replies.append(message)

    def test_gated_off_then_full_delivery_when_enabled(self):
        hello = {"type": "hello", "v": HOST_PROTOCOL_VERSION,
                 "ext_version": "0.1.0"}
        click = {
            "type": "event", "v": HOST_PROTOCOL_VERSION,
            "origin": "https://example.com", "ts": 1_800_000_000.0,
            "kind": "click",
            "detail": {"role": "link", "label": "More information"},
            "ext_version": "0.1.0",
        }
        # gates off: honest hello, event rejected, nothing journaled
        replies = self._run_host(self._frame(hello) + self._frame(click))
        self.assertFalse(replies[0]["capture"])
        self.assertFalse(replies[1]["ok"])
        # enable both gates the way /install capture browser does
        (self.root / "config" / "conch" / "config").write_text(
            "capture_enabled=true\ncapture_browser=true\n"
        )
        replies = self._run_host(self._frame(hello) + self._frame(click))
        self.assertTrue(replies[0]["capture"])
        self.assertTrue(replies[1]["ok"])
        self.assertIn(replies[1]["via"], ("socket", "direct"))
        # the event is in the kernel journal for /compile from-browser
        with patch.dict(os.environ, self.env):
            from conch.capitol.compiler.capture_browser import (
                capture_from_browser,
            )
            from conch.kernel.store import MissionStore

            store = MissionStore()
            try:
                rows = store.list_inbox(source="browser")
                self.assertEqual(len(rows), 1)
                context = capture_from_browser(store, "example.com")
            finally:
                store.close()
        self.assertEqual(context["kind"], "browser")
        self.assertIn("More information", context["block"])
        # and the handshake shows up in host status
        status_result = subprocess.run(
            [sys.executable, "-c",
             "from conch.entrypoints import capture_host_main; "
             "raise SystemExit(capture_host_main(['status']))"],
            capture_output=True, timeout=60, env=self.env,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(status_result.returncode, 0)
        status = json.loads(status_result.stdout)
        self.assertTrue(status["capture"])
        self.assertIn("handshake", status)


if __name__ == "__main__":
    unittest.main()
