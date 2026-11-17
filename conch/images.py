"""Image attachments: find, load, and convert images for the model.

Conversation history stores one canonical image block (inside a user
message's list ``content``)::

    {"type": "image",
     "source": {"type": "base64", "media_type": "image/png", "data": "..."},
     "name": "shot.png"}

``name`` is conch-only (it labels the image in transcripts and placeholders)
and never reaches an API: :func:`to_provider` rewrites history into each
provider's wire shape at request time, so a conversation can switch between
providers and still send its images correctly.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Callable, List, Optional, Tuple

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
# Keys conch adds to image blocks for its own use; stripped before any request.
CONCH_ONLY_KEYS = ("name",)
# Rough per-image prompt cost used for context estimates (Anthropic bills
# ~(w*h)/750 tokens, ~1.6k for a 1092x1092 image). Counting base64 length
# instead would make one screenshot look like half a million tokens.
IMAGE_TOKENS = 1600
# Anthropic rejects base64 images over 5 MB (encoded) or ~8000 px a side.
# 3.75 MB raw is 5 MB once base64-encoded.
MAX_RAW_BYTES = 3_750_000
MAX_DIMENSION = 8000
DOWNSCALE_PX = 1568

OPENAI_COMPATIBLE = ("openai", "cerebras", "bedrock", "openrouter", "custom")


class ImageError(ValueError):
    """An image could not be attached; the message is shown to the user."""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def sniff_mime(data: bytes) -> Optional[str]:
    """Media type from magic bytes; None for anything that isn't an image
    type every vision API accepts (the extension alone is never trusted)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_head(path: Path, size: int = 32) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def _is_image_file(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    return path.suffix.lower() in IMAGE_EXTENSIONS and sniff_mime(_read_head(path)) is not None


_FILE_URL_RE = re.compile(r"file://[^\s'\"<>]+")
_QUOTED_RE = re.compile(r"\"([^\"\n]+)\"|'([^'\n]+)'")
# A shell-ish word: backslash escapes (macOS drag-and-drop writes
# `/Users/me/Screen\ Shot\ 1.png`) or any non-space, non-quote char.
_WORD_RE = re.compile(r"(?:\\.|[^\s'\"\\])+")
_UNESCAPE_RE = re.compile(r"\\(.)")
_LEADING_PUNCT = "([{<"
_TRAILING_PUNCT = ".,;:!?)]}>"


def _file_url_path(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc not in ("", "localhost"):
        return ""
    return urllib.parse.unquote(parsed.path)


def _candidate_variants(raw: str) -> List[str]:
    """*raw* plus copies with sentence punctuation peeled off, so
    "see ~/a.png." and "(~/a.png)" still find the file."""
    variants = [raw]
    text = raw.lstrip(_LEADING_PUNCT)
    if text != raw:
        variants.append(text)
    while text and text[-1] in _TRAILING_PUNCT:
        text = text[:-1]
        variants.append(text)
    return variants


def _resolve(candidate: str, cwd: Path) -> Optional[Path]:
    if not candidate:
        return None
    path = Path(os.path.expanduser(candidate))
    if not path.is_absolute():
        path = cwd / path
    return path


def find_image_paths(text: str, cwd) -> List[Path]:
    """Image files referenced in a typed or pasted message.

    Recognizes absolute, ``~`` and cwd-relative paths, ``file://`` URLs,
    single/double-quoted paths, and backslash-escaped spaces. Only existing
    regular files with an image extension whose magic bytes match count;
    results are deduplicated and kept in the order they appear.
    """
    if not text or not any(ext in text.lower() for ext in IMAGE_EXTENSIONS):
        return []
    base = Path(cwd)
    found: List[Tuple[int, List[str]]] = []
    masked = list(text)

    def _mask(match) -> None:
        for index in range(match.start(), match.end()):
            masked[index] = " "

    for match in _FILE_URL_RE.finditer(text):
        found.append((match.start(), _candidate_variants(_file_url_path(match.group(0)))))
        _mask(match)
    for match in _QUOTED_RE.finditer("".join(masked)):
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        inner = inner.strip()
        if inner.startswith("file://"):
            inner = _file_url_path(inner)
        path = _resolve(inner, base)
        # Only a quoted span that names an image is consumed; prose
        # apostrophes ("don't … it's") must not hide the paths between them.
        if path is not None and inner.lower().endswith(IMAGE_EXTENSIONS) and _is_image_file(path):
            found.append((match.start(), [inner]))
            _mask(match)
    for match in _WORD_RE.finditer("".join(masked)):
        word = _UNESCAPE_RE.sub(r"\1", match.group(0))
        if "://" in word:
            continue
        found.append((match.start(), _candidate_variants(word)))

    found.sort(key=lambda item: item[0])
    results: List[Path] = []
    seen = set()
    for _position, variants in found:
        for candidate in variants:
            if not candidate.lower().endswith(IMAGE_EXTENSIONS):
                continue
            path = _resolve(candidate, base)
            if path is None or not _is_image_file(path):
                continue
            try:
                key = path.resolve()
            except OSError:
                key = path
            if key not in seen:
                seen.add(key)
                results.append(path)
            break
    return results


def parse_path_arg(arg: str) -> str:
    """A single path typed after ``/image``: quotes, backslash escapes,
    ``file://`` and ``~`` handled; relative paths stay relative."""
    text = (arg or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    else:
        text = _UNESCAPE_RE.sub(r"\1", text)
    if text.startswith("file://"):
        text = _file_url_path(text)
    return os.path.expanduser(text)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def image_dimensions(data: bytes, mime: str) -> Optional[Tuple[int, int]]:
    """(width, height) parsed from the header, or None when unknown."""
    try:
        if mime == "image/png" and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if mime == "image/gif" and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if mime == "image/webp" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8 ":
                width, height = struct.unpack("<HH", data[26:30])
                return width & 0x3FFF, height & 0x3FFF
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
        if mime == "image/jpeg":
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                    index += 1 if marker == 0xFF else 2
                    continue
                length = struct.unpack(">H", data[index + 2:index + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", data[index + 5:index + 9])
                    return width, height
                index += 2 + length
    except (struct.error, IndexError):
        return None
    return None


def _needs_downscale(data: bytes, mime: str) -> bool:
    if len(data) > MAX_RAW_BYTES:
        return True
    dims = image_dimensions(data, mime)
    return bool(dims) and max(dims) > MAX_DIMENSION


def _downscale(path: Path, name: str,
                dims: Optional[Tuple[int, int]] = None) -> Tuple[bytes, str]:
    """Shrink with macOS ``sips`` (no imaging library dependency): same
    format first, JPEG if that is still too large. ``sips -Z`` also
    upscales, so an image already under DOWNSCALE_PX keeps its size."""
    sips = shutil.which("sips")
    if not sips:
        raise ImageError(
            f"{name} is too large to send (limit {MAX_RAW_BYTES / 1e6:.2f} MB "
            f"and {MAX_DIMENSION}px a side) and no downscaler is available "
            "(macOS sips) — resize it and attach again"
        )
    workdir = tempfile.mkdtemp(prefix="conch-image-")
    try:
        px = str(min(DOWNSCALE_PX, max(dims)) if dims else DOWNSCALE_PX)
        attempts = (
            ("same", ["-Z", px], path.suffix or ".png"),
            ("jpeg", ["-s", "format", "jpeg", "-s", "formatOptions", "80",
                      "-Z", px], ".jpg"),
        )
        for label, options, suffix in attempts:
            out = Path(workdir) / f"scaled-{label}{suffix}"
            try:
                proc = subprocess.run(
                    [sips, *options, str(path), "--out", str(out)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    timeout=60,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ImageError(f"could not downscale {name}: {exc}") from exc
            if proc.returncode != 0 or not out.is_file():
                continue
            data = out.read_bytes()
            mime = sniff_mime(data)
            if mime and not _needs_downscale(data, mime):
                return data, mime
        raise ImageError(f"could not downscale {name} below the size limit")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def load_image(path, *, name: str = "") -> dict:
    """Read *path* into the canonical image block, downscaling when the
    file exceeds the API limits. Raises :class:`ImageError`."""
    path = Path(os.path.expanduser(str(path)))
    name = name or path.name
    try:
        if not path.is_file():
            raise ImageError(f"{path}: no such file")
        data = path.read_bytes()
    except OSError as exc:
        raise ImageError(f"{path}: {exc.strerror or exc}") from exc
    mime = sniff_mime(data)
    if mime is None:
        raise ImageError(f"{name} is not a PNG, JPEG, GIF, or WebP image")
    if _needs_downscale(data, mime):
        data, mime = _downscale(path, name, image_dimensions(data, mime))
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": base64.b64encode(data).decode("ascii"),
        },
        "name": name,
    }


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

def attachments_dir() -> Path:
    """Private directory for clipboard captures (0700)."""
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = Path(state) / "conch" / "attachments"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _private_file() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = attachments_dir() / f"clipboard-{stamp}-{secrets.token_hex(3)}.png"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    return path


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def clipboard_image() -> Tuple[Optional[Path], str]:
    """Save the clipboard's image as a private PNG.

    Returns ``(path, "")`` on success, ``(None, reason)`` when there is no
    image or no clipboard tool — never raises for a missing tool.
    """
    if sys.platform == "darwin":
        if not shutil.which("osascript"):
            return None, "osascript is not available to read the clipboard"
        commands = None
    else:
        commands = []
        if shutil.which("wl-paste"):
            commands.append(["wl-paste", "--no-newline", "--type", "image/png"])
        if shutil.which("xclip"):
            commands.append(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
        if not commands:
            return None, ("no clipboard tool found — install wl-clipboard "
                          "(wl-paste) or xclip, or attach a file: /image <path>")
    try:
        path = _private_file()
    except OSError as exc:
        return None, f"cannot create the attachments directory: {exc}"

    if commands is None:
        script = (
            "set png to (the clipboard as «class PNGf»)\n"
            f"set fh to open for access (POSIX file {_applescript_string(str(path))}) "
            "with write permission\n"
            "set eof fh to 0\n"
            "write png to fh\n"
            "close access fh\n"
        )
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _discard(path)
            return None, f"could not read the clipboard: {exc}"
        if proc.returncode != 0:
            _discard(path)
            return None, "the clipboard has no image"
    else:
        data = b""
        for command in commands:
            try:
                proc = subprocess.run(
                    command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if proc.returncode == 0 and proc.stdout:
                data = proc.stdout
                break
        if not data:
            _discard(path)
            return None, "the clipboard has no image"
        with open(path, "wb") as handle:
            handle.write(data)

    if sniff_mime(_read_head(path)) is None:
        _discard(path)
        return None, "the clipboard has no image"
    return path, ""


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def is_image_block(block) -> bool:
    return isinstance(block, dict) and block.get("type") == "image"


def placeholder(block: dict, note: str = "") -> str:
    name = block.get("name") or "image"
    return f"[image: {name} — {note}]" if note else f"[image: {name}]"


def image_bytes(block: dict) -> int:
    data = (block.get("source") or {}).get("data") or ""
    return len(data) * 3 // 4


def format_size(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1000:
        return f"{size / 1000:.0f} KB"
    return f"{size} B"


def attached_line(block: dict) -> str:
    return (f"  📎 {block.get('name') or 'image'} ({format_size(image_bytes(block))})"
            " \033[2m— sent with your next message\033[0m")


def user_content(text: str, blocks: List[dict]):
    """User message content: plain text, or text plus image blocks."""
    if not blocks:
        return text
    return [{"type": "text", "text": text}, *blocks]


def _message_has_images(message) -> bool:
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    return isinstance(content, list) and any(is_image_block(b) for b in content)


def has_images(messages) -> bool:
    return any(_message_has_images(message) for message in messages or ())


def _replace_images(message: dict, note: str) -> dict:
    """*message* with image blocks turned into placeholder text; the
    content collapses to a string when only text remains."""
    blocks = []
    for block in message["content"]:
        if is_image_block(block):
            blocks.append({"type": "text", "text": placeholder(block, note)})
        else:
            blocks.append(block)
    if all(isinstance(b, dict) and b.get("type") == "text" for b in blocks):
        return {**message, "content": "\n".join(str(b.get("text", "")) for b in blocks)}
    return {**message, "content": blocks}


def text_only(messages: List[dict], note: str = "") -> List[dict]:
    """*messages* with every image replaced by ``[image: name]`` text
    (``[image: name — note]`` with *note*). Same list when there are none."""
    if not has_images(messages):
        return messages
    return [
        _replace_images(m, note) if _message_has_images(m) else m
        for m in messages
    ]


def without_payloads(messages: List[dict]) -> Tuple[List[dict], int]:
    """(messages with images as placeholder text, image count) — for size
    estimates that must never count base64 as prompt text."""
    if not has_images(messages):
        return messages, 0
    count = sum(
        1 for m in messages if _message_has_images(m)
        for b in m["content"] if is_image_block(b)
    )
    return text_only(messages), count


def _texts(content: list) -> List[str]:
    return [
        str(b.get("text", "")) for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]


def to_provider(provider: str, messages: List[dict]) -> List[dict]:
    """Rewrite canonical image blocks into *provider*'s request shape.

    Returns *messages* itself when nothing carries an image (histories are
    large; the common case must not copy). Otherwise returns a new list;
    the caller's history is never mutated.
    """
    if not has_images(messages):
        return messages
    provider = (provider or "").lower()
    converted: List[dict] = []
    for message in messages:
        if not _message_has_images(message):
            converted.append(message)
            continue
        content = message["content"]
        if provider == "anthropic":
            blocks = [
                {k: v for k, v in b.items() if k not in CONCH_ONLY_KEYS}
                if is_image_block(b) else b
                for b in content
            ]
            converted.append({**message, "content": blocks})
        elif provider in OPENAI_COMPATIBLE:
            blocks = []
            for block in content:
                if is_image_block(block):
                    source = block.get("source") or {}
                    url = (f"data:{source.get('media_type', 'image/png')};base64,"
                           f"{source.get('data', '')}")
                    blocks.append({"type": "image_url", "image_url": {"url": url}})
                else:
                    blocks.append(block)
            converted.append({**message, "content": blocks})
        elif provider == "ollama":
            converted.append({
                **message,
                "content": "\n".join(_texts(content)),
                "images": [
                    (b.get("source") or {}).get("data", "")
                    for b in content if is_image_block(b)
                ],
            })
        else:
            converted.append(_replace_images(message, ""))
    return converted


# ---------------------------------------------------------------------------
# Shell glue (kept here so app.py stays a thin dispatcher)
# ---------------------------------------------------------------------------


def handle_attach_command(result: tuple, pending: List[dict],
                          out: Callable[[str], None] = print) -> None:
    """Run a ``/image`` result from ``handle_slash_command``:
    ``("attach_image", path_or_None)`` or ``("attach_image_clear",)``."""
    if result[0] == "attach_image_clear":
        count = len(pending)
        pending.clear()
        out(f"  \033[2m({count} pending image(s) dropped)\033[0m\n")
        return
    target = result[1] if len(result) > 1 else None
    temp = None
    if target is None:
        temp, reason = clipboard_image()
        if temp is None:
            out(f"  \033[31m✗ No image attached: {reason}\033[0m\n")
            return
        target = temp
    try:
        block = load_image(target)
    except ImageError as exc:
        out(f"  \033[31m✗ No image attached: {exc}\033[0m\n")
        return
    finally:
        if temp is not None:
            _discard(temp)
    pending.append(block)
    out(attached_line(block) + "\n")


def _vision_supported(provider: str, model: str, config: Optional[dict]) -> Optional[bool]:
    if (provider or "").lower() != "ollama":
        return True
    from .providers import ollama_model_supports_vision

    try:
        return ollama_model_supports_vision(model, config)
    except Exception:
        return None


def turn_images(user_input: str, pending: List[dict], *, provider: str,
                model: str, config: Optional[dict], cwd=None,
                out: Callable[[str], None] = print) -> List[dict]:
    """Image blocks for this turn: the pending ``/image`` attachments plus
    image paths found in *user_input* (one 📎 line each). Clears *pending*.
    Returns [] (with a warning) when the Ollama model lacks vision."""
    blocks = list(pending)
    pending.clear()
    for path in find_image_paths(user_input, cwd or os.getcwd()):
        try:
            block = load_image(path)
        except ImageError as exc:
            out(f"  \033[33m⚠ {exc} — not attached\033[0m")
            continue
        blocks.append(block)
        out(f"  📎 {block['name']} ({format_size(image_bytes(block))})")
    if blocks and _vision_supported(provider, model, config) is False:
        names = ", ".join(b.get("name") or "image" for b in blocks)
        out(f"  \033[33m⚠ {model} has no vision capability — {names} not "
            "attached; sending the text only\033[0m")
        return []
    return blocks
