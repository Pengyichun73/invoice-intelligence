"""Service health endpoint."""

from typing import Literal

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict

from invoice_intelligence.api.dependencies import ApiDependencyBundle
from invoice_intelligence.infrastructure.observability.metrics import get_metrics_registry
from invoice_intelligence.infrastructure.preflight import run_preflight

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """Health response exposed by the HTTP adapter."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    service: str
    environment: str


class ReadinessResponse(BaseModel):
    """Safe readiness response without credentials or dependency URLs."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ready"] = "ready"
    service: str
    checks: dict[str, str]


@router.get("/health", response_model=HealthResponse)
def read_health(dependencies: ApiDependencyBundle) -> HealthResponse:
    """Report process health without invoking infrastructure dependencies."""

    return HealthResponse(
        service=dependencies.settings.app_name,
        environment=dependencies.settings.environment.value,
    )


@router.get("/ready", response_model=ReadinessResponse)
def read_readiness(dependencies: ApiDependencyBundle) -> ReadinessResponse:
    """Report deployment readiness using safe configuration preflight checks."""

    result = run_preflight(dependencies.settings)
    if not result.ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "service_not_ready", "checks": result.checks},
        )
    return ReadinessResponse(
        service=dependencies.settings.app_name,
        checks=result.checks,
    )


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def read_metrics(dependencies: ApiDependencyBundle) -> PlainTextResponse:
    """Expose low-cardinality metrics only when explicitly enabled."""

    if not dependencies.settings.metrics_endpoint_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return PlainTextResponse(
        get_metrics_registry().render(),
        media_type="text/plain; version=0.0.4",
    )
