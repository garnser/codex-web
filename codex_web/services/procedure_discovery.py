"""Deterministic procedure discovery over the canonical Skill registry.

The caller supplies a canonical operation context, never model-extracted facts.
Missing facts and failed reads are blockers, not evidence of no match. This
service does not execute steps or make policy/approval decisions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from codex_web.definitions import DefinitionReference
from codex_web.definitions import (
    DefinitionLifecycle, DefinitionScope, reference_for,
)
from codex_web.procedure_discovery import ProcedureCandidate, ProcedureDiscovery
from codex_web.resources import ResourceLifecycle
from codex_web.services.skills import SkillNotFound, SkillResolutionFailure
from codex_web.services.definitions import DefinitionCompatibilityError
from codex_web.services.action_providers import ActionRequirementError
from codex_web.skills import SKILL_DEFINITION_KIND, SkillDefinition


def checksum(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class OperationContext:
    """Core-resolved context, not authority supplied by procedure instructions."""
    actor: Any
    project_id: str | None
    resource_ids: tuple[str, ...]
    operation: str | None
    task: str | None = None
    environment: str | None = None
    resource_tags: dict[str, tuple[str, ...]] = field(default_factory=dict)
    service_versions: dict[str, dict[str, str]] = field(default_factory=dict)
    provider_capabilities: tuple[str, ...] | None = None
    worker_capabilities: tuple[str, ...] | None = None
    capability_sources: tuple[str, ...] = ()
    capability_reasons: tuple[str, ...] = ()
    resolution_blockers: tuple[str, ...] = ()
    operation_capabilities: tuple[str, ...] = ()
    provider_type: str | None = None
    provider_instance: str | None = None
    provider_contract_version: str | None = None
    policy_source: str = "not-evaluated"
    policy_allows: bool = False
    enforcement: str = "unsupported"
    request_checksum: str | None = None
    required_skill_refs: tuple[DefinitionReference, ...] = ()


class ProcedureAdmissionError(ActionRequirementError):
    def __init__(self, discovery: ProcedureDiscovery | None = None, *, code=None, message=None):
        self.discovery = discovery
        self.procedure_discovery = discovery.model_dump(mode="json") if discovery is not None else None
        self.code = code or ("procedure_" + discovery.outcome if discovery is not None else "procedure_failed")
        super().__init__(message or ("; ".join(discovery.blockers) if discovery is not None else self.code))


class ProcedureDiscoveryService:
    def __init__(self, skills, resources, *, clock=None, max_candidates=256,
                 max_context_characters=12000, supporting_context: Callable | None = None, projects=None, execution_capabilities=None):
        self.skills = skills
        self.resources = resources
        self.projects = projects
        self.execution_capabilities = execution_capabilities
        self.clock = clock or skills.clock
        self.max_candidates = max_candidates
        self.max_context_characters = max_context_characters
        # An existing authorized bounded retrieval/composition service may be
        # injected. No ambient repository read, network fetch or second store.
        self.supporting_context = supporting_context

    @staticmethod
    def _visible(record, context):
        actor = context.actor
        return (
            record.scope_type == DefinitionScope.GLOBAL
            or record.scope_type == DefinitionScope.ORGANIZATION and record.scope_id == actor.organization_id
            or record.scope_type == DefinitionScope.WORKSPACE and record.scope_id == actor.workspace_id
            or record.scope_type == DefinitionScope.PROJECT and record.scope_id == context.project_id
        )

    def discover(self, context: OperationContext, *, previous: ProcedureDiscovery | None = None):
        """Return metadata plus bounded untrusted input; never throw lookup as no-match."""
        actor = context.actor
        now = float(self.clock())
        fingerprint = checksum({
            "actor": actor.identity_id, "organization": actor.organization_id,
            "workspace": actor.workspace_id, "project": context.project_id,
            "resources": sorted(set(context.resource_ids)), "operation": context.operation,
            "task": context.task, "environment": context.environment,
            "request_checksum": context.request_checksum,
            "required_skill_refs": [ref.model_dump(mode="json") for ref in context.required_skill_refs],
            "tags": context.resource_tags, "versions": context.service_versions,
            "provider_capabilities": sorted(context.provider_capabilities) if context.provider_capabilities is not None else None,
            "worker_capabilities": sorted(context.worker_capabilities) if context.worker_capabilities is not None else None,
            "operation_capabilities": sorted(context.operation_capabilities),
            "capability_sources": context.capability_sources,
            "capability_reasons": context.capability_reasons,
            "resolution_blockers": context.resolution_blockers,
            "provider": (context.provider_type, context.provider_instance, context.provider_contract_version),
            "policy": (context.policy_source, context.policy_allows), "enforcement": context.enforcement,
        })
        candidates = []
        selected = []
        blockers = []
        outcome = "no_match"
        guidance = ""
        sources = ()

        def result():
            return ProcedureDiscovery(
                actor_identity_id=actor.identity_id, organization_id=actor.organization_id,
                workspace_id=actor.workspace_id, project_id=context.project_id,
                resource_ids=tuple(sorted(set(context.resource_ids))), operation=context.operation,
                context_checksum=fingerprint, evaluated_at=now, outcome=outcome,
                selected=tuple(selected), candidates=tuple(candidates), blockers=tuple(blockers),
                policy_source=context.policy_source, policy_allows=context.policy_allows,
                instruction_checksum=checksum(guidance) if guidance else None,
                instruction_characters=len(guidance), supporting_sources=sources,
                capability_sources=context.capability_sources,
            ), guidance

        try:
            if context.resolution_blockers:
                outcome = "failed"
                blockers.extend(context.resolution_blockers)
                return result()
            if self.projects is not None and context.project_id is not None:
                self.projects.get(context.project_id, scope=actor.tenant)
            if len(context.resource_ids) > 32:
                raise ValueError("procedure_target_limit")
            resources = [self.resources.get(r, actor) for r in dict.fromkeys(context.resource_ids)]
            if any(r.lifecycle != ResourceLifecycle.ACTIVE for r in resources):
                raise ValueError("target Resource is not active")
            if len(context.required_skill_refs) > 32:
                raise ValueError("procedure_required_skill_limit")
            for required in context.required_skill_refs:
                try:
                    self.skills.validate_reference(required, actor=actor, project_id=context.project_id)
                    required_record = self.skills.definitions.get_record(required.record_id)
                    if required_record.payload.get("operational_procedure") is not None:
                        self.skills.validate_required_procedure(required, actor=actor, project_id=context.project_id)
                except Exception as exc:
                    reason = exc.code if isinstance(exc, SkillResolutionFailure) else "required_skill_unavailable"
                    candidates.append(ProcedureCandidate(reference=required, outcome="blocked", reasons=(reason,)))
                    blockers.append(reason)
                    outcome = "inaccessible"
            if blockers:
                return result()
            records = [r for r in self.skills.definitions.list_records(kind=SKILL_DEFINITION_KIND)
                       if self._visible(r, context)
                       and (r.published_at is not None or r.lifecycle in {
                           DefinitionLifecycle.PUBLISHED, DefinitionLifecycle.DISABLED, DefinitionLifecycle.QUARANTINED})]
            # Consider latest governed history in each slot, including expired
            # and quarantined tombstones. Filtering to active only would turn
            # an inaccessible required procedure into a false no-match.
            slots = {}
            for r in records:
                key = (r.definition_id, r.scope_type, r.scope_id)
                if key not in slots or r.revision > slots[key].revision:
                    slots[key] = r
            if len(slots) > self.max_candidates:
                raise ValueError("procedure_candidate_limit: narrow canonical scope before discovery")
            precedence = {DefinitionScope.GLOBAL: 0, DefinitionScope.ORGANIZATION: 1,
                          DefinitionScope.WORKSPACE: 2, DefinitionScope.PROJECT: 3}
            effective = {}
            for r in slots.values():
                if r.definition_id not in effective or precedence[r.scope_type] > precedence[effective[r.definition_id].scope_type]:
                    effective[r.definition_id] = r
            for slot in slots:
                active = [r for r in records if (r.definition_id, r.scope_type, r.scope_id) == slot
                          and r.lifecycle == DefinitionLifecycle.PUBLISHED]
                if len(active) > 1:
                    raise ValueError("ambiguous_published_procedure_slot")
            eligible = []
            for record in sorted(effective.values(), key=lambda r: (r.definition_id, r.record_id)):
                # Verify selectors before trusting them to reject a candidate.
                # Engine compatibility is relevant only after applicability.
                record = self.skills.definitions.validate_record_integrity(record)
                skill = SkillDefinition.model_validate(record.payload)
                p = skill.operational_procedure
                if p is None:
                    continue
                ref = reference_for(record)
                from codex_web.services.native_procedure_preparation import current_preparation
                physical_proof = current_preparation()
                if (not resources and context.operation is None and physical_proof is not None
                        and physical_proof.excludes(ref, context)):
                    candidates.append(ProcedureCandidate(reference=ref, outcome="rejected",
                        reasons=("native_physical_scope_empty",)))
                    continue
                rejected = []
                # Trigger matching also uses canonical capability requirements:
                # changing a provider/action name cannot bypass a procedure
                # governing the same privileged operation on the same target.
                operation_matches = context.operation is None or context.operation in p.operation_triggers or any(
                    s.action and s.action.action_id == context.operation for s in p.steps
                ) or bool(set(context.operation_capabilities) & {
                    permission.capability for s in p.steps for permission in s.permissions})
                task_matches = context.task is None or context.task in p.task_triggers
                if not operation_matches and not task_matches:
                    rejected.append("operation_and_task_do_not_match")
                # A missing task is not an unconditional match when an exact
                # operation is known. Native free text is deliberately unknown.
                if context.operation is not None and context.task is None and not operation_matches:
                    rejected.append("operation_does_not_match")
                selector_resources = {}
                unknown = []
                for selector in p.resource_selectors:
                    matches = [r for r in resources if r.resource_type in selector.resource_types
                               and (not selector.resource_ids or r.id in selector.resource_ids)]
                    if selector.tags:
                        known = [r for r in matches if r.id in context.resource_tags]
                        if len(known) < len(matches):
                            unknown.append("resource_tags_unresolved")
                        matches = [r for r in matches if r.id not in context.resource_tags
                                   or set(selector.tags).issubset(context.resource_tags[r.id])]
                    selector_resources[selector.id] = matches
                    if resources and not matches:
                        rejected.append("resource_selector_does_not_match:" + selector.id)
                if not resources:
                    unknown.append("target_resources_unresolved")
                if context.environment is None:
                    unknown.append("environment_unresolved")
                elif context.environment not in p.environments:
                    rejected.append("environment_does_not_match")
                for interval in p.service_versions:
                    for r in selector_resources[interval.resource_selector]:
                        v = context.service_versions.get(r.id, {}).get(interval.service)
                        if v is None:
                            unknown.append("service_version_unresolved:" + interval.service)
                        else:
                            version = tuple(map(int, v.split('.')))
                            if len(version) != 3:
                                raise ValueError("invalid canonical service version")
                            if not tuple(map(int, interval.min_version.split('.'))) <= version <= tuple(map(int, interval.max_version.split('.'))):
                                rejected.append("service_version_does_not_match:" + interval.service)
                if rejected:
                    candidates.append(ProcedureCandidate(reference=ref, outcome="rejected", reasons=tuple(sorted(set(rejected)))))
                    continue
                reasons = list(dict.fromkeys(unknown))
                try:
                    self.skills.definitions._validated_record(record)
                except DefinitionCompatibilityError:
                    reasons.append("procedure_incompatible")
                try:
                    self.skills.validate_required_procedure(ref, actor=actor, project_id=context.project_id)
                except SkillResolutionFailure as exc:
                    reasons.append(exc.code)
                except SkillNotFound:
                    reasons.append("procedure_inaccessible")
                if context.provider_type is not None:
                    operational_steps = [s for s in p.steps if s.action and (
                        s.action.action_id == context.operation or set(context.operation_capabilities) & {
                            permission.capability for permission in s.permissions})]
                    if any((s.action.provider_type, s.action.provider_instance, s.action.provider_contract_version)
                           != (context.provider_type, context.provider_instance, context.provider_contract_version)
                           for s in operational_steps):
                        reasons.append("procedure_provider_enforcement_unsupported")
                for kind, required, available in (
                    ("provider", p.required_provider_capabilities, context.provider_capabilities),
                    ("worker", p.required_worker_capabilities, context.worker_capabilities),
                ):
                    if required and available is None:
                        reasons.append(kind + "_capabilities_unresolved")
                        reasons.extend(reason for reason in context.capability_reasons if reason.startswith(kind + ":"))
                    elif required and not set(required).issubset(available):
                        reasons.append(kind + "_capabilities_missing")
                if reasons:
                    candidates.append(ProcedureCandidate(reference=ref, outcome="blocked", reasons=tuple(reasons)))
                    blockers.extend(reasons)
                    outcome = "inaccessible"
                    continue
                eligible.append((record, p))
            if blockers:
                return result()
            groups = {}
            for record, p in eligible:
                groups.setdefault(p.conflict_group, []).append((record, p))
            for group, matches in sorted(groups.items()):
                highest = max(p.precedence for _, p in matches)
                winners = [(r, p) for r, p in matches if p.precedence == highest]
                if len(winners) != 1:
                    outcome = "ambiguous"
                    blockers.append("equal_precedence_conflict:" + group)
                    candidates.extend(ProcedureCandidate(reference=reference_for(r), outcome="conflict", reasons=("equal_precedence_conflict:" + group,)) for r, _ in winners)
                else:
                    r, p = winners[0]
                    selected.append(reference_for(r))
                    candidates.append(ProcedureCandidate(reference=reference_for(r), outcome="selected", reasons=("applicability_matched",)))
                for r, p in matches:
                    if p.precedence < highest:
                        candidates.append(ProcedureCandidate(reference=reference_for(r), outcome="rejected", reasons=("lower_precedence:" + group,)))
            if blockers:
                selected.clear()
                return result()
            if previous and previous.context_checksum == fingerprint and previous.selected != tuple(selected):
                outcome = "inaccessible"
                blockers.append("procedure_revision_changed: re-admit and retain the previous discovery")
                return result()
            if selected:
                outcome = "matched"
                selection = self.skills.context_for_refs(tuple(selected), actor=actor,
                    objective=context.operation or context.task or "", max_chars=self.max_context_characters)
                # Consult the declarative contract as well as ordinary Skill
                # instructions. The selected operation's dependencies remain
                # descriptive data, never an implicit step execution plan.
                contracts = "\n" + json.dumps({r.definition_id: {
                    "reference": reference_for(r).model_dump(mode="json"),
                    "operational_procedure": p.model_dump(mode="json"),
                } for r, p in eligible if reference_for(r) in selected}, sort_keys=True)
                if selection.truncated or len(selection.text) + len(contracts) > self.max_context_characters:
                    outcome = "inaccessible"
                    blockers.append("procedure_context_limit: required instructions cannot be truncated")
                    return result()
                guidance = selection.text + contracts
                sources = tuple(selection.definition_record_ids) + tuple(selection.included_assets)
                if self.supporting_context is not None:
                    support = self.supporting_context(context, tuple(selected), self.max_context_characters - 1024 - len(guidance))
                    # Existing retrieval must return its canonical sources and
                    # a bounded authorized selection, never an unbounded corpus.
                    guidance += support.text
                    sources += tuple(support.source_refs)
                if len(guidance) > self.max_context_characters:
                    guidance = ""
                    outcome = "failed"
                    blockers.append("procedure_context_limit")
                    return result()
            from codex_web.services.native_procedure_preparation import current_preparation
            proof = current_preparation()
            provider_preparation = (
                context.enforcement in {"bounded_read_only", "isolated_preparation"}
                and proof is not None and not blockers and proof.permits(tuple(selected), context))
            if not provider_preparation and context.enforcement != "canonical_action" and (selected or context.enforcement not in {"bounded_read_only", "isolated_preparation", "canonical_rollback"}):
                outcome = "unsupported_enforcement"
                blockers.append("procedure_tool_enforcement_unsupported: use a canonical ActionIntent operation or policy-authorized isolated preparation")
                blockers.extend(context.capability_reasons)
            if guidance:
                from codex_web.security import TrustZone, envelope_untrusted, render_untrusted_content
                guidance = render_untrusted_content(envelope_untrusted(TrustZone.TOOL_OUTPUT, "canonical:procedure-discovery", guidance))
                if len(guidance) > self.max_context_characters:
                    guidance = ""
                    outcome = "failed"
                    blockers.append("procedure_context_limit")
            return result()
        except Exception as exc:
            # Preserve only a core failure type. Arbitrary reader exceptions can
            # contain credentials or tenant data; they are not guidance.
            outcome = "failed"
            guidance = ""
            selected.clear()
            blockers.append("procedure_discovery_failed:" + type(exc).__name__)
            return result()

    def require(self, context, *, previous=None):
        discovery, guidance = self.discover(context, previous=previous)
        if not discovery.admitted:
            raise ProcedureAdmissionError(discovery)
        return discovery, guidance

    def assignment_context(self, context, assignment, *, actor):
        from dataclasses import replace
        context = replace(context, required_skill_refs=tuple(getattr(assignment, "skill_refs", ())))
        if self.execution_capabilities is None:
            return replace(context, provider_capabilities=None, worker_capabilities=None,
                capability_reasons=("provider:capability_resolver_unavailable", "worker:capability_resolver_unavailable"))
        return self.execution_capabilities.resolve(context, assignment, actor=actor)

    def resource_facts(self, resource_ids, actor):
        resources = [self.resources.get(r, actor) for r in resource_ids]
        facts = {r.id: r.operational_context for r in resources}
        environments = {f.environment for f in facts.values() if f is not None and f.environment is not None}
        environment = next(iter(environments)) if len(environments) == 1 and all(
            f is not None and f.environment is not None for f in facts.values()) else None
        return dict(environment=environment,
            resource_tags={k: f.tags for k, f in facts.items() if f is not None},
            service_versions={k: f.service_versions for k, f in facts.items() if f is not None})

    def action_context(self, *, actor, request, definition, provider, policy_source, policy_allows):
        return OperationContext(
            actor=actor, project_id=request.project_id, resource_ids=request.resource_ids,
            operation=request.action_id, **self.resource_facts(request.resource_ids, actor),
            operation_capabilities=definition.required_authority,
            provider_type=provider.provider_type, provider_instance=provider.provider_instance,
            provider_contract_version=provider.contract_version,
            policy_source=policy_source, policy_allows=policy_allows,
            enforcement="canonical_action", request_checksum=checksum({"parameters": request.parameters, "dry_run": request.dry_run, "credential_ref": request.credential_ref}),
        )


_INPUT_ACTOR_UNSET = object()


class NativeProcedureAdmissionService:
    """Retain authorized isolated no-match work; refuse unmediated operations.

    The common native start boundary covers queued, delegated, WorkItem and
    resumed turns. A provider/model change cannot remove this callback. Required
    operational procedures need a canonical pre-mutation hook, never prompt-only
    enforcement or repeated user confirmation.
    """
    def __init__(self, discovery, workers, history, control_actor, execution_profiles=None):
        self.discovery = discovery
        self.workers = workers
        self.history = history
        self.control_actor = control_actor
        self.execution_profiles = execution_profiles
        self.input_authorizer = None
        self.native_session_attestor = None
        self.procedure_execution = None

    def _native_policy(self, assignment, actor, project, sandbox):
        """Existing exact profile permits isolated preparation, never host operations.

        Tool-level operational enforcement is unavailable. A successful no-match
        may retain the already authorized local preparation envelope only when
        the pinned profile, worker fence and workspace lease prove that envelope.
        """
        import time
        from codex_web.resources import ResourceType
        if assignment is None or self.execution_profiles is None:
            return "native_tools", "canonical:execution-profile-unresolved"
        try:
            profile, ref = self.execution_profiles.resolve(assignment.execution_profile_id,
                organization_id=actor.organization_id, workspace_id=actor.workspace_id, project_id=project.id)
            if ref != assignment.execution_profile_definition:
                return "native_tools", "canonical:execution-profile-pin-changed"
            source = f"definition:{ref.record_id}@{ref.revision}#{ref.checksum}"
            observed = time.time()
            if ((assignment.organization_id, assignment.workspace_id, assignment.project_id) !=
                    (actor.organization_id, actor.workspace_id, project.id)
                or assignment.sandbox != sandbox or sandbox not in {"read-only", "workspace-write", "danger-full-access"}
                or assignment.network.enabled or profile.network_enabled or profile.host_mutation
                or assignment.status not in {"claimed", "running"} or assignment.lease is None
                or assignment.lease.expires_at <= observed or assignment.lease.fence != assignment.fence
                or assignment.lease.worker_id != assignment.assigned_worker_id
                or (assignment.deadline_at is not None and assignment.deadline_at <= observed)):
                return "native_tools", source
            if not assignment.execution_workspace_id or self.workers.workspaces is None:
                return "native_tools", source
            # The ingress actor (for example Slack's service principal) is not
            # the workspace owner. Inspect only this canonical assignment's
            # workspace through the existing control-plane read boundary;
            # retain the requesting actor for discovery and all decisions.
            workspace = self.workers.workspaces.get(assignment.execution_workspace_id, self.control_actor)
            lease = self.workers.workspaces.store.lease(workspace.lease_id)
            if ((workspace.organization_id, workspace.workspace_id) !=
                    (actor.organization_id, actor.workspace_id)
                or workspace.status != "active" or workspace.execution_id != assignment.execution_id
                or workspace.project_id != project.id or workspace.subject != assignment.subject
                or set(assignment.resource_ids) - set(workspace.resource_ids)
                or lease is None or lease.released_at is not None or lease.expires_at <= observed
                or lease.execution_workspace_id != workspace.id or lease.execution_id != assignment.execution_id):
                return "native_tools", source
            targets = [self.discovery.resources.get(r, actor) for r in assignment.resource_ids]
            # A frozen READ context is metadata for canonical broker operations,
            # not a database credential, mount or native mutation capability.
            # The binding/session boundary revalidates current requester/owner
            # authority; retain its exact context and live READ lease here.
            context = assignment.resource_context
            context_ids = set(context.read_only_resource_ids) if context is not None else set()
            if (workspace.resource_context != context
                or set(workspace.read_only_resource_ids) != context_ids
                or (context is not None and context.requester_identity_id != actor.identity_id)
                or not context_ids.issubset(assignment.resource_ids)
                or any(lease.resource_modes.get(r) != "read" for r in context_ids)
                or {r.id for r in targets if r.resource_type != ResourceType.REPOSITORY} != context_ids):
                return "native_tools", source
            if profile.workspace_mode == "repository":
                if workspace.kind != "git_worktree" or not targets or profile.repository_access == "none":
                    return "native_tools", source
                if sandbox != "read-only" and (profile.repository_access != "mutable"
                    or not workspace.writable_repository_ids
                    or set(workspace.writable_repository_ids) - set(assignment.resource_ids)
                    or any(lease.resource_modes.get(r) != "write" for r in workspace.writable_repository_ids)
                    or (assignment.repository_scope is not None and
                        set(assignment.repository_scope.writable_repository_ids) != set(workspace.writable_repository_ids))):
                    return "native_tools", source
            elif workspace.kind != "scratch" or targets or profile.repository_access != "none":
                return "native_tools", source
            return ("bounded_read_only" if sandbox == "read-only" else "isolated_preparation"), source
        except Exception:
            return "native_tools", "canonical:execution-policy-unavailable"

    def _preparation_context(self, assignment, actor, project, sandbox, request_checksum, *, require_operations=True):
        enforcement, source = self._native_policy(assignment, actor, project, sandbox)
        if enforcement not in {"bounded_read_only", "isolated_preparation"}:
            raise ValueError("native preparation policy changed")
        profile, _ = self.execution_profiles.resolve(assignment.execution_profile_id,
            organization_id=actor.organization_id, workspace_id=actor.workspace_id, project_id=project.id)
        if require_operations and not {"procedure.read", "procedure.start", "procedure.advance"}.issubset(profile.control_plane_operations):
            raise ValueError("current execution Definition does not declare procedure operations")
        context = OperationContext(actor=actor, project_id=project.id,
            resource_ids=assignment.resource_ids, operation=None, request_checksum=request_checksum,
            policy_source=source, policy_allows=True, enforcement=enforcement,
            **self.discovery.resource_facts(assignment.resource_ids, actor))
        context = self.discovery.assignment_context(context, assignment, actor=actor)
        if context.worker_capabilities is None or context.provider_capabilities is None:
            raise ValueError("native preparation capabilities unavailable")
        return context

    def _preparation_discovery(self, assignment, actor, project, sandbox, request_checksum, selected):
        from dataclasses import replace
        from codex_web.services.native_procedure_preparation import NativeProviderPreparation, native_provider_preparation
        if self.native_session_attestor is None or self.procedure_execution is None:
            raise ValueError("native provider preparation is unavailable")
        context = self._preparation_context(assignment, actor, project, sandbox, request_checksum)
        expected_context = context
        def validate():
            self.native_session_attestor(assignment, self.procedure_execution)
            self.procedure_execution.validate_native_preparation(assignment, selected, actor)
            if self._preparation_context(assignment, actor, project, sandbox, request_checksum) != expected_context:
                raise ValueError("native preparation context changed")
        validate()
        source = "canonical:native-provider-preparation:" + checksum({
            "assignment":assignment.id,"fence":assignment.fence,"execution":assignment.execution_id,
            "definitions":[ref.model_dump(mode="json") for ref in selected],"policy":context.policy_source})
        context = replace(context, policy_source=context.policy_source + ";" + source,
            capability_sources=(*context.capability_sources, source))
        proof = NativeProviderPreparation(tuple(selected), source, validate, context=context)
        with native_provider_preparation(proof):
            discovery, text = self.discovery.discover(context)
        if not discovery.admitted or tuple(discovery.selected) != tuple(selected):
            raise ProcedureAdmissionError(discovery)
        return discovery, text

    def _empty_preparation_discovery(self, assignment, actor, project, sandbox, request_checksum, references):
        from dataclasses import replace
        from codex_web.services.native_procedure_preparation import NativeProviderPreparation, native_provider_preparation
        if self.native_session_attestor is None or self.procedure_execution is None:
            raise ValueError("physical scope is not attested")
        def validate():
            self.native_session_attestor(assignment, self.procedure_execution)
            self.procedure_execution.validate_native_preparation_owner(assignment)
            profile, _ = self.execution_profiles.resolve(assignment.execution_profile_id,
                organization_id=actor.organization_id, workspace_id=actor.workspace_id, project_id=project.id)
            workspace = self.workers.workspaces.get(assignment.execution_workspace_id, self.control_actor)
            lease = self.workers.workspaces.store.lease(workspace.lease_id)
            if (profile.workspace_mode != "scratch" or profile.repository_access != "none"
                    or assignment.resource_ids != () or assignment.resource_context is not None
                    or workspace.kind != "scratch" or workspace.resource_ids != ()
                    or workspace.resource_context is not None or workspace.read_only_resource_ids != ()
                    or workspace.writable_repository_ids != () or lease is None
                    or lease.resource_ids != () or lease.resource_modes != {}):
                raise ValueError("physical scope is not exactly empty")
            self._preparation_context(assignment, actor, project, sandbox, request_checksum, require_operations=False)
        validate()
        context = self._preparation_context(assignment, actor, project, sandbox, request_checksum, require_operations=False)
        source = "canonical:native-empty-physical-scope:" + checksum({
            "assignment":assignment.id,"fence":assignment.fence,"workspace":assignment.execution_workspace_id,
            "definitions":[ref.model_dump(mode="json") for ref in references],"policy":context.policy_source})
        context = replace(context, policy_source=context.policy_source + ";" + source,
            capability_sources=(*context.capability_sources,source))
        excluded = []
        def excludes(reference):
            if reference not in references:
                return False
            try:
                self.procedure_execution.validate_native_preparation(assignment, (reference,), actor, require_targets=False)
            except Exception:
                return False
            excluded.append(reference)
            return True
        proof = NativeProviderPreparation((),source,validate,context=context,exclude_empty=excludes)
        with native_provider_preparation(proof):
            discovery, text = self.discovery.discover(context)
        if not excluded or tuple(candidate.reference for candidate in discovery.candidates) != tuple(references):
            raise ValueError("physical-scope candidates changed or are unsupported")
        return discovery, text

    def preparation_input_guard(self, discovery, *, project, actor, execution_id, assignment_id, sandbox, request_checksum):
        """A fresh read-only check at the existing guarded native send boundary."""
        def validate():
            from codex_web.runtime.execution import ExecutionPrestartRefusal
            try:
                current = self.workers.assignment_for_execution(execution_id, actor=self.control_actor)
                if current.id != assignment_id:
                    raise ValueError("native assignment changed")
                if discovery.selected:
                    fresh, _ = self._preparation_discovery(current, actor, project, sandbox, request_checksum, discovery.selected)
                else:
                    fresh, _ = self._empty_preparation_discovery(current, actor, project, sandbox, request_checksum,
                        tuple(candidate.reference for candidate in discovery.candidates))
                if not fresh.admitted:
                    raise ProcedureAdmissionError(fresh)
                if fresh.context_checksum != discovery.context_checksum:
                    raise ValueError("native preparation fingerprint changed")
            except Exception as exc:
                raise ExecutionPrestartRefusal(status_code=409, detail={
                    "code":"procedure_native_preparation_changed",
                    "message":"Current isolated provider preparation proof is unavailable"}) from exc
        return validate

    async def __call__(self, *, project, actor, thread_id, execution_id, assignment_id, sandbox, source, request_checksum=None, input_actor=_INPUT_ACTOR_UNSET):
        import asyncio
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        if actor is None or (actor.organization_id, actor.workspace_id) != (project.organization_id, project.workspace_id):
            raise ExecutionPrestartRefusal(status_code=403, detail={"code": "procedure_actor_scope_unavailable"})
        assignment = None
        if assignment_id:
            try:
                assignment = await asyncio.to_thread(self.workers.assignment_for_execution,
                    execution_id, actor=self.control_actor)
            except Exception as exc:
                raise ExecutionPrestartRefusal(status_code=409, detail={
                    "code": "procedure_assignment_unavailable",
                    "message": "Resolve a live scoped assignment before native admission.",
                }) from exc
            if assignment.id != assignment_id:
                raise ExecutionPrestartRefusal(status_code=409, detail={"code": "procedure_assignment_changed"})
        if self.input_authorizer is not None:
            from codex_web.services.identity import AuthorizationError
            try:
                await asyncio.to_thread(self.input_authorizer, assignment=assignment,
                    actor=actor if input_actor is _INPUT_ACTOR_UNSET else input_actor, project=project, source=source)
            except AuthorizationError as exc:
                raise ExecutionPrestartRefusal(status_code=403, detail={
                    'code': 'native_input_authority_denied', 'message': str(exc),
                }) from exc
        targets = assignment.resource_ids if assignment is not None else ()
        enforcement, policy_source = await asyncio.to_thread(self._native_policy, assignment, actor, project, sandbox)
        context = OperationContext(actor=actor, project_id=project.id, resource_ids=targets, operation=None,
            request_checksum=request_checksum, policy_source=policy_source, policy_allows=True, enforcement=enforcement,
            **await asyncio.to_thread(self.discovery.resource_facts, targets, actor))
        if assignment is not None:
            context = await asyncio.to_thread(self.discovery.assignment_context, context, assignment, actor=actor)
            if context.worker_capabilities is None or context.provider_capabilities is None:
                from dataclasses import replace
                context = replace(context, enforcement="native_tools")
        discovery, text = await asyncio.to_thread(self.discovery.discover, context)
        if (assignment is not None and assignment.resource_ids == () and discovery.candidates
                and context.enforcement in {"bounded_read_only", "isolated_preparation"}
                and self.native_session_attestor is not None and self.procedure_execution is not None):
            try:
                discovery, text = await asyncio.to_thread(self._empty_preparation_discovery,
                    assignment, actor, project, sandbox, request_checksum,
                    tuple(candidate.reference for candidate in discovery.candidates))
            except Exception:
                pass  # Unknown/unsupported scope retains the original refusal.
        if (assignment is not None and discovery.outcome == "unsupported_enforcement"
                and discovery.selected and len(discovery.blockers) == 1
                and discovery.blockers[0].startswith("procedure_tool_enforcement_unsupported:")
                and context.enforcement in {"bounded_read_only", "isolated_preparation"}
                and self.native_session_attestor is not None and self.procedure_execution is not None):
            try:
                discovery, text = await asyncio.to_thread(self._preparation_discovery,
                    assignment, actor, project, sandbox, request_checksum, discovery.selected)
            except Exception:
                # Preserve the genuine unsupported refusal; no weaker no-match
                # or preparation claim when any live proof is unavailable.
                pass
        await asyncio.to_thread(self.history.record_procedure_discovery, thread_id, execution_id, discovery)
        if assignment is not None:
            from codex_web.services.execution_workers import ProcedureHistoryExhaustedError
            try:
                await asyncio.to_thread(self.workers.record_procedure_discovery, assignment.id, discovery,
                    expected_fence=assignment.fence, actor=self.control_actor)
            except ProcedureHistoryExhaustedError as exc:
                message = 'This assignment has retained its maximum procedure history. Renew the assignment before retrying the retained request.'
                raise ExecutionPrestartRefusal(status_code=409, detail={
                    'code': 'execution_preflight_blocked', 'message': message,
                    'retryable': True, 'blockers': [{
                        'code': 'procedure_assignment_exhausted', 'message': message,
                        'retryable': True, 'target_type': 'execution_assignment',
                        'target_id': assignment.id, 'remediation_route': '/api/execution-workers',
                    }],
                }) from exc
        if not discovery.admitted:
            raise ExecutionPrestartRefusal(status_code=409, detail={
                "code": "procedure_" + discovery.outcome, "message": "; ".join(discovery.blockers),
                "procedure_discovery": discovery.model_dump(mode="json"), "remediation_route": "/api/skills",
            })
        return discovery, text


    async def agent_session_admission(self, session, request, actor, phase):
        # Goal continuation and provider-neutral session APIs have their own
        # start/resume/objective surface; they must not evade the native gate.
        from types import SimpleNamespace
        project = SimpleNamespace(id=session.project_id, organization_id=session.organization_id,
            workspace_id=session.workspace_id)
        # The generic session adapter has no attestation that its native
        # session is running inside the assignment's worker transport. Caller-
        # supplied assignment IDs/cwd/sandbox are not that proof. The common
        # assignment-bound TurnExecution path supports isolated no-match work;
        # this direct provider path must remain explicitly unsupported.
        sandbox = "unsupported"
        return await self(project=project, actor=actor, thread_id=session.id,
            execution_id=session.execution_id or session.id, assignment_id=session.assignment_id,
            sandbox=sandbox, source="agent-session:" + phase, request_checksum=checksum(request.model_dump(mode="json")),
            input_actor=actor)


class BrokerProcedureAdmissionService:
    """Gate canonical broker handlers as well as downstream provider actions."""
    def __init__(self, discovery, workers, control_actor):
        self.discovery = discovery
        self.workers = workers
        self.control_actor = control_actor

    def inspect_current(self, *, assignment, actor, operation, resource_ids, payload):
        from types import SimpleNamespace
        from codex_web.action_providers import ActionRequest
        context = self.discovery.action_context(actor=actor,
            request=ActionRequest(action_id=operation.id, organization_id=actor.organization_id,
                workspace_id=actor.workspace_id, project_id=assignment.project_id,
                resource_ids=resource_ids, parameters=payload),
            definition=SimpleNamespace(required_authority=(operation.capability,)),
            provider=SimpleNamespace(provider_type=None, provider_instance=None, contract_version=None),
            policy_source="policy:assignment-control-plane", policy_allows=True)
        context = self.discovery.assignment_context(context, assignment, actor=actor)
        discovered, _ = self.discovery.discover(context)
        return discovered

    async def __call__(self, *, assignment, actor, operation, resource_ids, payload):
        import asyncio
        from codex_web.services.control_plane_broker import ControlPlaneBrokerRequestError
        discovery = await asyncio.to_thread(self.inspect_current, assignment=assignment,
            actor=actor, operation=operation, resource_ids=resource_ids, payload=payload)
        from codex_web.services.execution_workers import ProcedureHistoryExhaustedError
        try:
            await asyncio.to_thread(self.workers.record_procedure_discovery, assignment.id, discovery,
                expected_fence=assignment.fence, actor=self.control_actor)
        except ProcedureHistoryExhaustedError as exc:
            # Admission precedes the broker handler. This refusal proves only
            # that THIS request did not reach it; earlier unknown dispatches
            # remain unknown and must never be retried from this observation.
            error = ControlPlaneBrokerRequestError(
                "This assignment has retained its maximum procedure history. "
                "Finish the current turn, then renew its positively idle custody "
                "through retained native admission before submitting new coordination.",
                status_code=409, code="procedure_assignment_exhausted")
            error.procedure_discovery = discovery.model_copy(update={
                "outcome": "failed",
                "blockers": (*discovery.blockers, "procedure_assignment_exhausted"),
            }).model_dump(mode="json")
            raise error from exc
        if not discovery.admitted:
            error = ControlPlaneBrokerRequestError("; ".join(discovery.blockers), status_code=409)
            error.code = "procedure_" + discovery.outcome
            error.procedure_discovery = discovery.model_dump(mode="json")
            raise error
        return discovery


class ProcedureSupportingContextService:
    """Use existing authorized Memory retrieval with canonical object filters."""
    def __init__(self, memory):
        self.memory = memory

    def __call__(self, context, references, remaining_characters):
        from types import SimpleNamespace
        from codex_web.organizational_memory import KnowledgeQuery, KnowledgeRetrievalBudget, KnowledgeCanonicalRef
        if remaining_characters < 1024 or context.project_id is None:
            return SimpleNamespace(text="", source_refs=())
        targets = tuple(KnowledgeCanonicalRef(object_type="resource", object_id=r) for r in context.resource_ids)
        targets += tuple(KnowledgeCanonicalRef(object_type="definition", object_id=ref.record_id) for ref in references)
        retrieved = self.memory.search(KnowledgeQuery(
            text=context.operation or context.task or "", project_ids=(context.project_id,),
            canonical_target_refs=targets,
            budget=KnowledgeRetrievalBudget(top_k=3, candidate_limit=32,
                max_context_tokens=max(128, min(1000, (remaining_characters - 256) // 4))),
        ), actor=context.actor)
        sources = ["memory-retrieval:" + retrieved.retrieval_id]
        parts = []
        for item in retrieved.items:
            part = f"\n[Supporting Memory {item.knowledge_id}@{item.version}]\n{item.context_excerpt}\n"
            if sum(map(len, parts)) + len(part) <= remaining_characters:
                parts.append(part)
                sources.append(f"memory:{item.knowledge_id}@{item.version}")
        return SimpleNamespace(text="".join(parts), source_refs=tuple(sources))


class ProcedureExecutionCapabilityService:
    """Read the assigned worker and exact registered runtime; never issue grants.

    Runtime registration revision is pinned by ExecutionRuntimeBinding. Actual
    runtime capabilities are intersected with current canonical provider grants.
    Worker capability evidence comes from its tenant-scoped registered record,
    not the assignment's minimum requested capabilities. Missing facts stay None.
    """
    def __init__(self, workers, runtimes, providers, *, clock=None):
        import time
        self.workers = workers
        self.runtimes = runtimes
        self.providers = providers
        self.clock = clock or time.time

    def resolve(self, context, assignment, *, actor):
        from dataclasses import replace
        from codex_web.execution_workers import AssignmentStatus, WorkerLifecycle
        observed = float(self.clock())
        sources, reasons = [], []
        worker_caps = provider_caps = None
        same_scope = (assignment.organization_id, assignment.workspace_id, assignment.project_id) == (
            actor.organization_id, actor.workspace_id, context.project_id)
        lease = assignment.lease
        live = (same_scope and assignment.status in {AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}
            and lease is not None and lease.expires_at > observed and lease.fence == assignment.fence
            and lease.worker_id == assignment.assigned_worker_id
            and (assignment.deadline_at is None or assignment.deadline_at > observed))
        if not live:
            return replace(context, worker_capabilities=None, provider_capabilities=None,
                capability_reasons=("worker:assignment_not_live", "provider:assignment_not_live"))
        try:
            worker = self.workers.store.worker(assignment.assigned_worker_id)
            if worker is None or (worker.organization_id, worker.workspace_id) != (actor.organization_id, actor.workspace_id):
                reasons.append("worker:registered_worker_unavailable")
            elif worker.lifecycle not in {WorkerLifecycle.ACTIVE, WorkerLifecycle.DRAINING}:
                reasons.append("worker:registered_worker_not_active")
            else:
                worker_caps = tuple(worker.capabilities)
                sources.append(f"worker:{worker.id}@{worker.version}#" + checksum({
                    "identity": worker.service_identity_id, "capabilities": sorted(worker_caps)}))
        except Exception:
            reasons.append("worker:registered_worker_lookup_failed")
        binding = assignment.runtime_binding
        if binding is None:
            reasons.append("provider:pinned_runtime_unavailable")
        else:
            try:
                registration = self.runtimes.registration(binding.provider_id, binding.runtime_id)
                if registration.capability_revision != binding.capability_revision:
                    reasons.append("provider:pinned_runtime_revision_changed")
                else:
                    adapter = self.runtimes.get(binding.provider_id, binding.runtime_id)
                    provider = next((row for row in self.providers.discover(actor)
                        if row.provider.id == binding.provider_id), None)
                    if provider is None:
                        reasons.append("provider:canonical_provider_unavailable")
                    elif not provider.eligible:
                        reasons.append("provider:canonical_provider_not_eligible")
                    else:
                        provider_caps = tuple(sorted(set(registration.capabilities)
                            & set(adapter.capabilities) & set(provider.effective_capabilities)))
                        sources.extend((f"runtime:{binding.provider_id}/{binding.runtime_id}@{binding.capability_revision}",
                            f"agent-provider:{provider.provider.id}@{provider.provider.revision}#" + checksum(provider_caps)))
            except Exception:
                reasons.append("provider:runtime_capability_lookup_failed")
        return replace(context, worker_capabilities=worker_caps, provider_capabilities=provider_caps,
            capability_sources=tuple(sources), capability_reasons=tuple(reasons))


def install_native_procedure_admission(turns, sessions, discovery, workers, history, control_actor, execution_profiles, procedure_execution=None):
    """One production wiring point for native turns and provider-neutral sessions."""
    admission = NativeProcedureAdmissionService(discovery, workers, history, control_actor, execution_profiles)
    admission.native_session_attestor = getattr(turns, "attest_native_provider_preparation", None)
    admission.procedure_execution = procedure_execution
    turns.procedure_admission = admission
    sessions.procedure_admission = admission.agent_session_admission
    return admission
