"""Live ProcessCompiler proof against the local Capitol stack (opt-in).

Gated exactly like the Phase 3 live suite: skips unless
``CONCH_CAPITOL_LIVE`` is set, both APIs answer, ``CONCH_CAPITOL_ORG``
names the org, and an admin token resolves by reference. Every asset
carries a unique ``conch-compile-live-…`` prefix and the test ends with
``rollback_compilation`` — proving the one-command revert live — so the
org is left clean. (The operator-facing live proof keeps its assets and
uses the ``conch-compile-…`` prefix instead.)

The card here is the deterministic candidate (code-built, reproducible)
rather than a model emission: the compilation *session* is proven by the
scripted suites and the operator's live run; this test proves the C2
machinery — approve → materialize (real CapitolAdmin, real catalog) →
fail-closed pack load → real acceptance drill runs → dry-run supervising
mission → rollback — end to end on the real stack.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests import capitol_live_support as live
from tests.compiler_fixtures import candidate_card


class TestCompilerLiveProof(unittest.TestCase):
    def setUp(self):
        self.config = dict(live.live_config())
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        from conch.kernel.store import MissionStore

        self.store = MissionStore(root / "kernel" / "kernel.db")
        self.addCleanup(self.store.close)
        self.packs_dir = root / "packs"
        self.prefix = f"conch-compile-live-{live.unique_suffix()}"

    def live_card(self, discovery):
        """The candidate card, uniquely prefixed, reusing a discovered
        ledger collection (the plan's candidate reuses the existing
        collection; synthetic drill rows keep the drill account-free)."""
        raw = copy.deepcopy(candidate_card())
        text = json.dumps(raw)
        raw = json.loads(text.replace("conch-compile", self.prefix))
        collections = discovery.get("collections") or []
        ledger = next(
            (row for row in collections
             if row.get("name") == "together-funding-requests"),
            collections[0] if collections else None,
        )
        if ledger is None:
            raise unittest.SkipTest(
                "the live org has no collections to reuse"
            )
        raw["assets"]["reuse"] = [{
            "kind": "collection", "id": ledger["id"],
            "name": ledger["name"],
            "reason": "the ledger the report summarizes",
        }]
        raw["assets"]["create"]["collections"] = []
        stage = raw["assets"]["create"]["workflows"][0]["stages"][1]
        stage["system_prompt"] = (
            "You summarize funding-ledger activity. Your input is either "
            "'today' or a JSON array of synthetic ledger rows. When it "
            "parses as a JSON array, summarize exactly those rows and "
            "nothing else (do not call tools). Otherwise search "
            f"collection $collection:{ledger['id']} for today's events. "
            "Start your report with the exact line FUNDING LEDGER DAILY "
            "REPORT then one line per event."
        )
        return raw

    def test_full_arc_and_rollback(self):
        from conch.capitol.compiler.card import normalize_card
        from conch.capitol.compiler.materialize import (
            materialize_compilation,
            rollback_compilation,
            verify_compilation,
        )
        from conch.capitol.compiler.session import build_discovery
        from conch.kernel.model import CompilationStatus

        discovery = build_discovery(self.config)
        self.assertTrue(
            discovery["node_catalog"],
            "live discovery must include the node catalog",
        )
        card = normalize_card(
            self.live_card(discovery), discovery, prefix=self.prefix,
        )
        compilation = self.store.create_compilation(card, actor="live")
        cid = compilation["compilation_id"]
        self.store.decide_compilation(cid, "approve", decided_by="live")

        rolled_back = False
        try:
            state = materialize_compilation(
                self.store, self.config, cid,
                packs_dir=self.packs_dir, log=lambda line: None,
            )
            self.assertTrue(state["steps"])
            self.assertEqual(
                self.store.get_compilation(cid)["status"],
                CompilationStatus.MATERIALIZED,
            )
            outcome = verify_compilation(
                self.store, self.config, cid,
                packs_dir=self.packs_dir, log=lambda line: None,
            )
            self.assertEqual(
                outcome["status"], CompilationStatus.OPERATING
            )
            self.assertTrue(outcome["mission_id"])
            mission = self.store.get_mission(outcome["mission_id"])
            self.assertTrue(mission["spec"]["dry_run"])
            # replay == live all the way through the arc
            ok, message = self.store.replay_matches_live()
            self.assertTrue(ok, message)
        finally:
            try:
                rollback_compilation(
                    self.store, self.config, cid, log=lambda line: None,
                )
                rolled_back = True
            except Exception:
                if rolled_back:
                    raise
        self.assertEqual(
            self.store.get_compilation(cid)["status"],
            CompilationStatus.ROLLED_BACK,
        )


if __name__ == "__main__":
    unittest.main()
