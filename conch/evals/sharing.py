"""Explicit GitHub issue submission and verified dataset acceptance.

Importing this module, exporting a report and browsing local reports do no I/O.
Only the UI calls these entry points, after its default-no public preview.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

from .public import MAX_PUBLIC_BYTES, canonical_json, payload_hash, validate_public_report

REPOSITORY = "detournement/conch"
DATA_BRANCH = "eval-results"
WORKFLOW_PATH = ".github/workflows/eval-results.yml"
SUBMISSION_MARKER = "<!-- conch-eval-submission:v1 -->\n"
RECEIPT_MARKER = "<!-- conch-eval-receipt:v1 -->\n"
MAX_ENVELOPE_BYTES = MAX_PUBLIC_BYTES + 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ACTIONS_BOT_ID = 41898282
API_ROOT = "/repos/" + REPOSITORY
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
REJECTION_REASONS = {
    "invalid_payload": "The submission is not a valid bounded public evaluation report.",
    "invalid_submitter": "The GitHub issue must have a human GitHub author.",
    "conflicting_report": "Different content is already stored for this author and run ID.",
    "storage_unavailable": "Dataset publication failed; the issue is retained for retry.",
}


class SharingError(RuntimeError):
    """Actionable, credential-free sharing failure."""


class GitHubError(SharingError):
    def __init__(self, status: int = 0):
        self.status = status
        if status in (401, 403):
            message = "GitHub authentication or permission denied. Check gh auth status --hostname github.com; no login was attempted."
        elif status == 404:
            message = "GitHub resource is unavailable (404)."
        else:
            message = "GitHub request failed" + (" (HTTP %d)." % status if status else ". Check connectivity and gh authentication.")
        super().__init__(message)


def _json_load(text: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("Non-finite JSON value")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (ValueError, RecursionError, TypeError) as exc:
        raise ValueError("Invalid JSON document") from exc


def _run_gh(args: list, data: bytes = None, *, token: str = None) -> tuple:
    """Bound both wall time and combined output, without shell or credential argv."""
    executable = shutil.which("gh")
    if not executable:
        raise SharingError("Install GitHub CLI (gh) to share later; the local report is unchanged.")
    env = dict(os.environ, GH_HOST="github.com", GH_PROMPT_DISABLED="1", GH_PAGER="cat",
               GH_NO_UPDATE_NOTIFIER="1", GIT_TERMINAL_PROMPT="0", NO_COLOR="1")
    if token is not None:
        # GH_TOKEN wins over gh's mutable active account. Do not forward a
        # competing token or put the frozen credential in argv, logs or files.
        env["GH_TOKEN"] = token
        env.pop("GITHUB_TOKEN", None)
    try:
        process = subprocess.Popen([executable] + args, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    except OSError as exc:
        raise SharingError("Unable to start GitHub CLI; the local report is unchanged.") from exc
    output = bytearray()
    oversized = threading.Event()

    def receive():
        while True:
            chunk = process.stdout.read(8192)
            if not chunk:
                break
            if len(output) + len(chunk) > MAX_RESPONSE_BYTES:
                oversized.set()
                process.kill()
                break
            output.extend(chunk)

    def send():
        try:
            process.stdin.write(data)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    reader = threading.Thread(target=receive, daemon=True)
    reader.start()
    writer = None
    if data is not None:
        writer = threading.Thread(target=send, daemon=True)
        writer.start()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise SharingError("GitHub request timed out. Retry explicitly; your local report is retained.") from exc
    finally:
        reader.join(timeout=2)
        if writer is not None:
            writer.join(timeout=2)
        process.stdout.close()
    if oversized.is_set() or reader.is_alive():
        raise SharingError("GitHub response exceeded the safe size limit.")
    return process.returncode, output.decode("utf-8", errors="replace")


class GitHubAPI:
    def __init__(self):
        # Snapshot credentials before checking /user, then bind every request
        # in this transaction to that same identity, even if gh auth switches.
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            code, token = _run_gh(["auth", "token", "--hostname", "github.com"])
            if code:
                raise SharingError("GitHub authentication is unavailable. Check gh auth status --hostname github.com; no login was attempted.")
        token = token.strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]{8,4096}", token) is None:
            raise SharingError("GitHub returned an invalid authentication token; no request was sent.")
        self._token = token

    def request(self, method: str, path: str, data=None):
        # Every caller supplies a fixed github.com API path, never a returned URL.
        if not path.startswith("/") or "\n" in path or "\r" in path:
            raise SharingError("Invalid GitHub API path")
        args = ["api", "--hostname", "github.com", "--method", method, "--include",
                "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28", path]
        raw = None
        if data is not None:
            raw = canonical_json(data).encode()
            if len(raw) > 2 * MAX_ENVELOPE_BYTES:
                raise SharingError("GitHub request exceeds size limit")
            args += ["--input", "-"]
        code, text = _run_gh(args, raw, token=self._token)
        match = re.search(r"^HTTP/\S+ (\d{3})[^\n]*\n", text, re.MULTILINE)
        status = int(match.group(1)) if match else 0
        if code or not 200 <= status < 300:
            raise GitHubError(status)
        content = re.split(r"\r?\n\r?\n", text[match.start():], maxsplit=1)
        if len(content) != 2:
            raise SharingError("GitHub returned an invalid response")
        if not content[1].strip():
            return None
        try:
            return _json_load(content[1])
        except ValueError as exc:
            raise SharingError("GitHub returned invalid JSON") from exc


def _positive_id(value) -> bool:
    return type(value) is int and 0 < value < 2 ** 63


def _identity(api: GitHubAPI) -> dict:
    user = api.request("GET", "/user")
    if not isinstance(user, dict) or not _positive_id(user.get("id")) or not isinstance(user.get("login"), str) or not _LOGIN.fullmatch(user["login"]) or user.get("type") != "User":
        raise SharingError("Sharing requires authenticated human GitHub identity on github.com.")
    return user


def github_identity() -> str:
    return _identity(GitHubAPI())["login"]


def issue_url(number: int) -> str:
    if not _positive_id(number):
        raise ValueError("Invalid issue number")
    return "https://github.com/%s/issues/%d" % (REPOSITORY, number)


def dataset_path(payload: dict, author_id: int) -> str:
    payload = validate_public_report(payload)
    if not _positive_id(author_id):
        raise ValueError("Invalid GitHub author ID")
    return "results/%d/%d/%s.json" % (payload["suite"]["version"], author_id, payload["run_id"])


def _attempt_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid submission attempt ID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError("Invalid submission attempt ID") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("Submission attempt ID must be a canonical UUID4")
    return value


def encode_envelope(payload: dict, *, attempt_id: str = None) -> str:
    payload = validate_public_report(payload)
    attempt = _attempt_id(str(uuid.uuid4()) if attempt_id is None else attempt_id)
    return SUBMISSION_MARKER + canonical_json({"envelope_version": 1, "attempt_id": attempt,
                                               "payload_sha256": payload_hash(payload), "report": payload})


def parse_submission(body: str) -> dict:
    if type(body) is not str or len(body.encode()) > MAX_ENVELOPE_BYTES or not body.startswith(SUBMISSION_MARKER):
        raise ValueError("Invalid or oversized submission envelope")
    envelope = _json_load(body[len(SUBMISSION_MARKER):])
    if type(envelope) is not dict or set(envelope) != {"envelope_version", "attempt_id", "payload_sha256", "report"} or type(envelope["envelope_version"]) is not int or envelope["envelope_version"] != 1:
        raise ValueError("Invalid submission envelope fields")
    _attempt_id(envelope["attempt_id"])
    payload = validate_public_report(envelope["report"])
    if envelope["payload_sha256"] != payload_hash(payload):
        raise ValueError("Submission checksum mismatch")
    return dict(envelope, report=payload)


def parse_envelope(body: str) -> dict:
    return parse_submission(body)["report"]


def validate_record(record: dict) -> dict:
    if type(record) is not dict or set(record) != {"record_version", "payload_sha256", "report", "source"} or type(record["record_version"]) is not int or record["record_version"] != 1:
        raise ValueError("Invalid dataset record")
    payload = validate_public_report(record["report"])
    source = record["source"]
    if type(source) is not dict or set(source) != {"repository", "issue_number", "author_id", "workflow_run_id"} or source["repository"] != REPOSITORY:
        raise ValueError("Invalid dataset provenance")
    if not all(_positive_id(source[key]) for key in ("issue_number", "author_id", "workflow_run_id")):
        raise ValueError("Invalid dataset provenance IDs")
    if record["payload_sha256"] != payload_hash(payload):
        raise ValueError("Dataset checksum mismatch")
    return dict(record, report=payload, source=dict(source))


def read_dataset(api: GitHubAPI, payload: dict, author_id: int):
    """Pin reads to a commit, so concurrent branch updates cannot mix evidence."""
    try:
        ref = api.request("GET", API_ROOT + "/git/ref/heads/" + DATA_BRANCH)
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise
    commit = ref.get("object", {}).get("sha") if isinstance(ref, dict) else None
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise SharingError("Invalid dataset branch response")
    return read_dataset_at(api, payload, author_id, commit)


def read_dataset_at(api: GitHubAPI, payload: dict, author_id: int, commit: str):
    path = dataset_path(payload, author_id)
    try:
        entry = api.request("GET", API_ROOT + "/contents/" + path + "?ref=" + commit)
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise
    if not isinstance(entry, dict) or entry.get("type") != "file" or entry.get("path") != path or entry.get("encoding") != "base64" or type(entry.get("size")) is not int or not 0 < entry["size"] <= MAX_ENVELOPE_BYTES or type(entry.get("content")) is not str or len(entry["content"]) > MAX_ENVELOPE_BYTES * 2:
        raise SharingError("Invalid or oversized dataset file")
    try:
        raw = base64.b64decode("".join(entry["content"].splitlines()), validate=True)
        if len(raw) != entry["size"] or len(raw) > MAX_ENVELOPE_BYTES:
            raise ValueError("Dataset size mismatch")
        blob_sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if entry.get("sha") != blob_sha:
            raise ValueError("Dataset blob checksum mismatch")
        record = validate_record(_json_load(raw.decode("utf-8")))
        if record["source"]["author_id"] != author_id or record["report"]["run_id"] != payload["run_id"]:
            raise ValueError("Dataset ownership mismatch")
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise SharingError("Dataset content or provenance is invalid") from exc
    url = "https://github.com/%s/blob/%s/%s" % (REPOSITORY, commit, path)
    return record, url


def _trusted_run(api: GitHubAPI, run_id: int, *, require_success: bool = True) -> bool:
    if not _positive_id(run_id):
        return False
    run = api.request("GET", API_ROOT + "/actions/runs/%d" % run_id)
    repo = api.request("GET", API_ROOT)
    return (isinstance(run, dict) and isinstance(repo, dict) and run.get("id") == run_id
            and run.get("path") == WORKFLOW_PATH and run.get("event") in ("issues", "workflow_dispatch")
            and run.get("repository", {}).get("full_name") == REPOSITORY
            and run.get("head_repository", {}).get("full_name") == REPOSITORY
            and isinstance(repo.get("default_branch"), str) and run.get("head_branch") == repo["default_branch"]
            and run.get("status") == "completed"
            and (run.get("conclusion") == "success" if require_success else
                 run.get("conclusion") in ("success", "failure", "cancelled", "timed_out")))


def _verified_acceptance(api: GitHubAPI, payload: dict, author_id: int, found) -> bool:
    record, _url = found
    if record["payload_sha256"] != payload_hash(payload) or record["report"] != payload:
        raise SharingError("A different report is already published for this GitHub author and run ID.")
    source = record["source"]
    original = api.request("GET", API_ROOT + "/issues/%d" % source["issue_number"])
    try:
        source_payload = parse_envelope(original.get("body"))
    except (ValueError, AttributeError):
        return False
    return (original.get("user", {}).get("id") == author_id and not original.get("pull_request")
            # The immutable write may precede a failed comment/close request.
            # Its validated content and trusted producing run, not a comment
            # or the final housekeeping outcome, establish publication.
            and source_payload == payload
            and _trusted_run(api, source["workflow_run_id"], require_success=False))


def _metadata(issue: dict, payload: dict, *, author_id: int) -> dict:
    number = issue.get("number")
    if not _positive_id(number):
        raise SharingError("GitHub returned an invalid submission issue")
    try:
        submission = parse_submission(issue.get("body"))
    except ValueError as exc:
        raise SharingError("GitHub returned an invalid submission envelope") from exc
    if submission["report"] != payload:
        raise SharingError("GitHub returned a different submission payload")
    return {"status": "submitted", "issue_number": number, "issue_url": issue_url(number),
            "author_id": author_id, "attempt_id": submission["attempt_id"], "payload_sha256": payload_hash(payload),
            "submitted_at": datetime.now(timezone.utc).isoformat()}


def submit_report(payload: dict, *, identity: str) -> dict:
    """UI owns final confirmation; recheck account before any possible write."""
    payload = validate_public_report(payload)
    if not isinstance(identity, str) or not _LOGIN.fullmatch(identity):
        raise SharingError("An explicitly confirmed GitHub account is required.")
    api = GitHubAPI()
    user = _identity(api)
    if user["login"].lower() != identity.lower():
        raise SharingError("GitHub account changed since the preview. Preview again before sharing.")
    existing = read_dataset(api, payload, user["id"])
    if existing is not None:
        if not _verified_acceptance(api, payload, user["id"], existing):
            raise SharingError("An existing dataset record is not yet verified; refresh later instead of uploading again.")
        source_issue = api.request("GET", API_ROOT + "/issues/%d" % existing[0]["source"]["issue_number"])
        metadata = _metadata(source_issue, payload, author_id=user["id"])
        metadata.update(status="accepted", dataset_url=existing[1])
        return metadata
    title = "Conch eval: " + payload["run_id"]
    query = 'repo:%s is:issue author:%s in:title "%s"' % (REPOSITORY, user["login"], title)
    results = api.request("GET", "/search/issues?" + urlencode({"q": query, "per_page": 100}))
    if not isinstance(results, dict) or not isinstance(results.get("items"), list) or results.get("incomplete_results"):
        raise SharingError("Could not check previous submissions; retry explicitly later.")
    for issue in results["items"]:
        if issue.get("title") != title or issue.get("user", {}).get("id") != user["id"] or issue.get("pull_request"):
            continue
        try:
            old = parse_envelope(issue.get("body"))
        except ValueError:
            continue
        if old["run_id"] == payload["run_id"]:
            if old != payload:
                raise SharingError("A different submission already uses this run ID. Existing results will not be overwritten.")
            metadata = _refresh(api, _metadata(issue, payload, author_id=user["id"]))
            if metadata["status"] == "accepted":
                return metadata
            # A fresh nonce schedules `issues: edited` and makes earlier or
            # still-in-flight attempts' rejection receipts irrelevant.
            # Keep the issue and all failure comments; never require labels.
            body = encode_envelope(payload)
            updated = api.request("PATCH", API_ROOT + "/issues/%d" % issue["number"], {"body": body})
            if not isinstance(updated, dict) or updated.get("number") != issue["number"] or updated.get("user", {}).get("id") != user["id"]:
                raise SharingError("GitHub returned an inconsistent retry issue.")
            return _metadata(updated, payload, author_id=user["id"])
    issue = api.request("POST", API_ROOT + "/issues", {"title": title, "body": encode_envelope(payload)})
    if not isinstance(issue, dict):
        raise SharingError("GitHub returned an invalid submission issue")
    metadata = _metadata(issue, payload, author_id=user["id"])
    if issue.get("user", {}).get("id") != user["id"]:
        metadata.update(status="failed", reason="GitHub returned an inconsistent submission author. Inspect the issue before retrying.")
    return metadata


def _refresh(api: GitHubAPI, sharing: dict) -> dict:
    if not isinstance(sharing, dict) or not _positive_id(sharing.get("issue_number")) or not _positive_id(sharing.get("author_id")) or not isinstance(sharing.get("payload_sha256"), str) or not _HASH.fullmatch(sharing["payload_sha256"]):
        raise SharingError("Invalid saved sharing metadata")
    try:
        _attempt_id(sharing.get("attempt_id"))
    except ValueError as exc:
        raise SharingError("Invalid saved submission attempt") from exc
    number = sharing["issue_number"]
    issue = api.request("GET", API_ROOT + "/issues/%d" % number)
    try:
        payload = parse_envelope(issue.get("body"))
    except (ValueError, AttributeError) as exc:
        raise SharingError("The submission issue no longer contains a valid report.") from exc
    author = issue.get("user", {}).get("id")
    if author != sharing["author_id"] or issue.get("pull_request") or payload_hash(payload) != sharing["payload_sha256"]:
        raise SharingError("The submission issue author or content does not match the saved report.")
    result = {key: sharing[key] for key in ("issue_number", "author_id", "attempt_id", "payload_sha256", "submitted_at") if key in sharing}
    result.update(status="submitted", issue_url=issue_url(number))
    found = read_dataset(api, payload, author)
    if found is not None:
        try:
            accepted = _verified_acceptance(api, payload, author, found)
        except SharingError as exc:
            result.update(status="failed", reason=str(exc))
            return result
        if accepted:
            result.update(status="accepted", dataset_url=found[1])
            return result
    # No comment, even an Actions comment with a URL, proves acceptance.
    # Rejections must bind this payload to a successfully completed trusted job.
    for page in range(1, 11):
        comments = api.request("GET", API_ROOT + "/issues/%d/comments?per_page=100&page=%d" % (number, page))
        if not isinstance(comments, list):
            raise SharingError("GitHub returned invalid issue comments")
        for comment in reversed(comments):
            bot = comment.get("user", {})
            body = comment.get("body", "")
            if bot.get("id") != ACTIONS_BOT_ID or bot.get("type") != "Bot" or bot.get("login") != "github-actions[bot]" or not isinstance(body, str) or len(body) > 2048 or not body.startswith(RECEIPT_MARKER):
                continue
            try:
                receipt = _json_load(body[len(RECEIPT_MARKER):])
            except ValueError:
                continue
            if (not isinstance(receipt, dict) or receipt.get("status") != "rejected"
                    or receipt.get("run_id") != payload["run_id"] or receipt.get("payload_sha256") != sharing["payload_sha256"]
                    or receipt.get("attempt_id") != sharing["attempt_id"]
                    or receipt.get("reason") not in REJECTION_REASONS):
                continue
            if _trusted_run(api, receipt.get("workflow_run_id")):
                result.update(status="failed", reason=REJECTION_REASONS[receipt["reason"]])
                return result
        if len(comments) < 100:
            break
    return result


def refresh_submission(sharing: dict) -> dict:
    return _refresh(GitHubAPI(), sharing)
