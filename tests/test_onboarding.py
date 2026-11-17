"""First-run onboarding: gating, key handling, and config writing.

The wizard must only ever appear on a truly unconfigured interactive
launch, must never echo a key, and must leave 0600 files that
load_config actually honors.
"""

import io
import os
import re
import stat
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from conch import auth
from conch import config as config_mod
from conch import onboarding, seats


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
        # No official seat CLI unless a test says so: the wizard must never
        # spawn the developer's real claude/codex from the test suite.
        seat_cli = patch("conch.auth.cli_installed", return_value=False)
        seat_cli.start()
        self.addCleanup(seat_cli.stop)

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


def _verified(config, **_kwargs):
    """Stand-in for the post-setup model check: reports the configured
    model as verified without any network (its own tests live in
    test_modelcheck / TestWizardModelCheck below)."""
    from conch.modelcheck import OK, ModelCheckOutcome, ProbeResult

    provider = config.get("provider", "")
    model = config.get("chat_model") or config.get("model") or ""
    return ModelCheckOutcome(
        provider, model, ProbeResult(provider, model, OK, "stubbed")
    )


class TestWizardFlow(OnboardingCase):
    def _run(self, inputs, getpass_values):
        """Run the wizard with scripted answers; return captured stdout."""
        answers = iter(inputs)
        keys = iter(getpass_values)
        out = io.StringIO()
        with patch("conch.onboarding.detect_ollama", return_value=None), \
             patch("conch.onboarding.probe_provider",
                   return_value=(True, "key accepted")), \
             patch("conch.onboarding.ensure_working_model", _verified), \
             patch("builtins.input", side_effect=lambda *_: next(answers)), \
             patch("conch.onboarding.getpass.getpass",
                   side_effect=lambda *_: next(keys)), \
             patch("sys.stdout", out):
            self.assertTrue(onboarding.run_first_run_wizard())
        return out.getvalue()

    def test_writes_provider_config_and_0600_key_file(self):
        output = self._run(["anthropic", "y"], [SECRET])
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
        answers = iter(["ollama", "2"])
        with patch("conch.onboarding.detect_ollama",
                   return_value=["qwen2.5:7b", "llama3.3:latest"]), \
             patch("conch.onboarding.ensure_working_model", _verified), \
             patch("builtins.input", side_effect=lambda *_: next(answers)), \
             patch("sys.stdout", io.StringIO()):
            self.assertTrue(onboarding.run_first_run_wizard())
        config = Path(config_mod.get_config_path()).read_text()
        self.assertIn("provider = ollama", config)
        self.assertEqual(config_mod.load_config()["chat_model"], "llama3.3:latest")
        self.assertFalse(Path(config_mod.get_env_file_path()).exists())

    def test_config_written_by_wizard_loads(self):
        self._run(["anthropic", "n"], [SECRET])
        loaded = config_mod.load_config()
        self.assertEqual(loaded["provider"], "anthropic")
        self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), SECRET)


class TestCustomEndpointFlow(OnboardingCase):
    """The custom-provider branch must leave a config that starts clean:
    custom_base_url + custom_model persisted, api_key_env only when a key
    was given, and honest probe wording for a keyless endpoint."""

    def _run(self, inputs, getpass_values, models=(["stub-model"], True)):
        answers = iter(inputs)
        keys = iter(getpass_values)
        out = io.StringIO()
        with patch("conch.onboarding.detect_ollama", return_value=None), \
             patch("conch.onboarding.probe_provider",
                   return_value=(True, "endpoint reachable (no key sent)")), \
             patch("conch.onboarding.discover_custom_models",
                   return_value=models) as discover, \
             patch("conch.onboarding.ensure_working_model", _verified), \
             patch("builtins.input", side_effect=lambda *_: next(answers)), \
             patch("conch.onboarding.getpass.getpass",
                   side_effect=lambda *_: next(keys)), \
             patch("sys.stdout", out):
            self.assertTrue(onboarding.run_first_run_wizard())
        return out.getvalue(), discover

    def test_persists_custom_model_and_base_url_without_key(self):
        _, discover = self._run(
            ["custom", "http://127.0.0.1:18080/v1", "n"], [""]
        )
        text = Path(config_mod.get_config_path()).read_text()
        self.assertIn("custom_base_url = http://127.0.0.1:18080/v1", text)
        self.assertIn("custom_model = stub-model", text)
        self.assertNotIn("api_key_env", text)
        self.assertFalse(Path(config_mod.get_env_file_path()).exists())
        discover.assert_called_once_with("http://127.0.0.1:18080/v1", "")

    def test_loaded_config_is_complete_and_keyless(self):
        self._run(["custom", "http://127.0.0.1:18080/v1", "n"], [""])
        loaded = config_mod.load_config()
        self.assertEqual(loaded["provider"], "custom")
        self.assertEqual(loaded["custom_model"], "stub-model")
        self.assertEqual(loaded["model"], "stub-model")
        self.assertEqual(loaded["chat_model"], "stub-model")
        self.assertEqual(loaded["api_key_env"], "",
                         "the Anthropic default must not leak under custom")

    def test_key_is_stored_under_conch_name_and_referenced(self):
        output, discover = self._run(
            ["custom", "http://10.0.0.5:8000/v1", "n"], ["vllm-secret-1"]
        )
        text = Path(config_mod.get_config_path()).read_text()
        self.assertIn("api_key_env = CONCH_CUSTOM_API_KEY", text)
        env_text = Path(config_mod.get_env_file_path()).read_text()
        self.assertIn("CONCH_CUSTOM_API_KEY=vllm-secret-1", env_text)
        self.assertNotIn("vllm-secret-1", output)
        discover.assert_called_once_with("http://10.0.0.5:8000/v1", "vllm-secret-1")
        loaded = config_mod.load_config()
        self.assertEqual(loaded["api_key_env"], "CONCH_CUSTOM_API_KEY")

    def test_multiple_models_offer_a_choice(self):
        self._run(
            ["custom", "http://127.0.0.1:18080/v1", "n", "2"], [""],
            models=(["alpha", "beta"], True),
        )
        text = Path(config_mod.get_config_path()).read_text()
        self.assertIn("custom_model = beta", text)

    def test_unreachable_endpoint_leaves_model_unset(self):
        self._run(
            ["custom", "http://127.0.0.1:1/v1", "n"], [""], models=(None, False)
        )
        text = Path(config_mod.get_config_path()).read_text()
        self.assertIn("custom_base_url = http://127.0.0.1:1/v1", text)
        self.assertNotIn("custom_model", text)

    def test_no_conformant_model_still_persists(self):
        self._run(
            ["custom", "http://127.0.0.1:18080/v1", "n"], [""],
            models=(["plain-model"], False),
        )
        text = Path(config_mod.get_config_path()).read_text()
        self.assertIn("custom_model = plain-model", text)


class TestCustomProbeWording(OnboardingCase):
    def test_keyless_success_does_not_claim_a_key_was_accepted(self):
        with patch("conch.onboarding._http_get", return_value=(200, "")):
            ok, detail = onboarding.probe_provider("custom", "", "http://x/v1")
        self.assertTrue(ok)
        self.assertEqual(detail, "endpoint reachable (no key sent)")

    def test_keyed_success_still_says_key_accepted(self):
        with patch("conch.onboarding._http_get", return_value=(200, "")):
            ok, detail = onboarding.probe_provider("custom", "k", "http://x/v1")
        self.assertTrue(ok)
        self.assertEqual(detail, "key accepted")

    def test_keyless_401_explains_a_key_is_required(self):
        with patch("conch.onboarding._http_get", return_value=(401, "")):
            ok, detail = onboarding.probe_provider("custom", "", "http://x/v1")
        self.assertFalse(ok)
        self.assertEqual(detail, "the endpoint requires an API key")


class TestCustomProviderConfigDefaults(OnboardingCase):
    def test_custom_provider_has_empty_api_key_env_by_default(self):
        config_mod.set_config_values({
            "provider": "custom",
            "custom_base_url": "http://127.0.0.1:18080/v1",
            "custom_model": "m",
        })
        self.assertEqual(config_mod.load_config()["api_key_env"], "")

    def test_explicit_api_key_env_is_kept(self):
        config_mod.set_config_values({
            "provider": "custom",
            "custom_base_url": "http://127.0.0.1:18080/v1",
            "custom_model": "m",
            "api_key_env": "MY_VLLM_KEY",
        })
        self.assertEqual(config_mod.load_config()["api_key_env"], "MY_VLLM_KEY")


class TestMaybeRunGate(OnboardingCase):
    def test_noninteractive_is_a_noop(self):
        with patch.multiple(
            "conch.onboarding.sys", stdin=_NoTty(), stdout=_NoTty()
        ):
            self.assertFalse(onboarding.maybe_run_first_run_wizard())
        self.assertFalse(Path(config_mod.get_config_path()).exists())

    def test_cancelled_setup_exits_without_starting_chat_or_saving_config(self):
        with patch("conch.onboarding.should_offer_wizard", return_value=True), \
             patch("conch.onboarding.detect_ollama", return_value=None), \
             patch("builtins.input", side_effect=KeyboardInterrupt), \
             patch("sys.stdout", io.StringIO()), \
             self.assertRaises(SystemExit) as caught:
            onboarding.maybe_run_first_run_wizard()
        self.assertEqual(caught.exception.code, 0)
        self.assertFalse(Path(config_mod.get_config_path()).exists())


class WizardCase(OnboardingCase):
    """Scripted wizard walks: no network, no native CLI, no real model check."""

    def walk(self, inputs, keys=(), *, ollama=(None,), signed_in=(),
             installed=(), login_exit=1, columns=80, key_accepted=True,
             verification=()):
        """Drive maybe_run_first_run_wizard with scripted answers.

        The script runs out into Ctrl+C, so a walk that never connects a
        provider ends with ``exit_code == 0``. *ollama* lists successive
        detection results (the last repeats); *signed_in*/*installed* name
        the seats whose official CLI is on PATH; a native login exiting 0
        signs that seat in. *verification* lists model-check results in
        order (an exception fails that check); later checks succeed.
        """
        ready = set(signed_in)
        on_path = ready | set(installed)
        binaries = {seats.transport(provider).auth_command("login")[0]: provider
                    for provider in seats.PROVIDERS}
        detections = list(ollama)

        def detect():
            return detections.pop(0) if len(detections) > 1 else detections[0]

        def native(argv, **_kwargs):
            if not login_exit:
                ready.add(binaries[argv[0]])
            return Mock(returncode=login_exit)

        def status(provider, timeout=10):
            return provider in ready, "scripted"

        walk = SimpleNamespace(exit_code=None, outcome=None)
        out = io.StringIO()
        with ExitStack() as stack:
            def use(target, **kwargs):
                return stack.enter_context(patch(target, **kwargs))

            use("conch.onboarding.should_offer_wizard", return_value=True)
            use("conch.onboarding.detect_ollama", side_effect=detect)
            use("conch.onboarding.discover_custom_models",
                return_value=(None, False))
            use("conch.onboarding.probe_provider",
                return_value=(key_accepted, "scripted"))
            use("conch.auth.cli_installed",
                side_effect=lambda provider: provider in on_path)
            use("conch.seats.status", side_effect=status)
            use("conch.auth.status", side_effect=status)
            walk.login = use("conch.auth.subprocess.run", side_effect=native)
            checks = list(verification)

            def check(config, **kwargs):
                result = checks.pop(0) if checks else None
                if isinstance(result, BaseException):
                    raise result
                return _verified(config, **kwargs)

            walk.check = use("conch.onboarding.ensure_working_model",
                             side_effect=check)
            walk.asked = use("builtins.input",
                             side_effect=[*inputs, KeyboardInterrupt])
            walk.hidden = use("conch.onboarding.getpass.getpass",
                              side_effect=list(keys))
            stack.enter_context(
                patch.dict(os.environ, {"COLUMNS": str(columns)})
            )
            use("sys.stdout", new=out)
            try:
                walk.outcome = onboarding.maybe_run_first_run_wizard()
            except SystemExit as exc:
                walk.exit_code = exc.code
        walk.output = out.getvalue()
        return walk

    def assert_nothing_saved(self):
        self.assertFalse(Path(config_mod.get_config_path()).exists())
        self.assertFalse(Path(config_mod.get_env_file_path()).exists())

    def assert_back_at_provider_prompt_then_cancelled(self, walk, answered):
        """Every scripted answer was consumed, one more question was asked
        (the provider prompt, answered by Ctrl+C), and setup exited 0
        without saving or checking anything."""
        self.assertEqual(walk.asked.call_count, answered + 1)
        self.assertEqual(walk.exit_code, 0)
        self.assertIsNone(walk.outcome)
        walk.check.assert_not_called()
        self.assert_nothing_saved()


class TestWizardRecovery(WizardCase):
    """A choice that cannot be connected saves nothing and returns to the
    provider prompt instead of ending setup."""

    def test_missing_seat_cli_returns_to_the_provider_prompt(self):
        walk = self.walk(["claude-code"])
        self.assert_back_at_provider_prompt_then_cancelled(walk, answered=1)
        walk.login.assert_not_called()

    def test_failed_native_sign_in_returns_to_the_provider_prompt(self):
        walk = self.walk(["codex"], installed=["codex"], login_exit=1)
        self.assert_back_at_provider_prompt_then_cancelled(walk, answered=1)
        walk.login.assert_called_once()

    def test_empty_api_key_returns_to_the_provider_prompt(self):
        walk = self.walk(["openai"], keys=[""])
        self.assert_back_at_provider_prompt_then_cancelled(walk, answered=1)

    def test_rejected_api_key_is_not_saved_unless_the_user_insists(self):
        # Verify: yes; save anyway: no; then an empty key goes back.
        walk = self.walk(["openai", "y", "n"], keys=[SECRET, ""],
                         key_accepted=False)
        self.assert_back_at_provider_prompt_then_cancelled(walk, answered=3)
        self.assertEqual(walk.hidden.call_count, 2)

    def test_rejected_api_key_is_saved_when_the_user_insists(self):
        walk = self.walk(["openai", "y", "y"], keys=[SECRET], key_accepted=False)
        self.assertTrue(walk.outcome)
        self.assertIn(f"OPENAI_API_KEY={SECRET}",
                      Path(config_mod.get_env_file_path()).read_text())

    def test_unreachable_ollama_returns_to_the_provider_prompt(self):
        walk = self.walk(["ollama"], ollama=[None])
        self.assert_back_at_provider_prompt_then_cancelled(walk, answered=1)

    def test_ollama_without_tool_models_returns_to_the_provider_prompt(self):
        walk = self.walk(["ollama"], ollama=[[]])
        self.assert_back_at_provider_prompt_then_cancelled(walk, answered=1)

    def test_custom_endpoint_without_url_returns_to_the_provider_prompt(self):
        walk = self.walk(["custom", ""])
        self.assert_back_at_provider_prompt_then_cancelled(walk, answered=2)
        walk.hidden.assert_not_called()

    def test_another_provider_can_be_connected_after_a_dead_end(self):
        walk = self.walk(["claude-code", "openai", "openai", "n"],
                         keys=["", SECRET])
        self.assertIsNone(walk.exit_code)
        self.assertTrue(walk.outcome)
        self.assertIn("provider = openai",
                      Path(config_mod.get_config_path()).read_text())
        self.assertIn(f"OPENAI_API_KEY={SECRET}",
                      Path(config_mod.get_env_file_path()).read_text())
        self.assertNotIn(SECRET, walk.output)

    def test_failed_verification_can_restart_with_nothing_kept(self):
        from conch.bootstrap import StartupError

        # openai connects, its model fails; "choose different" → back to menu.
        walk = self.walk(["openai", "n", "y"], keys=[SECRET],
                         verification=[StartupError("no working model", code=69)])
        self.assert_back_at_provider_prompt_then_cancelled_after_check(walk)
        self.assertNotIn("OPENAI_API_KEY", os.environ)

    def test_failed_verification_then_another_provider_starts_clean(self):
        from conch.bootstrap import StartupError

        walk = self.walk(
            ["custom", "http://127.0.0.1:1/v1", "n", "", "anthropic", "n"],
            keys=["endpoint-secret", SECRET],
            verification=[StartupError("no working model", code=69)],
        )
        self.assertTrue(walk.outcome)
        loaded = config_mod.load_config()
        self.assertEqual(loaded["provider"], "anthropic")
        self.assertNotIn("custom_base_url",
                         Path(config_mod.get_config_path()).read_text())
        env_text = Path(config_mod.get_env_file_path()).read_text()
        self.assertIn(f"ANTHROPIC_API_KEY={SECRET}", env_text)
        self.assertNotIn("endpoint-secret", env_text)

    def test_declining_to_restart_keeps_the_setup_and_reports_failure(self):
        from conch.bootstrap import StartupError

        walk = self.walk(["openai", "n", "n"], keys=[SECRET],
                         verification=[StartupError("no working model", code=69)])
        self.assertEqual(walk.exit_code, 69)
        self.assertIn("provider = openai",
                      Path(config_mod.get_config_path()).read_text())

    def assert_back_at_provider_prompt_then_cancelled_after_check(self, walk):
        self.assertEqual(walk.exit_code, 0)
        self.assertIsNone(walk.outcome)
        walk.check.assert_called_once()
        self.assert_nothing_saved()

    def test_ollama_is_detected_again_when_chosen_again(self):
        # Menu detection, first choice: down. Second choice: up with a model.
        walk = self.walk(["ollama", "ollama"], ollama=[None, None, ["m1"]])
        self.assertTrue(walk.outcome)
        loaded = config_mod.load_config()
        self.assertEqual(loaded["provider"], "ollama")
        self.assertEqual(loaded["chat_model"], "m1")


class TestSeatFlow(WizardCase):
    def _saved(self):
        return Path(config_mod.get_config_path()).read_text()

    def test_signed_in_seat_is_selected_without_a_new_login(self):
        walk = self.walk(["codex"], signed_in=["codex"])
        walk.login.assert_not_called()
        walk.check.assert_called_once()
        self.assertTrue(walk.outcome)
        saved = self._saved()
        self.assertIn("provider = codex", saved)
        self.assertIn(f"chat_model = {seats.DEFAULT_MODELS['codex']}", saved)
        self.assertFalse(Path(config_mod.get_env_file_path()).exists())

    def test_enter_selects_the_first_signed_in_seat(self):
        walk = self.walk([""], signed_in=["codex"], installed=["claude-code"])
        walk.login.assert_not_called()
        self.assertIn("provider = codex", self._saved())

    def test_enter_prefers_an_installed_seat_over_a_missing_one(self):
        walk = self.walk([""], installed=["codex"], login_exit=0)
        self.assertEqual(walk.login.call_args.args[0],
                         seats.transport("codex").auth_command("login"))
        self.assertIn("provider = codex", self._saved())

    def test_successful_native_sign_in_selects_the_seat_and_verifies(self):
        walk = self.walk(["claude-code"], installed=["claude-code"],
                         login_exit=0)
        walk.login.assert_called_once()
        walk.check.assert_called_once()
        self.assertIn("provider = claude-code", self._saved())

    def test_status_failure_means_sign_in_needed_not_an_error(self):
        with patch("conch.auth.cli_installed", return_value=True), \
             patch("conch.seats.status", side_effect=RuntimeError("boom")):
            self.assertEqual(onboarding.seat_state("codex"),
                             onboarding.SEAT_NEEDS_LOGIN)


class TestSeatCliLookup(unittest.TestCase):
    def test_cli_is_found_on_the_path_the_transports_use(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            binary = Path(bin_dir) / seats.transport("codex").auth_command("status")[0]
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            with patch.dict(os.environ, {"PATH": bin_dir}):
                self.assertTrue(auth.cli_installed("codex"))
                self.assertFalse(auth.cli_installed("claude-code"))


class TestStandaloneLogin(OnboardingCase):
    """`conch auth login` keeps its exit codes; only success selects the seat."""

    def _login(self, native, signed_in):
        with patch("conch.auth.subprocess.run", **native), \
             patch("conch.auth.status", return_value=(signed_in, "scripted")), \
             patch("sys.stdout", io.StringIO()), \
             patch("sys.stderr", io.StringIO()):
            return auth.login("codex")

    def test_failures_keep_their_exit_codes_and_select_nothing(self):
        cases = (
            ({"side_effect": FileNotFoundError}, False, 1),
            ({"side_effect": PermissionError}, False, 1),
            ({"return_value": Mock(returncode=7)}, True, 7),
            ({"return_value": Mock(returncode=0)}, False, 1),
        )
        for native, signed_in, expected in cases:
            with self.subTest(native=native, signed_in=signed_in):
                self.assertEqual(self._login(native, signed_in), expected)
                self.assertFalse(Path(config_mod.get_config_path()).exists())

    def test_success_selects_the_seat(self):
        self.assertEqual(
            self._login({"return_value": Mock(returncode=0)}, True), 0
        )
        self.assertIn("provider = codex",
                      Path(config_mod.get_config_path()).read_text())


class TestWizardLayout(WizardCase):
    ANSI = re.compile(r"\033\[[0-9;]*m")

    def test_no_line_or_prompt_exceeds_the_wrap_width(self):
        dead_ends_then_custom = dict(
            inputs=["claude-code", "codex", "openai", "ollama", "ollama",
                    "custom", "", "bogus", "custom", "http://127.0.0.1:1/v1",
                    "y"],
            keys=["", ""], installed=["codex"], ollama=[None, None, []],
        )
        api_key = dict(inputs=["anthropic", "y"], keys=[SECRET],
                       ollama=[["m1", "m2"]])
        seat = dict(inputs=[""], signed_in=["claude-code"])
        ollama = dict(inputs=["ollama", ""], ollama=[["m1", "m2"]])
        for columns in (80, 100):
            for script in (dead_ends_then_custom, api_key, seat, ollama):
                with self.subTest(columns=columns, inputs=script["inputs"]):
                    walk = self.walk(columns=columns, **script)
                    self.assertTrue(walk.outcome)
                    shown = walk.output.splitlines() + [
                        call.args[0] for call in
                        walk.asked.call_args_list + walk.hidden.call_args_list
                    ]
                    limit = min(columns, onboarding.WRAP_WIDTH)
                    for line in shown:
                        self.assertLessEqual(
                            len(self.ANSI.sub("", line)), limit, line
                        )

    def test_displayed_paths_abbreviate_home_but_files_stay_put(self):
        walk = self.walk(["anthropic", "n"], keys=[SECRET])
        home = str(Path.home())
        config_path = config_mod.get_config_path()
        self.assertTrue(Path(config_path).exists())
        self.assertNotIn(home, walk.output)
        self.assertIn("~" + config_path[len(home):], walk.output)


class TestEnvFileLoading(OnboardingCase):
    def test_env_file_precedence_by_provenance(self):
        """A key the wizard/`conch keys` wrote is conch-managed and wins over
        a stale shell export; a hand-added line keeps environment precedence
        (see conch.config.load_env_file)."""
        config_mod._ENV_LOAD_STATE["from_file"].clear()
        config_mod.set_env_values({"OPENAI_API_KEY": "sk-from-file"})
        del os.environ["OPENAI_API_KEY"]  # set_env_values mirrors; reset
        config_mod.load_env_file()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-from-file")
        os.environ["OPENAI_API_KEY"] = "sk-real-env"
        config_mod.load_env_file()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-from-file")
        config_mod.forget_managed_key("OPENAI_API_KEY")  # now a hand-added line
        os.environ["OPENAI_API_KEY"] = "sk-real-env"
        config_mod._ENV_LOAD_STATE["from_file"].clear()
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
