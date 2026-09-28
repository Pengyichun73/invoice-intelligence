"""SQLAlchemy repositories for the reviewed-example PostgreSQL fact source."""

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Engine, case, delete, exists, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import WorkflowPersistenceError
from invoice_intelligence.application.ports.admission import MemorySupportDiversity
from invoice_intelligence.application.ports.projection_lease import ProjectionLease
from invoice_intelligence.domain.admission import MemoryAdmissionStatus
from invoice_intelligence.domain.examples import (
    ExampleEvidenceReference,
    ExampleIndexProjectionState,
    ExampleLabelType,
    ExampleScope,
    IndexProjectionStatus,
    IndexVersion,
    ModelVersion,
    PromptVersion,
    ReviewedExample,
)
from invoice_intelligence.domain.governance import (
    GovernanceAction,
    GovernanceAuditEvent,
    IndexGovernanceRecord,
    IndexProjectionCounts,
)
from invoice_intelligence.domain.workflow import JsonValue
from invoice_intelligence.infrastructure.persistence.sqlalchemy_governance import (
    add_governance_audit,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    CorrectionEventRow,
    CorrectionMemoryRow,
    CorrectionMemorySourceRow,
    DocumentRow,
    ExampleFeedbackRow,
    ExampleIndexProjectionRow,
    ExtractionRunRow,
    IndexVersionRow,
    MemoryAdmissionRecordRow,
    MemoryConflictExampleRow,
    MemoryConflictRow,
    ModelVersionRow,
    PromptVersionRow,
    ReviewedExampleRow,
    ReviewerReliabilitySnapshotRow,
)


class SQLAlchemyExampleRepository:
    """Implement reviewed-case and projection-state ports with short transactions."""

    def __init__(
        self,
        engine: Engine,
        *,
        projection_max_attempts: int = 5,
        projection_backoff_base_seconds: float = 2.0,
        projection_backoff_max_seconds: float = 300.0,
    ) -> None:
        if (
            projection_max_attempts <= 0
            or projection_backoff_base_seconds <= 0
            or projection_backoff_max_seconds < projection_backoff_base_seconds
        ):
            raise ValueError("Invalid projection retry policy")
        self._projection_max_attempts = projection_max_attempts
        self._projection_backoff_base_seconds = projection_backoff_base_seconds
        self._projection_backoff_max_seconds = projection_backoff_max_seconds
        self._dialect_name = engine.dialect.name
        self._sessions = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        )

    async def upsert(self, example: ReviewedExample) -> ReviewedExample:
        return await asyncio.to_thread(self._upsert_sync, example)

    async def get_by_source_event(
        self,
        tenant_id: str,
        source_event_id: str,
    ) -> ReviewedExample | None:
        return await asyncio.to_thread(
            self._get_by_source_event_sync,
            tenant_id,
            source_event_id,
        )

    async def get_many(
        self,
        scope: ExampleScope,
        example_ids: Sequence[str],
    ) -> tuple[ReviewedExample, ...]:
        return await asyncio.to_thread(
            self._get_many_sync,
            scope,
            tuple(example_ids),
        )

    async def get_eligible_by_ids(
        self,
        tenant_id: str,
        schema_version: str,
        example_ids: Sequence[str],
    ) -> tuple[ReviewedExample, ...]:
        return await asyncio.to_thread(
            self._get_eligible_by_ids_sync,
            tenant_id,
            schema_version,
            tuple(example_ids),
        )

    async def list_eligible(
        self,
        tenant_id: str,
        schema_version: str,
        *,
        limit: int,
        after_example_id: str | None = None,
    ) -> tuple[ReviewedExample, ...]:
        return await asyncio.to_thread(
            self._list_eligible_sync,
            tenant_id,
            schema_version,
            limit,
            after_example_id,
        )

    async def list_for_governance(
        self,
        tenant_id: str,
        *,
        schema_version: str | None,
        field_path: str | None,
        label_type: ExampleLabelType | None,
        is_valid: bool | None,
        limit: int,
        after_example_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[ReviewedExample, ...]:
        return await asyncio.to_thread(
            self._list_for_governance_sync,
            tenant_id,
            schema_version,
            field_path,
            label_type,
            is_valid,
            limit,
            after_example_id,
            run_id,
        )

    async def get_for_governance(
        self,
        tenant_id: str,
        example_id: str,
    ) -> ReviewedExample | None:
        return await asyncio.to_thread(
            self._get_for_governance_sync,
            tenant_id,
            example_id,
        )

    async def get_support_diversity(
        self,
        tenant_id: str,
        semantic_fingerprint: str,
    ) -> MemorySupportDiversity:
        return await asyncio.to_thread(
            self._get_support_diversity_sync,
            tenant_id,
            semantic_fingerprint,
        )

    async def invalidate(
        self,
        tenant_id: str,
        example_id: str,
        reason: str,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._invalidate_sync,
            tenant_id,
            example_id,
            reason,
            audit_event,
        )

    async def invalidate_schema(
        self,
        tenant_id: str,
        schema_version: str,
        reason: str,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> int:
        return await asyncio.to_thread(
            self._invalidate_schema_sync,
            tenant_id,
            schema_version,
            reason,
            audit_event,
        )

    async def delete_tenant(self, tenant_id: str) -> int:
        return await asyncio.to_thread(self._delete_tenant_sync, tenant_id)

    async def invalidate_expired(
        self,
        tenant_id: str,
        older_than: datetime,
        reason: str,
    ) -> tuple[str, ...]:
        return await asyncio.to_thread(
            self._invalidate_expired_sync,
            tenant_id,
            older_than,
            reason,
        )

    async def purge_invalidated(
        self,
        tenant_id: str,
        example_ids: Sequence[str],
    ) -> int:
        return await asyncio.to_thread(
            self._purge_invalidated_sync,
            tenant_id,
            tuple(example_ids),
        )

    async def register_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        schema_version: str,
        dense_model_version: ModelVersion,
        sparse_model_version: ModelVersion | None,
        rerank_model_version: ModelVersion | None,
        prompt_version: PromptVersion,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._register_version_sync,
            tenant_id,
            index_version,
            schema_version,
            dense_model_version,
            sparse_model_version,
            rerank_model_version,
            prompt_version,
            audit_event,
        )

    async def schedule(
        self,
        tenant_id: str,
        example_ids: Sequence[str],
        index_version: IndexVersion,
    ) -> None:
        await asyncio.to_thread(
            self._schedule_sync,
            tenant_id,
            tuple(example_ids),
            index_version,
        )

    async def claim_pending(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        limit: int,
        worker_id: str,
        lease_seconds: float,
    ) -> tuple[ProjectionLease[ReviewedExample], ...]:
        return await asyncio.to_thread(
            self._claim_pending_sync,
            tenant_id,
            index_version,
            limit,
            worker_id,
            lease_seconds,
        )

    async def list_pending(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        limit: int,
    ) -> tuple[ReviewedExample, ...]:
        return await asyncio.to_thread(
            self._list_pending_sync,
            tenant_id,
            index_version,
            limit,
        )

    async def requeue_stale(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        stale_before: datetime,
    ) -> int:
        return await asyncio.to_thread(
            self._requeue_stale_sync,
            tenant_id,
            index_version,
            stale_before,
        )

    async def renew_lease(
        self, tenant_id: str, example_id: str, index_version: IndexVersion,
        worker_id: str, lease_token: str, lease_seconds: float,
    ) -> None:
        await asyncio.to_thread(
            self._renew_lease_sync, tenant_id, example_id, index_version,
            worker_id, lease_token, lease_seconds,
        )

    def _renew_lease_sync(
        self, tenant_id: str, example_id: str, index_version: IndexVersion,
        worker_id: str, lease_token: str, lease_seconds: float,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._sessions.begin() as session:
            index_row = self._get_index_row(session, tenant_id, index_version)
            now = self._now()
            result = session.execute(
                update(ExampleIndexProjectionRow).where(
                    ExampleIndexProjectionRow.projection_id == self._projection_id(
                        tenant_id, example_id, index_row.index_version_id
                    ),
                    ExampleIndexProjectionRow.status == IndexProjectionStatus.PROCESSING.value,
                    ExampleIndexProjectionRow.worker_id == worker_id,
                    ExampleIndexProjectionRow.lease_token == lease_token,
                    ExampleIndexProjectionRow.lease_expires_at > now,
                ).values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            )
            if result.rowcount != 1:
                raise WorkflowPersistenceError("Projection lease is no longer owned")

    async def mark_projected(
        self,
        tenant_id: str,
        example_id: str,
        index_version: IndexVersion,
        projection_checksum: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        await asyncio.to_thread(
            self._mark_projected_sync,
            tenant_id,
            example_id,
            index_version,
            projection_checksum,
            worker_id,
            lease_token,
        )

    async def mark_failed(
        self,
        tenant_id: str,
        example_id: str,
        index_version: IndexVersion,
        error_code: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        await asyncio.to_thread(
            self._mark_failed_sync,
            tenant_id,
            example_id,
            index_version,
            error_code,
            worker_id,
            lease_token,
        )

    async def get_active_version(self, tenant_id: str) -> IndexVersion | None:
        return await asyncio.to_thread(self._get_active_version_sync, tenant_id)

    async def list_index_versions(
        self,
        tenant_id: str,
        *,
        schema_version: str | None = None,
    ) -> tuple[IndexVersion, ...]:
        return await asyncio.to_thread(
            self._list_index_versions_sync,
            tenant_id,
            schema_version,
        )

    async def list_example_projections(
        self,
        tenant_id: str,
        example_id: str,
        *,
        index_version: IndexVersion | None,
        status: IndexProjectionStatus | None,
        limit: int,
        after_projection_id: str | None = None,
    ) -> tuple[ExampleIndexProjectionState, ...]:
        return await asyncio.to_thread(
            self._list_example_projections_sync,
            tenant_id,
            example_id,
            index_version,
            status,
            limit,
            after_projection_id,
        )

    async def get_index_governance(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> IndexGovernanceRecord | None:
        return await asyncio.to_thread(
            self._get_index_governance_sync,
            tenant_id,
            index_version,
        )

    async def activate_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        await asyncio.to_thread(
            self._activate_version_sync,
            tenant_id,
            index_version,
        )

    def _upsert_sync(self, example: ReviewedExample) -> ReviewedExample:
        try:
            with self._sessions.begin() as session:
                self._validate_example_ownership(session, example)
                self._validate_correction_source(session, example)
                model_version_id = self._ensure_model_version(
                    session,
                    example.tenant_id,
                    example.model_version,
                )
                prompt_version_id = self._ensure_prompt_version(
                    session,
                    example.tenant_id,
                    example.prompt_version,
                )
                replay_key = self._example_replay_key(example)
                source_feedback_id, source_created = self._ensure_feedback(
                    session,
                    example,
                    replay_key,
                    model_version_id,
                    prompt_version_id,
                )
                now = self._now()
                values = {
                    "example_id": example.example_id,
                    "tenant_id": example.tenant_id,
                    "replay_key": replay_key,
                    "semantic_fingerprint": example.fingerprint,
                    "source_feedback_id": source_feedback_id,
                    "source_event_id": example.source_event_id,
                    "document_id": example.document_id,
                    "run_id": example.run_id,
                    "document_type": example.document_type,
                    "field_path": example.field_path,
                    "schema_version": example.schema_version,
                    "catalog_version": example.catalog_version,
                    "model_version_id": model_version_id,
                    "prompt_version_id": prompt_version_id,
                    "label_type": example.label_type.value,
                    "model_value_json": example.model_value,
                    "reviewed_value_json": example.reviewed_value,
                    "correction_reason": example.correction_reason,
                    "vendor_fingerprint": example.vendor_fingerprint,
                    "template_fingerprint": example.template_fingerprint,
                    "evidence_reference_json": self._evidence_payload(
                        example.evidence_reference
                    ),
                    "reviewer_id": example.reviewer_id,
                    "is_reviewed": example.is_reviewed,
                    "is_valid": example.is_valid,
                    "invalidated_reason": None,
                    "occurrence_count": 1,
                    "created_at": example.created_at,
                    "updated_at": now,
                    "last_seen_at": example.last_seen_at,
                    "invalidated_at": None,
                }
                canonical_created = self._insert_do_nothing(
                    session,
                    ReviewedExampleRow,
                    values,
                )
                record = self._find_example_record(
                    session,
                    example.tenant_id,
                    example.fingerprint,
                )
                if record is None:
                    row = session.get(ReviewedExampleRow, example.example_id)
                    if row is None:
                        raise WorkflowPersistenceError(
                            "Reviewed example upsert did not produce a durable row"
                        )
                    record = self._record_for_row(session, row)
                if source_created and not canonical_created:
                    session.execute(
                        update(ReviewedExampleRow)
                        .where(
                            ReviewedExampleRow.tenant_id == example.tenant_id,
                            ReviewedExampleRow.semantic_fingerprint
                            == example.fingerprint,
                        )
                        .values(
                            occurrence_count=ReviewedExampleRow.occurrence_count + 1,
                            last_seen_at=case(
                                (
                                    ReviewedExampleRow.last_seen_at
                                    < example.last_seen_at,
                                    example.last_seen_at,
                                ),
                                else_=ReviewedExampleRow.last_seen_at,
                            ),
                            updated_at=now,
                        )
                    )
                    record = self._find_example_record(
                        session,
                        example.tenant_id,
                        example.fingerprint,
                    )
                    if record is None:
                        raise WorkflowPersistenceError(
                            "Canonical reviewed example disappeared during merge"
                        )
                persisted = self._example_from_record(*record)
                if self._canonical(self._semantic_payload(persisted)) != self._canonical(
                    self._semantic_payload(example)
                ):
                    raise WorkflowPersistenceError(
                        "Reviewed-example replay key is bound to different immutable data"
                    )
                return persisted
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to persist reviewed example") from exc

    def _get_by_source_event_sync(
        self,
        tenant_id: str,
        source_event_id: str,
    ) -> ReviewedExample | None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("source_event_id", source_event_id)
        try:
            with self._sessions() as session:
                statement = self._example_select().where(
                    ReviewedExampleRow.tenant_id == tenant_id,
                    ExampleFeedbackRow.tenant_id == tenant_id,
                    ExampleFeedbackRow.source_event_id == source_event_id,
                ).join(
                    ExampleFeedbackRow,
                    (ExampleFeedbackRow.tenant_id == ReviewedExampleRow.tenant_id)
                    & (
                        ExampleFeedbackRow.semantic_fingerprint
                        == ReviewedExampleRow.semantic_fingerprint
                    ),
                ).limit(1)
                record = session.execute(statement).one_or_none()
                return self._example_from_record(*record) if record is not None else None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to retrieve reviewed example") from exc

    def _get_many_sync(
        self,
        scope: ExampleScope,
        example_ids: tuple[str, ...],
    ) -> tuple[ReviewedExample, ...]:
        if not example_ids:
            return ()
        try:
            with self._sessions() as session:
                records = session.execute(
                    self._example_select()
                    .join(
                        MemoryAdmissionRecordRow,
                        MemoryAdmissionRecordRow.example_id == ReviewedExampleRow.example_id,
                    )
                    .where(
                        ReviewedExampleRow.example_id.in_(example_ids),
                        ReviewedExampleRow.tenant_id == scope.tenant_id,
                        ReviewedExampleRow.document_type == scope.document_type,
                        ReviewedExampleRow.field_path == scope.field_path,
                        ReviewedExampleRow.schema_version == scope.schema_version,
                        ReviewedExampleRow.catalog_version == scope.catalog_version,
                        ReviewedExampleRow.is_reviewed.is_(True),
                        ReviewedExampleRow.is_valid.is_(True),
                        MemoryAdmissionRecordRow.tenant_id == scope.tenant_id,
                        MemoryAdmissionRecordRow.status
                        == MemoryAdmissionStatus.APPROVED.value,
                    )
                ).all()
                mapped = {
                    record[0].example_id: self._example_from_record(*record)
                    for record in records
                }
                return tuple(mapped[item] for item in example_ids if item in mapped)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to retrieve reviewed examples") from exc

    def _get_eligible_by_ids_sync(
        self,
        tenant_id: str,
        schema_version: str,
        example_ids: tuple[str, ...],
    ) -> tuple[ReviewedExample, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        if not example_ids:
            return ()
        try:
            with self._sessions() as session:
                records = session.execute(
                    self._example_select()
                    .join(
                        MemoryAdmissionRecordRow,
                        MemoryAdmissionRecordRow.example_id == ReviewedExampleRow.example_id,
                    )
                    .where(
                        ReviewedExampleRow.example_id.in_(example_ids),
                        ReviewedExampleRow.tenant_id == tenant_id,
                        ReviewedExampleRow.schema_version == schema_version,
                        ReviewedExampleRow.is_reviewed.is_(True),
                        ReviewedExampleRow.is_valid.is_(True),
                        MemoryAdmissionRecordRow.tenant_id == tenant_id,
                        MemoryAdmissionRecordRow.status
                        == MemoryAdmissionStatus.APPROVED.value,
                    )
                ).all()
                mapped = {
                    record[0].example_id: self._example_from_record(*record)
                    for record in records
                }
                return tuple(mapped[item] for item in example_ids if item in mapped)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to hydrate training examples") from exc

    def _list_eligible_sync(
        self,
        tenant_id: str,
        schema_version: str,
        limit: int,
        after_example_id: str | None,
    ) -> tuple[ReviewedExample, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if after_example_id is not None:
            self._require_text("after_example_id", after_example_id)
        try:
            with self._sessions() as session:
                statement = (
                    self._example_select()
                    .join(
                        MemoryAdmissionRecordRow,
                        MemoryAdmissionRecordRow.example_id == ReviewedExampleRow.example_id,
                    )
                    .where(
                        ReviewedExampleRow.tenant_id == tenant_id,
                        ReviewedExampleRow.schema_version == schema_version,
                        ReviewedExampleRow.is_reviewed.is_(True),
                        ReviewedExampleRow.is_valid.is_(True),
                        MemoryAdmissionRecordRow.tenant_id == tenant_id,
                        MemoryAdmissionRecordRow.status
                        == MemoryAdmissionStatus.APPROVED.value,
                    )
                )
                if after_example_id is not None:
                    statement = statement.where(
                        ReviewedExampleRow.example_id > after_example_id
                    )
                records = session.execute(
                    statement.order_by(ReviewedExampleRow.example_id).limit(limit)
                ).all()
                return tuple(self._example_from_record(*record) for record in records)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to page training examples") from exc

    def _list_for_governance_sync(
        self,
        tenant_id: str,
        schema_version: str | None,
        field_path: str | None,
        label_type: ExampleLabelType | None,
        is_valid: bool | None,
        limit: int,
        after_example_id: str | None,
        run_id: str | None,
    ) -> tuple[ReviewedExample, ...]:
        self._require_text("tenant_id", tenant_id)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        for name, value in (
            ("schema_version", schema_version),
            ("field_path", field_path),
            ("after_example_id", after_example_id),
            ("run_id", run_id),
        ):
            if value is not None:
                self._require_text(name, value)
        try:
            with self._sessions() as session:
                statement = self._example_select().where(
                    ReviewedExampleRow.tenant_id == tenant_id
                )
                if schema_version is not None:
                    statement = statement.where(
                        ReviewedExampleRow.schema_version == schema_version
                    )
                if field_path is not None:
                    statement = statement.where(ReviewedExampleRow.field_path == field_path)
                if run_id is not None:
                    statement = statement.where(
                        exists().where(
                            ExampleFeedbackRow.tenant_id == tenant_id,
                            ExampleFeedbackRow.semantic_fingerprint
                            == ReviewedExampleRow.semantic_fingerprint,
                            ExampleFeedbackRow.run_id == run_id,
                        )
                    )
                if label_type is not None:
                    statement = statement.where(
                        ReviewedExampleRow.label_type == label_type.value
                    )
                if is_valid is not None:
                    statement = statement.where(ReviewedExampleRow.is_valid.is_(is_valid))
                if after_example_id is not None:
                    cursor_row = session.scalar(
                        select(ReviewedExampleRow).where(
                            ReviewedExampleRow.tenant_id == tenant_id,
                            ReviewedExampleRow.example_id == after_example_id,
                        )
                    )
                    if cursor_row is None:
                        return ()
                    statement = statement.where(
                        tuple_(ReviewedExampleRow.last_seen_at, ReviewedExampleRow.example_id)
                        < (cursor_row.last_seen_at, after_example_id)
                    )
                records = session.execute(
                    statement.order_by(
                        ReviewedExampleRow.last_seen_at.desc(),
                        ReviewedExampleRow.example_id.desc(),
                    ).limit(limit)
                ).all()
                return tuple(self._example_from_record(*record) for record in records)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to page governed examples") from exc

    def _get_for_governance_sync(
        self,
        tenant_id: str,
        example_id: str,
    ) -> ReviewedExample | None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("example_id", example_id)
        try:
            with self._sessions() as session:
                record = session.execute(
                    self._example_select().where(
                        ReviewedExampleRow.tenant_id == tenant_id,
                        ReviewedExampleRow.example_id == example_id,
                    )
                ).one_or_none()
                return self._example_from_record(*record) if record is not None else None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to retrieve governed example") from exc

    def _get_support_diversity_sync(
        self,
        tenant_id: str,
        semantic_fingerprint: str,
    ) -> MemorySupportDiversity:
        self._require_text("tenant_id", tenant_id)
        self._require_text("semantic_fingerprint", semantic_fingerprint)
        try:
            with self._sessions() as session:
                counts = session.execute(
                    select(
                        func.count(func.distinct(ExampleFeedbackRow.document_id)),
                        func.count(func.distinct(ExampleFeedbackRow.reviewer_id)),
                    ).where(
                        ExampleFeedbackRow.tenant_id == tenant_id,
                        ExampleFeedbackRow.semantic_fingerprint
                        == semantic_fingerprint,
                        ExampleFeedbackRow.is_valid.is_(True),
                    )
                ).one()
                template_fingerprint = session.scalar(
                    select(ReviewedExampleRow.template_fingerprint).where(
                        ReviewedExampleRow.tenant_id == tenant_id,
                        ReviewedExampleRow.semantic_fingerprint
                        == semantic_fingerprint,
                    )
                )
                return MemorySupportDiversity(
                    distinct_documents=int(counts[0] or 0),
                    distinct_templates=1 if template_fingerprint is not None else 0,
                    distinct_reviewers=int(counts[1] or 0),
                )
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to calculate reviewed-example support diversity"
            ) from exc

    def _invalidate_sync(
        self,
        tenant_id: str,
        example_id: str,
        reason: str,
        audit_event: GovernanceAuditEvent | None,
    ) -> bool:
        self._require_text("tenant_id", tenant_id)
        self._require_text("example_id", example_id)
        self._require_text("reason", reason)
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(ReviewedExampleRow).where(
                        ReviewedExampleRow.tenant_id == tenant_id,
                        ReviewedExampleRow.example_id == example_id,
                    )
                )
                if row is None:
                    return False
                now = self._now()
                row.is_valid = False
                row.invalidated_reason = reason
                row.invalidated_at = now
                row.updated_at = now
                feedback_rows = session.scalars(
                    select(ExampleFeedbackRow).where(
                        ExampleFeedbackRow.tenant_id == tenant_id,
                        ExampleFeedbackRow.semantic_fingerprint
                        == row.semantic_fingerprint,
                    )
                ).all()
                for feedback in feedback_rows:
                    feedback.is_valid = False
                    feedback.invalidated_at = now
                session.execute(
                    update(ExampleIndexProjectionRow)
                    .where(
                        ExampleIndexProjectionRow.tenant_id == tenant_id,
                        ExampleIndexProjectionRow.example_id == example_id,
                    )
                    .values(
                        status=IndexProjectionStatus.INVALIDATED.value,
                        processing_started_at=None, worker_id=None,
                        lease_token=None, lease_expires_at=None,
                        invalidated_at=now,
                        updated_at=now,
                    )
                )
                self._invalidate_legacy_memories(
                    session,
                    tuple(
                        feedback.source_event_id
                        for feedback in feedback_rows
                        if feedback.source_event_id is not None
                    ),
                    reason,
                )
                if audit_event is not None:
                    self._require_audit_scope(
                        audit_event,
                        GovernanceAction.DISABLE_EXAMPLE,
                        tenant_id,
                        "reviewed_example",
                        example_id,
                        None,
                    )
                    add_governance_audit(session, audit_event)
                return True
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to invalidate reviewed example") from exc

    def _invalidate_schema_sync(
        self,
        tenant_id: str,
        schema_version: str,
        reason: str,
        audit_event: GovernanceAuditEvent | None,
    ) -> int:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        self._require_text("reason", reason)
        try:
            with self._sessions.begin() as session:
                rows = session.scalars(
                    select(ReviewedExampleRow).where(
                        ReviewedExampleRow.tenant_id == tenant_id,
                        ReviewedExampleRow.schema_version == schema_version,
                        ReviewedExampleRow.is_valid.is_(True),
                    )
                ).all()
                now = self._now()
                example_ids = tuple(row.example_id for row in rows)
                source_event_ids = tuple(
                    item
                    for item in session.scalars(
                        select(ExampleFeedbackRow.source_event_id).where(
                            ExampleFeedbackRow.tenant_id == tenant_id,
                            ExampleFeedbackRow.schema_version == schema_version,
                            ExampleFeedbackRow.source_event_id.is_not(None),
                        )
                    ).all()
                    if item is not None
                )
                for row in rows:
                    row.is_valid = False
                    row.invalidated_reason = reason
                    row.invalidated_at = now
                    row.updated_at = now
                session.execute(
                    update(ExampleFeedbackRow)
                    .where(
                        ExampleFeedbackRow.tenant_id == tenant_id,
                        ExampleFeedbackRow.schema_version == schema_version,
                    )
                    .values(is_valid=False, invalidated_at=now)
                )
                if example_ids:
                    session.execute(
                        update(ExampleIndexProjectionRow)
                        .where(
                            ExampleIndexProjectionRow.tenant_id == tenant_id,
                            ExampleIndexProjectionRow.example_id.in_(example_ids),
                        )
                        .values(
                            status=IndexProjectionStatus.INVALIDATED.value,
                            processing_started_at=None, worker_id=None,
                            lease_token=None, lease_expires_at=None,
                            invalidated_at=now,
                            updated_at=now,
                        )
                    )
                self._invalidate_legacy_memories(session, source_event_ids, reason)
                session.execute(
                    update(IndexVersionRow)
                    .where(
                        IndexVersionRow.tenant_id == tenant_id,
                        IndexVersionRow.schema_version == schema_version,
                    )
                    .values(
                        is_active=False,
                        is_valid=False,
                        invalidated_reason=reason,
                        retired_at=now,
                        invalidated_at=now,
                    )
                )
                if audit_event is not None:
                    self._require_audit_scope(
                        audit_event,
                        GovernanceAction.INVALIDATE_SCHEMA,
                        tenant_id,
                        "schema_version",
                        schema_version,
                        schema_version,
                    )
                    add_governance_audit(session, audit_event)
                return len(rows)
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to invalidate example Schema") from exc

    def _delete_tenant_sync(self, tenant_id: str) -> int:
        self._require_text("tenant_id", tenant_id)
        try:
            with self._sessions.begin() as session:
                count = int(
                    session.scalar(
                        select(func.count())
                        .select_from(ReviewedExampleRow)
                        .where(ReviewedExampleRow.tenant_id == tenant_id)
                    )
                    or 0
                )
                source_event_ids = tuple(
                    item
                    for item in session.scalars(
                        select(ReviewedExampleRow.source_event_id).where(
                            ReviewedExampleRow.tenant_id == tenant_id,
                            ReviewedExampleRow.source_event_id.is_not(None),
                        )
                    ).all()
                    if item is not None
                )
                self._delete_legacy_memories(session, source_event_ids)
                session.execute(
                    delete(MemoryConflictExampleRow).where(
                        MemoryConflictExampleRow.tenant_id == tenant_id
                    )
                )
                session.execute(
                    delete(MemoryConflictRow).where(MemoryConflictRow.tenant_id == tenant_id)
                )
                session.execute(
                    delete(ReviewerReliabilitySnapshotRow).where(
                        ReviewerReliabilitySnapshotRow.tenant_id == tenant_id
                    )
                )
                session.execute(
                    delete(ExampleIndexProjectionRow).where(
                        ExampleIndexProjectionRow.tenant_id == tenant_id
                    )
                )
                session.execute(
                    delete(ReviewedExampleRow).where(
                        ReviewedExampleRow.tenant_id == tenant_id
                    )
                )
                session.execute(
                    delete(ExampleFeedbackRow).where(
                        ExampleFeedbackRow.tenant_id == tenant_id
                    )
                )
                session.execute(
                    delete(IndexVersionRow).where(IndexVersionRow.tenant_id == tenant_id)
                )
                session.execute(
                    delete(PromptVersionRow).where(PromptVersionRow.tenant_id == tenant_id)
                )
                session.execute(
                    delete(ModelVersionRow).where(ModelVersionRow.tenant_id == tenant_id)
                )
                return count
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to delete tenant example data") from exc

    def _invalidate_expired_sync(
        self,
        tenant_id: str,
        older_than: datetime,
        reason: str,
    ) -> tuple[str, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("reason", reason)
        if older_than.tzinfo is None:
            raise ValueError("older_than must be timezone-aware")
        try:
            with self._sessions.begin() as session:
                rows = tuple(
                    session.scalars(
                        select(ReviewedExampleRow).where(
                            ReviewedExampleRow.tenant_id == tenant_id,
                            ReviewedExampleRow.last_seen_at < older_than,
                        )
                    ).all()
                )
                if not rows:
                    return ()
                example_ids = tuple(row.example_id for row in rows)
                fingerprints = tuple(row.semantic_fingerprint for row in rows)
                now = self._now()
                source_event_ids = tuple(
                    item for item in session.scalars(
                        select(ExampleFeedbackRow.source_event_id).where(
                            ExampleFeedbackRow.tenant_id == tenant_id,
                            ExampleFeedbackRow.semantic_fingerprint.in_(fingerprints),
                            ExampleFeedbackRow.source_event_id.is_not(None),
                        )
                    ).all() if item is not None
                )
                session.execute(
                    update(ExampleIndexProjectionRow).where(
                        ExampleIndexProjectionRow.tenant_id == tenant_id,
                        ExampleIndexProjectionRow.example_id.in_(example_ids),
                    ).values(
                        status=IndexProjectionStatus.INVALIDATED.value,
                        processing_started_at=None, worker_id=None,
                        lease_token=None, lease_expires_at=None,
                        invalidated_at=now,
                        updated_at=now,
                    )
                )
                session.execute(
                    update(ReviewedExampleRow).where(
                        ReviewedExampleRow.tenant_id == tenant_id,
                        ReviewedExampleRow.example_id.in_(example_ids),
                    ).values(
                        is_valid=False,
                        invalidated_reason=reason,
                        invalidated_at=now,
                        updated_at=now,
                    )
                )
                session.execute(
                    update(ExampleFeedbackRow).where(
                        ExampleFeedbackRow.tenant_id == tenant_id,
                        ExampleFeedbackRow.semantic_fingerprint.in_(fingerprints),
                    ).values(is_valid=False, invalidated_at=now)
                )
                self._invalidate_legacy_memories(session, source_event_ids, reason)
                return example_ids
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to invalidate expired examples") from exc

    def _purge_invalidated_sync(
        self,
        tenant_id: str,
        example_ids: tuple[str, ...],
    ) -> int:
        self._require_text("tenant_id", tenant_id)
        if not example_ids:
            return 0
        try:
            with self._sessions.begin() as session:
                rows = tuple(
                    session.scalars(
                        select(ReviewedExampleRow).where(
                            ReviewedExampleRow.tenant_id == tenant_id,
                            ReviewedExampleRow.example_id.in_(example_ids),
                            ReviewedExampleRow.is_valid.is_(False),
                        )
                    ).all()
                )
                if not rows:
                    return 0
                purge_ids = tuple(row.example_id for row in rows)
                fingerprints = tuple(row.semantic_fingerprint for row in rows)
                source_event_ids = tuple(
                    item for item in session.scalars(
                        select(ExampleFeedbackRow.source_event_id).where(
                            ExampleFeedbackRow.tenant_id == tenant_id,
                            ExampleFeedbackRow.semantic_fingerprint.in_(fingerprints),
                            ExampleFeedbackRow.source_event_id.is_not(None),
                        )
                    ).all() if item is not None
                )
                self._delete_legacy_memories(session, source_event_ids)
                session.execute(
                    delete(ExampleIndexProjectionRow).where(
                        ExampleIndexProjectionRow.tenant_id == tenant_id,
                        ExampleIndexProjectionRow.example_id.in_(purge_ids),
                    )
                )
                session.execute(
                    delete(ReviewedExampleRow).where(
                        ReviewedExampleRow.tenant_id == tenant_id,
                        ReviewedExampleRow.example_id.in_(purge_ids),
                    )
                )
                session.execute(
                    delete(MemoryConflictRow).where(
                        MemoryConflictRow.tenant_id == tenant_id,
                        ~(
                            select(MemoryConflictExampleRow.conflict_id)
                            .where(
                                MemoryConflictExampleRow.conflict_id
                                == MemoryConflictRow.conflict_id
                            )
                            .exists()
                        ),
                    )
                )
                session.execute(
                    delete(ExampleFeedbackRow).where(
                        ExampleFeedbackRow.tenant_id == tenant_id,
                        ExampleFeedbackRow.semantic_fingerprint.in_(fingerprints),
                    )
                )
                return len(rows)
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to purge invalidated examples") from exc

    def _register_version_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        schema_version: str,
        dense_model_version: ModelVersion,
        sparse_model_version: ModelVersion | None,
        rerank_model_version: ModelVersion | None,
        prompt_version: PromptVersion,
        audit_event: GovernanceAuditEvent | None,
    ) -> None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        try:
            with self._sessions.begin() as session:
                dense_id = self._ensure_model_version(
                    session,
                    tenant_id,
                    dense_model_version,
                )
                sparse_id = (
                    self._ensure_model_version(session, tenant_id, sparse_model_version)
                    if sparse_model_version is not None
                    else None
                )
                rerank_id = (
                    self._ensure_model_version(session, tenant_id, rerank_model_version)
                    if rerank_model_version is not None
                    else None
                )
                prompt_id = self._ensure_prompt_version(session, tenant_id, prompt_version)
                index_id = self._version_id("index", tenant_id, index_version.value)
                values = {
                    "index_version_id": index_id,
                    "tenant_id": tenant_id,
                    "version": index_version.value,
                    "schema_version": schema_version,
                    "dense_model_version_id": dense_id,
                    "sparse_model_version_id": sparse_id,
                    "rerank_model_version_id": rerank_id,
                    "prompt_version_id": prompt_id,
                    "is_active": False,
                    "is_valid": True,
                    "invalidated_reason": None,
                    "created_at": self._now(),
                    "activated_at": None,
                    "retired_at": None,
                    "invalidated_at": None,
                }
                self._insert_do_nothing(session, IndexVersionRow, values)
                row = session.get(IndexVersionRow, index_id)
                if row is None or self._index_definition(row) != (
                    tenant_id,
                    index_version.value,
                    schema_version,
                    dense_id,
                    sparse_id,
                    rerank_id,
                    prompt_id,
                ):
                    raise WorkflowPersistenceError(
                        "Index version is bound to different immutable configuration"
                    )
                if not row.is_valid:
                    raise WorkflowPersistenceError(
                        "Invalidated index version cannot be registered again"
                    )
                examples = session.scalars(
                    select(ReviewedExampleRow)
                    .join(
                        MemoryAdmissionRecordRow,
                        MemoryAdmissionRecordRow.example_id == ReviewedExampleRow.example_id,
                    )
                    .where(
                        ReviewedExampleRow.tenant_id == tenant_id,
                        ReviewedExampleRow.schema_version == schema_version,
                        ReviewedExampleRow.is_reviewed.is_(True),
                        ReviewedExampleRow.is_valid.is_(True),
                        MemoryAdmissionRecordRow.tenant_id == tenant_id,
                        MemoryAdmissionRecordRow.status
                        == MemoryAdmissionStatus.APPROVED.value,
                    )
                ).all()
                self._schedule_rows(session, tuple(examples), row)
                if audit_event is not None:
                    self._require_audit_scope(
                        audit_event,
                        GovernanceAction.REBUILD_INDEX,
                        tenant_id,
                        "index_version",
                        index_version.value,
                        index_version.value,
                    )
                    add_governance_audit(session, audit_event)
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to register index version") from exc

    def _schedule_sync(
        self,
        tenant_id: str,
        example_ids: tuple[str, ...],
        index_version: IndexVersion,
    ) -> None:
        if not example_ids:
            return
        self._require_text("tenant_id", tenant_id)
        try:
            with self._sessions.begin() as session:
                index_row = self._get_index_row(session, tenant_id, index_version)
                examples = session.scalars(
                    select(ReviewedExampleRow)
                    .join(
                        MemoryAdmissionRecordRow,
                        MemoryAdmissionRecordRow.example_id == ReviewedExampleRow.example_id,
                    )
                    .where(
                        ReviewedExampleRow.tenant_id == tenant_id,
                        ReviewedExampleRow.example_id.in_(example_ids),
                        ReviewedExampleRow.schema_version == index_row.schema_version,
                        ReviewedExampleRow.is_reviewed.is_(True),
                        ReviewedExampleRow.is_valid.is_(True),
                        MemoryAdmissionRecordRow.tenant_id == tenant_id,
                        MemoryAdmissionRecordRow.status
                        == MemoryAdmissionStatus.APPROVED.value,
                    )
                ).all()
                if len(examples) != len(set(example_ids)):
                    raise WorkflowPersistenceError(
                        "Projection scheduling contains missing or ineligible examples"
                    )
                self._schedule_rows(session, tuple(examples), index_row)
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to schedule example projections") from exc

    def _claim_pending_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        limit: int,
        worker_id: str,
        lease_seconds: float,
    ) -> tuple[ProjectionLease[ReviewedExample], ...]:
        if limit <= 0:
            raise ValueError("Projection claim limit must be greater than zero")
        self._require_text("worker_id", worker_id)
        if len(worker_id) > 128 or lease_seconds <= 0:
            raise ValueError("Invalid projection worker lease")
        self._require_text("tenant_id", tenant_id)
        try:
            with self._sessions.begin() as session:
                index_row = self._get_index_row(session, tenant_id, index_version)
                statement = (
                    select(ExampleIndexProjectionRow)
                    .join(
                        ReviewedExampleRow,
                        (ReviewedExampleRow.example_id == ExampleIndexProjectionRow.example_id)
                        & (ReviewedExampleRow.tenant_id == ExampleIndexProjectionRow.tenant_id),
                    )
                    .join(
                        MemoryAdmissionRecordRow,
                        MemoryAdmissionRecordRow.example_id == ReviewedExampleRow.example_id,
                    )
                    .where(
                        ExampleIndexProjectionRow.tenant_id == tenant_id,
                        ExampleIndexProjectionRow.index_version_id
                        == index_row.index_version_id,
                        ExampleIndexProjectionRow.attempt_count < self._projection_max_attempts,
                        (
                            (
                                ExampleIndexProjectionRow.status.in_((
                                    IndexProjectionStatus.PENDING.value,
                                    IndexProjectionStatus.FAILED.value,
                                ))
                                & (
                                    ExampleIndexProjectionRow.next_attempt_at.is_(None)
                                    | (ExampleIndexProjectionRow.next_attempt_at <= self._now())
                                )
                            )
                            | (
                                (ExampleIndexProjectionRow.status
                                 == IndexProjectionStatus.PROCESSING.value)
                                & (ExampleIndexProjectionRow.lease_expires_at <= self._now())
                            )
                        ),
                        ReviewedExampleRow.is_reviewed.is_(True),
                        ReviewedExampleRow.is_valid.is_(True),
                        MemoryAdmissionRecordRow.tenant_id == tenant_id,
                        MemoryAdmissionRecordRow.status
                        == MemoryAdmissionStatus.APPROVED.value,
                    )
                    .order_by(
                        ExampleIndexProjectionRow.updated_at,
                        ExampleIndexProjectionRow.projection_id,
                    )
                    .limit(limit)
                )
                if self._dialect_name == "postgresql":
                    statement = statement.with_for_update(skip_locked=True)
                projections = session.scalars(statement).all()
                now = self._now()
                claims: list[tuple[str, str, datetime]] = []
                for projection in projections:
                    projection.status = IndexProjectionStatus.PROCESSING.value
                    projection.attempt_count += 1
                    projection.last_error_code = None
                    projection.processing_started_at = now
                    projection.worker_id = worker_id
                    projection.lease_token = uuid4().hex
                    projection.lease_expires_at = now + timedelta(seconds=lease_seconds)
                    projection.next_attempt_at = None
                    projection.updated_at = now
                    claims.append((
                        projection.example_id,
                        projection.lease_token,
                        projection.lease_expires_at,
                    ))
                example_ids = tuple(item[0] for item in claims)
                records = self._load_example_records(session, tuple(example_ids))
                mapped = {
                    record[0].example_id: self._example_from_record(*record)
                    for record in records
                }
                return tuple(
                    ProjectionLease(mapped[item], worker_id, token, expires_at)
                    for item, token, expires_at in claims
                )
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to claim example projections") from exc

    def _list_pending_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        limit: int,
    ) -> tuple[ReviewedExample, ...]:
        if limit <= 0:
            raise ValueError("Projection list limit must be greater than zero")
        self._require_text("tenant_id", tenant_id)
        try:
            with self._sessions() as session:
                index_row = self._get_index_row(session, tenant_id, index_version)
                statement = (
                    self._example_select()
                    .join(
                        ExampleIndexProjectionRow,
                        (ExampleIndexProjectionRow.example_id == ReviewedExampleRow.example_id)
                        & (ExampleIndexProjectionRow.tenant_id == ReviewedExampleRow.tenant_id),
                    )
                    .join(
                        MemoryAdmissionRecordRow,
                        MemoryAdmissionRecordRow.example_id == ReviewedExampleRow.example_id,
                    )
                    .where(
                        ExampleIndexProjectionRow.tenant_id == tenant_id,
                        ExampleIndexProjectionRow.index_version_id
                        == index_row.index_version_id,
                        ExampleIndexProjectionRow.status.in_(
                            (
                                IndexProjectionStatus.PENDING.value,
                                IndexProjectionStatus.FAILED.value,
                            )
                        ),
                        ReviewedExampleRow.is_reviewed.is_(True),
                        ReviewedExampleRow.is_valid.is_(True),
                        MemoryAdmissionRecordRow.tenant_id == tenant_id,
                        MemoryAdmissionRecordRow.status
                        == MemoryAdmissionStatus.APPROVED.value,
                    )
                    .order_by(
                        ExampleIndexProjectionRow.updated_at,
                        ExampleIndexProjectionRow.projection_id,
                    )
                    .limit(limit)
                )
                records = session.execute(statement).all()
                return tuple(self._example_from_record(*record) for record in records)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to list example projections") from exc

    def _requeue_stale_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        stale_before: datetime,
    ) -> int:
        self._require_text("tenant_id", tenant_id)
        if stale_before.tzinfo is None:
            raise ValueError("stale_before must be timezone-aware")
        try:
            with self._sessions.begin() as session:
                index_row = self._get_index_row(session, tenant_id, index_version)
                result = session.execute(
                    update(ExampleIndexProjectionRow)
                    .where(
                        ExampleIndexProjectionRow.tenant_id == tenant_id,
                        ExampleIndexProjectionRow.index_version_id
                        == index_row.index_version_id,
                        ExampleIndexProjectionRow.status
                        == IndexProjectionStatus.PROCESSING.value,
                        ExampleIndexProjectionRow.lease_expires_at <= stale_before,
                    )
                    .values(
                        status=case(
                            (
                                ExampleIndexProjectionRow.attempt_count
                                >= self._projection_max_attempts,
                                IndexProjectionStatus.FAILED.value,
                            ),
                            else_=IndexProjectionStatus.PENDING.value,
                        ),
                        processing_started_at=None,
                        worker_id=None,
                        lease_token=None,
                        lease_expires_at=None,
                        next_attempt_at=None,
                        last_error_code="projection_lease_expired",
                        updated_at=self._now(),
                    )
                )
                return int(result.rowcount or 0)
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to requeue stale projections") from exc

    def _mark_projected_sync(
        self,
        tenant_id: str,
        example_id: str,
        index_version: IndexVersion,
        projection_checksum: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        self._require_text("projection_checksum", projection_checksum)
        self._require_text("worker_id", worker_id)
        self._require_text("lease_token", lease_token)
        try:
            with self._sessions.begin() as session:
                index_row = self._get_index_row(session, tenant_id, index_version)
                now = self._now()
                result = session.execute(
                    update(ExampleIndexProjectionRow).where(
                        ExampleIndexProjectionRow.projection_id == self._projection_id(
                            tenant_id, example_id, index_row.index_version_id
                        ),
                        ExampleIndexProjectionRow.tenant_id == tenant_id,
                        ExampleIndexProjectionRow.status == IndexProjectionStatus.PROCESSING.value,
                        ExampleIndexProjectionRow.worker_id == worker_id,
                        ExampleIndexProjectionRow.lease_token == lease_token,
                        ExampleIndexProjectionRow.lease_expires_at > now,
                    ).values(
                        status=IndexProjectionStatus.INDEXED.value,
                        projection_checksum=projection_checksum,
                        last_error_code=None, processing_started_at=None,
                        worker_id=None, lease_token=None, lease_expires_at=None,
                        next_attempt_at=None, indexed_at=now, updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    raise WorkflowPersistenceError("Projection lease is no longer owned")
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to mark projection indexed") from exc

    def _mark_failed_sync(
        self,
        tenant_id: str,
        example_id: str,
        index_version: IndexVersion,
        error_code: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        self._require_text("error_code", error_code)
        self._require_text("worker_id", worker_id)
        self._require_text("lease_token", lease_token)
        try:
            with self._sessions.begin() as session:
                index_row = self._get_index_row(session, tenant_id, index_version)
                now = self._now()
                row = self._get_projection(session, tenant_id, example_id, index_version)
                result = session.execute(
                    update(ExampleIndexProjectionRow).where(
                        ExampleIndexProjectionRow.projection_id == self._projection_id(
                            tenant_id, example_id, index_row.index_version_id
                        ),
                        ExampleIndexProjectionRow.status == IndexProjectionStatus.PROCESSING.value,
                        ExampleIndexProjectionRow.worker_id == worker_id,
                        ExampleIndexProjectionRow.lease_token == lease_token,
                        ExampleIndexProjectionRow.lease_expires_at > now,
                    ).values(
                        status=IndexProjectionStatus.FAILED.value,
                        last_error_code=error_code[:128], processing_started_at=None,
                        worker_id=None, lease_token=None, lease_expires_at=None,
                        next_attempt_at=(
                            now + timedelta(seconds=min(
                                self._projection_backoff_max_seconds,
                                self._projection_backoff_base_seconds
                                * 2 ** min(row.attempt_count - 1, 20),
                            )) if row.attempt_count < self._projection_max_attempts else None
                        ),
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    raise WorkflowPersistenceError("Projection lease is no longer owned")
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to mark projection failed") from exc

    def _get_active_version_sync(self, tenant_id: str) -> IndexVersion | None:
        self._require_text("tenant_id", tenant_id)
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(IndexVersionRow).where(
                        IndexVersionRow.tenant_id == tenant_id,
                        IndexVersionRow.is_active.is_(True),
                        IndexVersionRow.is_valid.is_(True),
                    )
                )
                return IndexVersion(row.version) if row is not None else None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to retrieve active index version") from exc

    def _list_index_versions_sync(
        self,
        tenant_id: str,
        schema_version: str | None,
    ) -> tuple[IndexVersion, ...]:
        self._require_text("tenant_id", tenant_id)
        if schema_version is not None:
            self._require_text("schema_version", schema_version)
        try:
            with self._sessions() as session:
                statement = select(IndexVersionRow.version).where(
                    IndexVersionRow.tenant_id == tenant_id
                )
                if schema_version is not None:
                    statement = statement.where(
                        IndexVersionRow.schema_version == schema_version
                    )
                versions = session.scalars(
                    statement.order_by(IndexVersionRow.created_at, IndexVersionRow.version)
                ).all()
                return tuple(IndexVersion(value) for value in versions)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to list index versions") from exc

    def _list_example_projections_sync(
        self,
        tenant_id: str,
        example_id: str,
        index_version: IndexVersion | None,
        status: IndexProjectionStatus | None,
        limit: int,
        after_projection_id: str | None,
    ) -> tuple[ExampleIndexProjectionState, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("example_id", example_id)
        if limit <= 0:
            raise ValueError("Projection query limit must be greater than zero")
        if after_projection_id is not None:
            self._require_text("after_projection_id", after_projection_id)
        try:
            with self._sessions() as session:
                statement = (
                    select(ExampleIndexProjectionRow, IndexVersionRow.version)
                    .join(
                        IndexVersionRow,
                        (
                            IndexVersionRow.index_version_id
                            == ExampleIndexProjectionRow.index_version_id
                        )
                        & (IndexVersionRow.tenant_id == tenant_id),
                    )
                    .where(
                        ExampleIndexProjectionRow.tenant_id == tenant_id,
                        ExampleIndexProjectionRow.example_id == example_id,
                        IndexVersionRow.tenant_id == tenant_id,
                    )
                )
                if index_version is not None:
                    statement = statement.where(
                        IndexVersionRow.version == index_version.value
                    )
                if status is not None:
                    statement = statement.where(
                        ExampleIndexProjectionRow.status == status.value
                    )
                if after_projection_id is not None:
                    statement = statement.where(
                        ExampleIndexProjectionRow.projection_id > after_projection_id
                    )
                records = session.execute(
                    statement.order_by(ExampleIndexProjectionRow.projection_id).limit(
                        limit
                    )
                ).all()
                return tuple(
                    self._projection_state_from_record(row, version)
                    for row, version in records
                )
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to list example projection states"
            ) from exc

    def _get_index_governance_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> IndexGovernanceRecord | None:
        self._require_text("tenant_id", tenant_id)
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(IndexVersionRow).where(
                        IndexVersionRow.tenant_id == tenant_id,
                        IndexVersionRow.version == index_version.value,
                    )
                )
                if row is None:
                    return None
                dense = session.get(ModelVersionRow, row.dense_model_version_id)
                sparse = (
                    session.get(ModelVersionRow, row.sparse_model_version_id)
                    if row.sparse_model_version_id is not None
                    else None
                )
                rerank = (
                    session.get(ModelVersionRow, row.rerank_model_version_id)
                    if row.rerank_model_version_id is not None
                    else None
                )
                prompt = session.get(PromptVersionRow, row.prompt_version_id)
                if (
                    dense is None
                    or prompt is None
                    or (row.sparse_model_version_id is not None and sparse is None)
                    or (row.rerank_model_version_id is not None and rerank is None)
                ):
                    raise WorkflowPersistenceError(
                        "Index version references a missing model or Prompt version"
                    )
                raw_counts = session.execute(
                    select(
                        ExampleIndexProjectionRow.status,
                        func.count(ExampleIndexProjectionRow.projection_id),
                    )
                    .where(
                        ExampleIndexProjectionRow.tenant_id == tenant_id,
                        ExampleIndexProjectionRow.index_version_id == row.index_version_id,
                    )
                    .group_by(ExampleIndexProjectionRow.status)
                ).all()
                counts = {str(status): int(count) for status, count in raw_counts}
                return IndexGovernanceRecord(
                    tenant_id=tenant_id,
                    index_version=IndexVersion(row.version),
                    schema_version=row.schema_version,
                    dense_model_version=ModelVersion(dense.version),
                    sparse_model_version=(
                        ModelVersion(sparse.version) if sparse is not None else None
                    ),
                    rerank_model_version=(
                        ModelVersion(rerank.version) if rerank is not None else None
                    ),
                    prompt_version=PromptVersion(prompt.version),
                    is_active=row.is_active,
                    is_valid=row.is_valid,
                    invalidated_reason=row.invalidated_reason,
                    projection_counts=IndexProjectionCounts(
                        pending=counts.get(IndexProjectionStatus.PENDING.value, 0),
                        processing=counts.get(IndexProjectionStatus.PROCESSING.value, 0),
                        indexed=counts.get(IndexProjectionStatus.INDEXED.value, 0),
                        failed=counts.get(IndexProjectionStatus.FAILED.value, 0),
                        invalidated=counts.get(IndexProjectionStatus.INVALIDATED.value, 0),
                    ),
                    created_at=self._aware(row.created_at),
                    activated_at=(
                        self._aware(row.activated_at) if row.activated_at is not None else None
                    ),
                    retired_at=(
                        self._aware(row.retired_at) if row.retired_at is not None else None
                    ),
                    invalidated_at=(
                        self._aware(row.invalidated_at)
                        if row.invalidated_at is not None
                        else None
                    ),
                )
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to retrieve index governance state") from exc

    def _activate_version_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        self._require_text("tenant_id", tenant_id)
        try:
            with self._sessions.begin() as session:
                row = self._get_index_row(session, tenant_id, index_version)
                if not row.is_valid:
                    raise WorkflowPersistenceError(
                        "Invalidated index version cannot be activated"
                    )
                incomplete = int(
                    session.scalar(
                        select(func.count())
                        .select_from(ExampleIndexProjectionRow)
                        .where(
                            ExampleIndexProjectionRow.tenant_id == tenant_id,
                            ExampleIndexProjectionRow.index_version_id
                            == row.index_version_id,
                            ExampleIndexProjectionRow.status.not_in(
                                (
                                    IndexProjectionStatus.INDEXED.value,
                                    IndexProjectionStatus.INVALIDATED.value,
                                )
                            ),
                        )
                    )
                    or 0
                )
                if incomplete:
                    raise WorkflowPersistenceError(
                        "Index version cannot be activated with incomplete projections"
                    )
                if row.is_active:
                    return
                now = self._now()
                session.execute(
                    update(IndexVersionRow)
                    .where(
                        IndexVersionRow.tenant_id == tenant_id,
                        IndexVersionRow.is_active.is_(True),
                    )
                    .values(is_active=False, retired_at=now)
                )
                session.flush()
                row.is_active = True
                row.activated_at = now
                row.retired_at = None
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to activate index version") from exc

    def _validate_correction_source(
        self,
        session: Session,
        example: ReviewedExample,
    ) -> None:
        if example.label_type is not ExampleLabelType.CORRECTED:
            return
        if example.source_event_id is None:
            raise WorkflowPersistenceError("Corrected example requires a correction event")
        event = session.get(CorrectionEventRow, example.source_event_id)
        if event is None:
            raise WorkflowPersistenceError("Source correction event does not exist")
        if (
            event.run_id != example.run_id
            or event.document_id != example.document_id
            or event.document_type != example.document_type
            or event.field_path != example.field_path
            or event.schema_version != example.schema_version
            or event.correction_reason != example.correction_reason
            or event.document_reference
            != example.evidence_reference.document_reference
            or event.image_reference != example.evidence_reference.image_reference
            or not event.is_reviewed
            or not event.is_valid
            or self._canonical(event.model_value_json)
            != self._canonical(example.model_value)
            or self._canonical(event.corrected_value_json)
            != self._canonical(example.reviewed_value)
        ):
            raise WorkflowPersistenceError(
                "Reviewed example does not match its source correction event"
            )

    @staticmethod
    def _validate_example_ownership(
        session: Session,
        example: ReviewedExample,
    ) -> None:
        run = session.get(ExtractionRunRow, example.run_id)
        document = session.get(DocumentRow, example.document_id)
        if (
            run is None
            or document is None
            or run.tenant_id != example.tenant_id
            or document.tenant_id != example.tenant_id
            or run.document_id != example.document_id
        ):
            raise WorkflowPersistenceError(
                "Reviewed example is outside the run/document tenant scope"
            )

    def _invalidate_legacy_memories(
        self,
        session: Session,
        source_event_ids: Sequence[str],
        reason: str,
    ) -> None:
        if self._dialect_name != "postgresql" or not source_event_ids:
            return
        memory_ids = select(CorrectionMemorySourceRow.memory_id).where(
            CorrectionMemorySourceRow.source_event_id.in_(source_event_ids)
        )
        session.execute(
            update(CorrectionMemoryRow)
            .where(CorrectionMemoryRow.memory_id.in_(memory_ids))
            .values(
                is_valid=False,
                disabled_reason=reason,
                updated_at=self._now(),
            )
        )

    def _delete_legacy_memories(
        self,
        session: Session,
        source_event_ids: Sequence[str],
    ) -> None:
        if self._dialect_name != "postgresql" or not source_event_ids:
            return
        memory_ids = tuple(
            session.scalars(
                select(CorrectionMemorySourceRow.memory_id).where(
                    CorrectionMemorySourceRow.source_event_id.in_(source_event_ids)
                )
            ).all()
        )
        session.execute(
            delete(CorrectionMemorySourceRow).where(
                CorrectionMemorySourceRow.source_event_id.in_(source_event_ids)
            )
        )
        if memory_ids:
            session.execute(
                delete(CorrectionMemoryRow).where(
                    CorrectionMemoryRow.memory_id.in_(memory_ids)
                )
            )

    def _ensure_feedback(
        self,
        session: Session,
        example: ReviewedExample,
        replay_key: str,
        model_version_id: str,
        prompt_version_id: str,
    ) -> tuple[str, bool]:
        values = {
            "feedback_id": example.source_feedback_id,
            "tenant_id": example.tenant_id,
            "replay_key": replay_key,
            "semantic_fingerprint": example.fingerprint,
            "run_id": example.run_id,
            "document_id": example.document_id,
            "field_path": example.field_path,
            "schema_version": example.schema_version,
            "label_type": example.label_type.value,
            "reviewer_id": example.reviewer_id,
            "reason": example.correction_reason,
            "source_event_id": example.source_event_id,
            "evidence_reference_json": self._evidence_payload(
                example.evidence_reference
            ),
            "model_version_id": model_version_id,
            "prompt_version_id": prompt_version_id,
            "is_valid": example.is_valid,
            "created_at": example.created_at,
            "invalidated_at": None,
        }
        created = self._insert_do_nothing(session, ExampleFeedbackRow, values)
        row = session.get(ExampleFeedbackRow, example.source_feedback_id)
        if row is None:
            row = session.scalar(
                select(ExampleFeedbackRow).where(
                    ExampleFeedbackRow.tenant_id == example.tenant_id,
                    ExampleFeedbackRow.replay_key == replay_key,
                )
            )
        immutable_values = {
            key: values[key]
            for key in (
                "tenant_id",
                "replay_key",
                "semantic_fingerprint",
                "run_id",
                "document_id",
                "field_path",
                "schema_version",
                "label_type",
                "reviewer_id",
                "reason",
                "source_event_id",
                "evidence_reference_json",
                "model_version_id",
                "prompt_version_id",
            )
        }
        if row is None or self._feedback_payload(row) != self._canonical(immutable_values):
            raise WorkflowPersistenceError(
                "Review feedback replay key is bound to different immutable data"
            )
        return row.feedback_id, created

    def _ensure_model_version(
        self,
        session: Session,
        tenant_id: str,
        version: ModelVersion,
    ) -> str:
        row_id = self._version_id("model", tenant_id, version.value)
        values = {
            "model_version_id": row_id,
            "tenant_id": tenant_id,
            "version": version.value,
            "created_at": self._now(),
        }
        self._insert_do_nothing(session, ModelVersionRow, values)
        row = session.get(ModelVersionRow, row_id)
        if row is None or row.tenant_id != tenant_id or row.version != version.value:
            raise WorkflowPersistenceError("Model version identity collision")
        return row_id

    def _ensure_prompt_version(
        self,
        session: Session,
        tenant_id: str,
        version: PromptVersion,
    ) -> str:
        row_id = self._version_id("prompt", tenant_id, version.value)
        values = {
            "prompt_version_id": row_id,
            "tenant_id": tenant_id,
            "version": version.value,
            "created_at": self._now(),
        }
        self._insert_do_nothing(session, PromptVersionRow, values)
        row = session.get(PromptVersionRow, row_id)
        if row is None or row.tenant_id != tenant_id or row.version != version.value:
            raise WorkflowPersistenceError("Prompt version identity collision")
        return row_id

    def _schedule_rows(
        self,
        session: Session,
        examples: tuple[ReviewedExampleRow, ...],
        index_row: IndexVersionRow,
    ) -> None:
        now = self._now()
        for example in examples:
            projection_id = self._projection_id(
                example.tenant_id,
                example.example_id,
                index_row.index_version_id,
            )
            values = {
                "projection_id": projection_id,
                "tenant_id": example.tenant_id,
                "example_id": example.example_id,
                "index_version_id": index_row.index_version_id,
                "status": IndexProjectionStatus.PENDING.value,
                "projection_checksum": None,
                "attempt_count": 0,
                "last_error_code": None,
                "processing_started_at": None,
                "indexed_at": None,
                "invalidated_at": None,
                "created_at": now,
                "updated_at": now,
            }
            if self._dialect_name == "postgresql":
                statement = pg_insert(ExampleIndexProjectionRow).values(**values)
            elif self._dialect_name == "sqlite":
                statement = sqlite_insert(ExampleIndexProjectionRow).values(**values)
            else:
                raise WorkflowPersistenceError("Unsupported business database dialect")
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=("tenant_id", "example_id", "index_version_id"),
                    set_={
                        "status": IndexProjectionStatus.PENDING.value,
                        "projection_checksum": None,
                        "attempt_count": 0,
                        "last_error_code": None,
                        "processing_started_at": None,
                        "indexed_at": None,
                        "invalidated_at": None,
                        "updated_at": now,
                    },
                    where=(
                        ExampleIndexProjectionRow.status
                        == IndexProjectionStatus.INVALIDATED.value
                    ),
                )
            )

    def _get_projection(
        self,
        session: Session,
        tenant_id: str,
        example_id: str,
        index_version: IndexVersion,
    ) -> ExampleIndexProjectionRow:
        self._require_text("tenant_id", tenant_id)
        self._require_text("example_id", example_id)
        index_row = self._get_index_row(session, tenant_id, index_version)
        row = session.get(
            ExampleIndexProjectionRow,
            self._projection_id(tenant_id, example_id, index_row.index_version_id),
        )
        if row is None:
            raise WorkflowPersistenceError("Example projection does not exist")
        if row.tenant_id != tenant_id or row.index_version_id != index_row.index_version_id:
            raise WorkflowPersistenceError("Example projection crosses a tenant boundary")
        example = session.get(ReviewedExampleRow, row.example_id)
        if example is None or example.tenant_id != tenant_id:
            raise WorkflowPersistenceError("Example projection references another tenant")
        return row

    def _get_index_row(
        self,
        session: Session,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> IndexVersionRow:
        row = session.get(
            IndexVersionRow,
            self._version_id("index", tenant_id, index_version.value),
        )
        if row is None or row.tenant_id != tenant_id or row.version != index_version.value:
            raise WorkflowPersistenceError("Index version does not exist")
        if not row.is_valid:
            raise WorkflowPersistenceError("Index version is invalidated")
        return row

    def _find_example_record(
        self,
        session: Session,
        tenant_id: str,
        semantic_fingerprint: str,
    ) -> tuple[ReviewedExampleRow, str, str] | None:
        record = session.execute(
            self._example_select().where(
                ReviewedExampleRow.tenant_id == tenant_id,
                ReviewedExampleRow.semantic_fingerprint == semantic_fingerprint,
            )
        ).one_or_none()
        return cast(tuple[ReviewedExampleRow, str, str] | None, record)

    def _record_for_row(
        self,
        session: Session,
        row: ReviewedExampleRow,
    ) -> tuple[ReviewedExampleRow, str, str]:
        record = session.execute(
            self._example_select().where(ReviewedExampleRow.example_id == row.example_id)
        ).one()
        return cast(tuple[ReviewedExampleRow, str, str], record)

    def _load_example_records(
        self,
        session: Session,
        example_ids: tuple[str, ...],
    ) -> tuple[tuple[ReviewedExampleRow, str, str], ...]:
        if not example_ids:
            return ()
        records = session.execute(
            self._example_select().where(ReviewedExampleRow.example_id.in_(example_ids))
        ).all()
        return cast(tuple[tuple[ReviewedExampleRow, str, str], ...], tuple(records))

    @staticmethod
    def _example_select() -> Any:
        return (
            select(
                ReviewedExampleRow,
                ModelVersionRow.version,
                PromptVersionRow.version,
            )
            .join(
                ModelVersionRow,
                ModelVersionRow.model_version_id == ReviewedExampleRow.model_version_id,
            )
            .join(
                PromptVersionRow,
                PromptVersionRow.prompt_version_id == ReviewedExampleRow.prompt_version_id,
            )
        )

    @classmethod
    def _example_from_record(
        cls,
        row: ReviewedExampleRow,
        model_version: str,
        prompt_version: str,
    ) -> ReviewedExample:
        evidence = row.evidence_reference_json
        if not isinstance(evidence, dict):
            raise WorkflowPersistenceError("Persisted example evidence must be an object")
        document_reference = evidence.get("document_reference")
        image_reference = evidence.get("image_reference")
        page_number = evidence.get("page_number")
        if not isinstance(document_reference, str):
            raise WorkflowPersistenceError("Persisted document evidence is invalid")
        if image_reference is not None and not isinstance(image_reference, str):
            raise WorkflowPersistenceError("Persisted image evidence is invalid")
        if page_number is not None and not isinstance(page_number, int):
            raise WorkflowPersistenceError("Persisted evidence page number is invalid")
        return ReviewedExample(
            example_id=row.example_id,
            tenant_id=row.tenant_id,
            source_feedback_id=row.source_feedback_id,
            source_event_id=row.source_event_id,
            document_id=row.document_id,
            run_id=row.run_id,
            document_type=row.document_type,
            field_path=row.field_path,
            schema_version=row.schema_version,
            catalog_version=row.catalog_version,
            model_version=ModelVersion(model_version),
            prompt_version=PromptVersion(prompt_version),
            label_type=ExampleLabelType(row.label_type),
            model_value=cast(JsonValue, row.model_value_json),
            reviewed_value=cast(JsonValue, row.reviewed_value_json),
            correction_reason=row.correction_reason,
            vendor_fingerprint=row.vendor_fingerprint,
            template_fingerprint=row.template_fingerprint,
            evidence_reference=ExampleEvidenceReference(
                document_reference=document_reference,
                image_reference=image_reference,
                page_number=page_number,
                evidence_source=cls._optional_string(
                    evidence,
                    "evidence_source",
                ),
                candidate_values=cls._string_tuple(
                    evidence,
                    "candidate_values",
                ),
                readability=cls._optional_string(evidence, "readability"),
                validation_signals=cls._string_tuple(
                    evidence,
                    "validation_signals",
                ),
                ambiguous=cls._optional_bool(evidence, "ambiguous"),
            ),
            reviewer_id=row.reviewer_id,
            is_reviewed=row.is_reviewed,
            is_valid=row.is_valid,
            created_at=cls._aware(row.created_at),
            fingerprint=row.semantic_fingerprint,
            occurrence_count=row.occurrence_count,
            last_seen_at=cls._aware(row.last_seen_at),
            invalidated_reason=row.invalidated_reason,
            invalidated_at=(
                cls._aware(row.invalidated_at) if row.invalidated_at is not None else None
            ),
        )

    @classmethod
    def _semantic_payload(cls, example: ReviewedExample) -> dict[str, JsonValue]:
        return {
            "tenant_id": example.tenant_id,
            "document_type": example.document_type,
            "field_path": example.field_path,
            "schema_version": example.schema_version,
            "catalog_version": example.catalog_version,
            "model_version": example.model_version.value,
            "prompt_version": example.prompt_version.value,
            "label_type": example.label_type.value,
            "model_value": example.model_value,
            "reviewed_value": example.reviewed_value,
            "correction_reason": example.correction_reason,
            "vendor_fingerprint": example.vendor_fingerprint,
            "template_fingerprint": example.template_fingerprint,
            "is_reviewed": example.is_reviewed,
            "fingerprint": example.fingerprint,
        }

    @classmethod
    def _projection_state_from_record(
        cls,
        row: ExampleIndexProjectionRow,
        index_version: str,
    ) -> ExampleIndexProjectionState:
        return ExampleIndexProjectionState(
            projection_id=row.projection_id,
            tenant_id=row.tenant_id,
            example_id=row.example_id,
            index_version=IndexVersion(index_version),
            status=IndexProjectionStatus(row.status),
            attempt_count=row.attempt_count,
            projection_checksum=row.projection_checksum,
            last_error_code=cls._safe_projection_error_code(row.last_error_code),
            processing_started_at=(
                cls._aware(row.processing_started_at)
                if row.processing_started_at is not None
                else None
            ),
            created_at=cls._aware(row.created_at),
            updated_at=cls._aware(row.updated_at),
            indexed_at=(
                cls._aware(row.indexed_at) if row.indexed_at is not None else None
            ),
            invalidated_at=(
                cls._aware(row.invalidated_at)
                if row.invalidated_at is not None
                else None
            ),
        )

    @staticmethod
    def _safe_projection_error_code(value: str | None) -> str | None:
        if value is None:
            return None
        allowed = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
        if 1 <= len(value) <= 128 and value[0].isalpha() and all(
            character in allowed for character in value
        ):
            return value
        return "projection_error_redacted"

    @staticmethod
    def _evidence_payload(reference: ExampleEvidenceReference) -> dict[str, JsonValue]:
        return {
            "document_reference": reference.document_reference,
            "image_reference": reference.image_reference,
            "page_number": reference.page_number,
            "evidence_source": reference.evidence_source,
            "candidate_values": list(reference.candidate_values),
            "readability": reference.readability,
            "validation_signals": list(reference.validation_signals),
            "ambiguous": reference.ambiguous,
        }

    @classmethod
    def _example_replay_key(cls, example: ReviewedExample) -> str:
        payload = {
            "tenant_id": example.tenant_id,
            "source_event_id": example.source_event_id,
            "run_id": example.run_id,
            "document_id": example.document_id,
            "field_path": example.field_path,
            "schema_version": example.schema_version,
            "catalog_version": example.catalog_version,
            "label_type": example.label_type.value,
            "reviewer_id": example.reviewer_id,
            "model_value": example.model_value,
            "reviewed_value": example.reviewed_value,
            "correction_reason": example.correction_reason,
            "evidence_reference": cls._evidence_payload(example.evidence_reference),
        }
        return sha256(cls._canonical(payload).encode("utf-8")).hexdigest()

    @classmethod
    def _feedback_payload(cls, row: ExampleFeedbackRow) -> str:
        return cls._canonical(
            {
                "tenant_id": row.tenant_id,
                "replay_key": row.replay_key,
                "semantic_fingerprint": row.semantic_fingerprint,
                "run_id": row.run_id,
                "document_id": row.document_id,
                "field_path": row.field_path,
                "schema_version": row.schema_version,
                "label_type": row.label_type,
                "reviewer_id": row.reviewer_id,
                "reason": row.reason,
                "source_event_id": row.source_event_id,
                "evidence_reference_json": row.evidence_reference_json,
                "model_version_id": row.model_version_id,
                "prompt_version_id": row.prompt_version_id,
            }
        )

    @staticmethod
    def _index_definition(
        row: IndexVersionRow,
    ) -> tuple[str, str, str, str, str | None, str | None, str]:
        return (
            row.tenant_id,
            row.version,
            row.schema_version,
            row.dense_model_version_id,
            row.sparse_model_version_id,
            row.rerank_model_version_id,
            row.prompt_version_id,
        )

    def _insert_do_nothing(
        self,
        session: Session,
        model: type[Any],
        values: dict[str, Any],
    ) -> bool:
        if self._dialect_name == "postgresql":
            statement = pg_insert(model).values(**values).on_conflict_do_nothing()
        elif self._dialect_name == "sqlite":
            statement = sqlite_insert(model).values(**values).on_conflict_do_nothing()
        else:
            raise WorkflowPersistenceError("Unsupported business database dialect")
        result = session.execute(statement)
        return result.rowcount == 1

    @staticmethod
    def _version_id(kind: str, tenant_id: str, version: str) -> str:
        return sha256(f"{kind}\0{tenant_id}\0{version}".encode()).hexdigest()

    @staticmethod
    def _projection_id(tenant_id: str, example_id: str, index_version_id: str) -> str:
        value = f"projection\0{tenant_id}\0{example_id}\0{index_version_id}"
        return sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _require_audit_scope(
        event: GovernanceAuditEvent,
        action: GovernanceAction,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        resource_version: str | None,
    ) -> None:
        if (
            event.action is not action
            or event.tenant_id != tenant_id
            or event.resource_type != resource_type
            or event.resource_id != resource_id
            or event.resource_version != resource_version
        ):
            raise WorkflowPersistenceError("Governance audit scope is inconsistent")

    @staticmethod
    def _require_text(name: str, value: str) -> None:
        if not value.strip():
            raise ValueError(f"{name} must not be empty")

    @staticmethod
    def _optional_string(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise WorkflowPersistenceError(f"Persisted evidence {key} is invalid")
        return value

    @staticmethod
    def _string_tuple(payload: dict[str, Any], key: str) -> tuple[str, ...]:
        value = payload.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise WorkflowPersistenceError(f"Persisted evidence {key} is invalid")
        return tuple(value)

    @staticmethod
    def _optional_bool(payload: dict[str, Any], key: str) -> bool:
        value = payload.get(key, False)
        if not isinstance(value, bool):
            raise WorkflowPersistenceError(f"Persisted evidence {key} is invalid")
        return value

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# Explicit boundary name for composition roots and future adapter discovery.
SQLAlchemyReviewedExampleRepository = SQLAlchemyExampleRepository
