"""Flow-pack binding for the generic folder-watch pattern.

The kernel's :mod:`conch.kernel.folderwatch` owns the mechanics of a
watched folder (polling, debounce, validation, quarantine, dedupe);
this module supplies the ``pack:<name>`` handler binding: a grouped
drop becomes a governed flow-pack session over the pack engine's
channel surface — for the ebay-listing pack, that's the same drafting →
clarify → revision review → exact-approval publish flow the Slack
intake drives.

Safety invariant, enforced structurally: **a file drop is never consent
for an effect**. The folder surface forces the pack's channel
auto-effect opt-in off, so publishing (or any other pack effect) always
requires the origin-bound approval challenge, regardless of caps.

The pack declares its folder surface with a ``watched_folder`` intake;
the folder's *path* lives in the operator's ``folder_watch_<name>``
config (a pack never chooses where on disk it reads from).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from ..channels import InboundMessage
from ..kernel.folderwatch import drops_overview
from .errors import CapitolError
from .packs import FlowPack, load_pack
from .packs.engine import PackChannelFlow, clean_text
from .packs.state import PackState

__all__ = [
    "FOLDER_CHANNEL",
    "FOLDER_SENDER",
    "PackFolderFlow",
    "PackFolderHandler",
    "drops_summary",
    "ebay_flow_config",
]

#: Synthetic channel for pack folder surfaces. Approvals created by
#: folder sessions are origin-bound to this channel, so an ``approve``
#: from a Slack thread (or any other origin) can never consume them.
FOLDER_CHANNEL = "folder"
FOLDER_SENDER = "folder:local"


def ebay_flow_config(config: dict) -> dict:
    """The config the eBay flows run under: ``ebay_agent`` (when set)
    overrides ``capitol_agent`` so listing sessions target the eBay
    orchestrator while the rest of conch keeps its default agent.
    Returns a shallow copy; the caller's config is never mutated."""
    flow_config = dict(config or {})
    ebay_agent = str(flow_config.get("ebay_agent") or "").strip()
    if ebay_agent:
        flow_config["capitol_agent"] = ebay_agent
    return flow_config


def _pack_flow_config(pack: FlowPack, config: dict) -> dict:
    flow_config = dict(config or {})
    if pack.name == "ebay-listing":
        flow_config = ebay_flow_config(flow_config)
    # A drop is never consent for an effect: force every channel
    # auto-effect opt-in off for the folder surface. (Key names are
    # pack-specific; the ebay pack's is ebay_channel_auto_publish.)
    for key in list(flow_config):
        if key.endswith("_channel_auto_publish"):
            flow_config[key] = "false"
    flow_config.setdefault("ebay_channel_auto_publish", "false")
    return flow_config


class PackFolderFlow(PackChannelFlow):
    """A pack's channel surface bound to the folder transport.

    Prefers the pack's ``watched_folder`` intake for its spec (start
    binding, context default) and falls back to the ``channel_message``
    intake's attachment discipline, so a pack that declares only the
    channel surface still folder-drives identically.
    """

    def __init__(self, pack: FlowPack, config: dict, approvals,
                 notify: Callable[[str, str, str], Any], *,
                 state: Optional[PackState] = None):
        super().__init__(
            pack, _pack_flow_config(pack, config), approvals, notify,
            state=state,
        )
        folder_intake = self._intake_spec("watched_folder")
        if folder_intake:
            merged = dict(self.intake)
            merged.update(folder_intake)
            self.intake = merged
            self.initial_binding = self.pack.binding(
                str(self.intake.get("start"))
            )
            self.initial_alias = str(self.initial_binding["workflow"])
        self.channel = FOLDER_CHANNEL

    def enabled(self) -> bool:
        intake_enabled = self._intake_spec("watched_folder").get(
            "enabled", True
        )
        return bool(intake_enabled)

    def _context_default(self) -> str:
        own = str(self.intake.get("context_default") or "")
        return own or super()._context_default()


class PackFolderHandler:
    """``pack:<name>`` folder-watch handler (plugin-registered)."""

    def __init__(self, pack_name: str, store, config: dict, log=print):
        self.pack = load_pack(pack_name)
        self.store = store
        self.config = config or {}
        self.log = log
        self._flow: Optional[PackFolderFlow] = None

    def _get_flow(self) -> PackFolderFlow:
        if self._flow is None:
            from ..remote import ApprovalStore

            self._flow = PackFolderFlow(
                self.pack, self.config, ApprovalStore(),
                lambda text, channel, thread: None,
            )
        return self._flow

    def accepts(self) -> Dict[str, Any]:
        flow = self._get_flow()
        spec = flow._attachments_spec() or {}
        suffixes = tuple(
            str(s).lower() for s in spec.get("suffixes") or ()
        )
        return {
            "extensions": suffixes or (
                ".jpg", ".jpeg", ".png", ".gif", ".webp"
            ),
            # The Slack intake's inbound caps, not the pack's upload
            # ceiling: quarantined local files follow channel discipline.
            "max_bytes": 12 * 1024 * 1024,
            "max_count": int(spec.get("max_count") or 12),
            "notes_sidecar": True,
            "magic": True,
        }

    # -- drops -----------------------------------------------------------------

    def handle_drop(self, watch: str, drop_id: str,
                    attachments: List[Any], notes: str,
                    notify: Callable[[str], None]) -> Optional[str]:
        flow = self._get_flow()
        if not flow.enabled():
            return (
                f"pack {self.pack.name} declares its folder intake "
                "disabled; drop archived, no session started"
            )
        message = InboundMessage(
            channel=FOLDER_CHANNEL, sender=FOLDER_SENDER,
            text=notes, thread_id=drop_id, ts=drop_id,
            attachments=list(attachments),
        )
        try:
            return flow.handle_message(message)
        except CapitolError as exc:
            return f"listing flow failed: {clean_text(exc, 400)}"

    # -- continuation verbs ------------------------------------------------------

    def handle_verb(self, payload: Dict[str, Any],
                    notify: Callable[[str], None]) -> Optional[str]:
        verb = str(payload.get("verb") or "")
        flow = self._get_flow()
        if verb == "answer":
            drop_id = str(payload.get("drop") or "")
            text = clean_text(str(payload.get("text") or ""), 2000)
            if not (drop_id and text):
                return "usage: answer needs a drop id and text"
            message = InboundMessage(
                channel=FOLDER_CHANNEL, sender=FOLDER_SENDER,
                text=text, thread_id=drop_id,
                ts=f"answer-{time.time()}",
            )
            try:
                return flow.handle_message(message)
            except CapitolError as exc:
                return f"answer failed: {clean_text(exc, 400)}"
        if verb in ("approve", "deny"):
            request_id = int(payload.get("request_id") or 0)
            pending = flow.approvals.pending().get(str(request_id))
            if not pending:
                return (f"approval #{request_id} is not pending "
                        "(expired, consumed, or unknown)")
            if str(pending.get("channel") or "") != FOLDER_CHANNEL:
                # Never consume another origin's approval: a Slack
                # thread's approval stays Slack's.
                return (f"approval #{request_id} belongs to another "
                        "surface and was left untouched")
            entry, error = flow.approvals.consume(
                request_id,
                channel=FOLDER_CHANNEL,
                thread_id=str(pending.get("thread_id") or ""),
                sender=str(pending.get("sender") or FOLDER_SENDER),
            )
            if entry is None:
                return (f"approval #{request_id} could not be "
                        f"consumed ({error})")
            drop_id = str(pending.get("thread_id") or "")
            message = InboundMessage(
                channel=FOLDER_CHANNEL, sender=FOLDER_SENDER,
                text=verb, thread_id=drop_id,
                ts=f"{verb}-{time.time()}",
            )
            try:
                return flow.handle_approval(
                    request_id, entry, verb, message
                )
            except CapitolError as exc:
                return f"{verb} failed: {clean_text(exc, 400)}"
        return f"unknown folder verb {verb!r}"


def pack_folder_handler_factory(target: str, store, config: dict,
                                log=print) -> PackFolderHandler:
    if not str(target or "").strip():
        raise ValueError("pack binding needs pack:<pack-name>")
    return PackFolderHandler(str(target).strip(), store, config, log=log)


def drops_summary() -> List[Dict[str, Any]]:
    """Drop records joined with their pack sessions — /ebay drops and
    the status exporter. Read-only; safe alongside the daemon."""
    pack_state = PackState(
        filename=load_pack("ebay-listing").state_file
    )
    sessions = pack_state.load().get("sessions", {})
    by_thread: Dict[str, Dict[str, Any]] = {}
    for session_id, session in sessions.items():
        thread = str(session.get("thread_id") or "")
        if thread:
            by_thread[thread] = dict(session, session_id=session_id)
    out: List[Dict[str, Any]] = []
    for record in drops_overview():
        drop_id = str(record.get("drop_id") or "")
        session = by_thread.get(drop_id) or {}
        out.append({
            "drop_id": drop_id,
            "watch": record.get("watch"),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "photos": record.get("files") or [],
            "notes": bool(record.get("notes")),
            "phase": str(session.get("phase") or "received"),
            "session_id": str(session.get("session_id") or ""),
            "approval_id": session.get("approval_id"),
            "history": record.get("history") or [],
        })
    return out
