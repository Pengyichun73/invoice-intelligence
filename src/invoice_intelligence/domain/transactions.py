"""独立交易分析 Domain；不扩展 InvoiceExtraction。"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TransactionReviewStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    ESCALATED = "escalated"


class RiskStatus(StrEnum):
    PENDING_RULE_REVIEW = "pending_rule_review"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    ESCALATED = "escalated"


class TransactionCandidate(BaseModel):
    """从发票结果派生的不可变、脱敏交易输入快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    tenant_id: str
    run_id: str
    document_id: str
    fingerprint: str = Field(min_length=64, max_length=64)
    vendor_key: str | None = None
    transaction_date: date | None = None
    amount: Decimal | None = None
    currency: str | None = None
    normalized_reference: str | None = None
    schema_version: str
    created_at: datetime


class TransactionClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    classification_id: str
    tenant_id: str
    candidate_id: str
    category: str | None = None
    subcategory: str | None = None
    rule_version: str | None = None
    model_version: str | None = None
    evidence: tuple[dict[str, Any], ...] = ()
    review_status: TransactionReviewStatus = TransactionReviewStatus.PENDING
    idempotency_key_hash: str
    created_at: datetime


class DuplicateTransactionCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    tenant_id: str
    candidate_transaction_id: str
    matched_transaction_id: str
    matching_dimensions: tuple[str, ...]
    time_window_days: int = Field(ge=0)
    amount_tolerance: Decimal = Field(ge=0)
    vendor_similarity: Decimal = Field(ge=0, le=1)
    evidence: tuple[dict[str, Any], ...] = ()
    review_status: TransactionReviewStatus = TransactionReviewStatus.PENDING
    idempotency_key_hash: str
    created_at: datetime


class SuspiciousTransactionCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    tenant_id: str
    candidate_transaction_id: str
    risk_rule: str
    rule_version: str | None = None
    triggering_evidence: tuple[dict[str, Any], ...] = ()
    advisory_score: Decimal | None = Field(default=None, ge=0, le=1)
    reviewer_decision: TransactionReviewStatus = TransactionReviewStatus.PENDING
    idempotency_key_hash: str
    created_at: datetime


class RiskAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment_id: str
    tenant_id: str
    candidate_transaction_id: str
    status: RiskStatus
    rule_version: str | None = None
    advisory_score: Decimal | None = Field(default=None, ge=0, le=1)
    reviewer_id: str | None = None
    reviewer_decision: TransactionReviewStatus | None = None
    evidence: tuple[dict[str, Any], ...] = ()
    idempotency_key_hash: str
    created_at: datetime
    revision: int = Field(default=1, ge=1)
