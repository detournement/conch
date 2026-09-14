"""Every conversation-state write must be crash-safe.

The 2db46ee5 incident: a power outage mid-save left a conversation file
with a valid JSON prefix and stale trailing bytes, and the shell crashed
at startup with ``json.decoder.JSONDecodeError: Extra data``. These tests
pin the fix: writes go through a unique tmp file that is fsynced and then
``os.replace``d, so an interruption at any point leaves the previous
complete file untouched.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conch.conversations import ConversationManager, _atomic_write_text


class TestAtomicWriteText(unittest.TestCase):
    def test_crash_before_replace_leaves_old_file_intact(self):
        """Simulated crash between the tmp write and the rename: the target
        keeps its previous complete content and the tmp file is cleaned up."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "conv.json"
            target.write_text('{"old": true}')
            with mock.patch(
                "conch.conversations.os.replace",
                side_effect=OSError("simulated power cut"),
            ):
                with self.assertRaises(OSError):
                    _atomic_write_text(target, '{"new": true}')
            self.assertEqual(target.read_text(), '{"old": true}')
            leftovers = [p for p in Path(tmp).iterdir() if p != target]
            self.assertEqual(leftovers, [], "tmp file must not litter the dir")

    def test_data_is_fsynced_before_replace(self):
        calls = []
        real_fsync = os.fsync
        real_replace = os.replace

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "conv.json"

            def spy_fsync(fd):
                calls.append("fsync")
                return real_fsync(fd)

            def spy_replace(src, dst):
                calls.append("replace")
                return real_replace(src, dst)

            with mock.patch("conch.conversations.os.fsync", spy_fsync), \
                    mock.patch("conch.conversations.os.replace", spy_replace):
                _atomic_write_text(target, '{"a": 1}')
            self.assertEqual(calls, ["fsync", "replace"],
                             "content must hit disk before the rename publishes it")
            self.assertEqual(json.loads(target.read_text()), {"a": 1})

    def test_existing_file_mode_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "conv.json"
            target.write_text("{}")
            target.chmod(0o600)
            _atomic_write_text(target, '{"a": 1}')
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_tmp_file_lives_in_the_target_directory(self):
        """The rename is only atomic within one filesystem, so the tmp file
        must be created next to the target, never in /tmp."""
        seen = []
        real_replace = os.replace

        def spy_replace(src, dst):
            seen.append(Path(src).parent)
            return real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "conv.json"
            with mock.patch("conch.conversations.os.replace", spy_replace):
                _atomic_write_text(target, "{}")
        self.assertEqual(seen, [Path(tmp)])


class TestManagerWritesAreAtomic(unittest.TestCase):
    def test_conversation_save_survives_simulated_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = ConversationManager()
                conv = mgr.create(model="m", provider="p")
                conv.messages = [{"role": "user", "content": "before"}]
                mgr.save(conv)
                on_disk = conv.path.read_text()

                conv.messages = [{"role": "user", "content": "after"}]
                with mock.patch(
                    "conch.conversations.os.replace",
                    side_effect=OSError("simulated power cut"),
                ):
                    with self.assertRaises(OSError):
                        conv.save()
                self.assertEqual(conv.path.read_text(), on_disk)
                self.assertEqual(
                    json.loads(conv.path.read_text())["messages"][0]["content"],
                    "before",
                )
                mgr.close()

    def test_index_save_survives_simulated_crash(self):
        from conch.conversations import _index_path

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = ConversationManager()
                mgr.create(model="m", provider="p")
                on_disk = _index_path().read_text()

                mgr2 = ConversationManager()
                with mock.patch(
                    "conch.conversations.os.replace",
                    side_effect=OSError("simulated power cut"),
                ):
                    with self.assertRaises(OSError):
                        mgr2.create(model="m", provider="p")
                self.assertEqual(_index_path().read_text(), on_disk)
                mgr.close()
                mgr2.close()


if __name__ == "__main__":
    unittest.main()
