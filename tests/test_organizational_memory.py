from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.data_governance import (
    DataClassification,
    GovernanceAction,
    GovernanceActionRequest,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.organizational_memory import (
    KnowledgeCreate,
    KnowledgeFreshness,
    KnowledgeLifecycle,
    KnowledgeObjectType,
    KnowledgeProvenance,
    KnowledgeQuery,
    KnowledgeRelationshipCreate,
    KnowledgeRelationshipType,
    KnowledgeRetrievalBudget,
    KnowledgeRevise,
    KnowledgeSourceKind,
)
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.organizational_memory import OrganizationalMemoryService
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.organizational_memory import OrganizationalMemoryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class OrganizationalMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.now = 2_000_000_000.0
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        registry = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        authority = install_authority_roles(registry)
        governance = DataGovernanceService(DataGovernanceStore(sqlite))
        self.governance = governance
        self.store = OrganizationalMemoryStore(sqlite)
        self.service = OrganizationalMemoryService(
            self.store,
            governance,
            authority,
            clock=lambda: self.now,
        )
        self.actor = AuthenticationActor(
            identity_id="local-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        self.other = AuthenticationActor(
            identity_id="local-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="other",
            workspace_id="other",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create(
        self,
        logical_key: str,
        *,
        title: str,
        summary: str,
        content: str,
        object_type: KnowledgeObjectType = KnowledgeObjectType.OTHER,
        project_id: str | None = None,
        tags: tuple[str, ...] = (),
        classification: DataClassification = DataClassification.INTERNAL,
        deny_model_context: bool = False,
        required_role_ids: tuple[str, ...] = (),
        review_after: float | None = None,
        valid_until: float | None = None,
        retention_expires_at: float | None = None,
        retention_action: GovernanceAction = GovernanceAction.REDACT,
        relationships: tuple[KnowledgeRelationshipCreate, ...] = (),
        source_governance_record_ids: tuple[str, ...] = (),
    ):
        return self.service.create(
            KnowledgeCreate(
                logical_key=logical_key,
                object_type=object_type,
                title=title,
                summary=summary,
                content=content,
                project_id=project_id,
                tags=tags,
                relationships=relationships,
                provenance=KnowledgeProvenance(
                    source_kind=KnowledgeSourceKind.MANUAL,
                    source_ref=f"test://{logical_key}",
                    authored_by=self.actor.identity_id,
                    authored_at=self.now - 100,
                    observed_at=self.now - 10,
                    source_governance_record_ids=source_governance_record_ids,
                ),
                classification=classification,
                deny_model_context=deny_model_context,
                required_role_ids=required_role_ids,
                review_after=review_after,
                valid_until=valid_until,
                retention_expires_at=retention_expires_at,
                retention_action=retention_action,
            ),
            actor=self.actor,
        )

    def test_hybrid_semantic_and_structured_retrieval_is_bounded_and_audited(self) -> None:
        database = self._create(
            "adr/database/postgres",
            title="PostgreSQL persistence decision",
            summary="Use PostgreSQL as the primary application database.",
            content=(
                "The architecture decision standardizes durable relational "
                "persistence on PostgreSQL for the control plane."
            ),
            object_type=KnowledgeObjectType.ARCHITECTURE_DECISION,
            project_id="project-a",
            tags=("architecture", "database"),
        )
        self._create(
            "product/onboarding",
            title="Onboarding product note",
            summary="Improve first-run activation.",
            content="Customer onboarding experiments target activation.",
            object_type=KnowledgeObjectType.PRODUCT,
            project_id="project-a",
            tags=("product",),
        )

        result = self.service.search(
            KnowledgeQuery(
                text="db storage architecture",
                object_types=(KnowledgeObjectType.ARCHITECTURE_DECISION,),
                project_ids=("project-a",),
                tags=("architecture",),
                budget=KnowledgeRetrievalBudget(
                    top_k=3,
                    candidate_limit=10,
                    max_context_tokens=128,
                ),
            ),
            actor=self.actor,
        )

        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].knowledge_id, database.id)
        self.assertGreater(result.items[0].semantic_score, 0)
        self.assertLessEqual(result.packed_tokens, 128)
        self.assertLessEqual(result.items[0].estimated_tokens, 128)
        self.assertIn("semantic:", " ".join(result.items[0].reasons))

        run = self.service.retrieval_run(result.retrieval_id, actor=self.actor)
        self.assertEqual(run.selected_knowledge_ids, (database.id,))
        self.assertIn(database.id, run.selected_scores)
        self.assertEqual(
            run.selected_freshness[database.id],
            KnowledgeFreshness.FRESH,
        )
        self.assertEqual(run.max_context_tokens, 128)
        self.assertNotIn("db storage architecture", str(run.filters))
        self.assertEqual(len(run.query_sha256), 64)

    def test_token_budget_truncates_large_content_without_exceeding_budget(self) -> None:
        record = self._create(
            "procedure/large-runbook",
            title="Large recovery runbook",
            summary="A deliberately large procedure.",
            content=("recovery database rollback validation " * 2000),
            object_type=KnowledgeObjectType.PROCEDURE,
            tags=("recovery",),
        )

        result = self.service.search(
            KnowledgeQuery(
                text="database recovery rollback",
                logical_keys=(record.logical_key,),
                budget=KnowledgeRetrievalBudget(
                    top_k=1,
                    candidate_limit=5,
                    max_context_tokens=128,
                ),
            ),
            actor=self.actor,
        )

        self.assertEqual(len(result.items), 1)
        self.assertLessEqual(result.packed_tokens, 128)
        self.assertLessEqual(result.items[0].estimated_tokens, 128)
        self.assertIn("truncated by memory retrieval budget", result.items[0].context_excerpt)

    def test_governance_classification_and_deny_model_context_fail_closed(self) -> None:
        confidential = self._create(
            "policy/confidential",
            title="Confidential policy",
            summary="Tenant-internal policy.",
            content="confidential operating policy",
            object_type=KnowledgeObjectType.POLICY,
            classification=DataClassification.CONFIDENTIAL,
        )
        restricted = self._create(
            "policy/restricted",
            title="Restricted policy",
            summary="Restricted policy.",
            content="restricted operating policy",
            object_type=KnowledgeObjectType.POLICY,
            classification=DataClassification.RESTRICTED,
        )
        secret = self._create(
            "policy/secret",
            title="Secret policy",
            summary="Secret policy.",
            content="secret operating policy",
            object_type=KnowledgeObjectType.POLICY,
            classification=DataClassification.SECRET,
        )
        denied = self._create(
            "policy/no-model",
            title="No model context",
            summary="Must never enter model context.",
            content="model restricted internal policy",
            object_type=KnowledgeObjectType.POLICY,
            deny_model_context=True,
        )

        default_result = self.service.search(
            KnowledgeQuery(
                text="operating policy",
                include_stale=True,
                budget=KnowledgeRetrievalBudget(top_k=10),
            ),
            actor=self.actor,
        )
        ids = {item.knowledge_id for item in default_result.items}
        self.assertIn(confidential.id, ids)
        self.assertNotIn(restricted.id, ids)
        self.assertNotIn(secret.id, ids)
        self.assertNotIn(denied.id, ids)
        reasons = {
            item.knowledge_id: item.reason
            for item in default_result.denied
        }
        self.assertEqual(
            reasons[restricted.id],
            "governance:classification_exceeds_context_limit",
        )
        self.assertEqual(
            reasons[secret.id],
            "governance:secret_data_never_enters_model_context",
        )
        self.assertEqual(
            reasons[denied.id],
            "governance:record_denies_model_context",
        )

        restricted_result = self.service.search(
            KnowledgeQuery(
                text="operating policy",
                max_classification=DataClassification.RESTRICTED,
                budget=KnowledgeRetrievalBudget(top_k=10),
            ),
            actor=self.actor,
        )
        restricted_ids = {
            item.knowledge_id for item in restricted_result.items
        }
        self.assertIn(restricted.id, restricted_ids)
        self.assertNotIn(secret.id, restricted_ids)

    def test_required_operational_role_and_tenant_scope_are_enforced(self) -> None:
        restricted_role = self._create(
            "policy/security-only",
            title="Security-only policy",
            summary="Only security role holders may retrieve it.",
            content="security handling requirements",
            object_type=KnowledgeObjectType.POLICY,
            required_role_ids=("security-specialist",),
        )

        result = self.service.search(
            KnowledgeQuery(
                text="security requirements",
                budget=KnowledgeRetrievalBudget(top_k=5),
            ),
            actor=self.actor,
        )
        self.assertEqual(result.items, ())
        self.assertEqual(
            {item.knowledge_id: item.reason for item in result.denied}[
                restricted_role.id
            ],
            "required_operational_role_missing",
        )

        with self.assertRaises(Exception):
            self.service.get(restricted_role.id, actor=self.other)

        foreign = self.service.search(
            KnowledgeQuery(
                text="security requirements",
                budget=KnowledgeRetrievalBudget(top_k=5),
            ),
            actor=self.other,
        )
        self.assertEqual(foreign.items, ())
        self.assertEqual(foreign.candidate_count, 0)

    def test_revision_makes_supersession_explicit_and_current_wins_default_retrieval(self) -> None:
        original = self._create(
            "policy/deployment",
            title="Deployment policy v1",
            summary="Old deployment policy.",
            content="Deploy directly after tests.",
            object_type=KnowledgeObjectType.POLICY,
            tags=("deployment",),
        )
        revised = self.service.revise(
            original.id,
            KnowledgeRevise(
                title="Deployment policy v2",
                summary="Current deployment policy.",
                content="Promote immutable artifacts through staged environments.",
                provenance=KnowledgeProvenance(
                    source_kind=KnowledgeSourceKind.MANUAL,
                    source_ref="test://policy/deployment/v2",
                    authored_by=self.actor.identity_id,
                    observed_at=self.now,
                ),
                reason="replace unsafe direct deployment guidance",
            ),
            actor=self.actor,
        )

        versions = self.service.versions(revised.id, actor=self.actor)
        self.assertEqual([item.version for item in versions], [1, 2])
        self.assertEqual(versions[0].lifecycle, KnowledgeLifecycle.SUPERSEDED)
        self.assertEqual(versions[0].superseded_by_id, revised.id)
        self.assertEqual(revised.previous_version_id, original.id)

        current = self.service.search(
            KnowledgeQuery(
                text="deployment staged immutable artifacts",
                logical_keys=(original.logical_key,),
                budget=KnowledgeRetrievalBudget(top_k=5),
            ),
            actor=self.actor,
        )
        self.assertEqual(
            {item.knowledge_id for item in current.items},
            {revised.id},
        )

        historical = self.service.search(
            KnowledgeQuery(
                text="deployment policy",
                logical_keys=(original.logical_key,),
                include_superseded=True,
                budget=KnowledgeRetrievalBudget(top_k=5),
            ),
            actor=self.actor,
        )
        by_id = {item.knowledge_id: item for item in historical.items}
        self.assertEqual(
            by_id[original.id].freshness,
            KnowledgeFreshness.SUPERSEDED,
        )
        self.assertEqual(
            by_id[revised.id].freshness,
            KnowledgeFreshness.FRESH,
        )
        relations = self.service.relationships(revised.id, actor=self.actor)
        self.assertTrue(
            any(
                item.relationship_type == KnowledgeRelationshipType.SUPERSEDES
                and item.target_knowledge_id == original.id
                for item in relations
            )
        )

    def test_stale_and_expired_memory_are_excluded_unless_explicitly_requested(self) -> None:
        stale = self._create(
            "architecture/stale",
            title="Stale architecture note",
            summary="Needs review.",
            content="legacy queue architecture",
            object_type=KnowledgeObjectType.ARCHITECTURE_DECISION,
            review_after=self.now - 1,
        )
        expired = self._create(
            "architecture/expired",
            title="Expired architecture note",
            summary="No longer temporally valid.",
            content="expired queue architecture",
            object_type=KnowledgeObjectType.ARCHITECTURE_DECISION,
            valid_until=self.now - 1,
        )

        default_result = self.service.search(
            KnowledgeQuery(
                text="queue architecture",
                budget=KnowledgeRetrievalBudget(top_k=5),
            ),
            actor=self.actor,
        )
        self.assertEqual(default_result.items, ())

        explicit = self.service.search(
            KnowledgeQuery(
                text="queue architecture",
                include_stale=True,
                budget=KnowledgeRetrievalBudget(top_k=5),
            ),
            actor=self.actor,
        )
        freshness = {
            item.knowledge_id: item.freshness
            for item in explicit.items
        }
        self.assertEqual(freshness[stale.id], KnowledgeFreshness.STALE)
        self.assertEqual(freshness[expired.id], KnowledgeFreshness.EXPIRED)

    def test_governed_deletion_cascades_to_derived_memory_and_removes_index_content(self) -> None:
        source = self._create(
            "policy/source",
            title="Source policy",
            summary="Source of derived policy.",
            content="source policy content",
            object_type=KnowledgeObjectType.POLICY,
            retention_action=GovernanceAction.DELETE,
        )
        derived = self.service.revise(
            source.id,
            KnowledgeRevise(
                title="Source policy revision",
                summary="Derived current policy.",
                content="derived current policy content",
                provenance=KnowledgeProvenance(
                    source_kind=KnowledgeSourceKind.MANUAL,
                    source_ref="test://policy/source/v2",
                    authored_by=self.actor.identity_id,
                    observed_at=self.now,
                ),
                reason="derive a new current version",
            ),
            actor=self.actor,
        )

        request = self.governance.request_action(
            GovernanceActionRequest(
                record_id=source.governance_record_id,
                action=GovernanceAction.DELETE,
                reason="test governed deletion",
            ),
            actor=self.actor,
        )
        completed = self.governance.execute_request(
            request.id,
            actor=self.actor,
        )
        self.assertEqual(completed.status.value, "completed")

        source_after = self.service.get(
            source.id,
            actor=self.actor,
            include_inactive=True,
        )
        derived_after = self.service.get(
            derived.id,
            actor=self.actor,
            include_inactive=True,
        )
        self.assertEqual(source_after.lifecycle, KnowledgeLifecycle.DELETED)
        self.assertEqual(derived_after.lifecycle, KnowledgeLifecycle.DELETED)
        self.assertEqual(source_after.content, "")
        self.assertEqual(derived_after.content, "")
        state = self.store.load()
        self.assertNotIn(
            source.id,
            {item.knowledge_id for item in state.embeddings},
        )
        self.assertNotIn(
            derived.id,
            {item.knowledge_id for item in state.embeddings},
        )

        result = self.service.search(
            KnowledgeQuery(
                text="source derived policy",
                include_superseded=True,
                include_invalid=True,
                include_stale=True,
            ),
            actor=self.actor,
        )
        self.assertEqual(result.items, ())


if __name__ == "__main__":
    unittest.main()
