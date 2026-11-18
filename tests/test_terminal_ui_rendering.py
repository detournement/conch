"""Live-panel geometry regressions using prompt-toolkit's actual renderer."""

import os
import tempfile
import unittest
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
from prompt_toolkit.input import DummyInput
from prompt_toolkit.key_binding.key_processor import KeyPress, _Flush
from prompt_toolkit.keys import Keys
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import ColorDepth, DummyOutput
from prompt_toolkit.utils import get_cwidth

from conch.terminal_ui import TerminalUI
from conch.themes import SCHEMES, style_for


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
        self.assertGreaterEqual(cursor.x, 0)
        self.assertLess(cursor.x, self.output.size.columns)
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


class PromptEchoTests(_UICase):
    async def test_prompt_boundaries_keep_literal_text_and_queue_order(self):
        import threading

        self.ui._thread = threading.current_thread()
        self.ui._schedule_publish = lambda: None
        prompt = "  first line\n\n" + "界e\u0301 long " * 20 + "\n\tlast line  "
        hostile = "\033[31mRED\033[0m\033]52;c;clipboard\a\x9b2J"
        for width in (12, 24, 80):
            with self.subTest(width=width):
                self.output.size = Size(rows=12, columns=width)
                self.ui._enqueue_output("BEFORE", trusted=False)
                self.ui._accept_messages([prompt, hostile])
                self.ui._enqueue_output("AFTER", trusted=False)
                queued = list(self.ui._output_queue)
                self.assertEqual(len(queued), 4)
                for fragments, trusted in queued[1:3]:
                    self.assertFalse(trusted)
                    self.assertTrue(all(get_cwidth(value) < width
                                        for style, value in fragments if style))
                self.assertIn(("", "\n" + prompt + "\n"), queued[1][0])
                self.assertEqual(self.ui._history.get_strings()[-2:], [prompt, hostile])
                with patch("conch.terminal_ui.print_formatted_text") as publish, \
                        patch.object(self.ui._app.renderer, "erase"), \
                        patch.object(self.ui._app, "_request_absolute_cursor_position"), \
                        patch.object(self.ui._app, "_redraw"):
                    await self.ui._publish_output()
                fragments = publish.call_args.args[1]
                transcript = fragment_list_to_text(fragments)
                self.assertTrue(transcript.startswith("BEFORE\n\n"))
                self.assertTrue(transcript.endswith("\n\nAFTER"))
                self.assertIn("\n" + prompt + "\n", transcript)
                inert = "[31mRED[0m]52;c;clipboard2J"
                self.assertIn(("", "\n" + inert + "\n"), fragments)
                self.assertLess(transcript.index(prompt), transcript.index(inert))
                self.assertNotIn("\033", transcript)
                self.assertNotIn("\x9b", transcript)
                self.assertNotIn("\a", transcript)


class _DirectUI(_UICase):
    """State changes applied inline: the event loop is not running here."""

    def setUp(self):
        super().setUp()
        self.ui._change = lambda function, *args: function(*args)
        self.published = []
        self.ui._enqueue_output = lambda text, trusted: self.published.append(
            fragment_list_to_text(to_formatted_text(text)))

    def queue(self, *texts, kind="steering"):
        from conch.terminal_ui import _PendingMessage

        self.ui._messages.extend(_PendingMessage(text, kind) for text in texts)


class ComposerStateTests(_DirectUI):
    async def test_pause_notice_disappears_on_resume_without_losing_draft(self):
        self.output.size = Size(rows=12, columns=80)
        self.ui.set_context(agents_paused=True)
        paused = self.render()
        self.assertIn("PAUSED", paused)
        self.assertIn("/ebrake off", paused)
        self.ui.set_context(agents_paused=False)
        resumed = self.render()
        self.assertNotIn("PAUSED", resumed)
        self.assertEqual(len(resumed.splitlines()), len(paused.splitlines()) - 1)

    async def test_live_theme_switch_can_enter_and_leave_monochrome_without_losing_draft(self):
        from prompt_toolkit.output import ColorDepth

        self.ui._no_color = False
        self.output.get_default_color_depth = lambda: ColorDepth.DEPTH_24_BIT
        self.ui.set_colorscheme("monochrome")
        self.assertEqual(self.ui._app.color_depth, ColorDepth.DEPTH_1_BIT)
        self.assertIn("draft-kept", self.render())
        self.ui.set_colorscheme("light")
        self.assertNotEqual(self.ui._app.color_depth, ColorDepth.DEPTH_1_BIT)
        self.assertIn("draft-kept", self.render())


class InfobarRenderingTests(_DirectUI):
    STATS = ("\033[2m2,037,305 in / 3,837 out  subscription seat  ~89.3 tok/s"
             "  ctx \033[32m16%\033[0m\033[2m  (claude-sonnet-5-5)\033[0m")

    def configure(self, mode="detail", **fields):
        self.ui.set_context(
            infobar=mode, infobar_stats=self.STATS, context_used=32_000,
            context_window=200_000, **fields)

    def assert_bar_geometry(self):
        rows = fragment_list_to_text(self.ui._infobar_text()).splitlines()
        self.assertLessEqual(len(rows), 2)
        self.assertEqual(len(rows), self.ui._infobar_height())
        self.assertTrue(all(get_cwidth(row) < self.output.size.columns for row in rows))
        self.assertNotIn("\033", "".join(rows))
        return rows

    async def test_detail_retains_all_metrics_at_normal_terminal_widths(self):
        self.configure()
        for width in (80, 100):
            with self.subTest(width=width):
                self.output.size = Size(rows=12, columns=width)
                rendered = " ".join(self.render().split())
                for field in ("2,037,305 in", "3,837 out", "subscription seat",
                              "~89.3 tok/s", "ctx 16%", "(claude-sonnet-5-5)"):
                    self.assertIn(field, rendered)
                self.assert_bar_geometry()

    async def test_resizing_modes_and_visibility_preserve_draft_cursor_and_history(self):
        self.ui._editor.buffer.cursor_position = 4
        self.ui._history.append_string("previous input")
        history = self.ui._history.get_strings()
        self.ui._busy = True
        self.ui._response = "Live output\n" * 20
        self.ui._detail_text = "Reasoning\n" * 20
        for mode in ("detail", "progress"):
            self.configure(mode)
            for height, width in ((12, 100), (12, 80), (6, 24), (4, 16), (2, 16)):
                with self.subTest(mode=mode, height=height, width=width):
                    self.output.size = Size(rows=height, columns=width)
                    self.assertIn("draft-kept", self.render())
                    self.assert_bar_geometry()
                    self.assertEqual(self.ui._editor.buffer.cursor_position, 4)
                    self.assertEqual(self.ui._history.get_strings(), history)
            self.ui.set_context(show_token_stats=False)
            self.assertEqual(self.ui._infobar_text(), [])
            self.assertIn("draft-kept", self.render())
            self.ui.set_context(show_token_stats=True)
            self.assertTrue(self.ui._infobar_text())

    async def test_progress_uses_budget_without_clamping_percentage(self):
        self.configure("progress")
        for width in (16, 24, 80, 100):
            for used, percent in ((0, "0%"), (32_000, "16%"), (250_000, "125%")):
                with self.subTest(width=width, used=used):
                    self.output.size = Size(rows=12, columns=width)
                    self.ui.set_context(context_used=used)
                    rendered = self.render()
                    self.assertIn(percent, rendered)
                    self.assertTrue("200,000" in rendered or "200k" in rendered)
                    rows = self.assert_bar_geometry()
                    ruler = next(row for row in rows if "[" in row)
                    fill = ruler.split("[", 1)[1].split("]", 1)[0]
                    self.assertTrue(set(fill) <= {"=", "-"})
                    if used == 0:
                        self.assertNotIn("=", fill)
                    elif used > 200_000:
                        self.assertNotIn("-", fill)
        for capacity in (None, 0):
            self.ui.set_context(context_window=capacity)
            self.assertNotIn("%", fragment_list_to_text(self.ui._infobar_text()))
            self.assertNotIn("[", fragment_list_to_text(self.ui._infobar_text()))
            self.render()

    async def test_brake_remains_separate_from_either_bar_and_token_visibility(self):
        self.output.size = Size(rows=12, columns=80)
        for mode in ("detail", "progress"):
            self.configure(mode, agents_paused=True)
            self.assertIn("PAUSED", self.render())
            self.assertIn("/ebrake off", self.render())
            self.ui.set_context(show_token_stats=False)
            self.assertIn("PAUSED", self.render())
            self.ui.set_context(agents_paused=False, show_token_stats=True)
            self.assertNotIn("PAUSED", self.render())
            self.assertTrue(self.ui._infobar_text())

    async def test_detail_severity_uses_theme_classes_instead_of_ansi_colors(self):
        self.output.size = Size(rows=12, columns=80)
        self.configure()
        for scheme in ("default", "dark", "light", "dracula", "solarized", "monochrome"):
            self.ui.set_colorscheme(scheme)
            for color, role in ((31, "error"), (33, "warning"), (32, "ok")):
                with self.subTest(scheme=scheme, color=color):
                    self.ui.set_context(infobar_stats=self.STATS.replace("[32m", f"[{color}m"))
                    self.assertIn(f"class:{role}", [style for style, _ in self.ui._infobar_text()])
                    self.assert_bar_geometry()
                    self.assertIn("16%", self.render())

    async def test_environment_color_opt_out_applies_to_both_bars(self):
        from prompt_toolkit.output import ColorDepth

        for environment in ({"NO_COLOR": "1"}, {"TERM": "dumb"}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                with tempfile.TemporaryDirectory() as directory:
                    self.ui = TerminalUI(lambda: [], os.path.join(directory, "history"))
                    self.ui._input = DummyInput()
                    self.ui._output = self.output
                    with create_app_session(input=self.ui._input, output=self.output):
                        self.ui._build()
                    self.ui._change = lambda function, *args: function(*args)
                    self.ui._editor.text = "draft-kept"
                    self.ui.set_colorscheme("light")
                    self.assertEqual(self.ui._app.color_depth, ColorDepth.DEPTH_1_BIT)
                    for mode in ("detail", "progress"):
                        self.configure(mode)
                        self.assert_bar_geometry()
                        for role in ("status", "ok", "warning", "error"):
                            attrs = self.ui._app.style.get_attrs_for_style_str(f"class:{role}")
                            self.assertFalse(attrs.color)
                            self.assertFalse(attrs.bgcolor)
                        self.assertIn("draft-kept", self.render())


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


class ColorSchemePickerTests(_DirectUI):
    def setUp(self):
        super().setUp()
        self.output.size = Size(rows=24, columns=100)
        self.ui.set_colorscheme("dark")

    def open_picker(self):
        return self.ui._open_modal(
            "choose", "Color scheme",
            [(name, name, description) for name, description in SCHEMES.items()],
            preview_colors=True)

    def key(self, key):
        app = self.ui._app
        with create_app_session(input=self.ui._input, output=self.output), set_app(app):
            app.key_processor.feed_multiple([KeyPress(key), _Flush])
            app.key_processor.process_keys()

    def mouse(self, name, event_type, button=MouseButton.NONE):
        rows = self.render().splitlines()
        row = next(index for index, text in enumerate(rows)
                   if text.startswith(("› ", "  ")) and name in text.split())
        column = rows[row].index(name)
        event = MouseEvent(Point(x=column, y=row), event_type, button, frozenset())
        app = self.ui._app
        with create_app_session(input=self.ui._input, output=self.output), set_app(app):
            app.layout.update_parents_relations()
            app.renderer.mouse_handlers.mouse_handlers[row][column](event)

    async def test_hover_previews_without_committing_and_escape_restores(self):
        result = self.open_picker()
        self.assertTrue(self.ui._app.mouse_support())
        self.mouse("light", MouseEventType.MOUSE_MOVE)
        self.assertEqual(self.ui._app.style, style_for("light"))
        self.assertEqual(self.ui._colorscheme, "dark")
        self.assertFalse(result.done())
        self.key(Keys.Escape)
        self.assertIsNone(result.result(timeout=1))
        self.assertEqual(self.ui._app.style, style_for("dark"))
        self.assertFalse(self.ui._app.mouse_support())
        self.assertEqual(self.ui._editor.text, "draft-kept")

    async def test_click_selects_filtered_row_and_restores_until_saved(self):
        result = self.open_picker()
        self.ui._query.text = "solarized"
        self.mouse("solarized", MouseEventType.MOUSE_UP, MouseButton.LEFT)
        self.assertEqual(result.result(timeout=1), "solarized")
        # The caller persists first; a failed save must leave the original visible.
        self.assertEqual(self.ui._colorscheme, "dark")
        self.assertEqual(self.ui._app.style, style_for("dark"))
        self.assertFalse(self.ui._app.mouse_support())

    async def test_keyboard_filter_and_superseding_approval_clear_preview(self):
        result = self.open_picker()
        self.key(Keys.Down)
        self.assertEqual(self.ui._app.style, style_for("light"))
        self.ui._query.text = "monochrome"
        self.assertEqual(self.ui._app.color_depth, ColorDepth.DEPTH_1_BIT)
        self.ui._query.text = "no-such-scheme"
        self.assertEqual(self.ui._app.style, style_for("dark"))
        self.key(Keys.ControlM)
        self.assertFalse(result.done())
        self.ui._query.text = "light"
        approval = self.ui._open_modal("ask", "Approve")
        self.assertIsNone(result.result(timeout=1))
        self.assertEqual(self.ui._app.style, style_for("dark"))
        self.assertFalse(self.ui._app.mouse_support())
        self.ui._answer.text = "yes"
        self.key(Keys.ControlM)
        self.assertEqual(approval.result(timeout=1), "yes")


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
