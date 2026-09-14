"""Matrix sessions are remote sessions — integration invariants.

Proven here: the safe_auto cap, REMOTE_EXCLUDED_TOOLS, and origin-bound
approvals (`approve N` in the Matrix thread; origin = matrix + room/thread
+ sender) hold for Matrix exactly as for Slack/SMS/email; approval
requests are additionally pushed over ntfy when ``notify_push = ntfy`` is
routed; the daemon's channel_intake lease covers the Matrix poller; and
the daemon's outbox delivery mirrors interrupts to ntfy.
"""

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.channels import InboundMessage
from conch.remote import REMOTE_EXCLUDED_TOOLS, RemoteLoop, RemoteShellClient
from conch.tooling import ToolRuntimeState, set_agent_mode

from tests.test_matrix_channel import (
    MATRIX_CONFIG,
    NTFY_CONFIG,
    _MatrixRouter,
    _sync_payload,
    _text_event,
)

ROOM = "!room:conch.local"
YOU = "@you:conch.local"


def _matrix_msg(text, sender=YOU, thread=ROOM):
    return InboundMessage(
        channel="matrix", sender=sender, text=text, thread_id=thread,
    )


class MatrixRemoteCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "MATRIX_ACCESS_TOKEN": "syt-secret-token",
            "NTFY_TOKEN": "",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        set_agent_mode(False)
        self.addCleanup(set_agent_mode, False)

    def make_loop(self, config=None, tools=()):
        config = config or dict(
            MATRIX_CONFIG, provider="ollama",
            chat_model="qwen3:8b", model="qwen3:8b",
        )
        state = ToolRuntimeState(
            all_tools=[], tool_map={},
            tools=[{"function": {"name": name}} for name in tools],
        )
        clients = {name: object() for name in tools}
        return RemoteLoop(config, conv_mgr=_FakeConvManager(),
                          chat_state=state, builtin_clients=clients)


class _FakeConvManager:
    def __init__(self):
        self.saved = []
        self._convs = {}

    def create(self, model, provider):
        import types
        conv = types.SimpleNamespace(
            id=f"conv{len(self._convs) + 1}", title="", model=model,
            provider=provider, messages=[],
        )
        self._convs[conv.id] = conv
        return conv

    def load(self, conv_id):
        return self._convs.get(conv_id)

    def save(self, conv):
        self.saved.append(conv.id)


class TestMatrixApprovalRoundTrip(MatrixRemoteCase):
    """approve/deny/origin-mismatch/expiry over the fake homeserver: the
    reply rides the real MatrixChannel.send path (recorded PUTs)."""

    def test_approve_executes_and_replies_in_room(self):
        loop = self.make_loop()
        rid = loop.approvals.add("echo matrix-approved", "matrix", ROOM, YOU)
        router = _MatrixRouter()
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("sys.stderr", io.StringIO()), \
             patch("sys.stdout", io.StringIO()):
            reply = loop.handle_inbound(_matrix_msg(f"approve {rid}"))
        self.assertIn("matrix-approved", reply)
        sends = router.send_requests()
        self.assertEqual(len(sends), 1, "the result went back over Matrix")
        body = json.loads(sends[0]["body"].decode())
        self.assertIn("matrix-approved", body["body"])
        self.assertIsNone(loop.approvals.pop(rid), "approval consumed")

    def test_approve_in_thread_binds_to_thread_origin(self):
        loop = self.make_loop()
        thread = f"{ROOM}|$root1"
        rid = loop.approvals.add("echo threaded", "matrix", thread, YOU)
        router = _MatrixRouter()
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("sys.stderr", io.StringIO()), \
             patch("sys.stdout", io.StringIO()):
            # same id from the top level of the room: origin mismatch
            top_level = loop.handle_inbound(
                _matrix_msg(f"approve {rid}", thread=ROOM)
            )
            self.assertIn("different sender or conversation",
                          top_level.replace("\n", " "))
            # from inside the thread it executes, and the reply carries
            # the m.thread relation back to the same root
            reply = loop.handle_inbound(
                _matrix_msg(f"approve {rid}", thread=thread)
            )
        self.assertIn("threaded", reply)
        final = json.loads(router.send_requests()[-1]["body"].decode())
        self.assertEqual(final["m.relates_to"]["event_id"], "$root1")

    def test_deny_discards(self):
        loop = self.make_loop()
        rid = loop.approvals.add("touch /tmp/x", "matrix", ROOM, YOU)
        router = _MatrixRouter()
        with patch("urllib.request.urlopen", side_effect=router):
            reply = loop.handle_inbound(_matrix_msg(f"deny {rid}"))
        self.assertIn("Denied", reply)
        self.assertIsNone(loop.approvals.pop(rid))

    def test_origin_mismatch_wrong_sender(self):
        loop = self.make_loop()
        rid = loop.approvals.add("echo x", "matrix", ROOM, YOU)
        router = _MatrixRouter()
        with patch("urllib.request.urlopen", side_effect=router):
            reply = loop.handle_inbound(
                _matrix_msg(f"approve {rid}", sender="@other:conch.local")
            )
        self.assertIn("different sender", reply)
        self.assertIn(str(rid), loop.approvals.pending(),
                      "a mismatched approve must not consume the request")

    def test_expired_approval_never_executes(self):
        loop = self.make_loop()
        rid = loop.approvals.add("echo too-late", "matrix", ROOM, YOU)
        router = _MatrixRouter()
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("conch.remote.time.time",
                   return_value=time.time() + 100000):
            reply = loop.handle_inbound(_matrix_msg(f"approve {rid}"))
        self.assertIn("expired", reply)
        self.assertNotIn("too-late", reply)


class TestMatrixRemoteCaps(MatrixRemoteCase):
    def test_matrix_turn_is_a_capped_remote_session(self):
        loop = self.make_loop(tools=(
            "local_shell", "delegate_task", "conch_config",
            "personal_items", "interactive_terminal",
        ))
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients, **kwargs):
            seen["system"] = messages[0]["content"]
            seen["tools"] = tools or []
            seen["clients"] = builtin_clients
            return "ok", {"total_tokens": 1}

        router = _MatrixRouter()
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("conch.runtime.chat_turn", fake_chat_turn):
            loop.handle_inbound(_matrix_msg("what's the disk usage?"))
        self.assertIn("operating REMOTELY over matrix", seen["system"])
        tool_names = {t["function"]["name"] for t in seen["tools"]}
        self.assertFalse(tool_names & REMOTE_EXCLUDED_TOOLS)
        self.assertFalse(set(seen["clients"]) & REMOTE_EXCLUDED_TOOLS)
        self.assertIsInstance(seen["clients"]["local_shell"],
                              RemoteShellClient)
        # personal_items capture stays available (allowlisted senders
        # only — the fail-closed allowlist gates who ever reaches it)
        self.assertNotIn("personal_items", REMOTE_EXCLUDED_TOOLS)
        self.assertIn("personal_items", tool_names)
        self.assertIn("personal_items", seen["clients"])

    def test_mutating_command_posts_approval_to_matrix_and_ntfy(self):
        config = dict(
            MATRIX_CONFIG, **NTFY_CONFIG, notify_push="ntfy",
            provider="ollama", chat_model="qwen3:8b", model="qwen3:8b",
        )
        loop = self.make_loop(config=config, tools=("local_shell",))
        router = _MatrixRouter()
        ntfy_posts = []
        real_router = router.__call__

        def routed(req, timeout=None):
            if req.full_url.startswith("http://ntfy.test/"):
                ntfy_posts.append({
                    "url": req.full_url, "headers": dict(req.headers),
                    "body": req.data,
                })
                from tests.test_matrix_channel import _FakeHTTPResponse
                return _FakeHTTPResponse({"id": "p1"})
            return real_router(req, timeout=timeout)

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients, **kwargs):
            result = builtin_clients["local_shell"].call_tool(
                "local_shell", {"command": "touch /tmp/matrix-x"}
            )
            return result["content"][0]["text"], {"total_tokens": 1}

        with patch("urllib.request.urlopen", side_effect=routed), \
             patch("conch.runtime.chat_turn", fake_chat_turn):
            reply = loop.handle_inbound(_matrix_msg("create that file"))
        self.assertIn("requires user approval", reply)
        # the approval prompt went over the Matrix thread ...
        prompts = [json.loads(r["body"].decode())["body"]
                   for r in router.send_requests()]
        self.assertTrue(any("approve 1" in p for p in prompts), prompts)
        # ... and buzzed the phone over ntfy with a high-priority push
        self.assertEqual(len(ntfy_posts), 1)
        self.assertEqual(ntfy_posts[0]["headers"]["Priority"], "high")
        self.assertIn(b"touch /tmp/matrix-x", ntfy_posts[0]["body"])
        self.assertIn("Click", ntfy_posts[0]["headers"],
                      "the push deep-links back to the Element room")

    def test_reply_length_bounded_for_matrix(self):
        loop = self.make_loop()
        router = _MatrixRouter()
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("x" * 10_000, {})):
            reply = loop.handle_inbound(_matrix_msg("tell me everything"))
        self.assertLessEqual(len(reply), 3100)

    def test_thread_maps_to_one_conversation(self):
        loop = self.make_loop()
        router = _MatrixRouter()
        thread = f"{ROOM}|$root7"
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("conch.runtime.chat_turn", lambda *a, **k: ("ok", {})):
            loop.handle_inbound(_matrix_msg("first", thread=thread))
            loop.handle_inbound(_matrix_msg("second", thread=thread))
            loop.handle_inbound(_matrix_msg("elsewhere", thread=ROOM))
        sessions = json.loads(
            (self.root / "state" / "conch" / "remote_sessions.json")
            .read_text()
        )
        self.assertEqual(
            sorted(sessions),
            [f"matrix:{ROOM}", f"matrix:{ROOM}|$root7"],
            "thread == conversation, root-scoped",
        )


class DaemonMatrixCase(unittest.TestCase):
    """Isolated-XDG kernel daemon hosting Matrix intake (fake homeserver)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "MATRIX_ACCESS_TOKEN": "syt-secret-token",
            "NTFY_TOKEN": "",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.root / "runtime").mkdir(parents=True, exist_ok=True)
        os.chmod(self.root / "runtime", 0o700)
        set_agent_mode(False)
        self.addCleanup(set_agent_mode, False)
        for target, replacement in (
            ("create_clients", lambda: {}),
            ("collect_tools", lambda clients: ([], {})),
            ("save_tool_cache", lambda tools: None),
        ):
            mcp_patcher = patch(f"conch.mcp.{target}", replacement)
            mcp_patcher.start()
            self.addCleanup(mcp_patcher.stop)

    def make_daemon(self, config):
        from conch.kernel.daemon import EdgeDaemon

        class FakeClock:
            def __init__(self):
                self.now = 1_800_000_000.0

            def __call__(self):
                return self.now

            def advance(self, seconds):
                self.now += float(seconds)

        self.clock = FakeClock()
        daemon = EdgeDaemon(
            config, clock=self.clock,
            session_factory=lambda m, msgs, c, caps: ("out", {}),
        )
        self.addCleanup(daemon.shutdown)
        daemon.start()
        return daemon

    def base_config(self, **overrides):
        config = dict(
            MATRIX_CONFIG,
            provider="ollama", chat_model="qwen3:8b", model="qwen3:8b",
            remote_enabled="true", remote_poll_interval=1,
            mission_reviews="false", mission_consolidation="false",
        )
        config.update(overrides)
        return config

    def test_matrix_intake_rides_the_channel_intake_lease(self):
        from conch.kernel.intake import (
            INTAKE_LEASE_KIND,
            INTAKE_LEASE_RESOURCE,
        )

        daemon = self.make_daemon(self.base_config())
        router = _MatrixRouter()
        router.sync_responses = [
            _sync_payload([], next_batch="s1"),  # cursor-establishing sync
            _sync_payload([_text_event("hello from the phone")],
                          next_batch="s2"),
        ]
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("answered over matrix", {})):
            daemon.tick()          # first pass establishes the cursor
            self.clock.advance(2)
            stats = daemon.tick()  # second pass answers the message
        self.assertEqual(stats.get("intake"), 1)
        sends = router.send_requests()
        self.assertEqual(len(sends), 1)
        self.assertIn("answered over matrix",
                      json.loads(sends[0]["body"].decode())["body"])
        lease = daemon.store.get_lease(
            INTAKE_LEASE_KIND, INTAKE_LEASE_RESOURCE
        )
        self.assertEqual(lease["holder"], daemon.holder,
                         "the matrix poller runs under the intake lease")

    def test_non_allowlisted_matrix_sender_dropped_in_daemon(self):
        daemon = self.make_daemon(self.base_config())
        router = _MatrixRouter()
        router.sync_responses = [
            _sync_payload([], next_batch="s1"),
            _sync_payload(
                [_text_event("do bad things",
                             sender="@mallory:conch.local")],
                next_batch="s2",
            ),
        ]
        called = []
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("conch.runtime.chat_turn",
                   lambda *a, **k: called.append(1) or ("x", {})), \
             patch("sys.stderr", io.StringIO()):
            daemon.tick()
            self.clock.advance(2)
            daemon.tick()
        self.assertEqual(called, [])
        self.assertEqual(router.send_requests(), [],
                         "no reply of any kind to a non-allowlisted sender")

    def test_outbox_delivery_mirrors_to_ntfy(self):
        config = self.base_config(
            **NTFY_CONFIG, notify_push="ntfy", notify_channel="matrix",
        )
        daemon = self.make_daemon(config)
        daemon.store.enqueue_outbox(
            "channel_notify", {"text": "mission milestone reached"},
            "test:milestone:1",
        )
        router = _MatrixRouter()
        ntfy_posts = []
        real_router = router.__call__

        def routed(req, timeout=None):
            if req.full_url.startswith("http://ntfy.test/"):
                ntfy_posts.append(req.data)
                from tests.test_matrix_channel import _FakeHTTPResponse
                return _FakeHTTPResponse({"id": "p1"})
            return real_router(req, timeout=timeout)

        with patch("urllib.request.urlopen", side_effect=routed):
            delivered = daemon.deliver_outbox()
        self.assertEqual(delivered, 1)
        # the digest went to the Matrix room AND the phone push
        self.assertEqual(len(router.send_requests()), 1)
        self.assertEqual(ntfy_posts, [b"mission milestone reached"])
        row = daemon.store.list_outbox(status="delivered")[0]
        self.assertEqual(row["transport"], "matrix+ntfy")

    def test_push_alone_counts_as_delivery_without_channel(self):
        config = dict(
            provider="ollama", chat_model="qwen3:8b", model="qwen3:8b",
            mission_reviews="false", mission_consolidation="false",
            **NTFY_CONFIG, notify_push="ntfy",
        )
        daemon = self.make_daemon(config)
        daemon.store.enqueue_outbox(
            "channel_notify", {"text": "digest"}, "test:digest:1",
        )
        ntfy_posts = []

        def routed(req, timeout=None):
            ntfy_posts.append(req.full_url)
            from tests.test_matrix_channel import _FakeHTTPResponse
            return _FakeHTTPResponse({"id": "p1"})

        with patch("urllib.request.urlopen", side_effect=routed):
            delivered = daemon.deliver_outbox()
        self.assertEqual(delivered, 1)
        self.assertEqual(ntfy_posts, ["http://ntfy.test/conch-alerts"])
        row = daemon.store.list_outbox(status="delivered")[0]
        self.assertEqual(row["transport"], "ntfy")


if __name__ == "__main__":
    unittest.main()
