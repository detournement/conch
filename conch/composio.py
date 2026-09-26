"""Composio integration -- real OAuth connection flow."""

from __future__ import annotations

import json
import os
import urllib.request
import webbrowser
from typing import Any, Dict, List, Optional, Tuple

_BASE = "https://backend.composio.dev"
_HEADERS_CACHE: Dict[str, str] = {}


def _headers() -> Dict[str, str]:
    if not _HEADERS_CACHE:
        api_key = os.environ.get("COMPOSIO_API_KEY", "").strip()
        _HEADERS_CACHE.update({
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "conch/1.0",
        })
    return dict(_HEADERS_CACHE)


def _api_get(path: str, params: Optional[Dict[str, str]] = None) -> Any:
    url = _BASE + path
    if params:
        qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items() if v)
        url += "?" + qs
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _api_post(path: str, body: dict) -> Any:
    req = urllib.request.Request(
        _BASE + path,
        data=json.dumps(body).encode(),
        headers=_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def is_available() -> bool:
    return bool(os.environ.get("COMPOSIO_API_KEY", "").strip())


def list_apps() -> List[Tuple[str, str]]:
    """List available Composio apps from the API, with fallback."""
    if not is_available():
        return _FALLBACK_APPS

    try:
        # v3 toolkits; v1 /apps was retired (HTTP 410).
        data = _api_get("/api/v3/toolkits", {"limit": "100"})
        items = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            return _FALLBACK_APPS
        apps = []
        for app in items:
            slug = (app.get("slug") or app.get("name") or "").lower()
            meta = app.get("meta") or {}
            desc = (meta.get("description") or app.get("name") or "")[:60]
            # `deprecated` is a metadata object (e.g. {"toolkitId": ...}),
            # not a boolean -- only skip on an explicit deprecation flag.
            dep = app.get("deprecated")
            is_dep = dep is True or (
                isinstance(dep, dict)
                and bool(dep.get("is_deprecated") or dep.get("deprecated"))
            )
            if slug and not is_dep:
                apps.append((slug, desc))
        return sorted(apps) if apps else _FALLBACK_APPS
    except Exception:
        return _FALLBACK_APPS


def check_connection(app_slug: str) -> Tuple[bool, str]:
    """Check if an app is already connected."""
    if not is_available():
        return False, "COMPOSIO_API_KEY not set"
    try:
        # v3 connected_accounts; v1 /connectedAccounts was retired (HTTP 410).
        data = _api_get("/api/v3/connected_accounts", {"limit": "100"})
        items = data.get("items", []) if isinstance(data, dict) else []
        target = (app_slug or "").lower()
        seen = []
        for item in items:
            toolkit = item.get("toolkit") or {}
            slug = (toolkit.get("slug") or "").lower()
            if slug != target:
                continue
            status = (item.get("status") or "").upper()
            seen.append(status)
            if status == "ACTIVE":
                return True, f"{app_slug} is connected (account: {str(item.get('id', 'unknown'))[:14]})"
        if seen:
            # Distinguish "never connected" from "needs re-auth" -- EXPIRED
            # accounts look connected in the dashboard but cannot serve tools.
            return False, f"{app_slug} connection is {seen[0]} -- re-authorize with /connect {app_slug}"
        return False, f"{app_slug} has no active connection"
    except Exception as exc:
        return False, f"Failed to check connection: {exc}"


def connect(app_slug: str) -> Tuple[bool, str]:
    """Initiate an OAuth connection for a Composio app.

    1. Look up the auth config for the app
    2. POST to create a connected account (initiates OAuth)
    3. Open the browser to the redirect URL
    """
    if not is_available():
        return False, "COMPOSIO_API_KEY not set"

    # Check if already connected
    connected, msg = check_connection(app_slug)
    if connected:
        return True, msg + ". Use /reload to refresh tools."

    # Step 1: Find auth config for this app
    auth_config_id = None
    try:
        data = _api_get("/api/v3/auth_configs", {
            "toolkit_slug": app_slug,
            "is_composio_managed": "true",
            "limit": "5",
        })
        items = data.get("items", []) if isinstance(data, dict) else []
        if items:
            auth_config_id = items[0].get("id")
    except Exception:
        pass

    if not auth_config_id:
        # Try v1 integrations as fallback
        try:
            data = _api_get("/api/v1/integrations", {"appName": app_slug})
            items = data.get("items", data) if isinstance(data, dict) else data
            if isinstance(items, list) and items:
                auth_config_id = items[0].get("id")
        except Exception:
            pass

    if not auth_config_id:
        return False, (
            f"No auth config found for '{app_slug}'. "
            "Use /apps to see available services."
        )

    # Step 2: Initiate connection
    try:
        # Try v3 endpoint first
        result = _api_post("/api/v3/connected_accounts", {
            "auth_config": {"id": auth_config_id},
            "connection": {
                "state": {"authScheme": "OAUTH2", "val": {}},
            },
        })
    except Exception:
        try:
            # Fall back to v1 endpoint
            result = _api_post("/api/v1/connectedAccounts", {
                "integrationId": auth_config_id,
            })
        except Exception as exc:
            return False, f"Failed to initiate connection: {exc}"

    redirect_url = (
        result.get("redirectUrl")
        or result.get("redirect_url")
        or result.get("redirectURL")
    )
    if redirect_url:
        try:
            webbrowser.open(redirect_url)
        except Exception:
            return True, (
                f"Open this URL to authenticate {app_slug}:\n"
                f"  {redirect_url}\n"
                "Then /reload to load new tools."
            )
        return True, (
            f"Opening browser for {app_slug} authentication...\n"
            "  Complete the sign-in, then /reload to load new tools."
        )

    status = (
        result.get("connectionStatus")
        or result.get("status")
        or "unknown"
    )
    if status.upper() == "ACTIVE":
        return True, f"{app_slug} connected successfully. /reload to load tools."

    return True, f"Connection initiated for {app_slug} (status: {status}). /reload when ready."


_FALLBACK_APPS: List[Tuple[str, str]] = [
    ("gmail", "Google Mail"),
    ("github", "GitHub"),
    ("slack", "Slack"),
    ("serpapi", "Web search"),
    ("google_calendar", "Google Calendar"),
    ("notion", "Notion"),
    ("linear", "Linear"),
    ("discord", "Discord"),
    ("google_drive", "Google Drive"),
    ("trello", "Trello"),
]
