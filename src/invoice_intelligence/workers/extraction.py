"""Run queued invoice workflows outside the HTTP request lifecycle."""

import asyncio
import logging
import os
import signal
import socket
from types import FrameType

from invoice_intelligence.application.ports.extraction_queue import (
    ExtractionQueueRepository,
    ExtractionWorkLease,
)
from invoice_intelligence.bootstrap import (
    build_container,
    close_application_container,
    open_invoice_workflow_runner,
)
from invoice_intelligence.config.logging import configure_logging
from invoice_intelligence.config.settings import get_settings
from invoice_intelligence.domain.workflow import WorkflowStatus

_LOGGER = logging.getLogger(__name__)
_LEASE_SECONDS = 900
_MAX_ATTEMPTS = 3


async def _run() -> None:
    settings = get_settings()
    if not settings.resolved_business_database_url.get_secret_value().startswith(
        "postgresql+psycopg://"
    ):
        raise RuntimeError("Extraction Worker requires PostgreSQL business storage")
    if settings.checkpoint_backend != "postgres":
        raise RuntimeError("Extraction Worker requires a shared PostgreSQL checkpointer")
    configure_logging(settings, component="extraction")
    container = build_container(settings)
    repository = container.extraction_queue_repository
    worker_id = os.getenv("INVOICE_INTELLIGENCE_EXTRACTION_WORKER_ID") or (
        f"extraction:{socket.gethostname()}:{os.getpid()}"
    )
    stop = asyncio.Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, request_stop)
    try:
        async with open_invoice_workflow_runner(container) as runner:
            if runner is None:
                raise RuntimeError("Vision extraction is not configured")
            while not stop.is_set():
                lease = await repository.claim(worker_id, _LEASE_SECONDS)
                if lease is None:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=2.0)
                    except TimeoutError:
                        pass
                    continue
                heartbeat_stop = asyncio.Event()
                lost = asyncio.Event()
                heartbeat = asyncio.create_task(
                    _renew_lease(repository, lease, heartbeat_stop, lost)
                )
                try:
                    if lease.kind == "start":
                        document = await container.business_repository.get_document(
                            lease.identity.document_id, lease.tenant_id
                        )
                        if document is None:
                            raise RuntimeError("queued_document_missing")
                        state = await runner.start_or_continue(
                            lease.identity, document, lease.tenant_id, lease.trace_id
                        )
                    elif lease.kind == "resume" and lease.correction is not None:
                        if lease.checkpoint_id is None:
                            raise RuntimeError("queued_review_checkpoint_missing")
                        state = await runner.resume_or_continue(
                            lease.identity, lease.correction, lease.checkpoint_id
                        )
                    else:
                        raise RuntimeError("queued_work_kind_invalid")
                    if state.get("review_fact_persistence_failed") is True:
                        raise RuntimeError("review_fact_persistence_failed")
                    if state.get("status") not in {
                        WorkflowStatus.PENDING_REVIEW.value,
                        WorkflowStatus.COMPLETED.value,
                        WorkflowStatus.FAILED.value,
                    }:
                        raise RuntimeError("workflow_not_terminal")
                    if not lost.is_set():
                        await repository.finish(lease)
                except Exception as exc:
                    _LOGGER.error(
                        "Extraction task failed",
                        extra={
                            "run_id": lease.identity.run_id,
                            "task_id": lease.task_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                    if not lost.is_set():
                        await repository.retry(
                            lease,
                            error_code=type(exc).__name__[:128],
                            max_attempts=_MAX_ATTEMPTS,
                            delay_seconds=min(60, 2 ** lease.attempt_count),
                        )
                finally:
                    heartbeat_stop.set()
                    await heartbeat
    finally:
        await close_application_container(container)


async def _renew_lease(
    repository: ExtractionQueueRepository,
    lease: ExtractionWorkLease,
    stop: asyncio.Event,
    lost: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_LEASE_SECONDS / 3)
            return
        except TimeoutError:
            try:
                if not await repository.renew(lease, _LEASE_SECONDS):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
