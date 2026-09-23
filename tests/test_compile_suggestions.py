"""Pattern mining (capture plan, feature 2): recurrence detection is
computed, never model-ranked.

Proven here: command normalization is stable and explainable, planted
recurrences are detected with exact counts and stable signatures
(same input → same signatures and ranking), below-threshold noise is
ignored, shorter grams fold into maximal ones, mission action sequences
mine through the same machinery, and the /compile suggestions →
from-suggestion path drafts a card carrying pattern provenance.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from conch.capitol.compiler.commands import (
    _SUGGESTION_CACHE,
    run_compile_command,
)
from conch.capitol.compiler.card import normalize_card
from conch.conversations import Conversation
from conch.kernel.patterns import (
    mine_sequences,
    normalize_command,
    occurrences_in_steps,
    paired_steps_from_messages,
    steps_from_mission_events,
)
from conch.kernel.store import MissionStore

from tests.compiler_fixtures import candidate_card, fake_discovery

CONFIG = {"capture_enabled": "true"}


def shell_call(command, call_id="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": "local_shell",
                     "arguments": json.dumps({"command": command})},
    }]}


DEPLOY_SEQUENCE = [
    "git pull origin main",
    "make build",
    "make deploy",
    "kubectl rollout status deploy/api",
]


class TestNormalization(unittest.TestCase):
    def test_options_paths_and_case(self):
        self.assertEqual(normalize_command("Git pull origin main"),
                         "git pull")
        self.assertEqual(
            normalize_command("kubectl --context=prod rollout status"),
            "kubectl rollout",
        )
        self.assertEqual(normalize_command("cat /var/log/syslog"),
                         "cat ·")
        self.assertEqual(normalize_command("curl https://x.test/a"),
                         "curl ·")

    def test_paired_alignment(self):
        messages = [shell_call("git pull origin main"),
                    shell_call("make deploy --force")]
        pairs = paired_steps_from_messages(messages)
        self.assertEqual([p[0] for p in pairs],
                         ["git pull", "make deploy"])
        self.assertEqual(pairs[0][1], "git pull origin main")


class TestMining(unittest.TestCase):
    def sources(self, repeats=3, noise=0):
        steps = [normalize_command(c) for c in DEPLOY_SEQUENCE]
        sources = [
            {"id": f"s{i}", "kind": "session", "steps": list(steps)}
            for i in range(repeats)
        ]
        for i in range(noise):
            sources.append({
                "id": f"n{i}", "kind": "session",
                "steps": [f"one-off-{i}-{j}" for j in range(4)],
            })
        return sources

    def test_planted_recurrence_exact_and_stable(self):
        first = mine_sequences(self.sources(repeats=3, noise=2))
        second = mine_sequences(self.sources(repeats=3, noise=2))
        self.assertEqual(first, second)  # determinism, whole result
        self.assertTrue(first)
        top = first[0]
        self.assertEqual(top["count"], 3)
        self.assertEqual(len(top["steps"]), 4)  # maximal, not the 3-grams
        self.assertEqual(sorted(top["sources"]), ["s0", "s1", "s2"])
        self.assertIn("3 occurrence(s) across 3 session(s)", top["why"])
        # noise sequences below threshold never appear
        self.assertFalse(
            [s for s in first if s["steps"][0].startswith("one-off")]
        )

    def test_below_threshold_ignored(self):
        self.assertEqual(mine_sequences(self.sources(repeats=2)), [])
        self.assertTrue(
            mine_sequences(self.sources(repeats=2), min_count=2)
        )

    def test_shorter_grams_fold_into_maximal(self):
        suggestions = mine_sequences(self.sources(repeats=4))
        lengths = sorted(len(s["steps"]) for s in suggestions)
        # the full 4-step shape is reported; its 3-step sub-shapes fold
        self.assertEqual(lengths, [4])

    def test_occurrences_in_steps(self):
        steps = ["a", "b", "c", "a", "b", "c"]
        self.assertEqual(occurrences_in_steps(steps, ["a", "b", "c"]),
                         [0, 3])


class TestMissionMining(unittest.TestCase):
    def test_action_steps(self):
        events = [
            {"kind": "action_recorded",
             "data": {"action_class": "read",
                      "detail": {"op": "fetch-metrics daily"}}},
            {"kind": "budget_reserved", "data": {}},
            {"kind": "action_recorded",
             "data": {"action_class": "communicate",
                      "detail": {"op": "notify slack"}}},
        ]
        steps = steps_from_mission_events(events)
        self.assertEqual(steps, [
            "action:read:fetch-metrics daily",
            "action:communicate:notify slack",
        ])


class TestSuggestionCommands(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": self._tmp.name,
            "XDG_CONFIG_HOME": os.path.join(self._tmp.name, "config"),
        })
        env.start()
        self.addCleanup(env.stop)
        _SUGGESTION_CACHE.clear()
        from conch.conversations import ConversationManager

        manager = ConversationManager()
        for index in range(3):
            conversation = Conversation(
                id=f"aa00bb0{index}", title=f"deploy {index}",
                model="m", provider="p",
                messages=[
                    shell_call(command, call_id=f"c{index}{j}")
                    for j, command in enumerate(DEPLOY_SEQUENCE)
                ],
            )
            manager.save(conversation)  # save() alone skips the index
        manager.close()

    def run_cmd(self, arg, config=None):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_compile_command(
                arg, dict(CONFIG if config is None else config)
            )
        return buffer.getvalue()

    def test_suggestions_listing_and_gate(self):
        gated = self.run_cmd("suggestions", config={})
        self.assertIn("/install capture", gated)
        output = self.run_cmd("suggestions")
        self.assertIn("Recurring shapes", output)
        self.assertIn("git pull", output)
        self.assertIn("3 occurrence(s) across 3 session(s)", output)
        self.assertIn("from-suggestion", output)

    def test_from_suggestion_drafts_with_provenance(self):
        self.run_cmd("suggestions")

        def fake_session(config, goal, **kwargs):
            block = kwargs["capture_context"]
            assert "Recurring pattern" in block
            assert "git pull origin main" in block  # raw evidence
            return normalize_card(candidate_card(), fake_discovery())

        with patch(
            "conch.capitol.compiler.session.run_compile_session",
            side_effect=fake_session,
        ):
            output = self.run_cmd("from-suggestion 1")
        self.assertIn("Compiled", output)
        store = MissionStore()
        self.addCleanup(store.close)
        rows = store.list_compilations()
        self.assertEqual(len(rows), 1)
        capture = store.compilation_capture(rows[0]["compilation_id"])
        self.assertEqual(capture["kind"], "pattern")
        self.assertEqual(capture["count"], 3)
        status = self.run_cmd(
            f"status {rows[0]['compilation_id']}"
        )
        self.assertIn("recurring pattern", status)

    def test_from_suggestion_requires_prior_listing(self):
        output = self.run_cmd("from-suggestion 1")
        self.assertIn("suggestions first", output)

    def test_threshold_flags(self):
        output = self.run_cmd("suggestions --min 4")
        self.assertIn("No recurring shapes", output)


if __name__ == "__main__":
    unittest.main()
