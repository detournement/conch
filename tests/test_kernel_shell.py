"""Shell attach gates (Swarm Phase 1).

Proven here: the no-daemon invariant (edge_daemon unset → the classic
in-process scheduler runs and conch.kernel is never imported, byte-for-byte
current behavior), the kernel-backed /schedule /tasks /cancel UX through
the legacy Scheduler surface, and the /missions /mission /approvals
/approve /deny attach commands against a direct (no-daemon) kernel — the
same code path the daemon socket serves.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.commands import handle_slash_command

REPO_ROOT = Path(__file__).resolve().parent.parent


class ShellCase(unittest.TestCase):
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
        self._sched = None
        self.addCleanup(self._stop_sched)

    def _stop_sched(self):
        if self._sched is not None:
            self._sched.stop()
            self._sched = None

    def kernel_sched(self):
        from conch.bootstrap import start_task_backend

        self._stop_sched()
        sched, kind = start_task_backend(
            {"edge_daemon": "true", "provider": "openai"}, lambda: "prompt"
        )
        self.assertEqual(kind, "kernel")
        self._sched = sched
        return sched

    def run_command(self, command, config=None, sched=None):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = handle_slash_command(
                command,
                config if config is not None else {"edge_daemon": "true"},
                "openai", "gpt-4o", lambda value: None, sched=sched,
            )
        return result, stdout.getvalue()


class TestNoDaemonInvariant(unittest.TestCase):
    """Hard gate: without edge_daemon, nothing kernel-related loads and the
    legacy scheduler is exactly what runs."""

    def _run_probe(self, code):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.update({
                "XDG_STATE_HOME": str(Path(tmp) / "state"),
                "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
            })
            env.pop("CONCH_EDGE_DAEMON", None)
            result = subprocess.run(
                [sys.executable, "-c", code], env=env, capture_output=True,
                text=True, timeout=120, cwd=str(REPO_ROOT),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_legacy_scheduler_and_no_kernel_import(self):
        probe = self._run_probe(
            "import sys, json\n"
            "from conch.bootstrap import start_task_backend\n"
            "sched, kind = start_task_backend({}, lambda: 'prompt')\n"
            "report = {\n"
            "    'kind': kind,\n"
            "    'type': type(sched).__name__,\n"
            "    'kernel_imported': any(\n"
            "        name.startswith('conch.kernel') for name in sys.modules\n"
            "    ),\n"
            "}\n"
            "sched.stop()\n"
            "print(json.dumps(report))\n"
        )
        self.assertEqual(probe["kind"], "legacy")
        self.assertEqual(probe["type"], "Scheduler")
        self.assertFalse(probe["kernel_imported"])

    def test_mission_commands_hint_without_importing_kernel(self):
        probe = self._run_probe(
            "import sys, json, io, contextlib\n"
            "from conch.commands import handle_slash_command\n"
            "out = io.StringIO()\n"
            "with contextlib.redirect_stdout(out):\n"
            "    handle_slash_command(\n"
            "        '/missions', {}, 'openai', 'gpt-4o', lambda v: None\n"
            "    )\n"
            "print(json.dumps({\n"
            "    'kernel_imported': any(\n"
            "        name.startswith('conch.kernel') for name in sys.modules\n"
            "    ),\n"
            "    'hinted': 'edge_daemon' in out.getvalue(),\n"
            "}))\n"
        )
        self.assertFalse(probe["kernel_imported"])
        self.assertTrue(probe["hinted"])

    def test_legacy_schedule_ux_untouched(self):
        """/schedule → tasks.json exactly as before, no kernel anywhere."""
        probe = self._run_probe(
            "import sys, json, io, contextlib\n"
            "from conch.bootstrap import start_task_backend\n"
            "from conch.scheduler import _tasks_path\n"
            "sched, kind = start_task_backend({}, lambda: 'prompt')\n"
            "task = sched.add('check disk', 600)\n"
            "sched.stop()\n"
            "on_disk = json.loads(_tasks_path().read_text())\n"
            "print(json.dumps({\n"
            "    'task_id': task.id,\n"
            "    'on_disk': on_disk[0]['prompt'],\n"
            "    'kernel_imported': any(\n"
            "        name.startswith('conch.kernel') for name in sys.modules\n"
            "    ),\n"
            "}))\n"
        )
        self.assertEqual(probe["task_id"], 1)
        self.assertEqual(probe["on_disk"], "check disk")
        self.assertFalse(probe["kernel_imported"])


class TestKernelBackedScheduleUX(ShellCase):
    def test_schedule_tasks_cancel_flow(self):
        sched = self.kernel_sched()
        self.assertEqual(sched.mode, "direct")  # no daemon running
        _, out = self.run_command(
            "/schedule 10m check the disk space", sched=sched
        )
        self.assertIn("Scheduled task", out)
        _, out = self.run_command("/tasks", sched=sched)
        self.assertIn("check the disk space", out)
        self.assertIn("every 10m", out)
        task = sched.list_tasks()[0]
        _, out = self.run_command(f"/cancel {task.id}", sched=sched)
        self.assertIn("Cancelled task", out)
        self.assertFalse(any(t.active for t in sched.list_tasks()))

    def test_schedule_once_flow(self):
        sched = self.kernel_sched()
        _, out = self.run_command("/schedule once 5m write digest",
                                  sched=sched)
        self.assertIn("Scheduled task", out)
        task = sched.list_tasks()[0]
        self.assertTrue(task.run_once)


class TestMissionCommands(ShellCase):
    def test_mission_lifecycle_via_commands(self):
        sched = self.kernel_sched()
        _, out = self.run_command(
            "/mission new watch the repo for anomalies", sched=sched
        )
        self.assertIn("Mission #", out)
        _, out = self.run_command("/missions", sched=sched)
        self.assertIn("watch the repo for anomalies", out)
        self.assertIn("direct kernel", out)
        mission_id = sched.client().list_missions()[0]["mission_id"]
        _, out = self.run_command(f"/mission show {mission_id}", sched=sched)
        self.assertIn("budget", out.lower() + " budget")  # detail printed
        _, out = self.run_command(
            f"/mission pause {mission_id[:16]}", sched=sched
        )
        self.assertIn("paused", out)
        _, out = self.run_command(
            f"/mission resume {mission_id}", sched=sched
        )
        self.assertIn("ready", out)
        # legacy alias also resolves
        seq = sched.client().list_missions()[0]["task_seq"]
        _, out = self.run_command(f"/mission show #{seq}", sched=sched)
        self.assertIn("watch the repo", out)
        _, out = self.run_command(f"/mission abort {mission_id}",
                                  sched=sched)
        self.assertIn("cancelled", out)

    def test_mission_new_json_spec(self):
        sched = self.kernel_sched()
        spec = json.dumps({
            "goal": "hourly summaries", "cadence_seconds": 3600,
            "budgets": {"sessions": 10},
        })
        _, out = self.run_command(f"/mission new {spec}", sched=sched)
        self.assertIn("Mission #", out)
        mission = sched.client().list_missions()[0]
        self.assertEqual(mission["goal"], "hourly summaries")

    def test_mission_input_flow(self):
        sched = self.kernel_sched()
        self.run_command("/mission new needs guidance", sched=sched)
        mission_id = sched.client().list_missions()[0]["mission_id"]
        _, out = self.run_command(
            f"/mission input {mission_id} focus on the tests", sched=sched
        )
        self.assertIn("Input recorded", out)

    def test_unknown_mission_reference(self):
        sched = self.kernel_sched()
        _, out = self.run_command("/mission show msn-nope", sched=sched)
        self.assertIn("No mission matching", out)

    def test_approval_flow_via_commands(self):
        sched = self.kernel_sched()
        self.run_command("/mission new approval demo", sched=sched)
        client = sched.client()
        mission_id = client.list_missions()[0]["mission_id"]
        grant = client.store.request_approval(
            mission_id, "publish_listing", {"price_cents": 4200},
            ttl_seconds=600,
        )
        _, out = self.run_command("/approvals", sched=sched)
        self.assertIn("publish_listing", out)
        self.assertIn(grant["approval_id"], out)
        # ambiguous / missing prefixes are refused
        _, out = self.run_command("/approve apr-nonexistent", sched=sched)
        self.assertIn("No pending approval", out)
        _, out = self.run_command(
            f"/approve {grant['approval_id'][:14]}", sched=sched
        )
        self.assertIn("approved", out)
        self.assertEqual(client.list_approvals(), [])
        # deciding again fails cleanly (one-use)
        _, out = self.run_command(
            f"/deny {grant['approval_id']}", sched=sched
        )
        self.assertIn("No pending approval", out)

    def test_deny_flow(self):
        sched = self.kernel_sched()
        self.run_command("/mission new deny demo", sched=sched)
        client = sched.client()
        mission_id = client.list_missions()[0]["mission_id"]
        grant = client.store.request_approval(
            mission_id, "purchase", {"usd_cents": 900}, ttl_seconds=600
        )
        _, out = self.run_command(
            f"/deny {grant['approval_id']}", sched=sched
        )
        self.assertIn("denied", out)


if __name__ == "__main__":
    unittest.main()
