"""File references survive /new without carrying old chat or depending on an LLM."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from conch.conversations import ConversationManager
from conch.filetools import FileToolsClient
from conch.tooling import LocalShellPolicy, PermissionState


class ArtifactHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.manager = ConversationManager()
        self.addCleanup(self.manager.close)

    def call(self, name, arguments, anthropic=False):
        if anthropic:
            return {"role": "assistant", "content": [
                {"type": "tool_use", "id": "file", "name": name, "input": arguments}]}
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "file", "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}}]}

    def test_lorenz_file_can_be_read_after_new_and_reload_without_old_history(self):
        client = FileToolsClient(permissions=PermissionState())
        client.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        path = self.root / "scary-math.html"
        content = "<script>const sigma=10, rho=28, beta=8/3;\n" \
                  "function step(p){return [sigma*(p.y-p.x),p.x*(rho-p.z)-p.y,p.x*p.y-beta*p.z]}\n</script>"
        with contextlib.redirect_stdout(io.StringIO()):
            result = client.call_tool("write_file", {"path": str(path), "content": content})
        self.assertNotIn("isError", result)
        for anthropic in (False, True):
            with self.subTest(anthropic=anthropic):
                old = self.manager.create("m", "p")
                old.messages = [
                    {"role": "user", "content": "make a scary math visualization"},
                    self.call("write_file", {"path": str(path), "content": content}, anthropic),
                    {"role": "assistant", "content": "Old instructions must not carry over."},
                ]
                self.manager.save(old)
                new = self.manager.create("m", "p", previous=old)
                loaded = self.manager.load(new.id)
                self.assertEqual(loaded.messages, [])
                references = json.loads(loaded.previous_context)
                self.assertEqual(references["conversation_id"], old.id)
                self.assertEqual(references["file_references"], [str(path)])
                self.assertNotIn("Old instructions", loaded.previous_context)
                self.assertNotIn("function step", loaded.previous_context)
                read = client.call_tool("read_file", {"path": references["file_references"][0]})
                self.assertIn("sigma=10, rho=28, beta=8/3", read["content"][0]["text"])
                self.assertIn("p.x*(rho-p.z)-p.y", read["content"][0]["text"])
                self.assertEqual(self.manager.load(old.id).messages, old.messages)
                self.assertEqual(self.manager.fork(loaded).previous_context, loaded.previous_context)

    def test_only_recent_distinct_bounded_file_paths_are_carried(self):
        old = self.manager.create("m", "p")
        old.messages = [self.call("write_file", {"path": f"file-{i}.txt", "content": "x"})
                        for i in range(8)]
        old.messages += [
            self.call("read_file", {"path": "file-4.txt"}),
            self.call("write_file", {"path": "x" * 513}),
            self.call("local_shell", {"path": "not-a-file-tool"}),
            {"role": "assistant", "tool_calls": [{"function": {
                "name": "write_file", "arguments": "not JSON"}}]},
        ]
        new = self.manager.create("m", "p", previous=old)
        self.assertEqual(json.loads(new.previous_context)["file_references"],
                         ["file-4.txt", "file-7.txt", "file-6.txt", "file-5.txt", "file-3.txt"])
        self.assertEqual(self.manager.create("m", "p", previous=new).previous_context, "")
        self.assertEqual(self.manager.create("m", "p").previous_context, "")

    def test_credentials_are_not_carried_in_paths_or_titles(self):
        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        old = self.manager.create("m", "p")
        old.title = "token " + token
        old.messages = [self.call("read_file", {"path": "safe.html"}),
                        self.call("write_file", {"path": token + ".txt", "content": "secret"})]
        new = self.manager.create("m", "p", previous=old)
        self.assertEqual(json.loads(new.previous_context)["file_references"], ["safe.html"])
        self.assertNotIn(token, new.previous_context)
        self.assertNotIn(token, new.path.read_text())


if __name__ == "__main__":
    unittest.main()
