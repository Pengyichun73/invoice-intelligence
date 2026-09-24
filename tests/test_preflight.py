from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from invoice_intelligence.infrastructure.preflight import run_preflight


def _settings(tmp_path: Path, *, password_file: Path | None, dsn: str) -> SimpleNamespace:
    return SimpleNamespace(
        environment=SimpleNamespace(value="production"),
        business_database_password_file=password_file,
        resolved_business_database_url=SecretStr(dsn),
        auth_mode="oidc",
        oidc_issuer="https://issuer.example",
        oidc_jwks_url="https://issuer.example/jwks",
        oidc_tls_verify=True,
        file_storage_backend="s3",
        object_storage_endpoint_url="https://s3.example",
        object_storage_tls_verify=True,
    )


def test_production_preflight_requires_readable_password_file(tmp_path: Path) -> None:
    password_file = tmp_path / "business-password"
    password_file.write_text("secret-value\n", encoding="utf-8")
    result = run_preflight(
        _settings(
            tmp_path,
            password_file=password_file,
            dsn="postgresql+psycopg://app:secret@postgres:5432/invoice",
        )
    )
    assert result.ready
    assert result.checks["business_database_password_file"] == "ok"
    assert result.checks["business_database"] == "ok"

    missing = run_preflight(
        _settings(
            tmp_path,
            password_file=tmp_path / "missing",
            dsn="postgresql+psycopg://app:secret@postgres:5432/invoice",
        )
    )
    assert not missing.ready
    assert missing.checks["business_database_password_file"] == (
        "absolute_readable_password_file_required"
    )


def test_production_preflight_rejects_non_postgres_resolved_dsn(tmp_path: Path) -> None:
    password_file = tmp_path / "business-password"
    password_file.write_text("secret-value\n", encoding="utf-8")
    result = run_preflight(
        _settings(
            tmp_path,
            password_file=password_file,
            dsn="sqlite:///invoice.db",
        )
    )
    assert not result.ready
    assert result.checks["business_database"] == "postgresql_password_file_dsn_required"
