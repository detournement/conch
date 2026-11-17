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


class LivePanelRenderingTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
