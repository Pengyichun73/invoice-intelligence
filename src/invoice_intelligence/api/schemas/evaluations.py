"""Safe HTTP contracts for offline evaluation snapshots and jobs."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from invoice_intelligence.domain.evaluation import EvaluationSuite


class SnapshotCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str = Field(min_length=1, max_length=64)
    field_path: str = Field(min_length=1, max_length=128)
    expected_present: bool
    predicted_present: bool
    field_correct: bool
    evidence_covered: bool
    amount_absolute_error: float | None = Field(default=None, ge=0, le=1_000_000)
    review_required: bool
    negative_false_recall: bool
    ocr_vision_consistent: bool


class CreateSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset_version: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    cases: tuple[SnapshotCaseRequest, ...] = Field(min_length=1, max_length=10_000)


class SnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: str
    dataset_version: str
    schema_version: str
    case_count: int
    content_sha256: str
    created_at: datetime


class CreateEvaluationJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot_id: str = Field(min_length=1, max_length=64)
    dataset_version: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    index_version: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=256)
    threshold_version: str = Field(min_length=1, max_length=128)


class CreateSuiteEvaluationJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=128)
    suite: EvaluationSuite
    index_version: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=256)
    retrieval_policy_version: str = Field(min_length=1, max_length=128)
    threshold_version: str = Field(min_length=1, max_length=128)
    catalog_version: str | None = Field(default=None, max_length=128)
    admission_policy_version: str | None = Field(default=None, max_length=128)
    field_binding_policy_version: str | None = Field(default=None, max_length=128)


class EvaluationJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    evidence_class: Literal["diagnostic_only", "suite_run"] = "diagnostic_only"
    snapshot_id: str | None
    dataset_id: str | None = None
    suite: EvaluationSuite | None = None
    retrieval_policy_version: str | None = None
    catalog_version: str | None = None
    admission_policy_version: str | None = None
    field_binding_policy_version: str | None = None
    evaluation_run_id: str | None = None
    dataset_version: str
    schema_version: str
    index_version: str
    model_version: str
    prompt_version: str
    threshold_version: str
    status: str
    attempt_count: int
    failure_code: str | None
    report: dict[str, object] | None
    created_at: datetime
    updated_at: datetime


class CreateScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot_id: str = Field(min_length=1, max_length=64)
    index_version: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=256)
    threshold_version: str = Field(min_length=1, max_length=128)
    interval_seconds: int = Field(ge=3600, le=31_536_000)


class ScheduleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule_id: str
    snapshot_id: str
    interval_seconds: int
    next_run_at: datetime
    enabled: bool
