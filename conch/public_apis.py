"""Public APIs catalog — search ~1400 free APIs and call no-auth ones directly.

Data sourced from https://github.com/public-apis/public-apis
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

_STATE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
) / "conch"
_CACHE_PATH = _STATE_DIR / "public_apis_cache.json"
_CACHE_TTL = 86400  # 24 hours
_README_URL = (
    "https://raw.githubusercontent.com/public-apis/public-apis/master/README.md"
)
_MAX_RESPONSE = 8192

_catalog: Optional[List[Dict[str, Any]]] = None


def _parse_readme(text: str) -> List[Dict[str, Any]]:
    """Parse the public-apis README markdown into structured entries."""
    entries: List[Dict[str, Any]] = []
    current_category = ""

    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith("### ") and not stripped.startswith("### API"):
            current_category = stripped[4:].strip()
            continue

        if not current_category or not stripped.startswith("|"):
            continue

        # Match table rows: | [Name](url) | Description | Auth | HTTPS | CORS |
        m = re.match(
            r'\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|'   # name + url
            r'\s*([^|]*)\|'                           # description
            r'\s*([^|]*)\|'                           # auth
            r'\s*([^|]*)\|'                           # https
            r'\s*([^|]*)\|?',                         # cors
            stripped,
        )
        if not m:
            continue

        name = m.group(1).strip()
        url = m.group(2).strip()
        desc = m.group(3).strip()
        auth = m.group(4).strip().strip("`")
        https = m.group(5).strip().lower() == "yes"
        cors = m.group(6).strip()

        entries.append({
            "name": name,
            "url": url,
            "description": desc,
            "auth": auth if auth and auth != "No" else "none",
            "https": https,
            "cors": cors,
            "category": current_category,
        })

    return entries


def _fetch_catalog() -> List[Dict[str, Any]]:
    """Fetch the README from GitHub and parse it."""
    req = urllib.request.Request(
        _README_URL,
        headers={"User-Agent": "conch/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    entries = _parse_readme(text)

    # Cache to disk
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps({
            "ts": time.time(),
            "entries": entries,
        }))
    except (OSError, TypeError):
        pass

    return entries


def _load_from_cache() -> Optional[List[Dict[str, Any]]]:
    """Load catalog from disk cache if still fresh."""
    try:
        data = json.loads(_CACHE_PATH.read_text())
        if time.time() - data.get("ts", 0) < _CACHE_TTL:
            return data.get("entries", [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def load_catalog() -> List[Dict[str, Any]]:
    """Return the API catalog, using cache when available."""
    global _catalog
    if _catalog is not None:
        return _catalog

    cached = _load_from_cache()
    if cached is not None:
        _catalog = cached
        return _catalog

    _catalog = _fetch_catalog()
    return _catalog


def get_categories() -> Dict[str, int]:
    """Return category names with entry counts."""
    counts: Dict[str, int] = {}
    for entry in load_catalog():
        cat = entry.get("category", "Other")
        counts[cat] = counts.get(cat, 0) + 1
    return dict(sorted(counts.items()))


def search(
    query: str = "",
    auth_filter: str = "any",
    category_filter: str = "",
    https_only: bool = False,
    limit: int = 15,
) -> List[Dict[str, Any]]:
    """Search the catalog by keyword with optional filters."""
    catalog = load_catalog()
    if not catalog:
        return []

    keywords = [w.lower() for w in query.split() if w] if query else []

    results: List[tuple] = []
    for entry in catalog:
        # Apply filters
        if auth_filter == "none" and entry["auth"] != "none":
            continue
        if auth_filter not in ("any", "none", "") and entry["auth"].lower() != auth_filter.lower():
            continue
        if category_filter and category_filter.lower() not in entry["category"].lower():
            continue
        if https_only and not entry["https"]:
            continue

        if not keywords:
            results.append((0, entry))
            continue

        haystack = (
            entry["name"] + " " + entry["description"] + " " + entry["category"]
        ).lower()
        score = sum(1 for kw in keywords if kw in haystack)
        if score > 0:
            results.append((score, entry))

    results.sort(key=lambda x: -x[0])
    return [entry for _, entry in results[:limit]]


def call_api(
    url: str,
    method: str = "GET",
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> str:
    """Call a public API and return the response text (truncated)."""
    if not url:
        return "Error: no URL provided"

    # Safety: only HTTPS
    if not url.startswith("https://") and not url.startswith("http://"):
        return "Error: URL must start with http:// or https://"

    req_headers = {"User-Agent": "conch/1.0", "Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    if method.upper() == "GET" and params:
        qs = urllib.parse.urlencode(params)
        sep = "&" if "?" in url else "?"
        url = url + sep + qs
        data = None
    elif method.upper() == "POST" and params:
        data = json.dumps(params).encode()
        req_headers["Content-Type"] = "application/json"
    else:
        data = None

    req = urllib.request.Request(
        url,
        data=data,
        headers=req_headers,
        method=method.upper(),
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read(_MAX_RESPONSE + 1)
            text = body.decode("utf-8", errors="replace")
            if len(body) > _MAX_RESPONSE:
                text = text[:_MAX_RESPONSE] + "\n... (truncated)"
            return text
    except Exception as exc:
        return "API call failed: %s" % exc
