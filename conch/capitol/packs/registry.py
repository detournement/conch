"""Pack discovery/loading and the intake + approval-kind registries (E10).

Packs are directories carrying a ``pack.json`` manifest (schema
``conch.flow_pack.v1``) plus optional ``README.md``/``assets/``::

    ~/.config/conch/packs/<name>/pack.json     (user packs — win by name)
    conch/capitol/packs/data/<name>/pack.json  (packs shipped with conch)

Loading is fail-closed: a malformed manifest raises
:class:`~conch.capitol.packs.manifest.PackError` rather than silently
skipping behavior. ``pack_digest`` (canonical-JSON sha256) is the pack
pin.

The registries de-hardcode the core seams the eBay pilot leaked into
``conch/remote.py``: channel intake routing iterates every loaded pack's
``channel_message`` intake, and approval consumption dispatches on the
approval-store ``kind`` through the packs that declare it — nothing in
core names a use case.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .engine import PackChannelFlow
from .manifest import FlowPack, PackError, load_pack_data

BUILTIN_DIR = Path(__file__).resolve().parent / "data"
MANIFEST_NAME = "pack.json"


def user_packs_dir() -> Path:
    root = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    )
    return root / "conch" / "packs"


def pack_dirs() -> List[Path]:
    """Every pack directory, user packs first (they win by name)."""
    found: List[Path] = []
    seen = set()
    for root in (user_packs_dir(), BUILTIN_DIR):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name in seen:
                continue
            if (entry / MANIFEST_NAME).is_file():
                found.append(entry)
                seen.add(entry.name)
    return found


def load_pack_dir(directory: Path) -> FlowPack:
    manifest_path = Path(directory) / MANIFEST_NAME
    try:
        data = json.loads(manifest_path.read_text())
    except FileNotFoundError:
        raise PackError(f"no {MANIFEST_NAME} in {directory}") from None
    except (ValueError, OSError) as exc:
        raise PackError(
            f"pack manifest {manifest_path} is not valid JSON: {exc}"
        ) from None
    pack = load_pack_data(data, source=str(directory))
    if pack.name != Path(directory).name:
        raise PackError(
            f"pack directory {Path(directory).name!r} does not match "
            f"manifest pack.name {pack.name!r} (failing closed)"
        )
    return pack


def load_pack(name: str) -> FlowPack:
    """Load one pack by name (user dir wins over builtin)."""
    for directory in pack_dirs():
        if directory.name == name:
            return load_pack_dir(directory)
    raise PackError(
        f"no pack named {name!r} (looked in {user_packs_dir()} and the "
        "built-in packs)"
    )


def list_packs() -> List[FlowPack]:
    """Load every discoverable pack (fail closed on a broken manifest)."""
    return [load_pack_dir(directory) for directory in pack_dirs()]


def list_pack_errors() -> Dict[str, str]:
    """Manifest problems by pack directory name (for the pack surface)."""
    problems: Dict[str, str] = {}
    for directory in pack_dirs():
        try:
            load_pack_dir(directory)
        except PackError as exc:
            problems[directory.name] = str(exc)
    return problems


# ---------------------------------------------------------------------------
# Channel-intake + approval-kind registries (used by conch.remote)
# ---------------------------------------------------------------------------

def channel_flows(config: dict, approvals,
                  notify: Callable[[str, str, str], Any]
                  ) -> List[PackChannelFlow]:
    """One channel flow per loaded pack that declares a channel intake."""
    flows: List[PackChannelFlow] = []
    for pack in list_packs():
        if pack.intake("channel_message") is None:
            continue
        flows.append(PackChannelFlow(pack, config, approvals, notify))
    return flows


def approval_flow(kind: str, config: dict, approvals,
                  notify: Callable[[str, str, str], Any]
                  ) -> Optional[PackChannelFlow]:
    """The channel flow whose pack declares approval-store *kind*."""
    for pack in list_packs():
        if kind in pack.approval_kinds() and (
            pack.intake("channel_message") is not None
        ):
            return PackChannelFlow(pack, config, approvals, notify)
    return None
