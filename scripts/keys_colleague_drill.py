#!/usr/bin/env python3
"""The colleague drill: prove the two-line instruction works blind.

    git pull
    ./bin/conch keys rotate      # or: conch keys rotate

A deliberately weird machine is built under an isolated HOME (never the
real one): an older ``conch`` shim first on PATH, a ``~/.zshrc`` that
exports a stale ``ANTHROPIC_API_KEY`` and sources a dot-env file, no env
file at all, an ``mcp.json`` with one ``${NAME}`` reference and one
inline secret, and an edge daemon speaking the *old* control protocol
(no ``keys.status``). Fake services stand in for Anthropic, Jira, and an
MCP server (no network). Then, as the colleague would, in a bash
non-login shell with no PATH setup:

1. ``git pull`` in a scratch clone parked at an older commit (skipped
   with ``--no-clone``: the suite runs the checkout directly);
2. ``./bin/conch keys rotate --from-env --yes`` with the fresh keys in
   the process environment (``op run -- …`` style) — ends *not green*
   with every weirdness named: the two binaries, the stale export at
   ``file:line``, the env file created, the inline mcp.json secret, and
   the daemon that must be restarted;
3. the named remaining steps: ``migrate-mcp --apply``, restart the daemon
   (the real daemon code is started in that HOME);
4. ``./bin/conch keys check`` and ``python3 -m conch keys check`` — green
   in both the shell and the daemon column.

Exit status 0 when every expectation holds; the transcript is printed
either way. Nothing here prints a secret value (the fakes are
throwaways, and the output is scanned for them anyway).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from keys_fakes import FakeServices  # noqa: E402
from modelcheck_stubs import StubEndpoint  # noqa: E402

FRESH_ANTHROPIC = "sk-ant-fresh-colleague-0123456789abcdef"
STALE_ANTHROPIC = "sk-ant-stale-from-zshrc-0123456789abcdef"
MCP_TOKEN = "mcp-hosted-token-0123456789abcdef"
JIRA_TOKEN = "jira-inline-token-0123456789abcdef"
SECRETS = (FRESH_ANTHROPIC, STALE_ANTHROPIC, MCP_TOKEN, JIRA_TOKEN)


class Drill:
    def __init__(self, root: Path, *, clone: bool, keep: bool):
        self.root = root
        self.clone = clone
        self.keep = keep
        self.home = root / "home"
        self.failures: list = []
        self.transcript: list = []

    # -- output ---------------------------------------------------------------

    def say(self, line: str = "") -> None:
        for secret in SECRETS:
            if secret in line:
                line = line.replace(secret, "<redacted-secret>")
                self.failures.append("a secret value reached the transcript")
        self.transcript.append(line)
        print(line, flush=True)

    def expect(self, condition: bool, what: str) -> None:
        mark = "PASS" if condition else "FAIL"
        self.say(f"  [{mark}] {what}")
        if not condition:
            self.failures.append(what)

    # -- the weird machine ----------------------------------------------------

    def build(self) -> None:
        self.say("== building the weird machine under " + str(self.root))
        if self.root.exists():
            shutil.rmtree(self.root)
        cfg = self.home / ".config" / "conch"
        cfg.mkdir(parents=True)
        (self.home / ".local" / "state").mkdir(parents=True)
        # services
        self.anthropic = StubEndpoint(["claude-sonnet-5"], require_token=FRESH_ANTHROPIC).start()
        self.services = FakeServices(mcp_tokens={MCP_TOKEN}, jira_accounts={"me@example.com": JIRA_TOKEN}).start()
        # config: anthropic active, daemon enabled, one referenced + one inline MCP secret
        (cfg / "config").write_text(
            "provider = anthropic\nmodel = claude-sonnet-5\napi_key_env = ANTHROPIC_API_KEY\n"
            "edge_daemon = true\nmodel_check_timeout = 2\n"
        )
        (cfg / "mcp.json").write_text(json.dumps({"mcpServers": {
            "hosted": {"type": "http", "url": self.services.mcp_url,
                       "headers": {"Authorization": "Bearer ${MCP_HOSTED_TOKEN}"}},
            "atlassian": {"type": "http", "url": self.services.mcp_url,
                          "headers": {"Authorization": "Bearer ${MCP_HOSTED_TOKEN}"},
                          "env": {"JIRA_URL": self.services.base, "JIRA_USERNAME": "me@example.com",
                                  "JIRA_API_TOKEN": JIRA_TOKEN}},
        }}, indent=2))
        os.chmod(cfg / "mcp.json", 0o644)
        # profile weirdness: stale export + a sourced dot-env
        (self.home / "dotfiles").mkdir()
        (self.home / "dotfiles" / ".env").write_text("export COMPOSIO_API_KEY=comp-from-dot-env-0123456789\n")
        (self.home / ".zshrc").write_text(
            "export PATH=/opt/homebrew/bin:$PATH\n"
            'export CONCH_DIR="$HOME/dotfiles"\n'
            '[[ -f "${CONCH_DIR}/.env" ]] && source "${CONCH_DIR}/.env"\n'
            f"export ANTHROPIC_API_KEY={STALE_ANTHROPIC}\n"
        )
        # an older conch shim first on PATH
        old = self.root / "old"
        (old / "conch").mkdir(parents=True)
        (old / "conch" / "__init__.py").write_text('__version__ = "0.4.0"\n')
        (old / "bin").mkdir()
        shim = old / "bin" / "conch"
        shim.write_text(
            "#!/usr/bin/env python3\n\"\"\"stale checkout shim\"\"\"\nimport sys\nfrom pathlib import Path\n"
            "ROOT = Path(__file__).resolve().parent.parent\nif str(ROOT) not in sys.path:\n"
            "    sys.path.insert(0, str(ROOT))\nprint('old shim', file=sys.stderr)\nsys.exit(99)\n"
        )
        shim.chmod(0o755)
        self.old_bin = old / "bin"
        # the colleague's checkout: a scratch clone parked at an older commit, or this checkout
        if self.clone:
            self.checkout = self.root / "clone"
            self._clone_at_older_commit()
        else:
            self.checkout = ROOT
        # an edge daemon speaking the OLD protocol (no keys.status)
        self.state_dir = self.home / ".local" / "state" / "conch"
        self.socket_path = self.state_dir / "run" / "edge.sock"
        self.old_daemon = OldProtocolDaemon(self.socket_path).start()

    def _clone_at_older_commit(self) -> None:
        subprocess.run(["git", "clone", "--quiet", str(ROOT), str(self.checkout)], check=True)
        branch = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        older = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD~1"],
                               capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", str(self.checkout), "checkout", "--quiet", "-B", branch, older], check=True)
        subprocess.run(["git", "-C", str(self.checkout), "branch", "--quiet", f"--set-upstream-to=origin/{branch}"],
                       check=True)
        self.say(f"  scratch clone at {older} (one commit behind {branch})")

    def env(self, **extra) -> dict:
        """The colleague's non-login shell environment: system PATH with
        the old shim first, HOME isolated, and the fresh keys as ``op run``
        would inject them."""
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.old_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "CONCH_ANTHROPIC_API_BASE": self.anthropic.v1,
            "NO_COLOR": "1",
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TERM": "dumb",
        }
        env.update(extra)
        return env

    def run(self, command: str, *, env: dict, cwd: Path, timeout: float = 240.0) -> subprocess.CompletedProcess:
        self.say(f"\n$ (cd {cwd.name} && {command})")
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", command], cwd=str(cwd), env=env,
                                capture_output=True, text=True, timeout=timeout)
        for line in (result.stdout + result.stderr).splitlines():
            self.say("  " + line)
        self.say(f"  (exit {result.returncode})")
        return result

    # -- the drill ------------------------------------------------------------

    def step_git_pull(self) -> None:
        if not self.clone:
            self.say("\n== 1. git pull (skipped: --no-clone, running the checkout directly)")
            return
        self.say("\n== 1. git pull")
        result = self.run("git pull --ff-only --quiet && git log --oneline -1", env=self.env(), cwd=self.checkout)
        self.expect(result.returncode == 0, "git pull fast-forwarded the scratch clone")
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        self.expect(head in result.stdout, f"clone is now at {head}")

    def step_rotate(self) -> None:
        self.say("\n== 2. ./bin/conch keys rotate --from-env --yes   (bash non-login, no PATH setup, fresh keys in env)")
        env = self.env(ANTHROPIC_API_KEY=FRESH_ANTHROPIC, MCP_HOSTED_TOKEN=MCP_TOKEN)
        result = self.run("./bin/conch keys rotate --from-env --yes", env=env, cwd=self.checkout)
        out = result.stdout + result.stderr
        first = out.splitlines()[0] if out else ""
        self.expect(first.startswith("conch ") and "install: checkout" in first, f"identity line first: {first}")
        self.expect("0.4.0" in out and "`conch` resolves here" in out, "doctor says the old 0.4.0 shim is what `conch` resolves to")
        self.expect("a colleague typing `conch` gets the other copy" in out or "running now" in out,
                    "doctor says which copy this command is running")
        self.expect("created " in out and "(0600)" in out, "doctor created the missing env file 0600")
        self.expect(".zshrc:4: STALE" in out, "doctor names the stale export at ~/.zshrc:4")
        self.expect("conch overrides it" in out, "…and says the saved key wins over it")
        self.expect("inline secret" in out and "migrate-mcp" in out, "doctor flags the inline mcp.json secret with the migrate fix")
        self.expect("predates `conch keys`" in out or "predates conch keys" in out, "doctor names the old-protocol daemon")
        self.expect("launchctl kickstart" in out or "systemctl --user restart" in out, "…with the restart line for this OS")
        self.expect("saved ANTHROPIC_API_KEY" in out, "rotate imported the fresh ANTHROPIC_API_KEY from the environment")
        self.expect("saved MCP_HOSTED_TOKEN" in out, "rotate imported MCP_HOSTED_TOKEN from the environment")
        self.expect(result.returncode == 1 and "not green yet" in out, "rotate ends NOT green while the daemon is old, with named remaining steps")
        env_file = self.home / ".config" / "conch" / "env"
        self.expect(env_file.exists() and (env_file.stat().st_mode & 0o777) == 0o600, "env file exists, mode 0600")
        text = env_file.read_text()
        self.expect(f"ANTHROPIC_API_KEY={FRESH_ANTHROPIC}" in text and f"MCP_HOSTED_TOKEN={MCP_TOKEN}" in text,
                    "env file holds both fresh keys")
        manifest = json.loads((self.home / ".config" / "conch" / "env.manifest.json").read_text())
        self.expect(set(manifest["keys"]) >= {"ANTHROPIC_API_KEY", "MCP_HOSTED_TOKEN"}, "both keys are conch-managed")

    def step_remaining(self) -> None:
        self.say("\n== 3. the named remaining steps")
        env = self.env(ANTHROPIC_API_KEY=STALE_ANTHROPIC)  # the shell's stale export is still there
        result = self.run("./bin/conch keys migrate-mcp --apply --yes", env=env, cwd=self.checkout)
        self.expect(result.returncode == 0 and "backup:" in result.stdout, "migrate-mcp moved the inline secret and backed mcp.json up")
        rewritten = json.loads((self.home / ".config" / "conch" / "mcp.json").read_text())
        self.expect(rewritten["mcpServers"]["atlassian"]["env"]["JIRA_API_TOKEN"] == "${JIRA_API_TOKEN}",
                    "mcp.json now references ${JIRA_API_TOKEN}")
        self.say("\n  (colleague restarts the daemon — here: the real conch-edge code is started in this HOME)")
        self.old_daemon.stop()
        self.new_daemon = self._start_real_daemon()
        self.expect(self.new_daemon is not None, "real edge daemon up on the control socket")

    def _start_real_daemon(self):
        os.environ.update(self.env())
        os.environ.pop("ANTHROPIC_API_KEY", None)
        from conch import config as config_mod
        from conch.kernel.daemon import EdgeDaemon

        config_mod._ENV_LOAD_STATE["from_file"].clear()
        config = config_mod.load_config()
        config.update({"mission_reviews": "false", "mission_consolidation": "false"})
        daemon = EdgeDaemon(config, session_factory=lambda *a: ("", {}))
        daemon.start()
        thread = threading.Thread(target=daemon.run_forever, daemon=True)
        thread.start()
        self._daemon_obj = daemon
        return daemon

    def step_check(self) -> None:
        self.say("\n== 4. ./bin/conch keys check   (stale export still in the colleague's shell)")
        env = self.env(ANTHROPIC_API_KEY=STALE_ANTHROPIC)
        result = self.run("./bin/conch keys check", env=env, cwd=self.checkout)
        out = result.stdout
        self.expect(result.returncode == 0, "check exits 0")
        self.expect("✓ every credential the active config needs verified (shell and daemon)" in out, "check is green in the shell AND the daemon column")
        self.expect("ok (same key)" in out, "daemon column: ok (same key)")
        self.expect("shell-env value: rejected" in out, "the stale shell export is called out as rejected while the saved key is used")
        self.say("\n== 5. python3 -m conch keys check   (zero-assumption fallback)")
        result = self.run("python3 -m conch keys check", env=env, cwd=self.checkout)
        self.expect(result.returncode == 0 and "via python -m conch" in result.stdout, "python3 -m conch keys check is green and identifies itself")
        self.say("\n== 6. the old shim is still what `conch` resolves to — doctor keeps saying so")
        result = self.run("./bin/conch keys doctor", env=env, cwd=self.checkout)
        self.expect("old/bin/conch" in result.stdout and "0.4.0" in result.stdout, "doctor still names the old shim")
        self.expect("answers keys.status" in result.stdout, "doctor sees the restarted daemon as current")

    def teardown(self) -> None:
        try:
            self.old_daemon.stop()
        except Exception:
            pass
        daemon = getattr(self, "_daemon_obj", None)
        if daemon is not None:
            daemon.request_stop()
            time.sleep(0.3)
        for server in (getattr(self, "anthropic", None), getattr(self, "services", None)):
            if server is not None:
                try:
                    server.stop()
                except Exception:
                    pass
        if not self.keep and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    def main(self) -> int:
        try:
            self.build()
            self.step_git_pull()
            self.step_rotate()
            self.step_remaining()
            self.step_check()
        finally:
            self.teardown()
        self.say("\n== result")
        if self.failures:
            for failure in self.failures:
                self.say(f"  FAIL: {failure}")
            return 1
        self.say("  all expectations held: the two-line instruction ends green on the weird machine")
        return 0


class OldProtocolDaemon:
    """A control socket that answers like the daemon shipped before
    ``conch keys``: ``status`` works, anything else is an unknown op."""

    def __init__(self, path: Path):
        self.path = path
        self._stop = threading.Event()

    def start(self) -> "OldProtocolDaemon":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            self.path.unlink()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self._server.listen(4)
        self._server.settimeout(0.3)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    raw = conn.recv(65536)
                    request = json.loads(raw.decode() or "{}")
                except Exception:
                    continue
                op = request.get("op")
                if op == "status":
                    body = {"v": 1, "ok": True, "result": {"holder": f"edge-oldhost-{os.getpid()}", "epoch": 3,
                                                           "uptime_seconds": 777777, "missions": {}}}
                else:
                    body = {"v": 1, "ok": False, "error": f"unknown control op {op!r}"}
                try:
                    conn.sendall((json.dumps(body) + "\n").encode())
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
        try:
            self.path.unlink()
        except OSError:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default="", help="where to build the weird HOME (default: a temp dir)")
    parser.add_argument("--no-clone", action="store_true", help="run this checkout instead of a scratch clone + git pull")
    parser.add_argument("--keep", action="store_true", help="leave the weird HOME in place afterwards")
    args = parser.parse_args(argv)
    # /tmp, not $TMPDIR: the daemon's AF_UNIX socket path must stay short (macOS caps it at 104 bytes).
    root = Path(args.root) if args.root else Path(tempfile.mkdtemp(prefix="conch-keys-", dir="/tmp"))
    return Drill(root.resolve(), clone=not args.no_clone, keep=args.keep).main()


if __name__ == "__main__":
    sys.exit(main())
