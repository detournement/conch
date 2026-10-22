"""The credential inventory: every key conch knows about or finds
configured — who consumes it, where its value effectively comes from,
its fingerprint, and whether a stale duplicate is lurking.

Sources, in the order the precedence rule weighs them (see
:func:`conch.config.load_env_file`):

* ``~/.config/conch/env`` — the env file (``managed`` when conch wrote
  the line through ``conch keys``/onboarding);
* the *inherited* process environment — the shell's exports, captured
  before conch loaded the env file, so "the daemon cannot see this one"
  and "this export shadows the saved key" are both decidable;
* ``~/.config/conch/mcp.json`` — ``${NAME}`` references (good) and
  inline secrets (flagged; ``conch keys migrate-mcp`` moves them);
* product credential providers (the Capitol registry bearers).

Values are read only to fingerprint them and to hand them to the live
verifiers; nothing here formats a value into any string.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from ..config import (
    get_bool,
    key_fingerprint,
    read_env_file,
    read_env_manifest,
)
from .shellenv import ExportSite, ShellEnvScan, describe_site, scan_shell_exports

Verifier = Callable[[float], Tuple[str, str]]

# Statuses a verifier may return / the table shows.
OK = "ok"
REJECTED = "rejected"
MISSING = "missing"
UNREACHABLE = "unreachable"
SKIPPED = "skipped"
ERROR = "error"
STALE = "stale"

PROVIDER_SERVICES = ("anthropic", "openai", "openrouter", "cerebras", "bedrock", "custom")

CHANNEL_TOKENS = (
    # (channel, config key naming the env var, default env var, config prefixes
    #  that mean "configured", the notify_channel value)
    ("slack", "slack_token_env", "SLACK_BOT_TOKEN", ("slack_",), "slack"),
    ("matrix", "matrix_token_env", "MATRIX_ACCESS_TOKEN", ("matrix_",), "matrix"),
    ("ntfy", "ntfy_token_env", "NTFY_TOKEN", ("ntfy_",), "ntfy"),
    ("email", "email_password_env", "EMAIL_PASSWORD", ("email_", "imap_", "smtp_"), "email"),
)


@dataclass
class Credential:
    name: str
    env_name: str = ""
    service: str = ""
    consumers: List[str] = field(default_factory=list)
    required: bool = False
    file_fp: str = ""
    env_fp: str = ""
    managed: bool = False
    effective: str = ""
    effective_fp: str = ""
    stale: str = ""
    export_sites: List[ExportSite] = field(default_factory=list)
    export_notes: List[str] = field(default_factory=list)
    inline_location: str = ""
    last_verified: str = ""
    notes: List[str] = field(default_factory=list)
    verify: Optional[Verifier] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    # filled by the live check
    status: str = ""
    detail: str = ""
    elapsed: float = 0.0
    alt_results: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    daemon: str = ""
    daemon_fp: str = ""

    @property
    def present(self) -> bool:
        return bool(self.effective_fp and self.effective_fp != "-")

    @property
    def settable(self) -> bool:
        return bool(self.env_name)

    def add_consumer(self, consumer: str, required: bool) -> None:
        if consumer not in self.consumers:
            self.consumers.append(consumer)
        self.required = self.required or required

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "env_name": self.env_name,
            "service": self.service,
            "consumers": list(self.consumers),
            "required": self.required,
            "source": self.effective,
            "fingerprint": self.effective_fp,
            "env_file_fingerprint": self.file_fp,
            "shell_env_fingerprint": self.env_fp,
            "managed": self.managed,
            "stale": self.stale,
            "exports": [site.location for site in self.export_sites],
            "export_notes": list(self.export_notes),
            "inline_location": self.inline_location,
            "last_verified": self.last_verified,
            "notes": list(self.notes),
            "status": self.status,
            "detail": self.detail,
            "daemon": self.daemon,
            "alternatives": {
                label: list(result) for label, result in self.alt_results.items()
            },
        }


@dataclass
class Inventory:
    credentials: List[Credential]
    config: Dict[str, Any]
    env_file: str
    env_file_exists: bool
    mcp_path: str
    mcp_servers: Dict[str, dict]
    mcp_error: str = ""
    shell_scan: Optional[ShellEnvScan] = None
    values: Dict[str, str] = field(default_factory=dict, repr=False)

    def get(self, name: str) -> Optional[Credential]:
        for credential in self.credentials:
            if credential.name == name or (credential.env_name and credential.env_name == name):
                return credential
        return None

    def required_names(self) -> List[str]:
        names: List[str] = []
        for credential in self.credentials:
            if credential.required and credential.env_name and credential.env_name not in names:
                names.append(credential.env_name)
        return names

    def env_names(self) -> List[str]:
        names: List[str] = []
        for credential in self.credentials:
            if credential.env_name and credential.env_name not in names:
                names.append(credential.env_name)
        return names


# ---------------------------------------------------------------------------
# mcp.json
# ---------------------------------------------------------------------------

def mcp_config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch" / "mcp.json"


def load_mcp_servers(path: Optional[Path] = None) -> Tuple[Dict[str, dict], str]:
    path = Path(path or mcp_config_path())
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError) as exc:
        return {}, f"{path}: {exc}"
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}, f"{path}: no mcpServers object"
    return {str(k): v for k, v in servers.items() if isinstance(v, dict)}, ""


@dataclass
class InlineSecret:
    server: str
    field: str          # "env.JIRA_API_TOKEN" / "headers.Authorization" / "url?api_key" / "args[3]"
    suggested_name: str
    value: str = field(repr=False, default="")

    @property
    def location(self) -> str:
        return f"{self.server} {self.field}"


_FLAG_SECRET_RE = re.compile(r"^--?(?:[a-z0-9-]*(?:token|key|secret|password|bearer|auth)[a-z0-9-]*)$", re.I)


def _looks_like_secret_value(value: str) -> bool:
    value = str(value or "").strip()
    if len(value) < 16 or " " in value:
        return False
    if re.fullmatch(r"https?://\S+", value):
        return False
    if "@" in value and "." in value and " " not in value and len(value) < 64:
        return False  # an email address (JIRA_USERNAME) is not a secret
    return bool(re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value)) or value.startswith(("sk-", "xox", "ghp_", "glpat-"))


_FIELD_ALIASES = {"authorization": "TOKEN", "x-api-key": "API_KEY", "api-key": "API_KEY",
                  "apikey": "API_KEY", "x-auth-token": "TOKEN"}
_SCHEME_RE = re.compile(r"^(Bearer|Basic|Token|token)\s+(\S+)$")


def _suggest_name(server: str, field_name: str) -> str:
    field_name = _FIELD_ALIASES.get(field_name.lower(), field_name)
    base = re.sub(r"[^A-Za-z0-9]+", "_", f"MCP_{server}_{field_name}").strip("_").upper()
    return base


def redact_url(url: str) -> str:
    """A URL safe to print: the query string (where inline keys hide) is
    replaced by ``?…``."""
    parts = urlsplit(str(url or ""))
    if not parts.query:
        return str(url or "")
    return urlunsplit(parts._replace(query="")) + "?…"


def inline_secrets(servers: Dict[str, dict]) -> List[InlineSecret]:
    """Inline (non-``${NAME}``) secret values in mcp.json entries, by the
    name heuristic for env/header/query keys and a value heuristic for
    positional args after a credential-looking flag."""
    from ..mcp import secret_like_name

    found: List[InlineSecret] = []
    for server, cfg in servers.items():
        for key, value in (cfg.get("env") or {}).items():
            if isinstance(value, str) and value.strip() and "$" not in value and secret_like_name(key):
                found.append(InlineSecret(server, f"env.{key}", str(key), value))
        for key, value in (cfg.get("headers") or {}).items():
            if not isinstance(value, str) or "$" in value or not value.strip():
                continue
            if secret_like_name(key) or key.lower() == "authorization" or _looks_like_secret_value(value.split(" ", 1)[-1]):
                scheme = _SCHEME_RE.match(value.strip())
                token = scheme.group(2) if scheme else value
                found.append(InlineSecret(server, f"headers.{key}", _suggest_name(server, key), token))
        url = cfg.get("url")
        if isinstance(url, str) and "?" in url and "$" not in url:
            for param, value in parse_qsl(urlsplit(url).query, keep_blank_values=False):
                if secret_like_name(param) or _looks_like_secret_value(value):
                    found.append(InlineSecret(server, f"url?{param}", _suggest_name(server, param), value))
        args = cfg.get("args") or []
        for index, arg in enumerate(args):
            if not isinstance(arg, str) or "$" in arg:
                continue
            if "=" in arg and arg.startswith("--"):
                flag, _, value = arg.partition("=")
                if _FLAG_SECRET_RE.match(flag) and value.strip():
                    found.append(InlineSecret(server, f"args[{index}]", _suggest_name(server, flag.strip("-")), value))
            elif index > 0 and isinstance(args[index - 1], str) and _FLAG_SECRET_RE.match(args[index - 1]) and arg.strip():
                found.append(InlineSecret(server, f"args[{index}]", _suggest_name(server, args[index - 1].strip("-")), arg))
    return found


def mcp_server_service(name: str, cfg: dict) -> str:
    env = cfg.get("env") or {}
    if any(str(k).upper().startswith("JIRA_") for k in env) or re.search(r"atlassian|jira", name, re.I):
        return "jira"
    url = str(cfg.get("url") or "")
    if "composio" in url or "composio" in name.lower():
        return "composio-mcp"
    return "mcp"


# ---------------------------------------------------------------------------
# Building the inventory
# ---------------------------------------------------------------------------

def _format_when(stamp: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(stamp)))
    except (TypeError, ValueError, OverflowError):
        return ""


def _last_verified(manifest: dict, name: str, fingerprint16: str) -> str:
    check = manifest.get("checks", {}).get(name)
    if isinstance(check, dict) and check.get("fingerprint") == fingerprint16:
        when = _format_when(check.get("at", 0))
        return f"{when} {check.get('status', '')}".strip()
    managed = manifest.get("keys", {}).get(name)
    if isinstance(managed, dict) and managed.get("fingerprint") == fingerprint16 and managed.get("verified_at"):
        return f"{_format_when(managed['verified_at'])} {managed.get('verified', '')}".strip()
    return ""


class InventoryBuilder:
    """Collects declared consumers, then resolves each NAME against the
    env file, the inherited environment, and mcp.json."""

    def __init__(self, config: dict, inherited: Dict[str, str], *,
                 home: Optional[Path] = None, cwd: Optional[Path] = None,
                 mcp_path: Optional[Path] = None,
                 scan_profiles: bool = True, with_plugins: bool = True):
        self.config = dict(config or {})
        self.inherited = dict(inherited or {})
        self.home = Path(home or Path.home())
        self.cwd = Path(cwd or os.getcwd())
        self.mcp_path = Path(mcp_path or mcp_config_path())
        self.scan_profiles = scan_profiles
        self.with_plugins = with_plugins
        self.file_entries = read_env_file()
        self.manifest = read_env_manifest()
        self.managed = set(self.manifest.get("keys", {})) & set(self.file_entries)
        self.by_name: Dict[str, Credential] = {}
        self.order: List[str] = []
        self.servers, self.mcp_error = load_mcp_servers(self.mcp_path)
        self.values: Dict[str, str] = {}

    # -- declarations -------------------------------------------------------

    def declare(self, env_name: str, service: str, consumer: str, *,
                required: bool, extra: Optional[dict] = None) -> Credential:
        credential = self.by_name.get(env_name)
        if credential is None:
            credential = Credential(name=env_name, env_name=env_name, service=service)
            self.by_name[env_name] = credential
            self.order.append(env_name)
        if not credential.service or credential.service == "unknown":
            credential.service = service
        credential.add_consumer(consumer, required)
        if extra:
            for key, value in extra.items():
                credential.extra.setdefault(key, value)
        return credential

    def _declare_providers(self) -> None:
        from ..providers import DEFAULT_API_KEY_ENVS, api_key_env_for

        config = self.config
        provider = str(config.get("provider") or "").lower()
        model = str(config.get("chat_model") or config.get("model") or "").strip()
        if provider in PROVIDER_SERVICES:
            key_env = api_key_env_for(provider, config)
            if key_env:
                label = f"{provider} (active model {model})" if model else f"{provider} (active provider)"
                if provider == "custom":
                    from ..providers import get_custom_base_url

                    label = f"custom endpoint {get_custom_base_url(config)} (active model {model})"
                self.declare(key_env, provider, label, required=True,
                             extra={"provider": provider, "config": dict(config)})
        weak = str(config.get("weak_provider") or "").lower()
        if weak in PROVIDER_SERVICES and weak != provider:
            key_env = DEFAULT_API_KEY_ENVS.get(weak, "")
            if key_env:
                self.declare(key_env, weak, f"{weak} (weak model: summaries, compaction, reviews)",
                             required=False, extra={"provider": weak})
        from ..modelcheck import parse_fallback_models

        for entry in parse_fallback_models(str(config.get("fallback_models") or "")):
            fb_provider = entry.split("/", 1)[0].strip().lower()
            if fb_provider in PROVIDER_SERVICES and fb_provider != provider:
                key_env = DEFAULT_API_KEY_ENVS.get(fb_provider, "")
                if key_env:
                    self.declare(key_env, fb_provider, f"{fb_provider} (fallback_models: {entry})",
                                 required=False, extra={"provider": fb_provider})
        for other, key_env in DEFAULT_API_KEY_ENVS.items():
            if not key_env or other == provider or key_env in self.by_name:
                continue
            if key_env in self.file_entries or self.inherited.get(key_env, "").strip():
                self.declare(key_env, other, f"{other} (available: /provider {other}, fallback)",
                             required=False, extra={"provider": other})

    def _declare_components(self) -> None:
        config = self.config
        status_url = str(config.get("status_page_url") or "").strip()
        if status_url:
            env = str(config.get("status_page_token_env") or "CONCH_STATUS_WRITE_TOKEN").strip()
            self.declare(env, "status", f"status page {status_url} (write token)", required=True,
                         extra={"url": status_url})
        backend = str(config.get("exec_backend") or "local").strip().lower()
        e2b_env = str(config.get("e2b_api_key_env") or "E2B_API_KEY").strip()
        if backend == "e2b":
            self.declare(e2b_env, "e2b", "sandbox e2b (exec_backend=e2b)", required=True,
                         extra={"url": str(config.get("e2b_api_url") or "https://api.e2b.app")})
        elif e2b_env in self.file_entries or self.inherited.get(e2b_env, "").strip():
            self.declare(e2b_env, "e2b", "sandbox e2b (/sandbox e2b)", required=False,
                         extra={"url": str(config.get("e2b_api_url") or "https://api.e2b.app")})
        llamaidx_url = str(config.get("llamaidx_url") or "").strip()
        llamaidx_env = str(config.get("llamaidx_token_env") or "").strip()
        if llamaidx_url and llamaidx_env:
            self.declare(llamaidx_env, "llamaidx", f"llama-idx registry {llamaidx_url} (read token)",
                         required=True, extra={"url": llamaidx_url})
        remote = get_bool(config, "remote_enabled")
        notify = str(config.get("notify_channel") or "").strip().lower()
        push = str(config.get("notify_push") or "").strip().lower()
        for channel, env_key, default_env, prefixes, notify_value in CHANNEL_TOKENS:
            configured = any(str(key).startswith(prefixes) for key in config) or notify == notify_value
            if channel == "ntfy":
                configured = configured or push == "ntfy"
            env = str(config.get(env_key) or default_env).strip()
            present = env in self.file_entries or self.inherited.get(env, "").strip()
            if not (configured or present):
                continue
            required = configured and (remote or notify == notify_value or (channel == "ntfy" and push == "ntfy"))
            self.declare(env, channel, f"{channel} channel" + (" (remote/notify)" if required else ""),
                         required=required, extra={"config": dict(config)})
        if any(str(key).startswith(("twilio_", "sms_")) for key in config) or notify == "sms":
            for env in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"):
                self.declare(env, "twilio", "sms channel (Twilio)", required=remote or notify == "sms")
        composio = "COMPOSIO_API_KEY"
        if composio in self.file_entries or self.inherited.get(composio, "").strip() or composio in self.by_name:
            self.declare(composio, "composio", "composio (/connect, /apps)", required=False)

    def _declare_mcp(self) -> None:
        from ..mcp import referenced_env_names

        for server, cfg in self.servers.items():
            refs = referenced_env_names({key: cfg.get(key) for key in ("headers", "env", "url", "args")})
            for name in refs:
                if name.upper().startswith("JIRA_"):
                    ref_service = "jira"
                elif name.upper() == "COMPOSIO_API_KEY":
                    ref_service = "composio"
                else:
                    ref_service = "mcp-ref"  # verified through the server's tools/list
                self.declare(name, ref_service, f"mcp/{server}", required=True,
                             extra={"server": server, "server_config": cfg})
            transport = (
                "http " + redact_url(str(cfg.get("url") or "")) if cfg.get("type") == "http"
                else "stdio " + str(cfg.get("command") or "")
            )
            server_cred = Credential(
                name=f"mcp/{server}", env_name="", service="mcp-server",
                consumers=[f"MCP server {server} ({transport.strip()})"],
                required=True, extra={"server": server, "server_config": cfg, "refs": refs},
            )
            if refs:
                server_cred.effective = "references " + ", ".join(f"${{{n}}}" for n in refs)
            else:
                server_cred.effective = "no credential in mcp.json"
            server_cred.effective_fp = ""
            key = f"mcp/{server}"
            self.by_name[key] = server_cred
            self.order.append(key)
        for secret in inline_secrets(self.servers):
            name = f"{secret.server}:{secret.field}"
            credential = Credential(
                name=name, env_name="", service="mcp-inline",
                consumers=[f"mcp/{secret.server}"], required=True,
                effective="mcp.json inline", effective_fp=key_fingerprint(secret.value),
                inline_location=f"{self.mcp_path.name} {secret.location}",
                stale=(
                    f"inline secret in {self.mcp_path} ({secret.location}) — cannot be"
                    f" rotated with conch keys set; run `conch keys migrate-mcp --apply`"
                    f" to move it into the env file as ${{{secret.suggested_name}}}"
                ),
                extra={"server": secret.server, "field": secret.field,
                       "suggested_name": secret.suggested_name},
            )
            self.values[name] = secret.value
            self.by_name[name] = credential
            self.order.append(name)
            server_cred = self.by_name.get(f"mcp/{secret.server}")
            if server_cred is not None:
                server_cred.effective = (server_cred.effective + "; " if server_cred.effective and not server_cred.effective.startswith("no credential") else "") + f"inline secret in {secret.field}"

    def _declare_unconsumed(self) -> None:
        for name in self.file_entries:
            if name not in self.by_name:
                self.declare(name, "unknown", "(no consumer found in the active config)", required=False)

    def _declare_plugins(self) -> None:
        if not self.with_plugins:
            return
        try:
            from ..plugins import credential_providers, load_builtin_plugins

            load_builtin_plugins()
            providers = credential_providers()
        except Exception:
            return
        for provider in providers:
            try:
                entries = provider.credentials(self.config) or []
            except Exception as exc:  # a product must never take the inventory down
                entries = [{
                    "name": f"{type(provider).__name__}", "env_name": "",
                    "service": "plugin", "consumers": ["(credential provider failed)"],
                    "required": False, "source": "", "fingerprint": "",
                    "notes": [f"{type(exc).__name__}: {exc}"], "verify": None,
                }]
            for entry in entries:
                self._adopt_plugin_entry(entry)

    def _adopt_plugin_entry(self, entry: dict) -> None:
        env_name = str(entry.get("env_name") or "")
        name = str(entry.get("name") or env_name or "?")
        if env_name:
            credential = self.declare(
                env_name, str(entry.get("service") or "plugin"),
                "; ".join(entry.get("consumers") or []) or name,
                required=bool(entry.get("required")),
            )
            credential.notes.extend(entry.get("notes") or [])
            credential.extra["plugin_verify"] = entry.get("verify")
            return
        credential = Credential(
            name=name, env_name="", service=str(entry.get("service") or "plugin"),
            consumers=list(entry.get("consumers") or []), required=bool(entry.get("required")),
            effective=str(entry.get("source") or "") or "missing",
            effective_fp=str(entry.get("fingerprint") or ""),
            notes=list(entry.get("notes") or []), verify=entry.get("verify"),
        )
        if credential.effective.startswith("registry:"):
            credential.effective = f"capitol registry ({credential.effective[len('registry:'):]})"
        key = name
        suffix = 2
        while key in self.by_name:
            key = f"{name} #{suffix}"
            suffix += 1
        credential.name = key
        self.by_name[key] = credential
        self.order.append(key)

    # -- resolution -----------------------------------------------------------

    def _resolve(self, credential: Credential, scan: Optional[ShellEnvScan]) -> None:
        name = credential.env_name
        if not name:
            return
        file_value = self.file_entries.get(name, "")
        env_value = self.inherited.get(name, "")
        credential.file_fp = key_fingerprint(file_value) if file_value else ""
        credential.env_fp = key_fingerprint(env_value) if env_value.strip() else ""
        credential.managed = name in self.managed
        effective_value = ""
        if file_value and credential.managed:
            credential.effective = "env file"
            effective_value = file_value
            if env_value.strip() and env_value != file_value:
                credential.stale = (
                    "the shell environment also exports this name with a different"
                    f" value ({credential.env_fp}); the saved key wins (conch-managed)"
                    " — the export is stale"
                )
        elif file_value:
            if env_value.strip() and env_value != file_value:
                credential.effective = "shell env"
                effective_value = env_value
                credential.stale = (
                    f"the env file holds a different value ({credential.file_fp}) that is"
                    " NOT used: the line was not written by conch, so the shell export"
                    f" wins — `conch keys set {name}` makes the saved key authoritative"
                )
            else:
                credential.effective = "env file" + ("" if credential.managed else " (hand-added)")
                effective_value = file_value
        elif env_value.strip():
            credential.effective = "shell env (daemon-blind)"
            effective_value = env_value
        else:
            credential.effective = "missing"
        credential.effective_fp = key_fingerprint(effective_value) if effective_value else "-"
        if effective_value:
            self.values[credential.name] = effective_value
        if file_value and env_value.strip() and env_value != file_value:
            self.values[f"{credential.name}@env-file"] = file_value
            self.values[f"{credential.name}@shell-env"] = env_value
        credential.last_verified = _last_verified(
            self.manifest, name, key_fingerprint(effective_value, 16) if effective_value else ""
        )
        if scan is not None:
            credential.export_sites = scan.for_name(name)
            credential.export_notes = [
                describe_site(site, credential.file_fp, credential.managed)
                for site in credential.export_sites
            ]

    def build(self) -> Inventory:
        self._declare_providers()
        self._declare_components()
        self._declare_mcp()
        self._declare_plugins()
        self._declare_unconsumed()
        scan = None
        if self.scan_profiles:
            names = [c.env_name for c in self.by_name.values() if c.env_name]
            try:
                scan = scan_shell_exports(names, home=self.home, cwd=self.cwd)
            except Exception:
                scan = None
        for credential in self.by_name.values():
            self._resolve(credential, scan)
        credentials = [self.by_name[key] for key in self.order]
        credentials.sort(key=lambda c: (not c.required, c.service == "unknown", c.name))
        return Inventory(
            credentials=credentials, config=self.config,
            env_file=str(Path(os.environ.get("XDG_CONFIG_HOME", self.home / ".config")) / "conch" / "env"),
            env_file_exists=bool(self.file_entries) or Path(
                os.environ.get("XDG_CONFIG_HOME", self.home / ".config"), "conch", "env"
            ).exists(),
            mcp_path=str(self.mcp_path), mcp_servers=self.servers,
            mcp_error=self.mcp_error, shell_scan=scan, values=self.values,
        )


def build_inventory(config: dict, inherited: Dict[str, str], **kwargs) -> Inventory:
    return InventoryBuilder(config, inherited, **kwargs).build()
