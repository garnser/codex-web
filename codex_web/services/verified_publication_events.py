"""Resolve provider notifications through existing verified publication lineage.

Indexes locate candidates only. Current typed intent/receipt/verification and
original Work Item scope are authoritative; branch names and MR prose are not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from codex_web.identity import TenantScope
from codex_web.services.action_providers import ActionProviderNotFoundError
from codex_web.services.projects import ProjectNotFoundError
from fastapi import HTTPException


@dataclass(frozen=True)
class VerifiedPublicationRoute:
    state: object
    branch_intent_id: str
    request_intent_id: str
    head_revision: str
    mr_ref: str

    def provenance(self):
        return {'work_item_ref': self.state.ref, 'branch_intent_id': self.branch_intent_id,
                'request_intent_id': self.request_intent_id, 'head_revision': self.head_revision,
                'mr_ref': self.mr_ref}


class PublicationEventRoutingRefused(ValueError):
    pass


class VerifiedPublicationEventRouting:
    def __init__(self, intents, *, get_state, project_resources, project_lookup, action_registry, resources):
        self.intents = intents
        self.get_state = get_state
        self.project_resources = project_resources
        self.project_lookup = project_lookup
        self.action_registry = action_registry
        self.resources = resources

    def _verified(self, intent):
        if intent.status != 'succeeded' or not intent.last_receipt_id or not intent.last_verification_id:
            return None
        state = self.intents.load(intent_id=intent.id)
        receipt = next((r for r in state.receipts if r.id == intent.last_receipt_id), None)
        verification = next((v for v in state.verifications if v.id == intent.last_verification_id), None)
        if (receipt is None or receipt.intent_id != intent.id or receipt.outcome != 'completed'
                or receipt.attempt != intent.attempt
                or receipt.action_id != intent.action_id or receipt.provider_type != intent.provider_type
                or receipt.provider_instance != intent.provider_instance
                or receipt.idempotency_key != intent.idempotency_key
                or receipt.result is None or receipt.result.status != 'succeeded'
                or receipt.result.action_id != intent.action_id
                or receipt.result.provider_binding_id != intent.binding_id
                or (receipt.result.idempotency_key is not None and receipt.result.idempotency_key != intent.idempotency_key)
                or verification is None or verification.intent_id != intent.id or not verification.verified
                or verification.provider_verification is None or not verification.provider_verification.verified
                or verification.verified_at < receipt.received_at
                or (intent.expected_evidence and verification.evidence_satisfied is not True)):
            return None
        return {**receipt.result.output, '_publication_verified_at': verification.verified_at}

    def _context(self, payload, project_id):
        project_path = str((payload.get('project') or {}).get('path_with_namespace') or '')
        try:
            project = self.project_lookup(project_id)
        except ProjectNotFoundError as error:
            raise PublicationEventRoutingRefused('publication_event_project_unavailable') from error
        tenant = TenantScope(organization_id=project.organization_id, workspace_id=project.workspace_id)
        attrs = payload.get('object_attributes') or {}
        kind = str(payload.get('object_kind') or payload.get('event_name') or '').casefold()
        branch = attrs.get('ref') if kind == 'pipeline' else attrs.get('source_branch')
        if not isinstance(branch, str) or not branch:
            return None
        candidates = self.intents.publication_candidates('project-branch',
            organization_id=tenant.organization_id,workspace_id=tenant.workspace_id,
            project_id=project_id,resource_id='',provider_instance='',value=branch)
        if not candidates:
            return None  # Legacy/read-only routing needs no mutation binding.
        # Distinguish the same branch name in another repository from a broken
        # current mapping for a genuinely published original. The receipt entity
        # is authoritative; current alias/resource lookup alone cannot prove it.
        published_here = any((output := self._verified(i)) is not None
            and output.get('repository') == project_path for i in candidates)
        catalog = self.resources.store.load()
        candidate_resource_ids = {rid for i in candidates for rid in i.resource_ids}
        declared_here = any(r.id in candidate_resource_ids and (
            (r.provenance and r.provenance.provider == 'gitlab' and r.provenance.external_id == project_path)
            or any(a.provider == 'gitlab' and a.value.strip('/') == project_path for a in r.aliases))
            for r in catalog.resources)
        if not published_here and not declared_here:
            return None
        resources = tuple(self.project_resources(project_id, alias_value=project_path, provider='gitlab'))
        if len(resources) != 1:
            raise PublicationEventRoutingRefused('publication_event_repository_unresolved')
        if not any(i.resource_ids == resources for i in candidates):
            raise PublicationEventRoutingRefused('publication_event_repository_unresolved')
        resource = next((r for r in catalog.resources if r.id == resources[0]
            and r.organization_id == tenant.organization_id and r.workspace_id == tenant.workspace_id), None)
        if resource is None or resource.lifecycle != 'active':
            raise PublicationEventRoutingRefused('publication_event_repository_unresolved')
        provenance = resource.provenance
        if provenance is None or provenance.provider != 'gitlab' or not provenance.provider_instance:
            raise PublicationEventRoutingRefused('publication_event_provider_binding_unresolved')
        bindings = []
        for binding in self.action_registry.store.load().bindings:
            if (binding.organization_id, binding.workspace_id) != (tenant.organization_id, tenant.workspace_id):
                continue
            if (not binding.enabled or binding.provider_type != 'gitlab'
                    or binding.project_id not in {None, project_id} or resources[0] not in binding.resource_ids):
                continue
            if binding.provider_instance != provenance.provider_instance:
                continue
            try:
                provider = self.action_registry.provider_configuration('gitlab', binding.provider_instance, scope=tenant)
            except ActionProviderNotFoundError as error:
                raise PublicationEventRoutingRefused('publication_event_provider_binding_unresolved') from error
            if not str(getattr(provider, 'api_base', '')).strip():
                continue
            bindings.append(binding)
        if len(bindings) != 1:
            raise PublicationEventRoutingRefused('publication_event_provider_binding_unresolved')
        binding = bindings[0]
        return project, resources, binding, project_path

    def ingress_provenance(self, payload, *, project_id):
        context = self._context(payload, project_id)
        if context is None:
            return {}
        _, resources, binding, _ = context
        return {'publication_binding_id': binding.id, 'publication_resource_id': resources[0],
                'publication_provider_instance': binding.provider_instance}

    def __call__(self, payload, *, project_id, provider_instance=None, canonical_event=None):
        # provider_instance is the authenticated transport endpoint, which may
        # be a proxy. Public provider identity comes ONLY from configured Resource
        # provenance and its unique matching canonical ActionProviderBinding.
        kind = str(payload.get('object_kind') or payload.get('event_name') or '').casefold()
        if kind not in {'pipeline', 'merge_request'}:
            return None
        attrs = payload.get('object_attributes') or {}
        context = self._context(payload, project_id)
        if context is None:
            return None
        project, resources, binding, project_path = context
        if canonical_event is not None:
            observed = canonical_event.payload.get('provider_payload') or {}
            if any(observed.get(k) != v for k,v in self.ingress_provenance(payload, project_id=project_id).items()):
                raise PublicationEventRoutingRefused('publication_event_ingress_binding_changed')
        scope = dict(organization_id=project.organization_id, workspace_id=project.workspace_id,
            project_id=project_id, resource_id=resources[0], provider_instance=binding.provider_instance)
        branch = attrs.get('ref') if kind == 'pipeline' else attrs.get('source_branch')
        if not isinstance(branch, str) or not branch:
            return None
        candidates = self.intents.publication_candidates('branch', value=branch, **scope)
        if not candidates:
            return None  # Unrelated legacy artifact events retain their routing.
        commit = attrs.get('last_commit')
        revision = attrs.get('sha') or (commit.get('id') if isinstance(commit, dict) else None)
        if (attrs.get('sha') and isinstance(commit, dict) and commit.get('id')
                and str(attrs['sha']).lower() != str(commit['id']).lower()):
            raise PublicationEventRoutingRefused('publication_event_conflicting_head')
        if not isinstance(revision, str) or re.fullmatch('[0-9a-fA-F]{40}', revision) is None:
            raise PublicationEventRoutingRefused('publication_event_exact_head_required')
        revision = revision.lower()
        claims = {}
        for ref in sorted({i.work_item_ref for i in candidates if i.work_item_ref}):
            original_scope = self.intents.publication_candidates('work', value=ref, **scope)
            verified = [(i, self._verified(i)) for i in original_scope if i.binding_id == binding.id]
            verified = [(i, output) for i, output in verified if output is not None]
            branches = [(i, o) for i, o in verified if i.action_id == 'code-host.branch.publish']
            requests = [(i, o) for i, o in verified if i.action_id == 'code-host.pull-request.upsert']
            if not branches or not requests:
                continue
            key = lambda pair: (pair[0].completed_at or pair[0].updated_at, pair[0].created_at, pair[0].id)
            branch_intent, branch_output = max(branches, key=key)
            request_intent, request_output = max(requests, key=key)
            number = request_output.get('change_request_number')
            original_issue = ref.removeprefix(project_path + '#')
            typed_repair = (
                ref.startswith(project_path + '#') and original_issue.isdigit()
                and type(branch_intent.request.parameters.get('existing_change_request_number')) is int
                and branch_intent.request.parameters['existing_change_request_number'] == number
                and type(branch_output.get('existing_change_request_number')) is int
                and branch_output['existing_change_request_number'] == number
                and type(branch_output.get('work_item_issue_number')) is int
                and branch_output['work_item_issue_number'] == int(original_issue)
                and (branch_intent.completed_at or branch_intent.updated_at)
                    >= (request_intent.completed_at or request_intent.updated_at))
            same_execution = (branch_intent.execution_id == request_intent.execution_id
                and branch_intent.requested_by == request_intent.requested_by)
            if (branch_intent.request.parameters.get('branch') != branch
                    or branch_intent.request.parameters.get('head_revision') != revision
                    or branch_output.get('branch') != branch or branch_output.get('head_revision') != revision
                    or request_intent.request.parameters.get('head') != branch
                    or request_output.get('head') != branch or not str(number).isdigit() or int(number) < 1
                    or not branch_intent.execution_id or not request_intent.execution_id
                    or not (same_execution or typed_repair)
                    or branch_intent.binding_id != request_intent.binding_id):
                continue
            if any(o.get('resource_id') != resources[0] or o.get('repository') != project_path
                   for o in (branch_output, request_output)):
                continue
            if kind == 'merge_request' and str(attrs.get('iid')) != str(number):
                continue
            try:
                state = self.get_state(ref)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
                state = None
            if (state is None or (state.organization_id, state.workspace_id, state.project_id)
                    != (project.organization_id, project.workspace_id, project_id)
                    or tuple(state.resource_ids) != resources
                    or state.current_stage == 'closed' or state.closed_at):
                continue
            mr_ref = project_path + '!' + str(number)
            relation = next((r for r in state.verified_artifact_relations or [] if r.ref == mr_ref), None)
            # A provider-attested repair publication to this SAME existing MR
            # can advance its head before the TaskSource snapshot refreshes.
            # Preserve that historical relation; do not rewrite/invent refs.
            if relation is not None and relation.head_revision != revision:
                if not typed_repair:
                    continue
                try:
                    observed = datetime.fromisoformat(relation.source_revision.replace('Z', '+00:00'))
                    if observed.tzinfo is None or observed.timestamp() > branch_output['_publication_verified_at']:
                        continue
                except (ValueError, OverflowError):
                    continue
            claims[state.ref] = VerifiedPublicationRoute(state, branch_intent.id,
                request_intent.id, revision, mr_ref)
        if len(claims) != 1:
            raise PublicationEventRoutingRefused('publication_event_lineage_ambiguous_or_stale')
        return next(iter(claims.values()))
