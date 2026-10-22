"""Which conch is this, and which conch will run? (install shapes)

A machine can carry several copies of conch at once: a git checkout on
PATH via ``bin/``, a ``uv tool`` install under ``~/.local/bin``, a stale
``pip install -e`` shim under ``/opt/homebrew/bin``, and whatever the
launchd agent or systemd unit points at. After ``git pull`` the first
question is "am I running what I just pulled?", so every ``conch keys``
command starts with an identity line::

    conch 0.7.1 (a1b2c3d) from /Users/me/conch — install: checkout

and ``conch keys doctor`` lists every other ``conch`` on PATH with its
version, commit, and shape, plus the one the daemon runs. Nothing here
executes a foreign ``conch`` binary (an old shim given an unknown flag
could do anything); shims are inspected statically and their package
imported with their own interpreter (``import conch`` has no side
effects).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import __version__

CHECKOUT = "checkout"
UV = "uv"
PIP_EDITABLE = "pip-editable"
PIP = "pip"
PIPX = "pipx"
UNKNOWN = "unknown"

_GIT_TIMEOUT = 3.0
_IMPORT_TIMEOUT = 15.0


def package_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def checkout_root(pkg_dir: Optional[Path] = None) -> Optional[Path]:
    """The repository root when *pkg_dir* is the package of a git
    checkout (``<root>/conch`` with ``<root>/.git`` or ``<root>/bin/conch``
    plus ``pyproject.toml``), else None."""
    pkg_dir = Path(pkg_dir or package_dir())
    root = pkg_dir.parent
    if (root / ".git").exists():
        return root
    if (root / "pyproject.toml").is_file() and (root / "bin" / "conch").is_file():
        return root
    return None


def _run(cmd: List[str], cwd: Optional[Path] = None,
         timeout: float = _GIT_TIMEOUT, env: Optional[dict] = None) -> str:
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True,
            text=True, timeout=timeout, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_commit(root: Path) -> Dict[str, Any]:
    """``{"commit": short sha, "dirty": bool, "committed_at": epoch}`` for
    a checkout (empty strings/0 when git is unavailable)."""
    commit = _run(["git", "rev-parse", "--short=7", "HEAD"], cwd=root)
    if not commit:
        return {"commit": "", "dirty": False, "committed_at": 0}
    dirty = bool(_run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root
    ))
    stamp = _run(["git", "log", "-1", "--format=%ct"], cwd=root)
    try:
        committed_at = int(stamp)
    except ValueError:
        committed_at = 0
    return {"commit": commit, "dirty": dirty, "committed_at": committed_at}


def install_source_record() -> Dict[str, str]:
    """``~/.local/state/conch/install-source`` as written by the website
    installer (``installed_at``, ``source``, ``sha256``, ``verified``,
    and ``commit`` when the installer could read it off the tarball)."""
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch"
    record: Dict[str, str] = {}
    try:
        for line in (state / "install-source").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                record[key.strip()] = value.strip()
    except OSError:
        pass
    return record


def _distribution_info(pkg_dir: Path) -> Dict[str, Any]:
    """Editable/direct-URL metadata for the ``conch-shell`` distribution
    *this interpreter* resolves — only trusted when it points at
    *pkg_dir* (a checkout may coexist with an unrelated pip -e shim)."""
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - py3.8
        return {}
    try:
        dist = metadata.distribution("conch-shell")
    except metadata.PackageNotFoundError:
        return {}
    info: Dict[str, Any] = {"version": dist.version}
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        raw = None
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        dir_info = data.get("dir_info") if isinstance(data, dict) else None
        if isinstance(dir_info, dict) and dir_info.get("editable"):
            info["editable"] = True
            info["editable_url"] = str(data.get("url") or "")
    locate = getattr(dist, "locate_file", None)
    if callable(locate):
        try:
            info["location"] = str(Path(str(locate(""))).resolve())
        except Exception:
            pass
    return info


def install_shape(pkg_dir: Optional[Path] = None) -> str:
    pkg_dir = Path(pkg_dir or package_dir())
    text = str(pkg_dir)
    if checkout_root(pkg_dir) is not None:
        info = _distribution_info(pkg_dir)
        url = str(info.get("editable_url") or "")
        if info.get("editable") and url.endswith(str(pkg_dir.parent)):
            return f"{CHECKOUT} (also pip-editable)"
        return CHECKOUT
    if "/uv/tools/" in text or "\\uv\\tools\\" in text:
        return UV
    if "/pipx/venvs/" in text:
        return PIPX
    if "site-packages" in text or "dist-packages" in text:
        return PIP
    info = _distribution_info(pkg_dir)
    if info.get("editable"):
        return PIP_EDITABLE
    return UNKNOWN


def install_identity() -> Dict[str, Any]:
    """Everything the identity line and ``doctor`` need about the copy of
    conch that is running: version, commit (checkout) or install record
    (installed copies), code path, shape, interpreter, how it was
    invoked."""
    pkg_dir = package_dir()
    root = checkout_root(pkg_dir)
    identity: Dict[str, Any] = {
        "version": __version__,
        "path": str(pkg_dir),
        "root": str(root or pkg_dir.parent),
        "shape": install_shape(pkg_dir),
        "python": sys.executable,
        "argv0": sys.argv[0] if sys.argv else "",
        "commit": "",
        "dirty": False,
        "committed_at": 0,
        "installed_at": "",
        "source": "",
    }
    if root is not None:
        identity.update(git_commit(root))
    else:
        record = install_source_record()
        identity["commit"] = record.get("commit", "")
        identity["installed_at"] = record.get("installed_at", "")
        identity["source"] = record.get("source", "")
    return identity


def identity_line(identity: Optional[Dict[str, Any]] = None) -> str:
    """``conch <version> (<commit>) from <path> — install: <shape>``."""
    identity = identity or install_identity()
    commit = identity.get("commit") or ""
    if commit:
        commit += "+dirty" if identity.get("dirty") else ""
    elif identity.get("installed_at"):
        commit = f"commit unknown; installed {str(identity['installed_at'])[:10]}"
        if identity.get("source"):
            commit += f" from {_short_source(str(identity['source']))}"
    else:
        commit = "commit unknown"
    invoked = ""
    argv0 = str(identity.get("argv0") or "")
    if argv0.endswith("__main__.py") or argv0 == "-m":
        invoked = " via python -m conch"
    return (
        f"conch {identity.get('version')} ({commit}) from"
        f" {identity.get('root')} — install: {identity.get('shape')}{invoked}"
    )


def _short_source(source: str) -> str:
    match = re.search(r"github\.com/([^/]+/[^/]+)/archive/refs/(heads|tags)/([^/]+)\.tar\.gz", source)
    if match:
        return f"github {match.group(1)} {match.group(3)}"
    return source[:60]


# ---------------------------------------------------------------------------
# Other copies on PATH, and the one the daemon runs
# ---------------------------------------------------------------------------

_SHEBANG_RE = re.compile(r"^#!\s*(\S+)(?:\s+(\S+))?")


def _probe_interpreter(python: str, extra_path: str = "",
                       timeout: float = _IMPORT_TIMEOUT) -> Dict[str, Any]:
    """Import ``conch`` with *python* (``PYTHONPATH=extra_path`` when the
    shim adds one) and report its version and location. Side-effect
    free: ``conch/__init__`` only defines the version."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
    }
    if extra_path:
        env["PYTHONPATH"] = extra_path
    snippet = (
        "import json, conch, os; print(json.dumps({'version': conch.__version__,"
        " 'file': os.path.dirname(os.path.abspath(conch.__file__))}))"
    )
    out = _run([python, "-c", snippet], cwd=Path("/"), timeout=timeout, env=env)
    if not out:
        return {}
    try:
        data = json.loads(out.splitlines()[-1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def describe_binary(path: Path) -> Dict[str, Any]:
    """Static description of one ``conch`` executable: resolved target,
    interpreter, the package it imports (version, path, shape, commit)."""
    path = Path(path)
    info: Dict[str, Any] = {
        "path": str(path), "resolved": str(path), "python": "",
        "package": "", "version": "", "shape": UNKNOWN, "commit": "",
        "error": "",
    }
    try:
        resolved = path.resolve()
        info["resolved"] = str(resolved)
        head = resolved.read_bytes()[:4096]
    except OSError as exc:
        info["error"] = f"unreadable: {exc}"
        return info
    text = head.decode("utf-8", "replace")
    first = text.splitlines()[0] if text else ""
    match = _SHEBANG_RE.match(first)
    python = ""
    if match:
        candidate = match.group(1)
        if candidate.endswith("/env") and match.group(2):
            candidate = match.group(2)
        python = candidate
    if not python or not re.search(r"python", python):
        info["error"] = "not a Python launcher"
        return info
    info["python"] = python
    extra = ""
    root_hint = resolved.parent.parent
    if "sys.path.insert(0, str(ROOT))" in text and (root_hint / "conch" / "__init__.py").is_file():
        extra = str(root_hint)  # our bin/conch shim: it adds its checkout root
    probe = _probe_interpreter(python, extra)
    if not probe:
        info["error"] = f"{python} cannot import conch"
        return info
    pkg = Path(str(probe.get("file") or ""))
    info["package"] = str(pkg)
    info["version"] = str(probe.get("version") or "")
    root = checkout_root(pkg)
    if root is not None:
        info["commit"] = git_commit(root).get("commit", "")
    # The *binary's* shape: our bin/conch inside the checkout, a uv/pipx
    # tool shim, or a console script (pip -e points one at a checkout).
    resolved_text = str(resolved)
    if extra:
        info["shape"] = CHECKOUT
    elif "/uv/tools/" in resolved_text or "/uv/tools/" in str(pkg):
        info["shape"] = UV
    elif "/pipx/venvs/" in resolved_text or "/pipx/venvs/" in str(pkg):
        info["shape"] = PIPX
    elif root is not None:
        info["shape"] = f"{PIP_EDITABLE} → {root}"
    elif pkg.name == "conch":
        info["shape"] = install_shape(pkg)
    return info


def binaries_on_path(name: str = "conch",
                     path_value: Optional[str] = None) -> List[Path]:
    """Every executable *name* on PATH, first match first (what the
    shell would run is index 0)."""
    found: List[Path] = []
    seen = set()
    for entry in (path_value if path_value is not None else os.environ.get("PATH", "")).split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / name
        try:
            if not (candidate.is_file() and os.access(candidate, os.X_OK)):
                continue
            key = str(candidate.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        found.append(candidate)
    return found


def same_package(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


def daemon_program() -> Dict[str, Any]:
    """What the OS supervisor runs for the edge daemon — parsed from the
    launchd plist (macOS) or the systemd user unit (Linux): the program
    argv, PYTHONPATH when set, and the described package it imports.
    ``{}`` when neither unit exists."""
    try:
        from ..kernel.install import launchd_plist_path, systemd_unit_path
    except Exception:
        return {}
    result: Dict[str, Any] = {}
    plist = launchd_plist_path()
    unit = systemd_unit_path()
    if plist.is_file():
        result = _parse_launchd(plist)
        result["supervisor"] = "launchd"
        result["unit_path"] = str(plist)
    elif unit.is_file():
        result = _parse_systemd(unit)
        result["supervisor"] = "systemd"
        result["unit_path"] = str(unit)
    else:
        return {}
    argv = result.get("argv") or []
    env = result.get("env") or {}
    package: Dict[str, Any] = {}
    if argv:
        first = str(argv[0])
        if re.search(r"python", Path(first).name):
            probe = _probe_interpreter(first, env.get("PYTHONPATH", ""))
            if probe:
                pkg = Path(str(probe.get("file") or ""))
                package = {
                    "python": first, "package": str(pkg),
                    "version": str(probe.get("version") or ""),
                    "shape": install_shape(pkg),
                    "commit": (git_commit(checkout_root(pkg)).get("commit", "")
                               if checkout_root(pkg) else ""),
                }
        else:
            described = describe_binary(Path(first))
            package = {
                key: described.get(key, "")
                for key in ("python", "package", "version", "shape", "commit")
            }
            if described.get("error"):
                package["error"] = described["error"]
    result["runs"] = package
    return result


def _parse_launchd(path: Path) -> Dict[str, Any]:
    try:
        import plistlib

        data = plistlib.loads(path.read_bytes())
    except Exception as exc:
        return {"error": f"cannot parse {path}: {exc}"}
    return {
        "argv": [str(arg) for arg in data.get("ProgramArguments") or []],
        "env": {str(k): str(v) for k, v in (data.get("EnvironmentVariables") or {}).items()},
        "label": str(data.get("Label") or ""),
    }


def _parse_systemd(path: Path) -> Dict[str, Any]:
    import shlex

    argv: List[str] = []
    env: Dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError as exc:
        return {"error": f"cannot read {path}: {exc}"}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ExecStart="):
            try:
                argv = shlex.split(line[len("ExecStart="):])
            except ValueError:
                argv = line[len("ExecStart="):].split()
        elif line.startswith("Environment="):
            value = line[len("Environment="):].strip().strip('"')
            if "=" in value:
                key, val = value.split("=", 1)
                env[key] = val
    return {"argv": argv, "env": env}


def restart_daemon_command() -> str:
    """The restart line for this platform's supervisor."""
    if sys.platform == "darwin":
        return "launchctl kickstart -k gui/$(id -u)/com.conch.edge"
    if sys.platform.startswith("linux"):
        return "systemctl --user restart conch-edge"
    return "restart conch-edge"


def process_start_time(pid: int) -> float:
    """Epoch seconds a process started (0 when unknown), via ``ps``."""
    out = _run(["ps", "-o", "lstart=", "-p", str(int(pid))])
    if not out:
        return 0.0
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %d %b %H:%M:%S %Y"):
        try:
            return time.mktime(time.strptime(" ".join(out.split()), fmt))
        except ValueError:
            continue
    return 0.0
