"""Credential detection for the persistent memory tiers.

The mission-lesson consolidation gate (conch/kernel/consolidate.py) already
rejects anything token-shaped from *mission lessons*. This module is the
same discipline for the interactive memory store: `save_memory` tool
writes, `/remember`, and auto session summaries must never persist
credential material, and retrieval must never surface a legacy entry that
predates the gate.

Detection is value-bearing, not word-bearing: mentioning "the API key is
in 1Password" is fine; "API key: <blob>" is not. Rules (labels are what
reports show — values never leave this module):

- well-known token shapes: JWTs, Atlassian ``ATATT`` tokens, AWS access
  key ids, OpenAI/Anthropic ``sk-``, Slack ``xox*``, GitHub/GitLab PATs,
  Google ``AIza``, Capitol ``cap_a2a_`` bearers, private key blocks
  (the same battery scripts/check-secrets.sh uses);
- assignment style: an auth word (password/token/api key/secret/...)
  directly assigned a non-placeholder value;
- generic high entropy near an auth word: a long digit-bearing
  mixed-class token within a small window of an auth word (this is what
  catches pasted Vercel-style tokens, which have no distinctive prefix —
  the label is refined to ``vercel_token`` when the word "vercel" is in
  the window).

Whole-entry rejection at write time, never sanitization — matching the
consolidation gate. Redaction exists only for the operator scrub flow
(``redact_credentials``), which replaces flagged spans and re-scans to a
fixpoint.
"""

from __future__ import annotations

import math
import re
from typing import List, Tuple

#: Simple, high-confidence token shapes. Labels are stable identifiers:
#: they appear in tool block messages, logs, and scrub reports.
_TOKEN_RULES = [
    ("private_key_block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]+)?")),
    ("atlassian_api_token", re.compile(r"\bATATT[A-Za-z0-9_\-=+/]{15,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("slack_token", re.compile(r"\bxox[baprsoe]-[0-9A-Za-z\-]{5,}")),
    ("github_token", re.compile(
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"
        r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("capitol_a2a_bearer", re.compile(r"\bcap_a2a_[A-Za-z0-9_\-]{4,}\b")),
]

#: Auth words that make a nearby or assigned value credential material.
_AUTH_WORD = (
    r"(?:pass(?:word|wd|code|phrase)?|pwd|api[ _-]?key|apikey|"
    r"access[ _-]?key|secret|token|bearer|credentials?|"
    r"private[ _-]?key|client[ _-]?secret|auth[ _-]?token)"
)

#: ``password: hunter2`` / ``api key = abcd1234`` — an auth word directly
#: assigned a value. The value group is what gets redacted.
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b" + _AUTH_WORD + r"\b[ \t]*[:=]+[ \t]*(\S{4,})"
)

#: Values that are placeholders, references, or prose rather than secrets:
#: bracketed/quoted markers, env-var style, or a leading stopword ("the
#: one in 1Password", "stored in keychain", "none", "not configured").
_PLACEHOLDER_VALUE_RE = re.compile(
    r"""(?ix)^(?:
        [\[<({*'"`]                                   # [redacted], <YOUR_..>
      | \$ | % | ~                                    # $ENV_VAR, %VAR%, ~path
      | x{4,} | \*{2,} | \.{3}                        # xxxx, ***, ...
      | (?:none|null|nil|unset|empty|missing|redacted|removed|hidden|
         masked|omitted|n/?a|yes|no|true|false|ok|set|configured|
         required|expired|revoked|rotated|invalid|unknown|tbd|todo|
         pending|example|placeholder|sample|dummy|fake|test|
         your[_a-z]*|my|the|an?|this|that|it|its|in|on|from|via|per|
         see|use[ds]?|check|ask|stored?|saved?|lives?|kept)\b
    )"""
)

#: Candidate for the entropy rule: long, unbroken, digit-bearing token.
_LONG_TOKEN_RE = re.compile(
    r"(?=[A-Za-z0-9_\-+/=]*\d)[A-Za-z0-9_\-+/=]{20,}"
)
_AUTH_WORD_NEAR_RE = re.compile(r"(?i)\b" + _AUTH_WORD + r"\b|\bauth\b")
_VERCEL_NEAR_RE = re.compile(r"(?i)\bvercel\b")

#: Window (chars, each direction) an auth word must fall in for the
#: generic entropy rule to treat a long token as credential material.
_NEAR_WINDOW = 100
_ENTROPY_THRESHOLD = 3.5


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    total = float(len(text))
    return -sum(
        (n / total) * math.log2(n / total) for n in counts.values()
    )


def _char_classes(token: str) -> int:
    return sum((
        bool(re.search(r"[a-z]", token)),
        bool(re.search(r"[A-Z]", token)),
        bool(re.search(r"\d", token)),
        bool(re.search(r"[_\-+/=]", token)),
    ))


def _scan(text: str) -> List[Tuple[str, int, int]]:
    """All findings as ``(label, start, end)`` spans. Spans cover the
    secret value (assignment matches redact the value, not the label)."""
    findings: List[Tuple[str, int, int]] = []
    for label, pattern in _TOKEN_RULES:
        for match in pattern.finditer(text):
            findings.append((label, match.start(), match.end()))
    for match in _ASSIGNMENT_RE.finditer(text):
        value = match.group(1)
        if _PLACEHOLDER_VALUE_RE.match(value):
            continue
        findings.append(("secret_assignment", match.start(1), match.end(1)))
    for match in _LONG_TOKEN_RE.finditer(text):
        token = match.group(0)
        if _char_classes(token) < 2:
            continue
        if _shannon_entropy(token) < _ENTROPY_THRESHOLD:
            continue
        window = text[max(0, match.start() - _NEAR_WINDOW):
                      match.end() + _NEAR_WINDOW]
        if not _AUTH_WORD_NEAR_RE.search(window):
            continue
        label = (
            "vercel_token" if _VERCEL_NEAR_RE.search(window)
            else "high_entropy_near_auth_word"
        )
        findings.append((label, match.start(), match.end()))
    return findings


def credential_findings(text: str) -> List[str]:
    """Distinct credential-type labels found in *text* (ordered by first
    occurrence), or an empty list when it is clean. Labels only — the
    matched values never leave this module."""
    labels: List[str] = []
    for label, _start, _end in sorted(_scan(str(text or "")),
                                      key=lambda f: (f[1], f[2])):
        if label not in labels:
            labels.append(label)
    return labels


def redact_credentials(text: str, marker: str,
                       max_passes: int = 5) -> Tuple[str, List[str]]:
    """Replace every flagged span with *marker*, re-scanning to a fixpoint
    (a redacted token can leave a neighboring one newly detectable — e.g.
    windowed matches). Returns ``(redacted_text, labels)``. For the
    operator scrub flow only; write paths reject whole entries instead."""
    text = str(text or "")
    labels: List[str] = []
    for _ in range(max_passes):
        findings = [
            f for f in _scan(text) if marker not in text[f[1]:f[2]]
        ]
        if not findings:
            break
        for label, _start, _end in sorted(findings, key=lambda f: f[1]):
            if label not in labels:
                labels.append(label)
        spans: List[List[int]] = []
        for _label, start, end in sorted(findings, key=lambda f: f[1]):
            if spans and start <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], end)
            else:
                spans.append([start, end])
        for start, end in reversed(spans):
            text = text[:start] + marker + text[end:]
    return text, labels


class CredentialRejected(ValueError):
    """A memory write was refused because the content matched credential
    detection. Carries type labels only, never the matched values."""

    def __init__(self, types: List[str]):
        self.types = list(types)
        super().__init__(
            "content matches credential pattern(s): " + ", ".join(self.types)
        )
