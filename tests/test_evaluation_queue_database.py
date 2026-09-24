"""评估 API、Scheduler 和 Worker 必须消费同一事实库队列。"""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from invoice_intelligence.workers import evaluation_queue_database


def test_queue_uses_resolved_business_password_file_dsn(monkeypatch) -> None:
    resolved = "postgresql+psycopg://worker:secret@postgres:5432/invoice"
    monkeypatch.setattr(
        evaluation_queue_database,
        "get_settings",
        lambda: SimpleNamespace(resolved_business_database_url=SecretStr(resolved)),
    )
    assert evaluation_queue_database.evaluation_queue_database_url() == resolved


def test_queue_rejects_sqlite(monkeypatch) -> None:
    monkeypatch.setattr(
        evaluation_queue_database,
        "get_settings",
        lambda: SimpleNamespace(
            resolved_business_database_url=SecretStr("sqlite+pysqlite:///:memory:")
        ),
    )
    with pytest.raises(RuntimeError, match="business PostgreSQL"):
        evaluation_queue_database.evaluation_queue_database_url()
