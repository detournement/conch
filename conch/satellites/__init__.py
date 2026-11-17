"""Capture satellites: discrete non-Python companions that ship inside
the conch-shell distribution as package data.

A satellite is not conch code — it runs somewhere else (a browser, some
day an editor or a phone) and talks to the kernel only through the
sanctioned transports (native messaging into ``conch-capture-host``,
``event.post`` on the control socket). Keeping the static files here,
under the package, is what makes ``/install capture browser`` work for
pip/uv/pipx installs: the wheel carries them, and setup copies them to
a stable per-user directory the browser can load.
"""
