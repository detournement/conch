"""The browser-capture satellite: a Chrome MV3 extension (plain JS, no
build step) shipped as package data in ``extension/``.

Source of truth for the extension lives here — nothing is generated,
nothing is downloaded at runtime. ``conch.kernel.browser_capture``
resolves this directory (``extension_dir``) and copies it to a stable
user-data location (``install_extension_files``) so Chrome's
load-unpacked reference survives pip upgrades and venv reinstalls.
See ``README.md`` next to this file for the privacy contract and the
manual test checklist.
"""
