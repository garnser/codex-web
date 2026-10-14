"""Curated UX content over canonical Memory, source and governance services."""
from __future__ import annotations

import hashlib
import json

from codex_web.data_governance import ContextFilterRequest
from codex_web.organizational_memory import (
    KnowledgeCanonicalRef, KnowledgeContextSelection, KnowledgeCreate,
    KnowledgeFreshness, KnowledgeQuery, KnowledgeRelationshipCreate,
    KnowledgeRelationshipType, KnowledgeRetrievalBudget, KnowledgeRevise,
)
from codex_web.services.organizational_memory import KnowledgeValidationError
from codex_web.ux_briefs import UX_BRIEF_TAG, UxBrief, UxBriefSource, UxBriefSelection


class UxBriefService:
    def __init__(self, memory, artifact_evidence=None):
        self.memory = memory
        self.artifact_evidence = artifact_evidence

    def _source(self, kind, record_id, project_id, actor):
        artifacts = self.artifact_evidence or self.memory.artifact_evidence
        if kind == "knowledge":
            record = self.memory.get(record_id, actor=actor, include_inactive=True)
        elif artifacts is not None and kind == "artifact":
            record = artifacts.get_artifact(record_id, actor=actor)
        elif artifacts is not None and kind == "evidence":
            record = artifacts.get_evidence(record_id, actor=actor)
        else:
            raise KnowledgeValidationError("canonical source service unavailable")
        if record.project_id not in {None, project_id}:
            raise KnowledgeValidationError("source is outside the selected Project")
        if not record.governance_record_id:
            raise KnowledgeValidationError("source governance record unavailable")
        return record

    @staticmethod
    def _pin(kind, record):
        if kind == "knowledge":
            return UxBriefSource(kind=kind, record_id=record.id, revision=str(record.version), sha256=record.content_sha256)
        # Artifact revision names alone cannot detect metadata/lifecycle changes.
        # Evidence has no mutable integer revision; retain its exact record hash.
        digest = hashlib.sha256(json.dumps(record.model_dump(mode="json"), sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        return UxBriefSource(kind=kind, record_id=record.id,
            revision=getattr(record, "revision", None) or "record:"+digest, sha256=digest)

    def pin_source(self, kind, record_id, *, project_id, actor):
        record = self._source(kind, record_id, project_id, actor)
        return self._pin(kind, record)

    def _resolve(self, brief, *, project_id, actor):
        pins = tuple(dict.fromkeys(source for section in brief.sections.values() for source in section.sources))
        records = []
        for pin in pins:
            record = self._source(pin.kind, pin.record_id, project_id, actor)
            if self._pin(pin.kind, record) != pin:
                raise KnowledgeValidationError("supporting source revision changed; inspect and repin it")
            records.append((pin, record))
        return records

    def _prepared(self, payload, *, project_id, actor):
        if project_id is None or payload.project_id != project_id:
            raise KnowledgeValidationError("brief must belong to the selected Project")
        if payload.object_type != "ux_context_brief":
            raise KnowledgeValidationError("UX briefs require the canonical UX brief Knowledge type")
        brief = UxBrief.model_validate_json(payload.content)
        sources = self._resolve(brief, project_id=project_id, actor=actor)
        governance_ids = tuple(dict.fromkeys((*payload.provenance.source_governance_record_ids,
            *(record.governance_record_id for _, record in sources))))
        evidence_ids = tuple(dict.fromkeys((*payload.provenance.evidence_ids,
            *(record.id for pin, record in sources if pin.kind == "evidence"))))
        relationships = list(payload.relationships)
        refs = list(payload.canonical_refs)
        for pin, record in sources:
            refs.append(KnowledgeCanonicalRef(object_type=pin.kind, object_id=record.id))
            if pin.kind == "knowledge":
                relationships.append(KnowledgeRelationshipCreate(target_knowledge_id=record.id,
                    relationship_type=KnowledgeRelationshipType.DERIVED_FROM))
        return payload.model_copy(update={
            "content": brief.model_dump_json(),
            "tags": tuple(dict.fromkeys((*payload.tags, UX_BRIEF_TAG))),
            "canonical_refs": tuple(dict.fromkeys(refs)),
            "relationships": tuple(dict.fromkeys(relationships)),
            "provenance": payload.provenance.model_copy(update={
                "source_governance_record_ids": governance_ids, "evidence_ids": evidence_ids}),
        })

    def create(self, payload: KnowledgeCreate, *, project_id, actor):
        if payload.project_id != project_id:
            raise KnowledgeValidationError("brief must belong to the selected Project")
        return self.memory.create(payload, actor=actor)

    def revise(self, knowledge_id, payload: KnowledgeRevise, *, project_id, actor):
        current = self.memory.get(knowledge_id, actor=actor, include_inactive=True)
        if current.project_id != project_id or current.object_type != "ux_context_brief":
            raise KnowledgeValidationError("brief unavailable in the selected Project")
        return self.memory.revise(knowledge_id, payload, actor=actor)

    def preview(self, selection: UxBriefSelection, *, project_id, actor):
        record = self.memory.get(selection.knowledge_id, actor=actor, include_inactive=True)
        if record.project_id != project_id or record.object_type != "ux_context_brief":
            raise KnowledgeValidationError("brief unavailable in the selected Project")
        if (record.version, record.content_sha256) != (selection.version, selection.sha256):
            raise KnowledgeValidationError("selected brief revision does not match canonical Memory")
        freshness = self.memory.freshness(record)
        omitted = []
        sections = []
        source_pins = []
        blocks = []
        source_details = {}
        headline = f"UX brief [memory:{record.id}@v{record.version}; sha256:{record.content_sha256}]"
        header = self.memory.context_header(record, include_source_lineage=False)
        allowance = min(2400, selection.max_context_tokens)
        brief = UxBrief.model_validate_json(record.content) if record.content else None
        for name in selection.sections:
            if freshness != KnowledgeFreshness.FRESH:
                omitted.append({"section":name.value,"reason":"brief_"+freshness.value})
                continue
            section = brief.sections.get(name) if brief is not None else None
            if section is None or not section.text:
                omitted.append({"section":name.value,"reason":"section_empty_or_missing"})
                continue
            denied = None
            for pin in section.sources:
                try:
                    source = self._source(pin.kind, pin.record_id, project_id, actor)
                    if self._pin(pin.kind, source) != pin:
                        denied = "source_revision_changed"
                        break
                    source_fresh = (self.memory.freshness(source) == KnowledgeFreshness.FRESH
                        if pin.kind == "knowledge" else source.lifecycle.value in {"active", "valid"})
                    deadline = getattr(source,"retention_expires_at",None)
                    if not source_fresh or (deadline is not None and self.memory.clock() >= deadline):
                        denied = "source_unavailable_or_stale"
                        break
                    filtered = self.memory.governance.filter_context(ContextFilterRequest(
                        record_ids=(source.governance_record_id,)), actor=actor)
                    if source.governance_record_id not in filtered.allowed_record_ids:
                        denied = "source_model_context_denied"
                        break
                    provenance = getattr(source,"provenance",None)
                    source_details[pin] = {"review_after":getattr(source,"review_after",None),
                        "valid_until":getattr(source,"valid_until",None),
                        "review_status":getattr(provenance,"review_status","unreviewed"),
                        "origin":str(getattr(provenance,"origin",getattr(source,"research_origin","unknown"))),
                        "result":str(getattr(source,"result","not applicable"))}
                except (RuntimeError, ValueError, KeyError):
                    # Do not reveal foreign source content, provider errors or secrets.
                    denied = "source_unavailable"
                    break
            if denied:
                omitted.append({"section":name.value,"reason":denied})
                continue
            lines = [name.value.replace("_"," ").title()+"\n"+section.text]
            for pin in section.sources:
                detail = source_details[pin]
                lines.append(f"Source [{pin.kind}:{pin.record_id}@{pin.revision}; sha256:{pin.sha256}] "
                    f"origin={detail['origin']}; review={detail['review_status']}; "
                    f"review_after={detail['review_after']}; valid_until={detail['valid_until']}; result={detail['result']}")
            block = "\n".join(lines)
            candidate = header + "\n\n".join([headline,*blocks,block])
            if self.memory._token_estimate(candidate) > allowance:
                omitted.append({"section":name.value,"reason":"section_exceeds_remaining_context_budget"})
                continue
            blocks.append(block)
            sections.append((name,section))
            source_pins.extend(section.sources)
        pins = tuple(dict.fromkeys(source_pins))
        attribution = KnowledgeContextSelection.model_validate({**selection.model_dump(),
            "sources":[pin.model_dump() for pin in pins]})
        def project(item):
            return "\n\n".join([headline,*blocks]) if sections else ""
        result = self.memory.search(KnowledgeQuery(knowledge_ids=(record.id,), project_ids=(project_id,),
            budget=KnowledgeRetrievalBudget(top_k=1,max_context_tokens=selection.max_context_tokens)),
            actor=actor,context_projection=project,context_selection=attribution)
        text = result.items[0].context_excerpt if result.items and sections else ""
        if sections and not text:
            omitted.extend({"section":name.value,"reason":"brief_model_context_denied"} for name,_ in sections)
        return {"context":text,"retrieval_id":result.retrieval_id,"selection":attribution.model_dump(mode="json"),
            "omitted":omitted,"source_metadata":[{"source":pin.model_dump(mode="json"),**source_details[pin]} for pin in pins],
            "packed_tokens":result.packed_tokens,"model_calls":0,
            "freshness":freshness.value,"delivery_proven":False}
