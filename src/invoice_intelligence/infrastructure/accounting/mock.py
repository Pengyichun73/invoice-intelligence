"""Deterministic mock adapters used by local and Docker deployments."""

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from invoice_intelligence.application.errors import ServiceUnavailableError
from invoice_intelligence.application.ports.accounting import PostingReceipt
from invoice_intelligence.domain.accounting import (
    AccountingCandidate,
    ExchangeRateSnapshot,
    PostingProposal,
    TaxAssessment,
)


class MockExchangeRateProvider:
    async def get_rate(
        self, *, base_currency: str, quote_currency: str, effective_at: datetime
    ) -> ExchangeRateSnapshot:
        if base_currency != quote_currency:
            raise ServiceUnavailableError(
                "Mock exchange-rate provider has no configured cross rate"
            )
        return ExchangeRateSnapshot(
            source="mock",
            quote_currency=quote_currency,
            base_currency=base_currency,
            rate=Decimal("1"),
            precision=8,
            effective_at=effective_at,
            snapshot_id=uuid4().hex,
        )


class MockAccountingPostingProvider:
    async def post(
        self,
        *,
        posting_request_id: str,
        candidate: AccountingCandidate,
        proposal: PostingProposal,
        tax_assessment: TaxAssessment,
        exchange_rate: ExchangeRateSnapshot | None,
    ) -> PostingReceipt:
        digest = sha256(
            f"{candidate.tenant_id}:{candidate.candidate_id}:{posting_request_id}".encode()
        ).hexdigest()[:24]
        return PostingReceipt(external_reference=f"mock-{digest}", posted_at=datetime.now(UTC))
