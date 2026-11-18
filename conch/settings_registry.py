"""The settings conch may change about itself, in one registry.

``conch_config`` (asked by the model) and the subagent panel (/agents) both
go through here: every value is validated, saved to the config file, and
applied to the running session where that is possible. Settings that widen
what runs without the user looking (permission mode, auto-approved command
prefixes or web hosts, cloud fallback, location lookup) are marked
``ask_first``: the caller must have the user's explicit confirmation before
calling :func:`set_setting` for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .themes import SCHEMES, current_scheme


@dataclass(frozen=True)
class Setting:
    key: str
    kind: str  # bool | int | number | choice | list | text | model
    description: str
    default: str = ""
    choices: Tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    ask_first: bool = False  # widens what runs unattended: needs the user's yes
    next_session: bool = False  # read only at startup
    group: str = "general"


def _fanout_default() -> str:
    from .fanout import DEFAULT_CONCURRENCY

    return str(DEFAULT_CONCURRENCY)


def _rounds_default() -> str:
    from .tooling import DelegateTaskClient

    return str(DelegateTaskClient.DEFAULT_ROUNDS)


SETTINGS: Tuple[Setting, ...] = (
    Setting("show_token_stats", "bool", "Info bar (or per-reply stats in plain sessions)", "true"),
    Setting("infobar", "choice", "Info bar style: detailed turn stats or context progress", "detail",
            choices=("detail", "progress")),
    Setting("colorscheme", "choice", "Terminal colors for the composer and panels", "default",
            choices=tuple(SCHEMES)),
    Setting("notifications", "choice", "Desktop alerts at the end of long interactive turns",
            "on", choices=("on", "off")),
    Setting("notify_after_seconds", "number",
            "Desktop notification when a turn runs at least this long (0 = off)", "30",
            minimum=0),
    Setting("git_checkpoints", "bool", "Turn-level git checkpoints behind /undo", "true"),
    Setting("github_context", "bool",
            "Load the GitHub repository's PRs, issues, and checks at startup", "true",
            next_session=True),
    Setting("session_summary_min_turns", "int",
            "User turns before a session is summarized into memory (0 = off)", "3",
            minimum=0, maximum=1000),
    Setting("multiline_paste", "bool", "A multi-line paste is one message", "true",
            next_session=True),
    Setting("model_check", "choice", "Verify the configured model at startup", "on",
            choices=("on", "off"), next_session=True),
    Setting("tool_profile", "text", "Tool profile applied at startup (blank: automatic)",
            next_session=True),
    Setting("fanout_model", "model",
            "Model fan-out workers use (blank: the session model; auto: chosen per task)",
            group="subagents"),
    Setting("fanout_concurrency", "int", "Fan-out workers that run at the same time", "",
            minimum=1, maximum=8, group="subagents"),
    Setting("subagent_model", "model",
            "Model delegate_task subagents use (blank: the session model)", group="subagents"),
    Setting("subagent_rounds", "int",
            "Tool-round budget for each subagent and fan-out worker", "",
            minimum=1, maximum=100, group="subagents"),
    Setting("side_task_model", "model",
            "Model for titles, summaries, and compaction (blank: the chat model)",
            group="subagents"),
    Setting("permission_mode", "choice",
            "Shell approval policy: prompt_all asks for everything, safe_auto runs "
            "read-only commands, yolo runs everything (destructive still asks)",
            "prompt_all", choices=("prompt_all", "safe_auto", "yolo"), ask_first=True),
    Setting("local_only", "choice",
            "Keep a local session from falling back to cloud providers", "auto",
            choices=("auto", "true", "false"), ask_first=True),
    Setting("allow_prefixes", "list", "Command prefixes that never ask (e.g. git status, ls)",
            ask_first=True, next_session=True),
    Setting("web_fetch_allow_domains", "list", "Hosts web_fetch reads without asking",
            ask_first=True),
    Setting("detect_location", "bool",
            "Look up your city from your public IP at startup (a network call)", "false",
            ask_first=True, next_session=True),
)

BY_KEY: Dict[str, Setting] = {setting.key: setting for setting in SETTINGS}
_TRUE = ("on", "true", "yes", "1", "enable", "enabled")
_FALSE = ("off", "false", "no", "0", "disable", "disabled")


def default_of(setting: Setting) -> str:
    if setting.key == "fanout_concurrency":
        return _fanout_default()
    if setting.key == "subagent_rounds":
        return _rounds_default()
    return setting.default


def current_value(config: dict, key: str) -> str:
    """The effective value (the default when unset)."""
    setting = BY_KEY[key]
    if key == "colorscheme":
        return current_scheme(config)
    if key == "notifications":
        from .notify import notify_after_seconds

        return "on" if notify_after_seconds(config) else "off"
    if key == "side_task_model":
        model = str(config.get("weak_model") or "").strip()
        if not model:
            return ""
        provider = str(config.get("weak_provider") or config.get("provider") or "").lower()
        return f"{provider}/{model}"
    raw = config.get(key)
    if raw is None or str(raw).strip() == "":
        return default_of(setting)
    return str(raw).strip()


def _validate_model(setting: Setting, value: str, config: Optional[dict]) -> str:
    from .model_catalog import DISCOVERED_PROVIDERS, REGISTRY
    from .providers import RAW_FNS, validate_model_for_provider

    lowered = value.lower()
    if lowered in ("", "default", "session", "none"):
        return ""
    if lowered == "auto":
        if setting.key != "fanout_model":
            raise ValueError("auto is only for fanout_model")
        return "auto"
    if value.startswith(f"{REGISTRY}/"):
        if setting.key != "fanout_model":
            raise ValueError("registry models work for fan-out workers and the chat model only")
        return value
    provider, slash, model = value.partition("/")
    provider = provider.lower()
    if not slash or provider not in RAW_FNS or not model:
        raise ValueError("use provider/model, e.g. anthropic/claude-sonnet-5")
    if provider not in DISCOVERED_PROVIDERS:
        # The config knows the models the user added (extra_models).
        ok, reason = validate_model_for_provider(provider, model, config)
        if ok is not True:
            raise ValueError(reason or f"unknown {provider} model '{model}'")
    return f"{provider}/{model}"


def normalize(setting: Setting, raw: str, config: Optional[dict] = None) -> str:
    """The stored form of *raw* for *setting*; ValueError says what is wrong."""
    value = str(raw if raw is not None else "").strip()
    if setting.kind == "bool":
        if value.lower() in _TRUE:
            return "true"
        if value.lower() in _FALSE:
            return "false"
        raise ValueError("use on or off")
    if setting.kind in ("int", "number"):
        if value.lower() in ("off", "none") and setting.minimum == 0:
            value = "0"
        if value == "" and default_of(setting) == "":
            return ""
        try:
            number = float(value) if setting.kind == "number" else int(value)
        except ValueError:
            raise ValueError("use a number") from None
        if setting.minimum is not None and number < setting.minimum:
            raise ValueError(f"must be at least {setting.minimum:g}")
        if setting.maximum is not None and number > setting.maximum:
            raise ValueError(f"must be at most {setting.maximum:g}")
        return f"{number:g}" if setting.kind == "number" else str(number)
    if setting.kind == "choice":
        lowered = value.lower().replace("-", "_")
        if setting.key == "model_check" and lowered in _TRUE + _FALSE:
            lowered = "on" if lowered in _TRUE else "off"
        if lowered not in setting.choices:
            raise ValueError("one of: " + ", ".join(setting.choices))
        return lowered
    if setting.kind == "list":
        return ", ".join(item.strip() for item in value.split(",") if item.strip())
    if setting.kind == "model":
        return _validate_model(setting, value, config)
    return value


def set_setting(config: dict, key: str, raw: str, *, permissions=None) -> str:
    """Validate, save, and apply one setting; the confirmation line.

    Raises KeyError for an unknown key and ValueError for a bad value. An
    ``ask_first`` setting must already be confirmed by the user."""
    from .config import set_config_values

    setting = BY_KEY[key]
    value = normalize(setting, raw, config)
    if key == "side_task_model":
        provider, _, model = value.partition("/")
        updates = {"weak_provider": provider, "weak_model": model}
    else:
        updates = {key: value}
    if key == "notifications" and value == "on":
        from .notify import DEFAULT_AFTER_SECONDS, notify_after_seconds

        # Explicitly enabling legacy configurations with a zero/off delay
        # must enable delivery. Otherwise leave the user's delay untouched.
        if not notify_after_seconds(dict(config, **updates)):
            updates["notify_after_seconds"] = f"{DEFAULT_AFTER_SECONDS:g}"
    path = set_config_values(updates)
    config.update(updates)
    if key == "permission_mode" and permissions is not None:
        permissions.set_permission_mode(value)
    shown = value or f"default ({default_of(setting) or 'unset'})"
    when = " It takes effect next session." if setting.next_session else ""
    return f"{key} = {shown} (saved to {path}).{when}"


def reset_setting(config: dict, key: str, *, permissions=None) -> str:
    """Back to the default value (saved)."""
    setting = BY_KEY[key]
    return set_setting(config, key, default_of(setting), permissions=permissions)


def describe(config: dict, group: Optional[str] = None) -> List[str]:
    """One line per setting: value, default, what it does, and its rules."""
    lines = []
    for setting in SETTINGS:
        if group and setting.group != group:
            continue
        value = current_value(config, setting.key) or "(unset)"
        notes = []
        if setting.choices:
            notes.append("|".join(setting.choices))
        if setting.ask_first:
            notes.append("asks the user first")
        if setting.next_session:
            notes.append("next session")
        extra = f" [{'; '.join(notes)}]" if notes else ""
        lines.append(f"- {setting.key} = {value} — {setting.description}{extra}")
    return lines


