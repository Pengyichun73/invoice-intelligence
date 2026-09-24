"""Ports owned by the accounting application boundary."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from invoice_intelligence.domain.accounting import (
    AccountingAdvisory,
    AccountingAuditEvent,
    AccountingCandidate,
    ExchangeRateSnapshot,
    PostingLine,
    PostingProposal,
    TaxAssessment,
)


@dataclass(frozen=True, slots=True)
class PostingReceipt:
    external_reference: str
    posted_at: datetime


class ExchangeRateProvider(Protocol):
    async def get_rate(
        self, *, base_currency: str, quote_currency: str, effective_at: datetime
    ) -> ExchangeRateSnapshot: ...


class AccountingPostingProvider(Protocol):
    async def post(
        self,
        *,
        posting_request_id: str,
        candidate: AccountingCandidate,
        proposal: PostingProposal,
        tax_assessment: TaxAssessment,
        exchange_rate: ExchangeRateSnapshot | None,
    ) -> PostingReceipt: ...


class AccountingRepository(Protocol):
    async def create_candidate(
        self, *, tenant_id: str, run_id: str, actor_id: str, trace_id: str | None
    ) -> AccountingCandidate: ...

    async def get_candidate(
        self, candidate_id: str, tenant_id: str
    ) -> AccountingCandidate | None: ...

    async def approve(
        self,
        *,
        candidate_id: str,
        tenant_id: str,
        expected_revision: int,
        actor_id: str,
        trace_id: str | None,
        tax_rule_version: str,
        chart_of_accounts_version: str,
        posting_rule_version: str,
        tax_code: str,
        taxable_amount: Decimal,
        tax_amount: Decimal,
        advisory: AccountingAdvisory | None,
        lines: tuple[PostingLine, ...],
    ) -> AccountingCandidate: ...

    async def reject(
        self,
        *,
        candidate_id: str,
        tenant_id: str,
        expected_revision: int,
        actor_id: str,
        trace_id: str | None,
        reason_code: str,
    ) -> AccountingCandidate: ...

    async def get_approved_package(
        self, candidate_id: str, tenant_id: str
    ) -> tuple[AccountingCandidate, PostingProposal, TaxAssessment] | None: ...

    async def save_exchange_rate(
        self, tenant_id: str, snapshot: ExchangeRateSnapshot
    ) -> ExchangeRateSnapshot: ...

    async def record_posting_attempt(
        self,
        *,
        candidate_id: str,
        tenant_id: str,
        expected_revision: int,
        actor_id: str,
        trace_id: str | None,
        outcome: str,
        external_reference: str | None,
        error_code: str | None,
    ) -> AccountingCandidate: ...

    async def list_audit(
        self, candidate_id: str, tenant_id: str
    ) -> tuple[AccountingAuditEvent, ...]: ...
