"""PostgreSQL scheduler that only enqueues due evaluation jobs."""

import asyncio
import os
import signal
from datetime import UTC, datetime

from invoice_intelligence.config.logging import configure_logging
from invoice_intelligence.config.settings import get_settings
from invoice_intelligence.infrastructure.persistence.database import create_business_engine
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation_jobs import (
    SQLAlchemyEvaluationJobRepository,
)
from invoice_intelligence.workers.evaluation_queue_database import (
    evaluation_queue_database_url,
)


async def _run() -> None:
    configure_logging(get_settings(), component="scheduler")
    dsn = evaluation_queue_database_url()
    worker_id = os.environ.get("INVOICE_INTELLIGENCE_SCHEDULER_WORKER_ID", "").strip()
    if not worker_id:
        raise RuntimeError("Scheduler requires a stable worker ID")
    engine = create_business_engine(dsn)
    repository = SQLAlchemyEvaluationJobRepository(engine)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        while not stop.is_set():
            await repository.enqueue_due(datetime.now(UTC), 50)
            try:
                await asyncio.wait_for(stop.wait(), timeout=30.0)
            except TimeoutError:
                pass
    finally:
        engine.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
