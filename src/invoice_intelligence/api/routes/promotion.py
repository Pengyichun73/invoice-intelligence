"""模型晋升的 HTTP 适配；门禁事实只由 Application Service 读取。"""

from typing import Annotated

from fastapi import APIRouter, Path, status

from invoice_intelligence.api.dependencies import (
    PromotionCandidateServiceDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.api.schemas.promotion import (
    PromotionApprovalRequest,
    PromotionCandidateCreateRequest,
    PromotionCandidateResponse,
    PromotionRejectionRequest,
    PromotionRollbackRequest,
)
from invoice_intelligence.domain.training import PromotionCandidateRecord

router = APIRouter(prefix="/model-promotion", tags=["model-promotion"])


def _response(candidate: PromotionCandidateRecord) -> PromotionCandidateResponse:
    return PromotionCandidateResponse(
        candidate_id=candidate.candidate_id,
        artifact_id=candidate.artifact_id,
        model_evaluation_id=candidate.model_evaluation_id,
        status=candidate.status,
        revision=candidate.revision,
        dataset_version=candidate.dataset_version,
        evaluation_run_id=candidate.evaluation_run_id,
        model_version=candidate.model_version,
        prompt_version=candidate.prompt_version,
        schema_version=candidate.schema_version,
        index_version=candidate.index_version,
        threshold_version=candidate.threshold_version,
        hard_failure_code=candidate.hard_failure_code,
        compatibility_errors=candidate.compatibility_errors,
        approved_by=candidate.approved_by,
        rejection_reason=candidate.rejection_reason,
    )


@router.post(
    "",
    response_model=PromotionCandidateResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
)
async def create_candidate(
    request: PromotionCandidateCreateRequest,
    service: PromotionCandidateServiceDependency,
    context: TrustedTenantContextDependency,
) -> PromotionCandidateResponse:
    return _response(
        await service.create(
            tenant_id=context.tenant_id,
            candidate_id=request.candidate_id,
            evaluation_run_id=request.evaluation_run_id,
            artifact_id=request.artifact_id,
            actor_id=context.actor_id,
            trace_id=context.trace_id,
        )
    )


@router.post(
    "/{candidate_id}/approve",
    response_model=PromotionCandidateResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def approve_candidate(
    candidate_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: PromotionApprovalRequest,
    service: PromotionCandidateServiceDependency,
    context: TrustedTenantContextDependency,
) -> PromotionCandidateResponse:
    return _response(
        await service.approve(
            tenant_id=context.tenant_id,
            candidate_id=candidate_id,
            expected_revision=request.expected_revision,
            actor_id=context.actor_id,
            trace_id=context.trace_id,
            target_status=request.target_status,
        )
    )


@router.post(
    "/{candidate_id}/reject",
    response_model=PromotionCandidateResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def reject_candidate(
    candidate_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: PromotionRejectionRequest,
    service: PromotionCandidateServiceDependency,
    context: TrustedTenantContextDependency,
) -> PromotionCandidateResponse:
    return _response(
        await service.reject(
            tenant_id=context.tenant_id,
            candidate_id=candidate_id,
            expected_revision=request.expected_revision,
            actor_id=context.actor_id,
            reason=request.reason,
            trace_id=context.trace_id,
        )
    )


@router.post(
    "/{candidate_id}/rollback",
    response_model=PromotionCandidateResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def rollback_candidate(
    candidate_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: PromotionRollbackRequest,
    service: PromotionCandidateServiceDependency,
    context: TrustedTenantContextDependency,
) -> PromotionCandidateResponse:
    return _response(
        await service.rollback(
            tenant_id=context.tenant_id,
            candidate_id=candidate_id,
            target_candidate_id=request.target_candidate_id,
            expected_revision=request.expected_revision,
            actor_id=context.actor_id,
            trace_id=context.trace_id,
        )
    )
