"""Flow-pack engine tests: manifest validation (fail closed), the
bounded template language, pack digests, registry loading/precedence,
and the engine boundaries (packs are data; caps clamp outcomes; secrets
never enter manifests)."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.capitol.errors import CapitolError
from conch.capitol.packs import (
    PackError,
    canonical_json,
    list_packs,
    load_pack,
    load_pack_data,
    pack_digest,
    user_packs_dir,
)
from conch.capitol.packs.engine import build_request, gateway_key
from conch.capitol.packs.templates import (
    RenderContext,
    TemplateError,
    evaluate_expression,
    parse_expression,
    render_formula,
    render_inline,
    render_lines,
)


def _ebay_manifest():
    return copy.deepcopy(load_pack("ebay-listing").raw)


class ManifestValidationTests(unittest.TestCase):
    def test_builtin_ebay_pack_loads_and_digests(self):
        pack = load_pack("ebay-listing")
        self.assertEqual(pack.name, "ebay-listing")
        self.assertTrue(pack.digest.startswith("sha256:"))
        self.assertEqual(pack.digest, pack_digest(pack.raw))
        # canonical json is key-order independent
        shuffled = json.loads(canonical_json(pack.raw))
        self.assertEqual(pack_digest(shuffled), pack.digest)

    def test_unknown_top_level_field_fails_closed(self):
        data = _ebay_manifest()
        data["tools"] = {"grant": "everything"}
        with self.assertRaises(PackError) as raised:
            load_pack_data(data)
        self.assertIn("unknown fields", str(raised.exception))

    def test_unknown_section_field_fails_closed(self):
        data = _ebay_manifest()
        data["approvals"][0]["run_command"] = "rm -rf /"
        with self.assertRaises(PackError):
            load_pack_data(data)

    def test_unsupported_schema_fails_closed(self):
        data = _ebay_manifest()
        data["schema"] = "conch.flow_pack.v2"
        with self.assertRaises(PackError) as raised:
            load_pack_data(data)
        self.assertIn("failing closed", str(raised.exception))

    def test_unknown_intake_kind_fails_closed(self):
        data = _ebay_manifest()
        data["intakes"].append({"kind": "python_hook", "module": "evil"})
        with self.assertRaises(PackError):
            load_pack_data(data)

    def test_unknown_cap_op_fails_closed(self):
        data = _ebay_manifest()
        data["approvals"][0]["caps"]["checks"][0]["op"] = "regex_eval"
        with self.assertRaises(PackError):
            load_pack_data(data)

    def test_binding_must_reference_known_workflow(self):
        data = _ebay_manifest()
        data["bindings"]["publish"]["workflow"] = "nonexistent"
        with self.assertRaises(PackError):
            load_pack_data(data)

    def test_bad_template_expression_fails_at_load(self):
        data = _ebay_manifest()
        data["requests"]["draft"]["fields"]["app_id"] = "${os.environ.PATH}"
        with self.assertRaises(CapitolError):
            load_pack_data(data)

    def test_phase_machine_is_engine_owned(self):
        data = _ebay_manifest()
        data["flow"]["phases"] = ["custom", "phases"]
        with self.assertRaises(PackError):
            load_pack_data(data)


class TemplateLanguageTests(unittest.TestCase):
    def _ctx(self, **kwargs):
        return RenderContext(**kwargs)

    def test_config_ref_default_and_required(self):
        ctx = self._ctx(config={"a": " x ", "empty": ""})
        self.assertEqual(
            evaluate_expression(parse_expression("${config.a}"), ctx), "x"
        )
        self.assertEqual(
            evaluate_expression(
                parse_expression("${config.missing:fallback}"), ctx
            ),
            "fallback",
        )
        self.assertEqual(
            evaluate_expression(
                parse_expression("${config.empty:fallback}"), ctx
            ),
            "fallback",
        )
        evaluate_expression(
            parse_expression("${config.missing!required}"), ctx
        )
        self.assertEqual(ctx.missing_required, ["missing"])

    def test_nested_default_expression(self):
        ctx = self._ctx(config={"actor": "cfg-actor"},
                        contract={"actor_principal_id": ""})
        value = evaluate_expression(
            parse_expression(
                "${contract.actor_principal_id:${config.actor:}}"
            ),
            ctx,
        )
        self.assertEqual(value, "cfg-actor")

    def test_projection_preserves_original_index_and_skips(self):
        ctx = self._ctx(contract={"media": [
            {"artifact_id": "a", "digest": "d1"},
            {"digest": "orphan"},
            {"artifact_id": "c", "digest": "d3"},
        ]})
        value = evaluate_expression(
            parse_expression(
                "${contract.media[].{artifact_id, digest} +order}"
            ),
            ctx,
        )
        self.assertEqual(value, [
            {"artifact_id": "a", "digest": "d1", "order": 0},
            {"artifact_id": "c", "digest": "d3", "order": 2},
        ])

    def test_nonempty_fails_closed(self):
        ctx = self._ctx(contract={"media": []})
        with self.assertRaises(TemplateError):
            evaluate_expression(
                parse_expression(
                    "${contract.media[].{artifact_id} !nonempty}"
                ),
                ctx,
            )

    def test_arithmetic_increment(self):
        ctx = self._ctx(contract={"revision": 4})
        self.assertEqual(
            evaluate_expression(
                parse_expression("${contract.revision + 1}"), ctx
            ),
            5,
        )

    def test_fill_missing_and_complete(self):
        ctx = self._ctx(
            config={"cfg_b": "from-config"},
            contract={"policies": {"a": "kept", "b": ""}},
        )
        expr = ("${contract.policies | fill_missing(config: a=cfg_a, "
                "b=cfg_b) !complete}")
        value = evaluate_expression(parse_expression(expr), ctx)
        self.assertEqual(value, {"a": "kept", "b": "from-config"})
        ctx_missing = self._ctx(config={}, contract={"policies": {}})
        with self.assertRaises(TemplateError) as raised:
            evaluate_expression(parse_expression(expr), ctx_missing)
        self.assertIn("a, b", str(raised.exception))

    def test_whole_contract_reference_is_identity(self):
        contract = {"schema": "x.v1", "n": 1}
        ctx = self._ctx(contract=contract)
        self.assertIs(
            evaluate_expression(parse_expression("${contract}"), ctx),
            contract,
        )

    def test_unknown_root_and_engine_function_fail(self):
        with self.assertRaises(TemplateError):
            parse_expression("${secrets.token}")
        with self.assertRaises(TemplateError):
            parse_expression("${engine.shell('rm -rf /')}")

    def test_formula_tokens(self):
        values = {"revision": 2, "draft_hash": "sha256:" + "ab" * 32,
                  "listing": {"title": "T" * 300, "quantity": 1},
                  "media": [1, 2, 3]}
        self.assertEqual(
            render_formula("POST r{revision} {draft_hash[-12:]}", values),
            "POST r2 " + ("ab" * 32)[-12:],
        )
        self.assertEqual(
            render_formula("{listing.title:5}", values), "TTTTT"
        )
        self.assertEqual(render_formula("{#media}", values), "3")
        self.assertEqual(
            render_formula("({listing.missing:40=untitled})", values),
            "(untitled)",
        )

    def test_render_lines_conditionals_and_each(self):
        spec = [
            "Draft r{revision}",
            {"if": "listing.description", "line": "About {listing.description:10}"},
            {"each": "warnings", "line": "! {item:20}"},
        ]
        lines = render_lines(spec, {
            "revision": 1, "listing": {}, "warnings": ["w1", "w2"],
        })
        self.assertEqual(lines, ["Draft r1", "! w1", "! w2"])
        inline = render_inline(
            [
                "Effect: {state:10=?}",
                {"if": "listing_id", "append": " — {listing_id}"},
            ],
            {"state": "", "listing_id": "77"},
        )
        self.assertEqual(inline, "Effect: ? — 77")


class RequestTemplateTests(unittest.TestCase):
    def setUp(self):
        self.pack = load_pack("ebay-listing")

    def test_require_fails_closed_on_missing_contract_field(self):
        with self.assertRaises(CapitolError) as raised:
            build_request(self.pack, "publish", config={},
                          contract={"app_id": "a"})
        self.assertIn("refusing to construct", str(raised.exception))

    def test_missing_required_config_keys_collected(self):
        with self.assertRaises(CapitolError) as raised:
            build_request(
                self.pack, "draft", config={},
                session={"id": "s", "thread_key": "t", "media": []},
                intake_text="ctx",
            )
        text = str(raised.exception)
        self.assertIn("ebay_actor_id", text)
        self.assertIn("ebay_merchant_location_key", text)

    def test_gateway_key_retry_suffix(self):
        contract = {
            "schema": "ebay.listing_revision.v1", "app_id": "app",
            "listing_session_id": "sess", "revision": 3,
            "draft_hash": "sha256:" + "9" * 64, "channel": "a2a",
            "thread_id": "t",
        }
        request = build_request(self.pack, "publish", config={},
                                contract=contract)
        self.assertEqual(request["idempotency_key"],
                         "app:sess:r3:publish")
        self.assertEqual(
            gateway_key(self.pack, "publish", request, attempt=1),
            "app:sess:r3:publish",
        )
        self.assertEqual(
            gateway_key(self.pack, "publish", request, attempt=2),
            "app:sess:r3:publish:retry2",
        )


class RegistryTests(unittest.TestCase):
    def test_user_pack_dir_precedence_and_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(Path(tmp))}):
                packs_dir = user_packs_dir()
                override = packs_dir / "ebay-listing"
                override.mkdir(parents=True)
                data = _ebay_manifest()
                data["pack"]["version"] = "9.9.9"
                (override / "pack.json").write_text(json.dumps(data))
                pack = load_pack("ebay-listing")
                self.assertEqual(pack.version, "9.9.9")
                self.assertIn(str(override), pack.source)
                names = [p.name for p in list_packs()]
                self.assertEqual(names.count("ebay-listing"), 1)

    def test_missing_pack_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(Path(tmp))}):
                with self.assertRaises(PackError):
                    load_pack("no-such-pack")

    def test_dir_name_must_match_pack_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(Path(tmp))}):
                bad = user_packs_dir() / "impostor"
                bad.mkdir(parents=True)
                (bad / "pack.json").write_text(
                    json.dumps(_ebay_manifest())
                )
                with self.assertRaises(PackError) as raised:
                    load_pack("impostor")
                self.assertIn("does not match", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
