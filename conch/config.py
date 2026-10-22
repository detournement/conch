"""Configuration loading helpers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional


DEFAULT_CONFIG: Dict[str, str] = {
    "provider": "anthropic",
    "model": "claude-sonnet-5",
    "chat_model": "claude-sonnet-5",
    "api_key_env": "ANTHROPIC_API_KEY",
    # Agent mode off by default: shell commands require confirmation unless
    # the user opts in with agent_mode=true in the config file.
    "agent_mode": "false",
    # Local providers are isolated from cloud fallback by default. Set false
    # explicitly to permit a configured local session to cross providers.
    "local_only": "auto",
    # Public-IP geolocation is opt-in; startup otherwise makes no such call.
    "detect_location": "false",
    # Per-message token stats line (tokens, cost, tok/s) in interactive
    # chat. On by default; /tks flips it for a session.
    "show_token_stats": "true",
    # Turn-level git checkpoints when cwd is a git repo: mutating turns
    # snapshot the worktree to refs/conch/checkpoints/* (user branch and
    # index untouched). /checkpoint and /undo manage them.
    "git_checkpoints": "true",
    # A session is summarized into memory on /new and exit only when it
    # had this many user turns (or ran a tool); 0/off disables summaries.
    "session_summary_min_turns": "3",
    # Startup model check: every launch probes the configured model before
    # the first call (models-list endpoint, or the tool-call conformance
    # probe for local endpoints) and, when it fails, offers alternatives
    # interactively or walks fallback_models non-interactively. "off"
    # skips the probe. model_check_timeout bounds each probe request.
    "model_check": "on",
    "model_check_timeout": "4",
    # Ordered, pre-approved "provider/model" entries (comma-separated) a
    # non-interactive run (pipe, cron, conch-ask, conch-edge) may switch
    # to when the configured model fails its startup check. Empty means
    # fail closed instead.
    "fallback_models": "",
}

ENV_CONFIG_KEYS = {
    "CONCH_PROVIDER": "provider",
    "CONCH_MODEL": "model",
    "CONCH_CHAT_MODEL": "chat_model",
    "CONCH_API_KEY_ENV": "api_key_env",
    "CONCH_BASE_URL": "base_url",
    "CONCH_OLLAMA_BASE_URL": "ollama_base_url",
    "CONCH_OLLAMA_NUM_CTX": "ollama_num_ctx",
    "CONCH_OLLAMA_CONTEXT_WINDOW": "ollama_context_window",
    "CONCH_OLLAMA_TEMPERATURE": "ollama_temperature",
    "CONCH_OLLAMA_NUM_PREDICT": "ollama_num_predict",
    "CONCH_OLLAMA_KEEP_ALIVE": "ollama_keep_alive",
    "CONCH_OLLAMA_THINK": "ollama_think",
    "CONCH_OLLAMA_TIMEOUT": "ollama_timeout",
    "CONCH_CUSTOM_BASE_URL": "custom_base_url",
    "CONCH_CUSTOM_MODEL": "custom_model",
    "CONCH_CUSTOM_CONTEXT_WINDOW": "custom_context_window",
    "CONCH_CUSTOM_TEMPERATURE": "custom_temperature",
    "CONCH_CUSTOM_MAX_TOKENS": "custom_max_tokens",
    "CONCH_CUSTOM_TIMEOUT": "custom_timeout",
    "CONCH_MAX_OUTPUT_TOKENS": "max_output_tokens",
    "CONCH_LOCAL_ONLY": "local_only",
    "CONCH_DETECT_LOCATION": "detect_location",
    "CONCH_AGENT_MODE": "agent_mode",
    "CONCH_SESSION_SUMMARY_MIN_TURNS": "session_summary_min_turns",
    "CONCH_MODEL_CHECK": "model_check",
    "CONCH_MODEL_CHECK_TIMEOUT": "model_check_timeout",
    "CONCH_FALLBACK_MODELS": "fallback_models",
    "CONCH_PERMISSION_MODE": "permission_mode",
    "CONCH_TOOL_PROFILE": "tool_profile",
    "CONCH_SSH_CONTROL_PERSIST": "ssh_control_persist",
    # Capitol A2A adapter (Swarm Phase 3). The bearer itself is never a
    # config value: capitol_bearer_env names the environment variable it
    # is read from (or the ~/.capitol-a2a registry supplies it).
    "CONCH_CAPITOL_BASE_URL": "capitol_base_url",
    "CONCH_CAPITOL_ORG": "capitol_org",
    "CONCH_CAPITOL_AGENT": "capitol_agent",
    "CONCH_CAPITOL_BEARER_ENV": "capitol_bearer_env",
    # Daemon-tick Capitol supervision cadence (seconds between poll passes).
    "CONCH_CAPITOL_POLL_SECONDS": "capitol_poll_seconds",
    # CapitolAdmin (builder profile, bounded production). Default off; the
    # admin token is resolved from the env var named here (or the
    # ~/.capitol-a2a registry's x_user_token), never stored.
    "CONCH_CAPITOL_ADMIN": "capitol_admin",
    "CONCH_CAPITOL_PLATFORM_URL": "capitol_platform_url",
    "CONCH_CAPITOL_ADMIN_TOKEN_ENV": "capitol_admin_token_env",
    # Capitol docs MCP server (ENG-5629/5630): the release-true platform
    # documentation endpoint. Unset = no docs tools, zero traffic.
    "CONCH_CAPITOL_DOCS_URL": "capitol_docs_url",
    # Edge daemon / mission kernel (Swarm Phase 1). Off by default: the
    # interactive shell keeps its in-process scheduler and never imports
    # the kernel unless this is enabled.
    "CONCH_EDGE_DAEMON": "edge_daemon",
    # Fleet controller (the fleet awakening). Off by default: the shell
    # never imports conch.fleet unless this is enabled.
    "CONCH_FLEET_CONTROLLER": "fleet_controller",
    # llama-idx inference registry (discovery only). Unset = feature off,
    # zero new startup traffic. llamaidx_token_env names the env var
    # holding the registry's read token (read_auth registries) — the
    # token itself is never a config value.
    "CONCH_LLAMAIDX_URL": "llamaidx_url",
    "CONCH_LLAMAIDX_TOKEN_ENV": "llamaidx_token_env",
}


def _parse_config_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip("'\"")
    return data


def get_config_path() -> str:
    """Path to the primary Conch config file (may not exist yet)."""
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch"
    return str(config_dir / "config")


def get_env_file_path() -> str:
    """Path to the conch-owned API key file (may not exist yet).

    ``~/.config/conch/env`` holds ``KEY=value`` lines (0600) that
    :func:`load_config` mirrors into the process environment — the
    canonical home for every credential conch uses, written by
    ``conch keys set`` / ``conch keys rotate`` and by first-run
    onboarding. Because conch itself loads it, daemons and
    launchd/systemd services see the keys without shell-profile
    exports.

    Precedence (see :func:`load_env_file`): a key *conch wrote* through
    ``conch keys`` (recorded in the manifest next to the file) is
    authoritative and overrides an inherited shell export of the same
    name — it was verified live when it was saved, and a lingering
    ``export`` in a profile is the classic stale-key trap after a
    rotation. A line someone added to the file by hand keeps the older
    rule: a variable already in the real environment wins.
    """
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch"
    return str(config_dir / "env")


ENV_MANIFEST_NAME = "env.manifest.json"


def get_env_manifest_path() -> str:
    """Sidecar of the env file recording which names conch manages,
    their fingerprints (never values), when they were saved, and the
    last live verification. 0600 like the env file."""
    return str(Path(get_env_file_path()).parent / ENV_MANIFEST_NAME)


def key_fingerprint(value: str, length: int = 8) -> str:
    """Irreversible, loggable reference to a secret: ``sha256:`` plus
    the first *length* hex digits of its digest. Never the bytes."""
    import hashlib

    if not value:
        return "-"
    return "sha256:" + hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()[:length]


def parse_env_lines(text: str) -> Dict[str, str]:
    """``NAME=value`` entries of an env file body (``export`` prefix
    and surrounding quotes tolerated; comments and blanks skipped).
    Later duplicates win, like a shell would apply them."""
    entries: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            entries[key] = value
    return entries


def read_env_file() -> Dict[str, str]:
    """Entries of the env file without touching ``os.environ``
    (``{}`` when the file is missing or unreadable)."""
    try:
        return parse_env_lines(Path(get_env_file_path()).read_text())
    except OSError:
        return {}


def read_env_manifest() -> Dict[str, object]:
    """The manifest as a dict (``{"version": 1, "keys": {}, "checks": {}}``
    when absent or unreadable — a damaged manifest only means no key
    counts as conch-managed, never a crash)."""
    try:
        import json

        data = json.loads(Path(get_env_manifest_path()).read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    keys = data.get("keys")
    data["keys"] = keys if isinstance(keys, dict) else {}
    checks = data.get("checks")
    data["checks"] = checks if isinstance(checks, dict) else {}
    return data


def _write_env_manifest(data: Dict[str, object]) -> None:
    import json

    _write_private_file(
        Path(get_env_manifest_path()),
        json.dumps(data, indent=2, sort_keys=True) + "\n",
    )


def managed_key_names() -> set:
    """Names the env file holds *and* conch recorded as its own."""
    return set(read_env_manifest()["keys"]) & set(read_env_file())


def record_managed_keys(updates: Dict[str, str], *, set_by: str = "conch keys",
                        verified: str = "") -> None:
    """Mark *updates* (name → value) as conch-managed: fingerprint, time,
    writer, and (when the caller verified live before saving) the
    verification outcome. Values never reach the manifest."""
    import time

    data = read_env_manifest()
    now = time.time()
    for name, value in updates.items():
        entry = {
            "fingerprint": key_fingerprint(value, 16),
            "set_at": now,
            "set_by": set_by,
        }
        if verified:
            entry["verified_at"] = now
            entry["verified"] = verified
        data["keys"][name] = entry
    _write_env_manifest(data)


def record_key_check(name: str, value: str, status: str, detail: str = "") -> None:
    """Remember the outcome of a live check for *name* (any source) so
    ``conch keys list`` can show "last verified" — keyed by fingerprint,
    so a rotated value never inherits the old verdict."""
    import time

    data = read_env_manifest()
    data["checks"][name] = {
        "fingerprint": key_fingerprint(value, 16),
        "at": time.time(),
        "status": status,
        "detail": str(detail)[:200],
    }
    managed = data["keys"].get(name)
    if isinstance(managed, dict) and managed.get("fingerprint") == key_fingerprint(value, 16):
        managed["verified_at"] = data["checks"][name]["at"]
        managed["verified"] = status
    _write_env_manifest(data)


def forget_managed_key(name: str) -> None:
    data = read_env_manifest()
    changed = bool(data["keys"].pop(name, None)) | bool(data["checks"].pop(name, None))
    if changed:
        _write_env_manifest(data)


# What the last load_env_file() did, by name: the values it mirrored from
# the file (so a later reload can tell "the file changed" from "the user
# exported something else"), managed names that overrode an inherited
# export, and unmanaged file entries shadowed by the inherited environment.
_ENV_LOAD_STATE: Dict[str, object] = {
    "from_file": {},
    "overrode": [],
    "shadowed": [],
}


def env_load_state() -> Dict[str, object]:
    """Snapshot of the last env-file load: ``overrode`` (managed names
    that replaced an inherited export), ``shadowed`` (unmanaged file
    entries the inherited environment won over), ``from_file`` (names
    whose value in ``os.environ`` came from the file)."""
    return {
        "from_file": dict(_ENV_LOAD_STATE["from_file"]),
        "overrode": list(_ENV_LOAD_STATE["overrode"]),
        "shadowed": list(_ENV_LOAD_STATE["shadowed"]),
    }


def load_env_file() -> List[str]:
    """Mirror the env file into ``os.environ``; return the names whose
    effective value changed.

    Rules, per ``NAME=value`` line:

    * not in the environment → set;
    * in the environment with the same value → nothing;
    * conch-managed (written through ``conch keys``/onboarding, see
      :func:`record_managed_keys`) → the file wins: the inherited value
      is replaced (recorded in ``env_load_state()["overrode"]``);
    * the environment still holds the value a *previous* load took from
      the file → the file changed since (a rotation): take the new value;
    * otherwise (hand-added line, different inherited export) → the
      environment wins (recorded in ``env_load_state()["shadowed"]``).

    Safe to call repeatedly; :func:`reload_env_file` is the explicit
    "re-read now" entry point for hot reloads.
    """
    entries = read_env_file()
    if not entries:
        return []
    managed = set(read_env_manifest()["keys"])
    from_file: Dict[str, str] = _ENV_LOAD_STATE["from_file"]  # type: ignore[assignment]
    overrode: List[str] = []
    shadowed: List[str] = []
    changed: List[str] = []
    for key, value in entries.items():
        current = os.environ.get(key)
        if current is None:
            os.environ[key] = value
            from_file[key] = value
            changed.append(key)
        elif current == value:
            from_file[key] = value
        elif key in managed:
            os.environ[key] = value
            from_file[key] = value
            overrode.append(key)
            changed.append(key)
        elif from_file.get(key) == current:
            os.environ[key] = value
            from_file[key] = value
            changed.append(key)
        else:
            shadowed.append(key)
    _ENV_LOAD_STATE["overrode"] = overrode
    _ENV_LOAD_STATE["shadowed"] = shadowed
    return changed


def reload_env_file() -> List[str]:
    """Re-read the env file now (after ``conch keys set``, on ``/reload``,
    on an auth failure, on SIGHUP in the daemon) and return the names
    whose effective value changed. Names removed from the file are left
    in the environment untouched — a running process never loses a key
    it was started with."""
    return load_env_file()


def _write_private_file(path: Path, text: str) -> None:
    """Write *text* to *path* atomically with 0600 perms (0700 parent):
    a temp file in the same directory, fsync, then rename over the
    target — so a crash mid-write never leaves a truncated key file, and
    keys never transit a world-readable state."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def set_config_values(updates: Dict[str, str], header: str = "") -> str:
    """Set ``key = value`` lines in the primary config file, in place.

    Existing assignments to the same key are replaced (first occurrence
    updated, duplicates dropped); new keys are appended. The file is
    created 0600 when missing — with *header* as its leading comment —
    and existing content, comments, and ordering are otherwise
    preserved. Returns the config path.
    """
    path = Path(get_config_path())
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = [header.rstrip()] if header else []
    remaining = dict(updates)
    output = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key} = {remaining.pop(key)}")
                continue
            if key in updates:
                continue  # duplicate assignment of an updated key
        output.append(line)
    for key, value in remaining.items():
        output.append(f"{key} = {value}")
    _write_private_file(path, "\n".join(output).rstrip("\n") + "\n")
    return str(path)


ENV_FILE_HEADER = (
    "# Conch credentials — loaded into the environment by conch itself"
    " (shell, conch-ask,\n"
    "# the edge daemon, workers, MCP server launches). Keep this file"
    " 0600.\n"
    "# Manage it with `conch keys` (list / check / set / rotate); keys"
    " saved that way are\n"
    "# authoritative over shell exports of the same name."
)


def _env_line_key(line: str) -> str:
    """The NAME of a ``NAME=…`` / ``export NAME=…`` line ("" otherwise)."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    if stripped.startswith("export "):
        stripped = stripped[len("export "):]
    return stripped.split("=", 1)[0].strip()


def set_env_values(updates: Dict[str, str], *, set_by: str = "conch keys",
                   verified: str = "", managed: bool = True) -> str:
    """Set ``KEY=value`` lines in the 0600 env file (see
    :func:`get_env_file_path`), mark them conch-managed in the manifest,
    and mirror them into ``os.environ`` so the running process sees
    them immediately. Returns the env path.

    Existing assignments are replaced in place (an ``export`` prefix is
    preserved; duplicates dropped); comments and other lines are kept
    byte-for-byte; new keys are appended. The write is atomic.

    Values are secrets: callers must never echo them, and this function
    never logs them.
    """
    path = Path(get_env_file_path())
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = ENV_FILE_HEADER.splitlines()
    remaining = dict(updates)
    output = []
    for line in lines:
        key = _env_line_key(line)
        if key and key in remaining:
            prefix = "export " if line.strip().startswith("export ") else ""
            output.append(f"{prefix}{key}={remaining.pop(key)}")
            continue
        if key and key in updates:
            continue  # duplicate assignment of an updated key
        output.append(line)
    for key, value in remaining.items():
        output.append(f"{key}={value}")
    _write_private_file(path, "\n".join(output).rstrip("\n") + "\n")
    if managed:
        record_managed_keys(updates, set_by=set_by, verified=verified)
    for key, value in updates.items():
        os.environ[key] = value
        _ENV_LOAD_STATE["from_file"][key] = value  # type: ignore[index]
    return str(path)


def remove_env_values(names) -> List[str]:
    """Drop ``NAME=…`` lines for *names* from the env file (comments and
    other lines untouched, atomic rewrite) and forget them in the
    manifest. The running process keeps its environment. Returns the
    names that were actually present."""
    path = Path(get_env_file_path())
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    wanted = {str(name) for name in names}
    removed: List[str] = []
    output = []
    for line in lines:
        key = _env_line_key(line)
        if key and key in wanted:
            if key not in removed:
                removed.append(key)
            continue
        output.append(line)
    if removed:
        _write_private_file(path, "\n".join(output).rstrip("\n") + "\n")
        for name in removed:
            forget_managed_key(name)
            _ENV_LOAD_STATE["from_file"].pop(name, None)  # type: ignore[union-attr]
    return removed


def resolve_editor(config: Optional[Dict[str, str]] = None) -> str:
    """Editor precedence: config ``editor``, then $VISUAL, then $EDITOR,
    then nano when installed, else vi.

    The value is a command string and may carry arguments
    (``editor = code --wait``); callers shlex-split it before appending
    the file path. One helper for every editor-backed surface (/notes
    now, /edit when the multiline branch merges) so the ``editor`` key
    always means the same thing. The nano floor is deliberate: on a
    machine with neither variable set, a modeless editor is a friendlier
    default than vi — set ``editor = vi`` to pin vi.
    """
    candidates = (
        (config or {}).get("editor"),
        os.environ.get("VISUAL"),
        os.environ.get("EDITOR"),
    )
    for candidate in candidates:
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    if shutil.which("nano"):
        return "nano"
    return "vi"


# ---------------------------------------------------------------------------
# Project-level context and config (plan 1.8)
# ---------------------------------------------------------------------------

PROJECT_CONTEXT_FILES = ("CONCH.md", "AGENTS.md")
# Budget so project instructions can't swamp a small local model (~1.5k tokens)
PROJECT_CONTEXT_MAX_CHARS = 6000


def _find_upwards(filenames, start_dir: str = "") -> Optional[Path]:
    """Return the first existing file among *filenames*, walking from
    *start_dir* (default cwd) up to the git root (inclusive) or fs root."""
    current = Path(start_dir or os.getcwd()).resolve()
    while True:
        for filename in filenames:
            candidate = current / filename
            if candidate.is_file():
                return candidate
        if (current / ".git").exists() or current.parent == current:
            return None
        current = current.parent


def find_project_rc(start_dir: str = "") -> Optional[Path]:
    """Nearest per-project .conchrc between cwd and the git root."""
    return _find_upwards((".conchrc",), start_dir)


def load_project_context(start_dir: str = "") -> str:
    """Contents of the nearest CONCH.md/AGENTS.md, capped for small models.

    These are persistent project instructions that live outside compactable
    history (the CLAUDE.md pattern); injected into the system prompt.
    """
    path = _find_upwards(PROJECT_CONTEXT_FILES, start_dir)
    if path is None:
        return ""
    try:
        text = path.read_text().strip()
    except OSError:
        return ""
    if not text:
        return ""
    if len(text) > PROJECT_CONTEXT_MAX_CHARS:
        text = text[:PROJECT_CONTEXT_MAX_CHARS] + "\n... [project context truncated]"
    return f"Project instructions (from {path}):\n{text}"


def load_config() -> Dict[str, str]:
    """Load config from the standard Conch locations.

    Precedence (last wins): defaults < ~/.config/conch/config < ~/.conchrc
    < per-project .conchrc (nearest file between cwd and the git root).

    The conch-owned key file (~/.config/conch/env) is mirrored into the
    environment first, so ``api_key_env`` lookups see onboarding-written
    keys everywhere — shell, one-shots, daemons — without profile edits.
    """
    load_env_file()
    config = dict(DEFAULT_CONFIG)
    explicit: Dict[str, str] = {}
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch"
    paths = [config_dir / "config", Path.home() / ".conchrc"]
    project_rc = find_project_rc()
    if project_rc is not None and project_rc != (Path.home() / ".conchrc"):
        paths.append(project_rc)
    for path in paths:
        explicit.update(_parse_config_file(path))
    for env_name, key in ENV_CONFIG_KEYS.items():
        if env_name in os.environ:
            explicit[key] = os.environ[env_name].strip()
    config.update(explicit)

    provider = config.get("provider", DEFAULT_CONFIG["provider"]).lower()
    config["provider"] = provider
    provider_defaults = {
        "cerebras": ("CEREBRAS_API_KEY", "gpt-oss-120b"),
        "bedrock": ("AWS_BEARER_TOKEN_BEDROCK", "moonshotai.kimi-k2.5"),
        "openrouter": ("OPENROUTER_API_KEY", "moonshotai/kimi-k3"),
        "openai": ("OPENAI_API_KEY", "gpt-4o-mini"),
        "anthropic": ("ANTHROPIC_API_KEY", "claude-sonnet-5"),
        "ollama": ("", "llama3.3"),
    }
    if provider in provider_defaults:
        default_key_env, default_model = provider_defaults[provider]
        if "api_key_env" not in explicit:
            config["api_key_env"] = default_key_env
        if "model" not in explicit or not config.get("model"):
            config["model"] = default_model
        if "chat_model" not in explicit or not config.get("chat_model"):
            config["chat_model"] = config["model"]

    if provider == "ollama":
        # base_url is shared across providers; remember it as the Ollama
        # endpoint so it survives provider switches and fallbacks.
        if config.get("base_url") and not config.get("ollama_base_url"):
            config["ollama_base_url"] = config["base_url"]
    elif provider == "custom":
        # Custom OpenAI-compatible endpoint (plan 2.4): vLLM, LM Studio, etc.
        # Keyless unless the user names a variable: the Anthropic default
        # from DEFAULT_CONFIG must not leak through (setdefault would keep
        # it, and /status would then claim api_key_env=ANTHROPIC_API_KEY).
        if "api_key_env" not in explicit:
            config["api_key_env"] = ""
        if config.get("base_url") and not config.get("custom_base_url"):
            config["custom_base_url"] = config["base_url"]
        model = (config.get("model") or "").strip()
        if "model" not in explicit or not model:
            # model inherited from defaults → the endpoint's custom_model wins
            model = (config.get("custom_model") or "").strip()
        config["model"] = model
        config["custom_model"] = (config.get("custom_model") or "").strip() or model
        # chat_model follows the endpoint's model unless explicitly set.
        if "chat_model" not in explicit or not config.get("chat_model"):
            config["chat_model"] = model
    elif provider not in provider_defaults:
        config.setdefault("api_key_env", "ANTHROPIC_API_KEY")
        config.setdefault("model", "claude-sonnet-5")
        config.setdefault("chat_model", config["model"])
    return config


def get_bool(cfg: dict, key: str, default: bool = False) -> bool:
    return str(cfg.get(key, str(default))).lower() in ("true", "1", "yes", "on")


def local_only_enabled(cfg: dict, provider: str = "") -> bool:
    value = str((cfg or {}).get("local_only", "auto")).strip().lower()
    if value == "auto":
        return (provider or (cfg or {}).get("provider", "")).lower() in (
            "ollama",
            "custom",
        )
    return value in ("true", "1", "yes", "on")


def get_int(cfg: dict, key: str, default: int = 0) -> int:
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default

