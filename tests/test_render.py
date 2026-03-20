"""Tests for conch.render — syntax highlighting and StreamPrinter."""

import io
import sys
import unittest
from unittest.mock import patch

from conch.render import StreamPrinter, highlight, _format_line, _highlight_code


class TestFormatLine(unittest.TestCase):
    def test_header_h1(self):
        result = _format_line("# Hello")
        self.assertIn("Hello", result)
        self.assertIn("\033[1;33m", result)

    def test_header_h2(self):
        result = _format_line("## Sub")
        self.assertIn("Sub", result)
        self.assertIn("\033[1;34m", result)

    def test_header_h3(self):
        result = _format_line("### Third")
        self.assertIn("Third", result)
        self.assertIn("\033[1;35m", result)

    def test_bold(self):
        result = _format_line("some **bold** text")
        self.assertIn("\033[1m", result)
        self.assertIn("bold", result)
        self.assertNotIn("**", result)

    def test_inline_code(self):
        result = _format_line("use `foo()` here")
        self.assertIn("\033[36m", result)
        self.assertIn("foo()", result)
        self.assertNotIn("`", result)

    def test_bullet_list(self):
        result = _format_line("- item one")
        self.assertIn("\u2022", result)

    def test_numbered_list(self):
        result = _format_line("1. first")
        self.assertIn("1.", result)

    def test_plain_text_unchanged(self):
        result = _format_line("just normal text")
        self.assertEqual(result, "just normal text")

    def test_horizontal_rule(self):
        result = _format_line("---")
        self.assertIn("\u2500", result)


class TestHighlight(unittest.TestCase):
    def test_returns_empty_unchanged(self):
        self.assertEqual(highlight(""), "")

    def test_code_block_delimiters(self):
        text = "before\n```python\nx = 1\n```\nafter"
        with patch("conch.render.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = True
            result = highlight(text)
        self.assertIn("\u2500", result)
        self.assertNotIn("```", result)

    def test_unclosed_code_block(self):
        text = "start\n```python\nx = 1"
        with patch("conch.render.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = True
            result = highlight(text)
        self.assertIn("x = 1", result)


class TestHighlightCode(unittest.TestCase):
    def test_known_language(self):
        result = _highlight_code("x = 1", "python")
        self.assertIn("x", result)

    def test_unknown_language_fallback(self):
        result = _highlight_code("hello", "not_a_real_lang")
        self.assertIn("hello", result)

    def test_empty_lang(self):
        result = _highlight_code("plain", "")
        self.assertIn("plain", result)


class TestStreamPrinter(unittest.TestCase):
    def setUp(self):
        self._real_stdout = sys.stdout
        self._buf = io.StringIO()
        sys.stdout = self._buf

    def tearDown(self):
        sys.stdout = self._real_stdout

    def test_full_line_emitted_on_newline(self):
        p = StreamPrinter()
        p.feed("Hello world\n")
        output = self._buf.getvalue()
        self.assertIn("Hello world", output)

    def test_partial_line_visible_immediately(self):
        p = StreamPrinter()
        p.feed("Hello")
        output = self._buf.getvalue()
        self.assertIn("Hello", output)

    def test_partial_cleared_on_newline(self):
        p = StreamPrinter()
        p.feed("He")
        p.feed("llo\n")
        output = self._buf.getvalue()
        self.assertIn("\033[2K", output)

    def test_flush_returns_full_text(self):
        p = StreamPrinter()
        p.feed("one\ntwo\n")
        p.feed("three")
        result = p.flush()
        self.assertEqual(result, "one\ntwo\nthree")

    def test_code_block_buffered(self):
        p = StreamPrinter()
        p.feed("```python\nx = 1\n```\n")
        output = self._buf.getvalue()
        self.assertIn("\u2500", output)

    def test_reset_clears_state(self):
        p = StreamPrinter()
        p.feed("data")
        p.reset()
        self.assertEqual(p._full_text, "")
        self.assertEqual(p._line_buf, "")
        self.assertEqual(p._partial_written, 0)


if __name__ == "__main__":
    unittest.main()
