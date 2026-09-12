"""Slack-first channel intake for the eBay pilot (Milestone 1b).

A Slack message carrying item photo(s) *is* the intake: it starts a
listing session in the same governed Capitol pipeline as ``/ebay``, and
the message's thread carries everything that follows — clarifying
questions and their answers, the drafted-revision review, the publish
approval, and confirmations. One thread == one listing session, bound
durably in the pilot state file, so replies (and approvals) keep working
across a conch restart.

The flow is a resumable state machine rather than a blocking
conversation: each inbound message advances exactly one step and every
step ends parked in durable state (``clarify`` — a terminal
``needs_info`` revision awaits answers; ``hitl`` — a run is parked
server-side on ``node.input_required``; ``awaiting_approval`` — a
drafted revision awaits the origin-bound publish decision). No thread
ever waits in memory for a human.

Injection posture: inbound text is item data, never control. It feeds
``item_context``/``revision_feedback`` and is displayed (control bytes
stripped) — it never selects a tool or a workflow. The only inbound
strings with control semantics are the origin-bound ``approve N`` /
``deny N`` replies, gated by the fail-closed sender allowlist
(channels), the approval store's exact ``(channel, thread_id, sender)``
binding, and the pinned revision identity checked again at consume time.

Approval semantics (the new ``ebay_publish`` approval kind): what the
store entry pins is the immutable revision identity (session, revision
number, ``draft_hash``); consuming it *constructs* the exact
``ebay.publish_request.v1`` from that revision — never runs a command.
Two independent staleness checks run in series: conch refuses when the
pinned identity no longer matches the session's current revision, and
Capitol's ``ebay_approval_node`` re-verifies the same identity plus the
challenge/confirmation/idempotency-key formulas before any provider
call. Any new revision drops the pending approval. Within-caps
auto-publish over a channel additionally requires the explicit
``ebay_channel_auto_publish=true`` opt-in (default: every channel
publish takes the approval path).
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Callable, Dict, List, Optional

from ..config import get_bool
from ..policy import evaluate_required_policy
from .client import FINAL_STATUS_EVENT, CapitolRuntime
from .ebay import (
    EFFECT_SCHEMA,
    MAX_CLARIFY_ROUNDS,
    MAX_PHOTOS,
    REJECTION_SCHEMA,
    REVISION_SCHEMA,
    PilotState,
    build_draft_request,
    build_publish_request,
    build_revise_request,
    challenge_for,
    clean_text,
    draft_idempotency_key,
    effect_listing,
    evaluate_caps,
    find_contract,
    resolve_workflows,
    start_chat_draft,
    workflow_inputs_key,
)
from .errors import CapitolAuthError, CapitolError

#: ApprovalStore entry kind whose consume constructs the publish request.
APPROVAL_KIND = "ebay_publish"

_RESTART_HINT = "Post the photos as a new message to start over."


class ChannelListingFlow:
    """Drives message-first listing sessions over channel threads.

    ``notify`` posts intermediate progress into the originating thread;
    each handler *returns* the final reply text, which the remote loop
    posts (and bounds) like any other channel reply.
    """

    def __init__(
        self,
        config: dict,
        approvals,
        notify: Callable[[str, str, str], Any],
        *,
        state: Optional[PilotState] = None,
    ):
        self.config = config or {}
        self.approvals = approvals
        self.notify = notify
        self.state = state or PilotState()
        self._input_keys: Dict[str, str] = {}

    # -- gating ---------------------------------------------------------------

    def enabled(self) -> bool:
        return bool(
            str(self.config.get("capitol_base_url") or "").strip()
            and get_bool(self.config, "ebay_channel_intake", True)
        )

    # -- runtime --------------------------------------------------------------

    def _runtime(self, session: Optional[Dict[str, Any]] = None) -> CapitolRuntime:
        """A fresh runtime per step: the bearer is resolved at call time
        (never stored) and the session's A2A context is re-threaded so
        audit attribution stays continuous across restarts."""
        runtime = CapitolRuntime.from_config(self.config)
        runtime.discover()
        context = str((session or {}).get("context_id") or "")
        if context:
            runtime.context_id = context
        return runtime

    # -- entry points ----------------------------------------------------------

    def handle_message(self, message) -> Optional[str]:
        """One inbound (already allowlisted) message → one flow step.

        Returns the in-thread reply, or None when the message is not for
        this flow (it then falls through to the normal remote turn).
        """
        key = f"{message.channel}:{message.thread_id}"
        session_id = self.state.thread_session(key)
        session = self.state.session(session_id) if session_id else None
        if session_id and session is None:
            session_id = None  # dangling binding: recover via a fresh start
        images = [
            attachment
            for attachment in getattr(message, "attachments", None) or []
            if str(attachment.mime_type).startswith("image/")
        ]
        if not session_id:
            if not images:
                return None
            if message.channel != "slack" or not self.enabled():
                return None
        try:
            if session_id:
                return self._steer(key, session_id, session, message, images)
            return self._start_session(key, message, images)
        except CapitolAuthError as exc:
            if session_id:
                self._recover_phase(session_id)
            return (
                f"Capitol credential needed: {clean_text(exc, 400)} — "
                "fix the bearer, then reply here (or repost the photos)."
            )
        except CapitolError as exc:
            hint = clean_text(getattr(exc, "hint", ""), 200)
            recovered = self._recover_phase(session_id) if session_id else ""
            if not session_id or recovered == "failed":
                tail = f" {_RESTART_HINT}"
            else:
                tail = " The previous draft still stands; reply again to continue."
            return (
                f"eBay listing step failed: {clean_text(exc, 500)}"
                + (f" ({hint})" if hint else "")
                + tail
            )

    def handle_approval(self, request_id: int, entry: Dict[str, Any],
                        verb: str, message) -> str:
        """Consume an ``ebay_publish`` approval (already origin-checked and
        popped by the store): deny drops it; approve constructs the exact
        publish request from the pinned revision — after re-checking that
        the pin still matches the session's current revision."""
        payload = entry.get("payload") or {}
        session_id = str(payload.get("session_id") or "")
        key = str(payload.get("thread_key")
                  or f"{message.channel}:{message.thread_id}")
        session = self.state.session(session_id)
        if verb == "deny":
            if session is not None:
                self.state.update_session(session_id, approval_id=None)
            return (
                f"Denied #{request_id} — nothing will be published. "
                "Reply with feedback to revise the draft, or ignore this "
                "thread."
            )
        if session is None:
            return (
                f"Approval #{request_id} refers to a listing session that "
                "no longer exists; nothing was published."
            )
        revision = session.get("revision") or {}
        pinned_revision = int(payload.get("revision") or -1)
        pinned_hash = str(payload.get("draft_hash") or "")
        current_revision = int(revision.get("revision") or -2)
        current_hash = str(revision.get("draft_hash") or "")
        stale = (
            pinned_revision != current_revision
            or not pinned_hash
            or pinned_hash != current_hash
            or int(session.get("approval_id") or 0) != int(request_id)
        )
        if stale:
            self.state.update_session(session_id, approval_id=None)
            fresh = ""
            if revision.get("status") == "draft_review":
                fresh = "\n" + self._request_approval(
                    key, session_id, revision,
                    ["the draft changed after that approval was issued"],
                )
            return (
                f"Approval #{request_id} is stale — the draft it approved "
                "is no longer current. Nothing was published." + fresh
            )
        self.state.update_session(session_id, approval_id=None)
        try:
            caps = evaluate_caps(self.config, revision)
            return self._publish(
                key, session_id, revision,
                caps_auto=caps.auto,
                approval_context={
                    "id": int(request_id),
                    "channel": str(message.channel),
                    "thread_id": str(message.thread_id),
                    "sender": str(message.sender),
                },
            )
        except CapitolAuthError as exc:
            self._recover_phase(session_id)
            return (
                f"Capitol credential needed: {clean_text(exc, 400)} — "
                "nothing was published."
            )
        except CapitolError as exc:
            self._recover_phase(session_id)
            return (
                f"Publish step failed before submission: "
                f"{clean_text(exc, 500)} — nothing was published."
            )

    def reissue_expired(self, message) -> Optional[str]:
        """An expired ``approve N`` landed in a thread whose session still
        awaits approval: mint a fresh origin-bound approval for the
        (unchanged) current revision."""
        key = f"{message.channel}:{message.thread_id}"
        session_id = self.state.thread_session(key)
        if not session_id:
            return None
        session = self.state.session(session_id)
        if not session or session.get("phase") != "awaiting_approval":
            return None
        revision = session.get("revision")
        if not isinstance(revision, dict) or not revision.get("draft_hash"):
            return None
        fresh = self._request_approval(
            key, session_id, revision, ["the previous approval expired"]
        )
        return "That approval expired; here is a fresh one.\n" + fresh

    # -- session start ----------------------------------------------------------

    def _start_session(self, key: str, message, images: List[Any]) -> str:
        photos = images[:MAX_PHOTOS]
        session_id = "slack-" + hashlib.sha256(
            f"{key}:{message.ts}".encode("utf-8")
        ).hexdigest()[:16]
        if self.state.session(session_id) is not None:
            # Same message delivered twice (crash between binding and
            # session write, or a cursor replay): never start a second
            # draft for the same message ts.
            self.state.bind_thread(key, session_id)
            return (
                "This message already started listing session "
                f"{session_id}; reply in this thread to continue it."
            )
        self.state.bind_thread(key, session_id)
        notes = clean_text(message.text, 4000).strip()
        item_context = notes or (
            "Image-only listing request; no user-supplied listing metadata."
        )
        self.state.update_session(
            session_id,
            phase="drafting",
            channel=str(message.channel),
            thread_id=str(message.thread_id),
            sender=str(message.sender),
            origin_ts=str(message.ts),
            item_context=item_context,
            photos=[a.path for a in photos],
        )
        runtime = self._runtime()
        workflows = resolve_workflows(runtime, self.config)
        intake = str(self.config.get("ebay_intake") or "chat").strip().lower()
        runtime.handshake()  # fresh A2A context per listing session
        self.state.update_session(
            session_id,
            workflows=workflows,
            context_id=runtime.context_id,
            intake=intake,
        )
        self.notify(
            f"Starting an eBay sandbox listing from your {len(photos)} "
            f"photo(s) (session {session_id}) — drafting…",
            message.channel, message.thread_id,
        )
        outcome, value = self._initial_draft(
            runtime, session_id, key, workflows,
            [a.path for a in photos], item_context, intake,
        )
        if outcome == "parked":
            return value
        revision = self._extract_revision(session_id, value)
        return self._after_revision(key, session_id, revision)

    def _initial_draft(
        self,
        runtime: CapitolRuntime,
        session_id: str,
        key: str,
        workflows: Dict[str, str],
        photo_paths: List[str],
        item_context: str,
        intake: str,
    ) -> tuple:
        if intake == "typed":
            app_id = str(self.config.get("ebay_app_id") or "conch-ebay")
            media = []
            for index, path in enumerate(photo_paths):
                uploaded = runtime.upload_artifact(path)
                uploaded["order"] = index
                media.append(uploaded)
            self.state.update_session(session_id, media=[
                {k: item[k]
                 for k in ("artifact_id", "digest", "order", "filename")}
                for item in media
            ])
            request = build_draft_request(
                self.config,
                listing_session_id=session_id,
                thread_id=key,
                media=media,
                item_context=item_context,
            )
            return self._run_typed(
                runtime, session_id, "draft", workflows["draft"], request,
                draft_idempotency_key(app_id, session_id, 1),
            )
        chat_message = (
            "Please draft a sandbox eBay listing from the attached "
            f"photo(s). Item context: {item_context}"
        )
        run_id, _reply = start_chat_draft(
            runtime, workflows["draft"], chat_message, photo_paths,
            say=lambda _text: None,
        )
        self.state.record_run(session_id, "draft", run_id, "")
        return self._supervise(runtime, session_id, "draft", run_id)

    # -- steering (replies in a bound thread) -------------------------------------

    def _steer(self, key: str, session_id: str, session: Dict[str, Any],
               message, images: List[Any]) -> Optional[str]:
        phase = str(session.get("phase") or "")
        note = ""
        if images and phase not in ("done", "failed"):
            note = ("This listing session already has its photos; the new "
                    "ones are ignored for now.\n")
        if phase in ("clarify", "awaiting_approval"):
            return note + self._revise(key, session_id, session, message)
        if phase == "hitl":
            return note + self._resume_hitl(key, session_id, session, message)
        if phase == "done":
            listing = session.get("listing") or {}
            url = clean_text(listing.get("listing_url"), 200)
            return (
                "This listing session is complete"
                + (f": {url}" if url else ".")
                + " Post new photos as a new message to sell another item."
            )
        if phase == "failed":
            return f"This listing session failed earlier. {_RESTART_HINT}"
        # drafting/publishing: only reachable when a step was interrupted
        # mid-run (crash) — the in-memory watch is gone.
        return (
            "This listing session was interrupted mid-step and cannot be "
            f"resumed. {_RESTART_HINT}"
        )

    def _revise(self, key: str, session_id: str,
                session: Dict[str, Any], message) -> str:
        revision = session.get("revision")
        if not isinstance(revision, dict) or not revision:
            self.state.update_session(session_id, phase="failed")
            return f"This session has no revision to revise. {_RESTART_HINT}"
        clarifying = str(session.get("phase")) == "clarify"
        rounds = int(session.get("clarify_rounds") or 0)
        if clarifying and rounds >= MAX_CLARIFY_ROUNDS:
            self.state.update_session(session_id, phase="failed")
            return (
                f"The draft still needs information after "
                f"{MAX_CLARIFY_ROUNDS} clarification rounds; stopping. "
                + _RESTART_HINT
            )
        # A new revision invalidates any pending approval.
        previous_approval = session.get("approval_id")
        if previous_approval:
            self.approvals.pop(int(previous_approval))
            self.state.update_session(session_id, approval_id=None)
        feedback = clean_text(message.text, 4000).strip() or (
            "No further information available; proceed with conservative "
            "assumptions."
        )
        request = build_revise_request(
            self.config, revision,
            revision_feedback=feedback,
            item_context=str(session.get("item_context") or ""),
        )
        runtime = self._runtime(session)
        workflows = dict(session.get("workflows") or {}) or resolve_workflows(
            runtime, self.config
        )
        self.state.update_session(
            session_id,
            phase="drafting",
            clarify_rounds=rounds + 1 if clarifying else rounds,
        )
        self.notify("Revising the draft…", message.channel, message.thread_id)
        outcome, value = self._run_typed(
            runtime, session_id, "draft", workflows["draft"], request,
            draft_idempotency_key(
                str(request["app_id"]),
                str(request["listing_session_id"]),
                int(request["expected_revision"]),
            ),
        )
        if outcome == "parked":
            return value
        new_revision = self._extract_revision(session_id, value)
        return self._after_revision(key, session_id, new_revision)

    def _resume_hitl(self, key: str, session_id: str,
                     session: Dict[str, Any], message) -> str:
        hitl = dict(session.get("hitl") or {})
        run_id = str(hitl.get("run_id") or "")
        if not run_id:
            self.state.update_session(session_id, phase="failed", hitl=None)
            return f"The parked checkpoint was lost. {_RESTART_HINT}"
        answer = clean_text(message.text, 2000).strip()
        runtime = self._runtime(session)
        if str(hitl.get("input_kind") or "") == "clarification":
            runtime.submit_clarification(
                run_id, str(hitl.get("request_id") or ""), answer,
                declined=not answer,
            )
        else:
            token = answer.lower()
            if token not in ("continue", "stop"):
                return ("This run is waiting on a checkpoint: reply exactly "
                        "'continue' or 'stop'.")
            runtime.submit_intervention(
                run_id, str(hitl.get("node_id") or ""),
                str(hitl.get("request_id") or ""), token,
            )
        kind = str(hitl.get("kind") or "draft")
        self.state.update_session(session_id, phase="drafting", hitl=None)
        outcome, value = self._supervise(
            runtime, session_id, kind, run_id,
            since=int(hitl.get("last_sequence") or 0) + 1,
        )
        if outcome == "parked":
            return value
        if kind == "publish":
            return self._finish_publish(session_id, value)
        revision = self._extract_revision(session_id, value)
        return self._after_revision(key, session_id, revision)

    # -- run supervision -----------------------------------------------------------

    def _run_typed(self, runtime: CapitolRuntime, session_id: str, kind: str,
                   workflow_id: str, request: Dict[str, Any],
                   idempotency_key: str) -> tuple:
        inputs = {
            workflow_inputs_key(runtime, workflow_id, self._input_keys):
            request
        }
        submission = runtime.call_workflow(
            workflow_id, inputs, idempotency_key=idempotency_key
        )
        run_id = str(submission["run_id"])
        self.state.record_run(session_id, kind, run_id, idempotency_key)
        return self._supervise(runtime, session_id, kind, run_id)

    def _supervise(self, runtime: CapitolRuntime, session_id: str, kind: str,
                   run_id: str, since: int = 0) -> tuple:
        """Watch a run to terminal, or park on ``node.input_required``.

        Returns ``("output", output)`` or ``("parked", reply_text)``. The
        parked run waits durably server-side; the persisted sequence
        cursor lets the resume watch continue without replay or loss.
        """
        final_state = ""
        last_sequence = max(0, int(since) - 1)
        for event in runtime.watch_run(run_id, since_sequence=since):
            event_type = str(event.get("event_type") or "")
            sequence = event.get("sequence")
            if isinstance(sequence, (int, float)):
                last_sequence = int(sequence)
                self.state.update_run(
                    session_id, run_id, last_sequence=last_sequence
                )
            if event_type == "node.input_required":
                return "parked", self._park_hitl(
                    session_id, kind, run_id, event, last_sequence
                )
            if event_type == FINAL_STATUS_EVENT:
                final_state = str(
                    (event.get("data") or {}).get("state") or ""
                )
        status = runtime.run_status(run_id)
        run_state = str((status or {}).get("status") or final_state or "")
        self.state.update_run(session_id, run_id, status=run_state)
        if run_state.lower() != "success":
            error = clean_text((status or {}).get("error_message"), 500)
            raise CapitolError(
                f"{kind} run {run_id} ended {run_state or 'unknown'}"
                f"{': ' + error if error else ''}"
            )
        return "output", runtime.workflow_output(run_id) or {}

    def _park_hitl(self, session_id: str, kind: str, run_id: str,
                   event: Dict[str, Any], last_sequence: int) -> str:
        data = event.get("data") or {}
        node = event.get("node") or {}
        prompt = clean_text(
            data.get("prompt")
            or (data.get("extra") or {}).get("prompt")
            or "The workflow needs input to continue.",
            2000,
        )
        input_kind = str(data.get("input_kind") or "")
        self.state.update_session(session_id, phase="hitl", hitl={
            "run_id": run_id,
            "kind": kind,
            "request_id": str(
                data.get("request_id")
                or (data.get("extra") or {}).get("request_id")
                or ""
            ),
            "node_id": str(node.get("node_id") or ""),
            "input_kind": input_kind,
            "last_sequence": int(last_sequence),
        })
        if input_kind == "clarification":
            return (f"The workflow asks: {prompt}\n"
                    "Reply in this thread to answer.")
        return (f"Workflow checkpoint: {prompt}\n"
                "Reply exactly 'continue' to proceed or 'stop' to halt.")

    # -- outcomes -------------------------------------------------------------------

    def _extract_revision(self, session_id: str,
                          output: Dict[str, Any]) -> Dict[str, Any]:
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
        # The full immutable revision is what later steps revise from and
        # what an approval consume constructs the publish request from.
        self.state.update_session(session_id, revision=revision)
        return revision

    def _after_revision(self, key: str, session_id: str,
                        revision: Dict[str, Any]) -> str:
        status = str(revision.get("status") or "")
        if status == "needs_info":
            self.state.update_session(session_id, phase="clarify")
            questions = [
                clean_text(question, 500)
                for question in revision.get("open_questions") or []
            ] or ["(no specific question provided)"]
            lines = ["The draft needs more information:"]
            lines.extend(f"  • {question}" for question in questions)
            lines.append("Reply in this thread with the answers.")
            return "\n".join(lines)
        if status != "draft_review":
            self.state.update_session(session_id, phase="failed")
            return (f"Unexpected revision status {status!r}; stopping. "
                    + _RESTART_HINT)
        summary = self._present(revision)
        decision = evaluate_caps(self.config, revision)
        if decision.auto and get_bool(
            self.config, "ebay_channel_auto_publish", False
        ):
            challenge = challenge_for(
                int(revision["revision"]), str(revision["draft_hash"])
            )
            session = self.state.session(session_id) or {}
            self.notify(
                summary + "\nWithin caps — publishing to the eBay sandbox "
                f"(challenge {challenge})…",
                str(session.get("channel") or "slack"),
                str(session.get("thread_id") or ""),
            )
            return self._publish(key, session_id, revision, caps_auto=True)
        reasons = list(decision.reasons) or [
            "channel auto-publish is off (set ebay_channel_auto_publish="
            "true to publish within caps automatically)"
        ]
        return summary + "\n" + self._request_approval(
            key, session_id, revision, reasons
        )

    def _present(self, revision: Dict[str, Any]) -> str:
        listing = revision.get("listing") or {}
        price = listing.get("price") or {}
        lines = [
            f"Draft r{revision.get('revision')} — "
            f"{clean_text(listing.get('title'), 120)}",
            f"  Price {clean_text(price.get('value'), 20)} "
            f"{clean_text(price.get('currency'), 8)}"
            f" · Category {clean_text(listing.get('category_id'), 40)}"
            f" · Condition {clean_text(listing.get('condition'), 60)}"
            f" · Qty {clean_text(listing.get('quantity'), 12)}",
            f"  Photos {len(revision.get('media') or [])}"
            f" · Hash …{str(revision.get('draft_hash'))[-12:]}",
        ]
        description = clean_text(listing.get("description"), 300)
        if description:
            lines.append(f"  About: {description}")
        for warning in revision.get("warnings") or []:
            lines.append(f"  ⚠ {clean_text(warning, 200)}")
        return "\n".join(lines)

    # -- approvals --------------------------------------------------------------------

    def _request_approval(self, key: str, session_id: str,
                          revision: Dict[str, Any],
                          reasons: List[str]) -> str:
        session = self.state.session(session_id) or {}
        previous = session.get("approval_id")
        if previous:
            self.approvals.pop(int(previous))
        revision_number = int(revision["revision"])
        draft_hash = str(revision["draft_hash"])
        title = clean_text(
            (revision.get("listing") or {}).get("title"), 120
        )
        request_id = self.approvals.add(
            f"publish eBay listing r{revision_number} "
            f"({title or 'untitled'})",
            str(session.get("channel") or "slack"),
            str(session.get("thread_id") or ""),
            str(session.get("sender") or ""),
            kind=APPROVAL_KIND,
            payload={
                "session_id": session_id,
                "thread_key": key,
                "revision": revision_number,
                "draft_hash": draft_hash,
            },
        )
        self.state.update_session(
            session_id, phase="awaiting_approval", approval_id=request_id
        )
        lines = [f"Publish approval needed [#{request_id}]:"]
        lines.extend(
            f"  • {clean_text(reason, 200)}" for reason in reasons
        )
        lines.append(
            f"Challenge: {challenge_for(revision_number, draft_hash)}"
        )
        lines.append(
            f"Reply 'approve {request_id}' to publish to the eBay sandbox, "
            f"'deny {request_id}' to stop, or reply with feedback to revise "
            "the draft (which voids this approval)."
        )
        return "\n".join(lines)

    # -- publish -----------------------------------------------------------------------

    def _publish(self, key: str, session_id: str, revision: Dict[str, Any],
                 *, caps_auto: bool,
                 approval_context: Optional[Dict[str, Any]] = None) -> str:
        session = self.state.session(session_id) or {}
        runtime = self._runtime(session)
        workflows = dict(session.get("workflows") or {}) or resolve_workflows(
            runtime, self.config
        )
        request = build_publish_request(
            revision,
            actor_principal_id=str(
                revision.get("actor_principal_id")
                or self.config.get("ebay_actor_id")
                or ""
            ),
            presentation_id=f"conch-channel-{uuid.uuid4().hex[:10]}",
        )
        policy_payload = {
            "workflow_id": workflows["publish"],
            "app_id": request["app_id"],
            "listing_session_id": request["listing_session_id"],
            "revision": request["revision"],
            "draft_hash": request["draft_hash"],
            "idempotency_key": request["idempotency_key"],
            "caps_auto": bool(caps_auto),
        }
        if approval_context:
            policy_payload["channel_approval"] = approval_context
        decision = evaluate_required_policy(
            "capitol.ebay.publish", policy_payload
        )
        if not decision.allowed:
            self.state.update_session(session_id, phase="failed")
            return (
                "Publish denied by required policy: "
                f"{decision.reason or decision.check or 'no reason given'} "
                "— nothing was published."
            )
        self.state.update_session(session_id, phase="publishing")
        # The request's embedded idempotency_key is the exact contract
        # formula and never varies. The *gateway* call key gets a retry
        # suffix on re-approval after a failed attempt — otherwise the
        # gateway would keep replaying the failed run id. Effectively-once
        # is still guaranteed by Capitol's effect ledger on the embedded
        # key: a duplicate attempt replays the durable receipt.
        attempts = int(
            (self.state.session(session_id) or {}).get("publish_attempts")
            or 0
        ) + 1
        self.state.update_session(session_id, publish_attempts=attempts)
        gateway_key = request["idempotency_key"] + (
            "" if attempts == 1 else f":retry{attempts}"
        )
        try:
            outcome, value = self._run_typed(
                runtime, session_id, "publish", workflows["publish"],
                request, gateway_key,
            )
        except CapitolError as exc:
            # The effect failed upstream (e.g. the expired sandbox user
            # token). The revision is still valid and the publish key is
            # revision-scoped, so re-approving retries safely — replay
            # can never double-post.
            hint = clean_text(getattr(exc, "hint", ""), 200)
            rearm = self._request_approval(
                key, session_id, revision,
                ["the previous publish attempt failed upstream"],
            )
            return (
                f"Publish failed: {clean_text(exc, 500)}"
                + (f" ({hint})" if hint else "")
                + "\nNothing was published. Fix the upstream issue, then "
                "re-approve to retry.\n" + rearm
            )
        if outcome == "parked":
            return value
        return self._finish_publish(session_id, value)

    def _finish_publish(self, session_id: str, output: Dict[str, Any]) -> str:
        effect = find_contract(output, EFFECT_SCHEMA)
        rejection = find_contract(output, REJECTION_SCHEMA)
        if effect is None and rejection is not None:
            detail = clean_text(
                (rejection.get("error_detail") or {}).get("message")
                or rejection.get("message"), 300,
            )
            self.state.update_session(session_id, phase="failed")
            return (
                f"Publish was rejected by Capitol's approval gate: {detail} "
                "— zero writes were made. " + _RESTART_HINT
            )
        if effect is None:
            self.state.update_session(session_id, phase="failed")
            return ("Publish run produced no ebay.inventory_effect.v1 "
                    "contract; treating this session as failed. "
                    + _RESTART_HINT)
        listing = effect_listing(effect)
        self.state.update_session(
            session_id, listing=listing, phase="done"
        )
        state_text = clean_text(listing.get("state"), 40) or "?"
        reply = f"Publish effect: {state_text}"
        if listing.get("listing_id"):
            reply += f" — listing {clean_text(listing.get('listing_id'), 40)}"
        if listing.get("listing_url"):
            reply += f"\n{clean_text(listing.get('listing_url'), 200)}"
        return reply

    # -- recovery ---------------------------------------------------------------------

    def _recover_phase(self, session_id: str) -> str:
        """After a failed step, park the session in the most honest durable
        phase: a parked run keeps priority, then the last good revision,
        else failed. Returns the phase chosen."""
        session = self.state.session(session_id) or {}
        if session.get("hitl"):
            phase = "hitl"
        else:
            revision = session.get("revision")
            if isinstance(revision, dict) and revision.get("draft_hash"):
                phase = (
                    "clarify"
                    if revision.get("status") == "needs_info"
                    else "awaiting_approval"
                )
            else:
                phase = "failed"
        self.state.update_session(session_id, phase=phase)
        return phase
