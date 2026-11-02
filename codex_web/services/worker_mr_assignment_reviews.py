"""Closed native MR review method, authenticated assignment/Role attribution.

No provider mutation, principal creation, generic verification method or caller
independence flag. Application composition injects the existing Role and positive
START owners explicitly; an absent owner is a visible refusal.
"""
from __future__ import annotations

from urllib.parse import quote

from codex_web.artifact_evidence import EvidenceCreate, VerificationCreate
from codex_web.code_host_merge_requirements import CodeHostMergeRequirementsFact
from codex_web.mr_closure_acceptance import MAX_REVIEW_BYTES, MRClosureScope
from codex_web.mr_assignment_review import (
    METHOD, RECEIPT_CONTRACT, AssignmentReviewRequest, AssignmentReviewError,
    AssignmentMRReviewManifest, AssignmentMRReviewReceipt, canonical, digest,
    start_provenance, publication_proof, qualification, review_policy, check_assignment_acceptance,
)
from codex_web.services.async_operations import run_settled


class WorkerMRAssignmentReviewAdapter:
    def __init__(self, adapter, *, execution_roles, preflights):
        self.adapter = adapter
        self.broker = adapter.broker
        self.roles = execution_roles
        self.preflights = preflights

    def source(self, assignment, request):
        return 'sha256:' + digest({'assignment_id': assignment.id, 'fence': assignment.fence,
            'execution_id': assignment.execution_id, 'request_id': request.request_id, 'method': METHOD})

    def existing(self, assignment, worker, source, request_digest=None):
        service = self.adapter.service()
        matches = [v for v in service.list_verifications(worker, work_item_ref=assignment.work_item_ref)
            if v.execution_id == assignment.execution_id and v.method == METHOD and v.source == source]
        if len(matches) > 1:
            self.adapter.denied('Ambiguous original MR assignment review; reconcile without replay')
        if not matches:
            return None
        v = matches[0]
        if len(v.evidence_ids) != 1 or v.verifier_identity_id != worker.identity_id or v.result == 'invalidated':
            self.adapter.denied('Original MR assignment review changed')
        e = service.get_evidence(v.evidence_ids[0], actor=worker)
        if (e.lifecycle != 'valid' or e.execution_id != assignment.execution_id
                or e.producer_identity_id != worker.identity_id
                or e.metadata.get('assignment_id') != assignment.id
                or e.metadata.get('assignment_fence') != assignment.fence
                or e.metadata.get('receipt_contract') != RECEIPT_CONTRACT
                or request_digest is not None and e.metadata.get('request_sha256') != request_digest):
            self.adapter.denied('Original MR assignment review content changed; no replacement')
        self.adapter.visible(e, assignment, worker)
        receipt = AssignmentMRReviewReceipt.model_validate_json(e.metadata.get('receipt_json', ''))
        if (digest(receipt.model_dump(mode='json')) != e.metadata.get('receipt_sha256')
                or v.findings != ('mr_assignment_review_receipt_sha256:' + e.metadata['receipt_sha256'],)):
            self.adapter.denied('Original MR assignment review receipt changed')
        return {'item': v.model_dump(mode='json'), 'evidence': e.model_dump(mode='json'),
            'receipt': receipt.model_dump(mode='json'), 'deduplicated': True,
            'principal_independence_claimed': False, 'merge_authorization': False}

    def validate_canonical_receipt(self, scope, artifact_id, verification_id, *, actor,
            require_principal_independence=False):
        """Closure-owner composition: reload actual records, never trust inputs.

        Historical leases may be retired. Positive original START and publication
        references are revalidated; current Role policy must still explicitly opt
        in. The method neither approves nor merges and adds no native operation.
        """
        service = self.adapter.service()
        v = service.get_verification(verification_id, actor=actor)
        if v.method != METHOD or len(v.evidence_ids) != 1:
            raise AssignmentReviewError('mr_assignment_review_fixed_method_required')
        e = service.get_evidence(v.evidence_ids[0], actor=actor)
        receipt = AssignmentMRReviewReceipt.model_validate_json(e.metadata.get('receipt_json', ''))
        artifact = service.get_artifact(artifact_id, actor=actor)
        if artifact.content is None or artifact.content.size_bytes > MAX_REVIEW_BYTES:
            raise AssignmentReviewError('mr_assignment_review_content_bounds')
        _, stream = service.open_artifact_content(artifact.id, actor=actor)
        content = bytearray()
        try:
            for chunk in stream:
                content.extend(chunk)
                if len(content) > MAX_REVIEW_BYTES:
                    raise AssignmentReviewError('mr_assignment_review_content_bounds')
        finally:
            close = getattr(stream, 'close', None)
            if close:
                close()
        # Managed content I/O is not an atomic metadata snapshot. Reload all
        # records before interpreting it so revocation/lifecycle changes cannot
        # be hidden by the pre-I/O header or caller-provided object.
        if (service.get_artifact(artifact.id, actor=actor) != artifact
                or service.get_verification(v.id, actor=actor) != v
                or service.get_evidence(e.id, actor=actor) != e):
            raise AssignmentReviewError('mr_assignment_review_records_changed_during_content')
        catalog, reference = self.roles.resolve_catalog(organization_id=scope.organization_id,
            workspace_id=scope.workspace_id, project_id=scope.project_id)
        workers = self.broker.execution_workers.store
        for observed in (receipt.implementation, receipt.validation):
            assignment = workers.assignment(observed.assignment_id)
            if assignment is None:
                raise AssignmentReviewError('mr_assignment_review_historical_assignment_missing')
            current = start_provenance(self.preflights, self.preflights.store.get(observed.preflight_id),
                assignment, workers.worker(assignment.assigned_worker_id))
            if current != observed:
                raise AssignmentReviewError('mr_assignment_review_historical_start_changed')
        original = self.broker.action_intents.get(receipt.implementation_intent_id, actor)
        history = self.broker.action_intents.store.load(intent_id=original.id)
        publication, verification = publication_proof(scope, original, history,
            receipt.implementation, now=self.broker._clock())
        if (publication.id, verification.id) != (receipt.publication_receipt_id, receipt.publication_verification_id):
            raise AssignmentReviewError('mr_assignment_review_historical_publication_changed')
        return check_assignment_acceptance(scope, artifact, v, e, bytes(content), receipt=receipt,
            current_catalog=catalog, current_catalog_ref=reference, now=self.broker._clock(),
            require_principal_independence=require_principal_independence)

    def lookup(self, assignment, worker_actor, resolved, validator):
        if (set(resolved.query) != {'method', 'source'} or resolved.query['method'] != [METHOD]
                or len(resolved.query['source']) != 1):
            self.adapter.denied('Exact MR assignment review method/source required')
        import re
        source = resolved.query['source'][0]
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', source):
            self.adapter.denied('Exact original request digest required')
        worker, _, _ = self.adapter.principals(assignment, worker_actor, resolved, validator, {})
        self.adapter.scope(assignment, assignment.work_item_ref)
        result = self.existing(assignment, worker, source)
        self.adapter.principals(assignment, worker_actor, resolved, validator, {})
        return {'outcome': 'unique' if result else 'unknown', 'original': result,
            'absence_is_not_permission_to_replay': True}

    def _origins(self, request, assignment):
        store = self.broker.execution_workers.store
        implementation = store.assignment(request.implementation_assignment_id)
        if implementation is None:
            raise AssignmentReviewError('mr_assignment_review_implementation_assignment_missing')
        starts = []
        for ident, owner in ((request.implementation_preflight_id, implementation),
                             (request.validation_preflight_id, assignment)):
            attempt = self.preflights.store.get(ident)
            starts.append(start_provenance(self.preflights, attempt, owner,
                store.worker(owner.assigned_worker_id)))
        if (implementation.organization_id, implementation.workspace_id, implementation.project_id) != (
                assignment.organization_id, assignment.workspace_id, assignment.project_id):
            raise AssignmentReviewError('mr_assignment_review_foreign_implementation')
        return implementation, starts[0], starts[1]

    async def create(self, assignment, worker_actor, resolved, payload, validator):
        try:
            request = AssignmentReviewRequest.model_validate(payload)
        except ValueError:
            self.adapter.denied('Invalid closed MR assignment review request; identity overrides forbidden')
        if self.roles is None or self.preflights is None:
            self.adapter.denied('Canonical MR review Role/START owners unavailable')
        worker, _, _ = self.adapter.principals(assignment, worker_actor, resolved, validator, payload)
        work = self.adapter.scope(assignment, assignment.work_item_ref)
        source = self.source(assignment, request)
        prior = self.existing(assignment, worker, source, digest(payload))
        if prior is not None:
            return prior
        service = self.adapter.service()
        artifact = service.get_artifact(request.manifest_artifact_id, actor=worker)
        self.adapter.visible(artifact, assignment, worker)
        if artifact.content is None or artifact.content.size_bytes > MAX_REVIEW_BYTES:
            self.adapter.denied('MR review requires bounded managed manifest content')
        pointer, stream = service.open_artifact_content(artifact.id, actor=worker)
        content = bytearray()
        try:
            for chunk in stream:
                content.extend(chunk)
                if len(content) > MAX_REVIEW_BYTES:
                    self.adapter.denied('MR review content exceeded bounds')
        finally:
            close = getattr(stream, 'close', None)
            if close:
                close()
        content = bytes(content)
        if len(content) != pointer.size_bytes:
            self.adapter.denied('MR review content size changed')
        manifest = AssignmentMRReviewManifest.model_validate_json(content)
        scope = MRClosureScope.model_validate(manifest.model_dump(include=set(MRClosureScope.model_fields)))
        if (scope.work_item_ref != assignment.work_item_ref or scope.project_id != assignment.project_id
                or (scope.organization_id, scope.workspace_id) != (assignment.organization_id, assignment.workspace_id)
                or scope.repository_resource_id != self.broker._writable_repository_id(assignment)
                or not scope.work_item_ref.endswith('!' + str(scope.pull_request_number))):
            self.adapter.denied('MR review manifest is outside current canonical Work/repository')
        context = dict(organization_id=assignment.organization_id, workspace_id=assignment.workspace_id,
            project_id=assignment.project_id)
        catalog, catalog_ref = self.roles.resolve_catalog(**context)
        try:
            review_policy(catalog)
            if catalog_ref.definition_schema_version != '1.1':
                raise AssignmentReviewError('mr_assignment_review_policy_schema_required')
            implementation, _, _ = self._origins(request, assignment)
        except AssignmentReviewError as exc:
            return {'outcome': 'inconclusive', 'code': str(exc), 'item': None, 'no_acceptance_recorded': True}
        # Existing authenticated provider read proves current exact MR/head/base;
        # it does not manufacture reviewer consent or a provider approval.
        from codex_web.services.broker_merge_requirements import read_merge_requirements
        read = self.broker._resolve_operation('GET', '/api/repository-facts/pull-requests/'
            + str(scope.pull_request_number) + '/merge-requirements?repository_resource_id='
            + quote(scope.repository_resource_id, safe=''))
        observation = await read_merge_requirements(self.broker, assignment, worker_actor, read, validator)
        fact = CodeHostMergeRequirementsFact.model_validate(observation['item'])
        binding_id = fact.binding_id.removeprefix('control-plane-read:')
        if (fact.changed or (fact.resource_id, fact.external_id, fact.head_revision, fact.base_revision, binding_id)
                != (scope.repository_resource_id, str(scope.pull_request_number), scope.head_sha,
                    scope.base_sha, scope.provider_binding_id)):
            self.adapter.denied('Current provider MR identity/head/base/binding changed')
        intents = self.broker.action_intents
        binding_pin = intents.execution.registry.binding(binding_id, worker).model_dump(mode='json')
        intent = intents.get(request.implementation_intent_id, worker)
        intents.store.load(intent_id=intent.id)  # initialize before writer boundary
        domains = self.adapter.guard_rows(assignment, scope.work_item_ref, validator)
        stores = [self.roles.registry.store, self.preflights.store, intents.store]
        root = service.store.store
        if any(s.store is not root for s in stores):
            self.adapter.denied('MR review provenance stores must share the canonical transaction')
        worker_store = self.broker.execution_workers.store
        domains[self.roles.registry.store.namespace] = (None,)
        domains[self.preflights.store.NAMESPACE] = tuple(self.preflights.store._attempt_key(i)
            for i in (request.implementation_preflight_id, request.validation_preflight_id))
        # Same namespace writer barrier includes future receipt keys: no unlocked
        # history inventory becomes a false completeness or original-proof claim.
        domains[intents.store.records_namespace] = ()
        domains[worker_store.assignment_namespace] = tuple(dict.fromkeys((assignment.id, implementation.id)))
        domains[worker_store.worker_namespace] = tuple(dict.fromkeys((assignment.assigned_worker_id,
            implementation.assigned_worker_id)))
        registry = intents.execution.registry.store
        if registry.store is not root:
            self.adapter.denied('MR review provider binding store differs')
        domains[registry.namespace] = (None,)
        returned = []

        def persist(rows):
            current_worker, _, _ = self.adapter.principals(assignment, worker_actor, resolved, validator, payload)
            if current_worker != worker:
                self.adapter.denied('Authenticated MR reviewer changed')
            current_work = self.adapter.scope(assignment, scope.work_item_ref)
            current_catalog, current_ref = self.roles.resolve_catalog(**context)
            if current_ref != catalog_ref or current_work.handoff != work.handoff:
                self.adapter.denied('Current MR review policy or accepted handoff changed')
            if self.broker._clock() >= fact.expires_at:
                self.adapter.denied('Exact MR provider observation expired')
            prior = self.existing(assignment, worker, source, digest(payload))
            if prior is not None:
                returned.append(prior)
                return {}
            current_artifact = service.get_artifact(artifact.id, actor=worker)
            if current_artifact != artifact:
                self.adapter.denied('Canonical MR manifest changed during review')
            self.adapter.visible(current_artifact, assignment, worker)
            original_assignment, impl, validation = self._origins(request, assignment)
            if validation.worker_identity_id != worker.identity_id:
                self.adapter.denied('MR reviewer worker principal differs from authenticated worker')
            if intents.execution.registry.binding(binding_id, worker).model_dump(mode='json') != binding_pin:
                self.adapter.denied('Current MR provider binding changed')
            original = intents.get(intent.id, worker)
            if (original.requested_by != original_assignment.created_by
                    or original.request.parameters.get('execution_workspace_id') != original_assignment.execution_workspace_id):
                self.adapter.denied('Publication requester/workspace differs from original implementation assignment')
            history = intents.store.load(intent_id=original.id)
            now = self.broker._clock()
            publication, verification = publication_proof(scope, original, history, impl, now=now)
            reviewed = qualification(scope, artifact, content, impl, validation,
                current_work.handoff, current_catalog, now=now)
            if (current_work.handoff.requested_at != request.expected_handoff_requested_at
                    or current_work.current_owner not in {validation.role_id, validation.profile_id}):
                self.adapter.denied('Exact accepted QA nonce/current recipient changed')
            receipt = AssignmentMRReviewReceipt(**scope.model_dump(), manifest_artifact_id=artifact.id,
                manifest_sha256=artifact.digest.value, disposition=reviewed.disposition,
                implementation=impl, validation=validation, implementation_intent_id=original.id,
                publication_receipt_id=publication.id, publication_verification_id=verification.id,
                handoff_requested_at=current_work.handoff.requested_at,
                handoff_acknowledged_at=current_work.handoff.acknowledged_at,
                handoff_from_role=current_work.handoff.from_agent, handoff_to_role=current_work.handoff.to_agent,
                policy_definition_ref=current_ref, recorded_at=now)
            receipt_json = canonical(receipt.model_dump(mode='json')).decode()
            if len(receipt_json.encode()) > MAX_REVIEW_BYTES:
                self.adapter.denied('MR review receipt exceeded bounded metadata contract')
            state = service.store._decode(rows[service.store.namespace][None])
            governance = service.governance.store._decode(rows[service.governance.store.namespace][None]) if service.governance else None
            if governance is None:
                self.adapter.denied('Governed MR review Evidence is required')
            creation_domains = {service.store.namespace: (None,), service.governance.store.namespace: (None,)}

            def compose(creation):
                nonlocal state, governance
                if creation.service is not service or creation.domains != creation_domains or creation.catalog_guard:
                    self.adapter.denied('MR review Evidence composition differs')
                state = creation.updater(state, governance)

            proof = service.create_evidence(EvidenceCreate(project_id=scope.project_id,
                work_item_ref=scope.work_item_ref, execution_id=assignment.execution_id,
                execution_workspace_id=assignment.execution_workspace_id, resource_ids=(scope.repository_resource_id,),
                evidence_type='review', artifact_ids=(artifact.id,), result='pass' if reviewed.disposition == 'accepted'
                    else 'fail' if reviewed.disposition == 'rejected' else 'info',
                provider=original.provider_type, source=source, observed_at=now,
                summary='Authenticated assignment/Role review; not principal independence or merge permission',
                metadata={'receipt_contract': RECEIPT_CONTRACT, 'receipt_sha256': digest(receipt.model_dump(mode='json')),
                    'receipt_json': receipt_json, 'request_sha256': digest(payload), 'assignment_id': assignment.id,
                    'assignment_fence': assignment.fence}), actor=worker, idempotency_key=source,
                _commit=compose, _state=state)

            def verification_commit(update):
                nonlocal state
                state = update(state)
                return state

            record = service.create_verification(VerificationCreate(work_item_ref=scope.work_item_ref,
                execution_id=assignment.execution_id, artifact_ids=(artifact.id,), evidence_ids=(proof.id,),
                method=METHOD, source=source, result='verified' if reviewed.disposition == 'accepted'
                    else 'rejected' if reviewed.disposition == 'rejected' else 'inconclusive',
                findings=('mr_assignment_review_receipt_sha256:' + digest(receipt.model_dump(mode='json')),)),
                actor=worker, _commit=verification_commit)
            returned.append({'item': record.model_dump(mode='json'), 'evidence': proof.model_dump(mode='json'),
                'receipt': receipt.model_dump(mode='json'), 'deduplicated': False,
                'principal_independence_claimed': False, 'merge_authorization': False})
            return {service.store.namespace: {None: state.model_dump(mode='json')},
                service.governance.store.namespace: {None: governance.model_dump(mode='json')}}

        try:
            await run_settled(lambda: root.record_mutate_many(domains, persist))
        except AssignmentReviewError as exc:
            return {'outcome': 'inconclusive', 'code': str(exc), 'item': None, 'no_acceptance_recorded': True}
        return returned[0]
