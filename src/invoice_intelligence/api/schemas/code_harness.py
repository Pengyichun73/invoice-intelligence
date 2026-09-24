"""HTTP contracts for bounded code generation and self-healing tasks."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from invoice_intelligence.code_harness.domain.errors import HarnessErrorCode
from invoice_intelligence.code_harness.domain.execution import (
    ExecutionBudget,
    HarnessTaskStatus,
)
from invoice_intelligence.code_harness.domain.patch_model import (
    PatchOperation,
    PatchOperationKind,
    PatchProposal,
)
from invoice_intelligence.code_harness.domain.versions import ExecutionVersionBinding


class ExecutionBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(default=3, gt=0, le=20)
    max_wall_time_seconds: int = Field(default=900, gt=0, le=86_400)
    max_patch_bytes: int = Field(default=256_000, gt=0, le=10_000_000)
    max_changed_files: int = Field(default=20, gt=0, le=500)
    max_changed_lines: int = Field(default=1_000, gt=0, le=100_000)
    max_patch_operations: int = Field(default=100, gt=0, le=1_000)
    max_model_tokens: int = Field(default=16_000, gt=0, le=1_000_000)

    def to_domain(self) -> ExecutionBudget:
        return ExecutionBudget(**self.model_dump())


class ExecutionVersionBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_version: str = Field(min_length=1, max_length=256)
    snapshot_version: str = Field(min_length=1, max_length=256)
    schema_version: str = Field(min_length=1, max_length=256)
    parser_version: str = Field(min_length=1, max_length=256)
    grammar_version: str = Field(min_length=1, max_length=256)
    redaction_version: str = Field(min_length=1, max_length=256)
    index_version: str = Field(min_length=1, max_length=256)
    model_version: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=256)
    patch_policy_version: str = Field(min_length=1, max_length=256)
    sandbox_policy_version: str = Field(min_length=1, max_length=256)
    watchdog_version: str = Field(min_length=1, max_length=256)

    @field_validator("*")
    @classmethod
    def require_normalized_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("version values must be normalized")
        return value

    def to_domain(self) -> ExecutionVersionBinding:
        return ExecutionVersionBinding(**self.model_dump())


class CreateHarnessTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_id: str = Field(min_length=1, max_length=256)
    request_fingerprint: str = Field(min_length=1, max_length=256)
    versions: ExecutionVersionBindingRequest
    budget: ExecutionBudgetRequest | None = None

    @field_validator("repository_id", "request_fingerprint")
    @classmethod
    def require_normalized_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("request values must be normalized")
        return value


class ResumeHarnessTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: int = Field(gt=0)


class PatchOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=4096)
    kind: str = Field(min_length=1, max_length=32)
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    payload_utf8: str = Field(max_length=1_000_000)
    base_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    anchor: str | None = Field(default=None, max_length=512)

    @field_validator("path", "kind", "base_checksum_sha256")
    @classmethod
    def require_normalized_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("patch values must be normalized")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "PatchOperationRequest":
        if self.end_byte < self.start_byte:
            raise ValueError("patch byte range is invalid")
        return self

    def to_domain(self) -> PatchOperation:
        return PatchOperation(
            path=self.path,
            kind=PatchOperationKind(self.kind),
            start_byte=self.start_byte,
            end_byte=self.end_byte,
            payload_utf8=self.payload_utf8.encode("utf-8"),
            base_checksum_sha256=self.base_checksum_sha256,
            anchor=self.anchor,
        )


class PatchProposalRequest(BaseModel):
    """Strict JSON boundary for a model-generated Patch Proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    patch_id: str = Field(min_length=1, max_length=512)
    tenant_id: str = Field(min_length=1, max_length=256)
    repository_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=512)
    snapshot_revision: int = Field(gt=0)
    operations: tuple[PatchOperationRequest, ...] = Field(min_length=1, max_length=100)
    patch_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprint: str = Field(min_length=1, max_length=512)

    @field_validator(
        "patch_id",
        "tenant_id",
        "repository_id",
        "snapshot_id",
        "fingerprint",
    )
    @classmethod
    def require_normalized_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("patch values must be normalized")
        return value

    def to_domain(self) -> PatchProposal:
        return PatchProposal(
            patch_id=self.patch_id,
            tenant_id=self.tenant_id,
            repository_id=self.repository_id,
            snapshot_id=self.snapshot_id,
            snapshot_revision=self.snapshot_revision,
            operations=tuple(item.to_domain() for item in self.operations),
            patch_checksum_sha256=self.patch_checksum_sha256,
            fingerprint=self.fingerprint,
        )


class HarnessTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    repository_id: str
    status: HarnessTaskStatus
    revision: int
    attempt_count: int
    versions: dict[str, str]
    budget: ExecutionBudgetRequest
    failure_code: HarnessErrorCode | None
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime | None
