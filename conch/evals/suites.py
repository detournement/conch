"""Registry of the versioned evaluation suites selectable with /eval --suite.

Each suite is a separate module with its own ID, version and implementation
hash, so adding or changing one never perturbs another suite's comparability.
"""

from typing import NamedTuple

from . import cases as _lite
from . import code_cases as _code

DEFAULT_SUITE = "conch-lite"


class Suite(NamedTuple):
    id: str
    version: int
    title: str
    module: object
    limits: dict
    default_deadline: int
    blurb: str
    verdict_scope: str

    @property
    def cases(self):
        return self.module.CASES

    def hash(self):
        return self.module.suite_hash()


SUITES = {
    "conch-lite": Suite(
        _lite.SUITE_ID, _lite.SUITE_VERSION, "Conch Lite", _lite,
        {"model_calls_per_case": 4, "tool_calls_per_case": 12, "max_output_tokens": 1024},
        300,
        "Checks practical Conch skills: shell commands, tools, file edits/saves, evidence, recovery and trust.",
        "Conch"),
    # Coding loops need search -> read -> edit -> test -> report as separate
    # observed responses, so this suite allows more calls than conch-lite.
    "conch-code": Suite(
        _code.SUITE_ID, _code.SUITE_VERSION, "Conch Code", _code,
        {"model_calls_per_case": 8, "tool_calls_per_case": _code.MAX_TOOL_CALLS, "max_output_tokens": 2048},
        600,
        "Checks coding skills on tiny fixture repos: fixing a failing test, a multi-file rename, "
        "search-then-edit, and honest test reporting. Tests are simulated; nothing is executed.",
        "Conch coding"),
}
ALIASES = {"lite": "conch-lite", "code": "conch-code"}
MAX_CALLS = max(suite.limits["model_calls_per_case"] for suite in SUITES.values())


def get_suite(name=None) -> Suite:
    key = ALIASES.get(name, name) if name else DEFAULT_SUITE
    if key not in SUITES:
        raise ValueError("Unknown evaluation suite: " + str(name) + " (choose " + ", ".join(sorted(ALIASES)) + ").")
    return SUITES[key]
