"""/rename, /export, /retry, /fork, /compact: the conversation helpers the
shell commands and the conversations page share."""

import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from conch.conversations import (
    ConversationManager,
    TITLE_MAX_CHARS,
    export_conversation,
    export_markdown,
    retry_point,
)
from conch.runtime import auto_compact, can_compact

GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
CONTEXT = "\n\n[context] Current date/time: Tuesday, November 17, 2026 09:00 AM (PST)"
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "iVBORw0KGgo="}, "name": "diagram.png"}


class _StateCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        env = mock.patch.dict("os.environ", {"XDG_STATE_HOME": os.path.join(tmp.name, "state")})
        env.start()
        self.addCleanup(env.stop)
        self.manager = ConversationManager()
        self.addCleanup(self.manager.close)

    def seed(self, messages, title="Deploy notes", usage=None):
        conv = self.manager.create(model="m", provider="p")
        conv.title = title
        conv.messages = messages
        conv.usage = dict(usage or {})
        self.manager.save(conv)
        return conv


def _openai_turns():
    return [
        {"role": "system", "content": "SYSTEM PROMPT TEXT"},
        {"role": "user", "content": "list the files" + CONTEXT},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "local_shell", "arguments": json.dumps({"command": "ls -la"})}}]},
        {"role": "tool", "tool_call_id": "c1", "content": f"token={GITHUB_TOKEN}\nREADME.md"},
        {"role": "assistant", "content": "There is a README."},
        {"role": "user", "content": [{"type": "text", "text": "what is this?" + CONTEXT}, IMAGE]},
        {"role": "assistant", "content": "A diagram."},
    ]


class ExportTests(_StateCase):
    def test_markdown_has_roles_tool_sections_and_images_without_private_parts(self):
        conv = self.seed(_openai_turns(), usage={"input_tokens": 1200, "output_tokens": 30,
                                                  "turns": 2, "cost": 0.01, "api_turns": 2})
        text = export_markdown(conv)
        self.assertTrue(text.startswith("# Deploy notes\n"))
        self.assertIn(conv.id, text)
        self.assertIn("1,200 in", text)
        self.assertEqual(text.count("## You"), 2)
        # Tool call and its output stay inside one assistant section.
        self.assertEqual(text.count("## Assistant"), 2)
        self.assertIn("> → local_shell", text)
        self.assertIn("ls -la", text)
        self.assertIn("<details><summary>tool output</summary>", text)
        self.assertIn("README.md", text)
        self.assertIn("[image: diagram.png]", text)
        self.assertNotIn("iVBORw0KGgo=", text)
        self.assertNotIn("[context]", text)
        self.assertNotIn("SYSTEM PROMPT TEXT", text)
        self.assertNotIn(GITHUB_TOKEN, text)
        self.assertIn("list the files", text)

    def test_credentials_the_user_typed_are_redacted_too(self):
        conv = self.seed([{"role": "user", "content": f"use {GITHUB_TOKEN}" + CONTEXT}])
        self.assertNotIn(GITHUB_TOKEN, export_markdown(conv))

    def test_anthropic_tool_results_stay_with_the_assistant(self):
        conv = self.seed([
            {"role": "user", "content": "go" + CONTEXT},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Running."},
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.txt"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file body"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        ])
        text = export_markdown(conv)
        self.assertEqual(text.count("## You"), 1)
        self.assertEqual(text.count("## Assistant"), 1)
        self.assertIn("> → read_file", text)
        self.assertIn("file body", text)

    def test_huge_tool_output_is_capped_and_fences_survive_backticks(self):
        conv = self.seed([
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "```\n" + "y" * 50000},
        ])
        text = export_markdown(conv)
        self.assertLess(len(text), 30000)
        self.assertIn("more characters not exported", text)
        self.assertIn("````", text)

    def test_default_path_never_overwrites_and_is_private(self):
        conv = self.seed(_openai_turns(), title="Deploy: notes / v2!")
        first = export_conversation(conv, cwd=self.tmp)
        second = export_conversation(conv, cwd=self.tmp)
        third = export_conversation(conv, cwd=self.tmp)
        self.assertEqual(first.parent, second.parent)
        self.assertEqual(first.name, f"deploy-notes-v2-{conv.id}.md")
        self.assertEqual(second.name, f"deploy-notes-v2-{conv.id}-1.md")
        self.assertEqual(third.name, f"deploy-notes-v2-{conv.id}-2.md")
        for path in (first, second, third):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_text(), first.read_text())

    def test_explicit_path_creates_parents_and_refuses_overwrite_without_force(self):
        conv = self.seed(_openai_turns())
        target = os.path.join(self.tmp, "out", "deep", "chat.md")
        written = export_conversation(conv, target)
        self.assertEqual(str(written), target)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        with open(target, "w") as handle:
            handle.write("keep me")
        os.chmod(target, 0o644)
        with self.assertRaises(FileExistsError):
            export_conversation(conv, target)
        with open(target) as handle:
            self.assertEqual(handle.read(), "keep me")
        export_conversation(conv, target, force=True)
        with open(target) as handle:
            self.assertIn("# Deploy notes", handle.read())
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)

    def test_relative_path_resolves_against_cwd(self):
        conv = self.seed(_openai_turns())
        written = export_conversation(conv, "notes/c.md", cwd=self.tmp)
        self.assertEqual(str(written), os.path.join(self.tmp, "notes", "c.md"))
        self.assertTrue(written.exists())


class RetryPointTests(unittest.TestCase):
    def test_last_real_prompt_skips_tool_result_carriers(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [{"type": "text", "text": "second"}, IMAGE]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "r"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]
        index = retry_point(messages)
        self.assertEqual(index, 3)
        kept = messages[:index]
        # What remains ends on a complete exchange: no orphaned tool_use.
        self.assertEqual(kept[-1], {"role": "assistant", "content": "ok"})

    def test_openai_tool_groups_are_dropped_whole(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "r"},
            {"role": "assistant", "content": "done"},
        ]
        index = retry_point(messages)
        self.assertEqual(index, 1)
        self.assertEqual(messages[:index], [{"role": "system", "content": "s"}])

    def test_none_without_a_prompt(self):
        self.assertIsNone(retry_point([]))
        self.assertIsNone(retry_point([{"role": "system", "content": "s"}]))
        self.assertIsNone(retry_point([
            {"role": "system", "content": "s"},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "r"}]},
        ]))


class ForkTests(_StateCase):
    def test_fork_copies_messages_independently_and_leaves_spend_behind(self):
        original = self.seed(_openai_turns(), usage={"input_tokens": 5, "turns": 1})
        before = json.loads(original.path.read_text())
        forked = self.manager.fork(original)
        self.assertNotEqual(forked.id, original.id)
        self.assertEqual(forked.messages, original.messages)
        self.assertEqual(forked.usage, {})
        self.assertEqual(forked.title, "Deploy notes (fork)")
        forked.messages.append({"role": "user", "content": "only in the fork"})
        forked.messages[1]["content"] = "changed"
        self.assertEqual(len(original.messages), len(_openai_turns()))
        self.assertNotEqual(original.messages[1]["content"], "changed")
        self.assertEqual(json.loads(original.path.read_text()), before)
        reloaded = self.manager.load(forked.id)
        self.assertEqual(reloaded.title, "Deploy notes (fork)")
        ids = [entry["id"] for entry in self.manager.list_all()]
        self.assertIn(forked.id, ids)
        self.assertIn(original.id, ids)

    def test_fork_by_id_and_unknown_id(self):
        original = self.seed([{"role": "user", "content": "hi"}], title="x" * TITLE_MAX_CHARS)
        forked = self.manager.fork(original.id)
        self.assertLessEqual(len(forked.title), TITLE_MAX_CHARS)
        self.assertTrue(forked.title.endswith(" (fork)"))
        self.assertIsNone(self.manager.fork("nope0000"))


class RenameTests(_StateCase):
    def test_rename_persists_in_file_and_index(self):
        conv = self.seed([{"role": "user", "content": "hi"}])
        self.manager.rename(conv, "  Release\n   checklist  ")
        self.assertEqual(conv.title, "Release checklist")
        fresh = ConversationManager()
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.load(conv.id).title, "Release checklist")
        entry = next(e for e in fresh.list_all() if e["id"] == conv.id)
        self.assertEqual(entry["title"], "Release checklist")

    def test_titles_are_capped_and_must_not_be_empty(self):
        conv = self.seed([{"role": "user", "content": "hi"}])
        self.manager.rename(conv.id, "t" * 500)
        self.assertEqual(len(self.manager.load(conv.id).title), TITLE_MAX_CHARS)
        with self.assertRaises(ValueError):
            self.manager.rename(conv, "  \n ")
        self.assertIsNone(self.manager.rename("nope0000", "x"))


class ForcedCompactionTests(unittest.TestCase):
    def _history(self, turns):
        messages = [{"role": "system", "content": "s"}]
        for n in range(turns):
            messages += [{"role": "user", "content": f"question {n}"},
                         {"role": "assistant", "content": f"answer {n}"}]
        return messages

    def test_force_compacts_below_the_threshold_with_focus(self):
        messages = self._history(8)
        seen = []

        def raw_fn(config, send, tools):
            seen.append(send)
            return {"content": "- summary"}

        self.assertFalse(auto_compact(list(messages), None, "ollama", {}, raw_fn))
        self.assertEqual(seen, [])
        compacted = list(messages)
        self.assertTrue(auto_compact(compacted, None, "ollama", {}, raw_fn,
                                     force=True, focus="the deploy plan"))
        self.assertIn("the deploy plan", seen[0][0]["content"])
        self.assertLess(len(compacted), len(messages))
        self.assertEqual(compacted[0], messages[0])
        self.assertIn("- summary", json.dumps(compacted))
        self.assertEqual(compacted[-1], messages[-1])

    def test_too_little_history_is_not_compactable(self):
        self.assertFalse(can_compact(self._history(2)))
        self.assertTrue(can_compact(self._history(8)))


class SlashCommandTests(_StateCase):
    def run_command(self, line, conv, messages):
        from conch.commands import handle_slash_command

        with mock.patch("builtins.print"):
            return handle_slash_command(line, {}, "p", "m", lambda *_: None,
                                        conv_mgr=self.manager, current_conv=conv,
                                        messages=messages)

    def test_rename_command_retitles_the_live_conversation(self):
        conv = self.seed([{"role": "user", "content": "hi"}])
        self.run_command("/rename   Shipping   plan ", conv, conv.messages)
        self.assertEqual(conv.title, "Shipping plan")
        self.assertEqual(self.manager.load(conv.id).title, "Shipping plan")
        self.run_command("/rename", conv, conv.messages)
        self.assertEqual(self.manager.load(conv.id).title, "Shipping plan")

    def test_export_command_needs_force_to_replace_a_file(self):
        conv = self.seed(_openai_turns())
        live = conv.messages + [{"role": "user", "content": "unsaved prompt"}]
        target = os.path.join(self.tmp, "x", "chat.md")
        self.run_command(f"/export {target}", conv, live)
        with open(target) as handle:
            self.assertIn("unsaved prompt", handle.read())
        with open(target, "w") as handle:
            handle.write("keep me")
        self.run_command(f"/export {target}", conv, live)
        with open(target) as handle:
            self.assertEqual(handle.read(), "keep me")
        self.run_command(f"/export {target} --force", conv, live)
        with open(target) as handle:
            self.assertIn("# Deploy notes", handle.read())


if __name__ == "__main__":
    unittest.main()
