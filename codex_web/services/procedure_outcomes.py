"""Deterministic verified-execution -> governed Memory -> inactive Skill draft.

No models, tools, publication, scheduler or private delivery store. Delivery
identity is the immutable assignment/pin, regardless of event retry/trigger name.
"""
import difflib
import hashlib
import json
from copy import copy, deepcopy

from codex_web.definitions import DefinitionLifecycle, definition_checksum, reference_for
from codex_web.data_governance import GovernedDataCreate, DataCategory, ContextFilterRequest
from codex_web.organizational_memory import KnowledgeCreate, KnowledgeProvenance
from codex_web.services.organizational_memory import KnowledgeConflictError, OrganizationalMemoryError
from codex_web.services.procedure_execution import ProcedureExecutionError, bounded_data
from codex_web.services.procedure_discovery import checksum
from codex_web.services.skills import SkillConflict
from codex_web.skills import SkillDefinition, SkillPromotionRequest, SkillProvenance
from codex_web.attention import AttentionItemCreate, AttentionSource
from codex_web.services.identity import IdentityError


class _TransactionDocuments:
    """A scoped view of update_many's locked documents, never a durable store.

    Domain engines run unchanged on private service/store copies. Their writes
    replace only the documents owned by the enclosing canonical transaction.
    No callback, index update or notification escapes before commit.
    """
    def __init__(self, documents):
        self.documents = documents

    def get(self, namespace, default=None):
        if namespace not in self.documents:
            raise SkillConflict("undeclared transaction dependency")
        return deepcopy(self.documents[namespace] if self.documents[namespace] is not None else default)

    def update(self, namespace, updater, default=None):
        updated = updater(self.get(namespace, default))
        self.documents[namespace] = deepcopy(updated)
        return deepcopy(updated)


class ProcedureOutcomeService:
    def __init__(self, runner, memory, *, incidents=None):
        self.runner, self.memory, self.incidents = runner, memory, incidents
        self.skills = runner.skills
        self.skills.outcome_drafts = self
        self.memory.governance.register_action_handler("procedure_skill", self.erase)
        # Existing source-deletion cascade owns traversal, including derived Skills.
        self.memory.procedure_outcomes = self
        self.runner.evidence.procedure_outcomes = self

    async def on_completion(self, assignment, actor, *, incident_id=None):
        """Meaningful completion delivery. A retry sees the same canonical draft.

        A learning conflict does not undo a verified operational outcome. Surface
        it in canonical Attention with metadata only; explicit conversion exposes
        the conflict through HTTP as well.
        """
        import asyncio
        from codex_web.services.definitions import DefinitionConflictError
        try:
            result = await asyncio.to_thread(self.convert, assignment.id, actor=actor, incident_id=incident_id,
                run_id=self.runner.run_id(assignment.procedure_checkpoint))
            if result["status"] in {"draft", "published"}:
                return result
            reason = "procedure_outcome_requires_review"
        except (SkillConflict, DefinitionConflictError, ProcedureExecutionError, IdentityError,
                OrganizationalMemoryError, LookupError):
            reason = "procedure_outcome_source_or_revision_conflict"
        await self.runner.attention.upsert(AttentionItemCreate(
            organization_id=assignment.organization_id, workspace_id=assignment.workspace_id,
            project_id=assignment.project_id, type="procedure_review",
            source=AttentionSource(object_type="execution_assignment", object_id=assignment.id),
            reason=reason, dedupe_key="procedure-outcome:" + self._identity(assignment),
            deep_link=f"/api/execution-workers/assignments/{assignment.id}/procedure/outcome?run_id={self.runner.run_id(assignment.procedure_checkpoint)}",
            diagnostic_refs=(f"execution-assignment:{assignment.id}",
                f"definition:{assignment.procedure_checkpoint.definition.record_id}"),
            severity="warning", owner_identity_id=actor.identity_id), actor_id=actor.identity_id)
        return {"status":"review_required", "review_needs":[reason], "draft":None}

    def _identity(self, assignment):
        pin = assignment.procedure_checkpoint
        return hashlib.sha256(json.dumps([
            assignment.organization_id, assignment.workspace_id, assignment.id,
            pin.definition.model_dump(mode="json"),
            *([pin.id] if pin.id else []),
        ], sort_keys=True).encode()).hexdigest()

    def _source(self, assignment_id, actor, run_id=None):
        assignment = self.runner.select_run(self.runner.get(assignment_id, actor), run_id)
        if assignment.procedure_checkpoint is None:
            raise ProcedureExecutionError("procedure_checkpoint_missing")
        pin = assignment.procedure_checkpoint
        record = self.skills.definitions.get_record(pin.definition.record_id)
        self.skills._assert_visible_record(record, actor)
        if reference_for(record) != pin.definition:
            raise ProcedureExecutionError("procedure_source_pin_changed")
        return assignment, record

    def extract(self, assignment_id, *, actor, incident_id=None, run_id=None):
        assignment, record = self._source(assignment_id, actor, run_id)
        pin = assignment.procedure_checkpoint
        procedure = SkillDefinition.model_validate(record.payload).operational_procedure
        if procedure is None:
            raise ProcedureExecutionError("procedure_source_missing")
        needs = []
        if record.lifecycle not in {DefinitionLifecycle.PUBLISHED, DefinitionLifecycle.SUPERSEDED}:
            needs.append("procedure_source_not_published_or_revoked")
        if SkillDefinition.model_validate(record.payload).lifecycle != "active":
            needs.append("procedure_source_archived")
        try:
            self.skills.validate_required_procedure(pin.definition, actor=actor, project_id=assignment.project_id)
        except (SkillConflict, LookupError, ValueError) as exc:
            needs.append(getattr(exc, "code", "procedure_source_unavailable"))
        if pin.status != "completed":
            needs.append("outcome_unverified_or_recovery_only")
        proof_ids = []
        signatures = []
        observations = []
        sources = [r.id for r in self.memory.governance.store.load().records
            if r.category == DataCategory.DEFINITION and r.object_id == record.record_id
            and (r.organization_id, r.workspace_id) == (actor.organization_id, actor.workspace_id)]
        # Recheck exact pins/proof provenance, not just a terminal status string.
        try:
            self.runner._inputs(assignment, actor, procedure)
            snapshots = [pin.input_snapshot_id]
            for step in procedure.steps:
                state = pin.steps[step.id]
                if state.status in {"failed", "verification_failed", "reconciliation_required"}:
                    needs.append("failed_or_uncertain_step:" + step.id)
                if state.status != "succeeded":
                    continue
                self.runner._proof(assignment, step, state, actor)
                if step.kind in {"mutation", "recovery"}:
                    if pin.steps[step.verification_step_id].status != "succeeded":
                        needs.append("missing_effect_verification:" + step.id)
                    intent = self.runner.intents.get(state.action_intent_id, self.runner.control_actor)
                    if intent.status != "succeeded" or intent.execution_id != assignment.execution_id:
                        needs.append("effect_not_verified:" + step.id)
                proof_ids.extend(state.evidence.values())
                if state.output_snapshot_id:
                    snapshots.append(state.output_snapshot_id)
                # Labels, IDs and typed contracts only: never copy output values,
                # log summaries, postmortem claims, or free-form deviation reasons.
                observations.append({"step_id": step.id, "kind": step.kind,
                    "evidence": state.evidence, "preconditions": [c.model_dump(mode="json") for c in step.preconditions],
                    "action_intent_id": state.action_intent_id,
                    "output_snapshot_id": state.output_snapshot_id,
                    "output_snapshot_checksum": state.output_snapshot_checksum})
            for artifact_id in snapshots:
                artifact = self.runner.evidence.get_artifact(artifact_id, actor=actor)
                if not artifact.governance_record_id:
                    raise ProcedureExecutionError("procedure_source_governance_missing")
                sources.append(artifact.governance_record_id)
            for proof_id in proof_ids:
                proof = self.runner.evidence.get_evidence(proof_id, actor=actor)
                if not proof.governance_record_id:
                    raise ProcedureExecutionError("procedure_source_governance_missing")
                sources.append(proof.governance_record_id)
                signature = proof.metadata.get("diagnostic_signature")
                if isinstance(signature, str) and len(signature) <= 1000:
                    signatures.append({"evidence_id":proof.id,
                        "sha256":hashlib.sha256(signature.encode()).hexdigest()})
                for artifact_id in proof.artifact_ids:
                    artifact = self.runner.evidence.get_artifact(artifact_id, actor=actor)
                    if (artifact.lifecycle != "active" or artifact.retention_expires_at is not None
                        and artifact.retention_expires_at <= self.runner.clock()):
                        raise ProcedureExecutionError("procedure_source_artifact_stale")
                    if artifact.governance_record_id:
                        sources.append(artifact.governance_record_id)
        except (RuntimeError, ValueError, LookupError) as exc:
            needs.append(getattr(exc, "code", "source_evidence_unavailable"))
        if not proof_ids:
            needs.append("verified_evidence_missing")
        if self.runner.clock() >= min(procedure.review_due_at, procedure.expires_at):
            needs.append("procedure_review_due")
        incident_ref = None
        postmortem_ref = None
        postmortem_knowledge = None
        if incident_id:
            if self.incidents is None:
                raise ProcedureExecutionError("incident_source_unavailable")
            incident = self.incidents.get(incident_id, actor=actor)
            if (incident.project_id != assignment.project_id
                or not set(incident.affected_resource_ids).issubset(assignment.resource_ids)
                or not set(incident.evidence_ids).intersection(proof_ids)):
                raise ProcedureExecutionError("incident_source_mismatch")
            if incident.status not in {"resolved", "postmortem", "closed"}:
                needs.append("incident_resolution_unverified")
            incident_ref = incident.id
            postmortem_ref = incident.postmortem_id
            if postmortem_ref:
                postmortem = self.incidents.store.load().postmortems[postmortem_ref]
                if postmortem.knowledge_id:
                    knowledge = self.memory.get(postmortem.knowledge_id, actor=actor)
                    postmortem_knowledge = {"id":knowledge.id, "version":knowledge.version,
                        "checksum":knowledge.content_sha256}
                    sources.append(knowledge.governance_record_id)
        facts = self.runner._facts(assignment, actor)
        if checksum(facts) != pin.resource_context_checksum:
            needs.append("resource_context_changed_requires_review")
        report = {"schema_version": "1.0", **({"run_id": pin.id} if pin.id else {}), "assignment_id": assignment.id,
            "execution_id": assignment.execution_id, "checkpoint_revision": pin.revision,
            "prior_definition": pin.definition.model_dump(mode="json"),
            "incident_id": incident_ref, "postmortem_id":postmortem_ref,
            "postmortem_knowledge":postmortem_knowledge, "input_snapshot_id": pin.input_snapshot_id,
            "input_snapshot_checksum": pin.input_snapshot_checksum,
            "resource_context_checksum": pin.resource_context_checksum,
            "resource_ids": assignment.resource_ids,
            "resource_facts":facts,
            "applicability": {"resource_selectors": [r.model_dump(mode="json") for r in procedure.resource_selectors],
                "environments": procedure.environments,
                "service_versions":[v.model_dump(mode="json") for v in procedure.service_versions]},
            "observations": observations,
            "diagnostic_signatures":signatures,
            "hypotheses": ["Underlying root cause remains unconfirmed; successful restoration does not prove causation."],
            "deviations": [{"step_id": d.step_id, "evidence_ids": d.evidence_ids,
                "authority_definition": d.authority_definition.model_dump(mode="json"),
                "review_required": True} for d in pin.deviations],
            "limits": "Only observed scope and steps are tested. Recovery branches and other versions require separate verification. Review deviations before changing executable steps.",
            "review_due_at": procedure.review_due_at, "expires_at": procedure.expires_at,
            "review_needs": sorted(set(needs))}
        bounded_data(report, 24000)
        return assignment, record, report, tuple(dict.fromkeys(proof_ids)), tuple(dict.fromkeys(sources))

    def _knowledge(self, assignment, report, evidence_ids, sources, actor):
        key = "procedure-outcome/" + self._identity(assignment)
        existing = [r for r in self.memory.store.list(organization_id=actor.organization_id,
            workspace_id=actor.workspace_id) if r.logical_key == key]
        payload = KnowledgeCreate(logical_key=key, object_type="procedure",
                title="Verified procedure outcome awaiting review", summary="Observed step proofs; root cause unconfirmed.",
                content=json.dumps(report, sort_keys=True), project_id=assignment.project_id,
                canonical_refs=[{"object_type":"execution_assignment", "object_id":assignment.id},
                    {"object_type":"definition", "object_id":assignment.procedure_checkpoint.definition.record_id},
                    *({"object_type":kind,"object_id":report[field]} for kind,field in
                        (("incident","incident_id"),("postmortem","postmortem_id")) if report[field])],
                relationships=([{"target_knowledge_id":report["postmortem_knowledge"]["id"],
                    "relationship_type":"derived_from"}] if report["postmortem_knowledge"] else []),
                provenance=KnowledgeProvenance(source_kind="evidence", source_ref=assignment.execution_id,
                    source_revision=str(assignment.procedure_checkpoint.revision), evidence_ids=evidence_ids,
                    source_governance_record_ids=sources, origin="secondary_research"),
                review_after=report["review_due_at"], valid_until=report["expires_at"])

        def verified_winner(item):
            current = self.memory.get(item.id, actor=actor)
            # Logical-key ownership is not evidence. Revalidate the deterministic
            # report and exact source attribution inside the shared transaction.
            if (current.version != 1 or current.content != payload.content
                or current.project_id != payload.project_id
                or current.object_type != payload.object_type
                or current.canonical_refs != payload.canonical_refs
                or current.provenance.source_kind != payload.provenance.source_kind
                or current.provenance.source_ref != payload.provenance.source_ref
                or current.provenance.source_revision != payload.provenance.source_revision
                or current.provenance.evidence_ids != payload.provenance.evidence_ids
                or not set(sources).issubset(current.provenance.source_governance_record_ids)
                or current.review_after != payload.review_after
                or current.valid_until != payload.valid_until):
                raise SkillConflict("procedure outcome Memory source changed; review required")
            return current

        if existing:
            return verified_winner(max(existing, key=lambda r: r.version))
        try:
            return self.memory.create(payload, actor=actor)
        except KnowledgeConflictError:
            # The existing Memory logical-key transaction is the process-safe
            # reservation. A racing delivery reads that winner; never revises it.
            records = self.memory.store.list(organization_id=actor.organization_id, workspace_id=actor.workspace_id)
            item = next(r for r in records if r.logical_key == key)
            return verified_winner(item)

    def inspect(self, assignment_id, *, actor, incident_id=None, run_id=None):
        assignment, prior, report, _, _ = self.extract(assignment_id, actor=actor, incident_id=incident_id, run_id=run_id)
        candidates = []
        governed = {r.object_id:r for r in self.memory.governance.store.load().records
            if r.object_type == "procedure_skill" and (r.organization_id, r.workspace_id)
            == (actor.organization_id, actor.workspace_id)}
        for record in self.skills.definitions.list_records(kind="agent.skill"):
            provenance = record.payload.get("provenance", {})
            if (record.record_id not in governed or provenance.get("source_type") != "procedure_outcome"
                or provenance.get("source_id") != assignment_id
                or record.derived_from_record_id not in {None, prior.record_id}):
                continue
            source_knowledge = self.memory.get(provenance["source_ref"], actor=actor)
            if source_knowledge.logical_key != "procedure-outcome/" + self._identity(assignment):
                continue
            self.skills._assert_visible_record(record, actor)
            history = [r for r in self.skills._records(record.definition_id, actor=actor)
                if r.scope_type == record.scope_type and r.scope_id == record.scope_id]
            latest = max(history, key=lambda r:r.revision)
            candidates.append({"draft":self.skills._view(record, actor=actor),
                "latest":self.skills._view(latest, actor=actor),
                "knowledge_id":provenance["source_ref"],
                "governance":governed[record.record_id].model_dump(mode="json"),
                "newer_revision":latest.record_id != record.record_id,
                "diff":"".join(difflib.unified_diff(prior.payload["instructions"].splitlines(True),
                    latest.payload["instructions"].splitlines(True),
                    fromfile=prior.record_id, tofile=latest.record_id))})
        return {"report":report, "drafts":candidates,
            "publication_required":not candidates or any(
                row["latest"]["definitionLifecycle"] not in {"published", "superseded"} for row in candidates)}

    def convert(self, assignment_id, *, actor, incident_id=None, skill_id=None, run_id=None):
        definitions = self.skills.definitions
        governance = self.memory.governance
        engines = (definitions, governance, self.memory, self.runner.evidence)
        shared = definitions.store.store
        if any(engine.store.store is not shared for engine in engines):
            raise SkillConflict("outcome derivation requires one canonical transactional store")
        defaults = {engine.store.namespace: None for engine in engines}
        result = []
        def apply(documents):
            view = _TransactionDocuments(documents)
            def staged(engine):
                clone = copy(engine)
                clone.store = copy(engine.store)
                clone.store.store = view
                return clone
            local = copy(self)
            local.memory = staged(self.memory)
            local.memory.governance = staged(governance)
            local.memory._index_upsert = lambda *args, **kwargs: None
            local.skills = copy(self.skills)
            local.skills.definitions = staged(definitions)
            local.skills.definitions.notifier = None
            local.runner = copy(self.runner)
            local.runner.skills = local.skills
            local.runner.evidence = staged(self.runner.evidence)
            local.memory.artifact_evidence = local.runner.evidence
            # Re-extract under the cross-domain writer lock, including current
            # proof state, Memory and governance, before any visible commit.
            result.append(local._convert(assignment_id, actor=actor,
                incident_id=incident_id, skill_id=skill_id, run_id=run_id))
            return documents
        shared.update_many(defaults, apply)
        response = result[0]
        if response.get("knowledge_id"):
            # Derived retrieval is rebuilt on demand after the authoritative
            # commit; an index failure must not turn committed success into 500.
            self.memory._retrieval_index_dirty = True
        return response

    def _convert(self, assignment_id, *, actor, incident_id=None, skill_id=None, run_id=None):
        assignment, prior, report, proofs, sources = self.extract(assignment_id, actor=actor, incident_id=incident_id, run_id=run_id)
        self.skills._require_mutation(actor, owner_identity_id=prior.payload["owner_identity_id"])
        self.memory._require_write(actor, project_id=assignment.project_id)
        if report["review_needs"]:
            return {"status":"review_required", "draft":None, "report":report}
        knowledge = self._knowledge(assignment, report, proofs, sources, actor)
        if self.memory._freshness(knowledge).value != "fresh":
            return {"status":"review_required", "draft":None, "report":report,
                "review_needs":["source_memory_stale"]}
        # Retry uses the first canonical report; later incident/postmortem links
        # are inspectable from canonical Incident Evidence, never silent edits.
        report = json.loads(knowledge.content)
        body = "Review verified observations and hypotheses before publication.\n" + json.dumps(report, sort_keys=True)
        skill = SkillDefinition.model_validate(prior.payload)
        skill = SkillDefinition.model_validate({**skill.model_dump(mode="json"), "instructions":body,
            "provenance":SkillProvenance(source_type="procedure_outcome", source_ref=knowledge.id,
                source_id=assignment.id, source_revision=str(knowledge.version),
                origin="imported",
                upstream_id=prior.record_id, upstream_digest=prior.checksum, evidence_ids=proofs).model_dump(mode="json")})
        target = skill_id or prior.definition_id
        draft = self.skills.promote_verified(SkillPromotionRequest(skill_id=target, name=skill.name,
            procedure=body, source_execution_id=assignment.execution_id, evidence_ids=proofs), actor=actor,
            canonical_skill=skill, prior_record=prior if target == prior.definition_id else None,
            idempotency_key="procedure-outcome:" + self._identity(assignment))
        governance = self.memory.governance
        state = governance.store.load()
        dependencies = {knowledge.governance_record_id}
        while True:
            more = dependencies | {source for row in state.records if row.id in dependencies
                for source in row.source_record_ids}
            if more == dependencies:
                break
            dependencies = more
        if (any(governance._record_from_state(state, ref, actor).lifecycle != "active" for ref in dependencies)
            or any(request.record_id in dependencies and request.status in {"pending", "blocked"}
                for request in state.requests)):
            raise SkillConflict("source governance changed or erasure is pending")
        existing = [r for r in state.records if r.object_type == "procedure_skill" and r.object_id == draft["recordId"]]
        if not existing:
            governance.register_domain_record(GovernedDataCreate(project_id=assignment.project_id,
                object_type="procedure_skill", object_id=draft["recordId"], category=DataCategory.DEFINITION,
                source_record_ids=(knowledge.governance_record_id,), reason="verified outcome draft derivation"), actor=actor)
        elif len(existing) != 1 or existing[0].source_record_ids != (knowledge.governance_record_id,):
            raise SkillConflict("derived Skill governance conflicts with its exact source")
        published = draft["publishedAt"] is not None
        return {"status":"published" if published else "draft", "draft":draft, "knowledge_id":knowledge.id, "report":report,
            "diff":"".join(difflib.unified_diff(prior.payload["instructions"].splitlines(True),
                body.splitlines(True), fromfile=prior.record_id, tofile=draft["recordId"])),
            "publication_required":not published, "assignment_changed":False}

    def assert_source(self, record, *, candidate=None, actor=None):
        source = record.payload["provenance"]
        try:
            assignment = self.runner.workers.store.assignment(source["source_id"])
            if assignment is None:
                raise SkillConflict("source assignment unavailable")
            source_actor = self.runner._actor(assignment)
            actor = actor or source_actor
            knowledge = self.memory.get(source["source_ref"], actor=actor)
            permitted = self.memory.governance.filter_context(ContextFilterRequest(
                record_ids=(knowledge.governance_record_id,)), actor=actor)
            if knowledge.governance_record_id not in permitted.allowed_record_ids:
                raise SkillConflict("source governance forbids procedure context use")
            governance = self.memory.governance.store.load()
            dependencies = {knowledge.governance_record_id, *knowledge.provenance.source_governance_record_ids}
            if any(r.record_id in dependencies and r.status in {"pending", "blocked"} for r in governance.requests):
                raise SkillConflict("source erasure is pending")
            if self.memory._freshness(knowledge).value != "fresh":
                raise SkillConflict("outcome source requires freshness review")
            source_report = json.loads(knowledge.content)
            _, _, report, _, _ = self.extract(assignment.id, actor=source_actor,
                run_id=source_report.get("run_id", "legacy"))
            if report["review_needs"]:
                raise SkillConflict("outcome evidence requires review")
            if candidate is not None:
                history = [r for r in self.skills.definitions.list_records(kind=candidate.kind,
                    definition_id=candidate.definition_id) if r.scope_type == candidate.scope_type
                    and r.scope_id == candidate.scope_id]
                if max(history, key=lambda r:r.revision).record_id != candidate.record_id:
                    raise SkillConflict("newer local revision requires conflict review")
                published = max((r for r in history if r.published_at is not None),
                    key=lambda r:r.revision, default=None)
                allowed = {candidate.record_id, source["upstream_id"]}
                by_id = {r.record_id:r for r in history}
                ancestor = candidate.derived_from_record_id
                while ancestor and ancestor not in allowed:
                    allowed.add(ancestor)
                    ancestor = by_id[ancestor].derived_from_record_id if ancestor in by_id else None
                if published is not None and published.record_id not in allowed:
                    raise SkillConflict("newer publication requires conflict review")
        except (RuntimeError, ValueError, LookupError) as exc:
            raise SkillConflict("outcome source requires review or is unavailable") from exc

    def erase(self, governed, action):
        # Governance is the explicit exception to immutable payload retention.
        # Erase ALL descendant revisions, even locally edited or published ones.
        def apply(records):
            affected = {governed.object_id}
            while True:
                more = affected | {r.record_id for r in records if r.derived_from_record_id in affected}
                if more == affected:
                    break
                affected = more
            result = []
            for r in records:
                if r.record_id in affected:
                    payload = SkillDefinition(name="Removed by source governance",
                        instructions="[removed by source governance]", lifecycle="archived",
                        provenance=SkillProvenance(source_type="governance_tombstone"),
                        owner_identity_id=r.payload["owner_identity_id"]).model_dump(mode="json")
                    r = r.model_copy(update={"payload":payload, "lifecycle":DefinitionLifecycle.QUARANTINED,
                        "checksum":definition_checksum(definition_id=r.definition_id, kind=r.kind,
                            definition_schema_version=r.definition_schema_version, payload=payload)})
                result.append(r)
            return result
        self.skills.definitions.store.update(apply)
        self.skills.definitions._cache.clear()
        return f"procedure-skill:{governed.object_id}:{action.value}:erased"
