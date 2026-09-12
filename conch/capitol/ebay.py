"""eBay pilot driver — a thin shim over the generic flow-pack engine.

Milestone 1 of the eBay pilot contract (shell-first intake): ``/ebay
<photo…> [-- notes]`` drives the *Draft or Revise Listing* / *Approve
and Publish Listing* workflows. Since refactor stage R1 every behavior
here — request construction, clarification relay, the caps clamp, the
exact-approval publish (confirmation exactly ``proceed to post``,
challenge exactly ``POST r{rev} {hash[-12:]}``, idempotency key exactly
``{app}:{session}:r{rev}:publish``), contract extraction, and durable
run linkage — is executed by the generic engine in
:mod:`conch.capitol.packs.engine`, driven by the ``ebay-listing`` flow
pack (``conch/capitol/packs/data/ebay-listing/pack.json``). Capitol's
``ebay_approval_node`` re-verifies the exact-approval fields
deterministically, so what the user approved stays byte-identical to
what the effect validates — two independent staleness checks in series.

This module keeps the historical import surface (constants, request
constructors, caps evaluator, ``PilotState``, ``EbayPilot``,
``run_ebay_command``) as thin delegates so existing callers and tests
keep working; the golden fixtures in ``tests/test_capitol_golden.py``
prove wire/approval equivalence with the pre-extraction driver.

Workflow/model text is untrusted business data throughout: it is
displayed (control characters stripped) and echoed into the next
request's data fields — it never selects or executes a tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import CapitolRuntime
from .errors import CapitolError
from .packs import FlowPack, PackState, load_pack
from .packs.engine import (
    CapsDecision,
    PackShellFlow,
    build_request,
    clean_text,
    evaluate_caps as _evaluate_caps,
    extract_contracts as _extract_contracts,
    extract_effect_facts,
    find_contract as _find_contract,
    resolve_workflows as _resolve_workflows,
    start_chat_intake,
    workflow_inputs_key,
)
from .packs.state import STATE_VERSION  # noqa: F401  (compat re-export)
from .packs.templates import render_formula

__all__ = [
    "CONFIRMATION_PHRASE", "CapsDecision", "DRAFT_REQUEST_SCHEMA",
    "EFFECT_SCHEMA", "EbayPilot", "MAX_CLARIFY_ROUNDS", "MAX_PHOTOS",
    "MAX_PHOTO_BYTES", "PHOTO_SUFFIXES", "PUBLISH_REQUEST_SCHEMA",
    "PilotState", "REJECTION_SCHEMA", "REVISION_SCHEMA",
    "build_draft_request", "build_publish_request", "build_revise_request",
    "challenge_for", "clean_text", "draft_idempotency_key",
    "ebay_pack", "effect_listing", "evaluate_caps", "extract_contracts",
    "find_contract", "publish_idempotency_key", "resolve_workflows",
    "run_ebay_command", "start_chat_draft", "workflow_inputs_key",
]

_PACK: Optional[FlowPack] = None


def ebay_pack() -> FlowPack:
    """The built-in ``ebay-listing`` flow pack (loaded once, validated)."""
    global _PACK
    if _PACK is None:
        _PACK = load_pack("ebay-listing")
    return _PACK


def _approval() -> Dict[str, Any]:
    return ebay_pack().approval("ebay_publish") or {}


def _attachments() -> Dict[str, Any]:
    for intake in ebay_pack().intakes:
        if intake.get("attachments"):
            return intake["attachments"]
    return {}


DRAFT_REQUEST_SCHEMA = str(ebay_pack().request_spec("draft")["schema"])
PUBLISH_REQUEST_SCHEMA = str(ebay_pack().request_spec("publish")["schema"])
REVISION_SCHEMA = ebay_pack().session_schema
EFFECT_SCHEMA = ebay_pack().effect_schema
REJECTION_SCHEMA = ebay_pack().rejection_schema

#: Exact-approval constants enforced by Capitol's ``ebay_approval_node``.
CONFIRMATION_PHRASE = str((_approval().get("exact") or {}).get("phrase"))

MAX_PHOTOS = int(_attachments().get("max_count") or 12)
PHOTO_SUFFIXES = tuple(_attachments().get("suffixes") or ())
MAX_PHOTO_BYTES = int(_attachments().get("max_bytes") or 0)
MAX_CLARIFY_ROUNDS = ebay_pack().max_clarify_rounds


def challenge_for(revision: int, draft_hash: str) -> str:
    """The literal approval challenge: ``POST r{rev} {hash[-12:]}``."""
    template = str((_approval().get("exact") or {}).get("challenge"))
    return render_formula(template, {
        "revision": int(revision), "draft_hash": str(draft_hash),
    })


def publish_idempotency_key(app_id: str, session_id: str,
                            revision: int) -> str:
    template = str(
        ebay_pack().request_spec("publish")["fields"]["idempotency_key"]
    )
    return render_formula(template, {
        "app_id": app_id, "listing_session_id": session_id,
        "revision": int(revision),
    })


def draft_idempotency_key(app_id: str, session_id: str,
                          revision: int) -> str:
    template = str(ebay_pack().request_spec("draft")["idempotency_key"])
    return render_formula(template, {
        "app_id": app_id, "listing_session_id": session_id,
        "expected_revision": int(revision),
    })


def extract_contracts(value: Any) -> List[Dict[str, Any]]:
    """Every typed ``ebay.*`` contract in a workflow output (E2 walker)."""
    return _extract_contracts(value, ebay_pack().contract_prefix)


def find_contract(value: Any, schema: str) -> Optional[Dict[str, Any]]:
    return _find_contract(value, schema, ebay_pack().contract_prefix)


def evaluate_caps(config: dict, revision: Dict[str, Any]) -> CapsDecision:
    """The pack's caps clamp (E9): price ceiling + category allowlist +
    the on/off toggle; unreadable outcomes route to exact approval."""
    return _evaluate_caps(ebay_pack(), config, revision)


class PilotState(PackState):
    """Durable {session → runs/revisions/listing} linkage, atomic writes
    (the pack state store bound to the historical ``ebay_pilot.json``)."""

    def __init__(self, path: Optional[Path] = None):
        super().__init__(path, filename=ebay_pack().state_file)


# ---------------------------------------------------------------------------
# Request constructors (compat delegates over the pack request templates;
# exact-match fields come from the immutable revision itself)
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
    request = build_request(
        ebay_pack(), "draft",
        config=config,
        session={"id": listing_session_id, "thread_key": thread_id,
                 "media": media},
        intake_text=item_context,
    )
    request["expected_revision"] = int(expected_revision)
    if mode == "revise":
        if current_revision is None:
            raise CapitolError("revise mode requires the current revision")
        request["mode"] = "revise"
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
    revision (identity echoed verbatim, media digests carried, policies
    back-filled from config only when the revision omits them)."""
    return build_request(
        ebay_pack(), "revise",
        config=config,
        session={"item_context": item_context},
        intake_text=revision_feedback,
        contract=revision,
    )


def build_publish_request(
    revision: Dict[str, Any],
    *,
    actor_principal_id: str,
    presentation_id: str,
) -> Dict[str, Any]:
    """The exact action: every staleness-checked field is copied verbatim
    from the immutable revision; the challenge/key are derived from its
    revision number and hash by the contract formulas."""
    request = build_request(ebay_pack(), "publish", config={},
                            contract=revision)
    request["actor_principal_id"] = actor_principal_id
    request["presentation_id"] = presentation_id
    return request


def effect_listing(effect: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize an ``ebay.inventory_effect.v1`` contract's listing facts."""
    return extract_effect_facts(ebay_pack(), effect)


def resolve_workflows(runtime: CapitolRuntime, config: dict
                      ) -> Dict[str, str]:
    """Locate the draft + publish workflows on the agent's allowlist
    (config pins win; the pack's discover rules match by name)."""
    return _resolve_workflows(runtime, ebay_pack(), config)


def start_chat_draft(
    runtime: CapitolRuntime,
    draft_workflow_id: str,
    message: str,
    photo_paths: List[str],
    say,
) -> tuple:
    """Launch the initial draft over chat FileParts; return (run_id,
    reply) — the chat-intake reconciliation path (E6)."""
    return start_chat_intake(
        runtime, draft_workflow_id, message, photo_paths, say,
        alias="draft",
    )


# ---------------------------------------------------------------------------
# The pilot flow (shell surface of the pack engine)
# ---------------------------------------------------------------------------

class EbayPilot(PackShellFlow):
    """Drives one listing session; UI arrives as injected callables so the
    same flow serves the shell today and channel threads identically."""

    def __init__(
        self,
        runtime: CapitolRuntime,
        config: dict,
        *,
        state: Optional[PilotState] = None,
        say=print,
        ask=None,
        confirm=None,
    ):
        super().__init__(
            ebay_pack(), runtime, config,
            state=state or PilotState(),
            say=say, ask=ask, confirm=confirm,
        )

    def sell(self, photo_paths: List[str], notes: str = "") -> Dict[str, Any]:
        """Full Milestone-1 loop: photos → draft (+clarify) → policy gate →
        exact approval → sandbox publish → listing id."""
        return self.run_intake(photo_paths, notes)

    # Historical method names kept for callers/tests.
    def upload_photos(self, paths: List[str]) -> List[Dict[str, Any]]:
        return self.upload_attachments(paths)

    def present_revision(self, revision: Dict[str, Any]):
        self.present_contract(revision)


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
