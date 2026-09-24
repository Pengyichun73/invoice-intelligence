"""Executable PostgreSQL-backed Harness Worker."""

import asyncio
import os
import signal
import socket
from types import FrameType

from invoice_intelligence.bootstrap import (
    build_container,
    build_harness_worker_service,
    close_application_container,
)
from invoice_intelligence.config.logging import configure_logging
from invoice_intelligence.config.settings import get_settings


def _default_worker_id() -> str:
    host = socket.gethostname().strip() or "localhost"
    return f"code-harness:{host}:{os.getpid()}"[:128]


def _install_stop_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for stop_signal in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if stop_signal is not None:
            signal.signal(stop_signal, request_stop)


async def _run() -> None:
    settings = get_settings()
    database_url = settings.resolved_business_database_url.get_secret_value()
    if not database_url.startswith("postgresql+psycopg://"):
        raise RuntimeError("Code Harness Worker requires PostgreSQL business storage")

    container = build_container(settings)
    configure_logging(settings)
    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)
    registered_sources = await container.code_harness_source_registry.list_enabled()
    service = build_harness_worker_service(
        container,
        sources={
            (source.tenant_id, source.repository_id): source
            for source in registered_sources
        },
        worker_id=os.getenv("INVOICE_INTELLIGENCE_CODE_HARNESS_WORKER_ID")
        or _default_worker_id(),
    )
    poll_interval = float(
        os.getenv("INVOICE_INTELLIGENCE_CODE_HARNESS_POLL_INTERVAL_SECONDS", "2")
    )
    try:
        while not stop_event.is_set():
            await service.execute_one()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except TimeoutError:
                continue
    finally:
        await close_application_container(container)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
