"""The env file as the single source of truth: atomic 0600 writes that
preserve comments, the conch-managed manifest, the precedence rule
(managed keys override inherited exports; hand-added lines do not), and
the hot-reload semantics of ``reload_env_file``."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import config as config_mod


class EnvFileCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / ".config" / "conch"
        self.config_dir.mkdir(parents=True)
        self.env_path = self.config_dir / "env"
        self._env = patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
        }, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        for name in ("ALPHA_KEY", "BETA_KEY", "GAMMA_KEY"):
            os.environ.pop(name, None)
            self.addCleanup(os.environ.pop, name, None)
        # Fresh load state per test.
        config_mod._ENV_LOAD_STATE["from_file"].clear()
        config_mod._ENV_LOAD_STATE["overrode"] = []
        config_mod._ENV_LOAD_STATE["shadowed"] = []

    def mode(self, path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)


class TestAtomicWrite(EnvFileCase):
    def test_set_preserves_comments_order_and_export_prefix(self):
        self.env_path.write_text(
            "# my keys\nexport ALPHA_KEY=old-alpha\n\n# beta below\nBETA_KEY=old-beta\n"
        )
        config_mod.set_env_values({"ALPHA_KEY": "new-alpha", "GAMMA_KEY": "gamma"})
        text = self.env_path.read_text()
        self.assertEqual(
            text,
            "# my keys\nexport ALPHA_KEY=new-alpha\n\n# beta below\nBETA_KEY=old-beta\nGAMMA_KEY=gamma\n",
        )
        self.assertEqual(self.mode(self.env_path), 0o600)
        self.assertFalse(list(self.config_dir.glob(".env.*.tmp")), "temp file left behind")

    def test_new_file_gets_header_and_0600(self):
        config_mod.set_env_values({"ALPHA_KEY": "v"})
        text = self.env_path.read_text()
        self.assertTrue(text.startswith("# Conch credentials"))
        self.assertIn("ALPHA_KEY=v\n", text)
        self.assertEqual(self.mode(self.env_path), 0o600)

    def test_duplicate_assignments_collapse(self):
        self.env_path.write_text("ALPHA_KEY=a\nALPHA_KEY=b\n")
        config_mod.set_env_values({"ALPHA_KEY": "c"})
        self.assertEqual(self.env_path.read_text(), "ALPHA_KEY=c\n")

    def test_remove_keeps_other_lines(self):
        self.env_path.write_text("# head\nALPHA_KEY=a\nBETA_KEY=b\n")
        config_mod.set_env_values({"ALPHA_KEY": "a"})  # managed
        removed = config_mod.remove_env_values(["ALPHA_KEY", "NOPE"])
        self.assertEqual(removed, ["ALPHA_KEY"])
        self.assertEqual(self.env_path.read_text(), "# head\nBETA_KEY=b\n")
        self.assertNotIn("ALPHA_KEY", config_mod.read_env_manifest()["keys"])

    def test_fingerprint_is_sha256_prefix_never_value(self):
        fp = config_mod.key_fingerprint("sk-secret-value")
        self.assertTrue(fp.startswith("sha256:"))
        self.assertEqual(len(fp), len("sha256:") + 8)
        self.assertNotIn("secret", fp)
        self.assertEqual(config_mod.key_fingerprint(""), "-")


class TestManifest(EnvFileCase):
    def test_set_records_managed_fingerprint_not_value(self):
        config_mod.set_env_values({"ALPHA_KEY": "sk-alpha-123456"}, set_by="conch keys set", verified="ok")
        manifest = json.loads((self.config_dir / "env.manifest.json").read_text())
        entry = manifest["keys"]["ALPHA_KEY"]
        self.assertEqual(entry["fingerprint"], config_mod.key_fingerprint("sk-alpha-123456", 16))
        self.assertEqual(entry["set_by"], "conch keys set")
        self.assertEqual(entry["verified"], "ok")
        self.assertNotIn("sk-alpha", (self.config_dir / "env.manifest.json").read_text())
        self.assertEqual(self.mode(self.config_dir / "env.manifest.json"), 0o600)
        self.assertEqual(config_mod.managed_key_names(), {"ALPHA_KEY"})

    def test_hand_added_line_is_not_managed(self):
        self.env_path.write_text("ALPHA_KEY=hand\n")
        self.assertEqual(config_mod.managed_key_names(), set())

    def test_record_key_check_keyed_by_fingerprint(self):
        config_mod.set_env_values({"ALPHA_KEY": "v1"})
        config_mod.record_key_check("ALPHA_KEY", "v1", "ok", "accepted")
        data = config_mod.read_env_manifest()
        self.assertEqual(data["checks"]["ALPHA_KEY"]["status"], "ok")
        self.assertEqual(data["keys"]["ALPHA_KEY"]["verified"], "ok")
        # A different value's verdict does not touch the managed record.
        config_mod.record_key_check("ALPHA_KEY", "v2", "rejected", "nope")
        data = config_mod.read_env_manifest()
        self.assertEqual(data["keys"]["ALPHA_KEY"]["verified"], "ok")
        self.assertEqual(data["checks"]["ALPHA_KEY"]["status"], "rejected")

    def test_damaged_manifest_means_nothing_managed(self):
        self.env_path.write_text("ALPHA_KEY=v\n")
        (self.config_dir / "env.manifest.json").write_text("{not json")
        self.assertEqual(config_mod.managed_key_names(), set())
        os.environ["ALPHA_KEY"] = "shell"
        config_mod.load_env_file()
        self.assertEqual(os.environ["ALPHA_KEY"], "shell")


class TestPrecedence(EnvFileCase):
    def test_unset_name_is_mirrored(self):
        self.env_path.write_text("ALPHA_KEY=from-file\n")
        changed = config_mod.load_env_file()
        self.assertEqual(os.environ["ALPHA_KEY"], "from-file")
        self.assertEqual(changed, ["ALPHA_KEY"])
        self.assertEqual(config_mod.env_load_state()["from_file"]["ALPHA_KEY"], "from-file")

    def test_hand_added_line_loses_to_inherited_export(self):
        self.env_path.write_text("ALPHA_KEY=from-file\n")
        os.environ["ALPHA_KEY"] = "from-shell"
        changed = config_mod.load_env_file()
        self.assertEqual(os.environ["ALPHA_KEY"], "from-shell")
        self.assertEqual(changed, [])
        self.assertEqual(config_mod.env_load_state()["shadowed"], ["ALPHA_KEY"])

    def test_managed_key_overrides_inherited_export(self):
        config_mod.set_env_values({"ALPHA_KEY": "from-file"})
        os.environ["ALPHA_KEY"] = "from-shell"
        changed = config_mod.load_env_file()
        self.assertEqual(os.environ["ALPHA_KEY"], "from-file")
        self.assertEqual(changed, ["ALPHA_KEY"])
        self.assertEqual(config_mod.env_load_state()["overrode"], ["ALPHA_KEY"])
        self.assertEqual(config_mod.env_load_state()["shadowed"], [])

    def test_reload_picks_up_rotated_value_even_unmanaged(self):
        self.env_path.write_text("ALPHA_KEY=v1\n")
        config_mod.load_env_file()
        self.assertEqual(os.environ["ALPHA_KEY"], "v1")
        self.env_path.write_text("ALPHA_KEY=v2\n")
        changed = config_mod.reload_env_file()
        self.assertEqual(changed, ["ALPHA_KEY"])
        self.assertEqual(os.environ["ALPHA_KEY"], "v2")

    def test_reload_leaves_removed_keys_in_process(self):
        self.env_path.write_text("ALPHA_KEY=v1\nBETA_KEY=b\n")
        config_mod.load_env_file()
        self.env_path.write_text("BETA_KEY=b\n")
        self.assertEqual(config_mod.reload_env_file(), [])
        self.assertEqual(os.environ["ALPHA_KEY"], "v1")

    def test_load_config_applies_precedence(self):
        (self.config_dir / "config").write_text("provider = openai\napi_key_env = ALPHA_KEY\n")
        config_mod.set_env_values({"ALPHA_KEY": "managed"})
        os.environ["ALPHA_KEY"] = "stale-export"
        config_mod.load_config()
        self.assertEqual(os.environ["ALPHA_KEY"], "managed")

    def test_parse_env_lines_tolerates_quotes_exports_comments(self):
        parsed = config_mod.parse_env_lines(
            "# c\nexport A='x y'\nB=\"q\"\nC=plain\nnot a line\n"
        )
        self.assertEqual(parsed, {"A": "x y", "B": "q", "C": "plain"})


if __name__ == "__main__":
    unittest.main()
