"""Credential gate on the interactive memory store (secret discipline).

The mission-lesson consolidation gate already keeps credentials out of
*mission lessons*; these tests prove the same discipline on every other
MemoryStore path:

- WRITE: MemoryStore.add rejects credential-bearing content whole (never
  sanitizes) for every interactive write path — the save_memory tool, the
  /remember command, and auto session summaries — with a clear message on
  the tool/command surfaces.
- READ: retrieval (build_context, rank_entries, and the memories section
  of search_conversations) re-scans on the way out, so a legacy entry that
  predates the gate — or slips patterns added later — is dropped and
  logged by TYPE, never surfaced.

Every planted credential here is a synthetic fixture (marked Synthetic /
SYNTHETIC) and allowlisted in .gitleaks.toml in the same commit that
introduced it, per the repo's secret discipline.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from conch.memory import MemoryStore, _memory_path
from conch.secretguard import (
    CredentialRejected,
    credential_findings,
    redact_credentials,
)


# Synthetic fixtures only — allowlisted in .gitleaks.toml.
JWT_FIXTURE = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzeW50aGV0aWMiOiJmaXh0dXJlIn0."
    "U1lOVEhFVElDU0lHTkFUVVJF"
)
ATLASSIAN_FIXTURE = "ATATT3xFfGF0SyntheticFixture00000000001111"
VERCEL_FIXTURE = (
    "Vercel Token 1 (personal account):\n"
    "Qq7Rt2LmVx9KpB4nWc8ZsD3fGh1JkY5aSynthFix"
)
PASSWORD_FIXTURE = "the sudo password: Fak3synthetic$ — never ask"
ENTROPY_FIXTURE = (
    "deploy token for staging: kD8fmQ2xLp0vRahZ7TgWc4Ns1Ey6Jb3USynthFix"
)


class GuardCase(unittest.TestCase):
    """Isolated XDG state/config so the user's real store is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def plant_legacy_entry(self, content, source="auto"):
        """Write an entry straight into memory.json, bypassing the write
        gate — exactly what a pre-gate legacy store looks like."""
        path = _memory_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            entries = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            entries = []
        entries.append({
            "id": max((int(e["id"]) for e in entries), default=0) + 1,
            "content": content,
            "created_at": "2026-05-07 17:56:10",
            "source": source,
        })
        path.write_text(json.dumps(entries))


class TestDetector(unittest.TestCase):
    def test_credential_shapes_detected_with_type_labels(self):
        self.assertIn("jwt", credential_findings(f"auth: {JWT_FIXTURE}"))
        self.assertIn(
            "atlassian_api_token",
            credential_findings(f"API Token: {ATLASSIAN_FIXTURE}"),
        )
        self.assertIn("vercel_token", credential_findings(VERCEL_FIXTURE))
        self.assertIn(
            "secret_assignment", credential_findings(PASSWORD_FIXTURE)
        )
        self.assertIn(
            "high_entropy_near_auth_word",
            credential_findings(ENTROPY_FIXTURE),
        )

    def test_credential_mentions_without_values_stay_clean(self):
        for text in (
            "stopped at credential setup since none were configured",
            "the Jira token lives in ~/.cursor/mcp.json under env",
            "api key: none configured yet",
            "password: stored in the keychain, ask before sudo",
            "I'd add `agent_mode = on` to the config so it persists",
            "[API error: model `zai-glm-4.7` does not exist —"
            " platform.openai.com/docs — request.id.abcdef]",
            "prefers rsync over scp for large trees",
        ):
            self.assertEqual(credential_findings(text), [], text)

    def test_redaction_reaches_a_clean_fixpoint(self):
        marker = "[credential removed 2026-09-12]"
        text = f"Jira URL: https://x.atlassian.net\nAPI Token: {ATLASSIAN_FIXTURE}"
        redacted, labels = redact_credentials(text, marker)
        self.assertIn("atlassian_api_token", labels)
        self.assertNotIn(ATLASSIAN_FIXTURE, redacted)
        self.assertIn("Jira URL: https://x.atlassian.net", redacted)
        self.assertEqual(credential_findings(redacted), [])


class TestWriteGate(GuardCase):
    def test_add_rejects_each_credential_type_whole(self):
        memory = MemoryStore()
        for fixture in (
            f"remember this auth header: {JWT_FIXTURE}",
            f"Jira API Token: {ATLASSIAN_FIXTURE}",
            VERCEL_FIXTURE,
            PASSWORD_FIXTURE,
            ENTROPY_FIXTURE,
        ):
            with self.assertRaises(CredentialRejected):
                memory.add(fixture)
        self.assertEqual(MemoryStore().get_all(), [])  # nothing landed

    def test_rejection_carries_types_never_values(self):
        try:
            MemoryStore().add(f"token: {ATLASSIAN_FIXTURE}")
        except CredentialRejected as exc:
            self.assertIn("atlassian_api_token", exc.types)
            self.assertNotIn(ATLASSIAN_FIXTURE, str(exc))
        else:
            self.fail("expected CredentialRejected")

    def test_clean_content_still_saves(self):
        memory = MemoryStore()
        entry = memory.add("prefers rsync over scp for large trees")
        self.assertEqual(int(entry["id"]), 1)
        self.assertEqual(len(MemoryStore().get_all()), 1)

    def test_save_memory_tool_blocks_with_clear_message(self):
        from conch.tooling import SaveMemoryClient

        client = SaveMemoryClient()
        client.bind(MemoryStore())
        result = client.call_tool(
            "save_memory", {"content": f"sudo password: {ATLASSIAN_FIXTURE}"}
        )
        text = result["content"][0]["text"]
        self.assertIn("Save blocked", text)
        self.assertIn("atlassian_api_token", text)
        self.assertNotIn(ATLASSIAN_FIXTURE, text)  # value never echoed
        self.assertEqual(MemoryStore().get_all(), [])

    def test_save_memory_tool_still_saves_clean_content(self):
        from conch.tooling import SaveMemoryClient

        client = SaveMemoryClient()
        client.bind(MemoryStore())
        result = client.call_tool(
            "save_memory", {"content": "user prefers vim keybindings"}
        )
        self.assertIn("Saved memory #1", result["content"][0]["text"])

    def test_remember_command_blocks_and_explains(self):
        from conch.commands import handle_slash_command

        memory = MemoryStore()
        out = io.StringIO()
        with redirect_stdout(out):
            handle_slash_command(
                f"/remember deploy key: {ATLASSIAN_FIXTURE}",
                {"provider": "ollama"}, "ollama", "qwen3", lambda v: None,
                memory=memory,
            )
        printed = out.getvalue()
        self.assertIn("Not saved", printed)
        self.assertIn("atlassian_api_token", printed)
        self.assertNotIn(ATLASSIAN_FIXTURE, printed)
        self.assertEqual(MemoryStore().get_all(), [])

    def test_session_summary_with_credentials_is_dropped_whole(self):
        from conch.app import _summarize_and_save

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "set up my jira access"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "thanks"},
        ]

        def raw_fn(config, msgs, tools):
            return {"content": f"Saved the Jira token: {ATLASSIAN_FIXTURE}"}

        memory = MemoryStore()
        _summarize_and_save(messages, {"provider": "openai"}, raw_fn, memory)
        self.assertEqual(MemoryStore().get_all(), [])

    def test_clean_session_summary_still_saves(self):
        from conch.app import _summarize_and_save

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "sort my downloads"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "thanks"},
        ]

        def raw_fn(config, msgs, tools):
            return {"content": "Sorted downloads folder by date."}

        memory = MemoryStore()
        _summarize_and_save(messages, {"provider": "openai"}, raw_fn, memory)
        entries = MemoryStore().get_all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], "summary")


class TestReadGuard(GuardCase):
    def test_build_context_drops_and_logs_legacy_credential_entries(self):
        self.plant_legacy_entry(f"vercel deploy token: {ENTROPY_FIXTURE}")
        self.plant_legacy_entry(
            "deploy notes: use rsync for the static assets", source="user"
        )
        memory = MemoryStore()
        with self.assertLogs("conch.memory", level="WARNING") as logs:
            context = memory.build_context("deploy token vercel rsync")
        self.assertIn("rsync", context)
        self.assertNotIn("SynthFix", context)
        joined = "\n".join(logs.output)
        self.assertIn("credential", joined)
        self.assertNotIn("SynthFix", joined)  # types logged, never values

    def test_rank_entries_never_returns_credentialed_entries(self):
        self.plant_legacy_entry(
            f"[jira setup] API Token: {ATLASSIAN_FIXTURE}",
            source="mission:msn-a",
        )
        self.plant_legacy_entry(
            "[jira setup] use the REST search endpoint for JQL",
            source="mission:msn-b",
        )
        memory = MemoryStore()
        with self.assertLogs("conch.memory", level="WARNING"):
            ranked = memory.rank_entries(
                "jira setup API token", source_prefix="mission:"
            )
        contents = [entry["content"] for entry in ranked]
        self.assertTrue(
            any("REST search" in content for content in contents)
        )
        self.assertFalse(
            any(ATLASSIAN_FIXTURE in content for content in contents)
        )

    def test_search_conversations_memories_section_is_guarded(self):
        from conch.conversations import ConversationManager
        from conch.tooling import SearchConversationsClient

        self.plant_legacy_entry(f"jira credentials: {ATLASSIAN_FIXTURE}")
        self.plant_legacy_entry("jira boards live under /jira/boards")
        conv_mgr = ConversationManager()
        self.addCleanup(conv_mgr.close)
        client = SearchConversationsClient()
        client.bind(conv_mgr, MemoryStore())
        result = client.call_tool("search_conversations", {"query": "jira"})
        text = result["content"][0]["text"]
        self.assertIn("/jira/boards", text)
        self.assertNotIn(ATLASSIAN_FIXTURE, text)

    def test_clean_store_retrieval_logs_nothing(self):
        MemoryStore().add("likes tabular diffs in reviews")
        memory = MemoryStore()
        with patch("conch.memory.logger.warning") as warn:
            context = memory.build_context("tabular diffs")
        self.assertIn("tabular", context)
        warn.assert_not_called()


class TestStoreFilePermissions(GuardCase):
    def test_memory_file_not_group_or_world_readable(self):
        MemoryStore().add("owner-only store")
        mode = os.stat(_memory_path()).st_mode & 0o777
        self.assertEqual(mode & 0o077, 0, f"memory.json mode {oct(mode)}")


if __name__ == "__main__":
    unittest.main()
