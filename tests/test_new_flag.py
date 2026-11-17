"""`conch --new` / `-n`: start the shell with a fresh conversation.

Covers invalid flag combinations and conversation selection, including
corruption fall-through: a quarantined most-recent conversation must not
stop startup. Argument parsing is covered in test_cli_args.
"""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conch.app import _startup_conversation, main
from conch.conversations import ConversationManager, _state_dir

FIXTURES = Path(__file__).parent / "fixtures" / "corrupt_conversations"


class TestNewFlagBoundary(unittest.TestCase):
    def test_new_flag_rejects_trailing_prompt(self):
        stderr = io.StringIO()
        with mock.patch("sys.argv", ["conch", "--new", "hello"]), \
                contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--new", stderr.getvalue())



class TestStartupConversation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            "os.environ", {"XDG_STATE_HOME": self._tmp.name}
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _seed_recent(self, mgr):
        conv = mgr.create(model="m", provider="p")
        conv.messages = [
            {"role": "system", "content": "old system"},
            {"role": "user", "content": "existing history"},
        ]
        mgr.save(conv)
        return conv

    def test_default_resumes_most_recent(self):
        mgr = ConversationManager()
        recent = self._seed_recent(mgr)
        conv, messages = _startup_conversation(mgr, "m", "p", "sys prompt")
        self.assertEqual(conv.id, recent.id)
        self.assertEqual(messages[0], {"role": "system", "content": "sys prompt"})
        self.assertEqual(messages[1]["content"], "existing history")
        mgr.close()

    def test_fresh_ignores_most_recent(self):
        mgr = ConversationManager()
        recent = self._seed_recent(mgr)
        conv, messages = _startup_conversation(
            mgr, "m", "p", "sys prompt", fresh=True
        )
        self.assertNotEqual(conv.id, recent.id)
        self.assertEqual(messages, [{"role": "system", "content": "sys prompt"}])
        # The old conversation is untouched and still switchable.
        self.assertIsNotNone(mgr.load(recent.id))
        mgr.close()

    def test_corrupt_most_recent_still_starts(self):
        """The incident path end to end: startup selection survives a
        corrupt most-recent conversation and lands on a usable one."""
        mgr = ConversationManager()
        healthy = self._seed_recent(mgr)
        state = _state_dir()
        shutil.copy(FIXTURES / "truncated.json", state / "bbbb2222.json")
        index_path = state / "index.json"
        index = json.loads(index_path.read_text())
        index["conversations"].insert(0, {
            "id": "bbbb2222", "title": "corrupt", "model": "m",
            "provider": "p", "updated_at": "2099-01-01T00:00:00",
            "message_count": 0,
        })
        index_path.write_text(json.dumps(index))
        mgr2 = ConversationManager()
        with contextlib.redirect_stderr(io.StringIO()):
            conv, messages = _startup_conversation(mgr2, "m", "p", "sys prompt")
        self.assertEqual(conv.id, healthy.id)
        self.assertTrue(messages)
        mgr.close()
        mgr2.close()

    def test_resume_id_starts_in_the_picked_conversation(self):
        """The conversations page hands its pick to startup; it wins over
        the most recent conversation."""
        mgr = ConversationManager()
        picked = self._seed_recent(mgr)
        self._seed_recent(mgr)  # now the most recent
        conv, messages = _startup_conversation(
            mgr, "m", "p", "sys prompt", resume_id=picked.id
        )
        self.assertEqual(conv.id, picked.id)
        self.assertEqual(messages[0], {"role": "system", "content": "sys prompt"})
        self.assertEqual(messages[1]["content"], "existing history")
        mgr.close()


if __name__ == "__main__":
    unittest.main()
