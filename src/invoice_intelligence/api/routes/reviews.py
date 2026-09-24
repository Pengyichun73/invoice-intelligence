"""Tenant-scoped human-review task protocol adapters."""

import base64
import json
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Header, Path, Query

from invoice_intelligence.api.dependencies import (
    ReviewTaskServiceDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.presenters import present_run
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.api.schemas.reviews import (
    ReviewTaskCancelRequest,
    ReviewTaskClaimRequest,
    ReviewTaskDetailResponse,
    ReviewTaskLeaseRequest,
    ReviewTaskListResponse,
    ReviewTaskMutationResponse,
    ReviewTaskReassignRequest,
    ReviewTaskRecoveryResponse,
    ReviewTaskSubmissionResponse,
    ReviewTaskSubmitRequest,
    ReviewTaskSummaryResponse,
)
from invoice_intelligence.api.schemas.workflows import (
    ExtractionResultResponse,
    ExtractionRunResponse,
    HumanCorrectionRequest,
    ReviewRequestResponse,
)
from invoice_intelligence.application.errors import BadRequestError
from invoice_intelligence.domain.field_semantics import FieldBindingReviewDecision
from invoice_intelligence.domain.json_types import JsonValue
from invoice_intelligence.domain.review_tasks import (
    ReviewTask,
    ReviewTaskCursor,
    ReviewTaskFilter,
    ReviewTaskStatus,
)
from invoice_intelligence.domain.workflow import FieldReviewDecision, HumanCorrection

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("", response_model=ReviewTaskListResponse, responses=STANDARD_ERROR_RESPONSES)
async def list_reviews(
    service: ReviewTaskServiceDependency,
    context: TrustedTenantContextDependency,
    reviewer_id: Annotated[str | None, Query(max_length=128)] = None,
    priority: Annotated[int | None, Query(ge=0, le=100)] = None,
    status: ReviewTaskStatus | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> ReviewTaskListResponse:
    page = await service.list_tasks(
        context,
        ReviewTaskFilter(
            reviewer_id=reviewer_id,
            priority=priority,
            status=status,
            created_from=created_from,
            created_to=created_to,
        ),
        limit=limit,
        cursor=_decode_cursor(cursor),
    )
    return ReviewTaskListResponse(
        items=tuple(_summary(item) for item in page.items),
        next_cursor=_encode_cursor(page.next_cursor),
    )


@router.post(
    "/recover-expired",
    response_model=ReviewTaskRecoveryResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def recover_expired_reviews(
    service: ReviewTaskServiceDependency,
    context: TrustedTenantContextDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ReviewTaskRecoveryResponse:
    items = await service.recover_expired(context, limit=limit)
    return ReviewTaskRecoveryResponse(
        recovered_count=len(items),
        items=tuple(_summary(item) for item in items),
    )


@router.get(
    "/{identifier}",
    response_model=ReviewTaskDetailResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_review(
    identifier: Annotated[str, Path(min_length=1)],
    service: ReviewTaskServiceDependency,
    context: TrustedTenantContextDependency,
) -> ReviewTaskDetailResponse:
    return await _detail(service, context, await service.get_task(context, identifier))


@router.post(
    "/{identifier}/claim",
    response_model=ReviewTaskMutationResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def claim_review(
    identifier: Annotated[str, Path(min_length=1)],
    request: ReviewTaskClaimRequest,
    service: ReviewTaskServiceDependency,
    context: TrustedTenantContextDependency,
) -> ReviewTaskMutationResponse:
    task = await service.claim(
        context,
        identifier,
        expected_revision=request.expected_revision,
        lease_seconds=request.lease_seconds,
    )
    return ReviewTaskMutationResponse(
        task=await _detail(service, context, task),
        lease_token=task.lease_token,
    )


@router.post(
    "/{identifier}/release",
    response_model=ReviewTaskMutationResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def release_review(
    identifier: Annotated[str, Path(min_length=1)],
    request: ReviewTaskLeaseRequest,
    service: ReviewTaskServiceDependency,
    context: TrustedTenantContextDependency,
) -> ReviewTaskMutationResponse:
    task = await service.release(
        context,
        identifier,
        expected_revision=request.expected_revision,
        lease_token=request.lease_token,
    )
    return ReviewTaskMutationResponse(task=await _detail(service, context, task))


@router.post(
    "/{identifier}/reassign",
    response_model=ReviewTaskMutationResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def reassign_review(
    identifier: Annotated[str, Path(min_length=1)],
    request: ReviewTaskReassignRequest,
    service: ReviewTaskServiceDependency,
    context: TrustedTenantContextDependency,
) -> ReviewTaskMutationResponse:
    task = await service.reassign(
        context,
        identifier,
        target_reviewer_id=request.target_reviewer_id,
        expected_revision=request.expected_revision,
        lease_token=request.lease_token,
    )
    return ReviewTaskMutationResponse(task=await _detail(service, context, task))


@router.post(
    "/{identifier}/cancel",
    response_model=ReviewTaskMutationResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def cancel_review(
    identifier: Annotated[str, Path(min_length=1)],
    request: ReviewTaskCancelRequest,
    service: ReviewTaskServiceDependency,
    context: TrustedTenantContextDependency,
) -> ReviewTaskMutationResponse:
    task = await service.cancel(
        context,
        identifier,
        expected_revision=request.expected_revision,
        reason=request.reason,
    )
    return ReviewTaskMutationResponse(task=await _detail(service, context, task))


@router.post(
    "/{identifier}/submit",
    response_model=ReviewTaskSubmissionResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def submit_claimed_review(
    identifier: Annotated[str, Path(min_length=1)],
    request: ReviewTaskSubmitRequest,
    service: ReviewTaskServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> ReviewTaskSubmissionResponse:
    result = await service.submit(
        context,
        identifier,
        _correction_from_request(request.correction, reviewer_id=context.actor_id),
        expected_revision=request.expected_revision,
        lease_token=request.lease_token,
        idempotency_key=idempotency_key,
    )
    return ReviewTaskSubmissionResponse(
        task=await _detail(service, context, result.task),
        run=present_run(result.run),
    )


@router.post(
    "/{run_id}",
    response_model=ExtractionRunResponse,
    responses=STANDARD_ERROR_RESPONSES,
    deprecated=True,
)
async def submit_review_legacy(
    run_id: Annotated[str, Path(min_length=1)],
    request: HumanCorrectionRequest,
    service: ReviewTaskServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> ExtractionRunResponse:
    result = await service.submit_legacy(
        context,
        run_id,
        _correction_from_request(request, reviewer_id=context.actor_id),
        idempotency_key=idempotency_key,
    )
    return present_run(result.run)


async def _detail(
    service: ReviewTaskServiceDependency,
    context: TrustedTenantContextDependency,
    task: ReviewTask,
) -> ReviewTaskDetailResponse:
    payload = await service.get_current_extraction(context, task)
    current_invoice = (
        ExtractionResultResponse.model_validate(payload).invoice if payload is not None else None
    )
    return ReviewTaskDetailResponse(
        **_summary(task).model_dump(),
        request=ReviewRequestResponse.model_validate(task.request_payload),
        current_invoice=current_invoice,
    )


def _summary(task: ReviewTask) -> ReviewTaskSummaryResponse:
    return ReviewTaskSummaryResponse(
        review_id=task.review_id,
        run_id=task.run_id,
        status=task.status,
        priority=task.priority,
        assigned_reviewer_id=task.assigned_reviewer_id,
        lease_expires_at=task.lease_expires_at,
        revision=task.revision,
        request_version=task.request_version,
        created_at=task.created_at,
        updated_at=task.updated_at,
        submitted_at=task.submitted_at,
        cancelled_at=task.cancelled_at,
        cancel_reason=task.cancel_reason,
    )


def _correction_from_request(
    request: HumanCorrectionRequest, *, reviewer_id: str
) -> HumanCorrection:
    corrected_data = (
        cast(dict[str, JsonValue], request.corrected_invoice.model_dump(mode="json"))
        if request.corrected_invoice is not None
        else None
    )
    return HumanCorrection(
        corrected_invoice=corrected_data,
        fields=tuple(
            FieldReviewDecision(
                field_path=item.field_path,
                action=item.action,
                reason=item.reason,
                rejected_value=item.rejected_value,
            )
            for item in request.fields
        ),
        reviewer_id=reviewer_id,
        document_type=request.document_type,
        field_bindings=tuple(
            FieldBindingReviewDecision(
                evidence_id=item.evidence_id,
                selected_canonical_field_path=item.selected_canonical_field_path,
                reason=item.reason,
            )
            for item in request.field_bindings
        ),
    )


def _encode_cursor(cursor: ReviewTaskCursor | None) -> str | None:
    if cursor is None:
        return None
    raw = json.dumps(
        {"created_at": cursor.created_at.isoformat(), "review_id": cursor.review_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> ReviewTaskCursor | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        return ReviewTaskCursor(
            created_at=datetime.fromisoformat(payload["created_at"]),
            review_id=str(payload["review_id"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BadRequestError("Review task cursor is invalid") from exc
