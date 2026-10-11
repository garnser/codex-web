from __future__ import annotations

import unittest
import json
import socket
import threading
import io
from pathlib import Path
from unittest.mock import patch

from scripts.local_deploy import health, set_service_revision, verify_canonical_guard, version_matches


class LocalDeployTests(unittest.TestCase):
    def _installation_health(self, *, plan_changes=None, observed=None, rollback=False):
        source, target = "a" * 40, "b" * 40
        qualification = dict(upgrade_plan_id="upgrade-1", service_instance_id="instance-1",
                             source_revision=source, target_revision=target, rollback_available=True)
        plan = dict(id="upgrade-1", service_instance_id="instance-1", maintenance_mode=True,
                    maintenance_scope="service", deployment_source_revision=source,
                    deployment_target_revision=target, rollback_available=True)
        plan.update(plan_changes or {})
        expected = source if rollback else target
        paths = []
        def response(url, **kwargs):
            paths.append(url)
            if url.endswith("/readyz"):
                raise AssertionError("readiness is false during a qualified drain")
            payload = ({"ok": True} if url.endswith("/livez") else
                       {"item": plan} if "/upgrades/" in url else
                       {"gitRevision": observed or expected})
            result = io.StringIO(json.dumps(payload));result.status = 200
            return result
        with patch("scripts.local_deploy.urllib.request.urlopen", side_effect=response), \
             patch("scripts.local_deploy.time.monotonic", side_effect=[0, 0, 100]), \
             patch("scripts.local_deploy.time.sleep"):
            result = health(expected, qualification, rollback=rollback)
        return result, paths

    def test_installation_can_be_verified_while_readiness_is_deliberately_disabled(self):
        healthy, paths = self._installation_health()
        self.assertTrue(healthy)
        self.assertTrue(any("/upgrades/upgrade-1" in path for path in paths))
        self.assertFalse(any(path.endswith("/readyz") for path in paths))

    def test_live_service_with_wrong_revision_or_plan_is_not_verified(self):
        cases = [{"maintenance_mode": False}, {"maintenance_scope": "tenant"},
                 {"service_instance_id": "other"}, {"deployment_target_revision": "c" * 40},
                 {"deployment_source_revision": "c" * 40}, {"id": "upgrade-other"}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertFalse(self._installation_health(plan_changes=changes)[0])
        self.assertFalse(self._installation_health(observed="c" * 40)[0])

    def test_rollback_verification_requires_current_plan_permission(self):
        self.assertTrue(self._installation_health(rollback=True)[0])
        self.assertFalse(self._installation_health(rollback=True, plan_changes={"rollback_available": False})[0])

    def test_revision_override_is_exact_and_fixed_to_codex_web_service(self):
        captured = []
        def run(*args, **kwargs):
            captured.append(args)
            if args[2] == "install":
                content = Path(args[5]).read_text()
                self.assertEqual(content, "[Service]\nEnvironment=CODEX_WEB_GIT_REVISION=" + "b" * 40 + "\n")
                self.assertEqual(args[-1], "/etc/systemd/system/codex-web.service.d/zz-local-deploy-revision.conf")
        with patch("scripts.local_deploy.run", side_effect=run):
            set_service_revision("b" * 40)
        self.assertEqual(captured[-1], ("sudo", "-n", "systemctl", "daemon-reload"))

    def test_revision_override_rejects_invalid_input_without_mutation(self):
        with patch("scripts.local_deploy.run") as run:
            with self.assertRaises(ValueError):set_service_revision("bad\nEnvironment=OTHER=value")
            run.assert_not_called()

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
