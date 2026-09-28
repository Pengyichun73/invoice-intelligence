"""SQLAlchemy engine construction for SQLite development and PostgreSQL production."""

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url


def create_business_engine(
    database_url: str,
    *,
    connect_timeout_seconds: float = 5.0,
    pool_timeout_seconds: float = 5.0,
    statement_timeout_seconds: float = 5.0,
) -> Engine:
    """Create a synchronous SQLAlchemy 2.x engine without creating any tables."""

    if (
        connect_timeout_seconds <= 0
        or pool_timeout_seconds <= 0
        or statement_timeout_seconds <= 0
    ):
        raise ValueError("Database timeouts must be positive")
    url = make_url(database_url)
    connect_args: dict[str, object] = {}
    if url.get_backend_name() == "sqlite":
        if url.database is None:
            raise ValueError("SQLite business database requires a file path")
        Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False, "timeout": 30.0}
    elif url.get_backend_name() == "postgresql":
        connect_args = {
            "connect_timeout": int(connect_timeout_seconds),
            "options": (
                f"-c statement_timeout={int(statement_timeout_seconds * 1000)}"
            ),
        }

    engine = create_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_timeout=pool_timeout_seconds,
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
