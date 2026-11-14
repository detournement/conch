"""Process boundary for provider-owned subscription clients; no token handling."""

from __future__ import annotations

import math
import os
import signal
import subprocess


class SeatError(RuntimeError):
    """A native subscription client could not complete the request."""


def environment() -> dict:
    """Keep native credential stores, but never let API credentials select billing."""
    env = dict(os.environ)
    for name in tuple(env):
        if (name.startswith(("ANTHROPIC_", "OPENAI_", "CLAUDE_CODE_", "CODEX_"))
                and name not in ("CODEX_HOME", "CLAUDE_CODE_USE_SYSTEM_KEYCHAIN")):
            env.pop(name, None)
    # The installed native CLI owns its login and refresh lifecycle.
    return env


def run(argv: list, *, prompt: str | None = None, timeout: float = 120,
        cwd: str | None = None) -> subprocess.CompletedProcess:
    """Bound a native client and its children. Prompts never enter process argv."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise SeatError("native client timeout must be a positive finite number")
    process = None
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=environment(), cwd=cwd,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(input=prompt, timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    except FileNotFoundError as exc:
        raise SeatError(f"{argv[0]} is not installed; install the official CLI first") from exc
    except subprocess.TimeoutExpired as exc:
        raise SeatError(f"{argv[0]} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise SeatError(f"could not launch {argv[0]} ({type(exc).__name__})") from exc
    finally:
        if process is not None:
            # The process may exit while leaving an MCP/helper descendant alive.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
