"""Scriptable HTTP stand-ins for the endpoints the startup model check
probes: an OpenAI-compatible server (``/v1/models``,
``/v1/chat/completions`` — llama.cpp, vLLM, OpenRouter, OpenAI itself), an
Anthropic server (``/v1/models``, ``/v1/messages``) and an Ollama server
(``/api/tags``, ``/api/show``). One :class:`StubEndpoint` can play any of
them; its behaviour per failure class is scripted through attributes:

* ``models`` — ids exposed by the list endpoints;
* ``tool_capable`` — ids that answer the forced tool-call conformance
  probe with the expected native tool call (others answer in text);
* ``require_token`` — when set, every request without that bearer /
  ``x-api-key`` gets a 401 (``auth_failed``);
* ``hang`` — seconds every handler sleeps before answering (``timeout``);
* ``force_status`` — ``(status, json_body)`` returned by every request
  (quota, rate limiting, server errors);
* ``ask_commands`` — what ask mode's forced ``shell_command`` tool call
  gets back, per model id (a model with no entry, or an empty one,
  answers in prose — the way a model that cannot call tools fails);
* ``requests`` — the log of ``(method, path)`` pairs the server saw;
  ``chat_tools`` — the tool names each ``/chat/completions`` request
  offered, in order, so a test can tell the conformance probe from the
  real call.

Nothing here ever records or echoes an Authorization header value.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple


def closed_port_url() -> str:
    """A loopback URL nothing listens on (connection refused)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


class StubEndpoint:
    def __init__(self, models=("stub-model",), tool_capable=None, *,
                 require_token: str = "", hang: float = 0.0,
                 force_status: Optional[Tuple[int, dict]] = None,
                 ask_commands: Optional[Dict[str, str]] = None):
        self.models: List[str] = list(models)
        self.tool_capable = set(self.models if tool_capable is None else tool_capable)
        self.require_token = require_token
        self.hang = hang
        self.force_status = force_status
        self.ask_commands: Dict[str, str] = dict(ask_commands or {})
        self.requests: List[Tuple[str, str]] = []
        self.chat_tools: List[List[str]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # silence
                pass

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw.decode() or "{}")
                except ValueError:
                    return {}

            def _authorized(self) -> bool:
                if not stub.require_token:
                    return True
                bearer = self.headers.get("Authorization", "")
                if bearer == f"Bearer {stub.require_token}":
                    return True
                return self.headers.get("x-api-key", "") == stub.require_token

            def _handle(self, method: str) -> None:
                path = self.path.split("?", 1)[0]
                with stub._lock:
                    stub.requests.append((method, path))
                body = self._body() if method == "POST" else {}
                if stub.hang:
                    deadline = time.monotonic() + stub.hang
                    while time.monotonic() < deadline and not stub._stop.is_set():
                        time.sleep(0.05)
                if stub.force_status is not None:
                    status, payload = stub.force_status
                    return self._send(status, payload)
                if not self._authorized():
                    return self._send(401, {"error": {
                        "message": "Incorrect API key provided",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }})
                if method == "GET" and path.endswith("/models"):
                    return self._send(200, {"object": "list", "data": [
                        {"id": name, "object": "model"} for name in stub.models
                    ]})
                if method == "GET" and path == "/api/tags":
                    return self._send(200, {"models": [
                        {"name": name, "model": name, "digest": f"sha256:{name}"}
                        for name in stub.models
                    ]})
                if method == "POST" and path == "/api/show":
                    name = body.get("model") or body.get("name") or ""
                    caps = ["completion"]
                    if name in stub.tool_capable:
                        caps.append("tools")
                    return self._send(200, {"capabilities": caps})
                if method == "POST" and path == "/api/chat":
                    name = body.get("model") or ""
                    if name not in stub.models:
                        return self._send(404, {"error": f"model '{name}' not found"})
                    return self._send(200, {
                        "model": name, "done": True, "done_reason": "stop",
                        "message": {"role": "assistant", "content": "pong"},
                        "prompt_eval_count": 1, "eval_count": 1,
                        "prompt_eval_duration": 1000000, "eval_duration": 1000000,
                    })
                if method == "POST" and path.endswith("/chat/completions"):
                    return self._chat(body)
                if method == "POST" and path.endswith("/messages"):
                    return self._anthropic(body)
                return self._send(404, {"error": {"message": f"no route for {path}"}})

            def _chat(self, body: dict) -> None:
                model = body.get("model", "")
                if model not in stub.models:
                    return self._send(404, {"error": {
                        "message": f"The model '{model}' does not exist",
                        "type": "invalid_request_error",
                        "code": "model_not_found",
                    }})
                offered = {
                    (tool.get("function") or {}).get("name")
                    for tool in body.get("tools") or [] if isinstance(tool, dict)
                }
                with stub._lock:
                    stub.chat_tools.append(sorted(name for name in offered if name))
                if "shell_command" in offered:
                    command = stub.ask_commands.get(model, "")
                    if command:
                        message = {
                            "role": "assistant", "content": None,
                            "tool_calls": [{
                                "id": "call_ask", "type": "function",
                                "function": {
                                    "name": "shell_command",
                                    "arguments": json.dumps({"command": command}),
                                },
                            }],
                        }
                    else:
                        message = {"role": "assistant",
                                   "content": f"You could run `{model}` style commands."}
                elif "conch_tool_probe" in offered:
                    if model in stub.tool_capable:
                        message = {
                            "role": "assistant", "content": None,
                            "tool_calls": [{
                                "id": "call_0", "type": "function",
                                "function": {
                                    "name": "conch_tool_probe",
                                    "arguments": json.dumps({"token": "conch-ok"}),
                                },
                            }],
                        }
                    else:
                        message = {"role": "assistant",
                                   "content": "I cannot call tools, sorry."}
                else:
                    message = {"role": "assistant", "content": "pong"}
                return self._send(200, {
                    "id": "chatcmpl-stub", "object": "chat.completion", "model": model,
                    "choices": [{"index": 0, "message": message,
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                              "total_tokens": 2},
                })

            def _anthropic(self, body: dict) -> None:
                model = body.get("model", "")
                if model not in stub.models:
                    return self._send(404, {"type": "error", "error": {
                        "type": "not_found_error", "message": f"model: {model}",
                    }})
                return self._send(200, {
                    "id": "msg_stub", "type": "message", "role": "assistant",
                    "model": model, "content": [{"type": "text", "text": "pong"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                })

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )

    def start(self) -> "StubEndpoint":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()

    @property
    def v1(self) -> str:
        return f"{self.url}/v1"

    def paths(self, method: Optional[str] = None) -> List[str]:
        with self._lock:
            return [p for m, p in self.requests if method is None or m == method]
