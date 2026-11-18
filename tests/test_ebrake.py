"""Session brake behavior: retained work, isolation, cancellation and real IPC."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conch import runtime
from conch.ebrake import AgentsCancelled, PauseController, current_controller
from conch.fanout import FanOutClient
from conch.providers import error_response
from conch.session import AgentSession
from conch.tooling import DelegateTaskClient


def tool(name):
    return {"type": "function", "function": {
        "name": name, "parameters": {"type": "object", "properties": {}}}}


def call(name, index=1):
    return {"id": f"call-{index}", "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class BrakeTestCase(unittest.TestCase):
    def setUp(self):
        self.sessions = []
        self.threads = []
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch("conch.runtime.Spinner", lambda *_: contextlib.nullcontext()))
        self.stack.enter_context(patch("conch.tooling.run_hook", return_value=(True, "")))

    def tearDown(self):
        for session in self.sessions:
            session.close()
        for thread in self.threads:
            thread.join(5)
        self.stack.close()
        self.assertFalse(any(thread.is_alive() for thread in self.threads), "orphaned pause waiter")

    def session(self, **kwargs):
        session = AgentSession({"provider": "openai", "model": "gpt-4o", "local_only": "false"}, **kwargs)
        self.sessions.append(session)
        return session

    def start(self, function):
        outcome = {}
        done = threading.Event()

        def run():
            try:
                outcome["result"] = function()
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return done, outcome

    def finish(self, work):
        done, outcome = work
        self.assertTrue(done.wait(5), "work did not settle")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]

    def observe_pause(self, session):
        parked = threading.Event()
        controller = session._pause_controller
        original = controller.wait

        def observe(*args, **kwargs):
            if controller.paused:
                parked.set()
            return original(*args, **kwargs)

        self.stack.enter_context(patch.object(controller, "wait", observe))
        return parked

    def provider(self, fn):
        self.stack.enter_context(patch.dict("conch.providers.RAW_FNS", {"openai": fn}))


class BrakeTests(BrakeTestCase):
    def test_paused_initial_turn_is_isolated_and_resumes(self):
        paused, independent = self.session(), self.session()
        parked = self.observe_pause(paused)
        requests = []

        def raw(_config, messages, _tools):
            requests.append(messages[-1]["content"])
            return {"content": "done"}

        self.provider(raw)
        paused.pause_agents()
        pending = self.start(lambda: paused.run_turn([{"role": "user", "content": "parked"}]))
        self.assertTrue(parked.wait(5))
        self.assertEqual(requests, [])
        self.assertEqual(self.finish(self.start(lambda: independent.run_turn(
            [{"role": "user", "content": "independent"}])))[0], "done")
        self.assertEqual(requests, ["independent"])
        paused.resume_agents()
        self.assertEqual(self.finish(pending)[0], "done")
        self.assertEqual(requests, ["independent", "parked"])
        self.assertFalse(paused.agents_paused)

    def test_pause_between_tools_retains_batch_and_releases_execution_lock(self):
        parent, independent = self.session(), self.session()
        parked = self.observe_pause(parent)
        calls = []
        requests = []
        history = [{"role": "user", "content": "batch"}]

        def execute(name, _arguments):
            calls.append(name)
            if name == "first":
                parent.pause_agents()
            return {"content": [{"type": "text", "text": name + " result"}]}

        parent.attach_clients({name: SimpleNamespace(call_tool=execute) for name in ("first", "second")})

        def raw(_config, messages, tools):
            requests.append(messages[-1]["content"])
            if tools and len(requests) == 1:
                return {"tool_calls": [call("first"), call("second", 2)]}
            return {"content": "done"}

        self.provider(raw)
        pending = self.start(lambda: parent.run_turn(history, tools=[tool("first"), tool("second")]))
        self.assertTrue(parked.wait(5))
        self.assertEqual(calls, ["first"])
        self.finish(self.start(lambda: independent.run_turn([{"role": "user", "content": "other"}])))
        self.assertEqual(calls, ["first"])
        parent.resume_agents()
        self.assertEqual(self.finish(pending)[0], "done")
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual([message["content"] for message in history if message["role"] == "tool"],
                         ["first result", "second result"])
        self.assertEqual(len(requests), 3)

    def test_inflight_request_finishes_but_its_tools_wait(self):
        session = self.session()
        parked = self.observe_pause(session)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []

        def raw(_config, messages, _tools):
            if not any(message["role"] == "tool" for message in messages):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("request was never released")
                return {"tool_calls": [call("probe")]}
            return {"content": "done"}

        session.attach_clients({"probe": SimpleNamespace(call_tool=lambda *_: (
            calls.append("probe") or {"content": [{"type": "text", "text": "ok"}]}))})
        self.provider(raw)
        pending = self.start(lambda: session.run_turn([{"role": "user", "content": "probe"}], tools=[tool("probe")]))
        self.assertTrue(entered.wait(5))
        session.pause_agents()
        release.set()
        self.assertTrue(parked.wait(5))
        self.assertEqual(calls, [])
        session.resume_agents()
        self.finish(pending)
        self.assertEqual(calls, ["probe"])

    def test_retry_stream_and_provider_fallback_obey_pause(self):
        for mode in ("retry", "key", "fallback", "stream"):
            with self.subTest(mode=mode):
                session = self.session()
                parked = self.observe_pause(session)
                requests = []

                def raw(*_args):
                    requests.append(mode)
                    if len(requests) == 1:
                        session.pause_agents()
                        return error_response("connection timed out" if mode in ("retry", "stream") else "invalid api key")
                    return {"content": "done"}

                with patch.dict("conch.providers.RAW_FNS", {"openai": raw, "anthropic": raw}), \
                        patch.dict("conch.providers.STREAM_FNS", {"openai": raw}), \
                        patch("conch.runtime.time.sleep"), \
                        patch("conch.runtime.retry_after_key_reload", return_value=mode == "key"), \
                        patch("conch.providers.get_fallback_chain", return_value=[("anthropic", "claude", True)]), \
                        patch("sys.stdin.isatty", return_value=False):
                    pending = self.start(lambda: session.run_turn(
                        [{"role": "user", "content": "go"}], on_token=(lambda _: None) if mode == "stream" else None))
                    self.assertTrue(parked.wait(5))
                    self.assertEqual(len(requests), 1)
                    session.resume_agents()
                    self.assertEqual(self.finish(pending)[0], "done")
                    self.assertEqual(len(requests), 2)

    def test_compaction_and_exhaustion_summary_wait(self):
        for mode in ("compact", "summary"):
            with self.subTest(mode=mode):
                session = self.session()
                parked = self.observe_pause(session)
                requests = []
                history = [{"role": "system", "content": "system"}] + [
                    {"role": "user" if index % 2 == 0 else "assistant", "content": f"message {index}"}
                    for index in range(20)]

                def raw(*_args):
                    requests.append(mode)
                    return {"content": "summary"}

                def work():
                    with session.agent_scope(), runtime.serialized_agent_execution():
                        session.pause_agents()
                        if mode == "compact":
                            return runtime.auto_compact(history, None, "openai", session.config, raw, force=True)
                        return runtime._graceful_exhaustion(session.config, "openai", raw, history,
                                                            {"input_tokens": 0, "output_tokens": 0}, "budget")

                pending = self.start(work)
                self.assertTrue(parked.wait(5))
                self.assertEqual(requests, [])
                session.resume_agents()
                self.assertTrue(self.finish(pending))
                self.assertEqual(requests, [mode])

    def test_delegated_child_and_nested_skill_runtime_inherit_scope(self):
        parent = self.session()
        parked = self.observe_pause(parent)
        parent.chat_state = SimpleNamespace(tools=[], tool_map={})
        delegate = DelegateTaskClient()
        delegate.bind_session(parent)
        controllers = []

        def raw(*_args):
            controllers.append(current_controller())
            if len(controllers) == 1:
                parent.pause_agents()
                # Synchronous skill calls use runtime directly, not AgentSession.
                return {"content": runtime.chat_turn(parent.config, "openai", raw,
                                                    [{"role": "user", "content": "nested"}], None, {}, {})[0]}
            return {"content": "nested answer"}

        self.provider(raw)
        pending = self.start(lambda: delegate.call_tool("delegate_task", {"task": "child"}))
        self.assertTrue(parked.wait(5))
        self.assertEqual(controllers, [parent._pause_controller])
        parent.resume_agents()
        self.assertIn("nested answer", self.finish(pending)["content"][0]["text"])
        self.assertEqual(controllers, [parent._pause_controller, parent._pause_controller])
        self.assertFalse(parent._pause_controller.closed, "child cleanup closed the parent")

    def test_close_while_paused_unblocks_without_request(self):
        session = self.session()
        parked = self.observe_pause(session)
        requests = []
        self.provider(lambda *_: requests.append(True) or {"content": "wrong"})
        session.pause_agents()
        pending = self.start(lambda: session.run_turn([{"role": "user", "content": "go"}]))
        self.assertTrue(parked.wait(5))
        session.close()
        session.resume_agents()
        self.assertTrue(pending[0].wait(5))
        self.assertIsInstance(pending[1].get("error"), AgentsCancelled)
        self.assertEqual(requests, [])
        with self.assertRaises(AgentsCancelled):
            session.run_turn([])

    def test_child_close_leaves_parent_and_other_children_alive(self):
        parent = self.session()
        child = self.session(pause_controller=parent._pause_controller)
        parked = self.observe_pause(parent)
        parent.pause_agents()
        pending = self.start(lambda: child.run_turn([]))
        self.assertTrue(parked.wait(5))
        child.close()
        self.assertTrue(pending[0].wait(5))
        self.assertIsInstance(pending[1].get("error"), AgentsCancelled)
        self.assertTrue(parent.agents_paused)
        parent.resume_agents()
        self.provider(lambda *_: {"content": "alive"})
        self.assertEqual(parent.run_turn([])[0], "alive")

    def test_ctrl_c_at_paused_boundary_unwinds_without_another_request(self):
        session = self.session()
        calls = []
        controller = session._pause_controller

        def interrupt_wait(_timeout):
            raise KeyboardInterrupt()

        def execute(*_args):
            session.pause_agents()
            return {"content": [{"type": "text", "text": "ok"}]}

        def raw(*_args):
            calls.append(True)
            return {"tool_calls": [call("pause")]}

        self.provider(raw)
        session.attach_clients({"pause": SimpleNamespace(call_tool=execute)})
        with patch.object(controller._condition, "wait", interrupt_wait):
            with self.assertRaises(KeyboardInterrupt):
                session.run_turn([{"role": "user", "content": "pause"}], tools=[tool("pause")])
        self.assertEqual(calls, [True])
        self.assertTrue(session.agents_paused)
        session.resume_agents()
        self.provider(lambda *_: {"content": "next turn"})
        self.assertEqual(session.run_turn([])[0], "next turn")


# A real child process executes run_envelope + runtime. Only external model
# and tool effects are replaced; IPC, process lifetime, and checkpoints are real.
WORKER_CODE = r'''
import json, sys, time
from pathlib import Path
from conch import bootstrap, memory, providers
from conch.ebrake import PauseController
from conch.fanout import run_envelope
original_wait = PauseController.wait
def observed_wait(self, *args, **kwargs):
    if self.paused:
        Path(envelope["config"]["marker_dir"], "parked-" + envelope["task"]).touch()
    return original_wait(self, *args, **kwargs)
PauseController.wait = observed_wait
class Probe:
    def call_tool(self, *_):
        Path(envelope["config"]["marker_dir"], "tool").touch()
        return {"content": [{"type": "text", "text": "ok"}]}
bootstrap.make_builtin_clients = lambda *a, **kw: {"probe": Probe()}
def inject(definitions, tool_map, clients):
    definitions.append({"type": "function", "function": {"name": "probe", "parameters": {"type": "object"}}})
import conch.tooling
conch.tooling.inject_builtin_tools = inject
memory.MemoryStore = lambda: None
count = 0
def raw(config, messages, tools):
    global count
    count += 1
    directory = Path(config["marker_dir"])
    (directory / ("request-" + envelope["task"] + "-" + str(count))).touch()
    if count == 1 and envelope["task"] == "first":
        while not (directory / "release-first").exists():
            time.sleep(.01)
        if tools:
            return {"tool_calls": [{"id": "probe-1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}]}
    return {"content": "finished"}
providers.RAW_FNS["openai"] = raw
envelope = json.loads(sys.stdin.read())
print(json.dumps(run_envelope(envelope)))
'''


class ProcessBrakeTests(BrakeTestCase):
    def client(self, directory):
        session = self.session()

        class Client(FanOutClient):
            def _spawn(self, envelope):
                Path(directory, "spawned-" + envelope["task"]).touch()
                return subprocess.Popen([sys.executable, "-c", WORKER_CODE],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, start_new_session=True)

        client = Client()
        client.bind_session(session)
        session.attach_clients({"fan_out": client})
        return session, client

    def envelope(self, directory, task="first", tools=()):
        return {"config": {"provider": "openai", "model": "gpt-4o", "marker_dir": directory},
                "task": task, "tools": list(tools)}

    def await_path(self, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            threading.Event().wait(.01)
        self.assertTrue(path.exists(), f"missing child boundary {path.name}")

    def test_running_child_parks_tools_and_next_model_request(self):
        with tempfile.TemporaryDirectory() as directory:
            session, client = self.client(directory)
            pending = self.start(lambda: client._run_worker(self.envelope(directory, tools=("probe",)), 30))
            self.await_path(Path(directory, "request-first-1"))
            session.pause_agents()
            Path(directory, "release-first").touch()
            self.await_path(Path(directory, "parked-first"))
            self.assertFalse(Path(directory, "tool").exists())
            self.assertFalse(Path(directory, "request-first-2").exists())
            session.resume_agents()
            self.assertTrue(self.finish(pending)["ok"])
            self.assertTrue(Path(directory, "tool").exists())
            self.assertTrue(Path(directory, "request-first-2").exists())
            path = session._pause_controller.export_path()
            session.close()
            self.assertFalse(os.path.exists(path))

    def test_queued_workers_cannot_spawn_until_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            session, client = self.client(directory)
            parked = self.observe_pause(session)
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(client._run_worker, self.envelope(directory), 30)
                second = pool.submit(client._run_worker, self.envelope(directory, "second"), 30)
                try:
                    self.await_path(Path(directory, "request-first-1"))
                    session.pause_agents()
                    Path(directory, "release-first").touch()
                    self.assertTrue(first.result(timeout=5)["ok"])
                    self.assertTrue(parked.wait(5))
                    self.assertFalse(Path(directory, "spawned-second").exists())
                    session.resume_agents()
                    self.assertTrue(second.result(timeout=5)["ok"])
                finally:
                    client.stop()

    def test_stop_and_close_cancel_running_and_queued_paused_workers(self):
        for close in (False, True):
            with self.subTest(close=close), tempfile.TemporaryDirectory() as directory:
                session, client = self.client(directory)
                running = self.start(lambda: client._run_worker(self.envelope(directory, tools=("probe",)), 30))
                self.await_path(Path(directory, "request-first-1"))
                session.pause_agents()
                Path(directory, "release-first").touch()
                self.await_path(Path(directory, "parked-first"))
                parked = self.observe_pause(session)
                queued = self.start(lambda: client._run_worker(self.envelope(directory, "second"), 30))
                self.assertTrue(parked.wait(5))
                session.close() if close else client.stop()
                self.assertEqual(self.finish(running)["error"], "cancelled")
                self.assertEqual(self.finish(queued)["error"], "cancelled")
                self.assertFalse(Path(directory, "spawned-second").exists())
                self.assertEqual(client._processes, set())

    def test_paused_time_is_not_charged_to_worker_deadline(self):
        for exhaust_after_resume in (False, True):
            with self.subTest(exhaust_after_resume=exhaust_after_resume):
                session = self.session()
                now = [100.0]
                controller = session._pause_controller
                attempts = []

                class Process:
                    def communicate(self, payload=None, timeout=None):
                        if timeout is None:
                            return json.dumps({"ok": True}), ""
                        attempts.append(payload)
                        if len(attempts) == 1:
                            now[0] += 1
                            controller.pause()
                        elif len(attempts) == 2:
                            now[0] += 1000
                        else:
                            controller.resume()
                            now[0] += 11 if exhaust_after_resume else 2
                            if not exhaust_after_resume:
                                return json.dumps({"ok": True}), ""
                        raise subprocess.TimeoutExpired("worker", timeout)

                client = FanOutClient()
                client.bind_session(session)
                with patch("conch.ebrake.time.monotonic", side_effect=lambda: now[0]), \
                        patch.object(client, "_spawn", return_value=Process()), \
                        patch.object(client, "_kill"):
                    result = client._run_worker({"task": "clock"}, timeout=10)
                self.assertEqual(result.get("ok"), not exhaust_after_resume)
                if exhaust_after_resume:
                    self.assertIn("timed out", result["error"])
                self.assertEqual(len(attempts), 3)
                self.assertIsNotNone(attempts[0])
                self.assertEqual(attempts[1:], [None, None])

    def test_removed_parent_transport_cancels_child_not_resumes(self):
        parent = PauseController()
        path = parent.export_path()
        child = PauseController(path)
        parent.pause()
        self.assertTrue(child.paused)
        parent.close()
        with self.assertRaises(AgentsCancelled):
            child.wait()
        self.assertFalse(self.session().agents_paused)

    def test_stopping_fanout_unparks_parent_and_releases_global_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            session, client = self.client(directory)
            session.config.update(marker_dir=directory, fanout_concurrency="1")
            session.chat_state = SimpleNamespace(tools=[])
            parked = self.observe_pause(session)

            def run():
                with session.agent_scope(), runtime.serialized_agent_execution():
                    return client.call_tool("fan_out", {"tasks": [
                        {"task": "first"}, {"task": "second"}]})

            pending = self.start(run)
            self.await_path(Path(directory, "request-first-1"))
            session.pause_agents()
            self.assertTrue(parked.wait(5))
            independent = self.session()
            self.provider(lambda *_: {"content": "independent"})
            self.assertEqual(self.finish(self.start(lambda: independent.run_turn([])))[0],
                             "independent")
            client.stop()
            self.assertTrue(pending[0].wait(5))
            self.assertIsInstance(pending[1].get("error"), AgentsCancelled)
            self.assertFalse(Path(directory, "spawned-second").exists())
            self.assertEqual(client._processes, set())
