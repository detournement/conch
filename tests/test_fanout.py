"""Fan-out: model selection, the parent tool's authority, and real workers."""

import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conch import fanout
from conch.fanout import FanOutClient, ModelChoice
from conch.session import AgentSession
from conch.tooling import LocalShellClient, LocalShellPolicy, PermissionState

KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CEREBRAS_API_KEY",
        "OPENROUTER_API_KEY", "AWS_BEARER_TOKEN_BEDROCK")


def _no_local_models():
    """No Ollama or custom endpoint answers during a test."""
    stack = contextlib.ExitStack()
    stack.enter_context(patch("conch.providers.list_ollama_models", return_value=[]))
    stack.enter_context(patch("conch.providers.list_custom_models", return_value=[]))
    return stack


class ModelTierTests(unittest.TestCase):
    def test_tiers_follow_seat_alias_price_band_and_local_size(self):
        for provider, model, tier in (
            ("claude-code", "haiku", "light"),
            ("claude-code", "sonnet", "standard"),
            ("claude-code", "opus", "heavy"),
            ("codex", "default", "standard"),
            ("openai", "gpt-6-luna", "light"),
            ("openai", "gpt-6-sol", "standard"),
            ("openai", "gpt-5.5", "heavy"),
            ("anthropic", "claude-opus-5-5", "heavy"),
            ("ollama", "qwen3:8b", "light"),
            ("ollama", "qwen3.8:27b", "standard"),
            ("ollama", "llama4:120b", "heavy"),
            ("custom", "some-mini-model", "light"),
            ("custom", "unlabelled", "standard"),
        ):
            with self.subTest(model=model):
                self.assertEqual(fanout.model_tier(provider, model), tier)

    def test_effort_is_inferred_only_from_the_task_text(self):
        self.assertEqual(fanout.classify_effort("List the files under src"), "light")
        self.assertEqual(fanout.classify_effort("Debug why the worker deadlocks"), "heavy")
        self.assertEqual(fanout.classify_effort("Port the parser to the new API"), "standard")


class InventoryTests(unittest.TestCase):
    """Auto may only choose among models that cost no more to reach than the
    session's own: a seat or local session never lands on API billing."""

    def inventory(self, config, *, keys=(), seats=(), ollama=()):
        environment = {name: "" for name in KEYS}
        environment.update({name: "key" for name in keys})
        with patch.dict(os.environ, environment), \
                patch("conch.providers.list_ollama_models", return_value=list(ollama)), \
                patch("conch.providers.list_custom_models", return_value=[]):
            return fanout.available_models(config, seat_ready=lambda name: name in seats)

    def providers(self, choices):
        return {choice.provider for choice in choices}

    def test_seat_session_never_offers_api_billed_models(self):
        choices = self.inventory(
            {"provider": "codex", "chat_model": "default"},
            keys=("OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
            seats=("claude-code",), ollama=("qwen3:8b",))
        self.assertEqual(self.providers(choices), {"codex", "claude-code", "ollama"})
        self.assertEqual(choices[0].label, "codex/default")

    def test_api_session_offers_its_catalog_other_keyed_providers_and_seats(self):
        choices = self.inventory(
            {"provider": "openai", "chat_model": "gpt-6-sol", "local_only": "false"},
            keys=("OPENAI_API_KEY", "ANTHROPIC_API_KEY"), seats=("codex",))
        self.assertEqual(self.providers(choices), {"openai", "anthropic", "codex"})
        self.assertIn("openai/gpt-6-luna", [choice.label for choice in choices])

    def test_unkeyed_provider_and_signed_out_seat_are_absent(self):
        choices = self.inventory(
            {"provider": "openai", "chat_model": "gpt-6-sol", "local_only": "false"},
            keys=("OPENAI_API_KEY",))
        self.assertEqual(self.providers(choices), {"openai"})

    def test_local_session_stays_local(self):
        choices = self.inventory(
            {"provider": "ollama", "chat_model": "qwen3.8:27b"},
            keys=("OPENAI_API_KEY",), seats=("codex",),
            ollama=("qwen3.8:27b", "qwen3:8b"))
        self.assertEqual(self.providers(choices), {"ollama"})

    def test_local_only_overrides_an_api_session(self):
        choices = self.inventory(
            {"provider": "openai", "chat_model": "gpt-6-sol", "local_only": "true"},
            keys=("OPENAI_API_KEY", "ANTHROPIC_API_KEY"), seats=("codex",),
            ollama=("qwen3:8b",))
        self.assertEqual(
            [choice.label for choice in choices if choice.provider != "openai"],
            ["ollama/qwen3:8b"])


class AutoChoiceTests(unittest.TestCase):
    def candidates(self):
        return [
            ModelChoice("openai", "gpt-6-sol", "standard", "api"),
            ModelChoice("openai", "gpt-6-luna", "light", "api"),
            ModelChoice("anthropic", "claude-haiku-4-5", "light", "api"),
            ModelChoice("anthropic", "claude-opus-5-5", "heavy", "api"),
        ]

    def test_effort_selects_the_tier_and_prefers_the_session_provider(self):
        for provider, effort, expected in (
            ("openai", "light", "openai/gpt-6-luna"),
            ("anthropic", "light", "anthropic/claude-haiku-4-5"),
            ("openai", "standard", "openai/gpt-6-sol"),
            ("openai", "heavy", "anthropic/claude-opus-5-5"),
        ):
            with self.subTest(provider=provider, effort=effort):
                choice = fanout.choose_auto(self.candidates(), effort, provider)
                self.assertEqual(choice.label, expected)

    def test_a_missing_tier_falls_to_a_stronger_model_before_a_weaker_one(self):
        only = [ModelChoice("codex", "default", "standard", "seat"),
                ModelChoice("ollama", "qwen3:8b", "light", "local")]
        self.assertEqual(fanout.choose_auto(only, "heavy", "codex").label, "codex/default")
        no_light = [ModelChoice("codex", "default", "standard", "seat")]
        self.assertEqual(fanout.choose_auto(no_light, "light", "codex").label, "codex/default")


class ResolveTests(unittest.TestCase):
    BASE = {"provider": "openai", "chat_model": "gpt-6-sol", "model": "gpt-6-sol",
            "local_only": "false"}

    def resolve(self, effort="standard", *, keys=("OPENAI_API_KEY",), **settings):
        environment = {name: "" for name in KEYS}
        environment.update({name: "key" for name in keys})
        config = dict(self.BASE, **settings)
        with patch.dict(os.environ, environment), _no_local_models():
            return fanout.resolve_model(
                config, effort,
                lambda: fanout.available_models(config, seat_ready=lambda name: False))

    def test_unset_and_default_use_the_session_model(self):
        for settings in ({}, {"fanout_model": "default"}):
            choice, warning = self.resolve(**settings)
            self.assertEqual((choice.label, warning), ("openai/gpt-6-sol", ""))

    def test_a_model_name_selects_it_on_the_session_provider(self):
        choice, warning = self.resolve(fanout_model="gpt-6-luna")
        self.assertEqual((choice.label, warning), ("openai/gpt-6-luna", ""))

    def test_subagent_model_applies_when_fanout_model_is_unset(self):
        self.assertEqual(self.resolve(subagent_model="gpt-6-luna")[0].label,
                         "openai/gpt-6-luna")
        self.assertEqual(
            self.resolve(subagent_model="gpt-6-luna", fanout_model="gpt-5.5")[0].label,
            "openai/gpt-5.5")

    def test_another_provider_needs_its_own_credential(self):
        setting = "anthropic/claude-haiku-4-5"
        choice, warning = self.resolve(fanout_model=setting)
        self.assertEqual(choice.label, "openai/gpt-6-sol")
        self.assertIn("ANTHROPIC_API_KEY", warning)
        choice, warning = self.resolve(
            fanout_model=setting, keys=("OPENAI_API_KEY", "ANTHROPIC_API_KEY"))
        self.assertEqual((choice.label, warning), (setting, ""))
        self.assertEqual(choice.apply(self.BASE)["api_key_env"], "ANTHROPIC_API_KEY")

    def test_an_unknown_model_falls_back_and_says_so(self):
        choice, warning = self.resolve(fanout_model="gpt-9-imaginary")
        self.assertEqual(choice.label, "openai/gpt-6-sol")
        self.assertTrue(warning)

    def test_auto_matches_effort(self):
        self.assertEqual(self.resolve("light", fanout_model="auto")[0].label,
                         "openai/gpt-6-luna")
        self.assertEqual(self.resolve("standard", fanout_model="auto")[0].label,
                         "openai/gpt-6-sol")


class _Parent:
    """A parent session with a real shell client and a fan-out client whose
    workers are replaced by *worker* (envelope, timeout) -> result."""

    def __init__(self, test, *, mode="prompt_all", interactive=True, answers=(),
                 worker=None, **settings):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.config = {
            "provider": "custom", "chat_model": "m", "model": "m", "custom_model": "m",
            "custom_base_url": "http://127.0.0.1:9/v1", **settings,
        }
        permissions = PermissionState(
            agent_mode=mode == "yolo",
            permission_mode=mode if mode != "yolo" else "prompt_all")
        self.asked = []
        answers = iter(answers)

        def ask(prompt):
            self.asked.append(prompt)
            return next(answers)

        self.shell = LocalShellClient(permissions=permissions)
        self.shell.set_policy(LocalShellPolicy(interactive=interactive, input_fn=ask))
        self.client = FanOutClient()
        self.envelopes = []
        worker = worker or (lambda envelope, timeout: {
            "ok": True, "summary": "summary of " + envelope["task"],
            "input_tokens": 100, "output_tokens": 10})

        def run(envelope, timeout):
            self.envelopes.append(envelope)
            return worker(envelope, timeout)

        self.client._run_worker = run
        clients = {name: object() for name in (
            "save_memory", "public_api", "conch_config", "delegate_task",
            "interactive_terminal", "personal_items", "search_conversations")}
        clients.update(local_shell=self.shell, fan_out=self.client)
        self.session = AgentSession(
            self.config, interactive=interactive, permissions=permissions,
            cwd=directory.name)
        test.addCleanup(self.session.close)
        self.session.attach_clients(clients, bind=False, chat_state=SimpleNamespace(
            tools=[{"function": {"name": name}} for name in clients]))
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "turns": 2}
        self.client.bind_session(self.session, usage=self.usage)

    def fan_out(self, *tasks):
        tasks = [task if isinstance(task, dict) else {"task": task} for task in tasks]
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            result = self.client.call_tool("fan_out", {"tasks": tasks})
        return result["content"][0]["text"]


class ParentAuthorityTests(unittest.TestCase):
    def test_ask_first_session_asks_once_and_workers_get_read_only_auto(self):
        parent = _Parent(self, answers=["y"])
        parent.fan_out("one", "two", "three")
        self.assertEqual(len(parent.asked), 1)
        self.assertEqual({e["permission_mode"] for e in parent.envelopes}, {"safe_auto"})

    def test_declining_starts_no_worker(self):
        parent = _Parent(self, answers=["n"])
        text = parent.fan_out("one", "two")
        self.assertEqual(parent.envelopes, [])
        self.assertIn("declined", text)

    def test_session_that_cannot_ask_gives_workers_no_extra_authority(self):
        parent = _Parent(self, interactive=False)
        parent.fan_out("one", "two")
        self.assertEqual(parent.asked, [])
        self.assertEqual({e["permission_mode"] for e in parent.envelopes}, {"prompt_all"})

    def test_workers_inherit_safe_auto_and_agent_mode_without_a_prompt(self):
        for mode in ("safe_auto", "yolo"):
            with self.subTest(mode=mode):
                parent = _Parent(self, mode=mode)
                parent.fan_out("one", "two")
                self.assertEqual(parent.asked, [])
                self.assertEqual({e["permission_mode"] for e in parent.envelopes}, {mode})

    def test_workers_never_get_self_management_or_recursive_tools(self):
        parent = _Parent(self, mode="safe_auto")
        parent.fan_out("one", "two")
        self.assertEqual(parent.envelopes[0]["tools"],
                         ["local_shell", "public_api", "search_conversations"])

    def test_sandboxed_session_withholds_the_shell_from_workers(self):
        parent = _Parent(self, mode="safe_auto")
        parent.shell._exec_backend = object()
        text = parent.fan_out("one", "two")
        self.assertNotIn("local_shell", parent.envelopes[0]["tools"])
        self.assertIn("sandbox", text)

    def test_session_allowed_prefixes_reach_workers(self):
        parent = _Parent(self, mode="safe_auto")
        parent.shell.allow_prefixes(["git status"])
        parent.fan_out("one", "two")
        self.assertEqual(parent.envelopes[0]["allow_prefixes"], ["git status"])


class ParentExecutionTests(unittest.TestCase):
    def test_results_keep_task_order_and_failures_do_not_hide_successes(self):
        def worker(envelope, timeout):
            if envelope["task"] == "two":
                return {"ok": False, "error": "timed out after 5s"}
            time.sleep(0.2 if envelope["task"] == "one" else 0)
            return {"ok": True, "summary": "summary of " + envelope["task"]}

        text = _Parent(self, mode="safe_auto", worker=worker,
                       fanout_concurrency="3").fan_out("one", "two", "three")
        self.assertIn("2 of 3", text)
        self.assertLess(text.index("summary of one"), text.index("timed out after 5s"))
        self.assertLess(text.index("timed out after 5s"), text.index("summary of three"))

    def test_invalid_requests_start_no_worker(self):
        parent = _Parent(self, mode="safe_auto")
        for tasks in ([], [{"task": "  "}], [{"task": "x"}] * (fanout.MAX_TASKS + 1)):
            with self.subTest(tasks=len(tasks)):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = parent.client.call_tool("fan_out", {"tasks": tasks})
                self.assertTrue(result["content"][0]["text"].startswith("Error"))
        self.assertEqual(parent.envelopes, [])

    def test_auto_routes_each_task_to_a_model_for_its_effort(self):
        inventory = [
            ModelChoice("custom", "m", "standard", "local"),
            ModelChoice("custom", "tiny-8b", "light", "local"),
            ModelChoice("custom", "big-120b", "heavy", "local"),
        ]
        parent = _Parent(self, mode="safe_auto", fanout_model="auto")
        with patch("conch.fanout.available_models", return_value=inventory):
            text = parent.fan_out(
                {"task": "a", "effort": "light"}, {"task": "b", "effort": "heavy"},
                {"task": "c"})
        models = {e["task"]: e["config"]["chat_model"] for e in parent.envelopes}
        self.assertEqual(models, {"a": "tiny-8b", "b": "big-120b", "c": "m"})
        self.assertIn("custom/tiny-8b", text)

    def test_a_failed_auto_pick_is_retried_on_the_session_model(self):
        inventory = [ModelChoice("custom", "m", "standard", "local"),
                     ModelChoice("custom", "tiny-8b", "light", "local")]

        def worker(envelope, timeout):
            if envelope["config"]["chat_model"] == "tiny-8b":
                return {"ok": False, "error": "the API rejected this key"}
            return {"ok": True, "summary": "answered by " + envelope["config"]["chat_model"]}

        parent = _Parent(self, mode="safe_auto", fanout_model="auto", worker=worker)
        with patch("conch.fanout.available_models", return_value=inventory):
            text = parent.fan_out({"task": "a", "effort": "light"}, {"task": "b"})
        self.assertIn("2 of 2", text)
        self.assertIn("answered by m", text)
        self.assertIn("the API rejected this key", text)
        self.assertEqual([e["config"]["chat_model"] for e in parent.envelopes
                          if e["task"] == "a"], ["tiny-8b", "m"])

    def test_a_failed_configured_model_is_reported_not_silently_replaced(self):
        def worker(envelope, timeout):
            return {"ok": False, "error": "the API rejected this key"}

        parent = _Parent(self, mode="safe_auto", fanout_model="other-model", worker=worker)
        with patch("conch.providers.validate_model_for_provider", return_value=(True, "")):
            text = parent.fan_out("a", "b")
        self.assertIn("0 of 2", text)
        self.assertEqual({e["config"]["chat_model"] for e in parent.envelopes},
                         {"other-model"})

    def test_worker_tokens_join_session_cost_without_counting_as_turns(self):
        parent = _Parent(self, mode="safe_auto")
        parent.fan_out("one", "two")
        self.assertEqual(parent.usage["input_tokens"], 200)
        self.assertEqual(parent.usage["output_tokens"], 20)
        self.assertEqual(parent.usage["turns"], 2)

    def test_configured_concurrency_runs_workers_at_the_same_time(self):
        together = threading.Barrier(3, timeout=5)

        def worker(envelope, timeout):
            together.wait()
            return {"ok": True, "summary": "ok"}

        text = _Parent(self, mode="safe_auto", worker=worker,
                       fanout_concurrency="3").fan_out("one", "two", "three")
        self.assertIn("3 of 3", text)

    def test_one_local_server_gets_one_worker_at_a_time_by_default(self):
        active, peak = [], []
        gate = threading.Lock()

        def run_worker(envelope, timeout):
            with gate:
                active.append(1)
                peak.append(len(active))
            time.sleep(0.05)
            with gate:
                active.pop()
            return {"ok": True, "summary": "ok"}

        parent = _Parent(self, mode="safe_auto")
        # The serial gate lives one level above the process boundary.
        del parent.client._run_worker
        with patch.object(FanOutClient, "_run_worker",
                          side_effect=lambda envelope, timeout: run_worker(envelope, timeout)):
            parent.fan_out("one", "two", "three")
        self.assertEqual(max(peak), 1)


class _Endpoint(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint: answers the tool probe, runs the shell
    command a task names, and otherwise replies after ``delay`` seconds."""

    delay = 0.0
    arrivals = []
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _send(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _message(self, message, finish):
        self._send({"choices": [{"message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 5}})

    def do_GET(self):
        self._send({"data": [{"id": "stub-model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        offered = [tool.get("function", {}).get("name") for tool in body.get("tools") or []]
        if "conch_tool_probe" in offered:
            return self._message({"role": "assistant", "content": None, "tool_calls": [{
                "id": "probe", "type": "function", "function": {
                    "name": "conch_tool_probe", "arguments": '{"token":"conch-ok"}'}}]},
                "tool_calls")
        messages = body.get("messages") or []
        # Local sessions prepend format examples; the task is the last user turn.
        task = next((str(m.get("content")) for m in reversed(messages)
                     if m.get("role") == "user"), "")
        last = messages[-1] if messages else {}
        if last.get("role") != "tool":
            with self.lock:
                type(self).arrivals.append(time.monotonic())
            if task.startswith("RUN "):
                return self._message({"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call", "type": "function", "function": {
                        "name": "local_shell",
                        "arguments": json.dumps({"command": task[4:]})}}]}, "tool_calls")
            time.sleep(type(self).delay)
            return self._message({"role": "assistant", "content": "done: " + task}, "stop")
        self._message({"role": "assistant",
                       "content": "tool said: " + str(last.get("content"))}, "stop")


class RealWorkerTests(unittest.TestCase):
    """Workers are real processes talking to a local endpoint."""

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        home = Path(self.root.name)
        self.work = home / "work"
        self.work.mkdir()
        environment = {
            "HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_STATE_HOME": str(home / "state"), "XDG_DATA_HOME": str(home / "data"),
            "CONCH_NO_WIZARD": "1",
        }
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)
        _Endpoint.delay = 0.0
        _Endpoint.arrivals = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Endpoint)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def parent(self, **settings):
        parent = _Parent(self, mode="safe_auto", **settings)
        parent.config.update(
            custom_base_url=f"http://127.0.0.1:{self.server.server_port}/v1",
            custom_model="stub-model", model="stub-model", chat_model="stub-model")
        parent.session.cwd = str(self.work)
        del parent.client._run_worker  # the real process boundary
        return parent

    def test_tasks_run_in_separate_processes_at_the_same_time(self):
        _Endpoint.delay = 1.5
        parent = self.parent(fanout_concurrency="3")
        text = parent.fan_out("alpha", "beta", "gamma")
        self.assertIn("3 of 3", text)
        for task in ("alpha", "beta", "gamma"):
            self.assertIn("done: " + task, text)
        self.assertEqual(len(_Endpoint.arrivals), 3)
        # All three were being served before the first could have answered.
        self.assertLess(max(_Endpoint.arrivals) - min(_Endpoint.arrivals), _Endpoint.delay)
        self.assertEqual(parent.usage["input_tokens"], 150)

    def test_worker_runs_read_only_commands_and_refuses_the_rest(self):
        (self.work / "notes.txt").write_text("visible to the worker\n")
        text = self.parent(fanout_concurrency="2").fan_out(
            "RUN cat notes.txt", "RUN touch written-by-worker")
        self.assertIn("visible to the worker", text)
        self.assertIn("cannot prompt", text)
        self.assertFalse((self.work / "written-by-worker").exists())

    def test_a_slow_worker_is_stopped_at_the_task_timeout(self):
        _Endpoint.delay = 30
        started = time.monotonic()
        text = self.parent(fanout_timeout="2", fanout_concurrency="2").fan_out("slow", "slower")
        self.assertIn("0 of 2", text)
        self.assertIn("timed out", text)
        self.assertLess(time.monotonic() - started, 15)

    def test_cancelling_stops_running_workers(self):
        _Endpoint.delay = 30
        parent = self.parent(fanout_concurrency="2")
        outcome = {}
        runner = threading.Thread(
            target=lambda: outcome.setdefault("text", parent.fan_out("slow", "slower")))
        started = time.monotonic()
        runner.start()
        deadline = time.monotonic() + 15
        while len(_Endpoint.arrivals) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        parent.client.stop()
        runner.join(timeout=15)
        self.assertFalse(runner.is_alive())
        self.assertIn("0 of 2", outcome["text"])
        self.assertLess(time.monotonic() - started, 25)


class FanoutCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        environment = {name: "" for name in KEYS}
        environment.update(HOME=self.root.name, OPENAI_API_KEY="key",
                           XDG_CONFIG_HOME=str(Path(self.root.name) / "config"))
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.config = {"provider": "openai", "chat_model": "gpt-6-sol",
                       "model": "gpt-6-sol", "local_only": "false"}

    def command(self, argument=""):
        from conch.commands import handle_slash_command

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()), \
                _no_local_models(), patch("conch.fanout._seat_ready", return_value=False):
            handle_slash_command(f"/fanout {argument}".strip(), self.config,
                                 "openai", "gpt-6-sol", lambda value: None)
        return output.getvalue()

    def saved(self):
        from conch import config as config_mod

        path = Path(config_mod.get_config_path())
        return path.read_text() if path.exists() else ""

    def test_a_usable_setting_is_saved_and_applies_to_this_session(self):
        for setting in ("auto", "gpt-6-luna"):
            with self.subTest(setting=setting):
                self.command(setting)
                self.assertEqual(self.config["fanout_model"], setting)
                self.assertIn(f"fanout_model = {setting}", self.saved())

    def test_an_unusable_setting_changes_nothing(self):
        for setting in ("gpt-9-imaginary", "anthropic/claude-haiku-4-5"):
            with self.subTest(setting=setting):
                self.command(setting)
                self.assertNotIn("fanout_model", self.config)
                self.assertNotIn("fanout_model", self.saved())

    def test_default_clears_the_setting(self):
        self.command("auto")
        self.command("default")
        self.assertEqual(self.config["fanout_model"], "")
        self.assertEqual(fanout.resolve_model(self.config, "light", list)[0].label,
                         "openai/gpt-6-sol")

    def test_status_lists_what_is_available_without_changing_anything(self):
        text = self.command()
        self.assertIn("openai/gpt-6-luna", text)
        self.assertNotIn("anthropic/", text)
        self.assertEqual(self.saved(), "")


if __name__ == "__main__":
    unittest.main()
