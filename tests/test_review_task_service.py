from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.errors import ResourceConflictError
from invoice_intelligence.application.services.review_tasks import (
    ReviewTaskService,
    ReviewTaskServiceConfig,
)
from invoice_intelligence.domain.document import DocumentReference
from invoice_intelligence.domain.extraction import ExtractionResult
from invoice_intelligence.domain.governance import TrustedTenantContext
from invoice_intelligence.domain.invoice import InvoiceExtraction
from invoice_intelligence.domain.review_tasks import ReviewTaskStatus
from invoice_intelligence.domain.workflow import (
    CorrectionEvent,
    FieldReviewDecision,
    HumanCorrection,
    HumanReviewAction,
    WorkflowIdentity,
    WorkflowStatus,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    CorrectionEventRow,
    DocumentRow,
    ExtractionRunRow,
    HumanCorrectionRow,
    MemoryReviewRecoveryRow,
    ReviewTaskRow,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_repository import (
    SQLAlchemyBusinessRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_review_tasks import (
    SQLAlchemyReviewTaskRepository,
)
from invoice_intelligence.infrastructure.serialization.pydantic import (
    PydanticExtractionStateCodec,
)


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _insert_task(engine, *, tenant_id: str = "tenant-a") -> None:
    now = datetime(2026, 9, 23, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            ReviewTaskRow(
                review_id="review-1",
                tenant_id=tenant_id,
                run_id="run-1",
                status=ReviewTaskStatus.PENDING_REVIEW.value,
                request_json={"fields": [], "field_bindings": [], "evidence_sources": []},
                version=1,
                priority=80,
                assigned_reviewer_id=None,
                lease_token=None,
                lease_expires_at=None,
                revision=1,
                created_at=now,
                updated_at=now,
                resolved_at=None,
                submitted_at=None,
                cancelled_at=None,
                cancel_reason=None,
            )
        )


def _context(tenant_id: str = "tenant-a", actor_id: str = "reviewer-a"):
    return TrustedTenantContext(
        tenant_id=tenant_id,
        actor_id=actor_id,
        permissions=frozenset(),
        trace_id="trace-1",
    )


@pytest.mark.asyncio
async def test_claim_is_tenant_scoped_and_revision_guarded() -> None:
    engine = _engine()
    _insert_task(engine)
    repository = SQLAlchemyReviewTaskRepository(engine)
    now = datetime(2026, 9, 23, 1, tzinfo=UTC)
    service = ReviewTaskService(
        repository=repository,
        workflow_service=object(),  # type: ignore[arg-type]
        config=ReviewTaskServiceConfig(default_lease_seconds=60, maximum_lease_seconds=300),
        clock=lambda: now,
    )

    assert await repository.get_review_task("tenant-b", "review-1") is None
    claimed = await service.claim(_context(), "review-1", expected_revision=1)
    assert claimed.status is ReviewTaskStatus.CLAIMED
    assert claimed.assigned_reviewer_id == "reviewer-a"
    assert claimed.revision == 2
    assert claimed.lease_token is not None

    with pytest.raises(ResourceConflictError):
        await service.claim(_context(), "review-1", expected_revision=1)


@pytest.mark.asyncio
async def test_expired_lease_is_recovered_and_can_be_reclaimed() -> None:
    engine = _engine()
    _insert_task(engine)
    repository = SQLAlchemyReviewTaskRepository(engine)
    claim_time = datetime(2026, 9, 23, 1, tzinfo=UTC)
    service = ReviewTaskService(
        repository=repository,
        workflow_service=object(),  # type: ignore[arg-type]
        config=ReviewTaskServiceConfig(default_lease_seconds=30, maximum_lease_seconds=300),
        clock=lambda: claim_time,
    )
    claimed = await service.claim(_context(), "review-1", expected_revision=1)

    recovery_time = claim_time + timedelta(seconds=31)
    recovery_service = ReviewTaskService(
        repository=repository,
        workflow_service=object(),  # type: ignore[arg-type]
        config=ReviewTaskServiceConfig(default_lease_seconds=30, maximum_lease_seconds=300),
        clock=lambda: recovery_time,
    )
    recovered = await recovery_service.recover_expired(_context(), limit=10)
    assert len(recovered) == 1
    assert recovered[0].status is ReviewTaskStatus.EXPIRED
    assert recovered[0].revision == claimed.revision + 1
    assert recovered[0].lease_token is None

    reclaimed = await recovery_service.claim(
        _context(actor_id="reviewer-b"),
        "review-1",
        expected_revision=recovered[0].revision,
    )
    assert reclaimed.status is ReviewTaskStatus.CLAIMED
    assert reclaimed.assigned_reviewer_id == "reviewer-b"


@pytest.mark.asyncio
async def test_reassign_requires_target_reviewer_to_claim_new_lease() -> None:
    engine = _engine()
    _insert_task(engine)
    repository = SQLAlchemyReviewTaskRepository(engine)
    now = datetime(2026, 9, 23, 1, tzinfo=UTC)
    service = ReviewTaskService(
        repository=repository,
        workflow_service=object(),  # type: ignore[arg-type]
        config=ReviewTaskServiceConfig(default_lease_seconds=60, maximum_lease_seconds=300),
        clock=lambda: now,
    )
    claimed = await service.claim(_context(), "review-1", expected_revision=1)
    assert claimed.lease_token is not None

    reassigned = await service.reassign(
        _context(),
        "review-1",
        target_reviewer_id="reviewer-b",
        expected_revision=claimed.revision,
        lease_token=claimed.lease_token,
    )
    assert reassigned.status is ReviewTaskStatus.PENDING_REVIEW
    assert reassigned.assigned_reviewer_id == "reviewer-b"
    assert reassigned.lease_token is None

    with pytest.raises(ResourceConflictError):
        await service.claim(
            _context(actor_id="reviewer-c"),
            "review-1",
            expected_revision=reassigned.revision,
        )

    target_claim = await service.claim(
        _context(actor_id="reviewer-b"),
        "review-1",
        expected_revision=reassigned.revision,
    )
    assert target_claim.status is ReviewTaskStatus.CLAIMED
    assert target_claim.assigned_reviewer_id == "reviewer-b"
    assert target_claim.lease_token is not None


@pytest.mark.asyncio
async def test_mark_submitted_is_idempotent_and_audited_once() -> None:
    engine = _engine()
    _insert_task(engine)
    repository = SQLAlchemyReviewTaskRepository(engine)
    now = datetime(2026, 9, 23, 1, tzinfo=UTC)
    service = ReviewTaskService(
        repository=repository,
        workflow_service=object(),  # type: ignore[arg-type]
        config=ReviewTaskServiceConfig(default_lease_seconds=60, maximum_lease_seconds=300),
        clock=lambda: now,
    )
    await service.claim(_context(), "review-1", expected_revision=1)

    submitted = await repository.mark_review_submitted(
        "tenant-a",
        "run-1",
        reviewer_id="reviewer-a",
        now=now,
        trace_id="trace-1",
    )
    replay = await repository.mark_review_submitted(
        "tenant-a",
        "run-1",
        reviewer_id="reviewer-a",
        now=now,
        trace_id="trace-1",
    )

    assert submitted == replay
    assert submitted.status is ReviewTaskStatus.SUBMITTED
    assert await repository.count_review_audits("tenant-a", "review-1", "submit") == 1


@pytest.mark.asyncio
async def test_review_fact_replay_does_not_duplicate_correction_event() -> None:
    engine = _engine()
    now = datetime(2026, 9, 23, 1, tzinfo=UTC)
    document = DocumentReference(
        document_id="document-1",
        storage_uri="local://tenant-a/document-1",
        mime_type="image/png",
        checksum="a" * 64,
    )
    identity = WorkflowIdentity(
        thread_id="thread-1",
        run_id="run-1",
        document_id=document.document_id,
    )
    with Session(engine) as session, session.begin():
        session.add(
            DocumentRow(
                document_id=document.document_id,
                tenant_id="tenant-a",
                storage_uri=document.storage_uri,
                mime_type=document.mime_type,
                checksum=document.checksum,
                created_at=now,
            )
        )
        session.add(
            ExtractionRunRow(
                run_id=identity.run_id,
                tenant_id="tenant-a",
                thread_id=identity.thread_id,
                document_id=identity.document_id,
                status=WorkflowStatus.PENDING_REVIEW.value,
                validation_route="review_required",
                failure_message=None,
                created_at=now,
                updated_at=now,
            )
        )

    invoice = InvoiceExtraction.model_validate(
        {field_path: None for field_path in InvoiceExtraction.model_fields}
    )
    result = ExtractionResult(
        invoice=invoice,
        field_evidence=(),
        anomalies=(),
    )
    correction = HumanCorrection(
        corrected_invoice=None,
        fields=(
            FieldReviewDecision(
                field_path="invoice_number",
                action=HumanReviewAction.CONFIRM_INCORRECT,
                reason="not_visible",
                rejected_value=None,
            ),
        ),
        reviewer_id="reviewer-a",
        document_type="invoice",
    )
    event = CorrectionEvent(
        document_type="invoice",
        field_path="invoice_number",
        model_value=None,
        corrected_value=None,
        correction_reason="not_visible",
        vendor_features={},
        template_features={},
        document_reference=document.document_id,
        image_reference=None,
        schema_version="invoice-v1",
        created_at=now,
        is_reviewed=True,
        is_valid=True,
    )
    repository = SQLAlchemyBusinessRepository(engine, PydanticExtractionStateCodec())

    first = await repository.save_review_facts(
        identity=identity,
        tenant_id="tenant-a",
        document=document,
        original_result=result,
        reviewed_result=result,
        review=correction,
        correction_events=(event,),
    )
    replay = await repository.save_review_facts(
        identity=identity,
        tenant_id="tenant-a",
        document=document,
        original_result=result,
        reviewed_result=result,
        review=correction,
        correction_events=(event,),
    )

    assert replay.recovery_id == first.recovery_id
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(HumanCorrectionRow)) == 1
        assert session.scalar(select(func.count()).select_from(CorrectionEventRow)) == 1
        assert session.scalar(select(func.count()).select_from(MemoryReviewRecoveryRow)) == 1
