"""交易候选来源、幂等和审核 CAS 的隔离数据库回归。"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invoice_intelligence.api.schemas.transactions import TransactionCandidateRequest
from invoice_intelligence.application.errors import (
    IdempotencyConflictError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from invoice_intelligence.application.services.transaction_analysis import (
    TransactionAnalysisService,
)
from invoice_intelligence.application.services.transaction_rules import MockTransactionRuleEngine
from invoice_intelligence.domain.governance import TrustedTenantContext
from invoice_intelligence.domain.invoice import InvoiceExtraction
from invoice_intelligence.domain.transactions import TransactionReviewStatus
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    DocumentRow,
    ExtractionResultRow,
    ExtractionRunRow,
    TransactionAnalysisAuditRow,
    TransactionCandidateRow,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_repository import (
    SQLAlchemyBusinessRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_transactions import (
    SQLAlchemyTransactionAnalysisRepository,
)
from invoice_intelligence.infrastructure.serialization.pydantic import PydanticExtractionStateCodec


def _fixture() -> tuple[TransactionAnalysisService, Engine]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 23, tzinfo=UTC)
    invoice = {name: None for name in InvoiceExtraction.model_fields}
    invoice.update(
        seller_name="Synthetic Vendor",
        invoice_number="SYN-001",
        invoice_date=date(2026, 9, 1),
        invoice_total_tax_price=Decimal("12.50"),
        currency="USD",
    )
    validated = InvoiceExtraction.model_validate(invoice)
    with Session(engine) as session, session.begin():
        for number in (1, 2):
            session.add(
                DocumentRow(
                    document_id=f"document-{number}",
                    tenant_id="tenant-a",
                    storage_uri=f"local://document-{number}",
                    mime_type="image/png",
                    checksum="a" * 64,
                    created_at=now,
                )
            )
            session.add(
                ExtractionRunRow(
                    run_id=f"run-{number}",
                    tenant_id="tenant-a",
                    thread_id=f"thread-{number}",
                    document_id=f"document-{number}",
                    status="completed" if number == 1 else "processing",
                    validation_route="accepted" if number == 1 else None,
                    failure_message=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ExtractionResultRow(
                    run_id=f"run-{number}",
                    document_id=f"document-{number}",
                    result_json={"invoice": validated.model_dump(mode="json")},
                    created_at=now,
                )
            )
    business = SQLAlchemyBusinessRepository(engine, PydanticExtractionStateCodec())
    return (
        TransactionAnalysisService(
            SQLAlchemyTransactionAnalysisRepository(engine),
            MockTransactionRuleEngine(),
            business,
            business,
            "3.0.0",
        ),
        engine,
    )


def _context(tenant_id: str = "tenant-a") -> TrustedTenantContext:
    return TrustedTenantContext(
        tenant_id=tenant_id,
        actor_id="reviewer-a",
        permissions=frozenset(),
    )


@pytest.mark.asyncio
async def test_candidate_only_uses_completed_same_tenant_result() -> None:
    service, engine = _fixture()
    with pytest.raises(ResourceNotFoundError):
        await service.analyze(_context("tenant-b"), run_id="run-1", idempotency_key="foreign-1")
    with pytest.raises(ResourceNotFoundError):
        await service.analyze(_context(), run_id="run-2", idempotency_key="pending-1")
    result = await service.analyze(_context(), run_id="run-1", idempotency_key="analyze-1")
    assert result.revision == 1
    with Session(engine) as session, session.begin():
        second = session.get(ExtractionRunRow, "run-2")
        assert second is not None
        second.status = "completed"
    with pytest.raises(IdempotencyConflictError):
        await service.analyze(_context(), run_id="run-2", idempotency_key="analyze-1")
    with Session(engine) as session:
        candidate = session.scalar(select(TransactionCandidateRow))
        assert candidate is not None
        assert candidate.payload_json["vendor_key"] != "Synthetic Vendor"
        assert candidate.payload_json["normalized_reference"] != "SYN-001"
        assert candidate.payload_json["amount"] == "12.50"


@pytest.mark.asyncio
async def test_review_replay_conflict_and_revision_cas() -> None:
    service, engine = _fixture()
    first = await service.analyze(_context(), run_id="run-1", idempotency_key="analyze-1")
    replay = await service.analyze(_context(), run_id="run-1", idempotency_key="analyze-1")
    assert replay == first
    reviewed = await service.review(
        _context(),
        candidate_id=first.candidate_transaction_id,
        decision=TransactionReviewStatus.CONFIRMED,
        expected_revision=1,
        idempotency_key="review-1",
    )
    assert reviewed.revision == 2
    assert await service.review(
        _context(),
        candidate_id=first.candidate_transaction_id,
        decision=TransactionReviewStatus.CONFIRMED,
        expected_revision=1,
        idempotency_key="review-1",
    ) == reviewed
    with pytest.raises(IdempotencyConflictError):
        await service.review(
            _context(),
            candidate_id=first.candidate_transaction_id,
            decision=TransactionReviewStatus.DISMISSED,
            expected_revision=1,
            idempotency_key="review-1",
        )
    with pytest.raises(ResourceConflictError):
        await service.review(
            _context(),
            candidate_id=first.candidate_transaction_id,
            decision=TransactionReviewStatus.ESCALATED,
            expected_revision=1,
            idempotency_key="review-2",
        )
    with pytest.raises(ResourceNotFoundError):
        await service.review(
            _context("tenant-b"),
            candidate_id=first.candidate_transaction_id,
            decision=TransactionReviewStatus.CONFIRMED,
            expected_revision=1,
            idempotency_key="review-foreign",
        )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TransactionAnalysisAuditRow)) == 2


def test_candidate_request_rejects_client_supplied_invoice_fields() -> None:
    with pytest.raises(ValidationError):
        TransactionCandidateRequest.model_validate(
            {"run_id": "run-1", "vendor_key": "untrusted"}
        )


@pytest.mark.asyncio
async def test_replay_recovers_after_fact_commits_before_idempotency_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, engine = _fixture()
    business = service._idempotency
    original_complete = business.complete_idempotency

    async def interrupted(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic process interruption")

    monkeypatch.setattr(business, "complete_idempotency", interrupted)
    with pytest.raises(RuntimeError):
        await service.analyze(_context(), run_id="run-1", idempotency_key="recover-1")
    monkeypatch.setattr(business, "complete_idempotency", original_complete)
    recovered = await service.analyze(_context(), run_id="run-1", idempotency_key="recover-1")
    assert recovered.revision == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TransactionAnalysisAuditRow)) == 1


@pytest.mark.asyncio
async def test_legacy_or_tampered_candidate_cannot_be_reviewed() -> None:
    service, engine = _fixture()
    assessment = await service.analyze(_context(), run_id="run-1", idempotency_key="analyze-1")
    with Session(engine) as session, session.begin():
        candidate = session.get(TransactionCandidateRow, assessment.candidate_transaction_id)
        assert candidate is not None
        candidate.payload_json = {**candidate.payload_json, "vendor_key": "client-supplied"}
    with pytest.raises(ResourceConflictError):
        await service.review(
            _context(),
            candidate_id=assessment.candidate_transaction_id,
            decision=TransactionReviewStatus.CONFIRMED,
            expected_revision=1,
            idempotency_key="review-legacy",
        )
    escalated = await service.review(
        _context(), candidate_id=assessment.candidate_transaction_id,
        decision=TransactionReviewStatus.ESCALATED,
        expected_revision=1, idempotency_key="escalate-legacy",
    )
    assert escalated.status.value == "escalated"
    assert escalated.reviewer_id == "reviewer-a"
    assert escalated.evidence[-1] == {"type": "legacy_candidate_source_untrusted"}
    assert await service.review(
        _context(), candidate_id=assessment.candidate_transaction_id,
        decision=TransactionReviewStatus.ESCALATED,
        expected_revision=1, idempotency_key="escalate-legacy",
    ) == escalated
    with Session(engine) as session:
        candidate = session.get(TransactionCandidateRow, assessment.candidate_transaction_id)
        assert candidate is not None
        assert candidate.payload_json["vendor_key"] == "client-supplied"
        audits = session.scalars(select(TransactionAnalysisAuditRow)).all()
        assert len(audits) == 2
        assert {item.decision for item in audits} == {
            "pending_rule_review", "escalated",
        }


@pytest.mark.asyncio
async def test_missing_completed_source_can_only_be_escalated() -> None:
    service, engine = _fixture()
    assessment = await service.analyze(
        _context(), run_id="run-1", idempotency_key="analyze-source-loss",
    )
    with Session(engine) as session, session.begin():
        run = session.get(ExtractionRunRow, "run-1")
        assert run is not None
        run.status = "processing"
    with pytest.raises(ResourceConflictError):
        await service.review(
            _context(), candidate_id=assessment.candidate_transaction_id,
            decision=TransactionReviewStatus.DISMISSED,
            expected_revision=1, idempotency_key="dismiss-source-loss",
        )
    escalated = await service.review(
        _context(), candidate_id=assessment.candidate_transaction_id,
        decision=TransactionReviewStatus.ESCALATED,
        expected_revision=1, idempotency_key="escalate-source-loss",
    )
    assert escalated.evidence[-1] == {"type": "legacy_candidate_source_untrusted"}
