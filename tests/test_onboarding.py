"""First-run onboarding: gating, key handling, and config writing.

The wizard must only ever appear on a truly unconfigured interactive
launch, must never echo a key, and must leave 0600 files that
load_config actually honors.
"""

import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import config as config_mod
from conch import onboarding


class _Tty(io.StringIO):
    def isatty(self):
        return True


class _NoTty(io.StringIO):
    def isatty(self):
        return False


class OnboardingCase(unittest.TestCase):
    """Fresh HOME/XDG per test so the real user config is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "config").mkdir()
        self._env = patch.dict(os.environ, {
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
        }, clear=False)
        self._env.start()
        # Strip every provider key + wizard override from the inherited env.
        from conch.providers import DEFAULT_API_KEY_ENVS

        self._removed = {}
        for name in list(DEFAULT_API_KEY_ENVS.values()) + ["CONCH_NO_WIZARD"]:
            if name and name in os.environ:
                self._removed[name] = os.environ.pop(name)
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)
        # Project rc discovery walks from cwd; run from the isolated root.
        self._cwd = os.getcwd()
        os.chdir(str(root))
        self.addCleanup(os.chdir, self._cwd)

    def _restore(self):
        os.environ.update(self._removed)
        self._env.stop()


class TestShouldOfferWizard(OnboardingCase):
    def _tty(self):
        return patch.multiple(
            "conch.onboarding.sys", stdin=_Tty(), stdout=_Tty()
        )

    def test_offers_on_clean_interactive_start(self):
        with self._tty():
            self.assertTrue(onboarding.should_offer_wizard())

    def test_skips_without_tty(self):
        with patch.multiple(
            "conch.onboarding.sys", stdin=_NoTty(), stdout=_NoTty()
        ):
            self.assertFalse(onboarding.should_offer_wizard())

    def test_skips_when_config_file_exists(self):
        path = Path(config_mod.get_config_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("provider = openai\n")
        with self._tty():
            self.assertFalse(onboarding.should_offer_wizard())

    def test_skips_when_env_file_exists(self):
        path = Path(config_mod.get_env_file_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("OPENAI_API_KEY=sk-e2e\n")
        with self._tty():
            self.assertFalse(onboarding.should_offer_wizard())

    def test_skips_when_conchrc_exists(self):
        (Path.home() / ".conchrc").write_text("provider = ollama\n")
        with self._tty():
            self.assertFalse(onboarding.should_offer_wizard())

    def test_skips_when_a_provider_key_is_set(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with self._tty():
                self.assertFalse(onboarding.should_offer_wizard())

    def test_skips_when_disabled_by_env(self):
        with patch.dict(os.environ, {"CONCH_NO_WIZARD": "1"}):
            with self._tty():
                self.assertFalse(onboarding.should_offer_wizard())


SECRET = "sk-ant-wizard-secret-123"


class TestWizardFlow(OnboardingCase):
    def _run(self, inputs, getpass_values):
        """Run the wizard with scripted answers; return captured stdout."""
        answers = iter(inputs)
        keys = iter(getpass_values)
        out = io.StringIO()
        with patch("conch.onboarding.detect_ollama", return_value=None), \
             patch("conch.onboarding.probe_provider",
                   return_value=(True, "key accepted")), \
             patch("builtins.input", side_effect=lambda *_: next(answers)), \
             patch("conch.onboarding.getpass.getpass",
                   side_effect=lambda *_: next(keys)), \
             patch("sys.stdout", out):
            self.assertTrue(onboarding.run_first_run_wizard())
        return out.getvalue()

    def test_writes_provider_config_and_0600_key_file(self):
        output = self._run(["1", "y"], [SECRET])
        config_path = Path(config_mod.get_config_path())
        env_path = Path(config_mod.get_env_file_path())
        self.assertIn("provider = anthropic", config_path.read_text())
        env_text = env_path.read_text()
        self.assertIn(f"ANTHROPIC_API_KEY={SECRET}", env_text)
        for path in (config_path, env_path):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"{path} mode {oct(mode)}")
        # The secret never reaches the screen.
        self.assertNotIn(SECRET, output)
        # And the running process sees the key immediately.
        self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), SECRET)

    def test_ollama_path_needs_no_key(self):
        answers = iter(["4", "n"])
        with patch("conch.onboarding.detect_ollama",
                   return_value=["llama3.3:latest"]), \
             patch("builtins.input", side_effect=lambda *_: next(answers)), \
             patch("sys.stdout", io.StringIO()):
            self.assertTrue(onboarding.run_first_run_wizard())
        config = Path(config_mod.get_config_path()).read_text()
        self.assertIn("provider = ollama", config)
        self.assertFalse(Path(config_mod.get_env_file_path()).exists())

    def test_skipped_key_still_writes_provider(self):
        output = self._run(["2"], [""])
        config = Path(config_mod.get_config_path()).read_text()
        self.assertIn("provider = openai", config)
        self.assertIn("OPENAI_API_KEY", output)  # the how-to hint

    def test_config_written_by_wizard_loads(self):
        self._run(["1", "n"], [SECRET])
        loaded = config_mod.load_config()
        self.assertEqual(loaded["provider"], "anthropic")
        self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), SECRET)


class TestMaybeRunGate(OnboardingCase):
    def test_noninteractive_is_a_noop(self):
        with patch.multiple(
            "conch.onboarding.sys", stdin=_NoTty(), stdout=_NoTty()
        ):
            self.assertFalse(onboarding.maybe_run_first_run_wizard())
        self.assertFalse(Path(config_mod.get_config_path()).exists())

    def test_ctrl_c_skips_cleanly(self):
        with patch.multiple(
            "conch.onboarding.sys", stdin=_Tty(), stdout=_Tty()
        ), patch("conch.onboarding.run_first_run_wizard",
                 side_effect=KeyboardInterrupt), \
             patch("sys.stdout", io.StringIO()):
            self.assertFalse(onboarding.maybe_run_first_run_wizard())


class TestEnvFileLoading(OnboardingCase):
    def test_env_file_loads_with_environment_precedence(self):
        config_mod.set_env_values({"OPENAI_API_KEY": "sk-from-file"})
        del os.environ["OPENAI_API_KEY"]  # set_env_values mirrors; reset
        config_mod.load_env_file()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-from-file")
        os.environ["OPENAI_API_KEY"] = "sk-real-env"
        config_mod.load_env_file()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-real-env")

    def test_export_prefix_tolerated(self):
        path = Path(config_mod.get_env_file_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export OPENROUTER_API_KEY='sk-or-1'\n")
        config_mod.load_env_file()
        self.assertEqual(os.environ["OPENROUTER_API_KEY"], "sk-or-1")


class TestConfigWriter(OnboardingCase):
    def test_create_update_preserve(self):
        path = config_mod.set_config_values(
            {"provider": "openai"}, header="# created by test"
        )
        Path(path).write_text(
            Path(path).read_text() + "# my comment\nmodel = gpt-4o-mini\n"
        )
        config_mod.set_config_values({"provider": "anthropic",
                                      "edge_daemon": "true"})
        text = Path(path).read_text()
        self.assertIn("provider = anthropic", text)
        self.assertNotIn("provider = openai", text)
        self.assertIn("# my comment", text)
        self.assertIn("model = gpt-4o-mini", text)
        self.assertIn("edge_daemon = true", text)


if __name__ == "__main__":
    unittest.main()
