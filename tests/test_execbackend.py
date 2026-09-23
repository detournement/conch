"""Gates for the pluggable execution backend (sandboxed shell commands).

Covered: backend routing in LocalShellClient (argv path, text path,
sandbox errors become clean tool results), the gated-off default, the
E2B client against a fake server encoding the researched Connect
contract (create -> Start stream -> kill), fail-closed behavior on
unknown API shapes, and the credential-non-forwarding invariant (host
environment never appears in sandbox requests). A live docker
round-trip is opt-in behind CONCH_SANDBOX_DOCKER_DRILL=1.
"""

import base64
import json
import os
import shutil
import struct
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from conch.execbackend import (
    BACKEND_KINDS,
    DockerExecBackend,
    E2BExecBackend,
    SandboxError,
    build_exec_backend,
)
from conch.tooling import LocalShellClient, LocalShellPolicy, PermissionState


def _text_of(result: dict) -> str:
    return result["content"][0]["text"]


class _FakeBackend:
    kind = "fake"

    def __init__(self, argv=None, output=("ok", 0), error=None):
        self._argv = argv
        self._output = output
        self._error = error
        self.closed = False
        self.ran = []

    def describe(self):
        return "fake backend"

    def wrap_argv(self, cmd):
        if self._error:
            raise SandboxError(self._error)
        if self._argv is not None:
            return list(self._argv) + [cmd]
        return None

    def run(self, cmd, timeout):
        self.ran.append((cmd, timeout))
        return self._output

    def close(self):
        self.closed = True


def _auto_shell(backend=None) -> LocalShellClient:
    perms = PermissionState()
    perms.set_agent_mode(True)
    client = LocalShellClient(permissions=perms)
    client.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
    if backend is not None:
        client.set_exec_backend(backend)
    return client


class BackendRoutingTests(unittest.TestCase):
    def test_no_backend_runs_locally(self):
        client = _auto_shell()
        self.assertIsNone(client.exec_backend())
        result = client.call_tool("local_shell", {"command": "echo local-path"})
        self.assertIn("local-path", _text_of(result))

    def test_argv_backend_routes_through_wrapped_argv(self):
        # The fake wraps commands as `sh -c <cmd>` argv — proving the
        # wrap_argv path executes the wrapped argv, not the raw command.
        backend = _FakeBackend(argv=["sh", "-c"])
        client = _auto_shell(backend)
        result = client.call_tool(
            "local_shell", {"command": "echo wrapped-argv-path"}
        )
        self.assertIn("wrapped-argv-path", _text_of(result))

    def test_text_backend_output_and_exit_code(self):
        backend = _FakeBackend(output=("remote says hi", 3))
        client = _auto_shell(backend)
        result = client.call_tool("local_shell", {"command": "anything"})
        text = _text_of(result)
        self.assertIn("remote says hi", text)
        self.assertIn("exit code 3", text)
        self.assertEqual(backend.ran[0][0], "anything")

    def test_sandbox_error_is_clean_tool_result(self):
        backend = _FakeBackend(error="container exploded")
        client = _auto_shell(backend)
        result = client.call_tool("local_shell", {"command": "true"})
        self.assertIn("Sandbox unavailable: container exploded", _text_of(result))

    def test_swapping_backend_closes_previous(self):
        first = _FakeBackend()
        second = _FakeBackend()
        client = _auto_shell(first)
        client.set_exec_backend(second)
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)

    def test_text_backend_respects_result_budget(self):
        backend = _FakeBackend(output=("x" * 100_000, 0))
        client = _auto_shell(backend)
        client.set_result_budget(2000)
        text = _text_of(client.call_tool("local_shell", {"command": "big"}))
        self.assertLess(len(text), 4000)


class GatingTests(unittest.TestCase):
    def _make_clients(self, config: dict, extra_env=None):
        from conch.bootstrap import make_builtin_clients
        from conch.memory import MemoryStore
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "HOME": tmp,
                "XDG_CONFIG_HOME": os.path.join(tmp, ".config"),
                "XDG_STATE_HOME": os.path.join(tmp, ".state"),
                "PATH": os.environ.get("PATH", ""),
            }
            env.update(extra_env or {})
            with mock.patch.dict(os.environ, env, clear=True):
                memory = MemoryStore()
                return make_builtin_clients(memory, config, interactive=False)

    def test_unset_config_means_no_backend(self):
        clients = self._make_clients({})
        self.assertIsNone(clients["local_shell"].exec_backend())

    def test_broken_backend_config_degrades_to_local(self):
        # e2b configured but no key in the environment: startup warns and
        # degrades to local instead of crashing the shell.
        clients = self._make_clients({"exec_backend": "e2b"})
        self.assertIsNone(clients["local_shell"].exec_backend())

    def test_unknown_backend_kind_fails_closed(self):
        with self.assertRaises(SandboxError):
            build_exec_backend("firecracker", {})

    def test_local_kind_returns_none(self):
        for kind in ("", "local", "off"):
            self.assertIsNone(build_exec_backend(kind, {}))

    def test_docker_missing_binary_fails_with_actionable_message(self):
        with mock.patch("conch.execbackend.shutil.which", return_value=None):
            with self.assertRaises(SandboxError) as ctx:
                DockerExecBackend({})
        self.assertIn("docker not found", str(ctx.exception))

    def test_docker_bad_mount_mode_fails_closed(self):
        with mock.patch("conch.execbackend.shutil.which", return_value="/usr/bin/docker"):
            with self.assertRaises(SandboxError):
                DockerExecBackend({"sandbox_docker_mount": "sideways"})

    def test_e2b_missing_key_fails_with_env_name(self):
        env = {k: v for k, v in os.environ.items() if k != "MY_E2B_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SandboxError) as ctx:
                E2BExecBackend({"e2b_api_key_env": "MY_E2B_KEY"})
        self.assertIn("MY_E2B_KEY", str(ctx.exception))


# ---------------------------------------------------------------------------
# Fake E2B service: control plane + envd Connect stream on one server.
# ---------------------------------------------------------------------------

def _frame(payload: dict, flags: int = 0) -> bytes:
    raw = json.dumps(payload).encode()
    return bytes([flags]) + struct.pack(">I", len(raw)) + raw


class _FakeE2B:
    """Scripted fake of the researched E2B contract (2026): REST
    ``POST /sandboxes`` create with X-API-KEY -> sandboxID/domain/
    envdAccessToken, envd ``POST /process.Process/Start`` Connect
    server-stream (start/data/end + EndStream frame), and DELETE kill."""

    def __init__(self, create_response=None, stream_frames=None):
        self.requests = []
        self.killed = []
        self._create_response = create_response
        self._stream_frames = stream_frames
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: N802
                pass

            def _read_body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def do_POST(self):  # noqa: N802
                body = self._read_body()
                headers = {k.lower(): v for k, v in self.headers.items()}
                fake.requests.append(("POST", self.path, headers, body))
                if self.path == "/sandboxes":
                    if self.headers.get("X-API-KEY") != "test-key":
                        self.send_response(401)
                        self.end_headers()
                        return
                    payload = fake._create_response or {
                        "sandboxID": "sbx-123",
                        # Point the "sandbox host" back at this fake.
                        "domain": f"IGNORED",
                        "envdAccessToken": "envd-token-abc",
                    }
                    raw = json.dumps(payload).encode()
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.path == "/process.Process/Start":
                    frames = fake._stream_frames or [
                        _frame({"event": {"start": {"pid": 7}}}),
                        _frame({"event": {"data": {
                            "stdout": base64.b64encode(b"hello from e2b\n").decode()
                        }}}),
                        _frame({"event": {"end": {"exitCode": 0}}}),
                        _frame({}, flags=0x02),
                    ]
                    raw = b"".join(frames)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/connect+json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.send_response(404)
                self.end_headers()

            def do_DELETE(self):  # noqa: N802
                fake.requests.append(("DELETE", self.path, dict(self.headers), b""))
                fake.killed.append(self.path)
                self.send_response(204)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _backend_for(fake: _FakeE2B, **config) -> E2BExecBackend:
    """An E2B backend whose control AND data planes hit the fake (the
    data-plane URL is rewritten from https://sandbox.{domain} to the
    fake server through a patched urlopen)."""
    import urllib.request

    cfg = {"e2b_api_url": fake.url, "e2b_api_key_env": "TEST_E2B_KEY"}
    cfg.update(config)

    def urlopen(req, timeout=0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.startswith("https://sandbox."):
            path = url.split("/", 3)[3] if url.count("/") >= 3 else ""
            req.full_url = f"{fake.url}/{path}"
        return urllib.request.urlopen(req, timeout=timeout)

    with mock.patch.dict(os.environ, {"TEST_E2B_KEY": "test-key"}):
        return E2BExecBackend(cfg, _urlopen=urlopen)


class E2BContractTests(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeE2B()
        self.addCleanup(self.fake.stop)

    def test_round_trip_create_run_kill(self):
        backend = _backend_for(self.fake)
        output, code = backend.run("echo hi", timeout=10)
        self.assertEqual(output, "hello from e2b\n")
        self.assertEqual(code, 0)
        backend.close()
        self.assertTrue(self.fake.killed)
        self.assertIn("/sandboxes/sbx-123", self.fake.killed[0])

    def test_envd_request_carries_contract_headers(self):
        backend = _backend_for(self.fake)
        backend.run("true", timeout=5)
        start = [r for r in self.fake.requests if r[1] == "/process.Process/Start"]
        self.assertEqual(len(start), 1)
        headers = start[0][2]
        self.assertEqual(headers.get("e2b-sandbox-id"), "sbx-123")
        self.assertEqual(headers.get("e2b-sandbox-port"), "49983")
        self.assertEqual(headers.get("connect-protocol-version"), "1")
        self.assertEqual(headers.get("x-access-token"), "envd-token-abc")

    def test_host_environment_never_forwarded(self):
        canary = "conch-canary-secret-9f2"
        with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": canary,
                                          "PATH": os.environ.get("PATH", "")}):
            backend = _backend_for(self.fake)
            backend.run("env", timeout=5)
        start = [r for r in self.fake.requests if r[1] == "/process.Process/Start"]
        body = start[0][3]
        self.assertNotIn(canary.encode(), body)
        # And the request's envs map is explicitly empty.
        payload = json.loads(body[5:5 + struct.unpack(">I", body[1:5])[0]])
        self.assertEqual(payload["process"]["envs"], {})

    def test_create_response_missing_token_fails_closed(self):
        self.fake._create_response = {"sandboxID": "sbx-9", "domain": "x"}
        backend = _backend_for(self.fake)
        with self.assertRaises(SandboxError) as ctx:
            backend.run("true", timeout=5)
        self.assertIn("envdAccessToken", str(ctx.exception))

    def test_stream_without_end_event_fails_closed(self):
        self.fake._stream_frames = [
            _frame({"event": {"data": {
                "stdout": base64.b64encode(b"partial").decode()}}}),
            _frame({}, flags=0x02),
        ]
        backend = _backend_for(self.fake)
        with self.assertRaises(SandboxError) as ctx:
            backend.run("true", timeout=5)
        self.assertIn("without a process end event", str(ctx.exception))

    def test_end_stream_error_frame_fails_closed(self):
        self.fake._stream_frames = [
            _frame({"error": {"code": "internal", "message": "boom"}},
                   flags=0x02),
        ]
        backend = _backend_for(self.fake)
        with self.assertRaises(SandboxError) as ctx:
            backend.run("true", timeout=5)
        self.assertIn("envd stream error", str(ctx.exception))

    def test_nonzero_exit_code_reported(self):
        self.fake._stream_frames = [
            _frame({"event": {"data": {
                "stderr": base64.b64encode(b"nope\n").decode()}}}),
            _frame({"event": {"end": {"exitCode": 2}}}),
            _frame({}, flags=0x02),
        ]
        backend = _backend_for(self.fake)
        output, code = backend.run("false", timeout=5)
        self.assertEqual(code, 2)
        self.assertIn("nope", output)


_DOCKER_DRILL = (
    os.environ.get("CONCH_SANDBOX_DOCKER_DRILL") == "1"
    and shutil.which("docker") is not None
)


@unittest.skipUnless(
    _DOCKER_DRILL,
    "docker sandbox drill opt-in (set CONCH_SANDBOX_DOCKER_DRILL=1 with "
    "a running Docker daemon)",
)
class DockerLiveDrill(unittest.TestCase):
    def test_docker_round_trip_and_cleanup(self):
        backend = DockerExecBackend(
            {"sandbox_docker_mount": "none",
             "sandbox_docker_image": "python:3.12-slim"},
        )
        try:
            argv = backend.wrap_argv("echo sandbox-drill-ok && id -u")
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=120)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("sandbox-drill-ok", proc.stdout)
            container = backend._container
            self.assertTrue(container)
        finally:
            backend.close()
        gone = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True, timeout=30,
        )
        self.assertNotEqual(gone.returncode, 0)


if __name__ == "__main__":
    unittest.main()
