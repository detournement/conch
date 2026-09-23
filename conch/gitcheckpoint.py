"""Turn-level git checkpoints: every code-writing turn leaves a restorable
snapshot, without ever touching the user's branch, index, stash, or history.

Mechanism (plumbing only, chosen for non-invasiveness):

- a TEMPORARY index file (``GIT_INDEX_FILE``) is seeded from HEAD, then
  ``git add -A`` captures the worktree — tracked and untracked files,
  with ``.gitignore`` respected for free and likely-secret paths
  excluded at add time via pathspec magic (so a restore can never
  delete or rewrite a secret file the snapshot deliberately skipped);
- ``git write-tree`` + ``git commit-tree -p HEAD`` mint a snapshot
  commit that lives only under ``refs/conch/checkpoints/<millis>``;
- the user's real index and branch are never read from or written to;
  restore is a plain ``git checkout <sha> -- .`` of the snapshot tree
  (files created after the snapshot are left alone).

The refs namespace is pruned to ``git_checkpoint_limit`` (default 20).
Everything here is best-effort: any git failure degrades to "no
checkpoint this turn", never a broken turn.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from typing import Dict, List, Optional, Tuple

REF_PREFIX = "refs/conch/checkpoints"

# Paths never captured in a checkpoint, matched with git's own icase
# glob pathspec magic against the full path. Deliberately name-shaped
# and conservative: the cost of excluding is only "not checkpointed",
# while a match keeps secret bytes out of the object store refs.
SECRET_PATHSPECS = (
    ".env", "**/.env", ".env.*", "**/.env.*",
    "*.pem", "**/*.pem", "*.key", "**/*.key",
    "*.p12", "**/*.p12", "*.pfx", "**/*.pfx",
    "id_rsa*", "**/id_rsa*", "id_ed25519*", "**/id_ed25519*",
    "id_ecdsa*", "**/id_ecdsa*",
    "credentials", "**/credentials", "credentials.json",
    "**/credentials.json", ".netrc", "**/.netrc",
    "*.keystore", "**/*.keystore",
)

_SNAPSHOT_IDENT = {
    "GIT_AUTHOR_NAME": "conch checkpoint",
    "GIT_AUTHOR_EMAIL": "checkpoint@conch.local",
    "GIT_COMMITTER_NAME": "conch checkpoint",
    "GIT_COMMITTER_EMAIL": "checkpoint@conch.local",
}


class GitCheckpoints:
    def __init__(self, config: dict, cwd: Optional[str] = None):
        self._config = config
        self._cwd = cwd or os.getcwd()
        self._root: Optional[str] = None
        self._root_checked = False

    # -- plumbing helpers ---------------------------------------------------

    def _git(self, *args: str, env: Optional[Dict[str, str]] = None,
             timeout: int = 30) -> Optional[str]:
        full_env = dict(os.environ)
        full_env.update(_SNAPSHOT_IDENT)
        if env:
            full_env.update(env)
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.repo_root() or self._cwd,
                capture_output=True, text=True, timeout=timeout,
                env=full_env,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def repo_root(self) -> Optional[str]:
        if self._root_checked:
            return self._root
        self._root_checked = True
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self._cwd, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode == 0:
            self._root = proc.stdout.strip() or None
        return self._root

    def enabled(self) -> bool:
        from .config import get_bool

        if not get_bool(self._config, "git_checkpoints", default=True):
            return False
        return self.repo_root() is not None

    def _head(self) -> Optional[str]:
        out = self._git("rev-parse", "--verify", "-q", "HEAD")
        return out.strip() if out else None

    # -- change detection ----------------------------------------------------

    def fingerprint(self) -> str:
        """Cheap digest of the worktree state (HEAD + porcelain status).
        Equal fingerprints across a turn mean the turn wrote nothing."""
        head = self._head() or "unborn"
        status = self._git("status", "--porcelain=v1", "-z") or ""
        digest = hashlib.sha1(
            (head + "\x00" + status).encode("utf-8", "replace")
        ).hexdigest()
        return digest

    # -- snapshot ------------------------------------------------------------

    def _last(self) -> Optional[Dict[str, str]]:
        entries = self.list()
        return entries[-1] if entries else None

    def snapshot(self, label: str) -> Optional[Dict[str, str]]:
        """Record one checkpoint; returns ``{sha, ref, stat}`` or None
        (disabled, nothing new, or any git failure)."""
        root = self.repo_root()
        if not root or not self.enabled():
            return None
        head = self._head()
        with tempfile.TemporaryDirectory(prefix="conch-ckpt-") as tmp:
            index = os.path.join(tmp, "index")
            env = {"GIT_INDEX_FILE": index}
            if head:
                if self._git("read-tree", "HEAD", env=env) is None:
                    return None
            else:
                if self._git("read-tree", "--empty", env=env) is None:
                    return None
            add_args = ["add", "-A", "--", "."]
            add_args += [
                f":(exclude,glob,icase){spec}" for spec in SECRET_PATHSPECS
            ]
            if self._git(*add_args, env=env) is None:
                return None
            tree = (self._git("write-tree", env=env) or "").strip()
        if not tree:
            return None
        last = self._last()
        if last:
            last_tree = (self._git("rev-parse", f"{last['sha']}^{{tree}}") or "").strip()
            if last_tree == tree:
                return None  # nothing new since the previous checkpoint
        elif head:
            head_tree = (self._git("rev-parse", "HEAD^{tree}") or "").strip()
            if head_tree == tree:
                return None  # worktree is clean; HEAD already has it
        message = f"conch checkpoint: {(label or 'turn').strip()[:200]}"
        commit_args = ["commit-tree", tree, "-m", message]
        if head:
            commit_args += ["-p", head]
        sha = (self._git(*commit_args) or "").strip()
        if not sha:
            return None
        ref = f"{REF_PREFIX}/{int(time.time() * 1000):013d}"
        if self._git("update-ref", ref, sha) is None:
            return None
        self._prune()
        base = head or (self._git("hash-object", "-t", "tree", "/dev/null") or "").strip()
        stat = ""
        if base:
            stat = (self._git("diff", "--shortstat", base, sha) or "").strip()
        return {"sha": sha, "ref": ref, "stat": stat or "no diff vs HEAD"}

    def _prune(self) -> None:
        from .config import get_int

        limit = max(1, get_int(self._config, "git_checkpoint_limit", 20))
        entries = self.list()
        for entry in entries[:-limit] if len(entries) > limit else []:
            self._git("update-ref", "-d", entry["ref"])

    # -- inspection / restore -------------------------------------------------

    def list(self) -> List[Dict[str, str]]:
        out = self._git(
            "for-each-ref", "--sort=refname",
            "--format=%(refname)%09%(objectname)%09%(contents:subject)",
            REF_PREFIX,
        )
        entries: List[Dict[str, str]] = []
        for line in (out or "").splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 2:
                continue
            label = parts[2] if len(parts) > 2 else ""
            if label.startswith("conch checkpoint: "):
                label = label[len("conch checkpoint: "):]
            entries.append({
                "ref": parts[0], "sha": parts[1], "label": label,
            })
        return entries

    def _is_checkpoint(self, sha: str) -> bool:
        return any(e["sha"] == sha for e in self.list())

    def diff_stat(self, sha: str) -> str:
        if not self._is_checkpoint(sha):
            return "not a conch checkpoint"
        head = self._head()
        base = head or ""
        out = self._git("diff", "--stat", base, sha) if base else None
        return (out or "").strip() or "(no diff vs HEAD)"

    def restore(self, sha: str) -> Tuple[bool, str]:
        """Write a checkpoint's tree back into the worktree.

        Only conch checkpoint refs are restorable; the user's index and
        branch are untouched (files land as uncommitted modifications),
        and files created after the snapshot are left in place."""
        if not self._is_checkpoint(sha):
            return False, "not a conch checkpoint (see /checkpoint list)"
        if self._git("checkout", sha, "--", ".") is None:
            return False, "git checkout failed"
        return True, (
            "worktree restored from checkpoint (uncommitted; review with "
            "git status / git diff)"
        )
