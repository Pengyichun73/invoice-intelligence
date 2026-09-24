"""Extraction-run query protocol adapters."""

from typing import Annotated

from fastapi import APIRouter, Path

from invoice_intelligence.api.dependencies import (
    ExtractionWorkflowServiceDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.presenters import present_result, present_run
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.api.schemas.workflows import (
    ExtractionResultEnvelopeResponse,
    ExtractionRunResponse,
)

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get(
    "/{run_id}",
    response_model=ExtractionRunResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_run(
    run_id: Annotated[str, Path(min_length=1)],
    service: ExtractionWorkflowServiceDependency,
    context: TrustedTenantContextDependency,
) -> ExtractionRunResponse:
    return present_run(await service.get_run(run_id, context.tenant_id))


@router.get(
    "/{run_id}/result",
    response_model=ExtractionResultEnvelopeResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_result(
    run_id: Annotated[str, Path(min_length=1)],
    service: ExtractionWorkflowServiceDependency,
    context: TrustedTenantContextDependency,
) -> ExtractionResultEnvelopeResponse:
    return present_result(await service.get_result(run_id, context.tenant_id))
