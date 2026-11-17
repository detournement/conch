"""``conch keys migrate-mcp`` — move inline secrets out of mcp.json.

An API token pasted straight into an ``env``/``headers`` block (or a
``?api_key=`` query parameter, or a ``--token X`` argument) is the
rotation trap: the next rotation means hand-editing JSON, and the file
is usually 0644. The migration rewrites each inline secret as a
``${NAME}`` reference (which the loader expands, loudly failing when
the variable is missing), saves the value into the env file as a
conch-managed key, backs the original up first, and tightens the mode.

Dry-run by default; ``--apply`` writes. Names and fingerprints only in
the output.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..config import key_fingerprint, read_env_file, set_env_values
from .inventory import InlineSecret, inline_secrets, load_mcp_servers, mcp_config_path


class MigrationPlan:
    def __init__(self, path: Path, servers: Dict[str, dict], secrets: List[InlineSecret]):
        self.path = path
        self.servers = servers
        self.secrets = secrets
        self.conflicts: List[str] = []
        existing = read_env_file()
        for secret in secrets:
            current = existing.get(secret.suggested_name)
            if current and current != secret.value:
                self.conflicts.append(
                    f"{secret.suggested_name} already exists in the env file with a different"
                    f" value ({key_fingerprint(current)} vs {key_fingerprint(secret.value)} in mcp.json)"
                )

    def lines(self) -> List[str]:
        out = []
        for secret in self.secrets:
            out.append(
                f"{secret.server} {secret.field}  → ${{{secret.suggested_name}}}"
                f"  (value {key_fingerprint(secret.value)}, {len(secret.value)} chars)"
            )
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mcp_json": str(self.path),
            "moves": [
                {"server": s.server, "field": s.field, "name": s.suggested_name,
                 "fingerprint": key_fingerprint(s.value)} for s in self.secrets
            ],
            "conflicts": list(self.conflicts),
        }


def plan_migration(path: Optional[Path] = None) -> MigrationPlan:
    path = Path(path or mcp_config_path())
    servers, error = load_mcp_servers(path)
    if error:
        raise ValueError(error)
    return MigrationPlan(path, servers, inline_secrets(servers))


def _rewrite(servers: Dict[str, dict], secrets: List[InlineSecret]) -> Dict[str, dict]:
    rewritten = json.loads(json.dumps(servers))  # deep copy, key order kept
    for secret in secrets:
        cfg = rewritten[secret.server]
        kind, _, rest = secret.field.partition(".")
        ref = f"${{{secret.suggested_name}}}"
        if kind == "env":
            cfg["env"][rest] = ref
        elif kind == "headers":
            current = str(cfg["headers"][rest])
            cfg["headers"][rest] = current.replace(secret.value, ref)
        elif secret.field.startswith("url?"):
            param = secret.field[len("url?"):]
            parts = urlsplit(str(cfg["url"]))
            query = [(k, ref if k == param and v == secret.value else v)
                     for k, v in parse_qsl(parts.query, keep_blank_values=True)]
            cfg["url"] = urlunsplit(parts._replace(query=urlencode(query, safe="${}")))
        elif secret.field.startswith("args["):
            index = int(secret.field[len("args["):-1])
            cfg["args"][index] = str(cfg["args"][index]).replace(secret.value, ref)
    return rewritten


def apply_migration(plan: MigrationPlan, *, now: Optional[float] = None) -> Dict[str, Any]:
    """Back up mcp.json (timestamped ``.bak-YYYYmmdd-HHMMSS``), write the
    secrets into the env file as managed keys, rewrite mcp.json with
    ``${NAME}`` references, chmod 600. Refuses on a name conflict."""
    if plan.conflicts:
        raise ValueError("refusing to migrate: " + "; ".join(plan.conflicts))
    if not plan.secrets:
        return {"backup": "", "moved": [], "mcp_json": str(plan.path)}
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    backup = plan.path.with_name(f"{plan.path.name}.bak-{stamp}")
    original = plan.path.read_bytes()
    fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(original)
    updates = {secret.suggested_name: secret.value for secret in plan.secrets}
    set_env_values(updates, set_by="conch keys migrate-mcp")
    try:
        document = json.loads(original.decode("utf-8"))
    except ValueError:
        document = {}
    document["mcpServers"] = _rewrite(plan.servers, plan.secrets)
    tmp = plan.path.with_name(f".{plan.path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(document, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(tmp), str(plan.path))
    try:
        os.chmod(str(plan.path), 0o600)
    except OSError:
        pass
    return {
        "backup": str(backup),
        "moved": [f"{s.server} {s.field} → ${{{s.suggested_name}}}" for s in plan.secrets],
        "mcp_json": str(plan.path),
    }
