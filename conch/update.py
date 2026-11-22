"""Update the running installation without discarding checkout work."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import uuid
from datetime import datetime, timezone
from http.client import HTTPException
from importlib import metadata
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .keys import identity
from .secretguard import credential_findings, redact_credentials

_DISTRIBUTION = "conch-shell"
_OFFICIAL_REPOSITORY = "https://github.com/detournement/conch"
_OFFICIAL_SOURCE = _OFFICIAL_REPOSITORY + "/archive/refs/heads/edge.tar.gz"
_GIT_TIMEOUT = 180
_INSTALL_TIMEOUT = 600
_CHECK_TIMEOUT = 3
_CHECK_MAX_BYTES = 64 * 1024
_OPERATIONS = (
    "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
    "rebase-apply", "sequencer", "BISECT_LOG", "index.lock",
)


class UpdateError(RuntimeError):
    """An update failed; the message describes the state and recovery."""


def _safe_text(text: str) -> str:
    # Git/pip can echo credentials embedded anywhere in a URL, including a
    # query string. Do not rely on recognizing every provider's token shape.
    text = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+", "[source URL]", str(text))
    return redact_credentials(text, "[redacted]")[0]


def _run(command: List[str], *, cwd: Optional[Path] = None,
         timeout: int = _GIT_TIMEOUT, env: Optional[dict] = None,
         input_text: Optional[str] = None, operation: str,
         allow_failure: bool = False) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command, cwd=str(cwd) if cwd else None, env=env,
            input=input_text, capture_output=True, text=True,
            errors="surrogateescape", timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise UpdateError(f"{operation} timed out after {timeout}s.") from None
    except OSError as exc:
        raise UpdateError(f"{operation} could not run: {_safe_text(str(exc))}") from None
    if result.returncode and not allow_failure:
        detail = _safe_text(result.stderr or result.stdout or "no diagnostic output")[-2000:]
        raise UpdateError(f"{operation} failed (exit {result.returncode}):\n{detail}")
    return result


def _git(root: Path, *args: str, allow_failure: bool = False,
         input_text: Optional[str] = None, timeout: int = _GIT_TIMEOUT,
         read_only: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # A caller's shell may be inside another repository or use an alternate
    # index. Those variables must never redirect an installation update.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_NAMESPACE", "GIT_PREFIX", "GIT_CONFIG"):
        env.pop(key, None)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
               GIT_MERGE_AUTOEDIT="no", GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true")
    options = []
    if read_only:
        # Avoid interactive helpers, SSH host-key writes, and implicit fetches
        # from partial clones. An inaccessible source simply has no notice.
        env.update(GIT_NO_LAZY_FETCH="1", GIT_ASKPASS="true", SSH_ASKPASS="true",
                   GIT_SSH_COMMAND="ssh -oBatchMode=yes -oStrictHostKeyChecking=yes "
                   "-oConnectTimeout=3 -oControlMaster=no -oControlPath=none "
                   "-oUpdateHostKeys=no -oPermitLocalCommand=no")
        options = ["-c", "credential.helper=", "-c", "core.askPass=true",
                   "-c", "protocol.ext.allow=never", "-c", "gc.auto=0",
                   "-c", "maintenance.auto=false"]
    return _run(
        ["git", "-c", "merge.autoStash=false", "-c", "rebase.autoStash=false",
         "-c", "rerere.enabled=false", "-c", "rebase.updateRefs=false",
         *options, "-C", str(root), *args], env=env, allow_failure=allow_failure,
        input_text=input_text, timeout=timeout, operation=f"git {args[0]}",
    )


def _git_path(root: Path, name: str) -> Path:
    path = Path(_git(root, "rev-parse", "--git-path", name).stdout.strip())
    return path if path.is_absolute() else root / path


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()


def _status(root: Path) -> str:
    return _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                "--ignore-submodules=none").stdout


def _checkout_state(root: Path) -> Tuple[str, str, str, str]:
    if not root.is_dir() or not (root / ".git").exists():
        raise UpdateError(
            f"{root} is not a root Git checkout. A source snapshot cannot be "
            "updated with Git; reinstall it from its original source."
        )
    top = _git(root, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(top).resolve() != root:
        raise UpdateError("Refusing to update a parent repository; use the actual Conch checkout.")
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
    if branch.returncode:
        raise UpdateError("Detached HEAD: switch to the intended branch and configure its upstream first.")
    for name in _OPERATIONS:
        if _git_path(root, name).exists():
            raise UpdateError(
                f"Unfinished Git operation ({name}); finish or abort it yourself before updating. "
                "For index.lock, first confirm no Git process is running."
            )
    if _git(root, "ls-files", "--unmerged", "-z").stdout:
        raise UpdateError("The index has unresolved conflicts; resolve and commit them before updating.")
    branch_name = branch.stdout.strip()
    remote = _git(root, "config", "--get", f"branch.{branch_name}.remote", allow_failure=True)
    refs = _git(root, "config", "--get-all", f"branch.{branch_name}.merge", allow_failure=True)
    upstream_refs = refs.stdout.splitlines()
    if remote.returncode or refs.returncode or len(upstream_refs) != 1 or not remote.stdout.strip():
        raise UpdateError(
            f"Branch {branch_name!r} needs one upstream. Set it with "
            "git branch --set-upstream-to=<remote>/<branch>, then retry."
        )
    upstream = upstream_refs[0]
    if not upstream.startswith("refs/heads/"):
        raise UpdateError("The upstream is not a branch; configure a branch upstream before updating.")
    return branch_name, remote.stdout.strip(), upstream, _head(root)


def _refuse_ignored_collisions(root: Path, target: str, original: str, rebase: bool) -> None:
    ignored = set(_git(root, "ls-files", "--others", "--ignored",
                       "--exclude-standard", "-z").stdout.split("\0")) - {""}
    if not ignored:
        return
    paths = set(_git(root, "ls-tree", "-r", "--name-only", "-z", target).stdout.split("\0")) - {""}
    if rebase:
        # Replayed commits can temporarily introduce a path absent at either
        # endpoint. Git would otherwise overwrite ignored files at that path.
        commits = _git(root, "rev-list", f"{target}..{original}").stdout
        if commits:
            paths.update(_git(
                root, "diff-tree", "--stdin", "--root", "--no-commit-id",
                "--no-renames", "-r", "--name-only", "-z", input_text=commits,
            ).stdout.split("\0"))
    parents = set()
    for path in paths:
        parent = path
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            parents.add(parent)
    for path in sorted(ignored):
        candidate = path.rstrip("/")
        collision = candidate in paths or candidate in parents
        while not collision and "/" in candidate:
            candidate = candidate.rsplit("/", 1)[0]
            collision = candidate in paths
        if collision:
            raise UpdateError(
                f"Ignored file {path!r} collides with an update path. Move it outside "
                "the checkout yourself, then retry; ignored files have not been touched."
            )


def _stash_entries(root: Path) -> List[Tuple[str, str, str]]:
    entries = []
    for line in _git(root, "stash", "list", "--format=%H%x00%gd%x00%gs").stdout.splitlines():
        fields = line.split("\0", 2)
        if len(fields) == 3:
            entries.append(tuple(fields))
    return entries


def _recovery(root: Path, stash: str) -> str:
    command = f"git -C {shlex.quote(str(root))}"
    return (
        f"Your update stash is retained as {stash}. Inspect it with "
        f"`{command} stash show --stat {stash}`. Resolve any current conflicts "
        "without discarding files; if it has not been restored, apply it to a clean "
        f"worktree with `{command} stash apply --index {stash}`. "
        f"Use `{command} stash list` to locate it and drop only that entry after recovery."
    )


def _save_work(root: Path) -> Optional[str]:
    if not _status(root):
        return None
    marker = "conch-update-" + uuid.uuid4().hex
    try:
        _git(root, "stash", "push", "--include-untracked", "--message", marker)
    except (UpdateError, KeyboardInterrupt) as exc:
        own = [sha for sha, _, subject in _stash_entries(root) if subject.endswith(": " + marker)]
        recovery = _recovery(root, own[0]) if own else "Inspect git status before retrying; no update was applied."
        raise UpdateError(f"Saving local work did not finish. {_safe_text(str(exc))}\n{recovery}") from None
    own = [sha for sha, _, subject in _stash_entries(root) if subject.endswith(": " + marker)]
    if len(own) != 1:
        raise UpdateError(
            f"Could not identify the update stash ({marker}); inspect git stash list "
            "and git status before continuing. No update was applied."
        )
    print(f"Saved staged, unstaged, and untracked work in update stash {own[0][:12]}.")
    return own[0]


def _restore_work(root: Path, stash: Optional[str]) -> None:
    if stash is None:
        return
    try:
        _git(root, "stash", "apply", "--index", stash)
    except (UpdateError, KeyboardInterrupt) as exc:
        raise UpdateError(
            f"Restoring local work failed; no conflicts were discarded or resolved automatically. "
            f"{_safe_text(str(exc))}\n{_recovery(root, stash)}"
        ) from None
    entries = [ref for sha, ref, _ in _stash_entries(root) if sha == stash]
    if len(entries) != 1:
        raise UpdateError(
            "Local work was restored, but the update stash could not be uniquely located for removal.\n"
            + _recovery(root, stash)
        )
    try:
        _git(root, "stash", "drop", entries[0])
    except UpdateError as exc:
        raise UpdateError(f"Local work was restored, but removing its stash failed: {exc}\n"
                          + _recovery(root, stash)) from None
    print("Restored local work, including the staged index; removed only the update stash.")


def _abort_update(root: Path, original: str) -> None:
    if _git_path(root, "rebase-merge").exists() or _git_path(root, "rebase-apply").exists():
        _git(root, "rebase", "--abort")
    elif _git_path(root, "MERGE_HEAD").exists():
        _git(root, "merge", "--abort")
    elif _head(root) != original:
        # A command may have completed its commit before reporting failure.
        # --merge, unlike --hard, refuses to overwrite intervening local edits.
        _git(root, "reset", "--merge", original)
    if _head(root) != original:
        raise UpdateError(f"Could not restore the original HEAD {original}; inspect git status.")


def update_checkout(root: Path, *, strategy: str = "merge", check: bool = False) -> bool:
    """Fetch and integrate one checkout's upstream; return whether HEAD changed.

    Package installation is intentionally separate: callers can update a Git
    checkout without redirecting the Python environment running this function.
    """
    if strategy not in ("merge", "rebase"):
        raise UpdateError("Unknown update strategy; choose merge or rebase.")
    root = Path(root).resolve()
    branch, remote, upstream, original = _checkout_state(root)
    print(f"Conch checkout: {root}\nBranch: {branch}; upstream: {_safe_text(remote)}/{upstream[11:]}")
    _git(root, "fetch", "--no-tags", "--no-recurse-submodules", "--", remote, upstream)
    target = _git(root, "rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.strip()
    counts = _git(root, "rev-list", "--left-right", "--count", f"{original}...{target}").stdout.split()
    ahead, behind = (int(value) for value in counts)
    action = ("already up to date" if not ahead and not behind else
              "keep local-only commits (no incoming commits)" if not behind else
              "fast-forward" if not ahead else strategy)
    print(f"Fetched {target[:12]}: {ahead} local-only, {behind} incoming commit(s); plan: {action}.")
    if check:
        print("Check only: HEAD, index, worktree, and stashes are unchanged; no packages installed.")
        return False
    if not behind:
        return False
    # Fail before moving work into a stash, including for unrelated histories.
    if _git(root, "merge-base", original, target, allow_failure=True).returncode:
        raise UpdateError("Upstream has unrelated history; inspect the configured upstream before updating.")
    _refuse_ignored_collisions(root, target, original, bool(ahead and strategy == "rebase"))
    stash = _save_work(root)
    started = False
    try:
        if _head(root) != original or _status(root):
            raise UpdateError(
                "The checkout changed during preparation or contains work Git cannot stash "
                "(for example, a dirty submodule). Stop other writers and preserve that work manually."
            )
        started = True
        if not ahead:
            _git(root, "merge", "--ff-only", "--commit", "--no-squash", "--no-overwrite-ignore", target)
        elif strategy == "rebase":
            _git(root, "rebase", "--rebase-merges", "--no-autostash", target)
        else:
            _git(root, "merge", "--no-ff", "--commit", "--no-squash", "--no-edit",
                 "--no-stat", "--no-overwrite-ignore", target)
    except (UpdateError, KeyboardInterrupt) as exc:
        try:
            if started:
                _abort_update(root, original)
        except (UpdateError, KeyboardInterrupt) as abort_exc:
            recovery = _recovery(root, stash) if stash else "Inspect git status; local work was not stashed."
            raise UpdateError(
                f"Update failed: {_safe_text(str(exc))}\nAutomatic abort failed: "
                f"{_safe_text(str(abort_exc))}\nOriginal HEAD: {original}. {recovery}"
            ) from None
        try:
            _restore_work(root, stash)
        except UpdateError as restore_exc:
            state = f"HEAD was restored to {original[:12]}" if started else "no update was started"
            raise UpdateError(f"Update failed; {state}.\n{restore_exc}") from None
        state = ("the operation was aborted, original HEAD and local work restored"
                 if started else "no update was started; saved local work restored")
        raise UpdateError(f"Update failed; {state}.\n{_safe_text(str(exc))}") from None
    changed = _head(root) != original
    try:
        _restore_work(root, stash)
    except UpdateError as exc:
        raise UpdateError(f"Git update was applied (HEAD {_head(root)[:12]}), but local-work recovery needs attention.\n{exc}") from None
    print(f"Checkout updated: {original[:12]} -> {_head(root)[:12]}.")
    return changed


def build_parser(prog: str = "conch update") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, allow_abbrev=False,
        description="Update this Conch installation from its Git/source origin, preserving local work.",
    )
    parser.add_argument("--check", action="store_true", help="fetch/report only; do not change local work or install packages")
    strategy = parser.add_mutually_exclusive_group()
    strategy.add_argument("--merge", dest="strategy", action="store_const", const="merge",
                          help="merge divergent checkout history (default)")
    strategy.add_argument("--rebase", dest="strategy", action="store_const", const="rebase",
                          help="explicitly rebase local commits onto the upstream")
    parser.set_defaults(strategy="merge")
    return parser


def _direct_url(dist) -> Optional[dict]:
    raw = dist.read_text("direct_url.json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise UpdateError("The installation's direct_url.json is invalid; repair its source metadata before updating.") from None
    if not isinstance(data, dict) or not isinstance(data.get("url"), str) or not data["url"]:
        raise UpdateError("The installation's direct URL metadata has no usable source URL.")
    return data


def _local_source(url: str) -> Optional[Path]:
    parsed = urlsplit(url)
    if parsed.scheme != "file":
        return None
    if parsed.netloc not in ("", "localhost"):
        raise UpdateError("Remote file URLs are not supported; reinstall from an explicit Git or HTTPS source.")
    return Path(unquote(parsed.path)).resolve()


def _running_distribution(pkg: Path):
    try:
        dist = metadata.distribution(_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None
    direct = _direct_url(dist)
    editable = direct and isinstance(direct.get("dir_info"), dict) and direct["dir_info"].get("editable")
    if editable:
        source = _local_source(direct["url"])
        matches = source is not None and (source / "conch").resolve() == pkg
    else:
        matches = Path(dist.locate_file("conch")).resolve() == pkg
    if not matches:
        raise UpdateError(
            "This interpreter has a different Conch installation registered. Refusing to replace it. "
            "Run this checkout with its own virtual-environment Python, then retry."
        )
    return dist


def _user_install(pkg: Path, dist, *, checkout: bool = False) -> bool:
    """Keep a pip --user installation in the same scheme, not the system copy."""
    location = Path(dist.locate_file("")).resolve() if dist is not None else pkg.parent
    user_site = site.getusersitepackages()
    user_sites = user_site if isinstance(user_site, (tuple, list)) else [user_site]
    if any(location == Path(path).resolve() for path in user_sites):
        return True
    if not checkout:
        environment_sites = {Path(sysconfig.get_path(key)).resolve() for key in ("purelib", "platlib")}
        if location not in environment_sites:
            raise UpdateError(
                "This Conch uses a custom installation directory outside this interpreter's "
                "package paths. Update it with its original --target/--prefix installation "
                "command; automatic update cannot safely select that destination."
            )
    return False


def _source_spec(url: str, *, subdirectory: str = "") -> str:
    if not url or any(char.isspace() or ord(char) < 32 for char in url):
        raise UpdateError("The recorded installation source is not a supported URL; reinstall from an explicit source.")
    git = url.startswith("git+")
    parsed = urlsplit(url[4:] if git else url)
    allowed = ("https", "http", "ssh", "git", "file") if git else ("https", "http", "file")
    if parsed.scheme not in allowed or (parsed.scheme != "file" and not parsed.hostname):
        raise UpdateError("Unsupported installation source; only Git URLs and direct HTTP(S)/local package URLs can be updated.")
    if parsed.password or (parsed.username and parsed.scheme != "ssh") or credential_findings(url):
        raise UpdateError("The source URL contains credentials; replace it with a credential-free URL and use a Git credential helper.")
    if subdirectory:
        parts = subdirectory.replace("\\", "/").split("/")
        if subdirectory.startswith("/") or ".." in parts or any(ord(c) < 32 for c in subdirectory):
            raise UpdateError("Unsafe source subdirectory in installation metadata; repair the metadata before updating.")
        if parsed.fragment:
            raise UpdateError("Ambiguous source subdirectory and URL fragment in installation metadata.")
        url += "#subdirectory=" + quote(subdirectory, safe="/")
    if parsed.scheme == "file" and not git:
        path = _local_source(url)
        if path is None or not path.exists():
            raise UpdateError("The recorded local installation source no longer exists; reinstall from its original Git/HTTPS source.")
    return f"{_DISTRIBUTION} @ {url}"


def _official_distribution(dist) -> bool:
    urls = list(dist.metadata.get_all("Project-URL") or [])
    urls.append(dist.metadata.get("Home-page", ""))
    return any(url.rsplit(",", 1)[-1].strip().rstrip("/").removesuffix(".git")
               == _OFFICIAL_REPOSITORY for url in urls)


def _installed_source(dist) -> Tuple[str, str]:
    direct = _direct_url(dist)
    record = identity.install_source_record()
    if direct:
        url = direct["url"]
        vcs = direct.get("vcs_info")
        subdirectory = direct.get("subdirectory", "")
        if not isinstance(subdirectory, str):
            raise UpdateError("Invalid source subdirectory in installation metadata.")
        if vcs is not None:
            if not isinstance(vcs, dict) or vcs.get("vcs") != "git":
                raise UpdateError("Only Git VCS installations can be updated automatically; use the original installer for this VCS.")
            revision = vcs.get("requested_revision", "")
            if not isinstance(revision, str) or any(c.isspace() or ord(c) < 32 for c in revision):
                raise UpdateError("Invalid Git revision in installation metadata.")
            url = url if url.startswith("git+") else "git+" + url
            if revision:
                parsed = urlsplit(url)
                url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path + "@" + quote(revision, safe="/"),
                                  parsed.query, parsed.fragment))
            return _source_spec(url, subdirectory=subdirectory), "recorded Git source" + (
                f" (revision {_safe_text(revision)})" if revision else " (default branch)"
            )
        local = _local_source(url)
        directory = direct.get("dir_info")
        archive = direct.get("archive_info")
        if not isinstance(directory, dict) and not isinstance(archive, dict):
            raise UpdateError("Unsupported direct URL metadata; repair the installation's recorded source before updating.")
        if isinstance(directory, dict) and (directory.get("editable") or local is None):
            raise UpdateError(
                "The editable/directory source is not a recognized Git checkout. "
                "Reinstall it from its original source; automatic update cannot safely replace it."
            )
        if local is not None and not local.exists() and record.get("source"):
            # Website installers may install an extracted temporary directory.
            # Their durable source record survives removal of that directory.
            return _source_spec(record["source"]), "recorded installer source"
        return _source_spec(url, subdirectory=subdirectory), "recorded direct URL source"
    if record.get("source"):
        return _source_spec(record["source"]), "recorded installer source"
    if _official_distribution(dist):
        return _source_spec(_OFFICIAL_SOURCE), "official GitHub edge tarball (no source recorded)"
    raise UpdateError(
        "No update source is recorded for this installation. Reinstall it from its original Git/direct URL source; "
        "Conch will not guess a fork's branch or fall back to an old PyPI package."
    )


def _check_git(root: Path, *args: str, allow_failure: bool = False,
                input_text: Optional[str] = None):
    return _git(root, *args, allow_failure=allow_failure, input_text=input_text,
                timeout=_CHECK_TIMEOUT, read_only=True)


def _remote_refs(root: Path, source: str, *refs: str) -> dict:
    result = _check_git(root, "ls-remote", "--", source, *refs)
    found = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0]):
            if fields[1] in refs:
                found[fields[1]] = fields[0]
    return found


def _checkout_update_available(root: Path) -> Optional[str]:
    if not (root / ".git").exists():
        return None
    if Path(_check_git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve() != root:
        return None
    branch = _check_git(root, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    remote = _check_git(root, "config", "--get", f"branch.{branch}.remote").stdout.strip()
    refs = _check_git(root, "config", "--get-all", f"branch.{branch}.merge").stdout.splitlines()
    if not remote or len(refs) != 1 or not refs[0].startswith("refs/heads/"):
        return None
    tip = _remote_refs(root, remote, refs[0]).get(refs[0])
    if not tip:
        return None
    head = _check_git(root, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    if head == tip:
        return None
    # A tip absent from our object database cannot be an ancestor in a full
    # clone. Never fetch just to answer this question, even in a partial clone.
    kind = _check_git(root, "cat-file", "--batch-check=%(objecttype)",
                       input_text=tip + "\n").stdout.strip()
    if kind == "commit":
        ancestor = _check_git(root, "merge-base", "--is-ancestor", tip, head,
                              allow_failure=True)
        if ancestor.returncode != 1:
            return None
    elif kind == tip + " missing":
        shallow = _check_git(root, "rev-parse", "--is-shallow-repository").stdout.strip()
        if shallow != "false":
            return None
    else:
        return None
    return f"upstream {tip[:12]}"


def _installed_git_update(pkg: Path, direct: dict) -> Optional[str]:
    vcs = direct["vcs_info"]
    commit = vcs.get("commit_id", "")
    revision = vcs.get("requested_revision", "")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        return None
    if revision and (re.fullmatch(r"[0-9a-fA-F]{7,64}", revision)
                     or revision.startswith("refs/tags/")):
        return None
    if revision.startswith("refs/") and not revision.startswith("refs/heads/"):
        return None
    ref = revision if revision.startswith("refs/heads/") else (
        "refs/heads/" + revision if revision else "HEAD"
    )
    # An unqualified tag must not accidentally track a same-named branch.
    tag = "refs/tags/" + revision if revision and not revision.startswith("refs/") else ""
    source = direct["url"].removeprefix("git+")
    refs = _remote_refs(pkg, source, ref, *([tag] if tag else []))
    if tag and tag in refs:
        return None
    tip = refs.get(ref)
    return f"source {tip[:12]}" if tip and tip != commit else None


def _version_key(version: str) -> Optional[tuple]:
    """Order numeric PEP 440 releases/pre/post/dev; unknown/local forms abstain."""
    match = re.fullmatch(
        r"v?(?:(\d+)!)?(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?"
        r"(?:\.post(\d+))?(?:\.dev(\d+))?", version, re.IGNORECASE,
    )
    if not match:
        return None
    epoch, release, pre, pre_number, post, dev = match.groups()
    numbers = [int(part) for part in release.split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    if pre:
        prerelease = ({"a": 0, "b": 1, "rc": 2}[pre.lower()], int(pre_number))
    else:
        prerelease = (-1, 0) if dev is not None and post is None else (3, 0)
    return (int(epoch or 0), tuple(numbers), prerelease,
            int(post) if post is not None else -1,
            int(dev) if dev is not None else float("inf"))


class _CheckRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com":
            raise UpdateError("Unsupported update-check redirect.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _archive_update_available(source: str) -> Optional[str]:
    match = re.fullmatch(
        re.escape(_OFFICIAL_REPOSITORY) + r"/archive/refs/heads/(edge|main)\.tar\.gz",
        source,
    )
    current = _version_key(__version__)
    if not match or current is None:
        return None
    url = ("https://raw.githubusercontent.com/detournement/conch/"
           + match[1] + "/conch/__init__.py")
    request = Request(url, headers={"User-Agent": "conch-update-check"})
    with build_opener(_CheckRedirectHandler()).open(request, timeout=_CHECK_TIMEOUT) as response:
        content = response.read(_CHECK_MAX_BYTES + 1)
    if len(content) > _CHECK_MAX_BYTES:
        return None
    # Parse a literal, never import/exec remote Python or trust a global
    # installer commit that may describe a different installation.
    tree = ast.parse(content.decode("utf-8"))
    versions = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                return None
            versions.append(node.value.value)
    if len(versions) != 1:
        return None
    available = _version_key(versions[0])
    return versions[0] if available is not None and available > current else None


def check_for_update() -> Optional[str]:
    """Quiet, read-only background check of this installation's update source.

    No fetch/install/cache writes. Git commands and HTTPS reads have short
    timeouts; unavailable, pinned, ambiguous or unsupported sources abstain.
    Archive checks only compare literal versions on official edge/main.
    """
    try:
        pkg = identity.package_dir().resolve()
        root = identity.checkout_root(pkg)
        if root is not None:
            return _checkout_update_available(Path(root).resolve())
        dist = _running_distribution(pkg)
        if dist is None:
            return None
        # Keep exactly the same source precedence and validation as installation.
        spec, _ = _installed_source(dist)
        direct = _direct_url(dist)
        if direct and direct.get("vcs_info") is not None:
            return _installed_git_update(pkg, direct)
        return _archive_update_available(spec.split(" @ ", 1)[1])
    except (UpdateError, OSError, HTTPException, ValueError, TypeError, SyntaxError, RecursionError):
        return None


def _installer_command(spec: str, *, editable: bool = False, user: bool = False) -> List[str]:
    probe = _run([sys.executable, "-m", "pip", "--version"], timeout=30,
                 operation="Checking this interpreter's pip", allow_failure=True)
    pip_usable = probe.returncode == 0
    if editable and pip_usable:
        version = re.match(r"pip (\d+)\.(\d+)", probe.stdout)
        if not version:
            raise UpdateError("Cannot determine pip's editable-build support; repair this interpreter's pip first.")
        if tuple(map(int, version.groups())) < (21, 3):
            if not user and shutil.which("uv"):
                pip_usable = False
            elif sys.prefix == sys.base_prefix and not user:
                raise UpdateError(
                    "This system Python's pip predates editable pyproject builds. "
                    "Use this checkout's virtual environment or install uv; system pip was not changed."
                )
            else:
                print("Upgrading this environment's pip for editable pyproject builds.")
                bootstrap = [sys.executable, "-m", "pip", "install", "--upgrade",
                             "--no-input", "--disable-pip-version-check"]
                if user:
                    bootstrap.append("--user")
                _install(bootstrap + ["pip>=21.3"], Path(sys.prefix))
    if pip_usable:
        command = [sys.executable, "-m", "pip", "install", "--upgrade", "--no-input",
                   "--disable-pip-version-check"]
        if user:
            command.append("--user")
        command += ["--editable", spec] if editable else ["--force-reinstall", "--no-cache-dir", spec]
        return command
    if user:
        raise UpdateError(
            "Updating this pip --user installation requires pip in the running interpreter. "
            "Install pip for this Python, then retry; uv cannot preserve this user install scheme."
        )
    uv = shutil.which("uv")
    if uv:
        command = [uv, "pip", "install", "--python", sys.executable, "--upgrade"]
        command += ["--editable", spec] if editable else ["--reinstall-package", _DISTRIBUTION, "--no-cache", spec]
        return command
    raise UpdateError(
        "This Conch interpreter has neither pip nor an available uv installer. "
        "Install pip in this environment or put uv on PATH, then retry."
    )


def _install(command: List[str], cwd: Path) -> None:
    env = os.environ.copy()
    # An environment/configured --target, --prefix, or --user must not redirect
    # installation into a different Conch copy. Ignore installer config files.
    for key in tuple(env):
        if (key.startswith("PIP_") or key.startswith("UV_")
                or key in ("PYTHONPATH", "PYTHONHOME", "GIT_DIR", "GIT_WORK_TREE",
                           "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                           "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CONFIG")):
            env.pop(key, None)
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_USER="0", UV_NO_CONFIG="1", GIT_TERMINAL_PROMPT="0")
    _run(command, cwd=cwd, env=env, timeout=_INSTALL_TIMEOUT,
         operation="Installing Conch and dependencies in the running Python environment")


def _refresh_source_record(spec: str) -> None:
    """Do not attribute the website installer's old hash/commit to new code."""
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch"
    record = state / "install-source"
    if not record.exists():
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=state,
                                         prefix=".install-source-", delete=False) as stream:
            temporary = Path(stream.name)
            stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            stream.write(f"installed_at={stamp}\nsource={spec.split(' @ ', 1)[1]}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, record)
    except OSError as exc:
        raise UpdateError(
            "The package update completed, but refreshing its install-source record failed: "
            f"{_safe_text(str(exc))}. Remove the old commit/sha256/verified fields from "
            f"{record} before trusting its provenance, then start a fresh Conch process."
        ) from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def update_checkout_installation(root: Path, *, strategy: str = "merge",
                                 check: bool = False) -> bool:
    """Update a checkout using the Python interpreter running this function.

    The website installer can load this module from a verified download while
    retaining an older checkout's interpreter and installation destination.
    """
    root = Path(root).resolve()
    if check:
        return update_checkout(root, strategy=strategy, check=True)
    pkg = (root / "conch").resolve()
    dist = _running_distribution(pkg)
    command = _installer_command(str(root), editable=True,
                                 user=_user_install(pkg, dist, checkout=True))
    changed = update_checkout(root, strategy=strategy)
    print("Refreshing the editable package and dependencies in this interpreter.")
    try:
        _install(command, root)
    except (UpdateError, OSError, ValueError, KeyboardInterrupt) as exc:
        state = ("Git changes were applied and local work restored, but "
                 if changed else "Git is unchanged, but ")
        raise UpdateError(
            state + "package/dependency installation failed: " + _safe_text(str(exc))
            + "\nFix the installer problem, rerun `conch update`, then start a fresh "
            "Conch process. Do not continue an old Conch process after updating its files."
        ) from None
    return changed


def main(argv=None, *, prog: str = "conch update") -> int:
    """CLI/slash entry point; never leak argparse's SystemExit to a caller."""
    try:
        args = build_parser(prog).parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        pkg = identity.package_dir().resolve()
        root = identity.checkout_root(pkg)
        shape = identity.install_shape(pkg)
        print(f"Running Conch: {pkg} ({shape})\nPython: {sys.executable}")
        if root is not None:
            root = Path(root).resolve()
            if not (root / ".git").exists():
                raise UpdateError(
                    "This Conch is a source snapshot, not a Git checkout. Refusing to search its parent repositories. "
                    "Reinstall this copy from its original source or run Conch from a real clone."
                )
            update_checkout_installation(root, strategy=args.strategy, check=args.check)
            if args.check:
                return 0
        else:
            dist = _running_distribution(pkg)
            if dist is None:
                raise UpdateError("Cannot identify the running conch-shell distribution; reinstall this copy from its original source.")
            spec, description = _installed_source(dist)
            print(f"Update source: {description}. Only this Python environment will be updated.")
            if args.check:
                print("Check only: would reinstall from this source; remote package versions are not compared. No packages installed.")
                return 0
            command = _installer_command(spec, user=_user_install(pkg, dist))
            _install(command, Path(sys.prefix))
            _refresh_source_record(spec)
        invoked = shutil.which(sys.argv[0]) or sys.argv[0]
        launcher = Path(invoked).absolute()
        if launcher.name != "conch" or not launcher.is_file():
            launcher = root / "bin/conch" if root else Path(sysconfig.get_path("scripts")) / "conch"
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("PYTHONPATH", "PYTHONHOME")}
        # Load the freshly installed repair code, not modules from this process.
        repair = _run(
            [sys.executable, "-m", "conch.launchers", "--launcher", str(launcher)],
            cwd=Path.home(), env=environment, timeout=60,
            operation="Reconciling shell startup launchers", allow_failure=True,
        )
        print(repair.stdout, end="")
        if repair.returncode:
            raise UpdateError(
                "Package updated, but launcher selection still needs attention. "
                + _safe_text(repair.stderr)
                + "\nResolve the reported startup conflicts before opening a new terminal."
            )
        print("Conch and dependencies updated. No modules were hot-reloaded. Exit this process and start `conch` again "
              "(using the same launcher/environment); restart any long-running Conch services separately.")
        return 0
    except (UpdateError, OSError, ValueError, KeyboardInterrupt) as exc:
        detail = _safe_text(str(exc)) or "Update interrupted. Inspect the installation before retrying."
        print(f"{prog}: {detail}", file=sys.stderr)
        return 1
