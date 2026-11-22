"""Command browsing exercises the real prompt-toolkit key dispatch and renderer."""

import os
import tempfile
import unittest

from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import DummyInput
from prompt_toolkit.key_binding.key_processor import KeyPress, _Flush
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth

from conch.terminal_ui import TerminalUI


class SizedOutput(DummyOutput):
    size = Size(rows=24, columns=80)

    def get_size(self):
        return self.size

    def get_rows_below_cursor_position(self):
        return 1


class CommandBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.commands = [
            ("/help", "Browse commands"),
            ("/model <name>", "Select a model"),
            ("/models", "List available models"),
            ("/notes [query]", "Search saved notes"),
        ]
        self.ui = TerminalUI(lambda: self.commands, os.path.join(directory.name, "history"))
        self.output = SizedOutput()
        self.ui._input = DummyInput()
        self.ui._output = self.output
        with create_app_session(input=self.ui._input, output=self.output):
            self.ui._build()

    def draft(self, text, cursor=None):
        self.ui._editor.buffer.document = Document(text, len(text) if cursor is None else cursor)

    def press(self, *keys):
        app = self.ui._app
        with create_app_session(input=self.ui._input, output=self.output), set_app(app):
            app.key_processor.feed_multiple([KeyPress(key) for key in keys] + [_Flush])
            app.key_processor.process_keys()

    def submitted(self):
        return [message.text for message in self.ui._messages]

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
        return [
            "".join(screen.data_buffer[y][x].char for x in range(self.output.size.columns)).rstrip()
            for y in range(screen.height)
        ]

    async def test_typing_slash_opens_first_selection_and_enter_runs_browsed_command(self):
        self.press("/")
        self.assertTrue(self.ui._command_visible())
        self.assertEqual(self.ui._command_selection, 0)
        self.press(Keys.Down)
        self.assertEqual(self.ui._editor.text, "/")
        self.press(Keys.ControlM)
        self.assertEqual(self.submitted(), ["/model"])
        self.assertEqual(self.ui._editor.text, "")

    async def test_tab_fills_only_command_and_argument_space_without_executing(self):
        self.draft("/mod")
        self.press(Keys.ControlI)
        self.assertEqual(self.ui._editor.text, "/model ")
        self.assertFalse(self.ui._command_visible())
        self.assertEqual(self.submitted(), [])
        self.press("x", Keys.ControlM)
        self.assertEqual(self.submitted(), ["/model x"])

    async def test_tab_on_argumentless_command_does_not_add_space(self):
        self.draft("/hel")
        self.press(Keys.ControlI)
        self.assertEqual(self.ui._editor.text, "/help")
        self.assertFalse(self.ui._command_visible())
        self.press(Keys.ControlM)
        self.assertEqual(self.submitted(), ["/help"])

    async def test_arguments_and_multiline_pastes_are_submitted_unchanged(self):
        drafts = ["/model chosen-name", "/model\tchosen-name", "/models\nexplain these", "/\n"]
        for text in drafts:
            with self.subTest(text=text):
                self.draft(text)
                self.assertFalse(self.ui._command_visible())
                self.press(Keys.ControlM)
                self.assertEqual(self.submitted()[-1], text)

    async def test_mid_draft_editing_does_not_select_a_command(self):
        self.draft("/models", cursor=3)
        self.assertFalse(self.ui._command_visible())
        self.press(Keys.ControlM)
        self.assertEqual(self.submitted(), ["/models"])

    async def test_alt_enter_keeps_multiline_binding_while_browser_is_open(self):
        self.draft("/mod")
        self.press(Keys.Escape, Keys.ControlM)
        self.assertEqual(self.ui._editor.text, "/mod\n")
        self.assertFalse(self.ui._command_visible())
        self.assertEqual(self.submitted(), [])

    async def test_escape_preserves_query_and_typing_reopens_browser(self):
        self.draft("/mod")
        self.press(Keys.Down, Keys.Escape)
        self.assertEqual(self.ui._editor.text, "/mod")
        self.assertFalse(self.ui._command_visible())
        self.press("e")
        self.assertTrue(self.ui._command_visible())
        self.assertEqual(self.ui._command_selection, 0)
        self.press(Keys.Escape, Keys.ControlM)
        self.assertEqual(self.ui._editor.text, "/mode\n")
        self.assertEqual(self.submitted(), [])

    async def test_ctrl_p_restores_original_multiline_draft_and_cursor_on_escape(self):
        text = "Keep this\nunfinished thought"
        self.draft(text, cursor=4)
        self.press(Keys.ControlP)
        self.assertTrue(self.ui._command_visible())
        self.assertIsNone(self.ui._modal)
        self.press(Keys.Down, "m", Keys.Escape)
        self.assertEqual(self.ui._editor.text, text)
        self.assertEqual(self.ui._editor.buffer.cursor_position, 4)
        self.assertEqual(self.submitted(), [])

    async def test_ctrl_p_accepts_command_instead_of_submitting_saved_draft(self):
        self.draft("unfinished thought")
        self.press(Keys.ControlP, Keys.Down, Keys.ControlM)
        self.assertEqual(self.submitted(), ["/model"])
        self.assertEqual(self.ui._editor.text, "")
        self.draft("/hel")
        self.press(Keys.Escape)
        self.assertEqual(self.ui._editor.text, "/hel")

    async def test_ctrl_p_can_reopen_a_dismissed_query(self):
        self.draft("/mod")
        self.press(Keys.Escape, Keys.ControlP)
        self.assertTrue(self.ui._command_visible())
        self.assertEqual(self.ui._editor.text, "/mod")
        self.press(Keys.Escape)
        self.assertEqual(self.ui._editor.text, "/mod")

    async def test_filter_resets_selection_and_ranks_names_before_descriptions(self):
        self.commands[:] = [
            ("/help", "Find model information"),
            ("/models", "List available engines"),
            ("/model <name>", "Choose an engine"),
        ]
        self.draft("/")
        self.press(Keys.Down, Keys.Down, *"model")
        self.assertEqual(self.ui._command_selection, 0)
        self.assertEqual([spec for spec, _ in self.ui._command_matches()],
                         ["/model <name>", "/models", "/help"])
        self.press(Keys.ControlM)
        self.assertEqual(self.submitted(), ["/model"])
        self.draft("/information")
        self.press(Keys.ControlM)
        self.assertEqual(self.submitted()[-1], "/help")

    async def test_catalog_changes_are_visible_without_rebuilding_ui(self):
        self.draft("/")
        self.commands.append(("/custom [input]", "A user-defined command"))
        self.press(*"custom", Keys.ControlI)
        self.assertEqual(self.ui._editor.text, "/custom ")
        self.assertEqual(self.submitted(), [])

    async def test_empty_results_keep_unknown_command_available_for_submission(self):
        self.draft("/unknown")
        self.assertEqual(self.ui._command_matches(), [])
        self.press(Keys.Down, Keys.PageDown, Keys.ControlI)
        self.assertEqual(self.ui._editor.text, "/unknown")
        self.press(Keys.ControlM)
        self.assertEqual(self.submitted(), ["/unknown"])

    async def test_page_navigation_scrolls_without_changing_draft(self):
        self.commands[:] = [(f"/item{index:02}", f"Action {index}") for index in range(30)]
        self.draft("/")
        self.press(Keys.PageDown)
        selected = self.ui._command_selection
        self.assertGreater(selected, 1)
        self.assertEqual(self.ui._editor.text, "/")
        rows = self.render()
        self.assertTrue(any(f"/item{selected:02}" in row for row in rows))
        self.assertTrue(any(f"{selected + 1}/30" in row for row in rows))
        self.press(Keys.PageUp)
        self.assertEqual(self.ui._command_selection, 0)
        self.press(Keys.Up)
        self.assertEqual(self.ui._command_selection, 0)


    async def test_busy_queue_off_preserves_accepted_command_for_later(self):
        self.ui._busy = True
        self.ui._queue_enabled = False
        self.draft("/mod")
        self.press(Keys.ControlM)
        self.assertEqual(self.ui._editor.text, "/model")
        self.assertFalse(self.ui._command_visible())
        self.assertEqual(self.submitted(), [])

    async def test_generic_choice_and_approval_do_not_use_browser_selection(self):
        self.draft("/")
        result = self.ui._open_modal("choose", "Choose model", [("engine", "Engine", "Available")])
        self.assertFalse(self.ui._command_visible())
        self.press(Keys.ControlM)
        self.assertEqual(result.result(), "engine")
        self.assertEqual(self.ui._editor.text, "/")
        result = self.ui._open_modal("ask", "Approve action")
        self.ui._answer.text = "/literal answer"
        self.press(Keys.ControlM)
        self.assertEqual(result.result(), "/literal answer")
        self.assertEqual(self.submitted(), [])

    async def test_narrow_short_browser_stays_below_visible_composer(self):
        self.commands[:] = [(f"/item{index:02}", "A long description " * 5) for index in range(20)]
        self.ui._busy = True
        self.ui._response = "Live output\n" * 20
        self.ui._detail_text = "Progress\n" * 20
        self.draft("/")
        for rows, columns in [(3, 12), (4, 16), (6, 20), (12, 24), (24, 80)]:
            with self.subTest(rows=rows, columns=columns):
                self.output.size = Size(rows=rows, columns=columns)
                self.press(Keys.PageDown)
                rendered = self.render()
                composer = next(index for index, row in enumerate(rendered) if row.startswith("> /"))
                selected = next(index for index, row in enumerate(rendered) if row.startswith("> /item"))
                self.assertGreater(selected, composer)
                self.assertEqual(self.ui._editor.text, "/")
                browser_rows = fragment_list_to_text(self.ui._command_text()).splitlines()
                self.assertLessEqual(len(browser_rows), self.ui._command_height())
                self.assertTrue(all(get_cwidth(row) < columns for row in browser_rows))

    async def test_infobar_height_lends_rows_to_browser_without_losing_selection_or_draft(self):
        self.commands[:] = [(f"/item{index:02}", "Action") for index in range(20)]
        self.draft("unfinished draft", cursor=4)
        self.press(Keys.ControlP)
        self.ui._context_fields.update(
            infobar_stats=("2,037,305 in / 3,837 out  subscription seat  ~89.3 tok/s"
                           "  ctx 16%  (claude-sonnet-5-5)"),
            context_used=32_000, context_window=200_000, agents_paused=True)
        for mode, notice in (("detail", None), ("detail", "1.10.0"),
                             ("progress", None), ("progress", "1.10.0")):
            self.ui._context_fields["infobar"] = mode
            self.ui._update_available = notice
            for rows, columns in ((3, 16), (4, 16), (5, 24), (6, 24), (12, 80), (12, 100)):
                with self.subTest(mode=mode, notice=notice, rows=rows, columns=columns):
                    self.output.size = Size(rows=rows, columns=columns)
                    self.press(Keys.PageDown)
                    selected = self.ui._command_selection
                    rendered = self.render()
                    self.assertTrue(any(row.startswith("> /") for row in rendered))
                    self.assertTrue(any(row.startswith(f"> /item{selected:02}") for row in rendered))
                    if notice and columns >= 80:
                        self.assertIn(notice, "\n".join(rendered))
                    budget = self.ui._command_budget()
                    bar_height = self.ui._infobar_height()
                    self.assertLessEqual(
                        len(fragment_list_to_text(self.ui._command_text()).splitlines()), budget)
                    self.ui._context_fields["show_token_stats"] = False
                    self.assertGreaterEqual(self.ui._command_budget(), budget + bar_height)
                    self.render()
                    self.assertEqual(self.ui._command_selection, selected)
                    self.ui._context_fields["show_token_stats"] = True
        self.press(Keys.Escape)
        self.assertEqual(self.ui._editor.text, "unfinished draft")
        self.assertEqual(self.ui._editor.buffer.cursor_position, 4)

    async def test_unicode_labels_align_and_untrusted_text_cannot_add_rows(self):
        self.output.size = Size(rows=24, columns=60)
        self.commands[:] = [
            ("/界", "first description"),
            ("/longer [arg]", "second description"),
            ("/danger", "third\n\t\x1b[31m\x9bunsafe"),
        ]
        self.draft("/")
        text = fragment_list_to_text(self.ui._command_text())
        rows = text.splitlines()
        first_column = get_cwidth(rows[0].split("first description")[0])
        second_column = get_cwidth(rows[1].split("second description")[0])
        self.assertEqual(first_column, second_column)
        self.assertEqual(len(rows), 4)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x9b", text)
        self.assertNotIn("\t", text)
        for width in (1, 2, 5, 12):
            self.output.size = Size(rows=24, columns=width)
            self.assertTrue(all(get_cwidth(row) < width for row in
                                fragment_list_to_text(self.ui._command_text()).splitlines()))


if __name__ == "__main__":
    unittest.main()
