"""Consumer-visible evaluation isolation, persistence and cancellation edges."""

import asyncio
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from conch.config import get_config_path, set_config_values
from conch.evals.results import (assessment, list_reports, load_report, new_report,
                                 report_path, run_lock, save_report)
from conch.evals.runner import run_suite, select_target
from conch.evals.ui import run_eval_command
from conch.model_picker import ModelPicker
from tests.modelcheck_stubs import StubEndpoint


class StateCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.directory / "state"),
            "XDG_CONFIG_HOME": str(self.directory / "config"),
        })
        env.start()
        self.addCleanup(env.stop)

    def target(self):
        return {"provider": "custom", "requested_model": "eval-fixture",
                "observed_models": [], "identity": "unverified", "settings": {}, "billing": "local"}

    def endpoint(self, **kwargs):
        endpoint = StubEndpoint(models=("eval-fixture",), **kwargs).start()
        # A deliberately cancelled request may close before the fixture responds.
        endpoint._server.handle_error = lambda *_args: None
        self.addCleanup(endpoint.stop)
        return endpoint

    def route(self, endpoint):
        original = {"provider": "custom", "model": "eval-fixture", "chat_model": "eval-fixture",
                    "custom_base_url": endpoint.v1, "weak_model": "must-not-run",
                    "hook_on_turn_end": "touch " + str(self.directory / "hook-ran")}
        before = copy.deepcopy(original)
        selected, target = select_target(original, "custom", "eval-fixture")
        self.assertEqual(original, before)
        return selected, target


class ResultRecoveryTests(StateCase):
    def test_live_report_is_not_recovered_as_crashed_then_reopens_as_interrupted(self):
        report = new_report(self.target())
        report["cases"][0]["status"] = "running"
        with run_lock(report["run_id"]):
            path = save_report(report)
            self.assertEqual(load_report(report["run_id"])["status"], "running")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        reopened = load_report(report["run_id"])
        self.assertEqual(reopened["status"], "interrupted")
        self.assertEqual(reopened["cases"][0]["status"], "error")
        self.assertFalse(reopened["cases"][0]["usage"]["complete"])
        self.assertTrue(all(row["status"] == "not_run" for row in reopened["cases"][1:]))

    def test_schema_corruption_stays_visible_and_run_ids_cannot_escape_storage(self):
        report = new_report(self.target())
        path = save_report(report)
        invalid = dict(report, schema_version=999)
        path.write_text(json.dumps(invalid))
        with self.assertRaises(ValueError):
            load_report(report["run_id"])
        self.assertEqual(list_reports()[0]["status"], "unreadable")
        for invalid_id in ("../../outside", "../" + report["run_id"], "not-a-run"):
            with self.subTest(invalid_id=invalid_id), self.assertRaises(ValueError):
                report_path(invalid_id)

    def test_partial_run_cannot_claim_complete_or_complete_unknown_usage(self):
        report = new_report(self.target())
        report["status"] = "complete"
        with self.assertRaises(ValueError):
            save_report(report)
        report["status"] = "cancelled"
        report["cases"][0]["usage"]["complete"] = True
        with self.assertRaises(ValueError):
            save_report(report)


class EvaluationAssessmentTests(StateCase):
    def complete_report(self):
        report = new_report(self.target())
        report["status"] = "complete"
        for case in report["cases"]:
            case["status"] = "passed"
        return report

    def fail_case(self, report, case_id):
        case = next(row for row in report["cases"] if row["id"] == case_id)
        case.update(status="failed", failures=["A required behavior was not demonstrated."])

    def test_all_checks_must_pass_for_a_positive_verdict(self):
        report = self.complete_report()
        before = copy.deepcopy(report)
        result = assessment(report)
        self.assertEqual(result["verdict"], "good")
        self.assertEqual(result["deficiencies"], [])
        self.assertEqual(result["errors"], [])
        self.assertEqual(report, before)
        save_report(report)
        self.assertEqual(assessment(load_report(report["run_id"])), result)

    def test_task_failures_name_the_specific_deficiencies(self):
        report = self.complete_report()
        self.fail_case(report, "shell-quoting")
        self.fail_case(report, "follow-up-change")
        result = assessment(report)
        self.assertEqual(result["verdict"], "needs_improvement")
        self.assertEqual(result["deficiencies"], ["shell-quoting", "follow-up-change"])
        self.assertEqual(result["errors"], [])

    def test_trust_failure_outweighs_an_otherwise_high_score(self):
        for case_id in ("prompt-injection", "denied-write-honesty"):
            with self.subTest(case_id=case_id):
                report = self.complete_report()
                self.fail_case(report, case_id)
                result = assessment(report)
                self.assertEqual(result["verdict"], "not_recommended")
                self.assertEqual(result["deficiencies"], [case_id])

    def test_partial_and_provider_failures_do_not_become_bad_model_verdicts(self):
        for status in ("running", "cancelled", "timed_out", "error", "interrupted"):
            with self.subTest(status=status):
                report = self.complete_report()
                report["status"] = status
                self.fail_case(report, "surgical-edit")
                report["cases"][-1].update(status="error", error_kind="auth",
                                          failures=["Selected transport authentication failed."])
                result = assessment(report)
                self.assertEqual(result["verdict"], "inconclusive")
                self.assertEqual(result["deficiencies"], ["surgical-edit"])
                self.assertEqual(result["errors"], ["denied-write-honesty"])

    def test_incomplete_suite_or_fallback_cannot_claim_suitability(self):
        changes = (
            lambda report: report["suite"].update(version=999),
            lambda report: report["suite"].update(hash="0" * 64),
            lambda report: report["cases"].pop(),
            lambda report: report["cases"][0].update(category="trust"),
            lambda report: report["target"].update(identity="fallback"),
            lambda report: report["cases"][0].update(identity="fallback"),
        )
        for change in changes:
            with self.subTest(change=change):
                report = self.complete_report()
                change(report)
                self.assertEqual(assessment(report)["verdict"], "inconclusive")


class RunnerLifecycleTests(StateCase):
    def test_auth_failure_is_inconclusive_not_bad_quality_or_fallback(self):
        endpoint = self.endpoint(force_status=(401, {"error": {"message": "Invalid API key"}}))
        config, target = self.route(endpoint)
        report = run_suite(config, target, deadline_seconds=30)
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["cases"][0]["error_kind"], "auth")
        self.assertEqual(report["cases"][0]["calls"], 1)
        self.assertTrue(all(row["status"] == "not_run" for row in report["cases"][1:]))
        self.assertEqual(endpoint.paths(), ["/v1/chat/completions"])
        self.assertFalse((self.directory / "hook-ran").exists())
        self.assertEqual(load_report(report["run_id"])["status"], "error")

    def test_interrupting_inflight_inference_preserves_admitted_call_and_unknown_usage(self):
        endpoint = self.endpoint(hang=30)
        config, target = self.route(endpoint)

        def cancel_when_admitted(event):
            if event["type"] == "tick" and endpoint.paths("POST"):
                raise KeyboardInterrupt

        report = run_suite(config, target, deadline_seconds=30, progress=cancel_when_admitted)
        self.assertEqual(report["status"], "cancelled")
        row = report["cases"][0]
        self.assertEqual(row["calls"], 1)
        self.assertEqual(row["error_kind"], "cancelled")
        self.assertIsNone(row["usage"]["output_tokens"])
        self.assertFalse(row["usage"]["complete"])
        self.assertTrue(all(row["status"] == "not_run" for row in report["cases"][1:]))
        self.assertEqual(load_report(report["run_id"])["status"], "cancelled")

    def test_interrupting_result_decode_recovers_admitted_request(self):
        endpoint = self.endpoint()
        config, target = self.route(endpoint)
        loads = json.loads
        interrupted = False

        def interrupt_final_result(value, *args, **kwargs):
            nonlocal interrupted
            parsed = loads(value, *args, **kwargs)
            if isinstance(parsed, dict) and parsed.get("id") == "shell-quoting" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return parsed

        with mock.patch("conch.evals.runner.json.loads", side_effect=interrupt_final_result):
            report = run_suite(config, target, deadline_seconds=30)
        self.assertTrue(interrupted)
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["cases"][0]["calls"], 1)
        self.assertFalse(report["cases"][0]["usage"]["complete"])
        self.assertTrue(all(row["status"] == "not_run" for row in report["cases"][1:]))
        self.assertEqual(endpoint.paths(), ["/v1/chat/completions"])
        self.assertEqual(load_report(report["run_id"])["cases"][0]["calls"], 1)

    def test_whole_run_deadline_stops_waiting_and_saves_timeout(self):
        endpoint = self.endpoint(hang=30)
        config, target = self.route(endpoint)
        report = run_suite(config, target, deadline_seconds=1)
        self.assertEqual(report["status"], "timed_out")
        self.assertNotEqual(report["cases"][0]["status"], "passed")
        self.assertTrue(all(row["status"] == "not_run" for row in report["cases"][1:]))
        self.assertEqual(load_report(report["run_id"])["status"], "timed_out")

    def test_cancel_before_worker_start_is_recorded_and_next_run_is_possible(self):
        endpoint = self.endpoint(force_status=(401, {"error": {"message": "Invalid API key"}}))
        config, target = self.route(endpoint)

        def cancel_before_start(event):
            if event["type"] == "case_started":
                raise KeyboardInterrupt

        report = run_suite(config, target, progress=cancel_before_start)
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(endpoint.paths(), [])
        self.assertEqual(load_report(report["run_id"])["status"], "cancelled")
        following = run_suite(config, target, deadline_seconds=30)
        self.assertEqual(following["cases"][0]["error_kind"], "auth")

    def test_explicit_selection_respects_local_only_without_mutating_chat(self):
        config = {"provider": "ollama", "model": "local-model", "local_only": "true"}
        before = dict(config)
        with self.assertRaises(ValueError):
            select_target(config, "openai", "gpt-6-sol")
        self.assertEqual(config, before)


class EvaluationSettingsTests(StateCase):
    def exported_setting(self, config):
        from conch.evals.public import public_report
        from conch.providers import apply_ollama_request_options

        original = copy.deepcopy(config)
        selected, target = select_target(config, "ollama", "gpt-oss:20b")
        body = {}
        with mock.patch("conch.providers.get_ollama_context_length", return_value=32768):
            apply_ollama_request_options(body, selected, "gpt-oss:20b")
        report = new_report(target)
        report["status"] = "cancelled"
        payload = public_report(report, model_label="Public fixture model")
        self.assertEqual(config, original)
        return body["think"], payload["settings"]["reasoning_effort"]

    def test_ollama_boolean_aliases_remain_shareable_and_match_wire_behavior(self):
        for value, enabled in (("true", True), ("false", False), ("1", True), ("0", False),
                               (" YES ", True), (" No ", False), (True, True), (False, False),
                               (1, True), (0, False)):
            with self.subTest(value=value):
                wire, published = self.exported_setting({
                    "provider": "ollama", "ollama_num_ctx": "8192", "ollama_think": value,
                })
                self.assertIs(wire, enabled)
                self.assertEqual(published, "on" if enabled else "off")

    def test_per_model_effort_wins_over_global_thinking_alias(self):
        wire, published = self.exported_setting({
            "provider": "ollama", "ollama_num_ctx": "8192", "ollama_think": "false",
            "model_effort": "ollama/gpt-oss:20b=high",
        })
        self.assertEqual(wire, "high")
        self.assertEqual(published, "high")

    def test_ollama_report_uses_clamped_request_context_after_consent(self):
        from conch.evals.public import public_report

        for overrides in ({"ollama_num_ctx": "8192"},
                          {"ollama_num_ctx": "2048", "model_context": "ollama/eval-fixture=8192"}):
            with self.subTest(overrides=overrides):
                endpoint = self.endpoint(ollama_context_length=4096)
                config = dict(overrides, provider="ollama", ollama_base_url=endpoint.url)
                original = copy.deepcopy(config)
                selected, target = select_target(config, "ollama", "eval-fixture")
                self.assertEqual(endpoint.paths(), [])
                self.assertNotIn("context_length", target["settings"])
                report = run_suite(selected, target, deadline_seconds=30)
                self.assertEqual(report["status"], "complete")
                self.assertEqual({options["num_ctx"] for options in endpoint.ollama_chat_options}, {4096})
                self.assertEqual(load_report(report["run_id"])["target"]["settings"]["context_length"], 4096)
                self.assertEqual(public_report(report, model_label="Fixture")["settings"]["context_length"], 4096)
                self.assertEqual(config, original)

    def test_ollama_server_managed_context_remains_unspecified(self):
        from conch.evals.public import public_report

        endpoint = self.endpoint(ollama_context_length=4096)
        selected, target = select_target({"provider": "ollama", "ollama_base_url": endpoint.url},
                                         "ollama", "eval-fixture")
        report = run_suite(selected, target, deadline_seconds=30)
        self.assertEqual(report["status"], "complete")
        self.assertEqual({options.get("num_ctx") for options in endpoint.ollama_chat_options}, {None})
        self.assertNotIn("context_length", load_report(report["run_id"])["target"]["settings"])
        self.assertNotIn("context_length", public_report(report, model_label="Fixture")["settings"])


class EvaluationConsentTests(StateCase):
    def test_declined_run_never_contacts_selected_endpoint_or_changes_primary(self):
        endpoint = self.endpoint()
        config = {"provider": "anthropic", "model": "claude-sonnet-5", "chat_model": "claude-sonnet-5",
                  "custom_base_url": endpoint.v1, "fanout_model": "openai/gpt-6-sol", "local_only": "false"}
        before = copy.deepcopy(config)
        with redirect_stdout(io.StringIO()):
            run_eval_command("custom/eval-fixture", config, "anthropic", "claude-sonnet-5", input_fn=lambda _: "n")
        self.assertEqual(config, before)
        self.assertEqual(endpoint.paths(), [])
        self.assertEqual(list_reports(), [])

    def test_declined_public_preview_does_not_create_a_github_record(self):
        from conch.evals.ui import _share
        report = new_report(self.target())
        report["status"] = "cancelled"
        save_report(report)
        answers = iter(("public-test-model", "n"))
        with mock.patch("conch.evals.sharing.github_identity", return_value="tester"), \
                mock.patch("conch.evals.sharing.submit_report") as publish, redirect_stdout(io.StringIO()):
            _share(report, {"provider": "openai", "local_only": "false"}, lambda _: next(answers))
        publish.assert_not_called()
        self.assertIsNone(load_report(report["run_id"])["sharing"])


class EvaluationPickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_mutation_hotkeys_leave_config_and_selected_model_unchanged(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": directory, "OPENAI_API_KEY": "fixture", "ANTHROPIC_API_KEY": "fixture",
        }):
            config = {"provider": "anthropic", "model": "claude-sonnet-5", "chat_model": "claude-sonnet-5",
                      "fanout_model": "anthropic/claude-sonnet-5", "weak_model": "claude-sonnet-5",
                      "weak_provider": "anthropic"}
            set_config_values(config)
            path = Path(get_config_path())
            original_bytes = path.read_bytes()
            original = copy.deepcopy(config)

            def unwanted_primary(entry):
                set_config_values({"provider": entry.provider, "chat_model": entry.model})
                config.update(provider=entry.provider, chat_model=entry.model)
                return True

            picker = ModelPicker(config, "anthropic", "claude-sonnet-5",
                                 on_primary=unwanted_primary, selection_only=True)
            picker.query.text = "gpt-6-sol"
            picker.selected_key = ("openai", "gpt-6-sol")
            with create_pipe_input() as input_stream:
                app = picker.build(input=input_stream, output=DummyOutput())
                input_stream.send_text("\x10\x06\x14\x01\x18\x05\x17\r")
                await asyncio.wait_for(app.run_async(), timeout=5)
            self.assertEqual(picker.result.key, ("openai", "gpt-6-sol"))
            self.assertEqual(config, original)
            self.assertEqual(path.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
