"""Harness task, attempt and execution facts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .errors import HarnessErrorCode, require_text
from .versions import ExecutionVersionBinding


class HarnessTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    REPAIR_PENDING = "repair_pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    QUARANTINED = "quarantined"


TERMINAL_TASK_STATUSES = frozenset(
    {HarnessTaskStatus.SUCCEEDED, HarnessTaskStatus.FAILED, HarnessTaskStatus.QUARANTINED}
)


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    max_attempts: int = 3
    max_wall_time_seconds: int = 900
    max_patch_bytes: int = 256_000
    max_changed_files: int = 20
    max_changed_lines: int = 1_000
    max_patch_operations: int = 100
    max_model_tokens: int = 16_000

    def __post_init__(self) -> None:
        values = (
            self.max_attempts,
            self.max_wall_time_seconds,
            self.max_patch_bytes,
            self.max_changed_files,
            self.max_changed_lines,
            self.max_patch_operations,
            self.max_model_tokens,
        )
        if any(value <= 0 for value in values):
            raise ValueError("execution budget values must be positive")


@dataclass(frozen=True, slots=True)
class HarnessTask:
    task_id: str
    tenant_id: str
    repository_id: str
    request_fingerprint: str
    status: HarnessTaskStatus
    revision: int
    attempt_count: int
    versions: ExecutionVersionBinding
    budget: ExecutionBudget
    failure_code: HarnessErrorCode | None
    created_at: datetime
    updated_at: datetime
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    next_attempt_at: datetime | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("task_id", "tenant_id", "repository_id", "request_fingerprint"):
            require_text(name, getattr(self, name))
        if self.trace_id is not None:
            require_text("trace_id", self.trace_id, max_length=128)
        if self.revision < 1 or self.attempt_count < 0:
            raise ValueError("task revision and attempt count are invalid")
        for name, value in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.next_attempt_at is not None and (
            self.next_attempt_at.tzinfo is None or self.next_attempt_at.utcoffset() is None
        ):
            raise ValueError("next_attempt_at must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.status in TERMINAL_TASK_STATUSES and self.failure_code is None and (
            self.status is not HarnessTaskStatus.SUCCEEDED
        ):
            raise ValueError("failed or quarantined tasks require a failure code")
        if self.status is HarnessTaskStatus.SUCCEEDED and self.failure_code is not None:
            raise ValueError("successful tasks cannot carry a failure code")
        lease = (self.lease_token, self.lease_expires_at)
        if any(value is not None for value in lease) and not all(value is not None for value in lease):
            raise ValueError("lease fields must be set together")


@dataclass(frozen=True, slots=True)
class HarnessAttempt:
    attempt_id: str
    task_id: str
    attempt_number: int
    revision: int
    worker_id: str
    lease_token: str
    started_at: datetime
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("attempt_id", "task_id", "worker_id", "lease_token"):
            require_text(name, getattr(self, name))
        if self.attempt_number < 1 or self.revision < 1:
            raise ValueError("attempt number and revision must be positive")
        for name, value in (("started_at", self.started_at), ("completed_at", self.completed_at)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
