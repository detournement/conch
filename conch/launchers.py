"""Static, user-only launcher repair shared by both installers.

Never source a profile or execute a discovered launcher. Only literal, verified
Conch references are rewritten. Arbitrary shell programs cannot be proven safe:
ambiguous overrides are reported with file/line, never their potentially secret
contents. Credential/env files and checkout helper integrations are preserved.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set, Tuple

_START = "# >>> conch launcher (managed) >>>"
_END = "# <<< conch launcher (managed) <<<"
_PUBLIC_MARKER = "# added by the conch installer"
_ASSIGN = re.compile(r"^\s*(?:export\s+)?(CONCH_DIR|ZDOTDIR|XDG_CONFIG_HOME|BASH_ENV|ENV|PATH)=(.*)$")
_SOURCE = re.compile(r"(?:^|&&|\|\||;|\bthen\b|\bdo\b)\s*(?:source|\.)\s+(\"[^\"]*\"|'[^']*'|[^\s;]+)")
_ALIAS = re.compile(r"^\s*alias\s+conch=(.+)$")
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\}|\$([A-Za-z_][A-Za-z_0-9]*)")
_PATH_TOKEN = "__CONCH_INHERITED_PATH__"
_HELPERS = ("conch", "conch-ask", "conch-chat", "conch-keys", "conch-edge",
            "conch-controller", "conch-worker", "conch-hostctl", "conch-run-with-timeout")


@dataclass
class RepairReport:
    launcher: Path
    scanned: List[Path] = field(default_factory=list)
    binaries: List[Path] = field(default_factory=list)
    changed: List[Path] = field(default_factory=list)
    pending: List[Path] = field(default_factory=list)
    backups: List[Path] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return not self.conflicts and not self.pending


def _inside(path: Path, home: Path) -> bool:
    try:
        path.resolve().relative_to(home.resolve())
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _read(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > 262144:
            return None
        with path.open(encoding="utf-8", newline="") as stream:
            return stream.read()
    except (OSError, UnicodeError, RuntimeError):
        return None


def _verified(path: Path) -> bool:
    """Recognize our shims statically, without importing foreign packages."""
    text = _read(path)
    if text is None or not os.access(path, os.X_OK):
        return False
    return bool(re.search(r"^from conch\.(?:app|cli|keys|entrypoints) import \w+", text, re.M))


def _checkout(root: Path) -> bool:
    return ((root / "conch" / "__init__.py").is_file()
            and (root / "pyproject.toml").is_file() and _verified(root / "bin/conch"))


def _literal(raw: str, variables: Mapping[str, str]) -> Optional[str]:
    """Expand only known literal path variables; never shell evaluation."""
    raw = raw.strip()
    if not raw or any(char in raw for char in ("`", "\n", "\r")) or "$(" in raw:
        return None
    # Preserve quote semantics while replacing variables with inert tokens before
    # shlex parsing. A path value may itself contain quotes, spaces or '$'.
    tokens: Dict[str, str] = {}
    rewritten: List[str] = []
    quote = ""
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and quote != "'" and index + 1 < len(raw):
            rewritten.append(raw[index:index + 2])
            index += 2
            continue
        if char in ("'", '"'):
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        if char == "$" and quote != "'":
            match = _VARIABLE.match(raw, index)
            if match and (match[1] or match[2]) in variables:
                token = f"__CONCH_LITERAL_{len(tokens)}__"
                tokens[token] = variables[match[1] or match[2]]
                rewritten.append(token)
                index = match.end()
                continue
        rewritten.append(char)
        index += 1
    try:
        words = shlex.split("".join(rewritten), comments=True)
    except ValueError:
        return None
    if len(words) != 1:
        return None
    value = words[0]
    if "$" in value:
        return None
    for token, replacement in tokens.items():
        value = value.replace(token, replacement)
    if "$" in value or any(c in value for c in "`;|&<>*?\n\r"):
        return None
    if value == "~" or value.startswith("~/"):
        value = variables["HOME"] + value[1:]
    return value


def _credential_file(path: Path) -> bool:
    third_party = {".venv", "venv", "site-packages", "node_modules", ".oh-my-zsh", ".zinit", "plugins"}
    return (path.name in (".env", "env", "credentials", "fish_variables", "activate", "activate.fish")
            or path.suffix == ".env" or bool(third_party.intersection(path.parts)))


def _ambiguous_profile(text: str, path: Path, report: RepairReport) -> bool:
    """Refuse multiline shell data before any line-oriented interpretation."""
    fish = path.suffix == ".fish"
    for line in text.splitlines(keepends=True):
        quote = ""
        substitutions: List[Tuple[str, str]] = []
        index = 0
        while index < len(line):
            char = line[index]
            if char == "\\" and (quote != "'" or fish):
                if index + 1 < len(line) and line[index + 1] not in "\r\n":
                    index += 2
                    continue
                break  # A continued physical line is also unsafe to split.
            if quote not in ("'", "$'") and line.startswith("$(", index):
                substitutions.append((")", quote))
                quote = ""
                index += 2
                continue
            if char == "`" and quote not in ("'", "$'"):
                if substitutions and substitutions[-1][0] == "`":
                    _, quote = substitutions.pop()
                else:
                    substitutions.append(("`", quote))
                    quote = ""
                index += 1
                continue
            if not quote and substitutions and char in "()":
                if char == "(":
                    substitutions.append((")", ""))
                elif substitutions[-1][0] == ")":
                    _, quote = substitutions.pop()
                index += 1
                continue
            if char in ("'", '"'):
                if not quote:
                    quote = "$'" if char == "'" and index and line[index - 1] == "$" else char
                elif quote == char or (quote == "$'" and char == "'"):
                    quote = ""
            elif not quote:
                if char == "#" and (index == 0 or line[index - 1] in " \t;|&()"):
                    break
                if line.startswith("<<", index):
                    break  # Includes quoted delimiters, <<- and here-strings.
            if char in "\r\n" and (quote or substitutions):
                break
            index += 1
        else:
            if not quote and not substitutions:
                continue
        if quote or substitutions or line[index:index + 1] != "#":
            report.conflicts.append(
                f"{path}: multiline or ambiguous shell data; move it to a separate script "
                "and configure launcher selection manually (not inspected or rewritten)")
            return True
    return False


class _Profiles:
    def __init__(self, home: Path, env: Mapping[str, str], report: RepairReport):
        self.home, self.env, self.report = home, env, report
        self.files: Dict[Path, str] = {}
        self.variables: Dict[Path, Dict[str, str]] = {}
        self.base = {k: env[k] for k in ("CONCH_DIR", "ZDOTDIR", "XDG_CONFIG_HOME", "BASH_ENV", "ENV") if env.get(k)}
        self.base.update(HOME=str(home), PATH=_PATH_TOKEN)
        self.targets: Set[Path] = set()
        self.helpers = False
        self.activations: List[Path] = []

    def scan(self, path: Path, variables: Optional[Dict[str, str]] = None, depth: int = 0) -> Dict[str, str]:
        values = dict(variables or self.base)
        if depth > 12:
            self.report.conflicts.append(f"{path}: source nesting exceeds safe inspection limit")
            return values
        if path in self.files or not path.exists():
            return values
        text = _read(path)
        if text is None:
            self.report.conflicts.append(f"{path}: cannot inspect startup file safely")
            return values
        self.files[path] = text
        self.variables[path] = dict(values)
        self.report.scanned.append(path)
        if _ambiguous_profile(text, path, self.report):
            return values
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            assignment = _ASSIGN.match(line)
            if assignment and assignment[1] != "PATH":
                literal = _literal(assignment[2], values)
                if literal is not None:
                    values[assignment[1]] = literal
                else:
                    values.pop(assignment[1], None)
                    if assignment[1] in ("ZDOTDIR", "CONCH_DIR"):
                        self.report.conflicts.append(f"{path}:{number}: computed {assignment[1]}; set a literal path and rerun")
            # Fish's common path assignments use separate words rather than '='.
            fish_set = re.match(r"^\s*set\s+-[A-Za-z]+\s+(CONCH_DIR|XDG_CONFIG_HOME)\s+(.+)$", line)
            if fish_set:
                literal = _literal(fish_set[2], values)
                if literal is not None:
                    values[fish_set[1]] = literal
            for source in _SOURCE.finditer(line):
                literal = _literal(source[1], values)
                if literal is not None and Path(literal).is_absolute():
                    target = Path(literal)
                    if target.name in ("activate", "activate.fish") and (target.parent / "conch").is_file():
                        self.activations.append(target)
                    # The helpers are programs, not startup customization files.
                    if target.name in ("conch.zsh", "conch.bash") and _checkout(target.parent.parent):
                        self.helpers = self.helpers or "CONCH_ASK_CMD" in (_read(target) or "")
                        continue
                    values.update(self.scan(target, values, depth + 1))
                elif "CONCH" in source[1]:
                    self.report.conflicts.append(f"{path}:{number}: computed Conch source; review it and rerun")
        return values

    def discover(self) -> None:
        home = self.home
        # zsh uses inherited ZDOTDIR for .zshenv, then its updated value.
        initial_zdot = Path(self.base.get("ZDOTDIR", str(home)))
        values = self.scan(initial_zdot / ".zshenv")
        zdot = Path(values.get("ZDOTDIR", str(initial_zdot)))
        if not zdot.is_absolute():
            self.report.conflicts.append("ZDOTDIR must be an absolute literal path")
            zdot = home
        for name in (".zprofile", ".zshrc", ".zlogin"):
            current = zdot / name
            values.update(self.scan(current, values))
            if name != ".zlogin" or current.exists():
                self.targets.add(current)
            new_zdot = Path(values.get("ZDOTDIR", str(zdot)))
            if not new_zdot.is_absolute():
                self.report.conflicts.append(f"{current}: computed/relative ZDOTDIR changes startup selection")
            else:
                zdot = new_zdot
        # Bash reads only the first existing login profile, not all three.
        login = next((home / n for n in (".bash_profile", ".bash_login", ".profile") if (home / n).exists()), home / ".bash_profile")
        self.scan(login)
        self.scan(home / ".bashrc")
        self.targets.update((login, home / ".bashrc"))
        for key in ("BASH_ENV", "ENV"):
            if self.env.get(key):
                raw = '"' + self.env[key].replace("\\", "\\\\").replace('"', '\\"') + '"'
                literal = _literal(raw, self.base)
                if literal is not None and Path(literal).is_absolute():
                    self.scan(Path(literal))
                else:
                    self.report.conflicts.append(f"{key}: computed or relative startup source; use an absolute literal path")
        config = Path(self.env.get("XDG_CONFIG_HOME", str(home / ".config")))
        fish = config / "fish"
        if fish.exists() or Path(self.env.get("SHELL", "")).name == "fish":
            for entry in sorted((fish / "conf.d").glob("*.fish")):
                self.scan(entry)
            self.scan(fish / "config.fish")
            self.scan(fish / "functions/conch.fish")
            self.targets.add(fish / "config.fish")
            universal = _read(fish / "fish_variables") or ""
            if re.search(r"SETUVAR.*(?:fish_user_paths|PATH).*conch", universal, re.I):
                self.report.conflicts.append(f"{fish / 'fish_variables'}: universal fish path references Conch; review `set -U fish_user_paths` (not rewritten)")


def _without_managed(text: str, path: Path, report: RepairReport) -> str:
    lines = text.splitlines(keepends=True)
    result: List[str] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() == _START:
            end = next((j for j in range(index + 1, len(lines)) if lines[j].strip() == _END), None)
            if end is None:
                report.conflicts.append(f"{path}:{index + 1}: unterminated Conch managed block; repair the markers")
                result.extend(lines[index:])
                break
            # Only discard our narrowly defined generated PATH block.
            body = lines[index + 1:end]
            value = (_literal(body[0][len("export PATH="):], {"PATH": _PATH_TOKEN, "HOME": ""})
                     if len(body) in (1, 2) and body[0].startswith("export PATH=") else None)
            shell_path = bool(value and value.endswith(":" + _PATH_TOKEN)
                              and len(value.split(":")) == 2 and Path(value.split(":")[0]).is_absolute())
            if len(body) == 2:
                helper = (_literal(body[1][len("export CONCH_ASK_CMD="):], {"HOME": ""})
                          if body[1].startswith("export CONCH_ASK_CMD=") else None)
                shell_path = bool(shell_path and helper and Path(helper).is_absolute()
                                  and Path(helper).name == "conch-ask")
            fish = (re.fullmatch(r"set -gx PATH '((?:\\.|[^'])*)' \$PATH\s*", body[0])
                    if len(body) == 1 else None)
            fish_path = bool(fish and fish[1].startswith("/"))
            if not (shell_path or fish_path):
                report.conflicts.append(f"{path}:{index + 1}: customized Conch managed block; retained for manual review")
                result.extend(lines[index:end + 1])
            elif any(line.strip() for line in lines[end + 1:]):
                # Embedded blocks may be the sole statement in a shell body.
                # The generated trailing block needs no placeholder (or each
                # repair would accumulate another no-op).
                result.append("true\n" if path.suffix == ".fish" else ":\n")
            index = end + 1
            continue
        if lines[index].strip() == _PUBLIC_MARKER and index + 1 < len(lines):
            if re.fullmatch(r'export PATH="/[^":$`\\\n]*:\$PATH"\s*', lines[index + 1]):
                result.append("true\n" if path.suffix == ".fish" else ":\n")
                index += 2
                continue
        result.append(lines[index])
        index += 1
    return "".join(result)


def _block(directory: Path, fish: bool, ask: Optional[Path] = None) -> str:
    quoted = shlex.quote(str(directory))
    if fish:
        # fish single quotes escape apostrophes/backslashes differently from sh.
        quoted = "'" + str(directory).replace("\\", "\\\\").replace("'", "\\'") + "'"
        command = f"set -gx PATH {quoted} $PATH"
    else:
        command = f"export PATH={quoted}:\"$PATH\""
        if ask is not None:
            command += "\nexport CONCH_ASK_CMD=" + shlex.quote(str(ask))
    return f"{_START}\n{command}\n{_END}\n"


def _rewrite(path: Path, original: str, values: Dict[str, str], directory: Path,
             target: bool, home: Path, report: RepairReport, ask: Optional[Path] = None) -> str:
    if _ambiguous_profile(original, path, report):
        return original
    text = _without_managed(original, path, report)
    lines = text.splitlines(keepends=True)
    result: List[str] = []
    protected = _credential_file(path)
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            result.append(line)
            continue
        if re.match(r"^(?:return|exit)(?:\s+\d+)?\s*(?:#.*)?$", line):
            report.conflicts.append(f"{path}:{number}: startup may stop before launcher selection; review the early return/exit")
        if re.match(r"^\s*(?:unsetopt\s+(?:RCS|rcs)|setopt\s+(?:NO_RCS|no_rcs))\s*(?:#.*)?$", line):
            report.conflicts.append(f"{path}:{number}: zsh startup files are disabled; enable RCS or invoke the selected launcher explicitly")
        assignment = _ASSIGN.match(line)
        if assignment and assignment[1] != "PATH":
            literal = _literal(assignment[2], values)
            if literal is not None:
                values[assignment[1]] = literal
        alias = _ALIAS.match(line)
        if alias:
            try:
                words = shlex.split(alias[1], comments=True)
                command = shlex.split(words[0]) if len(words) == 1 else []
            except ValueError:
                command = []
            old = (_VARIABLE.sub(lambda m: values.get(m[1] or m[2], m[0]), command[0])
                   if len(command) == 1 else None)
            if old and old.startswith("~/"):
                old = str(home) + old[1:]
            if old and _verified(Path(old)):
                if protected:
                    report.conflicts.append(f"{path}:{number}: Conch alias in protected env file; move it to a startup profile")
                else:
                    # Keep conditional/function bodies valid while removing
                    # only an alias which executes exactly Conch.
                    result.append("true\n" if path.suffix == ".fish" else ":\n")
                    continue
            else:
                report.conflicts.append(f"{path}:{number}: ambiguous conch alias; remove or update it to the selected launcher")
        if assignment and assignment[1] == "PATH":
            expanded = _literal(assignment[2], values)
            if expanded is not None:
                parts = expanded.split(":")
                kept = []
                removed = False
                for part in parts:
                    p = Path(part)
                    # The legacy source installer promoted an entire private venv.
                    root = p.parent.parent if p.name == "bin" and p.parent.name == ".venv" else None
                    if root is not None and _checkout(root):
                        removed = True
                    else:
                        kept.append(part)
                if removed:
                    if protected:
                        report.conflicts.append(f"{path}:{number}: legacy Conch PATH in protected env file; edit its PATH assignment")
                    else:
                        entries = [('\"$PATH\"' if p == _PATH_TOKEN else shlex.quote(p)) for p in kept]
                        line = "export PATH=" + ":".join(entries) + "\n"
            elif "CONCH_DIR" in assignment[2] or re.search(r"(?:^|[/\s])conch(?:[/\s]|$)", assignment[2]):
                report.conflicts.append(f"{path}:{number}: computed Conch PATH; simplify it to literal entries and rerun")
        if re.search(r"(?:^|[;\s])(?:function\s+conch(?=[\s(;]|$)|conch\s*\(\s*\))", stripped):
            report.conflicts.append(f"{path}:{number}: conch shell function overrides PATH; remove or update it")
        if path.suffix == ".fish" and re.match(r"alias\s+conch\s+", stripped):
            try:
                words = shlex.split(stripped)
                command = shlex.split(words[2]) if len(words) == 3 else []
            except ValueError:
                command = []
            if len(command) == 1 and _verified(Path(command[0])) and not protected:
                result.append("true\n")
                continue
            report.conflicts.append(f"{path}:{number}: fish conch alias overrides PATH; remove or update it")
        if path.suffix == ".fish" and re.match(r"function\s+conch(?=[\s;]|$)", stripped):
            report.conflicts.append(f"{path}:{number}: fish conch function overrides PATH; remove or update it")
        if ("alias" in stripped and re.search(r"(?:^|[\s;])conch=", stripped) and not alias):
            report.conflicts.append(f"{path}:{number}: compound conch alias; review it manually")
        result.append(line)
    rewritten = "".join(result)
    if protected:
        if rewritten != original:
            report.conflicts.append(f"{path}: Conch references in protected integration; update them manually")
        return original
    if target:
        rewritten = rewritten.rstrip("\n") + "\n\n" + _block(directory, path.suffix == ".fish", ask)
    return rewritten


def _save(path: Path, text: str, original: Optional[str], home: Path, report: RepairReport) -> None:
    if not _inside(path, home):
        report.conflicts.append(f"{path}: startup path resolves outside your home; not modified")
        return
    target = path.resolve()
    if target.exists() and target.stat().st_uid != os.getuid():
        report.conflicts.append(f"{path}: startup file is not owned by you; not modified")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if original is not None:
        # Refuse to race an editor or another installer.
        if _read(target) != original:
            report.conflicts.append(f"{path}: changed during inspection; rerun repair")
            return
        backup = target.with_name(target.name + f".conch-backup-{time.time_ns()}")
        fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(original)
        report.backups.append(backup)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=".conch-startup-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.chmod(name, mode)
        os.replace(name, target)
        report.changed.append(path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def _forward_helpers(launcher: Path, home: Path, report: RepairReport, check: bool) -> Optional[Path]:
    """Keep legacy shell widgets, but dispatch their requests to this installation.

    zsh invokes the timeout/chat scripts with system python3, so these adapters
    use only stdlib and exec the selected entry points with their own interpreter.
    """
    root = launcher.resolve().parent.parent
    if _checkout(root) and os.access(root / ".venv/bin/python", os.X_OK):
        ask_command = [str(root / ".venv/bin/python"), str(root / "bin/conch-ask")]
    elif _verified(launcher.parent / "conch-ask"):
        ask_command = [str(launcher.parent / "conch-ask")]
    else:
        report.conflicts.append("selected installation is missing conch-ask; rerun its installer to preserve shell helpers")
        return None
    directory = home / ".local/share/conch/shell-helpers"
    header = "#!/usr/bin/env python3\n# Conch managed helper forwarding\n"
    exec_script = ("import os, sys\n"
                   "command = {command!r} + sys.argv[1:]\n"
                   "os.execv(command[0], command)\n")
    scripts = {
        "conch-ask": header + exec_script.format(command=ask_command),
        "conch-chat": header + exec_script.format(command=[str(launcher)]),
        "conch-run-with-timeout": header + (
            "import subprocess, sys\n"
            "if len(sys.argv) < 3:\n"
            "    sys.exit('usage: conch-run-with-timeout SECONDS PROMPT')\n"
            f"command = {ask_command!r} + [' '.join(sys.argv[2:])]\n"
            "try:\n"
            "    result = subprocess.run(command, timeout=int(sys.argv[1]))\n"
            "except subprocess.TimeoutExpired:\n"
            "    sys.exit('conch-ask timed out')\n"
            "sys.exit(result.returncode)\n"
        ),
    }
    for name, content in scripts.items():
        path = directory / name
        original = _read(path)
        if not _inside(path, home):
            report.conflicts.append(f"{path}: helper path resolves outside your home; not modified")
            continue
        if original is None and (path.exists() or path.is_symlink()):
            report.conflicts.append(f"{path}: existing helper could not be inspected; not modified")
            continue
        if original is not None and not original.startswith(header):
            report.conflicts.append(f"{path}: unrecognized existing helper; not modified")
            continue
        if original == content and os.access(path, os.X_OK):
            continue
        if check:
            report.pending.append(path)
        else:
            try:
                _save(path, content, original, home, report)
                if path in report.changed:
                    path.chmod(0o700)
            except OSError:
                report.conflicts.append(f"{path}: unable to update helper forwarding; check permissions")
    return directory / "conch-ask"



def repair_launcher(launcher: Path, *, check: bool = False, home: Optional[Path] = None,
                    environ: Optional[Mapping[str, str]] = None) -> RepairReport:
    """Prefer a selected public launcher in fresh user shells; inspect only on check.

    Existing source integrations and their env files remain in place. No binaries,
    checkouts, venvs, or credential files are replaced. ``pending`` is check-only;
    ``conflicts`` requires attention even when unambiguous repairs were applied.
    """
    env = dict(os.environ if environ is None else environ)
    home = Path(home or env.get("HOME", str(Path.home()))).absolute()
    launcher = Path(launcher)
    report = RepairReport(launcher)
    if launcher.name != "conch" or not launcher.is_absolute() or not _verified(launcher):
        report.conflicts.append("selected launcher must be an absolute, executable Conch entry point")
        return report
    if any(char in str(launcher) for char in ("\n", "\r", ":")):
        report.conflicts.append("selected launcher path contains an unsupported newline or PATH separator")
        return report
    directory = launcher.parent
    source_root = launcher.resolve().parent.parent
    source_venv = (source_root / ".venv/bin"
                   if _checkout(source_root) and (source_root / ".venv/pyvenv.cfg").is_file()
                   else None)
    if (directory.parent / "pyvenv.cfg").is_file() or source_venv is not None:
        # Expose only Conch tools, preferring source wrappers to venv entrypoints.
        wrappers = source_root / "bin" if source_venv is not None else launcher.parent
        directory = home / ".local/share/conch/launchers"
        for name in _HELPERS:
            source = launcher if name == "conch" else wrappers / name
            if (name != "conch" and source_venv is not None
                    and not (source.is_file() and os.access(source, os.X_OK))):
                source = source_venv / name
            if not source.is_file() or not os.access(source, os.X_OK):
                continue
            destination = directory / name
            if destination.is_symlink() and destination.resolve() == source.resolve():
                continue
            if destination.exists() or destination.is_symlink():
                report.conflicts.append(f"{destination}: existing launcher shim is not the selected tool; not replaced")
            elif check:
                report.pending.append(destination)
            elif _inside(directory, home):
                directory.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(source)
                report.changed.append(destination)
            else:
                report.conflicts.append(f"{directory}: launcher shim directory resolves outside your home")
    profiles = _Profiles(home, env, report)
    profiles.discover()
    def same_installation(candidate: Path) -> bool:
        if candidate.resolve() == launcher.resolve():
            return True
        root = launcher.resolve().parent.parent
        return _checkout(root) and candidate.parent.resolve() == (root / ".venv/bin").resolve()

    for activation in profiles.activations:
        if not same_installation(activation.parent / "conch"):
            report.conflicts.append(f"{activation}: startup activates another Conch environment; stop auto-activating it or deliberately use that environment (not rewritten)")
    ask = _forward_helpers(launcher, home, report, check) if profiles.helpers else None
    for path in sorted(set(profiles.files) | profiles.targets, key=str):
        original = profiles.files.get(path)
        if path in profiles.targets and not _inside(path, home):
            report.conflicts.append(f"{path}: startup path resolves outside your home; not modified")
            continue
        if original is None and path.exists():
            continue  # unreadable or outside-home file was deliberately not loaded
        rewritten = _rewrite(path, original or "", dict(profiles.variables.get(path, profiles.base)),
                             directory, path in profiles.targets, home, report, ask)
        if rewritten == (original or ""):
            continue
        if not _inside(path, home):
            report.conflicts.append(f"{path}: startup path resolves outside your home; not modified")
        elif check:
            report.pending.append(path)
        else:
            try:
                _save(path, rewritten, original, home, report)
            except OSError:
                report.conflicts.append(f"{path}: unable to back up or update startup file; check permissions")
    seen: Set[str] = set()
    for entry in env.get("PATH", "").split(os.pathsep):
        candidate = Path(entry or os.curdir).absolute() / "conch"
        if candidate.is_file() and os.access(candidate, os.X_OK) and str(candidate) not in seen:
            seen.add(str(candidate))
            report.binaries.append(candidate)
    if env.get("CONCH_DIR"):
        candidate = Path(env["CONCH_DIR"]) / "bin/conch"
        if _verified(candidate) and candidate not in report.binaries:
            report.binaries.append(candidate)
    for name in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        if env.get(name):
            candidate = Path(env[name]) / "bin/conch"
            if candidate.is_file() and not same_installation(candidate):
                report.conflicts.append(f"{name} has another conch; deactivate that environment and open a new terminal")
    if report.binaries and report.binaries[0].resolve() != launcher.resolve():
        report.notes.append("current process PATH still selects another conch; open a new terminal after repair (existing aliases/functions/hashes cannot be changed by a child process)")
    report.conflicts = list(dict.fromkeys(report.conflicts))
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Repair user startup launcher selection without executing shell profiles")
    parser.add_argument("--launcher", required=True, type=Path, help="absolute public Conch launcher (not its symlink target)")
    parser.add_argument("--check", action="store_true", help="inspect only; do not write startup files, backups or shims")
    args = parser.parse_args(argv)
    report = repair_launcher(args.launcher, check=args.check)
    print(f"Selected Conch launcher: {report.launcher}")
    for path in report.binaries:
        print(f"Discovered executable: {path}")
    for path in report.changed:
        print(f"Updated: {path}")
    for path in report.backups:
        print(f"Private backup: {path}")
    for path in report.pending:
        print(f"Repair needed: {path}")
    for note in report.notes:
        print(f"Note: {note}")
    for conflict in report.conflicts:
        print(f"Unresolved: {conflict}")
    if report.usable:
        print("Fresh-shell startup selection configured. Custom computed shell code is not executed or proven; open a new terminal.")
    elif args.check and report.pending:
        print("Rerun without --check to apply safe startup repairs.")
    return 0 if report.usable else 1


if __name__ == "__main__":
    sys.exit(main())
