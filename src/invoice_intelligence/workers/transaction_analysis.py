"""独立交易分析 Worker 容器入口。"""

import asyncio
import logging
import signal
from threading import Event

from invoice_intelligence.bootstrap import build_container, close_application_container
from invoice_intelligence.config.logging import configure_logging


def main() -> None:
    stop = Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    container = build_container()
    configure_logging(container.settings, component="transaction-analysis")
    logger = logging.getLogger(__name__)
    try:
        while not stop.is_set():
            try:
                processed = asyncio.run(container.transaction_analysis_service.process_pending())
                if processed:
                    logger.info(
                        "transaction_analysis_batch_processed",
                        extra={"candidate_count": processed},
                    )
            except Exception as exc:
                logger.error(
                    "transaction_analysis_worker_poll_failed",
                    extra={"error_type": type(exc).__name__},
                )
            stop.wait(5)
    finally:
        asyncio.run(close_application_container(container))


if __name__ == "__main__":
    main()
