import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from conch.providers import (
    clear_local_model_caches,
    raw_custom,
    raw_ollama,
)
from conch.runtime import chat_turn


MODELS = ("qwen2.5:7b", "llama3.2:3b")


class _OllamaHandler(BaseHTTPRequestHandler):
    requests = []
    loaded = False

    def log_message(self, *_args):
        pass

    def _json(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/tags":
            self._json({
                "models": [
                    {
                        "name": model,
                        "digest": f"sha256:{index}",
                    }
                    for index, model in enumerate(MODELS)
                ] + [{
                    "name": "embed-only:latest",
                    "digest": "sha256:embed",
                }]
            })
            return
        if self.path == "/api/ps":
            self._json({
                "models": [
                    {"name": model, "context_length": 16384}
                    for model in MODELS
                ] if self.__class__.loaded else []
            })
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.__class__.requests.append((self.path, body))
        if self.path == "/api/show":
            capabilities = ["completion"]
            if body.get("model") in MODELS:
                capabilities.append("tools")
            self._json({
                "capabilities": capabilities,
                "model_info": {"llama.context_length": 32768},
            })
            return
        if self.path == "/api/chat":
            self.__class__.loaded = True
            messages = body.get("messages") or []
            if messages and messages[-1].get("role") == "tool":
                self._json({
                    "message": {
                        "role": "assistant",
                        "content": "tool completed",
                    },
                    "prompt_eval_count": 120,
                    "eval_count": 4,
                })
            else:
                self._json({
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "function": {
                                "name": "local_shell",
                                "arguments": {"command": "printf native-ok"},
                            }
                        }],
                    },
                    "prompt_eval_count": 100,
                    "eval_count": 8,
                })
            return
        self.send_error(404)


class _Shell:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {
            "content": [
                {"type": "text", "text": "native-ok"}
            ]
        }


class TestOllamaHTTPIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()
        cls.base_url = (
            f"http://127.0.0.1:{cls.server.server_address[1]}"
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        clear_local_model_caches()
        _OllamaHandler.requests.clear()
        _OllamaHandler.loaded = False

    def _run_model(self, model):
        config = {
            "provider": "ollama",
            "model": model,
            "chat_model": model,
            "ollama_base_url": self.base_url,
            "local_tool_exemplar": "true",
        }
        tool = {
            "type": "function",
            "function": {
                "name": "local_shell",
                "description": "Run a command",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"}
                    },
                    "required": ["command"],
                },
            },
        }
        shell = _Shell()
        messages = [
            {"role": "system", "content": "Use tools."},
            {"role": "user", "content": "print the marker"},
        ]
        with patch("sys.stderr", io.StringIO()):
            reply, usage = chat_turn(
                config,
                "ollama",
                raw_ollama,
                messages,
                [tool],
                {},
                {"local_shell": shell},
                max_tool_rounds=4,
            )
        self.assertEqual(reply, "tool completed")
        self.assertEqual(
            shell.calls,
            [("local_shell", {"command": "printf native-ok"})],
        )
        self.assertEqual(usage["model"], model)
        chat_bodies = [
            body
            for path, body in _OllamaHandler.requests
            if path == "/api/chat"
        ]
        self.assertEqual(len(chat_bodies), 2)
        self.assertEqual(
            sum(
                message.get("role") == "system"
                for message in chat_bodies[0]["messages"]
            ),
            1,
        )
        self.assertEqual(
            chat_bodies[0]["options"]["temperature"], 0.2
        )
        self.assertNotIn("num_ctx", chat_bodies[0]["options"])
        first_system = next(
            message["content"]
            for message in chat_bodies[0]["messages"]
            if message.get("role") == "system"
        )
        self.assertIn("4,096 tokens", first_system)
        second_system = next(
            message["content"]
            for message in chat_bodies[1]["messages"]
            if message.get("role") == "system"
        )
        self.assertIn("16,384 tokens", second_system)

    def test_qwen_native_tool_loop(self):
        self._run_model("qwen2.5:7b")

    def test_llama_native_tool_loop(self):
        self._run_model("llama3.2:3b")


class _CustomHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def _json(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            self._json({
                "data": [{
                    "id": "llama-cpp-test",
                    "context_length": 8192,
                }]
            })
            return
        if self.path in ("/v1/props", "/props"):
            self._json({
                "default_generation_settings": {"n_ctx": 8192},
                "chat_template": "tool-aware jinja",
            })
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.__class__.requests.append(body)
        tools = body.get("tools") or []
        first_name = (
            tools[0].get("function", {}).get("name") if tools else ""
        )
        if first_name == "conch_tool_probe":
            self._json({
                "choices": [{"message": {"tool_calls": [{
                    "id": "probe",
                    "type": "function",
                    "function": {
                        "name": "conch_tool_probe",
                        "arguments": '{"token":"conch-ok"}',
                    },
                }]}}]
            })
            return
        messages = body.get("messages") or []
        if messages and messages[-1].get("role") == "tool":
            message = {"role": "assistant", "content": "custom completed"}
        else:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "custom-call",
                    "type": "function",
                    "function": {
                        "name": "local_shell",
                        "arguments": '{"command":"printf custom-ok"}',
                    },
                }],
            }
        self._json({
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5},
        })


class TestCustomHTTPIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _CustomHandler)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()
        cls.base_url = (
            f"http://127.0.0.1:{cls.server.server_address[1]}/v1"
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        clear_local_model_caches()
        _CustomHandler.requests.clear()

    def test_llamacpp_compatible_discovery_probe_and_tool_loop(self):
        config = {
            "provider": "custom",
            "model": "llama-cpp-test",
            "chat_model": "llama-cpp-test",
            "custom_model": "llama-cpp-test",
            "custom_base_url": self.base_url,
        }
        tool = {
            "type": "function",
            "function": {
                "name": "local_shell",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"}
                    },
                    "required": ["command"],
                },
            },
        }
        shell = _Shell()
        with patch("sys.stderr", io.StringIO()):
            reply, _ = chat_turn(
                config,
                "custom",
                raw_custom,
                [{"role": "user", "content": "print marker"}],
                [tool],
                {},
                {"local_shell": shell},
                max_tool_rounds=4,
            )
        self.assertEqual(reply, "custom completed")
        self.assertEqual(
            shell.calls,
            [("local_shell", {"command": "printf custom-ok"})],
        )
        probe = next(
            body
            for body in _CustomHandler.requests
            if body.get("tool_choice") == "required"
        )
        self.assertEqual(
            probe["tools"][0]["function"]["name"],
            "conch_tool_probe",
        )
        actual = [
            body
            for body in _CustomHandler.requests
            if body.get("tool_choice") != "required"
        ]
        self.assertEqual(len(actual), 2)
        self.assertLessEqual(actual[0]["max_tokens"], 8192)
        first_system = next(
            message["content"]
            for message in actual[0]["messages"]
            if message.get("role") == "system"
        )
        self.assertIn("8,192 tokens", first_system)


if __name__ == "__main__":
    unittest.main()
