"""Desktop notification for long turns: when it fires, and that reply text
can never become AppleScript."""

import unittest
from unittest import mock

from conch import notify


class TurnFinishedTests(unittest.TestCase):
    def run_turn(self, config, elapsed, **kwargs):
        with mock.patch.object(notify, "send", return_value=True) as send:
            sent = notify.turn_finished(config, elapsed, reply=kwargs.get("reply", "Done."),
                                        failed=kwargs.get("failed", False),
                                        conversation="deploy notes")
        return sent, send

    def test_short_turns_stay_quiet_and_long_ones_notify(self):
        sent, send = self.run_turn({}, notify.DEFAULT_AFTER_SECONDS - 1)
        self.assertFalse(sent)
        send.assert_not_called()
        sent, send = self.run_turn({}, notify.DEFAULT_AFTER_SECONDS)
        self.assertTrue(sent)
        title, message = send.call_args[0]
        self.assertIn("deploy notes", title)
        self.assertEqual(message, "Done.")

    def test_threshold_is_configurable_and_zero_disables(self):
        self.assertTrue(self.run_turn({"notify_after_seconds": "5"}, 6)[0])
        for off in ("0", "off", "false"):
            sent, send = self.run_turn({"notify_after_seconds": off}, 3600)
            self.assertFalse(sent, off)
            send.assert_not_called()

    def test_unparseable_threshold_keeps_the_default(self):
        self.assertEqual(notify.notify_after_seconds({"notify_after_seconds": "soon"}),
                         notify.DEFAULT_AFTER_SECONDS)

    def test_failed_turn_says_so_instead_of_quoting_a_reply(self):
        _, send = self.run_turn({}, 60, reply="", failed=True)
        self.assertIn("failed", send.call_args[0][1])


class CommandTests(unittest.TestCase):
    def test_macos_passes_text_as_argv_not_script(self):
        hostile = 'x" & do shell script "rm -rf ~" & "'
        with mock.patch("shutil.which", return_value="/usr/bin/osascript"):
            argv = notify.notification_command("Conch", hostile, platform="darwin")
        script = " ".join(argv[1:-2])
        self.assertNotIn("rm -rf", script)
        self.assertEqual(argv[-1], hostile)

    def test_no_notifier_means_no_command(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(notify.notification_command("t", "m", platform="darwin"))
            self.assertIsNone(notify.notification_command("t", "m", platform="linux"))
        self.assertIsNone(notify.notification_command("t", "m", platform="win32"))


if __name__ == "__main__":
    unittest.main()
