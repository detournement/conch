"""Live-panel geometry regressions using prompt-toolkit's actual renderer."""

import os
import tempfile
import unittest

from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth

from conch.terminal_ui import TerminalUI


class SizedOutput(DummyOutput):
    size = Size(rows=12, columns=24)

    def get_size(self):
        return self.size

    def get_rows_below_cursor_position(self):
        return 1


class _UICase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ui = TerminalUI(lambda: [], os.path.join(directory.name, "history"))
        self.output = SizedOutput()
        self.ui._input = DummyInput()
        self.ui._output = self.output
        with create_app_session(input=self.ui._input, output=self.output):
            self.ui._build()
        self.ui._editor.text = "draft-kept"

    def render(self):
        app = self.ui._app
        with create_app_session(input=self.ui._input, output=self.output), set_app(app):
            app.render_counter += 1
            app.renderer.render(app, app.layout)
        screen = app.renderer._last_screen
        self.assertLessEqual(screen.height, self.output.size.rows)
        cursor = screen.get_cursor_position(self.ui._editor.window)
        self.assertGreaterEqual(cursor.y, 0)
        self.assertLess(cursor.y, self.output.size.rows)
        self.assertEqual(self.ui._editor.text, "draft-kept")
        return "\n".join(
            "".join(screen.data_buffer[y][x].char
                    for x in range(self.output.size.columns)).rstrip()
            for y in range(screen.height)
        )


class LivePanelRenderingTests(_UICase):

    def test_tail_is_bounded_by_display_cells_not_codepoints(self):
        for width in (12, 24, 41):
            with self.subTest(width=width):
                self.output.size = Size(rows=12, columns=width)
                text = "old\n" + "界e\u0301\twrapped " * 80 + "\nLATEST"
                tail = self.ui._tail_text(text, 4)
                self.assertLessEqual(len(tail.splitlines()), 4)
                self.assertTrue(all(get_cwidth(line) <= width for line in tail.splitlines()))
                self.assertNotIn("\t", tail)
                self.assertNotIn("old", tail)
                self.assertTrue(tail.endswith("LATEST"))

    async def test_growing_resized_and_toggled_panels_keep_latest_row_and_composer(self):
        self.ui._busy = True
        self.assertTrue(self.ui._details_visible)
        for width, count in ((41, 1), (24, 10), (12, 80), (41, 100)):
            with self.subTest(width=width, count=count):
                self.output.size = Size(rows=12, columns=width)
                self.ui._response = "response " * count
                self.ui._detail_text = "界e\u0301\twrapped " * count + "\nLIVE-END"
                self.assertIn("LIVE-END", self.render())
                self.ui._details_visible = False
                self.assertNotIn("LIVE-END", self.render())
                self.ui._details_visible = True
                self.assertIn("LIVE-END", self.render())


class _DirectUI(_UICase):
    """State changes applied inline: the event loop is not running here."""

    def setUp(self):
        super().setUp()
        self.ui._change = lambda function, *args: function(*args)
        self.published = []
        self.ui._enqueue_output = lambda text, trusted: self.published.append(text)

    def queue(self, *texts, kind="steering"):
        from conch.terminal_ui import _PendingMessage

        self.ui._messages.extend(_PendingMessage(text, kind) for text in texts)


class HintRowTests(_DirectUI):
    def hint_row(self):
        return self.render().splitlines()[-1]

    async def test_hints_are_dropped_whole_instead_of_cut(self):
        for busy in (False, True):
            self.ui._busy = busy
            self.output.size = Size(rows=12, columns=200)
            complete = self.hint_row().split(" · ")
            self.assertGreater(len(complete), 2)
            for width in (60, 41, 24):
                with self.subTest(busy=busy, width=width):
                    self.output.size = Size(rows=12, columns=width)
                    shown = self.hint_row().split(" · ")
                    self.assertLess(get_cwidth(self.hint_row()), width)
                    self.assertLess(len(shown), len(complete))
                    self.assertEqual(shown[0], complete[0])
                    self.assertTrue(set(shown) <= set(complete))

    async def test_pending_and_detail_keys_are_listed_only_when_they_apply(self):
        self.output.size = Size(rows=12, columns=200)
        idle = self.hint_row()
        self.queue("guidance")
        self.ui._detail_text = "Reasoning: thinking"
        with_state = self.hint_row()
        for key in ("Ctrl+Q", "Alt+R"):
            self.assertNotIn(key, idle)
            self.assertIn(key, with_state)


class TurnBoundaryTests(_DirectUI):
    def test_interrupt_returns_pending_messages_to_the_composer(self):
        self.queue("first", "second")
        self.queue("/status", kind="follow-up")
        self.assertEqual(self.ui.restore_pending(), 3)
        self.assertEqual(self.ui._editor.text, "first\nsecond\n/status\ndraft-kept")
        self.assertEqual(self.ui.take_steering(), [])
        self.assertEqual(len(self.ui._messages), 0)
        self.assertEqual(self.published, [])

    def test_nothing_pending_leaves_the_draft_alone(self):
        self.assertEqual(self.ui.restore_pending(), 0)
        self.assertEqual(self.ui._editor.text, "draft-kept")

    def test_guidance_is_echoed_after_the_reply_it_followed(self):
        self.queue("make it shorter")
        self.ui.update_response("Streaming reply")
        self.assertEqual(self.ui.take_steering(), ["make it shorter"])
        self.assertEqual(self.published, [])
        self.ui.finish_response("Streaming reply")
        self.assertEqual(len(self.published), 2)
        self.assertIn("Streaming reply", self.published[0])
        self.assertIn("make it shorter", self.published[1])

    def test_guidance_without_a_live_reply_is_echoed_at_once(self):
        self.queue("go")
        self.assertEqual(self.ui.take_steering(), ["go"])
        self.assertEqual(len(self.published), 1)

    async def test_progress_clears_at_turn_end_but_reasoning_stays(self):
        self.output.size = Size(rows=12, columns=60)
        for kind, kept in (("progress", False), ("reasoning", True)):
            with self.subTest(kind=kind):
                self.ui.set_busy(True)
                self.ui.feed_detail(kind, "DETAIL-MARK")
                self.assertIn("DETAIL-MARK", self.render())
                self.ui.set_busy(False)
                self.assertEqual("DETAIL-MARK" in self.render(), kept)

    def test_blank_submission_is_discarded_not_queued(self):
        self.ui._busy = True
        self.ui._editor.text = "  \n "
        self.ui._send()
        self.assertEqual(len(self.ui._messages), 0)
        self.assertEqual(self.ui._editor.text, "")
        self.ui._editor.text = "draft-kept"

    def test_declined_command_is_reported_as_declined_with_its_feedback(self):
        token = self.ui.tool_started("local_shell")
        self.ui.tool_finished(
            token, "User declined to execute the command. Feedback: use ls -la",
            error=True)
        line = self.published[-1]
        self.assertIn("declined", line)
        self.assertNotIn("error", line)
        self.assertIn("use ls -la", line)


class ChoiceRowTests(_DirectUI):
    async def test_long_choices_stay_one_row_each_so_all_remain_visible(self):
        self.output.size = Size(rows=20, columns=30)
        self.ui._modal = "choose"
        self.ui._modal_title = "Pick"
        self.ui._modal_choices = [
            (f"/c{index}", f"/c{index}", "description " * 12) for index in range(8)
        ]
        app = self.ui._app
        with create_app_session(input=self.ui._input, output=self.output), set_app(app):
            app.renderer.render(app, app.layout)
        screen = app.renderer._last_screen
        rows = ["".join(screen.data_buffer[y][x].char for x in range(30)).rstrip()
                for y in range(screen.height)]
        for index in range(8):
            self.assertEqual(sum(f"/c{index}" in row for row in rows), 1)
        # Search row + eight choices + exactly one hint row below the title.
        self.assertEqual(rows.index(next(row for row in rows if "/c7" in row)),
                         len(rows) - 2)
        self.assertLess(get_cwidth(rows[-1]), 30)


class LiveCommandTests(_DirectUI):
    """Panels and reports typed during a turn start at once; anything that
    changes the session still waits for the turn."""

    def setUp(self):
        super().setUp()
        import threading

        self.started = threading.Event()
        self.asked = []

        def live(command):
            self.asked.append(command)
            if command.split()[0] in ("/models", "/convos"):
                return self.started.set
            return None

        self.ui._live_command = live
        self.ui._busy = True

    def submit(self, text):
        self.ui._editor.text = text
        self.ui._send()

    def test_panel_runs_now_instead_of_queueing(self):
        self.submit("/models")
        self.assertTrue(self.started.wait(5), "panel runner never started")
        self.assertEqual(len(self.ui._messages), 0)
        self.assertIn("> /models\n", self.published)
        self.assertEqual(self.ui._editor.text, "")

    def test_session_changing_command_still_waits_for_the_turn(self):
        self.submit("/model gpt-5")
        self.assertFalse(self.started.is_set())
        self.assertEqual([(m.text, m.kind) for m in self.ui._messages],
                         [("/model gpt-5", "follow-up")])

    def test_chat_and_multiline_never_reach_the_router(self):
        self.submit("make it shorter")
        self.submit("/models\nand more")
        self.assertEqual(self.asked, [])
        self.assertEqual([m.kind for m in self.ui._messages], ["steering", "steering"])

    def test_panels_open_even_with_the_queue_off(self):
        self.ui._queue_enabled = False
        self.submit("/convos")
        self.assertTrue(self.started.wait(5))
        self.assertEqual(len(self.ui._messages), 0)

    def test_a_pick_queues_its_command_for_after_the_turn(self):
        self.ui.queue_command("/switch abc123")
        self.assertEqual([(m.text, m.kind) for m in self.ui._messages],
                         [("/switch abc123", "follow-up")])
        self.assertIn("/switch abc123", self.ui._notice)


class CaptureThreadTests(unittest.TestCase):
    def test_partial_lines_from_two_threads_never_splice(self):
        import threading
        import types

        from conch.terminal_ui import _Capture

        published = []
        ui = types.SimpleNamespace(
            _routing=threading.RLock(), _direct=False, _stopped=threading.Event(),
            _enqueue_output=lambda text, trusted: published.append(text),
        )
        capture = _Capture(ui, original=None)
        capture.write("turn: half")
        other = threading.Thread(target=lambda: capture.write("panel line\n"))
        other.start()
        other.join()
        capture.write(" done\n")
        self.assertEqual(published, ["panel line\n", "turn: half done\n"])


if __name__ == "__main__":
    unittest.main()
