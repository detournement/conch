"""Foundation-owned installers for optional Conch product distributions."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import urllib.parse
from importlib import metadata
from typing import List, Optional

from .secretguard import credential_findings

WORKS_DISTRIBUTION = "conch-works"
DEFAULT_WORKS_PACKAGE_SPEC = (
    "conch-works @ git+ssh://git@github.com/detournement/conch-works.git@main"
)


def _distribution_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


def _works_entrypoint_active() -> bool:
    from .plugins import loaded_plugin_entrypoints

    return any(
        identity.startswith("works=") for identity in loaded_plugin_entrypoints()
    )


def works_status(config: dict) -> str:
    version = _distribution_version(WORKS_DISTRIBUTION)
    if not version:
        return "package absent — /install works installs the optional module"
    required = ("capitol_base_url", "capitol_org", "capitol_agent")
    missing = [key for key in required if not str(config.get(key) or "").strip()]
    if missing:
        return (
            f"installed-unconfigured (conch-works v{version}; missing "
            + ", ".join(missing)
            + ")"
        )
    if not _works_entrypoint_active():
        return (
            f"configured (conch-works v{version}) — restart Conch to "
            "activate its plugin"
        )
    env_name = str(config.get("capitol_bearer_env") or "CAPITOL_A2A_BEARER").strip()
    credential = (
        "credential env available"
        if os.environ.get(env_name)
        else f"credential by reference ({env_name})"
    )
    return f"healthy (conch-works v{version}; plugin active; {credential})"


def _safe_package_spec(config: dict, override: str = "") -> str:
    spec = (
        str(override or "").strip()
        or str(os.environ.get("CONCH_WORKS_PACKAGE_SPEC") or "").strip()
        or str(config.get("works_package_spec") or "").strip()
        or DEFAULT_WORKS_PACKAGE_SPEC
    )
    if credential_findings(spec):
        raise ValueError(
            "Works package spec looks credential-bearing; use GitHub SSH/"
            "gh authentication or a credential helper, never a token in "
            "the package URL"
        )
    parsed = urllib.parse.urlsplit(spec.split("@", 1)[-1].strip())
    if parsed.scheme in ("http", "https") and (parsed.username or parsed.password):
        raise ValueError("Works package spec must not contain URL credentials")
    return spec


def _installer_command(spec: str) -> List[str]:
    probe = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return [sys.executable, "-m", "pip", "install", spec]
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, spec]
    raise RuntimeError(
        "this Conch environment has neither pip nor uv; install conch-works "
        "into the same environment manually, then restart Conch"
    )


def _install_works(config: dict, spec: str) -> bool:
    print(
        "\n  Installing the optional conch-works module into this Conch "
        "Python environment."
    )
    print(
        "  \033[2mPrivate GitHub access uses your existing Git/gh SSH "
        "authentication; no token is passed in argv or stored by Conch."
        "\033[0m"
    )
    try:
        command = _installer_command(spec)
    except RuntimeError as exc:
        print(f"\n  \033[31m{exc}\033[0m\n")
        return False
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr or result.stdout or "unknown installer error"
        detail = detail[-1600:]
        print(
            "\n  \033[31mconch-works install failed.\033[0m\n"
            "  \033[2mConfirm `gh auth status` / SSH access to the private "
            "detournement/conch-works repository, or set "
            "CONCH_WORKS_PACKAGE_SPEC to an accessible wheel/package spec."
            "\033[0m\n"
            f"  {detail}\n"
        )
        return False
    importlib.invalidate_caches()
    from .plugins import load_builtin_plugins

    load_builtin_plugins(refresh=True)
    print("\n  \033[1;32m✓ conch-works installed.\033[0m")
    return True


def _configure_works(config: dict) -> None:
    from .config import set_config_values

    print(
        "\n  \033[1mWorks configuration\033[0m — connect the optional module "
        "to a Capitol organization.\n  Only an environment-variable name "
        "is stored for credentials; bearer bytes never enter config."
    )
    current_workflow = str(config.get("capitol_base_url") or "").strip()
    current_platform = str(config.get("capitol_platform_url") or "").strip()
    current_org = str(config.get("capitol_org") or "").strip()
    current_agent = str(config.get("capitol_agent") or "").strip()
    current_env = str(config.get("capitol_bearer_env") or "CAPITOL_A2A_BEARER").strip()
    try:
        workflow_url = (
            input(
                f"  Workflow gateway URL"
                f"{f' [{current_workflow}]' if current_workflow else ''}: "
            ).strip()
            or current_workflow
        )
        platform_url = (
            input(
                f"  Platform API URL"
                f"{f' [{current_platform}]' if current_platform else ''}: "
            ).strip()
            or current_platform
        )
        org = (
            input(
                f"  Organization id{f' [{current_org}]' if current_org else ''}: "
            ).strip()
            or current_org
        )
        agent = (
            input(
                f"  Agent id{f' [{current_agent}]' if current_agent else ''}: "
            ).strip()
            or current_agent
        )
        env_name = (
            input(f"  Bearer environment variable [{current_env}]: ").strip()
            or current_env
        )
    except (EOFError, KeyboardInterrupt):
        print("\n  \033[2mWorks setup cancelled.\033[0m\n")
        return
    if not workflow_url or not org or not agent:
        print(
            "\n  \033[33mWorks remains installed-unconfigured:\033[0m "
            "workflow URL, org, and agent are required.\n"
        )
        return
    updates = {
        "capitol_base_url": workflow_url,
        "capitol_org": org,
        "capitol_agent": agent,
        "capitol_bearer_env": env_name,
    }
    if platform_url:
        updates["capitol_platform_url"] = platform_url
    path = set_config_values(updates)
    config.update(updates)
    print(
        f"\n  \033[1;32m✓ Works configured.\033[0m \033[2m({path})\033[0m\n"
        f"  \033[2mExport {env_name} or keep the bearer in the existing "
        "A2Actrl registry. Procedure REST reads additionally accept the "
        "existing CAPITOL_ADMIN_TOKEN/x_user_token reference. Restart "
        "Conch if /capitol is not yet visible.\033[0m\n"
    )


def setup_works(config: dict, args: Optional[List[str]] = None) -> None:
    args = list(args or [])
    override = ""
    while args:
        token = args.pop(0)
        if token == "--package" and args:
            override = args.pop(0)
            continue
        print(
            f"\n  \033[31mUnknown Works install option {token!r}.\033[0m "
            "\033[2mUse /install works [--package SPEC].\033[0m\n"
        )
        return
    if not _distribution_version(WORKS_DISTRIBUTION):
        try:
            spec = _safe_package_spec(config, override)
        except ValueError as exc:
            print(f"\n  \033[31m{exc}\033[0m\n")
            return
        if not _install_works(config, spec):
            return
    _configure_works(config)


def capture_status(config: dict) -> str:
    from .config import get_bool

    if not get_bool(config, "capture_enabled", False):
        return "disabled — /install capture enables local evidence capture"
    suffix = (
        "compile/materialize available"
        if _distribution_version(WORKS_DISTRIBUTION)
        else "collection enabled; /install works adds compile/materialize"
    )
    return f"enabled — {suffix}"


def setup_capture(config: dict, args: Optional[List[str]] = None) -> None:
    from .config import set_config_values

    args = [str(value).lower() for value in (args or [])]
    if args and args != ["browser"]:
        print(
            f"\n  \033[31mUnknown capture step {args[0]!r}.\033[0m "
            "\033[2mUse /install capture or /install capture browser."
            "\033[0m\n"
        )
        return
    updates = {"capture_enabled": "true"}
    if args == ["browser"]:
        from .kernel.browser_capture import (
            install_extension_files,
            install_native_host,
        )

        extension = install_extension_files()
        outcome = install_native_host(
            str(config.get("capture_browser_extension_id") or "")
        )
        updates["capture_browser"] = "true"
        print(f"\n  Browser extension files: {extension}")
        for browser, path in sorted(outcome["written"].items()):
            print(f"    {browser:<9} {path}")
    path = set_config_values(updates)
    config.update(updates)
    print(f"\n  \033[1;32m✓ Capture enabled.\033[0m \033[2m({path})\033[0m")
    if not _distribution_version(WORKS_DISTRIBUTION):
        print(
            "  \033[2mLocal journals/browser evidence can be collected "
            "without Works. Capture→Architecture Card, /compile, and "
            "materialization require the optional module: /install works."
            "\033[0m\n"
        )
    else:
        print(
            "  \033[2mWorks is installed; use /compile from-session, "
            "from-mission, from-history, or from-browser.\033[0m\n"
        )
