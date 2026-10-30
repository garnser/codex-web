"""Fixed MR observation protocol; observations never manufacture reviewer consent.

Provider policy and published procedure requirements remain data. This code owns
the provenance, completeness and exact-head predicates of the wire protocol.
"""
from __future__ import annotations

import hashlib
import json
import asyncio
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from codex_web.code_host_merge_requirements import CodeHostMergeRequirementsFact
from codex_web.artifact_evidence import EvidenceCreate, VerificationCreate, ArtifactCreate, sha256_digest
from codex_web.code_host_mr_observation import dispositions, MRObservationConflict
from codex_web.mr_observation_evidence import METHOD, current_observation
from codex_web.mr_continuation_input import METHOD as CONTINUATION_METHOD, INPUT_METHOD, input_values, encoded
from codex_web.data_governance import ContextFilterRequest
from codex_web.services.async_operations import run_settled
from codex_web.mr_observation_policy import (
    OBSERVATION_V2, CONTINUATION_V2, INPUT_V2, resolve_policy, validate_policy,
    canonical, expiry_valid, ObservationPolicyError,
)

MR_METHODS = (METHOD, CONTINUATION_METHOD, OBSERVATION_V2, CONTINUATION_V2)


class MRObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["code-host-mr-observation.v1", "code-host-mr-continuation-input.v1",
                    "code-host-mr-observation.v2", "code-host-mr-continuation-input.v2"]
    repository_resource_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,200}$")
    pull_request_number: StrictInt = Field(ge=1, le=2**31 - 1)
    expected_head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()




class WorkerMRVerificationAdapter:
    """Compose the existing read, verification admission and governed append owners."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.broker = adapter.broker

    def request(self, payload):
        try:
            return MRObservationRequest.model_validate(payload)
        except ValueError:
            self.adapter.denied("Invalid versioned MR observation request; result and identity overrides forbidden")

    def source(self, assignment, request):
        return "sha256:" + digest({"assignment_id": assignment.id,
            "execution_id": assignment.execution_id, "fence": assignment.fence,
            "method": request.method, "request_id": request.request_id})

    def scope(self, assignment, request):
        if self.broker._writable_repository_id(assignment) != request.repository_resource_id:
            self.adapter.denied("MR observation requires the exact assigned repository")
        workspace = self.broker.execution_workspaces.get(
            assignment.execution_workspace_id, self.broker._requester_actor(assignment))
        lease = self.broker.execution_workspaces.store.lease(workspace.lease_id)
        if (workspace.status != "active" or workspace.execution_id != assignment.execution_id
                or workspace.project_id != assignment.project_id
                or set(assignment.resource_ids) - set(workspace.resource_ids)
                or lease is None or lease.released_at is not None
                or lease.expires_at <= self.broker._clock()
                or lease.execution_id != assignment.execution_id
                or lease.execution_workspace_id != workspace.id
                or lease.owner_identity_id != workspace.owner_identity_id
                or workspace.owner_identity_id != assignment.created_by
                or lease.resource_modes.get(request.repository_resource_id) != "write"):
            self.adapter.denied("MR observation workspace custody changed")
        # This is an observation, not a Work claim. A nonempty canonical Work
        # association must nevertheless retain its actual Project/Resource scope.
        if assignment.work_item_ref:
            state = self.broker._state_for_target(assignment, assignment.work_item_ref)
            if not set(state.resource_ids).issubset(assignment.resource_ids):
                self.adapter.denied("MR observation Work Resource scope changed")

    def existing(self, assignment, worker, source, request_digest=None, method=METHOD):
        service = self.adapter.service()
        rows = [v for v in service.list_verifications(worker, work_item_ref=assignment.work_item_ref)
                if v.execution_id == assignment.execution_id and v.method == method
                and v.source == source and v.verifier_identity_id == worker.identity_id]
        if len(rows) > 1:
            self.adapter.denied("Ambiguous original MR observation; reconcile without replay")
        if not rows:
            return None
        record = rows[0]
        evidence = [service.get_evidence(i, actor=worker) for i in record.evidence_ids]
        if (len(evidence) != 3 or record.result == "invalidated"
                or any(e.producer_identity_id != worker.identity_id or e.lifecycle != "valid"
                    or e.metadata.get("assignment_id") != assignment.id
                    or e.metadata.get("assignment_fence") != assignment.fence
                    or e.execution_id != assignment.execution_id
                    or e.project_id != assignment.project_id
                    or e.resource_ids != (self.broker._writable_repository_id(assignment),)
                    or e.metadata.get("verification_method") != method for e in evidence)):
            self.adapter.denied("Original MR observation changed; no replacement proof permitted")
        governed = tuple(e.governance_record_id for e in evidence if e.governance_record_id)
        if request_digest is not None and any(
                e.metadata.get("request_sha256") != request_digest for e in evidence):
            self.adapter.denied("Original MR request identity has different content; reconcile without replay")
        if governed and (service.governance is None or service.governance.filter_context(
                ContextFilterRequest(record_ids=governed), actor=worker).denied_record_ids):
            self.adapter.denied("Original MR observation is no longer available to worker context")
        result = {"item": record.model_dump(mode="json"),
            "evidence": {e.metadata["observation_kind"]: e.model_dump(mode="json") for e in evidence},
            "deduplicated": True, "no_merge_or_QA_authorization": True,
            "observation_fresh": all(current_observation(e, now=self.broker._clock(),
                configuration=self.broker.mr_observation_configuration) for e in evidence)}
        if method in (CONTINUATION_METHOD, CONTINUATION_V2):
            ids = {e.metadata.get('continuation_input_artifact_id') for e in evidence}
            if len(ids) != 1 or None in ids:
                self.adapter.denied('Original MR continuation input association changed')
            artifact = service.get_artifact(next(iter(ids)), actor=worker)
            if (artifact.lifecycle != 'active' or artifact.producer_identity_id != worker.identity_id
                    or artifact.execution_id != assignment.execution_id or artifact.content is None
                    or artifact.metadata.get('verification_id') != record.id
                    or artifact.project_id != assignment.project_id
                    or artifact.work_item_ref != assignment.work_item_ref
                    or artifact.execution_workspace_id != assignment.execution_workspace_id
                    or artifact.resource_ids != (self.broker._writable_repository_id(assignment),)
                    or artifact.metadata.get('assignment_id') != assignment.id
                    or artifact.metadata.get('assignment_fence') != assignment.fence
                    or artifact.metadata.get('request_sha256') != evidence[0].metadata.get('request_sha256')
                    or not artifact.governance_record_id or service.governance is None):
                self.adapter.denied('Original MR continuation input is unavailable; no replacement permitted')
            if service.governance.filter_context(ContextFilterRequest(
                    record_ids=(artifact.governance_record_id,)), actor=worker).denied_record_ids:
                self.adapter.denied('Original MR continuation input is unavailable to worker context')
            result['procedure_input'] = artifact.model_dump(mode='json')
        return result

    def lookup(self, assignment, worker_actor, resolved, validator):
        """Reconcile the SAME request digest without another provider read or write."""
        import re
        query = resolved.query
        if (set(query) != {"method", "source"} or any(len(v) != 1 for v in query.values())
                or query["method"][0] not in MR_METHODS
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", query["source"][0])):
            self.adapter.denied("Exact MR method and original request digest required")
        worker, _, _ = self.adapter.principals(assignment, worker_actor, resolved, validator, {})
        result = self.existing(assignment, worker, query["source"][0], method=query["method"][0])
        self.adapter.principals(assignment, worker_actor, resolved, validator, {})
        return {"outcome": "unique" if result is not None else "unknown",
            "original": result, "absence_is_not_permission_to_replay": True}

    async def create(self, assignment, worker_actor, resolved, payload, validator):
        request = self.request(payload)
        try:
            return await self._create(assignment, worker_actor, resolved, payload, validator, request)
        except (MRObservationConflict, ObservationPolicyError) as exc:
            from codex_web.services.control_plane_broker import ControlPlaneBrokerRequestError
            raise ControlPlaneBrokerRequestError(
                "Original MR observation refused; reconcile exact source without replay",
                status_code=409, code=exc.code,
                mr_observation={"assignment_id": assignment.id,
                    "execution_id": assignment.execution_id, "fence": assignment.fence,
                    "method": request.method, "source": self.source(assignment, request),
                    "request_sha256": digest(request.model_dump(mode="json")),
                    "retryable": False, "reconcile_without_replay": True}) from exc

    async def _create(self, assignment, worker_actor, resolved, payload, validator, request):
        worker, requester, _ = await asyncio.to_thread(self.adapter.principals,
            assignment, worker_actor, resolved, validator, payload)
        await asyncio.to_thread(self.scope, assignment, request)
        source = self.source(assignment, request)
        request_digest = digest(request.model_dump(mode="json"))
        original = await asyncio.to_thread(self.existing, assignment, worker, source, request_digest, request.method)
        if original is not None:
            return original
        policy = (await asyncio.to_thread(resolve_policy, self.broker.mr_observation_configuration, now=self.broker._clock())
                  if request.method in (OBSERVATION_V2, CONTINUATION_V2) else None)
        continuation_method = request.method in (CONTINUATION_METHOD, CONTINUATION_V2)
        input_method = INPUT_V2 if request.method == CONTINUATION_V2 else INPUT_METHOD

        # Reuse the complete current requester/worker/Profile/credential/provider
        # read boundary. A previous broker audit or caller JSON is never a fact.
        from codex_web.services.broker_merge_requirements import read_merge_requirements
        read = self.broker._resolve_operation("GET", "/api/repository-facts/pull-requests/"
            + str(request.pull_request_number) + "/merge-requirements?repository_resource_id="
            + request.repository_resource_id)
        observation = await read_merge_requirements(self.broker, assignment, worker_actor, read, validator,
            observation_policy=policy)
        fact = CodeHostMergeRequirementsFact.model_validate(observation["item"])
        if (fact.resource_id != request.repository_resource_id
                or fact.external_id != str(request.pull_request_number)
                or (not expiry_valid(policy, fact.observed_at, fact.expires_at) if policy is not None
                    else not 0 < fact.expires_at - fact.observed_at <= 30)):
            self.adapter.denied("MR observation returned a different repository or request")
        raw = json.dumps(observation, sort_keys=True, separators=(",", ":"))
        if len(raw.encode()) > 262144:
            self.adapter.denied("MR observation exceeds its bounded governed record")
        result = dispositions(fact, expected_head=request.expected_head_sha, now=self.broker._clock())
        # Pin the actual canonical provider binding, not just its display name.
        binding = self.broker.action_intents.execution.registry.binding(
            fact.binding_id.removeprefix("control-plane-read:"), worker)
        binding_pin = binding.model_dump(mode="json")
        service = self.adapter.service()
        domains = self.adapter.guard_rows(assignment, assignment.work_item_ref, validator)
        registry = self.broker.action_intents.execution.registry.store
        if registry.store is not service.store.store:
            self.adapter.denied("MR observation provider registry must share the canonical transaction")
        domains[registry.namespace] = (None,)
        if policy is not None:
            config_store = self.broker.mr_observation_configuration.store
            if config_store.store is not service.store.store:
                self.adapter.denied('Observation policy must share canonical transaction')
            domains[config_store.namespace] = (None,)
        returned = []

        def persist(rows):
            if policy is not None:
                validate_policy(policy, self.broker.mr_observation_configuration, now=self.broker._clock())
            current_worker, current_requester, decision = self.adapter.principals(
                assignment, worker_actor, resolved, validator, payload)
            self.scope(assignment, request)
            if (current_worker != worker
                    or (current_requester.identity_id, current_requester.tenant, current_requester.principal_kind)
                        != (requester.identity_id, requester.tenant, requester.principal_kind)
                    or self.broker.action_intents.execution.registry.binding(binding.id, worker).model_dump(mode="json") != binding_pin):
                self.adapter.denied("MR observation principals or provider binding changed before append")
            dispositions(fact, expected_head=request.expected_head_sha, now=self.broker._clock())
            prior = self.existing(assignment, worker, source, request_digest, request.method)
            if prior is not None:
                returned.append(prior)
                return {}
            # Decode the complete locked documents once for this transaction.
            # Existing canonical creation updaters still validate every append;
            # no model or snapshot survives this callback or grants authority.
            state = service.store._decode(rows[service.store.namespace][None])
            governance = (service.governance.store._decode(rows[service.governance.store.namespace][None])
                          if service.governance is not None else None)
            creation_domains = {service.store.namespace: (None,)}
            if governance is not None:
                creation_domains[service.governance.store.namespace] = (None,)

            def compose(creation):
                nonlocal state
                if (creation.service is not service or creation.domains != creation_domains
                        or not set(creation.domains).issubset(domains)):
                    self.adapter.denied("MR observation governance transaction is incomplete")
                if creation.catalog_guard is not None:
                    self.adapter.denied("MR observation composition cannot replace a catalog guard")
                state = creation.updater(state, governance)

            proofs = {}
            for name, (kind, outcome) in result.items():
                proofs[name] = service.create_evidence(EvidenceCreate(
                    project_id=assignment.project_id, work_item_ref=assignment.work_item_ref,
                    execution_id=assignment.execution_id, execution_workspace_id=assignment.execution_workspace_id,
                    resource_ids=(request.repository_resource_id,), evidence_type=kind, result=outcome,
                    provider=fact.provider_type, source=source, external_id=str(request.pull_request_number),
                    observed_at=fact.observed_at,
                    summary="Canonical exact-head MR observation; not merge permission or new reviewer consent",
                    metadata={"verification_method": request.method, "observation_kind": name,
                        **({"continuation_input_artifact_id": service._idempotent_evidence_id(worker, source, source + ":input").replace("evidence-", "artifact-", 1)} if continuation_method else {}),
                        **({'observation_policy': canonical(policy.model_dump(mode='json')),
                            'observation_started_at': fact.observed_at} if policy else {}),
                        "assignment_id": assignment.id, "assignment_fence": assignment.fence,
                        "requester_identity_id": requester.identity_id,
                        "head_sha": fact.head_revision, "request_sha256": request_digest,
                        "provider_read_id": observation["scope"]["read_id"],
                        "observation_expires_at": fact.expires_at,
                        "provider_snapshot_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                        "provider_snapshot_json": raw}), actor=worker,
                    idempotency_key=source + ":" + name, _commit=compose, _state=state)

            def verification_commit(updater):
                nonlocal state
                state = updater(state)
                return state

            outcomes = [x[1] for x in result.values()]
            verification = service.create_verification(VerificationCreate(
                work_item_ref=assignment.work_item_ref, execution_id=assignment.execution_id,
                method=request.method, evidence_ids=tuple(e.id for e in proofs.values()), source=source,
                result="rejected" if "fail" in outcomes else "verified" if all(x == "pass" for x in outcomes) else "inconclusive",
                findings=("Original provider observations; verifier independence is computed, not asserted.",)),
                actor=worker, _commit=verification_commit)
            continuation = None
            if continuation_method:
                values = input_values(fact, expected_head=request.expected_head_sha, protocol=input_method)
                content = encoded(values)
                continuation = service.create_artifact(ArtifactCreate(
                    project_id=assignment.project_id, work_item_ref=assignment.work_item_ref,
                    execution_id=assignment.execution_id, execution_workspace_id=assignment.execution_workspace_id,
                    resource_ids=(request.repository_resource_id,), artifact_type='report',
                    name='Governed MR continuation input', provider=fact.provider_type, source=source,
                    external_id=str(request.pull_request_number), revision=fact.head_revision,
                    digest=sha256_digest(content),
                    metadata={'input_method': input_method, 'verification_id': verification.id,
                        **({'observation_policy': canonical(policy.model_dump(mode='json'))} if policy else {}),
                        'assignment_id': assignment.id, 'assignment_fence': assignment.fence,
                        'requester_identity_id': requester.identity_id, 'request_sha256': request_digest,
                        'source_evidence_ids': json.dumps(sorted(e.id for e in proofs.values()), separators=(',', ':')),
                        'provider_binding_sha256': digest(binding_pin),
                        'observed_requirements_fingerprint': values['observed_requirements_fingerprint']}),
                    actor=worker, source_evidence_ids=tuple(e.id for e in proofs.values()),
                    idempotency_key=source + ':input', _commit=compose)
                continuation = service.attach_artifact_content(continuation.id, (content,), actor=worker,
                    media_type='application/json', _state=state,
                    _commit=verification_commit)
            # Serialize each complete domain once, then check freshness. Expiry
            # during domain composition/dumping aborts all canonical appends.
            # Backend commit/transport can still take time; historical publication
            # is never permission to bypass the procedure's live input checks.
            changes = {service.store.namespace: {None: state.model_dump(mode="json")}}
            if governance is not None:
                changes[service.governance.store.namespace] = {None: governance.model_dump(mode="json")}
            response = {"item": verification.model_dump(mode="json"),
                **({"procedure_input": continuation.model_dump(mode="json")} if continuation is not None else {}),
                "evidence": {k: e.model_dump(mode="json") for k, e in proofs.items()},
                "attribution": {"producer_identity_id": worker.identity_id,
                    "authorizer_identity_id": requester.identity_id, "authority_decision_id": decision.id},
                "no_merge_or_QA_authorization": True}
            self.adapter.principals(assignment, worker_actor, resolved, validator, payload)
            self.scope(assignment, request)
            if policy is not None:
                validate_policy(policy, self.broker.mr_observation_configuration, now=self.broker._clock())
            dispositions(fact, expected_head=request.expected_head_sha, now=self.broker._clock())
            returned.append(response)
            return changes

        await run_settled(lambda: service.store.store.record_mutate_many(domains, persist))
        # Commit/projection work can outlast the observation even after the
        # callback's final guard. The original may now be durably published:
        # retain it and use the existing exact-source reconciliation refusal,
        # never report rollback or authorize a replacement provider read.
        dispositions(fact, expected_head=request.expected_head_sha, now=self.broker._clock())
        if policy is not None:
            await asyncio.to_thread(validate_policy, policy, self.broker.mr_observation_configuration, now=self.broker._clock())
        return returned[0]
