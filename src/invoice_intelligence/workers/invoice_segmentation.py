"""Durable invoice-boundary worker with fenced PostgreSQL claims."""

import asyncio
import logging
import signal

from invoice_intelligence.bootstrap import build_container, close_application_container
from invoice_intelligence.config.logging import configure_logging

_LOGGER = logging.getLogger(__name__)


async def _run() -> None:
    container = build_container()
    configure_logging(container.settings, component="invoice-segmentation")
    service = container.invoice_batch_service
    repository = container.invoice_batch_repository
    stop = asyncio.Event()
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, lambda *_: stop.set())
    try:
        while not stop.is_set():
            claim = await repository.claim()
            if claim is not None:
                done = asyncio.Event()
                renew = asyncio.create_task(_renew(repository, claim, done))
                try:
                    await service.process_claim(claim)
                except Exception as exc:
                    _LOGGER.error("Invoice segmentation failed", extra={
                        "batch_id": claim["batch_id"], "file_id": claim["file_id"],
                        "error_type": type(exc).__name__,
                    })
                    await repository.fail_claim(claim, type(exc).__name__)
                finally:
                    done.set()
                    await renew
            try:
                await service.dispatch_ready()
            except Exception as exc:
                _LOGGER.error("Invoice batch dispatch failed", extra={
                    "error_type": type(exc).__name__,
                })
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass
    finally:
        await close_application_container(container)


async def _renew(repository, claim: dict, done: asyncio.Event) -> None:
    while not done.is_set():
        try:
            await asyncio.wait_for(done.wait(), timeout=300)
        except TimeoutError:
            if not await repository.renew(claim):
                return


if __name__ == "__main__":
    asyncio.run(_run())
