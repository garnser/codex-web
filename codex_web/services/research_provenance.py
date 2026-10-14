"""Canonical source lifecycle checks for derived Memory research provenance."""

from collections.abc import Mapping

from codex_web.organizational_memory import (KnowledgeFreshness, KnowledgeLifecycle, KnowledgeRecord,
    KnowledgeProvenance, KnowledgeOrigin, KnowledgeOriginSource, KnowledgeRelationshipType)
from codex_web.services.data_governance import GovernanceConflictError
from codex_web.artifact_evidence import EvidenceLifecycle, ArtifactLifecycle


def research_governance_plan(governance, root):
    """Existing governed source links determine payload impact and legal holds."""
    if governance is None:
        return ()
    records = [row for row in governance.store.load().records
               if (row.organization_id, row.workspace_id) == (root.organization_id, root.workspace_id)
               and row.object_type in {"organizational_memory", "artifact", "evidence"}]
    affected = {root.id}
    while True:
        expanded = affected | {row.id for row in records if affected.intersection(row.source_record_ids)}
        if expanded == affected:
            break
        affected = expanded
    plan = tuple(row for row in records if row.id in affected and row.id != root.id)
    if any(row.legal_hold_at is not None for row in plan):
        raise GovernanceConflictError("derived research payload is protected by a legal hold")
    return plan


def research_sources_readable(memory, item, actor, records=None):
    if not item.provenance.origin_sources:
        return True
    records = records if records is not None else {row.id: row for row in memory.store.load().records}
    for snapshot in item.provenance.origin_sources:
        source = records.get(snapshot.knowledge_id)
        if source is None or not memory._same_scope(source, actor):
            return False
        if not memory._can_read(actor, project_id=source.project_id, classification=source.classification)[0]:
            return False
        if source.required_role_ids and not set(source.required_role_ids).intersection(memory._role_ids(actor, project_id=source.project_id)):
            return False
    return True


def research_evidence_freshness(memory, item):
    if item.provenance.origin == KnowledgeOrigin.UNKNOWN or not item.provenance.evidence_ids:
        return None
    if memory.artifact_evidence is None:
        return KnowledgeFreshness.INVALID
    state = memory.artifact_evidence.store.load()
    evidence = {row.id: row for row in state.evidence}
    artifacts = {row.id: row for row in state.artifacts}
    for evidence_id in item.provenance.evidence_ids:
        proof = evidence.get(evidence_id)
        if proof is None or proof.lifecycle != EvidenceLifecycle.VALID or (
            proof.organization_id, proof.workspace_id
        ) != (item.organization_id, item.workspace_id) or proof.project_id not in {None, item.project_id}:
            return KnowledgeFreshness.INVALID
        if any(source_id not in artifacts or artifacts[source_id].lifecycle != ArtifactLifecycle.ACTIVE
               for source_id in proof.artifact_ids):
            return KnowledgeFreshness.INVALID
    return None


def derived_source_freshness(
    item: KnowledgeRecord,
    records: Mapping[str, KnowledgeRecord],
    *,
    now: float,
) -> KnowledgeFreshness | None:
    # A newer cited revision carries older attribution for reproducibility.
    # Inspect the newest explicitly cited version of each source logical key;
    # never substitute a newer uncited version from the store.
    sources: dict[str, KnowledgeRecord] = {}
    for snapshot in item.provenance.origin_sources:
        source = records.get(snapshot.knowledge_id)
        if source is None or source.version != snapshot.version or (
            source.organization_id, source.workspace_id
        ) != (item.organization_id, item.workspace_id):
            return KnowledgeFreshness.INVALID
        if source.logical_key == item.logical_key:
            continue  # Historical attribution within this record's own lineage.
        previous = sources.get(source.logical_key)
        if previous is None or source.version > previous.version:
            sources[source.logical_key] = source
    outcomes = set()
    for source in sources.values():
        if source.lifecycle in {KnowledgeLifecycle.INVALID, KnowledgeLifecycle.REDACTED, KnowledgeLifecycle.DELETED}:
            outcomes.add(KnowledgeFreshness.INVALID)
        elif source.lifecycle == KnowledgeLifecycle.SUPERSEDED:
            outcomes.add(KnowledgeFreshness.STALE)
        if any(deadline is not None and now >= deadline for deadline in (
            source.valid_until, source.retention_expires_at,
        )):
            outcomes.add(KnowledgeFreshness.EXPIRED)
        elif source.review_after is not None and now >= source.review_after:
            outcomes.add(KnowledgeFreshness.STALE)
    for outcome in (KnowledgeFreshness.INVALID, KnowledgeFreshness.EXPIRED, KnowledgeFreshness.STALE):
        if outcome in outcomes:
            return outcome
    return None


def derive_research_provenance(memory, provenance, relationships, *, actor, project_id, previous=None, invalid):
    sources = [
        memory.get(row.target_knowledge_id, actor=actor)
        for row in relationships
        if row.relationship_type == KnowledgeRelationshipType.DERIVED_FROM
    ]
    if previous is not None:
        sources.append(previous)
    snapshots: dict[str, KnowledgeOriginSource] = {}
    for source in sources:
        snapshots[source.id] = KnowledgeOriginSource(
            knowledge_id=source.id,
            version=source.version,
            origin=source.provenance.origin,
            source_ref=source.provenance.source_ref,
            source_revision=source.provenance.source_revision,
        )
        for snapshot in source.provenance.origin_sources:
            snapshots[snapshot.knowledge_id] = snapshot
    if any(snapshots.get(row.knowledge_id) != row for row in provenance.origin_sources):
        raise invalid("origin sources must match canonical derivation links")
    origin = provenance.origin
    origins = {source.provenance.origin for source in sources}
    if "origin" in provenance.model_fields_set:
        origins.add(origin)
    if origins:
        origin = next(iter(origins)) if len(origins) == 1 else KnowledgeOrigin.MIXED
    evidence_governance = []
    if origin != KnowledgeOrigin.UNKNOWN and provenance.evidence_ids:
        if memory.artifact_evidence is None:
            raise invalid("canonical research evidence is unavailable")
        for evidence_id in provenance.evidence_ids:
            try:
                proof = memory.artifact_evidence.get_evidence(evidence_id, actor=actor)
            except (KeyError, RuntimeError) as error:
                raise invalid("research evidence is unavailable in this scope") from error
            if proof.project_id not in {None, project_id} or proof.governance_record_id is None:
                raise invalid("research evidence lacks compatible Project/governance provenance")
            evidence_governance.append(proof.governance_record_id)
            for artifact_id in proof.artifact_ids:
                try:
                    artifact = memory.artifact_evidence.get_artifact(artifact_id, actor=actor)
                except (KeyError, RuntimeError) as error:
                    raise invalid("research artifact is unavailable in this scope") from error
                if artifact.project_id not in {None, project_id} or artifact.governance_record_id is None:
                    raise invalid("research artifact lacks compatible Project/governance provenance")
                evidence_governance.append(artifact.governance_record_id)
    return KnowledgeProvenance.model_validate({
        **provenance.model_dump(),
        "origin": origin,
        "origin_sources": tuple(snapshots.values()),
        "source_governance_record_ids": tuple(dict.fromkeys((
            *provenance.source_governance_record_ids,
            *evidence_governance,
            *(source.governance_record_id for source in sources),
        ))),
    })
