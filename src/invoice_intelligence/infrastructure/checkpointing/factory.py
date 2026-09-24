"""Environment-selectable LangGraph checkpointer lifecycle."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from invoice_intelligence.config.settings import Settings


@asynccontextmanager
async def open_checkpointer(
    settings: Settings,
) -> AsyncIterator[BaseCheckpointSaver[str]]:
    """Open a development SQLite or production PostgreSQL checkpointer."""

    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    if settings.checkpoint_backend == "sqlite":
        checkpoint_path = settings.sqlite_checkpoint_path.expanduser().resolve()
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
            yield checkpointer
        return

    dsn = settings.postgres_checkpoint_dsn
    if dsn is None:
        raise ValueError("PostgreSQL checkpoint DSN is not configured")
    async with AsyncPostgresSaver.from_conn_string(dsn.get_secret_value()) as checkpointer:
        await checkpointer.setup()
        yield checkpointer
