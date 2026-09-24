"""短事务消费冲突重评估请求；不赋予案例或别名批准状态。"""

import asyncio
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import WorkflowPersistenceError
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    FieldAliasCandidateRow,
    MemoryAdmissionDecisionRow,
    MemoryAdmissionRecordRow,
    MemoryConflictReevaluationRequestRow,
    MemoryConflictResolutionDecisionRow,
    ReviewedExampleRow,
)


class SQLAlchemyConflictReevaluationConsumer:
    """一次只消费一条请求，目标变更与消费标记共用 PostgreSQL 事务。"""

    def __init__(self, engine: Engine) -> None:
        self._postgresql = engine.dialect.name == "postgresql"
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def consume_one(self) -> bool:
        return await asyncio.to_thread(self._consume_one_sync)

    def _consume_one_sync(self) -> bool:
        try:
            with self._sessions.begin() as session:
                statement = (
                    select(MemoryConflictReevaluationRequestRow)
                    .where(MemoryConflictReevaluationRequestRow.status == "pending")
                    .order_by(
                        MemoryConflictReevaluationRequestRow.created_at,
                        MemoryConflictReevaluationRequestRow.reevaluation_request_id,
                    )
                    .limit(1)
                )
                if self._postgresql:
                    statement = statement.with_for_update(skip_locked=True)
                request = session.scalar(statement)
                if request is None:
                    return False
                now = datetime.now(UTC)
                resolution = session.get(
                    MemoryConflictResolutionDecisionRow,
                    request.resolution_decision_id,
                )
                if resolution is None or resolution.tenant_id != request.tenant_id:
                    request.status = "invalid_target"
                elif request.target_type == "memory_admission":
                    request.status = self._requeue_admission(
                        session, request, resolution.conflict_id, now
                    )
                elif request.target_type == "field_alias":
                    candidate = session.get(FieldAliasCandidateRow, request.target_id)
                    request.status = (
                        "requires_review"
                        if candidate is not None and candidate.tenant_id == request.tenant_id
                        else "invalid_target"
                    )
                else:
                    request.status = "invalid_target"
                if request.status == "pending":
                    return False
                request.completed_at = now
                return True
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to consume conflict reevaluation") from exc

    def _requeue_admission(
        self,
        session: Session,
        request: MemoryConflictReevaluationRequestRow,
        conflict_id: str,
        now: datetime,
    ) -> str:
        statement = select(MemoryAdmissionRecordRow).where(
            MemoryAdmissionRecordRow.tenant_id == request.tenant_id,
            MemoryAdmissionRecordRow.example_id == request.target_id,
        )
        if self._postgresql:
            statement = statement.with_for_update()
        admission = session.scalar(statement)
        example = session.get(ReviewedExampleRow, request.target_id)
        if (
            admission is None
            or example is None
            or example.tenant_id != request.tenant_id
            or not example.is_reviewed
            or not example.is_valid
        ):
            return "invalid_target"
        if admission.worker_id is not None:
            return "pending"
        if admission.status == "quarantined":
            previous_decision = session.get(
                MemoryAdmissionDecisionRow, admission.current_decision_id
            )
            if (
                previous_decision is None
                or previous_decision.tenant_id != request.tenant_id
                or previous_decision.authority != "deterministic_policy"
                or previous_decision.reason_codes_json != ["open_review_conflict"]
                or conflict_id not in previous_decision.conflict_ids_json
            ):
                return "requires_review"
            revision = admission.revision + 1
            decision_id = sha256(
                f"conflict-requeue\0{request.reevaluation_request_id}".encode()
            ).hexdigest()
            session.add(
                MemoryAdmissionDecisionRow(
                    decision_id=decision_id,
                    tenant_id=request.tenant_id,
                    example_id=request.target_id,
                    previous_status="quarantined",
                    status="pending",
                    authority="deterministic_policy",
                    decided_by="service:conflict-reevaluation",
                    reason="conflict_resolved_reevaluate",
                    reason_codes_json=["conflict.resolved_reevaluation"],
                    assessment_ids_json=[],
                    conflict_ids_json=[conflict_id],
                    policy_version=admission.policy_version,
                    idempotency_key_hash=request.reevaluation_request_id,
                    revision=revision,
                    decided_at=now,
                )
            )
            admission.current_decision_id = decision_id
            admission.status = "pending"
            admission.revision = revision
        elif admission.status != "pending":
            return "requires_review"
        admission.next_attempt_at = now
        admission.attempt_count = 0
        admission.last_error_code = None
        admission.last_error_at = None
        admission.updated_at = now
        return "completed"
