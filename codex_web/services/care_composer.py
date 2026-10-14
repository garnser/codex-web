import json
import uuid
from codex_web.care_composer import CareFields, CareComposePlugin
from codex_web.input_plugins import InputEnvelope, InputPluginPipeline, InputPluginRegistration, InputPhase


class CareComposerService:
    def __init__(self, skills):self.skills=skills

    def example(self, reference, actor):
        self.skills.validate_reference(reference,actor=actor)
        record=self.skills.definitions.get_record(reference.record_id)
        assets=record.payload.get("assets",[])
        asset=next((item for item in assets if item["path"]=="care.json" and item["kind"]=="template"),None)
        if asset is None:raise ValueError("selected Skill does not provide a CARE example")
        return CareFields.model_validate(json.loads(asset["content"]))

    async def preview(self, request, actor):
        if request.template_ref:self.example(request.template_ref,actor)
        pipeline=InputPluginPipeline([InputPluginRegistration(plugin=CareComposePlugin(request.fields),
            phase=InputPhase.COMPOSE,definition_ref=request.template_ref,
            max_added_characters=16000,max_patch_bytes=128*1024)])
        result=await pipeline.execute(InputEnvelope(request_id="care-preview-"+uuid.uuid4().hex,
            organization_id=actor.organization_id,workspace_id=actor.workspace_id,actor_id=actor.identity_id,
            model_class="composer-preview",purpose="care.preview"))
        return {"message":result.envelope.messages[0].content,"suggestions":request.fields.suggestions(),
            "template_ref":request.template_ref.model_dump(mode="json") if request.template_ref else None,
            "references":list(request.fields.references),
            "composition_provenance":[row.model_dump(mode="json") for row in result.provenance],
            "max_characters":16000,"model_calls":0}
