from fastapi import APIRouter, HTTPException, Request
from codex_web.api.identity import request_actor
from codex_web.care_composer import CarePreviewRequest
from codex_web.definitions import DefinitionReference
from codex_web.services.projects import ProjectNotFoundError
from codex_web.services.skills import SkillError
from codex_web.services.skill_security import SkillSecurityBlocked


def build_care_composer_router(service, projects):
    router=APIRouter(prefix="/api/projects/{project_id}/care",tags=["input-composition"])

    def actor_for(project_id,request):
        try:projects.get(project_id,request.state.tenant_scope)
        except ProjectNotFoundError as error:raise HTTPException(status_code=404,detail="Project unavailable") from error
        return request_actor(request)

    @router.get("/examples")
    async def examples(project_id:str,request:Request):
        actor=actor_for(project_id,request)
        result=[]
        for view in service.skills.list(actor=actor,category="task-composer",include_drafts=False):
            reference=DefinitionReference.model_validate(view["definitionReference"])
            try:
                fields=service.example(reference,actor)
                result.append({"name":view["skill"]["name"],"reference":reference.model_dump(mode="json"),
                    "fields":fields.model_dump(mode="json"),"allowed":True})
            except (SkillError,SkillSecurityBlocked,ValueError):
                result.append({"name":view["skill"]["name"],"reference":reference.model_dump(mode="json"),
                    "allowed":False,"reason":"Example unavailable under current Skill publication/security controls"})
        return {"items":result,"max_characters":16000,"model_calls":0}

    @router.post("/preview")
    async def preview(project_id:str,payload:CarePreviewRequest,request:Request):
        actor=actor_for(project_id,request)
        try:return await service.preview(payload,actor)
        except SkillSecurityBlocked as error:raise HTTPException(status_code=403,detail="Skill security policy denies this example") from error
        except (SkillError,ValueError) as error:raise HTTPException(status_code=422,detail=str(error)) from error

    return router
