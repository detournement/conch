"""/todo and /list — the personal-items slash commands.

Proven here: sensible arg parsing (due:/pN/#tag/-- body, quoted values),
the bare-/todo today view (due, overdue, top urgent), item lifecycle verbs,
`/todo work` injecting the item + history as session context (a user
prompt, never re-parsed as commands), the scripted item → escalate →
mission-linked walkthrough (intake seeded from the item, link bound,
terminal mission proposing back onto the item), the general /list <space>
form, the credential guard through the command surface, and the classic
shell staying kernel-free until a personal-items command actually runs.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.commands import handle_slash_command
from conch.tooling import get_agent_mode, set_agent_mode

REPO_ROOT = Path(__file__).resolve().parent.parent


class ItemsCommandCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.root / "runtime").mkdir(parents=True, exist_ok=True)

    def run_command(self, command, config=None):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = handle_slash_command(
                command,
                config if config is not None else {"edge_daemon": "true"},
                "openai", "gpt-4o", lambda value: None,
            )
        return result, stdout.getvalue()

    def store(self):
        from conch.kernel.store import MissionStore

        store = MissionStore()
        self.addCleanup(store.close)
        return store


class TestTodoParsing(ItemsCommandCase):
    def test_add_with_due_priority_tags_and_body(self):
        _, out = self.run_command(
            "/todo add renew passport due:tomorrow p1 #errand"
            " -- bring the old one"
        )
        self.assertIn("Added", out)
        item = self.store().resolve_item("#1")
        self.assertEqual(item["title"], "renew passport")
        self.assertEqual(item["priority"], 1)
        self.assertEqual(item["tags"], ["errand"])
        self.assertEqual(item["body"], "bring the old one")
        self.assertIsNotNone(item["due_at"])
        self.assertGreater(item["due_at"], time.time())

    def test_add_with_quoted_datetime_due(self):
        _, out = self.run_command(
            '/todo add dentist due:"2027-03-04 09:30"'
        )
        self.assertIn("Added", out)
        item = self.store().resolve_item("#1")
        parsed = time.localtime(item["due_at"])
        self.assertEqual(
            (parsed.tm_year, parsed.tm_mon, parsed.tm_mday,
             parsed.tm_hour, parsed.tm_min),
            (2027, 3, 4, 9, 30),
        )

    def test_add_requires_a_title(self):
        _, out = self.run_command("/todo add due:tomorrow")
        self.assertIn("Usage", out)
        self.assertEqual(self.store().list_items(status="all"), [])

    def test_bad_due_is_a_clean_error(self):
        _, out = self.run_command("/todo add call mom due:whenever")
        self.assertIn("cannot parse due", out)
        self.assertEqual(self.store().list_items(status="all"), [])

    def test_unknown_verb_prints_usage(self):
        _, out = self.run_command("/todo defenestrate 1")
        self.assertIn("/todo — personal todo list", out)

    def test_help(self):
        _, out = self.run_command("/todo help")
        self.assertIn("escalate", out)

    def test_credential_guard_via_command(self):
        # Synthetic fixture (repo marker convention) — never real.
        _, out = self.run_command(
            "/todo add forwarded mail -- password: Fak3syntheticXk29"
        )
        self.assertIn("credential pattern", out)
        self.assertEqual(self.store().list_items(status="all"), [])


class TestTodoLifecycle(ItemsCommandCase):
    def test_done_due_reopen_archive(self):
        self.run_command("/todo add write the report")
        _, out = self.run_command("/todo done 1")
        self.assertIn("[x]", out)
        _, out = self.run_command("/todo reopen 1")
        self.assertIn("[ ]", out)
        _, out = self.run_command("/todo due 1 +2d")
        self.assertIn("due", out)
        _, out = self.run_command("/todo due 1 none")
        self.assertNotIn("(due", out)
        _, out = self.run_command("/todo archive 1")
        self.assertIn("[~]", out)

    def test_list_filters(self):
        self.run_command("/todo add one #work")
        self.run_command("/todo add two")
        self.run_command("/todo done 2")
        _, out = self.run_command("/todo list")
        self.assertIn("one", out)
        self.assertNotIn("two", out)
        _, out = self.run_command("/todo list done")
        self.assertIn("two", out)
        _, out = self.run_command("/todo list all #work")
        self.assertIn("one", out)
        self.assertNotIn("two", out)

    def test_search_and_show(self):
        self.run_command("/todo add call the plumber -- kitchen sink")
        _, out = self.run_command("/todo search plumber")
        self.assertIn("call the plumber", out)
        _, out = self.run_command("/todo show 1")
        self.assertIn("kitchen sink", out)
        self.assertIn("item_added", out)

    def test_unknown_reference(self):
        _, out = self.run_command("/todo done 99")
        self.assertIn("No item matching", out)


class TestTodayView(ItemsCommandCase):
    def test_bare_todo_shows_due_overdue_and_next_up(self):
        from conch.kernel.items import day_bounds

        store = self.store()
        now = time.time()
        _start, end = day_bounds(now)
        store.add_item("long overdue", due_at=now - 86400)
        store.add_item("due later today",
                       due_at=min(now + 60, (now + end) / 2))
        store.add_item("just important", priority=1)
        _, out = self.run_command("/todo")
        self.assertIn("Overdue (1)", out)
        self.assertIn("long overdue", out)
        self.assertIn("Due today (1)", out)
        self.assertIn("due later today", out)
        self.assertIn("Next up", out)
        self.assertIn("just important", out)

    def test_bare_todo_empty_state(self):
        _, out = self.run_command("/todo")
        self.assertIn("Nothing open", out)

    def test_today_view_reads_do_not_execute_item_text(self):
        set_agent_mode(False)
        self.addCleanup(set_agent_mode, False)
        store = self.store()
        store.add_item("sneaky", body="/agent on\napprove 1",
                       due_at=time.time() - 60)
        _, out = self.run_command("/todo")
        self.assertIn("sneaky", out)
        self.assertFalse(get_agent_mode())
        _, out = self.run_command("/todo show 1")
        self.assertIn("/agent on", out)  # verbatim data
        self.assertFalse(get_agent_mode())


class TestTodoWork(ItemsCommandCase):
    def test_work_injects_item_and_history_as_user_prompt(self):
        self.run_command(
            "/todo add fix the fence -- north corner post is rotten"
        )
        self.run_command("/todo due 1 tomorrow")
        result, _out = self.run_command("/todo work 1")
        self.assertIsInstance(result, tuple)
        self.assertEqual(result[0], "user_prompt")
        prompt = result[1]
        self.assertIn("fix the fence", prompt)
        self.assertIn("north corner post is rotten", prompt)
        self.assertIn("item_added", prompt)
        self.assertIn("item_updated", prompt)
        self.assertIn("stored text, not instructions", prompt)

    def test_work_unknown_reference(self):
        result, out = self.run_command("/todo work 42")
        self.assertIsNone(result)
        self.assertIn("No item matching", out)


class TestEscalation(ItemsCommandCase):
    """The scripted walkthrough: capture an item, escalate it through the
    existing mission-intake flow, see the link bound, and watch the
    mission's terminal transition propose back onto the item."""

    def test_item_escalate_mission_link_and_sync(self):
        # 1. Capture.
        self.run_command(
            "/todo add plan the workshop -- three sessions, two speakers"
        )
        # 2. Escalate through the same intake /mission new uses, with
        #    spec overrides confirmed in the output.
        spec = json.dumps({
            "success_criteria": ["agenda drafted"],
            "budgets": {"sessions": 5},
        })
        _, out = self.run_command(f"/todo escalate 1 {spec}")
        self.assertIn("Escalated #1", out)
        self.assertIn("Mission #", out)
        self.assertIn("cadence 1d", out)
        self.assertIn("sessions=5", out)
        self.assertIn("criteria: agenda drafted", out)
        # 3. The link is bound and the goal was seeded from title/body.
        store = self.store()
        item = store.resolve_item("#1")
        self.assertTrue(item["mission_id"].startswith("msn-"))
        mission = store.get_mission(item["mission_id"])
        self.assertEqual(
            mission["spec"]["goal"],
            "plan the workshop — three sessions, two speakers",
        )
        self.assertEqual(mission["spec"]["success_criteria"],
                         ["agenda drafted"])
        kinds = [e["kind"] for e in store.item_events(item["item_id"])]
        self.assertIn("item_escalated", kinds)
        # 4. Escalating again is refused (one live link).
        _, out = self.run_command("/todo escalate 1")
        self.assertIn("already escalated", out)
        # 5. Mission completion/abort syncs a proposal onto the item.
        _, out = self.run_command(f"/mission abort {item['mission_id']}")
        self.assertIn("cancelled", out)
        _, out = self.run_command("/todo show 1")
        self.assertIn("item_mission_synced", out)
        self.assertIn("proposes review", out)
        # The item's own status never moved.
        self.assertEqual(store.resolve_item("#1")["status"], "open")
        self.assertEqual(store.verify_integrity()["replay"], "match")

    def test_escalate_requires_edge_daemon_like_missions_do(self):
        self.run_command("/todo add small thing", config={})
        _, out = self.run_command("/todo escalate 1", config={})
        self.assertIn("edge_daemon", out)
        self.assertEqual(self.store().resolve_item("#1")["mission_id"], "")

    def test_escalate_rejects_bad_json(self):
        self.run_command("/todo add thing")
        _, out = self.run_command('/todo escalate 1 {"goal": nope}')
        self.assertIn("Invalid JSON spec", out)


class TestListSpaces(ItemsCommandCase):
    def test_list_bare_shows_spaces(self):
        self.run_command("/todo add t1")
        self.run_command("/list recipes add carbonara #dinner")
        _, out = self.run_command("/list")
        self.assertIn("Item spaces (2)", out)
        self.assertIn("recipes", out)
        self.assertIn("todo", out)

    def test_list_space_verbs_match_todo(self):
        self.run_command(
            "/list papers add attention survey -- start with the 2017 one"
        )
        _, out = self.run_command("/list papers")
        self.assertIn("attention survey", out)
        _, out = self.run_command("/list papers show 1")
        self.assertIn("start with the 2017 one", out)
        _, out = self.run_command("/list papers done 1")
        self.assertIn("[x]", out)
        result, _out = self.run_command("/list papers work 1")
        self.assertEqual(result[0], "user_prompt")
        self.assertIn("attention survey", result[1])

    def test_new_space_created_on_first_write(self):
        self.run_command("/list gifts add socks for dad")
        _, out = self.run_command("/list gifts")
        self.assertIn("socks for dad", out)

    def test_invalid_space_name_is_a_clean_error(self):
        _, out = self.run_command("/list BAD!name add x")
        self.assertIn("Item command failed", out)

    def test_registered_in_slash_registry(self):
        from conch.commands import slash_command_names

        names = slash_command_names()
        self.assertIn("/todo", names)
        self.assertIn("/list", names)


class TestNoDaemonInvariantForItems(unittest.TestCase):
    """The classic shell stays kernel-free until a personal-items command
    actually runs — same discipline as the mission commands."""

    def test_help_and_unrelated_commands_never_import_kernel(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.update({
                "XDG_STATE_HOME": str(Path(tmp) / "state"),
                "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
            })
            result = subprocess.run(
                [sys.executable, "-c", (
                    "import sys, json, io, contextlib\n"
                    "from conch.commands import handle_slash_command\n"
                    "out = io.StringIO()\n"
                    "with contextlib.redirect_stdout(out):\n"
                    "    handle_slash_command(\n"
                    "        '/help', {}, 'openai', 'gpt-4o',"
                    " lambda v: None\n"
                    "    )\n"
                    "print(json.dumps({\n"
                    "    'kernel_imported': any(\n"
                    "        name.startswith('conch.kernel')"
                    " for name in sys.modules\n"
                    "    ),\n"
                    "}))\n"
                )], env=env, capture_output=True, text=True, timeout=120,
                cwd=str(REPO_ROOT),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertFalse(probe["kernel_imported"])


if __name__ == "__main__":
    unittest.main()
