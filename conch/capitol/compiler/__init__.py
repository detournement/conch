"""ProcessCompiler (process-compiler plan C1/C2).

A stated goal becomes governed, operating infrastructure through a
reviewed compilation step: a bounded compilation session designs an
**Architecture Card** (``conch.capitol.compiler.card``), the user reviews
and approves it (`/compile` — an origin-bound kernel approval), and the
materializer (``conch.capitol.compiler.materialize``) drives the ledgered
``CapitolAdmin`` surface to provision exactly what the approved card
declares, writes the generated flow pack, proves the result with the
generated acceptance drill (``conch.capitol.compiler.drill``), and binds
a dry-run supervising mission.

Compilation state is event-sourced in the kernel (the ``compilations``
aggregate in :mod:`conch.kernel.store`): cards, versions, the approval
decision, materialization receipts, and drill results — replay == live,
full audit, one-command rollback.
"""
