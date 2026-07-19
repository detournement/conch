"""Repo-map-style orientation context (plan 3.2).

When the working directory is inside a git repository, build a compact
structural overview — ranked file list plus top-level symbols — within a
~1k-token budget (the aider repo-map pattern, cheap tree+regex version).
Injected into the system prompt at session start.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple


REPO_MAP_BUDGET_CHARS = 4000  # ~1.1k tokens
MAX_SYMBOL_FILES = 40         # only read this many files for symbols
MAX_FILE_BYTES = 200_000      # skip huge files when extracting symbols

# Extension weight: how interesting a file is for orientation.
_EXT_WEIGHTS = {
    ".py": 10, ".go": 10, ".rs": 10, ".ts": 9, ".tsx": 9, ".js": 9,
    ".jsx": 9, ".rb": 9, ".java": 8, ".kt": 8, ".c": 8, ".h": 8,
    ".cpp": 8, ".swift": 8, ".sh": 7, ".zsh": 7,
    ".md": 4, ".toml": 3, ".yaml": 3, ".yml": 3, ".json": 2, ".cfg": 2,
    ".ini": 2, ".txt": 1,
}

_SPECIAL_FILES = {
    "Makefile": 8, "Dockerfile": 8, "docker-compose.yml": 6,
    "pyproject.toml": 7, "package.json": 7, "go.mod": 7, "Cargo.toml": 7,
    "README.md": 8, "CONCH.md": 6, "AGENTS.md": 6,
}

_SKIP_NAME_RE = re.compile(
    r"(^\.)|(\.lock$)|(-lock\.(json|yaml)$)|(\.min\.(js|css)$)"
)

# Top-level symbol patterns per extension (cheap regex version; tree-sitter
# is the plan's "later").
_SYMBOL_PATTERNS: Dict[str, List[re.Pattern]] = {
    ".py": [re.compile(r"^(?:class|def)\s+([A-Za-z_]\w*)", re.MULTILINE)],
    ".go": [re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", re.MULTILINE),
            re.compile(r"^type\s+([A-Za-z_]\w*)", re.MULTILINE)],
    ".rs": [re.compile(r"^(?:pub\s+)?(?:fn|struct|enum|trait)\s+([A-Za-z_]\w*)", re.MULTILINE)],
    ".sh": [re.compile(r"^([A-Za-z_]\w*)\s*\(\)", re.MULTILINE)],
    ".rb": [re.compile(r"^\s*(?:class|module|def)\s+([A-Za-z_][\w.]*)", re.MULTILINE)],
}
_JS_PATTERNS = [
    re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$]\w*)", re.MULTILINE),
    re.compile(r"^(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$]\w*)", re.MULTILINE),
    re.compile(r"^export\s+const\s+([A-Za-z_$]\w*)", re.MULTILINE),
]
for _ext in (".js", ".jsx", ".ts", ".tsx"):
    _SYMBOL_PATTERNS[_ext] = _JS_PATTERNS


def find_git_root(start_dir: str = "") -> Optional[Path]:
    current = Path(start_dir or os.getcwd()).resolve()
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _list_files(root: Path) -> List[str]:
    """Tracked files via git ls-files, falling back to a filtered walk."""
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=str(root), capture_output=True, timeout=10,
        )
        if proc.returncode == 0:
            return [
                line for line in proc.stdout.decode("utf-8", errors="replace").splitlines()
                if line.strip()
            ]
    except (OSError, subprocess.TimeoutExpired):
        pass
    skip_dirs = {".git", "node_modules", "venv", ".venv", "__pycache__",
                 "dist", "build", ".tox", ".mypy_cache", "target"}
    files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for filename in filenames:
            rel = os.path.relpath(os.path.join(dirpath, filename), root)
            files.append(rel)
        if len(files) > 5000:
            break
    return files


def _rank(path: str) -> int:
    name = os.path.basename(path)
    if _SKIP_NAME_RE.search(name):
        return -1
    weight = _SPECIAL_FILES.get(name)
    if weight is None:
        weight = _EXT_WEIGHTS.get(os.path.splitext(name)[1].lower(), 0)
    if weight <= 0:
        return -1
    depth = path.count(os.sep)
    return weight * 10 - depth  # shallow files first within a weight class


def _symbols(root: Path, rel_path: str, limit: int = 12) -> List[str]:
    patterns = _SYMBOL_PATTERNS.get(os.path.splitext(rel_path)[1].lower())
    if not patterns:
        return []
    full = root / rel_path
    try:
        if full.stat().st_size > MAX_FILE_BYTES:
            return []
        text = full.read_text(errors="replace")
    except OSError:
        return []
    found: List[str] = []
    seen = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            symbol = match.group(1)
            if symbol.startswith("_") or symbol in seen:
                continue
            seen.add(symbol)
            found.append(symbol)
            if len(found) >= limit:
                return found
    return found


def build_repo_map(start_dir: str = "", budget_chars: int = REPO_MAP_BUDGET_CHARS) -> str:
    """Structural overview of the enclosing git repo, or "" when not in one."""
    root = find_git_root(start_dir)
    if root is None:
        return ""
    return build_map_for_root(root, budget_chars)


def build_map_for_root(root: Path, budget_chars: int = REPO_MAP_BUDGET_CHARS) -> str:
    """Ranked file + symbol overview for an explicit *root* directory.

    Works for non-git roots too (installed packages): _list_files falls back
    to a filtered walk when git ls-files isn't available there.
    """
    root = Path(root)
    files = _list_files(root)
    if not files:
        return ""

    ranked: List[Tuple[int, str]] = []
    for path in files:
        score = _rank(path)
        if score >= 0:
            ranked.append((score, path))
    ranked.sort(key=lambda item: (-item[0], item[1]))

    lines = [f"Repository map ({root.name}, {len(files)} tracked files):"]
    used = len(lines[0])
    listed = set()
    for i, (_, path) in enumerate(ranked):
        symbols = _symbols(root, path) if i < MAX_SYMBOL_FILES else []
        line = f"  {path}: {', '.join(symbols)}" if symbols else f"  {path}"
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1
        listed.add(path)

    remaining = len([p for _, p in ranked if p not in listed])
    if remaining:
        lines.append(f"  ... and {remaining} more files")
    return "\n".join(lines)


# Session cache: the map is built once per (cwd) and reused for prompt
# rebuilds (model switches re-run _build_system_prompt).
_map_cache: Dict[str, str] = {}


def get_repo_map(start_dir: str = "") -> str:
    key = str(Path(start_dir or os.getcwd()).resolve())
    if key not in _map_cache:
        _map_cache[key] = build_repo_map(start_dir)
    return _map_cache[key]
