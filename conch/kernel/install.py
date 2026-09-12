"""Managed uptime for the conch-edge daemon: install / uninstall / status.

``conch-edge install`` renders the launchd agent (macOS) or systemd user
unit (Linux) for *this* machine — resolved program path, PATH, log
locations — loads it, and verifies the daemon answers on its control
socket. It is the documented default way to run the daemon: the OS
supervisor restarts it on crash and brings it back after reboot, which is
what makes missions and channel intake genuinely always-on.

The files in ``deploy/`` are these same templates rendered with their
documentation defaults; a test keeps them byte-identical, so the copy a
user edits by hand and the copy the installer writes can never drift.

Secrets stay by reference: a rendered unit carries only PATH (plus
PYTHONPATH for repository checkouts). Channel and provider tokens the
daemon needs must reach it as environment by reference —
``launchctl setenv NAME value`` on macOS, or a systemd user drop-in with
``EnvironmentFile=`` pointing at a 0600 file on Linux — never bytes baked
into the plist or unit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from .control import daemon_alive
from .store import default_kernel_dir

LAUNCHD_LABEL = "com.conch.edge"

#: How long install waits for the supervised daemon to answer its socket.
VERIFY_SECONDS = 30.0

_EDGE_MAIN_SNIPPET = (
    "from conch.entrypoints import edge_main; "
    "import sys; sys.exit(edge_main())"
)

_LAUNCHD_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!--
  launchd template for the conch-edge daemon (macOS, per-user agent).

  The default way to install and manage the daemon is the built-in
  installer, which renders this template with your machine's paths:

      conch-edge install     # render + load + verify
      conch-edge status      # launchd state + daemon health
      conch-edge uninstall   # stop + remove

  Manual install (this file carries the documentation defaults):
    1. Adjust ProgramArguments to the absolute path of conch-edge
       (`which conch-edge`).
    2. cp deploy/com.conch.edge.plist ~/Library/LaunchAgents/
    3. launchctl bootstrap gui/$(id -u) \\
         ~/Library/LaunchAgents/com.conch.edge.plist

  The daemon requires `edge_daemon = true` in ~/.config/conch/config (it
  exits with EX_CONFIG otherwise), runs unprivileged as you, writes its log
  to ~/.local/state/conch/kernel/daemon.log, and exposes only a local
  0700-protected control socket - never a network port. Secrets stay by
  reference: this file carries only PATH; hand the daemon channel/provider
  tokens with `launchctl setenv NAME value`, never by editing this file.
-->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.conch.edge</string>
    <key>ProgramArguments</key>
    <array>
{program_lines}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
{environment_lines}
    </dict>
{working_directory_block}    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <!-- Restart on crash, but respect a clean exit (config disabled,
             another daemon holds the lock). -->
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>{stdout_path}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_path}</string>
</dict>
</plist>
"""

_SYSTEMD_TEMPLATE = """\
# systemd user-unit template for the conch-edge daemon (Linux).
#
# The default way to install and manage the daemon is the built-in
# installer, which renders this template with your machine's paths:
#
#     conch-edge install     # render + enable --now + verify
#     conch-edge status      # unit state + daemon health
#     conch-edge uninstall   # stop + remove
#
# Manual install (this file carries the documentation defaults):
#   1. Adjust ExecStart to the absolute path of conch-edge
#      (`which conch-edge`).
#   2. cp deploy/conch-edge.service ~/.config/systemd/user/
#   3. systemctl --user daemon-reload
#   4. systemctl --user enable --now conch-edge
#
# Manage:
#   systemctl --user status conch-edge
#   journalctl --user -u conch-edge -f
#
# The daemon requires `edge_daemon = true` in ~/.config/conch/config (it
# exits with EX_CONFIG otherwise), runs unprivileged as your user, keeps
# its kernel + log under ~/.local/state/conch/kernel/, and exposes only a
# local 0700-protected control socket - never a network port. Secrets stay
# by reference: give the daemon tokens via a drop-in with EnvironmentFile=
# pointing at a 0600 file, never inline in this unit.

[Unit]
Description=Conch edge daemon (durable mission kernel)
After=default.target

[Service]
Type=simple
ExecStart={exec_start}
{environment_lines}Restart=on-failure
RestartSec=10
# Graceful stop: SIGTERM finishes the in-flight kernel transaction.
KillSignal=SIGTERM
TimeoutStopSec=30
# Hardening (safe defaults for a per-user daemon; loosen only knowingly).
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""


# ---------------------------------------------------------------------------
# Resolution (what to run, with what environment)
# ---------------------------------------------------------------------------

def resolve_program() -> Tuple[List[str], Dict[str, str]]:
    """(argv, environment) for the supervised daemon on this machine.

    Prefers the installed ``conch-edge`` console script; a repository
    checkout without one falls back to the current interpreter with
    PYTHONPATH pointing at the package parent. The environment is a
    whitelist — PATH (so mission shell commands find tools) and, for the
    fallback, PYTHONPATH — never a copy of the caller's environment.
    """
    environment = {
        "PATH": os.environ.get("PATH", "").strip()
        or "/usr/local/bin:/usr/bin:/bin",
    }
    script = shutil.which("conch-edge")
    if script:
        return [script], environment
    package_parent = Path(__file__).resolve().parent.parent.parent
    environment["PYTHONPATH"] = str(package_parent)
    return [sys.executable, "-c", _EDGE_MAIN_SNIPPET], environment


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit_path() -> Path:
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", "").strip()
        or (Path.home() / ".config")
    )
    return config_home / "systemd" / "user" / "conch-edge.service"


def _launchd_log_paths() -> Tuple[str, str]:
    kernel_dir = default_kernel_dir()
    return str(kernel_dir / "launchd.out"), str(kernel_dir / "launchd.err")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_launchd_plist(program_args: List[str],
                         environment: Dict[str, str],
                         stdout_path: str, stderr_path: str,
                         working_directory: str = "") -> str:
    program_lines = "\n".join(
        f"        <string>{escape(arg)}</string>" for arg in program_args
    )
    environment_lines = "\n".join(
        f"        <key>{escape(key)}</key>\n"
        f"        <string>{escape(value)}</string>"
        for key, value in sorted(environment.items())
    )
    working_directory_block = ""
    if working_directory:
        working_directory_block = (
            "    <key>WorkingDirectory</key>\n"
            f"    <string>{escape(working_directory)}</string>\n"
        )
    return _LAUNCHD_TEMPLATE.format(
        program_lines=program_lines,
        environment_lines=environment_lines,
        working_directory_block=working_directory_block,
        stdout_path=escape(stdout_path),
        stderr_path=escape(stderr_path),
    )


def _systemd_quote(arg: str) -> str:
    if not arg or any(ch.isspace() for ch in arg):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def render_systemd_unit(program_args: List[str],
                        environment: Dict[str, str]) -> str:
    exec_start = " ".join(_systemd_quote(arg) for arg in program_args)
    environment_lines = "".join(
        f'Environment="{key}={value}"\n'
        for key, value in sorted(environment.items())
    )
    return _SYSTEMD_TEMPLATE.format(
        exec_start=exec_start, environment_lines=environment_lines
    )


def deploy_template_text(kind: str) -> str:
    """The documentation-default render — byte-identical to ``deploy/``."""
    if kind == "launchd":
        return render_launchd_plist(
            ["/usr/local/bin/conch-edge"],
            {"PATH": "/usr/local/bin:/usr/bin:/bin"},
            "/tmp/conch-edge.launchd.out",
            "/tmp/conch-edge.launchd.err",
        )
    if kind == "systemd":
        return render_systemd_unit(["%h/.local/bin/conch-edge"], {})
    raise ValueError(f"unknown deploy template kind {kind!r}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _default_runner(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _wait_for(predicate: Callable[[], bool], seconds: float,
              sleep: Callable[[float], None]) -> bool:
    deadline = time.time() + seconds
    while True:
        if predicate():
            return True
        if time.time() >= deadline:
            return False
        sleep(0.5)


def _print_daemon_health(out: Callable[[str], None]) -> None:
    from .control import ControlError, request

    try:
        status = request("status")
    except ControlError as exc:
        out(f"  control socket: no answer ({exc})")
        return
    missions = ", ".join(
        f"{count} {name}" for name, count in
        sorted((status.get("missions") or {}).items())
    ) or "none"
    out(f"  daemon: healthy — holder {status.get('holder')}, "
        f"epoch {status.get('epoch')}, "
        f"uptime {int(status.get('uptime_seconds') or 0)}s")
    out(f"  missions: {missions}")
    intake = status.get("channel_intake")
    if intake:
        out(f"  channel intake lease: {intake.get('holder')}")


def install_cmd(config: dict, *, platform_name: str = "",
                runner: Optional[Callable] = None,
                alive: Optional[Callable[[], bool]] = None,
                sleep: Callable[[float], None] = time.sleep,
                verify_seconds: float = VERIFY_SECONDS,
                out: Callable[[str], None] = print) -> int:
    """Render, load, and verify the OS-supervised daemon. Idempotent:
    reinstalling replaces the unit and restarts the daemon."""
    platform_name = platform_name or sys.platform
    runner = runner or _default_runner
    alive = alive or (lambda: daemon_alive())
    program_args, environment = resolve_program()
    if platform_name == "darwin":
        uid = os.getuid()
        plist_path = launchd_plist_path()
        stdout_path, stderr_path = _launchd_log_paths()
        Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
        rendered = render_launchd_plist(
            program_args, environment, stdout_path, stderr_path,
            working_directory=str(Path.home()),
        )
        # Idempotent reinstall: unload any previous instance first (a plain
        # `bootstrap` over a loaded label fails).
        runner(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"])
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(rendered)
        out(f"conch-edge install: wrote {plist_path}")
        result = runner(["launchctl", "bootstrap", f"gui/{uid}",
                         str(plist_path)])
        if result.returncode != 0:
            # Older launchd builds: fall back to the legacy loader.
            legacy = runner(["launchctl", "load", "-w", str(plist_path)])
            if legacy.returncode != 0:
                out("conch-edge install: launchctl could not load the agent:")
                out(f"  bootstrap: {result.stderr.strip() or result.stdout.strip()}")
                out(f"  load -w:   {legacy.stderr.strip() or legacy.stdout.strip()}")
                return 1
        runner(["launchctl", "enable", f"gui/{uid}/{LAUNCHD_LABEL}"])
        if not _wait_for(alive, verify_seconds, sleep):
            out(
                "conch-edge install: the agent is loaded but the daemon did"
                " not answer its control socket within"
                f" {int(verify_seconds)}s — check {stderr_path} and the"
                " daemon log."
            )
            return 1
        listed = runner(["launchctl", "list", LAUNCHD_LABEL])
        out(f"conch-edge install: launchd agent {LAUNCHD_LABEL} running"
            + (" (launchctl list: ok)" if listed.returncode == 0 else ""))
        _print_daemon_health(out)
        out(
            "  note: launchd starts the daemon with a minimal environment;"
            " tokens it needs (Slack, providers) must be supplied by"
            " reference, e.g. `launchctl setenv SLACK_BOT_TOKEN ...`."
        )
        return 0
    if platform_name.startswith("linux"):
        unit_path = systemd_unit_path()
        rendered = render_systemd_unit(program_args, environment)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(rendered)
        out(f"conch-edge install: wrote {unit_path}")
        reload_result = runner(["systemctl", "--user", "daemon-reload"])
        if reload_result.returncode != 0:
            out("conch-edge install: systemctl --user daemon-reload failed:"
                f" {reload_result.stderr.strip()}")
            return 1
        enable = runner(["systemctl", "--user", "enable", "--now",
                         "conch-edge"])
        if enable.returncode != 0:
            out("conch-edge install: systemctl --user enable --now failed:"
                f" {enable.stderr.strip()}")
            return 1
        if not _wait_for(alive, verify_seconds, sleep):
            out(
                "conch-edge install: the unit is enabled but the daemon did"
                " not answer its control socket within"
                f" {int(verify_seconds)}s — see journalctl --user -u"
                " conch-edge."
            )
            return 1
        out("conch-edge install: systemd user unit conch-edge running")
        _print_daemon_health(out)
        out(
            "  note: give the daemon tokens by reference via a drop-in"
            " with EnvironmentFile= pointing at a 0600 file."
        )
        return 0
    out(
        f"conch-edge install: unsupported platform {platform_name!r} —"
        " launchd (macOS) and systemd user units (Linux) are supported;"
        " see deploy/ for the templates."
    )
    return 2


def uninstall_cmd(config: dict, *, platform_name: str = "",
                  runner: Optional[Callable] = None,
                  alive: Optional[Callable[[], bool]] = None,
                  sleep: Callable[[float], None] = time.sleep,
                  out: Callable[[str], None] = print) -> int:
    """Stop the supervised daemon and remove the unit. Idempotent."""
    platform_name = platform_name or sys.platform
    runner = runner or _default_runner
    alive = alive or (lambda: daemon_alive())
    if platform_name == "darwin":
        uid = os.getuid()
        runner(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"])
        plist_path = launchd_plist_path()
        removed = False
        if plist_path.exists():
            plist_path.unlink()
            removed = True
        stopped = _wait_for(lambda: not alive(), 10.0, sleep)
        out(
            "conch-edge uninstall: "
            + (f"removed {plist_path}" if removed
               else "no launchd agent was installed")
            + ("; daemon stopped" if stopped else
               "; a daemon still answers the control socket (it may be"
               " running outside launchd)")
        )
        return 0
    if platform_name.startswith("linux"):
        runner(["systemctl", "--user", "disable", "--now", "conch-edge"])
        unit_path = systemd_unit_path()
        removed = False
        if unit_path.exists():
            unit_path.unlink()
            removed = True
        runner(["systemctl", "--user", "daemon-reload"])
        stopped = _wait_for(lambda: not alive(), 10.0, sleep)
        out(
            "conch-edge uninstall: "
            + (f"removed {unit_path}" if removed
               else "no systemd user unit was installed")
            + ("; daemon stopped" if stopped else
               "; a daemon still answers the control socket (it may be"
               " running outside systemd)")
        )
        return 0
    out(f"conch-edge uninstall: unsupported platform {platform_name!r}")
    return 2


def status_cmd(config: dict, *, platform_name: str = "",
               runner: Optional[Callable] = None,
               alive: Optional[Callable[[], bool]] = None,
               out: Callable[[str], None] = print) -> int:
    """Report supervisor state and daemon health. Exit 0 when healthy."""
    platform_name = platform_name or sys.platform
    runner = runner or _default_runner
    alive = alive or (lambda: daemon_alive())
    if platform_name == "darwin":
        plist_path = launchd_plist_path()
        out(f"  agent file: {plist_path}"
            + ("" if plist_path.exists() else " (not installed)"))
        listed = runner(["launchctl", "list", LAUNCHD_LABEL])
        out("  launchctl list: "
            + ("loaded" if listed.returncode == 0 else "not loaded"))
    elif platform_name.startswith("linux"):
        unit_path = systemd_unit_path()
        out(f"  unit file: {unit_path}"
            + ("" if unit_path.exists() else " (not installed)"))
        active = runner(["systemctl", "--user", "is-active", "conch-edge"])
        out(f"  systemctl --user is-active: {active.stdout.strip() or '?'}")
    else:
        out(f"  platform {platform_name!r}: no supervisor integration")
    if not alive():
        out("  daemon: not answering the control socket")
        return 1
    _print_daemon_health(out)
    return 0
