"""Golden-fixture equivalence gate for the eBay Capitol flows (R0).

The recorded fixtures in ``tests/fixtures/capitol_golden/`` freeze the
wire-call sequences, approval-store records, durable state, and reply
text the flow-pack refactor stages (R1 engine extraction, R2 eBay pack)
must reproduce byte-identically — modulo only the volatile fields the
scenario driver normalizes (generated ids, content hashes, timestamps;
see ``tests/capitol_golden_scenarios.py`` for the exact rules).

Re-record intentionally changed behavior with::

    CONCH_GOLDEN_RECORD=1 python -m unittest tests.test_capitol_golden

and review the fixture diff like any other code change.
"""

import os
import unittest

from tests.capitol_golden_scenarios import (
    SCENARIOS,
    fixture_path,
    load_fixture,
    record_fixture,
    run_scenario,
)


class GoldenEquivalenceTests(unittest.TestCase):
    maxDiff = None

    def _check(self, name):
        if os.environ.get("CONCH_GOLDEN_RECORD"):
            record_fixture(name)
            return
        self.assertTrue(
            fixture_path(name).exists(),
            f"missing golden fixture {fixture_path(name)}; record with "
            "CONCH_GOLDEN_RECORD=1 python -m unittest "
            "tests.test_capitol_golden",
        )
        expected = load_fixture(name)
        actual = run_scenario(name)
        self.assertEqual(expected, actual)

    def test_fixture_names_match_scenarios(self):
        if os.environ.get("CONCH_GOLDEN_RECORD"):
            self.skipTest("recording")
        recorded = {path.stem for path in fixture_path("x").parent.glob("*.json")}
        self.assertEqual(recorded, set(SCENARIOS),
                         "fixtures and scenario registry must stay in sync")


def _make_test(name):
    def test(self):
        self._check(name)
    test.__doc__ = SCENARIOS[name].__doc__
    return test


for _name in SCENARIOS:
    setattr(GoldenEquivalenceTests, f"test_{_name}", _make_test(_name))


if __name__ == "__main__":
    unittest.main()
