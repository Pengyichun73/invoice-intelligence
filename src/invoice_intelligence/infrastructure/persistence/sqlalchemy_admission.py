"""SQLAlchemy adapters for trusted-memory admission facts."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Engine, delete, exists, func, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import WorkflowPersistenceError
from invoice_intelligence.application.ports.admission import MemoryAdmissionWorkLease
from invoice_intelligence.domain.admission import (
    MemoryAdmissionDecision,
    MemoryAdmissionDecisionAuthority,
    MemoryAdmissionRecommendation,
    MemoryAdmissionRecord,
    MemoryAdmissionStatus,
    MemoryAssessmentSource,
    MemoryConflictRecord,
    MemoryConflictReevaluationTarget,
    MemoryConflictReevaluationTargetType,
    MemoryConflictResolutionDecision,
    MemoryConflictStatus,
    MemoryQualityAssessment,
    MemoryQualitySignal,
    ReviewerReliabilityProfile,
)
from invoice_intelligence.domain.examples import ModelVersion, PromptVersion
from invoice_intelligence.domain.governance import GovernanceAction, GovernanceAuditEvent
from invoice_intelligence.domain.workflow import SignalVerdict
from invoice_intelligence.infrastructure.persistence.sqlalchemy_governance import (
    add_governance_audit,
    require_governance_audit,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    ExampleFeedbackRow,
    ExampleIndexProjectionRow,
    FieldAliasCandidateDecisionRow,
    FieldAliasCandidateRow,
    MemoryAdmissionDecisionRow,
    MemoryAdmissionRecordRow,
    MemoryConflictExampleRow,
    MemoryConflictFieldAliasCandidateRow,
    MemoryConflictReevaluationRequestRow,
    MemoryConflictResolutionDecisionRow,
    MemoryConflictRow,
    MemoryQualityAssessmentRow,
    MemoryQualitySignalRow,
    ModelVersionRow,
    PromptVersionRow,
    ReviewedExampleRow,
    ReviewerReliabilitySnapshotRow,
)


class _SQLAlchemyAdmissionStore:
    def __init__(self, engine: Engine) -> None:
        self._dialect_name = engine.dialect.name
        self._sessions = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        )

    def _insert_do_nothing(
        self,
        session: Session,
        model: type[Any],
        values: dict[str, Any],
    ) -> bool:
        statement: Any
        if self._dialect_name == "postgresql":
            statement = pg_insert(model).values(**values).on_conflict_do_nothing()
        elif self._dialect_name == "sqlite":
            statement = sqlite_insert(model).values(**values).on_conflict_do_nothing()
        else:
            raise WorkflowPersistenceError("Unsupported business database dialect")
        return session.scalar(statement.returning(literal(1))) is not None

    @staticmethod
    def _require_text(name: str, value: str) -> None:
        if not value.strip() or value != value.strip():
            raise ValueError(f"{name} must be non-empty and normalized")

    @staticmethod
    def _require_aware(name: str, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _string_tuple(value: object, name: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise WorkflowPersistenceError(f"Persisted {name} must be a string array")
        return tuple(value)

    @staticmethod
    def _version_id(kind: str, tenant_id: str, version: str) -> str:
        return sha256(f"{kind}\0{tenant_id}\0{version}".encode("utf-8")).hexdigest()

    def _ensure_model_version(
        self,
        session: Session,
        tenant_id: str,
        version: ModelVersion,
    ) -> str:
        version_id = self._version_id("model", tenant_id, version.value)
        self._insert_do_nothing(
            session,
            ModelVersionRow,
            {
                "model_version_id": version_id,
                "tenant_id": tenant_id,
                "version": version.value,
                "created_at": datetime.now(UTC),
            },
        )
        row = session.get(ModelVersionRow, version_id)
        if row is None or row.tenant_id != tenant_id or row.version != version.value:
            raise WorkflowPersistenceError("Model version is bound to different data")
        return version_id

    def _ensure_prompt_version(
        self,
        session: Session,
        tenant_id: str,
        version: PromptVersion,
    ) -> str:
        version_id = self._version_id("prompt", tenant_id, version.value)
        self._insert_do_nothing(
            session,
            PromptVersionRow,
            {
                "prompt_version_id": version_id,
                "tenant_id": tenant_id,
                "version": version.value,
                "created_at": datetime.now(UTC),
            },
        )
        row = session.get(PromptVersionRow, version_id)
        if row is None or row.tenant_id != tenant_id or row.version != version.value:
            raise WorkflowPersistenceError("Prompt version is bound to different data")
        return version_id

    @staticmethod
    def _require_reviewed_example(
        session: Session,
        tenant_id: str,
        example_id: str,
        *,
        require_valid: bool,
    ) -> ReviewedExampleRow:
        row = session.get(ReviewedExampleRow, example_id)
        if (
            row is None
            or row.tenant_id != tenant_id
            or not row.is_reviewed
            or (require_valid and not row.is_valid)
        ):
            raise WorkflowPersistenceError("Reviewed example is outside the eligible tenant scope")
        return row


class SQLAlchemyMemoryAdmissionRepository(_SQLAlchemyAdmissionStore):
    """Persist current admission state, immutable decisions and assessments."""

    async def create_pending(
        self,
        initial_decision: MemoryAdmissionDecision,
    ) -> MemoryAdmissionRecord:
        return await asyncio.to_thread(self._create_pending_sync, initial_decision)

    async def get(
        self,
        tenant_id: str,
        example_id: str,
    ) -> MemoryAdmissionRecord | None:
        return await asyncio.to_thread(self._get_sync, tenant_id, example_id)

    async def save_assessment(
        self,
        assessment: MemoryQualityAssessment,
    ) -> MemoryQualityAssessment:
        return await asyncio.to_thread(self._save_assessment_sync, assessment)

    async def list_assessments(
        self,
        tenant_id: str,
        example_id: str,
    ) -> tuple[MemoryQualityAssessment, ...]:
        return await asyncio.to_thread(
            self._list_assessments_sync,
            tenant_id,
            example_id,
        )

    async def list_decisions(
        self,
        tenant_id: str,
        example_id: str,
    ) -> tuple[MemoryAdmissionDecision, ...]:
        return await asyncio.to_thread(self._list_decisions_sync, tenant_id, example_id)

    async def append_decision(
        self,
        decision: MemoryAdmissionDecision,
        *,
        expected_revision: int,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> MemoryAdmissionRecord:
        return await asyncio.to_thread(
            self._append_decision_sync,
            decision,
            expected_revision,
            audit_event,
        )

    async def claim_due(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[MemoryAdmissionWorkLease, ...]:
        return await asyncio.to_thread(
            self._claim_due_sync,
            worker_id,
            now,
            lease_expires_at,
            limit,
        )

    async def complete_claim(
        self,
        lease: MemoryAdmissionWorkLease,
        *,
        error_code: str | None = None,
        error_at: datetime | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._complete_claim_sync,
            lease,
            error_code,
            error_at,
        )

    async def retry_claim(
        self,
        lease: MemoryAdmissionWorkLease,
        *,
        next_attempt_at: datetime,
        error_code: str,
        error_at: datetime,
    ) -> bool:
        return await asyncio.to_thread(
            self._retry_claim_sync,
            lease,
            next_attempt_at,
            error_code,
            error_at,
        )

    async def filter_approved_example_ids(
        self,
        tenant_id: str,
        example_ids: Sequence[str],
    ) -> tuple[str, ...]:
        return await asyncio.to_thread(
            self._filter_approved_example_ids_sync,
            tenant_id,
            tuple(example_ids),
        )

    async def list_approved_example_ids(
        self,
        tenant_id: str,
        schema_version: str,
        *,
        limit: int,
        after_example_id: str | None = None,
    ) -> tuple[str, ...]:
        return await asyncio.to_thread(
            self._list_approved_example_ids_sync,
            tenant_id,
            schema_version,
            limit,
            after_example_id,
        )

    async def list_by_status(
        self,
        tenant_id: str,
        statuses: Sequence[MemoryAdmissionStatus],
        *,
        limit: int,
        after_example_id: str | None = None,
        run_id: str | None = None,
        field_path: str | None = None,
    ) -> tuple[MemoryAdmissionRecord, ...]:
        return await asyncio.to_thread(
            self._list_by_status_sync,
            tenant_id,
            tuple(statuses),
            limit,
            after_example_id,
            run_id,
            field_path,
        )

    async def list_for_schema(
        self,
        tenant_id: str,
        schema_version: str,
        *,
        limit: int,
        after_example_id: str | None = None,
    ) -> tuple[MemoryAdmissionRecord, ...]:
        return await asyncio.to_thread(
            self._list_for_schema_sync,
            tenant_id,
            schema_version,
            limit,
            after_example_id,
        )

    async def delete_tenant(self, tenant_id: str) -> int:
        return await asyncio.to_thread(self._delete_tenant_sync, tenant_id)

    async def purge_terminal_before(
        self,
        tenant_id: str,
        older_than: datetime,
    ) -> int:
        return await asyncio.to_thread(
            self._purge_terminal_before_sync,
            tenant_id,
            older_than,
        )

    def _create_pending_sync(
        self,
        decision: MemoryAdmissionDecision,
    ) -> MemoryAdmissionRecord:
        if (
            decision.previous_status is not None
            or decision.status is not MemoryAdmissionStatus.PENDING
        ):
            raise ValueError("Initial admission decision must create pending status")
        try:
            with self._sessions.begin() as session:
                example = self._require_reviewed_example(
                    session,
                    decision.tenant_id,
                    decision.example_id,
                    require_valid=True,
                )
                self._insert_decision(session, decision)
                values = {
                    "example_id": decision.example_id,
                    "tenant_id": decision.tenant_id,
                    "schema_version": example.schema_version,
                    "status": decision.status.value,
                    "current_decision_id": decision.decision_id,
                    "policy_version": decision.policy_version,
                    "revision": decision.revision,
                    "worker_id": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "attempt_count": 0,
                    "next_attempt_at": decision.decided_at,
                    "last_error_code": None,
                    "last_error_at": None,
                    "created_at": decision.decided_at,
                    "updated_at": decision.decided_at,
                }
                self._insert_do_nothing(session, MemoryAdmissionRecordRow, values)
                row = session.get(MemoryAdmissionRecordRow, decision.example_id)
                if row is None or row.tenant_id != decision.tenant_id:
                    raise WorkflowPersistenceError("Pending admission was not persisted")
                persisted_decision = session.scalar(
                    select(MemoryAdmissionDecisionRow).where(
                        MemoryAdmissionDecisionRow.tenant_id == decision.tenant_id,
                        MemoryAdmissionDecisionRow.example_id == decision.example_id,
                        MemoryAdmissionDecisionRow.revision == 1,
                    )
                )
                if (
                    persisted_decision is None
                    or self._decision_from_row(persisted_decision) != decision
                ):
                    raise WorkflowPersistenceError(
                        "Reviewed example is already bound to another admission"
                    )
                return self._record_from_row(row)
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to create pending memory admission") from exc

    def _get_sync(
        self,
        tenant_id: str,
        example_id: str,
    ) -> MemoryAdmissionRecord | None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("example_id", example_id)
        try:
            with self._sessions() as session:
                row = session.get(MemoryAdmissionRecordRow, example_id)
                if row is None or row.tenant_id != tenant_id:
                    return None
                return self._record_from_row(row)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read memory admission") from exc

    def _save_assessment_sync(
        self,
        assessment: MemoryQualityAssessment,
    ) -> MemoryQualityAssessment:
        try:
            with self._sessions.begin() as session:
                self._require_reviewed_example(
                    session,
                    assessment.tenant_id,
                    assessment.example_id,
                    require_valid=True,
                )
                admission = session.get(MemoryAdmissionRecordRow, assessment.example_id)
                if admission is None or admission.tenant_id != assessment.tenant_id:
                    raise WorkflowPersistenceError("Assessment requires an admission record")
                model_version_id = (
                    self._ensure_model_version(
                        session,
                        assessment.tenant_id,
                        assessment.model_version,
                    )
                    if assessment.model_version is not None
                    else None
                )
                prompt_version_id = (
                    self._ensure_prompt_version(
                        session,
                        assessment.tenant_id,
                        assessment.prompt_version,
                    )
                    if assessment.prompt_version is not None
                    else None
                )
                values = {
                    "assessment_id": assessment.assessment_id,
                    "tenant_id": assessment.tenant_id,
                    "example_id": assessment.example_id,
                    "source": assessment.source.value,
                    "quality_score": assessment.quality_score,
                    "recommendation": assessment.recommendation.value,
                    "reason_codes_json": list(assessment.reason_codes),
                    "policy_version": assessment.policy_version,
                    "input_fingerprint": assessment.input_fingerprint,
                    "model_version_id": model_version_id,
                    "prompt_version_id": prompt_version_id,
                    "advisory_only": True,
                    "assessed_at": assessment.created_at,
                }
                created = self._insert_do_nothing(
                    session,
                    MemoryQualityAssessmentRow,
                    values,
                )
                row = session.scalar(
                    select(MemoryQualityAssessmentRow).where(
                        MemoryQualityAssessmentRow.tenant_id == assessment.tenant_id,
                        MemoryQualityAssessmentRow.example_id == assessment.example_id,
                        MemoryQualityAssessmentRow.source == assessment.source.value,
                        MemoryQualityAssessmentRow.policy_version == assessment.policy_version,
                        MemoryQualityAssessmentRow.input_fingerprint
                        == assessment.input_fingerprint,
                    )
                )
                if row is None:
                    raise WorkflowPersistenceError("Assessment replay did not produce a row")
                if created:
                    for ordinal, signal in enumerate(assessment.signals):
                        session.add(
                            MemoryQualitySignalRow(
                                signal_id=self._signal_id(row.assessment_id, ordinal),
                                assessment_id=row.assessment_id,
                                ordinal=ordinal,
                                code=signal.code,
                                source=signal.source.value,
                                verdict=signal.verdict.value,
                                score=signal.score,
                                message=signal.message,
                                field_path=signal.field_path,
                                evidence_references_json=list(signal.evidence_references),
                            )
                        )
                    session.flush()
                persisted = self._assessment_from_row(session, row)
                if row.assessment_id == assessment.assessment_id and persisted != assessment:
                    raise WorkflowPersistenceError(
                        "Assessment identifier is bound to different immutable data"
                    )
                return persisted
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to persist memory assessment") from exc

    def _list_assessments_sync(
        self,
        tenant_id: str,
        example_id: str,
    ) -> tuple[MemoryQualityAssessment, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("example_id", example_id)
        try:
            with self._sessions() as session:
                rows = session.scalars(
                    select(MemoryQualityAssessmentRow)
                    .where(
                        MemoryQualityAssessmentRow.tenant_id == tenant_id,
                        MemoryQualityAssessmentRow.example_id == example_id,
                    )
                    .order_by(
                        MemoryQualityAssessmentRow.assessed_at,
                        MemoryQualityAssessmentRow.assessment_id,
                    )
                ).all()
                return tuple(self._assessment_from_row(session, row) for row in rows)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to list memory assessments") from exc

    def _list_decisions_sync(
        self,
        tenant_id: str,
        example_id: str,
    ) -> tuple[MemoryAdmissionDecision, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("example_id", example_id)
        try:
            with self._sessions() as session:
                rows = session.scalars(
                    select(MemoryAdmissionDecisionRow)
                    .where(
                        MemoryAdmissionDecisionRow.tenant_id == tenant_id,
                        MemoryAdmissionDecisionRow.example_id == example_id,
                    )
                    .order_by(MemoryAdmissionDecisionRow.revision)
                ).all()
                return tuple(self._decision_from_row(row) for row in rows)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to list admission decisions") from exc

    def _append_decision_sync(
        self,
        decision: MemoryAdmissionDecision,
        expected_revision: int,
        audit_event: GovernanceAuditEvent | None,
    ) -> MemoryAdmissionRecord:
        if decision.previous_status is None:
            raise ValueError("Admission transition requires a previous status")
        if expected_revision <= 0:
            raise ValueError("expected_revision must be greater than zero")
        try:
            with self._sessions.begin() as session:
                replay = session.scalar(
                    select(MemoryAdmissionDecisionRow).where(
                        MemoryAdmissionDecisionRow.tenant_id == decision.tenant_id,
                        MemoryAdmissionDecisionRow.idempotency_key_hash
                        == decision.idempotency_key_hash,
                    )
                )
                if replay is not None:
                    if self._decision_from_row(replay) != decision:
                        raise WorkflowPersistenceError(
                            "Admission idempotency key is bound to another decision"
                        )
                    current = session.get(MemoryAdmissionRecordRow, decision.example_id)
                    if current is None or current.current_decision_id != replay.decision_id:
                        raise WorkflowPersistenceError(
                            "Admission decision replay has already been superseded"
                        )
                    if audit_event is not None:
                        require_governance_audit(session, audit_event)
                    return self._record_from_row(current)

                statement = select(MemoryAdmissionRecordRow).where(
                    MemoryAdmissionRecordRow.tenant_id == decision.tenant_id,
                    MemoryAdmissionRecordRow.example_id == decision.example_id,
                )
                if self._dialect_name == "postgresql":
                    statement = statement.with_for_update()
                row = session.scalar(statement)
                if row is None:
                    raise WorkflowPersistenceError("Memory admission does not exist")
                self._require_reviewed_example(
                    session,
                    decision.tenant_id,
                    decision.example_id,
                    require_valid=decision.status is not MemoryAdmissionStatus.INVALIDATED,
                )
                if row.revision != expected_revision:
                    raise WorkflowPersistenceError("Memory admission revision conflict")
                if decision.previous_status.value != row.status:
                    raise WorkflowPersistenceError("Admission previous status is stale")
                if decision.revision != expected_revision + 1:
                    raise WorkflowPersistenceError("Admission revision is not sequential")
                self._validate_decision_references(session, decision)
                self._insert_decision(session, decision)
                if audit_event is not None:
                    expected_action = {
                        MemoryAdmissionStatus.PENDING: GovernanceAction.REQUEUE_ADMISSION,
                        MemoryAdmissionStatus.APPROVED: GovernanceAction.APPROVE_ADMISSION,
                        MemoryAdmissionStatus.REJECTED: GovernanceAction.REJECT_ADMISSION,
                        MemoryAdmissionStatus.QUARANTINED: (
                            GovernanceAction.QUARANTINE_ADMISSION
                        ),
                    }.get(decision.status)
                    if (
                        expected_action is None
                        or audit_event.action is not expected_action
                        or audit_event.tenant_id != decision.tenant_id
                        or audit_event.resource_type != "memory_admission"
                        or audit_event.resource_id != decision.example_id
                        or audit_event.resource_version != str(decision.revision)
                        or audit_event.reviewer_id != decision.decided_by
                    ):
                        raise WorkflowPersistenceError(
                            "Admission governance audit scope is inconsistent"
                        )
                    add_governance_audit(session, audit_event)
                row.status = decision.status.value
                row.current_decision_id = decision.decision_id
                row.policy_version = decision.policy_version
                row.revision = decision.revision
                row.updated_at = decision.decided_at
                row.attempt_count = 0
                row.next_attempt_at = decision.decided_at
                row.last_error_code = None
                row.last_error_at = None
                if decision.status is not MemoryAdmissionStatus.APPROVED:
                    session.execute(
                        update(ExampleIndexProjectionRow)
                        .where(
                            ExampleIndexProjectionRow.tenant_id == decision.tenant_id,
                            ExampleIndexProjectionRow.example_id == decision.example_id,
                        )
                        .values(
                            status="invalidated",
                            invalidated_at=decision.decided_at,
                            updated_at=decision.decided_at,
                        )
                    )
                return self._record_from_row(row)
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to append admission decision") from exc

    def _claim_due_sync(
        self,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[MemoryAdmissionWorkLease, ...]:
        self._require_text("worker_id", worker_id)
        self._require_aware("now", now)
        self._require_aware("lease_expires_at", lease_expires_at)
        if lease_expires_at <= now:
            raise ValueError("lease_expires_at must be later than now")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if self._dialect_name != "postgresql":
            raise WorkflowPersistenceError(
                "Memory admission Worker requires PostgreSQL"
            )
        try:
            with self._sessions.begin() as session:
                rows = session.scalars(
                    select(MemoryAdmissionRecordRow)
                    .where(
                        MemoryAdmissionRecordRow.status
                        == MemoryAdmissionStatus.PENDING.value,
                        MemoryAdmissionRecordRow.next_attempt_at.is_not(None),
                        MemoryAdmissionRecordRow.next_attempt_at <= now,
                        (
                            MemoryAdmissionRecordRow.lease_expires_at.is_(None)
                            | (MemoryAdmissionRecordRow.lease_expires_at <= now)
                        ),
                    )
                    .order_by(
                        MemoryAdmissionRecordRow.next_attempt_at,
                        MemoryAdmissionRecordRow.example_id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                ).all()
                leases: list[MemoryAdmissionWorkLease] = []
                for row in rows:
                    lease_token = uuid4().hex
                    row.worker_id = worker_id
                    row.lease_token = lease_token
                    row.lease_expires_at = lease_expires_at
                    row.attempt_count += 1
                    leases.append(
                        MemoryAdmissionWorkLease(
                            tenant_id=row.tenant_id,
                            example_id=row.example_id,
                            worker_id=worker_id,
                            lease_token=lease_token,
                            attempt_count=row.attempt_count,
                            lease_expires_at=lease_expires_at,
                        )
                    )
                session.flush()
                return tuple(leases)
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to claim memory admission work"
            ) from exc

    def _complete_claim_sync(
        self,
        lease: MemoryAdmissionWorkLease,
        error_code: str | None,
        error_at: datetime | None,
    ) -> bool:
        if (error_code is None) != (error_at is None):
            raise ValueError("error_code and error_at must be supplied together")
        if error_code is not None:
            self._require_text("error_code", error_code)
            if len(error_code) > 128:
                raise ValueError("error_code exceeds the persisted limit")
        if error_at is not None:
            self._require_aware("error_at", error_at)
        values: dict[str, object | None] = {
            "worker_id": None,
            "lease_token": None,
            "lease_expires_at": None,
            "next_attempt_at": None,
        }
        if error_code is not None:
            values["last_error_code"] = error_code
            values["last_error_at"] = error_at
        try:
            with self._sessions.begin() as session:
                result = cast(CursorResult[Any], session.execute(
                    update(MemoryAdmissionRecordRow)
                    .where(
                        MemoryAdmissionRecordRow.tenant_id == lease.tenant_id,
                        MemoryAdmissionRecordRow.example_id == lease.example_id,
                        MemoryAdmissionRecordRow.worker_id == lease.worker_id,
                        MemoryAdmissionRecordRow.lease_token == lease.lease_token,
                    )
                    .values(**values)
                ))
                return result.rowcount == 1
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError(
                "Unable to complete memory admission work"
            ) from exc

    def _retry_claim_sync(
        self,
        lease: MemoryAdmissionWorkLease,
        next_attempt_at: datetime,
        error_code: str,
        error_at: datetime,
    ) -> bool:
        self._require_aware("next_attempt_at", next_attempt_at)
        self._require_aware("error_at", error_at)
        self._require_text("error_code", error_code)
        if len(error_code) > 128:
            raise ValueError("error_code exceeds the persisted limit")
        if next_attempt_at <= error_at:
            raise ValueError("next_attempt_at must be later than error_at")
        try:
            with self._sessions.begin() as session:
                result = cast(CursorResult[Any], session.execute(
                    update(MemoryAdmissionRecordRow)
                    .where(
                        MemoryAdmissionRecordRow.tenant_id == lease.tenant_id,
                        MemoryAdmissionRecordRow.example_id == lease.example_id,
                        MemoryAdmissionRecordRow.worker_id == lease.worker_id,
                        MemoryAdmissionRecordRow.lease_token == lease.lease_token,
                    )
                    .values(
                        worker_id=None,
                        lease_token=None,
                        lease_expires_at=None,
                        next_attempt_at=next_attempt_at,
                        last_error_code=error_code,
                        last_error_at=error_at,
                    )
                ))
                return result.rowcount == 1
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError(
                "Unable to retry memory admission work"
            ) from exc

    def _filter_approved_example_ids_sync(
        self,
        tenant_id: str,
        example_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        self._require_text("tenant_id", tenant_id)
        if not example_ids:
            return ()
        try:
            with self._sessions() as session:
                approved = set(
                    session.scalars(
                        select(MemoryAdmissionRecordRow.example_id)
                        .join(
                            ReviewedExampleRow,
                            ReviewedExampleRow.example_id
                            == MemoryAdmissionRecordRow.example_id,
                        )
                        .where(
                            MemoryAdmissionRecordRow.tenant_id == tenant_id,
                            MemoryAdmissionRecordRow.example_id.in_(example_ids),
                            MemoryAdmissionRecordRow.status
                            == MemoryAdmissionStatus.APPROVED.value,
                            ReviewedExampleRow.tenant_id == tenant_id,
                            ReviewedExampleRow.is_reviewed.is_(True),
                            ReviewedExampleRow.is_valid.is_(True),
                        )
                    ).all()
                )
                return tuple(item for item in example_ids if item in approved)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to filter approved memories") from exc

    def _list_approved_example_ids_sync(
        self,
        tenant_id: str,
        schema_version: str,
        limit: int,
        after_example_id: str | None,
    ) -> tuple[str, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        try:
            with self._sessions() as session:
                statement = (
                    select(MemoryAdmissionRecordRow.example_id)
                    .join(
                        ReviewedExampleRow,
                        ReviewedExampleRow.example_id == MemoryAdmissionRecordRow.example_id,
                    )
                    .where(
                        MemoryAdmissionRecordRow.tenant_id == tenant_id,
                        MemoryAdmissionRecordRow.schema_version == schema_version,
                        MemoryAdmissionRecordRow.status == MemoryAdmissionStatus.APPROVED.value,
                        ReviewedExampleRow.tenant_id == tenant_id,
                        ReviewedExampleRow.is_reviewed.is_(True),
                        ReviewedExampleRow.is_valid.is_(True),
                    )
                )
                if after_example_id is not None:
                    self._require_text("after_example_id", after_example_id)
                    statement = statement.where(
                        MemoryAdmissionRecordRow.example_id > after_example_id
                    )
                return tuple(
                    session.scalars(
                        statement.order_by(MemoryAdmissionRecordRow.example_id).limit(limit)
                    ).all()
                )
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to list approved memories") from exc

    def _list_by_status_sync(
        self,
        tenant_id: str,
        statuses: tuple[MemoryAdmissionStatus, ...],
        limit: int,
        after_example_id: str | None,
        run_id: str | None,
        field_path: str | None,
    ) -> tuple[MemoryAdmissionRecord, ...]:
        if not statuses:
            raise ValueError("At least one admission status is required")
        return self._list_records_sync(
            tenant_id,
            limit,
            after_example_id,
            statuses=tuple(status.value for status in statuses),
            run_id=run_id,
            field_path=field_path,
        )

    def _list_for_schema_sync(
        self,
        tenant_id: str,
        schema_version: str,
        limit: int,
        after_example_id: str | None,
    ) -> tuple[MemoryAdmissionRecord, ...]:
        self._require_text("schema_version", schema_version)
        return self._list_records_sync(
            tenant_id,
            limit,
            after_example_id,
            schema_version=schema_version,
        )

    def _list_records_sync(
        self,
        tenant_id: str,
        limit: int,
        after_example_id: str | None,
        *,
        statuses: tuple[str, ...] | None = None,
        schema_version: str | None = None,
        run_id: str | None = None,
        field_path: str | None = None,
    ) -> tuple[MemoryAdmissionRecord, ...]:
        self._require_text("tenant_id", tenant_id)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        try:
            with self._sessions() as session:
                statement = select(MemoryAdmissionRecordRow).where(
                    MemoryAdmissionRecordRow.tenant_id == tenant_id
                )
                if statuses is not None:
                    statement = statement.where(MemoryAdmissionRecordRow.status.in_(statuses))
                if schema_version is not None:
                    statement = statement.where(
                        MemoryAdmissionRecordRow.schema_version == schema_version
                    )
                if run_id is not None:
                    self._require_text("run_id", run_id)
                if field_path is not None:
                    self._require_text("field_path", field_path)
                if run_id is not None or field_path is not None:
                    statement = statement.join(
                        ReviewedExampleRow,
                        ReviewedExampleRow.example_id == MemoryAdmissionRecordRow.example_id,
                    ).where(
                        ReviewedExampleRow.tenant_id == tenant_id,
                    )
                    if run_id is not None:
                        statement = statement.where(
                            exists().where(
                                ExampleFeedbackRow.tenant_id == tenant_id,
                                ExampleFeedbackRow.semantic_fingerprint
                                == ReviewedExampleRow.semantic_fingerprint,
                                ExampleFeedbackRow.run_id == run_id,
                            )
                        )
                    if field_path is not None:
                        statement = statement.where(ReviewedExampleRow.field_path == field_path)
                if after_example_id is not None:
                    self._require_text("after_example_id", after_example_id)
                    cursor_row = session.scalar(
                        select(MemoryAdmissionRecordRow).where(
                            MemoryAdmissionRecordRow.tenant_id == tenant_id,
                            MemoryAdmissionRecordRow.example_id == after_example_id,
                        )
                    )
                    if cursor_row is None:
                        return ()
                    statement = statement.where(
                        tuple_(MemoryAdmissionRecordRow.updated_at, MemoryAdmissionRecordRow.example_id)
                        < (cursor_row.updated_at, after_example_id)
                    )
                rows = session.scalars(
                    statement.order_by(
                        MemoryAdmissionRecordRow.updated_at.desc(),
                        MemoryAdmissionRecordRow.example_id.desc(),
                    ).limit(limit)
                ).all()
                return tuple(self._record_from_row(row) for row in rows)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to list memory admissions") from exc

    def _delete_tenant_sync(self, tenant_id: str) -> int:
        self._require_text("tenant_id", tenant_id)
        try:
            with self._sessions.begin() as session:
                example_ids = tuple(
                    session.scalars(
                        select(MemoryAdmissionRecordRow.example_id).where(
                            MemoryAdmissionRecordRow.tenant_id == tenant_id
                        )
                    ).all()
                )
                if example_ids:
                    assessment_ids = select(MemoryQualityAssessmentRow.assessment_id).where(
                        MemoryQualityAssessmentRow.tenant_id == tenant_id,
                        MemoryQualityAssessmentRow.example_id.in_(example_ids),
                    )
                    session.execute(
                        delete(MemoryQualitySignalRow).where(
                            MemoryQualitySignalRow.assessment_id.in_(assessment_ids)
                        )
                    )
                session.execute(
                    delete(MemoryQualityAssessmentRow).where(
                        MemoryQualityAssessmentRow.tenant_id == tenant_id
                    )
                )
                session.execute(
                    delete(MemoryAdmissionRecordRow).where(
                        MemoryAdmissionRecordRow.tenant_id == tenant_id
                    )
                )
                session.execute(
                    delete(MemoryAdmissionDecisionRow).where(
                        MemoryAdmissionDecisionRow.tenant_id == tenant_id
                    )
                )
                return len(example_ids)
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to delete tenant admission data") from exc

    def _purge_terminal_before_sync(
        self,
        tenant_id: str,
        older_than: datetime,
    ) -> int:
        self._require_text("tenant_id", tenant_id)
        self._require_aware("older_than", older_than)
        try:
            with self._sessions.begin() as session:
                example_ids = tuple(
                    session.scalars(
                        select(MemoryAdmissionRecordRow.example_id).where(
                            MemoryAdmissionRecordRow.tenant_id == tenant_id,
                            MemoryAdmissionRecordRow.status.in_(
                                (
                                    MemoryAdmissionStatus.REJECTED.value,
                                    MemoryAdmissionStatus.INVALIDATED.value,
                                )
                            ),
                            MemoryAdmissionRecordRow.updated_at < older_than,
                        )
                    ).all()
                )
                if not example_ids:
                    return 0
                assessment_ids = select(MemoryQualityAssessmentRow.assessment_id).where(
                    MemoryQualityAssessmentRow.tenant_id == tenant_id,
                    MemoryQualityAssessmentRow.example_id.in_(example_ids),
                )
                session.execute(
                    delete(MemoryQualitySignalRow).where(
                        MemoryQualitySignalRow.assessment_id.in_(assessment_ids)
                    )
                )
                session.execute(
                    delete(MemoryQualityAssessmentRow).where(
                        MemoryQualityAssessmentRow.tenant_id == tenant_id,
                        MemoryQualityAssessmentRow.example_id.in_(example_ids),
                    )
                )
                session.execute(
                    delete(MemoryAdmissionRecordRow).where(
                        MemoryAdmissionRecordRow.tenant_id == tenant_id,
                        MemoryAdmissionRecordRow.example_id.in_(example_ids),
                    )
                )
                session.execute(
                    delete(MemoryAdmissionDecisionRow).where(
                        MemoryAdmissionDecisionRow.tenant_id == tenant_id,
                        MemoryAdmissionDecisionRow.example_id.in_(example_ids),
                    )
                )
                return len(example_ids)
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to purge expired admission data") from exc

    def _insert_decision(
        self,
        session: Session,
        decision: MemoryAdmissionDecision,
    ) -> MemoryAdmissionDecisionRow:
        values = {
            "decision_id": decision.decision_id,
            "tenant_id": decision.tenant_id,
            "example_id": decision.example_id,
            "previous_status": (
                decision.previous_status.value
                if decision.previous_status is not None
                else None
            ),
            "status": decision.status.value,
            "authority": decision.authority.value,
            "decided_by": decision.decided_by,
            "reason": decision.reason,
            "reason_codes_json": list(decision.reason_codes),
            "assessment_ids_json": list(decision.assessment_ids),
            "conflict_ids_json": list(decision.conflict_ids),
            "policy_version": decision.policy_version,
            "idempotency_key_hash": decision.idempotency_key_hash,
            "revision": decision.revision,
            "decided_at": decision.decided_at,
        }
        self._insert_do_nothing(session, MemoryAdmissionDecisionRow, values)
        row = session.scalar(
            select(MemoryAdmissionDecisionRow).where(
                MemoryAdmissionDecisionRow.tenant_id == decision.tenant_id,
                MemoryAdmissionDecisionRow.example_id == decision.example_id,
                MemoryAdmissionDecisionRow.revision == decision.revision,
            )
        )
        if row is None or self._decision_from_row(row) != decision:
            raise WorkflowPersistenceError("Admission decision conflicts with persisted audit")
        return row

    @staticmethod
    def _validate_decision_references(
        session: Session,
        decision: MemoryAdmissionDecision,
    ) -> None:
        if decision.assessment_ids:
            assessment_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(MemoryQualityAssessmentRow)
                    .where(
                        MemoryQualityAssessmentRow.tenant_id == decision.tenant_id,
                        MemoryQualityAssessmentRow.example_id == decision.example_id,
                        MemoryQualityAssessmentRow.assessment_id.in_(decision.assessment_ids),
                    )
                )
                or 0
            )
            if assessment_count != len(decision.assessment_ids):
                raise WorkflowPersistenceError("Decision references invalid assessments")
        if decision.conflict_ids:
            conflict_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(MemoryConflictExampleRow)
                    .join(
                        MemoryConflictRow,
                        MemoryConflictRow.conflict_id == MemoryConflictExampleRow.conflict_id,
                    )
                    .where(
                        MemoryConflictExampleRow.tenant_id == decision.tenant_id,
                        MemoryConflictExampleRow.example_id == decision.example_id,
                        MemoryConflictExampleRow.conflict_id.in_(decision.conflict_ids),
                        MemoryConflictRow.tenant_id == decision.tenant_id,
                    )
                )
                or 0
            )
            if conflict_count != len(decision.conflict_ids):
                raise WorkflowPersistenceError("Decision references invalid conflicts")

    def _assessment_from_row(
        self,
        session: Session,
        row: MemoryQualityAssessmentRow,
    ) -> MemoryQualityAssessment:
        signal_rows = session.scalars(
            select(MemoryQualitySignalRow)
            .where(MemoryQualitySignalRow.assessment_id == row.assessment_id)
            .order_by(MemoryQualitySignalRow.ordinal)
        ).all()
        signals = tuple(
            MemoryQualitySignal(
                code=signal.code,
                source=MemoryAssessmentSource(signal.source),
                verdict=SignalVerdict(signal.verdict),
                score=signal.score,
                message=signal.message,
                field_path=signal.field_path,
                evidence_references=self._string_tuple(
                    signal.evidence_references_json,
                    "signal evidence references",
                ),
            )
            for signal in signal_rows
        )
        model_row = (
            session.get(ModelVersionRow, row.model_version_id)
            if row.model_version_id is not None
            else None
        )
        prompt_row = (
            session.get(PromptVersionRow, row.prompt_version_id)
            if row.prompt_version_id is not None
            else None
        )
        if (row.model_version_id is not None and model_row is None) or (
            row.prompt_version_id is not None and prompt_row is None
        ):
            raise WorkflowPersistenceError("Assessment references a missing version")
        return MemoryQualityAssessment(
            assessment_id=row.assessment_id,
            tenant_id=row.tenant_id,
            example_id=row.example_id,
            source=MemoryAssessmentSource(row.source),
            signals=signals,
            quality_score=row.quality_score,
            recommendation=MemoryAdmissionRecommendation(row.recommendation),
            reason_codes=self._string_tuple(row.reason_codes_json, "assessment reason codes"),
            policy_version=row.policy_version,
            input_fingerprint=row.input_fingerprint,
            created_at=self._aware(row.assessed_at),
            model_version=ModelVersion(model_row.version) if model_row is not None else None,
            prompt_version=PromptVersion(prompt_row.version) if prompt_row is not None else None,
            advisory_only=True,
        )

    def _decision_from_row(
        self,
        row: MemoryAdmissionDecisionRow,
    ) -> MemoryAdmissionDecision:
        return MemoryAdmissionDecision(
            decision_id=row.decision_id,
            tenant_id=row.tenant_id,
            example_id=row.example_id,
            previous_status=(
                MemoryAdmissionStatus(row.previous_status)
                if row.previous_status is not None
                else None
            ),
            status=MemoryAdmissionStatus(row.status),
            authority=MemoryAdmissionDecisionAuthority(row.authority),
            decided_by=row.decided_by,
            reason=row.reason,
            reason_codes=self._string_tuple(row.reason_codes_json, "decision reason codes"),
            assessment_ids=self._string_tuple(
                row.assessment_ids_json,
                "decision assessment ids",
            ),
            conflict_ids=self._string_tuple(row.conflict_ids_json, "decision conflict ids"),
            policy_version=row.policy_version,
            idempotency_key_hash=row.idempotency_key_hash,
            revision=row.revision,
            decided_at=self._aware(row.decided_at),
        )

    @staticmethod
    def _record_from_row(row: MemoryAdmissionRecordRow) -> MemoryAdmissionRecord:
        return MemoryAdmissionRecord(
            tenant_id=row.tenant_id,
            example_id=row.example_id,
            status=MemoryAdmissionStatus(row.status),
            current_decision_id=row.current_decision_id,
            policy_version=row.policy_version,
            revision=row.revision,
            created_at=(
                row.created_at
                if row.created_at.tzinfo is not None
                else row.created_at.replace(tzinfo=UTC)
            ),
            updated_at=(
                row.updated_at
                if row.updated_at.tzinfo is not None
                else row.updated_at.replace(tzinfo=UTC)
            ),
        )

    @staticmethod
    def _signal_id(assessment_id: str, ordinal: int) -> str:
        return sha256(f"signal\0{assessment_id}\0{ordinal}".encode("utf-8")).hexdigest()


class SQLAlchemyMemoryConflictRepository(_SQLAlchemyAdmissionStore):
    """Persist tenant-scoped example or alias-candidate conflicts."""

    async def save(self, conflict: MemoryConflictRecord) -> MemoryConflictRecord:
        return await asyncio.to_thread(self._save_sync, conflict)

    async def get(
        self,
        tenant_id: str,
        conflict_id: str,
    ) -> MemoryConflictRecord | None:
        return await asyncio.to_thread(self._get_sync, tenant_id, conflict_id)

    async def list_open_for_example(
        self,
        tenant_id: str,
        example_id: str,
    ) -> tuple[MemoryConflictRecord, ...]:
        return await asyncio.to_thread(
            self._list_open_for_example_sync,
            tenant_id,
            example_id,
        )

    async def list_open_for_alias_candidate(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> tuple[MemoryConflictRecord, ...]:
        return await asyncio.to_thread(
            self._list_open_for_alias_candidate_sync,
            tenant_id,
            candidate_id,
        )

    async def list_for_governance(
        self,
        tenant_id: str,
        statuses: Sequence[MemoryConflictStatus],
        *,
        alias_conflicts_only: bool,
        limit: int,
        after_conflict_id: str | None = None,
        field_path: str | None = None,
    ) -> tuple[MemoryConflictRecord, ...]:
        return await asyncio.to_thread(
            self._list_for_governance_sync,
            tenant_id,
            tuple(statuses),
            alias_conflicts_only,
            limit,
            after_conflict_id,
            field_path,
        )

    async def resolve(
        self,
        tenant_id: str,
        conflict_id: str,
        status: MemoryConflictStatus,
        resolution_decision_id: str,
        resolved_at: datetime,
    ) -> MemoryConflictRecord:
        return await asyncio.to_thread(
            self._resolve_sync,
            tenant_id,
            conflict_id,
            status,
            resolution_decision_id,
            resolved_at,
        )

    async def get_resolution_by_idempotency_hash(
        self,
        tenant_id: str,
        idempotency_key_hash: str,
    ) -> MemoryConflictResolutionDecision | None:
        return await asyncio.to_thread(
            self._get_resolution_by_idempotency_hash_sync,
            tenant_id,
            idempotency_key_hash,
        )

    async def resolve_with_decision(
        self,
        decision: MemoryConflictResolutionDecision,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> MemoryConflictRecord:
        return await asyncio.to_thread(
            self._resolve_with_decision_sync,
            decision,
            audit_event,
        )

    async def delete_tenant(self, tenant_id: str) -> int:
        return await asyncio.to_thread(self._delete_tenant_sync, tenant_id)

    async def purge_resolved_before(
        self,
        tenant_id: str,
        older_than: datetime,
    ) -> int:
        return await asyncio.to_thread(
            self._purge_resolved_before_sync,
            tenant_id,
            older_than,
        )

    def _save_sync(self, conflict: MemoryConflictRecord) -> MemoryConflictRecord:
        try:
            with self._sessions.begin() as session:
                if conflict.example_ids:
                    examples = session.scalars(
                        select(ReviewedExampleRow).where(
                            ReviewedExampleRow.tenant_id == conflict.tenant_id,
                            ReviewedExampleRow.example_id.in_(conflict.example_ids),
                        )
                    ).all()
                    if len(examples) != len(conflict.example_ids) or any(
                        row.document_type != conflict.document_type
                        or row.field_path != conflict.field_path
                        or row.schema_version != conflict.schema_version
                        for row in examples
                    ):
                        raise WorkflowPersistenceError(
                            "Conflict crosses its reviewed-example scope"
                        )
                else:
                    candidates = session.scalars(
                        select(FieldAliasCandidateRow).where(
                            FieldAliasCandidateRow.scope == "tenant",
                            FieldAliasCandidateRow.tenant_id == conflict.tenant_id,
                            FieldAliasCandidateRow.candidate_id.in_(
                                conflict.field_alias_candidate_ids
                            ),
                        )
                    ).all()
                    if len(candidates) != len(conflict.field_alias_candidate_ids) or any(
                        row.document_type != conflict.document_type
                        or row.schema_version != conflict.schema_version
                        or row.normalized_alias != conflict.field_path
                        or row.canonical_field_path not in conflict.candidate_field_paths
                        for row in candidates
                    ):
                        raise WorkflowPersistenceError(
                            "Conflict crosses its field-alias candidate scope"
                        )
                values = {
                    "conflict_id": conflict.conflict_id,
                    "tenant_id": conflict.tenant_id,
                    "document_type": conflict.document_type,
                    "field_path": conflict.field_path,
                    "schema_version": conflict.schema_version,
                    "conflict_type": conflict.conflict_type,
                    "fingerprint": conflict.fingerprint,
                    "reason_codes_json": list(conflict.reason_codes),
                    "evidence_references_json": list(conflict.evidence_references),
                    "candidate_field_paths_json": list(
                        conflict.candidate_field_paths
                    ),
                    "status": conflict.status.value,
                    "detected_at": conflict.detected_at,
                    "resolved_at": conflict.resolved_at,
                    "resolution_decision_id": conflict.resolution_decision_id,
                }
                created = self._insert_do_nothing(session, MemoryConflictRow, values)
                row = session.scalar(
                    select(MemoryConflictRow).where(
                        MemoryConflictRow.tenant_id == conflict.tenant_id,
                        MemoryConflictRow.fingerprint == conflict.fingerprint,
                    )
                )
                if row is None:
                    raise WorkflowPersistenceError("Conflict replay did not produce a row")
                if created:
                    for ordinal, example_id in enumerate(conflict.example_ids):
                        session.add(
                            MemoryConflictExampleRow(
                                conflict_id=row.conflict_id,
                                example_id=example_id,
                                tenant_id=conflict.tenant_id,
                                ordinal=ordinal,
                            )
                        )
                    for ordinal, candidate_id in enumerate(
                        conflict.field_alias_candidate_ids
                    ):
                        session.add(
                            MemoryConflictFieldAliasCandidateRow(
                                conflict_id=row.conflict_id,
                                candidate_id=candidate_id,
                                tenant_id=conflict.tenant_id,
                                ordinal=ordinal,
                            )
                        )
                    session.flush()
                persisted = self._conflict_from_row(session, row)
                if row.conflict_id == conflict.conflict_id and persisted != conflict:
                    raise WorkflowPersistenceError(
                        "Conflict identifier is bound to different immutable data"
                    )
                return persisted
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to persist memory conflict") from exc

    def _get_sync(
        self,
        tenant_id: str,
        conflict_id: str,
    ) -> MemoryConflictRecord | None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("conflict_id", conflict_id)
        try:
            with self._sessions() as session:
                row = session.get(MemoryConflictRow, conflict_id)
                if row is None or row.tenant_id != tenant_id:
                    return None
                return self._conflict_from_row(session, row)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read memory conflict") from exc

    def _list_open_for_example_sync(
        self,
        tenant_id: str,
        example_id: str,
    ) -> tuple[MemoryConflictRecord, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("example_id", example_id)
        try:
            with self._sessions() as session:
                rows = session.scalars(
                    select(MemoryConflictRow)
                    .join(
                        MemoryConflictExampleRow,
                        MemoryConflictExampleRow.conflict_id == MemoryConflictRow.conflict_id,
                    )
                    .where(
                        MemoryConflictRow.tenant_id == tenant_id,
                        MemoryConflictRow.status == MemoryConflictStatus.OPEN.value,
                        MemoryConflictExampleRow.tenant_id == tenant_id,
                        MemoryConflictExampleRow.example_id == example_id,
                    )
                    .order_by(MemoryConflictRow.detected_at, MemoryConflictRow.conflict_id)
                ).all()
                return tuple(self._conflict_from_row(session, row) for row in rows)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to list open memory conflicts") from exc

    def _list_open_for_alias_candidate_sync(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> tuple[MemoryConflictRecord, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("candidate_id", candidate_id)
        try:
            with self._sessions() as session:
                rows = session.scalars(
                    select(MemoryConflictRow)
                    .join(
                        MemoryConflictFieldAliasCandidateRow,
                        MemoryConflictFieldAliasCandidateRow.conflict_id
                        == MemoryConflictRow.conflict_id,
                    )
                    .where(
                        MemoryConflictRow.tenant_id == tenant_id,
                        MemoryConflictRow.status == MemoryConflictStatus.OPEN.value,
                        MemoryConflictFieldAliasCandidateRow.tenant_id == tenant_id,
                        MemoryConflictFieldAliasCandidateRow.candidate_id
                        == candidate_id,
                    )
                    .order_by(MemoryConflictRow.detected_at, MemoryConflictRow.conflict_id)
                ).all()
                return tuple(self._conflict_from_row(session, row) for row in rows)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to list open field alias conflicts"
            ) from exc

    def _list_for_governance_sync(
        self,
        tenant_id: str,
        statuses: tuple[MemoryConflictStatus, ...],
        alias_conflicts_only: bool,
        limit: int,
        after_conflict_id: str | None,
        field_path: str | None,
    ) -> tuple[MemoryConflictRecord, ...]:
        self._require_text("tenant_id", tenant_id)
        if not statuses:
            raise ValueError("At least one conflict status is required")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if after_conflict_id is not None:
            self._require_text("after_conflict_id", after_conflict_id)
        if field_path is not None:
            self._require_text("field_path", field_path)
        try:
            with self._sessions() as session:
                statement = select(MemoryConflictRow).where(
                    MemoryConflictRow.tenant_id == tenant_id,
                    MemoryConflictRow.status.in_(
                        tuple(status.value for status in statuses)
                    ),
                )
                if alias_conflicts_only:
                    statement = statement.where(
                        exists().where(
                            MemoryConflictFieldAliasCandidateRow.conflict_id
                            == MemoryConflictRow.conflict_id,
                            MemoryConflictFieldAliasCandidateRow.tenant_id == tenant_id,
                        )
                    )
                if field_path is not None:
                    statement = statement.where(MemoryConflictRow.field_path == field_path)
                if after_conflict_id is not None:
                    cursor_row = session.scalar(
                        select(MemoryConflictRow).where(
                            MemoryConflictRow.tenant_id == tenant_id,
                            MemoryConflictRow.conflict_id == after_conflict_id,
                        )
                    )
                    if cursor_row is None:
                        return ()
                    cursor_time = cursor_row.resolved_at or cursor_row.detected_at
                    statement = statement.where(
                        tuple_(
                            func.coalesce(MemoryConflictRow.resolved_at, MemoryConflictRow.detected_at),
                            MemoryConflictRow.conflict_id,
                        ) < (cursor_time, after_conflict_id)
                    )
                rows = session.scalars(
                    statement.order_by(
                        func.coalesce(MemoryConflictRow.resolved_at, MemoryConflictRow.detected_at).desc(),
                        MemoryConflictRow.conflict_id.desc(),
                    ).limit(limit)
                ).all()
                return tuple(self._conflict_from_row(session, row) for row in rows)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to list memory conflicts for governance"
            ) from exc

    def _resolve_sync(
        self,
        tenant_id: str,
        conflict_id: str,
        status: MemoryConflictStatus,
        resolution_decision_id: str,
        resolved_at: datetime,
    ) -> MemoryConflictRecord:
        self._require_text("tenant_id", tenant_id)
        self._require_text("conflict_id", conflict_id)
        self._require_text("resolution_decision_id", resolution_decision_id)
        self._require_aware("resolved_at", resolved_at)
        if status is MemoryConflictStatus.OPEN:
            raise ValueError("Conflict resolution must close the conflict")
        try:
            with self._sessions.begin() as session:
                statement = select(MemoryConflictRow).where(
                    MemoryConflictRow.tenant_id == tenant_id,
                    MemoryConflictRow.conflict_id == conflict_id,
                )
                if self._dialect_name == "postgresql":
                    statement = statement.with_for_update()
                row = session.scalar(statement)
                if row is None:
                    raise WorkflowPersistenceError("Memory conflict does not exist")
                linked_example_ids = tuple(
                    session.scalars(
                        select(MemoryConflictExampleRow.example_id).where(
                            MemoryConflictExampleRow.tenant_id == tenant_id,
                            MemoryConflictExampleRow.conflict_id == conflict_id,
                        )
                    ).all()
                )
                linked_candidate_ids = tuple(
                    session.scalars(
                        select(
                            MemoryConflictFieldAliasCandidateRow.candidate_id
                        ).where(
                            MemoryConflictFieldAliasCandidateRow.tenant_id == tenant_id,
                            MemoryConflictFieldAliasCandidateRow.conflict_id
                            == conflict_id,
                        )
                    ).all()
                )
                if linked_example_ids:
                    decision = session.get(
                        MemoryAdmissionDecisionRow,
                        resolution_decision_id,
                    )
                    valid_resolution = (
                        decision is not None
                        and decision.tenant_id == tenant_id
                        and decision.example_id in linked_example_ids
                    )
                else:
                    alias_decision = session.get(
                        FieldAliasCandidateDecisionRow,
                        resolution_decision_id,
                    )
                    candidate = (
                        session.get(FieldAliasCandidateRow, alias_decision.candidate_id)
                        if alias_decision is not None
                        else None
                    )
                    valid_resolution = (
                        alias_decision is not None
                        and alias_decision.candidate_id in linked_candidate_ids
                        and candidate is not None
                        and candidate.tenant_id == tenant_id
                    )
                if not valid_resolution:
                    raise WorkflowPersistenceError(
                        "Conflict resolution references an unrelated governance decision"
                    )
                if row.status != MemoryConflictStatus.OPEN.value:
                    if (
                        row.status != status.value
                        or row.resolution_decision_id != resolution_decision_id
                    ):
                        raise WorkflowPersistenceError("Conflict is already resolved differently")
                    return self._conflict_from_row(session, row)
                row.status = status.value
                row.resolution_decision_id = resolution_decision_id
                row.resolved_at = resolved_at
                return self._conflict_from_row(session, row)
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to resolve memory conflict") from exc

    def _get_resolution_by_idempotency_hash_sync(
        self,
        tenant_id: str,
        idempotency_key_hash: str,
    ) -> MemoryConflictResolutionDecision | None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("idempotency_key_hash", idempotency_key_hash)
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(MemoryConflictResolutionDecisionRow).where(
                        MemoryConflictResolutionDecisionRow.tenant_id == tenant_id,
                        MemoryConflictResolutionDecisionRow.idempotency_key_hash
                        == idempotency_key_hash,
                    )
                )
                return (
                    self._resolution_decision_from_row(session, row)
                    if row is not None
                    else None
                )
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to read conflict resolution decision"
            ) from exc

    def _resolve_with_decision_sync(
        self,
        decision: MemoryConflictResolutionDecision,
        audit_event: GovernanceAuditEvent | None,
    ) -> MemoryConflictRecord:
        try:
            with self._sessions.begin() as session:
                replay_row = session.scalar(
                    select(MemoryConflictResolutionDecisionRow).where(
                        MemoryConflictResolutionDecisionRow.tenant_id
                        == decision.tenant_id,
                        MemoryConflictResolutionDecisionRow.idempotency_key_hash
                        == decision.idempotency_key_hash,
                    )
                )
                if replay_row is not None:
                    replay = self._resolution_decision_from_row(session, replay_row)
                    if replay != decision:
                        raise WorkflowPersistenceError(
                            "Conflict resolution idempotency key is bound to another request"
                        )
                    row = session.scalar(
                        select(MemoryConflictRow).where(
                            MemoryConflictRow.tenant_id == decision.tenant_id,
                            MemoryConflictRow.conflict_id == decision.conflict_id,
                        )
                    )
                    if (
                        row is None
                        or row.resolution_decision_id != decision.resolution_decision_id
                    ):
                        raise WorkflowPersistenceError(
                            "Conflict resolution replay is inconsistent"
                        )
                    if audit_event is not None:
                        require_governance_audit(session, audit_event)
                    return self._conflict_from_row(session, row)

                statement = select(MemoryConflictRow).where(
                    MemoryConflictRow.tenant_id == decision.tenant_id,
                    MemoryConflictRow.conflict_id == decision.conflict_id,
                )
                if self._dialect_name == "postgresql":
                    statement = statement.with_for_update()
                row = session.scalar(statement)
                if row is None:
                    raise WorkflowPersistenceError("Memory conflict does not exist")
                if row.status != MemoryConflictStatus.OPEN.value:
                    raise WorkflowPersistenceError("Memory conflict status is stale")

                linked_example_ids = tuple(
                    session.scalars(
                        select(MemoryConflictExampleRow.example_id).where(
                            MemoryConflictExampleRow.tenant_id == decision.tenant_id,
                            MemoryConflictExampleRow.conflict_id == decision.conflict_id,
                        )
                    ).all()
                )
                linked_candidate_ids = tuple(
                    session.scalars(
                        select(MemoryConflictFieldAliasCandidateRow.candidate_id).where(
                            MemoryConflictFieldAliasCandidateRow.tenant_id
                            == decision.tenant_id,
                            MemoryConflictFieldAliasCandidateRow.conflict_id
                            == decision.conflict_id,
                        )
                    ).all()
                )
                expected_targets = {
                    (
                        MemoryConflictReevaluationTargetType.MEMORY_ADMISSION,
                        example_id,
                    )
                    for example_id in linked_example_ids
                }
                expected_targets.update(
                    (
                        MemoryConflictReevaluationTargetType.FIELD_ALIAS,
                        candidate_id,
                    )
                    for candidate_id in linked_candidate_ids
                )
                actual_targets = {
                    (target.target_type, target.target_id)
                    for target in decision.reevaluation_targets
                }
                if not expected_targets or actual_targets != expected_targets:
                    raise WorkflowPersistenceError(
                        "Conflict reevaluation targets do not match its source facts"
                    )
                candidate_paths = self._string_tuple(
                    row.candidate_field_paths_json,
                    "conflict candidate field paths",
                )
                if decision.selected_canonical_field_path is not None and (
                    decision.selected_canonical_field_path not in candidate_paths
                ):
                    raise WorkflowPersistenceError(
                        "Selected canonical field path is not a conflict candidate"
                    )
                if (
                    linked_candidate_ids
                    and decision.target_status is MemoryConflictStatus.RESOLVED
                    and decision.selected_canonical_field_path is None
                ):
                    raise WorkflowPersistenceError(
                        "Resolved field alias conflicts require a selected field"
                    )

                session.add(
                    MemoryConflictResolutionDecisionRow(
                        resolution_decision_id=decision.resolution_decision_id,
                        tenant_id=decision.tenant_id,
                        conflict_id=decision.conflict_id,
                        previous_status=decision.previous_status.value,
                        target_status=decision.target_status.value,
                        selected_canonical_field_path=(
                            decision.selected_canonical_field_path
                        ),
                        reviewer_id=decision.reviewer_id,
                        reason=decision.reason,
                        resolution_note=decision.resolution_note,
                        idempotency_key_hash=decision.idempotency_key_hash,
                        policy_version=decision.policy_version,
                        decided_at=decision.decided_at,
                    )
                )
                if audit_event is not None:
                    expected_action = (
                        GovernanceAction.RESOLVE_CONFLICT
                        if decision.target_status is MemoryConflictStatus.RESOLVED
                        else GovernanceAction.DISMISS_CONFLICT
                    )
                    if (
                        audit_event.action is not expected_action
                        or audit_event.tenant_id != decision.tenant_id
                        or audit_event.resource_type != "memory_conflict"
                        or audit_event.resource_id != decision.conflict_id
                        or audit_event.resource_version
                        != decision.resolution_decision_id
                        or audit_event.reviewer_id != decision.reviewer_id
                    ):
                        raise WorkflowPersistenceError(
                            "Conflict governance audit scope is inconsistent"
                        )
                    add_governance_audit(session, audit_event)
                for target in decision.reevaluation_targets:
                    request_id = sha256(
                        (
                            "conflict-reevaluation\0"
                            f"{decision.resolution_decision_id}\0"
                            f"{target.target_type.value}\0{target.target_id}"
                        ).encode("utf-8")
                    ).hexdigest()
                    session.add(
                        MemoryConflictReevaluationRequestRow(
                            reevaluation_request_id=request_id,
                            tenant_id=decision.tenant_id,
                            resolution_decision_id=decision.resolution_decision_id,
                            target_type=target.target_type.value,
                            target_id=target.target_id,
                            status="pending",
                            created_at=decision.decided_at,
                        )
                    )
                row.status = decision.target_status.value
                row.resolution_decision_id = decision.resolution_decision_id
                row.resolved_at = decision.decided_at
                session.flush()
                return self._conflict_from_row(session, row)
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to persist conflict resolution"
            ) from exc

    def _delete_tenant_sync(self, tenant_id: str) -> int:
        self._require_text("tenant_id", tenant_id)
        try:
            with self._sessions.begin() as session:
                count = int(
                    session.scalar(
                        select(func.count())
                        .select_from(MemoryConflictRow)
                        .where(MemoryConflictRow.tenant_id == tenant_id)
                    )
                    or 0
                )
                session.execute(
                    delete(MemoryConflictExampleRow).where(
                        MemoryConflictExampleRow.tenant_id == tenant_id
                    )
                )
                session.execute(
                    delete(MemoryConflictFieldAliasCandidateRow).where(
                        MemoryConflictFieldAliasCandidateRow.tenant_id == tenant_id
                    )
                )
                session.execute(
                    delete(MemoryConflictRow).where(MemoryConflictRow.tenant_id == tenant_id)
                )
                return count
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to delete tenant conflict data") from exc

    def _purge_resolved_before_sync(
        self,
        tenant_id: str,
        older_than: datetime,
    ) -> int:
        self._require_text("tenant_id", tenant_id)
        self._require_aware("older_than", older_than)
        try:
            with self._sessions.begin() as session:
                conflict_ids = tuple(
                    session.scalars(
                        select(MemoryConflictRow.conflict_id).where(
                            MemoryConflictRow.tenant_id == tenant_id,
                            MemoryConflictRow.status.in_(
                                (
                                    MemoryConflictStatus.RESOLVED.value,
                                    MemoryConflictStatus.DISMISSED.value,
                                )
                            ),
                            MemoryConflictRow.resolved_at < older_than,
                        )
                    ).all()
                )
                if not conflict_ids:
                    return 0
                session.execute(
                    delete(MemoryConflictExampleRow).where(
                        MemoryConflictExampleRow.tenant_id == tenant_id,
                        MemoryConflictExampleRow.conflict_id.in_(conflict_ids),
                    )
                )
                session.execute(
                    delete(MemoryConflictFieldAliasCandidateRow).where(
                        MemoryConflictFieldAliasCandidateRow.tenant_id == tenant_id,
                        MemoryConflictFieldAliasCandidateRow.conflict_id.in_(
                            conflict_ids
                        ),
                    )
                )
                session.execute(
                    delete(MemoryConflictRow).where(
                        MemoryConflictRow.tenant_id == tenant_id,
                        MemoryConflictRow.conflict_id.in_(conflict_ids),
                    )
                )
                return len(conflict_ids)
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to purge resolved conflicts") from exc

    def _conflict_from_row(
        self,
        session: Session,
        row: MemoryConflictRow,
    ) -> MemoryConflictRecord:
        example_ids = tuple(
            session.scalars(
                select(MemoryConflictExampleRow.example_id)
                .where(MemoryConflictExampleRow.conflict_id == row.conflict_id)
                .order_by(MemoryConflictExampleRow.ordinal)
            ).all()
        )
        candidate_ids = tuple(
            session.scalars(
                select(MemoryConflictFieldAliasCandidateRow.candidate_id)
                .where(
                    MemoryConflictFieldAliasCandidateRow.conflict_id
                    == row.conflict_id
                )
                .order_by(MemoryConflictFieldAliasCandidateRow.ordinal)
            ).all()
        )
        return MemoryConflictRecord(
            conflict_id=row.conflict_id,
            tenant_id=row.tenant_id,
            example_ids=example_ids,
            document_type=row.document_type,
            field_path=row.field_path,
            schema_version=row.schema_version,
            conflict_type=row.conflict_type,
            fingerprint=row.fingerprint,
            reason_codes=self._string_tuple(row.reason_codes_json, "conflict reason codes"),
            evidence_references=self._string_tuple(
                row.evidence_references_json,
                "conflict evidence references",
            ),
            status=MemoryConflictStatus(row.status),
            detected_at=self._aware(row.detected_at),
            resolved_at=self._aware(row.resolved_at) if row.resolved_at is not None else None,
            resolution_decision_id=row.resolution_decision_id,
            field_alias_candidate_ids=candidate_ids,
            candidate_field_paths=self._string_tuple(
                row.candidate_field_paths_json,
                "conflict candidate field paths",
            ),
        )

    def _resolution_decision_from_row(
        self,
        session: Session,
        row: MemoryConflictResolutionDecisionRow,
    ) -> MemoryConflictResolutionDecision:
        targets = tuple(
            MemoryConflictReevaluationTarget(
                target_type=MemoryConflictReevaluationTargetType(item.target_type),
                target_id=item.target_id,
            )
            for item in session.scalars(
                select(MemoryConflictReevaluationRequestRow)
                .where(
                    MemoryConflictReevaluationRequestRow.resolution_decision_id
                    == row.resolution_decision_id
                )
                .order_by(
                    MemoryConflictReevaluationRequestRow.target_type,
                    MemoryConflictReevaluationRequestRow.target_id,
                )
            ).all()
        )
        return MemoryConflictResolutionDecision(
            resolution_decision_id=row.resolution_decision_id,
            tenant_id=row.tenant_id,
            conflict_id=row.conflict_id,
            previous_status=MemoryConflictStatus(row.previous_status),
            target_status=MemoryConflictStatus(row.target_status),
            selected_canonical_field_path=row.selected_canonical_field_path,
            reviewer_id=row.reviewer_id,
            reason=row.reason,
            resolution_note=row.resolution_note,
            idempotency_key_hash=row.idempotency_key_hash,
            policy_version=row.policy_version,
            decided_at=self._aware(row.decided_at),
            reevaluation_targets=targets,
        )


class SQLAlchemyReviewerReliabilityRepository(_SQLAlchemyAdmissionStore):
    """Persist immutable tenant-scoped reviewer reliability snapshots."""

    async def get(
        self,
        tenant_id: str,
        reviewer_id: str,
        profile_version: str,
    ) -> ReviewerReliabilityProfile | None:
        return await asyncio.to_thread(
            self._get_sync,
            tenant_id,
            reviewer_id,
            profile_version,
        )

    async def save(
        self,
        profile: ReviewerReliabilityProfile,
    ) -> ReviewerReliabilityProfile:
        return await asyncio.to_thread(self._save_sync, profile)

    async def delete_tenant(self, tenant_id: str) -> int:
        return await asyncio.to_thread(self._delete_tenant_sync, tenant_id)

    async def purge_before(self, tenant_id: str, older_than: datetime) -> int:
        return await asyncio.to_thread(self._purge_before_sync, tenant_id, older_than)

    def _get_sync(
        self,
        tenant_id: str,
        reviewer_id: str,
        profile_version: str,
    ) -> ReviewerReliabilityProfile | None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("reviewer_id", reviewer_id)
        self._require_text("profile_version", profile_version)
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(ReviewerReliabilitySnapshotRow).where(
                        ReviewerReliabilitySnapshotRow.tenant_id == tenant_id,
                        ReviewerReliabilitySnapshotRow.reviewer_id == reviewer_id,
                        ReviewerReliabilitySnapshotRow.profile_version == profile_version,
                    )
                )
                return self._profile_from_row(row) if row is not None else None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read reviewer reliability") from exc

    def _save_sync(
        self,
        profile: ReviewerReliabilityProfile,
    ) -> ReviewerReliabilityProfile:
        snapshot_id = self._snapshot_id(profile)
        values = {
            "snapshot_id": snapshot_id,
            "tenant_id": profile.tenant_id,
            "reviewer_id": profile.reviewer_id,
            "profile_version": profile.profile_version,
            "policy_version": profile.policy_version,
            "reviewed_fact_count": profile.reviewed_fact_count,
            "approved_fact_count": profile.approved_fact_count,
            "quarantined_fact_count": profile.quarantined_fact_count,
            "rejected_fact_count": profile.rejected_fact_count,
            "conflict_count": profile.conflict_count,
            "reliability_score": profile.reliability_score,
            "minimum_sample_met": profile.minimum_sample_met,
            "calculated_at": profile.calculated_at,
        }
        try:
            with self._sessions.begin() as session:
                self._insert_do_nothing(session, ReviewerReliabilitySnapshotRow, values)
                row = session.scalar(
                    select(ReviewerReliabilitySnapshotRow).where(
                        ReviewerReliabilitySnapshotRow.tenant_id == profile.tenant_id,
                        ReviewerReliabilitySnapshotRow.reviewer_id == profile.reviewer_id,
                        ReviewerReliabilitySnapshotRow.profile_version
                        == profile.profile_version,
                    )
                )
                if row is None:
                    raise WorkflowPersistenceError("Reviewer snapshot was not persisted")
                persisted = self._profile_from_row(row)
                if persisted != profile:
                    raise WorkflowPersistenceError(
                        "Reviewer profile version is bound to different immutable data"
                    )
                return persisted
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to persist reviewer reliability") from exc

    def _delete_tenant_sync(self, tenant_id: str) -> int:
        self._require_text("tenant_id", tenant_id)
        try:
            with self._sessions.begin() as session:
                result = cast(CursorResult[Any], session.execute(
                    delete(ReviewerReliabilitySnapshotRow).where(
                        ReviewerReliabilitySnapshotRow.tenant_id == tenant_id
                    )
                ))
                return int(result.rowcount or 0)
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to delete reviewer snapshots") from exc

    def _purge_before_sync(self, tenant_id: str, older_than: datetime) -> int:
        self._require_text("tenant_id", tenant_id)
        self._require_aware("older_than", older_than)
        try:
            with self._sessions.begin() as session:
                result = cast(CursorResult[Any], session.execute(
                    delete(ReviewerReliabilitySnapshotRow).where(
                        ReviewerReliabilitySnapshotRow.tenant_id == tenant_id,
                        ReviewerReliabilitySnapshotRow.calculated_at < older_than,
                    )
                ))
                return int(result.rowcount or 0)
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to purge reviewer snapshots") from exc

    @staticmethod
    def _snapshot_id(profile: ReviewerReliabilityProfile) -> str:
        value = f"{profile.tenant_id}\0{profile.reviewer_id}\0{profile.profile_version}"
        return sha256(f"reviewer-reliability\0{value}".encode("utf-8")).hexdigest()

    @staticmethod
    def _profile_from_row(
        row: ReviewerReliabilitySnapshotRow,
    ) -> ReviewerReliabilityProfile:
        return ReviewerReliabilityProfile(
            tenant_id=row.tenant_id,
            reviewer_id=row.reviewer_id,
            profile_version=row.profile_version,
            policy_version=row.policy_version,
            reviewed_fact_count=row.reviewed_fact_count,
            approved_fact_count=row.approved_fact_count,
            quarantined_fact_count=row.quarantined_fact_count,
            rejected_fact_count=row.rejected_fact_count,
            conflict_count=row.conflict_count,
            reliability_score=row.reliability_score,
            minimum_sample_met=row.minimum_sample_met,
            calculated_at=(
                row.calculated_at
                if row.calculated_at.tzinfo is not None
                else row.calculated_at.replace(tzinfo=UTC)
            ),
        )
