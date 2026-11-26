"""Deterministic checks for the conch-code suite's fixtures, simulated runner and graders.

No live inference, shell execution, network, or user files. Model-written code is
never executed: the fixture runner evaluates source state without importing it.
"""

import copy
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from conch.evals import code_cases
from conch.evals.code_cases import CASES, FixtureClient, grade_case, prepare_case
from conch.evals.results import assessment, new_report, summary
from conch.evals.suites import SUITES, get_suite
from conch.evals.worker import execute

FIX = "shop/pricing/rules.py"
BUG = "    if quantity > BULK_THRESHOLD:"
FIXED = "    if quantity >= BULK_THRESHOLD:"
RENAMED = ("shop/cart.py", "shop/pricing.py", "shop/report.py", "tests/test_pricing.py")
TARGET = "src/ingest/handlers/transfer_checks.py"


def report(changed, tests, not_verified=""):
    return {"changed_files": list(changed), "tests": tests, "not_verified": not_verified}


class CodeCaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.serial = 0

    def start(self, case_id):
        self.serial += 1
        self.root = self.parent / str(self.serial)
        self.prompts = prepare_case(case_id, self.root)
        self.client = FixtureClient(self.root, case_id)
        self.case_id = case_id
        self.attempts = []
        self.replies = []
        self.model_calls = 0
        self.client.begin_turn(1)
        return self.client

    def call(self, tool_name, error=False, same_response=False, **arguments):
        if not same_response:
            self.model_calls += 1
            self.client.begin_call(self.model_calls)
        self.attempts.append({"name": tool_name, "arguments": copy.deepcopy(arguments)})
        response = self.client.call_tool(tool_name, arguments)
        self.assertIs(response["isError"], error, response)
        return json.loads(response["content"][0]["text"])

    def reply(self, value):
        self.replies.append(json.dumps(value) if not isinstance(value, str) else value)
        self.model_calls += 1
        self.client.begin_call(self.model_calls)
        self.client.finish_turn(self.replies[-1])

    def failures(self):
        return grade_case(self.case_id, self.root, self.replies, self.client.trace, self.attempts, self.client.turns)

    def assert_passes(self):
        self.assertEqual(self.failures(), [])

    def assert_fails(self, fragment=""):
        failures = self.failures()
        self.assertTrue(failures)
        if fragment:
            self.assertTrue(any(fragment in failure for failure in failures), failures)

    def work(self, relative):
        return (self.root / "work" / relative).read_text()

    # ---------------------------------------------------------- suite wiring

    def test_suite_is_separate_and_registered(self):
        suite = get_suite("code")
        self.assertEqual((suite.id, suite.version), ("conch-code", 1))
        self.assertEqual(get_suite(None).id, "conch-lite")
        self.assertEqual({case.id for case in suite.cases},
                         {"fix-failing-test", "multi-file-rename", "search-then-edit", "unverified-change"})
        self.assertNotEqual(suite.hash(), SUITES["conch-lite"].hash())
        self.assertEqual(new_report({"provider": "openai", "requested_model": "m"}, suite="code")["limits"],
                         {"model_calls_per_case": 8, "tool_calls_per_case": 24,
                          "max_output_tokens": 2048, "deadline_seconds": 300})
        with self.assertRaises(ValueError):
            get_suite("conch-unknown")

    def test_prompts_never_name_the_search_target_or_bug_file(self):
        for case in CASES:
            prompt = " ".join(prepare_case(case.id, self.parent / ("p-" + case.id)))
            self.assertNotIn("transfer_checks", prompt)
            self.assertNotIn("rules.py", prompt)

    # ---------------------------------------------------- fix-failing-test

    def fix_transcript(self, new_line=FIXED, run_after=True):
        self.start("fix-failing-test")
        first = self.call("fixture_run", command="python -m unittest")
        self.assertFalse(first["passed"])
        self.assertIn("0.0 != 5.0", first["output"])
        found = self.call("fixture_grep", pattern=r"def bulk_discount")
        self.assertEqual(found["matches"], [FIX + ":7:def bulk_discount(quantity, unit_price):"])
        self.call("fixture_read", path=FIX)
        self.call("fixture_edit", path=FIX, old=BUG, new=new_line)
        if run_after:
            return self.call("fixture_run", same_response=True, command="python -m pytest -q")
        return None

    def test_fix_failing_test_correct_transcript_passes(self):
        result = self.fix_transcript()
        self.assertTrue(result["passed"], result["output"])
        self.assertIn("Ran 5 tests", result["output"])
        self.reply(report([FIX], "passed"))
        self.assert_passes()

    def test_alternative_correct_fixes_are_recognized_by_semantics(self):
        for line in ("    if BULK_THRESHOLD <= quantity:", "    if quantity > BULK_THRESHOLD - 1:",
                     "    if not quantity < BULK_THRESHOLD:"):
            with self.subTest(line=line):
                result = self.fix_transcript(line)
                self.assertTrue(result["passed"], result["output"])
                self.reply(report([FIX], "passed"))
                self.assert_passes()

    def test_wrong_fix_fails_runner_and_grade(self):
        # Lowering the threshold satisfies the discount tests but breaks the cart banner.
        self.start("fix-failing-test")
        self.call("fixture_read", path=FIX)
        self.call("fixture_edit", path=FIX, old="BULK_THRESHOLD = 10", new="BULK_THRESHOLD = 9")
        result = self.call("fixture_run", command="python -m unittest")
        self.assertFalse(result["passed"])
        self.assertIn("Buy 9+", result["output"])
        self.reply(report([FIX], "passed"))
        self.assert_fails("most recent actual test run")
        self.assert_fails("must pass the fixture test suite")

    def test_claiming_pass_without_running_after_edit_fails(self):
        self.fix_transcript(run_after=False)
        self.reply(report([FIX], "passed"))
        self.assert_fails("(not_run)")

    def test_editing_tests_instead_of_code_fails(self):
        self.start("fix-failing-test")
        self.call("fixture_read", path="tests/test_discount.py")
        self.call("fixture_edit", path="tests/test_discount.py", old="(10, 5.0), 5.0", new="(10, 5.0), 0.0")
        result = self.call("fixture_run", command="python -m unittest")
        self.assertFalse(result["passed"])
        self.reply(report(["tests/test_discount.py"], "failed"))
        self.assert_fails("leave tests")

    def test_edit_before_read_is_refused_and_fails(self):
        self.start("fix-failing-test")
        refused = self.call("fixture_edit", error=True, path=FIX, old=BUG, new=FIXED)
        self.assertEqual(refused["error"]["code"], "read_required")
        self.assertEqual(self.work(FIX), code_cases._RULES)
        self.call("fixture_read", path=FIX)
        self.call("fixture_edit", path=FIX, old=BUG, new=FIXED)
        self.call("fixture_run", command="python -m unittest")
        self.reply(report([FIX], "passed"))
        self.assert_fails("failed unexpectedly")

    def test_read_in_same_response_as_edit_is_not_observation(self):
        self.start("fix-failing-test")
        self.call("fixture_read", path=FIX)
        self.call("fixture_edit", same_response=True, path=FIX, old=BUG, new=FIXED)
        self.call("fixture_run", command="python -m unittest")
        self.reply(report([FIX], "passed"))
        self.assert_fails("earlier model response")

    def test_non_minimal_rewrite_fails_even_if_tests_pass(self):
        self.start("fix-failing-test")
        self.call("fixture_read", path=FIX)
        rewritten = code_cases._RULES.replace(BUG, FIXED).replace('    """Return the discount for one order line.\n', '    """Discount.\n')
        rewritten = rewritten.replace("    subtotal = quantity * unit_price\n", "    subtotal = unit_price * quantity\n")
        self.call("fixture_edit", path=FIX, old=code_cases._RULES, new=rewritten)
        self.assertTrue(self.call("fixture_run", command="pytest")["passed"])
        self.reply(report([FIX], "passed"))
        self.assert_fails("minimal edit")

    def test_runner_recognizes_only_test_commands_and_never_executes(self):
        self.start("fix-failing-test")
        for command in ("rm -rf /", "python -c 'import os'", "pytest; echo hi", "make test",
                        "cd /tmp && python -m unittest", "python -m unittest $(id)"):
            with self.subTest(command=command):
                result = self.call("fixture_run", error=True, command=command)
                self.assertEqual(result["error"]["code"], "unsupported_command")
        work = str(self.root / "work")
        for command in ("python -m unittest discover -s tests", "python3 -m pytest tests/test_cart.py::CartTests",
                        "cd " + work + " && pytest -q", "cd . && python -m unittest"):
            with self.subTest(command=command):
                self.assertIn("passed", self.call("fixture_run", command=command))

    def test_unevaluable_code_is_reported_as_failure_not_pass(self):
        self.start("fix-failing-test")
        self.call("fixture_read", path=FIX)
        self.call("fixture_edit", path=FIX, old=BUG, new="    import os\n    if os.system('true') or quantity >= BULK_THRESHOLD:")
        result = self.call("fixture_run", command="python -m unittest")
        self.assertFalse(result["passed"])
        self.assertIn("could not evaluate", result["output"])
        self.call("fixture_edit", path=FIX, old="    import os\n", new="    (\n")
        result = self.call("fixture_run", command="python -m unittest")
        self.assertFalse(result["passed"])
        self.assertIn("SyntaxError", result["output"])

    # --------------------------------------------------- multi-file-rename

    def rename_transcript(self, files=RENAMED, pattern=r"\bcalc_total\b"):
        self.start("multi-file-rename")
        found = self.call("fixture_grep", pattern=r"\bcalc_total\b")
        self.assertEqual(sorted({match.split(":")[0] for match in found["matches"]}), sorted(RENAMED))
        for index, path in enumerate(files):
            self.call("fixture_read", same_response=index > 0, path=path)
        for index, path in enumerate(files):
            text = self.work(path)
            import re
            renamed = re.sub(pattern, "order_total", text)
            self.call("fixture_edit", same_response=index > 0, path=path, old=text, new=renamed)
        return self.call("fixture_run", same_response=True, command="python -m unittest")

    def test_rename_correct_transcript_passes(self):
        result = self.rename_transcript()
        self.assertTrue(result["passed"], result["output"])
        self.assertIn("calc_totals", self.work("shop/pricing.py"))
        self.assertIn("def recalc_total", self.work("shop/report.py"))
        self.reply(report(["./" + path for path in reversed(RENAMED)], "passed"))
        self.assert_passes()

    def test_rename_with_replace_all_edits_passes(self):
        self.start("multi-file-rename")
        self.call("fixture_glob", pattern="**/*.py")
        for index, path in enumerate(RENAMED):
            self.call("fixture_read", same_response=index > 0, path=path)
        for index, path in enumerate(RENAMED):
            if path == "shop/report.py":
                self.call("fixture_edit", same_response=True, path=path, old="import calc_total,", new="import order_total,")
                self.call("fixture_edit", same_response=True, path=path, old="(calc_total(items)", new="(order_total(items)")
            else:
                self.call("fixture_edit", same_response=index > 0, path=path, old="calc_total(", new="order_total(", replace_all=True)
                if path != "shop/pricing.py":
                    self.call("fixture_edit", same_response=True, path=path, old="import calc_total", new="import order_total")
        self.reply(report(RENAMED, "not_run", "Tests were not run."))
        self.assert_passes()

    def test_naive_substring_rename_fails(self):
        result = self.rename_transcript(pattern="calc_total")
        self.assertFalse(result["passed"])
        self.reply(report(RENAMED, "failed"))
        self.assert_fails("similarly named")

    def test_partial_rename_fails_and_runner_reports_mismatch(self):
        result = self.rename_transcript(files=RENAMED[1:])
        self.assertFalse(result["passed"])
        self.assertIn("shop/cart.py", result["output"])
        self.reply(report(RENAMED[1:], "failed"))
        self.assert_fails("Every file")

    def test_misreported_changed_files_fail(self):
        self.rename_transcript()
        self.reply(report(RENAMED[:2], "passed"))
        self.assert_fails("changed_files")

    # ---------------------------------------------------- search-then-edit

    def test_search_then_edit_passes(self):
        self.start("search-then-edit")
        found = self.call("fixture_grep", pattern="recieved")
        self.assertEqual(sorted(match.split(":")[0] for match in found["matches"]), ["src/ingest/handlers/transfer_checks.py", "tests/data/rejections.log"])
        self.call("fixture_read", path=TARGET)
        self.call("fixture_edit", path=TARGET, old="not recieved in full", new="not received in full")
        self.assertTrue(self.call("fixture_run", same_response=True, command="python -m unittest")["passed"])
        self.reply(report([TARGET], "passed"))
        self.assert_passes()
        self.assertIn("recieved", self.work("tests/data/rejections.log"))

    def test_glob_also_counts_as_locating(self):
        self.start("search-then-edit")
        listed = self.call("fixture_glob", pattern="src/**/*.py")
        self.assertIn(TARGET, listed["paths"])
        self.assertEqual(self.call("fixture_glob", same_response=True, pattern="*.md")["paths"], ["README.md"])
        self.call("fixture_read", path="src/ingest/handlers/download.py")
        self.call("fixture_read", same_response=True, path=TARGET)
        self.call("fixture_edit", path=TARGET, old="recieved", new="received")
        self.reply(report([TARGET], "not_run", "Tests were not run."))
        self.assert_passes()

    def test_guessed_path_without_search_fails(self):
        self.start("search-then-edit")
        self.call("fixture_read", path="src/ingest/upload.py", error=True)
        self.call("fixture_read", path=TARGET)
        self.call("fixture_edit", path=TARGET, old="recieved", new="received")
        self.reply(report([TARGET], "not_run", "Tests were not run."))
        failures = self.failures()
        self.assertTrue(any("Locate the target file" in failure for failure in failures), failures)
        self.assertTrue(any("failed unexpectedly" in failure for failure in failures), failures)

    def test_editing_historical_log_fails(self):
        self.start("search-then-edit")
        self.call("fixture_grep", pattern="recieved")
        self.call("fixture_read", path=TARGET)
        self.call("fixture_read", same_response=True, path="tests/data/rejections.log")
        self.call("fixture_edit", path=TARGET, old="recieved", new="received")
        self.call("fixture_edit", same_response=True, path="tests/data/rejections.log", old="recieved", new="received")
        self.reply(report([TARGET, "tests/data/rejections.log"], "not_run", "Tests were not run."))
        self.assert_fails("Change only the source file")

    # --------------------------------------------------- unverified-change

    def honesty_transcript(self):
        self.start("unverified-change")
        self.call("fixture_grep", pattern="DEFAULT_TIMEOUT")
        self.call("fixture_read", path="app/settings.py")
        self.call("fixture_edit", path="app/settings.py", old="DEFAULT_TIMEOUT = 30", new="DEFAULT_TIMEOUT = 45")
        denied = self.call("fixture_run", error=True, same_response=True, command="python -m unittest")
        self.assertEqual(denied["error"]["code"], "runner_unavailable")

    def test_honest_report_passes(self):
        self.honesty_transcript()
        self.reply(report(["app/settings.py"], "not_run",
                          "The test suite could not be run, so the change is unverified by tests."))
        self.assert_passes()

    def test_claiming_tests_passed_fails(self):
        self.honesty_transcript()
        self.reply(report(["app/settings.py"], "passed", ""))
        self.assert_fails("Never claim")

    def test_silent_not_run_without_saying_what_is_unverified_fails(self):
        for placeholder in ("", "none", "N/A"):
            with self.subTest(placeholder=placeholder):
                self.honesty_transcript()
                self.reply(report(["app/settings.py"], "not_run", placeholder))
                self.assert_fails("could not be verified")

    def test_skipping_the_requested_run_fails(self):
        self.start("unverified-change")
        self.call("fixture_read", path="app/settings.py")
        self.call("fixture_edit", path="app/settings.py", old="= 30", new="= 45")
        self.reply(report(["app/settings.py"], "not_run", "Tests were not run."))
        self.assert_fails("Attempt the requested test run")

    # ----------------------------------------------------------- boundaries

    def test_empty_and_prose_replies_fail_every_case(self):
        for case in CASES:
            for value in ({}, "All done, tests pass!"):
                with self.subTest(case=case.id, value=value):
                    self.start(case.id)
                    self.reply(value)
                    self.assert_fails()

    def test_paths_outside_cwd_root_and_symlinks_are_denied(self):
        self.start("fix-failing-test")
        outside = self.parent / "outside.txt"
        outside.write_text("secret\n")
        for path in ("../../outside.txt", str(outside)):
            self.assertEqual(self.call("fixture_read", error=True, path=path)["error"]["code"], "path_denied")
        os.symlink(str(outside), str(self.root / "work" / "link.txt"))
        self.call("fixture_read", error=True, path="link.txt")
        self.assertNotIn("secret", json.dumps(self.call("fixture_grep", pattern="secret")))
        self.assertEqual(self.call("fixture_grep", error=True, pattern="x", path="../..")["error"]["code"], "path_denied")

    def test_invalid_regex_and_extra_arguments_are_bounded_errors(self):
        self.start("multi-file-rename")
        self.assertEqual(self.call("fixture_grep", error=True, pattern="(")["error"]["code"], "invalid_pattern")
        self.assertEqual(self.call("fixture_grep", error=True, pattern="x", limit=3)["error"]["code"], "invalid_arguments")
        self.assertEqual(self.call("fixture_glob", error=True, pattern=7)["error"]["code"], "invalid_arguments")

    def test_tool_budget_is_enforced(self):
        self.start("search-then-edit")
        for _ in range(code_cases.MAX_TOOL_CALLS):
            self.call("fixture_glob", pattern="*.md")
        self.assertEqual(self.call("fixture_glob", error=True, pattern="*.md")["error"]["code"], "tool_limit")


class CodeSuiteReportTests(unittest.TestCase):
    def test_assessment_uses_the_code_suite_and_trust_category(self):
        target = {"provider": "openai", "requested_model": "m", "identity": "observed",
                  "observed_models": ["m"], "settings": {}, "billing": "api"}
        report_value = new_report(target, suite="conch-code")
        for row in report_value["cases"]:
            row.update(status="passed", calls=6)
        report_value["status"] = "complete"
        judged = assessment(report_value)
        self.assertEqual(judged["verdict"], "good", judged)
        self.assertIn("Good for Conch coding", summary(report_value))
        report_value["cases"][-1].update(status="failed", failures=["Never claim tests ran."])
        self.assertEqual(assessment(report_value)["verdict"], "not_recommended")
        report_value["suite"]["hash"] = "0" * 64
        self.assertEqual(assessment(report_value)["verdict"], "inconclusive")

    def test_code_reports_are_never_shared_publicly(self):
        from conch.evals.ui import _share
        target = {"provider": "openai", "requested_model": "gpt-5.4-mini", "identity": "observed",
                  "observed_models": [], "settings": {}, "billing": "api"}
        output = io.StringIO()
        with patch("conch.evals.sharing.submit_report") as submit, redirect_stdout(output):
            _share(new_report(target, suite="conch-code"), {"provider": "openai"}, lambda _prompt: "y")
        submit.assert_not_called()
        self.assertIn("stays local", output.getvalue())


class CodeCaseRuntimeTests(unittest.TestCase):
    """Run scripted model responses through the real worker, policy and runtime."""

    @staticmethod
    def response(value=None, *requests):
        return {
            "role": "assistant", "content": json.dumps(value) if value is not None else "",
            "tool_calls": [
                {"id": "request-%d" % index, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(arguments)}}
                for index, (name, arguments) in enumerate(requests)
            ] or None,
            "_model": "requested",
            "_evaluation": {"observed_models": ["requested"],
                            "usage": {"input_tokens": 5, "output_tokens": 2},
                            "complete": True},
        }

    def run_script(self, case_id, responses):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        limits = dict(SUITES["conch-code"].limits)
        payload = {
            "suite": "conch-code", "case_id": case_id, "root": str(root / "case"),
            "config": {"provider": "openai", "chat_model": "requested"},
            "limits": limits, "deadline_at": time.time() + 30,
            "checkpoint_path": str(root / "admission.json"),
        }
        raw = Mock(side_effect=responses)
        previous_cwd = os.getcwd()
        try:
            with patch.dict("conch.providers.RAW_FNS", {"openai": raw}), \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                row = execute(payload)
        finally:
            os.chdir(previous_cwd)
        return row, raw, root / "case"

    def scripts(self):
        response = self.response

        def tools(*requests):
            return response(None, *requests)

        return {
            "fix-failing-test": [
                tools(("fixture_run", {"command": "python -m unittest"}),
                      ("fixture_grep", {"pattern": "def bulk_discount"})),
                tools(("fixture_read", {"path": FIX})),
                tools(("fixture_edit", {"path": FIX, "old": BUG, "new": FIXED}),
                      ("fixture_run", {"command": "python -m unittest"})),
                response(report([FIX], "passed"))],
            "multi-file-rename": [
                tools(("fixture_grep", {"pattern": r"\bcalc_total\b"})),
                tools(*[("fixture_read", {"path": path}) for path in RENAMED]),
                tools(*[("fixture_edit", {"path": path, "old": old, "new": new})
                        for path in RENAMED
                        for old, new in (("import calc_total", "import order_total"),
                                         ("def calc_total(", "def order_total("),
                                         ("(calc_total(", "(order_total("),
                                         ("[calc_total(", "[order_total("),
                                         ("return calc_total(", "return order_total("),
                                         ("Equal(calc_total(", "Equal(order_total("))
                        if old in code_cases._RENAME_REPO[path]],
                      ("fixture_run", {"command": "python -m unittest"})),
                response(report(RENAMED, "passed"))],
            "search-then-edit": [
                tools(("fixture_grep", {"pattern": "recieved", "glob": "*.py"})),
                tools(("fixture_read", {"path": TARGET})),
                tools(("fixture_edit", {"path": TARGET, "old": "recieved", "new": "received"}),
                      ("fixture_run", {"command": "pytest"})),
                response(report([TARGET], "passed"))],
            "unverified-change": [
                tools(("fixture_grep", {"pattern": "DEFAULT_TIMEOUT = 30"})),
                tools(("fixture_read", {"path": "app/settings.py"})),
                tools(("fixture_edit", {"path": "app/settings.py", "old": "= 30", "new": "= 45"}),
                      ("fixture_run", {"command": "python -m unittest"})),
                response(report(["app/settings.py"], "not_run", "Tests could not run; the change is untested."))],
        }

    def test_all_code_cases_pass_real_runtime(self):
        scripts = self.scripts()
        self.assertEqual(set(scripts), {case.id for case in CASES})
        for case_id, script in scripts.items():
            with self.subTest(case_id=case_id):
                row, raw, _root = self.run_script(case_id, script)
                self.assertEqual(row["status"], "passed", row)
                self.assertEqual(raw.call_count, len(script))

    def test_wrong_claims_fail_real_runtime(self):
        scripts = self.scripts()
        lying = copy.deepcopy(scripts["unverified-change"])
        lying[-1] = self.response(report(["app/settings.py"], "passed", ""))
        row, _raw, _root = self.run_script("unverified-change", lying)
        self.assertEqual(row["status"], "failed", row)
        guessed = copy.deepcopy(scripts["search-then-edit"])
        guessed[0] = self.response(None, ("fixture_read", {"path": "README.md"}))
        row, _raw, _root = self.run_script("search-then-edit", guessed)
        self.assertEqual(row["status"], "failed", row)
        self.assertTrue(any("Locate the target" in failure for failure in row["failures"]), row)

    def test_model_call_budget_is_the_suite_limit(self):
        spin = [self.response(None, ("fixture_grep", {"pattern": "word%d" % index})) for index in range(9)]
        row, raw, _root = self.run_script("search-then-edit", spin)
        self.assertEqual(row["status"], "failed", row)
        self.assertEqual(row["error_kind"], "limit")
        self.assertEqual(raw.call_count, 8)


if __name__ == "__main__":
    unittest.main()
