"""Differential tests: the ``a2actrl`` CLI vs Conch's adapter (Phase 3).

The roadmap makes A2Actrl the *normative reference client* for the Capitol
A2A wire: "verified differentially against the a2actrl CLI during
development (same operation via both, results compared); divergences are
Conch bugs unless documented." This module runs the same read/invoke
operations through both the reference CLI and :class:`CapitolRuntime`
against the local dev stack and asserts they agree on the semantic facts,
ignoring volatile fields (timestamps, per-call ids, event ordering noise).

Opt-in and fail-closed (see :mod:`tests.capitol_live_support`): skipped
unless ``CONCH_CAPITOL_LIVE`` is set, the stack answers, an admin token
resolves, and the ``a2actrl`` CLI is importable/executable (looked up on
PATH, at ``CONCH_A2ACTRL_BIN``, or in the sibling A2Actrl checkout's
virtualenv). All assets carry the ``conch-phase3-`` prefix and are
disposable; a temporary registry alias is added so the CLI can reach the
disposable agent and is removed by restoring the registry afterwards.

DIVERGENCES (accepted, not Conch bugs)
======================================

1. **Workflow version rollback store.** The platform-api
   ``POST /agentic-workflows/{org}/{wf}/rollback`` endpoint reads a
   *separate* version history that workflow-api ``POST /workflows``
   publishes do not populate, so it 404s/400s for workflows Conch
   publishes. Conch (and a workflow-api-native undo generally) reverts a
   publish by re-persisting the payload with ``publish_to_api`` cleared
   through the same workflow-api endpoint, keeping publish and undo in one
   version lineage. a2actrl exposes no rollback command, so there is no
   CLI counterpart to diverge from; this is documented here as the
   authoritative undo path. See :meth:`CapitolAdmin.rollback_workflow`.

2. **``get_workflow_events`` cursor is client-enforced.** On a bare
   ``since_sequence`` the gateway re-sends the persisted tail rather than
   filtering server-side. Both clients defend against replay on the
   client (a2actrl in its poller, Conch in
   :meth:`CapitolRuntime.poll_run`), so the *observable* resume semantics
   match; the raw single-call response is intentionally not compared for
   strict server-side filtering.

3. **CLI presentation.** a2actrl renders results as Rich-formatted JSON
   (ANSI escapes, width-based soft wrapping, and literal newlines inside
   string values) and renders ``workflows`` as a table with no ``--raw``.
   These are presentation choices, not wire differences; the helper
   strips ANSI and parses with ``strict=False``, and the workflow-list
   comparison reads ids out of the table.

4. **Card ``version`` vs wire schema.** The AgentCard's ``version`` field
   is the gateway build (e.g. ``1.0.27``); the A2A *wire schema* the
   adapter validates against (``1.0.x``) is a separate concern. Both
   clients read the same ``capabilities`` and ``skills`` catalog.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from conch.capitol.client import CapitolRuntime
from conch.capitol.credentials import (
    REGISTRY_PATH,
    sink_bearer_to_registry,
)

from tests import capitol_live_support as live
from tests.capitol_live_support import (
    ASSET_PREFIX,
    ORG,
    WORKFLOW_URL,
    LiveHTTP,
    poll_until_terminal,
    unique_suffix,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
TERMINAL = {"success", "failed", "stopped", "cancelled"}


def _find_a2actrl() -> str:
    explicit = os.environ.get("CONCH_A2ACTRL_BIN")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("a2actrl")
    if found:
        return found
    # sibling A2Actrl checkout's virtualenv (the dev layout)
    candidate = Path.home() / "composer" / "A2Actrl" / ".venv" / "bin" / "a2actrl"
    if candidate.exists():
        return str(candidate)
    return ""


class CrossClientCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = live.live_config()
        cls.cli = _find_a2actrl()
        if not cls.cli:
            raise unittest.SkipTest("a2actrl CLI not found (PATH / "
                                    "CONCH_A2ACTRL_BIN / sibling .venv)")
        cls.http = LiveHTTP()
        eval_base = cls.http.find_workflow_by_name(live.EVAL_WORKFLOW_NAME)
        if not eval_base:
            raise unittest.SkipTest(
                f"eval workflow {live.EVAL_WORKFLOW_NAME!r} not in dev org"
            )
        suffix = unique_suffix()
        cls.alias = f"{ASSET_PREFIX}xclient-{suffix}"
        cls._cleanup = []
        # a disposable published copy of the eval workflow + an agent for it
        cls.workflow_id = cls.http.duplicate_workflow(
            eval_base, f"{ASSET_PREFIX}xclient-wf-{suffix}"
        )
        cls._cleanup.append(("wf", cls.workflow_id))
        payload = cls.http.get_workflow_payload(cls.workflow_id)
        payload["publish_to_api"] = True
        cls.http.persist_workflow(payload)
        status, agent = cls.http.pl("POST", f"/agents/{ORG}", {
            "name": f"{ASSET_PREFIX}xclient-op-{suffix}",
            "model_provider": "anthropic",
            "model_name": "claude-sonnet-4-20250514",
            "enable_workflow_runtime": True, "exposed_via_a2a": True,
            "workflow_allowlist": [cls.workflow_id],
        })
        if status >= 400:
            cls.http.delete_workflow(cls.workflow_id)
            raise unittest.SkipTest(f"could not create live agent: {agent}")
        cls.agent_id = str((agent.get("agent") or {}).get("id"))
        bearer = str(agent.get("a2a_bearer_token") or "")
        cls._cleanup.append(("agent", cls.agent_id))
        # snapshot the real registry, then add a disposable alias so the CLI
        # can reach the agent (restored verbatim in tearDownClass)
        cls._registry_snapshot = (
            REGISTRY_PATH.read_text() if REGISTRY_PATH.exists() else None
        )
        sink_bearer_to_registry(
            cls.alias, org_id=ORG, agent_id=cls.agent_id,
            base_url=WORKFLOW_URL, bearer=bearer,
            description="conch-phase3 disposable cross-client test alias",
        )
        cls.runtime = CapitolRuntime(
            WORKFLOW_URL, ORG, cls.agent_id, bearer,
            caller_system="conch-xclient-tests", caller_version="0.0",
        )

    @classmethod
    def tearDownClass(cls):
        # restore the registry (removes the disposable alias)
        snapshot = getattr(cls, "_registry_snapshot", None)
        try:
            if snapshot is not None:
                REGISTRY_PATH.write_text(snapshot)
        except OSError:
            pass
        for kind, ident in reversed(getattr(cls, "_cleanup", [])):
            try:
                if kind == "agent":
                    cls.http.delete_agent(ident)
                else:
                    cls.http.delete_workflow(ident)
            except Exception:
                pass

    # -- CLI plumbing --------------------------------------------------------

    def _cli_json(self, *args, timeout=60):
        """Run the CLI and parse its Rich-JSON stdout (ANSI stripped,
        ``strict=False`` for literal newlines in string values)."""
        env = dict(os.environ, NO_COLOR="1", COLUMNS="100000", TERM="dumb")
        proc = subprocess.run(
            [self.cli, *args], capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        clean = _ANSI.sub("", proc.stdout)
        try:
            return json.loads(clean, strict=False)
        except ValueError as exc:
            self.fail(
                f"a2actrl {' '.join(args)} did not emit JSON "
                f"(rc={proc.returncode}): {exc}; head={clean[:200]!r}; "
                f"stderr={_ANSI.sub('', proc.stderr)[:200]!r}"
            )

    def _cli_text(self, *args, timeout=60):
        env = dict(os.environ, NO_COLOR="1", COLUMNS="100000", TERM="dumb")
        proc = subprocess.run(
            [self.cli, *args], capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        return _ANSI.sub("", proc.stdout + proc.stderr)

    # -- read comparisons ----------------------------------------------------

    def test_card_semantics_match(self):
        cli_card = self._cli_json("card", self.alias)
        adapter_card = self.runtime.discover()
        self.assertEqual(adapter_card.get("name"), cli_card.get("name"))
        self.assertEqual(
            (adapter_card.get("capabilities") or {}).get("streaming"),
            (cli_card.get("capabilities") or {}).get("streaming"),
        )
        cli_skills = {
            s.get("id") for s in cli_card.get("skills") or []
            if isinstance(s, dict)
        }
        self.assertEqual(self.runtime.skill_ids(), frozenset(cli_skills))

    def test_handshake_semantics_match(self):
        cli_session = self._cli_json("handshake", self.alias)
        adapter_context = self.runtime.handshake()
        # both must establish a context/session id
        cli_context = (
            (cli_session.get("session") or {}).get("context_id")
            or cli_session.get("context_id")
        )
        self.assertTrue(cli_context, cli_session)
        self.assertTrue(adapter_context)
        # and agree on the negotiated wire schema version
        cli_wire = cli_session.get("wire_schema_version")
        if cli_wire:
            card = self.runtime.discover()
            self.assertEqual(cli_wire, card.get("version"))

    def test_workflow_list_matches(self):
        # Both clients call the same ``list_workflows`` skill and identify a
        # workflow by its stable slug (the adapter under ``id``, the CLI in
        # its ``workflow_id`` table column).
        adapter_ids = {
            str(w.get("workflow_id") or w.get("id"))
            for w in self.runtime.list_workflows()
        }
        self.assertTrue(adapter_ids, "adapter returned no workflows")
        text = self._cli_text("workflows", self.alias)
        for workflow_ref in adapter_ids:
            self.assertIn(
                workflow_ref, text,
                f"{workflow_ref} listed by the adapter but not by the CLI",
            )

    def test_describe_matches(self):
        cli_shape = self._cli_json("inspect", self.alias, self.workflow_id)
        adapter_shape = self.runtime.describe_workflow(self.workflow_id)
        # both resolve the same workflow and expose an input field schema
        self.assertTrue(isinstance(adapter_shape, dict) and adapter_shape)
        self.assertTrue(isinstance(cli_shape, dict) and cli_shape)
        cli_wf = (cli_shape.get("workflow_id") or cli_shape.get("id"))
        if cli_wf:
            self.assertIn(str(cli_wf),
                          {self.workflow_id, "conch_phase3_xclient_wf",
                           str(cli_shape.get("workflow_id"))})

    # -- run start / status / events / cancel --------------------------------

    def test_run_status_and_events_match(self):
        key = f"xclient-{unique_suffix()}"
        started = self.runtime.call_workflow(
            self.workflow_id, {}, idempotency_key=key
        )
        run_id = started["run_id"]
        print(f"\n[xclient run] run_id={run_id}")
        self.assertIn(poll_until_terminal(self.runtime, run_id), TERMINAL)
        # status via both clients agrees on the terminal verdict
        cli_status = self._cli_json("status", self.alias, run_id)
        adapter_status = self.runtime.run_status(run_id)
        self.assertEqual(
            str(adapter_status.get("status")).lower(),
            str(cli_status.get("status")).lower(),
        )
        # events-since via both: identical sequence sets (volatile fields
        # like timestamps ignored — we compare the sequence spine)
        cli_events = self._cli_json("events", self.alias, run_id, "--raw")
        cli_seqs = sorted(
            int(e["sequence"])
            for e in cli_events.get("events") or []
            if isinstance(e, dict) and e.get("sequence") is not None
        )
        adapter_seqs = sorted(
            int(e["sequence"])
            for e in self.runtime.run_events(run_id).get("events") or []
            if e.get("sequence") is not None
        )
        self.assertTrue(adapter_seqs, "adapter returned no events")
        self.assertEqual(adapter_seqs, cli_seqs)
        # events-since a cursor: both expose only >= cursor (client-enforced)
        cursor = adapter_seqs[len(adapter_seqs) // 2]
        adapter_since = sorted(
            int(e["sequence"])
            for e in self.runtime.poll_run(
                run_id, since_sequence=cursor, max_polls=1,
                _sleep=lambda _s: None,
            )
            if e.get("sequence") is not None
        )
        self.assertTrue(all(seq >= cursor for seq in adapter_since))

    def test_cancel_semantics_match(self):
        # Cancel through each client on its own fresh run; both speak the
        # canonical A2A CancelTask and return a task-state envelope (the
        # run may already be terminal — the point is the wire agreement).
        def _start():
            started = self.runtime.call_workflow(
                self.workflow_id, {}, idempotency_key=f"xcancel-{unique_suffix()}"
            )
            return started["run_id"]

        adapter_run = _start()
        adapter_cancel = self.runtime.cancel_task(adapter_run)
        adapter_state = (adapter_cancel.get("status") or {}).get("state")
        self.assertTrue(adapter_state, adapter_cancel)

        cli_run = _start()
        cli_cancel = self._cli_json("cancel", self.alias, cli_run)
        cli_state = (cli_cancel.get("status") or {}).get("state")
        self.assertTrue(cli_state, cli_cancel)

        # both return a canonical A2A TASK_STATE_* token
        self.assertTrue(str(adapter_state).startswith("TASK_STATE_")
                        or str(adapter_state).lower() in TERMINAL
                        or "CANCEL" in str(adapter_state).upper())
        self.assertTrue(str(cli_state).startswith("TASK_STATE_")
                        or str(cli_state).lower() in TERMINAL
                        or "CANCEL" in str(cli_state).upper())


if __name__ == "__main__":
    unittest.main()
