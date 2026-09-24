"""Framework-independent human-review task lifecycle."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from invoice_intelligence.domain.json_types import JsonValue


class ReviewTaskStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    CLAIMED = "claimed"
    SUBMITTED = "submitted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ReviewTaskAuditAction(StrEnum):
    CLAIM = "claim"
    RELEASE = "release"
    REASSIGN = "reassign"
    SUBMIT = "submit"
    EXPIRE = "expire"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class ReviewTask:
    review_id: str
    tenant_id: str
    run_id: str
    status: ReviewTaskStatus
    request_payload: dict[str, JsonValue]
    request_version: int
    priority: int
    assigned_reviewer_id: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None

    def __post_init__(self) -> None:
        for name, value in (
            ("review_id", self.review_id),
            ("tenant_id", self.tenant_id),
            ("run_id", self.run_id),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        if self.request_version < 1 or self.revision < 1:
            raise ValueError("Review task versions must be positive")
        if not 0 <= self.priority <= 100:
            raise ValueError("Review task priority must be between 0 and 100")
        for timestamp in (self.created_at, self.updated_at):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("Review task timestamps must be timezone-aware")
        lease_values = (
            self.assigned_reviewer_id,
            self.lease_token,
            self.lease_expires_at,
        )
        if self.status is ReviewTaskStatus.CLAIMED:
            if any(value is None for value in lease_values):
                raise ValueError("Claimed review tasks require a complete lease")
        elif self.lease_token is not None or self.lease_expires_at is not None:
            raise ValueError("Only claimed review tasks may carry lease state")
        if self.lease_expires_at is not None and (
            self.lease_expires_at.tzinfo is None or self.lease_expires_at.utcoffset() is None
        ):
            raise ValueError("Review task lease expiry must be timezone-aware")
        if self.status is ReviewTaskStatus.SUBMITTED:
            if self.submitted_at is None:
                raise ValueError("Submitted review tasks require submitted_at")
        elif self.submitted_at is not None:
            raise ValueError("Only submitted review tasks may carry submitted_at")
        if self.status is ReviewTaskStatus.CANCELLED:
            if self.cancelled_at is None or self.cancel_reason is None:
                raise ValueError("Cancelled review tasks require time and reason")
        elif self.cancelled_at is not None or self.cancel_reason is not None:
            raise ValueError("Only cancelled review tasks may carry cancellation data")


@dataclass(frozen=True, slots=True)
class ReviewTaskFilter:
    reviewer_id: str | None = None
    priority: int | None = None
    status: ReviewTaskStatus | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReviewTaskCursor:
    created_at: datetime
    review_id: str


@dataclass(frozen=True, slots=True)
class ReviewTaskPage:
    items: tuple[ReviewTask, ...]
    next_cursor: ReviewTaskCursor | None


@dataclass(frozen=True, slots=True)
class ReviewTaskAudit:
    audit_id: str
    tenant_id: str
    review_id: str
    action: ReviewTaskAuditAction
    actor_id: str
    target_reviewer_id: str | None
    from_status: ReviewTaskStatus
    to_status: ReviewTaskStatus
    revision: int
    reason_code: str
    trace_id: str | None
    created_at: datetime
