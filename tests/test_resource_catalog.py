from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.execution_contract_schema import execution_contract_for_work_item
from codex_web.execution_contract_seed import execution_role_catalog_seed_payload
from codex_web.execution_role_models import ExecutionRoleCatalogDefinition
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import Project, WorkItemState
from codex_web.resources import (
    RepositoryTargetSource,
    ResourceAlias,
    ResourceCreate,
    ResourceLifecycle,
    ResourceProvenance,
    ResourceRelationshipType,
    ResourceRisk,
    ResourceType,
    ResourceUpdate,
)
from codex_web.services.identity import IdentityService, TenantIsolationError
from codex_web.services.resources import (
    ResourceAmbiguousError,
    ResourceCatalogService,
    RepositoryTargetAmbiguousError,
    RepositoryTargetConflictError,
    RepositoryTargetMissingError,
    RepositoryTargetUnauthorizedError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore


ROLE_CONTRACTS = ExecutionRoleCatalogDefinition.model_validate(
    execution_role_catalog_seed_payload()
).role_map


class ResourceCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(sqlite))
        self.identity.bootstrap_local()
        self.actor = self.identity.local_trusted_actor()
        self.service = ResourceCatalogService(ResourceCatalogStore(sqlite))
        self.project = Project(
            id="home",
            organization_id="local",
            workspace_id="default",
            name="Home",
            path=str(root),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_alias_resolution_is_stable_across_provider_rename(self) -> None:
        resource = self.service.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Application repository",
                aliases=[
                    ResourceAlias(
                        namespace="provider",
                        provider="gitlab",
                        value="group/old-name",
                    )
                ],
                provenance=ResourceProvenance(
                    provider="gitlab",
                    provider_instance="gitlab.example",
                    external_id="42",
                    external_url="https://gitlab.example/group/old-name",
                ),
            ),
            actor=self.actor,
        )

        updated = self.service.update(
            resource.id,
            ResourceUpdate(
                name="Renamed application repository",
                aliases=[
                    ResourceAlias(
                        namespace="provider",
                        provider="gitlab",
                        value="group/old-name",
                    ),
                    ResourceAlias(
                        namespace="provider",
                        provider="gitlab",
                        value="group/new-name",
                    ),
                ],
                provenance=ResourceProvenance(
                    provider="gitlab",
                    provider_instance="gitlab.example",
                    external_id="42",
                    external_url="https://gitlab.example/group/new-name",
                ),
            ),
            actor=self.actor,
        )

        self.assertEqual(updated.id, resource.id)
        old_lookup = self.service.resolve(
            "group/old-name",
            actor=self.actor,
            expected_type=ResourceType.REPOSITORY,
            provider="gitlab",
        )
        new_lookup = self.service.resolve(
            "group/new-name",
            actor=self.actor,
            expected_type=ResourceType.REPOSITORY,
            provider="gitlab",
        )
        self.assertEqual(old_lookup.id, resource.id)
        self.assertEqual(new_lookup.id, resource.id)

    def test_duplicate_exact_alias_is_rejected_but_unqualified_lookup_can_be_ambiguous(self) -> None:
        self.service.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Repository",
                aliases=[ResourceAlias(namespace="legacy", value="production")],
            ),
            actor=self.actor,
        )
        with self.assertRaises(ResourceConflictError):
            self.service.create(
                ResourceCreate(
                    resource_type=ResourceType.REPOSITORY,
                    name="Duplicate repository",
                    aliases=[ResourceAlias(namespace="legacy", value="production")],
                ),
                actor=self.actor,
            )

        self.service.create(
            ResourceCreate(
                resource_type=ResourceType.ENVIRONMENT,
                name="Production environment",
                aliases=[ResourceAlias(namespace="environment", value="production")],
            ),
            actor=self.actor,
        )
        with self.assertRaises(ResourceAmbiguousError):
            self.service.resolve("production", actor=self.actor)

        repo = self.service.resolve(
            "production",
            actor=self.actor,
            expected_type=ResourceType.REPOSITORY,
            alias_namespace="legacy",
        )
        self.assertEqual(repo.resource_type, ResourceType.REPOSITORY)

    def test_relationship_traversal_is_deterministic_and_cycle_safe(self) -> None:
        repository = self.service.create(
            ResourceCreate(resource_type=ResourceType.REPOSITORY, name="Repo"),
            actor=self.actor,
        )
        service = self.service.create(
            ResourceCreate(resource_type=ResourceType.SERVICE, name="API"),
            actor=self.actor,
        )
        environment = self.service.create(
            ResourceCreate(resource_type=ResourceType.ENVIRONMENT, name="Production"),
            actor=self.actor,
        )
        target = self.service.create(
            ResourceCreate(resource_type=ResourceType.DEPLOYMENT_TARGET, name="Cluster"),
            actor=self.actor,
        )

        self.service.add_relationship(
            from_resource_id=repository.id,
            to_resource_id=service.id,
            relationship_type=ResourceRelationshipType.IMPLEMENTS,
            actor=self.actor,
        )
        self.service.add_relationship(
            from_resource_id=service.id,
            to_resource_id=environment.id,
            relationship_type=ResourceRelationshipType.DEPLOYS_TO,
            actor=self.actor,
        )
        self.service.add_relationship(
            from_resource_id=environment.id,
            to_resource_id=target.id,
            relationship_type=ResourceRelationshipType.HOSTED_IN,
            actor=self.actor,
        )
        self.service.add_relationship(
            from_resource_id=target.id,
            to_resource_id=repository.id,
            relationship_type=ResourceRelationshipType.DEPENDS_ON,
            actor=self.actor,
        )

        traversed = self.service.traverse(
            repository.id,
            actor=self.actor,
            direction="outgoing",
            max_depth=10,
        )
        self.assertEqual(
            {item.id for item in traversed},
            {service.id, environment.id, target.id},
        )
        self.assertEqual(len(traversed), 3)

        with self.assertRaises(ValueError):
            self.service.traverse(
                repository.id,
                actor=self.actor,
                direction="sideways",
            )

    def test_project_binding_and_legacy_migration_are_idempotent(self) -> None:
        first = self.service.migrate_legacy_strings(
            project=self.project,
            actor=self.actor,
            repositories=["group/app"],
            environments=["prod"],
        )
        second = self.service.migrate_legacy_strings(
            project=self.project,
            actor=self.actor,
            repositories=["group/app"],
            environments=["prod"],
        )

        self.assertEqual({item.id for item in first}, {item.id for item in second})
        bound = self.service.project_resources(self.project, actor=self.actor)
        self.assertEqual(len(bound), 2)
        self.assertEqual(
            {item.resource_type for item in bound},
            {ResourceType.REPOSITORY, ResourceType.ENVIRONMENT},
        )

    def test_project_resource_ids_can_filter_repository_provider_identity(self) -> None:
        alias_repository = self.service.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Application",
                aliases=[
                    ResourceAlias(
                        namespace="gitlab",
                        provider="gitlab",
                        value="group/application",
                    )
                ],
            ),
            actor=self.actor,
        )
        provenance_repository = self.service.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Platform",
                provenance=ResourceProvenance(
                    provider="gitlab",
                    external_id="group/platform",
                ),
            ),
            actor=self.actor,
        )
        environment = self.service.create(
            ResourceCreate(
                resource_type=ResourceType.ENVIRONMENT,
                name="Production",
            ),
            actor=self.actor,
        )
        for resource in (
            alias_repository,
            provenance_repository,
            environment,
        ):
            self.service.bind_project(
                project=self.project,
                resource_id=resource.id,
                actor=self.actor,
            )

        self.assertEqual(
            self.service.resource_ids_for_project(
                self.project,
                alias_value="/GROUP/APPLICATION/",
                provider="GitLab",
            ),
            [alias_repository.id],
        )
        self.assertEqual(
            self.service.resource_ids_for_project(
                self.project,
                alias_value="group/platform",
                provider="gitlab",
            ),
            [provenance_repository.id],
        )

        self.service.update(
            alias_repository.id,
            ResourceUpdate(lifecycle=ResourceLifecycle.DISABLED),
            actor=self.actor,
        )
        self.assertEqual(
            self.service.resource_ids_for_project(
                self.project,
                alias_value="group/application",
                provider="gitlab",
            ),
            [],
        )

    def test_cross_tenant_lookup_and_binding_fail_closed(self) -> None:
        resource = self.service.create(
            ResourceCreate(resource_type=ResourceType.DATABASE, name="Primary"),
            actor=self.actor,
        )
        foreign = AuthenticationActor(
            identity_id="foreign-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="other",
            workspace_id="other",
            roles=(MembershipRole.OWNER,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )

        with self.assertRaises(ResourceNotFoundError):
            self.service.get(resource.id, foreign)

        with self.assertRaises(TenantIsolationError):
            self.service.bind_project(
                project=self.project,
                resource_id=resource.id,
                actor=foreign,
            )

    def test_repository_target_selectors_must_converge_and_preserve_provenance(self) -> None:
        first = self.service.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="App",
            ),
            actor=self.actor,
        )
        second = self.service.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Platform",
            ),
            actor=self.actor,
        )
        self.service.bind_project(
            project=self.project,
            resource_id=first.id,
            actor=self.actor,
        )
        self.service.bind_project(
            project=self.project,
            resource_id=second.id,
            actor=self.actor,
        )

        converged = self.service.resolve_repository_target(
            self.project,
            actor=self.actor,
            work_item_resource_ids=(second.id,),
            work_item_ref="group/platform#42",
            explicit_repository_id=second.id,
            thread_profile_repository_id=second.id,
            routing_repository_id=second.id,
        )
        self.assertEqual(
            converged.mutable_repository_id,
            second.id,
        )
        self.assertEqual(
            converged.source,
            RepositoryTargetSource.WORK_ITEM,
        )
        self.assertEqual(
            converged.source_ref,
            "group/platform#42",
        )
        self.assertEqual(
            [item.source for item in converged.selection_evidence],
            [
                RepositoryTargetSource.WORK_ITEM,
                RepositoryTargetSource.EXPLICIT,
                RepositoryTargetSource.THREAD_PROFILE,
                RepositoryTargetSource.ROUTING_RULE,
            ],
        )
        self.assertEqual(
            {
                item.repository_id
                for item in converged.selection_evidence
            },
            {second.id},
        )

        with self.assertRaises(RepositoryTargetConflictError):
            self.service.resolve_repository_target(
                self.project,
                actor=self.actor,
                work_item_resource_ids=(second.id,),
                work_item_ref="group/platform#42",
                explicit_repository_id=first.id,
            )

        explicit = self.service.resolve_repository_target(
            self.project,
            actor=self.actor,
            explicit_repository_id=second.id,
        )
        self.assertEqual(
            explicit.mutable_repository_id,
            second.id,
        )
        self.assertEqual(
            explicit.source,
            RepositoryTargetSource.EXPLICIT,
        )

        profile = self.service.resolve_repository_target(
            self.project,
            actor=self.actor,
            thread_profile_repository_id=second.id,
        )
        self.assertEqual(
            profile.source,
            RepositoryTargetSource.THREAD_PROFILE,
        )

        routed = self.service.resolve_repository_target(
            self.project,
            actor=self.actor,
            routing_repository_id=first.id,
        )
        self.assertEqual(
            routed.mutable_repository_id,
            first.id,
        )
        self.assertEqual(
            routed.source,
            RepositoryTargetSource.ROUTING_RULE,
        )

    def test_repository_target_ambiguity_unauthorized_and_orchestration_only(self) -> None:
        first = self.service.create(
            ResourceCreate(resource_type=ResourceType.REPOSITORY, name="One"),
            actor=self.actor,
        )
        second = self.service.create(
            ResourceCreate(resource_type=ResourceType.REPOSITORY, name="Two"),
            actor=self.actor,
        )
        self.service.bind_project(project=self.project, resource_id=first.id, actor=self.actor)
        self.service.bind_project(project=self.project, resource_id=second.id, actor=self.actor)

        with self.assertRaises(RepositoryTargetAmbiguousError):
            self.service.resolve_repository_target(self.project, actor=self.actor)

        outsider = self.service.create(
            ResourceCreate(resource_type=ResourceType.REPOSITORY, name="Outside"),
            actor=self.actor,
        )
        with self.assertRaises(RepositoryTargetUnauthorizedError):
            self.service.resolve_repository_target(
                self.project,
                actor=self.actor,
                explicit_repository_id=outsider.id,
            )

        orchestration = self.service.resolve_repository_target(
            self.project,
            actor=self.actor,
            orchestration_only=True,
        )
        self.assertIsNone(orchestration.mutable_repository_id)
        self.assertEqual(
            orchestration.source,
            RepositoryTargetSource.ORCHESTRATION_ONLY,
        )

    def test_explicit_policy_requires_contextual_target_even_for_one_repository(self) -> None:
        repository = self.service.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Only repo",
            ),
            actor=self.actor,
        )
        self.service.bind_project(
            project=self.project,
            resource_id=repository.id,
            actor=self.actor,
        )
        explicit_project = self.project.model_copy(
            update={"repository_selection_policy": "explicit"}
        )

        with self.assertRaises(RepositoryTargetMissingError):
            self.service.resolve_repository_target(
                explicit_project,
                actor=self.actor,
            )

        resolved = self.service.resolve_repository_target(
            explicit_project,
            actor=self.actor,
            explicit_repository_id=repository.id,
        )
        self.assertEqual(
            resolved.mutable_repository_id,
            repository.id,
        )

    def test_disabled_resource_cannot_resolve_for_privileged_use(self) -> None:
        resource = self.service.create(
            ResourceCreate(
                resource_type=ResourceType.CLOUD_ACCOUNT,
                name="Production account",
                risk=ResourceRisk.CRITICAL,
                aliases=[ResourceAlias(namespace="cloud", value="123456789")],
            ),
            actor=self.actor,
        )
        self.service.update(
            resource.id,
            ResourceUpdate(lifecycle=ResourceLifecycle.DISABLED),
            actor=self.actor,
        )
        with self.assertRaises(ResourceNotFoundError):
            self.service.resolve(resource.id, actor=self.actor)

    def test_execution_contract_carries_stable_resource_targets(self) -> None:
        repository = self.service.create(
            ResourceCreate(resource_type=ResourceType.REPOSITORY, name="Repo"),
            actor=self.actor,
        )
        environment = self.service.create(
            ResourceCreate(resource_type=ResourceType.ENVIRONMENT, name="Prod"),
            actor=self.actor,
        )
        state = WorkItemState(
            ref="group/app#42",
            project_id="home",
            project_path="group/app",
            resource_ids=[repository.id, environment.id, repository.id],
            current_owner="james",
            current_stage="implementation_active",
            artifact_state="branch",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )

        contract = execution_contract_for_work_item(
            state,
            ROLE_CONTRACTS["james"],
        )

        self.assertEqual(contract.schema_version, "1.4")
        self.assertEqual(
            contract.target.resource_ids,
            (repository.id, environment.id),
        )


if __name__ == "__main__":
    unittest.main()
