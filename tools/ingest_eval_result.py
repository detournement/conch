#!/usr/bin/env python3
"""Trusted default-branch issue ingester. Issue text is data, never executable.

Run only from the eval-results GitHub workflow, with its scoped GITHUB_TOKEN.
The Git Data API creates an orphan data branch and compare-and-swap updates;
no checkout, shell, submitted path, label or contributor write access is used.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

# Executing this trusted script does not require installing the application or deps.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conch.evals.public import canonical_json, payload_hash  # noqa: E402
from conch.evals.sharing import (  # noqa: E402
    API_ROOT, DATA_BRANCH, MAX_ENVELOPE_BYTES, RECEIPT_MARKER, REJECTION_REASONS,
    REPOSITORY, SUBMISSION_MARKER, GitHubAPI, GitHubError, SharingError, _positive_id,
    dataset_path, parse_submission, read_dataset_at, validate_record,
)

MAX_COMMIT_ATTEMPTS = 6


class Rejected(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(REJECTION_REASONS[reason])


def _sha(response: dict) -> str:
    value = response.get("sha") if isinstance(response, dict) else None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise SharingError("GitHub returned an invalid Git object")
    return value


def publish_record(api: GitHubAPI, payload: dict, *, author_id: int, issue_number: int,
                   workflow_run_id: int) -> tuple:
    """Return (immutable URL, duplicate). Never overwrite an author/run record."""
    record = validate_record({"record_version": 1, "payload_sha256": payload_hash(payload),
                              "report": payload, "source": {"repository": REPOSITORY,
                              "issue_number": issue_number, "author_id": author_id,
                              "workflow_run_id": workflow_run_id}})
    path = dataset_path(payload, author_id)
    content = (canonical_json(record) + "\n").encode()
    if len(content) > MAX_ENVELOPE_BYTES:
        raise Rejected("invalid_payload")
    blob = None
    for _attempt in range(MAX_COMMIT_ATTEMPTS):
        head = None
        try:
            ref = api.request("GET", API_ROOT + "/git/ref/heads/" + DATA_BRANCH)
            head = _sha(ref.get("object", {}) if isinstance(ref, dict) else {})
        except GitHubError as exc:
            if exc.status != 404:
                raise
        base_tree = None
        if head is not None:
            found = read_dataset_at(api, payload, author_id, head)
            if found is not None:
                existing, url = found
                if existing["payload_sha256"] != record["payload_sha256"] or existing["report"] != record["report"]:
                    raise Rejected("conflicting_report")
                return url, True
            commit = api.request("GET", API_ROOT + "/git/commits/" + head)
            base_tree = _sha(commit.get("tree", {}) if isinstance(commit, dict) else {})
        if blob is None:
            blob = _sha(api.request("POST", API_ROOT + "/git/blobs", {
                "content": base64.b64encode(content).decode("ascii"), "encoding": "base64"}))
        tree_data = {"tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob}]}
        if base_tree is not None:
            tree_data["base_tree"] = base_tree
        tree = _sha(api.request("POST", API_ROOT + "/git/trees", tree_data))
        commit = _sha(api.request("POST", API_ROOT + "/git/commits", {
            "message": "Record self-reported Conch evaluation", "tree": tree,
            "parents": [head] if head is not None else [],
        }))
        try:
            if head is None:
                api.request("POST", API_ROOT + "/git/refs", {"ref": "refs/heads/" + DATA_BRANCH, "sha": commit})
            else:
                api.request("PATCH", API_ROOT + "/git/refs/heads/" + DATA_BRANCH, {"sha": commit, "force": False})
        except GitHubError as exc:
            # Both a racing orphan bootstrap and a non-fast-forward update retry
            # from the new head, rebuilding the tree so no accepted row is lost.
            if exc.status in (409, 422):
                continue
            raise
        return "https://github.com/%s/blob/%s/%s" % (REPOSITORY, commit, path), False
    raise SharingError("Concurrent dataset updates prevented publication; retry the retained issue.")


def _receipt(api: GitHubAPI, issue_number: int, payload, *, status: str, workflow_run_id: int,
             attempt_id: str = None, reason: str = "", dataset_url: str = "", duplicate: bool = False):
    receipt = {"receipt_version": 1, "status": status, "workflow_run_id": workflow_run_id, "attempt_id": attempt_id,
               "run_id": payload["run_id"] if payload is not None else None,
               "payload_sha256": payload_hash(payload) if payload is not None else None}
    if status == "accepted":
        receipt.update(dataset_url=dataset_url, duplicate=duplicate)
    else:
        receipt.update(reason=reason, detail=REJECTION_REASONS[reason])
    api.request("POST", API_ROOT + "/issues/%d/comments" % issue_number,
                {"body": RECEIPT_MARKER + canonical_json(receipt)})


def ingest_issue(api: GitHubAPI, issue_number: int, *, workflow_run_id: int) -> dict:
    if not _positive_id(issue_number) or not _positive_id(workflow_run_id):
        raise ValueError("Invalid trusted ingestion IDs")
    # Fetch from GitHub instead of trusting author IDs or stale bodies in envelopes.
    issue = api.request("GET", API_ROOT + "/issues/%d" % issue_number)
    if not isinstance(issue, dict) or issue.get("number") != issue_number or issue.get("pull_request"):
        raise ValueError("Expected a GitHub issue")
    body, title = issue.get("body"), issue.get("title")
    if not ((isinstance(body, str) and body.startswith(SUBMISSION_MARKER))
            or (isinstance(title, str) and title.startswith("Conch eval:"))):
        return {"status": "ignored"}
    payload = None
    attempt_id = None
    reason = ""
    try:
        try:
            submission = parse_submission(body)
            payload, attempt_id = submission["report"], submission["attempt_id"]
        except ValueError as exc:
            raise Rejected("invalid_payload") from exc
        author = issue.get("user", {})
        if not isinstance(author, dict) or not _positive_id(author.get("id")) or author.get("type") != "User":
            raise Rejected("invalid_submitter")
        url, duplicate = publish_record(api, payload, author_id=author["id"],
                                        issue_number=issue_number, workflow_run_id=workflow_run_id)
    except Rejected as exc:
        reason = exc.reason
    except (SharingError, ValueError):
        reason = "storage_unavailable"
    if reason:
        # Never delete or close failures: this is a durable inbox and dispatch can retry.
        _receipt(api, issue_number, payload, status="rejected", workflow_run_id=workflow_run_id, attempt_id=attempt_id, reason=reason)
        return {"status": "rejected", "reason": reason}
    _receipt(api, issue_number, payload, status="accepted", workflow_run_id=workflow_run_id,
             attempt_id=attempt_id, dataset_url=url, duplicate=duplicate)
    api.request("PATCH", API_ROOT + "/issues/%d" % issue_number, {"state": "closed", "state_reason": "completed"})
    return {"status": "accepted", "dataset_url": url, "duplicate": duplicate}


def main() -> int:
    try:
        if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or os.environ.get("GITHUB_ACTIONS") != "true":
            raise ValueError("Ingestion must run in the trusted repository workflow")
        event_path = Path(os.environ["GITHUB_EVENT_PATH"])
        with event_path.open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Oversized GitHub event")
        event = json.loads(raw)
        if event.get("repository", {}).get("full_name") != REPOSITORY:
            raise ValueError("Untrusted event repository")
        default_ref = "refs/heads/" + event["repository"]["default_branch"]
        if os.environ.get("GITHUB_REF") != default_ref:
            raise ValueError("Ingestion only runs on the default branch")
        name = os.environ.get("GITHUB_EVENT_NAME")
        if name == "issues" and event.get("action") in ("opened", "edited", "reopened"):
            number = event["issue"]["number"]
        elif name == "workflow_dispatch":
            text = event.get("inputs", {}).get("issue_number", "")
            if not isinstance(text, str) or re.fullmatch(r"[1-9][0-9]{0,12}", text) is None:
                raise ValueError("Invalid retry issue number")
            number = int(text)
        else:
            raise ValueError("Unsupported ingestion event")
        run_text = os.environ.get("GITHUB_RUN_ID", "")
        if re.fullmatch(r"[1-9][0-9]{0,18}", run_text) is None:
            raise ValueError("Invalid workflow run ID")
        outcome = ingest_issue(GitHubAPI(), number, workflow_run_id=int(run_text))
        # Only controlled status/reason enums go to logs, never issue or provider text.
        print("Evaluation ingestion: " + outcome["status"] + (" (" + outcome["reason"] + ")" if "reason" in outcome else ""))
        return 0
    except (KeyError, TypeError, ValueError, OSError, SharingError):
        print("Evaluation ingestion failed. The issue is retained; inspect workflow permissions and retry.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
