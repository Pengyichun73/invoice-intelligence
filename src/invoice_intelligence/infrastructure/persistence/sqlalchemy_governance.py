"""SQLAlchemy persistence for memory governance and retrieval telemetry."""

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast

from sqlalchemy import Engine, and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import WorkflowPersistenceError
from invoice_intelligence.domain.examples import (
    IndexVersion,
    ModelVersion,
    PromptVersion,
    RetrievalPolicyVersion,
)
from invoice_intelligence.domain.governance import (
    GovernanceAction,
    GovernanceAuditEvent,
    RetrievalFeedback,
    RetrievalFeedbackLabel,
    RetrievalMetricSummary,
    RetrievalStageMetrics,
    RetrievalTrace,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    MemoryGovernanceAuditRow,
    MemoryRetrievalFeedbackRow,
    RetrievalTraceRow,
)


def add_governance_audit(
    session: Session,
    event: GovernanceAuditEvent,
) -> None:
    """Append an audit row inside the caller's authoritative PostgreSQL transaction."""

    session.add(
        MemoryGovernanceAuditRow(
            audit_id=event.audit_id,
            tenant_id=event.tenant_id,
            action=event.action.value,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            reviewer_id=event.reviewer_id,
            reason=event.reason,
            resource_version=event.resource_version,
            trace_id=event.trace_id,
            idempotency_key_hash=event.idempotency_key_hash,
            created_at=event.created_at,
        )
    )


def require_governance_audit(
    session: Session,
    event: GovernanceAuditEvent,
) -> None:
    """Validate a replay audit when one exists; legacy decisions may predate it."""

    row = session.scalar(
        select(MemoryGovernanceAuditRow).where(
            MemoryGovernanceAuditRow.tenant_id == event.tenant_id,
            MemoryGovernanceAuditRow.action == event.action.value,
            MemoryGovernanceAuditRow.idempotency_key_hash
            == event.idempotency_key_hash,
        )
    )
    # Decisions created before the unified-audit migration have no matching row.
    # They remain replayable without inventing historical trace/version metadata.
    if row is None:
        return
    if (
        row.audit_id != event.audit_id
        or row.resource_type != event.resource_type
        or row.resource_id != event.resource_id
        or row.reviewer_id != event.reviewer_id
        or row.reason != event.reason
        or row.resource_version != event.resource_version
    ):
        raise WorkflowPersistenceError("Governance decision audit is inconsistent")


class SQLAlchemyMemoryGovernanceRepository:
    """Store only attributable actions and sensitive-value-free telemetry."""

    def __init__(self, engine: Engine) -> None:
        self._dialect_name = engine.dialect.name
        self._sessions = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        )

    async def save_audit(self, event: GovernanceAuditEvent) -> GovernanceAuditEvent:
        return await asyncio.to_thread(self._save_audit_sync, event)

    async def list_audits(
        self,
        tenant_id: str,
        *,
        action: GovernanceAction | None,
        resource_type: str | None,
        resource_id: str | None,
        trace_id: str | None,
        started_at: datetime | None,
        ended_at: datetime | None,
        limit: int,
        after_audit_id: str | None,
    ) -> Sequence[GovernanceAuditEvent]:
        return await asyncio.to_thread(
            self._list_audits_sync,
            tenant_id,
            action,
            resource_type,
            resource_id,
            trace_id,
            started_at,
            ended_at,
            limit,
            after_audit_id,
        )

    async def get_audit_by_idempotency_hash(
        self,
        tenant_id: str,
        action: GovernanceAction,
        idempotency_key_hash: str,
    ) -> GovernanceAuditEvent | None:
        return await asyncio.to_thread(
            self._get_audit_by_idempotency_hash_sync,
            tenant_id,
            action,
            idempotency_key_hash,
        )

    async def save_feedback(
        self,
        feedback: RetrievalFeedback,
        audit_event: GovernanceAuditEvent,
    ) -> RetrievalFeedback:
        return await asyncio.to_thread(
            self._save_feedback_sync,
            feedback,
            audit_event,
        )

    async def get_feedback(
        self,
        tenant_id: str,
        feedback_id: str,
    ) -> RetrievalFeedback | None:
        return await asyncio.to_thread(
            self._get_feedback_sync,
            tenant_id,
            feedback_id,
        )

    async def save_trace(self, trace: RetrievalTrace) -> None:
        await asyncio.to_thread(self._save_trace_sync, trace)

    async def get_trace(
        self,
        tenant_id: str,
        trace_id: str,
    ) -> RetrievalTrace | None:
        return await asyncio.to_thread(self._get_trace_sync, tenant_id, trace_id)

    async def mark_review_required(
        self,
        tenant_id: str,
        trace_ids: Sequence[str],
        review_required: bool,
    ) -> None:
        await asyncio.to_thread(
            self._mark_review_required_sync,
            tenant_id,
            tuple(trace_ids),
            review_required,
        )

    async def summarize(
        self,
        tenant_id: str,
        index_version: str,
    ) -> RetrievalMetricSummary:
        return await asyncio.to_thread(self._summarize_sync, tenant_id, index_version)

    def _save_audit_sync(self, event: GovernanceAuditEvent) -> GovernanceAuditEvent:
        values = {
            "audit_id": event.audit_id,
            "tenant_id": event.tenant_id,
            "action": event.action.value,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "reviewer_id": event.reviewer_id,
            "reason": event.reason,
            "resource_version": event.resource_version,
            "trace_id": event.trace_id,
            "idempotency_key_hash": event.idempotency_key_hash,
            "created_at": event.created_at,
        }
        try:
            with self._sessions.begin() as session:
                self._insert_do_nothing(session, MemoryGovernanceAuditRow, values)
                row = session.scalar(
                    select(MemoryGovernanceAuditRow).where(
                        MemoryGovernanceAuditRow.tenant_id == event.tenant_id,
                        MemoryGovernanceAuditRow.action == event.action.value,
                        MemoryGovernanceAuditRow.idempotency_key_hash
                        == event.idempotency_key_hash,
                    )
                )
                if row is None:
                    raise WorkflowPersistenceError("Governance audit was not persisted")
                persisted = self._audit_from_row(row)
                if self._canonical(self._audit_semantic_payload(persisted)) != self._canonical(
                    self._audit_semantic_payload(event)
                ):
                    raise WorkflowPersistenceError(
                        "Governance idempotency key is bound to a different audit event"
                    )
                return persisted
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to persist memory governance audit") from exc

    def _list_audits_sync(
        self,
        tenant_id: str,
        action: GovernanceAction | None,
        resource_type: str | None,
        resource_id: str | None,
        trace_id: str | None,
        started_at: datetime | None,
        ended_at: datetime | None,
        limit: int,
        after_audit_id: str | None,
    ) -> tuple[GovernanceAuditEvent, ...]:
        self._require_text("tenant_id", tenant_id)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        for name, value in (
            ("resource_type", resource_type),
            ("resource_id", resource_id),
            ("trace_id", trace_id),
            ("after_audit_id", after_audit_id),
        ):
            if value is not None:
                self._require_text(name, value)
        try:
            with self._sessions() as session:
                statement = select(MemoryGovernanceAuditRow).where(
                    MemoryGovernanceAuditRow.tenant_id == tenant_id
                )
                if action is not None:
                    statement = statement.where(
                        MemoryGovernanceAuditRow.action == action.value
                    )
                if resource_type is not None:
                    statement = statement.where(
                        MemoryGovernanceAuditRow.resource_type == resource_type
                    )
                if resource_id is not None:
                    statement = statement.where(
                        MemoryGovernanceAuditRow.resource_id == resource_id
                    )
                if trace_id is not None:
                    statement = statement.where(
                        MemoryGovernanceAuditRow.trace_id == trace_id
                    )
                if started_at is not None:
                    statement = statement.where(
                        MemoryGovernanceAuditRow.created_at >= started_at
                    )
                if ended_at is not None:
                    statement = statement.where(
                        MemoryGovernanceAuditRow.created_at <= ended_at
                    )
                if after_audit_id is not None:
                    cursor = session.scalar(
                        select(MemoryGovernanceAuditRow).where(
                            MemoryGovernanceAuditRow.tenant_id == tenant_id,
                            MemoryGovernanceAuditRow.audit_id == after_audit_id,
                        )
                    )
                    if cursor is None:
                        return ()
                    statement = statement.where(
                        or_(
                            MemoryGovernanceAuditRow.created_at < cursor.created_at,
                            and_(
                                MemoryGovernanceAuditRow.created_at == cursor.created_at,
                                MemoryGovernanceAuditRow.audit_id < cursor.audit_id,
                            ),
                        )
                    )
                rows = session.scalars(
                    statement.order_by(
                        MemoryGovernanceAuditRow.created_at.desc(),
                        MemoryGovernanceAuditRow.audit_id.desc(),
                    ).limit(limit)
                ).all()
                return tuple(self._audit_from_row(row) for row in rows)
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError(
                "Unable to list memory governance audits"
            ) from exc

    def _get_audit_by_idempotency_hash_sync(
        self,
        tenant_id: str,
        action: GovernanceAction,
        idempotency_key_hash: str,
    ) -> GovernanceAuditEvent | None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("idempotency_key_hash", idempotency_key_hash)
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(MemoryGovernanceAuditRow).where(
                        MemoryGovernanceAuditRow.tenant_id == tenant_id,
                        MemoryGovernanceAuditRow.action == action.value,
                        MemoryGovernanceAuditRow.idempotency_key_hash
                        == idempotency_key_hash,
                    )
                )
                return self._audit_from_row(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError(
                "Unable to read memory governance audit"
            ) from exc

    def _save_feedback_sync(
        self,
        feedback: RetrievalFeedback,
        audit_event: GovernanceAuditEvent,
    ) -> RetrievalFeedback:
        if (
            audit_event.tenant_id != feedback.tenant_id
            or audit_event.action is not GovernanceAction.SUBMIT_FEEDBACK
            or audit_event.resource_type != "retrieval_feedback"
            or audit_event.resource_id != feedback.feedback_id
            or audit_event.resource_version is not None
            or audit_event.reviewer_id != feedback.reviewer_id
        ):
            raise ValueError("Feedback audit scope is inconsistent")
        fingerprint = sha256(
            self._canonical(
                {
                    "tenant_id": feedback.tenant_id,
                    "trace_id": feedback.trace_id,
                    "example_id": feedback.example_id,
                    "label": feedback.label.value,
                    "reviewer_id": feedback.reviewer_id,
                    "reason": feedback.reason,
                }
            ).encode("utf-8")
        ).hexdigest()
        values = {
            "feedback_id": feedback.feedback_id,
            "tenant_id": feedback.tenant_id,
            "trace_id": feedback.trace_id,
            "example_id": feedback.example_id,
            "label": feedback.label.value,
            "reviewer_id": feedback.reviewer_id,
            "reason": feedback.reason,
            "fingerprint": fingerprint,
            "created_at": feedback.created_at,
        }
        try:
            with self._sessions.begin() as session:
                self._insert_do_nothing(session, MemoryRetrievalFeedbackRow, values)
                row = session.scalar(
                    select(MemoryRetrievalFeedbackRow).where(
                        MemoryRetrievalFeedbackRow.tenant_id == feedback.tenant_id,
                        MemoryRetrievalFeedbackRow.fingerprint == fingerprint,
                    )
                )
                if row is None:
                    raise WorkflowPersistenceError("Retrieval feedback was not persisted")
                persisted = self._feedback_from_row(row)
                if self._canonical(self._feedback_payload(persisted)) != self._canonical(
                    self._feedback_payload(feedback)
                ):
                    raise WorkflowPersistenceError(
                        "Retrieval feedback fingerprint is bound to different data"
                    )
                add_governance_audit(session, audit_event)
                return persisted
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to persist retrieval feedback") from exc

    def _get_feedback_sync(
        self,
        tenant_id: str,
        feedback_id: str,
    ) -> RetrievalFeedback | None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("feedback_id", feedback_id)
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(MemoryRetrievalFeedbackRow).where(
                        MemoryRetrievalFeedbackRow.tenant_id == tenant_id,
                        MemoryRetrievalFeedbackRow.feedback_id == feedback_id,
                    )
                )
                return self._feedback_from_row(row) if row is not None else None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to retrieve retrieval feedback") from exc

    def _save_trace_sync(self, trace: RetrievalTrace) -> None:
        values = self._trace_values(trace)
        try:
            with self._sessions.begin() as session:
                self._insert_do_nothing(session, RetrievalTraceRow, values)
                row = session.get(RetrievalTraceRow, trace.trace_id)
                if row is None:
                    raise WorkflowPersistenceError("Retrieval trace was not persisted")
                persisted = self._trace_from_row(row)
                if self._canonical(self._immutable_trace_payload(persisted)) != self._canonical(
                    self._immutable_trace_payload(trace)
                ):
                    raise WorkflowPersistenceError(
                        "Retrieval trace identifier is bound to different immutable data"
                    )
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to persist retrieval trace") from exc

    def _get_trace_sync(
        self,
        tenant_id: str,
        trace_id: str,
    ) -> RetrievalTrace | None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("trace_id", trace_id)
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(RetrievalTraceRow).where(
                        RetrievalTraceRow.tenant_id == tenant_id,
                        RetrievalTraceRow.trace_id == trace_id,
                    )
                )
                return self._trace_from_row(row) if row is not None else None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to retrieve retrieval trace") from exc

    def _mark_review_required_sync(
        self,
        tenant_id: str,
        trace_ids: tuple[str, ...],
        review_required: bool,
    ) -> None:
        self._require_text("tenant_id", tenant_id)
        if not trace_ids:
            return
        if len(trace_ids) != len(set(trace_ids)):
            raise ValueError("trace_ids must be unique")
        try:
            with self._sessions.begin() as session:
                session.execute(
                    update(RetrievalTraceRow)
                    .where(
                        RetrievalTraceRow.tenant_id == tenant_id,
                        RetrievalTraceRow.trace_id.in_(trace_ids),
                        RetrievalTraceRow.review_required.is_(None),
                    )
                    .values(review_required=review_required)
                )
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to bind retrieval validation route") from exc

    def _summarize_sync(
        self,
        tenant_id: str,
        index_version: str,
    ) -> RetrievalMetricSummary:
        self._require_text("tenant_id", tenant_id)
        self._require_text("index_version", index_version)
        try:
            with self._sessions() as session:
                row = session.execute(
                    select(
                        func.count(RetrievalTraceRow.trace_id),
                        func.sum(case((RetrievalTraceRow.empty_retrieval.is_(True), 1), else_=0)),
                        func.avg(RetrievalTraceRow.positive_hit_rate),
                        func.avg(RetrievalTraceRow.negative_hit_rate),
                        func.sum(
                            case((RetrievalTraceRow.review_required.is_not(None), 1), else_=0)
                        ),
                        func.sum(
                            case((RetrievalTraceRow.review_required.is_(True), 1), else_=0)
                        ),
                        func.sum(
                            case((RetrievalTraceRow.remote_model_error_count > 0, 1), else_=0)
                        ),
                        func.sum(func.coalesce(RetrievalTraceRow.input_tokens, 0)),
                        func.sum(func.coalesce(RetrievalTraceRow.output_tokens, 0)),
                        func.sum(RetrievalTraceRow.estimated_cost),
                        func.count(RetrievalTraceRow.estimated_cost),
                    ).where(
                        RetrievalTraceRow.tenant_id == tenant_id,
                        RetrievalTraceRow.index_version == index_version,
                    )
                ).one()
                trace_count = int(row[0] or 0)
                routed_count = int(row[4] or 0)
                cost_count = int(row[10] or 0)
                return RetrievalMetricSummary(
                    trace_count=trace_count,
                    empty_retrieval_rate=(float(row[1] or 0) / trace_count if trace_count else 0.0),
                    positive_hit_rate=float(row[2] or 0.0),
                    negative_hit_rate=float(row[3] or 0.0),
                    review_required_rate=(
                        float(row[5] or 0) / routed_count if routed_count else None
                    ),
                    remote_model_error_rate=(
                        float(row[6] or 0) / trace_count if trace_count else 0.0
                    ),
                    total_input_tokens=int(row[7] or 0),
                    total_output_tokens=int(row[8] or 0),
                    total_estimated_cost=(float(row[9]) if cost_count else None),
                )
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to summarize retrieval telemetry") from exc

    @staticmethod
    def _trace_values(trace: RetrievalTrace) -> dict[str, Any]:
        return {
            "trace_id": trace.trace_id,
            "tenant_id": trace.tenant_id,
            "document_type": trace.document_type,
            "field_path": trace.field_path,
            "schema_version": trace.schema_version,
            "index_version": trace.index_version.value if trace.index_version else None,
            "dense_model_version": trace.dense_model_version.value,
            "sparse_model_version": (
                trace.sparse_model_version.value if trace.sparse_model_version else None
            ),
            "rerank_model_version": (
                trace.rerank_model_version.value if trace.rerank_model_version else None
            ),
            "prompt_version": trace.prompt_version.value,
            "retrieval_policy_version": trace.retrieval_policy_version.value,
            "threshold_version": trace.threshold_version,
            "stage_metrics_json": {
                name: getattr(trace.stage_metrics, name)
                for name in trace.stage_metrics.__dataclass_fields__
            },
            "dense_candidate_count": trace.dense_candidate_count,
            "sparse_candidate_count": trace.sparse_candidate_count,
            "rerank_candidate_count": trace.rerank_candidate_count,
            "positive_result_count": trace.positive_result_count,
            "negative_result_count": trace.negative_result_count,
            "positive_example_ids_json": list(trace.positive_example_ids),
            "negative_example_ids_json": list(trace.negative_example_ids),
            "empty_retrieval": trace.empty_retrieval,
            "positive_hit_rate": trace.positive_hit_rate,
            "negative_hit_rate": trace.negative_hit_rate,
            "review_required": trace.review_required,
            "remote_model_error_count": trace.remote_model_error_count,
            "input_tokens": trace.input_tokens,
            "output_tokens": trace.output_tokens,
            "estimated_cost": trace.estimated_cost,
            "succeeded": trace.succeeded,
            "error_code": trace.error_code,
            "created_at": trace.created_at,
            "completed_at": trace.completed_at,
        }

    @classmethod
    def _trace_from_row(cls, row: RetrievalTraceRow) -> RetrievalTrace:
        metrics = cast(dict[str, object], row.stage_metrics_json)
        return RetrievalTrace(
            trace_id=row.trace_id,
            tenant_id=row.tenant_id,
            document_type=row.document_type,
            field_path=row.field_path,
            schema_version=row.schema_version,
            index_version=IndexVersion(row.index_version) if row.index_version else None,
            dense_model_version=ModelVersion(row.dense_model_version),
            sparse_model_version=(
                ModelVersion(row.sparse_model_version) if row.sparse_model_version else None
            ),
            rerank_model_version=(
                ModelVersion(row.rerank_model_version) if row.rerank_model_version else None
            ),
            prompt_version=PromptVersion(row.prompt_version),
            retrieval_policy_version=RetrievalPolicyVersion(row.retrieval_policy_version),
            threshold_version=row.threshold_version,
            stage_metrics=RetrievalStageMetrics(
                **{name: float(metrics[name]) for name in RetrievalStageMetrics.__dataclass_fields__}
            ),
            dense_candidate_count=row.dense_candidate_count,
            sparse_candidate_count=row.sparse_candidate_count,
            rerank_candidate_count=row.rerank_candidate_count,
            positive_result_count=row.positive_result_count,
            negative_result_count=row.negative_result_count,
            positive_example_ids=tuple(row.positive_example_ids_json),
            negative_example_ids=tuple(row.negative_example_ids_json),
            empty_retrieval=row.empty_retrieval,
            positive_hit_rate=row.positive_hit_rate,
            negative_hit_rate=row.negative_hit_rate,
            review_required=row.review_required,
            remote_model_error_count=row.remote_model_error_count,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            estimated_cost=row.estimated_cost,
            succeeded=row.succeeded,
            error_code=row.error_code,
            created_at=cls._aware(row.created_at),
            completed_at=cls._aware(row.completed_at),
        )

    @staticmethod
    def _immutable_trace_payload(trace: RetrievalTrace) -> dict[str, Any]:
        values = SQLAlchemyMemoryGovernanceRepository._trace_values(trace)
        values.pop("review_required")
        return values

    @staticmethod
    def _feedback_from_row(row: MemoryRetrievalFeedbackRow) -> RetrievalFeedback:
        return RetrievalFeedback(
            feedback_id=row.feedback_id,
            tenant_id=row.tenant_id,
            trace_id=row.trace_id,
            example_id=row.example_id,
            label=RetrievalFeedbackLabel(row.label),
            reviewer_id=row.reviewer_id,
            reason=row.reason,
            created_at=SQLAlchemyMemoryGovernanceRepository._aware(row.created_at),
        )

    @staticmethod
    def _feedback_payload(feedback: RetrievalFeedback) -> dict[str, object]:
        return {
            "tenant_id": feedback.tenant_id,
            "trace_id": feedback.trace_id,
            "example_id": feedback.example_id,
            "label": feedback.label.value,
            "reviewer_id": feedback.reviewer_id,
            "reason": feedback.reason,
        }

    @staticmethod
    def _audit_from_row(row: MemoryGovernanceAuditRow) -> GovernanceAuditEvent:
        return GovernanceAuditEvent(
            audit_id=row.audit_id,
            tenant_id=row.tenant_id,
            action=GovernanceAction(row.action),
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            reviewer_id=row.reviewer_id,
            reason=row.reason,
            resource_version=row.resource_version,
            trace_id=row.trace_id,
            idempotency_key_hash=row.idempotency_key_hash,
            created_at=SQLAlchemyMemoryGovernanceRepository._aware(row.created_at),
        )

    @staticmethod
    def _audit_semantic_payload(event: GovernanceAuditEvent) -> dict[str, object]:
        return {
            "audit_id": event.audit_id,
            "tenant_id": event.tenant_id,
            "action": event.action.value,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "reviewer_id": event.reviewer_id,
            "reason": event.reason,
            "resource_version": event.resource_version,
            "trace_id": event.trace_id,
            "idempotency_key_hash": event.idempotency_key_hash,
        }

    def _insert_do_nothing(
        self,
        session: Session,
        model: type[Any],
        values: dict[str, Any],
    ) -> None:
        if self._dialect_name == "postgresql":
            statement = pg_insert(model).values(**values).on_conflict_do_nothing()
        elif self._dialect_name == "sqlite":
            statement = sqlite_insert(model).values(**values).on_conflict_do_nothing()
        else:
            raise WorkflowPersistenceError("Unsupported business database dialect")
        session.execute(statement)

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _require_text(name: str, value: str) -> None:
        if not value.strip():
            raise ValueError(f"{name} must not be empty")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
