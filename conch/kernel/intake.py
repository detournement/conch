"""Daemon-hosted remote channel intake (always-on daemon work item).

The edge daemon hosts the remote channel loop so inbound Matrix/Slack/
SMS/email messages get full agent turns 24/7 with no interactive shell
attached.
Every remote-safety invariant is :class:`conch.remote.RemoteLoop`'s own —
fail-closed sender allowlists, the safe_auto permission cap, origin-bound
expiring approvals, ``REMOTE_EXCLUDED_TOOLS``, reply-length caps, and
thread == conversation mapping. This module adds hosting and coordination,
never policy.

Single-consumer coordination (takeover/handoff):

- Exactly one process may poll the channels. Every would-be host must hold
  the kernel ``channel_intake`` lease for each polling pass; channel
  cursors advance only inside a held lease, so a host change never drops
  nor double-answers a message — unclaimed messages simply wait on the
  transport for the next holder.
- Which process *tries* is config: ``remote_host = daemon`` (the default —
  the daemon hosts intake whenever ``edge_daemon`` is enabled) or
  ``remote_host = shell`` (the interactive shell keeps its classic loop
  and the daemon abstains). The lease enforces the invariant even when two
  processes disagree about that config.
- The daemon renews the lease every pass and releases it on shutdown; a
  crashed holder's lease expires after :func:`intake_lease_seconds`. A
  superseded (zombie) daemon is fenced by the controller epoch: its lease
  renewal raises, so it stops polling the moment a successor adopts the
  kernel.

Inference for channel turns uses the same provider config/env-override
behavior the daemon's mission sessions already use: each inbound turn
builds a fresh, fully wired host session through
:func:`conch.bootstrap.build_agent_session` with the daemon's config.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from .model import KernelError

INTAKE_LEASE_KIND = "channel_intake"
INTAKE_LEASE_RESOURCE = "channels"

#: Lease TTL floor. The effective TTL is three poll intervals when that is
#: larger, so a crashed holder is superseded after a few missed passes.
INTAKE_LEASE_MIN_SECONDS = 90.0


def intake_lease_seconds(poll_interval: float) -> float:
    """Lease TTL for a host polling every *poll_interval* seconds."""
    try:
        interval = float(poll_interval)
    except (TypeError, ValueError):
        interval = 60.0
    return max(3.0 * interval, INTAKE_LEASE_MIN_SECONDS)


class ChannelIntake:
    """Owns the daemon's channel-intake pass: cadence, lease, delegation.

    ``loop_factory`` exists for tests: it returns the RemoteLoop-shaped
    consumer to host. The default builds a real RemoteLoop wired to this
    kernel (fresh host session per turn, mission-input bridge into the
    engine).
    """

    def __init__(self, store, engine, config: dict, holder: str, *,
                 log: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.time,
                 loop_factory: Optional[Callable] = None):
        self.store = store
        self.engine = engine
        self.config = config or {}
        self.holder = holder
        self.clock = clock
        self._log = log or (lambda line: None)
        self._loop_factory = loop_factory
        self._loop = None
        self._last_poll = 0.0
        self._denied_by = ""
        self._unconfigured_logged = False

    # -- configuration ---------------------------------------------------------

    def enabled(self) -> bool:
        from ..config import get_bool

        if not get_bool(self.config, "remote_enabled"):
            return False
        host = str(self.config.get("remote_host") or "daemon").strip().lower()
        return host != "shell"

    def poll_interval(self) -> float:
        try:
            interval = float(self.config.get("remote_poll_interval", 60) or 60)
        except (TypeError, ValueError):
            interval = 60.0
        return max(interval, 1.0)

    # -- the intake pass ---------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> int:
        """One intake pass when due; returns inbound messages handled."""
        if not self.enabled():
            return 0
        current = float(now if now is not None else self.clock())
        if current - self._last_poll < self.poll_interval():
            return 0
        self._last_poll = current
        loop = self._ensure_loop()
        if not loop.manager.configured():
            if not self._unconfigured_logged:
                self._log(
                    "channel intake: remote_enabled is set but no channel"
                    " is configured (matrix/slack/sms/email)"
                )
                self._unconfigured_logged = True
            return 0
        try:
            lease = self.store.acquire_lease(
                INTAKE_LEASE_KIND, INTAKE_LEASE_RESOURCE, self.holder,
                intake_lease_seconds(self.poll_interval()),
            )
        except KernelError as exc:
            # Epoch fencing: a superseded daemon must stop polling now.
            self._log(f"channel intake: lease refused ({exc}) — not polling")
            return 0
        if lease is None:
            holder = (self.store.get_lease(
                INTAKE_LEASE_KIND, INTAKE_LEASE_RESOURCE
            ) or {}).get("holder", "?")
            if holder != self._denied_by:
                self._log(
                    f"channel intake: lease held by {holder!r} — this"
                    " daemon is not polling the channels"
                )
                self._denied_by = holder
            return 0
        self._denied_by = ""
        handled = 0
        for message in loop.manager.poll_all():
            # Every claimed message is processed even if a stop lands
            # mid-batch: the channel cursor has already advanced past it,
            # so skipping would drop it.
            try:
                loop.handle_inbound(message)
                handled += 1
            except Exception as exc:
                self._log(
                    f"channel intake: error handling {message.channel}"
                    f" message: {type(exc).__name__}: {exc}"
                )
        if handled:
            self._log(
                f"channel intake: answered {handled} inbound message(s)"
            )
        return handled

    def release(self) -> None:
        """Release the intake lease (graceful shutdown → instant handoff)."""
        try:
            self.store.release_lease(
                INTAKE_LEASE_KIND, INTAKE_LEASE_RESOURCE, self.holder
            )
        except KernelError:
            pass

    # -- hosting ---------------------------------------------------------------

    def _ensure_loop(self):
        if self._loop is None:
            factory = self._loop_factory or self._default_loop
            self._loop = factory()
        return self._loop

    def _default_loop(self):
        from ..remote import RemoteLoop

        return RemoteLoop(
            self.config,
            turn_session_factory=self._turn_session,
            mission_input=self._mission_input,
        )

    def _turn_session(self):
        from ..bootstrap import build_agent_session
        from ..tooling import default_permissions

        return build_agent_session(
            self.config, interactive=False,
            permissions=default_permissions(),
        )

    def _mission_input(self, mission_id: str, text: str, message) -> str:
        """Bridge for `input msn-… <text>`: kernel inbox + immediate wake."""
        source = f"{message.channel}:{message.sender}"
        try:
            result = self.engine.provide_input(
                mission_id, text, source=source
            )
        except KernelError as exc:
            return f"Mission input failed: {exc}"
        if result["woken"]:
            return (
                f"Input delivered to {mission_id} — it wakes now and will"
                " run at the next session slot."
            )
        return (
            f"Input recorded for {mission_id}; its state is unchanged and"
            " the input will be visible at its next session."
        )
