"""冲突关闭后的重评估消费不得绕过确定性硬失败和人工门禁。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invoice_intelligence.infrastructure.persistence.sqlalchemy_conflict_reevaluation import (
    SQLAlchemyConflictReevaluationConsumer,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    MemoryAdmissionDecisionRow,
    MemoryAdmissionRecordRow,
    MemoryConflictReevaluationRequestRow,
    MemoryConflictResolutionDecisionRow,
    ReviewedExampleRow,
)


def _store(reason_codes: list[str]) -> tuple[SQLAlchemyConflictReevaluationConsumer, Engine]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add(
            ReviewedExampleRow(
                example_id="example-1", tenant_id="tenant-a", replay_key="replay-1",
                semantic_fingerprint="fingerprint-1", source_feedback_id="feedback-1",
                document_id="document-1", run_id="run-1", document_type="invoice",
                field_path="invoice_number", schema_version="3.0.0",
                catalog_version="catalog-1", model_version_id="model-1",
                prompt_version_id="prompt-1", label_type="confirmed_correct",
                model_value_json=None, reviewed_value_json=None,
                evidence_reference_json={"document_reference": "document-1"},
                reviewer_id="reviewer-1", is_reviewed=True, is_valid=True,
                occurrence_count=1, created_at=now, updated_at=now, last_seen_at=now,
            )
        )
        session.add(
            MemoryAdmissionDecisionRow(
                decision_id="decision-1", tenant_id="tenant-a", example_id="example-1",
                previous_status="pending", status="quarantined",
                authority="deterministic_policy", decided_by="service:policy",
                reason="policy_review", reason_codes_json=reason_codes,
                assessment_ids_json=[], conflict_ids_json=["conflict-1"],
                policy_version="policy-1", idempotency_key_hash="old-key",
                revision=2, decided_at=now,
            )
        )
        session.add(
            MemoryAdmissionRecordRow(
                example_id="example-1", tenant_id="tenant-a", schema_version="3.0.0",
                status="quarantined", current_decision_id="decision-1",
                policy_version="policy-1", revision=2, attempt_count=0,
                created_at=now, updated_at=now,
            )
        )
        session.add(
            MemoryConflictResolutionDecisionRow(
                resolution_decision_id="resolution-1", tenant_id="tenant-a",
                conflict_id="conflict-1", previous_status="open",
                target_status="resolved", reviewer_id="reviewer-2",
                reason="reviewed", idempotency_key_hash="resolution-key",
                policy_version="policy-1", decided_at=now,
            )
        )
        session.add(
            MemoryConflictReevaluationRequestRow(
                reevaluation_request_id="request-1", tenant_id="tenant-a",
                resolution_decision_id="resolution-1", target_type="memory_admission",
                target_id="example-1", status="pending", created_at=now,
            )
        )
    return SQLAlchemyConflictReevaluationConsumer(engine), engine


@pytest.mark.asyncio
async def test_only_conflict_quarantine_is_requeued_once() -> None:
    consumer, engine = _store(["open_review_conflict"])
    assert await consumer.consume_one() is True
    assert await consumer.consume_one() is False
    with Session(engine) as session:
        admission = session.get(MemoryAdmissionRecordRow, "example-1")
        request = session.get(MemoryConflictReevaluationRequestRow, "request-1")
        decisions = session.scalars(select(MemoryAdmissionDecisionRow)).all()
        assert admission is not None and admission.status == "pending"
        assert admission.revision == 3 and admission.next_attempt_at is not None
        assert request is not None and request.status == "completed"
        assert len(decisions) == 2


@pytest.mark.asyncio
async def test_hard_failure_remains_quarantined_for_human_review() -> None:
    consumer, engine = _store(["deterministic_hard_failure", "invalid_evidence"])
    assert await consumer.consume_one() is True
    with Session(engine) as session:
        admission = session.get(MemoryAdmissionRecordRow, "example-1")
        request = session.get(MemoryConflictReevaluationRequestRow, "request-1")
        assert admission is not None and admission.status == "quarantined"
        assert admission.revision == 2
        assert request is not None and request.status == "requires_review"


@pytest.mark.asyncio
async def test_active_admission_lease_keeps_request_pending() -> None:
    consumer, engine = _store(["open_review_conflict"])
    with Session(engine) as session, session.begin():
        admission = session.get(MemoryAdmissionRecordRow, "example-1")
        assert admission is not None
        admission.worker_id = "worker-a"
        admission.lease_token = "lease-a"
        admission.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    assert await consumer.consume_one() is False
    with Session(engine) as session:
        request = session.get(MemoryConflictReevaluationRequestRow, "request-1")
        assert request is not None and request.status == "pending"
        assert request.completed_at is None


@pytest.mark.asyncio
async def test_cross_tenant_request_cannot_requeue_target() -> None:
    consumer, engine = _store(["open_review_conflict"])
    with Session(engine) as session, session.begin():
        request = session.get(MemoryConflictReevaluationRequestRow, "request-1")
        assert request is not None
        request.tenant_id = "tenant-b"
    assert await consumer.consume_one() is True
    with Session(engine) as session:
        admission = session.get(MemoryAdmissionRecordRow, "example-1")
        request = session.get(MemoryConflictReevaluationRequestRow, "request-1")
        assert admission is not None and admission.status == "quarantined"
        assert request is not None and request.status == "invalid_target"
