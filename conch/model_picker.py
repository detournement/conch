"""The model picker (/models, or /model with no name): every model conch can
run in one full-screen list, grouped by provider, filtered as you type.

Each row shows context window, price per million tokens, tier, and which
roles it holds: ● the chat model, F fan-out workers, T side tasks (titles,
summaries, compaction). Built-in catalogs show at once; local servers, the
llama-idx registry, seat sign-in, and Codex's native catalog fill in as they answer.

Enter makes the selection the chat model and closes the picker; Ctrl+P
sets it without closing (during a running turn, either queues the switch).
Ctrl+F and Ctrl+T assign the worker and side-task roles (saved to config);
Ctrl+E steps the model's reasoning effort and Ctrl+W its context
window where the provider has them (model_tuning); Ctrl+A adds a model the
catalog lacks, Ctrl+X removes one you added, Ctrl+R asks the servers again.
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
from typing import Callable, Dict, List, Optional, Tuple

from prompt_toolkit import Application
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.filters import Condition
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.output import ColorDepth, create_output
from prompt_toolkit.widgets import TextArea

from .convo_browser import Line, _compact, _fit, _fit_hints, _fit_right, _highlight, _Pane
from .model_catalog import (
    PROVIDER_ORDER,
    REGISTRY,
    ModelEntry,
    ProviderStatus,
    add_model,
    catalog_entries,
    live_sources,
    parse_new_model,
    remove_model,
    static_status,
)
from .themes import colors_disabled, current_scheme, style_for



def pick_model(config: dict, provider: str, model: str, *, purpose: Optional[str] = None,
               on_primary: Optional[Callable[[ModelEntry], bool]] = None,
               input=None, output=None) -> Optional[ModelEntry]:
    """Run the picker; return Enter's choice when no primary callback is supplied.
    *on_primary* validates/applies primary changes and returns whether they worked.
    *purpose* names a non-primary setting being chosen (e.g. fanout_model)."""
    picker = ModelPicker(config, provider, model, purpose=purpose, on_primary=on_primary)
    if input is None:
        input = create_input(sys.stdin)
    if output is None:
        output = create_output(stdout=sys.__stdout__ or sys.stdout)
    picker.build(input=input, output=output).run(pre_run=picker.start_live)
    return picker.result


async def pick_model_during_turn(config: dict, provider: str, model: str, *,
                                 purpose: Optional[str] = None,
                                 on_primary: Optional[Callable[[ModelEntry], bool]] = None
                                 ) -> Optional[ModelEntry]:
    """The picker on the live shell's own event loop while a turn runs: the
    chat-model pick is queued for when the turn ends; role changes save now."""
    picker = ModelPicker(config, provider, model, busy=True, purpose=purpose,
                         on_primary=on_primary)
    await picker.build().run_async(pre_run=picker.start_live, handle_sigint=False)
    return picker.result


def _price_text(entry: ModelEntry) -> str:
    if entry.price is not None:
        cost_in, cost_out = entry.price
        if not cost_in and not cost_out:
            return "free"
        return f"${cost_in:g}/${cost_out:g}"
    return {"seat": "seat", "local": "local"}.get(entry.billing, "—")


class ModelPicker:
    """State, rendering, and keys for one run of the picker."""

    def __init__(self, config: dict, provider: str, model: str, busy: bool = False,
                 purpose: Optional[str] = None,
                 on_primary: Optional[Callable[[ModelEntry], bool]] = None):
        self.config = config
        self.current = ((provider or "").lower(), model or "")
        self.busy = busy
        # The setting this pick fills when it is not the chat model.
        self.purpose = purpose
        self.on_primary = on_primary
        self.entries: List[ModelEntry] = catalog_entries(config)
        self.statuses: Dict[str, ProviderStatus] = static_status(config)
        self._pending = 0  # live sources still to answer
        self.mode = "list"  # list | add
        self.notice = ""
        self.result: Optional[ModelEntry] = None
        self.app: Optional[Application] = None
        self.rows: List[Tuple[str, object]] = []
        self.selected_key: Optional[Tuple[str, str]] = self.current
        self._top = 0
        self._height = 10
        self._pattern: Optional[re.Pattern] = None
        self.query = TextArea(multiline=False, prompt=[("class:prompt", " Filter: ")])
        self.query.buffer.on_text_changed += lambda _: self._refresh_rows(jump=True)
        self.add_input = TextArea(multiline=False,
                                  prompt=[("class:prompt", " Add model (provider/model-id): ")])
        self._refresh_rows()

    # --- data ---------------------------------------------------------------

    def start_live(self):
        """Ask seat CLIs, local servers, and the registry in the background,
        each on its own thread: rows and statuses land as each one answers,
        so an unreachable host never holds up the rest."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        sources = live_sources(self.config)
        self._pending += len(sources)

        def work(source):
            result = source(self.config)
            if loop is None:
                self.apply_source(*result)
                return
            try:
                loop.call_soon_threadsafe(self.apply_source, *result)
            except RuntimeError:
                pass  # the picker closed first

        for source in sources:
            threading.Thread(target=work, args=(source,), name="conch-models",
                             daemon=True).start()

    @property
    def loading(self) -> bool:
        return self._pending > 0

    def apply_source(self, covered, entries: List[ModelEntry],
                     statuses: Dict[str, ProviderStatus]):
        """One live source answered: its providers' rows replace theirs."""
        self.entries = [entry for entry in self.entries if entry.provider not in covered] + entries
        self.statuses.update(statuses)
        self._pending = max(0, self._pending - 1)
        self._refresh_rows()
        if self.app is not None:
            self.app.invalidate()

    def apply_live(self, entries: List[ModelEntry], statuses: Dict[str, ProviderStatus]):
        """Several sources' results at once. Only server-listed providers'
        rows are replaced; catalog providers just get their status."""
        self._pending += 1
        listed = {entry.provider for entry in entries}
        listed |= set(statuses) & {"ollama", "custom", REGISTRY}
        self.apply_source(tuple(listed), entries, statuses)

    def refresh(self):
        for provider, status in self.statuses.items():
            if status.available is None or provider in ("ollama", "custom", REGISTRY):
                self.statuses[provider] = ProviderStatus(None, "asking again…")
        self.start_live()
        self.notice = "Refreshing local servers, the registry, and seats…"

    def _words(self) -> List[str]:
        return [word for word in self.query.text.casefold().split() if word]

    def _refresh_rows(self, jump: bool = False):
        words = self._words()
        self._pattern = re.compile(
            "|".join(re.escape(word) for word in sorted(set(words), key=len, reverse=True)),
            re.IGNORECASE) if words else None
        by_provider: Dict[str, List[ModelEntry]] = {}
        for entry in self.entries:
            haystack = " ".join((entry.label, entry.billing, entry.tier,
                                 entry.source, entry.note)).casefold()
            if all(word in haystack for word in words):
                by_provider.setdefault(entry.provider, []).append(entry)
        rows: List[Tuple[str, object]] = []
        for provider in PROVIDER_ORDER:
            models = by_provider.get(provider, [])
            if models or (not words and provider in self.statuses):
                rows.append(("provider", provider))
                rows += [("model", entry) for entry in models]
        self.rows = rows
        keys = [entry.key for kind, entry in rows if kind == "model"]
        if jump and words and keys:
            self.selected_key = keys[0]
        elif self.selected_key not in keys:
            self.selected_key = keys[0] if keys else None

    # --- selection ------------------------------------------------------------

    def selected(self) -> Optional[ModelEntry]:
        for kind, entry in self.rows:
            if kind == "model" and entry.key == self.selected_key:
                return entry
        return None

    def _model_indexes(self) -> List[int]:
        return [index for index, (kind, _) in enumerate(self.rows) if kind == "model"]

    def move(self, delta: int):
        indexes = self._model_indexes()
        if not indexes:
            return
        keys = [self.rows[index][1].key for index in indexes]
        at = keys.index(self.selected_key) if self.selected_key in keys else 0
        self.selected_key = keys[max(0, min(len(keys) - 1, at + delta))]

    def is_current(self, entry: ModelEntry) -> bool:
        provider, model = self.current
        if entry.key == self.current:
            return True
        if entry.provider == "ollama" and provider == "ollama":
            from .providers import ollama_model_matches

            return ollama_model_matches(model, [entry.model])
        return False

    def fanout_label(self) -> str:
        from .fanout import fanout_setting

        setting = fanout_setting(self.config)
        if not setting or setting.lower() == "default":
            return "session model"
        return setting

    def side_label(self) -> str:
        weak = str(self.config.get("weak_model") or "").strip()
        if not weak:
            return "chat model"
        provider = str(self.config.get("weak_provider") or self.current[0]).lower()
        return f"{provider}/{weak}"

    def roles(self, entry: ModelEntry) -> str:
        badges = []
        if self.fanout_label() in (entry.label, entry.model):
            badges.append("F")
        if self.side_label() == entry.label:
            badges.append("T")
        return " ".join(badges)

    # --- actions ---------------------------------------------------------------

    def _usable(self, entry: ModelEntry) -> bool:
        status = self.statuses.get(entry.provider)
        if status is not None and status.available is False and not self.is_current(entry):
            self.notice = f"{entry.provider}: {status.note}"
            return False
        return True

    def choose(self):
        entry = self.selected()
        if entry is None or not self._usable(entry):
            return
        self.result = entry
        if self.app is not None:
            self.app.exit()

    async def set_primary(self, *, close=False):
        entry = self.selected()
        if (self.mode != "list" or self.purpose or self.on_primary is None
                or entry is None or not self._usable(entry)):
            return
        self.mode = "primary"
        self.notice = f"Selecting {entry.label}…"
        try:
            applied = await run_in_terminal(
                lambda: self.on_primary(entry), in_executor=True)
        finally:
            self.mode = "list"
        if not applied:
            self.notice = f"Primary model unchanged: {entry.label} did not pass validation."
            return
        if self.busy:
            self.notice = f"Queued {entry.label} as primary after this turn."
        else:
            self.current = entry.key
            self.notice = f"Primary model: {entry.label}."
        if close and self.app is not None:
            self.app.exit()

    def _set(self, values: Dict[str, str]):
        from .config import set_config_values

        set_config_values(values)
        self.config.update(values)

    def toggle_fanout(self):
        entry = self.selected()
        if entry is None:
            return
        if entry.provider == REGISTRY:
            self.notice = "Registry models can only be the chat model."
            return
        if self.fanout_label() in (entry.label, entry.model):
            self._set({"fanout_model": ""})
            self.notice = "Fan-out workers use the session model again."
            return
        if not self._usable(entry):
            return
        self._set({"fanout_model": entry.label})
        self.notice = f"Fan-out workers use {entry.label}."

    def toggle_side(self):
        entry = self.selected()
        if entry is None:
            return
        if entry.provider == REGISTRY:
            self.notice = "Registry models can only be the chat model."
            return
        if self.side_label() == entry.label:
            self._set({"weak_model": "", "weak_provider": ""})
            self.notice = "Titles, summaries, and compaction use the chat model again."
            return
        if not self._usable(entry):
            return
        self._set({"weak_model": entry.model, "weak_provider": entry.provider})
        self.notice = f"Titles, summaries, and compaction use {entry.label}."

    def cycle_effort(self):
        """Next effort level for the selected model, then back to default."""
        from . import model_tuning

        entry = self.selected()
        if entry is None:
            return
        if entry.provider == REGISTRY:
            self.notice = "Registry models run with their server's settings."
            return
        value, levels = model_tuning.cycle_effort(self.config, entry.provider, entry.model)
        what = "Thinking" if entry.provider == "ollama" else "Effort"
        if not levels:
            self.notice = (f"{entry.label} does not think (per the server)."
                           if entry.provider == "ollama"
                           else f"{entry.label} has no effort setting Conch can send.")
        elif value:
            self.notice = f"{what} for {entry.label}: {value} (of {', '.join(levels)})."
        else:
            self.notice = f"{what} for {entry.label}: the provider default."

    def cycle_context(self):
        """Next context window for the selected model, then back to default."""
        from . import model_tuning

        entry = self.selected()
        if entry is None:
            return
        value, sizes = model_tuning.cycle_context(self.config, entry.provider, entry.model)
        if not sizes:
            where = "its server" if entry.provider in ("custom", REGISTRY) else "the provider"
            self.notice = f"{entry.label}: the context window is fixed by {where}."
        elif value:
            self.notice = (f"Context window for {entry.label}: "
                           f"{model_tuning.context_label(value)} tokens "
                           f"(up to {model_tuning.context_label(sizes[-1])}).")
        else:
            self.notice = f"Context window for {entry.label}: the server default."

    def _tuning(self, entry: ModelEntry) -> str:
        from .model_tuning import describe

        return describe(self.config, entry.provider, entry.model)

    def start_add(self):
        entry = self.selected()
        provider = (entry.provider if entry and entry.provider not in
                    ("ollama", "custom", REGISTRY) else "openai")
        self.add_input.text = f"{provider}/"
        self.add_input.buffer.cursor_position = len(self.add_input.text)
        self.mode = "add"
        if self.app is not None:
            self.app.layout.focus(self.add_input)

    def finish_add(self, save: bool):
        text = self.add_input.text
        self.mode = "list"
        if self.app is not None:
            self.app.layout.focus(self.query)
        if not save:
            return
        try:
            provider, model = parse_new_model(text)
        except ValueError as exc:
            self.notice = str(exc)
            return
        if not add_model(self.config, provider, model):
            self.notice = f"{provider}/{model} is already listed."
        else:
            from .model_catalog import make_entry

            self.entries.append(make_entry(provider, model, self.config, "added"))
            self.notice = (f"Added {provider}/{model}. Enter switches to it "
                           "(it is verified on the switch).")
        self.query.text = ""
        self.selected_key = (provider, model)
        self._refresh_rows()

    def remove_selected(self):
        entry = self.selected()
        if entry is None:
            return
        if entry.source != "added":
            self.notice = "Only models you added can be removed."
            return
        remove_model(self.config, entry.provider, entry.model)
        # Roles must not keep naming a model the catalog no longer has.
        cleared = []
        if self.fanout_label() in (entry.label, entry.model):
            self._set({"fanout_model": ""})
            cleared.append("fan-out workers")
        if self.side_label() == entry.label:
            self._set({"weak_model": "", "weak_provider": ""})
            cleared.append("side tasks")
        # Land on a neighbour rather than jumping back to the top.
        self.move(1)
        if self.selected_key == entry.key:
            self.move(-1)
        self.entries = [item for item in self.entries if item.key != entry.key]
        self.notice = f"Removed {entry.label}." + (
            f" {' and '.join(cleared).capitalize()} use the default again." if cleared else "")
        self._refresh_rows()

    # --- rendering --------------------------------------------------------------

    def _render_header(self, width: int, height: int) -> List[List[Line]]:
        chat = "/".join(part for part in self.current if part) or "none"
        first = (f" Models · chat {chat} · fan-out {self.fanout_label()}"
                 f" · side tasks {self.side_label()}")
        if self.purpose:
            first = f" Choose a model for {self.purpose} ·" + first[len(" Models ·"):]
        count = sum(1 for kind, _ in self.rows if kind == "model")
        second = [f"{count} model{'s' if count != 1 else ''}"]
        if self._words():
            second.append(f"matching “{self.query.text.strip()}”")
        if self.loading:
            second.append("asking local servers, the registry, and seats…")
        return [[("class:bar", _fit(first, width))],
                [("class:muted", _fit(" " + " · ".join(second), width))]]

    def _columns(self, width: int) -> Tuple[int, int, int, int, int]:
        roles, context, price, tier = 3, 6, 13, 8
        if width < 80:
            tier = 0
        if width < 60:
            price = 0
        # marker + indent, then each column after a two-space gap
        fixed = 4 + 2 + roles + 2 + context + (2 + price if price else 0) + (2 + tier if tier else 0)
        # Names take what they need (up to the room left) so the numbers sit
        # beside them instead of at the far edge of a wide terminal.
        longest = max((len(entry.model) + (9 if entry.source == "added" else 0)
                       + (len(self._tuning(entry)) + 4 if self._tuning(entry) else 0)
                       for kind, entry in self.rows if kind == "model"), default=20)
        return max(10, min(width - fixed, max(24, longest + 2))), roles, context, price, tier

    def _render_list(self, width: int, height: int) -> List[List[Line]]:
        self._height = height
        if not self.rows:
            return [[("class:muted", _fit(f"  No model matches “{self.query.text.strip()}”. "
                                          "Ctrl+A adds one.", width))]]
        index = next((i for i, (kind, entry) in enumerate(self.rows)
                      if kind == "model" and entry.key == self.selected_key), 0)
        # Keep the provider row above the selection in view when possible.
        anchor = index - 1 if index > 0 and self.rows[index - 1][0] == "provider" else index
        if anchor < self._top:
            self._top = anchor
        elif index >= self._top + height:
            self._top = index - height + 1
        self._top = max(0, min(self._top, len(self.rows) - height))
        label_w, roles_w, context_w, price_w, tier_w = self._columns(width)
        lines: List[List[Line]] = []
        for kind, item in self.rows[self._top:self._top + height]:
            if kind == "provider":
                status = self.statuses.get(item) or ProviderStatus(None, "")
                note_style = {True: "class:ok", False: "class:error"}.get(
                    status.available, "class:muted")
                lines.append([("class:provider", f" {item}"),
                              (note_style, _fit(f"  {status.note}", max(0, width - len(item) - 1)))])
                continue
            entry: ModelEntry = item
            selected = entry.key == self.selected_key
            status = self.statuses.get(entry.provider)
            dim = status is not None and status.available is False
            plain = "class:selected" if selected else ("class:muted" if dim else "")
            muted = plain or "class:muted"
            tuning = self._tuning(entry)
            name = entry.model + ("  (added)" if entry.source == "added" else "") + (
                f"  ({entry.note})" if entry.note else "") + (f"  [{tuning}]" if tuning else "")
            row = [(plain or "class:current", "●" if self.is_current(entry) else " "),
                   (plain, "   ")]
            row += _highlight(_fit(name, label_w), plain, self._pattern)
            row += [(plain, "  "), (plain or "class:role", _fit(self.roles(entry), roles_w)),
                    (plain, "  "),
                    (muted, _fit_right(_compact(entry.context) if entry.context else "", context_w))]
            if price_w:
                row += [(plain, "  "), (plain or "class:price", _fit_right(_price_text(entry), price_w))]
            if tier_w:
                row += [(plain, "  "), (muted, _fit(entry.tier, tier_w))]
            lines.append(row)
        return lines

    def _render_details(self, width: int, height: int) -> List[List[Line]]:
        entry = self.selected()
        if entry is None:
            return [[], []]
        parts = [entry.label]
        if entry.context:
            parts.append(f"{entry.context:,}-token context")
        if entry.price is not None:
            parts.append(f"${entry.price[0]:g} in / ${entry.price[1]:g} out per 1M tokens")
        else:
            parts.append({"seat": "subscription seat", "local": "local inference"}.get(
                entry.billing, "price unknown"))
        parts.append(f"{entry.tier} tier")
        from . import model_tuning

        if model_tuning.effort_levels(entry.provider, entry.model, self.config, probe=False):
            effort = model_tuning.effort_for(self.config, entry.provider, entry.model)
            what = "thinking" if entry.provider == "ollama" else "effort"
            parts.append(f"{what} {effort or 'default'} (^E)")
        if entry.provider == "ollama":
            context = model_tuning.context_for(self.config, entry.provider, entry.model)
            parts.append(f"context {model_tuning.context_label(context) if context else 'server default'} (^W)")
        source = {"catalog": "built-in catalog", "added": "added by you",
                  "server": "reported by the server", "registry": "llama-idx registry"}
        parts.append(source.get(entry.source, entry.source))
        status = self.statuses.get(entry.provider)
        roles = []
        if self.is_current(entry):
            roles.append("chat model")
        badges = self.roles(entry)
        if "F" in badges:
            roles.append("fan-out workers")
        if "T" in badges:
            roles.append("side tasks")
        second = []
        if roles:
            second.append("Role: " + ", ".join(roles))
        if status is not None and status.note:
            second.append(f"{entry.provider}: {status.note}")
        return [[("class:title", _fit(" " + " · ".join(parts), width))],
                [("class:muted", _fit(" " + " · ".join(second), width))]]

    def _render_footer(self, width: int, height: int) -> List[List[Line]]:
        if self.notice:
            return [[("class:notice", _fit(" " + self.notice, width))]]
        if self.mode == "add":
            hints = ["Enter adds", "Esc cancels", "OpenRouter ids keep their slash: openrouter/vendor/model"]
        else:
            if self.purpose:
                first = f"Enter sets {self.purpose}"
            else:
                first = "Enter use after this turn" if self.busy else "Enter use"
            hints = [first]
            if self.on_primary is not None and not self.purpose:
                hints.append("^P primary (stay)")
            hints.extend([
                "type to filter", "^F fan-out", "^T side tasks", "^E effort",
                "^W context", "^A add", "^X remove", "^R refresh", "Esc quit"])
        return [[("class:muted", " " + _fit_hints(hints, width - 1))]]

    # --- application --------------------------------------------------------------

    def build(self, input=None, output=None) -> Application:
        listing = Window(_Pane(self._render_list), wrap_lines=False, always_hide_cursor=True)
        root = HSplit([
            Window(_Pane(self._render_header), height=2),
            self.query,
            listing,
            Window(_Pane(self._render_details), height=2),
            ConditionalContainer(self.add_input, Condition(lambda: self.mode == "add")),
            Window(_Pane(self._render_footer), height=1),
        ])
        scheme = current_scheme(self.config)
        no_color = colors_disabled() or scheme == "monochrome"
        self.app = Application(
            layout=Layout(root, focused_element=self.query),
            key_bindings=self._key_bindings(), full_screen=True, mouse_support=False,
            enable_page_navigation_bindings=False, input=input, output=output,
            color_depth=ColorDepth.DEPTH_1_BIT if no_color else None,
            style=style_for(scheme),
        )
        self.app.ttimeoutlen = 0.05
        self.app.key_processor.before_key_press += self._clear_notice
        return self.app

    def _clear_notice(self, _sender=None):
        self.notice = ""

    def _key_bindings(self) -> KeyBindings:
        keys = KeyBindings()
        listing = Condition(lambda: self.mode == "list")
        adding = Condition(lambda: self.mode == "add")

        def page() -> int:
            return max(1, self._height - 2)

        @keys.add("c-c", eager=True)
        def _quit(event):
            if self.mode != "primary":
                event.app.exit()

        @keys.add("up", filter=listing)
        def _up(event):
            self.move(-1)

        @keys.add("down", filter=listing)
        @keys.add("c-n", filter=listing)
        def _down(event):
            self.move(1)

        @keys.add("pageup", filter=listing)
        def _page_up(event):
            self.move(-page())

        @keys.add("pagedown", filter=listing)
        def _page_down(event):
            self.move(page())

        @keys.add("enter", filter=listing)
        async def _choose(event):
            if self.on_primary is not None and not self.purpose:
                await self.set_primary(close=True)
            else:
                self.choose()

        @keys.add("c-p", filter=listing)
        async def _primary(event):
            await self.set_primary()

        @keys.add("escape", filter=listing, eager=True)
        def _escape(event):
            if self.query.text:
                self.query.text = ""
            else:
                event.app.exit()

        @keys.add("c-f", filter=listing)
        def _fanout(event):
            self.toggle_fanout()

        @keys.add("c-t", filter=listing)
        def _side(event):
            self.toggle_side()

        @keys.add("c-a", filter=listing)
        def _add(event):
            self.start_add()

        @keys.add("c-x", filter=listing)
        def _remove(event):
            self.remove_selected()

        @keys.add("c-e", filter=listing)
        def _effort(event):
            self.cycle_effort()

        @keys.add("c-w", filter=listing)
        def _context(event):
            self.cycle_context()

        @keys.add("c-r", filter=listing)
        def _refresh(event):
            self.refresh()

        @keys.add("enter", filter=adding)
        def _save(event):
            self.finish_add(save=True)

        @keys.add("escape", filter=adding, eager=True)
        def _cancel(event):
            self.finish_add(save=False)

        return keys
