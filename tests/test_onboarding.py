"""Setup certifies the selected route, never key presence or failed alternatives."""

import io
import os
import stat
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from conch import auth, config as config_mod, onboarding, seats, modelcheck
from conch.bootstrap import StartupError


class Tty(io.StringIO):
    def isatty(self):
        return True


class OnboardingCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enter = ExitStack()
        self.addCleanup(self.enter.close)
        self.enter.enter_context(patch.dict(os.environ, {
            "HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "config"),
            "PATH": "/usr/bin:/bin",
        }, clear=True))
        self.enter.enter_context(patch.dict(config_mod._ENV_LOAD_STATE, {
            "from_file": {}, "overrode": [], "shadowed": [],
        }, clear=True))
        old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old_cwd)
        self.enter.enter_context(patch("conch.auth.cli_installed", return_value=False))
        self.enter.enter_context(patch("conch.onboarding.detect_ollama", return_value=None))
        self.enter.enter_context(patch("sys.stdin", Tty()))
        self.output = self.enter.enter_context(patch("sys.stdout", Tty()))
        self.error = self.enter.enter_context(patch("sys.stderr", io.StringIO()))
        self.config_path = Path(config_mod.get_config_path())
        self.env_path = Path(config_mod.get_env_file_path())

    def probe(self, provider, model, config, **kwargs):
        return modelcheck.ProbeResult(provider, model, modelcheck.OK, "dummy verified")

    def walk(self, answers, keys=(), probe=None, signed_in=()):
        states = {p: onboarding.SEAT_READY if p in signed_in else onboarding.SEAT_MISSING
                  for p in seats.PROVIDERS}
        with patch("conch.onboarding._detect", return_value=(states, None)), \
                patch("conch.onboarding.seat_state", side_effect=lambda p: states[p]), \
                patch("conch.onboarding.probe_model", side_effect=probe or self.probe) as check, \
                patch("builtins.input", side_effect=answers), \
                patch("conch.onboarding.getpass.getpass", side_effect=keys):
            outcome = onboarding.run_first_run_wizard()
        return outcome, check

    def snapshot(self):
        return {p: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
                for p in self.root.rglob("*") if p.is_file()}

    def existing(self):
        config_mod.set_config_values({"provider": "openai", "model": "gpt-4o-mini",
                                     "chat_model": "gpt-4o-mini", "send_cwd": "false"})
        config_mod.set_env_values({"OPENAI_API_KEY": "old-openai-secret",
                                  "ANTHROPIC_API_KEY": "old-anthropic-secret"})
        (self.root / "session-state").write_text("preserve prior history")


class TestWizardGate(OnboardingCase):
    def test_fresh_default_is_claude_and_subscription_choices_are_first(self):
        self.assertTrue(onboarding.should_offer_wizard())
        self.assertEqual(onboarding.WIZARD_PROVIDERS[0][0], "claude-code")
        self.assertEqual(onboarding.WIZARD_PROVIDERS[1][0], "codex")
        self.assertEqual(onboarding._default_choice({}), 1)

    def test_config_keys_and_no_wizard_prevent_automatic_fresh_setup(self):
        for path in (self.config_path, self.env_path, self.root / ".conchrc"):
            with self.subTest(path=path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# user-owned\n")
                self.assertFalse(onboarding.should_offer_wizard())
                path.unlink()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "invalid-is-still-configured"}):
            self.assertFalse(onboarding.should_offer_wizard())
        with patch.dict(os.environ, {"CONCH_NO_WIZARD": "1"}):
            self.assertFalse(onboarding.should_offer_wizard())

    def test_non_tty_never_prompts_or_signs_in(self):
        with patch("sys.stdin", io.StringIO()), \
                patch("builtins.input", side_effect=AssertionError("no prompts")), \
                patch("conch.auth.sign_in", side_effect=AssertionError("no native login")):
            self.assertFalse(onboarding.should_offer_wizard())
            self.assertIsNone(onboarding.maybe_run_first_run_wizard())
            with self.assertRaises(StartupError):
                onboarding.run_first_run_wizard()


class TestStagedWizard(OnboardingCase):
    def test_ollama_recovery_keeps_discovered_endpoint_over_stale_custom_url(self):
        from conch import providers
        from tests.modelcheck_stubs import StubEndpoint

        discovered = StubEndpoint(models=("recovery-model",)).start()
        self.addCleanup(discovered.stop)
        previous = StubEndpoint(models=("unrelated-model",)).start()
        self.addCleanup(previous.stop)
        config_mod.set_config_values({
            "provider": "custom", "base_url": previous.v1,
            "model": "unrelated-model", "chat_model": "unrelated-model",
        })
        with patch.dict(os.environ, {"OLLAMA_HOST": discovered.url}), \
                patch("builtins.input", return_value=""):
            models = providers.list_ollama_models(force_refresh=True)
            self.assertEqual(models, ["recovery-model"])
            setup = onboarding._connect_ollama(models)
            outcome = onboarding._verify_staged(setup)
            onboarding._save_setup(setup, outcome)
        saved = config_mod.load_config()
        self.assertTrue(outcome.saved and outcome.result.ok)
        self.assertEqual(saved["provider"], "ollama")
        self.assertEqual(saved["chat_model"], "recovery-model")
        self.assertEqual(providers.get_ollama_base_url(saved), discovered.url)
        self.assertEqual(previous.requests, [])

    def test_api_key_and_route_written_only_after_verified_with_private_modes(self):
        secret = "dummy-api-secret"
        def verified(provider, model, config, **kwargs):
            self.assertFalse(self.config_path.exists())
            self.assertFalse(self.env_path.exists())
            self.assertEqual(os.environ["OPENAI_API_KEY"], secret)
            self.assertEqual(config["api_key_env"], "OPENAI_API_KEY")
            return self.probe(provider, model, config)
        outcome, check = self.walk(["openai"], [secret], probe=verified)
        self.assertTrue(outcome.checked and outcome.result.ok and outcome.saved)
        check.assert_called_once()
        self.assertEqual(config_mod.load_config()["provider"], "openai")
        for path in (self.config_path, self.env_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn(secret, self.env_path.read_text())
        self.assertNotIn(secret, self.output.getvalue() + self.error.getvalue())

    def test_both_401s_then_cancel_preserves_all_files_credentials_and_environment(self):
        self.existing()
        before, environment = self.snapshot(), dict(os.environ)
        def rejected(provider, model, config, **kwargs):
            return modelcheck.ProbeResult(provider, model, modelcheck.AUTH_FAILED, "HTTP 401")
        with self.assertRaises(KeyboardInterrupt):
            self.walk(["openai", "y", "anthropic", "y", KeyboardInterrupt],
                      ["new-openai-secret", "new-anthropic-secret"], probe=rejected)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(dict(os.environ), environment)
        self.assertNotIn("new-openai-secret", self.output.getvalue())

    def test_declining_failed_setup_keeps_preexisting_files(self):
        self.existing()
        before = self.snapshot()
        def bad(provider, model, config):
            return modelcheck.ProbeResult(provider, model, modelcheck.AUTH_FAILED)
        with self.assertRaises(StartupError) as caught:
            self.walk(["openai", "n"], ["bad-key"], probe=bad)
        self.assertEqual(caught.exception.code, 69)
        self.assertEqual(self.snapshot(), before)

    def test_retry_to_seat_does_not_keep_failed_api_key_or_delete_existing_keys(self):
        self.existing()
        def selected(provider, model, config, **kwargs):
            status = modelcheck.OK if provider == "codex" else modelcheck.AUTH_FAILED
            return modelcheck.ProbeResult(provider, model, status)
        outcome, check = self.walk(["openai", "y", "codex", "n"],
                                   ["bad-new-key"], probe=selected, signed_in=["codex"])
        self.assertEqual(outcome.provider, "codex")
        self.assertEqual(check.call_count, 2)
        self.assertEqual(os.environ["OPENAI_API_KEY"], "old-openai-secret")
        self.assertNotIn("bad-new-key", self.env_path.read_text())
        self.assertIn("send_cwd = false", self.config_path.read_text())
        self.assertEqual(config_mod.load_config()["api_key_env"], "")

    def test_connect_both_seats_and_choose_primary_without_reading_vendor_tokens(self):
        with patch("conch.auth.select", side_effect=AssertionError("no early persistence")), \
                patch("conch.auth.sign_in", side_effect=AssertionError("already signed in")):
            outcome, check = self.walk(["claude-code", "y", "y"],
                                       signed_in=seats.PROVIDERS)
        self.assertEqual(outcome.provider, "codex")
        self.assertEqual(check.call_count, 2)
        self.assertEqual(config_mod.load_config()["provider"], "codex")
        self.assertFalse(self.env_path.exists())

    def test_second_seat_is_optional_and_first_probe_is_not_repeated(self):
        outcome, check = self.walk(["claude-code", "n"], signed_in=["claude-code"])
        self.assertEqual(outcome.provider, "claude-code")
        check.assert_called_once()

    def test_failed_second_seat_does_not_destroy_first_verified_choice(self):
        def selected(provider, model, config, **kwargs):
            return modelcheck.ProbeResult(provider, model,
                                         modelcheck.OK if provider == "claude-code" else modelcheck.TIMEOUT)
        outcome, check = self.walk(["claude-code", "y"], probe=selected,
                                   signed_in=seats.PROVIDERS)
        self.assertEqual(outcome.provider, "claude-code")
        self.assertEqual(check.call_count, 2)

    def test_missing_cli_has_exact_install_and_retry_guidance(self):
        with self.assertRaises(KeyboardInterrupt), \
                patch("conch.auth.sign_in", side_effect=AssertionError("missing binary")):
            self.walk(["claude-code", KeyboardInterrupt])
        self.assertIn("npm install -g @anthropic-ai/claude-code", self.output.getvalue())
        self.assertIn("choose it again", self.output.getvalue())
        self.assertFalse(self.config_path.exists())

    def test_native_sign_in_uses_official_flow_without_selecting_early(self):
        with patch("conch.onboarding.seat_state", return_value=onboarding.SEAT_NEEDS_LOGIN), \
                patch("conch.auth.sign_in", return_value=(0, "")) as login, \
                patch("conch.auth.select", side_effect=AssertionError("no select")):
            staged = onboarding._connect_seat("codex", "ChatGPT seat", {})
        login.assert_called_once_with("codex")
        self.assertEqual(staged.updates["provider"], "codex")
        self.assertFalse(self.config_path.exists())

    def test_custom_keyless_selection_cannot_carry_cloud_api_key(self):
        self.existing()
        def verify(provider, model, config, **kwargs):
            self.assertEqual(config["api_key_env"], "")
            self.assertEqual(config["custom_base_url"], "http://localhost:8080/v1")
            return self.probe(provider, model, config)
        with patch("conch.onboarding.discover_custom_models", return_value=(["local-tools"], True)):
            outcome, _ = self.walk(["custom", "http://localhost:8080/v1"], [""], probe=verify)
        self.assertEqual(outcome.provider, "custom")
        self.assertEqual(config_mod.load_config()["api_key_env"], "")
        self.assertIn("old-openai-secret", self.env_path.read_text())

    def test_higher_priority_route_conflict_is_actionable_and_saves_nothing(self):
        self.existing()
        (self.root / ".conchrc").write_text("provider = openai\n")
        before = self.snapshot()
        with self.assertRaises(StartupError), \
                patch("conch.onboarding.probe_model", side_effect=AssertionError("do not certify shadowed route")):
            onboarding._verify_staged(onboarding._stage("codex", "default"))
        self.assertEqual(self.snapshot(), before)


class TestSetupPersistence(OnboardingCase):
    secret = "new-setup-secret"

    def staged(self):
        setup = onboarding._stage("openai", "new-model")
        setup.keys = {"OPENAI_API_KEY": self.secret, "OPENROUTER_API_KEY": "new-router-secret"}
        outcome = modelcheck.ModelCheckOutcome(
            "openai", "new-model", self.probe("openai", "new-model", {}),
        )
        return setup, outcome

    def prior_state(self):
        self.existing()
        for path, mode in (
            (self.env_path, 0o400),
            (Path(config_mod.get_env_manifest_path()), 0o640),
            (self.config_path, 0o604),
        ):
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
            path.chmod(mode)
        os.environ["OPENAI_API_KEY"] = "inherited-openai-secret"
        os.environ["OPENROUTER_API_KEY"] = ""
        config_mod._ENV_LOAD_STATE["from_file"]["OPENAI_API_KEY"] = "previously-loaded-secret"
        config_mod._ENV_LOAD_STATE["overrode"] = ["ANTHROPIC_API_KEY"]
        config_mod._ENV_LOAD_STATE["shadowed"] = ["UNRELATED_API_KEY"]

    def assert_write_failures_restore(self, failure):
        before, environment = self.snapshot(), dict(os.environ)
        loaded = config_mod.env_load_state()
        replace = os.replace
        for target in (self.env_path, Path(config_mod.get_env_manifest_path()), self.config_path):
            for after_replace in (False, True):
                with self.subTest(target=target.name, after_replace=after_replace):
                    setup, outcome = self.staged()
                    injected = []
                    changed_credentials = []

                    def fail_write(source, destination):
                        if Path(destination) == target and not injected:
                            injected.append(target)
                            if target == self.config_path:
                                changed_credentials.append((
                                    self.env_path.read_text(),
                                    os.environ.get("OPENAI_API_KEY"),
                                    config_mod.env_load_state()["from_file"].get("OPENAI_API_KEY"),
                                ))
                            if after_replace:
                                replace(source, destination)
                            raise failure(self.secret)
                        return replace(source, destination)

                    expected = StartupError if failure is OSError else failure
                    with patch("conch.config.os.replace", side_effect=fail_write), \
                            self.assertRaises(expected) as caught:
                        onboarding._save_setup(setup, outcome)
                    self.assertEqual(injected, [target])
                    if target == self.config_path:
                        self.assertEqual(len(changed_credentials), 1)
                        text, current, from_file = changed_credentials[0]
                        self.assertIn(self.secret, text)
                        self.assertEqual(current, self.secret)
                        self.assertEqual(from_file, self.secret)
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(dict(os.environ), environment)
                    self.assertEqual(config_mod.env_load_state(), loaded)
                    self.assertFalse(outcome.saved)
                    if failure is OSError:
                        self.assertIn("unchanged", str(caught.exception))
                        self.assertNotIn(self.secret, str(caught.exception))
                    self.assertNotIn(self.secret, self.output.getvalue() + self.error.getvalue())

    def test_write_failure_restores_initially_absent_state(self):
        self.assert_write_failures_restore(OSError)

    def test_write_failure_restores_prior_bytes_modes_and_key_state(self):
        self.prior_state()
        self.assert_write_failures_restore(OSError)

    def test_interrupt_restores_initially_absent_state(self):
        self.assert_write_failures_restore(KeyboardInterrupt)

    def test_interrupt_restores_prior_bytes_modes_and_key_state(self):
        self.prior_state()
        self.assert_write_failures_restore(KeyboardInterrupt)

    def test_snapshot_failure_changes_nothing_and_hides_secret_details(self):
        self.prior_state()
        before, environment = self.snapshot(), dict(os.environ)
        loaded = config_mod.env_load_state()
        setup, outcome = self.staged()
        open_file = Path.open

        def unreadable(path, *args, **kwargs):
            if path == self.config_path:
                raise PermissionError(self.secret)
            return open_file(path, *args, **kwargs)

        with patch.object(Path, "open", unreadable), \
                patch("conch.config.os.replace") as replace, \
                self.assertRaises(StartupError) as caught:
            onboarding._save_setup(setup, outcome)
        replace.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(config_mod.env_load_state(), loaded)
        self.assertFalse(outcome.saved)
        self.assertIn("unchanged", str(caught.exception))
        self.assertNotIn(self.secret, str(caught.exception))

    def test_reporting_interruption_rolls_back_completed_writes(self):
        self.prior_state()
        before, environment = self.snapshot(), dict(os.environ)
        loaded = config_mod.env_load_state()
        for failure in (KeyboardInterrupt, EOFError):
            with self.subTest(failure=failure):
                setup, outcome = self.staged()

                def interrupted(*args, **kwargs):
                    self.assertIn(self.secret, self.env_path.read_text())
                    self.assertIn("new-model", self.config_path.read_text())
                    raise failure

                with patch("conch.onboarding._report_saved", side_effect=interrupted), \
                        self.assertRaises(failure):
                    onboarding._save_setup(setup, outcome)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(dict(os.environ), environment)
                self.assertEqual(config_mod.env_load_state(), loaded)
                self.assertFalse(outcome.saved)

    def test_keyless_setup_rolls_back_only_routing(self):
        self.prior_state()
        before, environment = self.snapshot(), dict(os.environ)
        loaded = config_mod.env_load_state()
        setup = onboarding._stage("codex", "default")
        outcome = modelcheck.ModelCheckOutcome("codex", "default", self.probe("codex", "default", {}))
        replace = os.replace
        writes = []

        def interrupted(source, destination):
            writes.append(Path(destination))
            replace(source, destination)
            if len(writes) == 1:
                raise KeyboardInterrupt

        with patch("conch.config.os.replace", side_effect=interrupted), \
                self.assertRaises(KeyboardInterrupt):
            onboarding._save_setup(setup, outcome)
        self.assertEqual(writes, [self.config_path, self.config_path])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(config_mod.env_load_state(), loaded)
        self.assertFalse(outcome.saved)

    def run_command(self, automatic=False):
        states = dict.fromkeys(seats.PROVIDERS, onboarding.SEAT_MISSING)
        results = []
        if self.config_path.exists() and not automatic:
            results.append(modelcheck.ProbeResult("openai", "gpt-4o-mini", modelcheck.AUTH_FAILED))
        results.append(self.probe("openai", "new-model", {}))
        with patch("conch.onboarding._detect", return_value=(states, None)), \
                patch("conch.onboarding.probe_model", side_effect=results), \
                patch("builtins.input", side_effect=["openai"]), \
                patch("conch.onboarding.getpass.getpass", return_value=self.secret):
            if automatic:
                return onboarding.maybe_run_first_run_wizard()
            return onboarding.main([])

    def test_command_cancellation_after_credentials_change_restores_state(self):
        self.existing()
        before, environment = self.snapshot(), dict(os.environ)
        loaded = config_mod.env_load_state()
        replace = os.replace

        def interrupted(source, destination):
            if Path(destination) == self.config_path:
                self.assertEqual(os.environ["OPENAI_API_KEY"], self.secret)
                raise KeyboardInterrupt
            return replace(source, destination)

        with patch("conch.config.os.replace", side_effect=interrupted):
            self.assertEqual(self.run_command(), 130)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(config_mod.env_load_state(), loaded)
        self.assertIn("unchanged", self.error.getvalue())

    def test_first_run_cancellation_removes_new_files_and_key_state(self):
        before, environment = self.snapshot(), dict(os.environ)
        loaded = config_mod.env_load_state()
        replace = os.replace

        def interrupted(source, destination):
            if Path(destination) == self.config_path:
                self.assertEqual(os.environ["OPENAI_API_KEY"], self.secret)
                raise KeyboardInterrupt
            return replace(source, destination)

        with patch("conch.config.os.replace", side_effect=interrupted), \
                self.assertRaises(SystemExit) as caught:
            self.run_command(automatic=True)
        self.assertEqual(caught.exception.code, 130)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(config_mod.env_load_state(), loaded)
        self.assertIn("Setup cancelled", self.output.getvalue())

    def test_rollback_write_failure_reports_partial_state_and_restores_other_resources(self):
        self.existing()
        before, environment = self.snapshot(), dict(os.environ)
        loaded = config_mod.env_load_state()
        replace = os.replace
        rolling_back = []

        def fail_write(source, destination):
            if Path(destination) == self.config_path:
                rolling_back.append(True)
                raise KeyboardInterrupt(self.secret)
            if Path(destination) == self.env_path and rolling_back:
                raise PermissionError(self.secret)
            return replace(source, destination)

        with patch("conch.config.os.replace", side_effect=fail_write):
            self.assertEqual(self.run_command(), modelcheck.MODEL_CHECK_EXIT_CODE)
        after = self.snapshot()
        self.assertIn(self.secret, self.env_path.read_text())
        self.assertEqual({p: value for p, value in after.items() if p != self.env_path},
                         {p: value for p, value in before.items() if p != self.env_path})
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(config_mod.env_load_state(), loaded)
        message = self.error.getvalue()
        self.assertIn("rollback was incomplete", message)
        self.assertIn("credentials may have changed", message)
        self.assertNotIn("unchanged", message)
        self.assertNotIn("cancelled", message)
        self.assertNotIn(self.secret, message + self.output.getvalue())

    def test_rollback_unlink_interruption_reports_partial_first_run_state(self):
        environment, loaded = dict(os.environ), config_mod.env_load_state()
        replace, unlink = os.replace, Path.unlink

        def interrupted_write(source, destination):
            if Path(destination) == self.config_path:
                raise OSError(self.secret)
            return replace(source, destination)

        def interrupted_unlink(path, *args, **kwargs):
            if path == self.env_path:
                raise KeyboardInterrupt(self.secret)
            return unlink(path, *args, **kwargs)

        with patch("conch.config.os.replace", side_effect=interrupted_write), \
                patch.object(Path, "unlink", interrupted_unlink), \
                self.assertRaises(SystemExit) as caught:
            self.run_command(automatic=True)
        self.assertEqual(caught.exception.code, modelcheck.MODEL_CHECK_EXIT_CODE)
        self.assertEqual(set(self.snapshot()), {self.env_path})
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(config_mod.env_load_state(), loaded)
        message = self.error.getvalue()
        self.assertIn("rollback was incomplete", message)
        self.assertNotIn("unchanged", message)
        self.assertNotIn("Setup cancelled", self.output.getvalue())
        self.assertNotIn(self.secret, message + self.output.getvalue())


class TestSetupCommand(OnboardingCase):
    def test_fresh_setup_offers_both_seats_before_any_default_inference(self):
        with patch("conch.onboarding.run_first_run_wizard") as wizard, \
                patch("conch.onboarding.probe_model", side_effect=AssertionError("no unchosen probe")):
            self.assertEqual(onboarding.main([]), 0)
        wizard.assert_called_once()

    def test_good_primary_succeeds_once_without_discovering_or_prompting(self):
        self.existing()
        before = self.snapshot()
        with patch("conch.onboarding.probe_model", side_effect=self.probe) as check, \
                patch("conch.onboarding.run_first_run_wizard", side_effect=AssertionError("already good")), \
                patch("conch.modelcheck.discover_alternatives", side_effect=AssertionError("primary only")), \
                patch("builtins.input", side_effect=AssertionError("no prompt")):
            self.assertEqual(onboarding.main([]), 0)
        check.assert_called_once()
        self.assertEqual(self.snapshot(), before)

    def test_check_is_noninteractive_failure_even_with_good_fallback_and_check_optout(self):
        self.existing()
        config_mod.set_config_values({"model_check": "off", "fallback_models": "codex/default"})
        before = self.snapshot()
        result = modelcheck.ProbeResult("openai", "gpt-4o-mini", modelcheck.AUTH_FAILED, "HTTP 401")
        with patch("conch.onboarding.probe_model", return_value=result) as check, \
                patch("conch.onboarding.run_first_run_wizard", side_effect=AssertionError("--check never recovers")), \
                patch("builtins.input", side_effect=AssertionError("no prompt")), \
                patch("conch.auth.sign_in", side_effect=AssertionError("no login")):
            self.assertEqual(onboarding.main(["--check"]), 69)
        check.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_local_only_cloud_config_never_transmits_credentials(self):
        self.existing()
        config_mod.set_config_values({"local_only": "true"})
        with patch("conch.onboarding.probe_model", side_effect=AssertionError("no cloud probes")):
            self.assertEqual(onboarding.main(["--check"]), 69)
        with patch("conch.modelcheck._request", side_effect=AssertionError("no HTTP")):
            result = modelcheck.probe_model("openai", "gpt-4o-mini", config_mod.load_config())
        self.assertEqual(result.status, modelcheck.MISCONFIGURED)

    def test_check_reports_invalid_primary_even_if_an_alternative_could_work(self):
        self.existing()
        bad = modelcheck.ProbeResult("openai", "gpt-4o-mini", modelcheck.AUTH_FAILED)
        with patch("conch.onboarding.probe_model", return_value=bad) as probe, \
                patch("conch.modelcheck.discover_alternatives", side_effect=AssertionError("no discovery")), \
                patch("builtins.input", side_effect=AssertionError("no prompts")):
            self.assertEqual(onboarding.main(["--check"]), 69)
        probe.assert_called_once()

    def test_non_tty_invalid_primary_fails_without_recovery(self):
        bad = modelcheck.ProbeResult("openai", "gpt-4o-mini", modelcheck.AUTH_FAILED)
        with patch("conch.onboarding.probe_model", return_value=bad), \
                patch("sys.stdin", io.StringIO()), \
                patch("conch.auth.sign_in", side_effect=AssertionError("no pipes")), \
                patch("builtins.input", side_effect=AssertionError("no prompts")):
            self.assertEqual(onboarding.main([]), 69)

    def test_stale_auth_command_runs_wizard_once_and_uses_its_verified_outcome(self):
        self.existing()
        bad = modelcheck.ProbeResult("openai", "gpt-4o-mini", modelcheck.AUTH_FAILED)
        good = modelcheck.ModelCheckOutcome("codex", "default", self.probe("codex", "default", {}))
        with patch("conch.onboarding.probe_model", return_value=bad) as check, \
                patch("conch.onboarding.run_first_run_wizard", return_value=good) as wizard:
            self.assertEqual(onboarding.main([]), 0)
        check.assert_called_once()
        wizard.assert_called_once()

    def test_cancelled_command_is_nonzero_and_preserves_state(self):
        self.existing()
        before = self.snapshot()
        bad = modelcheck.ProbeResult("openai", "gpt-4o-mini", modelcheck.AUTH_FAILED)
        with patch("conch.onboarding.probe_model", return_value=bad), \
                patch("conch.onboarding.run_first_run_wizard", side_effect=KeyboardInterrupt):
            self.assertEqual(onboarding.main([]), 130)
        self.assertEqual(self.snapshot(), before)

    def test_actual_probe_does_not_echo_401_body_or_key_and_distinguishes_404(self):
        self.existing()
        for code, status in ((401, modelcheck.AUTH_FAILED), (404, modelcheck.MODEL_NOT_FOUND)):
            with self.subTest(code=code), \
                    patch("conch.modelcheck._request", return_value=(code, {
                        "error": {"message": "Authorization: Bearer old-openai-secret RAW-AUTH-BODY"}})):
                result = modelcheck.probe_model("openai", "gpt-4o-mini", config_mod.load_config())
            self.assertEqual(result.status, status)
            self.assertNotIn("old-openai-secret", result.detail)
            self.assertNotIn("RAW-AUTH-BODY", result.detail)
            if code == 404:
                self.assertNotIn("key", result.detail.lower())


class TestStartupSetupRecovery(OnboardingCase):
    def recovery(self, candidates, answers):
        config = {"provider": "openai", "model": "gpt-4o-mini", "chat_model": "gpt-4o-mini"}
        bad = modelcheck.ProbeResult("openai", "gpt-4o-mini", modelcheck.AUTH_FAILED, "HTTP 401")
        good = modelcheck.ModelCheckOutcome("codex", "default", self.probe("codex", "default", {}))
        with patch("conch.modelcheck.probe_model", return_value=bad), \
                patch("conch.modelcheck.discover_alternatives", return_value=(candidates, [])), \
                patch("conch.modelcheck.probe_candidates"), \
                patch("conch.onboarding.run_first_run_wizard", return_value=good) as wizard, \
                patch("conch.onboarding.load_config", return_value={"provider": "codex", "model": "default", "chat_model": "default", "api_key_env": ""}), \
                patch("builtins.input", side_effect=answers):
            outcome = modelcheck.ensure_working_model(config, interactive=True)
        wizard.assert_called_once()
        self.assertTrue(outcome.switched)
        self.assertEqual(config["provider"], "codex")
        return outcome

    def test_all_invalid_alternatives_offer_setup_not_failed_menu(self):
        candidate = modelcheck.Candidate("anthropic", "claude-sonnet-5", "provider",
                                         result=modelcheck.ProbeResult("anthropic", "claude-sonnet-5", modelcheck.AUTH_FAILED))
        self.recovery([candidate], [""])
        self.assertIn("No verified alternatives", self.output.getvalue())
        self.assertNotIn("Switch to [", self.output.getvalue())

    def test_no_alternatives_can_reconnect_seats(self):
        self.recovery([], ["s"])

    def test_working_alternative_does_not_hide_seat_setup(self):
        candidate = modelcheck.Candidate("ollama", "local", "ollama",
                                         result=self.probe("ollama", "local", {}))
        self.recovery([candidate], ["s"])

    def test_runtime_check_optout_does_not_probe_or_prompt(self):
        with patch("conch.modelcheck.probe_model", side_effect=AssertionError("no probes")), \
                patch("builtins.input", side_effect=AssertionError("no prompt")):
            outcome = modelcheck.ensure_working_model({"provider": "ollama", "model": "local", "model_check": "off"}, interactive=True)
        self.assertFalse(outcome.checked)


class TestNativeAuth(OnboardingCase):
    def test_official_status_and_login_commands(self):
        self.assertEqual(seats.transport("codex").auth_command("status"),
                         ["codex", "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"', "login", "status"])
        claude = seats.transport("claude-code").auth_command("login")
        self.assertEqual(claude[-3:], ["auth", "login", "--claudeai"])
        self.assertNotIn("--api-key", claude)

    def test_failed_native_login_does_not_save_route(self):
        for native, status, expected in ((Mock(returncode=7), True, 7), (Mock(returncode=0), False, 1)):
            with patch("conch.auth.subprocess.run", return_value=native), \
                    patch("conch.auth.status", return_value=(status, "dummy status")):
                self.assertEqual(auth.login("codex"), expected)
            self.assertFalse(self.config_path.exists())

    def test_status_failure_is_sign_in_needed(self):
        with patch("conch.auth.cli_installed", return_value=True), \
                patch("conch.seats.status", side_effect=RuntimeError("private failure")):
            self.assertEqual(onboarding.seat_state("codex"), onboarding.SEAT_NEEDS_LOGIN)


class TestDummyNativeStatus(OnboardingCase):
    def test_official_cli_status_with_isolated_dummy_binaries(self):
        import json
        import shlex

        binary_dir = self.root / "bin"
        binary_dir.mkdir()
        responses = {
            "codex": "Logged in using ChatGPT",
            "claude": json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                   "apiProvider": "firstParty", "subscriptionType": "pro"}),
        }
        for binary, response in responses.items():
            path = binary_dir / binary
            path.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(response) + "\n")
            path.chmod(0o700)
        with patch.dict(os.environ, {"PATH": str(binary_dir) + ":/usr/bin:/bin"}):
            for provider in seats.PROVIDERS:
                with self.subTest(provider=provider):
                    ok, detail = seats.status(provider, timeout=1)
                    self.assertTrue(ok, detail)
                    self.assertNotIn("subscriptionType", detail)


class TestEnvFileLoading(OnboardingCase):
    def test_env_file_precedence_by_provenance(self):
        config_mod._ENV_LOAD_STATE["from_file"].clear()
        config_mod.set_env_values({"OPENAI_API_KEY": "sk-from-file"})
        del os.environ["OPENAI_API_KEY"]
        config_mod.load_env_file()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-from-file")
        os.environ["OPENAI_API_KEY"] = "sk-real-env"
        config_mod.load_env_file()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-from-file")
        config_mod.forget_managed_key("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "sk-real-env"
        config_mod._ENV_LOAD_STATE["from_file"].clear()
        config_mod.load_env_file()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-real-env")

    def test_export_prefix_tolerated(self):
        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        self.env_path.write_text("export OPENROUTER_API_KEY='sk-or-1'\n")
        config_mod.load_env_file()
        self.assertEqual(os.environ["OPENROUTER_API_KEY"], "sk-or-1")


class TestConfigWriter(OnboardingCase):
    def test_create_update_preserve(self):
        config_mod.set_config_values({"provider": "openai"}, header="# created by test")
        self.config_path.write_text(
            self.config_path.read_text() + "# my comment\nmodel = gpt-4o-mini\n"
        )
        config_mod.set_config_values({"provider": "anthropic", "edge_daemon": "true"})
        text = self.config_path.read_text()
        self.assertIn("provider = anthropic", text)
        self.assertNotIn("provider = openai", text)
        self.assertIn("# my comment", text)
        self.assertIn("model = gpt-4o-mini", text)
        self.assertIn("edge_daemon = true", text)


if __name__ == "__main__":
    unittest.main()
