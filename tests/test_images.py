"""Image attachments: detection, loading, wire conversion, accounting,
and storage. Behavior pinned here is what reaches the model and the disk."""

import base64
import contextlib
import copy
import io
import json
import stat
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from conch import images
from conch.images import (
    IMAGE_TOKENS,
    ImageError,
    find_image_paths,
    load_image,
    to_provider,
    user_content,
)


def _png_bytes(width=1, height=1):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


PNG = _png_bytes()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _block(name="shot.png", data=None):
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png",
                   "data": data if data is not None else base64.b64encode(PNG).decode()},
        "name": name,
    }


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name).resolve()
        patcher = mock.patch.dict(
            "os.environ", {"XDG_STATE_HOME": str(self.dir / "state"), "HOME": str(self.dir)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _file(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path


class TestFindImagePaths(_TmpCase):
    def setUp(self):
        super().setUp()
        self.spaced = self._file("Screen Shot 1.png", PNG)
        self.plain = self._file("plain.png", PNG)
        self.jpeg = self._file("pic.jpg", JPEG)
        self._file("fake.png", b"not an image at all")
        self._file("notes.txt", PNG)

    def find(self, text):
        return find_image_paths(text, self.dir)

    def test_drag_and_drop_escaped_spaces(self):
        text = "what is this " + str(self.spaced).replace(" ", "\\ ") + " please"
        self.assertEqual(self.find(text), [self.spaced])

    def test_quoted_paths(self):
        self.assertEqual(self.find(f'look "{self.spaced}"'), [self.spaced])
        self.assertEqual(self.find(f"look '{self.spaced}'"), [self.spaced])

    def test_home_relative_and_file_url(self):
        self.assertEqual(self.find("see ~/plain.png"), [self.plain])
        self.assertEqual(self.find("see plain.png."), [self.plain])
        url = "file://" + str(self.spaced).replace(" ", "%20")
        self.assertEqual(self.find(f"open {url}"), [self.spaced])

    def test_missing_non_image_and_mismatched_magic_ignored(self):
        text = "/nope/missing.png notes.txt fake.png https://example.com/plain.png"
        self.assertEqual(self.find(text), [])

    def test_order_kept_and_duplicates_dropped(self):
        text = f"plain.png then pic.jpg then {self.plain}"
        self.assertEqual(self.find(text), [self.plain, self.jpeg])

    def test_apostrophes_in_prose_do_not_hide_paths(self):
        self.assertEqual(self.find("don't open plain.png, it's fine"), [self.plain])


class TestLoadImage(_TmpCase):
    def test_canonical_block(self):
        block = load_image(self._file("a.png", PNG))
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["name"], "a.png")
        self.assertEqual(block["source"]["media_type"], "image/png")
        self.assertEqual(base64.b64decode(block["source"]["data"]), PNG)

    def test_magic_mismatch_and_missing_file_raise(self):
        with self.assertRaises(ImageError):
            load_image(self._file("fake.png", b"hello"))
        with self.assertRaises(ImageError):
            load_image(self.dir / "missing.png")

    def test_oversized_without_downscaler_fails_clearly(self):
        path = self._file("big.png", PNG)
        with mock.patch.object(images, "MAX_RAW_BYTES", 10), \
                mock.patch("conch.images.shutil.which", return_value=None):
            with self.assertRaises(ImageError) as caught:
                load_image(path)
        self.assertIn("too large", str(caught.exception))

    def test_oversized_dimensions_need_downscale(self):
        self.assertTrue(images._needs_downscale(_png_bytes(9000, 1), "image/png"))
        self.assertFalse(images._needs_downscale(PNG, "image/png"))


class TestClipboard(_TmpCase):
    def test_no_tool_returns_reason_instead_of_raising(self):
        with mock.patch("conch.images.sys.platform", "linux"), \
                mock.patch("conch.images.shutil.which", return_value=None):
            path, reason = images.clipboard_image()
        self.assertIsNone(path)
        self.assertTrue(reason)

    def test_linux_capture_is_private(self):
        result = mock.Mock(returncode=0, stdout=PNG)
        def which(name):
            return "/usr/bin/xclip" if name == "xclip" else None

        with mock.patch("conch.images.sys.platform", "linux"), \
                mock.patch("conch.images.shutil.which", side_effect=which), \
                mock.patch("conch.images.subprocess.run", return_value=result):
            path, reason = images.clipboard_image()
        self.assertEqual(reason, "")
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(path.parent, self.dir / "state" / "conch" / "attachments")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_empty_clipboard_leaves_no_file(self):
        result = mock.Mock(returncode=1, stdout=b"")
        with mock.patch("conch.images.sys.platform", "linux"), \
                mock.patch("conch.images.shutil.which", return_value="/usr/bin/wl-paste"), \
                mock.patch("conch.images.subprocess.run", return_value=result):
            path, reason = images.clipboard_image()
        self.assertIsNone(path)
        self.assertTrue(reason)
        self.assertEqual(list((self.dir / "state" / "conch" / "attachments").iterdir()), [])


class TestToProvider(unittest.TestCase):
    def setUp(self):
        self.messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": user_content("describe", [_block()])},
        ]
        self.original = copy.deepcopy(self.messages)

    def test_user_content_shapes(self):
        self.assertEqual(user_content("hi", []), "hi")
        content = user_content("hi", [_block()])
        self.assertEqual(content[0], {"type": "text", "text": "hi"})
        self.assertEqual(content[1]["type"], "image")

    def test_fast_path_returns_same_list(self):
        plain = [{"role": "user", "content": "hi"}]
        for provider in ("anthropic", "openai", "ollama", "codex"):
            self.assertIs(to_provider(provider, plain), plain)

    def test_anthropic_strips_conch_keys(self):
        out = to_provider("anthropic", self.messages)
        image = out[1]["content"][1]
        self.assertNotIn("name", image)
        self.assertEqual(image["source"], self.original[1]["content"][1]["source"])
        self.assertEqual(self.messages, self.original)

    def test_openai_compatible_data_url(self):
        for provider in ("openai", "cerebras", "bedrock", "openrouter", "custom"):
            out = to_provider(provider, self.messages)
            parts = out[1]["content"]
            self.assertEqual(parts[0], {"type": "text", "text": "describe"})
            self.assertEqual(parts[1]["type"], "image_url")
            data = self.original[1]["content"][1]["source"]["data"]
            self.assertEqual(parts[1]["image_url"]["url"], f"data:image/png;base64,{data}")
        self.assertEqual(self.messages, self.original)

    def test_ollama_images_field(self):
        out = to_provider("ollama", self.messages)
        self.assertEqual(out[1]["content"], "describe")
        self.assertEqual(out[1]["images"], [self.original[1]["content"][1]["source"]["data"]])

    def test_unknown_provider_gets_placeholder(self):
        out = to_provider("mystery", self.messages)
        self.assertEqual(out[1]["content"], "describe\n[image: shot.png]")


class TestAdaptersSendImages(unittest.TestCase):
    """The provider adapters put the converted shape on the wire and leave
    the caller's history untouched."""

    def _capture(self, call):
        sent = {}

        def fake_urlopen(req, *args, **kwargs):
            sent["body"] = json.loads(req.data.decode())
            raise OSError("offline")

        with mock.patch("conch.providers.urllib.request.urlopen", side_effect=fake_urlopen):
            call()
        return sent["body"]

    def setUp(self):
        self.messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": user_content("describe", [_block()])},
        ]
        self.original = copy.deepcopy(self.messages)

    def test_openai_raw_and_stream(self):
        from conch import providers

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "k"}):
            for fn in (providers.raw_openai, providers.stream_openai):
                body = self._capture(lambda: fn({"model": "gpt-4o"}, self.messages))
                self.assertEqual(body["messages"][1]["content"][1]["type"], "image_url")
        self.assertEqual(self.messages, self.original)

    def test_anthropic(self):
        from conch import providers

        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}):
            body = self._capture(
                lambda: providers.raw_anthropic({"model": "claude-x"}, self.messages)
            )
        image = body["messages"][0]["content"][1]
        self.assertEqual(image["type"], "image")
        self.assertNotIn("name", image)
        self.assertEqual(self.messages, self.original)

    def test_ollama(self):
        from conch import providers

        with mock.patch("conch.providers.validate_ollama_model", return_value=(True, "")), \
                mock.patch("conch.providers.apply_ollama_request_options"):
            body = self._capture(lambda: providers.raw_ollama({"model": "llava"}, self.messages))
        self.assertEqual(body["messages"][1]["content"], "describe")
        self.assertEqual(len(body["messages"][1]["images"]), 1)
        self.assertEqual(self.messages, self.original)


class TestRuntimeAccounting(unittest.TestCase):
    def test_estimate_tokens_counts_image_as_fixed_cost(self):
        from conch.runtime import estimate_tokens

        big = "A" * 2_000_000
        messages = [{"role": "user", "content": user_content("hi", [_block(data=big)])}]
        tokens = estimate_tokens(messages)
        self.assertGreaterEqual(tokens, IMAGE_TOKENS)
        self.assertLess(tokens, 2 * IMAGE_TOKENS)
        self.assertLess(tokens, len(big) // 4 // 100)

    def test_compaction_never_slices_base64(self):
        from conch.runtime import compress_context

        old_data, new_data = "O" * 4000, "N" * 4000
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": user_content("first", [_block("old.png", old_data)])},
            {"role": "assistant", "content": "x" * 20000},
            {"role": "user", "content": user_content("now", [_block("new.png", new_data)])},
        ]
        with mock.patch("conch.runtime.get_context_limit", return_value=3000):
            compressed = compress_context(messages, None, "openai", {})
        dumped = json.dumps(compressed)
        self.assertNotIn(old_data, dumped)
        self.assertIn(new_data, dumped)  # the current turn keeps its image intact
        for message in compressed:
            for block in message["content"] if isinstance(message["content"], list) else []:
                if block.get("type") == "image":
                    self.assertEqual(block["source"]["data"], new_data)

    def test_compaction_transcript_uses_placeholder(self):
        from conch.runtime import _message_as_text

        text = _message_as_text({"role": "user", "content": user_content("see", [_block()])})
        self.assertIn("[image: shot.png]", text)
        self.assertNotIn(_block()["source"]["data"], text)

    def test_provider_switch_round_trip_keeps_image(self):
        from conch.runtime import (
            normalize_messages_for_provider,
            normalize_messages_on_switch,
            sanitize_anthropic_messages,
        )

        block = _block()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": user_content("describe", [block])},
            {"role": "assistant", "content": "a cat"},
        ]
        sanitize_anthropic_messages(messages)
        normalize_messages_on_switch(messages, "openai")
        wire = to_provider("openai", normalize_messages_for_provider(messages, "openai"))
        self.assertEqual(wire[1]["content"][1]["type"], "image_url")
        normalize_messages_on_switch(messages, "anthropic")
        wire = to_provider("anthropic", normalize_messages_for_provider(messages, "anthropic"))
        self.assertEqual(wire[1]["content"][1]["source"], block["source"])
        wire = to_provider("ollama", normalize_messages_for_provider(messages, "ollama"))
        self.assertEqual(wire[1]["images"], [block["source"]["data"]])


class TestStorage(_TmpCase):
    AWS_KEY = "AKIAIOSFODNN7EXAMPLE"

    def test_scrub_leaves_image_bytes_untouched(self):
        from conch.conversations import REDACTION_MARKER, scrub_messages_for_storage

        data = "QUJD" + self.AWS_KEY + "QUJD"
        messages = [{"role": "assistant", "content": [
            {"type": "text", "text": f"key {self.AWS_KEY}"},
            _block(data=data),
        ]}]
        scrubbed = scrub_messages_for_storage(messages)
        self.assertIn(REDACTION_MARKER, scrubbed[0]["content"][0]["text"])
        self.assertEqual(scrubbed[0]["content"][1], _block(data=data))

    def test_save_load_round_trip(self):
        from conch.conversations import ConversationManager

        manager = ConversationManager()
        conv = manager.create(model="m", provider="anthropic")
        message = {"role": "user", "content": user_content("describe", [_block()])}
        conv.messages = [{"role": "system", "content": "sys"}, message]
        manager.save(conv)
        loaded = manager.load(conv.id)
        self.assertEqual(loaded.messages[1], message)


class TestShellGlue(_TmpCase):
    def test_turn_images_collects_pending_and_typed_paths(self):
        path = self._file("typed.png", PNG)
        pending = [_block("clip.png")]
        lines = []
        blocks = images.turn_images(
            f"what about {path}", pending, provider="openai", model="m",
            config={}, cwd=self.dir, out=lines.append,
        )
        self.assertEqual([b["name"] for b in blocks], ["clip.png", "typed.png"])
        self.assertEqual(pending, [])
        self.assertEqual(len(lines), 1)

    def test_ollama_without_vision_sends_text_only(self):
        path = self._file("typed.png", PNG)
        lines = []
        with mock.patch("conch.providers.ollama_model_supports_vision", return_value=False):
            blocks = images.turn_images(
                str(path), [], provider="ollama", model="qwen3", config={},
                cwd=self.dir, out=lines.append,
            )
        self.assertEqual(blocks, [])
        self.assertIn("qwen3", lines[-1])

    def test_attach_command(self):
        path = self._file("a.png", PNG)
        pending, lines = [], []
        images.handle_attach_command(("attach_image", str(path)), pending, out=lines.append)
        self.assertEqual(len(pending), 1)
        images.handle_attach_command(("attach_image", str(self.dir / "nope.png")),
                                     pending, out=lines.append)
        self.assertEqual(len(pending), 1)
        images.handle_attach_command(("attach_image_clear",), pending, out=lines.append)
        self.assertEqual(pending, [])

    def test_slash_command_parsing(self):
        from conch.commands import handle_slash_command

        def run(text):
            return handle_slash_command(text, {}, "openai", "m", lambda *_: None)

        self.assertEqual(run("/image"), ("attach_image", None))
        self.assertEqual(run("/image clear"), ("attach_image_clear",))
        self.assertEqual(run("/image ~/My\\ Shot.png"),
                         ("attach_image", str(self.dir / "My Shot.png")))
        self.assertEqual(run("/image '/tmp/a b.png'"), ("attach_image", "/tmp/a b.png"))

    def test_seat_prompt_gets_placeholder_and_one_warning(self):
        from conch import seats

        messages = [{"role": "user", "content": user_content("hi", [_block("seat.png")])}]
        err = io.StringIO()
        with mock.patch.object(seats, "_warned_images", set()), \
                contextlib.redirect_stderr(err):
            first = seats._seat_messages("codex", messages)
            seats._seat_messages("codex", messages)
        self.assertIn("[image: seat.png — not sent", first[0]["content"])
        self.assertNotIn(_block()["source"]["data"], json.dumps(first))
        self.assertEqual(err.getvalue().count("seat.png"), 1)


if __name__ == "__main__":
    unittest.main()
