"""FastAPI adapter for bounded Harness task orchestration."""

from typing import Annotated

from fastapi import APIRouter, Header, Path

from invoice_intelligence.api.dependencies import (
    CodeHarnessServiceDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.schemas.code_harness import (
    CreateHarnessTaskRequest,
    ExecutionBudgetRequest,
    HarnessTaskResponse,
    ResumeHarnessTaskRequest,
)
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.code_harness.domain.execution import HarnessTask
from invoice_intelligence.code_harness.domain.tenant_boundary import HarnessTenantContext
from invoice_intelligence.domain.governance import TrustedTenantContext

router = APIRouter(prefix="/code-harness/tasks", tags=["code-harness"])


@router.post("", response_model=HarnessTaskResponse, responses=STANDARD_ERROR_RESPONSES)
async def create_harness_task(
    request: CreateHarnessTaskRequest,
    service: CodeHarnessServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
    ],
) -> HarnessTaskResponse:
    task = await service.create_task(
        context=_harness_context(context),
        repository_id=request.repository_id,
        request_fingerprint=request.request_fingerprint,
        versions=request.versions.to_domain(),
        budget=request.budget.to_domain() if request.budget is not None else None,
        idempotency_key=idempotency_key,
    )
    return _response(task)


@router.get(
    "/{task_id}",
    response_model=HarnessTaskResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_harness_task(
    task_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: CodeHarnessServiceDependency,
    context: TrustedTenantContextDependency,
) -> HarnessTaskResponse:
    task = await service.get_task(
        context=_harness_context(context),
        task_id=task_id,
    )
    return _response(task)


@router.post(
    "/{task_id}/resume",
    response_model=HarnessTaskResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def resume_harness_task(
    task_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: ResumeHarnessTaskRequest,
    service: CodeHarnessServiceDependency,
    context: TrustedTenantContextDependency,
) -> HarnessTaskResponse:
    task = await service.resume_task(
        context=_harness_context(context),
        task_id=task_id,
        expected_revision=request.expected_revision,
    )
    return _response(task)


def _harness_context(context: TrustedTenantContext) -> HarnessTenantContext:
    return HarnessTenantContext(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        trace_id=context.trace_id,
    )


def _response(task: HarnessTask) -> HarnessTaskResponse:
    return HarnessTaskResponse(
        task_id=task.task_id,
        repository_id=task.repository_id,
        status=task.status,
        revision=task.revision,
        attempt_count=task.attempt_count,
        versions=task.versions.as_dict(),
        budget=ExecutionBudgetRequest(
            **{name: getattr(task.budget, name) for name in task.budget.__dataclass_fields__}
        ),
        failure_code=task.failure_code,
        created_at=task.created_at,
        updated_at=task.updated_at,
        next_attempt_at=task.next_attempt_at,
    )
