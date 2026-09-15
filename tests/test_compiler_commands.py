"""The ``/compile`` command family (C1 review surface).

Proven here with an isolated XDG state dir and a patched compilation
session: compile stores card v1, list/show render, show --diff diffs the
last two versions, approve is the origin-bound authorization moment (and
carries the pinned digest), reject records the reason, revise records a
new card version through a fresh session seeded with the prior card +
guidance, remote origins are refused with a clear message (v1 is
interactive-only), and the command is registered on the slash registry.
"""

import contextlib
import copy
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from conch.capitol.compiler.card import normalize_card
from conch.capitol.compiler.commands import run_compile_command
from conch.kernel.model import CompilationStatus
from conch.kernel.store import MissionStore

from tests.compiler_fixtures import (
    candidate_card,
    fake_discovery,
)


def run(arg, config=None, origin="local"):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        run_compile_command(arg, config or {}, origin=origin)
    return buffer.getvalue()


class CompileCommandCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": self._tmp.name,
            "XDG_CONFIG_HOME": os.path.join(self._tmp.name, "config"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.card = normalize_card(candidate_card(), fake_discovery())

    def store(self):
        store = MissionStore()
        self.addCleanup(store.close)
        return store

    def seed(self):
        """One stored compilation, as /compile would have created it."""
        store = self.store()
        return store, store.create_compilation(self.card, actor="tester")


class TestCompileAndRevise(CompileCommandCase):
    def test_compile_runs_session_and_stores_v1(self):
        with patch(
            "conch.capitol.compiler.session.run_compile_session",
            return_value=self.card,
        ) as session:
            output = run('"weekday 5pm funding report"')
        self.assertIn("✓ Compiled", output)
        self.assertIn("[compiled]", output)
        session.assert_called_once()
        self.assertEqual(
            session.call_args.args[1], "weekday 5pm funding report"
        )
        compilations = self.store().list_compilations()
        self.assertEqual(len(compilations), 1)
        self.assertEqual(compilations[0]["card_version"], 1)

    def test_open_questions_surface(self):
        parked = dict(self.card, open_questions=["which channel?"])
        with patch(
            "conch.capitol.compiler.session.run_compile_session",
            return_value=parked,
        ):
            output = run("do a thing")
        self.assertIn("open question", output)
        self.assertIn("which channel?", output)

    def test_revise_records_v2_with_guidance(self):
        store, compilation = self.seed()
        cid = compilation["compilation_id"]
        revised = copy.deepcopy(self.card)
        revised["narrative"] += " Now weekly."
        with patch(
            "conch.capitol.compiler.session.run_compile_session",
            return_value=revised,
        ) as session:
            output = run(f'revise {cid[:16]} "make it weekly"')
        self.assertIn("Card v2 recorded", output)
        kwargs = session.call_args.kwargs
        self.assertEqual(kwargs["guidance"], "make it weekly")
        self.assertEqual(kwargs["prior_card"], self.card)
        row = store.compilation_card(cid)
        self.assertEqual(row["card_version"], 2)
        self.assertEqual(row["guidance"], "make it weekly")


class TestReviewSurface(CompileCommandCase):
    def test_list_and_show(self):
        _store, compilation = self.seed()
        output = run("list")
        self.assertIn("Compilations (1):", output)
        self.assertIn("[compiled]", output)
        shown = run(f"show #{compilation['compilation_seq']}")
        self.assertIn("Architecture Card", shown)
        self.assertIn("Success criteria", shown)
        self.assertIn("Acceptance drill", shown)

    def test_show_diff(self):
        store, compilation = self.seed()
        cid = compilation["compilation_id"]
        revised = copy.deepcopy(self.card)
        revised["narrative"] += " Second edition."
        store.record_compilation_card(cid, revised, actor="tester")
        output = run(f"show {cid[:16]} --diff")
        self.assertIn("card v1", output)
        self.assertIn("card v2", output)
        self.assertIn("Second edition", output)

    def test_approve_pins_digest(self):
        store, compilation = self.seed()
        cid = compilation["compilation_id"]
        output = run(f"approve {cid[:16]}")
        self.assertIn("✓ Approved", output)
        self.assertIn("pinned", output)
        updated = store.get_compilation(cid)
        self.assertEqual(updated["status"], CompilationStatus.APPROVED)
        self.assertEqual(updated["approved_version"], 1)
        self.assertTrue(updated["approved_digest"].startswith("sha256:"))
        self.assertEqual(updated["decision_origin"], "local::")

    def test_reject_with_reason(self):
        store, compilation = self.seed()
        cid = compilation["compilation_id"]
        output = run(f"reject {cid[:16]} too broad for v1")
        self.assertIn("✓ Rejected", output)
        updated = store.get_compilation(cid)
        self.assertEqual(updated["status"], CompilationStatus.REJECTED)
        self.assertEqual(updated["decision_reason"], "too broad for v1")

    def test_double_approve_reports_cleanly(self):
        _store, compilation = self.seed()
        cid = compilation["compilation_id"]
        run(f"approve {cid[:16]}")
        output = run(f"approve {cid[:16]}")
        self.assertIn("illegal compilation transition", output)

    def test_status_shows_versions_and_decision(self):
        store, compilation = self.seed()
        cid = compilation["compilation_id"]
        run(f"approve {cid[:16]}")
        output = run(f"status {cid[:16]}")
        self.assertIn("approved: card v1", output)
        self.assertIn("card v1", output)

    def test_unknown_reference(self):
        self.seed()
        output = run("show cmp-nope")
        self.assertIn("No compilation matching", output)

    def test_help(self):
        output = run("help")
        self.assertIn("/compile", output)
        self.assertIn("materialize", output)


class TestOriginBinding(CompileCommandCase):
    def test_remote_origin_refused(self):
        for origin in ("slack", "sms", "session", "remote"):
            output = run("list", origin=origin)
            self.assertIn("interactive-only", output)
            self.assertIn(origin, output)

    def test_remote_origin_cannot_approve(self):
        store, compilation = self.seed()
        cid = compilation["compilation_id"]
        output = run(f"approve {cid[:16]}", origin="slack")
        self.assertIn("interactive-only", output)
        self.assertEqual(
            store.get_compilation(cid)["status"],
            CompilationStatus.COMPILED,
        )

    def test_no_self_approval_surface_exists(self):
        """The compilation session's toolset has no path to a decision:
        the workspace exposes requirements/emit_card only, and the store
        refuses any non-local decision origin."""
        from conch.capitol.compiler.session import (
            COMPILER_WORKSPACE_TOOL,
        )

        ops = COMPILER_WORKSPACE_TOOL["function"]["parameters"][
            "properties"
        ]["op"]["enum"]
        self.assertEqual(sorted(ops), ["emit_card", "requirements"])
        store, compilation = self.seed()
        from conch.kernel.model import ApprovalError

        with self.assertRaises(ApprovalError):
            store.decide_compilation(
                compilation["compilation_id"], "approve",
                decided_by="model", origin_channel="session",
            )


class TestRegistration(unittest.TestCase):
    def test_slash_registry(self):
        from conch.commands import all_slash_commands, slash_command_names

        self.assertIn("/compile", slash_command_names())
        entry = next(
            entry for entry in all_slash_commands()
            if entry[0].startswith("/compile")
        )
        self.assertIn("ProcessCompiler", entry[1])


if __name__ == "__main__":
    unittest.main()
