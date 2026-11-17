"""Required policy checks: a deterministic, fail-closed authorization layer.

Swarm Phase 0. This is deliberately distinct from user lifecycle hooks
(:func:`conch.tooling.run_hook`): user hooks are conveniences configured in
the user's config file and stay fail-open — a missing, crashing, or hanging
user hook must not brick the agent loop. Required policy checks are the
opposite. They are registered in code by subsystems, and the action is
DENIED whenever any registered check

- returns a deny decision,
- raises any exception,
- exceeds its timeout, or
- returns anything that is not a :class:`PolicyDecision` or a bool.

Non-negotiable invariant: models propose; deterministic policy authorizes.
Hooks may further restrict, but required policy always fails closed.

An empty registry allows: "required" refers to the registered checks — every
one of them must explicitly allow. Later phases (mission kernel, fleet,
Capitol adapter) register their checks here; Phase 0 wires the enforcement
seam into tool dispatch so behavior is already gated when they do.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from dataclasses import dataclass
from typing import Callable, List, Tuple

#: Per-check wall-clock budget. A check that cannot decide in time denies.
DEFAULT_CHECK_TIMEOUT = 5.0


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of required-policy evaluation."""

    allowed: bool
    reason: str = ""
    check: str = ""

    @staticmethod
    def allow(reason: str = "") -> "PolicyDecision":
        return PolicyDecision(True, reason)

    @staticmethod
    def deny(reason: str = "", check: str = "") -> "PolicyDecision":
        return PolicyDecision(False, reason, check)


class PolicyRegistry:
    """Ordered registry of required policy checks.

    A check is ``fn(event: str, payload: dict) -> PolicyDecision | bool``.
    Checks run in registration order; the first deny wins and evaluation
    stops. Every registered check must allow for the action to proceed.
    """

    def __init__(self, timeout: float = DEFAULT_CHECK_TIMEOUT):
        self._lock = threading.RLock()
        self._checks: List[Tuple[str, Callable]] = []
        self.timeout = float(timeout)

    def register(self, name: str, check: Callable) -> None:
        name = str(name or "").strip()
        if not name:
            raise ValueError("required policy check needs a non-empty name")
        if not callable(check):
            raise ValueError(f"required policy check {name!r} is not callable")
        with self._lock:
            if any(existing == name for existing, _ in self._checks):
                raise ValueError(
                    f"required policy check {name!r} is already registered"
                )
            self._checks.append((name, check))

    def unregister(self, name: str) -> bool:
        with self._lock:
            before = len(self._checks)
            self._checks = [
                (existing, fn) for existing, fn in self._checks
                if existing != name
            ]
            return len(self._checks) < before

    def names(self) -> List[str]:
        with self._lock:
            return [name for name, _ in self._checks]

    def clear(self) -> None:
        with self._lock:
            self._checks = []

    # -- evaluation ----------------------------------------------------------

    def _run_one(self, name: str, check: Callable, event: str,
                 payload: dict) -> PolicyDecision:
        # One short-lived worker thread per check so a hung check cannot
        # stall the caller past the timeout. A timed-out check may keep
        # running in the background; the deny stands regardless of what it
        # would eventually have returned.
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(check, event, payload)
            try:
                result = future.result(timeout=self.timeout)
            except _FutureTimeout:
                return PolicyDecision.deny(
                    f"required policy check '{name}' timed out after "
                    f"{self.timeout:g}s — failing closed",
                    check=name,
                )
            except Exception as exc:
                return PolicyDecision.deny(
                    f"required policy check '{name}' raised "
                    f"{type(exc).__name__}: {exc} — failing closed",
                    check=name,
                )
        finally:
            executor.shutdown(wait=False)
        if isinstance(result, PolicyDecision):
            if result.allowed:
                return PolicyDecision.allow(result.reason)
            return PolicyDecision.deny(
                result.reason or "denied by required policy check",
                check=result.check or name,
            )
        if isinstance(result, bool):
            if result:
                return PolicyDecision.allow()
            return PolicyDecision.deny(
                "denied by required policy check", check=name
            )
        return PolicyDecision.deny(
            f"required policy check '{name}' returned an invalid decision "
            f"({type(result).__name__}) — failing closed",
            check=name,
        )

    def evaluate(self, event: str, payload: dict) -> PolicyDecision:
        """Evaluate every registered check for *event*; first deny wins."""
        with self._lock:
            checks = list(self._checks)
        if not checks:
            return PolicyDecision.allow("no required policy checks registered")
        for name, check in checks:
            decision = self._run_one(name, check, event, payload)
            if not decision.allowed:
                return decision
        return PolicyDecision.allow(
            f"all {len(checks)} required policy check(s) allowed"
        )


#: Process-wide registry consumed by tool dispatch. Subsystems register
#: their required checks here; tests may construct private registries.
REQUIRED_POLICY = PolicyRegistry()


def register_required_policy(name: str, check: Callable) -> None:
    REQUIRED_POLICY.register(name, check)


def unregister_required_policy(name: str) -> bool:
    return REQUIRED_POLICY.unregister(name)


def evaluate_required_policy(event: str, payload: dict) -> PolicyDecision:
    return REQUIRED_POLICY.evaluate(event, payload)
