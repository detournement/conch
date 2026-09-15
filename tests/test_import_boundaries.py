"""Import-direction gates for the product breakout.

The dependency direction the breakout establishes, enforced statically
(companion to the runtime no-daemon invariant in test_kernel_shell):

- **foundation** (the shell: app, session, tooling, bootstrap,
  commands, plugins, ...) imports neither product package —
  ``conch.capitol`` and ``conch.fleet`` plug in through the seams in
  ``conch.plugins``;
- **kernel** (``conch.kernel``) is a foundation library: it never
  imports the products either, at any nesting depth — supervision and
  envelope-scoped tools arrive through registered providers;
- foundation modules never import ``conch.kernel`` at module scope
  (mission/todo/notes commands attach lazily, so the classic shell
  stays kernel-free until a kernel surface is actually used).

Documented exemptions, to move in the packaging stage:

- ``conch/entrypoints.py`` — the CLI shim owns every console script,
  including the product daemons (conch-controller, conch-worker,
  conch-hostctl); those entry points migrate to the product packages
  when the distributions split.
- ``conch/remote.py`` — the remote/channel loop is edge-product code;
  its capitol integrations (origin-bound capitol_control, pack channel
  flows, capitol_start approvals) are the works→edge seam, deferred.
"""

import ast
import subprocess
import sys
import unittest
from pathlib import Path

CONCH_ROOT = Path(__file__).resolve().parent.parent / "conch"

PRODUCT_PACKAGES = ("conch.capitol", "conch.fleet")

# Foundation exemptions for the product-import gate (see module docstring).
FOUNDATION_EXEMPT = {"entrypoints.py", "remote.py"}


def module_name(path: Path) -> str:
    """Dotted module name for a file under the conch package."""
    relative = path.relative_to(CONCH_ROOT.parent)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def resolve_relative(package_parts, level: int, module: str) -> str:
    """Absolute dotted target of a relative import from a package."""
    base = package_parts[: len(package_parts) - (level - 1)] if level else []
    target = list(base)
    if module:
        target.extend(module.split("."))
    return ".".join(target)


def import_targets(path: Path, module_scope_only: bool = False):
    """Every dotted import target in a file (absolute form).

    With ``module_scope_only`` we walk only the module body, so lazy
    function-level imports are permitted; otherwise the whole tree is
    scanned (imports at any depth count).
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    name = module_name(path)
    package_parts = name.split(".")
    if path.name != "__init__.py":
        package_parts = package_parts[:-1]
    nodes = tree.body if module_scope_only else list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                yield resolve_relative(
                    package_parts, node.level, node.module or ""
                )
            else:
                yield node.module or ""


def hits(targets, forbidden) -> list:
    return sorted(
        target for target in set(targets)
        if any(
            target == prefix or target.startswith(prefix + ".")
            for prefix in forbidden
        )
    )


class TestKernelNeverImportsProducts(unittest.TestCase):
    """The mission kernel is product-agnostic: no capitol/fleet imports
    at any depth (lazy included) — products register providers against
    conch.plugins instead."""

    def test_kernel_package(self):
        for path in sorted((CONCH_ROOT / "kernel").glob("*.py")):
            found = hits(import_targets(path), PRODUCT_PACKAGES)
            self.assertEqual(
                found, [],
                f"{path.name} imports product packages: {found}",
            )


class TestFoundationNeverImportsProducts(unittest.TestCase):
    """Foundation modules (conch/*.py) never import capitol/fleet at
    any depth. entrypoints.py and remote.py are documented exemptions
    (see module docstring) that move in the packaging stage."""

    def test_foundation_modules(self):
        for path in sorted(CONCH_ROOT.glob("*.py")):
            if path.name in FOUNDATION_EXEMPT:
                continue
            found = hits(import_targets(path), PRODUCT_PACKAGES)
            self.assertEqual(
                found, [],
                f"{path.name} imports product packages: {found}",
            )

    def test_exemptions_still_needed(self):
        """Prune the exemption list as modules get inverted."""
        for name in sorted(FOUNDATION_EXEMPT):
            path = CONCH_ROOT / name
            found = hits(import_targets(path), PRODUCT_PACKAGES)
            self.assertNotEqual(
                found, [],
                f"{name} no longer imports product packages — remove it"
                " from FOUNDATION_EXEMPT",
            )


class TestFoundationStaysKernelFreeAtImportTime(unittest.TestCase):
    """No foundation module imports conch.kernel at module scope: the
    static face of the no-daemon invariant (edge_daemon unset ⇒
    conch.kernel never imported). Lazy attach imports inside mission,
    todo, and notes command handlers remain fine."""

    def test_no_module_scope_kernel_imports(self):
        for path in sorted(CONCH_ROOT.glob("*.py")):
            found = hits(
                import_targets(path, module_scope_only=True),
                ("conch.kernel",),
            )
            self.assertEqual(
                found, [],
                f"{path.name} imports conch.kernel at module scope:"
                f" {found}",
            )


class TestPluginLoadingStaysCheap(unittest.TestCase):
    """Loading the plugin registrations must not load the products.

    Registration is metadata: the only product modules that may enter
    sys.modules are the two package docstrings and the plugin shims
    themselves. The adapters (capitol client/runtime, fleet delegate,
    the kernel) stay behind the config gates exactly as before the
    seam inversion.
    """

    def test_plugin_shims_only(self):
        script = (
            "import sys\n"
            "from conch.commands import slash_command_names\n"
            "names = slash_command_names()\n"
            "assert '/capitol' in names and '/fleet' in names, names\n"
            "allowed = {'conch.capitol', 'conch.capitol.plugin',\n"
            "           'conch.fleet', 'conch.fleet.plugin'}\n"
            "leaked = sorted(\n"
            "    name for name in sys.modules\n"
            "    if name.startswith(\n"
            "        ('conch.capitol', 'conch.fleet', 'conch.kernel')\n"
            "    ) and name not in allowed\n"
            ")\n"
            "assert not leaked, f'loaded beyond the shims: {leaked}'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(CONCH_ROOT.parent), capture_output=True, text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0,
            f"stdout: {result.stdout}\nstderr: {result.stderr}",
        )


class TestPluginRegistryIsLeafward(unittest.TestCase):
    """The seam registry itself must sit below everyone: no kernel or
    product imports at any depth (plugin modules are named as strings
    and imported only through load_builtin_plugins)."""

    def test_plugins_module(self):
        path = CONCH_ROOT / "plugins.py"
        found = hits(
            import_targets(path),
            ("conch.kernel",) + PRODUCT_PACKAGES,
        )
        self.assertEqual(found, [], f"plugins.py imports: {found}")


if __name__ == "__main__":
    unittest.main()
