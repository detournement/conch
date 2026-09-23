"""Capture→Card (capture plan, feature 1): journaled work becomes a
draft Architecture Card through the normal ``/compile`` pipeline.

Proven here: the mission-journal reader is bounded and kind-filtered
(head+tail elision on oversized journals), conversation traces extract
commands/tools deterministically, planted credentials reject the capture
whole on every path (reader output, conversation trace, and the
provenance dict at the store), the capture context block reaches the
compilation session as evidence, the stored compilation carries capture
provenance in its ``compilation_created`` journal event (replay == live
untouched), ``/compile status`` renders the provenance line, and a
session that cannot produce a valid card fails closed.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from conch.capitol.compiler.capture import (
    capture_from_conversation,
    capture_from_mission,
    compile_from_capture,
)
from conch.capitol.compiler.card import CardError, normalize_card
from conch.capitol.compiler.commands import run_compile_command
from conch.conversations import Conversation
from conch.kernel.capture import (
    MAX_CAPTURE_EVENTS,
    read_mission_capture,
    render_mission_capture,
)
from conch.kernel.model import KernelError
from conch.kernel.store import MissionStore
from conch.secretguard import CredentialRejected

from tests.compiler_fixtures import candidate_card, fake_discovery

FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2  # synthetic, tests only


class CaptureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": self._tmp.name,
            "XDG_CONFIG_HOME": os.path.join(self._tmp.name, "config"),
        })
        env.start()
        self.addCleanup(env.stop)

    def store(self):
        store = MissionStore()
        self.addCleanup(store.close)
        return store

    def seed_mission(self, store, *, notes=(), actions=0):
        mid = store.create_mission({
            "goal": "publish the weekly ops digest",
        })
        store.record_plan(mid, {
            "steps": ["collect metrics", "draft digest", "notify"],
        })
        for note in notes:
            store.record_note(mid, note)
        for index in range(actions):
            store.record_action(
                mid, "read", f"capture-test-{index}",
                detail={"op": f"fetch-metrics-{index}"},
            )
        store.record_checkpoint(mid, "digest drafted and sent")
        return mid


class TestMissionCaptureReader(CaptureCase):
    def test_bounded_kind_filtered_trace(self):
        store = self.store()
        mid = self.seed_mission(store, notes=("checked grafana",),
                                actions=3)
        capture = read_mission_capture(store, mid)
        self.assertEqual(capture["source"], "mission")
        self.assertEqual(capture["mission_id"], mid)
        self.assertEqual(capture["elided"], 0)
        kinds = {entry["kind"] for entry in capture["trace"]}
        self.assertIn("plan_recorded", kinds)
        self.assertIn("action_recorded", kinds)
        self.assertIn("checkpoint_recorded", kinds)
        # bookkeeping kinds never enter a capture
        self.assertNotIn("budget_reserved", kinds)
        self.assertNotIn("timer_created", kinds)
        self.assertGreater(capture["event_range"][1],
                           capture["event_range"][0])
        rendered = render_mission_capture(capture)
        self.assertIn("publish the weekly ops digest", rendered)
        self.assertIn("fetch-metrics-1", rendered)

    def test_oversized_journal_elides_head_tail(self):
        store = self.store()
        mid = self.seed_mission(store, actions=MAX_CAPTURE_EVENTS + 60)
        capture = read_mission_capture(store, mid)
        self.assertGreater(capture["elided"], 0)
        self.assertLessEqual(len(capture["trace"]),
                             MAX_CAPTURE_EVENTS + 1)
        markers = [e for e in capture["trace"] if e["kind"] == "…"]
        self.assertEqual(len(markers), 1)
        # the tail survives: the checkpoint is the journal's last entry
        self.assertEqual(capture["trace"][-1]["kind"],
                         "checkpoint_recorded")
        rendered = render_mission_capture(capture)
        self.assertIn("events elided", rendered)

    def test_prefix_resolution_and_ambiguity(self):
        store = self.store()
        mid = self.seed_mission(store)
        capture = read_mission_capture(store, mid[:12])
        self.assertEqual(capture["mission_id"], mid)
        with self.assertRaises(KernelError):
            read_mission_capture(store, "msn-nonexistent")

    def test_planted_credential_rejects_capture_whole(self):
        store = self.store()
        mid = self.seed_mission(
            store, notes=(f"use token {FAKE_TOKEN} for the api",),
        )
        with self.assertRaises(CredentialRejected):
            capture_from_mission(store, mid)


class TestConversationCapture(CaptureCase):
    def make_conversation(self, messages, conv_id="abc12345",
                          title="Deploy runbook"):
        conversation = Conversation(
            id=conv_id, title=title, model="m", provider="p",
            messages=messages,
        )
        conversation.save()
        return conversation

    def test_trace_extracts_commands_and_tools(self):
        conversation = self.make_conversation([
            {"role": "user", "content": "deploy the api"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "local_shell", "arguments":
                             json.dumps({"command": "git pull"})},
            }]},
            {"role": "tool", "tool_call_id": "c1",
             "content": "Already up to date."},
            {"role": "assistant", "content": "pulled; deploying now"},
        ])
        context = capture_from_conversation(conversation)
        self.assertEqual(context["kind"], "session")
        self.assertIn("ran: git pull", context["block"])
        self.assertIn("user: deploy the api", context["block"])
        self.assertIn("result: Already up to date.", context["block"])
        self.assertEqual(context["provenance"]["source"], "abc12345")
        self.assertTrue(context["default_goal"])

    def test_oversized_conversation_bounded(self):
        messages = []
        for index in range(600):
            messages.append({"role": "user",
                             "content": f"step {index}"})
        conversation = self.make_conversation(messages)
        context = capture_from_conversation(conversation)
        self.assertLessEqual(len(context["block"]), 9100)
        self.assertIn("elided", context["block"])
        self.assertGreater(context["provenance"]["elided"], 0)

    def test_planted_credential_rejects_whole(self):
        conversation = self.make_conversation([
            {"role": "user",
             "content": f"the key is {FAKE_TOKEN} ok?"},
        ])
        with self.assertRaises(CredentialRejected):
            capture_from_conversation(conversation)


class TestCaptureSynthesis(CaptureCase):
    def scripted(self, emissions):
        seen = {}

        def factory(messages, workspace, capitol_client, caps):
            seen["messages"] = messages
            for card in emissions:
                workspace.call_tool(
                    "compiler_workspace",
                    {"op": "emit_card", "card": card},
                )
            return "done", {"input_tokens": 1, "output_tokens": 1}
        return factory, seen

    def context(self):
        return {
            "kind": "session", "source_id": "abc12345",
            "label": "session abc12345 (Deploy runbook)",
            "default_goal": "automate the deploy runbook",
            "block": "Conversation abc12345\n  ran: git pull",
            "provenance": {"kind": "session", "source": "abc12345",
                           "message_count": 4, "trace_entries": 3,
                           "elided": 0},
        }

    def test_capture_block_reaches_the_session(self):
        factory, seen = self.scripted([candidate_card()])
        with patch(
            "conch.capitol.compiler.session.build_discovery",
            return_value=fake_discovery(),
        ):
            card, provenance = compile_from_capture(
                {}, self.context(), session_factory=factory,
            )
        user_message = seen["messages"][1]["content"]
        self.assertIn("CAPTURED WORK TRACE", user_message)
        self.assertIn("ran: git pull", user_message)
        self.assertIn("no authority", user_message)
        self.assertEqual(provenance["goal"],
                         "automate the deploy runbook")
        self.assertTrue(card["goal"])

    def test_invalid_synthesis_fails_closed(self):
        factory, _seen = self.scripted([{"schema": "bogus"}])
        with patch(
            "conch.capitol.compiler.session.build_discovery",
            return_value=fake_discovery(),
        ):
            with self.assertRaises(CardError):
                compile_from_capture(
                    {}, self.context(), session_factory=factory,
                )

    def test_goal_required_when_source_has_none(self):
        context = self.context()
        context["default_goal"] = ""
        factory, _seen = self.scripted([candidate_card()])
        from conch.capitol.errors import CapitolError

        with self.assertRaises(CapitolError):
            compile_from_capture({}, context, session_factory=factory)


class TestProvenanceStorage(CaptureCase):
    def test_capture_rides_the_created_event(self):
        store = self.store()
        card = normalize_card(candidate_card(), fake_discovery())
        provenance = {"kind": "session", "source": "abc12345",
                      "message_count": 4, "trace_entries": 3,
                      "elided": 0, "goal": "automate the deploy"}
        compilation = store.create_compilation(
            card, actor="tester", capture=provenance,
        )
        cid = compilation["compilation_id"]
        stored = store.compilation_capture(cid)
        self.assertEqual(stored, provenance)
        # goal-compiled compilations carry no capture
        plain = store.create_compilation(card, actor="tester")
        self.assertIsNone(
            store.compilation_capture(plain["compilation_id"])
        )
        # provenance is journal data: replay still equals live
        self.assertTrue(store.replay_matches_live())

    def test_credentialed_provenance_rejected(self):
        store = self.store()
        card = normalize_card(candidate_card(), fake_discovery())
        with self.assertRaises(CredentialRejected):
            store.create_compilation(
                card, actor="tester",
                capture={"kind": "session",
                         "source": f"token {FAKE_TOKEN}"},
            )

    def test_status_renders_provenance_line(self):
        store = self.store()
        card = normalize_card(candidate_card(), fake_discovery())
        compilation = store.create_compilation(
            card, actor="tester",
            capture={"kind": "mission", "source": "msn-abc",
                     "event_range": [3, 41], "total_events": 12,
                     "elided": 0, "goal": "g"},
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_compile_command(
                f"status {compilation['compilation_id']}", {},
            )
        output = buffer.getvalue()
        self.assertIn("captured from mission msn-abc", output)
        self.assertIn("events 3–41", output)


class TestFromSessionCommand(CaptureCase):
    def test_command_end_to_end(self):
        conversation = Conversation(
            id="abc12345", title="Deploy runbook", model="m",
            provider="p", messages=[
                {"role": "user", "content": "deploy the api"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{
                     "id": "c1", "type": "function",
                     "function": {"name": "local_shell", "arguments":
                                  json.dumps({"command": "make deploy"})},
                 }]},
            ],
        )
        conversation.save()

        def fake_session(config, goal, **kwargs):
            assert "CAPTURED WORK TRACE" in kwargs["capture_context"]
            assert "make deploy" in kwargs["capture_context"]
            return normalize_card(candidate_card(), fake_discovery())

        buffer = io.StringIO()
        with patch(
            "conch.capitol.compiler.session.run_compile_session",
            side_effect=fake_session,
        ):
            with contextlib.redirect_stdout(buffer):
                run_compile_command(
                    'from-session abc12345 "automate the deploy"',
                    {"capture_enabled": "true"},
                )
        output = buffer.getvalue()
        self.assertIn("Compiled", output)
        store = self.store()
        rows = store.list_compilations()
        self.assertEqual(len(rows), 1)
        capture = store.compilation_capture(rows[0]["compilation_id"])
        self.assertEqual(capture["kind"], "session")
        self.assertEqual(capture["source"], "abc12345")
        self.assertEqual(capture["goal"], "automate the deploy")

    def test_remote_origin_still_refused(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_compile_command(
                "from-session abc12345", {"capture_enabled": "true"},
                origin="slack",
            )
        self.assertIn("interactive-only", buffer.getvalue())

    def test_capture_verbs_gated_on_component(self):
        for verb in ("from-session", "from-mission x",
                     "from-email", "from-history 50 \"g\""):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                run_compile_command(verb, {})
            self.assertIn("/install capture", buffer.getvalue(),
                          f"{verb} not gated")


class TestCaptureComponent(CaptureCase):
    def test_registered_with_honest_status(self):
        from conch.plugins import components, load_builtin_plugins

        load_builtin_plugins()
        by_name = {c.name: c for c in components()}
        self.assertIn("capture", by_name)
        component = by_name["capture"]
        self.assertIn("/install capture", component.status({}))
        enabled = component.status({
            "capture_enabled": "true",
            "capture_email_folder": "Conch/Capture",
        })
        self.assertIn("enabled", enabled)
        self.assertIn("sessions", enabled)
        self.assertIn("Conch/Capture", enabled)

    def test_setup_enables_the_gate(self):
        from conch.capitol.plugin import _capture_setup

        config = {}
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _capture_setup(config)  # no email config → no prompts
        self.assertEqual(config.get("capture_enabled"), "true")
        self.assertIn("Capture enabled", buffer.getvalue())
        # the config file in the isolated XDG home carries the flag
        from conch.config import load_config

        self.assertEqual(load_config().get("capture_enabled"), "true")


def _raw_mail(sender, subject, body,
              date="Mon, 22 Sep 2026 10:00:00 +0000"):
    return (
        f"From: {sender}\r\nDate: {date}\r\nSubject: {subject}\r\n"
        f"\r\n{body}\r\n"
    ).encode()


class FakeImap:
    """The exact imaplib surface capture_from_email touches."""

    def __init__(self, messages):
        self.messages = dict(messages)  # uid -> raw bytes
        self.selected = None
        self.readonly = None

    def select(self, folder, readonly=False):
        self.selected = folder
        self.readonly = readonly
        return "OK", [b"1"]

    def uid(self, op, *args):
        if op == "search":
            spec = args[1]
            low = int(spec.split()[1].split(":")[0])
            uids = sorted(u for u in self.messages if u >= low)
            return "OK", [
                " ".join(str(u) for u in uids).encode() or b""
            ]
        if op == "fetch":
            raw = self.messages.get(int(args[0]))
            if raw is None:
                return "NO", [None]
            return "OK", [(b"1 (RFC822)", raw)]
        raise AssertionError(f"unexpected op {op}")

    def logout(self):
        return "BYE", []


class TestEmailCapture(CaptureCase):
    CONFIG = {
        "capture_email_folder": "Conch/Capture",
        "email_imap_host": "imap.example.com",
        "email_address": "me@example.com",
        "email_allowed_senders": "ada@example.com",
    }

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"EMAIL_PASSWORD": "x"})
        env.start()
        self.addCleanup(env.stop)

    def capture(self, messages, config=None):
        from conch.capitol.compiler.capture_email import (
            capture_from_email,
        )

        fake = FakeImap(messages)
        context, commit = capture_from_email(
            dict(config or self.CONFIG),
            imap_factory=lambda _config: fake,
        )
        return context, commit, fake

    def test_unconfigured_reasons_are_named(self):
        from conch.capitol.compiler.capture_email import (
            email_capture_unconfigured_reason,
        )

        self.assertIn("capture_email_folder",
                      email_capture_unconfigured_reason({}))
        partial = dict(self.CONFIG, email_allowed_senders="")
        self.assertIn("allowlist",
                      email_capture_unconfigured_reason(partial))

    def test_capture_allowlist_and_readonly(self):
        context, _commit, fake = self.capture({
            5: _raw_mail("ada@example.com", "Weekly invoice run",
                         "Step 1: export ledger\nStep 2: mail it"),
            6: _raw_mail("mallory@evil.example", "buy pills", "spam"),
        })
        self.assertTrue(fake.readonly)
        self.assertIn("Weekly invoice run", context["block"])
        self.assertNotIn("spam", context["block"])
        self.assertEqual(context["provenance"]["messages"], 1)
        self.assertEqual(context["provenance"]["skipped"], 1)
        self.assertIn("invoice", context["default_goal"].lower())

    def test_cursor_commits_only_on_success(self):
        from conch.capitol.errors import CapitolError

        messages = {
            5: _raw_mail("ada@example.com", "Run A", "body a"),
        }
        context, commit, _fake = self.capture(messages)
        # no commit yet → the same window re-reads
        context2, commit2, _fake = self.capture(messages)
        self.assertEqual(context2["provenance"]["uid_range"],
                         context["provenance"]["uid_range"])
        commit2()
        # committed → nothing new
        with self.assertRaises(CapitolError) as caught:
            self.capture(messages)
        self.assertIn("no new allowlisted mail", str(caught.exception))
        # new mail after the cursor is picked up
        messages[9] = _raw_mail("ada@example.com", "Run B", "body b")
        context3, _commit, _fake = self.capture(messages)
        self.assertIn("Run B", context3["block"])
        self.assertNotIn("Run A", context3["block"])

    def test_credential_in_body_rejects_whole(self):
        with self.assertRaises(CredentialRejected):
            self.capture({
                5: _raw_mail("ada@example.com", "keys",
                             f"the token is {FAKE_TOKEN}"),
            })


class TestHistoryCapture(CaptureCase):
    def write_history(self, lines, name=".zsh_history"):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def test_zsh_parse_collapse_and_drop(self):
        from conch.capitol.compiler.capture import capture_from_history

        path = self.write_history([
            ": 1758500000:0;git pull",
            ": 1758500001:0;git pull",
            ": 1758500002:0;make deploy",
            f": 1758500003:0;export GH_TOKEN={FAKE_TOKEN}",
            ": 1758500004:0;kubectl rollout status deploy/api",
        ])
        context = capture_from_history(50, path=path)
        block = context["block"]
        self.assertEqual(block.count("git pull"), 1)  # collapsed
        self.assertIn("make deploy", block)
        self.assertIn("kubectl rollout", block)
        self.assertNotIn(FAKE_TOKEN, block)
        self.assertEqual(context["provenance"]["dropped"], 1)
        self.assertIn("1 dropped", block)
        self.assertEqual(context["default_goal"], "")

    def test_plain_bash_format_and_missing_file(self):
        from conch.capitol.compiler.capture import capture_from_history
        from conch.capitol.errors import CapitolError

        path = self.write_history(
            ["git status", "docker build -t api ."],
            name=".bash_history",
        )
        context = capture_from_history(50, path=path)
        self.assertIn("docker build", context["block"])
        with self.assertRaises(CapitolError):
            capture_from_history(50, path="/nonexistent/hist")

    def test_command_requires_explicit_goal(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_compile_command(
                "from-history 50", {"capture_enabled": "true"},
            )
        self.assertIn("goal must be explicit",
                      buffer.getvalue().replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
