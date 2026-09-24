from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from invoice_intelligence.domain.transactions import RiskAssessment, TransactionReviewStatus


class TransactionCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=64)


class TransactionReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: TransactionReviewStatus
    expected_revision: int = Field(ge=1)


class TransactionAssessmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    assessment_id: str
    tenant_id: str
    candidate_transaction_id: str
    status: str
    rule_version: str | None
    advisory_score: Decimal | None
    reviewer_id: str | None
    reviewer_decision: TransactionReviewStatus | None
    evidence: tuple[dict, ...]
    created_at: datetime
    revision: int

    @classmethod
    def from_domain(cls, value: RiskAssessment):
        return cls.model_validate(value.model_dump())
