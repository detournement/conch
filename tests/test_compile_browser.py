"""Browser events through the capture→compile pipeline (browser
satellite).

Proven here: journaled browser events normalize to stable
``web:<host>:<action>`` steps and mine through the same deterministic
recurrence machinery as shell sequences; ``/compile from-browser``
drafts a card carrying browser provenance through the normal review
pipeline, filters by origin, and requires an explicit goal for
heterogeneous multi-origin captures; everything stays behind the
capture_enabled AND capture_browser gates (off by default — the mining
never reads browser rows and from-browser refuses when off); and copy
events never carry content.
"""

import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from conch.capitol.compiler.capture import capture_provenance_line
from conch.capitol.compiler.capture_browser import (
    browser_events,
    capture_from_browser,
    render_browser_event,
)
from conch.capitol.compiler.card import normalize_card
from conch.capitol.compiler.commands import run_compile_command
from conch.kernel.patterns import (
    mine_sequences,
    normalize_browser_step,
    steps_from_browser_events,
)
from conch.kernel.store import MissionStore

from tests.compiler_fixtures import candidate_card, fake_discovery

CONFIG = {"capture_enabled": "true", "capture_browser": "true"}

RELEASE_SEQUENCE = [
    {"kind": "nav", "detail": {"path": "/conch/releases"}},
    {"kind": "click",
     "detail": {"role": "button", "label": "Draft a new release"}},
    {"kind": "submit",
     "detail": {"form": "release", "fields": ["tag", "title", "notes"]}},
]


def payload(kind, detail, origin="https://github.com", ts=1_800_000_000.0):
    return {"origin": origin, "kind": kind, "ts": ts, "detail": detail,
            "ext_version": "0.1.0"}


class TestBrowserSteps(unittest.TestCase):
    def test_normalization_shapes(self):
        self.assertEqual(
            normalize_browser_step(payload(
                "click", {"role": "button",
                          "label": "Merge pull request #4821"},
            )),
            "web:github.com:click:button:merge-pull-request-#·",
        )
        self.assertEqual(
            normalize_browser_step(payload(
                "nav", {"path": "/conch/pulls/123"},
            )),
            "web:github.com:nav:conch",
        )
        self.assertEqual(
            normalize_browser_step(payload(
                "submit", {"form": "release", "fields": ["tag"]},
            )),
            "web:github.com:submit:release",
        )
        self.assertEqual(
            normalize_browser_step(payload("copy", {})),
            "web:github.com:copy",
        )

    def test_stability_across_noisy_labels(self):
        first = normalize_browser_step(payload(
            "click", {"role": "button", "label": "Merge #101"},
        ))
        second = normalize_browser_step(payload(
            "click", {"role": "button", "label": "Merge #202"},
        ))
        self.assertEqual(first, second)  # ids collapse to the same shape

    def test_garbage_yields_no_step(self):
        self.assertEqual(normalize_browser_step({}), "")
        self.assertEqual(
            normalize_browser_step({"kind": "click", "origin": ""}), ""
        )
        self.assertEqual(steps_from_browser_events(
            [payload("click", {"role": "b", "label": "x"}), {}]
        ), ["web:github.com:click:b:x"])

    def test_browser_steps_mine_like_shell_steps(self):
        steps = steps_from_browser_events(
            [payload(**event) for event in RELEASE_SEQUENCE]
        )
        sources = [
            {"id": f"browser:github.com:2026-09-{20 + day}",
             "kind": "browser", "steps": list(steps)}
            for day in range(3)
        ]
        suggestions = mine_sequences(sources)
        self.assertTrue(suggestions)
        top = suggestions[0]
        self.assertEqual(top["count"], 3)
        self.assertEqual(top["kinds"], ["browser"])
        self.assertTrue(top["steps"][0].startswith("web:github.com:nav"))


class BrowserCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": self._tmp.name,
            "XDG_CONFIG_HOME": os.path.join(self._tmp.name, "config"),
        })
        env.start()
        self.addCleanup(env.stop)

    def seed(self, repeats=3, origin="https://github.com"):
        store = MissionStore()
        try:
            key = 0
            for repeat in range(repeats):
                for event in RELEASE_SEQUENCE:
                    key += 1
                    store.receive_inbox(
                        "browser", f"browser:seed:{origin}:{key}",
                        payload(origin=origin,
                                ts=1_800_000_000.0 + key, **event),
                    )
        finally:
            store.close()

    def run_cmd(self, arg, config=None):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_compile_command(
                arg, dict(CONFIG if config is None else config)
            )
        return buffer.getvalue()


class TestBrowserReads(BrowserCase):
    def test_origin_filter_and_rendering(self):
        self.seed(repeats=1, origin="https://github.com")
        self.seed(repeats=1, origin="https://jira.example.com")
        store = MissionStore()
        self.addCleanup(store.close)
        both = browser_events(store)
        self.assertEqual(len(both), 6)
        github_only = browser_events(store, "github.com")
        self.assertEqual(len(github_only), 3)
        self.assertTrue(all(
            event["origin"] == "https://github.com"
            for event in github_only
        ))
        # full origins match too
        self.assertEqual(
            len(browser_events(store, "https://jira.example.com")), 3,
        )

    def test_copy_renders_without_content(self):
        line = render_browser_event(payload("copy", {}))
        self.assertIn("content never captured", line)

    def test_capture_context_and_provenance(self):
        self.seed(repeats=2)
        store = MissionStore()
        self.addCleanup(store.close)
        context = capture_from_browser(store, "github.com")
        self.assertEqual(context["kind"], "browser")
        self.assertIn("Draft a new release", context["block"])
        self.assertIn("field", context["block"].lower())
        self.assertTrue(context["default_goal"])  # origin given
        self.assertEqual(context["provenance"]["events"], 6)
        self.assertEqual(context["provenance"]["origins"],
                         ["github.com"])
        line = capture_provenance_line(dict(
            context["provenance"], source="github.com",
        ))
        self.assertIn("captured from browser github.com", line)
        self.assertIn("6 event(s)", line)

    def test_multi_origin_requires_explicit_goal(self):
        self.seed(repeats=1, origin="https://github.com")
        self.seed(repeats=1, origin="https://jira.example.com")
        store = MissionStore()
        self.addCleanup(store.close)
        context = capture_from_browser(store)
        self.assertEqual(context["default_goal"], "")


class TestFromBrowserCommand(BrowserCase):
    def test_gates_off_by_default(self):
        self.seed()
        gated = self.run_cmd("from-browser github.com", config={})
        self.assertIn("/install capture", gated)
        browser_gated = self.run_cmd(
            "from-browser github.com",
            config={"capture_enabled": "true"},
        )
        self.assertIn("Browser capture is not installed", browser_gated)
        self.assertIn("/install capture browser", browser_gated)

    def test_drafts_with_browser_provenance(self):
        self.seed()

        def fake_session(config, goal, **kwargs):
            block = kwargs["capture_context"]
            assert "Browser capture" in block
            assert "Draft a new release" in block
            return normalize_card(candidate_card(), fake_discovery())

        with patch(
            "conch.capitol.compiler.session.run_compile_session",
            side_effect=fake_session,
        ):
            output = self.run_cmd(
                'from-browser github.com "automate the release dance"'
            )
        self.assertIn("Compiled", output)
        store = MissionStore()
        self.addCleanup(store.close)
        rows = store.list_compilations()
        self.assertEqual(len(rows), 1)
        capture = store.compilation_capture(rows[0]["compilation_id"])
        self.assertEqual(capture["kind"], "browser")
        self.assertEqual(capture["source"], "github.com")
        self.assertEqual(capture["events"], 9)
        self.assertEqual(capture["goal"], "automate the release dance")
        status = self.run_cmd(f"status {rows[0]['compilation_id']}")
        self.assertIn("captured from browser github.com", status)

    def test_no_events_is_a_clear_error(self):
        output = self.run_cmd("from-browser github.com \"goal\"")
        self.assertIn("no journaled browser events", output)


class TestSuggestionsSeeBrowser(BrowserCase):
    def test_mining_gated_and_detecting(self):
        self.seed(repeats=3)
        # capture on, browser off: browser rows are never read
        off = self.run_cmd(
            "suggestions", config={"capture_enabled": "true"},
        )
        self.assertNotIn("web:github.com", off)
        on = self.run_cmd("suggestions")
        self.assertIn("Recurring shapes", on)
        self.assertIn("web:github.com:nav:conch", on)
        self.assertIn("browser", on)


if __name__ == "__main__":
    unittest.main()
