"""Deterministic isolated evaluation worker."""

import asyncio
import os
import signal

from invoice_intelligence.bootstrap import build_evaluation_worker_service
from invoice_intelligence.config.settings import get_settings
from invoice_intelligence.infrastructure.observability.metrics import (
    get_metrics_registry,
)
from invoice_intelligence.infrastructure.persistence.database import create_business_engine
from invoice_intelligence.workers.evaluation_queue_database import (
    evaluation_queue_database_url,
)


async def _run() -> None:
    dsn = evaluation_queue_database_url()
    worker_id = os.environ.get("INVOICE_INTELLIGENCE_EVALUATION_WORKER_ID", "").strip()
    if not worker_id:
        raise RuntimeError("Evaluation Worker requires a stable worker ID")
    engine = create_business_engine(dsn)
    service, repository = build_evaluation_worker_service(engine, get_settings())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        while not stop.is_set():
            recovered = await service.recover_expired()
            if recovered:
                get_metrics_registry().increment(
                    "invoice_worker_lease_expired_total",
                    value=recovered,
                    labels={"worker": "evaluation"},
                )
            job = await service.execute_one(worker_id)
            if job is not None:
                current = await repository.get_job(job.tenant_id, job.job_id)
                outcome = current.status.value if current is not None else "unknown"
                get_metrics_registry().record_worker(
                    worker="evaluation",
                    outcome=outcome,
                    retryable=current is not None
                    and current.failure_code is not None
                    and current.status.value == "failed",
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except TimeoutError:
                pass
    finally:
        engine.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
