"""FastAPI adapter for training intent registration and observation."""

from typing import Annotated

from fastapi import APIRouter, Header, Path

from invoice_intelligence.api.dependencies import (
    TrainingJobServiceDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.api.schemas.training import (
    CancelTrainingJobRequest,
    CreateTrainingJobRequest,
    TrainingJobResponse,
)
from invoice_intelligence.application.services.training_jobs import (
    CreateTrainingJobCommand,
)
from invoice_intelligence.domain.training_registry import TrainingJob

router = APIRouter(prefix="/training/jobs", tags=["training"])


@router.post("", response_model=TrainingJobResponse, responses=STANDARD_ERROR_RESPONSES)
async def create_training_job(
    request: CreateTrainingJobRequest,
    service: TrainingJobServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> TrainingJobResponse:
    job = await service.create(
        context,
        CreateTrainingJobCommand(**request.model_dump()),
        idempotency_key=idempotency_key,
    )
    return _response(job)


@router.get(
    "/{job_id}", response_model=TrainingJobResponse, responses=STANDARD_ERROR_RESPONSES
)
async def get_training_job(
    job_id: Annotated[str, Path(min_length=1, max_length=64)],
    service: TrainingJobServiceDependency,
    context: TrustedTenantContextDependency,
) -> TrainingJobResponse:
    return _response(await service.get(context, job_id))


@router.post(
    "/{job_id}/cancel",
    response_model=TrainingJobResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def cancel_training_job(
    job_id: Annotated[str, Path(min_length=1, max_length=64)],
    request: CancelTrainingJobRequest,
    service: TrainingJobServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> TrainingJobResponse:
    return _response(
        await service.cancel(
            context, job_id, reason=request.reason, idempotency_key=idempotency_key
        )
    )


@router.post(
    "/{job_id}/retry",
    response_model=TrainingJobResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def retry_training_job(
    job_id: Annotated[str, Path(min_length=1, max_length=64)],
    service: TrainingJobServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> TrainingJobResponse:
    return _response(
        await service.retry(context, job_id, idempotency_key=idempotency_key)
    )


def _response(job: TrainingJob) -> TrainingJobResponse:
    return TrainingJobResponse(
        job_id=job.job_id,
        status=job.status,
        dataset_export_id=job.dataset_export_id,
        dataset_version=job.dataset_version,
        schema_version=job.schema_version,
        index_version=job.index_version,
        model_version=job.model_version,
        prompt_version=job.prompt_version,
        provider=job.provider,
        target_type=job.target_type,
        training_run_id=job.training_run_id,
        remote_job_id=job.remote_job_id,
        retry_of_job_id=job.retry_of_job_id,
        cancel_requested_at=job.cancel_requested_at,
        claim_count=job.claim_count,
        failure_attempt_count=job.failure_attempt_count,
        failure_code=job.failure_code,
        revision=job.revision,
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
    )
