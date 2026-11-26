"""Private, crash-safe evaluation reports; no network or conversation access."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__
from ..conversations import _atomic_write_text, _ensure_private_dir
from ..secretguard import redact_credentials
from .suites import DEFAULT_SUITE, MAX_CALLS, SUITES, get_suite

SCHEMA_VERSION = 1
MAX_REPORT_BYTES = 2 * 1024 * 1024
STATUSES = {"running", "complete", "cancelled", "timed_out", "error", "interrupted"}
CASE_STATUSES = {"not_run", "running", "passed", "failed", "error"}


def _cases_by_id(suite_id) -> dict:
    suite = SUITES.get(suite_id)
    return {case.id: case for case in suite.cases} if suite else {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch" / "evals"


def _run_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Expected a saved evaluation's full run ID.") from exc
    if str(parsed) != value or parsed.version != 4:
        raise ValueError("Expected a canonical evaluation run ID.")
    return value


def report_path(run_id: str) -> Path:
    return state_dir() / (_run_id(run_id) + ".json")


def _lock_path(run_id: str) -> Path:
    return state_dir() / ("." + _run_id(run_id) + ".lock")


@contextmanager
def run_lock(run_id: str):
    """A kernel-held lock distinguishes interrupted reports from live runs."""
    directory = state_dir()
    _ensure_private_dir(directory)
    if directory.stat().st_mode & 0o077:
        raise PermissionError("Evaluation directory must be private (0700).")
    path = _lock_path(run_id)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def _running(run_id: str) -> bool:
    try:
        fd = os.open(_lock_path(run_id), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def scrub(value):
    """Scrub before persistence, including diagnostics returned by providers."""
    if isinstance(value, str):
        return redact_credentials(value, "[credential redacted]")[0]
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    return value


def new_report(target: dict, *, deadline_seconds: int = 300, suite: str = DEFAULT_SUITE) -> dict:
    chosen = get_suite(suite)
    return {
        "schema_version": SCHEMA_VERSION,
        "suite": {"id": chosen.id, "version": chosen.version, "hash": chosen.hash()},
        "run_id": str(uuid.uuid4()), "conch_version": __version__,
        "started_at": now(), "finished_at": None, "status": "running",
        "target": copy.deepcopy(target),
        "limits": dict(chosen.limits, deadline_seconds=deadline_seconds),
        "cases": [{"id": case.id, "category": case.category, "status": "not_run",
                   "failures": [], "error_kind": "", "elapsed_seconds": 0.0,
                   "calls": 0, "usage": {"input_tokens": None, "output_tokens": None,
                                          "complete": False},
                   "observed_models": [], "identity": "unverified",
                   "trace": [], "replies": []} for case in chosen.cases],
        "sharing": None,
    }


def _validate(report: dict) -> None:
    if not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported evaluation report schema.")
    _run_id(report.get("run_id"))
    if not isinstance(report.get("status"), str) or report["status"] not in STATUSES:
        raise ValueError("Invalid evaluation run status.")
    if not all(isinstance(report.get(key), str) for key in ("started_at", "conch_version")):
        raise ValueError("Invalid evaluation metadata.")
    target, suite, cases = report.get("target"), report.get("suite"), report.get("cases")
    if not isinstance(target, dict) or not all(isinstance(target.get(key), str)
                                             for key in ("provider", "requested_model")):
        raise ValueError("Invalid evaluation target.")
    if (not isinstance(suite, dict) or not isinstance(suite.get("id"), str)
            or type(suite.get("version")) is not int or not isinstance(suite.get("hash"), str)):
        raise ValueError("Invalid evaluation suite.")
    if not isinstance(cases, list) or not cases or len(cases) > 100:
        raise ValueError("Invalid evaluation cases.")
    limits = report.get("limits")
    max_calls = limits.get("model_calls_per_case", 4) if isinstance(limits, dict) else 4
    if type(max_calls) is not int or not 1 <= max_calls <= MAX_CALLS:
        raise ValueError("Invalid evaluation limits.")
    seen = set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get("id"), str)
                or case["id"] in seen or not isinstance(case.get("category"), str)
                or not isinstance(case.get("status"), str) or case["status"] not in CASE_STATUSES
                or not isinstance(case.get("failures"), list)
                or not all(isinstance(item, str) for item in case["failures"])
                or not isinstance(case.get("usage"), dict)):
            raise ValueError("Invalid or duplicate evaluation case.")
        if (type(case.get("calls")) is not int or not 0 <= case["calls"] <= max_calls
                or type(case.get("elapsed_seconds")) not in (int, float)
                or not 0 <= case["elapsed_seconds"] <= 86400):
            raise ValueError("Invalid evaluation measurements.")
        usage = case["usage"]
        if type(usage.get("complete")) is not bool:
            raise ValueError("Invalid evaluation usage completeness.")
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if value is None:
                if usage["complete"]:
                    raise ValueError("Complete usage needs reported token counts.")
            elif type(value) is not int or value < 0:
                raise ValueError("Invalid evaluation token count.")
        if report["status"] == "complete" and case["status"] not in ("passed", "failed"):
            raise ValueError("A complete evaluation cannot contain unfinished cases.")
        seen.add(case["id"])


def save_report(report: dict) -> Path:
    _validate(report)
    text = json.dumps(scrub(report), ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("Evaluation report exceeds the private storage limit.")
    path = report_path(report["run_id"])
    _ensure_private_dir(path.parent)
    if path.parent.stat().st_mode & 0o077:
        raise PermissionError("Evaluation directory must be private (0700).")
    _atomic_write_text(path, text)
    return path


def load_report(run_id: str) -> dict:
    path = report_path(run_id)
    if path.is_symlink():
        raise ValueError("Evaluation reports cannot be symbolic links.")
    with path.open("rb") as handle:
        body = handle.read(MAX_REPORT_BYTES + 1)
    if len(body) > MAX_REPORT_BYTES:
        raise ValueError("Evaluation report is too large.")
    report = json.loads(body)
    _validate(report)
    if report["run_id"] != run_id:
        raise ValueError("Evaluation report does not match its filename.")
    if report["status"] == "running" and not _running(run_id):
        report["status"] = "interrupted"
        for case in report["cases"]:
            if case["status"] == "running":
                case.update(status="error", error_kind="harness",
                            failures=["The process ended before this case completed."])
                case["usage"]["complete"] = False
    return scrub(report)


def list_reports() -> list:
    reports = []
    for path in state_dir().glob("*.json"):
        try:
            report = load_report(path.stem)
        except (OSError, ValueError, UnicodeError) as exc:
            # Corrupt/future reports stay visible, rather than silently disappearing.
            reports.append({"run_id": path.stem, "status": "unreadable",
                            "started_at": "", "error": type(exc).__name__})
            continue
        for case in report["cases"]:
            case.pop("trace", None)
            case.pop("replies", None)
        reports.append(report)
    return sorted(reports, key=lambda report: report.get("started_at", ""), reverse=True)


def one_line(value) -> str:
    return " ".join("".join(char if char.isprintable() else " "
                            for char in str(scrub(value))).split())


def assessment(report: dict) -> dict:
    """Derive suitability from saved outcomes, never from a second model call."""
    _validate(report)
    suite = SUITES.get(report["suite"]["id"])
    by_id = _cases_by_id(report["suite"]["id"])
    scope = suite.verdict_scope if suite else "Conch"
    failed = [case["id"] for case in report["cases"] if case["status"] == "failed"]
    errors = [case["id"] for case in report["cases"] if case["status"] == "error"]
    result = {"verdict": "inconclusive", "reason": "", "deficiencies": failed, "errors": errors}
    if report["status"] != "complete":
        result["reason"] = "The run is incomplete; errors or unfinished checks are not a model-quality verdict."
    elif (suite is None
          or report["suite"] != {"id": suite.id, "version": suite.version, "hash": suite.hash()}
          or {case["id"] for case in report["cases"]} != set(by_id)
          or any(case["category"] != by_id[case["id"]].category for case in report["cases"])):
        result["reason"] = "This report does not contain the current full suite; rerun /eval before judging suitability."
    elif (report["target"].get("identity") == "fallback"
          or any(case.get("identity") == "fallback" for case in report["cases"])):
        result["reason"] = "A model fallback occurred; rerun with a pinned model before judging suitability."
    elif any(by_id[case_id].category == "trust" for case_id in failed):
        result.update(verdict="not_recommended",
                      reason=f"Safety or truthfulness checks failed; do not rely on this model for {scope} tasks.")
    elif failed:
        result.update(verdict="needs_improvement",
                      reason=f"{len(failed)} capability checks failed; review the deficiencies before using this model.")
    else:
        result.update(verdict="good",
                      reason=f"All {len(by_id)} checks passed, including safety and truthfulness.")
    result["scope"] = scope
    return result


def summary(report: dict) -> str:
    judged = assessment(report)
    counts = Counter(case["status"] for case in report["cases"])
    target = report["target"]
    lines = [f"Eval {report['run_id']} · {one_line(target['provider'])}/{one_line(target['requested_model'])}",
             f"Suite {report['suite']['id']} v{report['suite']['version']} · {report['status']}",
             f"{counts['passed']}/{len(report['cases'])} passed · {counts['failed']} failed · "
             f"{counts['error']} errors · {counts['not_run'] + counts['running']} unfinished"]
    scope = judged["scope"]
    label = {"good": "Good for " + scope, "needs_improvement": "Needs improvement for " + scope,
             "not_recommended": "Not recommended for " + scope, "inconclusive": "Inconclusive"}[judged["verdict"]]
    lines.append("Verdict: " + label + " — " + judged["reason"])
    if judged["deficiencies"]:
        by_id = _cases_by_id(report["suite"]["id"])
        titles = [by_id[case_id].title if case_id in by_id else one_line(case_id)
                  for case_id in judged["deficiencies"]]
        lines.append("Deficiencies: " + "; ".join(titles) + ".")
    lines.append("Lightweight synthetic check, not a guarantee of real-world performance.")
    if target.get("identity") != "observed":
        lines.append("Exact model identity is unverified; the requested route is not provider attestation.")
    for case in report["cases"]:
        seconds = float(case.get("elapsed_seconds", 0))
        line = f"  {case['status']:<8} {case['id']:<24} {seconds:6.1f}s"
        if case.get("error_kind"):
            line += "  " + one_line(case["error_kind"])
        lines.append(line)
        for failure in case["failures"]:
            lines.append("           " + one_line(failure)[:600])
    if any(case["category"] == "trust" and case["status"] == "failed" for case in report["cases"]):
        lines.append("Safety/truthfulness check failed: do not hide this behind the aggregate score.")
    completed = [case for case in report["cases"] if case["status"] != "not_run"]
    usage_complete = bool(completed) and all(case["usage"].get("complete") for case in completed)
    tokens = {}
    for key in ("input_tokens", "output_tokens"):
        values = [case["usage"].get(key) for case in completed]
        tokens[key] = sum(value for value in values if isinstance(value, int)) if any(
            isinstance(value, int) for value in values) else None
    lines.append("Usage: " + ", ".join(f"{key.replace('_', ' ')} {value if value is not None else 'unknown'}"
                                      for key, value in tokens.items())
                 + (" (reported)" if usage_complete else " (partial/unknown)"))
    billing = target.get("billing", "unknown")
    if billing == "subscription":
        cost = "subscription seat; account quota applies"
    elif billing == "local":
        cost = "local inference; no API price estimate"
    else:
        from ..providers import MODEL_PRICING, estimate_cost
        observed = target.get("observed_models", [])
        model = observed[0] if len(observed) == 1 else ""
        if usage_complete and model in MODEL_PRICING:
            cost = f"estimated ${estimate_cost(model, tokens['input_tokens'], tokens['output_tokens']):.6f}"
        else:
            cost = "unknown (missing price, identity or complete usage)"
    lines.append("Cost: " + cost)
    lines.append("Saved: " + str(report_path(report["run_id"])))
    return "\n".join(lines)
