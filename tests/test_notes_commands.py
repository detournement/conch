"""/notes — editor-backed notes on the items store (addendum N1).

Proven here: the editor lifecycle through an injected fake terminal
(new/open/update, abandoned empty buffers, declined and failed
handoffs, scratch-file cleanup), deterministic title parsing (first-line
``# Title`` wins, then the argument, then the dated Untitled fallback),
edit history surfaced from the event log (replay == live), search over
titles+bodies scoped to the notes space, archive/reopen, the /todo-family
quick-add grammar with tags, the editor resolution chain (config wins,
$VISUAL beats $EDITOR, nano floor, vi last), the /terminal authority gate
(remote/non-TTY sessions refused toward quick-add and personal_items),
the credential write-guard (whole-note rejection, editor text preserved),
and note content staying stored text — never instructions.
"""

import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.commands import handle_slash_command, slash_command_names
from conch.config import resolve_editor
from conch.notes import parse_note_buffer
from conch.secure_terminal import TerminalRunResult
from conch.tooling import (
    InteractiveTerminalClient,
    LocalShellPolicy,
    get_agent_mode,
    set_agent_mode,
)


def _text(msg):
    return {"content": [{"type": "text", "text": msg}]}


class FakeEditorTerminal:
    """Injected in place of the session's interactive_terminal client:
    records the handoff, optionally rewrites the scratch buffer, and
    returns a scripted result — no real TTY, no subprocess."""

    def __init__(self, write=None, result=None, available=True):
        self.write = write            # str | callable(path) | None
        self.result = result          # TerminalRunResult override
        self.available = available
        self.calls = []
        self.seeds = []

    def handoff_available(self):
        return self.available

    def run_argv(self, argv, *, description, timeout=0,
                 noun="Interactive command"):
        self.calls.append({
            "argv": list(argv), "description": description, "noun": noun,
        })
        path = Path(argv[-1])
        self.seeds.append(path.read_text() if path.exists() else None)
        result = self.result or TerminalRunResult(
            approved=True, returncode=0
        )
        if result.approved:
            if callable(self.write):
                self.write(path)
            elif isinstance(self.write, str):
                path.write_text(self.write)
        return result, _text("ok")


class RecordingRunner:
    """A DirectTerminalRunner stand-in for the real-client smoke test:
    honors the policy gate, writes scripted content, never spawns."""

    def __init__(self, content):
        self.content = content
        self.policy = None
        self.argv = None
        self.description = ""

    def set_policy(self, policy):
        self.policy = policy

    def available(self):
        if self.policy is None or not self.policy.local_session:
            return False
        check = self.policy.tty_check or (lambda: False)
        return bool(check())

    def run(self, argv, *, description, timeout=0):
        self.argv = list(argv)
        self.description = description
        Path(argv[-1]).write_text(self.content)
        return TerminalRunResult(approved=True, returncode=0)


class NotesCommandCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        # patch.dict restores these on stop; the tests must not inherit
        # the developer machine's editor preferences.
        os.environ.pop("VISUAL", None)
        os.environ.pop("EDITOR", None)
        (self.root / "runtime").mkdir(parents=True, exist_ok=True)

    def run_command(self, command, config=None, tool_map=None):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = handle_slash_command(
                command,
                config if config is not None else {},
                "openai", "gpt-4o", lambda value: None,
                tool_map=tool_map,
            )
        return result, stdout.getvalue()

    def store(self):
        from conch.kernel.store import MissionStore

        store = MissionStore()
        self.addCleanup(store.close)
        return store

    def editor_map(self, fake):
        return {"interactive_terminal": fake}


# ---------------------------------------------------------------------------
# Editor resolution chain
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def scrubbed_env(**overrides):
    with patch.dict(os.environ):
        os.environ.pop("VISUAL", None)
        os.environ.pop("EDITOR", None)
        os.environ.update(overrides)
        yield


class TestEditorResolution(unittest.TestCase):
    def test_config_key_wins_over_everything(self):
        with scrubbed_env(VISUAL="visual-ed", EDITOR="editor-ed"):
            self.assertEqual(
                resolve_editor({"editor": "myed --wait"}), "myed --wait"
            )

    def test_visual_beats_editor(self):
        with scrubbed_env(VISUAL="visual-ed", EDITOR="editor-ed"):
            self.assertEqual(resolve_editor({}), "visual-ed")

    def test_editor_when_no_visual(self):
        with scrubbed_env(EDITOR="editor-ed"):
            self.assertEqual(resolve_editor({}), "editor-ed")

    def test_nano_floor_when_nothing_configured(self):
        with scrubbed_env(), patch(
            "conch.config.shutil.which", return_value="/usr/bin/nano"
        ) as which:
            self.assertEqual(resolve_editor({}), "nano")
        which.assert_called_once_with("nano")

    def test_vi_last_when_nano_missing(self):
        with scrubbed_env(), patch(
            "conch.config.shutil.which", return_value=None
        ):
            self.assertEqual(resolve_editor({}), "vi")

    def test_blank_values_fall_through(self):
        with scrubbed_env(VISUAL="   ", EDITOR="editor-ed"):
            self.assertEqual(resolve_editor({"editor": ""}), "editor-ed")

    def test_none_config_is_fine(self):
        with scrubbed_env(EDITOR="editor-ed"):
            self.assertEqual(resolve_editor(None), "editor-ed")


# ---------------------------------------------------------------------------
# Buffer/title parsing (pure)
# ---------------------------------------------------------------------------

class TestNoteBufferParsing(unittest.TestCase):
    def test_first_line_heading_becomes_title(self):
        self.assertEqual(
            parse_note_buffer("# Groceries\n\nmilk\neggs"),
            ("Groceries", "milk\neggs"),
        )

    def test_deeper_headings_accepted(self):
        self.assertEqual(
            parse_note_buffer("## Weekly plan\nbody"),
            ("Weekly plan", "body"),
        )

    def test_leading_blank_lines_skipped(self):
        self.assertEqual(
            parse_note_buffer("\n\n# Title\n\n\nbody line\n"),
            ("Title", "body line"),
        )

    def test_heading_beats_fallback(self):
        self.assertEqual(
            parse_note_buffer("# Real\nbody", fallback_title="argued"),
            ("Real", "body"),
        )

    def test_fallback_title_when_no_heading(self):
        self.assertEqual(
            parse_note_buffer("plain text\nmore", fallback_title="eggs"),
            ("eggs", "plain text\nmore"),
        )

    def test_untitled_fallback_carries_the_date(self):
        now = time.time()
        stamp = time.strftime("%Y-%m-%d", time.localtime(now))
        title, body = parse_note_buffer("plain text", now=now)
        self.assertEqual(title, f"Untitled {stamp}")
        self.assertEqual(body, "plain text")

    def test_empty_buffer_is_none(self):
        self.assertIsNone(parse_note_buffer(""))
        self.assertIsNone(parse_note_buffer("   \n\n  \n"))

    def test_bare_heading_marker_is_empty(self):
        self.assertIsNone(parse_note_buffer("# "))
        self.assertIsNone(parse_note_buffer("#\n\n"))

    def test_bare_heading_with_body_gets_untitled(self):
        now = time.time()
        stamp = time.strftime("%Y-%m-%d", time.localtime(now))
        self.assertEqual(
            parse_note_buffer("# \nbody", now=now),
            (f"Untitled {stamp}", "body"),
        )

    def test_crlf_normalized(self):
        self.assertEqual(
            parse_note_buffer("# T\r\n\r\nline1\r\nline2\r\n"),
            ("T", "line1\nline2"),
        )

    def test_hash_without_space_is_body_text(self):
        title, body = parse_note_buffer("#tagline\nrest",
                                        fallback_title="fb")
        self.assertEqual((title, body), ("fb", "#tagline\nrest"))


# ---------------------------------------------------------------------------
# /notes new — the editor path
# ---------------------------------------------------------------------------

class TestNotesNew(NotesCommandCase):
    def test_new_stores_title_body_and_editor_provenance(self):
        fake = FakeEditorTerminal(write="# Groceries\n\nmilk\neggs")
        _, out = self.run_command(
            "/notes new", tool_map=self.editor_map(fake)
        )
        self.assertIn("Note saved", out)
        item = self.store().resolve_item("#1")
        self.assertEqual(item["space"], "notes")
        self.assertEqual(item["title"], "Groceries")
        self.assertEqual(item["body"], "milk\neggs")
        self.assertEqual(item["source"], "editor")
        # The handoff got a scratch .md file and it is gone afterwards.
        scratch = fake.calls[0]["argv"][-1]
        self.assertTrue(scratch.endswith(".md"))
        self.assertFalse(os.path.exists(scratch))

    def test_new_title_argument_seeds_the_buffer(self):
        fake = FakeEditorTerminal(write=None)  # user saves the seed as-is
        _, out = self.run_command(
            "/notes new eggs and flour", tool_map=self.editor_map(fake)
        )
        self.assertIn("Note saved", out)
        self.assertEqual(fake.seeds[0], "# eggs and flour\n\n")
        item = self.store().resolve_item("#1")
        self.assertEqual(item["title"], "eggs and flour")
        self.assertEqual(item["body"], "")

    def test_buffer_heading_wins_over_the_argument(self):
        fake = FakeEditorTerminal(write="# Actual Title\n\nbody")
        self.run_command("/notes new eggs", tool_map=self.editor_map(fake))
        self.assertEqual(self.store().resolve_item("#1")["title"],
                         "Actual Title")

    def test_untitled_fallback_with_date(self):
        fake = FakeEditorTerminal(write="just some text\nsecond line")
        self.run_command("/notes new", tool_map=self.editor_map(fake))
        stamp = time.strftime("%Y-%m-%d")
        item = self.store().resolve_item("#1")
        self.assertEqual(item["title"], f"Untitled {stamp}")
        self.assertEqual(item["body"], "just some text\nsecond line")

    def test_abandoned_empty_buffer_creates_nothing(self):
        fake = FakeEditorTerminal(write=None)  # bare /notes new: empty seed
        _, out = self.run_command(
            "/notes new", tool_map=self.editor_map(fake)
        )
        self.assertIn("Empty buffer", out)
        self.assertEqual(self.store().list_items(space="notes",
                                                 status="all"), [])
        self.assertFalse(os.path.exists(fake.calls[0]["argv"][-1]))

    def test_declined_handoff_creates_nothing(self):
        fake = FakeEditorTerminal(
            write="# X\n\nbody", result=TerminalRunResult(approved=False)
        )
        _, out = self.run_command(
            "/notes new", tool_map=self.editor_map(fake)
        )
        self.assertIn("cancelled", out)
        self.assertEqual(self.store().list_items(space="notes",
                                                 status="all"), [])

    def test_nonzero_editor_exit_creates_nothing(self):
        fake = FakeEditorTerminal(
            write="# X\n\nbody",
            result=TerminalRunResult(approved=True, returncode=1),
        )
        _, out = self.run_command(
            "/notes new", tool_map=self.editor_map(fake)
        )
        self.assertIn("exited 1", out)
        self.assertEqual(self.store().list_items(space="notes",
                                                 status="all"), [])

    def test_config_editor_reaches_the_argv(self):
        fake = FakeEditorTerminal(write="# T\n\nb")
        self.run_command(
            "/notes new", config={"editor": "myed --wait"},
            tool_map=self.editor_map(fake),
        )
        argv = fake.calls[0]["argv"]
        self.assertEqual(argv[:2], ["myed", "--wait"])
        self.assertIn("myed", fake.calls[0]["description"])


# ---------------------------------------------------------------------------
# /notes open — reopen, update events, edit history
# ---------------------------------------------------------------------------

class TestNotesOpen(NotesCommandCase):
    def seed_note(self, title="Draft", body="original body"):
        fake = FakeEditorTerminal(write=f"# {title}\n\n{body}")
        self.run_command("/notes new", tool_map=self.editor_map(fake))

    def test_open_seeds_editor_with_current_note(self):
        self.seed_note()
        fake = FakeEditorTerminal(write="# Draft\n\nrewritten body")
        _, out = self.run_command(
            "/notes open 1", tool_map=self.editor_map(fake)
        )
        self.assertEqual(fake.seeds[0], "# Draft\n\noriginal body\n")
        self.assertIn("Edited", out)
        item = self.store().resolve_item("#1")
        self.assertEqual(item["body"], "rewritten body")

    def test_save_is_an_update_event_with_editor_provenance(self):
        self.seed_note()
        fake = FakeEditorTerminal(write="# Renamed\n\noriginal body")
        self.run_command("/notes open 1", tool_map=self.editor_map(fake))
        store = self.store()
        item = store.resolve_item("#1")
        self.assertEqual(item["title"], "Renamed")
        events = store.item_events(item["item_id"])
        updates = [e for e in events if e["kind"] == "item_updated"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["data"]["source"], "editor")
        self.assertEqual(sorted(updates[0]["data"]["fields"]), ["title"])

    def test_open_by_unambiguous_title_substring(self):
        self.seed_note(title="Garden layout", body="roses here")
        fake = FakeEditorTerminal(write="# Garden layout\n\ntulips now")
        _, out = self.run_command(
            "/notes open garden", tool_map=self.editor_map(fake)
        )
        self.assertIn("Edited", out)
        self.assertEqual(self.store().resolve_item("#1")["body"],
                         "tulips now")

    def test_ambiguous_title_lists_candidates_and_stays_out_of_editor(self):
        self.seed_note(title="meeting notes alpha")
        self.seed_note(title="meeting notes beta")
        fake = FakeEditorTerminal(write="# nope\n\nnope")
        _, out = self.run_command(
            "/notes open meeting", tool_map=self.editor_map(fake)
        )
        self.assertIn("2 notes match", out)
        self.assertIn("meeting notes alpha", out)
        self.assertIn("meeting notes beta", out)
        self.assertEqual(fake.calls, [])

    def test_exact_title_match_beats_substring_ambiguity(self):
        self.seed_note(title="recipe")
        self.seed_note(title="recipe ideas")
        _, out = self.run_command("/notes show recipe")
        self.assertIn("#1", out)
        self.assertNotIn("notes match", out)

    def test_unknown_reference(self):
        _, out = self.run_command(
            "/notes open zilch",
            tool_map=self.editor_map(FakeEditorTerminal()),
        )
        self.assertIn("No note matching", out)

    def test_item_from_another_space_is_not_a_note(self):
        self.run_command("/todo add plumber visit",
                         config={"edge_daemon": "true"})
        _, out = self.run_command(
            "/notes show 1"
        )
        self.assertIn("No note matching", out)

    def test_unchanged_buffer_records_no_event(self):
        self.seed_note()
        fake = FakeEditorTerminal(write=None)  # save the seed untouched
        _, out = self.run_command(
            "/notes open 1", tool_map=self.editor_map(fake)
        )
        self.assertIn("No changes", out)
        store = self.store()
        events = store.item_events(store.resolve_item("#1")["item_id"])
        self.assertEqual([e["kind"] for e in events], ["item_added"])

    def test_emptied_buffer_leaves_the_note_alone(self):
        self.seed_note()
        fake = FakeEditorTerminal(write="")
        _, out = self.run_command(
            "/notes open 1", tool_map=self.editor_map(fake)
        )
        self.assertIn("note unchanged", out)
        self.assertEqual(self.store().resolve_item("#1")["body"],
                         "original body")

    def test_show_surfaces_edit_count_and_history_replays(self):
        self.seed_note()
        for body in ("second version", "third version"):
            fake = FakeEditorTerminal(write=f"# Draft\n\n{body}")
            self.run_command("/notes open 1",
                             tool_map=self.editor_map(fake))
        _, out = self.run_command("/notes show 1")
        self.assertIn("third version", out)
        self.assertIn("item_added", out)
        self.assertIn("edited 2 times, last", out)
        store = self.store()
        self.assertEqual(store.verify_integrity()["replay"], "match")

    def test_show_on_fresh_note_says_never_edited(self):
        self.seed_note()
        _, out = self.run_command("/notes show 1")
        self.assertIn("never edited", out)


# ---------------------------------------------------------------------------
# Quick-add, list, search, archive/reopen
# ---------------------------------------------------------------------------

class TestNotesQuickAddAndLifecycle(NotesCommandCase):
    def test_quick_add_grammar_with_tags_and_body(self):
        _, out = self.run_command(
            '/notes add "grocery staples" #home -- milk, eggs'
        )
        self.assertIn("Note saved", out)
        item = self.store().resolve_item("#1")
        self.assertEqual(item["space"], "notes")
        self.assertEqual(item["title"], "grocery staples")
        self.assertEqual(item["tags"], ["home"])
        self.assertEqual(item["body"], "milk, eggs")
        self.assertEqual(item["source"], "chat")

    def test_quick_add_requires_a_title(self):
        _, out = self.run_command("/notes add -- body only")
        self.assertIn("Usage", out)
        self.assertEqual(self.store().list_items(space="notes",
                                                 status="all"), [])

    def test_note_alias_and_registry(self):
        _, out = self.run_command("/note add via the alias")
        self.assertIn("Note saved", out)
        names = slash_command_names()
        self.assertIn("/notes", names)
        self.assertIn("/note", names)

    def test_bare_notes_lists_recent_first_with_hints(self):
        self.run_command("/notes add first note")
        self.run_command("/notes add second note")
        fake = FakeEditorTerminal(write="# first note\n\nbumped body")
        self.run_command("/notes open first",
                         tool_map=self.editor_map(fake))
        _, out = self.run_command("/notes")
        self.assertIn("Notes (2)", out)
        self.assertLess(out.index("first note"), out.index("second note"))
        self.assertIn("new [title]", out)  # the hints line

    def test_bare_notes_empty_state(self):
        _, out = self.run_command("/notes")
        self.assertIn("No notes yet", out)

    def test_search_titles_and_bodies_scoped_to_notes(self):
        self.run_command("/notes add garden plan -- plant the roses")
        self.run_command("/todo add call the plumber",
                         config={"edge_daemon": "true"})
        _, out = self.run_command("/notes search roses")
        self.assertIn("garden plan", out)
        _, out = self.run_command("/notes search garden")
        self.assertIn("garden plan", out)
        _, out = self.run_command("/notes search plumber")
        self.assertIn("No notes matching", out)

    def test_search_requires_text(self):
        _, out = self.run_command("/notes search")
        self.assertIn("Usage", out)

    def test_archive_reopen_lifecycle(self):
        self.run_command("/notes add keep this around")
        _, out = self.run_command("/notes archive 1")
        self.assertIn("[~]", out)
        _, out = self.run_command("/notes")
        self.assertIn("No notes yet", out)
        self.assertIn("1 archived", out)
        # Archived notes stay reachable by title.
        _, out = self.run_command("/notes reopen keep this")
        self.assertIn("[ ]", out)
        _, out = self.run_command("/notes")
        self.assertIn("keep this around", out)
        store = self.store()
        self.assertEqual(store.verify_integrity()["replay"], "match")

    def test_archive_and_reopen_are_idempotent_messages(self):
        self.run_command("/notes add once")
        self.run_command("/notes archive 1")
        _, out = self.run_command("/notes archive 1")
        self.assertIn("already archived", out)
        self.run_command("/notes reopen 1")
        _, out = self.run_command("/notes reopen 1")
        self.assertIn("already open", out)

    def test_unknown_verb_and_help_print_usage(self):
        _, out = self.run_command("/notes frobnicate 1")
        self.assertIn("/notes — editor-backed notes", out)
        _, out = self.run_command("/notes help")
        self.assertIn("$VISUAL", out)


# ---------------------------------------------------------------------------
# Authority gate, credential guard, stored-text inertness
# ---------------------------------------------------------------------------

class TestNotesSafety(NotesCommandCase):
    REFUSAL_MARKERS = ("interactive local session", "/notes add",
                       "personal_items")

    def assert_refused(self, out):
        for marker in self.REFUSAL_MARKERS:
            self.assertIn(marker, out)

    def test_editor_ops_refuse_without_a_terminal_client(self):
        for tool_map in (None, {}):
            _, out = self.run_command("/notes new", tool_map=tool_map)
            self.assert_refused(out)
            _, out = self.run_command("/notes open 1", tool_map=tool_map)
            self.assert_refused(out)
        self.assertEqual(self.store().list_items(space="notes",
                                                 status="all"), [])

    def test_editor_ops_refuse_in_non_interactive_sessions(self):
        # The remote/scheduled/channel wiring: interactive=False on the
        # real client — authority refused even with a live TTY.
        client = InteractiveTerminalClient(
            runner=RecordingRunner("# X\n\nbody")
        )
        client.set_policy(
            LocalShellPolicy(interactive=False, tty_check=lambda: True)
        )
        _, out = self.run_command(
            "/notes new", tool_map={"interactive_terminal": client}
        )
        self.assert_refused(out)

    def test_editor_ops_refuse_without_a_real_tty(self):
        client = InteractiveTerminalClient(
            runner=RecordingRunner("# X\n\nbody")
        )
        client.set_policy(
            LocalShellPolicy(interactive=True, tty_check=lambda: False)
        )
        _, out = self.run_command(
            "/notes new", tool_map={"interactive_terminal": client}
        )
        self.assert_refused(out)

    def test_handoff_smoke_through_the_real_client(self):
        # Real InteractiveTerminalClient + policy plumbing, fake runner:
        # proves /notes rides the exact /terminal handoff path.
        runner = RecordingRunner("# Smoke\n\nvia the real client")
        client = InteractiveTerminalClient(runner=runner)
        client.set_policy(
            LocalShellPolicy(interactive=True, tty_check=lambda: True)
        )
        _, out = self.run_command(
            "/notes new", tool_map={"interactive_terminal": client}
        )
        self.assertIn("Note saved", out)
        self.assertTrue(runner.policy.local_session)
        self.assertTrue(runner.argv[-1].endswith(".md"))
        item = self.store().resolve_item("#1")
        self.assertEqual(item["title"], "Smoke")
        self.assertEqual(item["source"], "editor")

    def test_quick_add_credential_guard(self):
        # Synthetic fixture (repo marker convention) — never real.
        _, out = self.run_command(
            "/notes add forwarded mail -- password: Fak3syntheticXk29"
        )
        self.assertIn("credential pattern", out)
        self.assertEqual(self.store().list_items(space="notes",
                                                 status="all"), [])

    def test_editor_credential_guard_rejects_whole_and_keeps_buffer(self):
        fake = FakeEditorTerminal(
            write="# creds\n\npassword: Fak3syntheticXk29"
        )
        _, out = self.run_command(
            "/notes new", tool_map=self.editor_map(fake)
        )
        self.assertIn("credential pattern", out)
        self.assertEqual(self.store().list_items(space="notes",
                                                 status="all"), [])
        scratch = fake.calls[0]["argv"][-1]
        self.assertIn(scratch, out)          # told where the text lives
        self.assertTrue(os.path.exists(scratch))
        os.unlink(scratch)

    def test_note_content_is_stored_text_never_instructions(self):
        set_agent_mode(False)
        self.addCleanup(set_agent_mode, False)
        self.run_command("/notes add sneaky -- /agent on\napprove 1")
        _, out = self.run_command("/notes show 1")
        self.assertIn("/agent on", out)      # verbatim data
        self.assertFalse(get_agent_mode())
        _, out = self.run_command("/notes")
        self.assertIn("sneaky", out)
        self.assertFalse(get_agent_mode())


if __name__ == "__main__":
    unittest.main()
