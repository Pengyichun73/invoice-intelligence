"""Framework-independent training control-plane facts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TrainingJobStatus(StrEnum):
    PLANNED = "planned"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    QUARANTINED = "quarantined"


TERMINAL_TRAINING_JOB_STATUSES = frozenset(
    {
        TrainingJobStatus.SUCCEEDED,
        TrainingJobStatus.FAILED,
        TrainingJobStatus.CANCELLED,
        TrainingJobStatus.QUARANTINED,
    }
)

ALLOWED_TRAINING_JOB_TRANSITIONS = {
    TrainingJobStatus.PLANNED: frozenset(
        {
            TrainingJobStatus.SUBMITTED,
            TrainingJobStatus.CANCELLED,
            TrainingJobStatus.FAILED,
            TrainingJobStatus.QUARANTINED,
        }
    ),
    TrainingJobStatus.SUBMITTED: frozenset(
        {
            TrainingJobStatus.RUNNING,
            TrainingJobStatus.SUCCEEDED,
            TrainingJobStatus.FAILED,
            TrainingJobStatus.CANCELLED,
            TrainingJobStatus.QUARANTINED,
        }
    ),
    TrainingJobStatus.RUNNING: frozenset(
        {
            TrainingJobStatus.SUCCEEDED,
            TrainingJobStatus.FAILED,
            TrainingJobStatus.CANCELLED,
            TrainingJobStatus.QUARANTINED,
        }
    ),
    TrainingJobStatus.SUCCEEDED: frozenset(),
    TrainingJobStatus.FAILED: frozenset(),
    TrainingJobStatus.CANCELLED: frozenset(),
    TrainingJobStatus.QUARANTINED: frozenset(),
}


class TrainingProviderJobStatus(StrEnum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TrainingDatasetExport:
    export_id: str
    tenant_scope: str
    dataset_id: str
    dataset_version: str
    schema_version: str
    manifest_uri: str
    manifest_checksum_sha256: str
    record_count: int
    positive_count: int
    hard_negative_count: int
    created_by: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_text_fields(self)
        _require_sha256(self.manifest_checksum_sha256)
        if self.record_count <= 0 or self.positive_count <= 0:
            raise ValueError("Training dataset export requires positive records")
        if self.hard_negative_count < 0:
            raise ValueError("Hard-negative count cannot be negative")
        _require_aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class TrainingJob:
    job_id: str
    tenant_id: str
    dataset_export_id: str
    tenant_scope: str
    dataset_id: str
    dataset_version: str
    schema_version: str
    index_version: str
    model_version: str
    prompt_version: str
    provider: str
    target_type: str
    base_model: str
    base_model_version: str
    code_version: str
    training_run_id: str
    status: TrainingJobStatus
    requested_by: str
    created_at: datetime
    updated_at: datetime
    revision: int = 1
    remote_job_id: str | None = None
    retry_of_job_id: str | None = None
    cancel_requested_at: datetime | None = None
    cancel_requested_by: str | None = None
    cancel_reason: str | None = None
    claim_count: int = 0
    failure_attempt_count: int = 0
    next_attempt_at: datetime | None = None
    worker_id: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    submitted_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: str | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        _require_text_fields(self, exclude={"cancel_reason", "failure_code", "trace_id"})
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("Training job updated_at cannot precede created_at")
        if self.revision < 1 or self.claim_count < 0 or self.failure_attempt_count < 0:
            raise ValueError("Training job counters are invalid")
        for name in (
            "cancel_requested_at",
            "next_attempt_at",
            "lease_expires_at",
            "submitted_at",
            "started_at",
            "completed_at",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_aware(value, name)
        cancellation = (
            self.cancel_requested_at,
            self.cancel_requested_by,
            self.cancel_reason,
        )
        if any(value is not None for value in cancellation) and not all(
            value is not None for value in cancellation
        ):
            raise ValueError("Cancellation request fields must be set together")
        lease = (self.worker_id, self.lease_token, self.lease_expires_at)
        if any(value is not None for value in lease) and not all(
            value is not None for value in lease
        ):
            raise ValueError("Training lease fields must be set together")
        if self.status in TERMINAL_TRAINING_JOB_STATUSES and self.completed_at is None:
            raise ValueError("Terminal training jobs require completed_at")
        if self.status not in TERMINAL_TRAINING_JOB_STATUSES and self.completed_at is not None:
            raise ValueError("Non-terminal training jobs cannot have completed_at")
        if self.status in {TrainingJobStatus.FAILED, TrainingJobStatus.QUARANTINED}:
            if not self.failure_code:
                raise ValueError("Failed or quarantined jobs require a safe failure code")
        elif self.failure_code is not None:
            raise ValueError("Only failed or quarantined jobs may have a failure code")

    def can_transition_to(self, target: TrainingJobStatus) -> bool:
        return target is self.status or target in ALLOWED_TRAINING_JOB_TRANSITIONS[self.status]


@dataclass(frozen=True, slots=True)
class TrainingJobLease:
    job: TrainingJob
    worker_id: str
    lease_token: str
    revision: int


@dataclass(frozen=True, slots=True)
class TrainingProviderRequest:
    job_id: str
    tenant_id: str
    target_type: str
    base_model: str
    base_model_version: str
    dataset_manifest_uri: str
    dataset_manifest_checksum_sha256: str
    dataset_version: str
    schema_version: str
    index_version: str
    model_version: str
    prompt_version: str
    code_version: str

    def __post_init__(self) -> None:
        _require_text_fields(self)
        _require_sha256(self.dataset_manifest_checksum_sha256)


@dataclass(frozen=True, slots=True)
class TrainingProviderSnapshot:
    provider_job_id: str
    status: TrainingProviderJobStatus
    updated_at: datetime
    artifact_uri: str | None = None
    artifact_checksum_sha256: str | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.provider_job_id, "provider_job_id")
        _require_aware(self.updated_at, "updated_at")
        if self.status is TrainingProviderJobStatus.SUCCEEDED:
            if not self.artifact_uri or not self.artifact_checksum_sha256:
                raise ValueError("Successful provider jobs require artifact metadata")
            _require_sha256(self.artifact_checksum_sha256)
        elif self.artifact_uri is not None or self.artifact_checksum_sha256 is not None:
            raise ValueError("Only successful provider jobs may expose artifacts")
        if self.status is TrainingProviderJobStatus.FAILED and not self.failure_code:
            raise ValueError("Failed provider jobs require a safe failure code")


@dataclass(frozen=True, slots=True)
class TrainingArtifact:
    artifact_id: str
    tenant_id: str
    job_id: str
    training_run_id: str
    remote_job_id: str
    provider: str
    source_uri: str
    checksum_sha256: str
    size_bytes: int
    dataset_id: str
    dataset_version: str
    dataset_manifest_checksum_sha256: str
    schema_version: str
    index_version: str
    model_version: str
    prompt_version: str
    code_version: str
    created_by: str
    created_at: datetime
    trace_id: str | None = None

    def __post_init__(self) -> None:
        _require_text_fields(self, exclude={"trace_id"})
        _require_sha256(self.checksum_sha256)
        _require_sha256(self.dataset_manifest_checksum_sha256)
        if self.size_bytes <= 0:
            raise ValueError("Training artifact must not be empty")
        _require_aware(self.created_at, "created_at")


def _require_text_fields(value: object, *, exclude: set[str] | None = None) -> None:
    excluded = exclude or set()
    for name in value.__dataclass_fields__:  # type: ignore[attr-defined]
        if name in excluded:
            continue
        field_value = getattr(value, name)
        if isinstance(field_value, str):
            _require_text(field_value, name)


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("Checksum must be a lowercase SHA-256 hex digest")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
