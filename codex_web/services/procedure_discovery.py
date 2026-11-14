"""Deterministic procedure discovery over the canonical Skill registry.

The caller supplies a canonical operation context, never model-extracted facts.
Missing facts and failed reads are blockers, not evidence of no match. This
service does not execute steps or make policy/approval decisions.
"""
from __future__ import annotations

from codex_web.services.runtime_policy import RuntimePolicy

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
from codex_web.configuration import ConfigurationScope, EffectiveConfiguration
from codex_web.services.procedure_configuration import PROCEDURE_CONTEXT_CHARACTERS


def checksum(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class _EmptyPreparationUnavailable(ValueError):
    """Opaque empty-seal observation failure; never local refusal proof."""


class _EmptyPreparationContextUnavailable(_EmptyPreparationUnavailable):
    """Explicit context-resolution refusal, not a swallowed reader failure."""


def _empty_preparation_failure(context, discovery, *, validation_failed=False):
    # discover returns these exact blockers before candidate enumeration. Its
    # catch-all instead appends procedure_discovery_failed:<type>; even matching
    # caller context text cannot promote that opaque exception to refusal proof.
    closed_context = (not validation_failed and context.resolution_blockers
        and discovery.outcome == 'failed'
        and discovery.blockers == context.resolution_blockers
        and not discovery.candidates and not discovery.selected
        and not any(reason.startswith('procedure_discovery_failed:')
                    for reason in discovery.blockers))
    failure = _EmptyPreparationContextUnavailable if closed_context else _EmptyPreparationUnavailable
    return failure('Current empty native preparation observation is unavailable')


class _EmptyPreparationCandidatesChanged(ValueError):
    """Owned consistency check, not authority inferred from exception prose."""

    def __init__(self, *, no_exclusions, references_changed):
        super().__init__("physical-scope candidates changed or are unsupported")
        self.no_exclusions = no_exclusions
        self.references_changed = references_changed


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
                 max_context_characters=12000, supporting_context: Callable | None = None, projects=None, execution_capabilities=None,
                 context_capacity_resolver: Callable[[], EffectiveConfiguration] | None = None):
        self.skills = skills
        self.resources = resources
        self.projects = projects
        self.execution_capabilities = execution_capabilities
        self.clock = clock or skills.clock
        self.max_candidates = max_candidates
        self.max_context_characters = max_context_characters
        self.context_capacity_resolver = context_capacity_resolver
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

    def discover(self, context: OperationContext, *, previous: ProcedureDiscovery | None = None,
                 _include_supporting_context: bool = True, _definitions=None, _identity=None):
        """Return metadata plus bounded untrusted input; never throw lookup as no-match."""
        actor = context.actor
        definitions = _definitions or self.skills.definitions
        read_options = {'_definitions': _definitions} if _definitions is not None else {}
        if _identity is not None:
            read_options['_identity'] = _identity
        if _definitions is not None:
            self.skills.definitions.require_read_observation(_definitions)
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
            capacity = self.context_capacity_resolver() if self.context_capacity_resolver else None
            if self.context_capacity_resolver is not None and (
                not isinstance(capacity, EffectiveConfiguration)
                or
                capacity.key != PROCEDURE_CONTEXT_CHARACTERS.key
                or capacity.scope_type not in {None, ConfigurationScope.DEPLOYMENT}
                or capacity.scope_id is not None
            ):
                raise ValueError("Wrong procedure guidance capacity configuration scope/key")
            max_context_characters = PROCEDURE_CONTEXT_CHARACTERS.validate_value(
                capacity.value if capacity is not None else self.max_context_characters)
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
                    self.skills.validate_reference(required, actor=actor, project_id=context.project_id, **read_options)
                    required_record = definitions.get_record(required.record_id)
                    if required_record.payload.get("operational_procedure") is not None:
                        self.skills.validate_required_procedure(required, actor=actor, project_id=context.project_id, **read_options)
                except Exception as exc:
                    reason = exc.code if isinstance(exc, SkillResolutionFailure) else "required_skill_unavailable"
                    candidates.append(ProcedureCandidate(reference=required, outcome="blocked", reasons=(reason,)))
                    blockers.append(reason)
                    outcome = "inaccessible"
            if blockers:
                return result()
            records = [r for r in definitions.list_records(kind=SKILL_DEFINITION_KIND)
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
                record = definitions.validate_record_integrity(record)
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
                    self.skills.validate_required_procedure(ref, actor=actor, project_id=context.project_id, **read_options)
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
                selection = self.skills.context_for_refs(tuple(selected), actor=actor, project_id=context.project_id,
                    objective=context.operation or context.task or "", max_chars=max_context_characters,
                    **read_options)
                # Consult the declarative contract as well as ordinary Skill
                # instructions. The selected operation's dependencies remain
                # descriptive data, never an implicit step execution plan.
                contracts = "\n" + json.dumps({r.definition_id: {
                    "reference": reference_for(r).model_dump(mode="json"),
                    "operational_procedure": p.model_dump(mode="json"),
                } for r, p in eligible if reference_for(r) in selected}, sort_keys=True)
                if selection.truncated or len(selection.text) + len(contracts) > max_context_characters:
                    outcome = "inaccessible"
                    blockers.append("procedure_context_limit: required instructions cannot be truncated")
                    return result()
                guidance = selection.text + contracts
                sources = tuple(selection.definition_record_ids) + tuple(selection.included_assets)
                if _include_supporting_context and self.supporting_context is not None:
                    support = self.supporting_context(context, tuple(selected), max_context_characters - 1024 - len(guidance))
                    # Existing retrieval must return its canonical sources and
                    # a bounded authorized selection, never an unbounded corpus.
                    guidance += support.text
                    sources += tuple(support.source_refs)
                if len(guidance) > max_context_characters:
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
                if len(guidance) > max_context_characters:
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

    def resource_facts(self, resource_ids, actor, *, _resources=None):
        if _resources is not None:
            self.resources.require_read_observation(_resources)
        read_options = {'_read_observation': _resources} if _resources is not None else {}
        resources = [self.resources.get(r, actor, **read_options) for r in resource_ids]
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
        self.native_session_preparer = None
        self.procedure_execution = None
        self.reasoning_preparation = None
        self.bootstrap_bindings = None

    def _current_preparation_assignment(self, expected):
        """Point-read exact custody; only normal same-owner renewal may differ.

        Do not trust an attestor's return value or reuse an expired read snapshot.
        Observational timestamps and lease renewal timestamps do not change
        custody. Every other field, including the token/acquisition and input,
        credential and procedure history, remains an exact comparison target.
        """
        current = self.workers.assignment_for_execution(expected.execution_id, actor=self.control_actor)
        if (expected.lease is None or current.lease is None
                or expected.lease.model_copy(update={
                    "expires_at": current.lease.expires_at,
                    "renewed_at": current.lease.renewed_at}) != current.lease
                or expected.model_copy(update={"lease": current.lease,
                    "updated_at": current.updated_at}) != current):
            raise ValueError("native preparation assignment custody changed")
        return current

    def _preparation_profile(self, assignment, actor, project, _definitions=None):
        profiles = self.execution_profiles
        if _definitions is not None:
            from codex_web.services.execution_profile_definitions import ExecutionProfileDefinitionService
            # A custom resolver retains its complete current guarded call.
            if (type(profiles) is ExecutionProfileDefinitionService
                    and getattr(profiles.resolve, '__self__', None) is profiles
                    and getattr(profiles.resolve, '__func__', None) is _CANONICAL_PROFILE_RESOLVE
                    and getattr(profiles.record, '__self__', None) is profiles
                    and getattr(profiles.record, '__func__', None) is _CANONICAL_PROFILE_RECORD
                    and profiles.registry is self.discovery.skills.definitions):
                self.discovery.skills.definitions.require_read_observation(_definitions)
                profiles = ExecutionProfileDefinitionService(_definitions)
        return profiles.resolve(assignment.execution_profile_id,
            organization_id=actor.organization_id, workspace_id=actor.workspace_id, project_id=project.id)

    def _native_policy(self, assignment, actor, project, sandbox, *, _definitions=None):
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
            profile, ref = self._preparation_profile(assignment, actor, project, _definitions)
            if ref != assignment.execution_profile_definition:
                return "native_tools", "canonical:execution-profile-pin-changed"
            # Definition reads may span an ordinary worker heartbeat/renewal.
            assignment = self._current_preparation_assignment(assignment)
            source = f"definition:{ref.record_id}@{ref.revision}#{ref.checksum}"
            if profile.dependency_egress:
                # No-match preparation may use its separately governed HTTPS
                # transport. Selected procedures still require the original
                # no-egress session attestor and remain unsupported here.
                source += ";dependency-egress:1.0"
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

    def _preparation_context(self, assignment, actor, project, sandbox, request_checksum, *, require_operations=True,
                             _definitions=None):
        assignment = self._current_preparation_assignment(assignment)
        enforcement, source = self._native_policy(assignment, actor, project, sandbox,
            **({'_definitions': _definitions} if _definitions is not None else {}))
        if enforcement not in {"bounded_read_only", "isolated_preparation"}:
            raise ValueError("native preparation policy changed")
        profile, _ = self._preparation_profile(assignment, actor, project, _definitions)
        if require_operations and not {"procedure.read", "procedure.start", "procedure.advance"}.issubset(profile.control_plane_operations):
            raise ValueError("current execution Definition does not declare procedure operations")
        context = OperationContext(actor=actor, project_id=project.id,
            resource_ids=assignment.resource_ids, operation=None, request_checksum=request_checksum,
            policy_source=source, policy_allows=True, enforcement=enforcement,
            **self.discovery.resource_facts(assignment.resource_ids, actor))
        assignment = self._current_preparation_assignment(assignment)
        context = self.discovery.assignment_context(context, assignment, actor=actor)
        if context.worker_capabilities is None or context.provider_capabilities is None:
            raise ValueError("native preparation capabilities unavailable")
        return context

    def _preparation_discovery(self, assignment, actor, project, sandbox, request_checksum, selected, *, validate_credentials=True,
                               _include_supporting_context=True, _diagnostics=None, _definitions=None, _identity=None):
        from dataclasses import replace
        from codex_web.services.native_procedure_preparation import NativeProviderPreparation, native_provider_preparation
        from codex_web.native_empty_preparation import closed_native_preparation_diagnostics, CANONICAL_CODES
        from codex_web.services.threads import bootstrap_exception_type
        from codex_web.services.procedure_execution import ProcedureExecutionError
        read_options = {key:value for key,value in (('_definitions',_definitions),('_identity',_identity)) if value is not None}
        def checked(stage, operation):
            try:
                return operation()
            except Exception as exc:
                if _diagnostics is not None:
                    entry = {'stage':stage,'exception_type':bootstrap_exception_type(exc)}
                    if type(exc) is ProcedureExecutionError and exc.code in CANONICAL_CODES:
                        entry['code'] = exc.code
                    _diagnostics[:] = closed_native_preparation_diagnostics([*_diagnostics,entry])
                raise
        if self.native_session_attestor is None or self.procedure_execution is None:
            raise ValueError("native provider preparation is unavailable")
        context = checked('preparation_context', lambda: self._preparation_context(
            assignment, actor, project, sandbox, request_checksum,
            **({'_definitions': _definitions} if _definitions is not None else {})))
        expected_context = context
        def validate():
            current = checked('session_attestation', lambda: self._current_preparation_assignment(assignment))
            checked('session_attestation', lambda: self._attest_native_session(current))
            # Attestation itself may span a renewal; its return is not authority.
            current = checked('native_owner', lambda: self._current_preparation_assignment(current))
            checked('native_owner', lambda: self.procedure_execution.validate_native_preparation(current, selected, actor,
                **read_options))
            if validate_credentials:
                current = checked('credential_custody', lambda: self._current_preparation_assignment(current))
                checked('credential_custody', lambda: self.procedure_execution.validate_native_credential_custody(
                    current, selected, actor, **read_options))
            def consistent_context():
                if self._preparation_context(current, actor, project, sandbox, request_checksum,
                        **({'_definitions': _definitions} if _definitions is not None else {})) != expected_context:
                    raise ValueError("native preparation context changed")
            checked('preparation_context', consistent_context)
        validate()
        source = "canonical:native-provider-preparation:" + checksum({
            "assignment":assignment.id,"fence":assignment.fence,"execution":assignment.execution_id,
            "definitions":[ref.model_dump(mode="json") for ref in selected],"policy":context.policy_source})
        context = replace(context, policy_source=context.policy_source + ";" + source,
            capability_sources=(*context.capability_sources, source))
        proof = NativeProviderPreparation(tuple(selected), source, validate, context=context)
        with native_provider_preparation(proof):
            discovery, text = checked('discovery', lambda: self.discovery.discover(context,
                _include_supporting_context=_include_supporting_context,
                **read_options))
        def consistent_selection():
            if not discovery.admitted or tuple(discovery.selected) != tuple(selected):
                raise ProcedureAdmissionError(discovery)
        checked('candidate_consistency', consistent_selection)
        return discovery, text

    def _attest_native_session(self, assignment):
        from codex_web.services.native_procedure_preparation import attest_prepared_session
        return attest_prepared_session(assignment, self.procedure_execution, self.native_session_attestor)

    def _seal_initial_credentials(self, assignment, selected, actor, validate, *, metadata_references=()):
        options = {}
        from codex_web.runtime.execution import TurnExecutionService
        attestor, preparer = self.native_session_attestor, self.native_session_preparer
        canonical_pair = (
            getattr(attestor, '__func__', None) is TurnExecutionService.attest_native_provider_preparation
            and getattr(preparer, '__func__', None) is TurnExecutionService.prepare_native_provider_preparation
            and getattr(attestor, '__self__', None) is getattr(preparer, '__self__', None))
        if canonical_pair:
            from codex_web.services.native_procedure_preparation import native_session_observation
            def prepare():
                proof = preparer(assignment, self.procedure_execution)
                if self.native_session_attestor is not attestor or self.native_session_preparer is not preparer:
                    raise ValueError('Native preparation attestor pair changed')
                return native_session_observation(proof, attestor=attestor,
                    procedures=self.procedure_execution)
            options['prepare_session'] = prepare
        owner = self.procedure_execution
        from codex_web.services.procedure_execution import (
            ProcedureExecutionService, _CANONICAL_INITIAL_CREDENTIAL_ADMIT, _CANONICAL_INITIAL_SEAL_CHECK,
            _CANONICAL_INITIAL_IDENTITY_SEAL_CHECK, _CANONICAL_INITIAL_PROJECT_SEAL_CHECK,
            _CANONICAL_INITIAL_CAPACITY_SEAL_CHECK)
        # Only the canonical owner opts into explicit data sharing. Extensions
        # keep their original call signature and complete guarded validation.
        def canonical_reads():
            return (type(self) is NativeProcedureAdmissionService and type(owner) is ProcedureExecutionService
                and self.procedure_execution is owner
                and self.native_session_attestor is attestor and self.native_session_preparer is preparer
                and getattr(owner.admit_initial_native_credentials, '__self__', None) is owner
                and getattr(owner.admit_initial_native_credentials, '__func__', None) is _CANONICAL_INITIAL_CREDENTIAL_ADMIT
                and all(getattr(getattr(self, name), '__self__', None) is self
                    and getattr(getattr(self, name), '__func__', None) is original
                    for name, original in _CANONICAL_SEAL_READS.items())
                and getattr(self.discovery.discover, '__self__', None) is self.discovery
                and getattr(self.discovery.discover, '__func__', None) is _CANONICAL_DISCOVER
                and _CANONICAL_INITIAL_SEAL_CHECK(owner))
        if canonical_pair and canonical_reads():
            def observed(reader, projects=None, capacity=None):
                def with_identity(identity):
                    read_options = {'_definitions':reader}
                    if identity is not None:
                        read_options['_identity'] = identity
                    # Prepare facts only. Current judgments still run after
                    # comparison with the actual guarded Identity/Definition rows.
                    validate(self._current_preparation_assignment(assignment), **read_options)
                    def current(current_assignment):
                        if (not canonical_reads() or identity is not None
                                and not _CANONICAL_INITIAL_IDENTITY_SEAL_CHECK(owner)
                                or projects is not None and not _CANONICAL_INITIAL_PROJECT_SEAL_CHECK(owner)
                                or capacity is not None and not _CANONICAL_INITIAL_CAPACITY_SEAL_CHECK(owner)):
                            raise ValueError('Native preparation metadata owner changed before guarded use')
                        return validate(current_assignment, **read_options)
                    fresh_assignment = self._current_preparation_assignment(assignment)
                    return owner.admit_initial_native_credentials(fresh_assignment, selected, actor, current,
                        **read_options, **({'_projects':projects} if projects is not None else {}),
                        **({'_capacity':capacity} if capacity is not None else {}), **options)
                if _CANONICAL_INITIAL_IDENTITY_SEAL_CHECK(owner):
                    return owner.identity.observe_reads(with_identity)
                return with_identity(None)
            def observe_context(reader, capacity=None):
                if _CANONICAL_INITIAL_PROJECT_SEAL_CHECK(owner):
                    return self.discovery.projects.repository.observe_reads(lambda projects:
                        observed(reader, projects, capacity))
                return observed(reader, capacity=capacity)
            def read_context(reader):
                if reader is None:
                    # Unsupported Definition ancestry keeps every context read
                    # current under the original guarded path; no outer view
                    # may silently remain active without its own raw guard.
                    return owner.admit_initial_native_credentials(assignment, selected, actor, validate, **options)
                if _CANONICAL_INITIAL_CAPACITY_SEAL_CHECK(owner):
                    return self.discovery.context_capacity_resolver.observe_reads(
                        lambda capacity: observe_context(reader, capacity))
                return observe_context(reader)
            return owner.skills.observe_definition_reads(metadata_references or selected, read_context)
        return owner.admit_initial_native_credentials(assignment, selected, actor, validate, **options)

    def _admit_initial_native_credentials(self, assignment, selected, actor, project, sandbox, request_checksum, *, _diagnostics=None):
        # An explicit writer on the initial path only. Shared preparation and
        # guarded input remain read-only, including on resumed native sessions.
        expected, _ = self._preparation_discovery(assignment, actor, project, sandbox,
            request_checksum, selected, validate_credentials=False, _diagnostics=_diagnostics)
        def validate(current, *, _definitions=None, _identity=None):
            # Supporting Memory retrieval appends a canonical audit. Recheck all
            # selection/authority/attestation guards here, never start a nested
            # writer from the credential transaction. Guidance is retrieved
            # through the ordinary path again after the credential CAS.
            fresh, _ = self._preparation_discovery(current, actor, project, sandbox,
                request_checksum, selected, validate_credentials=False, _include_supporting_context=False,
                _diagnostics=_diagnostics,
                **{key:value for key,value in (('_definitions',_definitions),('_identity',_identity)) if value is not None})
            if fresh.context_checksum != expected.context_checksum:
                raise ValueError("initial native credential preparation changed")
        return self._seal_initial_credentials(
            self._current_preparation_assignment(assignment), selected, actor, validate)

    async def prepare_automation(self, *, project, actor, thread_id, execution_id,
                                 assignment_id, expected_fence, sandbox, definition,
                                 input_snapshot_id, request_checksum, automation_run_id):
        """Seal an unused native bootstrap for its real event input (#1982).

        This is preparation only: no native input, START or procedure checkpoint.
        Full ordinary discovery selects the credential scope; the Automation
        target must belong to it. Existing admitted custody is never enlarged.
        """
        import asyncio
        import hashlib
        import json
        from codex_web.services.procedure_execution import ProcedureExecutionError, bounded_data

        def prepare():
            engine = self.procedure_execution
            if engine is None or self.native_session_attestor is None:
                raise ProcedureExecutionError("procedure_automation_preparation_owner_unavailable")
            current_actor = engine.identity.actor_for_identity(actor.identity_id, scope=actor.tenant)
            if not current_actor.has_same_authority(actor):
                raise ProcedureExecutionError("procedure_automation_preparation_actor_changed")
            assignment = engine.get(assignment_id, current_actor)
            engine._live(assignment)
            if ((assignment.organization_id, assignment.workspace_id, assignment.project_id)
                    != (project.organization_id, project.workspace_id, project.id)
                    or assignment.created_by != current_actor.identity_id
                    or assignment.execution_id != execution_id
                    or assignment.fence != expected_fence or assignment.sandbox != sandbox
                    or not thread_id):
                raise ProcedureExecutionError("procedure_automation_preparation_assignment_changed")
            if self.bootstrap_bindings is None:
                raise ProcedureExecutionError("procedure_automation_preparation_binding_unavailable")
            bootstrap = self.bootstrap_bindings.get_by_thread(thread_id, current_actor)
            if ((bootstrap.assignment_id, bootstrap.execution_id, bootstrap.execution_workspace_id)
                    != (assignment.id, assignment.execution_id, assignment.execution_workspace_id)):
                raise ProcedureExecutionError("procedure_automation_preparation_thread_changed")
            artifact = engine.evidence.get_artifact(input_snapshot_id, actor=current_actor)
            previous = assignment.procedure_checkpoint
            reused = (previous is not None and previous.definition == definition
                and previous.input_snapshot_id == input_snapshot_id)
            if ((artifact.source != "automation-run:" + automation_run_id and not reused)
                    or artifact.execution_id != execution_id
                    or artifact.execution_workspace_id != assignment.execution_workspace_id
                    or set(artifact.resource_ids) != set(assignment.resource_ids)):
                raise ProcedureExecutionError("procedure_automation_preparation_input_scope")
            raw, _ = engine._content(input_snapshot_id, current_actor, assignment)
            canonical = json.dumps(bounded_data(json.loads(raw)), sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()
            if hashlib.sha256(canonical).hexdigest() != request_checksum:
                raise ProcedureExecutionError("procedure_automation_preparation_input_changed")
            context = self._preparation_context(assignment, current_actor, project,
                sandbox, request_checksum)
            discovery, _ = self.discovery.discover(context)
            selected = tuple(discovery.selected)
            if definition not in selected:
                raise ProcedureExecutionError("procedure_automation_preparation_target_unselected")
            custody = assignment.native_credential_custody
            if custody is not None and custody.admitted_at is None:
                assignment = self._admit_initial_native_credentials(assignment, selected,
                    current_actor, project, sandbox, request_checksum)
            # Same physical attestation/current full selection for both initial
            # sealing and already-admitted use; never reopen an empty/old seal.
            self._preparation_discovery(assignment, current_actor, project, sandbox,
                request_checksum, selected)
            return assignment
        return await asyncio.to_thread(prepare)

    def _empty_preparation_discovery(self, assignment, actor, project, sandbox, request_checksum, references, *,
                                     _include_supporting_context=True, _diagnostics=None, _definitions=None, _identity=None):
        from dataclasses import replace
        from codex_web.services.native_procedure_preparation import NativeProviderPreparation, native_provider_preparation
        from codex_web.native_empty_preparation import closed_empty_preparation_diagnostics
        from codex_web.services.threads import bootstrap_exception_type
        diagnostics = _diagnostics if _diagnostics is not None else []
        read_options = {key:value for key,value in (('_definitions',_definitions),('_identity',_identity)) if value is not None}
        def checked(stage, operation):
            try:
                return operation()
            except Exception as exc:
                diagnostics[:] = closed_empty_preparation_diagnostics([
                    *diagnostics, {'stage': stage, 'exception_type': bootstrap_exception_type(exc)}])
                raise
        def physical_scope():
            profile, _ = self._preparation_profile(assignment, actor, project, _definitions)
            workspace = self.workers.workspaces.get(assignment.execution_workspace_id, self.control_actor)
            lease = self.workers.workspaces.store.lease(workspace.lease_id)
            if (profile.workspace_mode != "scratch" or profile.repository_access != "none"
                    or assignment.resource_ids != () or assignment.resource_context is not None
                    or workspace.kind != "scratch" or workspace.resource_ids != ()
                    or workspace.resource_context is not None or workspace.read_only_resource_ids != ()
                    or workspace.writable_repository_ids != () or lease is None
                    or lease.resource_ids != () or lease.resource_modes != {}):
                raise ValueError("physical scope is not exactly empty")
        def preparation_context():
            return checked('preparation_context', lambda: self._preparation_context(
                assignment, actor, project, sandbox, request_checksum, require_operations=False,
                **({'_definitions': _definitions} if _definitions is not None else {})))
        def validate():
            def attest():
                if self.native_session_attestor is None or self.procedure_execution is None:
                    raise ValueError("physical scope is not attested")
                current = self._current_preparation_assignment(assignment)
                self._attest_native_session(current)
            checked('session_attestation', attest)
            checked('native_owner', lambda: self.procedure_execution.validate_native_preparation_owner(
                self._current_preparation_assignment(assignment), **({'_identity':_identity} if _identity is not None else {})))
            checked('physical_scope', physical_scope)
            preparation_context()
        validate()
        context = preparation_context()
        source = "canonical:native-empty-physical-scope:" + checksum({
            "assignment":assignment.id,"fence":assignment.fence,"workspace":assignment.execution_workspace_id,
            "definitions":[ref.model_dump(mode="json") for ref in references],"policy":context.policy_source})
        context = replace(context, policy_source=context.policy_source + ";" + source,
            capability_sources=(*context.capability_sources,source))
        excluded = []
        validation_failed = []
        def excludes(reference):
            if reference not in references:
                return False
            try:
                checked('candidate_validation', lambda: self.procedure_execution.validate_native_preparation(
                    self._current_preparation_assignment(assignment), (reference,), actor, require_targets=False,
                    **read_options))
            except Exception:
                validation_failed.append(True)
                return False
            excluded.append(reference)
            return True
        proof = NativeProviderPreparation((),source,validate,context=context,exclude_empty=excludes)
        with native_provider_preparation(proof):
            discovery, text = checked('discovery', lambda: self.discovery.discover(context,
                _include_supporting_context=_include_supporting_context,
                **read_options))
        def consistent():
            # Check unavailable observations even when another candidate was
            # excluded successfully and the full ordered inventory is unchanged.
            if validation_failed or discovery.outcome == 'failed':
                raise _empty_preparation_failure(context, discovery,
                    validation_failed=bool(validation_failed))
            references_changed = tuple(candidate.reference for candidate in discovery.candidates) != tuple(references)
            if not excluded or references_changed:
                raise _EmptyPreparationCandidatesChanged(no_exclusions=not excluded,
                    references_changed=references_changed)
        checked('candidate_consistency', consistent)
        return discovery, text

    def _reasoning_context(self, assignment, actor, project, sandbox, request_checksum):
        if (self.reasoning_preparation is None or assignment is None
                or assignment.runtime_binding is None
                or assignment.runtime_binding.runtime_id != 'codex-reasoning'):
            raise ValueError('Actual native reasoning preparation unavailable')
        context = self.reasoning_preparation(assignment=assignment, actor=actor,
            project=project, sandbox=sandbox, request_checksum=request_checksum)
        if (not isinstance(context, OperationContext) or context.actor != actor
                or context.project_id != project.id or context.resource_ids != ()
                or context.operation != 'model.reasoning'
                or context.provider_capabilities != () or context.worker_capabilities != ()
                or context.operation_capabilities != ()
                or context.request_checksum != request_checksum
                or context.enforcement != 'bounded_read_only' or not context.policy_allows):
            raise ValueError('Native reasoning operation scope unavailable')
        return context

    def preparation_input_guard(self, discovery, *, project, actor, execution_id, assignment_id, sandbox, request_checksum):
        """A fresh read-only check at the existing guarded native send boundary."""
        def validate():
            from codex_web.runtime.execution import ExecutionPrestartRefusal
            try:
                current = self.workers.assignment_for_execution(execution_id, actor=self.control_actor)
                if current.id != assignment_id:
                    raise ValueError("native assignment changed")
                if (discovery.operation == 'model.reasoning'
                        and current.runtime_binding is not None
                        and current.runtime_binding.runtime_id == 'codex-reasoning'):
                    context = self._reasoning_context(current, actor, project, sandbox, request_checksum)
                    fresh, _ = self.discovery.discover(context)
                elif discovery.selected:
                    fresh, _ = self._preparation_discovery(current, actor, project, sandbox, request_checksum, discovery.selected,
                        _include_supporting_context=False)
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
        is_reasoning = (assignment is not None and assignment.runtime_binding is not None
                        and assignment.runtime_binding.runtime_id == 'codex-reasoning')
        if is_reasoning:
            from codex_web.services.post_turn_readiness import PostTurnReadinessService
            try:
                context = await PostTurnReadinessService._run_owned(
                    lambda: asyncio.to_thread(self._reasoning_context, assignment, actor, project,
                                              sandbox, request_checksum),
                    seconds=RuntimePolicy.local_preparation_timeout(5.), name='native-reasoning-procedure-context')
            except Exception as exc:
                # The original local preparation owner alone supplies these
                # closed diagnostics. Exception prose and provider payloads do
                # not enter the retained blocker or confer retry authority.
                from codex_web.services.threads import bootstrap_exception_type
                from codex_web.services.native_model_provider import NativeReasoningPreparationError
                stage = 'procedure_context'
                diagnostic_exception = exc
                if isinstance(exc, NativeReasoningPreparationError):
                    stage = exc.stage
                    diagnostic_exception = exc.__cause__ or exc
                raise ExecutionPrestartRefusal(status_code=409, detail={
                    'code':'native_reasoning_preparation_unavailable',
                    'message':'Current canonical tool-free reasoning preparation is unavailable',
                    'native_preparation_stage':stage,
                    'native_exception_type':bootstrap_exception_type(diagnostic_exception)}) from exc
        else:
            targets = assignment.resource_ids if assignment is not None else ()
            enforcement, policy_source = await asyncio.to_thread(self._native_policy, assignment, actor, project, sandbox)
            context = OperationContext(actor=actor, project_id=project.id, resource_ids=targets, operation=None,
                request_checksum=request_checksum, policy_source=policy_source, policy_allows=True, enforcement=enforcement,
                **await asyncio.to_thread(self.discovery.resource_facts, targets, actor))
            if assignment is not None:
                assignment = await asyncio.to_thread(self._current_preparation_assignment, assignment)
                context = await asyncio.to_thread(self.discovery.assignment_context, context, assignment, actor=actor)
                if context.worker_capabilities is None or context.provider_capabilities is None:
                    from dataclasses import replace
                    context = replace(context, enforcement="native_tools")
        discovery, text = await asyncio.to_thread(self.discovery.discover, context)
        empty_preparation_references = None
        empty_preparation_diagnostics = []
        selected_preparation_diagnostics = []
        if (not is_reasoning and assignment is not None and assignment.resource_ids == () and discovery.candidates
                and context.enforcement in {"bounded_read_only", "isolated_preparation"}
                and self.native_session_attestor is not None and self.procedure_execution is not None):
            try:
                references = tuple(candidate.reference for candidate in discovery.candidates)
                discovery, text = await asyncio.to_thread(self._empty_preparation_discovery,
                    assignment, actor, project, sandbox, request_checksum, references,
                    _diagnostics=empty_preparation_diagnostics)
                empty_preparation_references = references
            except Exception as exc:
                if not empty_preparation_diagnostics:
                    from codex_web.services.threads import bootstrap_exception_type
                    empty_preparation_diagnostics.append({
                        'stage': 'fallback', 'exception_type': bootstrap_exception_type(exc)})
                # Unknown/unsupported scope retains the original refusal.
        if (not is_reasoning and assignment is not None and discovery.outcome == "unsupported_enforcement"
                and discovery.selected and len(discovery.blockers) == 1
                and discovery.blockers[0].startswith("procedure_tool_enforcement_unsupported:")
                and context.enforcement in {"bounded_read_only", "isolated_preparation"}
                and self.native_session_attestor is not None and self.procedure_execution is not None):
            try:
                custody = assignment.native_credential_custody
                if custody is not None and custody.admitted_at is None:
                    assignment = await asyncio.to_thread(self._admit_initial_native_credentials,
                        assignment, discovery.selected, actor, project, sandbox, request_checksum,
                        _diagnostics=selected_preparation_diagnostics)
                discovery, text = await asyncio.to_thread(self._preparation_discovery,
                    assignment, actor, project, sandbox, request_checksum, discovery.selected,
                    _diagnostics=selected_preparation_diagnostics)
            except Exception as exc:
                from codex_web.services.procedure_execution import ProcedureExecutionError
                if isinstance(exc, ProcedureExecutionError) and exc.code.startswith("procedure_credential_"):
                    raise ExecutionPrestartRefusal(status_code=409, detail={
                        "code": exc.code, "message": "Native procedure credential custody requires a fresh authorized bootstrap",
                        "retryable": False, "remediation_route": "/api/execution-workers/assignments"}) from exc
                if not selected_preparation_diagnostics:
                    from codex_web.services.threads import bootstrap_exception_type
                    selected_preparation_diagnostics.append({
                        'stage':'fallback','exception_type':bootstrap_exception_type(exc)})
                # The original unsupported outcome/first blocker remain. Closed
                # diagnostic categories cannot install preparation or retry proof.
        if (not is_reasoning and assignment is not None and discovery.admitted
                and self.procedure_execution is not None and not discovery.selected and assignment.native_credential_custody is not None
                and assignment.native_credential_custody.admitted_at is None):
            # A first no-match still seals an empty procedure scope. A later
            # publication cannot turn an already used bootstrap into a grant.
            def seal_empty():
                def validate(current, *, _definitions=None, _identity=None):
                    read_options = {key:value for key,value in (('_definitions',_definitions),('_identity',_identity)) if value is not None}
                    if empty_preparation_references is not None:
                        # Rebuild the same attested empty physical scope under
                        # the credential CAS, including candidate exclusions and
                        # policy/capability provenance. Supporting retrieval can
                        # audit, so it must remain outside this writer lock.
                        try:
                            fresh, _ = self._empty_preparation_discovery(current, actor, project,
                                sandbox, request_checksum, empty_preparation_references,
                                _include_supporting_context=False,
                                **read_options)
                        except _EmptyPreparationCandidatesChanged as exc:
                            raise ExecutionPrestartRefusal(status_code=409, detail={
                                "code": "procedure_native_preparation_changed",
                                "message": ("Current empty native preparation has no supported candidate exclusions"
                                    if exc.no_exclusions else
                                    "Current empty native preparation candidate references changed"),
                                "native_empty_preparation_diagnostics": [
                                    {"stage": "candidate_consistency", "exception_type": "ValueError"}],
                            }) from exc
                    else:
                        # A genuine zero-candidate discovery has no attested
                        # exclusions; retain its original plain-context check.
                        fresh_context = self.discovery.assignment_context(context, current, actor=actor)
                        fresh, _ = self.discovery.discover(fresh_context, _include_supporting_context=False,
                            **read_options)
                    # The plain zero-candidate branch has no empty-proof callback;
                    # it must also preserve errors caught by discovery as unknown.
                    if fresh.outcome == 'failed':
                        if empty_preparation_references is None:
                            raise _empty_preparation_failure(fresh_context, fresh)
                        raise _EmptyPreparationUnavailable('Current empty native preparation observation is unavailable')
                    if not fresh.admitted or fresh.selected or fresh.context_checksum != discovery.context_checksum:
                        # This comparison is an authoritative local pre-input
                        # refusal. Unexpected storage/attestor failures still
                        # propagate unchanged; old uncertain attempts stay so.
                        raise ExecutionPrestartRefusal(status_code=409, detail={
                            "code": "procedure_native_preparation_changed",
                            "message": "Current empty native preparation differs from its initial admission",
                        })
                return self._seal_initial_credentials(
                    self._current_preparation_assignment(assignment), (), actor, validate,
                    metadata_references=empty_preparation_references or ())
            try:
                assignment = await asyncio.to_thread(seal_empty)
            except Exception as exc:
                if type(exc) is _EmptyPreparationContextUnavailable:
                    # Only the explicit context-resolution decision is closed;
                    # opaque validator/storage/final-proof failures stay unknown.
                    # This boundary supplies the local refusal; failed facts,
                    # matching prose and transported codes supply no authority.
                    # The queue's atomic claim/capture guards still decide
                    # whether this same original may be recorded as blocked.
                    raise ExecutionPrestartRefusal(status_code=409, detail={
                        "code": "procedure_empty_preparation_unavailable",
                        "message": "Current empty native preparation could not be validated before input.",
                        "retryable": False,
                        "native_empty_preparation_diagnostics": [
                            {"stage": "discovery", "exception_type": "ValueError"}],
                    }) from exc
                from codex_web.services.procedure_execution import ProcedureExecutionError
                if isinstance(exc, ProcedureExecutionError) and exc.code.startswith("procedure_credential_"):
                    # This owner is still before native input. Retain the same
                    # known refusal as selected preparation without opening a
                    # used seal or treating arbitrary/RPC errors as noninput.
                    raise ExecutionPrestartRefusal(status_code=409, detail={
                        "code": exc.code, "message": "Native procedure credential custody requires a fresh authorized bootstrap",
                        "retryable": False, "remediation_route": "/api/execution-workers/assignments"}) from exc
                raise
        from codex_web.native_empty_preparation import closed_native_preparation_diagnostics
        selected_diagnostics = closed_native_preparation_diagnostics(selected_preparation_diagnostics)
        if not discovery.admitted and selected_diagnostics:
            markers = tuple('native_preparation_diagnostic:' + row['stage'] + ':' + row['exception_type']
                + (':' + row['code'] if 'code' in row else '') for row in selected_diagnostics)
            discovery = discovery.model_copy(update={'blockers':(*discovery.blockers,*markers)})
        await asyncio.to_thread(self.history.record_procedure_discovery, thread_id, execution_id, discovery)
        if assignment is not None:
            from codex_web.services.execution_workers import (
                ProcedureDiscoveryLeaseRefusal, ProcedureHistoryExhaustedError,
            )
            try:
                await asyncio.to_thread(self.workers.record_procedure_discovery, assignment.id, discovery,
                    expected_fence=assignment.fence, actor=self.control_actor)
            except ProcedureDiscoveryLeaseRefusal as exc:
                # Only this canonical writer guard is a local preinput witness.
                # Native resume/start has not been entered by this admission;
                # generic worker, storage and transport errors remain unknown.
                message = ('The assignment lease is no longer live. Repair its canonical custody '
                    'before explicitly retrying this retained request.')
                raise ExecutionPrestartRefusal(status_code=409, detail={
                    'code': 'execution_preflight_blocked', 'message': message,
                    'retryable': True, 'blockers': [{
                        'code': 'procedure_assignment_lease_unavailable', 'message': message,
                        'retryable': True, 'target_type': 'execution_assignment',
                        'target_id': assignment.id, 'remediation_route': '/api/execution-workers',
                    }],
                }) from exc
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
            from codex_web.native_empty_preparation import closed_empty_preparation_diagnostics
            diagnostics = closed_empty_preparation_diagnostics(empty_preparation_diagnostics)
            raise ExecutionPrestartRefusal(status_code=409, detail={
                "code": "procedure_" + discovery.outcome, "message": "; ".join(discovery.blockers),
                "procedure_discovery": discovery.model_dump(mode="json"), "remediation_route": "/api/skills",
                **({'native_preparation_diagnostics': selected_diagnostics} if selected_diagnostics else
                   {'native_empty_preparation_diagnostics': diagnostics} if diagnostics else {}),
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
    admission.native_session_preparer = getattr(turns, 'prepare_native_provider_preparation', None)
    admission.procedure_execution = procedure_execution
    admission.bootstrap_bindings = getattr(turns, "bootstrap_bindings", None)
    turns.procedure_admission = admission
    sessions.procedure_admission = admission.agent_session_admission
    return admission


_CANONICAL_DISCOVERY_CONTEXT_READS = {
    name: getattr(ProcedureDiscoveryService, name)
    for name in ('action_context', 'assignment_context', 'resource_facts')
}
_CANONICAL_CAPABILITY_RESOLVE = ProcedureExecutionCapabilityService.resolve

from codex_web.services.execution_profile_definitions import ExecutionProfileDefinitionService as _PreparationProfiles
_CANONICAL_PROFILE_RESOLVE = _PreparationProfiles.resolve
_CANONICAL_PROFILE_RECORD = _PreparationProfiles.record
_CANONICAL_DISCOVER = ProcedureDiscoveryService.discover
_CANONICAL_SEAL_READS = {name: getattr(NativeProcedureAdmissionService, name)
    for name in ('_empty_preparation_discovery', '_preparation_discovery', '_preparation_context',
                 '_native_policy', '_preparation_profile', '_current_preparation_assignment', '_attest_native_session')}
