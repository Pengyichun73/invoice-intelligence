"""容器健康检查应使用 Settings 解析后的业务数据库 secret。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from pydantic import SecretStr

from invoice_intelligence.workers import runtime_health


def test_business_health_probe_uses_password_file_resolved_dsn(monkeypatch) -> None:
    resolved = "postgresql+psycopg://worker:secret@postgres:5432/business"
    raw = "postgresql+psycopg://worker@postgres:5432/business"
    engine = MagicMock()
    monkeypatch.setattr(runtime_health, "get_settings", lambda: SimpleNamespace(
        resolved_business_database_url=SecretStr(resolved),
    ))
    monkeypatch.setattr(runtime_health, "run_preflight", lambda *_args, **_kwargs: (
        SimpleNamespace(ready=True)
    ))
    captured: list[str] = []

    def create_engine(url: str):
        captured.append(url)
        return engine

    monkeypatch.setattr(runtime_health, "create_business_engine", create_engine)
    monkeypatch.setenv("INVOICE_INTELLIGENCE_BUSINESS_DATABASE_URL", raw)
    monkeypatch.setattr("sys.argv", ["runtime_health"])

    runtime_health.main()

    assert captured == [resolved]
    engine.connect.return_value.__enter__.return_value.execute.assert_called_once()
    engine.dispose.assert_called_once()
