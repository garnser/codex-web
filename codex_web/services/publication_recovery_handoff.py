"""Governed recovery handoff on the existing workspace/action transaction boundary."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import time

from codex_web.execution_profiles import ExecutionProfileCatalogDefinition
from codex_web.execution_workspaces import (
    PublicationRecoveryHandoff, PublicationRecoveryHandoffRequest, PublicationRecoveryProvenance,
)
from codex_web.definitions import reference_for
from codex_web.code_hosts import CodeHostError
from codex_web.services.delivery_validation import validate_delivery_runs
from codex_web.models import WorkItemState
from codex_web.services.publication_recovery import (
    WS, PublicationRecoveryConflict, live_scope, scope_defaults,
)


class PublicationRecoveryHandoffService:
    def __init__(self, broker):
        self.broker = broker
        self.workspaces = broker.execution_workspaces
        self.intents = broker.action_intents
        self.workers = self.intents.execution_workers if self.intents else None

    def _initialize(self):
        if not self.workspaces or not self.intents or not self.workers or not self.broker.code_hosts:
            raise PublicationRecoveryConflict("Recovery handoff dependencies unavailable; ask the canonical execution operator")
        stores = (self.workspaces.store.store, self.intents.store.store, self.workers.store.store)
        if not all(item is stores[0] for item in stores):
            raise PublicationRecoveryConflict("Recovery handoff requires one canonical transactional StateStore")
        self.workspaces.store.load()
        self.workers.store.assignment("__inspection__")
        self.intents.store._ensure_records()
        return stores[0]

    def _profile(self, assignment):
        profiles = self.broker.execution_profiles
        if profiles is None or assignment.execution_profile_definition is None:
            raise PublicationRecoveryConflict("Exact published execution profile pin is required")
        record = profiles.registry.get_record(assignment.execution_profile_definition.record_id)
        profiles.registry._validated_record(record)
        if reference_for(record) != assignment.execution_profile_definition or record.lifecycle != "published":
            raise PublicationRecoveryConflict("Pinned execution profile is unavailable or incompatible")
        if record.scope_type == "project" and record.scope_id != assignment.project_id:
            raise PublicationRecoveryConflict("Pinned execution profile belongs to a different Project")
        now = time.time()
        if ((record.effective_from is not None and record.effective_from > now)
                or (record.effective_until is not None and record.effective_until <= now)):
            raise PublicationRecoveryConflict("Pinned execution profile is outside its effective interval")
        catalog = ExecutionProfileCatalogDefinition.model_validate(record.payload)
        profile = next((item for item in catalog.profiles if item.id == assignment.execution_profile_id), None)
        if (profile is None or profile.repository_access != "mutable" or profile.host_mutation
                or "repository.branch.publish" not in profile.control_plane_operations):
            raise PublicationRecoveryConflict("Pinned profile does not permit governed recovery publication")
        return record

    @staticmethod
    def _work_item(raw, assignment, repository_id, *, target):
        if raw is None:
            raise PublicationRecoveryConflict("Recovery Work Item is missing")
        item = WorkItemState.model_validate(raw)
        if (item.organization_id != assignment.organization_id or item.workspace_id != assignment.workspace_id
                or item.project_id != assignment.project_id or repository_id not in item.resource_ids):
            raise PublicationRecoveryConflict("Recovery Work Item tenant/Project/repository differs")
        owners = {assignment.agent_profile.profile_id, assignment.agent_profile.role_id}
        owner = item.current_owner if target else item.implementation_owner
        if owner not in owners:
            raise PublicationRecoveryConflict("Recovery Work Item implementation owner differs from the pinned agent")
        if target and (item.closed_at is not None or item.current_stage == "closed"):
            raise PublicationRecoveryConflict("New recovery Work Item is terminal; select an owned open issue")
        if not target and (item.closed_at is None or item.current_stage != "closed"
                           or item.terminal_outcome in {"failed", "cancelled"}):
            raise PublicationRecoveryConflict("Prior Work Item is not completed; verify its implementation and terminal source")
        return item

    @staticmethod
    def _head(workspace, expected):
        member = next((m for m in workspace.repository_members if m.resource_id == workspace.repository_resource_id), None)
        if member is None or member.access_mode != "write":
            raise PublicationRecoveryConflict("Writable repository member is missing")
        try:
            head = subprocess.run(["git", "-C", member.workspace_path, "rev-parse", "HEAD"],
                                  check=True, capture_output=True, text=True, timeout=10).stdout.strip()
            branch = subprocess.run(["git", "-C", member.workspace_path, "symbolic-ref", "--short", "HEAD"],
                                    check=True, capture_output=True, text=True, timeout=10).stdout.strip()
            dirty = subprocess.run(["git", "-C", member.workspace_path, "status", "--porcelain"],
                                   check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise PublicationRecoveryConflict("Cannot attest repository head; inspect workspace") from exc
        if head != expected or branch != member.branch_name or dirty:
            raise PublicationRecoveryConflict("Repository head/branch is stale or dirty; preserve and review the current checkout")

    def _completion(self, documents, assignment, prior, selected_ids):
        store = self.intents.store
        state = store._state(documents[store.records_namespace])
        related = [i for i in state.intents if i.organization_id == assignment.organization_id
                   and i.workspace_id == assignment.workspace_id and i.project_id == assignment.project_id
                   and prior.repository_resource_id in i.resource_ids
                   and (i.work_item_ref == prior.work_item_ref or i.execution_id == prior.source_execution_id)]
        receipts = {r.id: r for r in state.receipts}
        verifications = {v.id: v for v in state.verifications}
        for intent in related:
            if intent.status in {"pending", "claimed", "executing", "uncertain", "requires_reconciliation"}:
                raise PublicationRecoveryConflict(f"Intent {intent.id} is {intent.status}; inspect/reconcile it before handoff")
            if intent.status in {"failed", "cancelled", "rolled_back"}:
                receipt = receipts.get(intent.last_receipt_id)
                # No receipt, generic code or terminal label proves non-start.
                if (receipt is None or receipt.outcome == "unknown"
                        or any(r.outcome == "unknown" for r in state.receipts if r.intent_id == intent.id)):
                    raise PublicationRecoveryConflict(f"Intent {intent.id} lacks authoritative terminal disposition; reconcile historical outcome")
        selected = [i for i in related if i.id in selected_ids]
        if len(selected) != len(set(selected_ids)):
            raise PublicationRecoveryConflict("Completion intents are missing or outside prior scope")
        successful = {}
        for intent in selected:
            receipt = receipts.get(intent.last_receipt_id)
            verification = verifications.get(intent.last_verification_id)
            if (intent.status != "succeeded" or receipt is None or receipt.intent_id != intent.id
                    or receipt.action_id != intent.action_id or receipt.provider_type != intent.provider_type
                    or receipt.provider_instance != intent.provider_instance or receipt.idempotency_key != intent.idempotency_key
                    or receipt.outcome != "completed" or receipt.result is None or receipt.result.status != "succeeded"
                    or receipt.result.provider_binding_id != intent.binding_id or receipt.result.action_id != intent.action_id
                    or verification is None or verification.intent_id != intent.id or not verification.verified
                    or verification.provider_verification is None or not verification.provider_verification.verified
                    or (intent.expected_evidence and verification.evidence_satisfied is not True)
                    or intent.requested_by != prior.authorized_by
                    or intent.execution_id != prior.source_execution_id
                    or intent.created_at < prior.authorized_at
                    or intent.work_item_ref not in {None, prior.work_item_ref}):
                raise PublicationRecoveryConflict(f"Completion intent {intent.id} has no matching verified implementation/publication receipt")
            if intent.publication_recovery_scope and intent.publication_recovery_scope.recovery != prior:
                raise PublicationRecoveryConflict("Completion intent belongs to a different recovery revision")
            successful[intent.action_id] = (intent, receipt)
        branch = successful.get("code-host.branch.publish")
        merge = successful.get("code-host.pull-request.merge")
        if branch is None or merge is None:
            raise PublicationRecoveryConflict("Verified branch publication and merged implementation receipts are required")
        head = branch[1].result.output.get("head_revision")
        merge_output = merge[1].result.output
        if not head or merge_output.get("head_sha") != head or not merge_output.get("merge_commit_sha"):
            raise PublicationRecoveryConflict("Publication/implementation receipts do not identify the same exact source head")
        number = merge_output.get("pull_request_number")
        # Scope attribution is typed canonical state, not an interpretation of PR prose.
        # Legacy broker receipts may omit work_item_ref, but remain bound to the exact
        # one-issue workspace scope, originating execution/owner and publication head.
        branch_name = branch[1].result.output.get("branch")
        linked = False
        for intent in selected:
            if intent.action_id != "code-host.pull-request.upsert":
                continue
            receipt = receipts.get(intent.last_receipt_id)
            if (receipt and receipt.result and branch_name
                    and str(receipt.result.output.get("change_request_number")) == str(number)
                    and intent.request.parameters.get("head") == branch_name
                    and receipt.result.output.get("head") == branch_name
                    and branch[0].request.parameters.get("execution_workspace_id") == assignment.execution_workspace_id):
                linked = True
        if not linked:
            raise PublicationRecoveryConflict("Merged implementation lacks exact typed scope, owned PR and branch publication attribution")
        return related, state, head, str(merge_output["merge_commit_sha"]), str(number)

    async def handoff(self, request: PublicationRecoveryHandoffRequest, *, actor, preview=False):
        fingerprint = hashlib.sha256(json.dumps(request.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
        store = await asyncio.to_thread(self._initialize)
        defaults = {**scope_defaults(), self.intents.store.records_namespace: {},
                    self.workspaces.store.event_namespace: {}, "work_item_states": {}}
        canonical_assignment = await asyncio.to_thread(self.workers.store.assignment, request.expected_assignment_id)
        if canonical_assignment is None or canonical_assignment.organization_id != actor.organization_id or canonical_assignment.workspace_id != actor.workspace_id:
            raise PublicationRecoveryConflict("Recovery assignment is missing or outside tenant scope")
        await asyncio.to_thread(self.broker._state_for_target,
                                canonical_assignment, request.expected_prior_scope.work_item_ref)
        await asyncio.to_thread(self.broker._state_for_target,
                                canonical_assignment, request.work_item_ref)
        documents = {key: store.record_items(key) for key in defaults}
        assignment, workspace, prior = live_scope(documents, request.expected_assignment_id,
                                                  request.execution_workspace_id, actor.identity_id)
        if actor.organization_id != assignment.organization_id or actor.workspace_id != assignment.workspace_id:
            raise PublicationRecoveryConflict("Cross-tenant recovery denied")
        operation = self.broker._resolve_operation("POST", "/api/repository-workspace/publication-recovery").operation
        self.broker._authorize(assignment, operation, actor=actor, resource_ids=(request.repository_resource_id,))
        await asyncio.to_thread(self._profile, assignment)
        # A repeated exact request returns its durable receipt without performing another transition.
        for history in workspace.publication_recovery_history:
            if history.idempotency_key == request.idempotency_key:
                if history.request_fingerprint != fingerprint:
                    raise PublicationRecoveryConflict("Handoff idempotency key is bound to a different transition")
                if workspace.publication_recovery != history.current_scope:
                    raise PublicationRecoveryConflict("Handoff has since been superseded; inspect current scope")
                return {"item": workspace.model_dump(mode="json"), "handoff": history.model_dump(mode="json"), "repeated": True}
        if (prior is None or prior != request.expected_prior_scope
                or prior.work_item_ref == request.work_item_ref
                or request.expected_assignment_fence != assignment.fence
                or request.repository_resource_id != prior.repository_resource_id):
            raise PublicationRecoveryConflict("Expected prior scope, assignment fence or repository is stale; refresh inspection")
        old = self._work_item(documents["work_item_states"].get(prior.work_item_ref), assignment, request.repository_resource_id, target=False)
        target = self._work_item(documents["work_item_states"].get(request.work_item_ref), assignment, request.repository_resource_id, target=True)
        await asyncio.to_thread(self._head, workspace, request.expected_head_revision)
        related, intent_state, head, merge_revision, number = self._completion(documents, assignment, prior, request.completion_intent_ids)
        source = self.broker.work_items.task_source_registry.resolve(old, required=True)
        snapshot = await source.read(old.source_identity)
        if (snapshot.identity.source_type != old.source_identity.source_type
                or snapshot.identity.source_instance != old.source_identity.source_instance
                or snapshot.identity.external_id != old.source_identity.external_id
                or not snapshot.identity.revision or snapshot.source_state not in {"closed", "completed"}):
            raise PublicationRecoveryConflict("Prior issue has no exact authoritative terminal proof; inspect source")
        binding_id, repository_id = self.broker._code_host_binding(assignment=assignment, actor=actor)
        merged = await self.broker.code_hosts.pull_request(binding_id, repository_id, number, actor=actor)
        if merged.state not in {"merged", "closed"} or merged.merge_revision != merge_revision:
            raise PublicationRecoveryConflict("Authoritative implementation merge does not match retained receipt")
        checks = []
        for revision in dict.fromkeys((head, merge_revision)):
            try:
                runs = await asyncio.wait_for(self.broker.code_hosts.validation_runs(
                    binding_id, repository_id, revision, actor=actor), timeout=120)
            except (CodeHostError, ValueError, TimeoutError) as exc:
                raise PublicationRecoveryConflict("Authoritative applicability/qualification evidence is unavailable; inspect provider run/selection evidence") from exc
            checks.extend(validate_delivery_runs(runs, revision, recovery_required=revision == merge_revision))
        # Existing published Work Item requirements remain authoritative; never weaken them here.
        requirements = self.intents._work_item_requirements(old.ref, actor)
        evidence_snapshot = store.get("artifact_evidence")
        definitions_snapshot = store.get("definition_registry")
        if requirements:
            gate = await asyncio.to_thread(self.intents.artifact_evidence.evaluate,
                                          old.ref, requirements, actor=actor)
            if not gate.satisfied:
                raise PublicationRecoveryConflict("Prior Work Item evidence requirements are not satisfied")
        if preview:
            return {"allowed": True, "prior_scope": prior.model_dump(mode="json"), "work_item_ref": target.ref,
                    "source_identity": snapshot.identity.model_dump(mode="json"), "validation_checks": checks,
                    "completion_intent_ids": list(request.completion_intent_ids), "merge_revision": merge_revision,
                    "next_action": "Confirm retirement of this exact scope; execution subject, lease and permissions stay unchanged"}
        defaults["definition_registry"] = {"schema_version": "1.0", "records": []}
        if requirements:
            defaults["artifact_evidence"] = evidence_snapshot
        observed_assignment = assignment
        observed_old = old.model_dump(mode="json")
        observed_target = target.model_dump(mode="json")
        history_result = []
        def apply(current):
            now = time.time()
            latest_assignment, latest_workspace, latest_prior = live_scope(current, assignment.id, workspace.id, actor.identity_id, now=now)
            for retained in latest_workspace.publication_recovery_history:
                if retained.idempotency_key == request.idempotency_key:
                    if retained.request_fingerprint != fingerprint or latest_prior != retained.current_scope:
                        raise PublicationRecoveryConflict("Concurrent handoff key conflicts with current scope")
                    history_result.append(retained)
                    return current
            if (latest_assignment.fence != request.expected_assignment_fence
                    or latest_assignment.execution_profile_definition != observed_assignment.execution_profile_definition
                    or latest_assignment.agent_profile != observed_assignment.agent_profile
                    or latest_prior != request.expected_prior_scope
                    or current["work_item_states"].get(old.ref) != observed_old
                    or current["work_item_states"].get(target.ref) != observed_target):
                raise PublicationRecoveryConflict("Recovery scope/owner/evidence changed during validation; observe again")
            if current["definition_registry"] != definitions_snapshot:
                raise PublicationRecoveryConflict("Published definitions changed during validation; preview again")
            if requirements and current["artifact_evidence"] != evidence_snapshot:
                raise PublicationRecoveryConflict("Canonical completion evidence changed during validation; preview again")
            self._head(latest_workspace, request.expected_head_revision)
            current_related, current_intents, _, _, _ = self._completion(
                current, latest_assignment, latest_prior, request.completion_intent_ids)
            # Git attestation and receipt validation are bounded but may outlast a short lease.
            now = time.time()
            live_scope(current, assignment.id, workspace.id, actor.identity_id, now=now)
            new = PublicationRecoveryProvenance(**{**latest_prior.model_dump(), "work_item_ref": target.ref,
                                                    "authorized_at": now, "authorized_by": actor.identity_id})
            ids = {i.id for i in current_related}
            history = PublicationRecoveryHandoff(
                idempotency_key=request.idempotency_key, request_fingerprint=fingerprint, prior_scope=latest_prior, current_scope=new,
                execution_profile_definition=assignment.execution_profile_definition,
                agent_profile_record_id=assignment.agent_profile.profile_record_id,
                head_revision=request.expected_head_revision, authorized_by=actor.identity_id, retired_at=now,
                source_identity=snapshot.identity.model_dump(mode="json"), source_state=snapshot.source_state,
                completion_intent_ids=request.completion_intent_ids, intent_ids=tuple(sorted(ids)),
                receipt_ids=tuple(r.id for r in current_intents.receipts if r.intent_id in ids),
                verification_ids=tuple(v.id for v in current_intents.verifications if v.intent_id in ids),
                validation_checks=tuple(checks), merge_revision=merge_revision,
            )
            updated = latest_workspace.model_copy(update={"publication_recovery": new,
                "publication_recovery_history": (*latest_workspace.publication_recovery_history, history), "updated_at": now})
            current[WS][workspace.id] = updated.model_dump(mode="json")
            event_key = "handoff:" + hashlib.sha256((workspace.id + request.idempotency_key).encode()).hexdigest()
            current[self.workspaces.store.event_namespace][event_key] = {
                "workspace_id": workspace.id, "event_type": "publication_recovery_handoff", "occurred_at": now,
                "actor_identity_id": actor.identity_id, "details": {"prior_work_item_ref": old.ref,
                    "work_item_ref": target.ref, "assignment_id": assignment.id, "assignment_fence": assignment.fence,
                    "idempotency_key": request.idempotency_key, "head_revision": request.expected_head_revision}}
            history_result.append(history)
            return current
        committed = await asyncio.to_thread(store.update_many, defaults, apply)
        return {"item": committed[WS][workspace.id], "handoff": history_result[0].model_dump(mode="json"), "repeated": False}
