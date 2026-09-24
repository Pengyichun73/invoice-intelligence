"""Executable PostgreSQL-backed Memory Admission Worker."""

import asyncio
import os
import signal
import socket
from types import FrameType

from invoice_intelligence.application.services.memory_admission_worker import (
    MemoryAdmissionWorker,
    MemoryAdmissionWorkerConfig,
)
from invoice_intelligence.bootstrap import (
    build_container,
    close_application_container,
)
from invoice_intelligence.config.logging import configure_logging
from invoice_intelligence.config.settings import get_settings
from invoice_intelligence.domain.invoice import InvoiceExtraction


def _default_worker_id() -> str:
    host = socket.gethostname().strip() or "localhost"
    return f"memory-admission:{host}:{os.getpid()}"[:128]


def _install_stop_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    handler = request_stop
    signals = (signal.SIGINT, getattr(signal, "SIGTERM", None))
    for stop_signal in signals:
        if stop_signal is None:
            continue
        signal.signal(stop_signal, handler)


async def _run() -> None:
    settings = get_settings()
    database_url = settings.resolved_business_database_url.get_secret_value()
    if not database_url.startswith("postgresql+psycopg://"):
        raise RuntimeError("Memory Admission Worker requires PostgreSQL business storage")

    container = build_container(settings)
    configure_logging(settings)
    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)
    worker = MemoryAdmissionWorker[InvoiceExtraction](
        config=MemoryAdmissionWorkerConfig(
            worker_id=settings.memory_admission_worker_id or _default_worker_id(),
            poll_interval_seconds=(
                settings.memory_admission_worker_poll_interval_seconds
            ),
            batch_size=settings.memory_admission_worker_batch_size,
            max_concurrency=settings.memory_admission_worker_max_concurrency,
            lease_seconds=settings.memory_admission_worker_lease_seconds,
            max_attempts=settings.memory_admission_worker_max_attempts,
            backoff_base_seconds=(
                settings.memory_admission_worker_backoff_base_seconds
            ),
            backoff_max_seconds=(
                settings.memory_admission_worker_backoff_max_seconds
            ),
        ),
        admission_repository=container.memory_admission_repository,
        recovery_repository=container.business_repository,
        admission_service=container.memory_admission_service,
        example_repository=container.reviewed_example_repository,
        correction_event_repository=container.business_repository,
        document_repository=container.business_repository,
        business_query_repository=container.business_repository,
        extraction_codec=container.extraction_state_codec,
        output_schema=InvoiceExtraction,
        privacy_telemetry=container.privacy_telemetry,
    )
    try:
        await worker.run(stop_event)
    finally:
        await close_application_container(container)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
