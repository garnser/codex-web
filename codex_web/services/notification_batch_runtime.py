"""Canonical configuration and authority resolution for owned batch admission."""

import hashlib
import json

from fastapi import HTTPException

from codex_web.authority import (
    AuthorityEvaluationRequest,
    AuthorityLevel,
    AuthorityDecisionOutcome,
)
from codex_web.configuration import (
    ConfigurationContext,
    ConfigurationScope,
    ConfigurationSpec,
    ConfigurationValueKind,
)
from codex_web.notification_batch_controls import NotificationBatchControlSnapshot
from codex_web.resources import ResourceLifecycle, ResourceType

ENABLED = "execution.notification_batches.enabled"
MAX_MEMBERS = "execution.notification_batches.max_members"
MAX_BYTES = "execution.notification_batches.max_context_bytes"


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def install_notification_batch_configuration(service):
    scopes = [
        ConfigurationScope.DEPLOYMENT,
        ConfigurationScope.ORGANIZATION,
        ConfigurationScope.WORKSPACE,
        ConfigurationScope.PROJECT,
    ]
    specs = (
        ConfigurationSpec(
            key=ENABLED,
            value_kind=ConfigurationValueKind.BOOLEAN,
            default=False,
            allowed_scopes=scopes,
            category="Execution & Recovery",
            feature_flag=True,
            kill_switch_capable=True,
            description="Admit compatible pinned provider notifications as lossless batches. Enable only after full-writer deployment and recovery qualification; grants no authority.",
        ),
        ConfigurationSpec(
            key=MAX_MEMBERS,
            value_kind=ConfigurationValueKind.INTEGER,
            default=4,
            minimum=2,
            maximum=16,
            allowed_scopes=scopes,
            category="Execution & Recovery",
            description="Maximum preserved originals in one notification admission; individual outcomes still require reconciliation.",
        ),
        ConfigurationSpec(
            key=MAX_BYTES,
            value_kind=ConfigurationValueKind.INTEGER,
            default=65536,
            minimum=256,
            maximum=262144,
            allowed_scopes=scopes,
            category="Execution & Recovery",
            description="Maximum UTF8 bytes for the complete notification input, including its admission envelope. Existing profile budgets remain enforced.",
        ),
    )
    return tuple(service.register_spec(spec) for spec in specs)


class NotificationBatchRuntimeControls:
    def __init__(
        self,
        *,
        configuration,
        projects,
        profiles,
        execution_profiles,
        authority,
        resources,
        actor_resolver,
        work_item,
    ):
        self.configuration, self.projects, self.profiles = (
            configuration,
            projects,
            profiles,
        )
        self.execution_profiles, self.authority, self.resources = (
            execution_profiles,
            authority,
            resources,
        )
        self.actor_resolver, self.work_item = actor_resolver, work_item

    def policy_for(self, actor, project):
        if (actor.organization_id, actor.workspace_id) != (
            project.organization_id,
            project.workspace_id,
        ):
            return {"enabled": False}
        context = ConfigurationContext(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=project.id,
            subject_id=actor.identity_id,
        )
        records = [
            self.configuration.resolve(key, context)
            for key in (ENABLED, MAX_MEMBERS, MAX_BYTES)
        ]
        pins = [
            canonical_json(
                {
                    "key": record.key,
                    "record_id": record.record_id,
                    "revision": record.revision,
                    "scope_type": record.scope_type,
                    "scope_id": record.scope_id,
                    "source": record.source,
                    "value": record.value,
                }
            )
            for record in records
        ]
        return {
            "enabled": records[0].value is True,
            "max_members": records[1].value,
            "max_context_bytes": records[2].value,
            "configuration_pins_json": tuple(pins),
        }

    def resolve(self, item, project):
        try:
            return self._resolve(item, project)
        except (HTTPException, ValueError, LookupError, RuntimeError, PermissionError):
            # Missing/revoked/conflicting control-plane state cannot authorize
            # grouping. The ordinary admission path keeps its own refusal rules.
            return None

    def resolve_processing(self, item, project):
        # Explicit processing consumes no admission/model budget and does not
        # depend on enabling autonomous admission. It still resolves live scope,
        # exact profile access, Definitions, resources and Role authority.
        try:
            return self._resolve(item, project, processing=True)
        except (HTTPException, ValueError, LookupError, RuntimeError, PermissionError):
            return None

    def _resolve(self, item, project, *, processing=False):
        project = self.projects.get(project.id)
        actor = self.actor_resolver(item.agent_profile_actor_id, project)
        policy = self.policy_for(actor, project)
        if not processing and not policy["enabled"]:
            return None
        work = self.work_item(item.work_item_ref)
        if (work.organization_id, work.workspace_id, work.project_id) != (
            project.organization_id,
            project.workspace_id,
            project.id,
        ) or (not processing and work.handoff is not None):
            return None
        profile, access = self.profiles.resolve_for_execution(
            item.agent_profile_id,
            actor=actor,
            project_id=project.id,
            revision=item.agent_profile_revision,
        )
        execution, execution_ref = self.execution_profiles.resolve(
            item.execution_profile_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=project.id,
        )
        if not access.allowed or (
            profile.execution_profile_id
            and profile.execution_profile_id != execution.id
        ):
            return None
        associated_repositories = tuple(
            resource_id
            for resource_id in work.resource_ids
            if self.resources.get(resource_id, actor).resource_type
            == ResourceType.REPOSITORY
        )
        resource_ids = tuple(
            dict.fromkeys(
                (
                    *item.writable_repository_resource_ids,
                    *item.read_only_repository_resource_ids,
                    *associated_repositories,
                    *(
                        (item.repository_resource_id,)
                        if item.repository_resource_id
                        else ()
                    ),
                )
            )
        )
        if set(item.writable_repository_resource_ids) & set(
            item.read_only_repository_resource_ids
        ) or (
            item.writable_repository_resource_ids
            and item.repository_resource_id
            and item.repository_resource_id not in item.writable_repository_resource_ids
        ):
            return None
        bound = set(self.resources.resource_ids_for_project(project))
        resource_proofs = []
        for resource_id in resource_ids:
            resource = self.resources.get(resource_id, actor)
            if (
                resource_id not in bound
                or resource.resource_type != ResourceType.REPOSITORY
                or resource.lifecycle != ResourceLifecycle.ACTIVE
            ):
                return None
            resource_proofs.append(
                canonical_json(
                    resource.model_dump(
                        mode="json",
                        include={
                            "id",
                            "organization_id",
                            "workspace_id",
                            "resource_type",
                            "lifecycle",
                            "risk",
                            "sensitivity",
                            "updated_at",
                        },
                    )
                )
            )
        if execution.repository_access == "none":
            if (
                item.writable_repository_resource_ids
                or item.read_only_repository_resource_ids
            ):
                return None
            self.resources.resolve_repository_target(
                project, actor=actor, orchestration_only=True
            )
        elif len(item.writable_repository_resource_ids) <= 1:
            target = self.resources.resolve_repository_target(
                project,
                actor=actor,
                explicit_repository_id=item.repository_resource_id,
                work_item_resource_ids=work.resource_ids,
                work_item_ref=work.ref,
                read_only_repository_ids=item.read_only_repository_resource_ids,
                orchestration_only=False,
            )
            if (
                execution.repository_access != "none"
                and target.mutable_repository_id != item.repository_resource_id
            ):
                return None
        elif not set(associated_repositories).issubset(
            {
                *item.writable_repository_resource_ids,
                *item.read_only_repository_resource_ids,
            }
        ):
            return None
        max_bytes = min(
            policy["max_context_bytes"],
            profile.budgets.max_input_tokens or policy["max_context_bytes"],
        )
        if max_bytes < 256:
            return None
        decision = self.authority.evaluate(
            AuthorityEvaluationRequest(
                capability="api.threads.write",
                level=AuthorityLevel.EXECUTE,
                project_id=project.id,
                resource_ids=resource_ids,
                input_tokens=0 if processing else max_bytes,
                output_tokens=0 if processing else profile.budgets.max_output_tokens,
                amount_usd=0 if processing else profile.budgets.max_cost_usd,
                model_calls=0 if processing else 1,
            ),
            actor=actor,
        )
        if (
            decision.outcome != AuthorityDecisionOutcome.ALLOW
            or decision.definition_ref is None
        ):
            return None
        stable_decision = decision.model_dump(
            mode="json", exclude={"id", "evaluated_at"}
        )
        return NotificationBatchControlSnapshot(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=project.id,
            actor_id=actor.identity_id,
            agent_profile_id=profile.profile_id,
            agent_profile_revision=profile.revision,
            agent_profile_record_id=profile.record_id,
            agent_profile_sha256=hashlib.sha256(
                canonical_json(profile.model_dump(mode="json")).encode()
            ).hexdigest(),
            execution_profile_id=execution.id,
            execution_definition_ref=execution_ref,
            authority_definition_ref=decision.definition_ref,
            configuration_pins_json=policy["configuration_pins_json"],
            authority_decision_json=canonical_json(stable_decision),
            profile_access_json=canonical_json(access.model_dump(mode="json")),
            actor_scope_json=canonical_json(
                actor.model_dump(
                    mode="json",
                    include={
                        "identity_id",
                        "principal_kind",
                        "organization_id",
                        "workspace_id",
                        "roles",
                        "team_ids",
                    },
                )
            ),
            project_scope_json=canonical_json(
                project.model_dump(
                    mode="json",
                    exclude={
                        "name",
                        "authoritative_task_source",
                        "delivery_supervision",
                    },
                )
            ),
            resource_proofs_json=tuple(resource_proofs),
            max_members=policy["max_members"],
            max_context_bytes=max_bytes,
        )
