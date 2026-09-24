"""PostgreSQL persistence for promotion candidates with revision CAS."""

import asyncio
from datetime import UTC
from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import ResourceConflictError, WorkflowPersistenceError
from invoice_intelligence.domain.training import PromotionCandidateRecord, PromotionCandidateStatus
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    EvaluationRunRow,
    IndexVersionRow,
    ModelArtifactRow,
    ModelEvaluationRow,
    PromotionCandidateAuditRow,
    PromotionCandidateRow,
)


class SQLAlchemyPromotionCandidateRepository:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def create_candidate(
        self,
        candidate: PromotionCandidateRecord,
        *,
        actor_id: str,
        trace_id: str | None,
    ) -> None:
        await asyncio.to_thread(self._create_sync, candidate, actor_id, trace_id)

    async def get_candidate(
        self, tenant_id: str, candidate_id: str
    ) -> PromotionCandidateRecord | None:
        return await asyncio.to_thread(self._get_sync, tenant_id, candidate_id)

    async def transition_candidate(
        self, candidate: PromotionCandidateRecord, *, expected_revision: int,
        audit_action: str, actor_id: str, trace_id: str | None,
    ) -> PromotionCandidateRecord:
        return await asyncio.to_thread(
            self._transition_sync, candidate, expected_revision, audit_action, actor_id, trace_id
        )

    async def was_active(self, tenant_id: str, candidate_id: str) -> bool:
        return await asyncio.to_thread(self._was_active_sync, tenant_id, candidate_id)

    async def rollback_candidate(
        self,
        current: PromotionCandidateRecord,
        target: PromotionCandidateRecord,
        *,
        expected_revision: int,
        expected_target_revision: int,
        actor_id: str,
        trace_id: str | None,
    ) -> PromotionCandidateRecord:
        return await asyncio.to_thread(
            self._rollback_sync,
            current,
            target,
            expected_revision,
            expected_target_revision,
            actor_id,
            trace_id,
        )

    def _create_sync(
        self, candidate: PromotionCandidateRecord, actor_id: str, trace_id: str | None
    ) -> None:
        try:
            with self._sessions.begin() as session:
                session.add(_row(candidate))
                session.flush()
                session.add(PromotionCandidateAuditRow(
                    audit_id=uuid4().hex, tenant_id=candidate.tenant_id,
                    candidate_id=candidate.candidate_id,
                    action=(
                        "promotion_gate_rejected"
                        if candidate.status is PromotionCandidateStatus.REJECTED
                        else "promotion_candidate_created"
                    ),
                    actor_id=actor_id, from_status="none",
                    to_status=candidate.status.value, revision=candidate.revision,
                    trace_id=trace_id, created_at=candidate.created_at,
                ))
        except IntegrityError as exc:
            raise ResourceConflictError("Promotion candidate already exists") from exc
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Promotion candidate persistence failed") from exc

    def _get_sync(self, tenant_id: str, candidate_id: str) -> PromotionCandidateRecord | None:
        with self._sessions() as session:
            row = session.scalar(select(PromotionCandidateRow).where(
                PromotionCandidateRow.tenant_id == tenant_id,
                PromotionCandidateRow.candidate_id == candidate_id,
            ))
            return _domain(row) if row else None

    def _transition_sync(self, candidate, expected_revision, audit_action, actor_id, trace_id):
        try:
            with self._sessions.begin() as session:
                row = session.scalar(select(PromotionCandidateRow).where(
                    PromotionCandidateRow.tenant_id == candidate.tenant_id,
                    PromotionCandidateRow.candidate_id == candidate.candidate_id,
                ).with_for_update())
                if row is None or row.revision != expected_revision:
                    raise ResourceConflictError("Promotion candidate revision changed")
                if audit_action == "promotion_approved":
                    self._guard_evidence(session, candidate)
                if candidate.status is PromotionCandidateStatus.ACTIVE:
                    for previous in session.scalars(
                        select(PromotionCandidateRow).where(
                            PromotionCandidateRow.tenant_id == candidate.tenant_id,
                            PromotionCandidateRow.candidate_id != candidate.candidate_id,
                            PromotionCandidateRow.status == PromotionCandidateStatus.ACTIVE.value,
                        ).with_for_update()
                    ).all():
                        previous.status = PromotionCandidateStatus.ROLLBACK.value
                        previous.revision += 1
                        previous.updated_at = candidate.updated_at
                        session.add(PromotionCandidateAuditRow(
                            audit_id=uuid4().hex, tenant_id=previous.tenant_id,
                            candidate_id=previous.candidate_id, action="promotion_superseded",
                            actor_id=actor_id, from_status="active", to_status="rollback",
                            revision=previous.revision, trace_id=trace_id,
                            created_at=candidate.updated_at,
                        ))
                    session.flush()
                from_status = row.status
                row.status = candidate.status.value
                row.revision = candidate.revision
                row.metric_json = candidate.metric_values
                row.hard_failure_code = candidate.hard_failure_code
                row.compatibility_errors_json = list(candidate.compatibility_errors)
                row.approved_by = candidate.approved_by
                row.rejection_reason = candidate.rejection_reason
                row.updated_at = candidate.updated_at
                session.add(PromotionCandidateAuditRow(
                    audit_id=uuid4().hex, tenant_id=candidate.tenant_id,
                    candidate_id=candidate.candidate_id, action=audit_action,
                    actor_id=actor_id, from_status=from_status,
                    to_status=candidate.status.value, revision=candidate.revision,
                    trace_id=trace_id, created_at=candidate.updated_at,
                ))
                return candidate
        except ResourceConflictError:
            raise
        except IntegrityError as exc:
            raise ResourceConflictError("Only one active promotion is allowed") from exc
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Promotion candidate transition failed") from exc

    def _was_active_sync(self, tenant_id: str, candidate_id: str) -> bool:
        with self._sessions() as session:
            return session.scalar(select(PromotionCandidateAuditRow.audit_id).where(
                PromotionCandidateAuditRow.tenant_id == tenant_id,
                PromotionCandidateAuditRow.candidate_id == candidate_id,
                PromotionCandidateAuditRow.to_status == PromotionCandidateStatus.ACTIVE.value,
            ).limit(1)) is not None

    def _rollback_sync(
        self, current, target, expected_revision, expected_target_revision, actor_id, trace_id
    ):
        try:
            with self._sessions.begin() as session:
                rows = session.scalars(select(PromotionCandidateRow).where(
                    PromotionCandidateRow.tenant_id == current.tenant_id,
                    PromotionCandidateRow.candidate_id.in_(
                        (current.candidate_id, target.candidate_id)
                    ),
                ).order_by(PromotionCandidateRow.candidate_id).with_for_update()).all()
                by_id = {row.candidate_id: row for row in rows}
                current_row = by_id.get(current.candidate_id)
                target_row = by_id.get(target.candidate_id)
                if current_row is None or target_row is None:
                    raise ResourceConflictError("Rollback candidates changed")
                if (
                    current_row.status != PromotionCandidateStatus.ACTIVE.value
                    or current_row.revision != expected_revision
                    or target_row.status != PromotionCandidateStatus.ROLLBACK.value
                    or target_row.revision != expected_target_revision
                    or not session.scalar(select(PromotionCandidateAuditRow.audit_id).where(
                        PromotionCandidateAuditRow.tenant_id == target.tenant_id,
                        PromotionCandidateAuditRow.candidate_id == target.candidate_id,
                        PromotionCandidateAuditRow.to_status
                        == PromotionCandidateStatus.ACTIVE.value,
                    ).limit(1))
                ):
                    raise ResourceConflictError("Rollback target is no longer valid")
                self._guard_evidence(session, target)
                current_row.status = PromotionCandidateStatus.ROLLBACK.value
                current_row.revision = current.revision
                current_row.approved_by = actor_id
                current_row.updated_at = current.updated_at
                session.flush()
                target_row.status = PromotionCandidateStatus.ACTIVE.value
                target_row.revision = target.revision
                target_row.approved_by = actor_id
                target_row.updated_at = target.updated_at
                session.add_all((
                    PromotionCandidateAuditRow(
                        audit_id=uuid4().hex, tenant_id=current.tenant_id,
                        candidate_id=current.candidate_id, action="promotion_rollback",
                        actor_id=actor_id, from_status="active", to_status="rollback",
                        revision=current.revision, trace_id=trace_id,
                        created_at=current.updated_at,
                    ),
                    PromotionCandidateAuditRow(
                        audit_id=uuid4().hex, tenant_id=target.tenant_id,
                        candidate_id=target.candidate_id, action="promotion_restored",
                        actor_id=actor_id, from_status="rollback", to_status="active",
                        revision=target.revision, trace_id=trace_id,
                        created_at=target.updated_at,
                    ),
                ))
                return target
        except ResourceConflictError:
            raise
        except IntegrityError as exc:
            raise ResourceConflictError("Rollback active version changed") from exc
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Promotion rollback failed") from exc

    @staticmethod
    def _guard_evidence(session: Session, candidate: PromotionCandidateRecord) -> None:
        if candidate.artifact_id is None or candidate.model_evaluation_id is None:
            raise ResourceConflictError("Trusted promotion evidence is required")
        run = session.scalar(select(EvaluationRunRow).where(
            EvaluationRunRow.tenant_id == candidate.tenant_id,
            EvaluationRunRow.evaluation_run_id == candidate.evaluation_run_id,
        ).with_for_update())
        artifact = session.scalar(select(ModelArtifactRow).where(
            ModelArtifactRow.tenant_id == candidate.tenant_id,
            ModelArtifactRow.artifact_id == candidate.artifact_id,
        ).with_for_update())
        evaluation = session.scalar(select(ModelEvaluationRow).where(
            ModelEvaluationRow.tenant_id == candidate.tenant_id,
            ModelEvaluationRow.model_evaluation_id == candidate.model_evaluation_id,
            ModelEvaluationRow.artifact_id == candidate.artifact_id,
            ModelEvaluationRow.evaluation_run_id == candidate.evaluation_run_id,
        ).with_for_update())
        index = session.scalar(select(IndexVersionRow).where(
            IndexVersionRow.tenant_id == candidate.tenant_id,
            IndexVersionRow.version == candidate.index_version,
        ).with_for_update())
        if (
            run is None or run.status != "completed"
            or artifact is None or not artifact.is_valid
            or artifact.invalidated_at is not None
            or evaluation is None or evaluation.status != "passed"
            or index is None or not index.is_valid or index.activated_at is None
        ):
            raise ResourceConflictError("Trusted promotion evidence changed")


def _row(candidate: PromotionCandidateRecord) -> PromotionCandidateRow:
    return PromotionCandidateRow(
        candidate_id=candidate.candidate_id, tenant_id=candidate.tenant_id,
        artifact_id=candidate.artifact_id,
        model_evaluation_id=candidate.model_evaluation_id,
        dataset_version=candidate.dataset_version, evaluation_run_id=candidate.evaluation_run_id,
        model_version=candidate.model_version, prompt_version=candidate.prompt_version,
        schema_version=candidate.schema_version, index_version=candidate.index_version,
        threshold_version=candidate.threshold_version, status=candidate.status.value,
        revision=candidate.revision, metric_json=candidate.metric_values,
        hard_failure_code=candidate.hard_failure_code,
        compatibility_errors_json=list(candidate.compatibility_errors),
        approved_by=candidate.approved_by, rejection_reason=candidate.rejection_reason,
        created_at=candidate.created_at, updated_at=candidate.updated_at,
    )


def _domain(row: PromotionCandidateRow) -> PromotionCandidateRecord:
    return PromotionCandidateRecord(
        candidate_id=row.candidate_id, tenant_id=row.tenant_id,
        artifact_id=row.artifact_id,
        model_evaluation_id=row.model_evaluation_id,
        dataset_version=row.dataset_version, evaluation_run_id=row.evaluation_run_id,
        model_version=row.model_version, prompt_version=row.prompt_version,
        schema_version=row.schema_version, index_version=row.index_version,
        threshold_version=row.threshold_version,
        status=PromotionCandidateStatus(row.status), revision=row.revision,
        metric_values=dict(row.metric_json), hard_failure_code=row.hard_failure_code,
        compatibility_errors=tuple(row.compatibility_errors_json),
        created_at=(
            row.created_at.replace(tzinfo=UTC)
            if row.created_at.tzinfo is None else row.created_at
        ),
        updated_at=(
            row.updated_at.replace(tzinfo=UTC)
            if row.updated_at.tzinfo is None else row.updated_at
        ),
        approved_by=row.approved_by, rejection_reason=row.rejection_reason,
    )
