"""SQLAlchemy 2.x implementation of all business persistence ports."""

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from secrets import token_hex
from typing import TypeVar, cast
from uuid import uuid4

from sqlalchemy import Engine, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import (
    IdempotencyConflictError,
    WorkflowPersistenceError,
)
from invoice_intelligence.application.ports.business_persistence import (
    ExtractionResultRecord,
    ExtractionRunRecord,
    IdempotencyRecord,
    IdempotencyStatus,
    ReviewTaskRecord,
)
from invoice_intelligence.application.ports.memory import (
    MemoryRecoveryRecord,
    MemoryRecoveryStatus,
    MemoryRecoveryWorkLease,
    StoredCorrectionEvent,
)
from invoice_intelligence.application.ports.workflow import ExtractionStateCodec
from invoice_intelligence.domain.document import DocumentReference
from invoice_intelligence.domain.extraction import ExtractionResult
from invoice_intelligence.domain.field_semantics import FieldBindingReviewDecision
from invoice_intelligence.domain.json_types import JsonValue
from invoice_intelligence.domain.review_tasks import ReviewTaskStatus
from invoice_intelligence.domain.workflow import (
    CorrectionEvent,
    FieldReviewDecision,
    HumanCorrection,
    HumanReviewAction,
    ReviewRequest,
    ValidationRoute,
    WorkflowIdentity,
    WorkflowStatus,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    CorrectionEventRow,
    DocumentRow,
    ExtractionResultRow,
    ExtractionRunRow,
    HumanCorrectionRow,
    IdempotencyRequestRow,
    MemoryReviewRecoveryRow,
    ReviewTaskAuditRow,
    ReviewTaskRow,
    StoredObjectRow,
)
from invoice_intelligence.infrastructure.serialization.pydantic import (
    field_binding_evidence_to_payload,
)

InvoiceT = TypeVar("InvoiceT")


class SQLAlchemyBusinessRepository:
    """Portable business repository; schema lifecycle is owned by Alembic."""

    def __init__(
        self,
        engine: Engine,
        extraction_codec: ExtractionStateCodec,
    ) -> None:
        self._engine = engine
        self._dialect_name = engine.dialect.name
        self._extraction_codec = extraction_codec
        self._sessions = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        )

    def close(self) -> None:
        self._engine.dispose()

    async def save_document(self, document: DocumentReference, tenant_id: str) -> None:
        await asyncio.to_thread(self._save_document_sync, document, tenant_id)

    async def get_document(
        self,
        document_id: str,
        tenant_id: str,
    ) -> DocumentReference | None:
        return await asyncio.to_thread(self._get_document_sync, document_id, tenant_id)

    async def upsert_status(
        self,
        identity: WorkflowIdentity,
        status: WorkflowStatus,
        tenant_id: str,
        failure_message: str | None = None,
        validation_route: ValidationRoute | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._upsert_status_sync,
            identity,
            status,
            tenant_id,
            failure_message,
            validation_route,
        )

    async def get_run(self, run_id: str, tenant_id: str) -> ExtractionRunRecord | None:
        return await asyncio.to_thread(self._get_run_sync, run_id, tenant_id)

    async def upsert_result(
        self,
        identity: WorkflowIdentity,
        result: ExtractionResult[InvoiceT],
    ) -> None:
        await asyncio.to_thread(self._upsert_result_sync, identity, result)

    async def get_result(
        self,
        run_id: str,
        tenant_id: str,
    ) -> ExtractionResultRecord | None:
        return await asyncio.to_thread(self._get_result_sync, run_id, tenant_id)

    async def upsert_pending_review(
        self,
        identity: WorkflowIdentity,
        request: ReviewRequest,
    ) -> None:
        await asyncio.to_thread(self._upsert_pending_review_sync, identity, request)

    async def resolve_review(
        self,
        identity: WorkflowIdentity,
        tenant_id: str,
        reviewer_id: str,
        trace_id: str | None,
    ) -> None:
        await asyncio.to_thread(
            self._resolve_review_sync,
            identity,
            tenant_id,
            reviewer_id,
            trace_id,
        )

    async def get_review(
        self,
        run_id: str,
        tenant_id: str,
    ) -> ReviewTaskRecord | None:
        return await asyncio.to_thread(self._get_review_sync, run_id, tenant_id)

    async def save_human_correction(
        self,
        identity: WorkflowIdentity,
        correction: HumanCorrection,
    ) -> None:
        await asyncio.to_thread(
            self._save_human_correction_sync,
            identity,
            correction,
        )

    async def save_correction_events(
        self,
        tenant_id: str,
        identity: WorkflowIdentity,
        document: DocumentReference,
        events: tuple[CorrectionEvent, ...],
    ) -> tuple[StoredCorrectionEvent, ...]:
        if identity.document_id != document.document_id:
            raise WorkflowPersistenceError(
                "Correction-memory document_id does not match workflow identity"
            )
        if not tenant_id.strip() or tenant_id != tenant_id.strip():
            raise WorkflowPersistenceError("tenant_id must be non-empty and normalized")
        return await asyncio.to_thread(
            self._save_correction_events_sync,
            tenant_id,
            identity,
            document,
            events,
        )

    async def get_correction_event(
        self,
        tenant_id: str,
        event_id: str,
        *,
        run_id: str,
        document_id: str,
    ) -> StoredCorrectionEvent | None:
        return await asyncio.to_thread(
            self._get_correction_event_sync,
            tenant_id,
            event_id,
            run_id,
            document_id,
        )

    async def save_review_facts(
        self,
        *,
        identity: WorkflowIdentity,
        tenant_id: str,
        document: DocumentReference,
        original_result: ExtractionResult[InvoiceT],
        reviewed_result: ExtractionResult[InvoiceT],
        review: HumanCorrection,
        correction_events: tuple[CorrectionEvent, ...],
    ) -> MemoryRecoveryRecord:
        return await asyncio.to_thread(
            self._save_review_facts_sync,
            identity,
            tenant_id,
            document,
            original_result,
            reviewed_result,
            review,
            correction_events,
        )

    async def get_memory_recovery(
        self,
        tenant_id: str,
        recovery_id: str,
    ) -> MemoryRecoveryRecord | None:
        return await asyncio.to_thread(
            self._get_memory_recovery_sync,
            tenant_id,
            recovery_id,
        )

    async def claim_memory_recovery(
        self,
        tenant_id: str,
        recovery_id: str,
        worker_id: str,
        *,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MemoryRecoveryWorkLease | None:
        return await asyncio.to_thread(
            self._claim_memory_recoveries_sync,
            worker_id,
            now,
            lease_expires_at,
            1,
            tenant_id,
            recovery_id,
        )

    async def claim_due_memory_recoveries(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[MemoryRecoveryWorkLease, ...]:
        return await asyncio.to_thread(
            self._claim_memory_recoveries_sync,
            worker_id,
            now,
            lease_expires_at,
            limit,
            None,
            None,
        )

    async def complete_memory_recovery(
        self,
        lease: MemoryRecoveryWorkLease,
        *,
        example_ids: tuple[str, ...],
        completed_at: datetime,
    ) -> bool:
        return await asyncio.to_thread(
            self._complete_memory_recovery_sync,
            lease,
            example_ids,
            completed_at,
        )

    async def retry_memory_recovery(
        self,
        lease: MemoryRecoveryWorkLease,
        *,
        next_attempt_at: datetime | None,
        error_code: str,
        error_at: datetime,
    ) -> bool:
        return await asyncio.to_thread(
            self._retry_memory_recovery_sync,
            lease,
            next_attempt_at,
            error_code,
            error_at,
        )

    async def get_idempotency(
        self,
        operation: str,
        key: str,
    ) -> IdempotencyRecord | None:
        return await asyncio.to_thread(self._get_idempotency_sync, operation, key)

    async def claim_idempotency(
        self,
        operation: str,
        key: str,
        request_hash: str,
        resource_id: str,
    ) -> IdempotencyRecord:
        return await asyncio.to_thread(
            self._claim_idempotency_sync,
            operation,
            key,
            request_hash,
            resource_id,
        )

    async def complete_idempotency(
        self,
        operation: str,
        key: str,
        request_hash: str,
        response_payload: dict[str, JsonValue],
    ) -> None:
        await asyncio.to_thread(
            self._complete_idempotency_sync,
            operation,
            key,
            request_hash,
            response_payload,
        )

    async def release_idempotency(
        self,
        operation: str,
        key: str,
        request_hash: str,
        resource_id: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._release_idempotency_sync,
            operation,
            key,
            request_hash,
            resource_id,
        )

    def _save_document_sync(self, document: DocumentReference, tenant_id: str) -> None:
        try:
            with self._sessions.begin() as session:
                existing = session.get(DocumentRow, document.document_id)
                if existing is None:
                    now = self._now()
                    session.add(
                        StoredObjectRow(
                            object_id=document.document_id,
                            tenant_id=tenant_id,
                            parent_document_id=None,
                            object_kind="original",
                            stable_key=document.storage_uri,
                            storage_uri=document.storage_uri,
                            checksum=document.checksum,
                            media_type=document.mime_type,
                            size_bytes=document.size_bytes,
                            status=document.storage_status,
                            revision=1,
                            retention_until=document.retention_until,
                            attempt_count=0,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    session.add(
                        DocumentRow(
                            document_id=document.document_id,
                            tenant_id=tenant_id,
                            storage_uri=document.storage_uri,
                            mime_type=document.mime_type,
                            checksum=document.checksum,
                            original_object_id=document.document_id,
                            size_bytes=document.size_bytes,
                            storage_status=document.storage_status,
                            created_at=now,
                        )
                    )
                    return
                if (
                    existing.tenant_id != tenant_id
                    or existing.storage_uri != document.storage_uri
                    or existing.mime_type != document.mime_type
                    or existing.checksum != document.checksum
                ):
                    raise WorkflowPersistenceError(
                        "document_id is already bound to different immutable metadata"
                    )
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to persist document reference") from exc

    def _get_document_sync(
        self,
        document_id: str,
        tenant_id: str,
    ) -> DocumentReference | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(DocumentRow).where(
                        DocumentRow.document_id == document_id,
                        DocumentRow.tenant_id == tenant_id,
                    )
                )
                if row is None:
                    return None
                return DocumentReference(
                    document_id=row.document_id,
                    storage_uri=row.storage_uri,
                    mime_type=row.mime_type,
                    checksum=row.checksum,
                    size_bytes=row.size_bytes,
                    storage_status=row.storage_status,
                )
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to retrieve document reference") from exc

    def _upsert_status_sync(
        self,
        identity: WorkflowIdentity,
        status: WorkflowStatus,
        tenant_id: str,
        failure_message: str | None,
        validation_route: ValidationRoute | None,
    ) -> None:
        try:
            with self._sessions.begin() as session:
                row = session.get(ExtractionRunRow, identity.run_id)
                now = self._now()
                if row is None:
                    document = session.scalar(
                        select(DocumentRow).where(
                            DocumentRow.document_id == identity.document_id,
                            DocumentRow.tenant_id == tenant_id,
                        )
                    )
                    if document is None:
                        raise WorkflowPersistenceError(
                            "Workflow document is outside the tenant scope"
                        )
                    row = ExtractionRunRow(
                        run_id=identity.run_id,
                        tenant_id=tenant_id,
                        thread_id=identity.thread_id,
                        document_id=identity.document_id,
                        status=status.value,
                        validation_route=(
                            validation_route.value if validation_route is not None else None
                        ),
                        failure_message=failure_message,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    return
                if (
                    row.tenant_id != tenant_id
                    or row.thread_id != identity.thread_id
                    or row.document_id != identity.document_id
                ):
                    raise WorkflowPersistenceError(
                        "run_id is already bound to a different workflow identity"
                    )
                if row.status in {
                    WorkflowStatus.COMPLETED.value,
                    WorkflowStatus.FAILED.value,
                }:
                    return
                row.status = status.value
                row.failure_message = failure_message
                if validation_route is not None:
                    row.validation_route = validation_route.value
                row.updated_at = now
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to persist extraction run") from exc

    def _get_run_sync(self, run_id: str, tenant_id: str) -> ExtractionRunRecord | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(ExtractionRunRow).where(
                        ExtractionRunRow.run_id == run_id,
                        ExtractionRunRow.tenant_id == tenant_id,
                    )
                )
                if row is None:
                    return None
                recovery = session.scalar(
                    select(MemoryReviewRecoveryRow)
                    .where(
                        MemoryReviewRecoveryRow.tenant_id == tenant_id,
                        MemoryReviewRecoveryRow.run_id == run_id,
                    )
                    .order_by(MemoryReviewRecoveryRow.created_at.desc())
                    .limit(1)
                )
                return ExtractionRunRecord(
                    identity=WorkflowIdentity(
                        thread_id=row.thread_id,
                        run_id=row.run_id,
                        document_id=row.document_id,
                    ),
                    status=WorkflowStatus(row.status),
                    validation_route=(
                        ValidationRoute(row.validation_route)
                        if row.validation_route is not None
                        else None
                    ),
                    failure_message=row.failure_message,
                    created_at=self._aware(row.created_at),
                    updated_at=self._aware(row.updated_at),
                    memory_status=(
                        MemoryRecoveryStatus(recovery.status) if recovery is not None else None
                    ),
                    memory_trace_id=(recovery.trace_id if recovery is not None else None),
                    memory_error_code=(recovery.last_error_code if recovery is not None else None),
                )
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to retrieve extraction run") from exc

    def _upsert_result_sync(
        self,
        identity: WorkflowIdentity,
        result: ExtractionResult[InvoiceT],
    ) -> None:
        payload = self._extraction_codec.dump(result)
        try:
            with self._sessions.begin() as session:
                row = session.get(ExtractionResultRow, identity.run_id)
                if row is None:
                    session.add(
                        ExtractionResultRow(
                            run_id=identity.run_id,
                            document_id=identity.document_id,
                            result_json=payload,
                            created_at=self._now(),
                        )
                    )
                    return
                if row.document_id != identity.document_id or self._canonical(
                    row.result_json
                ) != self._canonical(payload):
                    raise WorkflowPersistenceError(
                        "Final result run_id is bound to different immutable data"
                    )
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to persist extraction result") from exc

    def _get_result_sync(
        self,
        run_id: str,
        tenant_id: str,
    ) -> ExtractionResultRecord | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(ExtractionResultRow)
                    .join(
                        ExtractionRunRow,
                        ExtractionRunRow.run_id == ExtractionResultRow.run_id,
                    )
                    .where(
                        ExtractionResultRow.run_id == run_id,
                        ExtractionRunRow.tenant_id == tenant_id,
                    )
                )
                if row is None:
                    return None
                return ExtractionResultRecord(
                    run_id=row.run_id,
                    document_id=row.document_id,
                    payload=self._json_object(row.result_json, "extraction result"),
                    created_at=self._aware(row.created_at),
                )
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to retrieve extraction result") from exc

    def _upsert_pending_review_sync(
        self,
        identity: WorkflowIdentity,
        request: ReviewRequest,
    ) -> None:
        payload = self._review_payload(request)
        review_id = sha256(f"review:{identity.run_id}".encode()).hexdigest()
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(ReviewTaskRow).where(ReviewTaskRow.run_id == identity.run_id)
                )
                now = self._now()
                if row is None:
                    run = session.get(ExtractionRunRow, identity.run_id)
                    if run is None:
                        raise WorkflowPersistenceError("Extraction run was not found")
                    session.add(
                        ReviewTaskRow(
                            review_id=review_id,
                            tenant_id=run.tenant_id,
                            run_id=identity.run_id,
                            status=ReviewTaskStatus.PENDING_REVIEW.value,
                            request_json=payload,
                            version=1,
                            priority=50,
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
                    return
                changed = self._canonical(row.request_json) != self._canonical(payload)
                reopened = row.status != ReviewTaskStatus.PENDING_REVIEW.value
                if changed or reopened:
                    row.request_json = payload
                    row.version += 1
                    row.revision += 1
                row.status = ReviewTaskStatus.PENDING_REVIEW.value
                row.assigned_reviewer_id = None
                row.lease_token = None
                row.lease_expires_at = None
                row.resolved_at = None
                row.submitted_at = None
                row.cancelled_at = None
                row.cancel_reason = None
                row.updated_at = now
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to persist review task") from exc

    def _resolve_review_sync(
        self,
        identity: WorkflowIdentity,
        tenant_id: str,
        reviewer_id: str,
        trace_id: str | None,
    ) -> None:
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(ReviewTaskRow).where(
                        ReviewTaskRow.run_id == identity.run_id,
                        ReviewTaskRow.tenant_id == tenant_id,
                    )
                )
                if row is None:
                    raise WorkflowPersistenceError("Pending review task was not found")
                if row.status == ReviewTaskStatus.SUBMITTED.value:
                    return
                if (
                    row.status != ReviewTaskStatus.CLAIMED.value
                    or row.assigned_reviewer_id != reviewer_id
                ):
                    raise WorkflowPersistenceError(
                        "Review task is not claimed by the submitting reviewer"
                    )
                now = self._now()
                from_status = row.status
                row.status = ReviewTaskStatus.SUBMITTED.value
                row.lease_token = None
                row.lease_expires_at = None
                row.revision += 1
                row.updated_at = now
                row.resolved_at = now
                row.submitted_at = now
                session.add(
                    ReviewTaskAuditRow(
                        audit_id=uuid4().hex,
                        tenant_id=tenant_id,
                        review_id=row.review_id,
                        action="submit",
                        actor_id=reviewer_id,
                        target_reviewer_id=reviewer_id,
                        from_status=from_status,
                        to_status=ReviewTaskStatus.SUBMITTED.value,
                        revision=row.revision,
                        reason_code="review_facts_persisted",
                        trace_id=trace_id,
                        created_at=now,
                    )
                )
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to resolve review task") from exc

    def _get_review_sync(
        self,
        run_id: str,
        tenant_id: str,
    ) -> ReviewTaskRecord | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(ReviewTaskRow)
                    .join(
                        ExtractionRunRow,
                        ExtractionRunRow.run_id == ReviewTaskRow.run_id,
                    )
                    .where(
                        ReviewTaskRow.run_id == run_id,
                        ExtractionRunRow.tenant_id == tenant_id,
                    )
                )
                if row is None:
                    return None
                return ReviewTaskRecord(
                    run_id=row.run_id,
                    status=ReviewTaskStatus(row.status),
                    request_payload=self._json_object(row.request_json, "review request"),
                    version=row.version,
                    created_at=self._aware(row.created_at),
                    updated_at=self._aware(row.updated_at),
                    resolved_at=(
                        self._aware(row.resolved_at) if row.resolved_at is not None else None
                    ),
                )
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to retrieve review task") from exc

    def _save_human_correction_sync(
        self,
        identity: WorkflowIdentity,
        correction: HumanCorrection,
    ) -> None:
        payload = self._human_correction_payload(correction)
        correction_id = sha256(
            f"{identity.run_id}\0{self._canonical(payload)}".encode()
        ).hexdigest()
        try:
            with self._sessions.begin() as session:
                row = session.get(HumanCorrectionRow, correction_id)
                if row is None:
                    session.add(
                        HumanCorrectionRow(
                            correction_id=correction_id,
                            run_id=identity.run_id,
                            correction_json=payload,
                            created_at=self._now(),
                        )
                    )
                    return
                if row.run_id != identity.run_id or self._canonical(
                    row.correction_json
                ) != self._canonical(payload):
                    raise WorkflowPersistenceError(
                        "correction_id is bound to different immutable data"
                    )
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to persist human correction") from exc

    def _save_correction_events_sync(
        self,
        tenant_id: str,
        identity: WorkflowIdentity,
        document: DocumentReference,
        events: tuple[CorrectionEvent, ...],
    ) -> tuple[StoredCorrectionEvent, ...]:
        if not events:
            return ()
        stored: list[StoredCorrectionEvent] = []
        try:
            with self._sessions.begin() as session:
                run = session.get(ExtractionRunRow, identity.run_id)
                persisted_document = session.get(DocumentRow, identity.document_id)
                if (
                    run is None
                    or persisted_document is None
                    or run.tenant_id != tenant_id
                    or persisted_document.tenant_id != tenant_id
                    or run.thread_id != identity.thread_id
                    or run.document_id != identity.document_id
                    or persisted_document.storage_uri != document.storage_uri
                    or persisted_document.mime_type != document.mime_type
                    or persisted_document.checksum != document.checksum
                ):
                    raise WorkflowPersistenceError(
                        "Correction events are outside the workflow tenant scope"
                    )
                for event in events:
                    payload = {
                        "run_id": identity.run_id,
                        "document_id": identity.document_id,
                        "event": self._correction_event_identity_payload(event),
                    }
                    event_id = sha256(self._canonical(payload).encode("utf-8")).hexdigest()
                    row = session.get(CorrectionEventRow, event_id)
                    if row is None:
                        values = dict(
                            event_id=event_id,
                            run_id=identity.run_id,
                            document_id=identity.document_id,
                            document_checksum=document.checksum,
                            document_type=event.document_type,
                            field_path=event.field_path,
                            model_value_json=event.model_value,
                            corrected_value_json=event.corrected_value,
                            correction_reason=event.correction_reason,
                            vendor_features_json=dict(event.vendor_features),
                            template_features_json=dict(event.template_features),
                            document_reference=event.document_reference,
                            image_reference=event.image_reference,
                            schema_version=event.schema_version,
                            is_reviewed=event.is_reviewed,
                            is_valid=event.is_valid,
                            created_at=event.created_at,
                        )
                        if self._dialect_name == "postgresql":
                            statement = pg_insert(CorrectionEventRow).values(**values)
                        elif self._dialect_name == "sqlite":
                            statement = sqlite_insert(CorrectionEventRow).values(**values)
                        else:
                            raise WorkflowPersistenceError("Unsupported business database dialect")
                        session.execute(
                            statement.on_conflict_do_nothing(index_elements=["event_id"])
                        )
                        row = session.get(CorrectionEventRow, event_id)
                        if row is None:
                            raise WorkflowPersistenceError(
                                "Correction event upsert did not produce a durable row"
                            )
                    persisted = self._correction_event_from_row(row)
                    persisted_payload = {
                        "run_id": row.run_id,
                        "document_id": row.document_id,
                        "event": self._correction_event_identity_payload(persisted),
                    }
                    if self._canonical(persisted_payload) != self._canonical(payload):
                        raise WorkflowPersistenceError(
                            "event_id is bound to different immutable correction data"
                        )
                    stored.append(StoredCorrectionEvent(event_id=event_id, event=persisted))
            return tuple(stored)
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to persist correction events") from exc

    def _get_correction_event_sync(
        self,
        tenant_id: str,
        event_id: str,
        run_id: str,
        document_id: str,
    ) -> StoredCorrectionEvent | None:
        if not tenant_id.strip() or tenant_id != tenant_id.strip():
            raise WorkflowPersistenceError("tenant_id must be non-empty and normalized")
        if not event_id.strip() or event_id != event_id.strip():
            raise WorkflowPersistenceError("event_id must be non-empty and normalized")
        if not run_id.strip() or run_id != run_id.strip():
            raise WorkflowPersistenceError("run_id must be non-empty and normalized")
        if not document_id.strip() or document_id != document_id.strip():
            raise WorkflowPersistenceError("document_id must be non-empty and normalized")
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(CorrectionEventRow)
                    .join(
                        ExtractionRunRow,
                        ExtractionRunRow.run_id == CorrectionEventRow.run_id,
                    )
                    .join(
                        DocumentRow,
                        DocumentRow.document_id == CorrectionEventRow.document_id,
                    )
                    .where(
                        CorrectionEventRow.event_id == event_id,
                        CorrectionEventRow.run_id == run_id,
                        CorrectionEventRow.document_id == document_id,
                        ExtractionRunRow.tenant_id == tenant_id,
                        DocumentRow.tenant_id == tenant_id,
                        ExtractionRunRow.document_id == CorrectionEventRow.document_id,
                    )
                )
                if row is None:
                    return None
                return StoredCorrectionEvent(
                    event_id=row.event_id,
                    event=self._correction_event_from_row(row),
                )
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to read correction event") from exc

    def _save_review_facts_sync(
        self,
        identity: WorkflowIdentity,
        tenant_id: str,
        document: DocumentReference,
        original_result: ExtractionResult[InvoiceT],
        reviewed_result: ExtractionResult[InvoiceT],
        review: HumanCorrection,
        correction_events: tuple[CorrectionEvent, ...],
    ) -> MemoryRecoveryRecord:
        if identity.document_id != document.document_id:
            raise WorkflowPersistenceError(
                "Review-fact document_id does not match workflow identity"
            )
        if not tenant_id.strip() or tenant_id != tenant_id.strip():
            raise WorkflowPersistenceError("tenant_id must be non-empty and normalized")
        review_payload = self._human_correction_payload(review)
        correction_id = sha256(
            f"{identity.run_id}\0{self._canonical(review_payload)}".encode()
        ).hexdigest()
        recovery_id = sha256(
            f"memory-review-recovery\0{tenant_id}\0{identity.run_id}\0{correction_id}".encode()
        ).hexdigest()
        trace_digest = sha256(f"{recovery_id}\0trace".encode()).hexdigest()
        trace_id = f"mem_{trace_digest[:32]}"
        original_payload = self._extraction_codec.dump(original_result)
        reviewed_payload = self._extraction_codec.dump(reviewed_result)
        now = self._now()
        try:
            with self._sessions.begin() as session:
                run = session.get(ExtractionRunRow, identity.run_id)
                persisted_document = session.get(DocumentRow, identity.document_id)
                if (
                    run is None
                    or persisted_document is None
                    or run.tenant_id != tenant_id
                    or persisted_document.tenant_id != tenant_id
                    or run.thread_id != identity.thread_id
                    or run.document_id != identity.document_id
                    or persisted_document.storage_uri != document.storage_uri
                    or persisted_document.mime_type != document.mime_type
                    or persisted_document.checksum != document.checksum
                ):
                    raise WorkflowPersistenceError(
                        "Review facts are outside the workflow tenant scope"
                    )

                correction_row = session.get(HumanCorrectionRow, correction_id)
                if correction_row is None:
                    session.add(
                        HumanCorrectionRow(
                            correction_id=correction_id,
                            run_id=identity.run_id,
                            correction_json=review_payload,
                            created_at=now,
                        )
                    )
                    session.flush()
                elif correction_row.run_id != identity.run_id or self._canonical(
                    correction_row.correction_json
                ) != self._canonical(review_payload):
                    raise WorkflowPersistenceError(
                        "correction_id is bound to different immutable data"
                    )

                stored_events = self._store_correction_events_in_session(
                    session,
                    tenant_id,
                    identity,
                    document,
                    correction_events,
                )
                event_ids = [item.event_id for item in stored_events]
                row = session.get(MemoryReviewRecoveryRow, recovery_id)
                immutable_payload = {
                    "tenant_id": tenant_id,
                    "run_id": identity.run_id,
                    "document_id": identity.document_id,
                    "correction_id": correction_id,
                    "trace_id": trace_id,
                    "correction_event_ids": event_ids,
                    "original_result": original_payload,
                    "reviewed_result": reviewed_payload,
                }
                if row is None:
                    row = MemoryReviewRecoveryRow(
                        recovery_id=recovery_id,
                        tenant_id=tenant_id,
                        run_id=identity.run_id,
                        document_id=identity.document_id,
                        correction_id=correction_id,
                        trace_id=trace_id,
                        status=MemoryRecoveryStatus.PENDING.value,
                        correction_event_ids_json=event_ids,
                        original_result_json=original_payload,
                        reviewed_result_json=reviewed_payload,
                        example_ids_json=[],
                        worker_id=None,
                        lease_token=None,
                        lease_expires_at=None,
                        attempt_count=0,
                        next_attempt_at=now,
                        last_error_code=None,
                        last_error_at=None,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                    )
                    session.add(row)
                    session.flush()
                elif self._canonical(
                    self._memory_recovery_immutable_payload(row)
                ) != self._canonical(immutable_payload):
                    raise WorkflowPersistenceError(
                        "memory recovery_id is bound to different immutable review facts"
                    )
                return self._memory_recovery_from_row(
                    session,
                    row,
                    stored_events=stored_events,
                )
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to persist review facts") from exc

    def _get_memory_recovery_sync(
        self,
        tenant_id: str,
        recovery_id: str,
    ) -> MemoryRecoveryRecord | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(MemoryReviewRecoveryRow).where(
                        MemoryReviewRecoveryRow.tenant_id == tenant_id,
                        MemoryReviewRecoveryRow.recovery_id == recovery_id,
                    )
                )
                return self._memory_recovery_from_row(session, row) if row is not None else None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read memory recovery") from exc

    def _claim_memory_recoveries_sync(
        self,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
        tenant_id: str | None,
        recovery_id: str | None,
    ) -> tuple[MemoryRecoveryWorkLease, ...] | MemoryRecoveryWorkLease | None:
        if limit <= 0 or not worker_id.strip() or lease_expires_at <= now:
            raise WorkflowPersistenceError("Invalid memory recovery claim")
        try:
            with self._sessions.begin() as session:
                statement = (
                    select(MemoryReviewRecoveryRow)
                    .where(
                        MemoryReviewRecoveryRow.status.in_(
                            (
                                MemoryRecoveryStatus.PENDING.value,
                                MemoryRecoveryStatus.FAILED_RETRYABLE.value,
                            )
                        ),
                        MemoryReviewRecoveryRow.next_attempt_at.is_not(None),
                        MemoryReviewRecoveryRow.next_attempt_at <= now,
                        or_(
                            MemoryReviewRecoveryRow.lease_expires_at.is_(None),
                            MemoryReviewRecoveryRow.lease_expires_at <= now,
                        ),
                    )
                    .order_by(
                        MemoryReviewRecoveryRow.next_attempt_at,
                        MemoryReviewRecoveryRow.recovery_id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
                if tenant_id is not None and recovery_id is not None:
                    statement = statement.where(
                        MemoryReviewRecoveryRow.tenant_id == tenant_id,
                        MemoryReviewRecoveryRow.recovery_id == recovery_id,
                    )
                rows = tuple(session.scalars(statement))
                leases: list[MemoryRecoveryWorkLease] = []
                for row in rows:
                    lease_token = token_hex(32)
                    row.worker_id = worker_id
                    row.lease_token = lease_token
                    row.lease_expires_at = lease_expires_at
                    row.attempt_count += 1
                    row.updated_at = now
                    session.flush()
                    leases.append(
                        MemoryRecoveryWorkLease(
                            record=self._memory_recovery_from_row(session, row),
                            worker_id=worker_id,
                            lease_token=lease_token,
                            lease_expires_at=lease_expires_at,
                        )
                    )
                if tenant_id is not None:
                    return leases[0] if leases else None
                return tuple(leases)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to claim memory recovery") from exc

    def _complete_memory_recovery_sync(
        self,
        lease: MemoryRecoveryWorkLease,
        example_ids: tuple[str, ...],
        completed_at: datetime,
    ) -> bool:
        try:
            with self._sessions.begin() as session:
                result = session.execute(
                    update(MemoryReviewRecoveryRow)
                    .where(
                        MemoryReviewRecoveryRow.recovery_id == lease.record.recovery_id,
                        MemoryReviewRecoveryRow.tenant_id == lease.record.tenant_id,
                        MemoryReviewRecoveryRow.worker_id == lease.worker_id,
                        MemoryReviewRecoveryRow.lease_token == lease.lease_token,
                    )
                    .values(
                        status=MemoryRecoveryStatus.COMPLETED.value,
                        example_ids_json=list(dict.fromkeys(example_ids)),
                        worker_id=None,
                        lease_token=None,
                        lease_expires_at=None,
                        next_attempt_at=None,
                        last_error_code=None,
                        last_error_at=None,
                        updated_at=completed_at,
                        completed_at=completed_at,
                    )
                )
                return result.rowcount == 1
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to complete memory recovery") from exc

    def _retry_memory_recovery_sync(
        self,
        lease: MemoryRecoveryWorkLease,
        next_attempt_at: datetime | None,
        error_code: str,
        error_at: datetime,
    ) -> bool:
        try:
            with self._sessions.begin() as session:
                result = session.execute(
                    update(MemoryReviewRecoveryRow)
                    .where(
                        MemoryReviewRecoveryRow.recovery_id == lease.record.recovery_id,
                        MemoryReviewRecoveryRow.tenant_id == lease.record.tenant_id,
                        MemoryReviewRecoveryRow.worker_id == lease.worker_id,
                        MemoryReviewRecoveryRow.lease_token == lease.lease_token,
                    )
                    .values(
                        status=MemoryRecoveryStatus.FAILED_RETRYABLE.value,
                        worker_id=None,
                        lease_token=None,
                        lease_expires_at=None,
                        next_attempt_at=next_attempt_at,
                        last_error_code=error_code,
                        last_error_at=error_at,
                        updated_at=error_at,
                    )
                )
                return result.rowcount == 1
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to reschedule memory recovery") from exc

    def _store_correction_events_in_session(
        self,
        session: Session,
        tenant_id: str,
        identity: WorkflowIdentity,
        document: DocumentReference,
        events: tuple[CorrectionEvent, ...],
    ) -> tuple[StoredCorrectionEvent, ...]:
        stored: list[StoredCorrectionEvent] = []
        for event in events:
            payload = {
                "run_id": identity.run_id,
                "document_id": identity.document_id,
                "event": self._correction_event_identity_payload(event),
            }
            event_id = sha256(self._canonical(payload).encode("utf-8")).hexdigest()
            row = session.get(CorrectionEventRow, event_id)
            if row is None:
                values = dict(
                    event_id=event_id,
                    run_id=identity.run_id,
                    document_id=identity.document_id,
                    document_checksum=document.checksum,
                    document_type=event.document_type,
                    field_path=event.field_path,
                    model_value_json=event.model_value,
                    corrected_value_json=event.corrected_value,
                    correction_reason=event.correction_reason,
                    vendor_features_json=dict(event.vendor_features),
                    template_features_json=dict(event.template_features),
                    document_reference=event.document_reference,
                    image_reference=event.image_reference,
                    schema_version=event.schema_version,
                    is_reviewed=event.is_reviewed,
                    is_valid=event.is_valid,
                    created_at=event.created_at,
                )
                if self._dialect_name == "postgresql":
                    statement = pg_insert(CorrectionEventRow).values(**values)
                elif self._dialect_name == "sqlite":
                    statement = sqlite_insert(CorrectionEventRow).values(**values)
                else:
                    raise WorkflowPersistenceError("Unsupported business database dialect")
                session.execute(statement.on_conflict_do_nothing(index_elements=["event_id"]))
                row = session.get(CorrectionEventRow, event_id)
                if row is None:
                    raise WorkflowPersistenceError(
                        "Correction event upsert did not produce a durable row"
                    )
            persisted = self._correction_event_from_row(row)
            persisted_payload = {
                "run_id": row.run_id,
                "document_id": row.document_id,
                "event": self._correction_event_identity_payload(persisted),
            }
            if self._canonical(persisted_payload) != self._canonical(payload):
                raise WorkflowPersistenceError(
                    "event_id is bound to different immutable correction data"
                )
            stored.append(StoredCorrectionEvent(event_id=event_id, event=persisted))
        return tuple(stored)

    def _memory_recovery_from_row(
        self,
        session: Session,
        row: MemoryReviewRecoveryRow,
        *,
        stored_events: tuple[StoredCorrectionEvent, ...] | None = None,
    ) -> MemoryRecoveryRecord:
        run = session.get(ExtractionRunRow, row.run_id)
        document = session.get(DocumentRow, row.document_id)
        correction = session.get(HumanCorrectionRow, row.correction_id)
        if run is None or document is None or correction is None:
            raise WorkflowPersistenceError("Memory recovery source facts are missing")
        event_ids = self._string_list(
            row.correction_event_ids_json,
            "memory recovery correction event IDs",
        )
        if stored_events is None:
            loaded: list[StoredCorrectionEvent] = []
            for event_id in event_ids:
                event_row = session.get(CorrectionEventRow, event_id)
                if event_row is None:
                    raise WorkflowPersistenceError("Memory recovery correction event is missing")
                loaded.append(
                    StoredCorrectionEvent(
                        event_id=event_id,
                        event=self._correction_event_from_row(event_row),
                    )
                )
            stored_events = tuple(loaded)
        return MemoryRecoveryRecord(
            recovery_id=row.recovery_id,
            tenant_id=row.tenant_id,
            identity=WorkflowIdentity(
                thread_id=run.thread_id,
                run_id=run.run_id,
                document_id=run.document_id,
            ),
            document=DocumentReference(
                document_id=document.document_id,
                storage_uri=document.storage_uri,
                mime_type=document.mime_type,
                checksum=document.checksum,
            ),
            trace_id=row.trace_id,
            status=MemoryRecoveryStatus(row.status),
            review=self._human_correction_from_payload(correction.correction_json),
            correction_events=stored_events,
            original_result_payload=self._json_object(
                row.original_result_json,
                "memory recovery original result",
            ),
            reviewed_result_payload=self._json_object(
                row.reviewed_result_json,
                "memory recovery reviewed result",
            ),
            example_ids=tuple(
                self._string_list(row.example_ids_json, "memory recovery example IDs")
            ),
            attempt_count=row.attempt_count,
            next_attempt_at=(
                self._aware(row.next_attempt_at) if row.next_attempt_at is not None else None
            ),
            last_error_code=row.last_error_code,
            last_error_at=(
                self._aware(row.last_error_at) if row.last_error_at is not None else None
            ),
            created_at=self._aware(row.created_at),
            updated_at=self._aware(row.updated_at),
        )

    @staticmethod
    def _memory_recovery_immutable_payload(
        row: MemoryReviewRecoveryRow,
    ) -> dict[str, object]:
        return {
            "tenant_id": row.tenant_id,
            "run_id": row.run_id,
            "document_id": row.document_id,
            "correction_id": row.correction_id,
            "trace_id": row.trace_id,
            "correction_event_ids": row.correction_event_ids_json,
            "original_result": row.original_result_json,
            "reviewed_result": row.reviewed_result_json,
        }

    @staticmethod
    def _string_list(value: object, label: str) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise WorkflowPersistenceError(f"Persisted {label} must be a string list")
        return cast(list[str], value)

    @classmethod
    def _human_correction_from_payload(cls, payload: object) -> HumanCorrection:
        data = cls._json_object(payload, "human correction")
        raw_fields = data.get("fields")
        raw_bindings = data.get("field_bindings", [])
        corrected_invoice = data.get("corrected_invoice")
        reviewer_id = data.get("reviewer_id")
        document_type = data.get("document_type")
        if (
            not isinstance(raw_fields, list)
            or not isinstance(raw_bindings, list)
            or not isinstance(reviewer_id, str)
            or (document_type is not None and not isinstance(document_type, str))
            or (corrected_invoice is not None and not isinstance(corrected_invoice, dict))
        ):
            raise WorkflowPersistenceError("Persisted human correction is invalid")
        fields: list[FieldReviewDecision] = []
        for item in raw_fields:
            if not isinstance(item, dict):
                raise WorkflowPersistenceError("Persisted review decision is invalid")
            field_path = item.get("field_path")
            action = item.get("action")
            reason = item.get("reason")
            if not isinstance(field_path, str) or not isinstance(action, str):
                raise WorkflowPersistenceError("Persisted review decision is invalid")
            fields.append(
                FieldReviewDecision(
                    field_path=field_path,
                    action=HumanReviewAction(action),
                    reason=reason if isinstance(reason, str) else None,
                    rejected_value=cast(JsonValue, item.get("rejected_value")),
                )
            )
        bindings: list[FieldBindingReviewDecision] = []
        for item in raw_bindings:
            if not isinstance(item, dict):
                raise WorkflowPersistenceError("Persisted field binding is invalid")
            evidence_id = item.get("evidence_id")
            field_path = item.get("selected_canonical_field_path")
            reason = item.get("reason")
            if not all(isinstance(value, str) for value in (evidence_id, field_path, reason)):
                raise WorkflowPersistenceError("Persisted field binding is invalid")
            bindings.append(
                FieldBindingReviewDecision(
                    evidence_id=cast(str, evidence_id),
                    selected_canonical_field_path=cast(str, field_path),
                    reason=cast(str, reason),
                )
            )
        return HumanCorrection(
            corrected_invoice=cast(dict[str, JsonValue] | None, corrected_invoice),
            fields=tuple(fields),
            reviewer_id=reviewer_id,
            document_type=cast(str | None, document_type),
            field_bindings=tuple(bindings),
        )

    def _get_idempotency_sync(
        self,
        operation: str,
        key: str,
    ) -> IdempotencyRecord | None:
        try:
            with self._sessions() as session:
                row = self._find_idempotency(session, operation, key)
                return self._idempotency_record(row) if row is not None else None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to retrieve idempotency record") from exc

    def _claim_idempotency_sync(
        self,
        operation: str,
        key: str,
        request_hash: str,
        resource_id: str,
    ) -> IdempotencyRecord:
        try:
            with self._sessions.begin() as session:
                existing = self._find_idempotency(session, operation, key)
                if existing is not None:
                    return self._validate_idempotency(existing, request_hash)
                now = self._now()
                row = IdempotencyRequestRow(
                    operation=operation,
                    idempotency_key=key,
                    request_hash=request_hash,
                    resource_id=resource_id,
                    status=IdempotencyStatus.IN_PROGRESS.value,
                    response_json=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                return self._idempotency_record(row)
        except IdempotencyConflictError:
            raise
        except IntegrityError:
            existing = self._get_idempotency_sync(operation, key)
            if existing is None:
                raise WorkflowPersistenceError(
                    "Idempotency claim conflicted without a durable record"
                )
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError("Idempotency key is bound to a different request")
            return existing
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to claim idempotency key") from exc

    def _complete_idempotency_sync(
        self,
        operation: str,
        key: str,
        request_hash: str,
        response_payload: dict[str, JsonValue],
    ) -> None:
        try:
            with self._sessions.begin() as session:
                row = self._find_idempotency(session, operation, key)
                if row is None:
                    raise WorkflowPersistenceError("Idempotency claim was not found")
                self._validate_idempotency(row, request_hash)
                if row.status == IdempotencyStatus.COMPLETED.value:
                    if self._canonical(row.response_json) != self._canonical(response_payload):
                        raise WorkflowPersistenceError(
                            "Completed idempotency response is immutable"
                        )
                    return
                row.status = IdempotencyStatus.COMPLETED.value
                row.response_json = response_payload
                row.updated_at = self._now()
        except (IdempotencyConflictError, WorkflowPersistenceError):
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to complete idempotency key") from exc

    def _release_idempotency_sync(
        self,
        operation: str,
        key: str,
        request_hash: str,
        resource_id: str,
    ) -> bool:
        try:
            with self._sessions.begin() as session:
                result = session.execute(
                    delete(IdempotencyRequestRow).where(
                        IdempotencyRequestRow.operation == operation,
                        IdempotencyRequestRow.idempotency_key == key,
                        IdempotencyRequestRow.request_hash == request_hash,
                        IdempotencyRequestRow.resource_id == resource_id,
                        IdempotencyRequestRow.status == IdempotencyStatus.IN_PROGRESS.value,
                    )
                )
                return result.rowcount == 1
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to release idempotency key") from exc

    @staticmethod
    def _find_idempotency(
        session: Session,
        operation: str,
        key: str,
    ) -> IdempotencyRequestRow | None:
        return session.scalar(
            select(IdempotencyRequestRow).where(
                IdempotencyRequestRow.operation == operation,
                IdempotencyRequestRow.idempotency_key == key,
            )
        )

    @classmethod
    def _validate_idempotency(
        cls,
        row: IdempotencyRequestRow,
        request_hash: str,
    ) -> IdempotencyRecord:
        if row.request_hash != request_hash:
            raise IdempotencyConflictError("Idempotency key is bound to a different request")
        return cls._idempotency_record(row)

    @staticmethod
    def _idempotency_record(row: IdempotencyRequestRow) -> IdempotencyRecord:
        response = row.response_json
        if response is not None and not isinstance(response, dict):
            raise WorkflowPersistenceError("Idempotency response must be a JSON object")
        return IdempotencyRecord(
            operation=row.operation,
            key=row.idempotency_key,
            request_hash=row.request_hash,
            resource_id=row.resource_id,
            status=IdempotencyStatus(row.status),
            response_payload=cast(dict[str, JsonValue] | None, response),
        )

    @staticmethod
    def _review_payload(request: ReviewRequest) -> dict[str, JsonValue]:
        return {
            "fields": [
                {
                    "field_path": field.field_path,
                    "current_value": field.current_value,
                    "candidate_values": list(field.candidate_values),
                    "triggered_rules": list(field.triggered_rules),
                    "reasons": list(field.reasons),
                    "user_action": field.user_action,
                }
                for field in request.fields
            ],
            "field_bindings": [
                field_binding_evidence_to_payload(item) for item in request.field_bindings
            ],
            "evidence_sources": [
                {
                    "field_path": item.field_path,
                    "source_type": item.source_type,
                    "source_id": item.source_id,
                    "candidate_values": list(item.candidate_values),
                    "page_number": item.page_number,
                    "bounding_box": (
                        list(item.bounding_box) if item.bounding_box is not None else None
                    ),
                    "provider_name": item.provider_name,
                    "provider_version": item.provider_version,
                    "model_version": item.model_version,
                    "provider_score": item.provider_score,
                    "source_reference": item.source_reference,
                    "comparison_outcome": item.comparison_outcome,
                    "reason_codes": list(item.reason_codes),
                }
                for item in request.evidence_sources
            ],
        }

    @staticmethod
    def _human_correction_payload(
        correction: HumanCorrection,
    ) -> dict[str, JsonValue]:
        return {
            "corrected_invoice": (
                dict(correction.corrected_invoice)
                if correction.corrected_invoice is not None
                else None
            ),
            "reviewer_id": correction.reviewer_id,
            "document_type": correction.document_type,
            "fields": [
                {
                    "field_path": field.field_path,
                    "action": field.action.value,
                    "reason": field.reason,
                    "rejected_value": field.rejected_value,
                }
                for field in correction.fields
            ],
            "field_bindings": [
                {
                    "evidence_id": item.evidence_id,
                    "selected_canonical_field_path": (item.selected_canonical_field_path),
                    "reason": item.reason,
                }
                for item in correction.field_bindings
            ],
        }

    @staticmethod
    def _correction_event_identity_payload(
        event: CorrectionEvent,
    ) -> dict[str, JsonValue]:
        return {
            "document_type": event.document_type,
            "field_path": event.field_path,
            "model_value": event.model_value,
            "corrected_value": event.corrected_value,
            "correction_reason": event.correction_reason,
            "vendor_features": dict(event.vendor_features),
            "template_features": dict(event.template_features),
            "document_reference": event.document_reference,
            "image_reference": event.image_reference,
            "schema_version": event.schema_version,
            "is_reviewed": event.is_reviewed,
            "is_valid": event.is_valid,
        }

    @classmethod
    def _correction_event_from_row(cls, row: CorrectionEventRow) -> CorrectionEvent:
        return CorrectionEvent(
            document_type=row.document_type,
            field_path=row.field_path,
            model_value=cast(JsonValue, row.model_value_json),
            corrected_value=cast(JsonValue, row.corrected_value_json),
            correction_reason=row.correction_reason,
            vendor_features=cast(dict[str, JsonValue], row.vendor_features_json),
            template_features=cast(dict[str, JsonValue], row.template_features_json),
            document_reference=row.document_reference,
            image_reference=row.image_reference,
            schema_version=row.schema_version,
            created_at=cls._aware(row.created_at),
            is_reviewed=row.is_reviewed,
            is_valid=row.is_valid,
        )

    @staticmethod
    def _json_object(value: object, label: str) -> dict[str, JsonValue]:
        if not isinstance(value, dict):
            raise WorkflowPersistenceError(f"Persisted {label} must be a JSON object")
        return cast(dict[str, JsonValue], value)

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
