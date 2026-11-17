"""Release-artifact packaging gates."""

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


class TestSourceDistributionManifest(unittest.TestCase):
    def test_changelog_is_included_in_source_distribution(self):
        changelog = REPO_ROOT / "CHANGELOG.md"
        self.assertTrue(changelog.is_file())
        directives = {
            tuple(line.split())
            for line in (REPO_ROOT / "MANIFEST.in").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn(("include", "CHANGELOG.md"), directives)


if __name__ == "__main__":
    unittest.main()
