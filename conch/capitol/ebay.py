"""eBay pilot driver — sandbox photo→listing over governed Capitol workflows.

Milestone 1 of the eBay pilot contract (shell-first intake): ``/ebay
<photo…> [-- notes]`` uploads photos as private org-scoped artifacts,
starts the *Draft or Revise Listing* workflow, relays clarifying questions
(both the terminal ``needs_info`` revision loop and mid-run HITL
``node.input_required`` events), presents the immutable drafted revision,
evaluates the deterministic caps policy, and — only then — constructs the
exact ``ebay.publish_request.v1`` for the *Approve and Publish Listing*
workflow: confirmation exactly ``proceed to post``, challenge exactly
``POST r{rev} {hash[-12:]}``, idempotency key exactly
``{app}:{session}:r{rev}:publish``. Capitol's ``ebay_approval_node``
re-verifies all of it deterministically, so what the user approved is
byte-identical to what the effect validates — two independent staleness
checks in series.

Where determinism lives (and where it doesn't): the workflow's models own
everything judgment-shaped — what the item is, the title, description,
category, price, and whether clarification is worth asking (the driver
relays ``open_questions``/HITL prompts verbatim and imposes no field
checklists). Deterministic machinery exists only at the
money/irreversibility boundaries:

- the exact publish action (confirmation/challenge/idempotency-key
  construction, plus the caps clamp deciding auto vs exact approval),
- the required-policy registry consult before submitting it, and
- durable run linkage (run ids, revision hashes, listing ids, cursors)
  in a tiny versioned JSON state file the Phase 1 kernel replaces later.

Workflow/model text is untrusted business data throughout: it is
displayed (control characters stripped) and echoed into the next
request's data fields — it never selects or executes a tool.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import get_bool
from ..policy import evaluate_required_policy
from .client import FINAL_STATUS_EVENT, CapitolRuntime
from .errors import CapitolAuthError, CapitolError, CapitolProtocolError

DRAFT_REQUEST_SCHEMA = "ebay.draft_request.v1"
PUBLISH_REQUEST_SCHEMA = "ebay.publish_request.v1"
REVISION_SCHEMA = "ebay.listing_revision.v1"
EFFECT_SCHEMA = "ebay.inventory_effect.v1"
REJECTION_SCHEMA = "ebay.approval_rejection.v1"

#: Exact-approval constants enforced by Capitol's ``ebay_approval_node``.
CONFIRMATION_PHRASE = "proceed to post"

MAX_PHOTOS = 12
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")
MAX_PHOTO_BYTES = 50 * 1024 * 1024
MAX_CLARIFY_ROUNDS = 5

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(value: Any, limit: int = 4000) -> str:
    """Workflow/model text is untrusted: strip control bytes, bound size."""
    text = _CONTROL_RE.sub("", str(value if value is not None else ""))
    return text[:limit]


def challenge_for(revision: int, draft_hash: str) -> str:
    """The literal approval challenge: ``POST r{rev} {hash[-12:]}``."""
    return f"POST r{int(revision)} {str(draft_hash)[-12:]}"


def publish_idempotency_key(app_id: str, session_id: str, revision: int) -> str:
    return f"{app_id}:{session_id}:r{int(revision)}:publish"


def draft_idempotency_key(app_id: str, session_id: str, revision: int) -> str:
    return f"{app_id}:{session_id}:r{int(revision)}:draft"


def extract_contracts(value: Any) -> List[Dict[str, Any]]:
    """Recursively collect every object whose ``schema`` starts ``ebay.``.

    This is the oversight app's audit-contract extraction: machine
    contracts are read from workflow outputs (never parsed out of chat
    prose, where the gateway redacts hashes).
    """
    found: List[Dict[str, Any]] = []

    def walk(node: Any):
        if isinstance(node, dict):
            schema = node.get("schema")
            if isinstance(schema, str) and schema.startswith("ebay."):
                found.append(node)
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


def find_contract(value: Any, schema: str) -> Optional[Dict[str, Any]]:
    for contract in extract_contracts(value):
        if contract.get("schema") == schema:
            return contract
    return None


# ---------------------------------------------------------------------------
# Caps: a thin clamp on outcomes at the publish boundary. Listing content
# (title, description, category choice, pricing judgment) is the
# workflow's call and is never linted or corrected here — the clamp only
# decides whether the outcome may auto-publish or needs exact approval.
# ---------------------------------------------------------------------------

class CapsDecision:
    """Outcome of the clamp for one drafted revision."""

    def __init__(self, auto: bool, reasons: List[str]):
        self.auto = bool(auto)
        self.reasons = list(reasons)

    def __repr__(self) -> str:
        return f"CapsDecision(auto={self.auto}, reasons={self.reasons!r})"


def evaluate_caps(config: dict, revision: Dict[str, Any]) -> CapsDecision:
    """Price ceiling + optional category allowlist + an on/off toggle.

    When a configured cap cannot read its outcome (no price on the
    revision while a ceiling is set), the clamp routes to exact approval
    — clamps clamp; they never guess.
    """
    reasons: List[str] = []
    listing = revision.get("listing") or {}
    if not get_bool(config, "ebay_auto_publish", True):
        reasons.append("auto-publish is disabled (ebay_auto_publish=false)")

    allowed_raw = str(config.get("ebay_allowed_category_ids") or "").strip()
    if allowed_raw:
        allowed = {c.strip() for c in allowed_raw.split(",") if c.strip()}
        category = str(listing.get("category_id") or "").strip()
        if category not in allowed:
            reasons.append(
                f"category {category or '(none)'} is outside the allowlist "
                f"({', '.join(sorted(allowed))})"
            )

    ceiling_raw = str(config.get("ebay_max_price_usd") or "").strip()
    if ceiling_raw:
        try:
            ceiling = float(ceiling_raw)
        except ValueError:
            ceiling = None
            reasons.append(
                f"ebay_max_price_usd={ceiling_raw!r} is not a number"
            )
        if ceiling is not None:
            try:
                price = float((listing.get("price") or {}).get("value"))
            except (TypeError, ValueError):
                price = None
            if price is None:
                reasons.append(
                    "the revision has no readable price to clamp"
                )
            elif price > ceiling:
                reasons.append(
                    f"price {price:.2f} USD is above the "
                    f"{ceiling:.2f} ceiling"
                )
    return CapsDecision(not reasons, reasons)


# ---------------------------------------------------------------------------
# Run-linkage state (tiny, forward-compatible; Phase 1 kernel replaces it)
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.RLock()
STATE_VERSION = 1


def _state_path() -> Path:
    root = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
    )
    return root / "conch" / "ebay_pilot.json"


class PilotState:
    """Durable {session → runs/revisions/listing} linkage, atomic writes."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or _state_path()

    def load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"version": STATE_VERSION, "sessions": {}}
        if not isinstance(data, dict) or "sessions" not in data:
            return {"version": STATE_VERSION, "sessions": {}}
        return data

    def _save(self, data: Dict[str, Any]):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self._path)
        try:
            self._path.chmod(0o600)
        except OSError:
            pass

    def update_session(self, session_id: str, **fields) -> Dict[str, Any]:
        with _STATE_LOCK:
            data = self.load()
            session = data["sessions"].setdefault(
                session_id, {"created_at": time.time()}
            )
            for key, value in fields.items():
                session[key] = value
            session["updated_at"] = time.time()
            self._save(data)
            return session

    def append(self, session_id: str, key: str, entry: Dict[str, Any]):
        with _STATE_LOCK:
            data = self.load()
            session = data["sessions"].setdefault(
                session_id, {"created_at": time.time()}
            )
            session.setdefault(key, []).append(entry)
            session["updated_at"] = time.time()
            self._save(data)

    def record_run(
        self, session_id: str, kind: str, run_id: str, idempotency_key: str
    ):
        self.append(session_id, "runs", {
            "kind": kind,
            "run_id": run_id,
            "idempotency_key": idempotency_key,
            "started_at": time.time(),
            "last_sequence": 0,
        })

    def update_run(self, session_id: str, run_id: str, **fields):
        with _STATE_LOCK:
            data = self.load()
            session = data["sessions"].get(session_id) or {}
            for run in session.get("runs", []):
                if run.get("run_id") == run_id:
                    run.update(fields)
            self._save(data)

    def sessions(self) -> Dict[str, Any]:
        return dict(self.load().get("sessions", {}))


# ---------------------------------------------------------------------------
# Request constructors (deterministic; exact-match fields come from the
# immutable revision itself, never re-derived)
# ---------------------------------------------------------------------------

def build_draft_request(
    config: dict,
    *,
    listing_session_id: str,
    thread_id: str,
    media: List[Dict[str, Any]],
    item_context: str,
    mode: str = "initial",
    expected_revision: int = 1,
    current_revision: Optional[Dict[str, Any]] = None,
    revision_feedback: str = "",
) -> Dict[str, Any]:
    actor = str(config.get("ebay_actor_id") or "").strip()
    if not actor:
        raise CapitolError(
            "ebay_actor_id is not configured (the org principal UUID the "
            "revision is attributed to)"
        )
    policies = {
        "fulfillment_policy_id": str(
            config.get("ebay_fulfillment_policy_id") or ""
        ).strip(),
        "payment_policy_id": str(
            config.get("ebay_payment_policy_id") or ""
        ).strip(),
        "return_policy_id": str(
            config.get("ebay_return_policy_id") or ""
        ).strip(),
        "merchant_location_key": str(
            config.get("ebay_merchant_location_key") or ""
        ).strip(),
    }
    missing = [key for key, value in policies.items() if not value]
    if missing:
        raise CapitolError(
            "seller policies are not configured: set ebay_"
            + ", ebay_".join(missing)
        )
    request: Dict[str, Any] = {
        "schema": DRAFT_REQUEST_SCHEMA,
        "mode": mode,
        "app_id": str(config.get("ebay_app_id") or "conch-ebay"),
        "account_ref": str(
            config.get("ebay_account_ref") or "org-ebay-sandbox"
        ),
        "listing_session_id": listing_session_id,
        "expected_revision": int(expected_revision),
        "actor_principal_id": actor,
        "channel": "a2a",
        "thread_id": thread_id,
        "media": [
            {"artifact_id": item["artifact_id"], "order": index}
            for index, item in enumerate(media)
        ],
        "seller_policies": policies,
        "item_context": item_context
        or "Image-only listing request; no user-supplied listing metadata.",
    }
    if mode == "revise":
        if current_revision is None:
            raise CapitolError("revise mode requires the current revision")
        request["current_revision"] = current_revision
        request["revision_feedback"] = revision_feedback
    return request


def build_revise_request(
    config: dict,
    revision: Dict[str, Any],
    *,
    revision_feedback: str,
    item_context: str = "",
) -> Dict[str, Any]:
    """A parent+1 revise request derived entirely from the immutable
    revision: identity fields, media (artifact ids + verified digests),
    and seller policies are echoed verbatim so lineage cannot drift;
    config only backfills policies when a partial (``needs_info``)
    revision omits them.
    """
    for field in ("app_id", "account_ref", "listing_session_id",
                  "actor_principal_id", "channel", "thread_id", "revision"):
        if not revision.get(field):
            raise CapitolError(
                f"current revision is missing {field!r}; refusing to revise"
            )
    media = [
        {
            "artifact_id": item.get("artifact_id"),
            "digest": item.get("digest"),
            "order": index,
        }
        for index, item in enumerate(revision.get("media") or [])
        if item.get("artifact_id")
    ]
    if not media:
        raise CapitolError("current revision carries no media to revise from")
    policies = dict(
        (revision.get("listing") or {}).get("policies") or {}
    )
    for key, config_key in (
        ("fulfillment_policy_id", "ebay_fulfillment_policy_id"),
        ("payment_policy_id", "ebay_payment_policy_id"),
        ("return_policy_id", "ebay_return_policy_id"),
        ("merchant_location_key", "ebay_merchant_location_key"),
    ):
        if not policies.get(key):
            fallback = str(config.get(config_key) or "").strip()
            if fallback:
                policies[key] = fallback
    missing = [
        key for key in ("fulfillment_policy_id", "payment_policy_id",
                        "return_policy_id", "merchant_location_key")
        if not policies.get(key)
    ]
    if missing:
        raise CapitolError(
            "revise needs seller policies (absent from the revision and "
            "config): " + ", ".join(missing)
        )
    return {
        "schema": DRAFT_REQUEST_SCHEMA,
        "mode": "revise",
        "app_id": revision["app_id"],
        "account_ref": revision["account_ref"],
        "listing_session_id": revision["listing_session_id"],
        "expected_revision": int(revision["revision"]) + 1,
        "actor_principal_id": revision["actor_principal_id"],
        "channel": revision["channel"],
        "thread_id": revision["thread_id"],
        "media": media,
        "seller_policies": {
            key: policies[key]
            for key in ("fulfillment_policy_id", "payment_policy_id",
                        "return_policy_id", "merchant_location_key")
        },
        "item_context": item_context
        or "Image-only listing request; no user-supplied listing metadata.",
        "current_revision": revision,
        "revision_feedback": revision_feedback,
    }


def build_publish_request(
    revision: Dict[str, Any],
    *,
    actor_principal_id: str,
    presentation_id: str,
) -> Dict[str, Any]:
    """The exact action: every staleness-checked field is copied verbatim
    from the immutable revision; the challenge/key are derived from its
    revision number and hash by the contract formulas."""
    for field in ("app_id", "listing_session_id", "revision", "draft_hash",
                  "channel", "thread_id"):
        if field not in revision:
            raise CapitolError(
                f"revision is missing {field!r}; refusing to construct a "
                "publish request from an incomplete contract"
            )
    revision_number = int(revision["revision"])
    draft_hash = str(revision["draft_hash"])
    return {
        "schema": PUBLISH_REQUEST_SCHEMA,
        "app_id": revision["app_id"],
        "listing_session_id": revision["listing_session_id"],
        "revision": revision_number,
        "draft_hash": draft_hash,
        "actor_principal_id": actor_principal_id,
        "channel": revision["channel"],
        "thread_id": revision["thread_id"],
        "presentation_id": presentation_id,
        "confirmation": CONFIRMATION_PHRASE,
        "challenge": challenge_for(revision_number, draft_hash),
        "idempotency_key": publish_idempotency_key(
            str(revision["app_id"]),
            str(revision["listing_session_id"]),
            revision_number,
        ),
        "current_revision": revision,
    }


# ---------------------------------------------------------------------------
# The pilot flow
# ---------------------------------------------------------------------------

class EbayPilot:
    """Drives one listing session; UI arrives as injected callables so the
    same flow serves the shell today and channel threads in Milestone 1b."""

    def __init__(
        self,
        runtime: CapitolRuntime,
        config: dict,
        *,
        state: Optional[PilotState] = None,
        say: Callable[[str], None] = print,
        ask: Optional[Callable[[str], str]] = None,
        confirm: Optional[Callable[[str], bool]] = None,
    ):
        self.runtime = runtime
        self.config = config
        self.state = state or PilotState()
        self.say = say
        self.ask = ask or (lambda prompt: input(prompt))
        self.confirm = confirm or self._default_confirm
        self._input_keys: Dict[str, str] = {}

    def _default_confirm(self, prompt: str) -> bool:
        return self.ask(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")

    # -- discovery ---------------------------------------------------------

    def resolve_workflows(self) -> Dict[str, str]:
        """Locate the draft + publish workflows on the agent's allowlist.

        Config pins (``ebay_draft_workflow`` / ``ebay_publish_workflow``)
        win; otherwise match by name. ``list_workflows`` returns the id
        ``call_workflow`` expects, so the discovered value is passed back
        verbatim.
        """
        pinned_draft = str(self.config.get("ebay_draft_workflow") or "").strip()
        pinned_publish = str(
            self.config.get("ebay_publish_workflow") or ""
        ).strip()
        if pinned_draft and pinned_publish:
            return {"draft": pinned_draft, "publish": pinned_publish}
        workflows = self.runtime.list_workflows()
        draft = pinned_draft
        publish = pinned_publish
        for workflow in workflows:
            name = str(workflow.get("name") or "").lower()
            identifier = str(
                workflow.get("workflow_id") or workflow.get("id") or ""
            )
            if not identifier:
                continue
            if not draft and "draft" in name:
                draft = identifier
            elif not publish and ("publish" in name or "approve" in name):
                publish = identifier
        if not draft or not publish:
            names = ", ".join(
                clean_text(w.get("name"), 80) or "?" for w in workflows
            ) or "(none)"
            raise CapitolError(
                "could not locate the draft/publish workflows on this "
                f"agent's allowlist (saw: {names}); pin them with "
                "ebay_draft_workflow / ebay_publish_workflow"
            )
        return {"draft": draft, "publish": publish}

    def _inputs_key(self, workflow_id: str) -> str:
        """Canonical inputs key for the workflow's JSON request input node."""
        if workflow_id in self._input_keys:
            return self._input_keys[workflow_id]
        key = "value"
        try:
            details = self.runtime.describe_workflow(workflow_id) or {}
            fields = [
                field for field in details.get("fields") or []
                if isinstance(field, dict)
            ]
            value_fields = [
                field for field in fields
                if str(field.get("field_id")) == "value"
            ]
            target = value_fields[0] if value_fields else (
                fields[0] if len(fields) == 1 else None
            )
            if target:
                node = str(target.get("node_instance_id") or "").strip()
                field_id = str(target.get("field_id") or "value")
                key = f"{node}.{field_id}" if node else field_id
        except CapitolError:
            pass  # fall back to the bare field id
        self._input_keys[workflow_id] = key
        return key

    # -- photos --------------------------------------------------------------

    def _validate_photo(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise CapitolError(f"photo not found: {path}")
        if path.suffix.lower() not in PHOTO_SUFFIXES:
            raise CapitolError(
                f"{path.name}: unsupported type (expected one of "
                f"{', '.join(PHOTO_SUFFIXES)})"
            )
        if path.stat().st_size > MAX_PHOTO_BYTES:
            raise CapitolError(f"{path.name}: over the 50 MB photo cap")
        return path

    def upload_photos(self, paths: List[str]) -> List[Dict[str, Any]]:
        if not paths:
            raise CapitolError("at least one photo path is required")
        if len(paths) > MAX_PHOTOS:
            raise CapitolError(f"at most {MAX_PHOTOS} photos per listing")
        media: List[Dict[str, Any]] = []
        for index, raw in enumerate(paths):
            path = self._validate_photo(raw)
            self.say(f"  uploading {path.name} …")
            uploaded = self.runtime.upload_artifact(str(path))
            uploaded["order"] = index
            media.append(uploaded)
        return media

    # -- run supervision -------------------------------------------------------

    def run_workflow(
        self,
        session_id: str,
        kind: str,
        workflow_id: str,
        request_value: Dict[str, Any],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Start a typed run, supervise it to terminal, return its output."""
        inputs = {self._inputs_key(workflow_id): request_value}
        submission = self.runtime.call_workflow(
            workflow_id, inputs, idempotency_key=idempotency_key
        )
        return self.supervise_run(
            session_id, kind, str(submission["run_id"]), idempotency_key
        )

    def supervise_run(
        self,
        session_id: str,
        kind: str,
        run_id: str,
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        """Watch a run to terminal and return its output.

        Mid-run ``node.input_required`` events are relayed to the user and
        answered through the HITL skills; every event advances the
        persisted ``last_sequence`` cursor so a resumed watch never
        replays or drops events.
        """
        self.state.record_run(session_id, kind, run_id, idempotency_key)
        self.say(f"  {kind} run {run_id} started")
        final_state = ""
        for event in self.runtime.watch_run(run_id):
            event_type = str(event.get("event_type") or "")
            sequence = event.get("sequence")
            if isinstance(sequence, (int, float)):
                self.state.update_run(
                    session_id, run_id, last_sequence=int(sequence)
                )
            if event_type == "node.node_started":
                node = (event.get("node") or {}).get("display_name") or ""
                if node:
                    self.say(f"    · {clean_text(node, 80)}")
            elif event_type == "node.input_required":
                self._answer_input_required(run_id, event)
            elif event_type == FINAL_STATUS_EVENT:
                final_state = str(
                    (event.get("data") or {}).get("state") or ""
                )
        status = self.runtime.run_status(run_id)
        run_state = str((status or {}).get("status") or final_state or "")
        self.state.update_run(session_id, run_id, status=run_state)
        if run_state.lower() != "success":
            error = clean_text((status or {}).get("error_message"), 500)
            raise CapitolError(
                f"{kind} run {run_id} ended {run_state or 'unknown'}"
                f"{': ' + error if error else ''}"
            )
        return self.runtime.workflow_output(run_id) or {}

    def _answer_input_required(self, run_id: str, event: Dict[str, Any]):
        """Relay a HITL checkpoint to the user; answers go back through the
        typed HITL skills (never through chat prose)."""
        data = event.get("data") or {}
        node = event.get("node") or {}
        request_id = str(
            data.get("request_id")
            or (data.get("extra") or {}).get("request_id")
            or ""
        )
        prompt = clean_text(
            data.get("prompt")
            or (data.get("extra") or {}).get("prompt")
            or "The workflow needs input to continue.",
            2000,
        )
        if str(data.get("input_kind") or "") == "clarification":
            self.say(f"\n  The workflow asks: {prompt}")
            answer = self.ask("  your answer: ").strip()
            self.runtime.submit_clarification(
                run_id, request_id, answer, declined=not answer
            )
            return
        # Human-Intervention panel: literal continue/stop token protocol.
        self.say(f"\n  Checkpoint: {prompt}")
        proceed = self.confirm("  continue this run?")
        self.runtime.submit_intervention(
            run_id,
            str(node.get("node_id") or ""),
            request_id,
            "continue" if proceed else "stop",
        )

    # -- presentation -----------------------------------------------------------

    def present_revision(self, revision: Dict[str, Any]):
        listing = revision.get("listing") or {}
        price = listing.get("price") or {}
        lines = [
            "",
            f"  Draft revision r{revision.get('revision')} "
            f"({clean_text(revision.get('draft_hash'), 80)})",
            f"    Title:     {clean_text(listing.get('title'), 120)}",
            f"    Category:  {clean_text(listing.get('category_id'), 40)}",
            f"    Condition: {clean_text(listing.get('condition'), 60)}",
            f"    Price:     {clean_text(price.get('value'), 20)} "
            f"{clean_text(price.get('currency'), 8)}",
            f"    Quantity:  {clean_text(listing.get('quantity'), 12)}",
            f"    Media:     {len(revision.get('media') or [])} photo(s)",
        ]
        policies = listing.get("policies") or {}
        if policies:
            lines.append(
                "    Shipping:  fulfillment "
                f"{clean_text(policies.get('fulfillment_policy_id'), 40)}, "
                f"returns {clean_text(policies.get('return_policy_id'), 40)}"
            )
        description = clean_text(listing.get("description"), 300)
        if description:
            lines.append(f"    About:     {description}")
        for warning in revision.get("warnings") or []:
            lines.append(f"    ⚠ {clean_text(warning, 200)}")
        self.say("\n".join(lines))

    # -- intake paths -------------------------------------------------------------

    def _extract_revision(
        self, session_id: str, output: Dict[str, Any]
    ) -> Dict[str, Any]:
        revision = find_contract(output, REVISION_SCHEMA)
        if revision is None:
            guidance = find_contract(output, "ebay.draft_request_guidance.v1")
            detail = ""
            if guidance:
                detail = clean_text(
                    (guidance.get("error_detail") or {}).get("message")
                    or guidance.get("message"), 300,
                )
            raise CapitolError(
                "draft run produced no ebay.listing_revision.v1 contract"
                + (f" (workflow guidance: {detail})" if detail else "")
            )
        self.state.append(session_id, "revisions", {
            "revision": revision.get("revision"),
            "draft_hash": revision.get("draft_hash"),
            "status": revision.get("status"),
        })
        return revision

    def _initial_draft_typed(
        self,
        session_id: str,
        workflows: Dict[str, str],
        photo_paths: List[str],
        item_context: str,
    ) -> Dict[str, Any]:
        """Typed intake: upload photos, then call the draft workflow with a
        constructed ebay.draft_request.v1.

        Known gateway gap (2026-09-11, local stack): request_upload_url /
        upload_file register artifacts only in the gateway's 24 h registry,
        which the eBay image node cannot resolve (it authorizes durable org
        Artifact rows) — the run then degrades to the typed no-image branch.
        Chat intake below is the promoted path; this stays for gateways
        that promote typed uploads.
        """
        app_id = str(self.config.get("ebay_app_id") or "conch-ebay")
        media = self.upload_photos(photo_paths)
        self.state.update_session(session_id, media=[
            {k: item[k] for k in ("artifact_id", "digest", "order", "filename")}
            for item in media
        ])
        request = build_draft_request(
            self.config,
            listing_session_id=session_id,
            thread_id=f"{session_id}:shell",
            media=media,
            item_context=item_context,
        )
        output = self.run_workflow(
            session_id, "draft", workflows["draft"], request,
            draft_idempotency_key(app_id, session_id, 1),
        )
        return self._extract_revision(session_id, output)

    def _initial_draft_chat(
        self,
        session_id: str,
        workflows: Dict[str, str],
        photo_paths: List[str],
        item_context: str,
    ) -> Dict[str, Any]:
        """Chat intake (the agent's designed path): photos ride the chat
        message as FileParts — the one A2A upload path the gateway promotes
        into durable org artifacts the image node can resolve — and the
        orchestrator's allowlisted draft_or_revise tool launches the run.
        The reply prose is untrusted and only displayed; the revision
        contract is read from the run's typed outputs.
        """
        if not photo_paths:
            raise CapitolError("at least one photo path is required")
        if len(photo_paths) > MAX_PHOTOS:
            raise CapitolError(f"at most {MAX_PHOTOS} photos per listing")
        for path in photo_paths:
            self._validate_photo(path)
        self.say("  attaching photo(s) to the intake message …")
        message = (
            "Please draft a sandbox eBay listing from the attached "
            f"photo(s). Item context: {item_context}"
        )
        reply = ""
        payload: Dict[str, Any] = {}
        try:
            payload = self.runtime.chat(message, files=photo_paths) or {}
            reply = clean_text(payload.get("assistant_reply"), 600)
        except (CapitolAuthError, CapitolProtocolError):
            raise
        except CapitolError:
            # The blocking chat turn outlived its transport, but the run it
            # launched may be live server-side. Never resend the intake
            # blindly (that would start a second draft run) — reconcile
            # from the conversation's run history instead.
            self.say("  chat transport dropped; reconciling run state …")
        run_id = str(payload.get("run_id") or "")
        for _attempt in range(6):
            if run_id:
                break
            listing = self.runtime.list_runs(workflows["draft"], limit=10)
            for run in listing.get("runs") or []:
                context = str(
                    run.get("started_by_context") or run.get("context_id")
                    or ""
                )
                if context and context == self.runtime.context_id:
                    run_id = str(run.get("run_id") or "")
                    break
            if not run_id:
                time.sleep(5)
        if not run_id:
            raise CapitolError(
                "the agent did not start a draft run"
                + (f" — it said: {reply}" if reply else "")
            )
        if reply:
            self.say(f"  agent: {reply}")
        output = self.supervise_run(session_id, "draft", run_id)
        return self._extract_revision(session_id, output)

    # -- the flow -----------------------------------------------------------------

    def sell(self, photo_paths: List[str], notes: str = "") -> Dict[str, Any]:
        """Full Milestone-1 loop: photos → draft (+clarify) → policy gate →
        exact approval → sandbox publish → listing id."""
        workflows = self.resolve_workflows()
        session_id = (
            f"conch-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        )
        intake = str(self.config.get("ebay_intake") or "chat").strip().lower()
        # Chat intake binds the server-side listing session to the A2A
        # context, so every listing session gets a fresh handshake.
        if intake == "chat" or not self.runtime.context_id:
            self.runtime.handshake()
        self.state.update_session(
            session_id,
            intake=intake,
            context_id=self.runtime.context_id,
            workflows=workflows,
        )
        self.say(f"  listing session {session_id} (intake: {intake})")

        item_context = notes.strip() or (
            "Image-only listing request; no user-supplied listing metadata."
        )
        if intake == "typed":
            revision = self._initial_draft_typed(
                session_id, workflows, photo_paths, item_context
            )
        else:
            revision = self._initial_draft_chat(
                session_id, workflows, photo_paths, item_context
            )

        clarify_rounds = 0
        while revision.get("status") == "needs_info":
            if clarify_rounds >= MAX_CLARIFY_ROUNDS:
                raise CapitolError(
                    "draft still needs info after "
                    f"{MAX_CLARIFY_ROUNDS} clarification rounds; stopping"
                )
            clarify_rounds += 1
            questions = [
                clean_text(question, 500)
                for question in revision.get("open_questions") or []
            ]
            self.say("\n  The draft needs more information:")
            answers: List[str] = []
            for question in questions or ["(no specific question provided)"]:
                answer = self.ask(f"    {question}\n    → ").strip()
                if answer:
                    answers.append(f"Q: {question} A: {answer}")
            feedback = (
                " ".join(answers) or "No further information available; "
                "proceed with conservative assumptions."
            )
            request = build_revise_request(
                self.config, revision,
                revision_feedback=feedback,
                item_context=item_context,
            )
            output = self.run_workflow(
                session_id, "draft", workflows["draft"], request,
                draft_idempotency_key(
                    str(request["app_id"]),
                    str(request["listing_session_id"]),
                    int(request["expected_revision"]),
                ),
            )
            revision = self._extract_revision(session_id, output)

        if revision.get("status") != "draft_review":
            raise CapitolError(
                f"unexpected revision status {revision.get('status')!r}"
            )
        self.present_revision(revision)

        decision = evaluate_caps(self.config, revision)
        challenge = challenge_for(
            int(revision["revision"]), str(revision["draft_hash"])
        )
        if decision.auto:
            self.say(
                "\n  Policy: within caps — auto-publish permitted "
                "(sandbox). Challenge: " + challenge
            )
            if not self.confirm("  publish this listing to the eBay sandbox?"):
                self.say("  stopped before publish (nothing was sent).")
                return {"session_id": session_id, "published": False,
                        "revision": revision}
        else:
            self.say("\n  Policy: exact approval required —")
            for reason in decision.reasons:
                self.say(f"    · {reason}")
            self.say(f"  To approve, type exactly: {CONFIRMATION_PHRASE}")
            typed = self.ask("  approval: ").strip().lower()
            if typed != CONFIRMATION_PHRASE:
                self.say("  approval phrase mismatch — not publishing.")
                return {"session_id": session_id, "published": False,
                        "revision": revision}

        presentation_id = f"conch-presentation-{uuid.uuid4().hex[:10]}"
        publish_request = build_publish_request(
            revision,
            actor_principal_id=str(
                revision.get("actor_principal_id")
                or self.config.get("ebay_actor_id")
                or ""
            ),
            presentation_id=presentation_id,
        )
        policy = evaluate_required_policy("capitol.ebay.publish", {
            "workflow_id": workflows["publish"],
            "app_id": publish_request["app_id"],
            "listing_session_id": publish_request["listing_session_id"],
            "revision": publish_request["revision"],
            "draft_hash": publish_request["draft_hash"],
            "idempotency_key": publish_request["idempotency_key"],
            "caps_auto": decision.auto,
        })
        if not policy.allowed:
            raise CapitolError(
                f"publish denied by required policy: {policy.reason}"
            )
        output = self.run_workflow(
            session_id, "publish", workflows["publish"], publish_request,
            publish_request["idempotency_key"],
        )
        effect = find_contract(output, EFFECT_SCHEMA)
        rejection = find_contract(output, REJECTION_SCHEMA)
        if effect is None and rejection is not None:
            detail = clean_text(
                (rejection.get("error_detail") or {}).get("message")
                or rejection.get("message"), 300,
            )
            raise CapitolError(
                f"publish was rejected by the approval gate: {detail}"
            )
        if effect is None:
            raise CapitolError(
                "publish run produced no ebay.inventory_effect.v1 contract"
            )
        listing = {
            "state": effect.get("state"),
            "listing_id": (effect.get("listing") or {}).get("listing_id")
            or effect.get("listing_id"),
            "offer_id": (effect.get("listing") or {}).get("offer_id")
            or effect.get("offer_id"),
            "sku": (effect.get("listing") or {}).get("sku")
            or effect.get("sku"),
            "listing_url": (effect.get("listing") or {}).get("listing_url")
            or effect.get("listing_url"),
        }
        self.state.update_session(session_id, listing=listing)
        state_text = clean_text(listing.get("state"), 40) or "?"
        self.say(
            f"\n  Publish effect: {state_text}"
            + (f" — listing {listing['listing_id']}" if listing.get("listing_id") else "")
            + (f"\n  {listing['listing_url']}" if listing.get("listing_url") else "")
        )
        return {
            "session_id": session_id,
            "published": str(listing.get("state") or "").upper() == "PUBLISHED",
            "revision": revision,
            "effect": effect,
            "listing": listing,
        }


# ---------------------------------------------------------------------------
# Shell entrypoint (/ebay …)
# ---------------------------------------------------------------------------

USAGE = (
    "\n  \033[1;36m/ebay — sandbox photo→listing pilot\033[0m\n"
    "    /ebay <photo…> [-- notes about the item]\n"
    "    /ebay sessions\n"
    "  \033[2mNeeds capitol_base_url/capitol_org/capitol_agent config and a\n"
    "  bearer in $CAPITOL_A2A_BEARER or ~/.capitol-a2a/agents.yaml.\n"
    "  Sandbox only: publishing goes through Capitol's exact-approval\n"
    "  workflow; nothing is sent without your confirmation.\033[0m\n"
)


def run_ebay_command(arg: str, config: dict) -> None:
    """Handle ``/ebay …`` from the interactive shell."""
    import shlex

    arg = (arg or "").strip()
    if not arg or arg.lower() in ("help", "-h", "--help"):
        print(USAGE)
        return
    if arg.lower() == "sessions":
        sessions = PilotState().sessions()
        if not sessions:
            print("\n  \033[2mNo eBay pilot sessions yet.\033[0m\n")
            return
        print(f"\n  \033[1;36meBay pilot sessions ({len(sessions)}):\033[0m")
        for session_id, info in sorted(sessions.items()):
            listing = info.get("listing") or {}
            revisions = info.get("revisions") or []
            tail = (
                f"listing {listing.get('listing_id')}"
                if listing.get("listing_id")
                else f"{len(revisions)} revision(s)"
            )
            print(f"    \033[1m{session_id}\033[0m  {tail}")
        print()
        return
    try:
        photo_args = shlex.split(arg)
    except ValueError as exc:
        print(f"\n  \033[31mInvalid arguments: {exc}\033[0m\n")
        return
    notes = ""
    if "--" in photo_args:
        split_at = photo_args.index("--")
        notes = " ".join(photo_args[split_at + 1:])
        photo_args = photo_args[:split_at]
    if not photo_args:
        print(USAGE)
        return
    try:
        runtime = CapitolRuntime.from_config(config)
        runtime.discover()
        pilot = EbayPilot(runtime, config)
        pilot.sell(photo_args, notes)
    except CapitolError as exc:
        print(f"\n  \033[31meBay pilot: {exc}\033[0m")
        if getattr(exc, "hint", ""):
            print(f"  \033[2m{exc.hint}\033[0m")
        print()
    except (KeyboardInterrupt, EOFError):
        print("\n  \033[33mstopped — no publish was submitted beyond "
              "completed runs.\033[0m\n")
