"""Framework-independent accounting domain entities."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class AccountingStatus(StrEnum):
    PENDING_RULE_REVIEW = "pending_rule_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    POSTED = "posted"


@dataclass(frozen=True, slots=True)
class AccountingCandidate:
    candidate_id: str
    tenant_id: str
    run_id: str
    status: AccountingStatus
    tax_rule_version: str | None
    chart_of_accounts_version: str | None
    posting_rule_version: str | None
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AccountingAdvisory:
    provider: str
    model_version: str
    recommendation_code: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaxAssessment:
    assessment_id: str
    candidate_id: str
    tax_rule_version: str
    tax_code: str
    taxable_amount: Decimal
    tax_amount: Decimal
    advisory: AccountingAdvisory | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PostingLine:
    account_code: str
    side: str
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class PostingProposal:
    proposal_id: str
    candidate_id: str
    chart_of_accounts_version: str
    posting_rule_version: str
    lines: tuple[PostingLine, ...]
    decided_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExchangeRateSnapshot:
    source: str
    quote_currency: str
    base_currency: str
    rate: Decimal
    precision: int
    effective_at: datetime
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class AccountingAuditEvent:
    audit_id: str
    tenant_id: str
    candidate_id: str
    action: str
    actor_id: str
    from_status: AccountingStatus | None
    to_status: AccountingStatus
    revision: int
    reason_code: str
    trace_id: str | None
    created_at: datetime
