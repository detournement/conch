"""Builtin file tools: ``read_file``, ``edit_file``, ``write_file``,
``grep`` and ``glob``.

Why dedicated tools instead of ``cat`` / heredocs / ``sed -i`` through
local_shell: the operator approves a *diff* of exactly what will change
(not an opaque shell one-liner), edits are exact-substring replacements
that fail loudly instead of silently mangling a file, writes are atomic
and keep the file's mode and line endings, and reads come back numbered,
bounded, and credential-redacted.

All five tools are served by one :class:`FileToolsClient` (group
``files``). Reads never prompt except for credential-bearing paths;
``grep``/``glob`` never prompt (matches inside credential-bearing files
are withheld); edits and writes follow the session permission mode (see
:meth:`FileToolsClient._approve_change`).

Read-before-edit: the client remembers what it read or wrote (path ->
mtime, size, content hash). ``edit_file``, and ``write_file`` replacing an
existing file, refuse a file this session never read, or one that changed
on disk since it was last read or written here (opt out with
``require_read_before_edit=false``).

``post_edit_check`` (see :func:`run_post_edit_check`) is run by the agent
loop after each successful edit or write; its output is appended to the
tool result the model sees.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import os
import re
import shlex
import shutil
import signal
import stat as _stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .tooling import LocalShellPolicy, PermissionState, default_permissions

READ_DEFAULT_LIMIT = 2000
READ_MAX_LINE_CHARS = 2000
DIFF_MAX_LINES = 80
DIFF_MAX_LINE_CHARS = 400
REDACTION_MARKER = "[credential redacted]"
_BINARY_SNIFF_BYTES = 8192
#: Larger files are tracked for read-before-edit by mtime and size.
_HASH_MAX_BYTES = 64 << 20

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
            "surrounding lines to make it unique. The file must have been "
            "read with read_file in this session and not changed on disk "
            "since (otherwise the edit is refused: read it again). Use this "
            "instead of sed -i, perl -pi, or "
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
            "of an existing file. Replacing an existing file requires having "
            "read it with read_file in this session. " + _HOST_PATHS
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

GREP_TOOL = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": (
            "Search file contents with a regular expression (ripgrep when "
            "installed). Read-only and never needs approval: use this instead "
            "of grep/rg/ag through local_shell. Searches `path` (default: the "
            "working directory) recursively, skipping .gitignored, hidden VCS "
            "and binary files. output_mode files_with_matches (default) lists "
            "matching files; content shows matching lines as path:line:text "
            "(with `context` lines around them); count shows matches per "
            "file. `glob` filters files (e.g. '*.py', '*.{ts,tsx}', "
            "'src/**/*.go'; a pattern without '/' matches file names at any "
            "depth). Results stop at head_limit entries and say so. " + _HOST_PATHS
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression to search for",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search (default: working directory)",
                },
                "glob": {
                    "type": "string",
                    "description": "Only search files matching this glob, e.g. '*.py'",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Ignore case (default false)",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                    "description": "files_with_matches (default), content, or count",
                },
                "context": {
                    "type": "integer",
                    "description": "Lines of context before and after each match (content mode)",
                },
                "head_limit": {
                    "type": "integer",
                    "description": "Maximum entries to return (default 200)",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
}

GLOB_TOOL = {
    "type": "function",
    "function": {
        "name": "glob",
        "description": (
            "Find files by name pattern, newest first (by modification "
            "time). Read-only and never needs approval: use this instead of "
            "find/fd/ls -R through local_shell. The pattern matches paths "
            "relative to `path` (default: the working directory): '**/*.py' "
            "finds Python files at any depth, '*.md' only at the top, "
            "'src/**/test_*.{ts,tsx}' supports braces. Skips .gitignored "
            "files inside a git repository. " + _HOST_PATHS
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. '**/*.py'",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search (default: working directory)",
                },
                "head_limit": {
                    "type": "integer",
                    "description": "Maximum paths to return (default 200)",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
}

FILE_TOOL_DEFS = (READ_FILE_TOOL, EDIT_FILE_TOOL, WRITE_FILE_TOOL, GREP_TOOL, GLOB_TOOL)
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


def _sensitive_context() -> Tuple[List[str], List[str]]:
    """(home forms, conch secret files): what :func:`sensitive_path_reason`
    compares against, computed once per search by grep."""
    return _path_forms(str(Path.home())), _conch_secret_files()


def sensitive_path_reason(path: str, _context=None) -> Optional[str]:
    """Short label for why *path* holds credentials, or None."""
    home_forms, conch_files = _context or _sensitive_context()
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


#: Path components that make a change ask in ``accept_edits`` mode even
#: inside the repository: git's own metadata (hooks run code, config can
#: point anywhere).
ACCEPT_EDITS_GUARDED_PARTS = frozenset({".git"})


def edit_root(cwd: str) -> str:
    """The directory ``accept_edits`` auto-applies inside: the git root
    enclosing *cwd*, else *cwd* itself (symlink-resolved)."""
    from .repomap import find_git_root

    try:
        root = find_git_root(cwd)
    except OSError:
        root = None
    return os.path.realpath(str(root) if root is not None else cwd)


def accept_edits_block_reason(path: str, cwd: str) -> Optional[str]:
    """None when ``accept_edits`` may apply a change to *path* without
    asking; otherwise a short reason it must ask.

    The literal path and its symlink-resolved target must both lie inside
    :func:`edit_root`, so a link inside the repository that points outside
    it counts as outside. Credential and system paths are checked
    separately (they ask in every mode)."""
    root = edit_root(cwd)
    roots = _path_forms(root)
    literal = os.path.abspath(path)
    try:
        real = os.path.realpath(path)
    except OSError:
        return f"outside the repository ({root})"
    if not any(_within(literal, item) for item in roots) or not _within(real, root):
        return f"outside the repository ({root})"
    for form in (literal, real):
        for item in roots:
            if not _within(form, item):
                continue
            parts = os.path.relpath(form, item).split(os.sep)
            hit = ACCEPT_EDITS_GUARDED_PARTS.intersection(parts)
            if hit:
                return f"a guarded path ({sorted(hit)[0]}/)"
    return None


class FileToolsClient:
    """Serves read_file / edit_file / write_file / grep / glob for one
    session."""

    name = "files"

    # Same default as LocalShellClient; bootstrap and runtime rescale it to
    # the model's window and the per-round share.
    DEFAULT_RESULT_BUDGET = 15000

    def __init__(self, permissions: Optional[PermissionState] = None,
                 config: Optional[dict] = None):
        self.policy = LocalShellPolicy()
        self._permissions = permissions
        self._config = config
        self._cwd: Optional[str] = None
        self._result_budget = self.DEFAULT_RESULT_BUDGET
        self._before_run = None
        # 'a' at the apply prompt: auto-apply ordinary file changes for
        # the rest of the session (guarded paths still ask).
        self._auto_apply = False
        # Read-before-edit record: realpath -> (mtime_ns, size, sha256)
        # of the content this session last read or wrote.
        self._seen: Dict[str, Tuple[int, int, str]] = {}
        self._seen_lock = threading.Lock()

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

    def configure(self, config: Optional[dict]) -> None:
        """Bind the session config (read at call time, so a changed
        require_read_before_edit applies at once)."""
        self._config = config

    @property
    def cwd(self) -> str:
        """The directory relative paths resolve against."""
        return self._cwd or os.getcwd()

    def remote_variant(self) -> "FileToolsClient":
        """A per-turn copy for remote/channel sessions: never interactive
        and pinned to prompt_all, so guarded reads refuse instead of
        prompting the local terminal on a channel's behalf."""
        clone = FileToolsClient(permissions=PermissionState(), config=self._config)
        clone.set_policy(LocalShellPolicy(interactive=False))
        clone._cwd = self._cwd
        clone._result_budget = self._result_budget
        return clone

    def fork(self) -> "FileToolsClient":
        """A copy for a delegated subagent: same policy, permissions, cwd,
        config and /undo hook, but its own read-before-edit record: the
        subagent must read what it edits, and the parent then sees the
        subagent's changes as changed-since-read."""
        clone = FileToolsClient(permissions=self._permissions, config=self._config)
        clone.policy = self.policy
        clone._cwd = self._cwd
        clone._result_budget = self._result_budget
        clone._before_run = self._before_run
        clone._auto_apply = self._auto_apply
        return clone

    # -- read-before-edit ----------------------------------------------------

    def _require_read(self) -> bool:
        value = (self._config or {}).get("require_read_before_edit", "true")
        return str(value).strip().lower() not in ("false", "0", "no", "off")

    def _remember(self, target: str, data: Optional[bytes] = None) -> None:
        """Record *target* as seen with content *data* (read from disk
        when None). Files over _HASH_MAX_BYTES read in part are recorded
        by mtime and size only (no full-file hash for a peek at a huge
        log)."""
        try:
            if data is None and os.path.getsize(target) > _HASH_MAX_BYTES:
                hexdigest = ""
            elif data is None:
                digest = hashlib.sha256()
                with open(target, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
                hexdigest = digest.hexdigest()
            else:
                hexdigest = hashlib.sha256(data).hexdigest()
            info = os.stat(target)
        except OSError:
            return
        with self._seen_lock:
            self._seen[target] = (info.st_mtime_ns, info.st_size, hexdigest)

    def _unread_error(self, target: str, shown: str, data: bytes,
                      action: str) -> Optional[str]:
        """Refusal text when *target* (current content *data*) was never
        read this session or changed since it was last read or written
        here; None when the change may proceed. The content hash decides
        (a touch that keeps the bytes is not a change)."""
        if not self._require_read():
            return None
        with self._seen_lock:
            record = self._seen.get(target)
        if record is None:
            return (
                f"Error: {shown} has not been read in this session; "
                f"read_file it first, then retry the {action}. Nothing was "
                "written."
            )
        if record[2]:
            changed = hashlib.sha256(data).hexdigest() != record[2]
        else:
            info = os.stat(target)
            changed = (info.st_mtime_ns, info.st_size) != record[:2]
        if changed:
            return (
                f"Error: {shown} changed on disk since you last read it; "
                f"read it again with read_file, then retry the {action} "
                "against its current content. Nothing was written."
            )
        return None

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
        if name in ("grep", "glob"):
            try:
                if name == "grep":
                    return self._grep(raw, arguments)
                return self._glob(raw, arguments)
            except OSError as exc:
                return self._text(
                    f"Error: {name} failed: {exc.strerror or exc}", error=True
                )
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
        target = os.path.realpath(path)
        with open(path, "rb") as handle:
            if b"\x00" in handle.read(_BINARY_SNIFF_BYTES):
                # Seen (as binary): write_file may then replace it.
                self._remember(target)
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
                    self._remember(target)
                    return self._text(not_text, error=True)
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    self._remember(target)
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
        # Any successful read (partial ones included) counts for
        # read-before-edit.
        self._remember(target)
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
        stale = self._unread_error(target, shown, original, "edit")
        if stale:
            return self._text(stale, error=True)
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
        written = updated.encode("utf-8")
        _atomic_write(target, written, mode)
        self._remember(target, written)
        summary = f"Edited {shown} (+{added} \u2212{removed} lines)"
        if replace_all and count > 1:
            summary += f", {count} replacements"
        return self._changed(summary, target)

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
            stale = self._unread_error(target, shown, original, "write")
            if stale:
                return self._text(stale, error=True)
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
        written = content.encode("utf-8")
        _atomic_write(target, written, mode)
        target = os.path.realpath(target)
        self._remember(target, written)
        if exists:
            return self._changed(
                f"Overwrote {shown} (+{added} \u2212{removed} lines)", target
            )
        return self._changed(
            f"Created {shown} ({len(content.splitlines())} lines)", target
        )

    def _changed(self, summary: str, target: str) -> dict:
        """A successful edit/write result. ``changed_path`` tells the agent
        loop which file to run ``post_edit_check`` on; it never reaches
        the model."""
        result = self._text(summary)
        result["changed_path"] = target
        return result

    # -- grep / glob -----------------------------------------------------------

    def _search_root(self, raw: str) -> str:
        return self._resolve(raw) if raw else os.path.normpath(self.cwd)

    def _grep(self, raw: str, arguments: dict) -> dict:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return self._text("Error: pattern is required.", error=True)
        mode = str(arguments.get("output_mode") or "files_with_matches")
        if mode not in GREP_MODES:
            return self._text(
                "Error: output_mode must be files_with_matches, content or "
                "count.", error=True,
            )
        try:
            context = _clamp(arguments.get("context"), 0, 0, GREP_MAX_CONTEXT)
            limit = _clamp(arguments.get("head_limit"), SEARCH_DEFAULT_LIMIT, 1,
                           SEARCH_MAX_LIMIT)
        except (TypeError, ValueError):
            return self._text(
                "Error: context and head_limit must be integers.", error=True
            )
        file_glob = str(arguments.get("glob") or "").strip()
        ignore_case = bool(arguments.get("case_insensitive") or False)
        root = self._search_root(raw)
        shown_root = self._shown(root)
        if not os.path.exists(root):
            return self._text(f"Error: {shown_root} does not exist.", error=True)
        sink = _GrepSink(self, mode, limit)
        rg = shutil.which("rg")
        if rg:
            error = _rg_search(rg, root, self.cwd, pattern, mode, context,
                               file_glob, ignore_case, sink)
        else:
            error = _py_search(root, pattern, mode, context, file_glob,
                               ignore_case, sink)
        if error and not sink.lines:
            return self._text(f"Error: {error}", error=True)
        quoted = pattern if len(pattern) <= 80 else pattern[:80] + "…"
        where = shown_root + (f" (glob {file_glob})" if file_glob else "")
        if sink.lines:
            body = "\n".join(sink.finish())
        else:
            body = f"No matches for /{quoted}/ in {where}."
        notes = []
        if sink.truncated:
            unit = {"content": "lines", "count": "files",
                    "files_with_matches": "files"}[mode]
            notes.append(
                f"[showing the first {limit} {unit}; more matched: narrow the "
                "pattern, path or glob, or raise head_limit]"
            )
        if sink.withheld:
            names = sorted(self._shown(p) for p in sink.withheld)
            listed = ", ".join(names[:5]) + (" …" if len(names) > 5 else "")
            notes.append(
                f"[{len(names)} matching credential-bearing file(s) withheld: "
                f"{listed}; read_file asks the user before reading those]"
            )
        if sink.timed_out:
            notes.append(
                f"[search stopped after {SEARCH_TIMEOUT}s; results are partial]"
            )
        if sink.skipped_large:
            notes.append(
                f"[{sink.skipped_large} file(s) over "
                f"{GREP_MAX_FILE_BYTES // (1 << 20)} MB not searched]"
            )
        if error:
            notes.append(f"[some paths could not be searched: {error}]")
        if notes:
            body += "\n" + "\n".join(notes)
        body, labels = _redact(body)
        if labels:
            body += (
                f"\n[conch redacted credential-shaped values: {', '.join(labels)}]"
            )
        return self._text(self._cap(body))

    def _glob(self, raw: str, arguments: dict) -> dict:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return self._text("Error: pattern is required.", error=True)
        try:
            limit = _clamp(arguments.get("head_limit"), SEARCH_DEFAULT_LIMIT, 1,
                           SEARCH_MAX_LIMIT)
        except (TypeError, ValueError):
            return self._text("Error: head_limit must be an integer.", error=True)
        base = self._search_root(raw)
        pattern = os.path.expanduser(pattern.strip())
        if os.path.isabs(pattern):
            base, pattern = os.sep, pattern.lstrip("/")
        while pattern.startswith("./"):
            pattern = pattern[2:]
        prefix, rest = _split_glob(pattern)
        root = os.path.normpath(os.path.join(base, prefix)) if prefix else base
        shown_root = self._shown(root)
        if not rest:
            # No wildcard: a literal path.
            if os.path.isfile(root):
                return self._text(shown_root)
            return self._text(f"No files match {pattern} (not a file).")
        if not os.path.isdir(root):
            return self._text(
                f"Error: {shown_root} does not exist or is not a directory.",
                error=True,
            )
        deadline = time.monotonic() + SEARCH_TIMEOUT
        files, partial = _list_files(root, deadline)
        try:
            regex = re.compile(_glob_source(rest))
        except re.error as exc:
            return self._text(f"Error: invalid glob pattern: {exc}", error=True)
        found: List[Tuple[float, str]] = []
        for rel in files:
            if not regex.fullmatch(rel):
                continue
            full = os.path.join(root, rel)
            try:
                info = os.stat(full)
            except OSError:
                continue
            if _stat.S_ISREG(info.st_mode):
                found.append((info.st_mtime, full))
        if not found:
            body = f"No files match {rest} under {shown_root}."
        else:
            found.sort(key=lambda item: (-item[0], item[1]))
            body = "\n".join(self._shown(path) for _, path in found[:limit])
            if len(found) > limit:
                body += (
                    f"\n[{len(found)} files matched; showing the newest "
                    f"{limit}. Narrow the pattern or raise head_limit]"
                )
        if partial:
            body += "\n[file listing was cut short; results may be partial]"
        return self._text(self._cap(body))

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
        still show the diff. accept_edits applies without asking inside the
        repository (git root, else the working directory) and asks outside
        it, for a symlink that resolves outside it, and under ``.git/``.
        Credential files and system locations ask in every mode and are
        refused when nobody can be asked."""
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
        outside = None
        if guard is None and mode == "accept_edits":
            outside = accept_edits_block_reason(path, self.cwd)
            if outside is None:
                print("  \033[2m(accept edits — inside the repository, auto-applying)\033[0m")
                return None
        if not self.policy.interactive:
            if outside:
                return (
                    f"Refused: {shown} is {outside}; accept_edits applies "
                    "changes without asking only inside the repository, and "
                    "this session cannot prompt. It was not changed."
                )
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
        elif outside:
            print(f"  \033[33m(accept edits — {outside}; asking)\033[0m")

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


# ---------------------------------------------------------------------------
# grep / glob helpers
# ---------------------------------------------------------------------------

GREP_MODES = ("files_with_matches", "content", "count")
GREP_MAX_CONTEXT = 20
GREP_MAX_LINE_CHARS = 500
GREP_MAX_FILE_BYTES = 20 << 20
SEARCH_DEFAULT_LIMIT = 200
SEARCH_MAX_LIMIT = 5000
SEARCH_TIMEOUT = 30
#: Never descended into by the fallback walker outside git repositories.
WALK_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})
WALK_MAX_FILES = 200000


def _clamp(value, default: int, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    return max(low, min(high, int(value)))


def _cut_line(text: str) -> str:
    if len(text) > GREP_MAX_LINE_CHARS:
        cut = len(text) - GREP_MAX_LINE_CHARS
        return text[:GREP_MAX_LINE_CHARS] + f"… [+{cut:,} chars]"
    return text


class _GrepSink:
    """Collects grep results from either backend: withholds matches in
    credential-bearing files, formats entries, and stops at the limit."""

    def __init__(self, client: "FileToolsClient", mode: str, limit: int):
        self.client = client
        self.mode = mode
        self.limit = limit
        self.lines: List[str] = []
        self.count = 0
        self.truncated = False
        self.timed_out = False
        self.skipped_large = 0
        self.withheld = set()
        self._sensitive: Dict[str, bool] = {}
        self._context = None

    def _is_sensitive(self, path: str) -> bool:
        known = self._sensitive.get(path)
        if known is None:
            if self._context is None:
                self._context = _sensitive_context()
            known = bool(sensitive_path_reason(path, self._context))
            self._sensitive[path] = known
        return known

    def separator(self) -> None:
        if self.lines and self.lines[-1] != "--":
            self.lines.append("--")

    def add(self, path: str, lineno: int = 0, sep: str = ":",
            text: str = "") -> bool:
        """Add one entry; False once the limit is passed (stop searching)."""
        path = os.path.abspath(path)
        if self._is_sensitive(path):
            self.withheld.add(path)
            return True
        if self.count >= self.limit:
            self.truncated = True
            return False
        self.count += 1
        shown = self.client._shown(path)
        if self.mode == "files_with_matches":
            self.lines.append(shown)
        elif self.mode == "count":
            self.lines.append(f"{shown}:{text}")
        else:
            self.lines.append(f"{shown}{sep}{lineno}{sep}{_cut_line(text)}")
        return True

    def finish(self) -> List[str]:
        while self.lines and self.lines[-1] == "--":
            self.lines.pop()
        return self.lines


def _rg_search(rg: str, root: str, cwd: str, pattern: str, mode: str,
               context: int, file_glob: str, ignore_case: bool,
               sink: _GrepSink) -> str:
    """Search with ripgrep, streaming into *sink*. Returns an error text
    (rg's complaint) or ''."""
    import tempfile

    args = [rg, "--no-config", "--color=never", "--hidden", "--glob=!.git",
            "--sort=path"]
    if ignore_case:
        args.append("--ignore-case")
    if file_glob:
        args.append("--glob=" + file_glob)
    if mode == "files_with_matches":
        args.append("--files-with-matches")
    elif mode == "count":
        args += ["--count", "--with-filename", "--null"]
    else:
        args += ["--line-number", "--with-filename", "--no-heading", "--null"]
        if context:
            args.append(f"--context={context}")
    args += ["--regexp", pattern, "--", root]
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(
            args, cwd=cwd if os.path.isdir(cwd) else None,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
        )
        timer = threading.Timer(SEARCH_TIMEOUT, proc.kill)
        timer.start()
        stopped = False
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                if line.endswith("\r"):
                    line = line[:-1]
                if mode == "files_with_matches":
                    keep = sink.add(line)
                elif line == "--":
                    sink.separator()
                    continue
                else:
                    path, _, rest = line.partition("\0")
                    if mode == "count":
                        keep = sink.add(path, text=rest)
                    else:
                        match = re.match(r"(\d+)([:-])", rest)
                        if not match:
                            continue
                        keep = sink.add(path, int(match.group(1)), match.group(2),
                                        rest[match.end():])
                if not keep:
                    stopped = True
                    proc.kill()
                    break
        except BaseException:
            proc.kill()
            raise
        finally:
            timer.cancel()
            returncode = proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
        if not stopped and returncode < 0:
            sink.timed_out = True
        if returncode == 2 and not stopped:
            errors.seek(0)
            message = errors.read().decode("utf-8", errors="replace").strip()
            lines = [line.strip() for line in message.splitlines() if line.strip()]
            return _visible(" ".join(lines[:6]) or "ripgrep failed", 400)
    return ""


def _git_files(root: str) -> Optional[List[str]]:
    """Tracked plus untracked-but-not-ignored files under *root* (relative
    to it), or None outside a git work tree."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(
            ["git", "-C", root, "ls-files", "-co", "--exclude-standard", "-z"],
            stdin=subprocess.DEVNULL, capture_output=True, env=env,
            timeout=SEARCH_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    names = [os.fsdecode(name) for name in proc.stdout.split(b"\0") if name]
    # An explicitly named ignored directory lists nothing: walk it instead.
    return names or None


def _list_files(root: str, deadline: float) -> Tuple[List[str], bool]:
    """Files under *root* relative to it, respecting .gitignore inside a
    git repository, and whether the listing was cut short."""
    names = _git_files(root)
    if names is not None:
        return sorted(names), False
    found: List[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in WALK_SKIP_DIRS)
        rel_dir = os.path.relpath(current, root)
        for name in sorted(files):
            found.append(name if rel_dir == "." else os.path.join(rel_dir, name))
        if len(found) >= WALK_MAX_FILES or time.monotonic() > deadline:
            return found, True
    return found, False


def _split_glob(pattern: str) -> Tuple[str, str]:
    """('literal/dir/prefix', 'rest/with/*wildcards')."""
    parts = pattern.split("/")
    for index, part in enumerate(parts):
        if any(ch in part for ch in "*?[{"):
            return "/".join(parts[:index]), "/".join(parts[index:])
    return pattern, ""


def _glob_source(pattern: str) -> str:
    """Regex source for a glob over '/'-separated relative paths: ``*``
    and ``?`` stay inside one path segment, ``**`` crosses segments
    (``**/`` also matches no directory), ``[...]`` and ``{a,b}`` work."""
    out: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**", i):
                if pattern.startswith("**/", i):
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            end = pattern.find("]", i + 2)
            if end == -1:
                out.append(re.escape(ch))
            else:
                body = pattern[i + 1:end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end + 1
                continue
        elif ch == "{":
            end = pattern.find("}", i)
            if end == -1:
                out.append(re.escape(ch))
            else:
                alternatives = pattern[i + 1:end].split(",")
                out.append("(?:" + "|".join(_glob_source(a) for a in alternatives) + ")")
                i = end + 1
                continue
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _glob_filter(file_glob: str) -> Callable[[str], bool]:
    """ripgrep's --glob semantics for the fallback: a pattern without '/'
    matches the file name at any depth, one with '/' the path relative
    to the search root; a leading '!' excludes."""
    if not file_glob:
        return lambda rel: True
    negate = file_glob.startswith("!")
    glob = file_glob[1:] if negate else file_glob
    glob = glob.lstrip("/")
    while glob.startswith("./"):
        glob = glob[2:]
    regex = re.compile(_glob_source(glob))
    by_name = "/" not in glob

    def matches(rel: str) -> bool:
        subject = os.path.basename(rel) if by_name else rel.replace(os.sep, "/")
        return bool(regex.fullmatch(subject)) != negate

    return matches


def _py_search(root: str, pattern: str, mode: str, context: int,
               file_glob: str, ignore_case: bool, sink: _GrepSink) -> str:
    """Pure-Python grep (no ripgrep): .gitignore-aware listing, binary
    files skipped. Returns an error text or ''."""
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return f"invalid regular expression: {exc}"
    try:
        wanted = _glob_filter(file_glob)
    except re.error as exc:
        return f"invalid glob: {exc}"
    deadline = time.monotonic() + SEARCH_TIMEOUT
    if os.path.isfile(root):
        paths = [root]
    else:
        names, partial = _list_files(root, deadline)
        if partial and time.monotonic() > deadline:
            sink.timed_out = True
        paths = [os.path.join(root, name) for name in names if wanted(name)]
    unreadable = 0
    for path in paths:
        if time.monotonic() > deadline:
            sink.timed_out = True
            break
        try:
            if not os.path.isfile(path):
                continue
            if os.path.getsize(path) > GREP_MAX_FILE_BYTES:
                sink.skipped_large += 1
                continue
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            unreadable += 1
            continue
        if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        hits = [i for i, line in enumerate(lines) if regex.search(line)]
        if not hits:
            continue
        if mode == "files_with_matches":
            keep = sink.add(path)
        elif mode == "count":
            keep = sink.add(path, text=str(len(hits)))
        else:
            keep = _emit_context(sink, path, lines, hits, context)
        if not keep:
            break
    if unreadable:
        return f"{unreadable} file(s) unreadable"
    return ""


def _emit_context(sink: _GrepSink, path: str, lines: List[str],
                  hits: List[int], context: int) -> bool:
    hit_set = set(hits)
    ranges: List[List[int]] = []
    for index in hits:
        start, end = max(0, index - context), min(len(lines) - 1, index + context)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], end)
        else:
            ranges.append([start, end])
    for start, end in ranges:
        if context:
            sink.separator()
        for index in range(start, end + 1):
            sep = ":" if index in hit_set else "-"
            if not sink.add(path, index + 1, sep, lines[index]):
                return False
    return True


# ---------------------------------------------------------------------------
# post_edit_check: the user's linter/type checker after each edit
# ---------------------------------------------------------------------------

POST_EDIT_CHECK_TIMEOUT = 30
POST_CHECK_OUTPUT_CHARS = 4000
_OFF_VALUES = ("off", "none", "false", "no", "0")


def post_edit_check_command(config: Optional[dict], path: str) -> str:
    """The configured check template for *path*: a ``post_edit_check.<ext>``
    entry for its extension wins (an empty or ``off`` one disables the
    check for that extension), else ``post_edit_check``; '' when none."""
    config = config or {}
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext:
        wanted = f"post_edit_check.{ext}"
        for key, value in config.items():
            if str(key).strip().lower() == wanted:
                command = str(value or "").strip()
                return "" if command.lower() in _OFF_VALUES else command
    command = str(config.get("post_edit_check") or "").strip()
    return "" if command.lower() in _OFF_VALUES else command


def cap_check_output(text: str, limit: int = POST_CHECK_OUTPUT_CHARS) -> str:
    """Head of *text* (diagnostics lead with what matters), marked when cut."""
    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + f"\n… [output truncated: {len(text) - limit:,} more chars]"
    return text


def _kill_group(proc: "subprocess.Popen") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            proc.kill()
        except OSError:
            pass


def run_post_edit_check(config: Optional[dict], path: str,
                        cwd: Optional[str] = None) -> str:
    """Run the configured ``post_edit_check`` for the just-changed *path*
    in *cwd* and describe the outcome for the model ('' when no check is
    configured). ``{path}`` in the template becomes the shell-quoted path
    (relative to *cwd* when inside it). User-configured, so it runs
    without approval; output is capped and credential-redacted."""
    template = post_edit_check_command(config, path)
    if not template:
        return ""
    try:
        timeout = int((config or {}).get("post_edit_check_timeout")
                      or POST_EDIT_CHECK_TIMEOUT)
    except (TypeError, ValueError):
        timeout = POST_EDIT_CHECK_TIMEOUT
    timeout = max(1, min(timeout, 600))
    base = cwd if cwd and os.path.isdir(cwd) else os.getcwd()
    shown = path
    try:
        rel = os.path.relpath(path, base)
        if rel != os.pardir and not rel.startswith(os.pardir + os.sep):
            shown = rel
    except ValueError:
        pass
    command = template.replace("{path}", shlex.quote(shown))
    label = _visible(command, 200)
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=base, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        return f"[post_edit_check `{label}` could not start: {exc}]"
    try:
        output, _ = proc.communicate(timeout=timeout)
        status = (
            "passed (exit 0)" if proc.returncode == 0
            else f"exited {proc.returncode}"
        )
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            output, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            output = b""
        status = f"timed out after {timeout}s"
    except BaseException:
        _kill_group(proc)
        proc.wait()
        raise
    text = cap_check_output(output.decode("utf-8", errors="replace"))
    text, _labels = _redact(text)
    header = f"[post_edit_check `{label}` {status}]"
    return f"{header}\n{text}" if text else header
