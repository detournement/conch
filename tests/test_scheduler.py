import tempfile
import time
import unittest
from unittest import mock

from conch.scheduler import Scheduler


class SchedulerTests(unittest.TestCase):
    def test_scheduler_runs_executor(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                sched = Scheduler()
                sched.set_executor(lambda prompt, task: seen.append((prompt, task.id)))
                task = sched.add("hello", 1, run_once=True)
                task.next_run_at = time.time() - 1
                sched._run_due_tasks()
                self.assertEqual(seen, [("hello", task.id)])
                self.assertFalse(task.active)


if __name__ == "__main__":
    unittest.main()
