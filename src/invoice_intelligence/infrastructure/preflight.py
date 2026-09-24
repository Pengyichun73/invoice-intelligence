"""Production preflight checks with safe, low-cardinality diagnostics."""

from dataclasses import dataclass
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
