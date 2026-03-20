"""Slack Bot transport for Conch -- runs AgentSession over Slack Socket Mode."""

from __future__ import annotations

import os
import sys
import threading
from typing import Dict

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    _HAS_SLACK = True
except ImportError:
    _HAS_SLACK = False

from .agent import AgentSession


class SlackTransport:
    """Manages per-channel AgentSessions and routes Slack events."""

    def __init__(self):
        if not _HAS_SLACK:
            print("conch: slack-bolt not installed. Run: pip install conch-shell[slack]", file=sys.stderr)
            sys.exit(1)

        bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        app_token = os.environ.get("SLACK_APP_TOKEN", "").strip()
        if not bot_token or not app_token:
            print("conch: set SLACK_BOT_TOKEN and SLACK_APP_TOKEN env vars.", file=sys.stderr)
            sys.exit(1)

        self.app = App(token=bot_token)
        self.app_token = app_token
        self._sessions: Dict[str, AgentSession] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

        self.app.event("app_mention")(self._handle_mention)
        self.app.event("message")(self._handle_dm)
        self.app.command("/conch")(self._handle_slash)

    def _get_session(self, channel_id: str) -> AgentSession:
        with self._global_lock:
            if channel_id not in self._sessions:
                self._sessions[channel_id] = AgentSession(
                    interactive=False, auto_agent=True,
                    log=lambda msg: None,
                )
                self._locks[channel_id] = threading.Lock()
            return self._sessions[channel_id]

    def _get_lock(self, channel_id: str) -> threading.Lock:
        with self._global_lock:
            return self._locks.setdefault(channel_id, threading.Lock())

    def _handle_mention(self, event, say):
        text = event.get("text", "")
        # Strip the bot mention from the text
        parts = text.split(">", 1)
        user_input = parts[1].strip() if len(parts) > 1 else text.strip()
        if not user_input:
            say("Hi! Send me a message and I will help.")
            return
        channel = event.get("channel", "default")
        thread_ts = event.get("thread_ts") or event.get("ts")
        self._process(channel, thread_ts, user_input, say)

    def _handle_dm(self, event, say):
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id"):
            return
        user_input = event.get("text", "").strip()
        if not user_input:
            return
        channel = event.get("channel", "default")
        thread_ts = event.get("thread_ts") or event.get("ts")
        self._process(channel, thread_ts, user_input, say)

    def _handle_slash(self, ack, command, say):
        ack()
        text = command.get("text", "").strip()
        channel = command.get("channel_id", "default")
        if not text:
            say("Usage: `/conch <message>` or `/conch /clear`")
            return
        self._process(channel, None, text, say)

    def _process(self, channel, thread_ts, user_input, say):
        # Use thread_ts as session key if in a thread, else channel
        session_key = thread_ts or channel
        session = self._get_session(session_key)
        lock = self._get_lock(session_key)

        with lock:
            # Handle slash commands
            if user_input.startswith("/"):
                result = session.handle_command(user_input)
                if result == "reload_tools":
                    session.reload_tools()
                    say("Tools reloaded. %d active." % session.tool_count, thread_ts=thread_ts)
                elif result == "clear_conversation":
                    session.clear_history()
                    say("Conversation cleared.", thread_ts=thread_ts)
                elif result == "new_conversation":
                    session.clear_history()
                    say("New conversation started.", thread_ts=thread_ts)
                else:
                    say("Command processed.", thread_ts=thread_ts)
                return

            try:
                reply, usage = session.turn(user_input)
            except Exception as exc:
                say("Error: %s" % str(exc)[:200], thread_ts=thread_ts)
                return

            if reply:
                # Slack has a 4000 char limit per message
                for i in range(0, len(reply), 3900):
                    chunk = reply[i:i + 3900]
                    say(chunk, thread_ts=thread_ts)
            else:
                say("_[no response]_", thread_ts=thread_ts)

    def start(self):
        print("Conch Slack bot starting (%s/%s)..." % (
            self._get_session("_init").provider,
            self._get_session("_init").model_name,
        ))
        handler = SocketModeHandler(self.app, self.app_token)
        try:
            handler.start()
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            for session in self._sessions.values():
                session.close()


def main():
    """Entry point for conch-slack / conch --slack."""
    transport = SlackTransport()
    transport.start()
