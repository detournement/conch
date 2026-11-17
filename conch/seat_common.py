"""Process boundary for provider-owned subscription clients; no token handling."""

from __future__ import annotations

import codecs
import io
import math
import os
import selectors
import signal
import subprocess
import time


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


def _stream(process, prompt, timeout, on_stdout_line):
    """Multiplex all three pipes; callbacks stay on the invoking UI thread."""
    deadline = time.monotonic() + timeout
    output = {"stdout": [], "stderr": []}
    pending = ""
    decoders = {
        name: io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder(getattr(process, name).encoding)(), True)
        for name in output
    }
    data = (prompt or "").encode(process.stdin.encoding)
    position = 0
    with selectors.DefaultSelector() as selector:
        for name in output:
            pipe = getattr(process, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        if data:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for key, _ in selector.select(remaining):
                pipe, name = key.fileobj, key.data
                if name == "stdin":
                    try:
                        position += os.write(pipe.fileno(), data[position:position + 4096])
                    except BrokenPipeError:
                        position = len(data)
                    except BlockingIOError:
                        continue
                    if position == len(data):
                        selector.unregister(pipe)
                        pipe.close()
                    continue
                try:
                    chunk = os.read(pipe.fileno(), 65536)
                except BlockingIOError:
                    continue
                text = decoders[name].decode(chunk, final=not chunk)
                output[name].append(text)
                if not chunk:
                    selector.unregister(pipe)
                    pipe.close()
                if name == "stdout":
                    pending += text
                    lines = pending.split("\n")
                    pending = lines.pop()
                    for line in lines:
                        on_stdout_line(line + "\n")
                    if not chunk and pending:
                        on_stdout_line(pending)
                        pending = ""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        process.wait(timeout=remaining)
    return "".join(output["stdout"]), "".join(output["stderr"])


def run(argv: list, *, prompt: str | None = None, timeout: float = 120,
        cwd: str | None = None, on_stdout_line=None) -> subprocess.CompletedProcess:
    """Bound the process group; optionally deliver stdout lines before exit.

    The callback runs synchronously on the caller's thread, never a pipe-reader
    worker. Stderr is drained concurrently but never sent to the callback.
    Prompts never enter process argv.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise SeatError("native client timeout must be a positive finite number")
    process = None
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=environment(), cwd=cwd,
            start_new_session=True,
        )
        if on_stdout_line is None:
            stdout, stderr = process.communicate(input=prompt, timeout=timeout)
        else:
            stdout, stderr = _stream(process, prompt, timeout, on_stdout_line)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    except FileNotFoundError as exc:
        raise SeatError(f"{argv[0]} is not installed; install the official CLI first") from exc
    except subprocess.TimeoutExpired as exc:
        raise SeatError(f"{argv[0]} timed out after {timeout:g}s") from exc
    except UnicodeError as exc:
        raise SeatError(f"{argv[0]} returned invalid text encoding") from exc
    except OSError as exc:
        raise SeatError(f"could not launch {argv[0]} ({type(exc).__name__})") from exc
    finally:
        if process is not None:
            # The process may exit while leaving an MCP/helper descendant alive.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Already gone; macOS reports an unreaped group as EPERM.
                pass
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
            process.wait()
