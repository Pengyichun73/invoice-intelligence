"""Accounting API contracts."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from invoice_intelligence.domain.accounting import AccountingStatus


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateAccountingCandidateRequest(_StrictModel):
    run_id: str = Field(min_length=1, max_length=64)


class PostingLineRequest(_StrictModel):
    account_code: str = Field(min_length=1, max_length=128)
    side: str
    amount: Decimal
    currency: str = Field(min_length=3, max_length=16)

    @field_validator("side")
    @classmethod
    def validate_side(cls, value: str) -> str:
        if value not in {"debit", "credit"}:
            raise ValueError("side must be debit or credit")
        return value


class AccountingAdvisoryRequest(_StrictModel):
    provider: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)
    recommendation_code: str = Field(min_length=1, max_length=128)
    reason_codes: tuple[str, ...] = Field(default=(), max_length=50)


class ApproveAccountingCandidateRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    tax_rule_version: str = Field(min_length=1, max_length=128)
    chart_of_accounts_version: str = Field(min_length=1, max_length=128)
    posting_rule_version: str = Field(min_length=1, max_length=128)
    tax_code: str = Field(min_length=1, max_length=128)
    taxable_amount: Decimal
    tax_amount: Decimal
    advisory: AccountingAdvisoryRequest | None = None
    lines: tuple[PostingLineRequest, ...] = Field(min_length=2, max_length=100)


class RejectAccountingCandidateRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    reason_code: str = Field(min_length=1, max_length=128)


class PostAccountingCandidateRequest(_StrictModel):
    expected_revision: int = Field(ge=1)


class ExchangeRateRequest(_StrictModel):
    base_currency: str = Field(min_length=3, max_length=16)
    quote_currency: str = Field(min_length=3, max_length=16)
    effective_at: datetime


class AccountingCandidateResponse(_StrictModel):
    candidate_id: str
    run_id: str
    status: AccountingStatus
    tax_rule_version: str | None
    chart_of_accounts_version: str | None
    posting_rule_version: str | None
    revision: int
    created_at: datetime
    updated_at: datetime


class ExchangeRateResponse(_StrictModel):
    source: str
    quote_currency: str
    base_currency: str
    rate: Decimal
    precision: int
    effective_at: datetime
    snapshot_id: str


class AccountingAuditResponse(_StrictModel):
    audit_id: str
    candidate_id: str
    action: str
    actor_id: str
    from_status: AccountingStatus | None
    to_status: AccountingStatus
    revision: int
    reason_code: str
    trace_id: str | None
    created_at: datetime
