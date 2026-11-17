"""The subagent panel (/agents): the activity registry fan-out and
delegate_task report into, the panel's settings editing (through the
settings registry), the picker round trip, and the command registration."""

import contextlib
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from conch import settings_registry, subagent_activity
from conch.agents_panel import PICK_MODEL, AgentsPanel, admin_loop, run_agents_panel
from conch.config import load_config
from conch.tooling import DelegateTaskClient
from tests.test_fanout import _Parent


class _ActivityCase(unittest.TestCase):
    def setUp(self):
        subagent_activity.clear()
        self.addCleanup(subagent_activity.clear)

    def statuses(self):
        return [(run.kind, run.label, run.status) for run in subagent_activity.snapshot()]


class RegistryTests(_ActivityCase):
    def test_running_first_oldest_first_then_most_recent_finished(self):
        first = subagent_activity.start("fan-out", "one\nmore detail", "openai/a")
        second = subagent_activity.start("delegate", "two", "openai/b")
        third = subagent_activity.start("fan-out", "three", "openai/c")
        fourth = subagent_activity.start("fan-out", "four", "openai/c")
        subagent_activity.finish(second, "failed", "boom")
        subagent_activity.finish(fourth, "done")
        runs = subagent_activity.snapshot()
        self.assertEqual([run.label for run in runs], ["one", "three", "four", "two"])
        self.assertEqual([run.status for run in runs],
                         ["running", "running", "done", "failed"])
        self.assertTrue(runs[0].running)
        self.assertIsNotNone(runs[2].ended)
        subagent_activity.finish(first, "cancelled")
        subagent_activity.finish(third, "done")
        self.assertFalse(any(run.running for run in subagent_activity.snapshot()))

    def test_finished_runs_are_capped_and_finishing_twice_is_ignored(self):
        tokens = [subagent_activity.start("fan-out", f"task {i}") for i in range(40)]
        for token in tokens:
            subagent_activity.finish(token, "done")
        subagent_activity.finish(tokens[-1], "failed")
        runs = subagent_activity.snapshot()
        self.assertEqual(len(runs), subagent_activity.KEEP_FINISHED)
        self.assertEqual(runs[0].label, "task 39")
        self.assertEqual(runs[0].status, "done")

    def test_unknown_status_is_refused(self):
        token = subagent_activity.start("delegate", "x")
        with self.assertRaises(ValueError):
            subagent_activity.finish(token, "maybe")


class FanOutActivityTests(_ActivityCase):
    def test_each_worker_is_recorded_with_its_outcome(self):
        def worker(envelope, timeout):
            if envelope["task"] == "two":
                return {"ok": False, "error": "the API rejected this key"}
            return {"ok": True, "summary": "summary of " + envelope["task"]}

        _Parent(self, mode="safe_auto", worker=worker).fan_out("one", "two")
        self.assertEqual(sorted(self.statuses()),
                         [("fan-out", "one", "done"), ("fan-out", "two", "failed")])
        self.assertTrue(all(run.model == "custom/m" for run in subagent_activity.snapshot()))

    def test_a_worker_that_raises_is_recorded_as_failed(self):
        def worker(envelope, timeout):
            raise RuntimeError("worker crashed")

        parent = _Parent(self, mode="safe_auto", worker=worker)
        with self.assertRaises(RuntimeError):
            parent.fan_out("one")
        self.assertEqual(self.statuses(), [("fan-out", "one", "failed")])

    def test_stopped_workers_are_recorded_as_cancelled(self):
        parent = None

        def worker(envelope, timeout):
            parent.client.stop()
            return {"ok": False, "error": "cancelled"}

        parent = _Parent(self, mode="safe_auto", worker=worker)
        parent.fan_out("one")
        self.assertEqual(self.statuses(), [("fan-out", "one", "cancelled")])


class DelegateActivityTests(_ActivityCase):
    def client(self):
        client = DelegateTaskClient()
        client._chat_state = SimpleNamespace(tools=[], tool_map={})
        client._config = {"provider": "openai", "chat_model": "gpt-6-sol"}
        return client

    def call(self, client, **arguments):
        with contextlib.redirect_stderr(io.StringIO()):
            return client.call_tool("delegate_task", dict({"task": "look around"}, **arguments))

    def test_success_and_failure_replies(self):
        client = self.client()
        with mock.patch.object(DelegateTaskClient, "_run",
                               return_value=client._text("Subagent summary:\nall good")):
            self.call(client)
        with mock.patch.object(DelegateTaskClient, "_run",
                               return_value=client._text("Error: subagent failed: x")):
            self.call(client)
        runs = subagent_activity.snapshot()
        self.assertEqual([run.status for run in runs], ["failed", "done"])
        self.assertEqual(runs[1].model, "openai/gpt-6-sol")

    def test_an_exception_is_recorded_and_the_lock_released(self):
        client = self.client()
        with mock.patch.object(DelegateTaskClient, "_run", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                self.call(client)
        with mock.patch.object(DelegateTaskClient, "_run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.call(client)
        self.assertEqual([run.status for run in subagent_activity.snapshot()],
                         ["cancelled", "failed"])
        self.assertTrue(client._lock.acquire(blocking=False))

    def test_skill_scoped_runs_name_the_skill_and_its_model(self):
        client = self.client()
        skill = {"name": "review", "model": "claude-haiku-4-5", "provider": "anthropic",
                 "tools": None, "rounds": 0, "description": "", "body": "b"}
        with mock.patch("conch.skills.get_skill", return_value=skill), \
                mock.patch.object(DelegateTaskClient, "_run",
                                  return_value=client._text("Subagent summary:\nok")):
            self.call(client, skill="review")
        run = subagent_activity.snapshot()[0]
        self.assertIn("review", run.label)
        self.assertEqual((run.kind, run.model, run.status),
                         ("delegate", "anthropic/claude-haiku-4-5", "done"))


class _ConfigCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name, "HOME": tmp.name,
                                           "OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "x"})
        env.start()
        self.addCleanup(env.stop)
        self.config = {"provider": "openai", "chat_model": "gpt-6-sol", "model": "gpt-6-sol"}
        subagent_activity.clear()
        self.addCleanup(subagent_activity.clear)

    def panel(self, key, **options):
        return AgentsPanel(self.config, select=key, **options)


class PanelTests(_ConfigCase):
    def edit(self, panel, text):
        panel.edit()
        self.assertEqual(panel.mode, "edit")
        panel.edit_input.text = text
        panel.finish_edit(save=True)

    def test_editing_a_number_saves_it(self):
        panel = self.panel("fanout_concurrency")
        self.edit(panel, "3")
        self.assertEqual(panel.mode, "list")
        self.assertEqual(settings_registry.current_value(self.config, "fanout_concurrency"), "3")
        self.assertEqual(load_config().get("fanout_concurrency"), "3")
        self.assertTrue(panel.notice)

    def test_invalid_input_keeps_the_value_and_says_why(self):
        settings_registry.set_setting(self.config, "subagent_rounds", "12")
        panel = self.panel("subagent_rounds")
        for bad in ("lots", "0", "1000"):
            with self.subTest(bad=bad):
                self.edit(panel, bad)
                self.assertEqual(panel.mode, "edit")
                self.assertTrue(panel.notice)
                self.assertEqual(self.config["subagent_rounds"], "12")
                self.assertEqual(load_config().get("subagent_rounds"), "12")
                panel.finish_edit(save=False)

    def test_reset_restores_the_default(self):
        settings_registry.set_setting(self.config, "fanout_concurrency", "2")
        panel = self.panel("fanout_concurrency")
        panel.reset()
        setting = settings_registry.BY_KEY["fanout_concurrency"]
        self.assertEqual(settings_registry.current_value(self.config, "fanout_concurrency"),
                         settings_registry.default_of(setting))

    def test_model_settings_ask_for_the_picker(self):
        for key in ("fanout_model", "subagent_model", "side_task_model"):
            with self.subTest(key=key):
                panel = self.panel(key)
                panel.edit()
                self.assertEqual(panel.result, (PICK_MODEL, key))
                self.assertEqual(panel.mode, "list")

    def test_auto_only_for_the_fanout_model(self):
        panel = self.panel("subagent_model")
        panel.set_auto()
        self.assertFalse(self.config.get("subagent_model"))
        panel = self.panel("fanout_model")
        panel.set_auto()
        self.assertEqual(load_config().get("fanout_model"), "auto")

    def test_stop_reaches_running_fanout_workers_only(self):
        stop = mock.Mock()
        self.panel("fanout_model", stop_fanout=stop).stop()
        stop.assert_not_called()  # nothing running
        subagent_activity.start("fan-out", "work", "openai/gpt-6-sol")
        panel = self.panel("fanout_model")
        panel.stop()  # no stop callable: says so
        self.assertTrue(panel.notice)
        self.panel("fanout_model", stop_fanout=stop).stop()
        stop.assert_called_once_with()

    def test_keys_drive_the_running_panel(self):
        with create_pipe_input() as pipe:
            pipe.send_text("\r7\rq")
            result = run_agents_panel(self.config, select="fanout_concurrency",
                                      input=pipe, output=DummyOutput())
        self.assertIsNone(result)
        self.assertEqual(load_config().get("fanout_concurrency"), "7")
        with create_pipe_input() as pipe:
            pipe.send_text("\x1b[A" * 5 + "\r")
            result = run_agents_panel(self.config, select="subagent_rounds",
                                      input=pipe, output=DummyOutput())
        self.assertEqual(result, (PICK_MODEL, "fanout_model"))

    def test_rendering_shows_settings_skills_and_activity(self):
        subagent_activity.start("fan-out", "summarize the logs", "openai/gpt-6-luna")
        panel = self.panel("fanout_model")
        text = "\n".join("".join(fragment for _, fragment in line)
                         for line in panel._render_body(120, 40))
        for expected in ("fanout_concurrency", "side_task_model", "summarize the logs",
                         "openai/gpt-6-luna"):
            self.assertIn(expected, text)


class AdminLoopTests(_ConfigCase):
    def test_a_picked_model_is_saved_and_the_panel_reopens_on_it(self):
        calls = []
        outcomes = iter([(PICK_MODEL, "subagent_model"), (PICK_MODEL, "fanout_model"), None])

        def run_panel(select, notice):
            calls.append((select, notice))
            return next(outcomes)

        picks = iter([SimpleNamespace(label="openai/gpt-6-luna"), None])
        admin_loop(self.config, "openai", "gpt-6-sol", run_panel,
                   lambda config, provider, model, purpose=None: next(picks))
        self.assertEqual(load_config().get("subagent_model"), "openai/gpt-6-luna")
        self.assertEqual(self.config["subagent_model"], "openai/gpt-6-luna")
        self.assertFalse(self.config.get("fanout_model"))  # picker cancelled
        self.assertEqual([select for select, _ in calls],
                         [None, "subagent_model", "fanout_model"])
        self.assertTrue(calls[1][1])

    def test_an_unusable_pick_changes_nothing(self):
        outcomes = iter([(PICK_MODEL, "side_task_model"), None])
        notices = []

        def run_panel(select, notice):
            notices.append(notice)
            return next(outcomes)

        admin_loop(self.config, "openai", "gpt-6-sol", run_panel,
                   lambda *args, **kwargs: SimpleNamespace(label="llamaidx/some-model"))
        self.assertFalse(self.config.get("weak_model"))
        self.assertTrue(notices[-1])


class CommandTests(_ConfigCase):
    def test_agents_and_subagents_are_commands(self):
        from conch.commands import SLASH_COMMANDS, known_slash_commands

        listed = {spec.split()[0] for spec, _ in SLASH_COMMANDS}
        for name in ("/agents", "/subagents"):
            self.assertIn(name, known_slash_commands())
            self.assertIn(name, listed)

    def test_without_a_terminal_both_print_the_settings(self):
        from conch.commands import handle_slash_command

        for name in ("/agents", "/subagents"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    mock.patch("conch.convo_browser.can_browse", return_value=False):
                result = handle_slash_command(name, self.config, "openai", "gpt-6-sol",
                                              lambda value: None)
            self.assertIsNone(result)
            for key in ("fanout_model", "fanout_concurrency", "subagent_rounds"):
                self.assertIn(key, out.getvalue())


if __name__ == "__main__":
    unittest.main()
