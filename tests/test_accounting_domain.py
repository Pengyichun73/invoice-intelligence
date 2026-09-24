from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.errors import (
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from invoice_intelligence.application.ports.accounting import (
    AccountingPostingProvider,
    PostingReceipt,
)
from invoice_intelligence.application.services.accounting import AccountingService
from invoice_intelligence.domain.accounting import (
    AccountingAdvisory,
    AccountingCandidate,
    AccountingStatus,
    ExchangeRateSnapshot,
    PostingLine,
    PostingProposal,
    TaxAssessment,
)
from invoice_intelligence.domain.governance import TrustedTenantContext
from invoice_intelligence.infrastructure.accounting.mock import (
    MockAccountingPostingProvider,
    MockExchangeRateProvider,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_accounting import (
    SQLAlchemyAccountingRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    DocumentRow,
    ExtractionResultRow,
    ExtractionRunRow,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_repository import (
    SQLAlchemyBusinessRepository,
)
from invoice_intelligence.infrastructure.serialization.pydantic import (
    PydanticExtractionStateCodec,
)


def _service(
    posting_provider: AccountingPostingProvider | None = None,
) -> AccountingService:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 23, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            DocumentRow(
                document_id="document-1",
                tenant_id="tenant-a",
                storage_uri="local://document-1",
                mime_type="image/png",
                checksum="a" * 64,
                created_at=now,
            )
        )
        session.add(
            ExtractionRunRow(
                run_id="run-1",
                tenant_id="tenant-a",
                thread_id="thread-1",
                document_id="document-1",
                status="completed",
                validation_route="accepted",
                failure_message=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ExtractionResultRow(
                run_id="run-1",
                document_id="document-1",
                result_json={"invoice": {}},
                created_at=now,
            )
        )
    business = SQLAlchemyBusinessRepository(engine, PydanticExtractionStateCodec())
    return AccountingService(
        repository=SQLAlchemyAccountingRepository(engine),
        idempotency_repository=business,
        exchange_rate_provider=MockExchangeRateProvider(),
        posting_provider=posting_provider or MockAccountingPostingProvider(),
    )


def _context(tenant_id: str = "tenant-a") -> TrustedTenantContext:
    return TrustedTenantContext(
        tenant_id=tenant_id, actor_id="reviewer-a", permissions=frozenset(), trace_id="trace-1"
    )


@pytest.mark.asyncio
async def test_candidate_requires_same_tenant_completed_run_and_starts_pending() -> None:
    service = _service()
    candidate = await service.create_candidate(
        _context(), run_id="run-1", idempotency_key="candidate-1"
    )
    replay = await service.create_candidate(
        _context(), run_id="run-1", idempotency_key="candidate-1"
    )

    assert candidate == replay
    assert candidate.status is AccountingStatus.PENDING_RULE_REVIEW
    assert candidate.revision == 1
    with pytest.raises(ResourceNotFoundError):
        await service.create_candidate(
            _context("tenant-b"), run_id="run-1", idempotency_key="candidate-tenant-b"
        )


@pytest.mark.asyncio
async def test_versioned_review_is_required_before_mock_posting() -> None:
    service = _service()
    candidate = await service.create_candidate(
        _context(), run_id="run-1", idempotency_key="candidate-1"
    )
    approved = await service.approve(
        _context(),
        candidate_id=candidate.candidate_id,
        expected_revision=candidate.revision,
        tax_rule_version="tax-2026.09",
        chart_of_accounts_version="coa-7",
        posting_rule_version="posting-3",
        tax_code="VAT",
        taxable_amount=Decimal("100.00"),
        tax_amount=Decimal("10.00"),
        advisory=AccountingAdvisory(
            provider="mock-advisor",
            model_version="structured-model-v2",
            recommendation_code="review-tax-code",
            reason_codes=("tax_label_present",),
        ),
        lines=(
            PostingLine("expense", "debit", Decimal("110.00"), "CNY"),
            PostingLine("payable", "credit", Decimal("110.00"), "CNY"),
        ),
        idempotency_key="approve-1",
    )

    assert approved.status is AccountingStatus.APPROVED
    assert approved.tax_rule_version == "tax-2026.09"
    posted = await service.post(
        _context(),
        candidate_id=candidate.candidate_id,
        expected_revision=approved.revision,
        exchange_rate=None,
        idempotency_key="post-1",
    )
    assert posted.status is AccountingStatus.POSTED
    assert posted.revision == approved.revision + 2
    audit = await service.list_audit(_context(), candidate.candidate_id)
    assert [event.action for event in audit] == ["create_candidate", "approve", "started", "post"]


class _FailingPostingProvider:
    async def post(
        self,
        *,
        posting_request_id: str,
        candidate: AccountingCandidate,
        proposal: PostingProposal,
        tax_assessment: TaxAssessment,
        exchange_rate: ExchangeRateSnapshot | None,
    ) -> PostingReceipt:
        raise RuntimeError("remote body must not escape the adapter")


@pytest.mark.asyncio
async def test_provider_failure_preserves_approved_fact_and_is_audited() -> None:
    service = _service(_FailingPostingProvider())
    candidate = await service.create_candidate(
        _context(), run_id="run-1", idempotency_key="candidate-1"
    )
    approved = await service.approve(
        _context(),
        candidate_id=candidate.candidate_id,
        expected_revision=1,
        tax_rule_version="tax-1",
        chart_of_accounts_version="coa-1",
        posting_rule_version="posting-1",
        tax_code="VAT",
        taxable_amount=Decimal("10"),
        tax_amount=Decimal("1"),
        advisory=None,
        lines=(
            PostingLine("expense", "debit", Decimal("11"), "CNY"),
            PostingLine("payable", "credit", Decimal("11"), "CNY"),
        ),
        idempotency_key="approve-1",
    )

    with pytest.raises(ServiceUnavailableError):
        await service.post(
            _context(),
            candidate_id=candidate.candidate_id,
            expected_revision=approved.revision,
            exchange_rate=None,
            idempotency_key="post-failure-1",
        )

    current = await service.get_candidate(_context(), candidate.candidate_id)
    assert current.status is AccountingStatus.APPROVED
    assert current.revision == approved.revision + 2
    audit = await service.list_audit(_context(), candidate.candidate_id)
    assert [event.action for event in audit[-2:]] == ["started", "failed"]
