from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.data_governance import build_data_governance_router
from codex_web.api.identity import install_identity_middleware
from codex_web.data_governance import (
    ContextFilterRequest,
    DataCategory,
    DataClassification,
    ExportAuthorizationRequest,
    GovernedDataCreate,
    GovernedDataLifecycle,
    GovernanceAction,
    GovernanceActionRequest,
    GovernanceRequestStatus,
)
from codex_web.identity import (
    AuthenticationAssurance,
    AuthenticationActor,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.data_governance import (
    DataGovernanceService,
    GovernanceConflictError,
    GovernanceNotFoundError,
)
from codex_web.services.identity import IdentityService
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class DataGovernanceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        store = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.service = DataGovernanceService(DataGovernanceStore(store))
        self.admin = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.other = AuthenticationActor(
            identity_id="other-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="other",
            workspace_id="other",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_derived_record_cannot_downgrade_classification_or_outlive_source(self) -> None:
        source = self.service.register(
            GovernedDataCreate(
                object_type="thread",
                object_id="thread-1",
                category=DataCategory.THREAD,
                classification=DataClassification.CONFIDENTIAL,
                retention_expires_at=100.0,
                residency_tags=("eu",),
                deny_model_context=True,
            ),
            actor=self.admin,
        )
        derived = self.service.register(
            GovernedDataCreate(
                object_type="memory",
                object_id="summary-1",
                category=DataCategory.MEMORY,
                classification=DataClassification.INTERNAL,
                retention_expires_at=200.0,
                residency_tags=("se",),
                source_record_ids=(source.id,),
            ),
            actor=self.admin,
        )

        self.assertEqual(derived.requested_classification, DataClassification.INTERNAL)
        self.assertEqual(derived.classification, DataClassification.CONFIDENTIAL)
        self.assertEqual(derived.retention_expires_at, 100.0)
        self.assertEqual(derived.residency_tags, ("eu", "se"))
        self.assertTrue(derived.deny_model_context)

    def test_context_filter_denies_secret_credentials_and_over_limit_records(self) -> None:
        public = self.service.register(
            GovernedDataCreate(
                object_type="artifact",
                object_id="a1",
                category=DataCategory.ARTIFACT,
                classification=DataClassification.PUBLIC,
            ),
            actor=self.admin,
        )
        restricted = self.service.register(
            GovernedDataCreate(
                object_type="log",
                object_id="l1",
                category=DataCategory.LOG,
                classification=DataClassification.RESTRICTED,
            ),
            actor=self.admin,
        )
        secret = self.service.register(
            GovernedDataCreate(
                object_type="prompt",
                object_id="p1",
                category=DataCategory.PROMPT,
                classification=DataClassification.SECRET,
            ),
            actor=self.admin,
        )
        credential = self.service.register(
            GovernedDataCreate(
                object_type="credential",
                object_id="cred-1",
                category=DataCategory.CREDENTIAL,
                classification=DataClassification.CONFIDENTIAL,
            ),
            actor=self.admin,
        )

        result = self.service.filter_context(
            ContextFilterRequest(
                record_ids=(public.id, restricted.id, secret.id, credential.id),
                max_classification=DataClassification.CONFIDENTIAL,
            ),
            actor=self.admin,
        )
        self.assertEqual(result.allowed_record_ids, (public.id,))
        reasons = {item.record_id: item.reason for item in result.decisions}
        self.assertEqual(reasons[restricted.id], "classification_exceeds_context_limit")
        self.assertEqual(reasons[secret.id], "secret_data_never_enters_model_context")
        self.assertEqual(
            reasons[credential.id],
            "credential_data_never_enters_model_context",
        )

    def test_retention_sweep_respects_legal_hold_and_deduplicates_requests(self) -> None:
        due = self.service.register(
            GovernedDataCreate(
                object_type="memory",
                object_id="m1",
                category=DataCategory.MEMORY,
                retention_expires_at=10.0,
                retention_action=GovernanceAction.DELETE,
            ),
            actor=self.admin,
        )
        held = self.service.register(
            GovernedDataCreate(
                object_type="audit",
                object_id="audit-1",
                category=DataCategory.AUDIT,
                retention_expires_at=10.0,
            ),
            actor=self.admin,
        )
        self.service.set_legal_hold(held.id, "legal matter", actor=self.admin)

        first = self.service.retention_sweep(actor=self.admin, now=11.0)
        second = self.service.retention_sweep(actor=self.admin, now=12.0)

        self.assertEqual(first.due_record_ids, (held.id, due.id))
        self.assertEqual(first.held_record_ids, (held.id,))
        self.assertEqual(len(first.request_ids), 1)
        self.assertEqual(first.request_ids, second.request_ids)

    def test_governed_action_requires_domain_handler_and_records_receipt(self) -> None:
        record = self.service.register(
            GovernedDataCreate(
                object_type="memory",
                object_id="memory-1",
                category=DataCategory.MEMORY,
            ),
            actor=self.admin,
        )
        request = self.service.request_action(
            GovernanceActionRequest(
                record_id=record.id,
                action=GovernanceAction.REDACT,
                reason="privacy request",
            ),
            actor=self.admin,
        )
        with self.assertRaises(GovernanceConflictError):
            self.service.execute_request(request.id, actor=self.admin)
        blocked = self.service.list_requests(self.admin)[0]
        self.assertEqual(blocked.status, GovernanceRequestStatus.BLOCKED)
        self.assertEqual(blocked.blocked_reason, "adapter_unavailable")

        calls: list[tuple[str, GovernanceAction]] = []

        def handler(item, action):
            calls.append((item.object_id, action))
            return "memory-redaction:1"

        self.service.register_action_handler("memory", handler)
        completed = self.service.execute_request(request.id, actor=self.admin)
        self.assertEqual(completed.status, GovernanceRequestStatus.COMPLETED)
        self.assertEqual(completed.adapter_receipt_ref, "memory-redaction:1")
        self.assertEqual(calls, [("memory-1", GovernanceAction.REDACT)])
        updated = self.service.get_record(record.id, self.admin)
        self.assertEqual(updated.lifecycle, GovernedDataLifecycle.REDACTED)

    def test_legal_hold_blocks_action_until_released(self) -> None:
        record = self.service.register(
            GovernedDataCreate(
                object_type="memory",
                object_id="m-held",
                category=DataCategory.MEMORY,
            ),
            actor=self.admin,
        )
        self.service.set_legal_hold(record.id, "investigation", actor=self.admin)
        request = self.service.request_action(
            GovernanceActionRequest(
                record_id=record.id,
                action=GovernanceAction.DELETE,
                reason="retention",
            ),
            actor=self.admin,
        )
        self.assertEqual(request.status, GovernanceRequestStatus.BLOCKED)
        with self.assertRaises(GovernanceConflictError):
            self.service.execute_request(request.id, actor=self.admin)

        self.service.release_legal_hold(record.id, actor=self.admin)
        self.service.register_action_handler("memory", lambda item, action: "deleted:1")
        completed = self.service.execute_request(request.id, actor=self.admin)
        self.assertEqual(completed.status, GovernanceRequestStatus.COMPLETED)

    def test_scope_isolation_hides_record_existence(self) -> None:
        record = self.service.register(
            GovernedDataCreate(
                object_type="thread",
                object_id="thread-private",
                category=DataCategory.THREAD,
            ),
            actor=self.admin,
        )
        with self.assertRaises(GovernanceNotFoundError):
            self.service.get_record(record.id, self.other)

    def test_export_is_manifest_only_and_audited_without_payload(self) -> None:
        record = self.service.register(
            GovernedDataCreate(
                object_type="decision",
                object_id="decision-1",
                category=DataCategory.DECISION,
                classification=DataClassification.CONFIDENTIAL,
                residency_tags=("eu",),
            ),
            actor=self.admin,
        )
        result = self.service.authorize_export(
            ExportAuthorizationRequest(record_ids=(record.id,)),
            actor=self.admin,
        )
        self.assertEqual(result.items[0].object_id, "decision-1")
        events = self.service.events(self.admin)
        serialized = " ".join(event.model_dump_json() for event in events)
        self.assertIn("export_authorize", serialized)
        self.assertNotIn("sensitive payload", serialized)


class DataGovernanceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        store = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(store))
        self.identity.bootstrap_local()
        self.service = DataGovernanceService(DataGovernanceStore(store))
        self.app = FastAPI()
        install_identity_middleware(self.app, self.identity)
        self.app.include_router(build_data_governance_router(self.service))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_local_admin_can_register_and_filter_context_through_api(self) -> None:
        with patch.dict(os.environ, {"CODEX_WEB_IDENTITY_MODE": "local-trusted"}):
            with TestClient(self.app) as client:
                response = client.post(
                    "/api/data-governance/records",
                    json={
                        "object_type": "prompt",
                        "object_id": "prompt-1",
                        "category": "prompt",
                        "classification": "restricted",
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                record_id = response.json()["item"]["id"]
                filtered = client.post(
                    "/api/data-governance/context/filter",
                    json={
                        "record_ids": [record_id],
                        "max_classification": "confidential",
                    },
                )
                self.assertEqual(filtered.status_code, 200, filtered.text)
                self.assertEqual(filtered.json()["denied_record_ids"], [record_id])


if __name__ == "__main__":
    unittest.main()
