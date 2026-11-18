"""Builtin file tools: ``read_file``, ``edit_file``, ``write_file``.

Why dedicated tools instead of ``cat`` / heredocs / ``sed -i`` through
local_shell: the operator approves a *diff* of exactly what will change
(not an opaque shell one-liner), edits are exact-substring replacements
that fail loudly instead of silently mangling a file, writes are atomic
and keep the file's mode and line endings, and reads come back numbered,
bounded, and credential-redacted.

All three tools are served by one :class:`FileToolsClient` (group
``files``). Reads never prompt except for credential-bearing paths;
edits and writes follow the session permission mode (see
:meth:`FileToolsClient._approve_change`).
"""

from __future__ import annotations

import dataclasses
import difflib
import os
import stat as _stat
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from .tooling import LocalShellPolicy, PermissionState, default_permissions

READ_DEFAULT_LIMIT = 2000
READ_MAX_LINE_CHARS = 2000
DIFF_MAX_LINES = 80
DIFF_MAX_LINE_CHARS = 400
REDACTION_MARKER = "[credential redacted]"
_BINARY_SNIFF_BYTES = 8192

_HOST_PATHS = (
    "Runs on the Conch host as the current OS user, not inside the model CLI's "
    "inference sandbox. Absolute and ~ paths are supported outside the working "
    "directory; MCP server directory allowlists do not scope this builtin. "
    "OS permissions and Conch's approval and sensitive-path guards still apply. "
)


READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a UTF-8 text file with line numbers (cat -n style). Use "
            "this instead of cat/head/tail/sed in local_shell to look at file "
            "contents. Returns up to `limit` lines (default 2000) starting at "
            "1-based line `offset`; continue with a larger offset for long "
            "files. Binary files and directories are refused. Relative paths "
            "resolve against the session working directory. " + _HOST_PATHS
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "offset": {
                    "type": "integer",
                    "description": "1-based line to start from (default 1)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum lines to return (default 2000)",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}

EDIT_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "Change an existing UTF-8 text file by replacing an exact "
            "substring. old_string must match the file exactly (including "
            "indentation) and be unique unless replace_all is true; include "
            "surrounding lines to make it unique. Read the file with "
            "read_file first. Use this instead of sed -i, perl -pi, or "
            "rewriting the file through the shell. " + _HOST_PATHS
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence (default false)",
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    },
}

WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Create a UTF-8 text file (parent directories too) or replace an "
            "existing file's entire content. Use this instead of shell "
            "heredocs or echo redirection; prefer edit_file for changing part "
            "of an existing file. " + _HOST_PATHS
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {
                    "type": "string",
                    "description": "Complete new file content",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
}

FILE_TOOL_DEFS = (READ_FILE_TOOL, EDIT_FILE_TOOL, WRITE_FILE_TOOL)
FILE_TOOL_NAMES = tuple(tool["function"]["name"] for tool in FILE_TOOL_DEFS)


# ---------------------------------------------------------------------------
# Guarded paths
# ---------------------------------------------------------------------------

#: Credential-bearing file names (compared case-insensitively). One set
#: governs both directions: reading one needs confirmation before its
#: contents reach the model, changing one needs confirmation even in
#: agent/yolo mode, and both are refused when nobody can be asked.
SENSITIVE_FILE_NAMES = frozenset({
    ".netrc", "_netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc",
    ".htpasswd", ".vault-token", ".boto", ".s3cfg", ".conchrc",
    "credentials", "credentials.json", "credentials.toml",
    "secrets.json", "secrets.yaml", "secrets.yml",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk",
    "id_ed25519_sk", "authorized_keys", "kubeconfig",
})
#: Key, certificate, keystore, and keychain files by extension.
SENSITIVE_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".keychain",
    ".keychain-db", ".kdbx", ".ppk",
)
#: Credential directories, relative to the home directory.
SENSITIVE_HOME_DIRS = (
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker",
    ".config/gcloud", ".config/gh", ".password-store", "Library/Keychains",
)
#: System locations a model has no business rewriting without the
#: operator looking (``/Library`` here never covers ``~/Library``).
SYSTEM_PATH_PREFIXES = (
    "/etc", "/private/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64",
    "/opt", "/boot", "/dev", "/proc", "/sys", "/System", "/Library",
    "/Applications", "/var/db", "/private/var/db",
)


def _within(path: str, root: str) -> bool:
    root = root.rstrip(os.sep) or os.sep
    return path == root or path.startswith(root + os.sep)


def _path_forms(path: str) -> List[str]:
    """The literal absolute path and its symlink-resolved target: a guard
    must hold for both, or a link named ``notes.txt`` pointing at
    ``~/.ssh/id_rsa`` would walk straight past it."""
    forms = [os.path.abspath(path)]
    try:
        real = os.path.realpath(path)
    except OSError:
        real = forms[0]
    if real not in forms:
        forms.append(real)
    return forms


def _conch_secret_files() -> List[str]:
    """Conch's own key file, its manifest, its config (tokens and the
    permission settings live there), and the MCP server list."""
    from .config import get_config_path, get_env_file_path, get_env_manifest_path

    config_path = get_config_path()
    files = [
        get_env_file_path(), get_env_manifest_path(), config_path,
        str(Path(config_path).parent / "mcp.json"),
    ]
    resolved = []
    for item in files:
        resolved.extend(_path_forms(item))
    return resolved


def sensitive_path_reason(path: str) -> Optional[str]:
    """Short label for why *path* holds credentials, or None."""
    home_forms = _path_forms(str(Path.home()))
    conch_files = _conch_secret_files()
    for form in _path_forms(path):
        name = os.path.basename(form).lower()
        if form in conch_files:
            return "conch credential/config file"
        if name.startswith(".env"):
            return "environment (.env) file"
        if name in SENSITIVE_FILE_NAMES:
            return "credential file"
        if name.endswith(SENSITIVE_SUFFIXES):
            return "key/certificate file"
        for home in home_forms:
            for rel in SENSITIVE_HOME_DIRS:
                if _within(form, os.path.join(home, rel)):
                    return f"credential directory ~/{rel}"
    return None


def system_path_reason(path: str) -> Optional[str]:
    """Short label when *path* is inside a system location, or None."""
    home_forms = _path_forms(str(Path.home()))
    for form in _path_forms(path):
        if any(_within(form, home) for home in home_forms):
            continue
        for prefix in SYSTEM_PATH_PREFIXES:
            if _within(form, prefix):
                return f"system location {prefix}"
    return None


def _visible(text: str, limit: int = 0) -> str:
    """*text* safe to print on the operator's terminal: control characters
    (escape sequences, carriage returns) become visible escapes so a file
    or a model-supplied string cannot hide or rewrite what the approval
    prompt shows."""
    clean = "".join(
        ch if ch == "\t" or (ch.isprintable() and ch != "\x7f")
        else (f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}")
        for ch in text
    )
    if limit and len(clean) > limit:
        clean = clean[:limit] + "…"
    return clean


def _atomic_write(path: str, data: bytes, mode: Optional[int]) -> None:
    """Write *data* to *path* via a temp file in the same directory and
    ``os.replace`` — readers see the old or the new file, never a torn
    one. *mode* (the replaced file's permission bits) is restored; a new
    file gets the usual umask-filtered 0666."""
    directory = os.path.dirname(path) or "."
    base = os.path.basename(path)
    while True:
        tmp = os.path.join(directory, f".{base}.{os.urandom(4).hex()}.conch-tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            break
        except FileExistsError:
            continue
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _decode_text(data: bytes) -> Optional[str]:
    """UTF-8 text, or None for binary / other encodings (never guessed:
    rewriting a Latin-1 file as UTF-8 would corrupt it)."""
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _uses_crlf(text: str) -> bool:
    """True when every line ending is CRLF (mixed files are edited raw)."""
    return "\r\n" in text and "\n" not in text.replace("\r\n", "")


def _diff(old: str, new: str, fromfile: str, tofile: str) -> Tuple[List[str], int, int]:
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile=fromfile, tofile=tofile,
        n=3, lineterm="",
    ))
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return lines, added, removed


def _diff_names(shown: str) -> Tuple[str, str]:
    """Unified-diff file labels: git's a/ b/ prefixes for relative paths,
    the path itself when absolute (never ``a//abs``)."""
    if os.path.isabs(shown):
        return shown, shown
    return f"a/{shown}", f"b/{shown}"


class FileToolsClient:
    """Serves read_file / edit_file / write_file for one session."""

    name = "files"

    # Same default as LocalShellClient; bootstrap and runtime rescale it to
    # the model's window and the per-round share.
    DEFAULT_RESULT_BUDGET = 15000

    def __init__(self, permissions: Optional[PermissionState] = None):
        self.policy = LocalShellPolicy()
        self._permissions = permissions
        self._cwd: Optional[str] = None
        self._result_budget = self.DEFAULT_RESULT_BUDGET
        self._before_run = None
        # 'a' at the apply prompt: auto-apply ordinary file changes for
        # the rest of the session (guarded paths still ask).
        self._auto_apply = False

    # -- wiring (mirrors LocalShellClient) ----------------------------------

    def set_before_run(self, hook) -> None:
        """Register ``hook(description)`` to run right before an approved
        change is written (app.py takes the /undo checkpoint there).
        Hook failures never block the write."""
        self._before_run = hook

    def _fire_before_run(self, what: str) -> None:
        if self._before_run is None:
            return
        try:
            self._before_run(what)
        except Exception:
            pass

    def bind_permissions(self, permissions: Optional[PermissionState]):
        self._permissions = permissions

    def permissions(self) -> PermissionState:
        return self._permissions or default_permissions()

    def set_cwd(self, cwd):
        """Base for relative paths — the same directory local_shell runs
        commands in (None = process cwd)."""
        self._cwd = str(cwd) if cwd else None

    def set_policy(self, policy: LocalShellPolicy):
        self.policy = policy

    def set_result_budget(self, budget_chars: int):
        if budget_chars > 0:
            self._result_budget = budget_chars

    def remote_variant(self) -> "FileToolsClient":
        """A per-turn copy for remote/channel sessions: never interactive
        and pinned to prompt_all, so guarded reads refuse instead of
        prompting the local terminal on a channel's behalf."""
        clone = FileToolsClient(permissions=PermissionState())
        clone.set_policy(LocalShellPolicy(interactive=False))
        clone._cwd = self._cwd
        clone._result_budget = self._result_budget
        return clone

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _text(msg: str, error: bool = False) -> dict:
        result = {"content": [{"type": "text", "text": msg}]}
        if error:
            result["isError"] = True
        return result

    def _resolve(self, path: str) -> str:
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            expanded = os.path.join(self._cwd or os.getcwd(), expanded)
        return os.path.normpath(expanded)

    def _shown(self, abs_path: str) -> str:
        """Path as reported: relative when under the working directory
        (compared literally, then symlink-resolved: macOS /var is
        /private/var)."""
        base = self._cwd or os.getcwd()
        for path, root in ((abs_path, base),
                           (os.path.realpath(abs_path), os.path.realpath(base))):
            try:
                rel = os.path.relpath(path, root)
            except ValueError:
                continue
            if rel != os.pardir and not rel.startswith(os.pardir + os.sep):
                return rel
        return abs_path

    def call_tool(self, name: str, arguments: dict) -> dict:
        arguments = arguments or {}
        raw = str(arguments.get("path") or "").strip()
        if not raw:
            return self._text("Error: path is required.", error=True)
        try:
            if name == "read_file":
                return self._read(raw, arguments)
            if name == "edit_file":
                return self._edit(raw, arguments)
            if name == "write_file":
                return self._write(raw, arguments)
        except UnicodeError:
            return self._text(
                "Error: the text contains characters that cannot be encoded "
                "as UTF-8 (lone surrogates); nothing was written.", error=True,
            )
        except OSError as exc:
            shown = self._shown(self._resolve(raw))
            return self._text(
                f"Error: {shown}: {exc.strerror or exc}", error=True
            )
        return self._text(f"Error: unknown file tool {name!r}.", error=True)

    # -- read_file ----------------------------------------------------------

    def _read(self, raw: str, arguments: dict) -> dict:
        path = self._resolve(raw)
        shown = self._shown(path)
        if not os.path.exists(path):
            return self._text(f"Error: {shown} does not exist.", error=True)
        if os.path.isdir(path):
            return self._text(
                f"Error: {shown} is a directory, not a file; list it with "
                "local_shell (ls) instead.", error=True,
            )
        if not os.path.isfile(path):
            return self._text(
                f"Error: {shown} is not a regular file.", error=True
            )
        try:
            offset = max(1, int(arguments.get("offset") or 1))
            limit = int(arguments.get("limit") or READ_DEFAULT_LIMIT)
        except (TypeError, ValueError):
            return self._text("Error: offset and limit must be integers.", error=True)
        if limit < 1:
            limit = READ_DEFAULT_LIMIT

        reason = sensitive_path_reason(path)
        if reason:
            refusal = self._confirm_sensitive_read(shown, reason)
            if refusal:
                return self._text(refusal)

        not_text = (
            f"Error: {shown} is not UTF-8 text (binary or another encoding); "
            "refusing to read it."
        )
        numbered: List[str] = []
        more = False
        line_no = 0
        last = offset + limit - 1
        with open(path, "rb") as handle:
            if b"\x00" in handle.read(_BINARY_SNIFF_BYTES):
                return self._text(not_text, error=True)
            handle.seek(0)
            for raw_line in handle:
                line_no += 1
                if line_no < offset:
                    continue
                if line_no > last:
                    more = True
                    break
                if b"\x00" in raw_line:
                    return self._text(not_text, error=True)
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    return self._text(not_text, error=True)
                line = line.rstrip("\n")
                if line.endswith("\r"):
                    line = line[:-1]
                if line_no == 1 and line.startswith("\ufeff"):
                    line = line[1:]
                if len(line) > READ_MAX_LINE_CHARS:
                    cut = len(line) - READ_MAX_LINE_CHARS
                    line = (
                        line[:READ_MAX_LINE_CHARS]
                        + f"… [line truncated: {cut:,} more chars]"
                    )
                # Unpadded: the tool loop strips result text, which would
                # leave only the first row's padding missing.
                numbered.append(f"{line_no}\t{line}")
        if not numbered:
            if line_no == 0:
                return self._text(f"{shown} is empty.")
            return self._text(
                f"{shown} has {line_no} lines; offset {offset} is past the end."
            )
        body = "\n".join(numbered)
        if more:
            body += (
                f"\n… more lines follow; continue with offset={last + 1}"
            )
        body, labels = _redact(body)
        if labels:
            body += (
                f"\n[conch redacted credential-shaped values: {', '.join(labels)}]"
            )
        return self._text(self._cap(body))

    def _cap(self, text: str) -> str:
        if len(text) > self._result_budget:
            from .runtime import truncate_middle

            text = truncate_middle(text, self._result_budget)
        return text

    def _confirm_sensitive_read(self, shown: str, reason: str) -> Optional[str]:
        """None when the operator allows reading a credential-bearing file;
        otherwise the refusal/decline text. Asks in every mode."""
        if not self.policy.interactive:
            return (
                f"Refused: {shown} is a {reason}; reading it requires "
                "interactive confirmation."
            )
        from .render import clear_active_spinners

        clear_active_spinners()
        print(
            f"\n  \033[1;31m\u26a0 Read sensitive file\033[0m "
            f"\033[1m{_visible(shown)}\033[0m \033[2m({reason}; confirmation "
            "required even in agent mode)\033[0m",
            flush=True,
        )
        _input = self.policy.input_fn or input
        try:
            sys.stdout.flush()
            answer = _input("  \033[1;33mRead? [y/n]\033[0m ").strip().lower()
        except (EOFError, KeyboardInterrupt) as exc:
            return self._no_answer_decline(exc, shown, "read")
        if answer in ("y", "yes"):
            return None
        return f"User declined to let you read {shown}."

    # -- edit_file / write_file ---------------------------------------------

    def _edit(self, raw: str, arguments: dict) -> dict:
        old_string = arguments.get("old_string")
        new_string = arguments.get("new_string")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return self._text(
                "Error: old_string and new_string must be strings.", error=True
            )
        replace_all = bool(arguments.get("replace_all") or False)
        path = self._resolve(raw)
        shown = self._shown(path)
        if not old_string:
            return self._text(
                "Error: old_string is empty; use write_file to create or "
                "replace a whole file.", error=True,
            )
        if old_string == new_string:
            return self._text(
                "Error: old_string and new_string are identical; nothing to change.",
                error=True,
            )
        if not os.path.exists(path):
            return self._text(
                f"Error: {shown} does not exist; use write_file to create it.",
                error=True,
            )
        if not os.path.isfile(path):
            return self._text(f"Error: {shown} is not a regular file.", error=True)
        target = os.path.realpath(path)
        with open(target, "rb") as handle:
            original = handle.read()
        text = _decode_text(original)
        if text is None:
            return self._text(
                f"Error: {shown} is not UTF-8 text; edit_file only changes "
                "UTF-8 files.", error=True,
            )
        crlf = _uses_crlf(text)
        working = text.replace("\r\n", "\n") if crlf else text
        old_n = old_string.replace("\r\n", "\n") if crlf else old_string
        new_n = new_string.replace("\r\n", "\n") if crlf else new_string
        count = working.count(old_n)
        if count == 0:
            hint = (
                " old_string contains a redaction marker: the file holds a "
                "credential there that you were never shown; edit around it."
                if REDACTION_MARKER in old_string else ""
            )
            return self._text(
                f"Error: old_string was not found in {shown}. Read the file "
                "with read_file and copy the exact text (including "
                f"whitespace and indentation).{hint}", error=True,
            )
        if count > 1 and not replace_all:
            return self._text(
                f"Error: old_string matches {count} places in {shown}. Include "
                "more surrounding context to make it unique, or set "
                "replace_all to true to change every occurrence.", error=True,
            )
        updated = (
            working.replace(old_n, new_n) if replace_all
            else working.replace(old_n, new_n, 1)
        )
        lines, added, removed = _diff(working, updated, *_diff_names(shown))
        refusal = self._approve_change("Edit", shown, path, lines)
        if refusal:
            return self._text(refusal)
        with open(target, "rb") as handle:
            if handle.read() != original:
                return self._text(
                    f"Error: {shown} changed while the edit awaited approval; "
                    "nothing was written. Read it again and retry.", error=True,
                )
        if crlf:
            updated = updated.replace("\n", "\r\n")
        mode = _stat.S_IMODE(os.stat(target).st_mode)
        self._fire_before_run(f"edit {shown}")
        _atomic_write(target, updated.encode("utf-8"), mode)
        summary = f"Edited {shown} (+{added} \u2212{removed} lines)"
        if replace_all and count > 1:
            summary += f", {count} replacements"
        return self._text(summary)

    def _write(self, raw: str, arguments: dict) -> dict:
        content = arguments.get("content")
        if not isinstance(content, str):
            return self._text("Error: content must be a string.", error=True)
        path = self._resolve(raw)
        shown = self._shown(path)
        if os.path.isdir(path):
            return self._text(f"Error: {shown} is a directory.", error=True)
        exists = os.path.lexists(path)
        if exists and not os.path.isfile(path):
            return self._text(f"Error: {shown} is not a regular file.", error=True)
        target = os.path.realpath(path) if exists else path
        original = None
        mode = None
        if exists:
            with open(target, "rb") as handle:
                original = handle.read()
            mode = _stat.S_IMODE(os.stat(target).st_mode)
            if original == content.encode("utf-8"):
                return self._text(f"No changes: {shown} already has this content.")
            old_text = _decode_text(original)
            if old_text is None:
                body, added, removed = _diff("", content, "/dev/null", _diff_names(shown)[1])
                lines = [f"(replacing {len(original):,} bytes of non-UTF-8 content)"] + body
            else:
                lines, added, removed = _diff(old_text, content, *_diff_names(shown))
            header = "Overwrite"
        else:
            lines, added, removed = _diff("", content, "/dev/null", _diff_names(shown)[1])
            header = "Create"
        refusal = self._approve_change(header, shown, path, lines)
        if refusal:
            return self._text(refusal)
        if exists:
            with open(target, "rb") as handle:
                if handle.read() != original:
                    return self._text(
                        f"Error: {shown} changed while the write awaited "
                        "approval; nothing was written.", error=True,
                    )
        elif os.path.lexists(path):
            return self._text(
                f"Error: {shown} appeared while the write awaited approval; "
                "nothing was written.", error=True,
            )
        self._fire_before_run(f"write {shown}")
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _atomic_write(target, content.encode("utf-8"), mode)
        if exists:
            return self._text(f"Overwrote {shown} (+{added} \u2212{removed} lines)")
        return self._text(f"Created {shown} ({len(content.splitlines())} lines)")

    def _print_diff(self, lines: List[str]) -> None:
        shown = lines[:DIFF_MAX_LINES]
        for line in shown:
            text = _visible(line, DIFF_MAX_LINE_CHARS)
            if line.startswith(("+++", "---")):
                color = "\033[1m"
            elif line.startswith("@@"):
                color = "\033[36m"
            elif line.startswith("+"):
                color = "\033[32m"
            elif line.startswith("-"):
                color = "\033[31m"
            else:
                color = "\033[2m"
            print(f"    {color}{text}\033[0m")
        if len(lines) > DIFF_MAX_LINES:
            print(f"    \033[2m\u2026 {len(lines) - DIFF_MAX_LINES} more lines\033[0m")

    def _approve_change(self, verb: str, shown: str, path: str,
                        diff_lines: List[str]) -> Optional[str]:
        """Show the change and decide whether it may be written.

        Returns None to apply, or the decline/refusal text for the model.
        prompt_all and safe_auto ask (a write is a mutation, never "safe");
        agent/yolo mode or the session 'a' choice apply without asking but
        still show the diff. Credential files and system locations ask in
        every mode and are refused when nobody can be asked."""
        from .render import clear_active_spinners

        clear_active_spinners()
        print(
            f"\n  \033[1;33m\u270e {verb}\033[0m \033[1m{_visible(shown)}\033[0m",
            flush=True,
        )
        self._print_diff(diff_lines)

        guard = sensitive_path_reason(path) or system_path_reason(path)
        mode = self.permissions().get_permission_mode()
        auto_execute = self.policy.allow_auto_execute or mode == "yolo"
        if guard is None and (auto_execute or self._auto_apply):
            note = (
                "agent mode \u2014 auto-applying" if auto_execute
                else "file edits auto-approved this session"
            )
            print(f"  \033[2m({note})\033[0m")
            return None
        if not self.policy.interactive:
            if guard:
                return (
                    f"Refused: {shown} is a {guard}; changing it requires "
                    "interactive confirmation. It was not changed."
                )
            return (
                "Background tasks cannot prompt for file-change confirmation; "
                f"{shown} was not changed."
            )
        if guard:
            print(
                f"  \033[1;31m\u26a0 {guard[0].upper() + guard[1:]} \u2014 "
                "confirmation required (even in agent mode)\033[0m"
            )

        _input = self.policy.input_fn or input
        while True:
            try:
                sys.stdout.flush()
                answer = _input("  \033[1;33mApply? [y/n/a/A/?]\033[0m ").strip()
            except (EOFError, KeyboardInterrupt) as exc:
                return self._no_answer_decline(exc, shown, "change")
            if answer == "?":
                print(
                    "    \033[1my\033[0m / \033[1mEnter\033[0m  Apply this change\n"
                    "    \033[1mn\033[0m          Decline (with optional feedback to the LLM)\n"
                    "    \033[1ma\033[0m          Apply, and auto-apply file edits for the rest of the session\n"
                    "    \033[1mA\033[0m          Turn on agent mode (auto-execute everything), then apply"
                )
                continue
            break

        if answer.lower() in ("", "y", "yes"):
            return None
        if answer == "A":
            self.permissions().set_agent_mode(True)
            self.policy = dataclasses.replace(self.policy, allow_auto_execute=True)
            print("  \033[1;32mAgent mode: ON\033[0m \u2014 commands and file edits will auto-execute")
            return None
        if answer.lower() in ("a", "always"):
            if guard:
                print("  \033[2m(applied once \u2014 guarded paths always ask)\033[0m")
            else:
                self._auto_apply = True
                print("  \033[2m(will auto-apply file edits this session)\033[0m")
            return None
        try:
            feedback = _input("  \033[2mReason (optional):\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            feedback = ""
        msg = f"User declined to apply the change to {shown}."
        if feedback:
            msg += f" Feedback: {feedback}"
        return msg

    @staticmethod
    def _no_answer_decline(exc: BaseException, shown: str, what: str) -> str:
        """Fail closed when the prompt gets no answer (EOF or Ctrl-C), like
        LocalShellClient._no_answer_decline: no answer counts as no."""
        how = "interrupted" if isinstance(exc, KeyboardInterrupt) else "closed (EOF)"
        done = "File not read" if what == "read" else "Change not applied"
        print(
            f"\n  \033[1;31m\u2717 {done}\033[0m \033[2m\u2014 "
            f"approval prompt {how}; no answer counts as no.\033[0m",
            flush=True,
        )
        if what == "read":
            return (
                f"User did not approve reading {shown} (the approval prompt "
                f"was {how}); it was not read."
            )
        return (
            f"User did not approve the change to {shown} (the approval "
            f"prompt was {how}); nothing was written."
        )


def _redact(text: str) -> Tuple[str, List[str]]:
    from .secretguard import redact_credentials

    return redact_credentials(text, REDACTION_MARKER)
