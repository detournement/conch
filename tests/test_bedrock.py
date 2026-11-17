"""Tests for the AWS Bedrock provider (OpenAI-compatible endpoint).

Covers base-URL/region resolution, request body shaping, catalog wiring,
and the <reasoning> stream filter used for kimi-k2-thinking.
"""

import unittest
from unittest.mock import patch

from conch.providers import (
    DEFAULT_API_KEY_ENVS,
    DEFAULT_BEDROCK_REGION,
    DEFAULT_CHAT_MODEL_BY_PROVIDER,
    KNOWN_MODELS,
    RAW_FNS,
    STREAM_FNS,
    _bedrock_body,
    _ReasoningStreamFilter,
    get_bedrock_base_url,
    strip_think_blocks,
)


class TestBedrockBaseUrl(unittest.TestCase):
    def test_default_region(self):
        with patch.dict("os.environ", {}, clear=False):
            for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
                with patch.dict("os.environ", {var: ""}):
                    pass
        self.assertEqual(
            get_bedrock_base_url({}),
            f"https://bedrock-runtime.{DEFAULT_BEDROCK_REGION}.amazonaws.com/openai/v1",
        )

    def test_config_region_wins(self):
        with patch.dict("os.environ", {"AWS_REGION": "eu-west-1"}):
            url = get_bedrock_base_url({"bedrock_region": "us-west-2"})
        self.assertIn("us-west-2", url)

    def test_env_region_fallback(self):
        with patch.dict("os.environ", {"AWS_REGION": "eu-west-1"}):
            url = get_bedrock_base_url({})
        self.assertIn("eu-west-1", url)


class TestBedrockBody(unittest.TestCase):
    def test_default_model_and_shape(self):
        body = _bedrock_body({}, [{"role": "user", "content": "hi"}], None)
        self.assertEqual(body["model"], "moonshotai.kimi-k2.5")
        self.assertIn("max_tokens", body)
        self.assertNotIn("tools", body)

    def test_tools_are_sanitized_copies(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "t",
                "parameters": {"type": "object", "properties": {
                    "xs": {"type": "array"},  # missing items
                }},
            },
        }]
        body = _bedrock_body({}, [], tools)
        sent = body["tools"][0]["function"]["parameters"]["properties"]["xs"]
        self.assertIn("items", sent)
        # original untouched
        self.assertNotIn("items", tools[0]["function"]["parameters"]["properties"]["xs"])


class TestBedrockCatalog(unittest.TestCase):
    def test_registered_everywhere(self):
        self.assertIn("bedrock", KNOWN_MODELS)
        self.assertIn("bedrock", RAW_FNS)
        self.assertIn("bedrock", STREAM_FNS)
        self.assertEqual(DEFAULT_API_KEY_ENVS["bedrock"], "AWS_BEARER_TOKEN_BEDROCK")

    def test_default_model_in_catalog(self):
        self.assertIn(
            DEFAULT_CHAT_MODEL_BY_PROVIDER["bedrock"], KNOWN_MODELS["bedrock"]
        )

    def test_glm_5_registered(self):
        from conch.providers import MODEL_CONTEXT_WINDOWS, MODEL_PRICING
        self.assertIn("zai.glm-5", KNOWN_MODELS["bedrock"])
        self.assertIn("zai.glm-5", MODEL_CONTEXT_WINDOWS)
        self.assertIn("zai.glm-5", MODEL_PRICING)


class TestReasoningStripping(unittest.TestCase):
    def test_strip_reasoning_blocks(self):
        text = "<reasoning>chain of thought</reasoning>The answer is 4."
        self.assertEqual(strip_think_blocks(text), "The answer is 4.")

    def test_strip_unterminated_reasoning(self):
        text = "Answer.<reasoning>cut off mid-"
        self.assertEqual(strip_think_blocks(text), "Answer.")

    def test_think_blocks_still_stripped(self):
        self.assertEqual(strip_think_blocks("<think>x</think>ok"), "ok")


class TestReasoningStreamFilter(unittest.TestCase):
    def _run(self, chunks):
        out = []
        filt = _ReasoningStreamFilter(out.append)
        for chunk in chunks:
            filt.feed(chunk)
        filt.finish()
        return "".join(out)

    def test_passthrough(self):
        self.assertEqual(self._run(["hello ", "world"]), "hello world")

    def test_whole_block_in_one_chunk(self):
        self.assertEqual(
            self._run(["<reasoning>hmm</reasoning>", "answer"]), "answer"
        )

    def test_block_split_across_chunks(self):
        self.assertEqual(
            self._run(["<reas", "oning>hidden</reas", "oning>visible"]), "visible"
        )

    def test_text_around_block(self):
        self.assertEqual(
            self._run(["before<reasoning>x</reasoning>after"]), "beforeafter"
        )

    def test_lone_angle_bracket_not_swallowed(self):
        self.assertEqual(self._run(["a < b"]), "a < b")

    def test_unterminated_block_suppressed(self):
        self.assertEqual(self._run(["ok<reasoning>never ends"]), "ok")


if __name__ == "__main__":
    unittest.main()
