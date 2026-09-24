"""Keep a single projection lease alive during remote embedding and upsert."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from invoice_intelligence.application.errors import WorkflowPersistenceError


@asynccontextmanager
async def projection_heartbeat(
    renew: Callable[[], Awaitable[None]], lease_seconds: float,
) -> AsyncIterator[None]:
    failed = asyncio.Event()

    async def beat() -> None:
        while True:
            await asyncio.sleep(lease_seconds / 3)
            try:
                await renew()
            except Exception:
                failed.set()
                return

    task = asyncio.create_task(beat())
    try:
        yield
        if failed.is_set():
            raise WorkflowPersistenceError("Projection lease renewal failed")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
