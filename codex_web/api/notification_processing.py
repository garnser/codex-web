"""Thread-scoped explicit processing; request text cannot supply an outcome."""
import asyncio

from fastapi import Request

from codex_web.api.identity import request_actor
from codex_web.notification_processing import NotificationProcessingRequest


def attach_notification_processing(router, service):
    @router.get('/api/threads/{thread_id}/notification-batches/{batch_id}/processing')
    async def preview_processing(thread_id: str, batch_id: str, request: Request):
        return await asyncio.to_thread(service.preview, batch_id, thread_id=thread_id,
            project=request.state.thread_project, actor=request_actor(request))

    @router.post('/api/threads/{thread_id}/notification-batches/{batch_id}/processing')
    async def process_original(thread_id: str, batch_id: str,
                               payload: NotificationProcessingRequest, request: Request):
        return await asyncio.to_thread(service.process, batch_id, payload, thread_id=thread_id,
            project=request.state.thread_project, actor=request_actor(request))
