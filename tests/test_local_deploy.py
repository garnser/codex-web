from __future__ import annotations

import unittest
import json
import socket
import threading
import io
import subprocess
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

from scripts.local_deploy import (health, set_service_revision, verify_canonical_guard,
                                 version_matches, materialize_candidate, checkout,
                                 origin_identity, running_repository_origin)


class CandidateMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.remote, self.seed = self.root / "origin.git", self.root / "seed"
        self.source, self.live = self.root / "source", self.root / "live"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "init", "--initial-branch=main", str(self.seed))
        self.git(self.seed, "config", "user.email", "fixture@example.invalid")
        self.git(self.seed, "config", "user.name", "Fixture")
        (self.seed / "old.txt").write_text("original\n")
        (self.seed / ".gitignore").write_text("__pycache__/\n")
        (self.seed / "codex_web").mkdir()
        (self.seed / "codex_web" / "__init__.py").write_text("FIXTURE = True\n")
        (self.seed / "server.py").write_text("from codex_web import FIXTURE\n")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "Original")
        self.old = self.git(self.seed, "rev-parse", "HEAD").strip()
        self.git(self.seed, "remote", "add", "origin", str(self.remote))
        self.git(self.seed, "push", "origin", "main")
        self.git(self.root, "clone", "--no-local", str(self.remote), str(self.live))
        (self.seed / "candidate.txt").write_text("qualified candidate\n")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "Candidate")
        self.candidate = self.git(self.seed, "rev-parse", "HEAD").strip()
        self.git(self.seed, "push", "origin", "main")
        self.git(self.root, "clone", "--no-local", str(self.remote), str(self.source))

    @staticmethod
    def git(path, *args):
        return subprocess.check_output(["git", "-C", str(path), *args],
                                       text=True, stderr=subprocess.DEVNULL)

    def materialize(self):
        return materialize_candidate(self.source, self.live, self.candidate,
                                     self.old, str(self.remote))

    def test_independent_clone_materializes_without_changing_head_or_files(self):
        self.materialize()
        self.assertEqual(self.git(self.live, "rev-parse", "HEAD").strip(), self.old)
        self.assertFalse((self.live / "candidate.txt").exists())
        self.assertEqual(self.git(self.live, "status", "--porcelain"), "")
        checkout(self.live, self.candidate)
        self.assertEqual((self.live / "candidate.txt").read_text(), "qualified candidate\n")

    def test_complete_candidate_does_not_fetch(self):
        self.materialize()
        from scripts import local_deploy
        original = local_deploy.safe_git
        def guarded(path, *args, **kwargs):
            if args[0] == "fetch":
                self.fail("complete candidate must not fetch")
            return original(path, *args, **kwargs)
        with patch("scripts.local_deploy.safe_git", side_effect=guarded):
            self.materialize()

    def test_known_commit_with_missing_blob_is_repaired_in_pack_storage(self):
        self.git(self.live, "-c", "fetch.unpackLimit=100000", "fetch", "origin", self.candidate)
        blob = self.git(self.source, "rev-parse", self.candidate + ":candidate.txt").strip()
        loose = self.live / ".git" / "objects" / blob[:2] / blob[2:]
        self.assertTrue(loose.exists())
        loose.unlink()
        loose.parent.chmod(0o555)
        try:
            self.materialize()
            self.assertFalse(loose.exists())
            self.assertTrue(list((self.live / ".git" / "objects" / "pack").glob("*.pack")))
            self.assertEqual(self.git(self.live, "cat-file", "-p", blob), "qualified candidate\n")
        finally:
            if loose.parent.exists():
                loose.parent.chmod(0o755)

    def test_dirty_checkout_is_preserved_without_fetch(self):
        (self.live / "old.txt").write_text("unpublished edits\n")
        with self.assertRaisesRegex(RuntimeError, "unexpected changes"):
            self.materialize()
        self.assertEqual((self.live / "old.txt").read_text(), "unpublished edits\n")
        self.assertEqual(self.git(self.live, "rev-parse", "HEAD").strip(), self.old)

    def test_foreign_origin_and_abbreviated_candidate_are_rejected(self):
        self.git(self.live, "remote", "set-url", "origin", str(self.root / "foreign.git"))
        with self.assertRaisesRegex(RuntimeError, "origin differs"):
            self.materialize()
        with self.assertRaisesRegex(RuntimeError, "exact lowercase SHA"):
            materialize_candidate(self.source, self.live, self.candidate[:8], self.old, str(self.remote))

    def test_fetch_failure_preserves_old_checkout(self):
        from scripts import local_deploy
        original = local_deploy.safe_git
        for failure in (subprocess.TimeoutExpired("fetch", 120),
                        subprocess.CalledProcessError(128, "fetch")):
            def fail_fetch(path, *args, **kwargs):
                if args[0] == "fetch":
                    raise failure
                return original(path, *args, **kwargs)
            with self.subTest(failure=type(failure).__name__), \
                 patch("scripts.local_deploy.safe_git", side_effect=fail_fetch):
                with self.assertRaises(type(failure)):
                    self.materialize()
        self.assertEqual(self.git(self.live, "rev-parse", "HEAD").strip(), self.old)
        self.assertEqual((self.live / "old.txt").read_text(), "original\n")

    def test_changed_origin_during_fetch_is_rejected_before_checkout(self):
        from scripts import local_deploy
        original = local_deploy.safe_git
        def change_origin(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if args[0] == "fetch":
                self.git(self.live, "remote", "set-url", "origin", str(self.root / "foreign.git"))
            return result
        with patch("scripts.local_deploy.safe_git", side_effect=change_origin):
            with self.assertRaisesRegex(RuntimeError, "origin differs"):
                self.materialize()
        self.assertEqual(self.git(self.live, "rev-parse", "HEAD").strip(), self.old)

    def test_changed_head_during_fetch_is_not_overwritten(self):
        from scripts import local_deploy
        original = local_deploy.safe_git
        def change_head(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if args[0] == "fetch":
                self.git(self.live, "checkout", "--detach", self.candidate)
            return result
        with patch("scripts.local_deploy.safe_git", side_effect=change_head):
            with self.assertRaisesRegex(RuntimeError, "revision changed"):
                self.materialize()
        self.assertEqual(self.git(self.live, "rev-parse", "HEAD").strip(), self.candidate)

    def test_hooks_do_not_run_during_fetch_or_checkout(self):
        marker = self.root / "hook-ran"
        hook = self.live / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        hook.chmod(0o755)
        self.materialize()
        checkout(self.live, self.candidate)
        self.assertFalse(marker.exists())

    def test_ignored_collision_is_preserved_and_checkout_does_not_switch(self):
        (self.live / ".git" / "info" / "exclude").write_text("candidate.txt\n")
        artifact = self.live / "candidate.txt"
        artifact.write_text("unpublished ignored artifact\n")
        self.materialize()
        with self.assertRaises(subprocess.CalledProcessError):
            checkout(self.live, self.candidate)
        self.assertEqual(artifact.read_text(), "unpublished ignored artifact\n")
        self.assertEqual(self.git(self.live, "rev-parse", "HEAD").strip(), self.old)

    def test_network_origin_credentials_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "credentials"):
            origin_identity("https://user:secret@example.invalid/repository.git")

    def test_running_build_identity_requires_exact_revision_and_network_origin(self):
        for source, revision in [(str(self.remote), self.old),
                                 ("https://example.invalid/repo", "b" * 40)]:
            response = io.StringIO(json.dumps({"ok": True, "build": {
                "source": source, "gitRevision": revision}}))
            with patch("scripts.local_deploy.urllib.request.urlopen", return_value=response):
                with self.assertRaises(RuntimeError):
                    running_repository_origin(self.old)

    def test_main_verifies_objects_before_guard_and_tree_before_restart(self):
        from scripts import local_deploy
        original_run = local_deploy.run
        restarts = []
        marker = self.root / "source-hook-ran"
        hook = self.source / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        hook.chmod(0o755)
        def guarded(fd):
            self.assertEqual(self.git(self.live, "rev-parse", "HEAD").strip(), self.old)
            self.assertEqual(self.git(self.live, "show", self.candidate + ":candidate.txt"),
                             "qualified candidate\n")
            return {"rollback_available": False}
        def run(*args, **kwargs):
            if args[0] == "sudo":
                self.assertEqual(self.git(self.live, "rev-parse", "HEAD").strip(), self.candidate)
                self.assertEqual(self.git(self.live, "status", "--porcelain"), "")
                restarts.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")
            return original_run(*args, **kwargs)
        argv = ["installer", "--candidate", self.candidate, "--source", str(self.source),
                "--live", str(self.live), "--expected-live", self.old, "--guard-fd", "0"]
        with patch.object(sys, "argv", argv), \
             patch("scripts.local_deploy.LOCAL_DEPLOY_LOCK", self.root / "deploy.lock"), \
             patch("scripts.local_deploy.running_repository_origin", return_value=str(self.remote)), \
             patch("scripts.local_deploy.verify_canonical_guard", side_effect=guarded) as guard, \
             patch("scripts.local_deploy.run", side_effect=run), \
             patch("scripts.local_deploy.set_service_revision"), \
             patch("scripts.local_deploy.health", return_value=True):
            self.assertEqual(local_deploy.main(), 0)
        guard.assert_called_once()
        self.assertEqual(len(restarts), 1)
        self.assertFalse(marker.exists())

    def test_main_fetch_failure_never_consumes_guard_or_restarts(self):
        from scripts import local_deploy
        original = local_deploy.safe_git
        def fail(path, *args, **kwargs):
            if path == self.live and args[0] == "fetch":
                raise subprocess.TimeoutExpired("fetch", 120)
            return original(path, *args, **kwargs)
        argv = ["installer", "--candidate", self.candidate, "--source", str(self.source),
                "--live", str(self.live), "--expected-live", self.old, "--guard-fd", "0"]
        with patch.object(sys, "argv", argv), \
             patch("scripts.local_deploy.LOCAL_DEPLOY_LOCK", self.root / "deploy.lock"), \
             patch("scripts.local_deploy.running_repository_origin", return_value=str(self.remote)), \
             patch("scripts.local_deploy.safe_git", side_effect=fail), \
             patch("scripts.local_deploy.verify_canonical_guard") as guard, \
             patch("scripts.local_deploy.set_service_revision") as service:
            with self.assertRaises(subprocess.TimeoutExpired):
                local_deploy.main()
        guard.assert_not_called()
        service.assert_not_called()
        self.assertEqual(self.git(self.live, "rev-parse", "HEAD").strip(), self.old)

    def test_head_change_during_canonical_guard_aborts_without_restart(self):
        from scripts import local_deploy
        def change_head(fd):
            self.git(self.live, "checkout", "--detach", self.candidate)
            return {"rollback_available": False}
        argv = ["installer", "--candidate", self.candidate, "--source", str(self.source),
                "--live", str(self.live), "--expected-live", self.old, "--guard-fd", "0"]
        with patch.object(sys, "argv", argv), \
             patch("scripts.local_deploy.LOCAL_DEPLOY_LOCK", self.root / "deploy.lock"), \
             patch("scripts.local_deploy.running_repository_origin", return_value=str(self.remote)), \
             patch("scripts.local_deploy.verify_canonical_guard", side_effect=change_head), \
             patch("scripts.local_deploy.set_service_revision") as service:
            with self.assertRaisesRegex(RuntimeError, "revision changed during canonical"):
                local_deploy.main()
        service.assert_not_called()
        self.assertEqual(self.git(self.live, "rev-parse", "HEAD").strip(), self.candidate)


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
