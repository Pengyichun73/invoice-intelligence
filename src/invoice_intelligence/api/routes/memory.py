"""FastAPI adapter for tenant-scoped reviewed-example memory governance."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, status

from invoice_intelligence.api.dependencies import (
    MemoryGovernanceServiceDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.presenters import (
    present_evaluation_run,
    present_governance_audits,
    present_governance_operation,
    present_index_projection_execution,
    present_memory_admission_batch_decision,
    present_memory_admission_decision_result,
    present_memory_admission_detail,
    present_memory_admission_page,
    present_memory_example,
    present_memory_example_page,
    present_memory_example_projections,
    present_memory_feedback,
    present_memory_index,
    present_ocr_metrics,
)
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.api.schemas.memory import (
    AdmissionBatchDecisionRequest,
    AdmissionDecisionRequest,
    EvaluationRunResponse,
    GovernanceAuditListResponse,
    GovernanceOperationResponse,
    GovernanceReasonRequest,
    IndexProjectionExecutionResponse,
    IndexProjectionRequest,
    IndexRebuildRequest,
    MemoryAdmissionBatchDecisionResponse,
    MemoryAdmissionDecisionResultResponse,
    MemoryAdmissionDetailResponse,
    MemoryAdmissionListResponse,
    MemoryExampleListResponse,
    MemoryExampleProjectionListResponse,
    MemoryExampleResponse,
    MemoryFeedbackRequest,
    MemoryFeedbackResponse,
    MemoryIndexResponse,
    OCRMetricsSummaryResponse,
)
from invoice_intelligence.application.services.memory_governance import IndexRebuildSpec
from invoice_intelligence.domain.admission import MemoryAdmissionStatus
from invoice_intelligence.domain.examples import (
    ExampleLabelType,
    IndexProjectionStatus,
    IndexVersion,
    ModelVersion,
    PromptVersion,
)
from invoice_intelligence.domain.governance import GovernanceAction

router = APIRouter(prefix="/memory", tags=["memory-governance"])


@router.get(
    "/ocr-metrics",
    response_model=OCRMetricsSummaryResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_ocr_metrics(
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> OCRMetricsSummaryResponse:
    return present_ocr_metrics(await service.get_ocr_metrics(context))


@router.get(
    "/audits",
    response_model=GovernanceAuditListResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def list_governance_audits(
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    operation: Annotated[GovernanceAction | None, Query()] = None,
    resource_type: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    resource_id: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    trace_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    started_at: Annotated[datetime | None, Query()] = None,
    ended_at: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> GovernanceAuditListResponse:
    return present_governance_audits(
        await service.list_audits(
            context,
            action=operation,
            resource_type=resource_type,
            resource_id=resource_id,
            trace_id=trace_id,
            started_at=started_at,
            ended_at=ended_at,
            limit=limit,
            cursor=cursor,
        )
    )


@router.get(
    "/admissions",
    response_model=MemoryAdmissionListResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def list_admissions(
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    admission_statuses: Annotated[
        list[MemoryAdmissionStatus] | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    run_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    field_path: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> MemoryAdmissionListResponse:
    return present_memory_admission_page(
        await service.list_admissions(
            context,
            statuses=tuple(admission_statuses or tuple(MemoryAdmissionStatus)),
            limit=limit,
            cursor=cursor,
            run_id=run_id,
            field_path=field_path,
        )
    )


async def _decide_admissions_batch(
    target_status: MemoryAdmissionStatus,
    request: AdmissionBatchDecisionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: str | None,
) -> MemoryAdmissionBatchDecisionResponse:
    return present_memory_admission_batch_decision(
        await service.decide_admissions_batch(
            context,
            target_status,
            items=tuple(
                (item.admission_id, item.expected_revision) for item in request.items
            ),
            reason=request.reason,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/admissions/batch/approve",
    response_model=MemoryAdmissionBatchDecisionResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def approve_admissions_batch(
    request: AdmissionBatchDecisionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryAdmissionBatchDecisionResponse:
    return await _decide_admissions_batch(
        MemoryAdmissionStatus.APPROVED,
        request,
        service,
        context,
        idempotency_key,
    )


@router.post(
    "/admissions/batch/reject",
    response_model=MemoryAdmissionBatchDecisionResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def reject_admissions_batch(
    request: AdmissionBatchDecisionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryAdmissionBatchDecisionResponse:
    return await _decide_admissions_batch(
        MemoryAdmissionStatus.REJECTED,
        request,
        service,
        context,
        idempotency_key,
    )


@router.post(
    "/admissions/batch/quarantine",
    response_model=MemoryAdmissionBatchDecisionResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def quarantine_admissions_batch(
    request: AdmissionBatchDecisionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryAdmissionBatchDecisionResponse:
    return await _decide_admissions_batch(
        MemoryAdmissionStatus.QUARANTINED,
        request,
        service,
        context,
        idempotency_key,
    )


@router.post(
    "/admissions/batch/reassess",
    response_model=MemoryAdmissionBatchDecisionResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def reassess_admissions_batch(
    request: AdmissionBatchDecisionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryAdmissionBatchDecisionResponse:
    return await _decide_admissions_batch(
        MemoryAdmissionStatus.PENDING,
        request,
        service,
        context,
        idempotency_key,
    )


@router.get(
    "/admissions/{admission_id}",
    response_model=MemoryAdmissionDetailResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_admission(
    admission_id: Annotated[str, Path(min_length=1, max_length=64)],
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> MemoryAdmissionDetailResponse:
    return present_memory_admission_detail(
        await service.get_admission(context, admission_id)
    )


async def _decide_admission(
    admission_id: str,
    target_status: MemoryAdmissionStatus,
    request: AdmissionDecisionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: str | None,
) -> MemoryAdmissionDecisionResultResponse:
    return present_memory_admission_decision_result(
        await service.decide_admission(
            context,
            admission_id,
            target_status,
            reason=request.reason,
            expected_revision=request.expected_revision,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/admissions/{admission_id}/approve",
    response_model=MemoryAdmissionDecisionResultResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def approve_admission(
    admission_id: Annotated[str, Path(min_length=1, max_length=64)],
    request: AdmissionDecisionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryAdmissionDecisionResultResponse:
    return await _decide_admission(
        admission_id,
        MemoryAdmissionStatus.APPROVED,
        request,
        service,
        context,
        idempotency_key,
    )


@router.post(
    "/admissions/{admission_id}/reject",
    response_model=MemoryAdmissionDecisionResultResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def reject_admission(
    admission_id: Annotated[str, Path(min_length=1, max_length=64)],
    request: AdmissionDecisionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryAdmissionDecisionResultResponse:
    return await _decide_admission(
        admission_id,
        MemoryAdmissionStatus.REJECTED,
        request,
        service,
        context,
        idempotency_key,
    )


@router.post(
    "/admissions/{admission_id}/quarantine",
    response_model=MemoryAdmissionDecisionResultResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def quarantine_admission(
    admission_id: Annotated[str, Path(min_length=1, max_length=64)],
    request: AdmissionDecisionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryAdmissionDecisionResultResponse:
    return await _decide_admission(
        admission_id,
        MemoryAdmissionStatus.QUARANTINED,
        request,
        service,
        context,
        idempotency_key,
    )


@router.post(
    "/admissions/{admission_id}/reassess",
    response_model=MemoryAdmissionDecisionResultResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def reassess_admission(
    admission_id: Annotated[str, Path(min_length=1, max_length=64)],
    request: AdmissionDecisionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryAdmissionDecisionResultResponse:
    return await _decide_admission(
        admission_id,
        MemoryAdmissionStatus.PENDING,
        request,
        service,
        context,
        idempotency_key,
    )


@router.get(
    "/examples",
    response_model=MemoryExampleListResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def list_examples(
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    schema_version: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    field_path: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    run_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    label_type: Annotated[ExampleLabelType | None, Query()] = None,
    is_valid: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> MemoryExampleListResponse:
    return present_memory_example_page(
        await service.list_examples(
            context,
            schema_version=schema_version,
            field_path=field_path,
            run_id=run_id,
            label_type=label_type,
            is_valid=is_valid,
            limit=limit,
            cursor=cursor,
        )
    )


@router.get(
    "/examples/{example_id}",
    response_model=MemoryExampleResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_example(
    example_id: Annotated[str, Path(min_length=1, max_length=64)],
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> MemoryExampleResponse:
    return present_memory_example(await service.get_example(context, example_id))


@router.get(
    "/examples/{example_id}/projections",
    response_model=MemoryExampleProjectionListResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def list_example_projections(
    example_id: Annotated[str, Path(min_length=1, max_length=64)],
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    index_version: Annotated[
        str | None,
        Query(min_length=1, max_length=256),
    ] = None,
    projection_status: Annotated[
        IndexProjectionStatus | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> MemoryExampleProjectionListResponse:
    return present_memory_example_projections(
        await service.list_example_projections(
            context,
            example_id,
            index_version=index_version,
            status=projection_status,
            limit=limit,
            cursor=cursor,
        )
    )


@router.post(
    "/examples/{example_id}/disable",
    response_model=GovernanceOperationResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def disable_example(
    example_id: Annotated[str, Path(min_length=1, max_length=64)],
    request: GovernanceReasonRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> GovernanceOperationResponse:
    return present_governance_operation(
        await service.disable_example(
            context,
            example_id,
            request.reason,
            idempotency_key,
        )
    )


@router.post(
    "/schema-versions/{schema_version}/invalidate",
    response_model=GovernanceOperationResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def invalidate_schema(
    schema_version: Annotated[str, Path(min_length=1, max_length=64)],
    request: GovernanceReasonRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> GovernanceOperationResponse:
    return present_governance_operation(
        await service.invalidate_schema(
            context,
            schema_version,
            request.reason,
            idempotency_key,
        )
    )


@router.post(
    "/indexes/rebuild",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MemoryIndexResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def rebuild_index(
    request: IndexRebuildRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryIndexResponse:
    return present_memory_index(
        await service.rebuild_index(
            context,
            IndexRebuildSpec(
                index_version=IndexVersion(request.index_version),
                schema_version=request.schema_version,
                dense_model_version=ModelVersion(request.dense_model_version),
                sparse_model_version=(
                    ModelVersion(request.sparse_model_version)
                    if request.sparse_model_version is not None
                    else None
                ),
                rerank_model_version=(
                    ModelVersion(request.rerank_model_version)
                    if request.rerank_model_version is not None
                    else None
                ),
                prompt_version=PromptVersion(request.prompt_version),
                reason=request.reason,
            ),
            idempotency_key,
        )
    )


@router.get(
    "/indexes/{index_version}",
    response_model=MemoryIndexResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_index(
    index_version: Annotated[str, Path(min_length=1, max_length=256)],
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> MemoryIndexResponse:
    return present_memory_index(await service.get_index(context, index_version))


@router.post(
    "/indexes/{index_version}/project",
    response_model=IndexProjectionExecutionResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def project_index(
    index_version: Annotated[str, Path(min_length=1, max_length=256)],
    request: IndexProjectionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> IndexProjectionExecutionResponse:
    return present_index_projection_execution(
        await service.project_index(context, index_version, limit=request.limit)
    )


@router.post(
    "/indexes/{index_version}/activate",
    response_model=MemoryIndexResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def activate_index(
    index_version: Annotated[str, Path(min_length=1, max_length=256)],
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> MemoryIndexResponse:
    return present_memory_index(await service.activate_index(context, index_version))


@router.post("/indexes/{index_version}/rollback", response_model=MemoryIndexResponse, responses=STANDARD_ERROR_RESPONSES)
async def rollback_index(
    index_version: Annotated[str, Path(min_length=1, max_length=256)],
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> MemoryIndexResponse:
    return present_memory_index(await service.rollback_index(context, index_version))


@router.post(
    "/feedback",
    response_model=MemoryFeedbackResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def submit_feedback(
    request: MemoryFeedbackRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryFeedbackResponse:
    return present_memory_feedback(
        await service.submit_feedback(
            context,
            trace_id=request.trace_id,
            example_id=request.example_id,
            label=request.label,
            reason=request.reason,
            idempotency_key=idempotency_key,
        )
    )


@router.get(
    "/evaluations/{evaluation_run_id}",
    response_model=EvaluationRunResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_evaluation(
    evaluation_run_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> EvaluationRunResponse:
    return present_evaluation_run(
        await service.get_evaluation(context, evaluation_run_id)
    )
