"""HTTP contracts for production human-review task operations."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from invoice_intelligence.api.schemas.workflows import (
    ExtractionRunResponse,
    HumanCorrectionRequest,
    ReviewRequestResponse,
)
from invoice_intelligence.domain.invoice import InvoiceExtraction
from invoice_intelligence.domain.review_tasks import ReviewTaskStatus


class ReviewTaskSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    run_id: str
    status: ReviewTaskStatus
    priority: int
    assigned_reviewer_id: str | None
    lease_expires_at: datetime | None
    revision: int
    request_version: int
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None


class ReviewTaskDetailResponse(ReviewTaskSummaryResponse):
    request: ReviewRequestResponse
    current_invoice: InvoiceExtraction | None = None


class ReviewTaskListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: tuple[ReviewTaskSummaryResponse, ...]
    next_cursor: str | None


class ReviewTaskRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_revision: int = Field(ge=1)


class ReviewTaskClaimRequest(ReviewTaskRevisionRequest):
    lease_seconds: int | None = Field(default=None, ge=1, le=86_400)


class ReviewTaskLeaseRequest(ReviewTaskRevisionRequest):
    lease_token: str = Field(min_length=64, max_length=64)


class ReviewTaskReassignRequest(ReviewTaskLeaseRequest):
    target_reviewer_id: str = Field(min_length=1, max_length=128)

    @field_validator("target_reviewer_id")
    @classmethod
    def normalize_target_reviewer(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("target_reviewer_id must not be blank")
        return normalized


class ReviewTaskCancelRequest(ReviewTaskRevisionRequest):
    reason: str = Field(min_length=1, max_length=512)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class ReviewTaskSubmitRequest(ReviewTaskLeaseRequest):
    correction: HumanCorrectionRequest


class ReviewTaskMutationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task: ReviewTaskDetailResponse
    lease_token: str | None = None


class ReviewTaskSubmissionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task: ReviewTaskDetailResponse
    run: ExtractionRunResponse
    execution_status: Literal["queued", "finished"] = "finished"


class ReviewTaskRecoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    recovered_count: int
    items: tuple[ReviewTaskSummaryResponse, ...]
