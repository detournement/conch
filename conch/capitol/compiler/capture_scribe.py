"""Scribe importer (capture plan, feature 3): Scribe's MCP server as a
capture source.

Scribe (scribe.com) captures office workflows as step-by-step guides and
exposes them — documents, Optimize workflows, insights — through a
hosted MCP server (``https://mcp.scribe.com/mcp``). This importer
consumes that surface as *input* to compilation: a guide or workflow
search becomes a capture-context block for the same Capture→Card
synthesis every other source feeds. Complement, not competition — their
capture becomes our compiler's raw material.

Contract reality (verified against Scribe's public docs 2026-09):

- The hosted server authenticates with **OAuth 2.0** via a browser
  flow; conch does not implement that dance. Supply a pre-obtained
  access token by env reference (``scribe_token_env``, default
  ``SCRIBE_MCP_TOKEN``) — it rides as a bearer header, never logged,
  never stored in config. A 401 parks with a clear message.
- Scribe does not publish its MCP tool names, so nothing is hardcoded:
  tools are **discovered** per session (``tools/list``) and selected by
  capability — a search-shaped tool, preferring exact well-known names,
  else the first tool whose name contains "search". No match fails
  closed naming the tools that were actually offered.
- Result shapes are validated fail-closed: a tool result without the
  MCP ``content`` list of text blocks is an error, never guessed at.
- ``scribe_mcp_url`` unset = the source is absent: zero traffic, the
  llamaidx gating pattern.

Live verification requires a Scribe workspace and token; none is
configured in this environment, so the contract is encoded in the fake
server harness (tests) and marked for a live pass when an account
exists.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from ..errors import CapitolAuthError, CapitolError
from .capture import CAPTURE_BLOCK_CAP, guard_capture_text

DEFAULT_TOKEN_ENV = "SCRIBE_MCP_TOKEN"
_RESULT_TEXT_CAP = CAPTURE_BLOCK_CAP - 600

#: Search-tool preference: exact names first, substring fallback.
_SEARCH_PREFERRED = ("search", "search_documents", "cross_source_search",
                     "search_workflows")


def scribe_unconfigured_reason(config: dict) -> str:
    if not str(config.get("scribe_mcp_url") or "").strip():
        return ("scribe_mcp_url is not set — the Scribe source is "
                "absent until you point it at an MCP endpoint "
                "(hosted: https://mcp.scribe.com/mcp)")
    return ""


def _bearer(config: dict) -> str:
    env = str(config.get("scribe_token_env")
              or DEFAULT_TOKEN_ENV).strip()
    return os.environ.get(env, "").strip()


def _client(config: dict):
    from ...mcp import HttpMcpClient

    headers = {}
    token = _bearer(config)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return HttpMcpClient(
        "scribe", str(config["scribe_mcp_url"]).strip(),
        headers=headers,
    )


def _pick_search_tool(tools: List[dict]) -> Tuple[str, str]:
    """The search tool and its query argument name, discovered from the
    live tool list — fail closed naming what was offered.

    ``HttpMcpClient.list_tools`` returns OpenAI-wrapped defs
    (``{"type": "function", "function": {name, description,
    parameters}}``); unwrap before selecting.
    """
    by_name: Dict[str, dict] = {}
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) \
            else None
        if isinstance(function, dict) and function.get("name"):
            by_name[str(function["name"])] = function
    chosen: Optional[dict] = None
    for name in _SEARCH_PREFERRED:
        if name in by_name:
            chosen = by_name[name]
            break
    if chosen is None:
        for name, tool in sorted(by_name.items()):
            if "search" in name.lower():
                chosen = tool
                break
    if chosen is None:
        raise CapitolError(
            "scribe capture: no search-shaped tool on the MCP server "
            "(offered: " + ", ".join(sorted(by_name)) + ")"
        )
    schema = chosen.get("parameters") or {}
    properties = schema.get("properties") or {}
    for key in ("query", "q", "search", "text"):
        if key in properties:
            return str(chosen["name"]), key
    string_keys = [
        key for key, spec in sorted(properties.items())
        if isinstance(spec, dict) and spec.get("type") == "string"
    ]
    if string_keys:
        return str(chosen["name"]), string_keys[0]
    raise CapitolError(
        f"scribe capture: tool {chosen.get('name')!r} has no "
        "string query parameter (failing closed)"
    )


def _result_text(result: dict) -> str:
    """MCP tool-result content → text, fail-closed on unknown shapes."""
    if not isinstance(result, dict):
        raise CapitolError(
            "scribe capture: tool result is not an object "
            "(failing closed)"
        )
    if result.get("isError"):
        blocks = result.get("content") or []
        detail = " ".join(
            str(block.get("text") or "") for block in blocks
            if isinstance(block, dict)
        )[:300]
        raise CapitolError(f"scribe capture: tool error: {detail}")
    blocks = result.get("content")
    if not isinstance(blocks, list) or not blocks:
        raise CapitolError(
            "scribe capture: tool result carries no content list "
            "(unknown shape — failing closed)"
        )
    parts: List[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            raise CapitolError(
                "scribe capture: non-object content block "
                "(unknown shape — failing closed)"
            )
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    text = "\n".join(part for part in parts if part.strip()).strip()
    if not text:
        raise CapitolError(
            "scribe capture: the search returned no text content"
        )
    return text


def capture_from_scribe(config: dict, query: str, *,
                        client_factory=None) -> Dict[str, Any]:
    """One Scribe MCP search → a capture context for card synthesis."""
    reason = scribe_unconfigured_reason(config)
    if reason:
        raise CapitolError(f"scribe capture unavailable: {reason}")
    query = str(query or "").strip()
    if not query:
        raise CapitolError(
            "scribe capture needs a guide name or search query"
        )
    factory = client_factory or _client
    client = factory(config)
    try:
        tools = client.list_tools()
    except Exception as exc:
        raise CapitolError(f"scribe capture: MCP unreachable: {exc}")
    if not tools:
        raise CapitolError(
            "scribe capture: the MCP server offered no tools — check "
            "the token in the configured env var (Scribe's hosted "
            "server needs an OAuth access token) and that your "
            "workspace enables the MCP server"
        )
    tool_name, query_key = _pick_search_tool(tools)
    result = client.call_tool(tool_name, {query_key: query})
    text = _result_text(result)
    # HttpMcpClient folds transport/JSON-RPC failures into a content
    # block rather than raising — surface them as errors, not capture.
    if text.startswith("MCP HTTP error:"):
        if "401" in text or "unauthorized" in text.lower():
            raise CapitolAuthError(
                "scribe capture: unauthorized — obtain a Scribe OAuth "
                "access token and export it in the env var named by "
                "scribe_token_env"
            )
        raise CapitolError(f"scribe capture: {text[:300]}")
    if len(text) > _RESULT_TEXT_CAP:
        text = text[:_RESULT_TEXT_CAP - 15] + "\n... [clipped]"
    host = str(config["scribe_mcp_url"]).split("//")[-1].split("/")[0]
    block = guard_capture_text(
        f"Scribe workflow context — query {query!r} via {host} "
        f"(tool {tool_name}):\n{text}"
    )
    return {
        "kind": "scribe",
        "source_id": query,
        "label": f"Scribe guide/search {query!r}",
        "default_goal": (
            f"Automate the process documented in Scribe: \"{query}\""
        ),
        "block": block,
        "provenance": {
            "kind": "scribe",
            "source": query,
            "server": host,
            "tool": tool_name,
        },
    }
