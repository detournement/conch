"""Color contrast, environment suppression, and persisted theme selection."""

import os
import tempfile
import unittest
from unittest import mock


from conch import settings_registry, themes


CLASSES = {
    "", "prompt", "title", "value", "bar", "selected", "search", "muted",
    "column", "rule", "status", "current", "provider", "section", "assistant",
    "match", "role", "user", "running", "notice", "price", "cost", "ok",
    "done", "error", "failed", "warning", "cancelled", "tool",
}


def _luminance(color):
    channels = [int(color[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


class ThemeTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_effective_scheme_normalizes_and_falls_back_without_mutation(self):
        for raw in (None, "", "unknown", [], {}, 5):
            config = {"colorscheme": raw}
            with self.subTest(raw=raw):
                self.assertEqual(themes.current_scheme(config), "default")
                self.assertEqual(settings_registry.current_value(config, "colorscheme"), "default")
                self.assertEqual(config, {"colorscheme": raw})
                self.assertEqual(themes.style_for(raw).style_rules,
                                 themes.style_for("default").style_rules)
        self.assertEqual(themes.current_scheme({}), "default")
        self.assertEqual(themes.current_scheme({"colorscheme": " LIGHT "}), "light")

    def test_explicit_backgrounds_keep_text_and_accents_readable(self):
        for name in ("dark", "light", "dracula", "solarized"):
            style = themes.style_for(name)
            for cls in ("", "muted", "status", "current", "notice", "cost", "error", "warning"):
                with self.subTest(name=name, cls=cls):
                    attrs = style.get_attrs_for_style_str(f"class:{cls}" if cls else "")
                    first, second = sorted((_luminance(attrs.color), _luminance(attrs.bgcolor)))
                    self.assertGreaterEqual((second + 0.05) / (first + 0.05), 4.5)

    def test_environment_overrides_even_cached_colored_schemes(self):
        for name in themes.SCHEMES:
            themes.style_for(name)
        for environment in ({"NO_COLOR": "", "TERM": "xterm"}, {"TERM": "dumb"}):
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertTrue(themes.colors_disabled())
                for name in themes.SCHEMES:
                    self.assert_colorless(themes.style_for(name))
        self.assertFalse(themes.colors_disabled())
        self.assertTrue(themes.style_for("light").get_attrs_for_style_str("").bgcolor)

    def test_monochrome_keeps_selection_and_emphasis_without_environment(self):
        style = themes.style_for("monochrome")
        self.assert_colorless(style)
        self.assertTrue(style.get_attrs_for_style_str("class:selected").reverse)
        self.assertTrue(style.get_attrs_for_style_str("class:title").bold)
        self.assertTrue(style.get_attrs_for_style_str("class:match").underline)

    def assert_colorless(self, style):
        for cls in CLASSES:
            attrs = style.get_attrs_for_style_str(f"class:{cls}" if cls else "")
            self.assertFalse(attrs.color)
            self.assertFalse(attrs.bgcolor)

    def test_registry_rejects_invalid_choice_before_writing(self):
        config = {"colorscheme": "light"}
        with mock.patch("conch.config.set_config_values") as save:
            with self.assertRaisesRegex(ValueError, "one of:"):
                settings_registry.set_setting(config, "colorscheme", "not-a-theme")
            save.assert_not_called()
        self.assertEqual(config, {"colorscheme": "light"})

    def test_registry_persists_normalized_selection_and_reset(self):
        from conch.config import load_config

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
                config = {}
                other_session = {"colorscheme": "dark"}
                settings_registry.set_setting(config, "colorscheme", " LIGHT ")
                self.assertEqual(config["colorscheme"], "light")
                self.assertEqual(load_config()["colorscheme"], "light")
                self.assertEqual(other_session["colorscheme"], "dark")
                settings_registry.reset_setting(config, "colorscheme")
                self.assertEqual(load_config()["colorscheme"], "default")


if __name__ == "__main__":
    unittest.main()
