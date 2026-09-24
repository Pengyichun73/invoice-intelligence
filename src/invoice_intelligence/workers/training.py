"""Executable PostgreSQL-backed Training Worker."""

import asyncio
import os
import signal
import socket
from types import FrameType

from invoice_intelligence.application.services.training_worker import (
    TrainingWorkerConfig,
    TrainingWorkerService,
)
from invoice_intelligence.bootstrap import build_container, close_application_container
from invoice_intelligence.config.logging import configure_logging
from invoice_intelligence.config.settings import get_settings


def _worker_id() -> str:
    configured = get_settings().training_worker_id
    if configured:
        return configured
    host = socket.gethostname().strip() or "localhost"
    return f"training:{host}:{os.getpid()}"[:128]


def _install_stop_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for stop_signal in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if stop_signal is not None:
            signal.signal(stop_signal, request_stop)


async def _run() -> None:
    settings = get_settings()
    if not settings.resolved_business_database_url.get_secret_value().startswith(
        "postgresql+psycopg://"
    ):
        raise RuntimeError("Training Worker requires PostgreSQL business storage")
    container = build_container(settings)
    configure_logging(settings)
    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)
    worker = TrainingWorkerService(
        config=TrainingWorkerConfig(
            worker_id=_worker_id(),
            poll_interval_seconds=settings.training_worker_poll_interval_seconds,
            batch_size=settings.training_worker_batch_size,
            lease_seconds=settings.training_worker_lease_seconds,
            max_attempts=settings.training_worker_max_attempts,
            backoff_base_seconds=settings.training_worker_backoff_base_seconds,
            backoff_max_seconds=settings.training_worker_backoff_max_seconds,
        ),
        registry=container.training_registry_repository,
        provider=container.training_provider,
        artifact_reader=container.training_artifact_reader,
    )
    try:
        await worker.run(stop_event)
    finally:
        await close_application_container(container)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
