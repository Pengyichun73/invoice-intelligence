"""SQLAlchemy engine construction for SQLite development and PostgreSQL production."""

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url


def create_business_engine(database_url: str) -> Engine:
    """Create a synchronous SQLAlchemy 2.x engine without creating any tables."""

    url = make_url(database_url)
    connect_args: dict[str, object] = {}
    if url.get_backend_name() == "sqlite":
        if url.database is None:
            raise ValueError("SQLite business database requires a file path")
        Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False, "timeout": 30.0}

    engine = create_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )
    if url.get_backend_name() == "sqlite":
        event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def _configure_sqlite_connection(dbapi_connection: Any, _: object) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 30000")
        cursor.execute("PRAGMA journal_mode = WAL")
    finally:
        cursor.close()
