"""Personal items kernel aggregate (personal-items plan P1).

Proven here: the items aggregate follows the full kernel event discipline
(immutable per-item hash chains → projection, replay == live), deterministic
urgency and due-window queries under a fixed clock (overdue > due-today >
explicit priority > age, ties broken stably), the credential write-guard
(whole-item rejection), the item status machine (reopen allowed, done and
archived only via their own events), the escalation link (item → mission,
one live link, terminal missions journal a proposal back onto the item in
the same transaction), and the memory fence (item events are never
consolidation input; items never appear in memory retrieval or mission
rehydration).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.kernel import items as items_mod
from conch.kernel.model import (
    EVENT_KINDS,
    ITEM_EVENT_KINDS,
    ConflictError,
    ItemStatus,
    KernelError,
    MissionState,
    kernel_id,
    normalize_item_priority,
    normalize_item_tags,
    normalize_space,
    parse_kernel_id,
)
from conch.kernel.store import MissionStore
from conch.secretguard import CredentialRejected


class FakeClock:
    def __init__(self, start=1_790_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class ItemsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "kernel" / "kernel.db"
        # Anchor the fake clock at local midday so "today" windows can
        # never straddle a midnight, whatever timezone runs the suite.
        start, _end = items_mod.day_bounds(1_790_000_000.0)
        self.clock = FakeClock(start + 12 * 3600)
        self.store = MissionStore(self.db_path, clock=self.clock)
        self.addCleanup(self.store.close)

    def add(self, title, **kwargs):
        return self.store.add_item(title, **kwargs)


class TestItemModel(unittest.TestCase):
    def test_item_events_registered_and_id_kind_parses(self):
        self.assertTrue(ITEM_EVENT_KINDS <= EVENT_KINDS)
        self.assertEqual(parse_kernel_id(kernel_id("item")), "item")

    def test_space_normalization(self):
        self.assertEqual(normalize_space(""), "todo")
        self.assertEqual(normalize_space("Recipes"), "recipes")
        with self.assertRaises(KernelError):
            normalize_space("no spaces allowed")
        with self.assertRaises(KernelError):
            normalize_space("x" * 40)

    def test_priority_bounds(self):
        self.assertIsNone(normalize_item_priority(None))
        self.assertEqual(normalize_item_priority("p2"), 2)
        self.assertEqual(normalize_item_priority(5), 5)
        with self.assertRaises(KernelError):
            normalize_item_priority(0)
        with self.assertRaises(KernelError):
            normalize_item_priority(6)
        with self.assertRaises(KernelError):
            normalize_item_priority("soon")

    def test_tags_sorted_deduped_lowercased(self):
        self.assertEqual(
            normalize_item_tags(["#Zed", "alpha", "zed", ""]),
            ["alpha", "zed"],
        )
        with self.assertRaises(KernelError):
            normalize_item_tags(["bad tag"])


class TestItemCrud(ItemsCase):
    def test_add_projects_all_fields(self):
        due = self.clock.now + 3600
        item = self.add(
            "renew passport", body="bring the old one", due_at=due,
            priority=1, tags=["errand", "#Gov"], source="chat",
            actor="user",
        )
        self.assertEqual(item["space"], "todo")
        self.assertEqual(item["status"], ItemStatus.OPEN)
        self.assertEqual(item["title"], "renew passport")
        self.assertEqual(item["body"], "bring the old one")
        self.assertEqual(item["due_at"], due)
        self.assertEqual(item["priority"], 1)
        self.assertEqual(item["tags"], ["errand", "gov"])
        self.assertEqual(item["mission_id"], "")
        self.assertEqual(item["version"], 1)
        self.assertIsInstance(item["item_seq"], int)

    def test_title_required(self):
        with self.assertRaises(KernelError):
            self.add("   ")

    def test_update_fields_and_versioning(self):
        item = self.add("draft the talk")
        version = self.store.update_item(
            item["item_id"],
            {"title": "draft the keynote", "priority": 2,
             "tags": ["work"], "space": "papers"},
        )
        self.assertEqual(version, 2)
        updated = self.store.get_item(item["item_id"])
        self.assertEqual(updated["title"], "draft the keynote")
        self.assertEqual(updated["space"], "papers")
        self.assertEqual(updated["priority"], 2)
        with self.assertRaises(ConflictError):
            self.store.update_item(
                item["item_id"], {"body": "x"}, expected_version=1
            )

    def test_update_unknown_field_fails_closed(self):
        item = self.add("x")
        with self.assertRaises(KernelError):
            self.store.update_item(item["item_id"], {"urgency": 1})

    def test_due_can_be_cleared(self):
        item = self.add("call", due_at=self.clock.now + 60)
        self.store.update_item(item["item_id"], {"due_at": None})
        self.assertIsNone(self.store.get_item(item["item_id"])["due_at"])

    def test_status_machine(self):
        item = self.add("finish the report")
        self.store.complete_item(item["item_id"])
        self.assertEqual(
            self.store.get_item(item["item_id"])["status"], ItemStatus.DONE
        )
        with self.assertRaises(KernelError):
            self.store.complete_item(item["item_id"])  # done → done illegal
        # reopen rides item_updated; done/archived must use their events
        self.store.update_item(item["item_id"], {"status": "open"})
        self.assertEqual(
            self.store.get_item(item["item_id"])["status"], ItemStatus.OPEN
        )
        with self.assertRaises(KernelError):
            self.store.update_item(item["item_id"], {"status": "done"})
        self.store.archive_item(item["item_id"])
        self.assertEqual(
            self.store.get_item(item["item_id"])["status"],
            ItemStatus.ARCHIVED,
        )
        self.store.update_item(item["item_id"], {"status": "open"})

    def test_history_is_the_item_chain(self):
        item = self.add("read the paper")
        self.store.update_item(item["item_id"], {"priority": 1})
        self.store.complete_item(item["item_id"])
        kinds = [
            event["kind"]
            for event in self.store.item_events(item["item_id"])
        ]
        self.assertEqual(
            kinds, ["item_added", "item_updated", "item_completed"]
        )

    def test_replay_matches_live_after_full_lifecycle(self):
        item = self.add("lifecycle", due_at=self.clock.now + 60,
                        priority=3, tags=["t"])
        self.clock.advance(10)
        self.store.update_item(
            item["item_id"], {"body": "notes", "due_at": None}
        )
        other = self.add("stays open", space="recipes")
        self.store.complete_item(item["item_id"])
        self.store.update_item(item["item_id"], {"status": "open"})
        self.store.archive_item(item["item_id"])
        mission_id = self.store.create_mission(
            {"goal": "help", "budgets": {}}
        )
        self.store.escalate_item(other["item_id"], mission_id)
        self.store.transition_mission(mission_id, MissionState.READY)
        self.store.transition_mission(mission_id, MissionState.CANCELLED)
        report = self.store.verify_integrity()
        self.assertEqual(report["replay"], "match")

    def test_resolution_by_id_prefix_and_seq(self):
        item = self.add("resolve me")
        self.add("decoy")
        by_id = self.store.resolve_item(item["item_id"])
        self.assertEqual(by_id["item_id"], item["item_id"])
        by_prefix = self.store.resolve_item(item["item_id"][:24])
        self.assertEqual(by_prefix["item_id"], item["item_id"])
        by_seq = self.store.resolve_item(f"#{item['item_seq']}")
        self.assertEqual(by_seq["item_id"], item["item_id"])
        self.assertIsNone(self.store.resolve_item("item-"))  # ambiguous
        self.assertIsNone(self.store.resolve_item("zzz"))
        # A mission's seq alias is not an item.
        mission_id = self.store.create_mission(
            {"goal": "not an item", "budgets": {}}
        )
        seq = self.store.event_tail(mission_id, limit=1)[0]["seq"]
        self.assertIsNone(self.store.resolve_item(str(seq)))

    def test_list_filters_and_deterministic_order(self):
        first = self.add("a", tags=["x"])
        self.clock.advance(1)
        second = self.add("b", space="recipes")
        self.clock.advance(1)
        third = self.add("c", tags=["x", "y"])
        self.store.complete_item(second["item_id"])
        open_todo = self.store.list_items(space="todo")
        self.assertEqual(
            [item["item_id"] for item in open_todo],
            [first["item_id"], third["item_id"]],
        )
        self.assertEqual(
            [item["item_id"] for item in self.store.list_items(
                status="done"
            )],
            [second["item_id"]],
        )
        self.assertEqual(
            len(self.store.list_items(status="all")), 3
        )
        self.assertEqual(
            [item["item_id"] for item in self.store.list_items(tag="y")],
            [third["item_id"]],
        )
        with self.assertRaises(KernelError):
            self.store.list_items(status="pending")

    def test_search_escapes_like_wildcards(self):
        hit = self.add("battery at 100% now")
        self.add("battery at 10x now")
        rows = self.store.search_items("100%")
        self.assertEqual([row["item_id"] for row in rows],
                         [hit["item_id"]])

    def test_spaces_listing(self):
        self.add("t1")
        self.add("r1", space="recipes")
        done = self.add("t2")
        self.store.complete_item(done["item_id"])
        spaces = self.store.list_item_spaces()
        self.assertEqual(
            spaces,
            [
                {"space": "recipes", "total": 1, "open": 1},
                {"space": "todo", "total": 2, "open": 1},
            ],
        )


class TestItemCredentialGuard(ItemsCase):
    """All credential values here are synthetic fixtures carrying the
    repo's marker convention (SynthFix / Fak3synthetic / allowlisted in
    .gitleaks.toml) — never real."""

    def test_add_rejected_whole(self):
        with self.assertRaises(CredentialRejected) as ctx:
            self.add("creds", body="password: Fak3syntheticHunter2")
        self.assertIn("secret_assignment", ctx.exception.types)
        self.assertEqual(self.store.list_items(status="all"), [])

    def test_title_and_tags_are_guarded(self):
        with self.assertRaises(CredentialRejected):
            self.add("token sk-abcdefghij0123456789SynthFix")
        item = self.add("clean")
        with self.assertRaises(CredentialRejected):
            self.store.update_item(
                item["item_id"],
                {"body": "AKIASYNTHETICFIXTURE is the aws access key"},
            )
        self.assertEqual(self.store.get_item(item["item_id"])["body"], "")

    def test_forwarded_email_style_body_rejected(self):
        # The plan's example: a forwarded email carrying a password is
        # rejected whole, never sanitized (same detector as the memory
        # write gate — value-bearing assignment shapes).
        body = (
            "From: it@example.com\nSubject: your account\n\n"
            "username: thom\npassword: Fak3syntheticXk29\n"
        )
        with self.assertRaises(CredentialRejected):
            self.add("forwarded email", body=body)
        self.assertEqual(self.store.list_items(status="all"), [])


class TestUrgencyDeterminism(ItemsCase):
    """Fixed-clock fixtures: the urgency order and due windows are pure
    functions of (items, now) — two runs can never disagree. The clock
    advances 60s between adds so every created_at is distinct."""

    def fixture(self):
        base = self.clock.now  # local midday
        _start, end = items_mod.day_bounds(base)
        named = {}
        for name, title, kwargs in (
            ("overdue_old", "overdue old", {"due_at": base - 7200}),
            ("overdue_new", "overdue new", {"due_at": base - 60}),
            ("due_soon", "due soon", {"due_at": base + 3600}),
            ("due_tonight", "due tonight", {"due_at": end - 1}),
            ("p1", "p1 item", {"priority": 1}),
            ("p3", "p3 item", {"priority": 3}),
            ("aged", "aged undated", {}),
            ("future", "due next week", {"due_at": base + 7 * 86400}),
            ("fresh", "fresh undated", {}),
        ):
            named[name] = self.add(title, **kwargs)
            self.clock.advance(60)
        return named

    def test_fixed_order_overdue_due_today_priority_age(self):
        self.fixture()
        now = self.clock.now  # base + 540s: still the same local day
        ordered = items_mod.most_urgent(self.store, now, limit=20)
        titles = [item["title"] for item in ordered]
        self.assertEqual(titles, [
            "overdue old", "overdue new",   # overdue, most overdue first
            "due soon", "due tonight",      # due today, soonest first
            "p1 item", "p3 item",           # explicit priority
            "aged undated",                 # undated: oldest first
            "due next week",                # future-due ranks by age tier
            "fresh undated",
        ])
        # Deterministic: a second computation is identical.
        again = items_mod.most_urgent(self.store, now, limit=20)
        self.assertEqual([item["item_id"] for item in ordered],
                         [item["item_id"] for item in again])

    def test_due_windows(self):
        named = self.fixture()
        now = self.clock.now
        self.assertEqual(
            [item["title"] for item in items_mod.overdue(self.store, now)],
            ["overdue old", "overdue new"],
        )
        self.assertEqual(
            [item["title"] for item in items_mod.due_today(
                self.store, now
            )],
            ["due soon", "due tonight"],
        )
        # The future-due item is neither overdue nor due today.
        self.assertNotIn(
            named["future"]["item_id"],
            [item["item_id"] for item in items_mod.due_today(
                self.store, now
            )],
        )

    def test_urgency_reasons_are_explainable(self):
        named = self.fixture()
        now = self.clock.now
        self.assertTrue(items_mod.urgency_reason(
            named["overdue_old"], now
        ).startswith("overdue"))
        self.assertTrue(items_mod.urgency_reason(
            named["due_soon"], now
        ).startswith("due today"))
        self.assertEqual(
            items_mod.urgency_reason(named["p1"], now), "priority p1"
        )
        self.assertTrue(items_mod.urgency_reason(
            named["aged"], now
        ).startswith("open"))

    def test_done_items_never_rank(self):
        item = self.add("finished", due_at=self.clock.now - 60)
        self.store.complete_item(item["item_id"])
        self.assertEqual(
            items_mod.most_urgent(self.store, self.clock.now, 10), []
        )

    def test_parse_due_forms(self):
        now = self.clock.now
        _start, end = items_mod.day_bounds(now)
        self.assertEqual(items_mod.parse_due("today", now), end - 1)
        self.assertEqual(
            items_mod.parse_due("tomorrow", now), end - 1 + 86400
        )
        self.assertEqual(items_mod.parse_due("+2d", now), now + 172800)
        self.assertEqual(items_mod.parse_due("+3h", now), now + 10800)
        self.assertEqual(items_mod.parse_due("+45m", now), now + 2700)
        self.assertIsNone(items_mod.parse_due("none", now))
        self.assertIsNone(items_mod.parse_due(None, now))
        exact = items_mod.parse_due("2027-01-05 09:30", now)
        import time as _time
        parsed = _time.localtime(exact)
        self.assertEqual(
            (parsed.tm_year, parsed.tm_mon, parsed.tm_mday,
             parsed.tm_hour, parsed.tm_min),
            (2027, 1, 5, 9, 30),
        )
        date_only = items_mod.parse_due("2027-01-05", now)
        parsed = _time.localtime(date_only)
        self.assertEqual(
            (parsed.tm_hour, parsed.tm_min, parsed.tm_sec), (23, 59, 59)
        )
        with self.assertRaises(KernelError):
            items_mod.parse_due("whenever", now)


class TestEscalation(ItemsCase):
    def mission(self, **overrides):
        spec = {"goal": "escalated work", "budgets": {}}
        spec.update(overrides)
        return self.store.create_mission(spec)

    def test_escalate_binds_one_live_link(self):
        item = self.add("big rock")
        mission_id = self.mission()
        self.store.escalate_item(item["item_id"], mission_id)
        linked = self.store.get_item(item["item_id"])
        self.assertEqual(linked["mission_id"], mission_id)
        self.assertEqual(
            [row["item_id"] for row in self.store.find_items_by_mission(
                mission_id
            )],
            [item["item_id"]],
        )
        with self.assertRaises(KernelError):
            self.store.escalate_item(item["item_id"], self.mission())

    def test_escalate_requires_open_item_and_known_mission(self):
        done = self.add("already done")
        self.store.complete_item(done["item_id"])
        with self.assertRaises(KernelError):
            self.store.escalate_item(done["item_id"], self.mission())
        item = self.add("fine")
        with self.assertRaises(KernelError):
            self.store.escalate_item(item["item_id"], "msn-missing")

    def test_terminal_mission_syncs_a_proposal_event(self):
        item = self.add("ship the feature")
        mission_id = self.mission()
        self.store.escalate_item(item["item_id"], mission_id)
        self.store.transition_mission(mission_id, MissionState.READY)
        self.store.transition_mission(mission_id, MissionState.ACTIVE)
        self.store.transition_mission(mission_id, MissionState.SUCCEEDED)
        events = self.store.item_events(item["item_id"])
        synced = [e for e in events if e["kind"] == "item_mission_synced"]
        self.assertEqual(len(synced), 1)
        self.assertEqual(synced[0]["data"]["proposal"], "complete")
        self.assertEqual(synced[0]["data"]["mission_id"], mission_id)
        # The proposal never moves the item's status by itself.
        self.assertEqual(
            self.store.get_item(item["item_id"])["status"], ItemStatus.OPEN
        )
        self.assertEqual(self.store.verify_integrity()["replay"], "match")

    def test_aborted_mission_proposes_review(self):
        item = self.add("watch the thing")
        mission_id = self.mission()
        self.store.escalate_item(item["item_id"], mission_id)
        self.store.transition_mission(mission_id, MissionState.READY)
        self.store.transition_mission(mission_id, MissionState.CANCELLED)
        synced = [
            e for e in self.store.item_events(item["item_id"])
            if e["kind"] == "item_mission_synced"
        ]
        self.assertEqual(synced[0]["data"]["proposal"], "review")

    def test_archived_items_are_not_synced(self):
        item = self.add("stale link")
        mission_id = self.mission()
        self.store.escalate_item(item["item_id"], mission_id)
        self.store.archive_item(item["item_id"])
        self.store.transition_mission(mission_id, MissionState.READY)
        self.store.transition_mission(mission_id, MissionState.CANCELLED)
        kinds = [
            e["kind"] for e in self.store.item_events(item["item_id"])
        ]
        self.assertNotIn("item_mission_synced", kinds)


class TestMemoryFence(ItemsCase):
    """Items are never auto-saved as memories; consolidation and the
    interactive memory retrieval paths never read personal spaces."""

    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(self._tmp.name) / "state"),
            "XDG_CONFIG_HOME": str(Path(self._tmp.name) / "config"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_item_events_disjoint_from_consolidation_input(self):
        from conch.kernel.consolidate import DELTA_EVENT_KINDS

        self.assertFalse(set(DELTA_EVENT_KINDS) & ITEM_EVENT_KINDS)

    def test_consolidation_delta_carries_no_item_content(self):
        from conch.kernel import consolidate as consolidate_mod

        marker_title = "FENCE-MARKER-TITLE"
        marker_body = "FENCE-MARKER-BODY"
        self.add(marker_title, body=marker_body)
        mission_id = self.store.create_mission(
            {"goal": "unrelated mission", "budgets": {}}
        )
        linked = self.add("linked item", body=marker_body)
        self.store.escalate_item(linked["item_id"], mission_id)
        mission = self.store.get_mission(mission_id)
        text = consolidate_mod.session_delta_text(
            self.store, mission, 6000
        )
        self.assertNotIn(marker_title, text)
        self.assertNotIn(marker_body, text)

    def test_mission_rehydration_carries_no_item_content(self):
        from conch.kernel.engine import MissionEngine

        marker = "FENCE-MARKER-REHYDRATE"
        self.add(marker, body=marker)
        mission_id = self.store.create_mission(
            {"goal": "plain mission", "budgets": {}}
        )
        engine = MissionEngine(self.store, {}, holder="test")
        context = engine.build_context(self.store.get_mission(mission_id))
        self.assertNotIn(marker, context)

    def test_items_never_reach_memory_store(self):
        from conch.memory import MemoryStore

        self.add("remember the milk", body="two liters")
        memory = MemoryStore()
        self.assertEqual(memory.get_all(), [])
        self.assertEqual(memory.build_context("remember the milk"), "")


if __name__ == "__main__":
    unittest.main()
