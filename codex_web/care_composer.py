"""Bounded optional task guidance; never an authority or execution contract."""
import json
from pydantic import BaseModel, ConfigDict, Field, model_validator
from codex_web.definitions import DefinitionReference, DefinitionDraftCreate
from codex_web.skills import SkillDefinition, SKILL_DEFINITION_KIND, SKILL_DEFINITION_SCHEMA_VERSION
from codex_web.input_plugins import InputPatch, InputPhase

CARE_MAX_CHARS = 16000


class CareFields(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    context: str = Field(default="", max_length=4000)
    action: str = Field(default="", max_length=4000)
    rules: str = Field(default="", max_length=4000)
    examples: str = Field(default="", max_length=4000)
    references: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def bounded(self):
        if any(len(ref)>512 for ref in self.references):
            raise ValueError("each reference must be at most 512 characters")
        if len(self.compose())>CARE_MAX_CHARS:
            raise ValueError("composed request exceeds 16000 characters")
        return self

    def compose(self):
        sections = [("Context",self.context),("Action / desired output",self.action),
                    ("Rules / constraints (task guidance)",self.rules),("Examples",self.examples)]
        text="\n\n".join(title+"\n"+value for title,value in sections if value)
        if self.references:text+="\n\nReferences\n"+"\n".join(self.references)
        return text.strip()

    def suggestions(self):
        return [hint for field,hint in ((self.context,"Add relevant context if the task needs it."),
            (self.action,"Name the outcome or output format you want."),
            (self.rules,"Mention important constraints if any."),
            (self.examples,"Add an example or reference when helpful.")) if not field]


class CarePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    fields: CareFields
    template_ref: DefinitionReference | None = None


class CareComposePlugin:
    id="care.compose"
    version="1.0"
    transport="local"

    def __init__(self, fields):self.fields=fields

    async def transform(self,envelope,context):
        return InputPatch(plugin_id=self.id,plugin_version=self.version,phase=InputPhase.COMPOSE,
            changes={"messages":[{"role":"user","content":self.fields.compose()}]})


def bootstrap_care_examples(registry):
    examples = (
        ("care.investigate-defect","Investigate a defect",CareFields(
            context="A reported defect affects a specific user workflow. Include the relevant repository and reproduction details.",
            action="Find the cause, propose a focused fix, and state how to verify it.",
            rules="Preserve existing behavior outside the defect. Use evidence and identify uncertainty.",
            examples="Expected result: a concise diagnosis, affected files, and validation steps.")),
        ("care.critique-ui","Critique a proposed UI change",CareFields(
            context="A proposed interface change should support its intended users and task. Include the relevant design or screenshot reference.",
            action="Critique the proposal and prioritize concrete improvements.",
            rules="Consider keyboard access, mobile use, clarity, loading and error states. Distinguish observations from assumptions.",
            examples="Expected result: prioritized findings with rationale and suggested changes.")),
    )
    return registry.bootstrap([DefinitionDraftCreate(definition_id=skill_id,
        kind=SKILL_DEFINITION_KIND,definition_schema_version=SKILL_DEFINITION_SCHEMA_VERSION,
        actor="bootstrap",reason="Initial optional CARE task example; preserve published operator revisions",
        payload=SkillDefinition(name=name,description="Optional task-composer example; user-editable guidance.",
            instructions="Use the template as an editable user request. It grants no authority or execution access.",
            categories=("task-composer",),owner_identity_id="bootstrap",
            assets=({"path":"care.json","kind":"template","media_type":"application/json",
                "content":json.dumps(fields.model_dump(mode="json")),"context_mode":"never",
                "security_class":"template"},)).model_dump(mode="json")) for skill_id,name,fields in examples])
