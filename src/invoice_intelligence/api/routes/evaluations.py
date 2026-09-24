"""Tenant-scoped offline evaluation API; no work runs in HTTP requests."""

from typing import Annotated

from fastapi import APIRouter, Header, Path, Query

from invoice_intelligence.api.dependencies import (
    EvaluationJobServiceDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.api.schemas.evaluations import (
    CreateEvaluationJobRequest,
    CreateScheduleRequest,
    CreateSnapshotRequest,
    CreateSuiteEvaluationJobRequest,
    EvaluationJobResponse,
    ScheduleResponse,
    SnapshotResponse,
)
from invoice_intelligence.application.services.evaluation_jobs import (
    CreateEvaluationJobCommand,
    CreateEvaluationScheduleCommand,
    CreateSnapshotCommand,
    CreateSuiteEvaluationJobCommand,
)
from invoice_intelligence.domain.evaluation_jobs import (
    DatasetSnapshot,
    EvaluationJob,
    EvaluationSchedule,
    SnapshotCase,
)

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


def _snapshot(value: DatasetSnapshot) -> SnapshotResponse:
    return SnapshotResponse(
        snapshot_id=value.snapshot_id, dataset_version=value.dataset_version,
        schema_version=value.schema_version, case_count=len(value.cases),
        content_sha256=value.content_sha256, created_at=value.created_at,
    )


def _job(value: EvaluationJob) -> EvaluationJobResponse:
    return EvaluationJobResponse(
        job_id=value.job_id, snapshot_id=value.snapshot_id,
        evidence_class=value.evidence_class,
        dataset_id=value.dataset_id, suite=value.suite,
        retrieval_policy_version=value.retrieval_policy_version,
        catalog_version=value.catalog_version,
        admission_policy_version=value.admission_policy_version,
        field_binding_policy_version=value.field_binding_policy_version,
        evaluation_run_id=value.evaluation_run_id,
        dataset_version=value.dataset_version, schema_version=value.schema_version,
        index_version=value.index_version, model_version=value.model_version,
        prompt_version=value.prompt_version, threshold_version=value.threshold_version,
        status=value.status.value, attempt_count=value.attempt_count,
        failure_code=value.failure_code, report=value.report,
        created_at=value.created_at, updated_at=value.updated_at,
    )


def _schedule(value: EvaluationSchedule) -> ScheduleResponse:
    return ScheduleResponse(
        schedule_id=value.schedule_id, snapshot_id=value.snapshot_id,
        interval_seconds=value.interval_seconds, next_run_at=value.next_run_at,
        enabled=value.enabled,
    )


@router.post("/snapshots", response_model=SnapshotResponse, responses=STANDARD_ERROR_RESPONSES)
async def create_snapshot(
    request: CreateSnapshotRequest,
    service: EvaluationJobServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> SnapshotResponse:
    value = await service.create_snapshot(
        context, CreateSnapshotCommand(
            dataset_version=request.dataset_version,
            schema_version=request.schema_version,
            cases=tuple(SnapshotCase(**case.model_dump()) for case in request.cases),
        ), idempotency_key=idempotency_key,
    )
    return _snapshot(value)


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotResponse, responses=STANDARD_ERROR_RESPONSES)
async def get_snapshot(
    snapshot_id: Annotated[str, Path(min_length=1, max_length=64)],
    service: EvaluationJobServiceDependency,
    context: TrustedTenantContextDependency,
) -> SnapshotResponse:
    return _snapshot(await service.get_snapshot(context, snapshot_id))


@router.post("/jobs", response_model=EvaluationJobResponse, responses=STANDARD_ERROR_RESPONSES)
async def create_job(
    request: CreateEvaluationJobRequest,
    service: EvaluationJobServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> EvaluationJobResponse:
    return _job(await service.create_job(
        context, CreateEvaluationJobCommand(**request.model_dump()),
        idempotency_key=idempotency_key,
    ))


@router.get("/jobs", response_model=list[EvaluationJobResponse], responses=STANDARD_ERROR_RESPONSES)
async def list_jobs(
    service: EvaluationJobServiceDependency,
    context: TrustedTenantContextDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EvaluationJobResponse]:
    return [_job(value) for value in await service.list_jobs(context, limit=limit, offset=offset)]


@router.post(
    "/suite-jobs",
    response_model=EvaluationJobResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def create_suite_job(
    request: CreateSuiteEvaluationJobRequest,
    service: EvaluationJobServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> EvaluationJobResponse:
    return _job(await service.create_suite_job(
        context, CreateSuiteEvaluationJobCommand(**request.model_dump()),
        idempotency_key=idempotency_key,
    ))


@router.get("/jobs/{job_id}", response_model=EvaluationJobResponse, responses=STANDARD_ERROR_RESPONSES)
async def get_job(
    job_id: Annotated[str, Path(min_length=1, max_length=64)],
    service: EvaluationJobServiceDependency,
    context: TrustedTenantContextDependency,
) -> EvaluationJobResponse:
    return _job(await service.get_job(context, job_id))


@router.post("/schedules", response_model=ScheduleResponse, responses=STANDARD_ERROR_RESPONSES)
async def create_schedule(
    request: CreateScheduleRequest,
    service: EvaluationJobServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> ScheduleResponse:
    return _schedule(await service.create_schedule(
        context, CreateEvaluationScheduleCommand(**request.model_dump()),
        idempotency_key=idempotency_key,
    ))


@router.get("/schedules", response_model=list[ScheduleResponse], responses=STANDARD_ERROR_RESPONSES)
async def list_schedules(
    service: EvaluationJobServiceDependency,
    context: TrustedTenantContextDependency,
) -> list[ScheduleResponse]:
    return [_schedule(value) for value in await service.list_schedules(context)]


@router.post("/schedules/{schedule_id}/disable", status_code=204, responses=STANDARD_ERROR_RESPONSES)
async def disable_schedule(
    schedule_id: Annotated[str, Path(min_length=1, max_length=64)],
    service: EvaluationJobServiceDependency,
    context: TrustedTenantContextDependency,
) -> None:
    await service.disable_schedule(context, schedule_id)
