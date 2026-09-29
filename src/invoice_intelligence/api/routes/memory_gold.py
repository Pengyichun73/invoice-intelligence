"""OIDC-scoped three-person gold annotation endpoints."""

from typing import Annotated

from fastapi import APIRouter, Path

from invoice_intelligence.api.dependencies import (
    MemoryGoldServiceDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.api.schemas.memory_gold import (
    GoldAdjudicationRequest,
    GoldAnnotationRequest,
    GoldCaseResponse,
)

router = APIRouter(prefix="/memory/gold", tags=["memory-gold"])


@router.post(
    "/{document_id}/annotations", response_model=GoldCaseResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def submit_annotation(
    document_id: Annotated[str, Path(min_length=1, max_length=36)],
    request: GoldAnnotationRequest,
    service: MemoryGoldServiceDependency,
    context: TrustedTenantContextDependency,
) -> GoldCaseResponse:
    return GoldCaseResponse.model_validate(await service.submit(
        context, document_id, request.template_group,
        request.versions.model_dump(), request.fields,
    ))


@router.post(
    "/{document_id}/adjudication", response_model=GoldCaseResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def adjudicate(
    document_id: Annotated[str, Path(min_length=1, max_length=36)],
    request: GoldAdjudicationRequest,
    service: MemoryGoldServiceDependency,
    context: TrustedTenantContextDependency,
) -> GoldCaseResponse:
    return GoldCaseResponse.model_validate(await service.adjudicate(
        context, document_id, request.field_choices,
    ))


@router.get(
    "/{document_id}", response_model=GoldCaseResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_case(
    document_id: Annotated[str, Path(min_length=1, max_length=36)],
    service: MemoryGoldServiceDependency,
    context: TrustedTenantContextDependency,
) -> GoldCaseResponse:
    return GoldCaseResponse.model_validate(await service.get(context, document_id))
