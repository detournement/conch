"""PackState performance contract: deferred run cursors, retention, and
compact atomic writes — plus the channel watch loop's use of them (no
per-event file rewrite, flush on park/finish/error, replay-safe)."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from conch.capitol.channel_flow import ChannelListingFlow
from conch.capitol.errors import CapitolError
from conch.capitol.packs.state import PackState

DAY = 86400.0


class _Clock:
    def __init__(self, now=1_000_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class StateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "pack_state.json"
        self.clock = _Clock()

    def state(self, **kwargs):
        kwargs.setdefault("clock", self.clock)
        return PackState(self.path, **kwargs)

    def on_disk(self):
        return json.loads(self.path.read_text())

    def run_cursor(self, session_id="s1", run_id="r1"):
        session = self.on_disk()["sessions"][session_id]
        return [r for r in session["runs"] if r["run_id"] == run_id][0][
            "last_sequence"
        ]


class TestDeferredCursor(StateCase):
    def test_writes_every_n_events_not_every_event(self):
        state = self.state(cursor_flush_events=10,
                           cursor_flush_seconds=3600)
        state.record_run("s1", "draft", "r1", "")
        writes = []
        original = state._save
        state._save = lambda data: (writes.append(1), original(data))
        for sequence in range(1, 10):
            state.advance_cursor("s1", "r1", sequence)
        self.assertEqual(writes, [])
        self.assertEqual(self.run_cursor(), 0)
        state.advance_cursor("s1", "r1", 10)
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.run_cursor(), 10)
        state.advance_cursor("s1", "r1", 11)
        state.flush_cursors()
        self.assertEqual(self.run_cursor(), 11)
        state.flush_cursors()  # nothing pending: no write
        self.assertEqual(len(writes), 2)

    def test_time_cadence_flushes(self):
        state = self.state(cursor_flush_events=1000,
                           cursor_flush_seconds=5)
        state.record_run("s1", "draft", "r1", "")
        state.advance_cursor("s1", "r1", 1)
        self.assertEqual(self.run_cursor(), 0)
        self.clock.now += 6
        state.advance_cursor("s1", "r1", 2)
        self.assertEqual(self.run_cursor(), 2)

    def test_any_other_write_carries_the_cursor(self):
        state = self.state(cursor_flush_events=1000)
        state.record_run("s1", "draft", "r1", "")
        state.advance_cursor("s1", "r1", 7)
        state.update_session("s1", phase="hitl")
        self.assertEqual(self.run_cursor(), 7)

    def test_cursor_never_moves_backwards_across_writers(self):
        state = self.state(cursor_flush_events=1000)
        state.record_run("s1", "draft", "r1", "")
        state.advance_cursor("s1", "r1", 5)
        # Another process already persisted a newer cursor.
        self.state().update_run("s1", "r1", last_sequence=9)
        state.flush_cursors()
        self.assertEqual(self.run_cursor(), 9)

    def test_concurrent_writers_keep_each_others_sessions(self):
        first = self.state(cursor_flush_events=1000)
        second = self.state()
        first.record_run("s1", "draft", "r1", "")
        first.advance_cursor("s1", "r1", 3)
        second.update_session("s2", phase="drafting")
        first.flush_cursors()
        sessions = self.on_disk()["sessions"]
        self.assertIn("s2", sessions)
        self.assertEqual(self.run_cursor(), 3)


class TestWrites(StateCase):
    def test_compact_atomic_private_and_no_temp_leftovers(self):
        state = self.state()
        state.update_session("s1", phase="drafting", note="x" * 10)
        text = self.path.read_text()
        self.assertNotIn("\n", text)
        self.assertEqual(json.loads(text)["sessions"]["s1"]["phase"],
                         "drafting")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(sorted(os.listdir(self._tmp.name)),
                         ["pack_state.json"])

    def test_reads_legacy_pretty_files(self):
        self.path.write_text(json.dumps({
            "version": 1, "sessions": {"old": {"phase": "drafting"}},
        }, indent=2, sort_keys=True))
        self.assertEqual(self.state().session("old"), {"phase": "drafting"})


class TestRetention(StateCase):
    def seed(self):
        now = self.clock.now
        old = now - 45 * DAY
        self.path.write_text(json.dumps({
            "version": 1,
            "sessions": {
                "old-done": {"phase": "done", "updated_at": old},
                "old-failed": {"phase": "failed", "updated_at": old},
                "old-shell": {"updated_at": old},
                "old-active": {"phase": "hitl", "updated_at": old,
                               "hitl": {"run_id": "parked"},
                               "runs": [
                                   {"run_id": "parked", "status": "",
                                    "started_at": old},
                                   {"run_id": "parked-done",
                                    "status": "success",
                                    "started_at": old},
                               ]},
                "old-awaiting": {"phase": "done", "updated_at": old,
                                 "approval_id": 4},
                "new-done": {"phase": "done", "updated_at": now - DAY},
            },
            "threads": {
                "slack:1": "old-done", "slack:2": "new-done",
                "slack:3": "old-active",
            },
        }))

    def test_drops_old_terminal_sessions_and_their_threads(self):
        self.seed()
        self.state().update_session("fresh", phase="drafting")
        data = self.on_disk()
        self.assertEqual(sorted(data["sessions"]), [
            "fresh", "new-done", "old-active", "old-awaiting",
        ])
        self.assertEqual(data["threads"],
                         {"slack:2": "new-done", "slack:3": "old-active"})
        # Finished runs age out of live sessions; the parked one stays.
        runs = data["sessions"]["old-active"]["runs"]
        self.assertEqual([r["run_id"] for r in runs], ["parked"])

    def test_retention_is_configurable_and_can_be_disabled(self):
        self.seed()
        state = self.state().configure_retention(
            {"capitol_pack_retention_days": "60"}
        )
        state.update_session("fresh", phase="drafting")
        self.assertIn("old-done", self.on_disk()["sessions"])
        state.configure_retention({"capitol_pack_retention_days": 0})
        state.update_session("fresh", phase="drafting")
        self.assertIn("old-done", self.on_disk()["sessions"])
        state.configure_retention({"capitol_pack_retention_days": 30})
        state.update_session("fresh", phase="drafting")
        self.assertNotIn("old-done", self.on_disk()["sessions"])

    def test_caps_history_lists_but_not_session_data(self):
        state = self.state(history_cap=3)
        state.update_session("s1", photos=list(range(10)))
        for revision in range(6):
            state.append("s1", "revisions", {"revision": revision})
            state.append("s1", "notes", {"n": revision})
            state.record_run("s1", "draft", f"r{revision}", "")
        session = self.on_disk()["sessions"]["s1"]
        self.assertEqual([r["revision"] for r in session["revisions"]],
                         [3, 4, 5])
        self.assertEqual([r["n"] for r in session["notes"]], [3, 4, 5])
        self.assertEqual([r["run_id"] for r in session["runs"]],
                         ["r3", "r4", "r5"])
        self.assertEqual(session["photos"], list(range(10)))


class _Runtime:
    def __init__(self, events, fail_after=None):
        self.events = events
        self.fail_after = fail_after

    def watch_run(self, run_id, since_sequence=0):
        for index, event in enumerate(self.events):
            if self.fail_after is not None and index == self.fail_after:
                raise CapitolError("stream kept dropping")
            yield event

    def run_status(self, run_id):
        return {"status": "success"}

    def workflow_output(self, run_id):
        return {"ok": True}


def _ev(sequence, event_type="node.node_progress", **extra):
    event = {"event_type": event_type, "sequence": sequence}
    event.update(extra)
    return event


class TestChannelWatchLoop(StateCase):
    def flow(self, **kwargs):
        flow = object.__new__(ChannelListingFlow)
        flow.state = self.state(**kwargs)
        flow.state.record_run("s1", "draft", "r1", "")
        writes = []
        original = flow.state._save
        flow.state._save = lambda data: (writes.append(1), original(data))
        return flow, writes

    def test_no_per_event_rewrite_and_final_flush(self):
        flow, writes = self.flow(cursor_flush_events=100,
                                 cursor_flush_seconds=3600)
        events = [_ev(n) for n in range(1, 251)]
        events.append({"event_type": "_final_status",
                       "data": {"state": "success"}})
        kind, output = flow._supervise(_Runtime(events), "s1", "draft",
                                       "r1")
        self.assertEqual((kind, output), ("output", {"ok": True}))
        # 2 cadence flushes + the final flush + the status update.
        self.assertLessEqual(len(writes), 4)
        self.assertEqual(self.run_cursor(), 250)
        self.assertEqual(
            self.on_disk()["sessions"]["s1"]["runs"][0]["status"], "success"
        )

    def test_error_flushes_the_cursor(self):
        flow, _ = self.flow(cursor_flush_events=100)
        runtime = _Runtime([_ev(n) for n in range(1, 20)], fail_after=7)
        with self.assertRaises(CapitolError):
            flow._supervise(runtime, "s1", "draft", "r1")
        self.assertEqual(self.run_cursor(), 7)

    def test_park_persists_exact_cursor_and_skips_replays(self):
        flow, _ = self.flow(cursor_flush_events=100)
        events = [_ev(5), _ev(6), _ev(4), _ev(6),
                  _ev(7, "node.input_required",
                      data={"request_id": "q", "input_kind":
                            "clarification", "prompt": "size?"})]
        kind, reply = flow._supervise(_Runtime(events), "s1", "draft",
                                      "r1", since=5)
        self.assertEqual(kind, "parked")
        self.assertIn("size?", reply)
        session = self.on_disk()["sessions"]["s1"]
        self.assertEqual(session["hitl"]["last_sequence"], 7)
        self.assertEqual(self.run_cursor(), 7)


if __name__ == "__main__":
    unittest.main()
