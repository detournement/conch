"""The colleague drill as a test: ``scripts/keys_colleague_drill.py``
builds a deliberately weird machine under a temp HOME (older conch shim
first on PATH, stale export in ~/.zshrc, no env file, inline mcp.json
secret, old-protocol daemon), runs ``./bin/conch keys rotate --from-env
--yes`` from a bash non-login shell, performs the named remaining steps,
and ends green in both the shell and daemon columns — with fake services
only. The script asserts its own expectations; this test runs it the way
CI would (no clone: the checkout itself is exercised)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "keys_colleague_drill.py"


class TestColleagueDrill(unittest.TestCase):
    def test_two_line_instruction_ends_green(self):
        # /tmp keeps the drill's AF_UNIX socket path under the macOS 104-byte cap.
        with tempfile.TemporaryDirectory(prefix="ck-", dir="/tmp") as tmp:
            env = {key: value for key, value in os.environ.items()
                   if key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "TERM")}
            env["HOME"] = tmp  # the drill isolates HOME itself; never the real one
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--no-clone", "--root", str(Path(tmp) / "machine")],
                cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600,
            )
        transcript = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, transcript)
        for needle in (
            "install: checkout",
            "`conch` resolves here",
            ".zshrc:4: STALE",
            "created ",
            "migrate-mcp",
            "predates `conch keys`",
            "saved ANTHROPIC_API_KEY",
            "✓ every credential the active config needs verified (shell and daemon)",
            "via python -m conch",
            "all expectations held",
        ):
            self.assertIn(needle, transcript)
        self.assertNotIn("sk-ant-fresh", transcript)
        self.assertNotIn("sk-ant-stale", transcript)
        self.assertNotIn("<redacted-secret>", transcript)


if __name__ == "__main__":
    unittest.main()
