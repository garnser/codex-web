"""Disposable runner lifecycle composition, never the host application."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import sys

os.umask(0o077)
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from tests.runner_failure_fixture import RunnerFailureFixture

if 'codex_web.application' in sys.modules or 'server' in sys.modules:
    raise RuntimeError('isolated fixture must not import the application')


async def main(listener):
    fixture = await RunnerFailureFixture().start()
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await fixture.close()
            print(json.dumps({'fixture_terminal': True, 'process_reaped': fixture.process.returncode is not None,
                'executor_work_remaining': len(fixture.session._executor_work)}), flush=True)
    fixture.app.router.lifespan_context = lifespan
    fixture.app.mount('/static', StaticFiles(directory=ROOT/'static'), name='static')
    @fixture.app.get('/projects/home/threads', response_class=HTMLResponse)
    async def page():
        return (ROOT/'tests/browser/runner_failure_fixture.html').read_text()
    @fixture.app.get('/fixture/metadata')
    async def metadata():
        return {'assignment_id': fixture.session.assignment_id, 'fence': fixture.session.fence}
    @fixture.app.get('/fixture/history')
    async def history():
        return fixture.service.thread_history.thread('failure-thread')
    @fixture.app.post('/fixture/fail')
    async def fail():
        await fixture.fail()
        return {'process_reaped': fixture.process.returncode is not None,
            'executor_work_remaining': len(fixture.session._executor_work),
            'source_retained': all((fixture.workspace_path/name).read_text() == content for name, content in fixture.files.items()),
            'git_status_retained': fixture.git_status() == fixture.git_status_before}
    @fixture.app.post('/fixture/expire')
    async def expire():
        assignment = fixture.worker_service.store.assignment(fixture.session.assignment_id)
        fixture.worker_service.recover_expired(actor=fixture.admin, now=assignment.lease.expires_at+1)
        return {'recovered': True}
    print(json.dumps({'port': listener.getsockname()[1]}), flush=True)
    await uvicorn.Server(uvicorn.Config(fixture.app, log_level='warning', access_log=False)).serve(sockets=[listener])


if __name__ == '__main__':
    if (ROOT/'data').is_symlink():
        raise SystemExit('Refusing runner fixture through shared data symlink')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        asyncio.run(main(listener))
