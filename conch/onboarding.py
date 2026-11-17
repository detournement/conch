"""First-run onboarding: the interactive provider/key wizard.

Runs once on a truly unconfigured interactive launch, in three steps:
choose a provider, connect it, verify a model. The default path uses
provider sign-in through the official Claude Code or Codex CLI.
API-key providers remain explicit alternatives; their keys use hidden
input and the private ``~/.config/conch/env`` file. Subscription tokens
remain entirely in the native CLI's credential store. Conch writes only
the provider/model selection to ``~/.config/conch/config`` (0600).

A choice that cannot be completed (CLI missing, sign-in failed, no key,
Ollama down, no URL) saves nothing and returns to the provider menu;
nothing is written until a provider is actually connected.

Non-interactive contexts (pipes, daemons, workers, CI) never see the
wizard: it requires a real TTY on stdin and stdout, and honors
``CONCH_NO_WIZARD=1``. It never overwrites existing configuration —
any configured surface disables it entirely.
"""

from __future__ import annotations

import getpass
import os
import shutil
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib import error, request

from . import auth, seats
from .config import (
    display_path,
    find_project_rc,
    get_config_path,
    get_env_file_path,
    get_env_manifest_path,
    load_config,
    remove_env_values,
    set_config_values,
    set_env_values,
)
from .modelcheck import ModelCheckOutcome, ensure_working_model
from .render import Spinner

CYAN = "\033[1;36m"
GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RST = "\033[0m"

INDENT = "  "
WRAP_WIDTH = 88

# (provider, key env, label, one-line pitch). The wizard's menu order.
WIZARD_PROVIDERS = (
    ("claude-code", "", "Claude seat", "Claude subscription"),
    ("codex", "", "ChatGPT seat", "ChatGPT subscription"),
    ("anthropic", "ANTHROPIC_API_KEY", "Anthropic API", "API key, billed separately"),
    ("openai", "OPENAI_API_KEY", "OpenAI API", "API key, billed separately"),
    ("openrouter", "OPENROUTER_API_KEY", "OpenRouter",
     "one key, many models"),
    ("ollama", "", "Ollama", "local models, no API key"),
    ("custom", "", "Custom endpoint", "any OpenAI-compatible server"),
)

# A seat's menu state: what the user sees, and what choosing it has to do.
SEAT_READY = "signed in"
SEAT_NEEDS_LOGIN = "installed, sign-in needed"
SEAT_MISSING = "CLI not installed"
SEAT_STATUS_TIMEOUT = 5.0

CONFIG_HEADER = (
    "# Conch config — written by first-run setup. Edit anytime;\n"
    "# see config.example in the repo for every option."
)


def _wrap_width() -> int:
    return max(40, min(shutil.get_terminal_size((80, 24)).columns, WRAP_WIDTH))


def _say(text: str, color: str = "", *, prefix: str = INDENT,
         prefix_color: str = "") -> None:
    """Print prose word-wrapped to min(terminal columns, WRAP_WIDTH).

    *prefix* opens the first line and sets the hanging indent of the rest.
    Colors go on after wrapping, so textwrap never counts ANSI codes.
    """
    lines = textwrap.wrap(text, width=_wrap_width() - len(prefix),
                          break_on_hyphens=False) or [""]
    for number, line in enumerate(lines):
        head = " " * len(prefix) if number else prefix
        if prefix_color and not number:
            head = f"{prefix_color}{head}{RST}"
        print(head + (f"{color}{line}{RST}" if color else line))


def _step(number: int, title: str) -> None:
    print()
    _say(f"Step {number} of 3 · {title}", BOLD)


def _confirm(question: str, default: bool) -> bool:
    answer = input(
        f"{INDENT}{question} [{'Y/n' if default else 'y/N'}]: "
    ).strip().lower()
    return default if not answer else answer in ("y", "yes")


def _http_get(url: str, headers: dict, timeout: float = 12.0) -> Tuple[int, bytes]:
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except error.HTTPError as exc:
        return exc.code, b""


def detect_ollama() -> Optional[List[str]]:
    """Tool-capable model names from the local Ollama server — the only
    ones the startup model check accepts — or None when it is unreachable."""
    from .providers import list_ollama_models

    try:
        return list_ollama_models(force_refresh=True)
    except Exception:
        return None


def seat_state(provider: str) -> str:
    """Whether the seat's official CLI is on PATH and natively signed in.
    Any status failure means sign-in is needed, never an error."""
    try:
        if not auth.cli_installed(provider):
            return SEAT_MISSING
        ok, _detail = seats.status(provider, timeout=SEAT_STATUS_TIMEOUT)
    except Exception:
        return SEAT_NEEDS_LOGIN
    return SEAT_READY if ok else SEAT_NEEDS_LOGIN


def _detect() -> Tuple[Dict[str, str], Optional[List[str]]]:
    """(seat states, Ollama models), probed concurrently under one spinner."""
    with Spinner("Detecting seats and local models"), \
            ThreadPoolExecutor(max_workers=len(seats.PROVIDERS) + 1) as pool:
        ollama = pool.submit(detect_ollama)
        states = dict(zip(seats.PROVIDERS,
                          pool.map(seat_state, seats.PROVIDERS)))
        return states, ollama.result()


def probe_provider(provider: str, api_key: str,
                   base_url: str = "") -> Tuple[bool, str]:
    """One read-only reachability/auth probe. The key travels only in a
    request header; nothing about it is ever printed or logged."""
    try:
        if provider == "anthropic":
            status, _ = _http_get(
                "https://api.anthropic.com/v1/models",
                {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
        elif provider == "openai":
            status, _ = _http_get(
                "https://api.openai.com/v1/models",
                {"Authorization": f"Bearer {api_key}"},
            )
        elif provider == "openrouter":
            status, _ = _http_get(
                "https://openrouter.ai/api/v1/key",
                {"Authorization": f"Bearer {api_key}"},
            )
        elif provider == "custom":
            headers = (
                {"Authorization": f"Bearer {api_key}"} if api_key else {}
            )
            status, _ = _http_get(
                f"{base_url.rstrip('/')}/models", headers
            )
        else:
            return False, f"no probe for provider {provider!r}"
    except Exception as exc:
        return False, f"unreachable ({type(exc).__name__})"
    if status in (401, 403):
        if provider == "custom" and not api_key:
            return False, "the endpoint requires an API key"
        return False, "the API rejected this key"
    if 200 <= status < 300:
        if provider == "custom" and not api_key:
            # Nothing was authenticated: say what was actually checked.
            return True, "endpoint reachable (no key sent)"
        return True, "key accepted"
    return False, f"unexpected response (HTTP {status})"


def should_offer_wizard() -> bool:
    """True only for a truly unconfigured, interactive launch."""
    if os.environ.get("CONCH_NO_WIZARD", "").strip():
        return False
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except (AttributeError, ValueError):
        return False
    if Path(get_config_path()).exists():
        return False
    if Path(get_env_file_path()).exists():
        return False
    if (Path.home() / ".conchrc").exists():
        return False
    if find_project_rc() is not None:
        return False
    from .providers import DEFAULT_API_KEY_ENVS

    for env_name in DEFAULT_API_KEY_ENVS.values():
        if env_name and os.environ.get(env_name, "").strip():
            return False
    return True


def _print_menu(seat_states: Dict[str, str],
                ollama_models: Optional[List[str]]) -> None:
    label_width = max(len(row[2]) for row in WIZARD_PROVIDERS) + 2
    for index, (provider, _env, label, pitch) in enumerate(
        WIZARD_PROVIDERS, start=1
    ):
        note, color = pitch, DIM
        if provider in seat_states:
            note = f"{pitch} — {seat_states[provider]}"
            if seat_states[provider] == SEAT_READY:
                color = GREEN
        elif provider == "ollama":
            if ollama_models is None:
                note = f"{pitch} — not running"
            else:
                note = f"{pitch} — {len(ollama_models)} tool-capable model(s)"
                if ollama_models:
                    color = GREEN
        _say(note, color, prefix=f"    {index}. {label:<{label_width}}",
             prefix_color=BOLD)


def _default_choice(seat_states: Dict[str, str]) -> int:
    """Menu number Enter selects: the first signed-in seat, else the first
    installed one, else the first row."""
    for wanted in (SEAT_READY, SEAT_NEEDS_LOGIN):
        for index, row in enumerate(WIZARD_PROVIDERS, start=1):
            if seat_states.get(row[0]) == wanted:
                return index
    return 1


def _ask_provider(default: int) -> Tuple[str, str, str]:
    """(provider, key_env, label) for the chosen menu row."""
    count = len(WIZARD_PROVIDERS)
    by_name = {row[0]: row for row in WIZARD_PROVIDERS}
    while True:
        raw = input(
            f"{INDENT}Provider [1-{count}, Enter={default}]: "
        ).strip().lower() or str(default)
        if raw.isdecimal() and 1 <= int(raw) <= count:
            return WIZARD_PROVIDERS[int(raw) - 1][:3]
        if raw in by_name:
            return by_name[raw][:3]
        _say(f"Pick a number between 1 and {count}.", RED)


def _collect_key(env_name: str) -> str:
    """The API key via getpass — hidden input; empty means go back."""
    _say(f"The key is stored in {display_path(get_env_file_path())} (0600),"
         " never echoed, never logged.", DIM)
    return getpass.getpass(f"{INDENT}{env_name} (Enter to go back): ").strip()


CUSTOM_KEY_ENV = "CONCH_CUSTOM_API_KEY"


def discover_custom_models(base_url: str, api_key: str = "",
                           timeout: float = 5.0) -> Tuple[Optional[List[str]], bool]:
    """(models, tool_capable) for a custom endpoint: the models that passed
    conch's native tool-call conformance probe when any did, else every
    model the endpoint lists (tool_capable=False); None when unreachable.
    A key is passed only through the process environment for the probe."""
    from .providers import list_custom_models

    config = {"provider": "custom", "custom_base_url": base_url}
    previous = os.environ.get(CUSTOM_KEY_ENV)
    if api_key:
        config["api_key_env"] = CUSTOM_KEY_ENV
        os.environ[CUSTOM_KEY_ENV] = api_key
    try:
        capable = list_custom_models(config, timeout=timeout, force_refresh=True)
        if capable is None:
            return None, False
        if capable:
            return list(capable), True
        listed = list_custom_models(
            config, timeout=timeout, tool_capable_only=False
        )
        return list(listed or []), False
    finally:
        if api_key:
            if previous is None:
                os.environ.pop(CUSTOM_KEY_ENV, None)
            else:
                os.environ[CUSTOM_KEY_ENV] = previous


def _choose_custom_model(base_url: str, api_key: str) -> str:
    """Pick the endpoint's model so the config is complete on first launch
    (an empty custom_model otherwise warns on every start). Returns ""
    when nothing could be discovered — the user is told how to set it."""
    _say(f"Looking up models at {base_url} …", DIM)
    with Spinner("Discovering custom models"):
        models, tool_capable = discover_custom_models(base_url, api_key)
    config_path = display_path(get_config_path())
    if models is None:
        _say("! Endpoint unreachable.", YELLOW)
        _say(f"Add `custom_model = <name>` to {config_path} once it is up"
             " (conch verifies it at startup).", DIM)
        return ""
    if not models:
        _say("! The endpoint lists no models.", YELLOW)
        _say(f"Set `custom_model = <name>` in {config_path} when one is"
             " loaded.", DIM)
        return ""
    if tool_capable:
        _say(f"✓ {len(models)} model(s) passed the native tool-call check",
             GREEN)
    else:
        _say("! No model passed the native tool-call check — tool use may"
             " not work with this endpoint.", YELLOW)
    return _choose_model(models)


def _choose_model(models: List[str]) -> str:
    """Select an installed/discovered model before the startup capability check."""
    if len(models) == 1:
        _say(f"model: {models[0]}", DIM)
        return models[0]
    shown = min(len(models), 12)
    for index, name in enumerate(models[:shown], start=1):
        _say(name, prefix=f"    {index}. ", prefix_color=BOLD)
    while True:
        raw = input(f"{INDENT}Model [1-{shown}, Enter=1]: ").strip()
        if not raw:
            return models[0]
        if raw.isdecimal() and 1 <= int(raw) <= shown:
            return models[int(raw) - 1]
        if raw in models:
            return raw
        _say(f"Pick a number between 1 and {shown}.", RED)


def _probe(provider: str, key: str, base_url: str = "") -> bool:
    with Spinner("Verifying provider reachability"):
        ok, detail = probe_provider(provider, key, base_url)
    _say(f"✓ {detail}" if ok else f"! {detail}", GREEN if ok else YELLOW)
    return ok


def _report_saved(config_path: str, key_saved: bool = False) -> None:
    print()
    _say(f"Configuration saved: {display_path(config_path)}", DIM)
    if key_saved:
        _say(f"Key saved: {display_path(get_env_file_path())} (0600)."
             " `conch keys` lists, verifies, and rotates it later.", DIM)


def _seat_cli_missing(provider: str, label: str) -> bool:
    install, docs = auth.install_hint(provider)
    _say(f"! The {label} needs its official CLI, which is not installed.",
         YELLOW)
    _say(install, prefix="    Install: ")
    _say(docs, prefix="    Docs:    ")
    _say("Then choose it again, or pick another provider.", DIM,
         prefix="    ")
    return False


def _connect_seat(provider: str, label: str,
                  seat_states: Dict[str, str]) -> bool:
    """Select a signed-in seat, running the native sign-in only when needed.
    The state is re-read on every choice that was not already signed in, so
    installing or signing in elsewhere and choosing again just works."""
    if seat_states.get(provider) != SEAT_READY:
        with Spinner(f"Checking {label} sign-in"):
            seat_states[provider] = seat_state(provider)
    if seat_states[provider] == SEAT_MISSING:
        return _seat_cli_missing(provider, label)
    if seat_states[provider] != SEAT_READY:
        _say("Starting the provider's own sign-in; finish it in your"
             " browser.", DIM)
        try:
            code, reason = auth.sign_in(provider)
        except FileNotFoundError:
            seat_states[provider] = SEAT_MISSING
            return _seat_cli_missing(provider, label)
        if code:
            _say(f"! {reason or 'Sign-in did not complete.'}", YELLOW)
            _say("Choose it again to retry, or pick another provider.", DIM)
            return False
        seat_states[provider] = SEAT_READY
    model, config_path = auth.select(provider)
    _say(f"✓ {label} is signed in.", GREEN)
    _say(f"Saved {provider}/{model} to {display_path(config_path)}; your"
         " sign-in stays with the provider's CLI.", DIM)
    return True


def _connect_ollama(models: Optional[List[str]]) -> bool:
    """Save the chosen local model, or say what to run when there is none."""
    from .providers import (
        DEFAULT_CHAT_MODEL_BY_PROVIDER,
        get_ollama_base_url,
        local_endpoint_policy_error,
    )

    if models is None:
        base_url = get_ollama_base_url()
        blocked = local_endpoint_policy_error("ollama", base_url, {})
        if blocked:
            _say(f"! {blocked}", YELLOW)
        else:
            _say(f"! Ollama is not reachable at {base_url}.", YELLOW)
            _say("Start it with `ollama serve` (or open the Ollama app),"
                 " then choose Ollama again.", DIM)
        return False
    if not models:
        _say("! Ollama is running but has no model that supports tool"
             " calling, which Conch requires.", YELLOW)
        _say("Pull one with `ollama pull"
             f" {DEFAULT_CHAT_MODEL_BY_PROVIDER['ollama']}`, then choose"
             " Ollama again.", DIM)
        return False
    selected = _choose_model(models)
    _report_saved(set_config_values(
        {"provider": "ollama", "model": selected, "chat_model": selected},
        header=CONFIG_HEADER,
    ))
    return True


def _connect_custom() -> bool:
    base_url = input(
        f"{INDENT}Base URL (e.g. http://localhost:8080/v1): "
    ).strip().rstrip("/")
    if not base_url:
        _say("! No URL entered, so nothing was saved.", YELLOW)
        return False
    key = getpass.getpass(
        f"{INDENT}API key for it, if any (Enter for none): "
    ).strip()
    if _confirm("Verify with one live request?", True):
        _probe("custom", key, base_url)
    updates = {"provider": "custom", "custom_base_url": base_url}
    custom_model = _choose_custom_model(base_url, key)
    if custom_model:
        updates["custom_model"] = custom_model
    if key:
        # Custom endpoints read OPENAI_API_KEY-style bearer via api_key_env;
        # store under a conch-specific name and point config at it.
        updates["api_key_env"] = CUSTOM_KEY_ENV
    config_path = set_config_values(updates, header=CONFIG_HEADER)
    if key:
        set_env_values({CUSTOM_KEY_ENV: key}, set_by="first-run setup")
    _report_saved(config_path, key_saved=bool(key))
    return True


def _connect_api_key(provider: str, env_name: str) -> bool:
    while True:
        key = _collect_key(env_name)
        if not key:
            _say("! No key entered, so nothing was saved.", YELLOW)
            return False
        if (not _confirm("Verify with one live request?", True)
                or _probe(provider, key)
                or _confirm("Save this key anyway?", False)):
            break
        _say("Enter the key again, or press Enter to pick another"
             " provider.", DIM)
    config_path = set_config_values({"provider": provider},
                                    header=CONFIG_HEADER)
    # Same write path as `conch keys set`: the key becomes conch-managed
    # (authoritative over a stale shell export) and shows up in
    # `conch keys list` with its provenance.
    set_env_values({env_name: key}, set_by="first-run setup")
    _report_saved(config_path, key_saved=True)
    return True


def run_first_run_wizard() -> ModelCheckOutcome:
    """Choose a provider, connect it, and return its startup verification
    outcome. A choice that cannot be connected saves nothing and returns to
    the provider menu, and so can one whose model then fails verification."""
    from .bootstrap import StartupError

    seat_states, ollama_models = _detect()
    print()
    _say("Welcome to Conch", CYAN)
    _say("Three steps: choose a provider, connect it, verify a model."
         " Ctrl+C exits setup; run conch again to return.")
    print()
    _say("Claude and ChatGPT seats use your subscription through the"
         " provider's official CLI. API keys are optional and billed"
         " separately. You can change providers later with /provider.")
    while True:
        _step(1, "Choose a provider")
        _print_menu(seat_states, ollama_models)
        print()
        provider, env_name, label = _ask_provider(_default_choice(seat_states))
        _step(2, f"Connect {label}")
        if provider in seat_states:
            connected = _connect_seat(provider, label, seat_states)
        elif provider == "ollama":
            if not ollama_models:
                with Spinner("Detecting local models"):
                    ollama_models = detect_ollama()
            connected = _connect_ollama(ollama_models)
        elif provider == "custom":
            connected = _connect_custom()
        else:
            connected = _connect_api_key(provider, env_name)
        if connected:
            _step(3, "Verify a model")
            try:
                return verify_configured_model()
            except StartupError:
                try:
                    again = _confirm("Choose a different provider?", True)
                except (KeyboardInterrupt, EOFError):
                    again = False
                if not again:
                    raise
                _discard_setup()
                _say("Nothing from that attempt was kept.", DIM)


def _discard_setup() -> None:
    """Remove what this run wrote, so another provider starts clean. The
    wizard only runs when none of these files exist, so nothing of the
    user's own is ever deleted."""
    names = [row[1] for row in WIZARD_PROVIDERS if row[1]] + [CUSTOM_KEY_ENV]
    for name in remove_env_values(names):
        os.environ.pop(name, None)
    for path in (get_config_path(), get_env_file_path(),
                 get_env_manifest_path()):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def verify_configured_model() -> ModelCheckOutcome:
    """Run the startup model check on the configuration the wizard just
    wrote, so first run ends in a verified-working state — or in the same
    approved alternatives flow the shell uses. Because the wizard is the
    step that writes config, an approved pick is saved as the default
    (``persist_mode="always"``); declining raises StartupError for the
    gatekeeper to turn into a clean non-zero exit."""
    config = load_config()
    label = f"{config.get('provider')}/{config.get('chat_model') or config.get('model')}"
    _say(f"Checking {label} …", DIM)
    outcome = ensure_working_model(config, interactive=True, persist_mode="always",
                                   restart_hint=False)
    if not outcome.checked:
        _say(f"model check is off (model_check=off); {label} unverified", DIM)
    elif not outcome.switched:
        detail = outcome.result.detail if outcome.result is not None else ""
        _say(f"✓ {outcome.label} verified"
             + (f" — {detail}" if detail else ""), GREEN)
    return outcome


def maybe_run_first_run_wizard() -> Optional[ModelCheckOutcome]:
    """Return the first-run check, skip configured users, or exit cancelled setup."""
    try:
        if not should_offer_wizard():
            return None
    except Exception:
        return None
    from .bootstrap import StartupError

    try:
        return run_first_run_wizard()
    except (KeyboardInterrupt, EOFError):
        print()
        _say("Setup cancelled. Run `conch` again when ready.", DIM)
        sys.exit(0)
    except StartupError as exc:
        # The post-setup model check found nothing working (or the user
        # declined every alternative): the config is saved; exit cleanly
        # so the next launch re-checks instead of chatting with a model
        # that is known not to answer.
        print(str(exc), file=sys.stderr)
        _say(f"Setup is saved in {display_path(get_config_path())}. Run"
             " `conch` again once the model is reachable, or edit that"
             " file.", DIM)
        sys.exit(exc.code)
