"""CapitolAdmin — the bounded builder/provisioning profile (Phase 3).

A separately authorized surface over Capitol's platform and workflow
management APIs, for the pre-authorized production creation the roadmap
allows: orchestrator agents, deployment bearers, workflow publish/
version-pin/rollback, agent allowlists, schedules, and collection
bindings. It is **off by default** and refuses to construct unless the
``capitol_admin`` config flag is set; every mutation additionally passes
the required-policy registry (event ``capitol.admin.{op}``), which fails
closed on any registered deny.

Discipline every mutation shares (:meth:`CapitolAdmin._mutation`):

- a caller-supplied idempotency key, ledgered as a kernel external
  action **before** the wire call — a committed duplicate returns the
  recorded outcome without touching Capitol again;
- a version pin and a rollback reference captured in the ledger detail
  (prior workflow version, prior allowlist, created asset id — whatever
  undoes the mutation);
- transport-uncertain outcomes resolve ``unknown`` and reconcile by
  query on the next attempt (create ops adopt an existing same-name
  asset instead of duplicating it) — never a blind retry.

Secrets: minted/rotated bearer values stream straight into the A2Actrl
registry (``~/.capitol-a2a/agents.yaml``, 0600) via
:func:`conch.capitol.credentials.sink_bearer_to_registry`; callers, the
ledger, and logs only ever see a fingerprint. The admin token (platform
user JWT) resolves per call from the environment or the registry and is
scrubbed from every error.

Wire surfaces (local stack, verified against the served OpenAPI):

- platform-api: ``POST/GET/PATCH/DELETE /agents/{org}[/{agent}]``,
  ``POST .../rotate-a2a-token``, ``POST/GET/DELETE .../bearers``,
  ``POST/GET/DELETE /collections/{org}[/{id}]``
- workflow-api: ``GET/POST/DELETE /api/v1/orgs/{org}/workflows[/{wf}]``,
  ``.../workflows/{wf}/schedules[/{id}]``, ``.../workflows/{wf}/versions``

Publish and its undo both go through the workflow-api ``POST /workflows``
persist (toggling ``publish_to_api``), so they share one version lineage;
the platform-api ``/agentic-workflows/.../rollback`` endpoint reads a
separate, unpopulated version store for workflow-api-published workflows
(DIVERGENCE, documented in :mod:`conch.capitol.crossclient`).

Deferred (documented in PLAN.md as Capitol-side epics): hosted-app and
published-data provisioning wait for the idempotent unified management
API; today's app surfaces are read-only here.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

import urllib.error
import urllib.request

from ..config import get_bool
from ..policy import evaluate_required_policy
from ..swarm.protocol import ActionClass
from .credentials import (
    ensure_endpoint_allowed,
    redact_text,
    resolve_admin_token,
    sink_bearer_to_registry,
)
from .errors import CapitolAuthError, CapitolError, CapitolProtocolError

DEFAULT_TIMEOUT = 60.0

#: Ledger action classes per admin operation kind.
_ACTION_CLASSES = {
    "create_agent": ActionClass.PROVISION,
    "delete_agent": ActionClass.DELETE,
    "rotate_bearer": ActionClass.ACCOUNT_CHANGE,
    "mint_deployment_bearer": ActionClass.ACCOUNT_CHANGE,
    "revoke_bearer": ActionClass.ACCOUNT_CHANGE,
    "set_workflow_allowlist": ActionClass.ACCOUNT_CHANGE,
    "bind_agent_collections": ActionClass.ACCOUNT_CHANGE,
    "persist_workflow": ActionClass.PROVISION,
    "delete_workflow": ActionClass.DELETE,
    "publish_workflow": ActionClass.PUBLISH,
    "rollback_workflow": ActionClass.PROVISION,
    "create_schedule": ActionClass.PROVISION,
    "update_schedule": ActionClass.PROVISION,
    "delete_schedule": ActionClass.DELETE,
    "create_collection": ActionClass.PROVISION,
    "delete_collection": ActionClass.DELETE,
}

#: Response keys that may carry secret bytes; stripped before anything is
#: returned, ledgered, or logged.
_SECRET_KEYS = ("a2a_bearer_token", "bearer_token", "token", "jwt")


def _scrub(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: _scrub(value)
            for key, value in payload.items()
            if key not in _SECRET_KEYS
        }
    if isinstance(payload, list):
        return [_scrub(item) for item in payload]
    return payload


class CapitolAdmin:
    """Gated provisioning client bound to one org and one kernel mission.

    ``store``/``mission_id`` anchor the external-action ledger — every
    mutation is recorded under that mission. Reads never touch the
    ledger.
    """

    def __init__(
        self,
        *,
        platform_url: str,
        workflow_url: str,
        org_id: str,
        token: str,
        store=None,
        mission_id: str = "",
        config: Optional[dict] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.platform_url = str(platform_url or "").rstrip("/")
        self.workflow_url = str(workflow_url or "").rstrip("/")
        self.org_id = str(org_id or "")
        self._token = str(token or "")
        self.store = store
        self.mission_id = str(mission_id or "")
        self.config = config or {}
        self.timeout = float(timeout)
        if not (self.platform_url and self.workflow_url and self.org_id):
            raise CapitolError(
                "CapitolAdmin needs platform_url, workflow_url, and org_id"
            )
        if not self._token:
            raise CapitolAuthError("CapitolAdmin needs a non-empty token")

    def __repr__(self) -> str:  # never the token
        return (
            f"CapitolAdmin(platform_url={self.platform_url!r}, "
            f"org_id={self.org_id!r})"
        )

    @classmethod
    def from_config(cls, config: dict, *, store=None,
                    mission_id: str = "", **kwargs) -> "CapitolAdmin":
        """Construct the builder profile from config — fail closed unless
        ``capitol_admin`` is explicitly enabled."""
        config = config or {}
        if not get_bool(config, "capitol_admin", False):
            raise CapitolError(
                "the Capitol builder profile is disabled: set "
                "capitol_admin=true (and capitol_platform_url) to enable "
                "bounded provisioning"
            )
        workflow_url = str(config.get("capitol_base_url") or "").strip()
        platform_url = str(config.get("capitol_platform_url") or "").strip()
        org_id = str(config.get("capitol_org") or "").strip()
        if not (workflow_url and platform_url and org_id):
            raise CapitolError(
                "CapitolAdmin is not configured: set capitol_base_url, "
                "capitol_platform_url, and capitol_org"
            )
        ensure_endpoint_allowed(config, workflow_url)
        ensure_endpoint_allowed(config, platform_url)
        token, _source = resolve_admin_token(config, org_id, platform_url)
        return cls(
            platform_url=platform_url, workflow_url=workflow_url,
            org_id=org_id, token=token, store=store,
            mission_id=mission_id, config=config, **kwargs,
        )

    # -- transport -----------------------------------------------------------

    def _redact(self, text: str) -> str:
        return redact_text(str(text), (self._token,))

    def _request(self, base: str, path: str, *,
                 method: str = "GET",
                 body: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{base}{path}"
        data = (
            json.dumps(body).encode("utf-8") if body is not None else None
        )
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = b""
            try:
                detail = exc.read()
            except Exception:
                pass
            finally:
                try:
                    exc.close()
                except Exception:
                    pass
            text = detail.decode("utf-8", "replace")[:1000]
            if exc.code in (401, 403):
                raise CapitolAuthError(
                    self._redact(
                        f"Capitol admin auth failed (HTTP {exc.code}) at "
                        f"{method} {url}: {text}"
                    ),
                    http_status=exc.code,
                ) from None
            raise CapitolError(
                self._redact(
                    f"Capitol admin HTTP {exc.code} at {method} {url}: "
                    f"{text}"
                ),
                http_status=exc.code,
                retryable=exc.code in (429, 502, 503, 504),
            ) from None
        except OSError as exc:
            raise CapitolError(
                self._redact(
                    f"Capitol admin endpoint unreachable at {url}: {exc}"
                ),
                retryable=True, category="transport",
            ) from None
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CapitolProtocolError(
                self._redact(
                    f"Capitol admin returned a non-JSON body at {url}: "
                    f"{exc}"
                )
            ) from None

    # -- the mutation discipline ------------------------------------------------

    def _mutation(self, op: str, idempotency_key: str,
                  summary: Dict[str, Any],
                  fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        """Policy gate → ledger → effect → resolve, in that order."""
        if not str(idempotency_key or "").strip():
            raise CapitolError(
                f"capitol admin {op} requires an idempotency_key"
            )
        if self.store is None or not self.mission_id:
            raise CapitolError(
                "capitol admin mutations need a kernel store and "
                "mission_id (the external-action ledger is mandatory)"
            )
        decision = evaluate_required_policy(f"capitol.admin.{op}", dict(
            summary, org_id=self.org_id, idempotency_key=idempotency_key,
        ))
        if not decision.allowed:
            raise CapitolError(
                f"capitol admin {op} denied by required policy: "
                f"{decision.reason}"
            )
        ledger_key = f"capitol-admin:{op}:{idempotency_key}"
        action = self.store.record_action(
            self.mission_id, _ACTION_CLASSES[op], ledger_key,
            detail=dict(summary, op=op),
        )
        if action.get("duplicate") and action.get("status") == "committed":
            recorded = self.store.get_action(action["action_id"]) or {}
            try:
                detail = json.loads(recorded.get("detail") or "{}")
            except ValueError:
                detail = {}
            result = detail.get("result") or {}
            result["replayed"] = True
            return result
        try:
            result = _scrub(fn())
        except CapitolError as exc:
            from ..kernel.model import KernelError

            status = (
                "unknown" if exc.retryable is True or (
                    exc.category == "transport"
                ) else "failed"
            )
            try:
                self.store.resolve_action(
                    action["action_id"], status,
                    {"error": self._redact(str(exc))[:500], "op": op},
                )
            except KernelError:
                pass
            raise
        from ..kernel.model import KernelError

        try:
            self.store.resolve_action(
                action["action_id"], "committed",
                dict(summary, op=op, result=result),
            )
        except KernelError:
            pass  # an earlier attempt resolved it (unknown → re-run)
        return result

    # -- agents -------------------------------------------------------------------

    def list_agents(self) -> List[Dict[str, Any]]:
        payload = self._request(self.platform_url, f"/agents/{self.org_id}")
        agents = (
            payload.get("agents") if isinstance(payload, dict) else payload
        )
        return _scrub(agents) if isinstance(agents, list) else []

    def get_agent(self, agent_id: str) -> Dict[str, Any]:
        payload = self._request(
            self.platform_url, f"/agents/{self.org_id}/{agent_id}"
        )
        agent = (
            payload.get("agent") if isinstance(payload, dict)
            and "agent" in payload else payload
        )
        return _scrub(agent) if isinstance(agent, dict) else {}

    def _find_agent_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        for agent in self.list_agents():
            if str(agent.get("name") or "") == name:
                return agent
        return None

    def create_orchestrator_agent(
        self,
        name: str,
        workflow_ids: List[str],
        *,
        idempotency_key: str,
        description: str = "",
        system_prompt: str = "",
        model_provider: str = "anthropic",
        model_name: str = "claude-opus-4-7",
        registry_alias: str = "",
    ) -> Dict[str, Any]:
        """Create an A2A orchestrator bound to *workflow_ids*.

        The once-only response bearer goes straight into the A2Actrl
        registry; the returned dict carries only its fingerprint.
        Reconciliation: an unknown earlier outcome adopts an existing
        agent with the same name instead of creating a duplicate.
        """
        name = str(name).strip()

        def effect() -> Dict[str, Any]:
            existing = self._find_agent_by_name(name)
            if existing is not None:
                return {
                    "agent_id": str(existing.get("id") or ""),
                    "name": name,
                    "adopted_existing": True,
                    "rollback_ref": {"kind": "delete_agent",
                                     "agent_id": str(existing.get("id"))},
                }
            body = {
                "name": name,
                "description": description
                or f"Conch-provisioned orchestrator ({name})",
                "model_provider": model_provider,
                "model_name": model_name,
                "system_prompt": system_prompt or (
                    "You are an A2A orchestrator agent. Interpret the "
                    "caller's intent and invoke the workflow skills on "
                    "your agent card. Never invoke workflows outside "
                    "your allowlist."
                ),
                "max_tokens": 32000,
                "temperature": 0.3,
                "top_p": 1.0,
                "thinking_enabled": False,
                "enable_workflow_runtime": True,
                "enable_workflow_authoring": False,
                "enable_a2a_outbound": True,
                "workflow_allowlist": [str(w) for w in workflow_ids],
                "workflow_allowlist_labels": [],
                "mcp_tool_allowlist": [],
                "data_collection_allowlist": [],
                "guardrail_allowlist": [],
                "peer_agent_allowlist": [],
                "custom_skills": [],
                "skill_overrides": {},
                "exposed_via_a2a": True,
            }
            payload = self._request(
                self.platform_url, f"/agents/{self.org_id}",
                method="POST", body=body,
            )
            agent = payload.get("agent") or {}
            agent_id = str(agent.get("id") or "")
            bearer = str(payload.get("a2a_bearer_token") or "")
            fingerprint = ""
            if bearer:
                fingerprint = sink_bearer_to_registry(
                    registry_alias or f"conch-{name[:40]}",
                    org_id=self.org_id, agent_id=agent_id,
                    base_url=self.workflow_url, bearer=bearer,
                    description=f"minted by conch CapitolAdmin for {name}",
                )
            return {
                "agent_id": agent_id,
                "name": str(agent.get("name") or name),
                "bearer_fingerprint": fingerprint,
                "endpoint": str(payload.get("a2a_endpoint_url") or ""),
                "card_url": str(payload.get("a2a_agent_card_url") or ""),
                "workflow_allowlist": [str(w) for w in workflow_ids],
                "rollback_ref": {"kind": "delete_agent",
                                 "agent_id": agent_id},
            }
        return self._mutation(
            "create_agent", idempotency_key,
            {"name": name, "workflow_ids": list(workflow_ids)},
            effect,
        )

    def delete_agent(self, agent_id: str, *,
                     idempotency_key: str) -> Dict[str, Any]:
        def effect() -> Dict[str, Any]:
            self._request(
                self.platform_url, f"/agents/{self.org_id}/{agent_id}",
                method="DELETE",
            )
            return {"agent_id": agent_id, "deleted": True}
        return self._mutation(
            "delete_agent", idempotency_key, {"agent_id": agent_id},
            effect,
        )

    def rotate_bearer(self, agent_id: str, *,
                      idempotency_key: str,
                      registry_alias: str = "") -> Dict[str, Any]:
        """Rotate the agent's primary A2A bearer; the new value lands in
        the registry, the old one stops working platform-side."""
        def effect() -> Dict[str, Any]:
            payload = self._request(
                self.platform_url,
                f"/agents/{self.org_id}/{agent_id}/rotate-a2a-token",
                method="POST",
            )
            bearer = str(
                payload.get("a2a_bearer_token")
                or payload.get("bearer_token") or ""
            )
            if not bearer:
                raise CapitolProtocolError(
                    "rotate-a2a-token returned no bearer value"
                )
            fingerprint = sink_bearer_to_registry(
                registry_alias or f"conch-rotated-{agent_id[:8]}",
                org_id=self.org_id, agent_id=agent_id,
                base_url=self.workflow_url, bearer=bearer,
            )
            return {"agent_id": agent_id,
                    "bearer_fingerprint": fingerprint,
                    "rollback_ref": {"kind": "rotate_again",
                                     "agent_id": agent_id}}
        return self._mutation(
            "rotate_bearer", idempotency_key, {"agent_id": agent_id},
            effect,
        )

    def mint_deployment_bearer(
        self, agent_id: str, label: str, *, idempotency_key: str,
    ) -> Dict[str, Any]:
        """Mint a named additional bearer (deployment credential)."""
        def effect() -> Dict[str, Any]:
            payload = self._request(
                self.platform_url,
                f"/agents/{self.org_id}/{agent_id}/bearers",
                method="POST", body={"label": str(label)},
            )
            bearer_row = payload.get("bearer") or {}
            bearer = str(payload.get("a2a_bearer_token") or "")
            if not bearer:
                raise CapitolProtocolError(
                    "bearer mint returned no bearer value"
                )
            fingerprint = sink_bearer_to_registry(
                str(label), org_id=self.org_id, agent_id=agent_id,
                base_url=self.workflow_url, bearer=bearer,
                description=f"deployment bearer {label}",
            )
            bearer_id = str(bearer_row.get("id") or "")
            return {"agent_id": agent_id, "label": str(label),
                    "bearer_id": bearer_id,
                    "bearer_fingerprint": fingerprint,
                    "rollback_ref": {"kind": "revoke_bearer",
                                     "agent_id": agent_id,
                                     "bearer_id": bearer_id}}
        return self._mutation(
            "mint_deployment_bearer", idempotency_key,
            {"agent_id": agent_id, "label": str(label)}, effect,
        )

    def revoke_bearer(self, agent_id: str, bearer_id: str, *,
                      idempotency_key: str) -> Dict[str, Any]:
        def effect() -> Dict[str, Any]:
            self._request(
                self.platform_url,
                f"/agents/{self.org_id}/{agent_id}/bearers/{bearer_id}",
                method="DELETE",
            )
            return {"agent_id": agent_id, "bearer_id": bearer_id,
                    "revoked": True}
        return self._mutation(
            "revoke_bearer", idempotency_key,
            {"agent_id": agent_id, "bearer_id": bearer_id}, effect,
        )

    def _patch_agent(self, agent_id: str, updates: Dict[str, Any],
                     prior_keys: List[str]) -> Dict[str, Any]:
        prior_agent = self.get_agent(agent_id)
        prior = {key: prior_agent.get(key) for key in prior_keys}
        self._request(
            self.platform_url, f"/agents/{self.org_id}/{agent_id}",
            method="PATCH", body=updates,
        )
        return {"agent_id": agent_id, "applied": updates,
                "rollback_ref": {"kind": "patch_agent",
                                 "agent_id": agent_id, "prior": prior}}

    def set_workflow_allowlist(
        self, agent_id: str, workflow_ids: List[str], *,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Pin the agent's workflow allowlist; the prior list is the
        rollback reference."""
        workflow_ids = [str(w) for w in workflow_ids]
        return self._mutation(
            "set_workflow_allowlist", idempotency_key,
            {"agent_id": agent_id, "workflow_ids": workflow_ids},
            lambda: self._patch_agent(
                agent_id, {"workflow_allowlist": workflow_ids},
                ["workflow_allowlist"],
            ),
        )

    def bind_agent_collections(
        self, agent_id: str, collection_ids: List[str], *,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        collection_ids = [str(c) for c in collection_ids]
        return self._mutation(
            "bind_agent_collections", idempotency_key,
            {"agent_id": agent_id, "collection_ids": collection_ids},
            lambda: self._patch_agent(
                agent_id,
                {"data_collection_allowlist": collection_ids},
                ["data_collection_allowlist"],
            ),
        )

    # -- workflows: persist / publish / versions / rollback ---------------------------

    def persist_workflow(
        self,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
        create_only: bool = False,
    ) -> Dict[str, Any]:
        """Create or update a workflow definition (one new version).

        ``payload`` is a full ``AdvancedWorkflowPayload`` dict carrying its
        own ``id`` (the caller supplies a stable UUID so create is
        idempotent across reconciles). The version persisted becomes the
        pin; the rollback reference is the prior top version when the
        workflow already existed, else ``delete_workflow`` — undoing a
        first persist removes the asset.
        """
        workflow_id = str((payload or {}).get("id") or "").strip()
        if not workflow_id:
            raise CapitolError(
                "persist_workflow payload requires a stable id"
            )
        name = str((payload or {}).get("name") or "")

        def effect() -> Dict[str, Any]:
            prior_version: Dict[str, Any] = {}
            existed = False
            try:
                prior_version = self._current_version(workflow_id)
                existed = bool(
                    prior_version
                    or (self.get_workflow(workflow_id) or {}).get("workflow")
                )
            except CapitolError as exc:
                # 404 = no such workflow; the access layer answers 403 for
                # unknown ids too (existence is not revealed). Either way
                # the POST below is the authoritative act — a genuine
                # permission problem fails there.
                if exc.http_status not in (403, 404):
                    raise
            if existed and create_only:
                raise CapitolError(
                    f"workflow {workflow_id} already exists; compiled "
                    "workflow updates are blocked until exact prior-version "
                    "rollback is implemented (reuse/adopt it explicitly)"
                )
            persisted = self._request(
                self.workflow_url,
                f"/api/v1/orgs/{self.org_id}/workflows",
                method="POST", body=dict(payload),
            )
            new_version = self._current_version(workflow_id)
            rollback_ref: Dict[str, Any] = (
                {"kind": "persist_prior_version",
                 "workflow_id": workflow_id,
                 "prior_version_id": str(prior_version.get("id") or "")}
                if existed else
                {"kind": "delete_workflow", "workflow_id": workflow_id}
            )
            return {
                "workflow_id": str(
                    (persisted or {}).get("workflow_id") or workflow_id
                ),
                "name": name,
                "created": not existed,
                "version_pin": str(new_version.get("id") or ""),
                "version_number": new_version.get("version_number"),
                "rollback_ref": rollback_ref,
            }
        return self._mutation(
            "persist_workflow", idempotency_key,
            {"workflow_id": workflow_id, "name": name}, effect,
        )

    def delete_workflow(self, workflow_id: str, *,
                        idempotency_key: str) -> Dict[str, Any]:
        def effect() -> Dict[str, Any]:
            self._request(
                self.workflow_url,
                f"/api/v1/orgs/{self.org_id}/workflows/{workflow_id}",
                method="DELETE",
            )
            return {"workflow_id": workflow_id, "deleted": True}
        return self._mutation(
            "delete_workflow", idempotency_key,
            {"workflow_id": workflow_id}, effect,
        )

    def workflow_versions(self, workflow_id: str) -> List[Dict[str, Any]]:
        payload = self._request(
            self.workflow_url,
            f"/api/v1/orgs/{self.org_id}/workflows/{workflow_id}/versions",
        )
        versions = (payload or {}).get("versions")
        return versions if isinstance(versions, list) else []

    def _current_version(self, workflow_id: str) -> Dict[str, Any]:
        versions = self.workflow_versions(workflow_id)
        return versions[0] if versions else {}

    def get_workflow(self, workflow_id: str) -> Dict[str, Any]:
        payload = self._request(
            self.workflow_url,
            f"/api/v1/orgs/{self.org_id}/workflows/{workflow_id}",
        )
        return payload if isinstance(payload, dict) else {}

    def publish_workflow(self, workflow_id: str, *,
                         idempotency_key: str) -> Dict[str, Any]:
        """Set ``publish_to_api`` on a workflow (persisting a new version).

        The version persisted becomes the pin; the version current before
        the mutation is the rollback reference. Already-published
        workflows are a recorded no-op.
        """
        def effect() -> Dict[str, Any]:
            envelope = self.get_workflow(workflow_id)
            inner = envelope.get("workflow") or {}
            payload = inner.get("payload") or {}
            if not payload:
                raise CapitolError(
                    f"workflow {workflow_id} has no payload to persist"
                )
            prior_version = self._current_version(workflow_id)
            if payload.get("publish_to_api") is True:
                return {
                    "workflow_id": workflow_id,
                    "already_published": True,
                    "version_pin": str(prior_version.get("id") or ""),
                    "rollback_ref": {"kind": "noop"},
                }
            payload["publish_to_api"] = True
            persisted = self._request(
                self.workflow_url,
                f"/api/v1/orgs/{self.org_id}/workflows",
                method="POST", body=payload,
            )
            new_version = self._current_version(workflow_id)
            return {
                "workflow_id": str(
                    persisted.get("workflow_id") or workflow_id
                ),
                "published": True,
                "version_pin": str(new_version.get("id") or ""),
                "rollback_ref": {
                    "kind": "rollback_workflow",
                    "workflow_id": workflow_id,
                    "prior_version_id": str(prior_version.get("id") or ""),
                },
            }
        return self._mutation(
            "publish_workflow", idempotency_key,
            {"workflow_id": workflow_id}, effect,
        )

    def rollback_workflow(self, workflow_id: str, *,
                          idempotency_key: str) -> Dict[str, Any]:
        """Revert a Conch-published workflow to its pre-publish state.

        The exact inverse of :meth:`publish_workflow`: re-persist the
        current payload with ``publish_to_api`` cleared, through the same
        workflow-api ``POST /workflows`` endpoint publish used. This keeps
        publish and undo in one version lineage (a new PUBLISHED version
        recording the reverted state) and is verifiable by a follow-up
        GET. An already-unpublished workflow is a recorded no-op.

        (The platform-api ``/agentic-workflows/.../rollback`` endpoint
        reads a *separate* version store that workflow-api publishes do
        not populate, so it 404s / 400s for workflows published this way —
        see the DIVERGENCES note in :mod:`conch.capitol.crossclient`.)
        """
        def effect() -> Dict[str, Any]:
            envelope = self.get_workflow(workflow_id)
            inner = envelope.get("workflow") or {}
            payload = inner.get("payload") or {}
            if not payload:
                raise CapitolError(
                    f"workflow {workflow_id} has no payload to persist"
                )
            before = self._current_version(workflow_id)
            if payload.get("publish_to_api") is not True:
                return {
                    "workflow_id": workflow_id,
                    "already_unpublished": True,
                    "version_pin": str(before.get("id") or ""),
                    "rollback_ref": {"kind": "noop"},
                }
            payload["publish_to_api"] = False
            self._request(
                self.workflow_url,
                f"/api/v1/orgs/{self.org_id}/workflows",
                method="POST", body=payload,
            )
            after = self._current_version(workflow_id)
            return {
                "workflow_id": workflow_id,
                "rolled_back": True,
                "rolled_back_from": str(before.get("id") or ""),
                "version_pin": str(after.get("id") or ""),
                "rollback_ref": {
                    "kind": "publish_workflow",
                    "workflow_id": workflow_id,
                    "prior_version_id": str(before.get("id") or ""),
                },
            }
        return self._mutation(
            "rollback_workflow", idempotency_key,
            {"workflow_id": workflow_id}, effect,
        )

    # -- schedules ---------------------------------------------------------------

    def list_schedules(self, workflow_id: str) -> List[Dict[str, Any]]:
        payload = self._request(
            self.workflow_url,
            f"/api/v1/orgs/{self.org_id}/workflows/{workflow_id}/schedules",
        )
        if isinstance(payload, list):
            return payload
        schedules = (payload or {}).get("schedules")
        return schedules if isinstance(schedules, list) else []

    def create_schedule(
        self, workflow_id: str, name: str, cron_expression: str, *,
        idempotency_key: str, timezone: str = "UTC",
        input_overrides: Optional[Dict[str, Any]] = None,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        def effect() -> Dict[str, Any]:
            for schedule in self.list_schedules(workflow_id):
                if str(schedule.get("name") or "") == str(name):
                    return {
                        "workflow_id": workflow_id,
                        "schedule_id": str(schedule.get("id") or ""),
                        "adopted_existing": True,
                        "rollback_ref": {
                            "kind": "delete_schedule",
                            "workflow_id": workflow_id,
                            "schedule_id": str(schedule.get("id") or ""),
                        },
                    }
            body: Dict[str, Any] = {
                "name": str(name),
                "cron_expression": str(cron_expression),
                "timezone": str(timezone),
                "enabled": bool(enabled),
            }
            if input_overrides is not None:
                body["input_overrides"] = input_overrides
            payload = self._request(
                self.workflow_url,
                f"/api/v1/orgs/{self.org_id}/workflows/{workflow_id}"
                "/schedules",
                method="POST", body=body,
            )
            schedule_id = str(
                (payload or {}).get("id")
                or (payload or {}).get("schedule_id") or ""
            )
            return {
                "workflow_id": workflow_id,
                "schedule_id": schedule_id,
                "cron_expression": str(cron_expression),
                "enabled": bool(enabled),
                "rollback_ref": {"kind": "delete_schedule",
                                 "workflow_id": workflow_id,
                                 "schedule_id": schedule_id},
            }
        return self._mutation(
            "create_schedule", idempotency_key,
            {"workflow_id": workflow_id, "name": str(name),
             "cron_expression": str(cron_expression)},
            effect,
        )

    def update_schedule(
        self, workflow_id: str, schedule_id: str,
        updates: Dict[str, Any], *, idempotency_key: str,
    ) -> Dict[str, Any]:
        def effect() -> Dict[str, Any]:
            prior = {}
            for schedule in self.list_schedules(workflow_id):
                if str(schedule.get("id") or "") == str(schedule_id):
                    prior = {
                        key: schedule.get(key)
                        for key in updates if key in schedule
                    }
                    break
            payload = self._request(
                self.workflow_url,
                f"/api/v1/orgs/{self.org_id}/workflows/{workflow_id}"
                f"/schedules/{schedule_id}",
                method="PUT", body=dict(updates),
            )
            return {
                "workflow_id": workflow_id,
                "schedule_id": schedule_id,
                "applied": dict(updates),
                "schedule": _scrub(payload) if isinstance(payload, dict)
                else {},
                "rollback_ref": {"kind": "update_schedule",
                                 "workflow_id": workflow_id,
                                 "schedule_id": schedule_id,
                                 "prior": prior},
            }
        return self._mutation(
            "update_schedule", idempotency_key,
            {"workflow_id": workflow_id, "schedule_id": schedule_id},
            effect,
        )

    def delete_schedule(self, workflow_id: str, schedule_id: str, *,
                        idempotency_key: str) -> Dict[str, Any]:
        def effect() -> Dict[str, Any]:
            self._request(
                self.workflow_url,
                f"/api/v1/orgs/{self.org_id}/workflows/{workflow_id}"
                f"/schedules/{schedule_id}",
                method="DELETE",
            )
            return {"workflow_id": workflow_id,
                    "schedule_id": schedule_id, "deleted": True}
        return self._mutation(
            "delete_schedule", idempotency_key,
            {"workflow_id": workflow_id, "schedule_id": schedule_id},
            effect,
        )

    # -- collections -------------------------------------------------------------

    def list_collections(self) -> List[Dict[str, Any]]:
        payload = self._request(
            self.platform_url, f"/collections/{self.org_id}"
        )
        if isinstance(payload, list):
            return payload
        collections = (payload or {}).get("collections")
        return collections if isinstance(collections, list) else []

    def create_collection(
        self, name: str, *, idempotency_key: str,
        destination: str = "qdrant", description: str = "",
    ) -> Dict[str, Any]:
        def effect() -> Dict[str, Any]:
            for collection in self.list_collections():
                if str(collection.get("name") or "") == str(name):
                    return {
                        "collection_id": str(collection.get("id") or ""),
                        "name": str(name),
                        "adopted_existing": True,
                        "rollback_ref": {
                            "kind": "delete_collection",
                            "collection_id": str(
                                collection.get("id") or ""
                            ),
                        },
                    }
            payload = self._request(
                self.platform_url, f"/collections/{self.org_id}",
                method="POST", body={
                    "orgid": self.org_id,
                    "name": str(name),
                    "destination": str(destination),
                    "description": description
                    or f"Conch-provisioned collection {name}",
                },
            )
            row = (
                payload.get("collection")
                if isinstance(payload, dict) and "collection" in payload
                else payload
            )
            collection_id = str(
                (row or {}).get("id") or (row or {}).get("collection_id")
                or ""
            )
            return {
                "collection_id": collection_id,
                "name": str(name),
                "rollback_ref": {"kind": "delete_collection",
                                 "collection_id": collection_id},
            }
        return self._mutation(
            "create_collection", idempotency_key,
            {"name": str(name), "destination": str(destination)}, effect,
        )

    def delete_collection(self, collection_id: str, *,
                          idempotency_key: str) -> Dict[str, Any]:
        """Soft-delete a collection (the platform's default DELETE)."""
        def effect() -> Dict[str, Any]:
            self._request(
                self.platform_url,
                f"/collections/{self.org_id}/{collection_id}",
                method="DELETE",
            )
            return {"collection_id": collection_id, "deleted": True}
        return self._mutation(
            "delete_collection", idempotency_key,
            {"collection_id": collection_id}, effect,
        )
