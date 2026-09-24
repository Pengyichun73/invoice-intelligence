"""Production preflight checks with safe, low-cardinality diagnostics."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Safe preflight result; values never contain credentials or URLs."""

    ready: bool
    checks: dict[str, str]


def run_preflight(settings: Any, *, worker_id: str | None = None) -> PreflightResult:
    """Validate deployment invariants without contacting external providers."""

    checks: dict[str, str] = {}
    production = getattr(getattr(settings, "environment", None), "value", None) == "production"

    if production:
        checks["business_database_password_file"] = _password_file_check(settings)
        checks["business_database"] = _business_database_check(settings)
        checks["auth"] = (
            "ok"
            if getattr(settings, "auth_mode", None) == "oidc"
            else "production_requires_oidc"
        )
        checks["storage"] = (
            "ok"
            if getattr(settings, "file_storage_backend", None) == "s3"
            else "production_forbids_local_storage"
        )
        issuer = getattr(settings, "oidc_issuer", None)
        jwks = getattr(settings, "oidc_jwks_url", None)
        checks["oidc_tls"] = (
            "ok"
            if issuer
            and jwks
            and urlsplit(issuer).scheme == "https"
            and urlsplit(jwks).scheme == "https"
            and getattr(settings, "oidc_tls_verify", False)
            else "https_and_tls_verification_required"
        )
        endpoint = getattr(settings, "object_storage_endpoint_url", None)
        checks["object_storage_tls"] = (
            "ok"
            if endpoint
            and urlsplit(endpoint).scheme == "https"
            and getattr(settings, "object_storage_tls_verify", False)
            else "https_and_tls_verification_required"
        )
    else:
        checks["environment"] = "development_or_staging"

    if worker_id is not None:
        checks["worker_id"] = "ok" if worker_id.strip() else "stable_worker_id_required"

    return PreflightResult(
        ready=all(value in {"ok", "development_or_staging"} for value in checks.values()),
        checks=checks,
    )


def _password_file_check(settings: Any) -> str:
    """Verify the production secret path without exposing its path or contents."""

    value = getattr(settings, "business_database_password_file", None)
    if not isinstance(value, Path) or not value.is_absolute() or not value.is_file():
        return "absolute_readable_password_file_required"
    try:
        with value.open("r", encoding="utf-8") as source:
            content = source.read(4097)
    except (OSError, UnicodeError):
        return "absolute_readable_password_file_required"
    if len(content) > 4096:
        return "absolute_readable_password_file_required"
    password = content.rstrip("\r\n")
    if not password or any(character in password for character in "\x00\r\n"):
        return "absolute_readable_password_file_required"
    return "ok"


def _business_database_check(settings: Any) -> str:
    """Confirm the resolved production DSN uses the supported PostgreSQL driver."""

    try:
        resolved = getattr(settings, "resolved_business_database_url")
        dsn = (
            resolved.get_secret_value()
            if hasattr(resolved, "get_secret_value")
            else str(resolved)
        )
    except (AttributeError, OSError, ValueError, TypeError):
        return "postgresql_password_file_dsn_required"
    parsed = urlsplit(dsn)
    if (
        parsed.scheme != "postgresql+psycopg"
        or not parsed.hostname
    ):
        return "postgresql_password_file_dsn_required"
    return "ok"
