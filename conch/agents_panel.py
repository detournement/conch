"""The subagent panel (/agents, /subagents): how subagents are set up and
what they are doing, on one full-screen page.

Settings: the fan-out and delegate_task settings from the settings
registry. Enter edits the selected one (numbers inline; models through the
model picker), ``a`` sets the fan-out model to auto, Delete resets to the
default. Every change is validated, saved, and applied by
:mod:`conch.settings_registry`.

Skill subagents: the skills delegate_task can run (``skill=<name>``), with
their model preference, tools, and round budget.

Activity: fan-out workers and delegate_task runs of this session, live;
``x`` stops running fan-out workers.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, List, Optional

from prompt_toolkit import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.output import ColorDepth, create_output
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from . import settings_registry, subagent_activity
from .convo_browser import Line, _fit, _fit_hints, _fit_right, _Pane

GROUP = "subagents"

_STYLE = {
    "bar": "reverse bold", "title": "bold", "column": "bold #888888",
    "muted": "#888888", "selected": "reverse", "section": "bold #5fafff",
    "value": "bold", "prompt": "bold", "notice": "#ffd75f",
    "running": "bold #ffd75f", "done": "#87d787", "failed": "#ff6666",
    "cancelled": "#888888",
}

_BLANK_VALUE = {
    "fanout_model": "session model",
    "subagent_model": "session model",
    "side_task_model": "chat model",
}

_MARKS = {"running": "●", "done": "✓", "failed": "✗", "cancelled": "–"}

PICK_MODEL = "pick_model"


def group_settings() -> List[settings_registry.Setting]:
    return [setting for setting in settings_registry.SETTINGS if setting.group == GROUP]


def shown_value(config: dict, setting: settings_registry.Setting) -> str:
    return settings_registry.current_value(config, setting.key) or _BLANK_VALUE.get(
        setting.key, "unset")


def shown_default(setting: settings_registry.Setting) -> str:
    return settings_registry.default_of(setting) or _BLANK_VALUE.get(setting.key, "unset")


def _rules(setting: settings_registry.Setting) -> str:
    if setting.kind == "int":
        low = "" if setting.minimum is None else f"{setting.minimum:g}"
        high = "" if setting.maximum is None else f"{setting.maximum:g}"
        return f"a whole number {low}–{high}" if low and high else "a whole number"
    if setting.kind == "model":
        extra = ", or auto" if setting.key == "fanout_model" else ""
        return f"a model from the picker{extra}"
    return setting.kind


def load_skill_rows() -> List[dict]:
    from .skills import load_skills

    try:
        return sorted(load_skills().values(), key=lambda skill: skill["name"])
    except Exception:  # an unreadable skills dir must not close the panel
        return []


def skill_model(skill: dict) -> str:
    provider, model = skill.get("provider") or "", skill.get("model") or ""
    if provider and model:
        return f"{provider}/{model}"
    return model or (f"{provider} (its default)" if provider else "subagent model")


def skill_tools(skill: dict) -> str:
    tools = skill.get("tools")
    return "all tools" if tools is None else (", ".join(tools) or "no tools")


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds // 60 % 60:02d}m"


def describe_text(config: dict) -> List[str]:
    """The panel as plain lines, for a terminal that cannot show it."""
    from .skills import skill_one_liner

    lines = ["Subagent settings:"] + ["  " + line for line in
                                      settings_registry.describe(config, GROUP)]
    lines.append("Skill subagents (delegate_task with skill=<name>):")
    skills = load_skill_rows()
    if not skills:
        lines.append("  (none)")
    for skill in skills:
        rounds = skill.get("rounds") or "default"
        lines.append(f"  - {skill['name']} — {skill_model(skill)} · {skill_tools(skill)}"
                     f" · rounds {rounds} — {skill_one_liner(skill)}")
    runs = subagent_activity.snapshot()
    lines.append("Activity:")
    if not runs:
        lines.append("  (no subagent has run in this session)")
    for run in runs:
        lines.append(f"  {_MARKS.get(run.status, ' ')} {run.status:<9} {run.kind:<8}"
                     f" {_duration(run.seconds()):>7}  {run.model}  {run.label}")
    return lines


def run_agents_panel(config: dict, *, stop_fanout: Optional[Callable[[], None]] = None,
                     select: Optional[str] = None, notice: str = "",
                     input=None, output=None) -> Optional[tuple]:
    """Run the panel on the terminal. ``(PICK_MODEL, key)`` when a model
    setting is to be chosen in the model picker, else None."""
    panel = AgentsPanel(config, stop_fanout=stop_fanout, select=select, notice=notice)
    if input is None:
        input = create_input(sys.stdin)
    if output is None:
        output = create_output(stdout=sys.__stdout__ or sys.stdout)
    panel.build(input=input, output=output).run()
    return panel.result


async def run_agents_panel_during_turn(config: dict, *,
                                       stop_fanout: Optional[Callable[[], None]] = None,
                                       select: Optional[str] = None,
                                       notice: str = "") -> Optional[tuple]:
    """The panel on the live shell's own event loop while a turn runs."""
    panel = AgentsPanel(config, stop_fanout=stop_fanout, busy=True, select=select,
                        notice=notice)
    await panel.build().run_async(handle_sigint=False)
    return panel.result


def admin_loop(config: dict, provider: str, model: str,
               run_panel: Callable[[Optional[str], str], Optional[tuple]],
               pick_model: Callable[..., object]) -> None:
    """Panel, then the model picker when the panel asks for it, then the
    panel again, until the panel is closed.

    *run_panel(select, notice)* shows the panel with *select* highlighted
    and *notice* on its status line; *pick_model(config, provider, model,
    purpose=key)* returns a model entry (``.label`` is provider/model) or None."""
    select, notice = None, ""
    while True:
        outcome = run_panel(select, notice)
        if not outcome or outcome[0] != PICK_MODEL:
            return
        key = outcome[1]
        select, notice = key, ""
        entry = pick_model(config, provider, model, purpose=key)
        if entry is None:
            notice = f"{key} unchanged."
            continue
        try:
            notice = settings_registry.set_setting(config, key, entry.label)
        except ValueError as exc:
            notice = f"{key}: {exc} — unchanged."


class AgentsPanel:
    """State, rendering, and keys for one run of the panel."""

    def __init__(self, config: dict, *, stop_fanout: Optional[Callable[[], None]] = None,
                 busy: bool = False, select: Optional[str] = None, notice: str = ""):
        self.config = config
        self.stop_fanout = stop_fanout
        self.busy = busy
        self.settings = group_settings()
        keys = [setting.key for setting in self.settings]
        self.index = keys.index(select) if select in keys else 0
        self.skills = load_skill_rows()
        self.mode = "list"  # list | edit
        self.notice = notice
        self.result: Optional[tuple] = None
        self.app: Optional[Application] = None
        self._body: Optional[Window] = None
        self.edit_input = TextArea(multiline=False, prompt=lambda: [
            ("class:prompt", f" {self.selected.key} ({_rules(self.selected)};"
                             " blank = default): ")])

    # --- selection and actions -------------------------------------------------

    @property
    def selected(self) -> settings_registry.Setting:
        return self.settings[self.index]

    def move(self, delta: int):
        self.index = max(0, min(len(self.settings) - 1, self.index + delta))

    def _exit(self):
        if self.app is not None and self.app.is_running:
            self.app.exit()

    def edit(self):
        """Enter: a model setting goes to the model picker (the panel
        closes and says which); a number opens the inline input."""
        setting = self.selected
        if setting.kind == "model":
            self.result = (PICK_MODEL, setting.key)
            self._exit()
            return
        self.mode = "edit"
        self.edit_input.text = str(self.config.get(setting.key) or "")
        self.edit_input.buffer.cursor_position = len(self.edit_input.text)
        if self.app is not None:
            self.app.layout.focus(self.edit_input)

    def finish_edit(self, save: bool):
        """Enter saves through the registry (a bad value keeps the old one
        and says why, with the input still open); Esc cancels."""
        setting = self.selected
        if save:
            try:
                self.notice = settings_registry.set_setting(
                    self.config, setting.key, self.edit_input.text)
            except ValueError as exc:
                self.notice = f"{setting.key}: {exc} — unchanged."
                return
        self.mode = "list"
        if self.app is not None and self._body is not None:
            self.app.layout.focus(self._body)

    def set_auto(self):
        if self.selected.key != "fanout_model":
            self.notice = "a (auto) is only for fanout_model."
            return
        self.notice = settings_registry.set_setting(self.config, "fanout_model", "auto")

    def reset(self):
        self.notice = settings_registry.reset_setting(self.config, self.selected.key)

    def stop(self):
        running = [run for run in subagent_activity.snapshot()
                   if run.running and run.kind == "fan-out"]
        if self.stop_fanout is None:
            self.notice = "Nothing can be stopped here: no turn is running."
        elif not running:
            self.notice = "No fan-out worker is running."
        else:
            self.stop_fanout()
            self.notice = (f"Stopping {len(running)} fan-out worker"
                           f"{'s' if len(running) != 1 else ''}…")

    # --- rendering ---------------------------------------------------------------

    def _render_header(self, width: int, height: int) -> List[List[Line]]:
        values = {setting.key: shown_value(self.config, setting) for setting in self.settings}
        first = (f" Subagents · fan-out {values.get('fanout_model', '')}"
                 f" · delegate {values.get('subagent_model', '')}"
                 f" · side tasks {values.get('side_task_model', '')}")
        runs = subagent_activity.snapshot()
        running = sum(1 for run in runs if run.running)
        second = [f"{running} running", f"{len(runs) - running} finished",
                  f"{len(self.skills)} skill{'s' if len(self.skills) != 1 else ''}"]
        if self.busy:
            second.append("a turn is running: changes apply to the next subagent call")
        return [[("class:bar", _fit(first, width))],
                [("class:muted", _fit(" " + " · ".join(second), width))]]

    def _setting_lines(self, width: int) -> List[List[Line]]:
        key_w = max(len(setting.key) for setting in self.settings) + 2
        value_w = max(14, min(32, max(len(shown_value(self.config, s)) for s in self.settings)))
        default_w = max(14, min(20, max(len(shown_default(s)) for s in self.settings)))
        desc_w = max(0, width - 3 - key_w - value_w - 2 - default_w - 2)
        lines: List[List[Line]] = [[("class:section", _fit(" Settings", width))]]
        lines.append([("class:column", _fit(
            "   " + _fit("setting", key_w) + _fit("value", value_w) + "  "
            + _fit("default", default_w) + "  " + "what it does", width))])
        for index, setting in enumerate(self.settings):
            selected = index == self.index
            plain = "class:selected" if selected else ""
            muted = plain or "class:muted"
            lines.append([
                (plain, " ▸ " if selected else "   "),
                (plain, _fit(setting.key, key_w)),
                (plain or "class:value", _fit(shown_value(self.config, setting), value_w)),
                (plain, "  "), (muted, _fit(shown_default(setting), default_w)),
                (plain, "  "), (muted, _fit(setting.description, desc_w)),
            ])
        return lines

    def _skill_lines(self, width: int, room: int) -> List[List[Line]]:
        from .skills import skill_one_liner, skills_dir

        lines: List[List[Line]] = [[("class:section", _fit(
            " Skill subagents · delegate_task with skill=<name>", width))]]
        if not self.skills:
            lines.append([("class:muted", _fit(
                f"   None yet: save one with skill_manage or add a .md file to {skills_dir()}",
                width))])
            return lines
        name_w = max(10, min(20, max(len(skill["name"]) for skill in self.skills) + 2))
        model_w, tools_w, rounds_w = 24, 22, 10
        desc_w = max(0, width - 3 - name_w - model_w - 2 - tools_w - 2 - rounds_w - 2)
        shown = self.skills
        if len(shown) > max(1, room - 1):
            shown = shown[:max(1, room - 2)]
        for skill in shown:
            rounds = f"{skill['rounds']} rounds" if skill.get("rounds") else "default"
            lines.append([
                ("", "   "), ("class:title", _fit(skill["name"], name_w)),
                ("", _fit(skill_model(skill), model_w)), ("", "  "),
                ("class:muted", _fit(skill_tools(skill), tools_w)), ("", "  "),
                ("class:muted", _fit(rounds, rounds_w)), ("", "  "),
                ("class:muted", _fit(skill_one_liner(skill), desc_w)),
            ])
        if len(shown) < len(self.skills):
            lines.append([("class:muted", _fit(
                f"   … {len(self.skills) - len(shown)} more (/skills lists them all)", width))])
        return lines

    def _activity_lines(self, width: int, room: int) -> List[List[Line]]:
        runs = subagent_activity.snapshot()
        lines: List[List[Line]] = [[("class:section", _fit(" Activity", width))]]
        if not runs:
            lines.append([("class:muted", _fit(
                "   No subagent has run in this session.", width))])
            return lines
        now = time.time()
        status_w, kind_w, time_w, model_w = 10, 9, 8, 28
        label_w = max(0, width - 5 - status_w - kind_w - time_w - 2 - model_w - 2)
        for run in runs[:max(1, room - 1)]:
            style = f"class:{run.status}"
            text = run.label + (f" — {run.summary}" if run.summary and not run.running
                                and run.status != "done" else "")
            lines.append([
                ("", "   "), (style, _MARKS.get(run.status, " ") + " "),
                (style, _fit(run.status, status_w)), ("", _fit(run.kind, kind_w)),
                ("class:muted", _fit_right(_duration(run.seconds(now)), time_w)), ("", "  "),
                ("class:muted", _fit(run.model, model_w)), ("", "  "),
                ("", _fit(text, label_w)),
            ])
        return lines

    def _render_body(self, width: int, height: int) -> List[List[Line]]:
        settings = self._setting_lines(width)
        runs = len(subagent_activity.snapshot())
        # Activity keeps at least its header plus a few rows; skills get the rest.
        activity_room = max(2, min(runs + 1, height - len(settings) - 6))
        skills_room = max(2, height - len(settings) - activity_room - 2)
        lines = settings + [[]] + self._skill_lines(width, skills_room) + [[]]
        lines += self._activity_lines(width, max(2, height - len(lines)))
        return lines[:height]

    def _render_details(self, width: int, height: int) -> List[List[Line]]:
        setting = self.selected
        first = f" {setting.key} = {shown_value(self.config, setting)}"
        second = (f" {setting.description}. Takes {_rules(setting)};"
                  f" default {shown_default(setting)}.")
        return [[("class:title", _fit(first, width))], [("class:muted", _fit(second, width))]]

    def _render_footer(self, width: int, height: int) -> List[List[Line]]:
        if self.notice:
            return [[("class:notice", _fit(" " + self.notice, width))]]
        if self.mode == "edit":
            hints = ["Enter saves", "Esc cancels", "blank = default"]
        else:
            hints = ["↑↓ select", "Enter edit" if self.selected.kind != "model" else
                     "Enter pick model", "Del reset", "a fan-out auto",
                     "x stop fan-out", "Esc quit"]
        return [[("class:muted", " " + _fit_hints(hints, width - 1))]]

    # --- application ---------------------------------------------------------------

    def build(self, input=None, output=None) -> Application:
        self._body = Window(_Pane(self._render_body, focusable=True), wrap_lines=False,
                            always_hide_cursor=True)
        root = HSplit([
            Window(_Pane(self._render_header), height=2),
            self._body,
            Window(_Pane(self._render_details), height=2),
            ConditionalContainer(self.edit_input, Condition(lambda: self.mode == "edit")),
            Window(_Pane(self._render_footer), height=1),
        ])
        no_color = "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb"
        self.app = Application(
            layout=Layout(root, focused_element=self._body),
            key_bindings=self._key_bindings(), full_screen=True, mouse_support=False,
            enable_page_navigation_bindings=False, input=input, output=output,
            color_depth=ColorDepth.DEPTH_1_BIT if no_color else None,
            style=Style.from_dict(_STYLE), refresh_interval=0.5,
        )
        self.app.ttimeoutlen = 0.05
        self.app.key_processor.before_key_press += self._clear_notice
        return self.app

    def _clear_notice(self, _sender=None):
        self.notice = ""

    def _key_bindings(self) -> KeyBindings:
        keys = KeyBindings()
        listing = Condition(lambda: self.mode == "list")
        editing = Condition(lambda: self.mode == "edit")

        @keys.add("c-c", eager=True)
        def _quit(event):
            event.app.exit()

        @keys.add("escape", filter=listing, eager=True)
        @keys.add("q", filter=listing)
        def _close(event):
            event.app.exit()

        @keys.add("up", filter=listing)
        @keys.add("c-p", filter=listing)
        @keys.add("k", filter=listing)
        def _up(event):
            self.move(-1)

        @keys.add("down", filter=listing)
        @keys.add("c-n", filter=listing)
        @keys.add("j", filter=listing)
        def _down(event):
            self.move(1)

        @keys.add("enter", filter=listing)
        def _edit(event):
            self.edit()

        @keys.add("a", filter=listing)
        def _auto(event):
            self.set_auto()

        @keys.add("delete", filter=listing)
        @keys.add("backspace", filter=listing)
        def _reset(event):
            self.reset()

        @keys.add("x", filter=listing)
        def _stop(event):
            self.stop()

        @keys.add("enter", filter=editing)
        def _save(event):
            self.finish_edit(save=True)

        @keys.add("escape", filter=editing, eager=True)
        def _cancel(event):
            self.finish_edit(save=False)

        return keys
