"""Business database credentials stay in process memory, not Compose environment."""

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from invoice_intelligence.config.settings import Settings


def _base_settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        business_database_url=(
            "postgresql+psycopg://invoice_intelligence@postgres:5432/invoice_intelligence"
        ),
        **overrides,
    )


def test_password_file_is_encoded_into_resolved_url_without_changing_config() -> None:
    with patch.object(Path, "open", side_effect=lambda *a, **kw: StringIO("sample:@/%?#\n")):
        settings = _base_settings(business_database_password_file=Path("business-password"))
        resolved = make_url(settings.resolved_business_database_url.get_secret_value())

    assert resolved.password == "sample:@/%?#"
    assert resolved.username == "invoice_intelligence"
    assert resolved.host == "postgres"
    assert resolved.database == "invoice_intelligence"
    assert make_url(settings.business_database_url.get_secret_value()).password is None
    assert "sample:@/%?#" not in repr(settings)


def test_development_accepts_inline_password_without_password_file() -> None:
    settings = Settings(
        _env_file=None,
        business_database_url="postgresql+psycopg://invoice_intelligence:local@localhost/db",
    )
    assert make_url(settings.resolved_business_database_url.get_secret_value()).password == "local"


@pytest.mark.parametrize(
    "content", (None, "", "invalid\nsecond-line", "x" * 4097),
    ids=("missing", "empty", "multiline", "oversized"),
)
def test_missing_or_invalid_password_file_fails_closed(content: str | None) -> None:
    if content is None:
        opener = patch.object(Path, "open", side_effect=OSError("unreadable"))
    else:
        opener = patch.object(Path, "open", return_value=StringIO(content))
    with opener, pytest.raises(ValidationError, match="business_database_password_file"):
        _base_settings(business_database_password_file=Path("business-password"))


def test_inline_password_and_password_file_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="not both"):
        Settings(
            _env_file=None,
            business_database_url="postgresql+psycopg://user:inline@postgres/db",
            business_database_password_file=Path("business-password"),
        )


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "dev_tenant_id": None,
        "auth_mode": "oidc",
        "oidc_issuer": "https://id.example/realms/invoice",
        "oidc_audience": "invoice-api",
        "oidc_jwks_url": "https://id.example/realms/invoice/certs",
        "file_storage_backend": "s3",
        "object_storage_endpoint_url": "https://storage.example",
        "object_storage_tenant_hmac_key": "non-default-test-key",
        "reviewed_example_fingerprint_salt": "non-default-test-salt",
        "milvus_enabled": True,
        "milvus_uri": "https://milvus.example",
        "reranking_provider": "qwen",
        "business_database_url": "postgresql+psycopg://invoice@postgres:5432/invoice",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_requires_readable_password_file() -> None:
    with pytest.raises(
        ValidationError, match="Production requires business_database_password_file"
    ):
        _production_settings()

    with patch.object(Path, "open", side_effect=lambda *a, **kw: StringIO("test-password")):
        settings = _production_settings(
            business_database_password_file=Path.cwd() / "business-password"
        )
        resolved = make_url(settings.resolved_business_database_url.get_secret_value())
    assert resolved.password == "test-password"


def test_production_rejects_inline_password_without_exposing_it() -> None:
    with pytest.raises(ValidationError) as error:
        _production_settings(
            business_database_url=(
                "postgresql+psycopg://invoice:do-not-print-this@postgres:5432/invoice"
            )
        )
    assert "business_database_password_file" in str(error.value)
    assert "do-not-print-this" not in str(error.value)


def test_password_file_requires_postgresql() -> None:
    with pytest.raises(ValidationError, match="requires PostgreSQL"):
        Settings(
            _env_file=None,
            correction_memory_backend="disabled",
            business_database_url="sqlite+pysqlite:///test.sqlite",
            business_database_password_file=Path("business-password"),
        )
