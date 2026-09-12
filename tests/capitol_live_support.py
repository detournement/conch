"""Shared gating and fixtures for the live Capitol tests (Swarm Phase 3).

These exercise the real *local dev* Capitol stack the roadmap targets —
workflow-api on :8300, platform-api on :8811, the dev org — rather than a
fake. They are opt-in and fail-closed: every case skips unless

- ``CONCH_CAPITOL_LIVE`` is set in the environment (so the routine
  ``unittest discover`` never creates real assets or waits on live runs),
- both APIs answer a health probe, and
- an admin token (platform user JWT) resolves from the environment or the
  A2Actrl registry for the dev org.

Nothing here prints, logs, or returns a token or bearer — credentials are
referenced by name only. Every asset these tests create carries the
``conch-phase3-`` prefix and is torn down in the same test, including any
temporary A2Actrl registry entry added so the ``a2actrl`` CLI can talk to
a disposable agent.

The knobs:

- ``CONCH_CAPITOL_WORKFLOW_URL`` (default ``http://localhost:8300``)
- ``CONCH_CAPITOL_PLATFORM_URL`` (default ``http://localhost:8811``)
- ``CONCH_CAPITOL_ORG``          (required: the target org UUID — there is
  no default, so the live suite additionally skips until it is set)
"""

from __future__ import annotations

import json
import os
import time
import unittest
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

from conch.capitol.credentials import resolve_admin_token

LIVE_ENV = "CONCH_CAPITOL_LIVE"
ORG_ENV = "CONCH_CAPITOL_ORG"
ASSET_PREFIX = "conch-phase3-"

WORKFLOW_URL = os.environ.get(
    "CONCH_CAPITOL_WORKFLOW_URL", "http://localhost:8300"
).rstrip("/")
PLATFORM_URL = os.environ.get(
    "CONCH_CAPITOL_PLATFORM_URL", "http://localhost:8811"
).rstrip("/")
#: The org every live asset is created in. Deliberately has no default:
#: the UUID identifies a private deployment, so it must come from the
#: operator's environment.
ORG = os.environ.get(ORG_ENV, "")

#: A tiny, deterministic, LLM-free workflow that emits a passing eval
#: suite — used to drive live invoke/supervise/eval reads. Discovered by
#: name so a changed id does not break the suite; disposable copies of it
#: get the conch-phase3- prefix.
EVAL_WORKFLOW_NAME = "Legacy Eval Compatibility Proof"


def _probe(url: str, timeout: float = 2.0) -> bool:
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500          # 401/404 still means "answering"
    except OSError:
        return False


def live_config() -> Dict[str, str]:
    """Return the live config dict, or raise SkipTest with the reason.

    The config is the same shape ``CapitolAdmin.from_config`` /
    ``CapitolRuntime.from_config`` consume, with the builder profile
    enabled. Never contains a token.
    """
    if not os.environ.get(LIVE_ENV):
        raise unittest.SkipTest(
            f"set {LIVE_ENV}=1 to run the live Capitol stack tests"
        )
    if not ORG:
        raise unittest.SkipTest(
            f"set {ORG_ENV} to the target org UUID for the live tests"
        )
    if not _probe(f"{PLATFORM_URL}/health"):
        raise unittest.SkipTest(f"platform-api unreachable at {PLATFORM_URL}")
    if not (_probe(f"{WORKFLOW_URL}/version")
            or _probe(f"{WORKFLOW_URL}/api/v1")):
        raise unittest.SkipTest(f"workflow-api unreachable at {WORKFLOW_URL}")
    try:
        resolve_admin_token({}, ORG, PLATFORM_URL)
    except Exception as exc:  # CapitolAuthError and anything upstream
        raise unittest.SkipTest(
            f"no Capitol admin token for org {ORG}: {type(exc).__name__}"
        )
    return {
        "capitol_admin": "true",
        "capitol_base_url": WORKFLOW_URL,
        "capitol_platform_url": PLATFORM_URL,
        "capitol_org": ORG,
    }


class LiveHTTP:
    """Minimal authenticated HTTP helper for test setup/teardown only.

    Used to mint the disposable *workflows* the admin surface then
    operates on (CapitolAdmin intentionally has no from-scratch workflow
    authoring — that is deferred to the unified management API), and to
    read back raw state for assertions. The admin/runtime *behaviour*
    under test always goes through CapitolAdmin / CapitolRuntime, never
    this helper.
    """

    def __init__(self) -> None:
        self.workflow_token, _ = resolve_admin_token({}, ORG, WORKFLOW_URL)
        self.platform_token, _ = resolve_admin_token({}, ORG, PLATFORM_URL)

    def _do(self, token: str, base: str, method: str, path: str,
            body: Optional[dict] = None, timeout: float = 30.0) -> Tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{base}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
            try:
                return exc.code, (json.loads(raw) if raw else {})
            except ValueError:
                return exc.code, {"detail": raw.decode("utf-8", "replace")}

    def wf(self, method: str, path: str, body=None, timeout=30.0):
        return self._do(self.workflow_token, WORKFLOW_URL, method, path,
                        body, timeout)

    def pl(self, method: str, path: str, body=None, timeout=30.0):
        return self._do(self.platform_token, PLATFORM_URL, method, path,
                        body, timeout)

    # -- workflow fixtures ---------------------------------------------------

    def list_workflows(self) -> List[Dict[str, Any]]:
        _status, payload = self.wf(
            "GET", f"/api/v1/orgs/{ORG}/workflows"
        )
        return (payload or {}).get("workflows") or []

    def find_workflow_by_name(self, name: str) -> Optional[str]:
        for row in self.list_workflows():
            if str(row.get("name") or "") == name:
                return str(row.get("id") or "")
        return None

    def duplicate_workflow(self, base_id: str, name: str) -> str:
        status, payload = self.wf(
            "POST", f"/api/v1/orgs/{ORG}/workflows/duplicate",
            {"workflow_id": base_id, "name": name},
        )
        if status >= 400:
            raise AssertionError(f"duplicate {base_id} failed: {payload}")
        return str((payload.get("workflow") or {}).get("id") or "")

    def get_workflow_payload(self, workflow_id: str) -> Dict[str, Any]:
        _status, payload = self.wf(
            "GET", f"/api/v1/orgs/{ORG}/workflows/{workflow_id}"
        )
        return (payload.get("workflow") or {}).get("payload") or {}

    def persist_workflow(self, payload: Dict[str, Any]) -> Tuple[int, Any]:
        return self.wf("POST", f"/api/v1/orgs/{ORG}/workflows", payload)

    def delete_workflow(self, workflow_id: str) -> int:
        status, _ = self.wf(
            "DELETE", f"/api/v1/orgs/{ORG}/workflows/{workflow_id}"
        )
        return status

    def delete_agent(self, agent_id: str) -> int:
        status, _ = self.pl("DELETE", f"/agents/{ORG}/{agent_id}")
        return status


def unique_suffix() -> str:
    return uuid.uuid4().hex[:8]


def poll_until_terminal(runtime, run_id: str, *, timeout: float = 90.0,
                        interval: float = 2.0) -> str:
    """Poll ``get_workflow_status`` until a terminal state or timeout."""
    deadline = time.time() + timeout
    last = "unknown"
    terminal = {"success", "failed", "stopped", "cancelled"}
    while time.time() < deadline:
        last = str(runtime.run_status(run_id).get("status") or "").lower()
        if last in terminal:
            return last
        time.sleep(interval)
    return last
