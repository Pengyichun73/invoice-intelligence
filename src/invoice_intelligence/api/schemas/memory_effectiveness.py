"""Value-free memory effectiveness query responses."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class StageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: Literal["observed", "not_observed"]
    observed_count: int
    blocker_codes: list[str]
    count_scope: Literal["window", "current", "latest_run"]
    since: datetime | None
    until: datetime | None
    version: str | None
    evidence_at: datetime | None


class OperationalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_count: int
    extraction_run_count: int
    reviewed_example_count: int
    pending_admission_count: int
    approved_admission_count: int
    indexed_example_count: int
    retrieval_count: int
    nonempty_retrieval_count: int
    review_required_retrieval_count: int
    active_index_version: str | None
    latest_document_at: datetime | None
    latest_extraction_at: datetime | None
    latest_review_at: datetime | None
    latest_admission_at: datetime | None
    latest_projection_at: datetime | None
    latest_retrieval_at: datetime | None
    evaluation_job_count: int
    completed_evaluation_job_count: int
    latest_evaluation_job_at: datetime | None
    gold_case_count: int
    frozen_gold_case_count: int
    frozen_template_group_count: int
    latest_frozen_gold_at: datetime | None


class EffectivenessOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: datetime
    until: datetime
    operational: OperationalResponse
    stages: list[StageResponse]
    benefit_status: Literal["insufficient_evidence", "demonstrated", "not_demonstrated"]
    latest_benefit_run_id: str | None
    benefit_blocker_codes: list[str]


class EffectivenessStagesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: datetime
    until: datetime
    stages: list[StageResponse]


class EffectivenessScenariosResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str | None
    benefit_status: Literal["insufficient_evidence", "demonstrated", "not_demonstrated"]
    scenarios: list["EffectivenessScenarioResponse"]
    blocker_codes: list[str]


class BenefitMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    coverage_sufficient: bool
    review_reduction_vs_ocr: float
    correct_field_delta: int
    wrong_auto_passes: int
    p95_extra_ms: float


class EffectivenessScenarioResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: str
    value: str
    field_path: str | None
    sample_count: int
    review_reduction_vs_ocr: float
    correct_field_delta: int
    wrong_auto_passes: int
    p95_extra_ms: float
    passed: bool


class EffectivenessRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    status: Literal["pending", "running", "completed", "failed"]
    dataset_digest: str
    schema_version: str
    catalog_version: str
    index_version: str
    model_version: str
    prompt_version: str
    case_count: int
    template_group_count: int
    metrics: BenefitMetricsResponse | None
    scenarios: list[EffectivenessScenarioResponse]
    blocker_codes: list[str]
    created_at: datetime
    completed_at: datetime | None
