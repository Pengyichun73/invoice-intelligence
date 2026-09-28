"""SQLAlchemy implementation of tenant-scoped human review tasks."""

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Engine, and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import (
    ResourceConflictError,
    ResourceNotFoundError,
    WorkflowPersistenceError,
)
from invoice_intelligence.domain.json_types import JsonValue
from invoice_intelligence.domain.review_tasks import (
    ReviewTask,
    ReviewTaskAudit,
    ReviewTaskAuditAction,
    ReviewTaskCursor,
    ReviewTaskFilter,
    ReviewTaskPage,
    ReviewTaskStatus,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    ExtractionWorkItemRow,
    ReviewTaskAuditRow,
    ReviewTaskRow,
)


class SQLAlchemyReviewTaskRepository:
    """Keep task transitions atomic through tenant-scoped revision CAS."""

    def __init__(self, engine: Engine) -> None:
        self._dialect_name = engine.dialect.name
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def list_review_tasks(
        self,
        tenant_id: str,
        filters: ReviewTaskFilter,
        *,
        limit: int,
        cursor: ReviewTaskCursor | None,
    ) -> ReviewTaskPage:
        return await asyncio.to_thread(self._list_sync, tenant_id, filters, limit, cursor)

    async def get_review_task(self, tenant_id: str, identifier: str) -> ReviewTask | None:
        return await asyncio.to_thread(self._get_sync, tenant_id, identifier)

    async def claim_review_task(
        self,
        tenant_id: str,
        review_id: str,
        *,
        reviewer_id: str,
        expected_revision: int,
        lease_token: str,
        lease_expires_at: datetime,
        now: datetime,
        audit: ReviewTaskAudit,
    ) -> ReviewTask:
        return await asyncio.to_thread(
            self._claim_sync,
            tenant_id,
            review_id,
            reviewer_id,
            expected_revision,
            lease_token,
            lease_expires_at,
            now,
            audit,
        )

    async def release_review_task(
        self,
        tenant_id: str,
        review_id: str,
        *,
        reviewer_id: str,
        lease_token: str,
        expected_revision: int,
        now: datetime,
        audit: ReviewTaskAudit,
    ) -> ReviewTask:
        return await asyncio.to_thread(
            self._release_sync,
            tenant_id,
            review_id,
            reviewer_id,
            lease_token,
            expected_revision,
            now,
            audit,
        )

    async def reassign_review_task(
        self,
        tenant_id: str,
        review_id: str,
        *,
        actor_id: str,
        target_reviewer_id: str,
        lease_token: str,
        expected_revision: int,
        now: datetime,
        audit: ReviewTaskAudit,
    ) -> ReviewTask:
        return await asyncio.to_thread(
            self._reassign_sync,
            tenant_id,
            review_id,
            actor_id,
            target_reviewer_id,
            lease_token,
            expected_revision,
            now,
            audit,
        )

    async def cancel_review_task(
        self,
        tenant_id: str,
        review_id: str,
        *,
        expected_revision: int,
        reason: str,
        now: datetime,
        audit: ReviewTaskAudit,
    ) -> ReviewTask:
        return await asyncio.to_thread(
            self._cancel_sync,
            tenant_id,
            review_id,
            expected_revision,
            reason,
            now,
            audit,
        )

    async def expire_due_review_tasks(
        self,
        tenant_id: str,
        *,
        now: datetime,
        actor_id: str,
        trace_id: str | None,
        limit: int,
    ) -> tuple[ReviewTask, ...]:
        return await asyncio.to_thread(
            self._expire_due_sync, tenant_id, now, actor_id, trace_id, limit
        )

    async def mark_review_submitted(
        self,
        tenant_id: str,
        run_id: str,
        *,
        reviewer_id: str,
        now: datetime,
        trace_id: str | None,
    ) -> ReviewTask:
        return await asyncio.to_thread(
            self._mark_submitted_sync,
            tenant_id,
            run_id,
            reviewer_id,
            now,
            trace_id,
        )

    async def count_review_audits(self, tenant_id: str, review_id: str, action: str) -> int:
        return await asyncio.to_thread(self._count_audits_sync, tenant_id, review_id, action)

    def _list_sync(
        self,
        tenant_id: str,
        filters: ReviewTaskFilter,
        limit: int,
        cursor: ReviewTaskCursor | None,
    ) -> ReviewTaskPage:
        if not 1 <= limit <= 200:
            raise ValueError("Review task page size must be between 1 and 200")
        try:
            with self._sessions() as session:
                statement = select(ReviewTaskRow).where(ReviewTaskRow.tenant_id == tenant_id)
                if filters.reviewer_id is not None:
                    statement = statement.where(
                        ReviewTaskRow.assigned_reviewer_id == filters.reviewer_id
                    )
                if filters.priority is not None:
                    statement = statement.where(ReviewTaskRow.priority == filters.priority)
                if filters.status is not None:
                    statement = statement.where(ReviewTaskRow.status == filters.status.value)
                if filters.created_from is not None:
                    statement = statement.where(ReviewTaskRow.created_at >= filters.created_from)
                if filters.created_to is not None:
                    statement = statement.where(ReviewTaskRow.created_at <= filters.created_to)
                if cursor is not None:
                    statement = statement.where(
                        or_(
                            ReviewTaskRow.created_at < cursor.created_at,
                            and_(
                                ReviewTaskRow.created_at == cursor.created_at,
                                ReviewTaskRow.review_id < cursor.review_id,
                            ),
                        )
                    )
                rows = session.scalars(
                    statement.order_by(
                        ReviewTaskRow.created_at.desc(),
                        ReviewTaskRow.review_id.desc(),
                    ).limit(limit + 1)
                ).all()
                has_more = len(rows) > limit
                page_rows = rows[:limit]
                items = tuple(self._from_row(row) for row in page_rows)
                next_cursor = None
                if has_more and page_rows:
                    last = page_rows[-1]
                    next_cursor = ReviewTaskCursor(
                        created_at=self._aware(last.created_at),
                        review_id=last.review_id,
                    )
                return ReviewTaskPage(items=items, next_cursor=next_cursor)
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to list review tasks") from exc

    def _get_sync(self, tenant_id: str, identifier: str) -> ReviewTask | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(ReviewTaskRow).where(
                        ReviewTaskRow.tenant_id == tenant_id,
                        or_(
                            ReviewTaskRow.review_id == identifier,
                            ReviewTaskRow.run_id == identifier,
                        ),
                    )
                )
                return self._from_row(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to retrieve review task") from exc

    def _claim_sync(
        self,
        tenant_id: str,
        review_id: str,
        reviewer_id: str,
        expected_revision: int,
        lease_token: str,
        lease_expires_at: datetime,
        now: datetime,
        audit: ReviewTaskAudit,
    ) -> ReviewTask:
        conditions = or_(
            and_(
                ReviewTaskRow.status.in_(
                    [ReviewTaskStatus.PENDING_REVIEW.value, ReviewTaskStatus.EXPIRED.value]
                ),
                or_(
                    ReviewTaskRow.assigned_reviewer_id.is_(None),
                    ReviewTaskRow.assigned_reviewer_id == reviewer_id,
                ),
            ),
            and_(
                ReviewTaskRow.status == ReviewTaskStatus.CLAIMED.value,
                ReviewTaskRow.lease_expires_at <= now,
            ),
        )
        values: dict[str, Any] = {
            "status": ReviewTaskStatus.CLAIMED.value,
            "assigned_reviewer_id": reviewer_id,
            "lease_token": lease_token,
            "lease_expires_at": lease_expires_at,
            "revision": expected_revision + 1,
            "updated_at": now,
            "submitted_at": None,
            "cancelled_at": None,
            "cancel_reason": None,
        }
        return self._transition(tenant_id, review_id, expected_revision, conditions, values, audit)

    def _release_sync(
        self,
        tenant_id: str,
        review_id: str,
        reviewer_id: str,
        lease_token: str,
        expected_revision: int,
        now: datetime,
        audit: ReviewTaskAudit,
    ) -> ReviewTask:
        conditions = and_(
            ReviewTaskRow.status == ReviewTaskStatus.CLAIMED.value,
            ReviewTaskRow.assigned_reviewer_id == reviewer_id,
            ReviewTaskRow.lease_token == lease_token,
            ReviewTaskRow.lease_expires_at > now,
        )
        values = {
            "status": ReviewTaskStatus.PENDING_REVIEW.value,
            "assigned_reviewer_id": None,
            "lease_token": None,
            "lease_expires_at": None,
            "revision": expected_revision + 1,
            "updated_at": now,
        }
        return self._transition(tenant_id, review_id, expected_revision, conditions, values, audit)

    def _reassign_sync(
        self,
        tenant_id: str,
        review_id: str,
        actor_id: str,
        target_reviewer_id: str,
        lease_token: str,
        expected_revision: int,
        now: datetime,
        audit: ReviewTaskAudit,
    ) -> ReviewTask:
        conditions = and_(
            ReviewTaskRow.status == ReviewTaskStatus.CLAIMED.value,
            ReviewTaskRow.assigned_reviewer_id == actor_id,
            ReviewTaskRow.lease_token == lease_token,
            ReviewTaskRow.lease_expires_at > now,
        )
        values = {
            "status": ReviewTaskStatus.PENDING_REVIEW.value,
            "assigned_reviewer_id": target_reviewer_id,
            "lease_token": None,
            "lease_expires_at": None,
            "revision": expected_revision + 1,
            "updated_at": now,
        }
        return self._transition(tenant_id, review_id, expected_revision, conditions, values, audit)

    def _cancel_sync(
        self,
        tenant_id: str,
        review_id: str,
        expected_revision: int,
        reason: str,
        now: datetime,
        audit: ReviewTaskAudit,
    ) -> ReviewTask:
        conditions = ReviewTaskRow.status.in_(
            [
                ReviewTaskStatus.PENDING_REVIEW.value,
                ReviewTaskStatus.CLAIMED.value,
                ReviewTaskStatus.EXPIRED.value,
            ]
        )
        values = {
            "status": ReviewTaskStatus.CANCELLED.value,
            "lease_token": None,
            "lease_expires_at": None,
            "cancelled_at": now,
            "cancel_reason": reason,
            "revision": expected_revision + 1,
            "updated_at": now,
            "resolved_at": now,
        }
        return self._transition(tenant_id, review_id, expected_revision, conditions, values, audit)

    def _transition(
        self,
        tenant_id: str,
        review_id: str,
        expected_revision: int,
        state_condition: Any,
        values: dict[str, Any],
        audit: ReviewTaskAudit,
    ) -> ReviewTask:
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(ReviewTaskRow).where(
                        ReviewTaskRow.tenant_id == tenant_id,
                        ReviewTaskRow.review_id == review_id,
                    ).with_for_update()
                )
                if row is None:
                    raise ResourceNotFoundError("Review task was not found")
                if self._has_active_resume(session, tenant_id, row.run_id):
                    raise ResourceConflictError("Review decision is already processing")
                changed = cast(
                    CursorResult[Any],
                    session.execute(
                        update(ReviewTaskRow)
                        .where(
                            ReviewTaskRow.tenant_id == tenant_id,
                            ReviewTaskRow.review_id == review_id,
                            ReviewTaskRow.revision == expected_revision,
                            state_condition,
                        )
                        .values(**values)
                    ),
                )
                if changed.rowcount != 1:
                    raise ResourceConflictError("Review task revision, lease, or status changed")
                self._add_audit(session, audit)
                row = session.get(ReviewTaskRow, review_id)
                if row is None:
                    raise WorkflowPersistenceError("Updated review task was not found")
                return self._from_row(row)
        except (ResourceNotFoundError, ResourceConflictError, WorkflowPersistenceError):
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to transition review task") from exc

    def _expire_due_sync(
        self,
        tenant_id: str,
        now: datetime,
        actor_id: str,
        trace_id: str | None,
        limit: int,
    ) -> tuple[ReviewTask, ...]:
        try:
            with self._sessions.begin() as session:
                statement = (
                    select(ReviewTaskRow)
                    .where(
                        ReviewTaskRow.tenant_id == tenant_id,
                        ReviewTaskRow.status == ReviewTaskStatus.CLAIMED.value,
                        ReviewTaskRow.lease_expires_at <= now,
                    )
                    .order_by(ReviewTaskRow.lease_expires_at, ReviewTaskRow.review_id)
                    .limit(limit)
                )
                if self._dialect_name == "postgresql":
                    statement = statement.with_for_update(skip_locked=True)
                rows = session.scalars(statement).all()
                tasks: list[ReviewTask] = []
                for row in rows:
                    if self._has_active_resume(session, tenant_id, row.run_id):
                        continue
                    from_status = ReviewTaskStatus(row.status)
                    expired_reviewer_id = row.assigned_reviewer_id
                    row.status = ReviewTaskStatus.EXPIRED.value
                    row.assigned_reviewer_id = None
                    row.lease_token = None
                    row.lease_expires_at = None
                    row.revision += 1
                    row.updated_at = now
                    self._add_audit(
                        session,
                        ReviewTaskAudit(
                            audit_id=uuid4().hex,
                            tenant_id=tenant_id,
                            review_id=row.review_id,
                            action=ReviewTaskAuditAction.EXPIRE,
                            actor_id=actor_id,
                            target_reviewer_id=expired_reviewer_id,
                            from_status=from_status,
                            to_status=ReviewTaskStatus.EXPIRED,
                            revision=row.revision,
                            reason_code="lease_expired",
                            trace_id=trace_id,
                            created_at=now,
                        ),
                    )
                    session.flush()
                    tasks.append(self._from_row(row))
                return tuple(tasks)
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to expire review task leases") from exc

    @staticmethod
    def _has_active_resume(session: Session, tenant_id: str, run_id: str) -> bool:
        return session.scalar(
            select(ExtractionWorkItemRow.task_id).where(
                ExtractionWorkItemRow.tenant_id == tenant_id,
                ExtractionWorkItemRow.run_id == run_id,
                ExtractionWorkItemRow.kind == "resume",
                ExtractionWorkItemRow.status.in_(("pending", "leased")),
            ).limit(1)
        ) is not None

    def _mark_submitted_sync(
        self,
        tenant_id: str,
        run_id: str,
        reviewer_id: str,
        now: datetime,
        trace_id: str | None,
    ) -> ReviewTask:
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(ReviewTaskRow).where(
                        ReviewTaskRow.tenant_id == tenant_id,
                        ReviewTaskRow.run_id == run_id,
                    )
                )
                if row is None:
                    raise ResourceNotFoundError("Review task was not found")
                if row.status == ReviewTaskStatus.SUBMITTED.value:
                    return self._from_row(row)
                if (
                    row.status != ReviewTaskStatus.CLAIMED.value
                    or row.assigned_reviewer_id != reviewer_id
                ):
                    raise ResourceConflictError("Review task is not claimed by this reviewer")
                old_status = ReviewTaskStatus(row.status)
                row.status = ReviewTaskStatus.SUBMITTED.value
                row.lease_token = None
                row.lease_expires_at = None
                row.submitted_at = now
                row.resolved_at = now
                row.updated_at = now
                row.revision += 1
                self._add_audit(
                    session,
                    ReviewTaskAudit(
                        audit_id=uuid4().hex,
                        tenant_id=tenant_id,
                        review_id=row.review_id,
                        action=ReviewTaskAuditAction.SUBMIT,
                        actor_id=reviewer_id,
                        target_reviewer_id=reviewer_id,
                        from_status=old_status,
                        to_status=ReviewTaskStatus.SUBMITTED,
                        revision=row.revision,
                        reason_code="review_facts_persisted",
                        trace_id=trace_id,
                        created_at=now,
                    ),
                )
                session.flush()
                return self._from_row(row)
        except (ResourceNotFoundError, ResourceConflictError):
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to submit review task") from exc

    def _count_audits_sync(self, tenant_id: str, review_id: str, action: str) -> int:
        with self._sessions() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(ReviewTaskAuditRow)
                    .where(
                        ReviewTaskAuditRow.tenant_id == tenant_id,
                        ReviewTaskAuditRow.review_id == review_id,
                        ReviewTaskAuditRow.action == action,
                    )
                )
                or 0
            )

    @staticmethod
    def _add_audit(session: Session, audit: ReviewTaskAudit) -> None:
        session.add(
            ReviewTaskAuditRow(
                audit_id=audit.audit_id,
                tenant_id=audit.tenant_id,
                review_id=audit.review_id,
                action=audit.action.value,
                actor_id=audit.actor_id,
                target_reviewer_id=audit.target_reviewer_id,
                from_status=audit.from_status.value,
                to_status=audit.to_status.value,
                revision=audit.revision,
                reason_code=audit.reason_code,
                trace_id=audit.trace_id,
                created_at=audit.created_at,
            )
        )

    @classmethod
    def _from_row(cls, row: ReviewTaskRow) -> ReviewTask:
        return ReviewTask(
            review_id=row.review_id,
            tenant_id=row.tenant_id,
            run_id=row.run_id,
            status=ReviewTaskStatus(row.status),
            request_payload=cast(dict[str, JsonValue], dict(row.request_json)),
            request_version=row.version,
            priority=row.priority,
            assigned_reviewer_id=row.assigned_reviewer_id,
            lease_token=row.lease_token,
            lease_expires_at=(
                cls._aware(row.lease_expires_at) if row.lease_expires_at is not None else None
            ),
            revision=row.revision,
            created_at=cls._aware(row.created_at),
            updated_at=cls._aware(row.updated_at),
            submitted_at=(cls._aware(row.submitted_at) if row.submitted_at else None),
            cancelled_at=(cls._aware(row.cancelled_at) if row.cancelled_at else None),
            cancel_reason=row.cancel_reason,
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
