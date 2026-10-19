"""Disposable HTTP composition of the real playbook services for Chromium.

Never imports the application, opens deployment data, contacts a business service,
or supplies browser-owned lifecycle/discovery/execution state. The controlled
provider and worker are shared with the canonical SQLite execution qualification.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from codex_web.api.definitions import build_definitions_router
from codex_web.api.execution_workers import build_execution_workers_router
from codex_web.api.action_intents import build_action_intents_router
from codex_web.api.procedure_discovery import build_procedure_discovery_router
from codex_web.api.procedure_execution import build_procedure_execution_router
from codex_web.api.procedure_outcomes import build_procedure_outcomes_router
from codex_web.api.skill_security import build_skill_security_router
from codex_web.api.skills import build_skills_router
from codex_web.api.resources import build_resources_router
from codex_web.services.identity import TenantIsolationError
from codex_web.identity import MembershipRole
from codex_web.skill_security import SkillScanRequest, SkillSecurityPolicyUpdate
from codex_web.skills import SkillCreate, SkillPublish
from tests.test_procedure_outcomes import FullProcedureOutcomeTests
from tests.test_skills import actor as fixture_actor

if "codex_web.application" in sys.modules or "server" in sys.modules:
    raise RuntimeError("The isolated playbook fixture must not import the application")


async def make_app():
    fixture = FullProcedureOutcomeTests(methodName="runTest")
    if "--approval" in sys.argv:
        fixture._testMethodName = "test_awaiting_approval_browser"
    elif "--deviation" in sys.argv:
        fixture._testMethodName = "test_deviation_browser"
    await fixture.asyncSetUp()
    outcomes = fixture.outcomes()
    fixture.skills.security.update_policy(SkillSecurityPolicyUpdate(
        scan_locally_authored=True, max_risk_score=37,
        reason="Chromium exact-revision scan qualification"), actor=fixture.actor)
    fixture.skills.security.scan(fixture.skills.get(fixture.pin.definition_id, actor=fixture.actor),
        SkillScanRequest(), actor=fixture.actor)
    production_contract = fixture.skills.get(fixture.pin.definition_id, actor=fixture.actor)["skill"]["operational_procedure"]
    production_contract = json.loads(json.dumps(production_contract))
    production_contract["environments"] = ["production"]
    production_candidate = fixture.skills.create(SkillCreate(skill_id="production-only", name="Production only",
        instructions="Follow canonical policy; this description grants no authority.",
        operational_procedure=production_contract), actor=fixture.actor)
    fixture.skills.security.scan(production_candidate, SkillScanRequest(), actor=fixture.actor)
    fixture.skills.publish(production_candidate["skillId"], production_candidate["recordId"], SkillPublish(), actor=fixture.actor)

    @asynccontextmanager
    async def lifespan(app):
        yield
        fixture.tearDown()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def identity(request: Request, call_next):
        mode = request.cookies.get("fixture_tenant")
        if mode == "foreign":
            request.state.identity_actor = fixture_actor("foreign", workspace_id="workspace-foreign", admin=True)
        elif mode == "member":
            request.state.identity_actor = fixture.actor.model_copy(update={"identity_id": "limited-reader", "roles": (MembershipRole.MEMBER,)})
        elif mode in {None, "home"}:
            request.state.identity_actor = fixture.actor
        else:
            return JSONResponse({"detail": "Unknown synthetic tenant mode"}, status_code=422)
        return await call_next(request)

    class Projects:
        def get(self, project_id, *, scope):
            if scope != fixture.actor.tenant:
                raise TenantIsolationError("Fixture Project is outside this tenant")
            if project_id not in {"home", "other"}:
                raise HTTPException(404, "Project unavailable")
            return {"id": project_id}

    # Register the preview path before the dynamic /api/skills/{skill_id} route.
    app.include_router(build_procedure_discovery_router(fixture.discovery, fixture.execution, fixture.intents, Projects()))
    app.include_router(build_skills_router(fixture.skills))
    app.include_router(build_definitions_router(fixture.definition_registry))
    app.include_router(build_skill_security_router(fixture.skills.security, fixture.skills))
    app.include_router(build_procedure_execution_router(fixture.runner))
    app.include_router(build_procedure_outcomes_router(outcomes))
    app.include_router(build_execution_workers_router(fixture.workers))
    app.include_router(build_action_intents_router(fixture.intents))
    app.include_router(build_resources_router(fixture.resources, Projects()))
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/fixture/metadata")
    async def metadata():
        return {"skill_id": fixture.pin.definition_id, "pin": fixture.pin.model_dump(mode="json"),
            "assignment_id": fixture.assignment.id, "resource_id": fixture.resource.id,
            "binding_id": fixture.binding.id, "actor_id": fixture.actor.identity_id,
            "project_id": "home", "task_kind": "gitlab.maintenance", "operation": "fixture.change"}

    @app.post("/fixture/tenant/{tenant}")
    async def tenant(tenant: str):
        # Never construct a cookie from route input, even in this isolated fixture.
        if tenant == "home":
            mode = "home"
        elif tenant == "foreign":
            mode = "foreign"
        elif tenant == "member":
            mode = "member"
        else:
            raise HTTPException(422, "Unknown synthetic tenant mode")
        response = JSONResponse({"tenant": mode})
        response.set_cookie("fixture_tenant", mode, httponly=True, samesite="strict")
        return response

    @app.post("/fixture/provider/{mode}")
    async def provider_mode(mode: str):
        if mode not in {"unknown", "failed", "normal"}:
            raise HTTPException(422, "Unknown controlled provider mode")
        fixture.provider.unknown = mode == "unknown"
        fixture.provider.fail = mode == "failed"
        return {"mode": mode}

    @app.get("/api/identity/me")
    async def me(request: Request):
        return request.state.identity_actor.model_dump(mode="json")

    @app.get("/api/agent-profiles")
    @app.get("/api/skill-sources")
    async def optional_empty_catalogs():
        return {"items": []}

    @app.get("/projects/{project_id}/skills", response_class=HTMLResponse)
    async def skills_page(project_id: str):
        if project_id not in {"home", "other"}:
            raise HTTPException(404)
        return f'''<!doctype html><html><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="stylesheet" href="/static/styles.css">
        <link rel="stylesheet" href="/static/skill_library_ui.css"></head>
        <body data-project-id="{project_id}"><details id="developer-panel" open>
        <div class="developer-grid"></div>
        <button id="refresh-execution-workers">Refresh workers</button>
        <div id="execution-worker-status"></div><div id="execution-worker-list"></div>
        <div id="execution-assignment-list"></div><div id="execution-worker-events"></div>
        </details><script type="module" src="/static/skill_library_ui.js"></script>
        <script src="/static/execution_worker_admin.js"></script></body></html>'''

    return app


if __name__ == "__main__":
    # Bind atomically rather than guessing a global test port. Each worker owns
    # its process, SQLite database, provider, readiness service and workspace.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        app = asyncio.run(make_app())
        print(json.dumps({"port": listener.getsockname()[1]}), flush=True)
        uvicorn.run(app, log_level="warning", access_log=False, fd=listener.fileno())
