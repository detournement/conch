"""The model picker and its catalog: added models become switchable,
provider-exact switching, roles saved to config and cleared with the model
they name, and unusable providers refused before anything changes."""

import asyncio
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from conch import model_catalog
from conch.model_catalog import ModelEntry, ProviderStatus
from conch.model_picker import ModelPicker


class _ConfigCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name,
                                           "ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": "x"})
        env.start()
        self.addCleanup(env.stop)
        self.config = {"provider": "anthropic", "chat_model": "claude-sonnet-5",
                       "model": "claude-sonnet-5"}

    def saved(self):
        from conch.config import load_config

        return load_config()


class CatalogTests(_ConfigCase):
    def test_added_model_persists_and_passes_catalog_validation(self):
        from conch.providers import validate_model_for_provider

        self.assertFalse(validate_model_for_provider("openai", "gpt-7-preview", self.config)[0])
        self.assertTrue(model_catalog.add_model(self.config, "openai", "gpt-7-preview"))
        self.assertFalse(model_catalog.add_model(self.config, "openai", "gpt-7-preview"))
        self.assertEqual(validate_model_for_provider("openai", "gpt-7-preview", self.saved()),
                         (True, ""))
        self.assertEqual(model_catalog.provider_for("gpt-7-preview", self.saved()), "openai")
        self.assertTrue(model_catalog.remove_model(self.config, "openai", "gpt-7-preview"))
        self.assertFalse(validate_model_for_provider("openai", "gpt-7-preview", self.saved())[0])

    def test_new_model_text_names_the_provider_first(self):
        self.assertEqual(model_catalog.parse_new_model("openrouter/vendor/model-x"),
                         ("openrouter", "vendor/model-x"))
        for bad in ("gpt-7", "ollama/qwen3", "nosuch/model", "openai/", "openai/has space"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                model_catalog.parse_new_model(bad)

    def test_switch_command_names_the_provider_exactly(self):
        self.assertEqual(ModelEntry("bedrock", "zai.glm-5").switch_command(),
                         "/model zai.glm-5 --provider bedrock")
        self.assertEqual(ModelEntry("llamaidx", "llamaidx/box/m").switch_command(),
                         "/model llamaidx/box/m")


class PickerTests(_ConfigCase):
    def picker(self):
        picker = ModelPicker(self.config, "anthropic", "claude-sonnet-5")
        picker.apply_live([], {"claude-code": ProviderStatus(False, "CLI not installed"),
                               "codex": ProviderStatus(True, "signed in")})
        return picker

    def select(self, picker, provider, model):
        picker.selected_key = (provider, model)
        self.assertIsNotNone(picker.selected())

    def test_unusable_provider_is_refused_with_its_reason(self):
        picker = self.picker()
        self.select(picker, "claude-code", "opus")
        picker.choose()
        self.assertIsNone(picker.result)
        self.assertIn("CLI not installed", picker.notice)
        self.select(picker, "openai", "gpt-6-sol")
        picker.choose()
        self.assertEqual(picker.result.key, ("openai", "gpt-6-sol"))

    def test_typing_filters_and_selects_the_first_match(self):
        picker = self.picker()
        picker.query.text = "kimi"
        labels = [entry.label for kind, entry in picker.rows if kind == "model"]
        self.assertTrue(labels)
        self.assertTrue(all("kimi" in label for label in labels))
        self.assertEqual(picker.selected().label, labels[0])

    def test_late_current_model_receives_highlight_unless_user_has_moved(self):
        active = ModelEntry("ollama", "local:latest")
        for moved in (False, True):
            with self.subTest(moved=moved):
                picker = ModelPicker(self.config, "ollama", active.model)
                if moved:
                    picker.move(1)
                previous = picker.selected_key
                picker.apply_live([active], {"ollama": ProviderStatus(True, "ready")})
                self.assertEqual(picker.selected_key, previous if moved else active.key)

    def test_roles_save_to_config_and_clear_with_a_removed_model(self):
        picker = self.picker()
        picker.start_add()
        picker.add_input.text = "openai/gpt-7-preview"
        picker.finish_add(save=True)
        self.assertEqual(picker.selected().key, ("openai", "gpt-7-preview"))
        picker.toggle_fanout()
        picker.toggle_side()
        saved = self.saved()
        self.assertEqual(saved.get("fanout_model"), "openai/gpt-7-preview")
        self.assertEqual((saved.get("weak_provider"), saved.get("weak_model")),
                         ("openai", "gpt-7-preview"))
        picker.remove_selected()
        saved = self.saved()
        self.assertFalse(saved.get("fanout_model"))
        self.assertFalse(saved.get("weak_model"))
        self.assertEqual(model_catalog.added_models(saved), [])

    def test_only_added_models_can_be_removed(self):
        picker = self.picker()
        self.select(picker, "openai", "gpt-6-sol")
        picker.remove_selected()
        self.assertIn("Only models you added", picker.notice)
        self.assertIn(("openai", "gpt-6-sol"),
                      [entry.key for kind, entry in picker.rows if kind == "model"])

    def test_native_refresh_removes_stale_models_but_keeps_user_additions(self):
        self.config["extra_models"] = "codex/user-model"
        picker = ModelPicker(self.config, "codex", "default")
        source = model_catalog._seat_source("codex")
        with mock.patch("conch.auth.cli_installed", return_value=True), \
             mock.patch("conch.seats.status", return_value=(True, "signed in")), \
             mock.patch("conch.seat_codex.list_models",
                        side_effect=[["native-old"], ["native-new"]]):
            picker.apply_source(*source(self.config))
            self.select(picker, "codex", "native-old")
            picker.apply_source(*source(self.config))
        keys = {entry.key for entry in picker.entries}
        self.assertNotIn(("codex", "native-old"), keys)
        self.assertNotEqual(picker.selected_key, ("codex", "native-old"))
        self.assertIn(("codex", "native-new"), keys)
        self.assertIn(("codex", "default"), keys)
        self.select(picker, "codex", "user-model")
        picker.remove_selected()
        self.assertNotIn(("codex", "user-model"), model_catalog.added_models(self.config))

    def test_native_discovery_failure_does_not_revoke_signed_in_seat(self):
        from conch.seat_common import SeatError

        picker = ModelPicker(self.config, "codex", "default")
        source = model_catalog._seat_source("codex")
        with mock.patch("conch.auth.cli_installed", return_value=True), \
             mock.patch("conch.seats.status", return_value=(True, "signed in")), \
             mock.patch("conch.seat_codex.list_models",
                        side_effect=[["native-model"], SeatError("catalog unavailable")]):
            picker.apply_source(*source(self.config))
            self.select(picker, "codex", "native-model")
            picker.apply_source(*source(self.config))
        self.assertTrue(picker.statuses["codex"].available)
        self.assertIn("model list unavailable", picker.statuses["codex"].note)
        picker.choose()
        self.assertEqual(picker.result.key, ("codex", "native-model"))


class DiscoveryPickerTests(_ConfigCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        from conch.providers import clear_local_model_caches

        clear_local_model_caches()
        self.addCleanup(clear_local_model_caches)
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, payload, status=200):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                requests.append(("GET", self.path, None))
                if self.path == "/v1/models":
                    self.respond({"data": [{"id": "tool-model"}, {"id": "text-model"}]})
                elif self.path == "/api/tags":
                    self.respond({"models": [{"name": "ollama-model", "digest": "test"}]})
                elif self.path == "/v1/inference?tools=true":
                    self.respond({"registry_version": "0.1", "providers": [{
                        "name": "box", "status": "up", "flavor": "openai",
                        "base_url": "http://127.0.0.1:1/v1",
                        "models": [{"id": "registry-model", "tools": True}],
                    }]})
                else:
                    self.respond({}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(("POST", self.path, body))
                if self.path == "/api/show":
                    self.respond({"capabilities": ["tools"]})
                elif self.path == "/v1/chat/completions":
                    message = {"content": "not a tool caller"}
                    if body["model"] == "tool-model":
                        message = {"tool_calls": [{"type": "function", "function": {
                            "name": "conch_tool_probe",
                            "arguments": '{"token":"conch-ok"}',
                        }}]}
                    self.respond({"choices": [{"message": message}]})
                else:
                    self.respond({}, 404)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        self.config.update(custom_base_url=base + "/v1", ollama_base_url=base,
                           llamaidx_url=base, local_only="false")

    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout=5)

    async def test_selection_only_open_and_refresh_use_metadata_without_native_processes(self):
        original = dict(self.config)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("native process")) as popen, \
             mock.patch("conch.seats.status") as seat_status, \
             mock.patch("conch.seat_codex.list_models") as native_models:
            picker = ModelPicker(self.config, "anthropic", "claude-sonnet-5",
                                 selection_only=True)
            with create_pipe_input() as input:
                app = picker.build(input=input, output=DummyOutput())
                task = asyncio.create_task(app.run_async(pre_run=picker.start_live))
                try:
                    await self.wait_until(lambda: app.is_running and not picker.loading)
                    keys = {entry.key for entry in picker.entries}
                    self.assertTrue({
                        ("custom", "tool-model"), ("custom", "text-model"),
                        ("ollama", "ollama-model"), ("llamaidx", "llamaidx/box/registry-model"),
                        ("codex", "default"), ("claude-code", "opus"),
                    }.issubset(keys))
                    input.send_text("\x12")  # Ctrl-R uses the same safe discovery mode.
                    await self.wait_until(lambda: (
                        sum(path == "/v1/models" for _, path, _ in self.requests) == 2
                        and not picker.loading))
                    self.assertEqual(picker.statuses["codex"].note,
                                     "sign-in not checked (metadata only)")
                    self.assertEqual(self.config, original)
                    picker.query.text = "text-model"
                    input.send_text("\r")
                    await asyncio.wait_for(task, timeout=5)
                    self.assertEqual(picker.result.key, ("custom", "text-model"))
                finally:
                    if not task.done():
                        app.exit()
                    await asyncio.wait_for(task, timeout=5)
            seat_status.assert_not_called()
            native_models.assert_not_called()
            popen.assert_not_called()
        paths = [(method, path) for method, path, _ in self.requests]
        self.assertEqual(paths.count(("GET", "/v1/models")), 2)
        self.assertEqual(paths.count(("GET", "/api/tags")), 2)
        self.assertEqual(paths.count(("GET", "/v1/inference?tools=true")), 2)
        self.assertTrue(set(paths).issubset({
            ("GET", "/v1/models"), ("GET", "/api/tags"),
            ("GET", "/v1/inference?tools=true"), ("POST", "/api/show"),
        }), paths)

    async def test_normal_picker_still_probes_custom_models_and_discovers_native_seats(self):
        with mock.patch("conch.auth.cli_installed", return_value=True), \
             mock.patch("conch.seats.status", return_value=(True, "signed in")) as seats, \
             mock.patch("conch.seat_codex.list_models", return_value=["native-model"]) as models:
            picker = ModelPicker(self.config, "anthropic", "claude-sonnet-5")
            picker.start_live()
            await self.wait_until(lambda: not picker.loading)
        keys = {entry.key for entry in picker.entries}
        self.assertIn(("custom", "tool-model"), keys)
        self.assertNotIn(("custom", "text-model"), keys)
        self.assertIn(("codex", "native-model"), keys)
        self.assertEqual({call.args[0] for call in seats.call_args_list},
                         {"claude-code", "codex"})
        models.assert_called_once()
        self.assertEqual(
            [body["model"] for method, path, body in self.requests
             if (method, path) == ("POST", "/v1/chat/completions")],
            ["tool-model", "text-model"],
        )


class PrimaryPickerTests(_ConfigCase, unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout=5)

    def primary_picker(self):
        from conch.commands import handle_slash_command

        self.config["model_check"] = "off"

        def apply(entry):
            return handle_slash_command(
                entry.switch_command(), self.config,
                self.config["provider"], self.config["chat_model"],
                lambda _on: None) is not None

        return ModelPicker(self.config, "anthropic", "claude-sonnet-5",
                           on_primary=apply)

    async def test_enter_applies_highlighted_row_once_then_closes(self):
        picker = self.primary_picker()
        picker.query.text = "gpt-6-sol"
        picker.selected_key = ("openai", "gpt-6-sol")
        with create_pipe_input() as input:
            app = picker.build(input=input, output=DummyOutput())
            # A repeated Return/arrow in the same input batch must not move
            # the selection out from under the asynchronous validation.
            input.send_text("\r\x1b[B\r")
            with mock.patch.object(picker, "on_primary", wraps=picker.on_primary) as apply:
                await asyncio.wait_for(app.run_async(), timeout=5)
                self.assertEqual([call.args[0].key for call in apply.call_args_list],
                                 [("openai", "gpt-6-sol")])
        self.assertEqual((self.config["provider"], self.config["chat_model"]), picker.current)
        self.assertEqual(picker.current, ("openai", "gpt-6-sol"))

    async def test_ctrl_p_switches_primary_and_keeps_filter_and_role_controls_live(self):
        picker = self.primary_picker()
        picker.query.text = "gpt-6-sol"
        picker.selected_key = ("openai", "gpt-6-sol")
        with create_pipe_input() as input:
            app = picker.build(input=input, output=DummyOutput())
            task = asyncio.create_task(app.run_async())
            try:
                await self.wait_until(lambda: app.is_running)
                input.send_text("\x10")
                await self.wait_until(lambda: picker.current == ("openai", "gpt-6-sol"))
                self.assertTrue(app.is_running)
                self.assertEqual(self.config["chat_model"], "gpt-6-sol")
                self.assertEqual(picker.query.text, "gpt-6-sol")
                self.assertEqual(picker.selected_key, picker.current)
                input.send_text("\x14")
                await self.wait_until(lambda: self.config.get("weak_model") == "gpt-6-sol")
                self.assertEqual(self.saved()["weak_provider"], "openai")
                self.assertTrue(app.is_running)
                input.send_text("\x03")
                await asyncio.wait_for(task, timeout=5)
                self.assertEqual(self.config["provider"], "openai")
            finally:
                if not task.done():
                    app.exit()
                    await task

    async def test_failed_primary_validation_keeps_previous_model_and_picker_open(self):
        picker = self.primary_picker()
        picker.entries.append(ModelEntry("openai", "not-a-known-model"))
        picker.query.text = "not-a-known-model"
        original = dict(self.config)
        with create_pipe_input() as input:
            app = picker.build(input=input, output=DummyOutput())
            task = asyncio.create_task(app.run_async())
            try:
                await self.wait_until(lambda: app.is_running)
                input.send_text("\x10")
                await self.wait_until(lambda: "unchanged" in picker.notice)
                self.assertEqual(self.config, original)
                self.assertEqual(picker.current, ("anthropic", "claude-sonnet-5"))
                self.assertTrue(app.is_running)
            finally:
                if not task.done():
                    app.exit()
                await asyncio.wait_for(task, timeout=5)


class ModelCommandTests(_ConfigCase):
    def test_explicit_provider_wins_over_catalog_inference(self):
        """The same id can exist under several providers; the picker's
        --provider must decide, not catalog order."""
        from conch.commands import handle_slash_command

        self.config["model_check"] = "off"
        messages = [{"role": "system", "content": "s"}]
        with mock.patch("conch.model_catalog.provider_for", return_value="anthropic"):
            result = handle_slash_command(
                "/model gpt-6-sol --provider openai", self.config, "anthropic",
                "claude-sonnet-5", lambda _on: None, messages=messages)
        self.assertEqual(result[:2], ("openai", "gpt-6-sol"))
        self.assertEqual(self.config["provider"], "openai")


if __name__ == "__main__":
    unittest.main()
