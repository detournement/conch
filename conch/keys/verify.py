"""Live verification of credentials — the cheapest authenticated call
per service, classified into ``ok`` / ``rejected`` / ``missing`` /
``unreachable`` / ``skipped`` / ``error``.

Provider keys go through :func:`conch.modelcheck.probe_key` (the same
request plumbing and failure classes as the startup model check — no
second probe). Jira uses ``/rest/api/3/myself``, Composio its toolkit
list, E2B its sandbox list, Slack ``auth.test``, MCP servers their own
``initialize``/``tools/list`` handshake, the Capitol registry the agent
card (through the product's credential provider). The status page's
ingest endpoint is write-only, so that token gets a format check.

Every verifier takes the value explicitly (never through the process
environment) so several can run in parallel and a stale duplicate can
be verified alongside the effective value. Nothing here prints a value.
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .inventory import (
    ERROR,
    MISSING,
    OK,
    REJECTED,
    SKIPPED,
    UNREACHABLE,
    Credential,
    Inventory,
)

DEFAULT_TIMEOUT = 8.0
MCP_TIMEOUT = 20.0

COMPOSIO_API_BASE = "https://backend.composio.dev"
SLACK_API_BASE = "https://slack.com/api"


def _base(name: str, default: str) -> str:
    return (os.environ.get(name, "").strip() or default).rstrip("/")


def _http(url: str, headers: Dict[str, str], timeout: float,
          method: str = "GET", body: Optional[bytes] = None) -> Tuple[int, str]:
    merged = {"User-Agent": "conch-keys/1.0"}
    merged.update(headers)
    req = urllib.request.Request(url, data=body, headers=merged, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return int(getattr(response, "status", 200) or 200), response.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(4096).decode("utf-8", "replace")
        except Exception:
            text = ""
        finally:
            try:
                exc.close()
            except Exception:
                pass
        return int(exc.code), text


def _classify(status: int, text: str, service: str, name: str) -> Tuple[str, str]:
    from ..providers import auth_rejected_message

    if 200 <= status < 300:
        return OK, f"{name} accepted by {service} (HTTP {status})"
    if status in (401, 403):
        return REJECTED, auth_rejected_message(service, name, status, _short(text))
    if status == 429:
        return OK, f"{name} accepted by {service} (rate limited: HTTP 429 — the credential itself is valid)"
    if status >= 500:
        return UNREACHABLE, f"{service} answered HTTP {status} ({_short(text)})"
    return ERROR, f"{service} answered HTTP {status} ({_short(text)})"


def _short(text: str, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("message", "error", "detail", "errorMessages"):
                if data.get(key):
                    text = str(data[key])
                    break
    except ValueError:
        pass
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _transport(exc: BaseException, where: str) -> Tuple[str, str]:
    reason = getattr(exc, "reason", exc)
    text = str(reason) or type(exc).__name__
    if "timed out" in text.lower():
        return UNREACHABLE, f"{where} timed out"
    return UNREACHABLE, f"{where} unreachable ({text})"


# ---------------------------------------------------------------------------
# Per-service verifiers (value passed explicitly)
# ---------------------------------------------------------------------------

def verify_provider(provider: str, name: str, value: str, config: dict,
                    timeout: float) -> Tuple[str, str]:
    from ..modelcheck import AUTH_FAILED, NO_KEY, OK as PROBE_OK, TIMEOUT, UNREACHABLE as PROBE_UNREACHABLE, probe_key

    if not value:
        return MISSING, f"{name} is not set"
    merged = dict(config or {})
    merged["provider"] = provider
    merged["api_key_env"] = name
    result = probe_key(provider, merged, timeout=timeout, api_key=value)
    if result.status == PROBE_OK:
        return OK, result.detail
    if result.status == AUTH_FAILED:
        return REJECTED, result.detail
    if result.status == NO_KEY:
        return MISSING, result.detail
    if result.status in (TIMEOUT, PROBE_UNREACHABLE):
        return UNREACHABLE, result.detail
    return ERROR, result.describe()


def verify_jira(url: str, username: str, token: str, timeout: float,
                name: str = "JIRA_API_TOKEN") -> Tuple[str, str]:
    if not token:
        return MISSING, f"{name} is not set"
    if not url:
        return SKIPPED, "JIRA_URL is not set for this server, cannot probe /rest/api/3/myself"
    raw = f"{username}:{token}".encode("utf-8")
    headers = {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
               "Accept": "application/json"}
    target = url.rstrip("/") + "/rest/api/3/myself"
    try:
        status, text = _http(target, headers, timeout)
    except Exception as exc:
        return _transport(exc, target)
    if 200 <= status < 300:
        who = ""
        try:
            who = str(json.loads(text).get("emailAddress") or json.loads(text).get("displayName") or "")
        except ValueError:
            pass
        return OK, f"{name} accepted by Jira ({url})" + (f" as {who}" if who else "")
    return _classify(status, text, f"Jira ({url})", name)


def verify_composio(value: str, timeout: float, name: str = "COMPOSIO_API_KEY") -> Tuple[str, str]:
    if not value:
        return MISSING, f"{name} is not set"
    target = _base("CONCH_COMPOSIO_API_BASE", COMPOSIO_API_BASE) + "/api/v3/toolkits?limit=1"
    try:
        status, text = _http(target, {"x-api-key": value, "Accept": "application/json"}, timeout)
    except Exception as exc:
        return _transport(exc, "backend.composio.dev")
    return _classify(status, text, "composio", name)


def verify_e2b(value: str, base_url: str, timeout: float, name: str = "E2B_API_KEY") -> Tuple[str, str]:
    if not value:
        return MISSING, f"{name} is not set"
    target = (base_url or "https://api.e2b.app").rstrip("/") + "/sandboxes"
    try:
        status, text = _http(target, {"X-API-Key": value, "Accept": "application/json"}, timeout)
    except Exception as exc:
        return _transport(exc, target)
    return _classify(status, text, "e2b", name)


def verify_slack(value: str, timeout: float, name: str = "SLACK_BOT_TOKEN") -> Tuple[str, str]:
    if not value:
        return MISSING, f"{name} is not set"
    target = _base("CONCH_SLACK_API_BASE", SLACK_API_BASE) + "/auth.test"
    try:
        status, text = _http(target, {"Authorization": f"Bearer {value}"}, timeout, method="POST", body=b"")
    except Exception as exc:
        return _transport(exc, "slack.com")
    try:
        data = json.loads(text)
    except ValueError:
        data = {}
    if isinstance(data, dict) and data.get("ok"):
        return OK, f"{name} accepted by slack (team {data.get('team', '?')})"
    if isinstance(data, dict) and data.get("error") in ("invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired"):
        from ..providers import auth_rejected_message

        return REJECTED, auth_rejected_message("slack", name, 401, str(data.get("error")))
    return _classify(status, text, "slack", name)


def verify_matrix(value: str, homeserver: str, timeout: float,
                  name: str = "MATRIX_ACCESS_TOKEN") -> Tuple[str, str]:
    if not value:
        return MISSING, f"{name} is not set"
    if not homeserver:
        return SKIPPED, "matrix_homeserver is not configured; cannot call /whoami"
    target = homeserver.rstrip("/") + "/_matrix/client/v3/account/whoami"
    try:
        status, text = _http(target, {"Authorization": f"Bearer {value}"}, timeout)
    except Exception as exc:
        return _transport(exc, homeserver)
    return _classify(status, text, f"matrix ({homeserver})", name)


def verify_llamaidx(value: str, url: str, timeout: float, name: str) -> Tuple[str, str]:
    if not value:
        return MISSING, f"{name} is not set"
    from ..llamaidx import get_llamaidx_url

    base = get_llamaidx_url({"llamaidx_url": url})
    if not base:
        return SKIPPED, "llamaidx_url is not configured"
    target = base + "/v1/models"
    try:
        status, text = _http(target, {"Authorization": f"Bearer {value}"}, timeout)
    except Exception as exc:
        return _transport(exc, base)
    return _classify(status, text, f"llama-idx ({base})", name)


def verify_format_only(value: str, name: str, why: str) -> Tuple[str, str]:
    if not value:
        return MISSING, f"{name} is not set"
    problem = format_problem(name, value)
    if problem:
        return ERROR, f"{why}; format check: {problem}"
    return SKIPPED, f"{why}; present ({len(value)} chars, format ok)"


def verify_mcp_server(server: str, cfg: dict, values: Dict[str, str],
                      timeout: float) -> Tuple[str, str]:
    """Exercise one mcp.json server's auth the way a session would: HTTP
    servers get the Streamable-HTTP handshake plus ``tools/list``; stdio
    servers are started and asked for ``tools/list`` (bounded)."""
    from ..mcp import (
        HttpMcpClient,
        McpConfigError,
        StdioMcpClient,
        _expand_env,
        expand_references,
        referenced_env_names,
    )

    refs = referenced_env_names({key: cfg.get(key) for key in ("headers", "env", "url", "args")})
    previous = {name: os.environ.get(name) for name in refs}
    try:
        for name in refs:
            if values.get(name):
                os.environ[name] = values[name]
        try:
            if cfg.get("type") == "http" and cfg.get("url"):
                client = HttpMcpClient(
                    server, expand_references(str(cfg["url"]), server=server, field="url"),
                    _expand_env(cfg.get("headers"), server=server, field="headers"),
                    connect_timeout=min(timeout, 10.0), timeout=timeout,
                )
            elif cfg.get("command"):
                client = StdioMcpClient(server, str(cfg["command"]), list(cfg.get("args") or []),
                                        cfg.get("env"), read_timeout=timeout)
            else:
                return SKIPPED, "entry has neither url nor command"
        except McpConfigError as exc:
            return MISSING, str(exc)
        client.credential_names = refs
        try:
            tools = client.list_tools()
            error = str(getattr(client, "last_error", "") or "")
            unreachable = bool(getattr(client, "unreachable", False))
        finally:
            try:
                client.close()
            except Exception:
                pass
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    if not error:
        return OK, f"mcp/{server} answered tools/list ({len(tools)} tools)"
    if unreachable:
        return UNREACHABLE, f"mcp/{server} {error}"
    if re.search(r"HTTP (401|403)|was rejected by", error):
        return REJECTED, error
    if error.startswith("mcp.json:"):
        return MISSING, error
    return ERROR, f"mcp/{server}: {error}"


# ---------------------------------------------------------------------------
# Format sanity (for `set`)
# ---------------------------------------------------------------------------

_PREFIX_HINTS = {
    "ANTHROPIC_API_KEY": ("sk-ant-",),
    "OPENAI_API_KEY": ("sk-",),
    "OPENROUTER_API_KEY": ("sk-or-",),
    "SLACK_BOT_TOKEN": ("xoxb-", "xoxp-"),
    "E2B_API_KEY": ("e2b_",),
}


def format_problem(name: str, value: str) -> str:
    """A hard problem with a pasted value ("" when plausible): control
    characters, embedded whitespace, placeholder text, implausible
    length. Prefix mismatches are soft (see :func:`format_warning`)."""
    if not value:
        return "empty value"
    if any(ch.isspace() for ch in value):
        return "contains whitespace (a stray newline or space from the paste?)"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return "contains control characters"
    if re.fullmatch(r"<.*>|\$\{?\w+\}?|xxx+|your[-_ ]?key.*|changeme|todo", value, re.I):
        return "looks like a placeholder, not a key"
    if len(value) < 8:
        return f"only {len(value)} characters — too short for a credential"
    return ""


def format_warning(name: str, value: str) -> str:
    hints = _PREFIX_HINTS.get(name)
    if hints and not value.startswith(hints):
        return f"{name} values usually start with {' or '.join(hints)}; this one does not (saving anyway)"
    return ""


# ---------------------------------------------------------------------------
# Running the checks
# ---------------------------------------------------------------------------

def attach_verifiers(inventory: Inventory, timeout: float = DEFAULT_TIMEOUT) -> None:
    """Give every credential a ``verify(timeout)`` closure bound to its
    effective value (and alternative values when a stale duplicate
    exists)."""
    config = inventory.config
    values = inventory.values

    def verifier_for(credential: Credential, value_key: str):
        name = credential.env_name or credential.name
        service = credential.service
        value = values.get(value_key, "")
        extra = credential.extra
        if service in ("anthropic", "openai", "openrouter", "cerebras", "bedrock", "custom"):
            provider_config = extra.get("config") or config
            return lambda t: verify_provider(service, name, value, provider_config, t)
        if service == "jira":
            server_cfg = extra.get("server_config") or _jira_server(inventory)
            env = dict((server_cfg or {}).get("env") or {})
            url = _resolve_literal(env.get("JIRA_URL", ""), values, inventory)
            user = _resolve_literal(env.get("JIRA_USERNAME", ""), values, inventory)
            if name == "JIRA_URL" or name == "JIRA_USERNAME":
                return lambda t: (SKIPPED, f"{name} is not a secret; verified through JIRA_API_TOKEN")
            return lambda t: verify_jira(url, user, value, t, name=name)
        if service in ("composio", "composio-mcp"):
            return lambda t: verify_composio(value, t, name=name)
        if service == "e2b":
            return lambda t: verify_e2b(value, str(extra.get("url") or ""), t, name=name)
        if service == "slack":
            return lambda t: verify_slack(value, t, name=name)
        if service == "matrix":
            homeserver = str((extra.get("config") or config).get("matrix_homeserver") or "")
            return lambda t: verify_matrix(value, homeserver, t, name=name)
        if service == "llamaidx":
            return lambda t: verify_llamaidx(value, str(extra.get("url") or ""), t, name)
        if service == "status":
            return lambda t: verify_format_only(
                value, name, "status page ingest is write-only (no read probe)"
            )
        if service in ("ntfy", "twilio", "email"):
            return lambda t: verify_format_only(value, name, f"no cheap read-only probe for {service}")
        if service == "mcp-server":
            server = str(extra.get("server") or "")
            cfg = extra.get("server_config") or {}
            return lambda t: verify_mcp_server(server, cfg, values, max(t, MCP_TIMEOUT))
        if service == "mcp-inline":
            field_name = str(extra.get("field") or "")
            server_cfg = next(
                (cfg for s, cfg in inventory.mcp_servers.items() if s == extra.get("server")), {}
            )
            if field_name.upper().endswith("JIRA_API_TOKEN"):
                env = dict((server_cfg or {}).get("env") or {})
                url = _resolve_literal(env.get("JIRA_URL", ""), values, inventory)
                user = _resolve_literal(env.get("JIRA_USERNAME", ""), values, inventory)
                return lambda t: verify_jira(url, user, value, t, name=credential.name)
            if "composio" in str(extra.get("server") or "").lower() or "composio" in str(
                (server_cfg or {}).get("url") or ""
            ):
                return lambda t: verify_composio(value, t, name=credential.name)
            return lambda t: (
                SKIPPED,
                f"inline value verified through mcp/{extra.get('server')}; migrate it with"
                " `conch keys migrate-mcp --apply`",
            )
        if service == "mcp-ref":
            return lambda t: (SKIPPED, f"verified through mcp/{extra.get('server')} (tools/list)")
        plugin_verify = extra.get("plugin_verify")
        if callable(plugin_verify):
            return plugin_verify
        if service == "unknown":
            return lambda t: (SKIPPED, "no consumer in the active config knows how to verify this name")
        return lambda t: (SKIPPED, f"no verifier for {service}")

    for credential in inventory.credentials:
        if credential.verify is not None:
            continue  # product-provided
        credential.verify = verifier_for(credential, credential.name)
        for label in ("env-file", "shell-env"):
            alt_key = f"{credential.name}@{label}"
            if alt_key in values:
                credential.extra[f"verify@{label}"] = verifier_for(credential, alt_key)


def _jira_server(inventory: Inventory) -> dict:
    for cfg in inventory.mcp_servers.values():
        env = cfg.get("env") or {}
        if any(str(k).upper().startswith("JIRA_") for k in env):
            return cfg
    return {}


def _resolve_literal(value: str, values: Dict[str, str], inventory: Inventory) -> str:
    """Expand ``${NAME}`` in a non-secret mcp.json value (JIRA_URL) from
    the inventory's effective values, then the environment."""
    from ..mcp import _REFERENCE_RE

    def sub(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        return values.get(name) or os.environ.get(name, "")

    return _REFERENCE_RE.sub(sub, str(value or ""))


def run_checks(inventory: Inventory, names: Optional[List[str]] = None, *,
               timeout: float = DEFAULT_TIMEOUT, workers: int = 8,
               record: bool = True) -> List[Credential]:
    """Verify the selected credentials in parallel (each bounded by
    *timeout*; MCP servers by at least :data:`MCP_TIMEOUT`); fill
    ``status``/``detail``/``elapsed``; remember the outcome in the
    manifest so ``list`` can show "last verified". Returns the checked
    credentials."""
    from ..config import record_key_check

    attach_verifiers(inventory, timeout)
    wanted = set(names or [])
    selected = [
        credential for credential in inventory.credentials
        if not wanted or credential.name in wanted or credential.env_name in wanted
    ]

    def run_one(credential: Credential) -> None:
        started = time.monotonic()
        try:
            if not credential.present and credential.service not in ("mcp-server",):
                credential.status, credential.detail = MISSING, (
                    f"{credential.env_name or credential.name} is not set anywhere conch looks"
                )
            elif credential.verify is None:
                credential.status, credential.detail = SKIPPED, "no verifier"
            else:
                status, detail = credential.verify(timeout)
                credential.status, credential.detail = str(status), str(detail)
            for label in ("env-file", "shell-env"):
                alt = credential.extra.get(f"verify@{label}")
                if callable(alt):
                    try:
                        credential.alt_results[label] = tuple(alt(timeout))  # type: ignore[assignment]
                    except Exception as exc:
                        credential.alt_results[label] = (ERROR, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # a verifier must never take the check down
            credential.status, credential.detail = ERROR, f"{type(exc).__name__}: {exc}"
        credential.elapsed = time.monotonic() - started
        if credential.status in (OK, REJECTED):
            credential.last_verified = time.strftime("%Y-%m-%d %H:%M") + " (now)"

    if selected:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(selected))) as pool:
            list(pool.map(run_one, selected))
    _propagate_server_verdicts(inventory, selected)
    if record:
        for credential in selected:
            value = inventory.values.get(credential.name, "")
            if credential.env_name and value and credential.status in (OK, REJECTED):
                try:
                    record_key_check(credential.env_name, value, credential.status, credential.detail)
                except OSError:
                    pass
    return selected


def _propagate_server_verdicts(inventory: Inventory, checked: List[Credential]) -> None:
    """A ``${NAME}`` referenced only by an MCP server is verified through
    that server's handshake: copy the server row's verdict onto the NAME
    row (so ``rotate`` prompts for a rejected one), naming the server."""
    from ..mcp import secret_like_name

    by_name = {c.name: c for c in inventory.credentials}
    for server_row in checked:
        if server_row.service != "mcp-server" or not server_row.status:
            continue
        server = str(server_row.extra.get("server") or "")
        refs = [n for n in (server_row.extra.get("refs") or []) if n in by_name]
        secret_refs = [n for n in refs if secret_like_name(n)] or refs
        for name in refs:
            row = by_name[name]
            if row.service != "mcp-ref" or row.status not in ("", SKIPPED):
                continue
            if server_row.status == OK:
                row.status, row.detail = OK, f"{name} accepted through mcp/{server} (tools/list answered)"
            elif server_row.status == REJECTED and name in secret_refs:
                row.status, row.detail = REJECTED, server_row.detail
            elif server_row.status in (UNREACHABLE, MISSING, ERROR):
                row.status, row.detail = server_row.status, server_row.detail
            if row.status in (OK, REJECTED):
                row.last_verified = time.strftime("%Y-%m-%d %H:%M") + " (now)"


# ---------------------------------------------------------------------------
# The daemon column
# ---------------------------------------------------------------------------

def daemon_view(config: dict, names: List[str], *, timeout: float = 3.0) -> Dict[str, Any]:
    """What the running edge daemon sees for *names* (fingerprints,
    presence, source), or why it cannot be asked: ``disabled`` (no
    ``edge_daemon=true``), ``not running`` (socket silent), ``protocol``
    (an older daemon without ``keys.status`` — restart it)."""
    if not get_bool_safe(config, "edge_daemon"):
        return {"state": "disabled"}
    try:
        from ..kernel.control import ControlError, request
    except Exception as exc:
        return {"state": "error", "detail": f"{type(exc).__name__}: {exc}"}
    try:
        result = request("keys.status", {"names": list(names)}, timeout=timeout)
    except ControlError as exc:
        text = str(exc)
        if "unknown control op" in text:
            try:
                status = request("status", timeout=timeout)
            except ControlError:
                status = {}
            return {"state": "protocol", "detail": text, "status": status}
        if "socket unavailable" in text or "connection failed" in text:
            return {"state": "not running", "detail": text}
        return {"state": "error", "detail": text}
    if not isinstance(result, dict):
        return {"state": "error", "detail": "malformed keys.status reply"}
    result["state"] = "ok"
    return result


def get_bool_safe(config: dict, key: str) -> bool:
    from ..config import get_bool

    try:
        return get_bool(config or {}, key)
    except Exception:
        return False


def annotate_daemon_column(inventory: Inventory, view: Dict[str, Any]) -> None:
    """Fill ``credential.daemon`` from a :func:`daemon_view` result."""
    state = view.get("state")
    for credential in inventory.credentials:
        if not credential.env_name:
            credential.daemon = "n/a" if state in ("ok", "protocol", "not running") else "—"
            if state == "disabled":
                credential.daemon = "—"
            continue
        if state == "disabled":
            credential.daemon = "—"
            continue
        if state == "not running":
            credential.daemon = "not running"
            continue
        if state == "protocol":
            credential.daemon = "? (old daemon: restart)"
            continue
        if state != "ok":
            credential.daemon = f"? ({view.get('detail', 'error')})"
            continue
        seen = (view.get("keys") or {}).get(credential.env_name) or {}
        fingerprint = str(seen.get("fingerprint") or "")
        credential.daemon_fp = fingerprint
        if not seen.get("present"):
            if credential.present:
                credential.daemon = "missing (daemon-blind)"
            else:
                credential.daemon = "missing"
        elif credential.present and fingerprint == credential.effective_fp:
            credential.daemon = (
                "ok (same key)" if credential.status == OK
                else ("rejected (same key)" if credential.status == REJECTED else "same key")
            )
        elif credential.present:
            credential.daemon = f"stale ({fingerprint}) → conch keys reload"
        else:
            credential.daemon = f"has {fingerprint} (shell has none)"
