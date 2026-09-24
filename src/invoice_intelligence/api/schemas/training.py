"""HTTP contracts for Training Job registry operations."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from invoice_intelligence.domain.training import TrainingTargetType
from invoice_intelligence.domain.training_registry import TrainingJobStatus


class CreateTrainingJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_export_id: str = Field(min_length=1, max_length=64)
    tenant_scope: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    index_version: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=256)
    target_type: TrainingTargetType
    base_model: str = Field(min_length=1, max_length=256)
    base_model_version: str = Field(min_length=1, max_length=256)
    code_version: str = Field(min_length=1, max_length=128)
    manifest_uri: str = Field(min_length=1, max_length=1024)
    manifest_checksum_sha256: str = Field(min_length=64, max_length=64)
    record_count: int = Field(gt=0)
    positive_count: int = Field(gt=0)
    hard_negative_count: int = Field(ge=0)

    @field_validator("manifest_checksum_sha256")
    @classmethod
    def validate_checksum(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError("manifest checksum must be lowercase SHA-256")
        return value


class CancelTrainingJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reason: str = Field(min_length=1, max_length=512)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class TrainingJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    status: TrainingJobStatus
    dataset_export_id: str
    dataset_version: str
    schema_version: str
    index_version: str
    model_version: str
    prompt_version: str
    provider: str
    target_type: str
    training_run_id: str
    remote_job_id: str | None
    retry_of_job_id: str | None
    cancel_requested_at: datetime | None
    claim_count: int
    failure_attempt_count: int
    failure_code: str | None
    revision: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
