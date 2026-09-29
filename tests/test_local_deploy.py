from __future__ import annotations

import unittest

from scripts.local_deploy import version_matches


class LocalDeployTests(unittest.TestCase):
    def test_version_matches_full_and_abbreviated_expected_revision(self) -> None:
        expected = "17f2ae70e4a80c02f9c82a59263d9f8d528fc64d"

        self.assertTrue(version_matches(expected, expected))
        self.assertTrue(version_matches("17f2ae7", expected))
        self.assertTrue(version_matches("17F2AE70E4A8", expected))

    def test_version_rejects_short_invalid_or_different_values(self) -> None:
        expected = "17f2ae70e4a80c02f9c82a59263d9f8d528fc64d"

        self.assertFalse(version_matches("17f2ae", expected))
        self.assertFalse(version_matches("17f2ae!", expected))
        self.assertFalse(version_matches("4ac465f", expected))
        self.assertFalse(version_matches("", expected))


if __name__ == "__main__":
    unittest.main()
