from __future__ import annotations

import unittest

from fastapi import FastAPI

from codex_web.models import ApprovalSlackMessage, BotThreadDetail
from codex_web.services.approvals import ApprovalService
from codex_web.services.bot_details import BotDetailService, install_bot_detail_service


class _StateHost:
    def __init__(self) -> None:
        self.approvals: dict[str, list[ApprovalSlackMessage]] = {}
        self.details: dict[str, list[BotThreadDetail]] = {}
        self.approval_saves = 0
        self.detail_saves = 0

    def _load_approval_messages(self):
        return {key: list(values) for key, values in self.approvals.items()}

    def _save_approval_messages(self, values):
        self.approvals = {key: list(items) for key, items in values.items()}
        self.approval_saves += 1

    def _load_bot_details(self):
        return {key: list(values) for key, values in self.details.items()}

    def _save_bot_details(self, values):
        self.details = {key: list(items) for key, items in values.items()}
        self.detail_saves += 1


class ApprovalMessageStateTests(unittest.TestCase):
    def test_message_bookkeeping_deduplicates_forgets_and_rebinds_existing_service(self) -> None:
        host = _StateHost()
        service = ApprovalService(host)

        for _ in range(2):
            service.remember_message(
                42,
                connection_id="conn",
                channel="C1",
                message_ts="123.4",
                context="James",
                thread_id="thread-1",
            )

        self.assertEqual(len(host.approvals["42"]), 1)
        self.assertEqual(host.approval_saves, 1)
        service.forget_messages(42)
        self.assertNotIn("42", host.approvals)
        self.assertEqual(host.approval_saves, 2)
        self.assertIs(host._remember_approval_message.__self__, service)
        self.assertIs(host._forget_approval_messages.__self__, service)


class BotDetailStateTests(unittest.TestCase):
    def test_details_are_bounded_and_latest_is_returned(self) -> None:
        host = _StateHost()
        service = BotDetailService(host)

        for index in range(25):
            service.record("thread-1", "command", f"item-{index}", str(index))

        self.assertEqual(len(host.details["thread-1"]), 20)
        self.assertEqual(service.latest("thread-1").title, "item-24")
        self.assertEqual(host.details["thread-1"][0].title, "item-5")

    def test_retarget_updates_detail_thread_identity(self) -> None:
        host = _StateHost()
        service = BotDetailService(host)
        service.record("old", "file", "changed", "diff")

        service.retarget("old", "new")

        self.assertNotIn("old", host.details)
        self.assertEqual(host.details["new"][0].thread_id, "new")

    def test_installer_rebinds_detail_entrypoints(self) -> None:
        app = FastAPI()
        host = _StateHost()

        service = install_bot_detail_service(app, host)

        self.assertIs(app.state.bot_detail_service, service)
        self.assertIs(host._record_bot_detail.__self__, service)
        self.assertIs(host._latest_bot_detail.__self__, service)
        self.assertIs(host._retarget_bot_details.__self__, service)


if __name__ == "__main__":
    unittest.main()
