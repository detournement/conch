"""Flow packs: declarative Capitol use cases run by one generic engine.

Conch's primary Capitol capability is generic, A2Actrl-parity control —
``CapitolRuntime``/``CapitolAdmin`` plus the ``/capitol`` shell surface.
Use cases (eBay listing, funding intake, …) are expressed as
``conch.flow_pack.v1`` JSON manifests — workflow bindings, deterministic
request templates, intakes, clarification relay, approval classes with
caps, cursors/dedupe, notifications — executed by the engine in
:mod:`conch.capitol.packs.engine`. Nothing use-case-specific is core;
packs are data and can never grant tools or authority (design:
``conch-capitol-control-design.md``).
"""

from .manifest import (  # noqa: F401
    PACK_SCHEMA,
    FlowPack,
    PackError,
    canonical_json,
    load_pack_data,
    pack_digest,
)
from .registry import (  # noqa: F401
    BUILTIN_DIR,
    approval_flow,
    channel_flows,
    list_pack_errors,
    list_packs,
    load_pack,
    load_pack_dir,
    pack_dirs,
    user_packs_dir,
)
from .state import PackState  # noqa: F401
