"""PostgreSQL-queued index projection worker.

The worker only performs ``project`` and ``verify``. Alias activation and rollback are
explicit API operations and are never performed by this process.
"""

import asyncio
import signal
from datetime import UTC, datetime
from types import FrameType

from invoice_intelligence.bootstrap import build_container, close_application_container
from invoice_intelligence.config.logging import configure_logging
from invoice_intelligence.config.settings import get_settings
from invoice_intelligence.infrastructure.observability.metrics import get_metrics_registry


def _install_stop_handlers(event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def stop(_signal: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(event.set)

    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if signum is not None:
            signal.signal(signum, stop)


async def _process_tenant(
    container, tenant_id: str, mode: str, batch_size: int, verify: bool,
    stale_before: datetime, worker_id: str, lease_seconds: float,
) -> int:
    processed = 0
    example_service = container.example_index_projection_service
    field_service = container.field_semantic_index_projection_service
    if mode in {"reviewed_examples", "both"} and example_service is not None:
        for version in await example_service.list_index_versions(tenant_id):
            await example_service.requeue_stale(tenant_id, version, stale_before)
            result = await example_service.project_pending(
                tenant_id, version, limit=batch_size,
                worker_id=worker_id, lease_seconds=lease_seconds,
            )
            get_metrics_registry().record_worker(
                worker="index_projection",
                outcome="indexed" if result.indexed else "idle",
                retryable=result.failed > 0,
            )
            processed += result.indexed + result.failed
            if verify:
                await example_service.verify_index_version(tenant_id, version)
    if mode in {"field_semantics", "both"} and field_service is not None:
        for version in await field_service.list_index_versions(tenant_id):
            await field_service.requeue_stale(tenant_id, version, stale_before)
            result = await field_service.project_pending(
                tenant_id, version, limit=batch_size,
                worker_id=worker_id, lease_seconds=lease_seconds,
            )
            get_metrics_registry().record_worker(
                worker="field_semantic_projection",
                outcome="indexed" if result.indexed else "idle",
                retryable=result.failed > 0,
            )
            processed += result.indexed + result.failed
            if verify:
                await field_service.verify_index_version(tenant_id, version)
    return processed


async def _run() -> None:
    settings = get_settings()
    if not settings.index_projection_worker_enabled:
        raise RuntimeError("Index projection worker is disabled")
    if not settings.resolved_business_database_url.get_secret_value().startswith("postgresql+psycopg://"):
        raise RuntimeError("Index projection worker requires PostgreSQL business storage")
    if not settings.index_projection_worker_tenant_ids:
        raise RuntimeError("index_projection_worker_tenant_ids must contain at least one tenant")
    if not settings.index_projection_worker_id:
        raise RuntimeError("index_projection_worker_id must be configured")
    container = build_container(settings)
    configure_logging(settings)
    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)
    try:
        while not stop_event.is_set():
            stale_before = datetime.now(UTC)
            for tenant_id in settings.index_projection_worker_tenant_ids:
                await _process_tenant(
                    container,
                    tenant_id,
                    settings.index_projection_worker_mode,
                    settings.index_projection_worker_batch_size,
                    settings.index_projection_worker_verify,
                    stale_before,
                    settings.index_projection_worker_id,
                    settings.index_projection_worker_lease_seconds,
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.index_projection_worker_poll_interval_seconds,
                )
            except TimeoutError:
                pass
    finally:
        await close_application_container(container)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
