"""Skill system (plan 4.1): reusable skill definitions in
~/.config/conch/skills/.

One markdown file per skill: frontmatter (name, description, allowed tools,
optional model/provider preference, optional round budget) plus a body of
instructions/procedure. Skills are the richer sibling of custom slash
commands: a slash command is a one-shot prompt template; a skill also scopes
*tools* and *model*, can be invoked by the model itself (skill_manage tool,
in-context), and drives skill-scoped subagents (plan 4.2).
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


def skills_dir() -> Path:
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch"
    return config_dir / "skills"


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


def load_skills() -> Dict[str, Dict[str, Any]]:
    """Return {name: skill} for every *.md file in the skills dir."""
    skills: Dict[str, Dict[str, Any]] = {}
    directory = skills_dir()
    if not directory.is_dir():
        return skills
    for path in sorted(directory.glob("*.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        skill = parse_skill(text, path.stem.strip().lower())
        if skill is None:
            continue
        skill["path"] = str(path)
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
    return f"{header}\n{body}"


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
        desc = skill.get("description") or "(no description)"
        lines.append(f"- {name}: {desc[:100]}")
    if len(skills) > SKILLS_CONTEXT_MAX:
        lines.append(f"- ... and {len(skills) - SKILLS_CONTEXT_MAX} more")
    return "\n".join(lines)
