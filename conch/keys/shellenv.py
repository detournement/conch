"""Where does the shell get a credential from? (profile export scan)

After a rotation the classic trap is an ``export ANTHROPIC_API_KEY=…``
still sitting in ``~/.zshenv`` (or a file it sources) that shadows the
fresh key — or, under conch's precedence rule, is silently overridden
and confuses the next person who reads the profile. ``conch keys``
therefore scans the usual startup files, follows one level of
``source``/``.`` includes (expanding ``~``, ``$HOME``, and variables the
same file exported literally, e.g. ``${CONCH_DIR}``), looks at direnv
``.envrc`` files from the working directory up, and reports every
``file:line`` that assigns a credential NAME — with a fingerprint when
the value is a literal, so the report can say *stale* or *same* without
ever showing the value.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..config import key_fingerprint

PROFILE_FILES = (
    "~/.zshenv", "~/.zprofile", "~/.zshrc", "~/.zlogin",
    "~/.bash_profile", "~/.bashrc", "~/.profile", "~/.bash_login",
    "~/.config/fish/config.fish",
)
FISH_CONF_D = "~/.config/fish/conf.d"

_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_FISH_EXPORT_RE = re.compile(
    r"^\s*set\s+((?:-[A-Za-z]+\s+)+)([A-Za-z_][A-Za-z0-9_]*)\s+(.*)$"
)
_SOURCE_RE = re.compile(r"""(?:^|&&|\|\||;)\s*(?:source|\.)\s+(["']?)([^\s"']+)\1""")
_DIRENV_EXPORT_RE = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


@dataclass
class ExportSite:
    file: str
    line: int
    name: str
    literal: bool
    fingerprint: str = ""
    via: str = ""  # the profile that sourced this file, when any

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass
class ShellEnvScan:
    sites: Dict[str, List[ExportSite]] = field(default_factory=dict)
    scanned: List[str] = field(default_factory=list)
    unreadable: List[str] = field(default_factory=list)

    def for_name(self, name: str) -> List[ExportSite]:
        return list(self.sites.get(name, []))


def _assignment(line: str, is_fish: bool):
    """``(NAME, raw_value)`` when *line* assigns/exports a variable."""
    match = _ASSIGN_RE.match(line)
    if match:
        return match.group(1), match.group(2)
    if is_fish:
        fish = _FISH_EXPORT_RE.match(line)
        if fish and "x" in fish.group(1).replace("-", ""):
            return fish.group(2), fish.group(3)
    return None


_HOME_ONLY_RE = re.compile(r"\$\{?HOME\}?")


def _strip_value(raw: str, home: Optional[Path] = None) -> Optional[str]:
    """The literal value of an assignment RHS, or None when it is
    computed (``$VAR``, ``$(cmd)``, backticks). ``$HOME``/``~`` are the
    one expansion performed, so ``export CONCH_DIR="$HOME/conch"`` still
    lets a later ``source "${CONCH_DIR}/.env"`` be followed."""
    value = raw.strip()
    # drop a trailing comment on an unquoted value
    if value and value[0] not in "\"'":
        value = value.split(" #", 1)[0].strip()
    quote = ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        quote = value[0]
        value = value[1:-1]
    if quote != "'" and home is not None:
        value = _HOME_ONLY_RE.sub(str(home), value)
        if value.startswith("~/") or value == "~":
            value = str(home) + value[1:]
    if quote != "'" and re.search(r"\$|`", value):
        return None
    return value


def _expand_path(raw: str, literals: Dict[str, str], home: Path) -> Optional[Path]:
    text = raw.strip()
    text = text.replace("~", str(home), 1) if text.startswith("~") else text

    def sub(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        if name == "HOME":
            return str(home)
        return literals.get(name, match.group(0))

    text = re.sub(r"\$\{(\w+)\}|\$(\w+)", sub, text)
    if "$" in text:
        return None  # still computed: cannot follow safely
    return Path(text)


def _scan_file(path: Path, names: Iterable[str], scan: ShellEnvScan,
               home: Path, *, via: str = "", follow: bool = True) -> None:
    wanted = set(names)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        scan.unreadable.append(str(path))
        return
    display = str(path)
    if display in scan.scanned:
        return
    scan.scanned.append(display)
    literals: Dict[str, str] = {}
    includes: List[Path] = []
    for number, line in enumerate(lines, start=1):
        if line.strip().startswith("#"):
            continue
        assignment = _assignment(line, path.suffix == ".fish")
        if assignment:
            name, raw = assignment
            literal = _strip_value(raw, home if name not in wanted else None)
            if literal is not None and name not in wanted:
                literals[name] = literal
            if name in wanted:
                site = ExportSite(
                    display, number, name, literal is not None,
                    key_fingerprint(literal) if literal else "", via,
                )
                scan.sites.setdefault(name, []).append(site)
            continue
        if follow:
            src = _SOURCE_RE.search(line)
            if src:
                target = _expand_path(src.group(2), literals, home)
                if target is not None:
                    includes.append(target)
    for target in includes:
        if target.is_file():
            _scan_file(target, wanted, scan, home, via=display, follow=False)


def scan_shell_exports(names: Iterable[str], *, home: Optional[Path] = None,
                       cwd: Optional[Path] = None,
                       profiles: Iterable[str] = PROFILE_FILES) -> ShellEnvScan:
    """Every profile/direnv assignment of any NAME in *names*."""
    home = Path(home or Path.home())
    names = [str(name) for name in names if name]
    scan = ShellEnvScan()
    if not names:
        return scan
    for entry in profiles:
        path = Path(str(entry).replace("~", str(home), 1))
        if path.is_file():
            _scan_file(path, names, scan, home)
    conf_d = Path(FISH_CONF_D.replace("~", str(home), 1))
    if conf_d.is_dir():
        for path in sorted(conf_d.glob("*.fish")):
            _scan_file(path, names, scan, home)
    current = Path(cwd or os.getcwd()).resolve()
    seen = set()
    while True:
        envrc = current / ".envrc"
        if envrc.is_file() and str(envrc) not in seen:
            seen.add(str(envrc))
            _scan_file(envrc, names, scan, home, via="direnv", follow=False)
        if current.parent == current:
            break
        current = current.parent
    return scan


def describe_site(site: ExportSite, file_fingerprint: str, managed: bool) -> str:
    """One line explaining what an export means under the precedence rule
    relative to the value saved in the env file (fingerprint only)."""
    where = site.location + (f" (sourced from {site.via})" if site.via and site.via != "direnv" else "")
    if site.via == "direnv":
        where += " (direnv)"
    if not site.literal:
        return f"{where}: computed value — cannot compare; check it by hand"
    if not file_fingerprint or file_fingerprint == "-":
        return f"{where}: exported here (not in the env file) — this is the shell's only copy"
    if site.fingerprint == file_fingerprint:
        return f"{where}: same value as the env file (harmless duplicate)"
    if managed:
        return (
            f"{where}: STALE — different from the saved key; conch overrides"
            " it (saved key is conch-managed), but remove the line to avoid"
            " confusing other tools"
        )
    return (
        f"{where}: STALE — different from the env file and it WINS over it"
        " (the env-file entry is not conch-managed): delete the line, or run"
        f" `conch keys set {site.name}` to make the saved key authoritative"
    )
