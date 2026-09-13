"""The shipped-skills convention and the capitol / pack-author skills.

Proven here: built-in package skills (conch/skills_data/<name>/SKILL.md)
are discovered by the same loader as user skills, with user skills
winning by name (the flow-pack registry rule); the two shipped skills
carry valid trigger-rich frontmatter, scope the capitol_control tool,
fit the injection budget un-truncated, and point the model at their
companion reference/cookbook files; the pack-author reference documents
the grammar the engine actually implements (checked against the
manifest module's own constants, not the design doc); a skill-scoped
sub-turn is the explicit offer of capitol_control; and the capitol
cookbook's backfill/watch/artifact recipes execute as written against
the fake gateway.
"""

import io
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from conch.capitol.packs.manifest import (
    _CAP_OPS,
    _TOP_LEVEL_KEYS,
    ENGINE_PHASES,
    ENGINE_RECOVERY_ORDER,
)
from conch.capitol.tool import CapitolSessionClient, derive_idempotency_key
from conch.skills import (
    SKILL_BODY_MAX_CHARS,
    build_skills_context,
    builtin_skills_dir,
    get_skill,
    load_skills,
    render_skill,
)
from conch.tooling import DelegateTaskClient, ToolRuntimeState

from tests.test_capitol_client import AGENT, BEARER, ORG, FakeGateway

SHIPPED = ("capitol", "pack-author")


class IsolatedDirsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user_dir = self.root / "config" / "conch" / "skills"
        self.user_dir.mkdir(parents=True)


class TestBuiltinSkillLoading(IsolatedDirsCase):
    def test_shipped_skills_discovered(self):
        skills = load_skills()
        for name in SHIPPED:
            self.assertIn(name, skills)
            skill = skills[name]
            self.assertTrue(skill.get("builtin"))
            self.assertTrue(Path(skill["path"]).name == "SKILL.md")
            self.assertTrue(Path(skill["dir"]).is_dir())

    def test_user_skill_wins_by_name(self):
        (self.user_dir / "capitol.md").write_text(
            "---\nname: capitol\ndescription: my override\n---\n"
            "Do it my way."
        )
        skill = load_skills()["capitol"]
        self.assertEqual(skill["description"], "my override")
        self.assertNotIn("builtin", skill)

    def test_companion_files_ship_beside_each_skill(self):
        for name in SHIPPED:
            directory = builtin_skills_dir() / name
            for companion in ("reference.md", "cookbook.md"):
                path = directory / companion
                self.assertTrue(path.is_file(), f"{name}/{companion}")
                self.assertGreater(len(path.read_text()), 1000,
                                   f"{name}/{companion} looks empty")

    def test_render_points_at_companions_untruncated(self):
        for name in SHIPPED:
            skill = get_skill(name)
            self.assertLessEqual(len(skill["body"]), SKILL_BODY_MAX_CHARS,
                                 f"{name} body would be truncated")
            rendered = render_skill(skill)
            self.assertNotIn("[skill truncated]", rendered)
            self.assertIn("reference.md", rendered)
            self.assertIn("cookbook.md", rendered)
            self.assertIn(skill["dir"], rendered)

    def test_skills_context_advertises_both(self):
        context = build_skills_context()
        for name in SHIPPED:
            self.assertIn(name, context)

    def test_package_data_globs_cover_the_files(self):
        pyproject = Path(builtin_skills_dir()).parent.parent / "pyproject.toml"
        text = pyproject.read_text()
        self.assertIn('skills_data/*/*.md', text)


class TestFrontmatter(IsolatedDirsCase):
    def test_capitol_frontmatter(self):
        skill = get_skill("capitol")
        self.assertEqual(skill["name"], "capitol")
        # Trigger-rich, A2Actrl-style: names the tool, the verbs, and
        # the artifacts that should summon it.
        for trigger in ("Use when", "run", "backfill", "HITL",
                        "artifacts", "evals", "capitol_control"):
            self.assertIn(trigger, skill["description"])
        self.assertEqual(skill["tools"],
                         ["capitol_control", "local_shell"])

    def test_pack_author_frontmatter(self):
        skill = get_skill("pack-author")
        self.assertEqual(skill["name"], "pack-author")
        for trigger in ("Use when", "flow pack", "pack.json",
                        "conch.flow_pack.v1", "caps"):
            self.assertIn(trigger, skill["description"])
        self.assertEqual(skill["tools"],
                         ["local_shell", "capitol_control"])

    def test_capitol_hard_rules_present(self):
        body = get_skill("capitol")["body"]
        self.assertIn("Never invent workflow ids", body)
        self.assertIn("Always key effectful starts", body)
        self.assertIn("Park on ambiguity", body)
        self.assertIn("Relay HITL questions verbatim", body)
        self.assertIn("Admin is user-explicit", body)

    def test_pack_author_states_the_invariants_verbatim(self):
        body = get_skill("pack-author")["body"]
        self.assertIn("Packs are data, not code", body)
        self.assertIn("Packs cannot grant tools or\n   authority",
                      body.replace("**", ""))
        self.assertIn("Caps clamp outcomes; they never script reasoning",
                      body.replace("**", "").replace('"', '"'))


class TestPackAuthorReferenceMatchesEngine(IsolatedDirsCase):
    """The reference documents the implemented grammar — checked against
    the manifest module's own constants so doc drift fails a test."""

    def _reference(self) -> str:
        return (builtin_skills_dir() / "pack-author" /
                "reference.md").read_text()

    def test_every_top_level_section_documented(self):
        text = self._reference()
        for section in _TOP_LEVEL_KEYS:
            self.assertIn(section, text, f"section {section} undocumented")

    def test_cap_ops_match_the_engine(self):
        text = self._reference()
        for op in _CAP_OPS:
            self.assertIn(f"`{op}`", text)
        # The unimplemented design ops appear only as named divergences.
        self.assertIn("gte_float", text)
        self.assertIn("not implemented", text)

    def test_phase_machine_documented_as_fixed(self):
        text = self._reference()
        for phase in ENGINE_PHASES:
            self.assertIn(phase, text)
        for step in ENGINE_RECOVERY_ORDER:
            self.assertIn(step, text)
        self.assertIn("not\nprogrammable", text)

    def test_id_scheme_and_fill_missing_divergences(self):
        text = self._reference()
        self.assertIn("channel-sha16", text)
        self.assertIn("field=config_key", text)
        self.assertIn("golden_scenarios", text)


class TestSkillScopedOffer(IsolatedDirsCase):
    """The shipped capitol skill is the explicit offer of the
    implicitly-excluded capitol_control tool (personal_items precedent)."""

    def test_capitol_skill_scopes_the_tool_into_a_subturn(self):
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients, **kwargs):
            seen["tools"] = {t["function"]["name"] for t in tools}
            seen["clients"] = set(builtin_clients)
            seen["system"] = messages[0]["content"]
            return "done", {}

        client = DelegateTaskClient()
        all_tools = [
            {"function": {"name": "local_shell"}},
            {"function": {"name": "capitol_control"}},
            {"function": {"name": "public_api"}},
        ]
        state = ToolRuntimeState(all_tools=all_tools, tool_map={},
                                 tools=all_tools)
        builtins = {
            "local_shell": object(),
            "capitol_control": object(),
            "public_api": object(),
            "delegate_task": client,
        }
        client.bind({"provider": "openai"}, state, builtins)
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool("delegate_task", {
                "task": "backfill funding intake for last week",
                "skill": "capitol",
            })
        self.assertEqual(seen["tools"], {"capitol_control", "local_shell"})
        self.assertIn("capitol_control", seen["clients"])
        # The skill body (with its hard rules) rode into the prompt.
        self.assertIn("Never invent workflow ids", seen["system"])


class TestCookbookSmoke(IsolatedDirsCase):
    """The capitol cookbook's recipes, executed as written against the
    fake gateway: backfill start (recipe 1), watch-and-summarize
    (recipe 2), and the docx artifact fetch with the PK check
    (recipe 4)."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        super().setUp()
        FakeGateway.reset(self.port)
        patcher = patch.dict(os.environ, {"CAPITOL_A2A_BEARER": BEARER})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = CapitolSessionClient({
            "capitol_base_url": f"http://127.0.0.1:{self.port}",
            "capitol_org": ORG,
            "capitol_agent": AGENT,
        })

    def call(self, args):
        return self.client.call_tool(
            "capitol_control", args
        )["content"][0]["text"]

    def test_backfill_watch_outputs_sequence(self):
        window = "after:2026/09/01 before:2026/09/08"
        # 1. discover — pick the id from the catalog.
        catalog = self.call({"op": "workflows"})
        self.assertIn("draft-wf", catalog)
        # 2. describe — the input contract names the request-input key.
        described = self.call({"op": "describe", "workflow_id": "draft-wf"})
        self.assertIn("node-json-input.value", described)
        # 3. start keyed with the window as input_value.
        started = self.call({
            "op": "start", "workflow_id": "draft-wf",
            "input_value": window,
        })
        self.assertIn("run run-1 started", started)
        self.assertIn(
            derive_idempotency_key(
                "draft-wf", {"node-json-input.value": window}
            ),
            started,
        )
        _, data, _ = FakeGateway.calls[-1]
        self.assertEqual(data["inputs"], {"node-json-input.value": window})
        # Retrying the backfill replays instead of double-ingesting.
        self.assertIn("replayed", self.call({
            "op": "start", "workflow_id": "draft-wf",
            "input_value": window,
        }))
        # 4. watch bounded → terminal summary → outputs.
        FakeGateway.runs["run-1"] = {
            "status": "success",
            "output": {"result": {"ledgered": 2, "packets": 1}},
        }
        FakeGateway.run_events["run-1"] = [
            {"run_id": "run-1", "sequence": 1,
             "event_type": "node.node_started", "scope": "node",
             "node": {"node_id": "n1", "display_name": "Triage"},
             "data": {}},
            {"run_id": "run-1", "sequence": 2,
             "event_type": "workflow.run_completed", "scope": "workflow",
             "node": {}, "data": {}},
        ]
        watched = self.call({
            "op": "watch", "run_id": "run-1", "deadline_seconds": 30,
        })
        self.assertIn("terminal state success", watched)
        self.assertIn("op='outputs'", watched)
        outputs = self.call({"op": "outputs", "run_id": "run-1"})
        self.assertIn('"packets": 1', outputs)

    def test_docx_artifact_fetch_with_pk_check(self):
        FakeGateway.blobs["file-docx"] = b"PK\x03\x04rest-of-docx"
        text = self.call({
            "op": "download", "file_id": "file-docx",
            "filename": "packet.docx",
        })
        self.assertIn("downloaded to", text)
        path = Path(text.split("downloaded to ", 1)[1].split(" (", 1)[0])
        self.assertTrue(path.is_file())
        # The cookbook's verification step: PK zip magic.
        self.assertEqual(path.read_bytes()[:2], b"PK")
        from conch.channels import quarantine_dir

        self.assertTrue(str(path).startswith(str(quarantine_dir())))

    def test_clarification_relay_sequence(self):
        FakeGateway.runs["run-1"] = {"status": "success", "output": {}}
        FakeGateway.run_events["run-1"] = [
            {"run_id": "run-1", "sequence": 1,
             "event_type": "node.input_required", "scope": "node",
             "node": {"node_id": "n1", "display_name": "Research Agent"},
             "data": {"request_id": "req-42",
                      "input_kind": "clarification",
                      "prompt": "Which fund vintage should the packet "
                                "target?"}},
            {"run_id": "run-1", "sequence": 2,
             "event_type": "workflow.run_completed", "scope": "workflow",
             "node": {}, "data": {}},
        ]
        watched = self.call({"op": "watch", "run_id": "run-1"})
        self.assertIn("request_id=req-42", watched)
        self.assertIn("Which fund vintage", watched)
        answered = self.call({
            "op": "respond", "run_id": "run-1", "request_id": "req-42",
            "response": "2024 vintage, per the user",
        })
        self.assertIn("clarification answered", answered)
        skill, data, _ = FakeGateway.calls[-1]
        self.assertEqual(skill, "submit_clarification_response")
        self.assertEqual(data["response"], "2024 vintage, per the user")


class TestJsonExamplesParse(unittest.TestCase):
    """Every ```json block in the shipped skills parses — a cookbook
    with unparseable examples is the bug."""

    def _blocks(self, path: Path):
        text = path.read_text()
        blocks = []
        chunks = text.split("```json")
        for chunk in chunks[1:]:
            blocks.append(chunk.split("```", 1)[0].strip())
        return blocks

    def test_all_json_blocks_parse(self):
        for name in SHIPPED:
            for doc in ("SKILL.md", "reference.md", "cookbook.md"):
                path = builtin_skills_dir() / name / doc
                if not path.is_file():
                    continue
                for index, block in enumerate(self._blocks(path)):
                    for line in block.splitlines():
                        line = line.strip()
                        if not line or not line.startswith(("{", "[")):
                            continue
                    try:
                        parsed = [
                            json.loads(candidate)
                            for candidate in self._split_objects(block)
                        ]
                    except ValueError as exc:
                        self.fail(
                            f"{name}/{doc} json block {index} does not "
                            f"parse: {exc}\n{block[:400]}"
                        )
                    self.assertTrue(parsed or block == "")

    @staticmethod
    def _split_objects(block: str):
        """A block may hold several whole-line JSON objects (tool-call
        sequences); split on top-level braces."""
        objects = []
        depth = 0
        current = []
        for char in block:
            if char in "{[":
                depth += 1
            if depth > 0:
                current.append(char)
            if char in "}]":
                depth -= 1
                if depth == 0 and current:
                    objects.append("".join(current))
                    current = []
        return objects or ([block] if block.strip() else [])


if __name__ == "__main__":
    unittest.main()
