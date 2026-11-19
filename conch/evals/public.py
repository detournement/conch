"""Allowlisted, bounded public measurements; never a redacted private report."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from typing import Optional

from conch.secretguard import credential_findings

MAX_PUBLIC_BYTES = 32768
CASE_CATEGORIES = {
    "shell-quoting": "shell", "explain-only": "shell",
    "tool-arguments": "tools", "dependent-lookup": "tools",
    "surgical-edit": "files", "outside-cwd-save": "files",
    "grounded-answer": "grounding", "data-calculation": "grounding",
    "tool-error-recovery": "recovery", "follow-up-change": "recovery",
    "prompt-injection": "trust", "denied-write-honesty": "trust",
}
# Provider names are public adapter identities, never endpoint or deployment IDs.
_CLOUD_PROVIDERS = ("anthropic", "bedrock", "cerebras", "openai", "openrouter")
PUBLIC_PROVIDERS = _CLOUD_PROVIDERS + ("ollama", "custom", "claude-code", "codex")
_IDENTITIES = ("observed", "unverified", "fallback")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(validate_public_report(payload)).encode()).hexdigest()


def _object(value: object, keys: set, name: str) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("Invalid " + name + " fields")
    return value


def _integer(value: object, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Invalid " + name)
    return value


def _choice(value: object, choices: tuple, name: str) -> str:
    if type(value) is not str or value not in choices:
        raise ValueError("Invalid " + name)
    return value


def _pattern(value: object, pattern: str, name: str) -> str:
    if type(value) is not str or re.fullmatch(pattern, value, flags=re.ASCII) is None:
        raise ValueError("Invalid " + name)
    return value


def _seconds(value: object) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 86400 or not math.isfinite(value):
        raise ValueError("Invalid elapsed time")
    return round(float(value), 6)


def _usage(value: object) -> dict:
    value = _object(value, {"input_tokens", "output_tokens", "complete"}, "usage")
    if type(value["complete"]) is not bool:
        raise ValueError("Invalid usage completeness")
    for key in ("input_tokens", "output_tokens"):
        if value[key] is not None:
            _integer(value[key], 0, 1_000_000_000, key)
        elif value["complete"]:
            raise ValueError("Complete usage must contain both token counts")
    return dict(value)


def _totals(cases: list) -> dict:
    usage = {}
    for key in ("input_tokens", "output_tokens"):
        counts = [case["usage"][key] for case in cases]
        usage[key] = sum(counts) if all(n is not None for n in counts) else None
    usage["complete"] = all(case["usage"]["complete"] for case in cases)
    return {"elapsed_seconds": round(sum(c["elapsed_seconds"] for c in cases), 6),
            "calls": sum(c["calls"] for c in cases), "usage": usage}


def default_public_label(report: dict) -> Optional[str]:
    target = report.get("target", {})
    if not isinstance(target, dict):
        return None
    provider, model = target.get("provider"), target.get("requested_model")
    if provider in _CLOUD_PROVIDERS and isinstance(model, str):
        # Keep schema validation/ingestion stdlib-only. The app's authoritative
        # catalog is needed only when the local UI chooses a default label.
        from conch.providers import KNOWN_MODELS

        if model in KNOWN_MODELS.get(provider, ()):
            # Known gateway namespaces are public, but slashes and hostname-like
            # punctuation are deliberately not permitted in arbitrary labels.
            return re.sub(r"(?<![0-9])\.|\.(?![0-9])|/", " ", model)
    return None


def validate_public_report(payload: dict) -> dict:
    """Reject unknown keys, unbounded values and credentials; return canonical data."""
    try:
        encoded = canonical_json(payload)
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ValueError("Public report must be finite JSON") from exc
    if len(encoded.encode()) > MAX_PUBLIC_BYTES:
        raise ValueError("Public report exceeds size limit")
    p = _object(payload, {"schema_version", "suite", "run_id", "conch_version", "status",
                          "model", "settings", "limits", "cases", "totals", "self_reported"},
                "public report")
    _integer(p["schema_version"], 1, 1, "schema version")
    if p["self_reported"] is not True:
        raise ValueError("Public measurements must be marked self-reported")
    suite = _object(p["suite"], {"id", "version", "hash"}, "suite")
    _choice(suite["id"], ("conch-lite",), "suite ID")
    _integer(suite["version"], 1, 1, "suite version")
    _pattern(suite["hash"], r"[0-9a-f]{64}", "suite hash")
    run_id = _pattern(p["run_id"], r"[0-9a-f-]{36}", "run ID")
    try:
        parsed_id = uuid.UUID(run_id)
    except ValueError as exc:
        raise ValueError("Invalid run ID") from exc
    if parsed_id.version != 4 or str(parsed_id) != run_id:
        raise ValueError("Run ID must be a canonical UUID4")
    _pattern(p["conch_version"], r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:(?:a|b|rc)[0-9]{1,4})?", "Conch version")
    _choice(p["status"], ("running", "complete", "cancelled", "timed_out", "error", "interrupted"), "run status")
    model = _object(p["model"], {"provider", "label", "identity", "billing"}, "public model")
    _choice(model["provider"], PUBLIC_PROVIDERS, "model provider")
    _pattern(model["label"], r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", "public model label")
    if re.search(r"(?<![0-9])\.|\.(?![0-9])|\b[0-9]{1,3}(?:\.[0-9]{1,3}){3}\b|(?:https?|www)[ ._-]", model["label"], re.I):
        raise ValueError("Public model label must not contain paths or endpoints")
    _choice(model["identity"], _IDENTITIES, "model identity")
    _choice(model["billing"], ("api", "subscription", "local", "unknown"), "billing type")
    settings = p["settings"]
    if type(settings) is not dict or not set(settings) <= {"reasoning_effort", "context_length"}:
        raise ValueError("Invalid effective settings")
    if "reasoning_effort" in settings:
        _choice(settings["reasoning_effort"], ("none", "minimal", "low", "medium", "high", "xhigh", "max", "auto", "off", "on"), "reasoning effort")
    if "context_length" in settings:
        _integer(settings["context_length"], 1, 100_000_000, "context length")
    limits = _object(p["limits"], {"model_calls_per_case", "tool_calls_per_case", "deadline_seconds", "max_output_tokens"}, "limits")
    _integer(limits["model_calls_per_case"], 4, 4, "model call limit")
    _integer(limits["tool_calls_per_case"], 12, 12, "tool call limit")
    _integer(limits["deadline_seconds"], 1, 86400, "deadline")
    _integer(limits["max_output_tokens"], 1, 65536, "output limit")
    if type(p["cases"]) is not list or len(p["cases"]) != len(CASE_CATEGORIES):
        raise ValueError("Public report must contain all twelve cases")
    cases = []
    for row, (case_id, category) in zip(p["cases"], CASE_CATEGORIES.items()):
        row = _object(row, {"id", "category", "status", "failure_count", "elapsed_seconds", "calls", "usage", "identity"}, "case")
        _choice(row["id"], (case_id,), "case order or ID")
        _choice(row["category"], (category,), "case category")
        _choice(row["status"], ("not_run", "running", "passed", "failed", "error"), "case status")
        _integer(row["failure_count"], 0, 100, "failure count")
        _integer(row["calls"], 0, 4, "case calls")
        _choice(row["identity"], _IDENTITIES, "case identity")
        if row["status"] in ("passed", "not_run") and row["failure_count"]:
            raise ValueError("Unfailed case has assertion failures")
        if row["status"] == "failed" and not row["failure_count"]:
            raise ValueError("Failed case must have an assertion failure")
        if row["status"] == "not_run" and (row["calls"] or row["elapsed_seconds"]):
            raise ValueError("Unrun case has execution metrics")
        if p["status"] == "complete" and row["status"] not in ("passed", "failed"):
            raise ValueError("Complete report has unfinished cases")
        cases.append(dict(row, elapsed_seconds=_seconds(row["elapsed_seconds"]), usage=_usage(row["usage"])))
    totals = _totals(cases)
    incoming = _object(p["totals"], {"elapsed_seconds", "calls", "usage"}, "totals")
    _integer(incoming["calls"], 0, 48, "total calls")
    _usage(incoming["usage"])
    if _seconds(incoming["elapsed_seconds"]) != totals["elapsed_seconds"] or incoming["calls"] != totals["calls"] or incoming["usage"] != totals["usage"]:
        raise ValueError("Aggregate metrics do not match cases")
    result = dict(p, suite=dict(suite), model=dict(model), settings=dict(settings),
                  limits=dict(limits), cases=cases, totals=totals)
    if credential_findings(canonical_json(result)):
        raise ValueError("Public report contains possible credentials; choose a safe public label")
    return result


def public_report(report: dict, *, model_label: Optional[str] = None) -> dict:
    label = default_public_label(report) if model_label is None else model_label
    if label is None:
        raise ValueError("This model needs an explicit public label; its private identifier will not be shared")
    try:
        target = report["target"]
        cases = [{"id": row["id"], "category": row["category"], "status": row["status"],
                  "failure_count": len(row["failures"]), "elapsed_seconds": _seconds(row["elapsed_seconds"]),
                  "calls": row["calls"], "usage": _usage(row["usage"]), "identity": row["identity"]}
                 for row in report["cases"]]
        payload = {"schema_version": 1, "suite": {k: report["suite"][k] for k in ("id", "version", "hash")},
                   "run_id": report["run_id"], "conch_version": report["conch_version"], "status": report["status"],
                   "model": {"provider": target["provider"], "label": label,
                             "identity": target["identity"], "billing": target["billing"]},
                   "settings": {k: v for k, v in target["settings"].items() if k in ("reasoning_effort", "context_length")},
                   "limits": {k: report["limits"][k] for k in ("model_calls_per_case", "tool_calls_per_case", "deadline_seconds", "max_output_tokens")},
                   "cases": cases, "totals": _totals(cases), "self_reported": True}
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Local report does not match the evaluation schema") from exc
    return validate_public_report(payload)
