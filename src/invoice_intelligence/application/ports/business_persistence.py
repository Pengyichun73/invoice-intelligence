"""Business read models and idempotency persistence boundaries."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from invoice_intelligence.application.ports.memory import MemoryRecoveryStatus
from invoice_intelligence.domain.json_types import JsonValue
from invoice_intelligence.domain.review_tasks import ReviewTaskStatus
from invoice_intelligence.domain.workflow import (
    ValidationRoute,
    WorkflowIdentity,
    WorkflowStatus,
)


class IdempotencyStatus(StrEnum):
    """Lifecycle of one protocol-level idempotent operation."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ExtractionRunRecord:
    """Stable business view of one extraction run."""

    identity: WorkflowIdentity
    status: WorkflowStatus
    validation_route: ValidationRoute | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime
    memory_status: MemoryRecoveryStatus | None = None
    memory_trace_id: str | None = None
    memory_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionResultRecord:
    """Stable persisted result view independent of ORM models."""

    run_id: str
    document_id: str
    payload: dict[str, JsonValue]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewTaskRecord:
    """Current review task exposed through the application layer."""

    run_id: str
    status: ReviewTaskStatus
    request_payload: dict[str, JsonValue]
    version: int
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """One operation/key binding and its immutable request fingerprint."""

    operation: str
    key: str
    request_hash: str
    resource_id: str
    status: IdempotencyStatus
    response_payload: dict[str, JsonValue] | None


class BusinessQueryRepository(Protocol):
    """Read business state without consulting LangGraph checkpoints."""

    async def get_run(
        self,
        run_id: str,
        tenant_id: str,
    ) -> ExtractionRunRecord | None:
        """Return one run by its business identifier."""

        ...

    async def get_result(
        self,
        run_id: str,
        tenant_id: str,
    ) -> ExtractionResultRecord | None:
        """Return a completed structured result when present."""

        ...

    async def get_review(
        self,
        run_id: str,
        tenant_id: str,
    ) -> ReviewTaskRecord | None:
        """Return the current persisted review task when present."""

        ...


class IdempotencyRepository(Protocol):
    """Atomically bind protocol idempotency keys to request fingerprints."""

    async def get_idempotency(
        self,
        operation: str,
        key: str,
    ) -> IdempotencyRecord | None:
        """Read an existing idempotency binding."""

        ...

    async def claim_idempotency(
        self,
        operation: str,
        key: str,
        request_hash: str,
        resource_id: str,
    ) -> IdempotencyRecord:
        """Create a binding or return the identical existing binding."""

        ...

    async def complete_idempotency(
        self,
        operation: str,
        key: str,
        request_hash: str,
        response_payload: dict[str, JsonValue],
    ) -> None:
        """Idempotently mark a matching operation complete."""

        ...

    async def release_idempotency(
        self,
        operation: str,
        key: str,
        request_hash: str,
        resource_id: str,
    ) -> bool:
        """Release only the caller-owned in-progress claim after a retryable failure."""

        ...
