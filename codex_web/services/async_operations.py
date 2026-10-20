"""Settle one existing blocking operation before propagating cancellation.

No retries, executor ownership, durable work or authority are introduced here.
"""
import asyncio
import contextvars
from anyio import CancelScope


async def settle_future(future, on_cancel=None):
    # One non-failing completion waiter per observer. Wrapping the worker
    # future on each cancellation adds callbacks/proxies whose exceptions
    # can be logged even though the actual thread remains correctly owned.
    loop = asyncio.get_running_loop()
    completion = loop.create_future()
    def notify():
        if not completion.done():
            completion.set_result(None)
    future.add_done_callback(lambda _: loop.call_soon_threadsafe(notify))
    cancelled = False
    try:
        await asyncio.shield(completion)
    except asyncio.CancelledError:
        cancelled = True
        if on_cancel is not None:
            on_cancel()
        # ASGI task groups use level cancellation. Shield its cancelled
        # scope while joining so checkpoints do not spin; explicit repeated
        # asyncio.Task.cancel() still cannot abandon the actual thread.
        with CancelScope(shield=True):
            while not completion.done():
                try:
                    await asyncio.shield(completion)
                except asyncio.CancelledError:
                    cancelled = True
    # Retrieve a terminal exception even when the observing task was
    # cancelled. Neither a cancelled wrapper nor a lost observer stops work.
    error = future.exception()
    if cancelled:
        raise asyncio.CancelledError
    if error is not None:
        raise error
    return future.result()


async def run_settled(operation, *, on_cancel=None):
    context = contextvars.copy_context()
    future = asyncio.get_running_loop().run_in_executor(None, context.run, operation)
    return await settle_future(future, on_cancel=on_cancel)
