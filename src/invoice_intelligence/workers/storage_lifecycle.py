"""Standalone PostgreSQL-driven object lifecycle worker."""

import asyncio
import os
import signal
import socket

from invoice_intelligence.application.services.storage_lifecycle import StorageLifecycleService
from invoice_intelligence.bootstrap import build_container, close_application_container
from invoice_intelligence.config.logging import configure_logging
from invoice_intelligence.infrastructure.persistence.database import create_business_engine
from invoice_intelligence.infrastructure.persistence.sqlalchemy_stored_objects import (
    SQLAlchemyStoredObjectRepository,
)


async def run() -> None:
    container = build_container()
    configure_logging(container.settings, component="storage-lifecycle")
    engine = create_business_engine(
        container.settings.resolved_business_database_url.get_secret_value()
    )
    repository = SQLAlchemyStoredObjectRepository(engine)
    service = StorageLifecycleService(repository, container.file_storage)
    worker_id = f"storage-lifecycle:{socket.gethostname()}:{os.getpid()}"
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop.set)
        except NotImplementedError:
            signal.signal(name, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        while not stop.is_set():
            processed = await service.process_batch(
                worker_id,
                container.settings.storage_lifecycle_batch_size,
                container.settings.storage_lifecycle_lease_seconds,
            )
            if processed == 0:
                try:
                    await asyncio.wait_for(
                        stop.wait(),
                        timeout=container.settings.storage_lifecycle_poll_interval_seconds,
                    )
                except TimeoutError:
                    pass
    finally:
        engine.dispose()
        await close_application_container(container)


if __name__ == "__main__":
    asyncio.run(run())
