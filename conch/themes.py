"""Shared terminal colors for the composer, modal dialogs, and full-screen panels.

The default scheme keeps Conch's terminal-native background and existing accents.
Explicit light/dark schemes set both foreground and background; monochrome and
NO_COLOR/TERM=dumb retain emphasis and selection without specifying any colors.
"""

from __future__ import annotations

import os
from functools import lru_cache

from prompt_toolkit.styles import Style


SCHEMES = {
    "default": "Conch's original colors on your terminal background",
    "dark": "High-contrast dark background with cool accents",
    "light": "White background with dark, readable accents",
    "dracula": "Dracula's dark purple background and bright accents",
    "solarized": "Solarized dark with blue-green and warm accents",
    "monochrome": "Terminal-native text with emphasis, without colors",
}

# Palette entries describe meaning, not individual panels. Every panel consumes
# the same class mapping below, so an error or selection always looks the same.
_PALETTES = {
    "default": {
        "base": "", "muted": "#888888", "rule": "#555555",
        "accent": "#5fafff", "highlight": "#ffd75f", "success": "#87d787",
        "error": "#ff6666", "warning": "#ff8787", "status": "#88aaaa",
    },
    "dark": {
        "base": "fg:#eeeeee bg:#1c1c1c", "muted": "#aaaaaa", "rule": "#808080",
        "accent": "#87cfff", "highlight": "#ffdf87", "success": "#87d787",
        "error": "#ff8787", "warning": "#ffaf5f", "status": "#87d7d7",
    },
    "light": {
        "base": "fg:#202020 bg:#ffffff", "muted": "#595959", "rule": "#767676",
        "accent": "#005faf", "highlight": "#875f00", "success": "#005f00",
        "error": "#af0000", "warning": "#af0000", "status": "#005f5f",
    },
    "dracula": {
        "base": "fg:#f8f8f2 bg:#282a36", "muted": "#a5acc9", "rule": "#a5acc9",
        "accent": "#bd93f9", "highlight": "#f1fa8c", "success": "#50fa7b",
        "error": "#ff5555", "warning": "#ffb86c", "status": "#8be9fd",
    },
    "solarized": {
        "base": "fg:#93a1a1 bg:#002b36", "muted": "#839496", "rule": "#839496",
        "accent": "#2aa8dc", "highlight": "#b58900", "success": "#859900",
        "error": "#ef6464", "warning": "#d79432", "status": "#2aa198",
    },
}


def colors_disabled() -> bool:
    """Whether the environment requests color-free terminal output."""
    return "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb"


def current_scheme(config: dict) -> str:
    """Return this session's valid configured scheme, or the default."""
    name = config.get("colorscheme")
    if not isinstance(name, str):
        return "default"
    name = name.strip().lower()
    return name if name in SCHEMES else "default"


def style_for(name: str) -> Style:
    """Resolve a scheme without changing config; environment overrides win."""
    name = current_scheme({"colorscheme": name})
    return _style_for("monochrome" if colors_disabled() else name)


@lru_cache(maxsize=len(SCHEMES))
def _style_for(name: str) -> Style:
    palette = _PALETTES.get(name, {})

    def semantic(role: str, emphasis: str = "") -> str:
        return " ".join(part for part in (emphasis, palette.get(role, "")) if part)

    return Style.from_dict({
        "": palette.get("base", ""),
        "prompt": "bold",
        "title": "bold",
        "value": "bold",
        "bar": "reverse bold",
        "selected": "reverse",
        "search": "",
        "muted": semantic("muted"),
        "column": semantic("muted", "bold"),
        "rule": semantic("rule"),
        "status": semantic("status"),
        "current": semantic("accent", "bold"),
        "provider": semantic("accent", "bold"),
        "section": semantic("accent", "bold"),
        "assistant": semantic("accent", "bold"),
        "match": semantic("highlight", "bold underline" if not palette else "bold"),
        "role": semantic("highlight", "bold"),
        "user": semantic("highlight", "bold"),
        "running": semantic("highlight", "bold"),
        "notice": semantic("highlight"),
        "price": semantic("success"),
        "cost": semantic("success"),
        "ok": semantic("success"),
        "done": semantic("success"),
        "error": semantic("error"),
        "failed": semantic("error"),
        "warning": semantic("warning", "bold"),
        "cancelled": semantic("muted"),
        "tool": semantic("muted"),
    })
