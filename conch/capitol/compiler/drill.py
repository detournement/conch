"""The ``workflow_drill`` acceptance kind: synthetic inputs through the
REAL workflows, expected gates asserted.

Generated packs declare ``acceptance.kind = "workflow_drill"`` with a
fixtures file (installed by materialization into the pack's ``assets/``
directory from the approved card). Each fixture triggers its workflow on
the serving stack with the synthetic input, waits (bounded) for a
terminal state, and asserts the expected gates: terminal status and
required output markers. Any gate failure raises
:class:`~conch.capitol.errors.CapitolError` — the caller decides what a
failure means (``/capitol pack verify`` reports it; the compiler leaves
the compilation at status=materialized with the failure attached and
rollback on offer).

Fixture shape (written by :mod:`conch.capitol.compiler.card`)::

    [{"workflow": "$create:<identity>", "workflow_id": "<uuid5>",
      "override_key": "<node_id>.<field>", "input": <value>,
      "expect": {"status": "success", "output_contains": ["..."]}}]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..errors import CapitolError

#: Hard bound on one drill run's wait (seconds); configurable down.
DRILL_TIMEOUT_DEFAULT = 600.0
DRILL_POLL_SECONDS = 5.0


def _default_driver(config: dict):
    """A RunDriver against the configured stack (admin token by
    reference; never printed)."""
    from ..credentials import resolve_admin_token
    from ..together_funding import RunDriver

    workflow_url = str(config.get("capitol_base_url") or "").strip()
    platform_url = str(config.get("capitol_platform_url") or "").strip()
    org = str(config.get("capitol_org") or "").strip()
    if not (workflow_url and org):
        raise CapitolError(
            "workflow_drill needs capitol_base_url and capitol_org"
        )
    token, _source = resolve_admin_token(
        config, org, platform_url or workflow_url
    )
    return RunDriver(workflow_url, token, org)


def load_fixtures(pack) -> List[Dict[str, Any]]:
    """The pack's installed drill fixtures (fail closed)."""
    acceptance = pack.raw.get("acceptance") or {}
    relative = str(acceptance.get("fixtures") or "")
    if not relative:
        raise CapitolError(
            f"pack {pack.name} declares workflow_drill without a "
            "fixtures file"
        )
    if not pack.source:
        raise CapitolError(
            f"pack {pack.name} has no source directory to read fixtures "
            "from"
        )
    path = Path(pack.source) / relative
    try:
        fixtures = json.loads(path.read_text())
    except FileNotFoundError:
        raise CapitolError(
            f"drill fixtures missing: {path}"
        ) from None
    except (ValueError, OSError) as exc:
        raise CapitolError(
            f"drill fixtures unreadable at {path}: {exc}"
        ) from None
    if not isinstance(fixtures, list) or not fixtures:
        raise CapitolError(
            f"drill fixtures at {path} must be a non-empty JSON list"
        )
    return fixtures


def run_fixtures(
    fixtures: List[Dict[str, Any]],
    config: dict,
    *,
    driver=None,
    timeout: Optional[float] = None,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Run every fixture; returns evidence. Raises on the first failed
    gate (fail closed — a drill is a proof, not a report card)."""
    driver = driver or _default_driver(config)
    if timeout is None:
        try:
            timeout = float(
                config.get("compile_drill_timeout", DRILL_TIMEOUT_DEFAULT)
                or DRILL_TIMEOUT_DEFAULT
            )
        except (TypeError, ValueError):
            timeout = DRILL_TIMEOUT_DEFAULT
    evidence: Dict[str, Any] = {"runs": []}
    for index, fixture in enumerate(fixtures):
        where = f"fixture[{index}]"
        workflow_id = str(fixture.get("workflow_id") or "")
        override_key = str(fixture.get("override_key") or "")
        expect = fixture.get("expect") or {}
        if not workflow_id or not override_key:
            raise CapitolError(
                f"{where} is missing workflow_id/override_key — "
                "regenerate the pack from the card"
            )
        value = fixture.get("input")
        if not isinstance(value, str):
            value = json.dumps(value, sort_keys=True)
        log(f"  drill {where}: workflow {workflow_id}")
        submitted = driver.trigger(workflow_id, {override_key: value})
        run_id = str(submitted.get("run_id") or "")
        if not run_id:
            raise CapitolError(
                f"{where}: the runs API returned no run_id"
            )
        log(f"    run {run_id} (waiting up to {int(timeout)}s)")
        detail = driver.wait_terminal(
            workflow_id, run_id, timeout=timeout,
            poll=DRILL_POLL_SECONDS, log=lambda line: log(f"  {line}"),
        )
        status = str(detail.get("status") or "").lower()
        wanted = str(expect.get("status") or "success").lower()
        run_evidence = {
            "workflow_id": workflow_id, "run_id": run_id,
            "status": status,
        }
        evidence["runs"].append(run_evidence)
        if status != wanted:
            raise CapitolError(
                f"{where}: run {run_id} ended {status!r}, expected "
                f"{wanted!r}: "
                + str(detail.get("error_message") or "")[:300]
            )
        blob = json.dumps(detail, default=str)
        missing = [
            marker for marker in expect.get("output_contains") or []
            if str(marker) not in blob
        ]
        if missing:
            raise CapitolError(
                f"{where}: run {run_id} output lacks expected marker(s): "
                + ", ".join(repr(marker) for marker in missing)
            )
        run_evidence["markers"] = list(
            expect.get("output_contains") or []
        )
        log(f"    ✓ {status}"
            + (f", markers present: {len(run_evidence['markers'])}"
               if run_evidence["markers"] else ""))
    return evidence


def run_workflow_drill(pack, config: dict, *, driver=None,
                       timeout: Optional[float] = None,
                       log: Callable[[str], None] = print
                       ) -> Dict[str, Any]:
    """`/capitol pack verify` entrypoint for workflow_drill packs."""
    fixtures = load_fixtures(pack)
    evidence = run_fixtures(
        fixtures, config, driver=driver, timeout=timeout, log=log,
    )
    evidence["pack"] = pack.name
    evidence["pack_digest"] = pack.digest
    return evidence
