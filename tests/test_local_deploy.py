from __future__ import annotations

import unittest
import json
import socket
import threading

from scripts.local_deploy import verify_canonical_guard, version_matches


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

    def test_canonical_guard_requires_verified_one_shot_response(self) -> None:
        parent, child = socket.socketpair()

        def approve() -> None:
            try:
                self.assertEqual(parent.recv(32), b"verify\n")
                parent.sendall(
                    json.dumps(
                        {
                            "ok": True,
                            "qualification": {
                                "upgrade_plan_id": "upgrade-1",
                                "preflight_evidence_id": "evidence-1",
                            },
                        }
                    ).encode()
                    + b"\n"
                )
            finally:
                parent.close()

        thread = threading.Thread(target=approve)
        thread.start()
        qualification = verify_canonical_guard(child.detach())
        thread.join(timeout=2)
        self.assertEqual(qualification["upgrade_plan_id"], "upgrade-1")

    def test_canonical_guard_fails_closed_on_denial(self) -> None:
        parent, child = socket.socketpair()

        def deny() -> None:
            try:
                self.assertEqual(parent.recv(32), b"verify\n")
                parent.sendall(b'{"ok":false,"error":"stale preflight"}\n')
            finally:
                parent.close()

        thread = threading.Thread(target=deny)
        thread.start()
        with self.assertRaisesRegex(RuntimeError, "stale preflight"):
            verify_canonical_guard(child.detach())
        thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
