"""Optional product-package installer safety and status states."""

import os
import unittest
from unittest.mock import patch

from conch.optional_packages import (
    DEFAULT_WORKS_PACKAGE_SPEC,
    _safe_package_spec,
    works_status,
)


class OptionalWorksTests(unittest.TestCase):
    def test_default_private_source_uses_ssh_without_secret(self):
        spec = _safe_package_spec({})
        self.assertEqual(spec, DEFAULT_WORKS_PACKAGE_SPEC)
        self.assertIn("git+ssh://git@github.com/", spec)
        self.assertNotIn("token", spec.lower())

    def test_package_override_rejects_credentialed_url(self):
        with self.assertRaisesRegex(ValueError, "credential"):
            _safe_package_spec(
                {},
                "conch-works @ "
                "https://ghp_abcdefghijklmnop@github.com/"
                "detournement/conch-works.git",
            )

    def test_environment_package_override_is_supported(self):
        with patch.dict(
            os.environ,
            {"CONCH_WORKS_PACKAGE_SPEC": "/tmp/conch_works.whl"},
        ):
            self.assertEqual(_safe_package_spec({}), "/tmp/conch_works.whl")

    def test_status_matrix(self):
        with patch(
            "conch.optional_packages._distribution_version",
            return_value="",
        ):
            self.assertIn("package absent", works_status({}))
        with patch(
            "conch.optional_packages._distribution_version",
            return_value="0.7.1",
        ), patch(
            "conch.optional_packages._works_entrypoint_active",
            return_value=False,
        ):
            self.assertIn(
                "installed-unconfigured", works_status({}),
            )
            configured = {
                "capitol_base_url": "http://localhost:8300",
                "capitol_org": "org-1",
                "capitol_agent": "agent-1",
            }
            self.assertIn("configured", works_status(configured))
        with patch(
            "conch.optional_packages._distribution_version",
            return_value="0.7.1",
        ), patch(
            "conch.optional_packages._works_entrypoint_active",
            return_value=True,
        ):
            self.assertIn(
                "healthy",
                works_status({
                    "capitol_base_url": "http://localhost:8300",
                    "capitol_org": "org-1",
                    "capitol_agent": "agent-1",
                }),
            )


if __name__ == "__main__":
    unittest.main()

