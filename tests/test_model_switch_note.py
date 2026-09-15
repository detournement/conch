"""Mid-conversation model-switch notes.

A committed switch — the /model and /provider slash commands or the agent's
conch_config set_model action — appends one compact system-role note at the
switch boundary, so the newly selected model stops trusting stale pre-switch
claims about which model is active (field failure: after a switch the new
model believed an earlier turn that said only the previous model existed and
misdescribed itself). Also covers the switch-message labels that lead with
the registry identity, wire safety of mid-history system notes (anthropic
folds system messages into its system param), and save/load persistence.
"""

import io
import os
import tempfile
import unittest
from unittest.mock import patch

from conch.commands import handle_slash_command
from conch.providers import clear_local_model_caches
from conch.runtime import (
    append_model_switch_note,
    normalize_messages_for_provider,
)
from tests.test_llamaidx import FakeLlamaCppBox, FakeOllamaBox, FakeRegistry


def _quiet():
    return patch("sys.stdout", new_callable=io.StringIO)


def _notes(messages):
    return [
        m for m in messages
        if m.get("role") == "system"
        and str(m.get("content", "")).startswith("Model switched:")
    ]


def _history():
    """A conversation with pre-switch turns, including a stale claim about
    the active model — exactly the shape the note exists to fence off."""
    return [
        {"role": "system", "content": "You are conch."},
        {"role": "user", "content": "which model is this?"},
        {
            "role": "assistant",
            "content": "The only available model here is claude-sonnet-5.",
        },
    ]


class SwitchNoteHelperTests(unittest.TestCase):
    def test_nothing_appended_at_conversation_start(self):
        # Initial model selection isn't a switch: no note without prior
        # non-system turns.
        for messages in (None, [], [{"role": "system", "content": "s"}]):
            note = append_model_switch_note(
                messages, provider="openai", model="gpt-5.5", config={}
            )
            self.assertIsNone(note)
            if messages:
                self.assertEqual(_notes(messages), [])

    def test_cloud_note_names_provider_without_base_url(self):
        messages = _history()
        note = append_model_switch_note(
            messages, provider="anthropic", model="claude-sonnet-5", config={}
        )
        self.assertEqual(
            note,
            "Model switched: this conversation is now served by "
            "claude-sonnet-5 (anthropic). Prior turns may reference a "
            "different active model; statements about the previously active "
            "model no longer describe the current one.",
        )
        self.assertEqual(messages[-1], {"role": "system", "content": note})

    def test_local_note_carries_base_url(self):
        note = append_model_switch_note(
            _history(),
            provider="ollama",
            model="qwen3:8b",
            config={"ollama_base_url": "http://192.0.2.7:11434"},
        )
        self.assertIn("qwen3:8b (ollama) at http://192.0.2.7:11434.", note)

    def test_registry_note_carries_registry_identity_and_adapter(self):
        note = append_model_switch_note(
            _history(),
            provider="custom",
            model="qwen3-14b",
            config={"custom_base_url": "http://192.0.2.50:8080/v1"},
            registry_name="llamaidx/burt/qwen3-14b",
        )
        self.assertIn(
            "now served by qwen3-14b (llamaidx/burt/qwen3-14b, custom "
            "adapter) at http://192.0.2.50:8080/v1.",
            note,
        )

    def test_reselecting_the_same_model_notes_once(self):
        messages = _history()
        first = append_model_switch_note(
            messages, provider="openai", model="gpt-5.5", config={}
        )
        second = append_model_switch_note(
            messages, provider="openai", model="gpt-5.5", config={}
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(_notes(messages)), 1)

    def test_each_distinct_switch_notes_once(self):
        messages = _history()
        append_model_switch_note(
            messages, provider="openai", model="gpt-5.5", config={}
        )
        messages.append({"role": "user", "content": "and now?"})
        messages.append({"role": "assistant", "content": "gpt-5.5 here."})
        append_model_switch_note(
            messages, provider="anthropic", model="claude-sonnet-5", config={}
        )
        self.assertEqual(len(_notes(messages)), 2)


class SlashSwitchNoteTests(unittest.TestCase):
    """The /model and /provider paths append the note when they commit."""

    def setUp(self):
        clear_local_model_caches()
        self.registry = FakeRegistry().start()
        self.addCleanup(self.registry.stop)

    def _config(self):
        return {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "chat_model": "claude-sonnet-5",
            "llamaidx_url": self.registry.base_url,
        }

    def _switch(self, command, config, messages):
        with _quiet() as out:
            result = handle_slash_command(
                command, config, "anthropic", "claude-sonnet-5",
                lambda v: None, messages=messages,
            )
        return result, out.getvalue()

    def _llamacpp_burt(self):
        box = FakeLlamaCppBox(models=["qwen3-14b"]).start()
        self.addCleanup(box.stop)
        self.registry.providers = [
            self.registry.provider_entry(
                name="burt", flavor="llamacpp", base_url=box.base_url,
                models=[self.registry.model_entry("qwen3-14b")],
            )
        ]
        return box

    def test_llamaidx_switch_appends_exactly_one_note(self):
        box = self._llamacpp_burt()
        messages = _history()
        result, _ = self._switch(
            "/model llamaidx/burt/qwen3-14b", self._config(), messages
        )
        self.assertIsNotNone(result)
        notes = _notes(messages)
        self.assertEqual(len(notes), 1)
        self.assertIs(messages[-1], notes[0])
        note = notes[0]["content"]
        self.assertIn("now served by qwen3-14b", note)
        self.assertIn("(llamaidx/burt/qwen3-14b, custom adapter)", note)
        self.assertIn(f"at {box.base_url}/v1", note)
        self.assertIn("Prior turns may reference a different active model", note)
        self.assertIn("no longer describe the current one", note)

    def test_llamaidx_switch_message_leads_with_registry_identity(self):
        box = self._llamacpp_burt()
        _, out = self._switch(
            "/model llamaidx/burt/qwen3-14b", self._config(), _history()
        )
        self.assertIn("Switched to llamaidx/burt/qwen3-14b", out)
        self.assertIn(f"(custom adapter, {box.base_url})", out)
        self.assertNotIn("Switched to custom/qwen3-14b", out)
        self.assertNotIn("(via ", out)

    def test_ollama_flavor_switch_labels_ollama_adapter(self):
        box = FakeOllamaBox(models=["qwen3:8b"]).start()
        self.addCleanup(box.stop)
        self.registry.providers = [
            self.registry.provider_entry(
                name="minibox", flavor="ollama", base_url=box.base_url,
                models=[self.registry.model_entry("qwen3:8b")],
            )
        ]
        messages = _history()
        _, out = self._switch(
            "/model llamaidx/minibox/qwen3:8b", self._config(), messages
        )
        self.assertIn("Switched to llamaidx/minibox/qwen3:8b", out)
        self.assertIn(f"(ollama adapter, {box.base_url})", out)
        [note] = _notes(messages)
        self.assertIn(
            f"(llamaidx/minibox/qwen3:8b, ollama adapter) at {box.base_url}.",
            note["content"],
        )

    def test_no_note_on_initial_selection(self):
        self._llamacpp_burt()
        messages = [{"role": "system", "content": "You are conch."}]
        result, _ = self._switch(
            "/model llamaidx/burt/qwen3-14b", self._config(), messages
        )
        self.assertIsNotNone(result)  # the switch itself commits
        self.assertEqual(_notes(messages), [])

    def test_ordinary_model_switch_appends_note(self):
        messages = _history()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
            result, _ = self._switch("/model gpt-5.5", self._config(), messages)
        self.assertEqual(result[:2], ("openai", "gpt-5.5"))
        [note] = _notes(messages)
        self.assertIn("now served by gpt-5.5 (openai).", note["content"])

    def test_provider_switch_appends_note(self):
        messages = _history()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
            result, _ = self._switch("/provider openai", self._config(), messages)
        self.assertEqual(result[:2], ("openai", "gpt-4o-mini"))
        [note] = _notes(messages)
        self.assertIn("now served by gpt-4o-mini (openai).", note["content"])

    def test_refused_switch_appends_nothing(self):
        box = FakeLlamaCppBox(models=["qwen3-14b"], tool_capable=False).start()
        self.addCleanup(box.stop)
        self.registry.providers = [
            self.registry.provider_entry(
                name="burt", flavor="llamacpp", base_url=box.base_url,
                models=[self.registry.model_entry("qwen3-14b")],  # stale yes
            )
        ]
        messages = _history()
        result, _ = self._switch(
            "/model llamaidx/burt/qwen3-14b", self._config(), messages
        )
        self.assertIsNone(result)
        self.assertEqual(_notes(messages), [])


class ToolPathSwitchNoteTests(unittest.TestCase):
    """The conch_config set_model path: the queued action carries the
    registry identity, and applying it (as the app loop does between turns)
    records exactly one note."""

    def setUp(self):
        clear_local_model_caches()
        self.registry = FakeRegistry().start()
        self.addCleanup(self.registry.stop)

    def _client(self, config):
        from conch.tooling import ConchConfigClient

        client = ConchConfigClient()
        client.bind("anthropic", "claude-sonnet-5", {}, config)
        return client

    def _apply(self, action, config, messages):
        """Replicate the app loop's set_model application: commit the
        provider/model and adapter overrides to config, then record the
        switch note (conch.app processes pending_actions the same way)."""
        _, prov, mod = action[:3]
        overrides = action[3] if len(action) > 3 else None
        registry_name = action[4] if len(action) > 4 else ""
        config["provider"] = prov
        config["chat_model"] = mod
        config["model"] = mod
        if overrides:
            config.update(overrides)
        return append_model_switch_note(
            messages, provider=prov, model=mod, config=config,
            registry_name=registry_name,
        )

    def test_llamaidx_action_carries_registry_name_and_new_label(self):
        box = FakeLlamaCppBox(models=["qwen3-14b"]).start()
        self.addCleanup(box.stop)
        self.registry.providers = [
            self.registry.provider_entry(
                name="burt", flavor="llamacpp", base_url=box.base_url,
                models=[self.registry.model_entry("qwen3-14b")],
            )
        ]
        config = {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "chat_model": "claude-sonnet-5",
            "llamaidx_url": self.registry.base_url,
        }
        client = self._client(config)
        text = client.call_tool(
            "conch_config",
            {"action": "set_model", "value": "llamaidx/burt/qwen3-14b"},
        )["content"][0]["text"]
        self.assertIn(
            f"Model switch to llamaidx/burt/qwen3-14b (custom adapter, "
            f"{box.base_url}; free, self-hosted) is queued.",
            text,
        )
        self.assertIn("NEXT user message", text)
        [action] = client.pending_actions
        self.assertEqual(action[4], "llamaidx/burt/qwen3-14b")

        # Applying the queued action lands exactly one note at the boundary.
        messages = _history()
        note = self._apply(action, config, messages)
        self.assertEqual(len(_notes(messages)), 1)
        self.assertIn("now served by qwen3-14b", note)
        self.assertIn("(llamaidx/burt/qwen3-14b, custom adapter)", note)
        self.assertIn(f"at {box.base_url}/v1", note)

    def test_plain_cloud_action_notes_provider_only(self):
        config = {"provider": "anthropic", "model": "claude-sonnet-5",
                  "chat_model": "claude-sonnet-5"}
        client = self._client(config)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
            client.call_tool(
                "conch_config", {"action": "set_model", "value": "gpt-5.5"}
            )
        [action] = client.pending_actions
        self.assertEqual(action, ("set_model", "openai", "gpt-5.5"))
        messages = _history()
        note = self._apply(action, config, messages)
        self.assertEqual(len(_notes(messages)), 1)
        self.assertIn("now served by gpt-5.5 (openai).", note)

    def test_apply_on_fresh_conversation_appends_nothing(self):
        messages = [{"role": "system", "content": "You are conch."}]
        note = self._apply(
            ("set_model", "openai", "gpt-5.5"), {"provider": "anthropic"},
            messages,
        )
        self.assertIsNone(note)
        self.assertEqual(_notes(messages), [])


class MidHistorySystemNoteWireTests(unittest.TestCase):
    """The persisted note must reach every provider safely: anthropic folds
    system-role messages into its top-level system param (last one wins), so
    a mid-history note kept as system-role would REPLACE the real system
    prompt there."""

    def _switched_history(self):
        messages = _history()
        append_model_switch_note(
            messages,
            provider="custom",
            model="qwen3-14b",
            config={"custom_base_url": "http://192.0.2.50:8080/v1"},
            registry_name="llamaidx/burt/qwen3-14b",
        )
        messages.append({"role": "user", "content": "so which model are you?"})
        return messages

    def test_anthropic_keeps_leading_system_and_converts_the_note(self):
        out = normalize_messages_for_provider(self._switched_history(), "anthropic")
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[0]["content"], "You are conch.")
        self.assertEqual(
            [m["role"] for m in out[1:]].count("system"), 0,
            "mid-history system notes must not reach anthropic as system-role",
        )
        converted = [
            m for m in out
            if m["role"] == "user" and "Model switched:" in m["content"]
        ]
        self.assertEqual(len(converted), 1)
        self.assertTrue(
            converted[0]["content"].startswith("[conversation context]\n")
        )

    def test_local_providers_keep_single_leading_system(self):
        for provider in ("ollama", "custom"):
            out = normalize_messages_for_provider(
                self._switched_history(), provider
            )
            self.assertEqual(out[0]["role"], "system")
            self.assertEqual(out[0]["content"], "You are conch.")
            self.assertEqual([m["role"] for m in out[1:]].count("system"), 0)
            converted = [
                m for m in out
                if m["role"] == "user" and "Model switched:" in m["content"]
            ]
            self.assertEqual(len(converted), 1, provider)

    def test_openai_compatible_keeps_the_system_role_note(self):
        out = normalize_messages_for_provider(self._switched_history(), "openai")
        mid_system = [
            m for m in out[1:]
            if m["role"] == "system" and "Model switched:" in m["content"]
        ]
        self.assertEqual(len(mid_system), 1)


class NotePersistenceTests(unittest.TestCase):
    def test_note_survives_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_STATE_HOME": tmp}
        ):
            from conch.conversations import ConversationManager

            mgr = ConversationManager()
            try:
                conv = mgr.create(model="claude-sonnet-5", provider="anthropic")
                conv.messages = _history()
                append_model_switch_note(
                    conv.messages, provider="openai", model="gpt-5.5", config={}
                )
                mgr.save(conv)
                loaded = mgr.load(conv.id)
            finally:
                mgr.close()
            [note] = _notes(loaded.messages)
            self.assertIn("now served by gpt-5.5 (openai).", note["content"])


if __name__ == "__main__":
    unittest.main()
