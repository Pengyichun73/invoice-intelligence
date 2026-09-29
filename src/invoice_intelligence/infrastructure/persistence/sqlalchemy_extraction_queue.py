"""PostgreSQL queue for invoice workflow execution."""

import asyncio
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Engine, and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import (
    IdempotencyConflictError,
    ResourceConflictError,
    ResourceNotFoundError,
    WorkflowPersistenceError,
)
from invoice_intelligence.application.ports.business_persistence import ExtractionRunRecord
from invoice_intelligence.application.ports.extraction_queue import ExtractionWorkLease
from invoice_intelligence.domain.review_tasks import ReviewTaskStatus
from invoice_intelligence.domain.workflow import (
    HumanCorrection,
    ValidationRoute,
    WorkflowIdentity,
    WorkflowStatus,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    DocumentRow,
    ExtractionRunRow,
    ExtractionWorkItemRow,
    ReviewTaskRow,
)
from invoice_intelligence.workflow.state import (
    human_correction_from_state,
    human_correction_to_state,
)


class SQLAlchemyExtractionQueueRepository:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def enqueue_start(
        self,
        identity: WorkflowIdentity,
        tenant_id: str,
        *,
        idempotency_hash: str | None,
        request_hash: str,
        trace_id: str | None,
        actor_id: str | None = None,
    ) -> ExtractionRunRecord:
        return await asyncio.to_thread(
            self._enqueue_start_sync, identity, tenant_id, idempotency_hash, request_hash, trace_id, actor_id
        )

    def _enqueue_start_sync(
        self,
        identity: WorkflowIdentity,
        tenant_id: str,
        idempotency_hash: str | None,
        request_hash: str,
        trace_id: str | None,
        actor_id: str | None,
    ) -> ExtractionRunRecord:
        try:
            with self._sessions.begin() as session:
                if idempotency_hash is not None:
                    existing = session.scalar(
                        select(ExtractionWorkItemRow).where(
                            ExtractionWorkItemRow.tenant_id == tenant_id,
                            ExtractionWorkItemRow.kind == "start",
                            ExtractionWorkItemRow.idempotency_hash == idempotency_hash,
                        )
                    )
                    if existing is not None:
                        if existing.request_hash != request_hash:
                            raise IdempotencyConflictError(
                                "Extraction key belongs to another request"
                            )
                        run = session.get(ExtractionRunRow, existing.run_id)
                        if run is None:
                            raise WorkflowPersistenceError("Queued extraction run is missing")
                        if actor_id is not None and run.created_by != actor_id:
                            raise IdempotencyConflictError("Extraction key belongs to another actor")
                        return self._run_record(run)
                document = session.scalar(
                    select(DocumentRow).where(
                        DocumentRow.document_id == identity.document_id,
                        DocumentRow.tenant_id == tenant_id,
                    )
                )
                if document is None:
                    raise ResourceNotFoundError("Document was not found")
                now = datetime.now(UTC)
                run = ExtractionRunRow(
                    run_id=identity.run_id,
                    tenant_id=tenant_id,
                    created_by=actor_id,
                    thread_id=identity.thread_id,
                    document_id=identity.document_id,
                    status=WorkflowStatus.RECEIVED.value,
                    validation_route=None,
                    failure_message=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(run)
                session.add(
                    ExtractionWorkItemRow(
                        task_id=f"extract_{uuid4().hex}",
                        run_id=identity.run_id,
                        tenant_id=tenant_id,
                        kind="start",
                        status="pending",
                        correction_json=None,
                        idempotency_hash=idempotency_hash,
                        request_hash=request_hash,
                        trace_id=trace_id,
                        checkpoint_id=None,
                        worker_id=None,
                        claim_token=None,
                        lease_expires_at=None,
                        attempt_count=0,
                        next_attempt_at=None,
                        last_error_code=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return self._run_record(run)
        except IntegrityError as exc:
            if idempotency_hash is not None:
                with self._sessions() as session:
                    existing = session.scalar(
                        select(ExtractionWorkItemRow).where(
                            ExtractionWorkItemRow.tenant_id == tenant_id,
                            ExtractionWorkItemRow.kind == "start",
                            ExtractionWorkItemRow.idempotency_hash == idempotency_hash,
                        )
                    )
                    if existing is not None:
                        if existing.request_hash != request_hash:
                            raise IdempotencyConflictError(
                                "Extraction key belongs to another request"
                            ) from exc
                        run = session.get(ExtractionRunRow, existing.run_id)
                        if run is not None:
                            if actor_id is not None and run.created_by != actor_id:
                                raise IdempotencyConflictError("Extraction key belongs to another actor") from exc
                            return self._run_record(run)
            raise WorkflowPersistenceError("Unable to enqueue extraction") from exc
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to enqueue extraction") from exc

    async def enqueue_resume(
        self,
        identity: WorkflowIdentity,
        tenant_id: str,
        correction: HumanCorrection,
        *,
        idempotency_hash: str,
        request_hash: str,
        trace_id: str | None,
        checkpoint_id: str,
        review_id: str,
        expected_revision: int,
        lease_token: str,
    ) -> ExtractionRunRecord:
        return await asyncio.to_thread(
            self._enqueue_resume_sync,
            identity, tenant_id, correction, idempotency_hash, request_hash, trace_id,
            checkpoint_id,
            review_id, expected_revision, lease_token,
        )

    async def has_resume(
        self,
        run_id: str,
        tenant_id: str,
        *,
        idempotency_hash: str,
        request_hash: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._has_resume_sync, run_id, tenant_id, idempotency_hash, request_hash
        )

    def _has_resume_sync(
        self,
        run_id: str,
        tenant_id: str,
        idempotency_hash: str,
        request_hash: str,
    ) -> bool:
        try:
            with self._sessions() as session:
                existing = session.scalar(
                    select(ExtractionWorkItemRow).where(
                        ExtractionWorkItemRow.tenant_id == tenant_id,
                        ExtractionWorkItemRow.kind == "resume",
                        ExtractionWorkItemRow.idempotency_hash == idempotency_hash,
                    )
                )
                if existing is None:
                    return False
                if existing.status == "failed":
                    return False
                if (
                    existing.run_id != run_id or existing.request_hash != request_hash
                ):
                    raise ResourceConflictError("Review decision was already submitted")
                return True
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to read queued review") from exc

    def _enqueue_resume_sync(
        self,
        identity: WorkflowIdentity,
        tenant_id: str,
        correction: HumanCorrection,
        idempotency_hash: str,
        request_hash: str,
        trace_id: str | None,
        checkpoint_id: str,
        review_id: str,
        expected_revision: int,
        lease_token: str,
    ) -> ExtractionRunRecord:
        try:
            with self._sessions.begin() as session:
                run = session.scalar(
                    select(ExtractionRunRow).where(
                        ExtractionRunRow.run_id == identity.run_id,
                        ExtractionRunRow.tenant_id == tenant_id,
                    ).with_for_update()
                )
                if run is None:
                    raise ResourceNotFoundError("Extraction run was not found")
                review = session.scalar(
                    select(ReviewTaskRow).where(
                        ReviewTaskRow.review_id == review_id,
                        ReviewTaskRow.run_id == identity.run_id,
                        ReviewTaskRow.tenant_id == tenant_id,
                    ).with_for_update()
                )
                if review is None:
                    raise ResourceNotFoundError("Review task was not found")
                existing = session.scalar(
                    select(ExtractionWorkItemRow).where(
                        ExtractionWorkItemRow.tenant_id == tenant_id,
                        ExtractionWorkItemRow.kind == "resume",
                        ExtractionWorkItemRow.idempotency_hash == idempotency_hash,
                    )
                )
                if existing is not None:
                    if (
                        existing.run_id != identity.run_id
                        or existing.request_hash != request_hash
                    ):
                        raise ResourceConflictError("Review decision was already submitted")
                    if existing.status != "failed":
                        return self._run_record(run)
                    if run.status != WorkflowStatus.PENDING_REVIEW.value:
                        raise ResourceConflictError("Review cannot be retried in current run state")
                    existing.status = "pending"
                    existing.attempt_count = 0
                    existing.next_attempt_at = None
                    existing.last_error_code = None
                    existing.updated_at = datetime.now(UTC)
                    run.status = WorkflowStatus.PROCESSING.value
                    run.updated_at = existing.updated_at
                    return self._run_record(run)
                if run.status != WorkflowStatus.PENDING_REVIEW.value:
                    raise ResourceConflictError("Extraction run is not pending human review")
                now = datetime.now(UTC)
                if (
                    review.revision != expected_revision
                    or review.status != ReviewTaskStatus.CLAIMED.value
                    or review.assigned_reviewer_id != correction.reviewer_id
                    or review.lease_token != lease_token
                    or review.lease_expires_at is None
                    or review.lease_expires_at <= now
                ):
                    raise ResourceConflictError("Review task lease changed")
                active = session.scalar(
                    select(ExtractionWorkItemRow.task_id).where(
                        ExtractionWorkItemRow.run_id == identity.run_id,
                        ExtractionWorkItemRow.kind == "resume",
                        ExtractionWorkItemRow.status.in_(("pending", "leased")),
                    )
                )
                if active is not None:
                    raise ResourceConflictError("Review decision is already processing")
                run.status = WorkflowStatus.PROCESSING.value
                run.updated_at = now
                session.add(
                    ExtractionWorkItemRow(
                        task_id=f"review_{uuid4().hex}",
                        run_id=identity.run_id,
                        tenant_id=tenant_id,
                        kind="resume",
                        status="pending",
                        correction_json=human_correction_to_state(correction),
                        idempotency_hash=idempotency_hash,
                        request_hash=request_hash,
                        trace_id=trace_id,
                        checkpoint_id=checkpoint_id,
                        worker_id=None,
                        claim_token=None,
                        lease_expires_at=None,
                        attempt_count=0,
                        next_attempt_at=None,
                        last_error_code=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return self._run_record(run)
        except IntegrityError as exc:
            with self._sessions() as session:
                existing = session.scalar(
                    select(ExtractionWorkItemRow).where(
                        ExtractionWorkItemRow.tenant_id == tenant_id,
                        ExtractionWorkItemRow.kind == "resume",
                        ExtractionWorkItemRow.idempotency_hash == idempotency_hash,
                    )
                )
                if (
                    existing is not None
                    and existing.run_id == identity.run_id
                    and existing.request_hash == request_hash
                    and existing.status != "failed"
                ):
                    run = session.get(ExtractionRunRow, identity.run_id)
                    if run is not None and run.tenant_id == tenant_id:
                        return self._run_record(run)
            raise ResourceConflictError("Review decision was already submitted") from exc
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to enqueue review resume") from exc

    async def claim(self, worker_id: str, lease_seconds: int) -> ExtractionWorkLease | None:
        return await asyncio.to_thread(self._claim_sync, worker_id, lease_seconds)

    def _claim_sync(self, worker_id: str, lease_seconds: int) -> ExtractionWorkLease | None:
        try:
            with self._sessions.begin() as session:
                now = datetime.now(UTC)
                row = session.scalar(
                    select(ExtractionWorkItemRow)
                    .where(
                        or_(
                            and_(
                                ExtractionWorkItemRow.status == "pending",
                                or_(
                                    ExtractionWorkItemRow.next_attempt_at.is_(None),
                                    ExtractionWorkItemRow.next_attempt_at <= now,
                                ),
                            ),
                            and_(
                                ExtractionWorkItemRow.status == "leased",
                                ExtractionWorkItemRow.lease_expires_at <= now,
                            ),
                        )
                    )
                    .order_by(ExtractionWorkItemRow.created_at, ExtractionWorkItemRow.task_id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if row is None:
                    return None
                run = session.get(ExtractionRunRow, row.run_id)
                if run is None or run.tenant_id != row.tenant_id:
                    raise WorkflowPersistenceError("Queued run ownership is invalid")
                row.status = "leased"
                row.worker_id = worker_id
                row.claim_token = token_hex(32)
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                row.attempt_count += 1
                row.updated_at = now
                correction = (
                    human_correction_from_state(row.correction_json)
                    if row.correction_json is not None else None
                )
                return ExtractionWorkLease(
                    task_id=row.task_id,
                    identity=WorkflowIdentity(
                        thread_id=run.thread_id, run_id=run.run_id, document_id=run.document_id
                    ),
                    tenant_id=row.tenant_id,
                    kind=row.kind,
                    correction=correction,
                    trace_id=row.trace_id,
                    checkpoint_id=row.checkpoint_id,
                    claim_token=row.claim_token,
                    lease_expires_at=row.lease_expires_at,
                    attempt_count=row.attempt_count,
                )
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to claim extraction work") from exc

    async def renew(self, lease: ExtractionWorkLease, lease_seconds: int) -> bool:
        return await asyncio.to_thread(
            self._update_lease_sync, lease, "renew", lease_seconds, None, 0
        )

    async def finish(self, lease: ExtractionWorkLease) -> bool:
        return await asyncio.to_thread(self._update_lease_sync, lease, "finish", 0, None, 0)

    async def retry(
        self,
        lease: ExtractionWorkLease,
        *,
        error_code: str,
        max_attempts: int,
        delay_seconds: int,
    ) -> bool:
        return await asyncio.to_thread(
            self._update_lease_sync, lease, "retry", max_attempts, error_code, delay_seconds
        )

    def _update_lease_sync(
        self,
        lease: ExtractionWorkLease,
        action: str,
        value: int,
        error_code: str | None,
        delay_seconds: int,
    ) -> bool:
        try:
            with self._sessions.begin() as session:
                now = datetime.now(UTC)
                values: dict[str, object] = {"updated_at": now}
                if action == "renew":
                    values["lease_expires_at"] = now + timedelta(seconds=value)
                elif action == "finish":
                    values.update(
                        status="done", worker_id=None, claim_token=None, lease_expires_at=None
                    )
                else:
                    exhausted = lease.attempt_count >= value
                    values.update(
                        status="failed" if exhausted else "pending",
                        worker_id=None,
                        claim_token=None,
                        lease_expires_at=None,
                        next_attempt_at=(
                            None if exhausted else now + timedelta(seconds=delay_seconds)
                        ),
                        last_error_code=error_code,
                    )
                result = cast(CursorResult[Any], session.execute(
                    update(ExtractionWorkItemRow)
                    .where(
                        ExtractionWorkItemRow.task_id == lease.task_id,
                        ExtractionWorkItemRow.tenant_id == lease.tenant_id,
                        ExtractionWorkItemRow.status == "leased",
                        ExtractionWorkItemRow.claim_token == lease.claim_token,
                        ExtractionWorkItemRow.lease_expires_at > now,
                    )
                    .values(**values)
                ))
                if result.rowcount == 1 and action == "retry" and lease.attempt_count >= value:
                    run = session.get(ExtractionRunRow, lease.identity.run_id)
                    if run is not None and run.status not in {
                        WorkflowStatus.COMPLETED.value, WorkflowStatus.FAILED.value,
                    }:
                        run.status = (
                            WorkflowStatus.PENDING_REVIEW.value
                            if lease.kind == "resume" else WorkflowStatus.FAILED.value
                        )
                        run.failure_message = (
                            "审核处理未完成，请重新领取后重试"
                            if lease.kind == "resume"
                            else "后台处理多次失败，请按 run_id 联系管理员"
                        )
                        run.updated_at = now
                return result.rowcount == 1
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to update extraction work lease") from exc

    @staticmethod
    def _run_record(run: ExtractionRunRow) -> ExtractionRunRecord:
        return ExtractionRunRecord(
            identity=WorkflowIdentity(
                thread_id=run.thread_id, run_id=run.run_id, document_id=run.document_id
            ),
            status=WorkflowStatus(run.status),
            validation_route=(
                ValidationRoute(run.validation_route) if run.validation_route is not None else None
            ),
            failure_message=run.failure_message,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )
