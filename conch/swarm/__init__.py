"""Conch swarm subsystems (dormant until their phases land).

Phase 0 ships only the versioned wire-protocol foundations in
:mod:`conch.swarm.protocol`. The interactive shell never imports this
package; nothing here touches the network, the filesystem, or credentials.
"""
