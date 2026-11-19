"""Deterministic public export and GitHub transport/provenance regressions."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from conch.evals import sharing
from conch.evals.public import (CASE_CATEGORIES, MAX_PUBLIC_BYTES, canonical_json,
                                default_public_label, payload_hash, public_report,
                                validate_public_report)
from tools.ingest_eval_result import ingest_issue


def private_report():
    return {
        "schema_version": 1, "suite": {"id": "conch-lite", "version": 1, "hash": "a" * 64},
        "run_id": "53d1e9d3-09db-4c3e-b0c7-afd7256cb4c0", "conch_version": "0.9.5",
        "started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:00:20+00:00",
        "status": "complete", "target": {"provider": "openai", "requested_model": "gpt-4o",
        "observed_models": ["private-transport-id"], "identity": "observed", "settings": {}, "billing": "api"},
        "limits": {"model_calls_per_case": 4, "tool_calls_per_case": 12,
                   "deadline_seconds": 300, "max_output_tokens": 1024},
        "cases": [{"id": name, "category": category, "status": "passed", "failures": [],
                   "error_kind": "", "elapsed_seconds": 1.25, "calls": 2,
                   "usage": {"input_tokens": 20, "output_tokens": 5, "complete": True},
                   "observed_models": ["private-transport-id"], "identity": "observed", "trace": [], "replies": []}
                  for name, category in CASE_CATEGORIES.items()], "sharing": None,
    }


def _git_hash(kind, content):
    return hashlib.sha1(kind.encode() + b" " + str(len(content)).encode() + b"\0" + content).hexdigest()


class GitHubStore:
    """REST stub with real content addressing, immutable snapshots and FF rules.

    Reject unexpected paths, request fields and contributor write attempts. It
    does not echo requests into success responses: commits only become readable
    after valid ref updates, and contents are resolved against that snapshot.
    """
    def __init__(self):
        self.user = {"id": 73, "login": "alice", "type": "User"}
        self.calls = []
        self.issues, self.comments, self.blobs, self.trees, self.commits = {}, {}, {}, {}, {}
        self.head = None
        self.contents_write = True
        self.auth_error = False
        self.search_visible = True
        self.race_once = False
        self.always_conflict = False
        self.foreign_path = "results/1/999/4dc64619-8c74-48a0-bf30-95e9351acf04.json"
        self.runs = {}
        self.add_run(501)

    def add_run(self, number):
        self.runs[number] = {"id": number, "path": sharing.WORKFLOW_PATH, "event": "issues",
                             "repository": {"full_name": sharing.REPOSITORY},
                             "head_repository": {"full_name": sharing.REPOSITORY},
                             "head_branch": "main", "status": "completed", "conclusion": "success"}

    def add_issue(self, payload, *, number=None, author=None):
        number = number or max(self.issues, default=0) + 1
        self.issues[number] = {"number": number, "title": "Conch eval: " + payload["run_id"],
                               "body": sharing.encode_envelope(payload), "user": copy.deepcopy(author or self.user),
                               "state": "open", "html_url": sharing.issue_url(number)}
        return self.issues[number]

    def add_comment(self, number, body, user=None):
        self.comments.setdefault(number, []).append({"body": body, "user": user or {
            "id": sharing.ACTIONS_BOT_ID, "login": "github-actions[bot]", "type": "Bot"}})

    def _tree(self, rows):
        sha = _git_hash("tree", canonical_json(rows).encode())
        self.trees[sha] = dict(rows)
        return sha

    def _commit(self, tree, parents):
        data = {"tree": {"sha": tree}, "parents": [{"sha": p} for p in parents]}
        sha = _git_hash("commit", canonical_json(data).encode())
        self.commits[sha] = data
        return sha

    def race(self):
        rows = {} if self.head is None else dict(self.trees[self.commits[self.head]["tree"]["sha"]])
        blob = _git_hash("blob", b"another accepted record\n")
        self.blobs[blob] = b"another accepted record\n"
        rows[self.foreign_path] = blob
        self.head = self._commit(self._tree(rows), [self.head] if self.head else [])

    def request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        parsed = urlsplit(path)
        route, query = parsed.path, parse_qs(parsed.query)
        root = sharing.API_ROOT
        if method == "GET" and route == "/user":
            if self.auth_error:
                raise sharing.GitHubError(401)
            return copy.deepcopy(self.user)
        if method == "GET" and route == root:
            return {"full_name": sharing.REPOSITORY, "default_branch": "main"}
        if method == "GET" and route == "/search/issues":
            assert "repo:" + sharing.REPOSITORY in query["q"][0]
            assert "author:alice" in query["q"][0]
            return {"items": copy.deepcopy(list(self.issues.values())) if self.search_visible else [],
                    "incomplete_results": False}
        if method == "GET" and route.startswith(root + "/actions/runs/"):
            return copy.deepcopy(self.runs[int(route.rsplit("/", 1)[1])])
        if route == root + "/issues" and method == "POST":
            assert set(data) == {"title", "body"}, "ordinary contributors cannot set labels"
            payload = sharing.parse_envelope(data["body"])
            issue = self.add_issue(payload)
            issue.update(title=data["title"], body=data["body"])
            return copy.deepcopy(issue)
        match = re.fullmatch(re.escape(root) + r"/issues/(\d+)(/comments)?", route)
        if match:
            number = int(match.group(1))
            if number not in self.issues:
                raise sharing.GitHubError(404)
            if match.group(2):
                if method == "GET":
                    start = (int(query.get("page", ["1"])[0]) - 1) * 100
                    return copy.deepcopy(self.comments.get(number, [])[start:start + 100])
                if method == "POST":
                    assert set(data) == {"body"}
                    self.add_comment(number, data["body"])
                    return {"id": len(self.comments[number])}
            elif method == "GET":
                return copy.deepcopy(self.issues[number])
            elif method == "PATCH":
                if set(data) == {"body"}:
                    assert self.issues[number]["user"]["id"] == self.user["id"], "authors edit their own issues"
                    sharing.parse_envelope(data["body"])
                else:
                    assert data == {"state": "closed", "state_reason": "completed"}
                self.issues[number].update(data)
                return copy.deepcopy(self.issues[number])
        if method == "GET" and route == root + "/git/ref/heads/" + sharing.DATA_BRANCH:
            if self.head is None:
                raise sharing.GitHubError(404)
            return {"object": {"sha": self.head, "type": "commit"}}
        if method == "GET" and route.startswith(root + "/contents/"):
            filename = route[len(root + "/contents/"):]
            commit = query["ref"][0]
            assert re.fullmatch(r"[0-9a-f]{40}", commit), "contents must be commit-pinned"
            rows = self.trees[self.commits[commit]["tree"]["sha"]]
            if filename not in rows:
                raise sharing.GitHubError(404)
            blob = rows[filename]
            raw = self.blobs[blob]
            return {"type": "file", "path": filename, "size": len(raw), "encoding": "base64",
                    "content": base64.b64encode(raw).decode(), "sha": blob}
        if method == "GET" and route.startswith(root + "/git/commits/"):
            return copy.deepcopy(self.commits[route.rsplit("/", 1)[1]])
        if route.startswith(root + "/git/") and method in ("POST", "PATCH"):
            if not self.contents_write:
                raise sharing.GitHubError(403)
            if route == root + "/git/blobs":
                assert set(data) == {"content", "encoding"} and data["encoding"] == "base64"
                content = base64.b64decode(data["content"], validate=True)
                sha = _git_hash("blob", content)
                self.blobs[sha] = content
                return {"sha": sha}
            if route == root + "/git/trees":
                assert set(data) <= {"base_tree", "tree"}
                rows = dict(self.trees[data["base_tree"]]) if "base_tree" in data else {}
                for row in data["tree"]:
                    assert set(row) == {"path", "mode", "type", "sha"}
                    assert row["mode"] == "100644" and row["type"] == "blob"
                    assert row["sha"] in self.blobs
                    rows[row["path"]] = row["sha"]
                return {"sha": self._tree(rows)}
            if route == root + "/git/commits":
                assert set(data) == {"message", "tree", "parents"}
                assert data["tree"] in self.trees and all(p in self.commits for p in data["parents"])
                return {"sha": self._commit(data["tree"], data["parents"])}
            if route in (root + "/git/refs", root + "/git/refs/heads/" + sharing.DATA_BRANCH):
                if self.race_once:
                    self.race_once = False
                    self.race()
                    raise sharing.GitHubError(422 if method == "POST" else 409)
                if self.always_conflict:
                    raise sharing.GitHubError(409)
                if method == "POST":
                    assert set(data) == {"ref", "sha"} and data["ref"] == "refs/heads/eval-results"
                    if self.head is not None:
                        raise sharing.GitHubError(422)
                    assert self.commits[data["sha"]]["parents"] == [], "bootstrap must be orphan"
                else:
                    assert set(data) == {"sha", "force"} and data["force"] is False
                    if self.commits[data["sha"]]["parents"] != [{"sha": self.head}]:
                        raise sharing.GitHubError(422)
                self.head = data["sha"]
                return {"object": {"sha": self.head}}
        raise AssertionError("Unexpected request: %s %s" % (method, path))


class PublicExportTests(unittest.TestCase):
    def test_private_canaries_are_not_public_and_unknown_usage_stays_unknown(self):
        private = private_report()
        canaries = ["/Users/alice/private-project", "https://internal.example/v1", "secret-customer-model",
                    "sk-" + "A" * 24, "seat-session-alice", "private-fingerprint-123"]
        private["target"].update(provider="custom", requested_model=canaries[2], observed_models=canaries,
                                  endpoint=canaries[1], native_identity=canaries[4])
        private["target"]["settings"]["endpoint"] = canaries[1]
        private["config"] = dict(enumerate(canaries))
        private["cases"][0].update(status="failed", failures=canaries, replies=canaries,
                                    trace=[{"arguments": canaries}], error_kind=canaries[3])
        private["cases"][0]["usage"] = {"input_tokens": None, "output_tokens": None, "complete": False}
        with patch.object(sharing, "_run_gh", side_effect=AssertionError("no network during export")):
            with self.assertRaises(ValueError):
                public_report(private)
            result = public_report(private, model_label="Local candidate A")
        serialized = canonical_json(result)
        for canary in canaries:
            self.assertNotIn(canary, serialized)
        self.assertEqual(result["cases"][0]["failure_count"], len(canaries))
        self.assertIsNone(result["totals"]["usage"]["input_tokens"])
        self.assertFalse(result["totals"]["usage"]["complete"])
        self.assertNotIn("cost", result)

    def test_official_default_is_exact_not_official_looking(self):
        private = private_report()
        self.assertEqual(default_public_label(private), "gpt-4o")
        for model in ("gpt-4o-secret-deployment", "/home/alice/model", "company/gpt-4o"):
            private["target"]["requested_model"] = model
            self.assertIsNone(default_public_label(private))
        private["target"].update(provider="custom", requested_model="gpt-4o")
        self.assertIsNone(default_public_label(private))

    def test_current_cloud_catalog_exports_public_adapter_and_safe_default_label(self):
        for provider, model, label in (
            ("openai", "gpt-6-sol", "gpt-6-sol"),
            ("anthropic", "claude-opus-5-5", "claude-opus-5-5"),
            ("cerebras", "gpt-oss-120b", "gpt-oss-120b"),
            ("bedrock", "moonshotai.kimi-k2.5", "moonshotai kimi-k2.5"),
            ("openrouter", "moonshotai/kimi-k3", "moonshotai kimi-k3"),
        ):
            with self.subTest(provider=provider):
                private = private_report()
                private["target"].update(provider=provider, requested_model=model)
                result = public_report(private)
                self.assertEqual(result["model"]["provider"], provider)
                self.assertEqual(result["model"]["label"], label)
        for provider, model in (("custom", "gpt-6-sol"), ("ollama", "gpt-6-sol"),
                                ("claude-code", "sonnet"), ("codex", "default")):
            private = private_report()
            private["target"].update(provider=provider, requested_model=model)
            self.assertIsNone(default_public_label(private))
            with self.assertRaises(ValueError):
                public_report(private)
            self.assertEqual(public_report(private, model_label="Candidate A")["model"]["provider"], provider)

    def test_ollama_off_on_reasoning_settings_are_not_rejected_or_lost(self):
        private = private_report()
        private["target"].update(provider="ollama", requested_model="private-local-name", billing="local")
        for effort in ("off", "on"):
            private["target"]["settings"]["reasoning_effort"] = effort
            exported = public_report(private, model_label="Local candidate")
            self.assertEqual(exported["settings"]["reasoning_effort"], effort)
            self.assertEqual(exported["model"]["provider"], "ollama")
            self.assertNotIn("private-local-name", canonical_json(exported))

    def test_explicit_label_cannot_carry_credentials_paths_or_endpoints(self):
        for label in ("sk-" + "a" * 24, "ghp_" + "A" * 24, "../private", "C:\\Users\\secret", "https://host/v1", "internal.example.com", "internal.example.ai", "10.10.1.3"):
            with self.subTest(label=label), self.assertRaises(ValueError):
                public_report(private_report(), model_label=label)

    def test_strict_schema_rejects_bad_values_and_aggregate_claims(self):
        payload = public_report(private_report())
        mutations = [lambda p: p.update(transcript="private"), lambda p: p["cases"].pop(),
                     lambda p: p["cases"][0].update(category="trust"),
                     lambda p: p["cases"][0].update(calls=True),
                     lambda p: p["cases"][0].update(calls=5),
                     lambda p: p["cases"][0].update(elapsed_seconds=float("nan")),
                     lambda p: p["cases"][0].update(elapsed_seconds=10 ** 1000),
                     lambda p: p["cases"][0].update(status="not_run"),
                     lambda p: p["totals"].update(calls=1),
                     lambda p: p["model"].update(endpoint="secret"),
                     lambda p: p["model"].update(provider="google"),
                     lambda p: p["model"].update(provider="https://internal.example"),
                     lambda p: p["model"].pop("provider"),
                     lambda p: p["settings"].update(reasoning_effort="private-setting"),
                     lambda p: p.update(run_id="../../outside"),
                     lambda p: p.update(model={"label": "x" * MAX_PUBLIC_BYTES})]
        for mutate in mutations:
            invalid = copy.deepcopy(payload)
            mutate(invalid)
            with self.subTest(payload=invalid), self.assertRaises(ValueError):
                validate_public_report(invalid)

    def test_canonical_hash_ignores_key_order_and_rejects_duplicate_json_fields(self):
        payload = public_report(private_report())
        reordered = dict(reversed(list(payload.items())))
        self.assertEqual(payload_hash(payload), payload_hash(reordered))
        envelope = sharing.encode_envelope(payload)
        self.assertEqual(sharing.parse_envelope(envelope), payload)
        duplicate = envelope.replace('"envelope_version":1', '"envelope_version":1,"envelope_version":1')
        for invalid in (duplicate, envelope + "\n$(touch /tmp/owned)", "x" * (sharing.MAX_ENVELOPE_BYTES + 1)):
            with self.assertRaises(ValueError):
                sharing.parse_envelope(invalid)

    def test_retry_nonce_changes_envelope_not_payload_hash_and_is_strict(self):
        payload = public_report(private_report())
        first = sharing.parse_submission(sharing.encode_envelope(payload))
        second = sharing.parse_submission(sharing.encode_envelope(payload))
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(first["payload_sha256"], second["payload_sha256"])
        self.assertEqual(first["report"], second["report"])
        for attempt in (None, "../../outside", "dd78c3fe-e3b9-19c7-b5d0-4966f9ce28e8"):
            invalid = dict(first, attempt_id=attempt)
            with self.assertRaises(ValueError):
                sharing.parse_submission(sharing.SUBMISSION_MARKER + canonical_json(invalid))
        old_envelope = dict(first)
        del old_envelope["attempt_id"]
        with self.assertRaises(ValueError):
            sharing.parse_submission(sharing.SUBMISSION_MARKER + canonical_json(old_envelope))


class GitHubSharingTests(unittest.TestCase):
    def setUp(self):
        self.store = GitHubStore()
        self.payload = public_report(private_report())
        self.transport = patch.object(sharing, "GitHubAPI", return_value=self.store)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def submit(self):
        return sharing.submit_report(self.payload, identity="alice")

    def test_identity_mismatch_or_auth_failure_never_posts_or_logs_in(self):
        with self.assertRaisesRegex(sharing.SharingError, "account changed"):
            sharing.submit_report(self.payload, identity="bob")
        self.store.auth_error = True
        with self.assertRaisesRegex(sharing.SharingError, "authentication"):
            self.submit()
        self.assertTrue(all(method == "GET" and path == "/user" for method, path, _ in self.store.calls))

    def test_nonmaintainer_needs_only_issue_creation_and_pending_deduplicates(self):
        self.store.contents_write = False
        metadata = self.submit()
        original_body = self.store.issues[metadata["issue_number"]]["body"]
        self.assertEqual(metadata["status"], "submitted")
        self.assertEqual(self.submit()["issue_number"], metadata["issue_number"])
        writes = [(path, data) for method, path, data in self.store.calls if method != "GET"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0][0], sharing.API_ROOT + "/issues")
        self.assertEqual(set(writes[0][1]), {"title", "body"})
        self.assertEqual(writes[1][0], sharing.API_ROOT + "/issues/%d" % metadata["issue_number"])
        self.assertEqual(set(writes[1][1]), {"body"})
        self.assertNotEqual(writes[1][1]["body"], original_body)
        self.assertEqual(sharing.parse_envelope(writes[1][1]["body"]), self.payload)
        self.assertEqual(len(self.store.issues), 1)
        self.assertEqual(sharing.github_identity(), "alice")

    def test_saved_retry_reuses_issue_before_search_index_catches_up(self):
        metadata = self.submit()
        self.store.search_visible = False
        self.store.contents_write = False
        before = len(self.store.calls)
        retried = sharing.submit_report(self.payload, identity="alice", previous=metadata)
        self.assertEqual(retried["issue_number"], metadata["issue_number"])
        self.assertNotEqual(retried["attempt_id"], metadata["attempt_id"])
        self.assertEqual(len(self.store.issues), 1)
        self.assertFalse(any(path.startswith("/search/issues")
                             for _, path, _ in self.store.calls[before:]))
        self.assertEqual(sharing.refresh_submission(retried)["status"], "submitted")

    def test_saved_retry_cannot_retarget_another_account_or_payload(self):
        metadata = self.submit()
        for changed in ({"author_id": 999}, {"payload_sha256": "b" * 64}):
            with self.subTest(changed=changed):
                before = len(self.store.calls)
                with self.assertRaisesRegex(sharing.SharingError, "different account or report"):
                    sharing.submit_report(self.payload, identity="alice", previous=dict(metadata, **changed))
                self.assertTrue(all(method == "GET" for method, _, _ in self.store.calls[before:]))
                self.assertEqual(len(self.store.issues), 1)

    def test_comments_never_prove_acceptance_but_real_dataset_does(self):
        metadata = self.submit()
        number = metadata["issue_number"]
        spoof = sharing.RECEIPT_MARKER + canonical_json({"status": "accepted", "dataset_url": "https://evil.example/record",
                                                        "run_id": self.payload["run_id"], "payload_sha256": payload_hash(self.payload)})
        self.store.add_comment(number, spoof, {"id": 2, "login": "github-actions[bot]", "type": "Bot"})
        self.store.add_comment(number, spoof)
        self.assertEqual(sharing.refresh_submission(metadata)["status"], "submitted")
        ingest_issue(self.store, number, workflow_run_id=501)
        accepted = sharing.refresh_submission(metadata)
        self.assertEqual(accepted["status"], "accepted")
        self.assertIn("/blob/" + self.store.head + "/results/1/73/", accepted["dataset_url"])
        self.assertNotIn("evil", accepted["dataset_url"])
        self.assertEqual(self.submit()["status"], "accepted")
        self.assertEqual(len(self.store.issues), 1)

    def test_dataset_requires_matching_source_author_and_trusted_completed_workflow(self):
        metadata = self.submit()
        ingest_issue(self.store, metadata["issue_number"], workflow_run_id=501)
        self.store.runs[501]["path"] = ".github/workflows/unrelated.yml"
        self.assertEqual(sharing.refresh_submission(metadata)["status"], "submitted")
        self.store.runs[501]["path"] = sharing.WORKFLOW_PATH
        self.store.runs[501]["status"] = "in_progress"
        self.assertEqual(sharing.refresh_submission(metadata)["status"], "submitted")
        self.store.runs[501]["status"] = "completed"
        self.store.runs[501]["conclusion"] = "failure"  # publish succeeded, comment/close failed
        self.assertEqual(sharing.refresh_submission(metadata)["status"], "accepted")
        self.store.runs[501]["head_branch"] = "untrusted-branch"
        self.assertEqual(sharing.refresh_submission(metadata)["status"], "submitted")

    def test_valid_git_blob_with_forged_source_author_is_not_acceptance(self):
        metadata = self.submit()
        ingest_issue(self.store, metadata["issue_number"], workflow_run_id=501)
        foreign = self.store.add_issue(self.payload, author={"id": 999, "login": "bob", "type": "User"})
        record, _ = sharing.read_dataset(self.store, self.payload, 73)
        record["source"]["issue_number"] = foreign["number"]
        raw = (canonical_json(record) + "\n").encode()
        blob = _git_hash("blob", raw)
        self.store.blobs[blob] = raw
        rows = dict(self.store.trees[self.store.commits[self.store.head]["tree"]["sha"]])
        rows[sharing.dataset_path(self.payload, 73)] = blob
        self.store.head = self.store._commit(self.store._tree(rows), [self.store.head])
        self.assertEqual(sharing.refresh_submission(metadata)["status"], "submitted")

    def test_valid_but_conflicting_dataset_is_not_accepted(self):
        metadata = self.submit()
        ingest_issue(self.store, metadata["issue_number"], workflow_run_id=501)
        self.payload["model"]["label"] = "Different candidate"
        self.store.issues[metadata["issue_number"]]["body"] = sharing.encode_envelope(self.payload)
        metadata["payload_sha256"] = payload_hash(self.payload)
        refreshed = sharing.refresh_submission(metadata)
        self.assertEqual(refreshed["status"], "failed")
        self.assertIn("different report", refreshed["reason"])

    def test_verified_rejection_exposes_safe_reason_and_explicit_retry_keeps_inbox(self):
        metadata = self.submit()
        self.store.contents_write = False
        result = ingest_issue(self.store, metadata["issue_number"], workflow_run_id=501)
        self.assertEqual(result["reason"], "storage_unavailable")
        rejected = sharing.refresh_submission(metadata)
        self.assertEqual(rejected["status"], "failed")
        self.assertIn("retained", rejected["reason"])
        retried = self.submit()
        self.assertEqual(retried["issue_number"], metadata["issue_number"])
        self.assertEqual(retried["status"], "submitted")
        self.assertEqual(len(self.store.comments[metadata["issue_number"]]), 1)
        self.assertEqual(self.store.issues[metadata["issue_number"]]["state"], "open")
        self.assertNotEqual(retried["attempt_id"], metadata["attempt_id"])
        self.assertEqual(retried["payload_sha256"], metadata["payload_sha256"])
        self.assertEqual(sharing.refresh_submission(retried)["status"], "submitted")
        # An older in-flight job can post another trusted failure after retry.
        old_receipt = self.store.comments[metadata["issue_number"]][0]["body"]
        self.store.add_comment(metadata["issue_number"], old_receipt)
        self.assertEqual(sharing.refresh_submission(retried)["status"], "submitted")
        # A failure for the current nonce is still exposed, and later successful
        # publication is accepted for either attempt without changing the data.
        ingest_issue(self.store, metadata["issue_number"], workflow_run_id=501)
        self.assertEqual(sharing.refresh_submission(retried)["status"], "failed")
        self.store.contents_write = True
        ingest_issue(self.store, metadata["issue_number"], workflow_run_id=501)
        self.assertEqual(sharing.refresh_submission(retried)["status"], "accepted")
        self.assertEqual(sharing.refresh_submission(metadata)["status"], "accepted")

    def test_rejection_requires_matching_hash_bot_id_and_workflow(self):
        metadata = self.submit()
        receipt = {"status": "rejected", "run_id": self.payload["run_id"], "payload_sha256": "b" * 64,
                   "reason": "conflicting_report", "workflow_run_id": 501, "attempt_id": metadata["attempt_id"]}
        self.store.add_comment(metadata["issue_number"], sharing.RECEIPT_MARKER + canonical_json(receipt))
        receipt["payload_sha256"] = metadata["payload_sha256"]
        self.store.add_comment(metadata["issue_number"], sharing.RECEIPT_MARKER + canonical_json(receipt),
                               {"id": 9, "login": "github-actions[bot]", "type": "Bot"})
        self.assertEqual(sharing.refresh_submission(metadata)["status"], "submitted")


class GitHubTransportTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(sharing.os.environ, {"GH_TOKEN": "alice-token", "GITHUB_TOKEN": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def test_account_switch_between_identity_and_write_uses_frozen_credentials(self):
        payload = public_report(private_report())
        for source in ("GH_TOKEN", "GITHUB_TOKEN", "stored"):
            for retry in (False, True):
                with self.subTest(source=source, retry=retry):
                    store = GitHubStore()
                    if retry:
                        store.add_issue(payload)
                    current_account = {"token": "alice-token"}
                    captured = []
                    api_tokens = []

                    def wire(args, data=None, *, token=None):
                        if args[0] == "auth":
                            self.assertEqual(args, ["auth", "token", "--hostname", "github.com"])
                            captured.append(current_account["token"])
                            return 0, current_account["token"] + "\n"
                        api_tokens.append(token)
                        store.user = ({"id": 73, "login": "alice", "type": "User"} if token == "alice-token"
                                      else {"id": 999, "login": "bob", "type": "User"})
                        method = args[args.index("--method") + 1]
                        path = next(arg for arg in args if arg.startswith("/"))
                        try:
                            result = store.request(method, path, json.loads(data) if data is not None else None)
                        except sharing.GitHubError as exc:
                            return 1, "HTTP/2.0 %d Error\n\n{}" % exc.status
                        if path == "/user":
                            # Both stored gh account and environment can change
                            # before any write. Neither may change this upload.
                            current_account["token"] = "bob-token"
                            sharing.os.environ["GH_TOKEN"] = "bob-token"
                            sharing.os.environ["GITHUB_TOKEN"] = "bob-token"
                        return 0, "HTTP/2.0 200 OK\n\n" + canonical_json(result)

                    environment = {"GH_TOKEN": "", "GITHUB_TOKEN": ""}
                    if source != "stored":
                        environment[source] = "alice-token"
                    with patch.dict(sharing.os.environ, environment), patch.object(sharing, "_run_gh", side_effect=wire):
                        metadata = sharing.submit_report(payload, identity="alice")
                    self.assertEqual(metadata["status"], "submitted")
                    self.assertEqual(metadata["author_id"], 73)
                    self.assertEqual(len(store.issues), 1)
                    self.assertEqual(store.issues[metadata["issue_number"]]["user"]["id"], 73)
                    self.assertTrue(api_tokens and all(token == "alice-token" for token in api_tokens))
                    self.assertEqual(captured, ["alice-token"] if source == "stored" else [])
                    self.assertNotIn("alice-token", canonical_json(metadata))

    def test_frozen_token_of_wrong_account_is_rejected_before_public_write(self):
        response = 'HTTP/2.0 200 OK\n\n{"id":999,"login":"bob","type":"User"}'
        with patch.dict(sharing.os.environ, {"GH_TOKEN": "bob-token"}), patch.object(sharing, "_run_gh", return_value=(0, response)) as run:
            with self.assertRaisesRegex(sharing.SharingError, "account changed"):
                sharing.submit_report(public_report(private_report()), identity="alice")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], "/user")
        self.assertEqual(run.call_args.kwargs["token"], "bob-token")

    def test_failed_private_token_capture_never_echoes_output(self):
        secret = "ghp_" + "Z" * 30
        with patch.dict(sharing.os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}), patch.object(sharing, "_run_gh", return_value=(1, secret)) as run:
            with self.assertRaises(sharing.SharingError) as caught:
                sharing.GitHubAPI()
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(run.call_args.args[0], ["auth", "token", "--hostname", "github.com"])

    def test_json_body_is_stdin_not_shell_or_argv_and_hostname_is_fixed(self):
        data = {"body": "$(touch /tmp/never-executed)\n' ; exit 0"}
        response = 'HTTP/2.0 201 Created\nContent-Type: application/json\n\n{"number":7}'
        with patch.object(sharing, "_run_gh", return_value=(0, response)) as run:
            self.assertEqual(sharing.GitHubAPI().request("POST", sharing.API_ROOT + "/issues", data), {"number": 7})
        args, raw = run.call_args.args
        self.assertEqual(json.loads(raw), data)
        self.assertNotIn(data["body"], args)
        self.assertEqual(args[args.index("--hostname") + 1], "github.com")
        self.assertEqual(args[-2:], ["--input", "-"])
        self.assertNotIn("--label", args)

    def test_raw_auth_errors_cannot_expose_credentials(self):
        secret = "sk-" + "Q" * 30
        with patch.object(sharing, "_run_gh", return_value=(1, "HTTP/2.0 401 Unauthorized\n\n" + secret)):
            with self.assertRaises(sharing.SharingError) as caught:
                sharing.GitHubAPI().request("GET", "/user")
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("gh auth status", str(caught.exception))

    def test_process_has_no_shell_and_output_and_time_are_bounded(self):
        class Process:
            def __init__(self, output, timeout=False):
                self.stdout = io.BytesIO(output)
                self.stdin = io.BytesIO()
                self.returncode = 0
                self.killed = False
                self.timeout = timeout

            def wait(self, timeout=None):
                if self.timeout and not self.killed:
                    raise subprocess.TimeoutExpired("gh", timeout)
                return 0

            def kill(self):
                self.killed = True

        huge = Process(b"x" * (sharing.MAX_RESPONSE_BYTES + 1))
        with patch.object(sharing.shutil, "which", return_value="/usr/bin/gh"), patch.object(sharing.subprocess, "Popen", return_value=huge) as spawn:
            with self.assertRaisesRegex(sharing.SharingError, "size limit"):
                sharing._run_gh(["api", "/user"], token="frozen-token")
        self.assertTrue(huge.killed)
        self.assertFalse(spawn.call_args.kwargs.get("shell", False))
        self.assertEqual(spawn.call_args.kwargs["env"]["GH_PROMPT_DISABLED"], "1")
        self.assertEqual(spawn.call_args.kwargs["env"]["GH_TOKEN"], "frozen-token")
        self.assertNotIn("GITHUB_TOKEN", spawn.call_args.kwargs["env"])
        self.assertNotIn("frozen-token", spawn.call_args.args[0])
        slow = Process(b"", timeout=True)
        with patch.object(sharing.shutil, "which", return_value="/usr/bin/gh"), patch.object(sharing.subprocess, "Popen", return_value=slow):
            with self.assertRaisesRegex(sharing.SharingError, "timed out"):
                sharing._run_gh(["api", "/user"])
        self.assertTrue(slow.killed)

    @unittest.skipUnless(hasattr(os, "WNOHANG"), "POSIX child reaping")
    def test_interrupt_terminates_and_reaps_the_active_github_process(self):
        spawn = subprocess.Popen
        children = []

        def interrupt_wait(*args, **kwargs):
            child = spawn(*args, **kwargs)
            children.append(child)
            wait = child.wait

            def interrupted(timeout=None):
                if timeout == 30:
                    raise KeyboardInterrupt
                return wait(timeout=timeout)

            child.wait = interrupted
            return child

        try:
            with patch.object(sharing.shutil, "which", return_value=sys.executable), \
                    patch.object(sharing.subprocess, "Popen", side_effect=interrupt_wait):
                with self.assertRaises(KeyboardInterrupt):
                    sharing._run_gh(["-c", "import time; time.sleep(60)"], b"request body")
            self.assertEqual(len(children), 1)
            self.assertEqual(children[0].returncode, -9)
            with self.assertRaises(ChildProcessError):
                os.waitpid(children[0].pid, os.WNOHANG)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    unittest.main()
