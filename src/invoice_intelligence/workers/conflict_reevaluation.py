"""消费已关闭冲突的 PostgreSQL 重评估请求。"""

import asyncio
import signal

from invoice_intelligence.config.logging import configure_logging
from invoice_intelligence.config.settings import get_settings
from invoice_intelligence.infrastructure.persistence.database import create_business_engine
from invoice_intelligence.infrastructure.persistence.sqlalchemy_conflict_reevaluation import (
    SQLAlchemyConflictReevaluationConsumer,
)


async def _run() -> None:
    settings = get_settings()
    dsn = settings.resolved_business_database_url.get_secret_value()
    if not dsn.startswith("postgresql+psycopg://"):
        raise RuntimeError("Conflict reevaluation requires PostgreSQL business storage")
    configure_logging(settings)
    engine = create_business_engine(dsn)
    consumer = SQLAlchemyConflictReevaluationConsumer(engine)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if signum is None:
            continue
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        while not stop.is_set():
            processed = await consumer.consume_one()
            if processed:
                continue
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
