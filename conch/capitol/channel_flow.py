"""Slack-first channel intake for the eBay pilot — a thin shim over the
generic flow-pack engine.

A Slack message carrying item photo(s) *is* the intake: it starts a
listing session in the same governed Capitol pipeline as ``/ebay``, and
the message's thread carries everything that follows — clarifying
questions and their answers, the drafted-revision review, the publish
approval, and confirmations. One thread == one listing session, bound
durably in the pack state file, so replies (and approvals) keep working
across a conch restart.

Since refactor stage R1 the whole flow — the resumable phase machine,
thread↔session dedupe, request construction, HITL parking, the caps
clamp, the origin-bound ``ebay_publish`` approval kind whose consume
*constructs* the exact ``ebay.publish_request.v1`` (never runs a
command), staleness re-checks on both sides, expiry re-issue, and the
gateway retry-key suffix — lives in
:class:`conch.capitol.packs.engine.PackChannelFlow`, driven by the
``ebay-listing`` flow pack. This module pins that pack and keeps the
historical class name; the golden fixtures in
``tests/test_capitol_golden.py`` prove wire/approval equivalence with
the pre-extraction flow.

Injection posture (engine-global): inbound text is item data, never
control. The only inbound strings with control semantics are the
origin-bound ``approve N`` / ``deny N`` replies, gated by the
fail-closed sender allowlist (channels), the approval store's exact
``(channel, thread_id, sender)`` binding, and the pinned revision
identity checked again at consume time.
"""

from __future__ import annotations

from typing import Optional

from .ebay import PilotState, ebay_pack
from .packs.engine import PackChannelFlow
from .packs.state import PackState

#: ApprovalStore entry kind whose consume constructs the publish request.
APPROVAL_KIND = ebay_pack().approval_kinds()[0]


class ChannelListingFlow(PackChannelFlow):
    """Message-first listing sessions over channel threads (the channel
    surface of the pack engine, bound to the ``ebay-listing`` pack).

    ``notify`` posts intermediate progress into the originating thread;
    each handler *returns* the final reply text, which the remote loop
    posts (and bounds) like any other channel reply.
    """

    def __init__(self, config: dict, approvals, notify, *,
                 state: Optional[PackState] = None):
        super().__init__(
            ebay_pack(), config or {}, approvals, notify,
            state=state or PilotState(),
        )
