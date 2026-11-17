"""Hot reload: an interactive session re-reads ~/.config/conch/env when a
provider rejects its key and retries exactly once with the new value;
``/reload`` and ``/keys`` refresh the same way; a key that did not change
on disk is not retried (the normal fallback path runs)."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import config as config_mod
from conch.providers import auth_rejected_message, error_response
from conch.runtime import chat_turn, retry_after_key_reload


class HotReloadCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        self.config_dir = home / ".config" / "conch"
        self.config_dir.mkdir(parents=True)
        self.env_path = self.config_dir / "env"
        patcher = patch.dict(os.environ, {
            "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY", None)
        config_mod._ENV_LOAD_STATE["from_file"].clear()
        self.calls = []

    def write_env(self, value: str) -> None:
        self.env_path.write_text(f"ANTHROPIC_API_KEY={value}\n")

    def fake_raw(self, config, messages, tools=None):
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.calls.append(key)
        if key != "fresh-key":
            return error_response(auth_rejected_message("anthropic", "ANTHROPIC_API_KEY", 401, "invalid x-api-key"))
        return {"role": "assistant", "content": "hello from the new key", "tool_calls": None,
                "_usage": {"input_tokens": 1, "output_tokens": 1}}

    def turn(self):
        err = io.StringIO()
        with patch("sys.stderr", err), patch("conch.runtime.time.sleep"), \
                patch("conch.providers.get_fallback_chain", return_value=[]):
            reply, usage = chat_turn(
                config={"provider": "anthropic", "chat_model": "claude-sonnet-5",
                        "api_key_env": "ANTHROPIC_API_KEY"},
                provider="anthropic", raw_fn=self.fake_raw,
                messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
                tools=None, tool_map={}, builtin_clients={}, max_tool_rounds=2,
            )
        return reply, usage, err.getvalue()

    def test_retries_once_after_key_rotated_on_disk(self):
        self.write_env("old-key")
        config_mod.load_env_file()
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "old-key")
        self.write_env("fresh-key")  # `conch keys set` from another terminal
        reply, usage, err = self.turn()
        self.assertEqual(reply, "hello from the new key")
        self.assertEqual(self.calls, ["old-key", "fresh-key"])
        self.assertIn("ANTHROPIC_API_KEY changed in the conch env file — retrying with the new key", err)
        self.assertNotIn("fresh-key", err)

    def test_no_retry_when_nothing_changed(self):
        self.write_env("old-key")
        config_mod.load_env_file()
        reply, usage, err = self.turn()
        self.assertEqual(reply, "")
        self.assertEqual(self.calls, ["old-key"])
        self.assertIn("ANTHROPIC_API_KEY was rejected by anthropic (401", usage.get("error", ""))
        self.assertIn("/keys set <NAME>", err)

    def test_helper_only_fires_on_auth_errors(self):
        self.write_env("old-key")
        config_mod.load_env_file()
        self.write_env("fresh-key")
        self.assertFalse(retry_after_key_reload("anthropic", {"api_key_env": "ANTHROPIC_API_KEY"}, "HTTP 500"))
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "old-key")
        self.assertTrue(retry_after_key_reload(
            "anthropic", {"api_key_env": "ANTHROPIC_API_KEY"},
            auth_rejected_message("anthropic", "ANTHROPIC_API_KEY", 401),
        ))
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "fresh-key")

    def test_daemon_session_factory_sees_reloaded_key(self):
        """The daemon re-reads the file every tick; its per-session model
        check then probes with the new value (no restart)."""
        from conch.kernel.daemon import EdgeDaemon

        self.write_env("old-key")
        config_mod.load_env_file()
        daemon = EdgeDaemon({"provider": "anthropic", "edge_daemon": "true"},
                            session_factory=lambda *a: ("", {}))
        daemon.start()
        self.addCleanup(daemon.shutdown)
        self.write_env("fresh-key")
        os.utime(self.env_path, (1, 1))
        daemon.tick()
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "fresh-key")
        self.assertIn("credentials reloaded (env file changed): ANTHROPIC_API_KEY", daemon.log_path.read_text())
        self.assertNotIn("fresh-key", daemon.log_path.read_text())


if __name__ == "__main__":
    unittest.main()
