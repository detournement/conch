"""Long-turn delivery, terminal-origin routing, and untrusted display text."""

import html
import shlex
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

    def test_toggling_alerts_preserves_delay_and_enables_legacy_off(self):
        import tempfile
        from conch.config import load_config
        from conch.settings_registry import current_value, set_setting

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
                notify.os.environ, {"HOME": home, "XDG_CONFIG_HOME": home}):
            config = {}
            set_setting(config, "notify_after_seconds", "7")
            set_setting(config, "notifications", "off")
            self.assertFalse(self.run_turn(config, 60)[0])
            config = load_config()
            self.assertEqual(current_value(config, "notifications"), "off")
            set_setting(config, "notifications", "on")
            self.assertFalse(self.run_turn(config, 6)[0])
            self.assertTrue(self.run_turn(load_config(), 7)[0])
            config["notify_after_seconds"] = "off"
            self.assertEqual(current_value(config, "notifications"), "off")
            set_setting(config, "notifications", "on")
            self.assertTrue(self.run_turn(load_config(), notify.DEFAULT_AFTER_SECONDS)[0])

    def test_unparseable_threshold_keeps_the_default(self):
        self.assertEqual(notify.notify_after_seconds({"notify_after_seconds": "soon"}),
                         notify.DEFAULT_AFTER_SECONDS)

    def test_failed_turn_says_so_instead_of_quoting_a_reply(self):
        _, send = self.run_turn({}, 60, reply="", failed=True)
        self.assertIn("failed", send.call_args[0][1])


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(notify.os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.popen = mock.patch.object(notify.subprocess, "Popen").start()
        self.addCleanup(mock.patch.stopall)

    def test_apple_terminal_click_uses_os_tty_not_environment_or_text(self):
        hostile = '-execute "evil"; $(bad) \\ \x1b]52;c;data\x07'
        notify.os.environ.update(TERM_PROGRAM="Apple_Terminal",
                                 TERM="xterm-256color", TTY="/dev/ttys999")
        with mock.patch.object(notify.sys, "platform", "darwin"), \
                mock.patch.object(notify.shutil, "which", return_value="/opt/homebrew/bin/terminal-notifier"), \
                mock.patch.object(notify.os, "ttyname", return_value="/dev/ttys042"), \
                mock.patch.object(notify.os, "getsid", return_value=123):
            self.assertTrue(notify.send(hostile, hostile))
        argv = self.popen.call_args.args[0]
        click = shlex.split(argv[argv.index("-execute") + 1])
        self.assertEqual(click, ["/usr/bin/osascript", "-e", notify._FOCUS_TERMINAL,
                                "/dev/ttys042", "123"])
        self.assertNotIn("evil", click[2])
        self.assertNotIn("/dev/ttys999", " ".join(argv))
        self.assertNotIn("-activate", argv)
        self.assertEqual(argv[argv.index("-title") + 1], "\\" + notify._plain(hostile, 80))
        self.assertEqual(argv[argv.index("-message") + 1],
                         "\\" + notify._plain(hostile, notify._MAX_MESSAGE_CHARS))
        self.assertTrue(self.popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("shell", self.popen.call_args.kwargs)

    def test_apple_terminal_missing_notifier_uses_stock_tab_bell(self):
        notify.os.environ["TERM_PROGRAM"] = "Apple_Terminal"
        with mock.patch.object(notify.sys, "platform", "darwin"), \
                mock.patch.object(notify.shutil, "which", return_value=None), \
                mock.patch.object(notify, "_tty_output", return_value=True) as tty:
            self.assertTrue(notify.send("t", "m"))
        tty.assert_called_once_with("\a")
        self.popen.assert_not_called()

    def test_apple_terminal_without_usable_tty_never_activates_another_window(self):
        notify.os.environ["TERM_PROGRAM"] = "Apple_Terminal"
        for invalid in ("/dev/pts/1", '/dev/ttys000"; echo bad', "/dev/tty"):
            with self.subTest(tty=invalid), \
                    mock.patch.object(notify.sys, "platform", "darwin"), \
                    mock.patch.object(notify.shutil, "which", return_value="/notifier"), \
                    mock.patch.object(notify.os, "ttyname", return_value=invalid):
                self.assertFalse(notify.send("t", "m"))
        self.popen.assert_not_called()

    def test_native_protocol_is_bound_to_tty_and_sanitizes_control_and_field_injection(self):
        hostile = "4;2;\x1b\\\x07\x9c\x1b]52;c;secret\x07%p\nnext"
        for platform, environment in (
                ("darwin", {"TERM_PROGRAM": "iTerm.app"}),
                ("darwin", {"TERM_PROGRAM": "ghostty"}),
                ("darwin", {"TERM_PROGRAM": "WezTerm"}),
                ("linux", {"TERM": "xterm-kitty"}),
                ("linux", {"TERM": "xterm-ghostty"})):
            with self.subTest(platform=platform, environment=environment), \
                    mock.patch.dict(notify.os.environ, environment, clear=True), \
                    mock.patch.object(notify.sys, "platform", platform), \
                    mock.patch.object(notify, "_tty_output", return_value=True) as tty:
                self.assertTrue(notify.send(hostile, hostile))
                payload = tty.call_args.args[0]
                self.assertTrue(payload.startswith("\x1b]9;"))
                self.assertTrue(payload.endswith("\x1b\\"))
                text = payload[len("\x1b]9;"):-len("\x1b\\")]
                self.assertNotIn(";", text)
                self.assertTrue(all(c.isprintable() for c in text))
                self.assertIn("%p", text)
        self.popen.assert_not_called()

    def test_native_writes_use_fixed_printf_format_and_controlling_terminal(self):
        terminal = mock.MagicMock()
        terminal.fileno.return_value = 42
        terminal.isatty.return_value = True
        terminal.__enter__.return_value = terminal
        opened = mock.mock_open()
        opened.return_value = terminal
        payload = "\x1b]9;Conch: %s%n\x1b\\"
        with mock.patch("builtins.open", opened):
            self.assertTrue(notify._tty_output(payload))
        opened.assert_called_once_with("/dev/tty", "r+b", buffering=0)
        argv = self.popen.call_args.args[0]
        self.assertEqual(argv, ["/usr/bin/printf", "%s", payload])
        self.assertEqual(self.popen.call_args.kwargs["stdout"], 42)

    def test_linux_desktop_uses_notify_send_without_any_mac_helper(self):
        body = r'<a href="evil">click</a> \n \x1b \123'
        for desktop in ({"DISPLAY": ":1"}, {"WAYLAND_DISPLAY": "wayland-0"}):
            with self.subTest(desktop=desktop), \
                    mock.patch.dict(notify.os.environ, desktop, clear=True), \
                    mock.patch.object(notify.sys, "platform", "linux"), \
                    mock.patch.object(notify.shutil, "which", return_value="/usr/bin/notify-send") as which:
                self.assertTrue(notify.send("--action=bad", body))
            which.assert_called_once_with("notify-send")
            argv = self.popen.call_args.args[0]
            self.assertEqual(argv[:4], ["/usr/bin/notify-send", "--app-name=Conch", "--",
                                       "--action=bad"])
            self.assertEqual(argv[4], html.escape(body, quote=False).replace("\\", "\\\\"))

    def test_unavailable_native_route_can_fall_back_to_linux_desktop(self):
        notify.os.environ.update(TERM_PROGRAM="ghostty", DISPLAY=":0")
        with mock.patch.object(notify.sys, "platform", "linux"), \
                mock.patch.object(notify, "_tty_output", return_value=False), \
                mock.patch.object(notify.shutil, "which", return_value="/usr/bin/notify-send"):
            self.assertTrue(notify.send("t", "m"))
        self.assertEqual(self.popen.call_args.args[0][0], "/usr/bin/notify-send")

    def test_mux_does_not_assume_outer_terminal_protocol_reaches_current_pane(self):
        for mux in ({"TMUX": "/tmp/tmux"}, {"STY": "screen"}, {"TERM": "screen-256color"}):
            with self.subTest(mux=mux), \
                    mock.patch.dict(notify.os.environ, dict(TERM_PROGRAM="iTerm.app", **mux), clear=True), \
                    mock.patch.object(notify.sys, "platform", "darwin"), \
                    mock.patch.object(notify, "_tty_output") as tty:
                self.assertFalse(notify.send("t", "m"))
                tty.assert_not_called()
        self.popen.assert_not_called()

    def test_headless_missing_tool_and_unsupported_platform_are_noops(self):
        for platform, environment in (
                ("linux", {}),
                ("linux", {"DISPLAY": ":0"}),
                ("darwin", {}),
                ("win32", {"TERM_PROGRAM": "iTerm.app"})):
            with self.subTest(platform=platform, environment=environment), \
                    mock.patch.dict(notify.os.environ, environment, clear=True), \
                    mock.patch.object(notify.sys, "platform", platform), \
                    mock.patch.object(notify.shutil, "which", return_value=None):
                self.assertFalse(notify.send("t", "m"))
        self.popen.assert_not_called()

    def test_ssh_never_posts_remote_content_to_local_desktop_or_native_output(self):
        notify.os.environ.update(SSH_CONNECTION="host port host port", DISPLAY=":0",
                                 TERM_PROGRAM="ghostty")
        with mock.patch.object(notify.sys, "platform", "linux"), \
                mock.patch.object(notify, "_tty_output") as tty:
            self.assertFalse(notify.send("t", "m"))
        tty.assert_not_called()
        self.popen.assert_not_called()

    def test_missing_controlling_tty_and_failed_spawn_are_safe(self):
        with mock.patch("builtins.open", side_effect=OSError):
            self.assertFalse(notify._tty_output("t"))
        notify.os.environ["DISPLAY"] = ":0"
        with mock.patch.object(notify.sys, "platform", "linux"), \
                mock.patch.object(notify.shutil, "which", return_value="/notify-send"):
            self.popen.side_effect = OSError
            self.assertFalse(notify.send("t", "m"))


if __name__ == "__main__":
    unittest.main()
