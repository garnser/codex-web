"""Finite cleanup of only the broker handlers owned by one test fixture."""
import asyncio


async def stop_owned_broker(broker, *, timeout=8):
    handlers = tuple(broker._handler_tasks)
    await broker.stop()
    if not handlers:
        return
    done, pending = await asyncio.wait(handlers, timeout=timeout)
    # Never cancel other work or treat a client timeout as permission to replay.
    # A failed cleanup is a failed test, not a positively joined handler.
    if pending:
        raise AssertionError('owned coordination broker handlers did not settle')
    results = await asyncio.gather(*done, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise AssertionError(
                'owned coordination broker handler failed: ' + type(result).__name__
            ) from result
