"""Repository awareness for a session started inside a git checkout.

The repository, its GitHub remote, and the branch come from the local
checkout at once (one git call, no network). For a GitHub repository a
snapshot follows in the background from one ``gh api graphql`` request:
description, default branch, latest release, the current branch's pull
request and checks, and the most recently updated open pull requests and
issues. The snapshot joins the system prompt once it arrives, and the
status bar shows the repository, branch, pull request, and check state.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .repomap import find_git_root

FETCH_TIMEOUT_SECONDS = 10
LIST_LIMIT = 5
TITLE_CHARS = 70
DESCRIPTION_CHARS = 160
PROMPT_BUDGET_CHARS = 2000

GITHUB_ICON = "🐙"
BRANCH_ICON = "⎇"
CHECK_ICONS = {"passing": "✓", "failing": "✗", "pending": "●"}

_QUERY = """
query($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner description url visibility isFork isArchived
    parent { nameWithOwner }
    defaultBranchRef { name }
    latestRelease { tagName publishedAt }
    pullRequests(states: OPEN, first: %(limit)d, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount nodes { number title isDraft headRefName author { login } }
    }
    issues(states: OPEN, first: %(limit)d, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount nodes { number title labels(first: 3) { nodes { name } } }
    }
    ref(qualifiedName: $branch) {
      associatedPullRequests(first: 1, orderBy: {field: UPDATED_AT, direction: DESC}) {
        nodes { number title state isDraft reviewDecision baseRefName url }
      }
      target { ... on Commit { abbreviatedOid statusCheckRollup { state contexts(first: 50) {
        nodes { __typename ... on CheckRun { name conclusion } ... on StatusContext { context state } }
      } } } }
    }
  }
}
""" % {"limit": LIST_LIMIT}

# scp-like (git@host:owner/name.git) and URL (https://, ssh://host:port/)
# remotes. Credentials in the user part are matched, never kept.
_REMOTE_RE = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?(?P<host>[^/:@]+)(?::\d+)?[:/]"
    r"(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)

_FAILED_RUNS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_ROLLUP = {"SUCCESS": "passing", "FAILURE": "failing", "ERROR": "failing",
           "PENDING": "pending", "EXPECTED": "pending"}


def parse_remote_url(url: str) -> Optional[Tuple[str, str, str]]:
    """(host, owner, name) of a hosted git remote URL, or None for local paths."""
    match = _REMOTE_RE.match((url or "").strip())
    if not match:
        return None
    return match.group("host"), match.group("owner"), match.group("name")


def github_host(host: str) -> str:
    """The GitHub host a remote host stands for, or "" when it is not GitHub.
    SSH aliases (github-work, github.com-personal) mean github.com; github.*
    names are GitHub Enterprise Server."""
    host = host.lower()
    if host == "github.com" or host.endswith(".github.com") or host.startswith("github.com"):
        return "github.com"
    if host.startswith("github."):
        return host
    if "github" in host and "." not in host:
        return "github.com"
    return ""


def current_branch(root: Path) -> str:
    """The checked-out branch, "" when HEAD is detached. Reads the HEAD file
    (worktrees included), so the status bar can ask on every refresh."""
    git = root / ".git"
    try:
        if git.is_file():  # worktree or submodule: "gitdir: <path>"
            gitdir = Path(git.read_text().partition("gitdir:")[2].strip())
            git = gitdir if gitdir.is_absolute() else (root / gitdir).resolve()
        head = (git / "HEAD").read_text().strip()
    except OSError:
        return ""
    prefix = "ref: refs/heads/"
    return head[len(prefix):] if head.startswith(prefix) else ""


@dataclass(frozen=True)
class Repo:
    root: Path
    host: str = ""  # GitHub host when a remote is on GitHub
    owner: str = ""
    name: str = ""

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}" if self.host else self.root.name


def detect(start_dir: str = "") -> Optional[Repo]:
    """The git repository enclosing *start_dir*, with its GitHub remote when
    one exists (origin first, then upstream, then any other)."""
    root = find_git_root(start_dir)
    if root is None:
        return None
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "config", "--get-regexp", r"^remote\..*\.url$"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        listed = ""
    remotes: Dict[str, str] = {}
    for line in listed.splitlines():
        key, _, url = line.partition(" ")
        remotes.setdefault(key[len("remote."):-len(".url")], url.strip())
    for remote in sorted(remotes, key=lambda name: (name != "origin", name != "upstream", name)):
        parsed = parse_remote_url(remotes[remote])
        host = github_host(parsed[0]) if parsed else ""
        if host:
            return Repo(root, host, parsed[1], parsed[2])
    return Repo(root)


def _gh_error(stderr: str) -> str:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    text = lines[-1] if lines else "gh failed"
    if text.startswith("gh: "):
        text = text[4:]
    lowered = (stderr or "").lower()
    if any(sign in lowered for sign in ("auth login", "bad credentials", "401", "gh_token")):
        return "gh is not signed in (gh auth login)"
    return text[:120]


def fetch(repo: Repo, branch: str) -> Tuple[Optional[dict], str]:
    """(repository data, "") from GitHub through gh, or (None, why not)."""
    gh = shutil.which("gh")
    if not gh:
        return None, "the gh CLI is not installed"
    args = [gh, "api", "graphql", "-f", f"query={_QUERY}", "-f", f"owner={repo.owner}",
            "-f", f"name={repo.name}", "-f", f"branch=refs/heads/{branch}"]
    if repo.host != "github.com":
        args += ["--hostname", repo.host]
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
    try:
        done = subprocess.run(args, capture_output=True, text=True, env=env,
                              timeout=FETCH_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "GitHub did not answer in time"
    except OSError as exc:
        return None, f"gh failed: {exc}"
    if done.returncode != 0:
        return None, _gh_error(done.stderr)
    try:
        data = json.loads(done.stdout)["data"]["repository"]
    except (ValueError, KeyError, TypeError):
        return None, "unexpected answer from gh"
    if not data:
        return None, "repository not found on GitHub"
    return data, ""


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _nodes(value) -> List[dict]:
    return [node for node in ((value or {}).get("nodes") or []) if isinstance(node, dict)]


def checks(data: Optional[dict]) -> Tuple[str, List[str]]:
    """(passing | failing | pending | "", names of failing checks) for the
    head commit of the fetched branch."""
    target = ((data or {}).get("ref") or {}).get("target") or {}
    rollup = target.get("statusCheckRollup") or {}
    failing: List[str] = []
    for node in _nodes(rollup.get("contexts")):
        if node.get("__typename") == "CheckRun":
            bad, name = node.get("conclusion") in _FAILED_RUNS, node.get("name")
        else:
            bad, name = node.get("state") in ("FAILURE", "ERROR"), node.get("context")
        if bad and name and name not in failing:
            failing.append(name)
    return _ROLLUP.get(rollup.get("state") or "", ""), failing


def branch_pr(data: Optional[dict]) -> Optional[dict]:
    """The most recently updated pull request whose head is the fetched branch."""
    prs = _nodes(((data or {}).get("ref") or {}).get("associatedPullRequests"))
    return prs[0] if prs else None


def _pr_state(pr: dict) -> str:
    state = str(pr.get("state") or "").lower()
    if state == "open" and pr.get("isDraft"):
        state = "draft"
    return state


class RepoContext:
    """One session's view of its repository and, on GitHub, its snapshot."""

    def __init__(self, repo: Repo):
        self.repo = repo
        self.branch = current_branch(repo.root)  # the branch the snapshot is for
        self.data: Optional[dict] = None
        self.error = ""
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._listeners: List[Callable[[], None]] = []
        if not repo.host:
            self.done.set()  # nothing to fetch

    def start(self) -> "RepoContext":
        if not self.done.is_set():
            threading.Thread(target=self._fetch, name="conch-github", daemon=True).start()
        return self

    def skip(self, reason: str) -> "RepoContext":
        """Settle without fetching (github_context off)."""
        self.error = reason
        self._settle()
        return self

    def _fetch(self) -> None:
        try:
            self.data, self.error = fetch(self.repo, self.branch)
        except Exception as exc:  # never let a background fetch kill the shell
            self.data, self.error = None, f"fetch failed: {exc}"
        finally:
            self._settle()

    def _settle(self) -> None:
        with self._lock:
            self.done.set()
            listeners, self._listeners = self._listeners, []
        for listener in listeners:
            try:
                listener()
            except Exception:
                pass

    def on_ready(self, callback: Callable[[], None]) -> None:
        """Run *callback* once the snapshot settles (now, if it has)."""
        with self._lock:
            if not self.done.is_set():
                self._listeners.append(callback)
                return
        callback()

    # --- views --------------------------------------------------------------------

    def indicator(self) -> str:
        """Status-bar text: repository, live branch, and, on the snapshot's
        branch, its open pull request and check state."""
        branch = current_branch(self.repo.root)
        if self.repo.host:
            parts = [f"{GITHUB_ICON} {self.repo.slug}", f"{BRANCH_ICON} {branch or 'detached'}"]
        else:
            parts = [f"{self.repo.slug}", f"{BRANCH_ICON} {branch or 'detached'}"]
        if self.data and branch == self.branch:
            pr = branch_pr(self.data)
            if pr and _pr_state(pr) in ("open", "draft"):
                parts.append(f"#{pr.get('number')}")
            icon = CHECK_ICONS.get(checks(self.data)[0])
            if icon:
                parts.append(icon)
        return " ".join(parts)

    def banner_line(self) -> str:
        """One startup line: where the session is, then what GitHub says."""
        parts = [self.indicator()]
        if not self.repo.host:
            return parts[0]
        if self.data:
            pr = branch_pr(self.data)
            if pr:
                parts.append(f"PR #{pr.get('number')} {_pr_state(pr)}")
            status, failing = checks(self.data)
            if status == "failing" and failing:
                parts.append(f"checks failing: {', '.join(failing[:3])}")
            elif status:
                parts.append(f"checks {status}")
            prs = (self.data.get("pullRequests") or {}).get("totalCount", 0)
            issues = (self.data.get("issues") or {}).get("totalCount", 0)
            parts.append(f"{prs} open PR{'s' if prs != 1 else ''}")
            parts.append(f"{issues} open issue{'s' if issues != 1 else ''}")
        elif self.error:
            parts.append(f"no GitHub details: {self.error}")
        else:
            parts.append("loading GitHub details")
        return " · ".join(parts)

    def prompt_section(self) -> str:
        """Bounded system-prompt context: the repository, and once fetched,
        its GitHub snapshot."""
        repo, data = self.repo, self.data
        branch = f"current branch {self.branch}" if self.branch else "detached HEAD"
        if not repo.host:
            return ""  # the repo map already covers a plain git checkout
        where = f"{repo.slug}" + (f" on {repo.host}" if repo.host != "github.com" else "")
        if not data:
            lines = [f"GitHub repository: {where}; {branch}."]
            if self.error:
                lines.append(f"GitHub details were not loaded at startup ({self.error}); "
                             "the gh CLI fetches pull requests, issues, and checks.")
            return "\n".join(lines)
        flags = [str(data.get("visibility") or "").lower()]
        if data.get("isFork") and data.get("parent"):
            flags.append(f"fork of {data['parent'].get('nameWithOwner')}")
        if data.get("isArchived"):
            flags.append("archived")
        head = f"GitHub repository: {where} ({', '.join(flag for flag in flags if flag)})"
        if data.get("description"):
            head += f" — {_cut(data['description'], DESCRIPTION_CHARS)}"
        lines = [head]
        facts = [str(data.get("url") or "")]
        if data.get("defaultBranchRef"):
            facts.append(f"default branch {data['defaultBranchRef'].get('name')}")
        release = data.get("latestRelease")
        if release:
            facts.append(f"latest release {release.get('tagName')} "
                         f"({str(release.get('publishedAt') or '')[:10]})")
        lines.append(" · ".join(fact for fact in facts if fact))
        status, failing = checks(data)
        target = (data.get("ref") or {}).get("target") or {}
        current = f"Current branch: {self.branch}" if self.branch else "Detached HEAD"
        if target.get("abbreviatedOid"):
            current += f" (GitHub head {target['abbreviatedOid']}"
            current += f", checks {status})" if status else ")"
        elif self.branch:
            current += " (not on GitHub)"
        if failing:
            current += f". Failing checks: {', '.join(failing[:6])}"
        lines.append(current + ".")
        pr = branch_pr(data)
        if pr:
            details = [_pr_state(pr)]
            if pr.get("reviewDecision"):
                details.append(f"review {str(pr['reviewDecision']).lower().replace('_', ' ')}")
            lines.append(f"Pull request for this branch: #{pr.get('number')} "
                         f"\"{_cut(pr.get('title'), TITLE_CHARS)}\" ({', '.join(details)}) "
                         f"into {pr.get('baseRefName')} — {pr.get('url')}")
        prs = data.get("pullRequests") or {}
        if prs.get("totalCount"):
            listed = "; ".join(
                f"#{node.get('number')} {_cut(node.get('title'), TITLE_CHARS)} "
                f"[{node.get('headRefName')}, @{(node.get('author') or {}).get('login', '?')}"
                f"{', draft' if node.get('isDraft') else ''}]"
                for node in _nodes(prs))
            lines.append(f"Open pull requests ({prs['totalCount']}): {listed}")
        else:
            lines.append("Open pull requests: none.")
        issues = data.get("issues") or {}
        if issues.get("totalCount"):
            listed = "; ".join(
                f"#{node.get('number')} {_cut(node.get('title'), TITLE_CHARS)}"
                + (f" [{', '.join(label.get('name', '') for label in _nodes(node.get('labels')))}]"
                   if _nodes(node.get("labels")) else "")
                for node in _nodes(issues))
            lines.append(f"Open issues ({issues['totalCount']}, most recently updated): {listed}")
        else:
            lines.append("Open issues: none.")
        lines.append("This GitHub snapshot is from session start; use gh (gh pr view, "
                     "gh run list, gh issue view) for current state.")
        text = "\n".join(lines)
        return text if len(text) <= PROMPT_BUDGET_CHARS else text[:PROMPT_BUDGET_CHARS - 1] + "…"


# One context per working directory for the life of the process; the system
# prompt is rebuilt from it (model switches, /new) without detecting again.
_contexts: Dict[str, Optional[RepoContext]] = {}


def _key(start_dir: str) -> str:
    return str(Path(start_dir or os.getcwd()).resolve())


def start(config: Optional[dict] = None, start_dir: str = "") -> Optional[RepoContext]:
    """Detect the enclosing repository and start its GitHub fetch (once per
    directory). None outside a git checkout."""
    from .config import get_bool

    key = _key(start_dir)
    if key not in _contexts:
        repo = detect(start_dir)
        context = RepoContext(repo) if repo else None
        if context is not None:
            if get_bool(config or {}, "github_context", True):
                context.start()
            elif repo.host:
                context.skip("github_context is off")
        _contexts[key] = context
    return _contexts[key]


def get(start_dir: str = "") -> Optional[RepoContext]:
    """The context :func:`start` made for this directory, if any."""
    return _contexts.get(_key(start_dir))
