"""Verified provider setup and recovery.

Subscription seats use official Claude Code and Codex CLIs; Conch never
reads their tokens. API keys and routing are staged in memory and saved
only after a bounded model probe. Pipes and --check never start login.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import getpass
import os
import shutil
import stat
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import auth, seats
from .config import (
    display_path,
    find_project_rc,
    get_config_path,
    get_env_file_path,
    load_config,
    set_config_values,
    set_env_values,
)
from .modelcheck import MODEL_CHECK_EXIT_CODE, ModelCheckOutcome, probe_model
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
                  seat_states: Dict[str, str]) -> Optional[StagedSetup]:
    """Connect natively without selecting a default or touching token files."""
    with Spinner(f"Checking {label} sign-in"):
        seat_states[provider] = seat_state(provider)
    if seat_states[provider] == SEAT_MISSING:
        _seat_cli_missing(provider, label)
        return None
    if seat_states[provider] != SEAT_READY:
        if not _interactive_terminal():
            _say("Sign-in needs a terminal. Run `conch setup` there.", YELLOW)
            return None
        _say("Starting the provider's own sign-in; finish it in your browser.", DIM)
        try:
            code, _reason = auth.sign_in(provider)
        except FileNotFoundError:
            seat_states[provider] = SEAT_MISSING
            _seat_cli_missing(provider, label)
            return None
        if code:
            _say("! Native sign-in did not complete. Retry or choose another provider.", YELLOW)
            return None
        seat_states[provider] = SEAT_READY
    return _stage(provider, seats.DEFAULT_MODELS[provider])


def _connect_ollama(models: Optional[List[str]]) -> Optional[StagedSetup]:
    """Stage a local model, or explain how to make one available."""
    from .providers import (
        DEFAULT_CHAT_MODEL_BY_PROVIDER,
        get_ollama_base_url,
        local_endpoint_policy_error,
    )

    base_url = get_ollama_base_url()
    if models is None:
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
    return _stage("ollama", selected, ollama_base_url=base_url)


def _connect_custom() -> Optional[StagedSetup]:
    base_url = input(
        f"{INDENT}Base URL (e.g. http://localhost:8080/v1): "
    ).strip().rstrip("/")
    if not base_url:
        _say("! No URL entered, so nothing was saved.", YELLOW)
        return None
    key = getpass.getpass(
        f"{INDENT}API key for it, if any (Enter for none): "
    ).strip()
    model = _choose_custom_model(base_url, key)
    if not model:
        return None
    setup = _stage("custom", model, custom_base_url=base_url,
                   custom_model=model, api_key_env=CUSTOM_KEY_ENV if key else "")
    if key:
        setup.keys[CUSTOM_KEY_ENV] = key
    return setup


def _connect_api_key(provider: str, env_name: str) -> Optional[StagedSetup]:
    from .providers import DEFAULT_CHAT_MODEL_BY_PROVIDER

    key = _collect_key(env_name)
    if not key:
        _say("! No key entered, so nothing was saved.", YELLOW)
        return None
    setup = _stage(provider, DEFAULT_CHAT_MODEL_BY_PROVIDER[provider])
    setup.keys[env_name] = key
    return setup


@dataclass
class StagedSetup:
    updates: Dict[str, str]
    keys: Dict[str, str] = field(default_factory=dict, repr=False)


def _stage(provider: str, model: str, **overrides) -> StagedSetup:
    from .modelcheck import Candidate

    return StagedSetup(Candidate(provider, model, "provider",
                                 overrides=overrides).config_updates())


def _interactive_terminal() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


@contextmanager
def _temporary_keys(keys):
    previous = {name: os.environ.get(name) for name in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _verify_primary(config: dict) -> ModelCheckOutcome:
    """Certify only the effective primary, respecting explicit offline policy."""
    from .bootstrap import StartupError
    from .config import local_only_enabled
    from .modelcheck import model_check_enabled

    if not model_check_enabled(config):
        raise StartupError(
            "conch: model_check=off; the primary is unverified (no request sent)."
            " Run `conch setup` in a terminal to explicitly select and verify a route.",
            code=MODEL_CHECK_EXIT_CODE,
        )
    if local_only_enabled(config) and config.get("provider") not in ("ollama", "custom"):
        raise StartupError(
            "conch: local_only blocks the configured cloud route (no credentials sent)."
            " Choose a local provider with `conch setup`, or explicitly change local_only.",
            code=MODEL_CHECK_EXIT_CODE,
        )

    provider = config.get("provider", "")
    model = config.get("chat_model") or config.get("model") or ""
    result = probe_model(provider, model, config)
    if not result.ok:
        raise StartupError(
            f"conch: {result.label} is not usable: {result.describe()}"
            " — run `conch setup` in a terminal to reconnect or choose a provider.",
            code=MODEL_CHECK_EXIT_CODE,
        )
    return ModelCheckOutcome(provider, model, result)


def _verify_staged(setup: StagedSetup) -> ModelCheckOutcome:
    from .config import ENV_CONFIG_KEYS, _parse_config_file
    from .bootstrap import StartupError

    # Preview higher-priority routing without writing or deleting overrides.
    higher = _parse_config_file(Path.home() / ".conchrc")
    project = find_project_rc()
    if project is not None:
        higher.update(_parse_config_file(project))
    higher.update({key: os.environ[name].strip()
                  for name, key in ENV_CONFIG_KEYS.items() if name in os.environ})
    if any(key in higher and higher[key] != value
           for key, value in setup.updates.items()):
        raise StartupError(
            "conch: ~/.conchrc, project .conchrc, or CONCH_* overrides the selected"
            " routing. Update that override or run setup outside this project;"
            " no files were changed.", code=MODEL_CHECK_EXIT_CODE,
        )
    config = load_config()
    config.update(setup.updates)
    # Choosing a route explicitly approves its probe, not a runtime policy change.
    config["model_check"] = "on"
    with _temporary_keys(setup.keys), Spinner("Verifying selected model"):
        return _verify_primary(config)


def _snapshot_setup_file(path: Path) -> Optional[Tuple[bytes, int]]:
    try:
        with path.open("rb") as handle:
            return handle.read(), stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
    except FileNotFoundError:
        return None


def _save_setup(setup: StagedSetup, outcome: ModelCheckOutcome) -> ModelCheckOutcome:
    """Commit verified setup, restoring files and key state on failure."""
    from .bootstrap import StartupError
    from .config import _ENV_LOAD_STATE, _write_private_file, get_env_manifest_path

    paths = [("routing configuration", Path(get_config_path()))]
    if setup.keys:
        paths[:0] = [
            ("credentials", Path(get_env_file_path())),
            ("credential manifest", Path(get_env_manifest_path())),
        ]
    try:
        snapshots = [(label, path, _snapshot_setup_file(path)) for label, path in paths]
    except Exception:
        raise StartupError(
            "conch: could not read existing setup state; Conch configuration"
            " and credentials are unchanged.", code=MODEL_CHECK_EXIT_CODE,
        ) from None
    key_states = [
        (label, mapping, {name: mapping.get(name) for name in setup.keys})
        for label, mapping in (
            ("process credentials", os.environ),
            ("credential reload state", _ENV_LOAD_STATE["from_file"]),
        )
    ]
    try:
        if setup.keys:
            set_env_values(setup.keys, set_by="verified setup", verified="ok")
        path = set_config_values(setup.updates, header=CONFIG_HEADER)
        _report_saved(path, key_saved=bool(setup.keys))
    except BaseException as exc:
        # One bounded rollback pass; keep restoring other resources even if
        # a write fails or a second interrupt arrives during restoration.
        failed = []
        for label, path, original in reversed(snapshots):
            try:
                if _snapshot_setup_file(path) == original:
                    continue
                if original is None:
                    path.unlink()
                else:
                    _write_private_file(path, original[0], mode=original[1])
            except BaseException:
                failed.append(label)
        for label, mapping, previous in key_states:
            for name, value in previous.items():
                try:
                    if value is None:
                        mapping.pop(name, None)
                    else:
                        mapping[name] = value
                except BaseException:
                    if label not in failed:
                        failed.append(label)
        if failed:
            raise StartupError(
                "conch: setup failed and rollback was incomplete; "
                + ", ".join(failed) + " may have changed."
                " Check Conch credential and routing files before retrying setup.",
                code=MODEL_CHECK_EXIT_CODE,
            ) from None
        if isinstance(exc, (KeyboardInterrupt, EOFError)) or not isinstance(exc, Exception):
            raise
        raise StartupError(
            "conch: could not save setup; existing Conch configuration"
            " and credentials are unchanged.", code=MODEL_CHECK_EXIT_CODE,
        ) from None
    outcome.saved = True
    return outcome


def run_first_run_wizard() -> ModelCheckOutcome:
    """Stage, verify, then commit; failed retries never delete user state."""
    from .bootstrap import StartupError
    if not _interactive_terminal():
        raise StartupError("conch: setup needs a terminal; use `conch setup --check`"
                           " for non-interactive verification.", code=MODEL_CHECK_EXIT_CODE)

    seat_states, ollama_models = _detect()
    print()
    _say("Welcome to Conch", CYAN)
    _say("Choose a provider, connect it, then verify a model. Ctrl+C cancels;"
         " run `conch setup` to return.")
    _say("Claude and ChatGPT subscriptions use official native sign-in."
         " You can connect both and choose your primary. API keys are optional"
         " and billed separately; local models are available too.")
    while True:
        _step(1, "Choose a provider")
        _print_menu(seat_states, ollama_models)
        provider, env_name, label = _ask_provider(_default_choice(seat_states))
        _step(2, f"Connect {label}")
        if provider in seat_states:
            setup = _connect_seat(provider, label, seat_states)
        elif provider == "ollama":
            if not ollama_models:
                ollama_models = detect_ollama()
            setup = _connect_ollama(ollama_models)
        elif provider == "custom":
            setup = _connect_custom()
        else:
            setup = _connect_api_key(provider, env_name)
        if not setup:
            continue
        _step(3, "Verify a model")
        try:
            outcome = _verify_staged(setup)
        except StartupError as exc:
            _say(str(exc), YELLOW)
            _say("No Conch files or credentials were changed.", DIM)
            if _confirm("Choose a different provider?", True):
                continue
            raise
        _say(f"✓ {outcome.label} verified", GREEN)
        if provider in seats.PROVIDERS:
            other = next(name for name in seats.PROVIDERS if name != provider)
            other_label = next(row[2] for row in WIZARD_PROVIDERS if row[0] == other)
            if _confirm(f"Also connect {other_label}? (optional)", False):
                second = _connect_seat(other, other_label, seat_states)
                if second:
                    try:
                        second_outcome = _verify_staged(second)
                    except StartupError as exc:
                        _say(str(exc), YELLOW)
                        _say("Keeping the first verified provider.", DIM)
                    else:
                        _say(f"✓ {second_outcome.label} verified", GREEN)
                        if _confirm(f"Use {other_label} as primary?", False):
                            setup, outcome = second, second_outcome
        return _save_setup(setup, outcome)


def verify_configured_model() -> ModelCheckOutcome:
    """Verify the effective primary without spending on unrelated routes."""
    outcome = _verify_primary(load_config())
    _say(f"✓ {outcome.label} verified", GREEN)
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
        sys.exit(130)
    except StartupError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(exc.code)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="conch setup", description="Verify or reconnect your primary provider."
    )
    parser.add_argument("--check", action="store_true",
                        help="verify the effective primary without prompting or changing files")
    args = parser.parse_args(argv)
    from .bootstrap import StartupError

    try:
        if not args.check and should_offer_wizard():
            run_first_run_wizard()
            return 0
        try:
            verify_configured_model()
            return 0
        except StartupError as exc:
            print(str(exc), file=sys.stderr)
            if args.check or not _interactive_terminal() or os.environ.get("CONCH_NO_WIZARD", "").strip():
                return exc.code
        run_first_run_wizard()
        return 0
    except (KeyboardInterrupt, EOFError):
        print("conch: setup cancelled; existing Conch configuration is unchanged.",
              file=sys.stderr)
        return 130
    except StartupError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
