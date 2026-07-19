"""Conch package.

Versioning: the minor version tracks the repo's merged-PR count plus the
in-flight release PR (PR #3 ≙ 0.4.0, so the Phase 0-4 branch — the next PR —
carries 0.5.0). Single-sourced here; pyproject.toml reads it dynamically.
"""

__version__ = "0.5.0"
