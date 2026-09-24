from pydantic import BaseModel, ConfigDict, Field

from invoice_intelligence.domain.training import PromotionCandidateStatus


class PromotionCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: str
    artifact_id: str | None
    model_evaluation_id: str | None
    status: PromotionCandidateStatus
    revision: int
    dataset_version: str
    evaluation_run_id: str
    model_version: str
    prompt_version: str
    schema_version: str
    index_version: str
    threshold_version: str
    hard_failure_code: str | None
    compatibility_errors: tuple[str, ...]
    approved_by: str | None
    rejection_reason: str | None


class PromotionCandidateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: str = Field(min_length=1, max_length=128)
    evaluation_run_id: str = Field(min_length=1, max_length=128)
    artifact_id: str = Field(min_length=1, max_length=64)


class PromotionApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_revision: int = Field(ge=1)
    target_status: PromotionCandidateStatus


class PromotionRejectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_revision: int = Field(ge=1)
    reason: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class PromotionRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_revision: int = Field(ge=1)
    target_candidate_id: str = Field(min_length=1, max_length=128)
