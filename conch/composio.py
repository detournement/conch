"""Composio integration — connect authenticated services (Gmail, Slack, etc.).

When the user asks for a service that isn't connected yet, Conch can use the
Composio API to initiate an OAuth flow, open the browser, and update the MCP
server so the new tools appear on the next chat session.

Requires COMPOSIO_API_KEY in the environment.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_URL = "https://backend.composio.dev/api/v3"

POPULAR_APPS = [
    ("gmail", "Gmail — read, send, search email"),
    ("slack", "Slack — messages, channels, reactions"),
    ("github", "GitHub — repos, issues, PRs, actions"),
    ("google_calendar", "Google Calendar — events, scheduling"),
    ("google_drive", "Google Drive — files, folders, sharing"),
    ("google_sheets", "Google Sheets — read, write spreadsheets"),
    ("notion", "Notion — pages, databases, search"),
    ("linear", "Linear — issues, projects, teams"),
    ("discord", "Discord — messages, channels, servers"),
    ("spotify", "Spotify — playback, playlists, search"),
    ("twitter", "Twitter/X — tweets, search, timeline"),
    ("trello", "Trello — boards, cards, lists"),
    ("asana", "Asana — tasks, projects, workspaces"),
    ("hubspot", "HubSpot — CRM, contacts, deals"),
    ("salesforce", "Salesforce — CRM, leads, opportunities"),
]


def _api_key() -> str:
    key = os.environ.get("COMPOSIO_API_KEY", "").strip()
    if not key:
        env_path = Path(os.environ.get("CONCH_DIR", os.path.expanduser("~/conch"))) / ".env"
        if env_path.is_file():
            with open(env_path) as f:
                for line in f:
                    m = re.match(r'^export\s+COMPOSIO_API_KEY=["\']?([^"\'\s]+)', line.strip())
                    if m:
                        key = m.group(1)
                        break
    return key


def _request(method: str, path: str, body: dict = None,
             params: dict = None) -> Tuple[dict, int]:
    key = _api_key()
    if not key:
        return {"error": "COMPOSIO_API_KEY not set"}, 0

    url = f"{BASE_URL}{path}"
    if params:
        qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{qs}"

    headers = {
        "x-api-key": key,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()), r.status
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = {"error": str(e)}
        return err_body, e.code
    except Exception as e:
        return {"error": str(e)}, 0


def _get_mcp_server_id() -> Optional[str]:
    """Extract the Composio MCP server ID from the local mcp.json config."""
    mcp_path = Path(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    ) / "conch" / "mcp.json"
    if not mcp_path.is_file():
        return None
    try:
        with open(mcp_path) as f:
            cfg = json.load(f)
        url = cfg.get("mcpServers", {}).get("composio", {}).get("url", "")
        m = re.search(r"/mcp/([a-f0-9-]+)", url)
        return m.group(1) if m else None
    except Exception:
        return None


def is_available() -> bool:
    """Check if Composio API key is configured."""
    return bool(_api_key())


def list_apps() -> List[Tuple[str, str]]:
    return POPULAR_APPS


def get_auth_config(app_slug: str) -> Optional[dict]:
    """Get the Composio-managed auth config for an app."""
    data, status = _request("GET", "/auth_configs", params={
        "toolkit_slug": app_slug,
        "is_composio_managed": "true",
    })
    items = data.get("items", data.get("results", []))
    if isinstance(items, list) and items:
        return items[0]
    return None


def check_connection(app_slug: str) -> Optional[dict]:
    """Check if the user already has an active connection for an app."""
    data, status = _request("GET", "/connected_accounts", params={
        "toolkit_slugs": app_slug,
        "status": "ACTIVE",
        "user_id": "conch",
    })
    items = data.get("items", data.get("results", []))
    if isinstance(items, list) and items:
        return items[0]
    return None


def initiate_connection(app_slug: str) -> Tuple[Optional[str], Optional[str]]:
    """Start an OAuth flow for an app. Returns (redirect_url, error)."""
    auth_cfg = get_auth_config(app_slug)
    if not auth_cfg:
        return None, f"No Composio auth config found for '{app_slug}'"

    auth_config_id = auth_cfg.get("id", "")

    data, status = _request("POST", "/connected_accounts", body={
        "auth_config": {"id": auth_config_id},
        "connection": {"state": {"authScheme": "OAUTH2"}},
        "user_id": "conch",
    })

    redirect_url = data.get("redirectUrl") or data.get("redirect_url")
    if redirect_url:
        return redirect_url, None

    error = data.get("error") or data.get("message") or "Unknown error initiating connection"
    return None, str(error)


def update_mcp_server(app_slug: str) -> Tuple[bool, str]:
    """Add an app's auth config to the MCP server so its tools appear."""
    server_id = _get_mcp_server_id()
    if not server_id:
        return False, "Could not find Composio MCP server ID in mcp.json"

    auth_cfg = get_auth_config(app_slug)
    if not auth_cfg:
        return False, f"No auth config found for '{app_slug}'"

    data, status = _request("GET", f"/mcp/{server_id}")
    current_ids = data.get("auth_config_ids", [])

    auth_id = auth_cfg.get("id", "")
    if auth_id in current_ids:
        return True, "Already configured"

    current_ids.append(auth_id)
    patch_data, patch_status = _request("PATCH", f"/mcp/{server_id}", body={
        "auth_config_ids": current_ids,
    })

    if patch_status in (200, 204):
        return True, "MCP server updated"
    return False, patch_data.get("error", f"HTTP {patch_status}")


def open_browser(url: str):
    """Open a URL in the user's default browser."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def connect(app_slug: str) -> Tuple[bool, str]:
    """Full connection flow: check existing, initiate OAuth, update MCP server.

    Returns (success, message_for_user).
    """
    existing = check_connection(app_slug)
    if existing:
        ok, msg = update_mcp_server(app_slug)
        return True, f"{app_slug} is already connected. {msg if ok else ''} Restart chat to load tools."

    redirect_url, error = initiate_connection(app_slug)
    if error:
        return False, error
    if not redirect_url:
        return False, "No redirect URL returned"

    open_browser(redirect_url)

    return True, (
        f"Opening browser for {app_slug} authentication...\n"
        f"  Complete the sign-in, then restart chat to load the new tools.\n"
        f"  URL: {redirect_url}"
    )
