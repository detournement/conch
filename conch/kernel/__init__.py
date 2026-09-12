"""Durable mission/controller kernel (Swarm Phase 1).

One transactional SQLite store (WAL, single-writer thread) is the sole
coordination truth for missions, plans, tasks, attempts, timers, leases,
checkpoints, budgets, approvals, external actions, inbox/outbox, artifacts,
and resource bindings. JSON/JSONL remain export/audit formats only.

Import discipline: the interactive shell never imports this package unless
``edge_daemon=true`` — the no-daemon invariant (Conch stays a shell first)
is enforced by tests. Everything in here is stdlib-only.

Submodules:

- :mod:`conch.kernel.model` — states, transitions, event taxonomy, spec.
- :mod:`conch.kernel.store` — the transactional event/projection store.
- :mod:`conch.kernel.engine` — bounded work sessions over AgentSession.
- :mod:`conch.kernel.daemon` — the supervised ``conch-edge`` daemon.
- :mod:`conch.kernel.control` — versioned JSON control-socket protocol.
- :mod:`conch.kernel.client` — shell attach (socket or direct, same API).
- :mod:`conch.kernel.migrate` — legacy ``tasks.json`` migration.
"""
