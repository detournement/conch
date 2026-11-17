"""Skill system (plan 4.1): reusable skill definitions in
~/.config/conch/skills/ plus skills shipped with the package.

One markdown file per skill: frontmatter (name, description, allowed tools,
optional model/provider preference, optional round budget) plus a body of
instructions/procedure. Skills are the richer sibling of custom slash
commands: a slash command is a one-shot prompt template; a skill also scopes
*tools* and *model*, can be invoked by the model itself (skill_manage tool,
in-context), and drives skill-scoped subagents (plan 4.2).

Built-in skills (the flow-pack precedent, ``conch/capitol/packs/data``):
``conch/skills_data/<name>/SKILL.md`` ships with the package, discovered
by the same loader, with companion documents (reference.md, cookbook.md)
beside the SKILL.md that the rendered block points the model at. A user
skill of the same name in ~/.config/conch/skills/ wins.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


SKILL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
# Bound the block injected into context so a skill can't swamp small models.
SKILL_BODY_MAX_CHARS = 8000
SKILLS_CONTEXT_MAX = 12  # skills listed in the system prompt
# Per-skill line budget in the system-prompt block: the first sentence
# plus the "Use when …" trigger sentence, so natural-language requests
# route to the right skill without paying for the whole description.
SKILLS_CONTEXT_DESC_MAX = 260
# The one-liner /skills and the introspect report show.
SKILL_ONE_LINER_MAX = 140

# Tools a shipped skill may declare that belong to an optional product
# module or a config gate — the message a user sees when a skill needs
# one that this install/session does not have.
SKILL_TOOL_HINTS = {
    "capitol_control": (
        "the Capitol runtime tool: `/install works`, then set "
        "capitol_base_url (+ capitol_org, capitol_agent) in the conch "
        "config and restart conch"
    ),
    "fleet_delegate": "the fleet task plane: `/install fleet`",
    "personal_items": "personal items: enable personal_items in the conch config",
}


def skill_one_liner(skill: Dict[str, Any],
                    limit: int = SKILL_ONE_LINER_MAX) -> str:
    """The first sentence of a skill's description, bounded.

    Trigger-rich frontmatter is for skill *selection*; listings and the
    bounded introspect report want one scannable line.
    """
    description = (skill.get("description") or "").strip()
    if not description:
        return "(no description)"
    first = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0].strip()
    return _clip(first, limit)


def skill_context_line(skill: Dict[str, Any],
                       limit: int = SKILLS_CONTEXT_DESC_MAX) -> str:
    """First sentence plus the "Use when …" trigger sentence, bounded."""
    description = (skill.get("description") or "").strip()
    if not description:
        return "(no description)"
    sentences = re.split(r"(?<=[.!?])\s+", description)
    summary = sentences[0].strip()
    trigger = next(
        (s.strip() for s in sentences[1:] if s.lower().startswith("use when")),
        "",
    )
    if trigger and trigger not in summary:
        summary = f"{summary} {trigger}"
    return _clip(summary, limit)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(",;:—- ") + "…"


def missing_skill_tools(skill: Dict[str, Any], available) -> List[str]:
    """Tools the skill declares that *available* (a tool_map or a set of
    tool names) does not provide. ``available=None`` means availability is
    unknown (nothing is reported missing). A skill without a ``tools``
    line declares nothing."""
    if available is None:
        return []
    required = skill.get("tools") or []
    names = set(available.keys() if hasattr(available, "keys") else available)
    return [tool for tool in required if tool not in names]


def describe_missing_skill_tools(skill_name: str, missing: List[str]) -> str:
    """The user-facing explanation when a skill's tools are unavailable."""
    lines = [
        f"Skill '{skill_name}' needs tools this session does not have: "
        + ", ".join(missing)
        + "."
    ]
    for tool in missing:
        hint = SKILL_TOOL_HINTS.get(
            tool, "not loaded here — /tools lists what this session has"
        )
        lines.append(f"  {tool} — {hint}")
    return "\n".join(lines)


def skills_dir() -> Path:
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch"
    return config_dir / "skills"


def builtin_skills_dir() -> Path:
    """Skills shipped with the package: one directory per skill."""
    return Path(__file__).resolve().parent / "skills_data"


def parse_skill(text: str, default_name: str) -> Optional[Dict[str, Any]]:
    """Parse a skill file: ``---`` frontmatter (key: value lines) + body.

    Files without frontmatter are accepted as body-only skills. Returns None
    for files with an empty body.
    """
    text = (text or "").strip()
    meta: Dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                meta[key.strip().lower()] = value.strip().strip("'\"")
            body = parts[2].strip()
    if not body:
        return None

    name = (meta.get("name") or default_name).strip().lower()
    if not SKILL_NAME_RE.fullmatch(name):
        return None

    tools: Optional[List[str]] = None
    raw_tools = meta.get("tools", "").strip()
    if raw_tools and raw_tools.lower() not in ("all", "*"):
        tools = [t.strip() for t in raw_tools.split(",") if t.strip()]

    rounds = 0
    try:
        rounds = int(meta.get("rounds", 0) or 0)
    except (TypeError, ValueError):
        rounds = 0

    return {
        "name": name,
        "description": meta.get("description", "").strip(),
        "tools": tools,  # None = all tools allowed
        "model": meta.get("model", "").strip(),
        "provider": meta.get("provider", "").strip().lower(),
        "rounds": rounds,
        "body": body,
    }


def _load_skill_file(path: Path, default_name: str) -> Optional[Dict[str, Any]]:
    try:
        text = path.read_text()
    except OSError:
        return None
    skill = parse_skill(text, default_name)
    if skill is None:
        return None
    skill["path"] = str(path)
    return skill


def load_skills() -> Dict[str, Dict[str, Any]]:
    """Return {name: skill} — built-in package skills
    (``skills_data/<name>/SKILL.md``) first, then every *.md file in the
    user skills dir; a user skill wins by name (the pack-registry rule).
    """
    skills: Dict[str, Dict[str, Any]] = {}
    roots = [builtin_skills_dir()]
    try:
        from .plugins import (
            load_builtin_plugins,
            skill_directories,
        )

        load_builtin_plugins()
        roots.extend(skill_directories())
    except ImportError:
        pass
    seen_roots = set()
    for builtin_root in roots:
        try:
            resolved = str(Path(builtin_root).resolve())
        except OSError:
            resolved = str(builtin_root)
        if resolved in seen_roots:
            continue
        seen_roots.add(resolved)
        if not Path(builtin_root).is_dir():
            continue
        for entry in sorted(Path(builtin_root).iterdir()):
            manifest = entry / "SKILL.md"
            if not entry.is_dir() or not manifest.is_file():
                continue
            skill = _load_skill_file(manifest, entry.name.strip().lower())
            if skill is None:
                continue
            skill["dir"] = str(entry)
            skill["builtin"] = True
            skills[skill["name"]] = skill
    directory = skills_dir()
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            skill = _load_skill_file(path, path.stem.strip().lower())
            if skill is None:
                continue
            skills[skill["name"]] = skill
    return skills


def get_skill(name: str) -> Optional[Dict[str, Any]]:
    return load_skills().get((name or "").strip().lower())


def render_skill(skill: Dict[str, Any]) -> str:
    """The block injected into context when a skill is used."""
    body = skill["body"]
    if len(body) > SKILL_BODY_MAX_CHARS:
        body = body[:SKILL_BODY_MAX_CHARS] + "\n... [skill truncated]"
    header = f"[Skill: {skill['name']}]"
    if skill.get("description"):
        header += f" {skill['description']}"
    rendered = f"{header}\n{body}"
    directory = skill.get("dir")
    if directory:
        companions = sorted(
            path.name
            for path in Path(directory).glob("*.md")
            if path.name != "SKILL.md"
        )
        if companions:
            rendered += (
                f"\n\n[Skill files: {directory}/ — read "
                + ", ".join(companions)
                + " there when this skill points at them]"
            )
        assets = skill_asset_dirs(directory)
        if assets:
            rendered += (
                "\n[Skill assets: "
                + ", ".join(
                    f"{directory}/{name}/ ({count} files)"
                    for name, count in assets
                )
                + " — copy these verbatim when the skill says to scaffold "
                "from them; do not retype them]"
            )
    return rendered


def skill_asset_dirs(directory) -> List[tuple]:
    """``[(subdir_name, file_count), …]`` for the non-markdown asset trees
    shipped beside a SKILL.md (e.g. ``templates/``), sorted by name."""
    result = []
    try:
        entries = sorted(Path(directory).iterdir())
    except OSError:
        return result
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith((".", "__")):
            continue
        count = sum(
            1 for path in entry.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
        if count:
            result.append((entry.name, count))
    return result


def format_skill_file(
    name: str,
    description: str,
    body: str,
    tools: Optional[List[str]] = None,
    model: str = "",
    rounds: int = 0,
) -> str:
    lines = ["---", f"name: {name}"]
    if description:
        lines.append(f"description: {description}")
    if tools:
        lines.append(f"tools: {', '.join(tools)}")
    if model:
        lines.append(f"model: {model}")
    if rounds:
        lines.append(f"rounds: {rounds}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


def save_skill(
    name: str,
    description: str,
    body: str,
    tools: Optional[List[str]] = None,
    model: str = "",
    rounds: int = 0,
) -> Path:
    """Write a skill file (overwrites an existing skill of the same name)."""
    name = (name or "").strip().lower()
    if not SKILL_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid skill name: {name!r}")
    if not (body or "").strip():
        raise ValueError("skill body must not be empty")
    directory = skills_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(format_skill_file(name, description, body, tools, model, rounds))
    return path


def delete_skill(name: str) -> bool:
    name = (name or "").strip().lower()
    directory = skills_dir()
    path = directory / f"{name}.md"
    if not path.is_file():
        return False
    path.unlink()
    return True


def build_skills_context() -> str:
    """One compact system-prompt block advertising available skills."""
    skills = load_skills()
    if not skills:
        return ""
    lines = ["Available skills (invoke with the skill_manage tool, action='use'):"]
    for name, skill in list(sorted(skills.items()))[:SKILLS_CONTEXT_MAX]:
        lines.append(f"- {name}: {skill_context_line(skill)}")
    if len(skills) > SKILLS_CONTEXT_MAX:
        lines.append(f"- ... and {len(skills) - SKILLS_CONTEXT_MAX} more")
    return "\n".join(lines)
