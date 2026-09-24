"""Fail-closed entrypoint for PostgreSQL claim/lease based workers."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass

import psycopg

from invoice_intelligence.config.settings import get_settings


@dataclass(frozen=True)
class QueuedWorkerSpec:
    name: str
    queue_table: str
    worker_id_env: str


async def run_queued_worker(spec: QueuedWorkerSpec) -> None:
    dsn = get_settings().resolved_business_database_url.get_secret_value()
    if not dsn.startswith("postgresql+psycopg://"):
        raise RuntimeError(f"{spec.name} requires PostgreSQL business storage")
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    worker_id = os.environ.get(spec.worker_id_env, "").strip()
    if not worker_id:
        raise RuntimeError(f"{spec.name} requires a stable {spec.worker_id_env}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    # Queue tables are deliberately not invented here. Deployment is healthy only
    # after the owning migration/service provides the claim/lease contract.
    async with await psycopg.AsyncConnection.connect(dsn) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT to_regclass(%s)", (spec.queue_table,))
            row = await cursor.fetchone()
        if not row or row[0] is None:
            raise RuntimeError(
                f"{spec.name} queue table {spec.queue_table!r} is unavailable; refusing to poll"
            )
    raise RuntimeError(
        f"{spec.name} queue adapter is not implemented; claim/lease service must be registered"
    )


def main(spec: QueuedWorkerSpec) -> None:
    asyncio.run(run_queued_worker(spec))
