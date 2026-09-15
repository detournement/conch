"""Conch fleet: trusted SSH hosts running bounded, replaceable workers.

Swarm Phase 2. Submodules (imported lazily; importing ``conch.fleet``
alone pulls in nothing beyond this docstring):

- ``artifacts``  — reproducible signed single-file worker builds
- ``hostctl``    — the on-host control utility (stdlib-only, single file)
- ``registry``   — FleetRegistry persisted in the mission kernel
- ``worker``     — the worker supervisor runtime
- ``taskexec``   — bounded task execution over AgentSession
- ``transport``  — WorkerTransport: JSON RPC over SSH stdio
- ``enroll``     — trusted host enrollment flow
- ``plane``      — the distributed task plane (scheduling, leases, retries)
- ``controller`` — the supervised conch-controller daemon (the awakening)
- ``client``     — /fleet + fleet_delegate attach (socket or direct-drive)
- ``authority``  — owner grants, worker ceilings, the envelope clamp
- ``delegate``   — the model-callable fleet_delegate tool

Workers are replaceable compute, never state authorities: mission truth,
credentials, approvals, budgets, and external-action ledgers stay on the
controller. Nothing in the interactive shell depends on this package.
"""
