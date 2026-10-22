"""Expected Recovery refusals stay typed at the ordinary Upgrade backup API."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI

from codex_web.api.upgrades import build_upgrades_router
from codex_web.services.identity import IdentityService
from codex_web.services.recovery import RecoveryError, RecoveryMemoryConflictError
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class UpgradeBackupRecoveryErrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        identity = IdentityService(IdentityStateStore(SQLiteStateStore(Path(directory.name) / "state.db")))
        identity.bootstrap_local()
        self.actor = identity.local_trusted_actor()
        self.capture = Mock()
        self.app = FastAPI()
        self.app.include_router(build_upgrades_router(SimpleNamespace(capture_pre_upgrade_backup=self.capture)))

    async def request(self):
        with patch("codex_web.api.upgrades.request_actor", return_value=self.actor):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app,
                    raise_app_exceptions=False), base_url="http://fixture") as client:
                return await client.post("/api/upgrades/existing-plan/backup")

    async def test_expected_memory_refusal_is_json_conflict_without_repeating_capture(self):
        self.capture.side_effect = RecoveryMemoryConflictError(
            "recovery_memory_insufficient: required=100 available=99")
        response = await self.request()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "recovery_memory_insufficient: required=100 available=99"})
        self.capture.assert_called_once_with("existing-plan", actor=self.actor)

    async def test_other_expected_recovery_error_matches_recovery_api(self):
        self.capture.side_effect = RecoveryError("configured destination is unavailable")
        response = await self.request()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "configured destination is unavailable"})
        self.capture.assert_called_once()

    async def test_unexpected_exception_is_not_relabelled_as_safe_recovery_refusal(self):
        self.capture.side_effect = RuntimeError("unexpected private backend failure")
        response = await self.request()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("unexpected private", response.text)
        self.capture.assert_called_once()
