"""``conch keys doctor`` — detect and explain install and shell weirdness
instead of failing on it.

Each check yields a :class:`Finding` (``ok`` / ``warn`` / ``fail``) with
a plain-language message and, where one exists, the exact fix. The
checks: which ``conch`` binaries are on PATH and which one you are
running; which copy the daemon's launchd agent / systemd unit runs and
whether the running daemon predates the code on disk; a checkout that
is newer than the installed copy; the env file (missing, wrong mode,
unreadable — created/fixed with confirmation); stale profile exports
(``file:line``, and whether they shadow the saved key); mcp.json mode
and inline secrets; the active provider's key being set at all.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import get_env_file_path
from . import identity as ident
from .inventory import Inventory

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Finding:
    level: str
    code: str
    message: str
    fix: str = ""
    details: List[str] = field(default_factory=list)
    #: An action doctor took (file created, mode fixed): shown even when
    #: ok-level findings are otherwise folded away.
    acted: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message,
                "fix": self.fix, "details": list(self.details)}


def _mode(path: Path) -> Optional[int]:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_binaries(identity: Dict[str, Any], *,
                   path_value: Optional[str] = None,
                   describe: Callable[[Path], Dict[str, Any]] = ident.describe_binary) -> List[Finding]:
    """Every ``conch`` on PATH, which one ``conch`` resolves to, and
    whether it is the copy running now."""
    binaries = ident.binaries_on_path("conch", path_value)
    if not binaries:
        return [Finding(WARN, "path.none",
                        "`conch` is not on PATH — keep using ./bin/conch keys … from the"
                        " checkout or python3 -m conch keys …; nothing else is affected",
                        "to put it on PATH: ./install.sh (checkout) or the website installer")]
    described = [describe(path) for path in binaries]
    mine = str(identity.get("path") or "")
    lines: List[str] = []
    running_index: Optional[int] = None
    versions = set()
    for index, info in enumerate(described):
        is_me = bool(info.get("package")) and ident.same_package(info["package"], mine)
        if is_me and running_index is None:
            running_index = index
        label = f"{info.get('shape', '?')}"
        if info.get("version"):
            label += f" {info['version']}"
            versions.add((info["version"], info.get("commit", "")))
        if info.get("commit"):
            label += f" {info['commit']}"
        if info.get("error"):
            label += f" ({info['error']})"
        marks = []
        if index == 0:
            marks.append("`conch` resolves here")
        if is_me:
            marks.append("running now")
        lines.append(
            f"{index + 1}. {info['path']}"
            + (f" → {info['resolved']}" if info.get("resolved") not in ("", info["path"]) else "")
            + f"  [{label}]" + (f"  ← {', '.join(marks)}" if marks else "")
        )
    if len(binaries) == 1:
        level = OK
        message = f"one conch on PATH: {binaries[0]}"
        if running_index is None:
            level = WARN
            message = (
                f"`conch` on PATH is {binaries[0]}, but this command is running"
                f" {identity.get('root')} ({identity.get('shape')}) — a colleague typing"
                " `conch` gets the other copy"
            )
        return [Finding(level, "path.single", message, details=lines)]
    message = f"{len(binaries)} conch binaries on PATH; `conch` runs #1"
    if running_index is None:
        message += f"; this command is running a copy that is not on PATH ({identity.get('root')})"
    elif running_index != 0:
        message += f"; you are running #{running_index + 1}"
    fix = ""
    if len(versions) > 1:
        fix = (
            "they are different versions — remove or reorder the stale ones"
            " (a `pip install -e` shim: `pip uninstall conch-shell` with that"
            " interpreter; an old uv tool: `uv tool uninstall conch-shell`), or"
            " keep the checkout first on PATH deliberately"
        )
    return [Finding(WARN if len(versions) > 1 else OK, "path.multiple", message, fix, lines)]


def check_installed_copies(identity: Dict[str, Any], *, describe=ident.describe_binary,
                           path_value: Optional[str] = None) -> List[Finding]:
    """A checkout newer than an installed copy (uv/pip) → reinstall line."""
    if not str(identity.get("shape", "")).startswith(ident.CHECKOUT):
        return []
    findings: List[Finding] = []
    for path in ident.binaries_on_path("conch", path_value):
        info = describe(path)
        if not info.get("package") or ident.same_package(info["package"], str(identity.get("path"))):
            continue
        if info.get("shape") in (ident.UV, ident.PIP, ident.PIPX):
            record = ident.install_source_record()
            installed_at = record.get("installed_at", "")
            older = False
            try:
                installed_epoch = time.mktime(time.strptime(installed_at[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
                older = bool(identity.get("committed_at")) and installed_epoch < float(identity["committed_at"])
            except (ValueError, TypeError):
                pass
            if info.get("version") != identity.get("version") or older:
                findings.append(Finding(
                    WARN, "install.stale",
                    f"installed copy {path} ({info.get('shape')} {info.get('version') or '?'}"
                    + (f", installed {installed_at[:10]}" if installed_at else "")
                    + f") is older than this checkout ({identity.get('version')}"
                    + (f" {identity.get('commit')}" if identity.get("commit") else "") + ")",
                    "reinstall it from the checkout's branch: "
                    "curl -fsSL https://conch-site-gamma.vercel.app/install | sh"
                    "   (or: uv tool install --force --reinstall /path/to/checkout)",
                ))
    return findings


def check_env_file(*, fix: bool = False, confirm: Optional[Callable[[str], bool]] = None) -> List[Finding]:
    path = Path(get_env_file_path())
    findings: List[Finding] = []
    if not path.exists():
        finding = Finding(WARN, "envfile.missing",
                          f"env file {path} does not exist — nothing saved for the daemon or"
                          " other shells to read",
                          "conch keys set <NAME> creates it (0600); `conch keys import"
                          " --all-configured` copies the keys your shell already exports")
        if fix and (confirm is None or confirm(f"Create {path} (0600) now?")):
            from ..config import ENV_FILE_HEADER, _write_private_file

            try:
                _write_private_file(path, ENV_FILE_HEADER + "\n")
                finding = Finding(OK, "envfile.created", f"created {path} (0600)", acted=True)
            except OSError as exc:
                finding = Finding(FAIL, "envfile.unwritable", f"cannot create {path}: {exc}",
                                  f"fix permissions on {path.parent} and rerun")
        findings.append(finding)
        return findings
    if not os.access(path, os.R_OK):
        return [Finding(FAIL, "envfile.unreadable", f"{path} exists but is not readable by you",
                        f"chown $(id -un) {path} && chmod 600 {path}")]
    mode = _mode(path)
    if mode is not None and mode & 0o077:
        finding = Finding(WARN, "envfile.mode", f"{path} is mode {oct(mode)} — other users can read your keys",
                          f"chmod 600 {path}")
        if fix and (confirm is None or confirm(f"chmod 600 {path} now?")):
            try:
                os.chmod(path, 0o600)
                finding = Finding(OK, "envfile.mode", f"{path} mode fixed to 0600", acted=True)
            except OSError as exc:
                finding = Finding(FAIL, "envfile.mode", f"cannot chmod {path}: {exc}", f"chmod 600 {path}")
        findings.append(finding)
    else:
        findings.append(Finding(OK, "envfile.ok", f"env file {path} present, mode 0600"))
    parent_mode = _mode(path.parent)
    if parent_mode is not None and parent_mode & 0o022:
        findings.append(Finding(WARN, "envfile.dirmode",
                                f"{path.parent} is group/world-writable ({oct(parent_mode)})",
                                f"chmod 700 {path.parent}"))
    return findings


def check_profiles(inventory: Inventory) -> List[Finding]:
    findings: List[Finding] = []
    stale: List[str] = []
    harmless: List[str] = []
    only_shell: List[str] = []
    for credential in inventory.credentials:
        for note in credential.export_notes:
            if "STALE" in note:
                stale.append(f"{credential.env_name}: {note}")
            elif "only copy" in note:
                only_shell.append(f"{credential.env_name}: {note}")
            else:
                harmless.append(f"{credential.env_name}: {note}")
    if stale:
        findings.append(Finding(WARN, "profile.stale",
                                f"{len(stale)} profile export(s) carry a value different from the env file",
                                "edit the listed file:line (delete the export) — or `conch keys set NAME`"
                                " to make the saved key authoritative where the export currently wins",
                                stale))
    if only_shell:
        findings.append(Finding(WARN, "profile.shell-only",
                                f"{len(only_shell)} key(s) exist only as shell exports — the daemon,"
                                " cron, and launchd never see them",
                                "conch keys import --all-configured   (copies them into the env file after"
                                " verifying each)", only_shell))
    if harmless:
        findings.append(Finding(OK, "profile.duplicates", f"{len(harmless)} profile export(s) duplicate the"
                                " env file with the same value", "", harmless))
    scan = inventory.shell_scan
    if scan is not None and scan.unreadable:
        findings.append(Finding(WARN, "profile.unreadable", "some profile files could not be read",
                                "", list(scan.unreadable)))
    if not findings:
        findings.append(Finding(OK, "profile.clean", "no credential exports in shell profiles or direnv"))
    return findings


def check_mcp(inventory: Inventory) -> List[Finding]:
    findings: List[Finding] = []
    path = Path(inventory.mcp_path)
    if inventory.mcp_error:
        return [Finding(FAIL, "mcp.unparseable", f"mcp.json cannot be read: {inventory.mcp_error}",
                        "fix the JSON (a backup may sit next to it as mcp.json.bak*)")]
    if not path.exists():
        return [Finding(OK, "mcp.none", "no mcp.json (no MCP servers configured)")]
    inline = [c for c in inventory.credentials if c.service == "mcp-inline"]
    if inline:
        findings.append(Finding(WARN, "mcp.inline",
                                f"{len(inline)} inline secret(s) in {path} — a rotation means editing JSON"
                                " by hand, and the file is " + (oct(_mode(path) or 0)),
                                "conch keys migrate-mcp --apply   (backs the file up, moves the values into"
                                " the env file, rewrites them as ${NAME})",
                                [f"{c.inline_location} → ${{{c.extra.get('suggested_name')}}}" for c in inline]))
    mode = _mode(path)
    if mode is not None and mode & 0o077 and inline:
        findings.append(Finding(WARN, "mcp.mode", f"{path} is mode {oct(mode)} and holds secrets", f"chmod 600 {path}"))
    missing_refs = [
        c for c in inventory.credentials
        if c.service in ("mcp-ref", "jira", "composio-mcp") and not c.present
        and any(str(x).startswith("mcp/") for x in c.consumers)
    ]
    if missing_refs:
        findings.append(Finding(FAIL, "mcp.missing-ref",
                                "mcp.json references variables that are not set anywhere",
                                "conch keys set <NAME> for each",
                                [f"{c.env_name} ← {', '.join(c.consumers)}" for c in missing_refs]))
    if not findings:
        findings.append(Finding(OK, "mcp.ok", f"{path}: credentials by reference only"))
    return findings


def check_provider(inventory: Inventory) -> List[Finding]:
    config = inventory.config
    provider = str(config.get("provider") or "").lower()
    if provider in ("ollama",) or (provider == "custom" and not str(config.get("api_key_env") or "").strip()):
        return [Finding(OK, "provider.keyless", f"active provider {provider} needs no API key")]
    active = [c for c in inventory.credentials if c.extra.get("provider") == provider and c.required]
    if not active:
        return []
    credential = active[0]
    if not credential.present:
        return [Finding(FAIL, "provider.missing",
                        f"{credential.env_name} (the active provider {provider}) is not set anywhere",
                        f"conch keys set {credential.env_name}")]
    if credential.stale:
        return [Finding(WARN, "provider.stale-duplicate",
                        f"{credential.env_name}: {credential.stale}")]
    return [Finding(OK, "provider.present", f"{credential.env_name} present ({credential.effective})")]


def check_daemon(inventory: Inventory, identity: Dict[str, Any], view: Dict[str, Any], *,
                 program: Optional[Dict[str, Any]] = None) -> List[Finding]:
    """Which copy the supervisor runs, whether the running daemon is
    older than the code on disk, and the restart line for this OS."""
    from ..config import get_bool

    findings: List[Finding] = []
    config = inventory.config
    if not get_bool(config, "edge_daemon"):
        return [Finding(OK, "daemon.disabled", "edge daemon not enabled (edge_daemon unset) — no daemon column")]
    program = ident.daemon_program() if program is None else program
    restart = ident.restart_daemon_command()
    runs = (program or {}).get("runs") or {}
    if program:
        where = runs.get("package") or " ".join(program.get("argv") or [])
        label = f"{runs.get('shape', '?')} {runs.get('version', '?')}"
        if runs.get("commit"):
            label += f" {runs['commit']}"
        same = bool(runs.get("package")) and ident.same_package(str(runs.get("package")), str(identity.get("path")))
        findings.append(Finding(
            OK if same else WARN, "daemon.program",
            f"{program.get('supervisor')} unit {program.get('unit_path')} runs {where} [{label}]"
            + ("" if same else f" — not the copy running this command ({identity.get('root')})"),
            "" if same else f"to point the daemon at this copy: conch-edge install  (then {restart})",
        ))
    state = view.get("state")
    if state == "not running":
        findings.append(Finding(WARN, "daemon.down", "edge daemon is enabled but not answering on its socket",
                                f"start it: conch-edge install   (or {restart})"))
        return findings
    if state == "protocol":
        status = view.get("status") or {}
        holder = str(status.get("holder") or "")
        pid = holder.rsplit("-", 1)[-1] if holder else ""
        started = ident.process_start_time(int(pid)) if pid.isdigit() else 0.0
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) if started else "unknown time"
        findings.append(Finding(WARN, "daemon.old-protocol",
                                f"the running daemon (pid {pid or '?'}, up since {since}) predates `conch keys`"
                                " — it cannot report which keys it sees and will not hot-reload them",
                                f"restart it so it runs the code on disk: {restart}"))
        return findings
    if state == "ok":
        daemon_identity = view.get("identity") or {}
        started = float(view.get("started_at") or 0)
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) if started else "?"
        same_commit = (not identity.get("commit")) or daemon_identity.get("commit") in ("", None, identity.get("commit"))
        code_newer = bool(identity.get("committed_at")) and started and float(identity["committed_at"]) > started
        if daemon_identity.get("commit") and identity.get("commit") and daemon_identity["commit"] != identity["commit"]:
            findings.append(Finding(WARN, "daemon.stale-code",
                                    f"daemon pid {view.get('pid')} runs commit {daemon_identity['commit']} (up since {since});"
                                    f" the code on disk is {identity.get('commit')}",
                                    f"restart it: {restart}"))
        elif code_newer and same_commit and daemon_identity.get("shape", "").startswith("checkout"):
            findings.append(Finding(WARN, "daemon.stale-code",
                                    f"daemon pid {view.get('pid')} started {since}, before the latest commit on disk",
                                    f"restart it: {restart}"))
        else:
            findings.append(Finding(OK, "daemon.current",
                                    f"daemon pid {view.get('pid')} ({daemon_identity.get('shape', '?')}"
                                    f" {view.get('version', '?')}"
                                    + (f" {daemon_identity.get('commit')}" if daemon_identity.get("commit") else "")
                                    + f", up since {since}) answers keys.status"))
        blind = [c.env_name for c in inventory.credentials
                 if c.env_name and c.required and c.present and c.daemon.startswith("missing")]
        stale = [c.env_name for c in inventory.credentials if c.env_name and c.daemon.startswith("stale")]
        if blind:
            findings.append(Finding(FAIL, "daemon.blind",
                                    f"the daemon cannot see {len(blind)} required key(s): {', '.join(blind)}",
                                    "conch keys import " + " ".join(blind) + "   (then conch keys reload)"))
        if stale:
            findings.append(Finding(WARN, "daemon.stale-keys",
                                    f"the daemon holds older values for: {', '.join(stale)}",
                                    "conch keys reload"))
        return findings
    if state == "disabled":
        return findings
    findings.append(Finding(WARN, "daemon.unknown", f"daemon state unknown: {view.get('detail', '')}"))
    return findings


def check_python(identity: Dict[str, Any]) -> List[Finding]:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if sys.version_info < (3, 9):
        return [Finding(FAIL, "python.old", f"Python {version} is too old for conch (needs 3.9+)",
                        "run with python3.9+ (e.g. python3.12 -m conch keys rotate)")]
    return [Finding(OK, "python.ok", f"Python {version} at {identity.get('python')}")]


def run_doctor(inventory: Inventory, identity: Dict[str, Any], view: Dict[str, Any], *,
               fix: bool = False, confirm: Optional[Callable[[str], bool]] = None,
               path_value: Optional[str] = None,
               describe=ident.describe_binary,
               program: Optional[Dict[str, Any]] = None) -> List[Finding]:
    findings: List[Finding] = []
    findings += check_python(identity)
    findings += check_binaries(identity, path_value=path_value, describe=describe)
    findings += check_installed_copies(identity, describe=describe, path_value=path_value)
    findings += check_env_file(fix=fix, confirm=confirm)
    findings += check_provider(inventory)
    findings += check_profiles(inventory)
    findings += check_mcp(inventory)
    findings += check_daemon(inventory, identity, view, program=program)
    return findings


def worst(findings: List[Finding]) -> str:
    levels = {OK: 0, WARN: 1, FAIL: 2}
    return max((f.level for f in findings), key=lambda level: levels.get(level, 0), default=OK)
