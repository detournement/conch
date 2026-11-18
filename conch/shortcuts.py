"""The keyboard reference in the command menu. Key names describe the terminal,
not a new binding: macOS Option-as-Meta and Alt both send an Escape prefix.
"""
from __future__ import annotations

import sys


def meta_key(key: str) -> str:
    return ("Option+" if sys.platform == "darwin" else "Alt+") + key


def shortcut_text(query: str = "") -> str:
    """Group Conch's bindings by where they work; search keys, actions or sections."""
    groups = {
        "Chat": [
            ("Enter", "Send; during a turn, steer at the next safe boundary"),
            (meta_key("Q"), "Queue a follow-up instead of steering"),
            (meta_key("R"), "Hide or show reasoning and progress"),
            (meta_key("Enter"), "Insert a newline"),
            ("Ctrl+Q", "Open pending messages"),
            ("/ or Ctrl+P", "Browse commands"),
            ("Ctrl+C", "Stop work; when idle, cancel input (twice to exit)"),
            ("Ctrl+D", "Exit with an empty draft; otherwise delete at the cursor"),
        ],
        "Command browser": [
            ("Type", "Filter commands"),
            ("Up / Down", "Select a command; PgUp / PgDn move a page"),
            ("Enter / Tab", "Run the command / fill it without running"),
            ("Esc", "Dismiss; restore the draft when opened with Ctrl+P"),
        ],
        "Pending messages": [
            ("Up / Down", "Select pending guidance or a follow-up"),
            ("Enter", "Edit the selection; Enter again saves it"),
            (meta_key("Enter"), "Insert a newline while editing"),
            ("Delete / Backspace", "Remove the selected message"),
            ("Esc", "Cancel the edit or close the list; Ctrl+Q closes either"),
        ],
        "Model picker": [
            ("Type", "Filter models"),
            ("Up / Down", "Move the highlight; Ctrl+N also moves down"),
            ("PgUp / PgDn", "Move a page"),
            ("Enter", "Set the highlighted primary model and close (queue if busy)"),
            ("Ctrl+P", "Set the primary model and stay in the picker"),
            ("Ctrl+F / Ctrl+T", "Toggle the fan-out / side-task model"),
            ("Ctrl+E / Ctrl+W", "Cycle reasoning effort / context window"),
            ("Ctrl+A / Ctrl+X", "Add a model / remove an added model"),
            ("Ctrl+R", "Refresh the model list"),
            ("Enter / Esc", "In an add or role picker, confirm / cancel the choice"),
            ("Esc / Ctrl+C", "Clear the filter then exit / exit directly"),
        ],
        "Conversations": [
            ("Up / Down", "Select (also k / j or Ctrl+P / Ctrl+N); PgUp / PgDn page"),
            ("Home / End", "First / last conversation (also g / G)"),
            ("/", "Search; Enter keeps the filter, Esc clears it"),
            ("Enter", "Resume the selected conversation"),
            ("v or Right", "Read the transcript; Left, q or Esc returns"),
            ("b / Space", "Page up / down in the transcript; arrows, k / j and g / G scroll"),
            ("s", "Change sort order"),
            ("r / f / e", "Rename / fork / export the selected conversation"),
            ("d or Delete", "Request deletion; y confirms, any other key cancels"),
            ("q / Esc / Ctrl+C", "Leave the page (Esc clears an active filter first)"),
        ],
        "Subagents": [
            ("Up / Down", "Move through settings (also k / j or Ctrl+P / Ctrl+N)"),
            ("Enter", "Edit a value or choose its model; Enter saves, Esc cancels"),
            ("Delete / Backspace", "Reset a setting to its default"),
            ("a / x", "Auto-select the fan-out model / stop running fan-out"),
            ("Esc / q / Ctrl+C", "Close the panel"),
        ],
        "Menus and prompts": [
            ("Type, Up / Down", "Filter and navigate choices"),
            ("Enter / Esc", "Confirm or cancel; Ctrl+C and Ctrl+D also dismiss"),
            ("?", "Show choices at a shell approval prompt"),
            ("y / n / e", "Approve / decline / edit at the shell approval prompt"),
            ("a / A", "Allow the command prefix / enable agent mode at shell approval"),
            (". or Ctrl+D", "Finish a /paste block; Ctrl+C discards it"),
        ],
        "Shell integration (after conch-setup)": [
            ("Ctrl+Space", "Ask from Bash or Zsh"),
            ("Esc Esc or Ctrl+G", "Ask from Zsh"),
            ("Ctrl+X then Ctrl+G", "Start chat from Zsh"),
        ],
    }
    words = query.casefold().split()
    sections = []
    for heading, bindings in groups.items():
        lines = [f"  {key:<20} {action}" for key, action in bindings
                 if all(word in f"{heading} {key} {action}".casefold() for word in words)]
        if lines:
            sections.append("\n".join([heading, *lines]))
    return "\n\n".join(sections) or "No matching shortcuts."


def meta_tip() -> str:
    if sys.platform == "darwin":
        return "If Option types a symbol, enable Option as Meta in your terminal, or press Esc then the key."
    return "For Alt shortcuts you can also press Esc then the key."


async def browse_shortcuts(config: dict) -> None:
    """Read-only, including during a turn: no reference row executes a command."""
    from prompt_toolkit import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout
    from prompt_toolkit.output import ColorDepth
    from prompt_toolkit.widgets import Label, TextArea

    from .themes import colors_disabled, current_scheme, style_for

    query = TextArea(multiline=False, prompt="Filter: ")
    body = TextArea(text=shortcut_text(), read_only=True, scrollbar=True, focusable=False)
    query.buffer.on_text_changed += lambda _: setattr(body, "text", shortcut_text(query.text))
    keys = KeyBindings()

    @keys.add("up")
    @keys.add("down")
    @keys.add("pageup")
    @keys.add("pagedown")
    def scroll(event):
        key = event.key_sequence[-1].key
        page = body.window.render_info.window_height if body.window.render_info else 1
        count = max(1, page - 1) if key in ("pageup", "pagedown") else 1
        (body.buffer.cursor_up if key in ("up", "pageup") else body.buffer.cursor_down)(count)

    @keys.add("enter", eager=True)
    @keys.add("escape", eager=True)
    @keys.add("c-c", eager=True)
    @keys.add("c-d", eager=True)
    def close(event):
        event.app.exit()

    scheme = current_scheme(config)
    root = HSplit([Label(" Keyboard shortcuts", style="class:bar"), query, body,
                   Label(meta_tip(), style="class:muted"),
                   Label("Up/Down or PgUp/PgDn scroll · Type to filter · Esc closes", style="class:muted")])
    app = Application(layout=Layout(root, focused_element=query), key_bindings=keys,
                      full_screen=True, style=style_for(scheme),
                      color_depth=ColorDepth.DEPTH_1_BIT if colors_disabled() or scheme == "monochrome" else None)
    app.ttimeoutlen = 0.05
    await app.run_async(handle_sigint=False)
