"""The ``conch keys`` command line (and the ``/keys`` slash form).

Every invocation starts with the identity line (which conch, which
commit, which install shape), captures the *inherited* process
environment before conch loads its env file (so stale-duplicate and
daemon-blind detection see what the shell really exported), then runs
one subcommand. No subcommand ever prints a credential value.
"""

from __future__ import annotations

import argparse
import difflib
import getpass
import json
import os
import shutil
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import (
    get_env_file_path,
    key_fingerprint,
    load_config,
    parse_env_lines,
    reload_env_file,
    remove_env_values,
    set_env_values,
)
from . import identity as ident
from .inventory import (
    ERROR,
    MISSING,
    OK,
    REJECTED,
    SKIPPED,
    UNREACHABLE,
    Credential,
    Inventory,
    build_inventory,
)
from .verify import (
    DEFAULT_TIMEOUT,
    annotate_daemon_column,
    attach_verifiers,
    daemon_view,
    format_problem,
    format_warning,
    run_checks,
)

SUBCOMMANDS = ("list", "check", "set", "unset", "import", "rotate", "migrate-mcp",
               "doctor", "required", "reload")

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_USAGE = 2


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

class Out:
    def __init__(self, stream=None, color: Optional[bool] = None, json_mode: bool = False):
        self.stream = stream or sys.stdout
        if color is None:
            color = bool(getattr(self.stream, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
        self.color = color
        self.json_mode = json_mode

    def c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def say(self, line: str = "") -> None:
        if not self.json_mode:
            print(line, file=self.stream, flush=True)

    def status(self, status: str) -> str:
        palette = {OK: "1;32", REJECTED: "1;31", MISSING: "31", UNREACHABLE: "33",
                   SKIPPED: "2", ERROR: "31", "stale": "35"}
        return self.c(palette.get(status, ""), status)


def _width() -> int:
    try:
        return max(80, shutil.get_terminal_size(fallback=(120, 24)).columns)
    except Exception:
        return 120


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def render_table(out: Out, credentials: List[Credential], *, daemon: bool = True,
                 checked: bool = True) -> None:
    width = _width()
    headers = ["NAME", "consumer", "source", "fingerprint", "verified"]
    if checked:
        headers.append("shell")
    if daemon:
        headers.append("daemon")
    rows: List[List[str]] = []
    for c in credentials:
        source = c.effective or "?"
        if c.stale:
            source += " ⚠"
        fp = c.effective_fp or "-"
        verified = c.last_verified or "-"
        row = [c.name, "; ".join(c.consumers) or "-", source, fp, verified]
        if checked:
            row.append(c.status or "-")
        if daemon:
            row.append(c.daemon or "-")
        rows.append(row)
    # column widths: the fixed columns are capped, the consumer column
    # takes what is left of the terminal width (at least 20).
    caps = {"NAME": 32, "source": 30, "fingerprint": 15, "verified": 22, "shell": 11, "daemon": 30}
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    consumer_index = headers.index("consumer")
    for i, header in enumerate(headers):
        if header in caps:
            widths[i] = min(widths[i], caps[header])
    other = sum(widths[i] for i in range(len(headers)) if i != consumer_index) + 2 * (len(headers) - 1)
    widths[consumer_index] = max(20, min(widths[consumer_index], width - other))

    def fmt(row: List[str], header: bool = False) -> str:
        cells = []
        for i, cell in enumerate(row):
            text = _clip(cell, widths[i]).ljust(widths[i])
            if not header and headers[i] in ("shell",):
                text = out.status(cell.strip()) + " " * (widths[i] - len(_clip(cell, widths[i])))
            elif not header and headers[i] == "daemon":
                code = "1;32" if cell.startswith("ok") else ("31" if cell.startswith(("missing", "rejected", "stale", "?")) else "2")
                text = out.c(code, _clip(cell, widths[i])) + " " * (widths[i] - len(_clip(cell, widths[i])))
            elif not header and headers[i] == "source" and "⚠" in cell:
                text = out.c("35", _clip(cell, widths[i])) + " " * (widths[i] - len(_clip(cell, widths[i])))
            cells.append(text)
        return "  ".join(cells).rstrip()

    out.say(out.c("1", fmt(headers, header=True)))
    for row in rows:
        out.say(fmt(row))


def render_notes(out: Out, credentials: List[Credential], *, verbose: bool) -> None:
    """Below the table: what each non-ok row means and how to fix it."""
    printed = False
    for c in credentials:
        lines: List[str] = []
        if c.status and c.status not in (OK,) and c.detail:
            lines.append(f"{out.status(c.status)}: {c.detail}")
        elif verbose and c.detail:
            lines.append(f"{out.status(c.status)}: {c.detail}")
        for label, (alt_status, alt_detail) in sorted(c.alt_results.items()):
            lines.append(f"{label} value: {out.status(alt_status)} — {alt_detail}")
        if c.stale:
            lines.append(out.c("35", "stale duplicate: ") + c.stale)
        for note in c.export_notes:
            if verbose or "STALE" in note or "only copy" in note:
                lines.append("shell profile: " + note)
        for note in c.notes:
            lines.append(note)
        if c.daemon.startswith(("missing (daemon-blind)", "stale")):
            lines.append(out.c("33", "daemon: ") + (
                f"the daemon cannot see this key — `conch keys import {c.env_name}`"
                if c.daemon.startswith("missing") else "the daemon holds an older value — `conch keys reload`"
            ))
        if lines:
            if not printed:
                out.say()
                printed = True
            out.say(out.c("1", c.name))
            for line in lines:
                out.say(f"  {line}")


def render_findings(out: Out, findings, *, verbose: bool = True) -> None:
    from .doctor import FAIL, OK as D_OK, WARN

    marks = {D_OK: out.c("1;32", "✓"), WARN: out.c("33", "!"), FAIL: out.c("1;31", "✗")}
    for finding in findings:
        if finding.level == D_OK and not verbose and not getattr(finding, "acted", False):
            continue
        out.say(f"  {marks.get(finding.level, '?')} {finding.message}")
        for detail in finding.details:
            out.say(f"      {detail}")
        if finding.fix:
            out.say(f"      {out.c('2', 'fix: ')}{finding.fix}")


# ---------------------------------------------------------------------------
# Shared context
# ---------------------------------------------------------------------------

class Context:
    def __init__(self, inherited: Dict[str, str], out: Out, *, timeout: float,
                 in_session: bool = False):
        self.inherited = inherited
        self.out = out
        self.timeout = timeout
        self.in_session = in_session
        self.identity = ident.install_identity()
        self.config = load_config()
        self._inventory: Optional[Inventory] = None

    def inventory(self, refresh: bool = False) -> Inventory:
        if self._inventory is None or refresh:
            self._inventory = build_inventory(self.config, self.inherited)
        return self._inventory

    def daemon(self, names: List[str]) -> Dict[str, Any]:
        return daemon_view(self.config, names)

    def confirm(self, question: str, assume_yes: bool) -> bool:
        if assume_yes:
            self.out.say(f"  {question} [auto-yes]")
            return True
        if not sys.stdin.isatty():
            self.out.say(f"  {question} — no TTY to answer; skipping (use --yes)")
            return False
        try:
            answer = input(f"  {question} [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in ("y", "yes")


def _nudge_daemon(ctx: Context, reason: str) -> List[str]:
    """Ask the running daemon to re-read the env file; the restart line
    when it cannot (older code, not running). Returns report lines."""
    from ..config import get_bool

    if not get_bool(ctx.config, "edge_daemon"):
        return []
    try:
        from ..kernel.control import ControlError, request
    except Exception as exc:
        return [f"daemon: cannot import control client ({exc})"]
    restart = ident.restart_daemon_command()
    try:
        result = request("keys.reload", {"reason": reason}, timeout=5.0)
    except ControlError as exc:
        text = str(exc)
        if "unknown control op" in text:
            return [f"daemon: running older code without hot reload — restart it: {restart}"]
        if "socket unavailable" in text or "connection failed" in text:
            return ["daemon: not running (it reads the env file when it starts)"]
        return [f"daemon: reload failed ({text}) — restart it: {restart}"]
    changed = (result or {}).get("changed") or {}
    if changed:
        return ["daemon reloaded: " + ", ".join(f"{name} {fp}" for name, fp in changed.items())]
    return ["daemon reloaded: no change in what it sees (same values)"]


def _mcp_servers_referencing(inventory: Inventory, names: List[str]) -> List[str]:
    from ..mcp import referenced_env_names

    hits = []
    for server, cfg in inventory.mcp_servers.items():
        refs = referenced_env_names({k: cfg.get(k) for k in ("headers", "env", "url", "args")})
        if any(name in refs for name in names):
            hits.append(server)
    return hits


def _required_ok(credentials: List[Credential], daemon_state: str) -> Tuple[bool, List[str]]:
    """Exit-code rule: no required credential may be rejected or missing
    in the shell column, and — when the daemon is enabled and answering —
    the daemon must hold the same value for each required key. A service
    that is unreachable (the local Capitol stack is down) or answers an
    unexpected error is reported but does not make the run red: nothing
    about the *credential* is known to be wrong."""
    problems: List[str] = []
    daemon_flagged = False
    for c in credentials:
        if not c.required:
            continue
        if c.status in (REJECTED, MISSING):
            problems.append(f"{c.name}: {c.status} — {c.detail}")
        if daemon_state == "ok" and c.env_name and c.daemon.startswith(("missing", "stale")):
            problems.append(f"{c.name}: daemon {c.daemon.split(' →')[0]}")
        elif daemon_state == "protocol" and c.env_name and not daemon_flagged:
            daemon_flagged = True
            problems.append("daemon: the running edge daemon predates conch keys — restart it"
                            f" ({ident.restart_daemon_command()}) and re-run check")
    return (not problems), problems


def _warnings(credentials: List[Credential]) -> List[str]:
    return [
        f"{c.name}: {c.status} — {c.detail}"
        for c in credentials if c.required and c.status in (UNREACHABLE, ERROR)
    ]


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_list(ctx: Context, args) -> int:
    inventory = ctx.inventory()
    view = ctx.daemon(inventory.env_names())
    annotate_daemon_column(inventory, view)
    if args.json:
        print(json.dumps({
            "identity": ctx.identity,
            "env_file": inventory.env_file,
            "daemon": {k: v for k, v in view.items() if k != "keys"},
            "credentials": [c.as_dict() for c in inventory.credentials],
        }, indent=2))
        return EXIT_OK
    out = ctx.out
    out.say(out.c("2", f"env file: {inventory.env_file}"
                  + ("" if inventory.env_file_exists else " (missing)")
                  + f"   mcp.json: {inventory.mcp_path}"))
    render_table(out, inventory.credentials, checked=False, daemon=view.get("state") != "disabled")
    render_notes(out, inventory.credentials, verbose=args.verbose)
    _daemon_footer(ctx, view)
    out.say()
    out.say(out.c("2", "conch keys check verifies each key live; conch keys rotate fixes what fails."))
    return EXIT_OK


def _daemon_footer(ctx: Context, view: Dict[str, Any]) -> None:
    out = ctx.out
    state = view.get("state")
    if state == "protocol":
        out.say(out.c("33", "daemon: ") + "the running edge daemon predates `conch keys` (no keys.status);"
                f" restart it to get the daemon column and hot reload: {ident.restart_daemon_command()}")
    elif state == "not running":
        out.say(out.c("33", "daemon: ") + "edge daemon enabled but not running")
    elif state == "ok":
        identity = view.get("identity") or {}
        out.say(out.c("2", f"daemon: pid {view.get('pid')} {identity.get('shape', '')} {view.get('version', '')}"
                      f" {identity.get('commit', '')} reads {view.get('env_file')}"))


def cmd_check(ctx: Context, args) -> int:
    inventory = ctx.inventory()
    names = list(getattr(args, "names", None) or [])
    unknown = [n for n in names if inventory.get(n) is None]
    if unknown:
        ctx.out.say(ctx.out.c("31", f"unknown credential(s): {', '.join(unknown)}")
                    + f" — `conch keys list` shows the names (known: {', '.join(inventory.env_names())})")
        return EXIT_USAGE
    checked = run_checks(inventory, names or None, timeout=ctx.timeout)
    view = ctx.daemon(inventory.env_names())
    annotate_daemon_column(inventory, view)
    ok, problems = _required_ok(checked, str(view.get("state")))
    if args.json:
        print(json.dumps({
            "identity": ctx.identity,
            "ok": ok,
            "problems": problems,
            "daemon": {k: v for k, v in view.items() if k != "keys"},
            "credentials": [c.as_dict() for c in checked],
        }, indent=2))
        return EXIT_OK if ok else EXIT_PROBLEMS
    out = ctx.out
    render_table(out, checked, daemon=view.get("state") != "disabled")
    render_notes(out, checked, verbose=args.verbose)
    _daemon_footer(ctx, view)
    out.say()
    warnings = _warnings(checked)
    if ok:
        out.say(out.c("1;32", "✓ every credential the active config needs verified")
                + (" (shell and daemon)" if view.get("state") == "ok" else ""))
    else:
        out.say(out.c("1;31", "✗ not green:"))
        for problem in problems:
            out.say(f"    {problem}")
        out.say(out.c("2", "  conch keys rotate walks through the fixes."))
    for warning in warnings:
        out.say(out.c("33", "  ! could not verify: ") + warning)
    return EXIT_OK if ok else EXIT_PROBLEMS


def cmd_required(ctx: Context, args) -> int:
    inventory = ctx.inventory()
    required = [c for c in inventory.credentials if c.required]
    if args.json:
        print(json.dumps([{
            "name": c.name, "env_name": c.env_name, "consumers": c.consumers,
            "settable": c.settable, "present": c.present,
        } for c in required], indent=2))
        return EXIT_OK
    out = ctx.out
    out.say(out.c("1", "Credentials the active config needs (ask your key admin for these):"))
    for c in required:
        how = "conch keys set " + c.env_name if c.env_name else (
            "re-bootstrap (Capitol registry)" if c.service.startswith("capitol") else
            ("conch keys migrate-mcp --apply" if c.service == "mcp-inline" else "configured in mcp.json"))
        state = out.c("1;32", "present") if c.present or c.service == "mcp-server" else out.c("31", "missing")
        out.say(f"  {c.name:<32} {state:<18} {'; '.join(c.consumers)}")
        out.say(f"  {'':<32} {out.c('2', how)}")
    return EXIT_OK


def _read_secret(ctx: Context, name: str, prompt: str, *, from_stdin: bool) -> Optional[str]:
    if from_stdin:
        data = sys.stdin.read()
        value = data.strip().splitlines()[0].strip() if data.strip() else ""
        return value or None
    if not sys.stdin.isatty():
        ctx.out.say(ctx.out.c("31", f"no TTY to prompt for {name}; use --stdin (pbpaste | conch keys set {name} --stdin)"))
        return None
    try:
        value = getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return value or None


def _verify_value(ctx: Context, inventory: Inventory, credential: Credential,
                  value: str) -> Tuple[str, str]:
    """Verify a candidate value for *credential* (never via os.environ)."""
    probe = Credential(**{k: v for k, v in credential.__dict__.items()
                          if k not in ("status", "detail", "elapsed", "alt_results", "daemon", "daemon_fp", "verify")})
    probe.verify = None
    scratch = Inventory(
        credentials=[probe], config=inventory.config, env_file=inventory.env_file,
        env_file_exists=inventory.env_file_exists, mcp_path=inventory.mcp_path,
        mcp_servers=inventory.mcp_servers, values=dict(inventory.values),
    )
    scratch.values[probe.name] = value
    for alt in list(scratch.values):
        if alt.startswith(probe.name + "@"):
            scratch.values.pop(alt)
    probe.effective_fp = key_fingerprint(value)
    attach_verifiers(scratch, ctx.timeout)
    if probe.verify is None:
        return SKIPPED, "no verifier"
    try:
        status, detail = probe.verify(ctx.timeout)
    except Exception as exc:
        return ERROR, f"{type(exc).__name__}: {exc}"
    return str(status), str(detail)


def _save(ctx: Context, name: str, value: str, *, set_by: str, verified: str) -> None:
    set_env_values({name: value}, set_by=set_by, verified=verified)
    reload_env_file()


def _after_save(ctx: Context, names: List[str], *, reason: str) -> List[str]:
    """Reload consumers after a save; returns the lines describing what
    still needs a human (profile exports, /reload, daemon restart)."""
    inventory = ctx.inventory(refresh=True)
    lines: List[str] = []
    for name in names:
        credential = inventory.get(name)
        if credential is None:
            continue
        for note in credential.export_notes:
            if "STALE" in note or "only copy" in note:
                lines.append(f"{name}: shell profile {note}")
    servers = _mcp_servers_referencing(inventory, names)
    if servers:
        lines.append("MCP servers " + ", ".join(f"mcp/{s}" for s in servers)
                     + " read the new value on their next start — in a running conch session type /reload")
    lines.extend(_nudge_daemon(ctx, reason))
    return lines


def cmd_set(ctx: Context, args) -> int:
    out = ctx.out
    name = args.name.strip()
    inventory = ctx.inventory()
    credential = inventory.get(name)
    if credential is not None and not credential.settable:
        out.say(out.c("31", f"{name} is not an environment-variable credential")
                + (f" — {credential.stale}" if credential.stale else ""))
        return EXIT_USAGE
    consumer = "; ".join(credential.consumers) if credential else "(no consumer in the active config — saved anyway)"
    value = _read_secret(ctx, name, f"  {name} ({consumer})\n  new value (hidden, Enter to cancel): ",
                         from_stdin=args.stdin)
    if not value:
        out.say("  cancelled — nothing written")
        return EXIT_USAGE
    problem = format_problem(name, value)
    if problem and not args.force:
        out.say(out.c("31", f"  refusing to save {name}: {problem}") + " (--force to override)")
        return EXIT_PROBLEMS
    warning = format_warning(name, value)
    if warning:
        out.say(out.c("33", f"  note: {warning}"))
    status, detail = (SKIPPED, "verification skipped (--no-verify)")
    if not args.no_verify and credential is not None:
        out.say(out.c("2", f"  verifying {name} against {credential.service or 'its service'} …"))
        status, detail = _verify_value(ctx, inventory, credential, value)
        out.say(f"  {out.status(status)}: {detail}")
        if status == REJECTED and not args.force:
            out.say(out.c("31", f"  not saved: the service rejected this value for {name}")
                    + " (--force saves it anyway)")
            return EXIT_PROBLEMS
    elif credential is None:
        status, detail = SKIPPED, "no consumer in the active config to verify against"
    _save(ctx, name, value, set_by="conch keys set", verified=status if status in (OK, REJECTED) else "")
    out.say(out.c("1;32", f"  ✓ saved {name} ({key_fingerprint(value)}) to {get_env_file_path()} (0600, conch-managed)"))
    previous = ctx.inherited.get(name, "")
    if previous and previous != value:
        out.say(out.c("33", f"  your shell still exports an older {name} ({key_fingerprint(previous)});"
                      " conch uses the saved key (conch-managed keys win) — but clean the export up:"))
    for line in _after_save(ctx, [name], reason=f"conch keys set {name}"):
        out.say(f"  {line}")
    return EXIT_OK if status != REJECTED else EXIT_PROBLEMS


def cmd_unset(ctx: Context, args) -> int:
    removed = remove_env_values([args.name])
    if removed:
        ctx.out.say(ctx.out.c("1;32", f"  ✓ removed {args.name} from {get_env_file_path()}")
                    + " (running sessions keep their copy until restarted)")
        for line in _nudge_daemon(ctx, f"conch keys unset {args.name}"):
            ctx.out.say(f"  {line}")
        return EXIT_OK
    ctx.out.say(f"  {args.name} is not in {get_env_file_path()}")
    return EXIT_PROBLEMS


def _import_candidates(ctx: Context, names: Optional[List[str]], all_configured: bool) -> List[Credential]:
    inventory = ctx.inventory()
    wanted: List[Credential] = []
    for c in inventory.credentials:
        if not c.env_name:
            continue
        inherited = ctx.inherited.get(c.env_name, "").strip()
        if not inherited:
            continue
        if names and c.env_name not in names:
            continue
        if not names and not all_configured:
            continue
        if c.file_fp == key_fingerprint(inherited):
            continue  # already saved, same value
        wanted.append(c)
    if names:
        known = {c.env_name for c in wanted}
        for name in names:
            if name not in known:
                value = ctx.inherited.get(name, "").strip()
                if value and inventory.get(name) is None:
                    wanted.append(Credential(name=name, env_name=name, service="unknown",
                                             consumers=["(no consumer found)"]))
    return wanted


def cmd_import(ctx: Context, args) -> int:
    out = ctx.out
    names = list(args.names or [])
    if not names and not args.all_configured:
        out.say("usage: conch keys import NAME… | --all-configured")
        return EXIT_USAGE
    candidates = _import_candidates(ctx, names, args.all_configured)
    missing = [n for n in names if not ctx.inherited.get(n, "").strip()]
    for name in missing:
        out.say(out.c("33", f"  {name}: not in this shell's environment — nothing to import"))
    if not candidates:
        out.say("  nothing to import: every configured key your shell exports is already saved with the same value")
        return EXIT_OK if not missing else EXIT_PROBLEMS
    out.say(out.c("1", "Keys present in this shell's environment but not (or differently) in the env file:"))
    for c in candidates:
        out.say(f"  {c.env_name:<30} shell {key_fingerprint(ctx.inherited[c.env_name])}"
                + (f"   env file {c.file_fp}" if c.file_fp else "   env file: absent")
                + f"   ({'; '.join(c.consumers)})")
    if not ctx.confirm(f"Verify and copy {len(candidates)} key(s) into {get_env_file_path()}?", args.yes):
        out.say("  nothing written")
        return EXIT_PROBLEMS
    inventory = ctx.inventory()
    saved: List[str] = []
    failed = 0
    for c in candidates:
        value = ctx.inherited[c.env_name].strip()
        status, detail = (SKIPPED, "verification skipped (--no-verify)")
        if not args.no_verify:
            status, detail = _verify_value(ctx, inventory, c, value)
        if status == REJECTED and not args.force:
            out.say(f"  {out.status(status)} {c.env_name}: {detail} — not imported (your shell's value is stale too)")
            failed += 1
            continue
        _save(ctx, c.env_name, value, set_by="conch keys import",
              verified=status if status in (OK, REJECTED) else "")
        out.say(f"  {out.status(status)} {c.env_name}: saved ({key_fingerprint(value)}) — {detail}")
        saved.append(c.env_name)
    if saved:
        for line in _after_save(ctx, saved, reason="conch keys import"):
            out.say(f"  {line}")
    return EXIT_OK if not failed else EXIT_PROBLEMS


def cmd_reload(ctx: Context, args) -> int:
    changed = reload_env_file()
    out = ctx.out
    out.say("  this process: " + (", ".join(f"{n} {key_fingerprint(os.environ.get(n, ''))}" for n in changed) or "no change"))
    for line in _nudge_daemon(ctx, "conch keys reload"):
        out.say(f"  {line}")
    return EXIT_OK


def cmd_doctor(ctx: Context, args) -> int:
    from .doctor import run_doctor, worst

    inventory = ctx.inventory()
    view = ctx.daemon(inventory.env_names())
    annotate_daemon_column(inventory, view)
    findings = run_doctor(inventory, ctx.identity, view, fix=args.fix,
                          confirm=lambda q: ctx.confirm(q, args.yes))
    if args.json:
        print(json.dumps({"identity": ctx.identity, "findings": [f.as_dict() for f in findings],
                          "worst": worst(findings)}, indent=2))
        return EXIT_OK if worst(findings) != "fail" else EXIT_PROBLEMS
    ctx.out.say(ctx.out.c("1", "doctor"))
    render_findings(ctx.out, findings, verbose=True)
    return EXIT_OK if worst(findings) != "fail" else EXIT_PROBLEMS


def cmd_migrate_mcp(ctx: Context, args) -> int:
    from .mcpmigrate import apply_migration, plan_migration

    out = ctx.out
    try:
        plan = plan_migration(args.path)
    except ValueError as exc:
        out.say(out.c("31", f"  {exc}"))
        return EXIT_PROBLEMS
    if args.json and not args.apply:
        print(json.dumps(plan.as_dict(), indent=2))
        return EXIT_OK
    if not plan.secrets:
        out.say(f"  {plan.path}: no inline secrets — every credential is a ${{NAME}} reference already")
        return EXIT_OK
    out.say(out.c("1", f"inline secrets in {plan.path}:"))
    for line in plan.lines():
        out.say(f"  {line}")
    if plan.conflicts:
        for conflict in plan.conflicts:
            out.say(out.c("31", f"  conflict: {conflict}"))
        out.say("  resolve the conflicts (conch keys unset NAME, or rename) before --apply")
        return EXIT_PROBLEMS
    if not args.apply:
        out.say(out.c("2", "  dry run — nothing written. Re-run with --apply to back up mcp.json, move the values"
                      " into the env file, and rewrite them as ${NAME}."))
        return EXIT_OK
    if not ctx.confirm(f"Back up {plan.path.name} and rewrite {len(plan.secrets)} value(s) as ${{NAME}}?", args.yes):
        out.say("  nothing written")
        return EXIT_PROBLEMS
    try:
        result = apply_migration(plan)
    except (OSError, ValueError) as exc:
        out.say(out.c("31", f"  migration failed: {exc}"))
        return EXIT_PROBLEMS
    out.say(out.c("1;32", f"  ✓ backup: {result['backup']}"))
    for move in result["moved"]:
        out.say(f"  ✓ {move}")
    out.say(f"  ✓ {result['mcp_json']} rewritten (0600); values saved to {get_env_file_path()} (conch-managed)")
    names = [s.suggested_name for s in plan.secrets]
    for line in _after_save(ctx, names, reason="conch keys migrate-mcp"):
        out.say(f"  {line}")
    if args.json:
        print(json.dumps(result, indent=2))
    return EXIT_OK


# ---------------------------------------------------------------------------
# rotate — the guided flow
# ---------------------------------------------------------------------------

def _stdin_values() -> Dict[str, str]:
    data = sys.stdin.read()
    return parse_env_lines(data)


def cmd_rotate(ctx: Context, args) -> int:
    from .doctor import OK as D_OK, run_doctor, worst

    out = ctx.out
    out.say(out.c("1", "1/4 doctor") + out.c("2", " — install, PATH, env file, shell profiles, daemon"))
    inventory = ctx.inventory()
    view = ctx.daemon(inventory.env_names())
    annotate_daemon_column(inventory, view)
    findings = run_doctor(inventory, ctx.identity, view, fix=True,
                          confirm=lambda q: ctx.confirm(q, args.yes))
    render_findings(out, findings, verbose=args.verbose)
    if worst(findings) == D_OK and not args.verbose:
        out.say("  " + out.c("1;32", "✓") + " nothing unusual about this install")
    inventory = ctx.inventory(refresh=True)  # the env file may have just been created

    out.say()
    out.say(out.c("1", "2/4 check") + out.c("2", " — verifying every key live"))
    checked = run_checks(inventory, None, timeout=ctx.timeout)
    view = ctx.daemon(inventory.env_names())
    annotate_daemon_column(inventory, view)
    render_table(out, checked, daemon=view.get("state") != "disabled")
    render_notes(out, checked, verbose=args.verbose)

    out.say()
    out.say(out.c("1", "3/4 rotate") + out.c("2", " — new values for what failed"))
    stdin_values = _stdin_values() if args.stdin else {}
    human: List[str] = []
    changed: List[str] = []
    interactive = sys.stdin.isatty() and not args.stdin and not args.from_env
    todo: List[Credential] = []
    for c in checked:
        if not c.env_name:
            if c.status in (REJECTED, MISSING) and c.required:
                human.append(f"{c.name}: {c.detail}" + (f" — {c.stale}" if c.stale else ""))
            continue
        needs = c.status in (REJECTED, MISSING, ERROR) or (c.status == UNREACHABLE and c.required and False)
        daemon_blind = c.daemon.startswith("missing (daemon-blind)") or (
            view.get("state") == "disabled" and c.effective.startswith("shell env") and c.required
        )
        if needs and (c.required or args.all):
            todo.append(c)
        elif c.status == OK and (daemon_blind or c.effective.startswith("shell env")) and (args.from_env or args.yes or interactive):
            todo.append(c)  # verified in the shell but not saved: import it
    if not todo:
        out.say("  " + out.c("1;32", "✓") + " nothing to rotate")
    for c in todo:
        name = c.env_name
        label = "; ".join(c.consumers)
        candidate: Optional[str] = None
        source = ""
        if name in stdin_values and stdin_values[name].strip():
            candidate, source = stdin_values[name].strip(), "stdin"
        elif args.from_env or c.status == OK:
            inherited = ctx.inherited.get(name, "").strip()
            if inherited and key_fingerprint(inherited) != c.file_fp:
                candidate, source = inherited, "shell env"
        if candidate is None and interactive and c.status != OK:
            out.say(f"  {out.c('1', name)} — {label}")
            out.say(f"    {out.status(c.status)}: {c.detail}")
            attempts = 0
            while attempts < 3:
                attempts += 1
                value = _read_secret(ctx, name, "    new value (hidden, Enter to skip): ", from_stdin=False)
                if not value:
                    break
                problem = format_problem(name, value)
                if problem:
                    out.say(out.c("31", f"    {problem} — try again"))
                    continue
                status, detail = _verify_value(ctx, inventory, c, value)
                out.say(f"    {out.status(status)}: {detail}")
                if status == REJECTED:
                    continue
                candidate, source = value, "prompt"
                break
        if candidate is None:
            if c.status != OK:
                how = "" if interactive else " — pass it via --from-env, --stdin, or run interactively"
                out.say(f"  {out.c('1', name)}: {out.status(c.status)} and no new value available{how}")
                human.append(f"{name}: {c.status} — {c.detail} (no new value provided)")
            continue
        problem = format_problem(name, candidate)
        if problem:
            human.append(f"{name}: value from {source} looks wrong — {problem}")
            continue
        if source != "prompt":
            status, detail = _verify_value(ctx, inventory, c, candidate)
            out.say(f"  {out.c('1', name)} from {source}: {out.status(status)} — {detail}")
            if status == REJECTED:
                human.append(f"{name}: the value from {source} is rejected too ({detail})")
                continue
        else:
            status = OK
        _save(ctx, name, candidate, set_by="conch keys rotate", verified=status if status in (OK, REJECTED) else "")
        out.say(out.c("1;32", f"    ✓ saved {name} ({key_fingerprint(candidate)}) — conch-managed"))
        changed.append(name)

    out.say()
    out.say(out.c("1", "4/4 reload + verify") + out.c("2", " — every consumer"))
    if changed:
        for line in _after_save(ctx, changed, reason="conch keys rotate"):
            out.say(f"  {line}")
    else:
        for line in _nudge_daemon(ctx, "conch keys rotate"):
            out.say(f"  {line}")
    inventory = ctx.inventory(refresh=True)
    checked = run_checks(inventory, None, timeout=ctx.timeout)
    view = ctx.daemon(inventory.env_names())
    annotate_daemon_column(inventory, view)
    render_table(out, checked, daemon=view.get("state") != "disabled")
    ok, problems = _required_ok(checked, str(view.get("state")))
    for c in checked:
        for note in c.export_notes:
            if "STALE" in note:
                human.append(f"{c.env_name}: {note}")
        if c.service == "mcp-inline":
            human.append(f"{c.name}: inline secret in mcp.json — conch keys migrate-mcp --apply")
    if view.get("state") == "protocol":
        human.append(f"daemon: restart it so it reloads keys and reports its column: {ident.restart_daemon_command()}")
    for finding in findings:
        if finding.code in ("daemon.stale-code", "daemon.program", "install.stale", "path.multiple") and finding.level != D_OK and finding.fix:
            human.append(f"{finding.message} — {finding.fix}")
    out.say()
    if args.json:
        print(json.dumps({"identity": ctx.identity, "ok": ok, "problems": problems, "changed": changed,
                          "remaining": human, "credentials": [c.as_dict() for c in checked],
                          "doctor": [f.as_dict() for f in findings]}, indent=2))
        return EXIT_OK if ok else EXIT_PROBLEMS
    if ok:
        out.say(out.c("1;32", "✓ green: every credential the active config needs verifies")
                + (" in the shell and the daemon" if view.get("state") == "ok" else ""))
    else:
        out.say(out.c("1;31", "✗ not green yet:"))
        for problem in problems:
            out.say(f"    {problem}")
    for warning in _warnings(checked):
        out.say(out.c("33", "  ! could not verify: ") + warning)
    seen = set()
    human = [h for h in human if not (h in seen or seen.add(h))]
    if human:
        out.say(out.c("1", "still needs a human:"))
        for line in human:
            out.say(f"  • {line}")
    return EXIT_OK if ok else EXIT_PROBLEMS


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        if "invalid choice" in message:
            bad = message.split("invalid choice: ")[1].split(" ")[0].strip("'\"")
            close = difflib.get_close_matches(bad, SUBCOMMANDS, n=3, cutoff=0.5)
            hint = f" — did you mean {', '.join(close)}?" if close else ""
            print(f"{self.prog}: unknown subcommand {bad!r}{hint} (try: conch keys --help)", file=sys.stderr)
            sys.exit(EXIT_USAGE)
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def build_parser(prog: str = "conch keys") -> argparse.ArgumentParser:
    parser = _Parser(
        prog=prog,
        description=(
            "See, verify, rotate, and reload every credential conch uses — the\n"
            "interactive shell, conch-ask, the edge daemon, workers, and MCP servers\n"
            "all read ~/.config/conch/env, and keys saved through this command are\n"
            "authoritative over shell exports of the same name.\n\n"
            "Rotated keys? Run:   conch keys rotate"
        ),
        epilog=(
            "Values are never printed or logged; the tables show sha256 fingerprints.\n"
            "Fallbacks when `conch` is not on PATH: ./bin/conch keys … (checkout),\n"
            "python3 -m conch keys …, ~/.local/bin/conch keys … (uv install)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS",
                        help=f"per-service verification bound (default {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--no-color", action="store_true", help="plain output")
    sub = parser.add_subparsers(dest="command", metavar="subcommand")

    def common(p, *, json_flag=True, verbose=True):
        # The global options are accepted after the subcommand too
        # (`conch keys list --no-color`); SUPPRESS keeps the parser-level
        # default when they are not repeated.
        p.add_argument("--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS",
                       help=argparse.SUPPRESS)
        p.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS,
                       help=argparse.SUPPRESS)
        if json_flag:
            p.add_argument("--json", action="store_true", help="machine-readable output (no values)")
        if verbose:
            p.add_argument("-v", "--verbose", action="store_true", help="show details for ok rows too")

    p = sub.add_parser("list", help="every credential: consumer, effective source, fingerprint, last verified (default)")
    common(p)
    p = sub.add_parser("check", help="live-verify keys against their services; exit 1 if the active config is not green")
    p.add_argument("names", nargs="*", metavar="NAME")
    common(p)
    p = sub.add_parser("set", help="prompt for a new value (never echoed), verify it, save it 0600, reload consumers")
    p.add_argument("name", metavar="NAME")
    p.add_argument("--stdin", action="store_true", help="read the value from stdin (pbpaste | conch keys set NAME --stdin)")
    p.add_argument("--no-verify", action="store_true", help="save without the live check")
    p.add_argument("--force", action="store_true", help="save even if the service rejects it or the format looks off")
    common(p, json_flag=False, verbose=False)
    p = sub.add_parser("unset", help="remove a key from the env file")
    p.add_argument("name", metavar="NAME")
    common(p, json_flag=False, verbose=False)
    p = sub.add_parser("import", help="copy keys your shell exports into the env file (so the daemon sees them)")
    p.add_argument("names", nargs="*", metavar="NAME")
    p.add_argument("--all-configured", action="store_true", help="every key the active config uses that the shell exports")
    p.add_argument("--yes", "-y", action="store_true", help="skip the confirmation")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--force", action="store_true", help="import even if the service rejects the value")
    common(p, json_flag=False, verbose=False)
    p = sub.add_parser("rotate", help="guided: doctor → check → prompt for what failed → verify → save → reload")
    p.add_argument("--from-env", action="store_true",
                   help="take new values from this process's environment (op run / direnv / CI), no prompts")
    p.add_argument("--stdin", action="store_true", help="read NAME=value lines from stdin (dotenv format), no prompts")
    p.add_argument("--yes", "-y", action="store_true", help="auto-confirm doctor fixes and imports")
    p.add_argument("--all", action="store_true", help="also rotate optional keys that fail (not just required ones)")
    common(p)
    p = sub.add_parser("migrate-mcp", help="move inline mcp.json secrets into the env file as ${NAME} (dry run unless --apply)")
    p.add_argument("--apply", action="store_true", help="write (a timestamped .bak is made first)")
    p.add_argument("--path", default=None, help="mcp.json to migrate (default ~/.config/conch/mcp.json)")
    p.add_argument("--yes", "-y", action="store_true")
    common(p, verbose=False)
    p = sub.add_parser("doctor", help="explain install/PATH/profile/daemon weirdness; --fix repairs the env file")
    p.add_argument("--fix", action="store_true", help="create/chmod the env file (asks unless --yes)")
    p.add_argument("--yes", "-y", action="store_true")
    common(p)
    p = sub.add_parser("required", help="the NAMEs the active config needs (what to ask the key admin for)")
    common(p, verbose=False)
    p = sub.add_parser("reload", help="re-read the env file here and tell the running daemon to do the same")
    common(p, json_flag=False, verbose=False)
    return parser


COMMANDS: Dict[str, Callable[[Context, Any], int]] = {
    "list": cmd_list, "check": cmd_check, "set": cmd_set, "unset": cmd_unset,
    "import": cmd_import, "rotate": cmd_rotate, "migrate-mcp": cmd_migrate_mcp,
    "doctor": cmd_doctor, "required": cmd_required, "reload": cmd_reload,
}


def main(argv: Optional[List[str]] = None, *, prog: str = "conch keys",
         inherited: Optional[Dict[str, str]] = None) -> int:
    """Entry point for ``conch keys …`` / ``conch-keys …`` /
    ``python3 -m conch keys …``. Returns the exit status."""
    snapshot = dict(os.environ) if inherited is None else dict(inherited)
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser(prog)
    args = parser.parse_args(argv)
    command = args.command or "list"
    if command == "list" and not hasattr(args, "json"):
        args.json = False
        args.verbose = False
    json_mode = bool(getattr(args, "json", False))
    out = Out(color=False if args.no_color or json_mode else None, json_mode=json_mode)
    identity = ident.install_identity()
    if not json_mode:
        out.say(out.c("2", ident.identity_line(identity)))
    ctx = Context(snapshot, out, timeout=float(args.timeout))
    ctx.identity = identity
    try:
        return int(COMMANDS[command](ctx, args))
    except KeyboardInterrupt:
        out.say("\n  interrupted — nothing partially written (saves are atomic)")
        return 130


# ---------------------------------------------------------------------------
# /keys in the interactive shell
# ---------------------------------------------------------------------------

SLASH_USAGE = (
    "\n  \033[1;36m/keys commands:\033[0m\n"
    "    /keys                    inventory: name, consumer, source, fingerprint\n"
    "    /keys check [NAME…]      verify live (shell + daemon)\n"
    "    /keys set NAME           new value (hidden prompt), verify, save, hot-reload this session\n"
    "    /keys unset NAME         remove from the env file\n"
    "    /keys import [NAME…|--all-configured]  copy shell-exported keys into the env file\n"
    "    /keys rotate             guided rotation (doctor → check → prompt → verify → reload)\n"
    "    /keys doctor | required | reload | migrate-mcp [--apply]\n"
)


def run_slash(arg: str, config: dict, *, inherited: Optional[Dict[str, str]] = None) -> Optional[str]:
    """``/keys …`` inside the shell. Returns ``"reload_tools"`` when a
    saved key is referenced by an MCP server (so the loop restarts the
    servers with the new value), else None."""
    import shlex

    try:
        tokens = shlex.split(arg or "")
    except ValueError as exc:
        print(f"\n  \033[31m/keys: {exc}\033[0m\n")
        return None
    if tokens and tokens[0] in ("help", "-h", "--help"):
        print(SLASH_USAGE)
        return None
    snapshot = dict(inherited) if inherited is not None else dict(os.environ)
    before = {name: os.environ.get(name) for name in list(os.environ)}
    try:
        code = main(tokens or ["list"], prog="/keys", inherited=snapshot)
    except SystemExit as exc:  # argparse errors
        code = int(exc.code or 0) if isinstance(exc.code, int) else 2
        if code == EXIT_USAGE and not tokens:
            print(SLASH_USAGE)
    print()
    changed = [name for name, value in os.environ.items() if before.get(name) != value]
    if not changed:
        return None
    try:
        inventory = build_inventory(config, snapshot, scan_profiles=False, with_plugins=False)
        if _mcp_servers_referencing(inventory, changed):
            return "reload_tools"
    except Exception:
        pass
    return None
