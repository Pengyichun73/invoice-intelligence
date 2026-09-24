"""SQLAlchemy object lifecycle queue."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import WorkflowPersistenceError
from invoice_intelligence.domain.storage import ObjectKind, StoredObjectRecord, StoredObjectStatus
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import StoredObjectRow


class SQLAlchemyStoredObjectRepository:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def register_available(self, record: StoredObjectRecord) -> None:
        await asyncio.to_thread(self._register_available, record)

    def _register_available(self, record: StoredObjectRecord) -> None:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.get(StoredObjectRow, record.object_id)
            if existing is not None:
                if (
                    existing.tenant_id != record.tenant_id
                    or existing.storage_uri != record.storage_uri
                    or existing.checksum != record.checksum
                ):
                    raise WorkflowPersistenceError(
                        "Artifact object id is bound to different immutable metadata"
                    )
                return
            session.add(
                StoredObjectRow(
                    object_id=record.object_id,
                    tenant_id=record.tenant_id,
                    parent_document_id=record.parent_document_id,
                    object_kind=record.kind.value,
                    stable_key=record.storage_uri,
                    storage_uri=record.storage_uri,
                    checksum=record.checksum,
                    media_type=record.media_type,
                    size_bytes=record.size_bytes,
                    status=StoredObjectStatus.AVAILABLE.value,
                    revision=1,
                    retention_until=record.retention_until,
                    attempt_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )

    async def claim_deletions(
        self, worker_id: str, limit: int, lease_seconds: int
    ) -> tuple[StoredObjectRecord, ...]:
        return await asyncio.to_thread(self._claim, worker_id, limit, lease_seconds)

    def _claim(
        self, worker_id: str, limit: int, lease_seconds: int
    ) -> tuple[StoredObjectRecord, ...]:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            session.execute(
                update(StoredObjectRow)
                .where(
                    StoredObjectRow.status == StoredObjectStatus.AVAILABLE.value,
                    StoredObjectRow.retention_until.is_not(None),
                    StoredObjectRow.retention_until <= now,
                )
                .values(
                    status=StoredObjectStatus.DELETE_PENDING.value,
                    revision=StoredObjectRow.revision + 1,
                )
            )
            rows = tuple(
                session.scalars(
                    select(StoredObjectRow)
                    .where(
                        or_(
                            StoredObjectRow.status == StoredObjectStatus.DELETE_PENDING.value,
                            (
                                (StoredObjectRow.status == StoredObjectStatus.DELETING.value)
                                & (StoredObjectRow.lease_expires_at < now)
                            ),
                        ),
                        or_(
                            StoredObjectRow.next_attempt_at.is_(None),
                            StoredObjectRow.next_attempt_at <= now,
                        ),
                    )
                    .order_by(StoredObjectRow.updated_at, StoredObjectRow.object_id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            claimed: list[StoredObjectRecord] = []
            for row in rows:
                row.status = StoredObjectStatus.DELETING.value
                row.worker_id = worker_id
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                row.attempt_count += 1
                row.revision += 1
                row.updated_at = now
                claimed.append(self._to_domain(row))
            return tuple(claimed)

    async def mark_deleted(self, object_id: str, worker_id: str, revision: int) -> None:
        await asyncio.to_thread(self._complete, object_id, worker_id, revision, True, None, False)

    async def mark_failure(
        self, object_id: str, worker_id: str, revision: int, error_code: str, retryable: bool
    ) -> None:
        await asyncio.to_thread(
            self._complete, object_id, worker_id, revision, False, error_code, retryable
        )

    def _complete(
        self,
        object_id: str,
        worker_id: str,
        revision: int,
        deleted: bool,
        error_code: str | None,
        retryable: bool,
    ) -> None:
        now = datetime.now(UTC)
        values: dict[str, object] = {
            "status": (
                StoredObjectStatus.DELETED.value
                if deleted
                else (
                    StoredObjectStatus.DELETE_PENDING.value
                    if retryable
                    else StoredObjectStatus.FAILED.value
                )
            ),
            "worker_id": None,
            "lease_expires_at": None,
            "updated_at": now,
            "revision": revision + 1,
            "last_error_code": error_code,
            "last_error_at": now if error_code else None,
            "deleted_at": now if deleted else None,
        }
        if retryable:
            values["next_attempt_at"] = now + timedelta(seconds=30)
        with self._sessions.begin() as session:
            result = session.execute(
                update(StoredObjectRow)
                .where(
                    StoredObjectRow.object_id == object_id,
                    StoredObjectRow.worker_id == worker_id,
                    StoredObjectRow.revision == revision,
                    StoredObjectRow.status == StoredObjectStatus.DELETING.value,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise WorkflowPersistenceError("Stored object lifecycle claim is stale")

    @staticmethod
    def _to_domain(row: StoredObjectRow) -> StoredObjectRecord:
        return StoredObjectRecord(
            object_id=row.object_id,
            tenant_id=row.tenant_id,
            parent_document_id=row.parent_document_id,
            kind=ObjectKind(row.object_kind),
            storage_uri=row.storage_uri,
            checksum=row.checksum,
            media_type=row.media_type,
            size_bytes=row.size_bytes,
            status=StoredObjectStatus(row.status),
            revision=row.revision,
            retention_until=row.retention_until,
            delete_after=row.delete_after,
        )
