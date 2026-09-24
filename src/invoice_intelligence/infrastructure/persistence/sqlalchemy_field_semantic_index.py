"""SQLAlchemy adapter for rebuildable field semantic index projection state."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import WorkflowPersistenceError
from invoice_intelligence.application.ports.projection_lease import ProjectionLease
from invoice_intelligence.domain.examples import IndexProjectionStatus, IndexVersion, ModelVersion
from invoice_intelligence.domain.field_semantics import (
    FieldSemanticCatalogVersion,
    FieldSemanticIndexVersionRecord,
    FieldSemanticProjectionTask,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    FieldSemanticIndexProjectionRow,
    FieldSemanticIndexVersionRow,
    ModelVersionRow,
)


class SQLAlchemyFieldSemanticProjectionRepository:
    """Persist version, lease, retry, activation, and invalidation facts."""

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

    async def register_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        dense_model_version: ModelVersion,
        sparse_model_version: ModelVersion | None,
    ) -> None:
        await asyncio.to_thread(
            self._register_version_sync,
            tenant_id,
            index_version,
            schema_version,
            catalog_version,
            dense_model_version,
            sparse_model_version,
        )

    async def schedule(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        tasks: Sequence[FieldSemanticProjectionTask],
    ) -> None:
        await asyncio.to_thread(
            self._schedule_sync,
            tenant_id,
            index_version,
            tuple(tasks),
        )

    async def claim_pending(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        limit: int,
        worker_id: str,
        lease_seconds: float,
    ) -> tuple[ProjectionLease[FieldSemanticProjectionTask], ...]:
        return await asyncio.to_thread(
            self._claim_pending_sync,
            tenant_id,
            index_version,
            limit,
            worker_id,
            lease_seconds,
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
        self, tenant_id: str, semantic_id: str, index_version: IndexVersion,
        worker_id: str, lease_token: str, lease_seconds: float,
    ) -> None:
        await asyncio.to_thread(
            self._renew_lease_sync, tenant_id, semantic_id, index_version,
            worker_id, lease_token, lease_seconds,
        )

    def _renew_lease_sync(
        self, tenant_id: str, semantic_id: str, index_version: IndexVersion,
        worker_id: str, lease_token: str, lease_seconds: float,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._sessions.begin() as session:
            version = self._get_version_row(session, tenant_id, index_version)
            now = self._now()
            result = session.execute(
                update(FieldSemanticIndexProjectionRow).where(
                    FieldSemanticIndexProjectionRow.tenant_id == tenant_id,
                    FieldSemanticIndexProjectionRow.semantic_id == semantic_id,
                    FieldSemanticIndexProjectionRow.index_version_id
                    == version.index_version_id,
                    FieldSemanticIndexProjectionRow.status
                    == IndexProjectionStatus.PROCESSING.value,
                    FieldSemanticIndexProjectionRow.worker_id == worker_id,
                    FieldSemanticIndexProjectionRow.lease_token == lease_token,
                    FieldSemanticIndexProjectionRow.lease_expires_at > now,
                ).values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            )
            if result.rowcount != 1:
                raise WorkflowPersistenceError("Projection lease is no longer owned")

    async def mark_projected(
        self,
        tenant_id: str,
        semantic_id: str,
        index_version: IndexVersion,
        projection_checksum: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        await asyncio.to_thread(
            self._mark_projected_sync,
            tenant_id,
            semantic_id,
            index_version,
            projection_checksum,
            worker_id,
            lease_token,
        )

    async def mark_failed(
        self,
        tenant_id: str,
        semantic_id: str,
        index_version: IndexVersion,
        error_code: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        await asyncio.to_thread(
            self._mark_failed_sync,
            tenant_id,
            semantic_id,
            index_version,
            error_code,
            worker_id,
            lease_token,
        )

    async def get_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> FieldSemanticIndexVersionRecord | None:
        return await asyncio.to_thread(
            self._get_version_sync,
            tenant_id,
            index_version,
        )

    async def get_active_version(
        self,
        tenant_id: str,
        schema_version: str,
    ) -> FieldSemanticIndexVersionRecord | None:
        return await asyncio.to_thread(
            self._get_active_version_sync,
            tenant_id,
            schema_version,
        )

    async def list_versions(
        self,
        tenant_id: str,
        *,
        schema_version: str | None = None,
    ) -> tuple[IndexVersion, ...]:
        return await asyncio.to_thread(
            self._list_versions_sync,
            tenant_id,
            schema_version,
        )

    async def activate_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        await asyncio.to_thread(self._activate_version_sync, tenant_id, index_version)

    async def reset_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> int:
        return await asyncio.to_thread(self._reset_version_sync, tenant_id, index_version)

    async def invalidate_catalog(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        reason: str,
    ) -> tuple[IndexVersion, ...]:
        return await asyncio.to_thread(
            self._invalidate_sync,
            tenant_id,
            schema_version,
            catalog_version,
            reason,
        )

    async def invalidate_schema(
        self,
        tenant_id: str,
        schema_version: str,
        reason: str,
    ) -> tuple[IndexVersion, ...]:
        return await asyncio.to_thread(
            self._invalidate_sync,
            tenant_id,
            schema_version,
            None,
            reason,
        )

    async def delete_tenant(self, tenant_id: str) -> tuple[IndexVersion, ...]:
        return await asyncio.to_thread(self._delete_tenant_sync, tenant_id)

    def _register_version_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        dense_model_version: ModelVersion,
        sparse_model_version: ModelVersion | None,
    ) -> None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        try:
            with self._sessions.begin() as session:
                dense_id = self._ensure_model_version(
                    session, tenant_id, dense_model_version
                )
                sparse_id = (
                    self._ensure_model_version(session, tenant_id, sparse_model_version)
                    if sparse_model_version is not None
                    else None
                )
                row_id = self._version_id(tenant_id, index_version)
                values = {
                    "index_version_id": row_id,
                    "tenant_id": tenant_id,
                    "version": index_version.value,
                    "schema_version": schema_version,
                    "catalog_version": catalog_version.value,
                    "dense_model_version_id": dense_id,
                    "sparse_model_version_id": sparse_id,
                    "is_active": False,
                    "is_valid": True,
                    "invalidated_reason": None,
                    "created_at": self._now(),
                    "activated_at": None,
                    "retired_at": None,
                    "invalidated_at": None,
                }
                self._insert_do_nothing(session, FieldSemanticIndexVersionRow, values)
                row = session.get(FieldSemanticIndexVersionRow, row_id)
                expected = (
                    tenant_id,
                    index_version.value,
                    schema_version,
                    catalog_version.value,
                    dense_id,
                    sparse_id,
                )
                if row is None or self._version_definition(row) != expected:
                    raise WorkflowPersistenceError(
                        "Field semantic index version is bound to different metadata"
                    )
                if not row.is_valid:
                    raise WorkflowPersistenceError(
                        "Invalidated field semantic index version cannot be registered again"
                    )
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError(
                "Unable to register field semantic index version"
            ) from exc

    def _schedule_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        tasks: tuple[FieldSemanticProjectionTask, ...],
    ) -> None:
        if not tasks:
            return
        self._require_text("tenant_id", tenant_id)
        semantic_ids = tuple(task.semantic_id for task in tasks)
        if len(semantic_ids) != len(set(semantic_ids)):
            raise ValueError("Field semantic projection tasks must be unique")
        try:
            with self._sessions.begin() as session:
                version = self._get_version_row(session, tenant_id, index_version)
                if version.is_active:
                    raise WorkflowPersistenceError(
                        "New projections require a non-active index build"
                    )
                for task in tasks:
                    self._validate_task(task, version)
                    values = {
                        "projection_id": task.projection_id,
                        "tenant_id": tenant_id,
                        "semantic_id": task.semantic_id,
                        "index_version_id": version.index_version_id,
                        "document_type": task.document_type,
                        "canonical_field_path": task.canonical_field_path,
                        "schema_version": task.schema_version,
                        "catalog_version": task.catalog_version.value,
                        "display_name": task.display_name,
                        "description": task.description,
                        "value_type": task.value_type,
                        "approved_aliases_json": list(task.approved_aliases),
                        "negative_aliases_json": list(task.negative_aliases),
                        "context_anchors_json": list(task.context_anchors),
                        "source_fingerprint": task.source_fingerprint,
                        "status": IndexProjectionStatus.PENDING.value,
                        "projection_checksum": None,
                        "attempt_count": 0,
                        "last_error_code": None,
                        "processing_started_at": None,
                        "indexed_at": None,
                        "invalidated_at": None,
                        "created_at": self._now(),
                        "updated_at": self._now(),
                    }
                    self._insert_do_nothing(
                        session, FieldSemanticIndexProjectionRow, values
                    )
                    row = session.get(FieldSemanticIndexProjectionRow, task.projection_id)
                    if row is None or self._projection_definition(row) != (
                        tenant_id,
                        task.semantic_id,
                        version.index_version_id,
                        task.document_type,
                        task.canonical_field_path,
                        task.schema_version,
                        task.catalog_version.value,
                        task.display_name,
                        task.description,
                        task.value_type,
                        tuple(task.approved_aliases),
                        tuple(task.negative_aliases),
                        tuple(task.context_anchors),
                        task.source_fingerprint,
                    ):
                        raise WorkflowPersistenceError(
                            "Field semantic projection identity collision"
                        )
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError(
                "Unable to schedule field semantic projections"
            ) from exc

    def _claim_pending_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        limit: int,
        worker_id: str,
        lease_seconds: float,
    ) -> tuple[ProjectionLease[FieldSemanticProjectionTask], ...]:
        if limit <= 0:
            raise ValueError("Projection claim limit must be greater than zero")
        self._require_text("worker_id", worker_id)
        if len(worker_id) > 128 or lease_seconds <= 0:
            raise ValueError("Invalid projection worker lease")
        try:
            with self._sessions.begin() as session:
                version = self._get_version_row(session, tenant_id, index_version)
                statement = (
                    select(FieldSemanticIndexProjectionRow)
                    .where(
                        FieldSemanticIndexProjectionRow.tenant_id == tenant_id,
                        FieldSemanticIndexProjectionRow.index_version_id
                        == version.index_version_id,
                        FieldSemanticIndexProjectionRow.attempt_count
                        < self._projection_max_attempts,
                        (
                            (
                                FieldSemanticIndexProjectionRow.status.in_((
                                    IndexProjectionStatus.PENDING.value,
                                    IndexProjectionStatus.FAILED.value,
                                ))
                                & (
                                    FieldSemanticIndexProjectionRow.next_attempt_at.is_(None)
                                    | (FieldSemanticIndexProjectionRow.next_attempt_at
                                       <= self._now())
                                )
                            )
                            | (
                                (FieldSemanticIndexProjectionRow.status
                                 == IndexProjectionStatus.PROCESSING.value)
                                & (FieldSemanticIndexProjectionRow.lease_expires_at <= self._now())
                            )
                        ),
                    )
                    .order_by(
                        FieldSemanticIndexProjectionRow.updated_at,
                        FieldSemanticIndexProjectionRow.projection_id,
                    )
                    .limit(limit)
                )
                if self._dialect_name == "postgresql":
                    statement = statement.with_for_update(skip_locked=True)
                rows = session.scalars(statement).all()
                now = self._now()
                tasks: list[ProjectionLease[FieldSemanticProjectionTask]] = []
                for row in rows:
                    row.status = IndexProjectionStatus.PROCESSING.value
                    row.attempt_count += 1
                    row.last_error_code = None
                    row.processing_started_at = now
                    row.worker_id = worker_id
                    row.lease_token = uuid4().hex
                    row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                    row.next_attempt_at = None
                    row.updated_at = now
                    tasks.append(ProjectionLease(
                        self._task_from_row(row, version), worker_id,
                        row.lease_token, row.lease_expires_at,
                    ))
                session.flush()
                return tuple(tasks)
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to claim field semantic projections"
            ) from exc

    def _requeue_stale_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        stale_before: datetime,
    ) -> int:
        self._require_aware("stale_before", stale_before)
        try:
            with self._sessions.begin() as session:
                version = self._get_version_row(session, tenant_id, index_version)
                result = session.execute(
                    update(FieldSemanticIndexProjectionRow)
                    .where(
                        FieldSemanticIndexProjectionRow.tenant_id == tenant_id,
                        FieldSemanticIndexProjectionRow.index_version_id
                        == version.index_version_id,
                        FieldSemanticIndexProjectionRow.status
                        == IndexProjectionStatus.PROCESSING.value,
                        FieldSemanticIndexProjectionRow.lease_expires_at <= stale_before,
                    )
                    .values(
                        status=case(
                            (
                                FieldSemanticIndexProjectionRow.attempt_count
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
            raise WorkflowPersistenceError(
                "Unable to requeue stale field semantic projections"
            ) from exc

    def _mark_projected_sync(
        self,
        tenant_id: str,
        semantic_id: str,
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
                version = self._get_version_row(session, tenant_id, index_version)
                now = self._now()
                result = session.execute(
                    update(FieldSemanticIndexProjectionRow).where(
                        FieldSemanticIndexProjectionRow.tenant_id == tenant_id,
                        FieldSemanticIndexProjectionRow.semantic_id == semantic_id,
                        FieldSemanticIndexProjectionRow.index_version_id
                        == version.index_version_id,
                        FieldSemanticIndexProjectionRow.status
                        == IndexProjectionStatus.PROCESSING.value,
                        FieldSemanticIndexProjectionRow.worker_id == worker_id,
                        FieldSemanticIndexProjectionRow.lease_token == lease_token,
                        FieldSemanticIndexProjectionRow.lease_expires_at > now,
                    ).values(
                        status=IndexProjectionStatus.INDEXED.value,
                        projection_checksum=projection_checksum, last_error_code=None,
                        processing_started_at=None, worker_id=None, lease_token=None,
                        lease_expires_at=None, next_attempt_at=None,
                        indexed_at=now, updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    raise WorkflowPersistenceError("Projection lease is no longer owned")
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError(
                "Unable to mark field semantic projection indexed"
            ) from exc

    def _mark_failed_sync(
        self,
        tenant_id: str,
        semantic_id: str,
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
                version = self._get_version_row(session, tenant_id, index_version)
                row = self._get_projection(session, tenant_id, semantic_id, index_version)
                now = self._now()
                result = session.execute(
                    update(FieldSemanticIndexProjectionRow).where(
                        FieldSemanticIndexProjectionRow.tenant_id == tenant_id,
                        FieldSemanticIndexProjectionRow.semantic_id == semantic_id,
                        FieldSemanticIndexProjectionRow.index_version_id
                        == version.index_version_id,
                        FieldSemanticIndexProjectionRow.status
                        == IndexProjectionStatus.PROCESSING.value,
                        FieldSemanticIndexProjectionRow.worker_id == worker_id,
                        FieldSemanticIndexProjectionRow.lease_token == lease_token,
                        FieldSemanticIndexProjectionRow.lease_expires_at > now,
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
            raise WorkflowPersistenceError(
                "Unable to mark field semantic projection failed"
            ) from exc

    def _get_version_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> FieldSemanticIndexVersionRecord | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(FieldSemanticIndexVersionRow).where(
                        FieldSemanticIndexVersionRow.tenant_id == tenant_id,
                        FieldSemanticIndexVersionRow.version == index_version.value,
                    )
                )
                return self._version_record(session, row) if row is not None else None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to read field semantic index version"
            ) from exc

    def _get_active_version_sync(
        self,
        tenant_id: str,
        schema_version: str,
    ) -> FieldSemanticIndexVersionRecord | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(FieldSemanticIndexVersionRow).where(
                        FieldSemanticIndexVersionRow.tenant_id == tenant_id,
                        FieldSemanticIndexVersionRow.schema_version == schema_version,
                        FieldSemanticIndexVersionRow.is_active.is_(True),
                        FieldSemanticIndexVersionRow.is_valid.is_(True),
                    )
                )
                return self._version_record(session, row) if row is not None else None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to read active field semantic index version"
            ) from exc

    def _list_versions_sync(
        self,
        tenant_id: str,
        schema_version: str | None,
    ) -> tuple[IndexVersion, ...]:
        self._require_text("tenant_id", tenant_id)
        if schema_version is not None:
            self._require_text("schema_version", schema_version)
        try:
            with self._sessions() as session:
                statement = select(FieldSemanticIndexVersionRow.version).where(
                    FieldSemanticIndexVersionRow.tenant_id == tenant_id
                )
                if schema_version is not None:
                    statement = statement.where(
                        FieldSemanticIndexVersionRow.schema_version == schema_version
                    )
                return tuple(
                    IndexVersion(value)
                    for value in session.scalars(
                        statement.order_by(FieldSemanticIndexVersionRow.created_at)
                    ).all()
                )
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to list field semantic index versions"
            ) from exc

    def _activate_version_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        try:
            with self._sessions.begin() as session:
                row = self._get_version_row(session, tenant_id, index_version)
                if row.is_active:
                    return
                counts = self._projection_counts(session, row.index_version_id)
                incomplete = sum(
                    count
                    for status, count in counts.items()
                    if status not in {
                        IndexProjectionStatus.INDEXED.value,
                        IndexProjectionStatus.INVALIDATED.value,
                    }
                )
                if incomplete or counts.get(IndexProjectionStatus.INDEXED.value, 0) == 0:
                    raise WorkflowPersistenceError(
                        "Field semantic index version is not ready for activation"
                    )
                now = self._now()
                session.execute(
                    update(FieldSemanticIndexVersionRow)
                    .where(
                        FieldSemanticIndexVersionRow.tenant_id == tenant_id,
                        FieldSemanticIndexVersionRow.schema_version == row.schema_version,
                        FieldSemanticIndexVersionRow.is_active.is_(True),
                    )
                    .values(is_active=False, retired_at=now)
                )
                row.is_active = True
                row.activated_at = now
                row.retired_at = None
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError(
                "Unable to activate field semantic index version"
            ) from exc

    def _reset_version_sync(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> int:
        try:
            with self._sessions.begin() as session:
                row = self._get_version_row(session, tenant_id, index_version)
                if row.is_active:
                    raise WorkflowPersistenceError(
                        "Active field semantic indexes cannot be rebuilt in place"
                    )
                result = session.execute(
                    update(FieldSemanticIndexProjectionRow)
                    .where(
                        FieldSemanticIndexProjectionRow.tenant_id == tenant_id,
                        FieldSemanticIndexProjectionRow.index_version_id
                        == row.index_version_id,
                        FieldSemanticIndexProjectionRow.status
                        != IndexProjectionStatus.INVALIDATED.value,
                    )
                    .values(
                        status=IndexProjectionStatus.PENDING.value,
                        projection_checksum=None,
                        last_error_code=None,
                        processing_started_at=None,
                        worker_id=None, lease_token=None, lease_expires_at=None,
                        attempt_count=0,
                        next_attempt_at=None,
                        indexed_at=None,
                        updated_at=self._now(),
                    )
                )
                return int(result.rowcount or 0)
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError(
                "Unable to reset field semantic index version"
            ) from exc

    def _invalidate_sync(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion | None,
        reason: str,
    ) -> tuple[IndexVersion, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        self._require_text("reason", reason)
        try:
            with self._sessions.begin() as session:
                statement = select(FieldSemanticIndexVersionRow).where(
                    FieldSemanticIndexVersionRow.tenant_id == tenant_id,
                    FieldSemanticIndexVersionRow.schema_version == schema_version,
                )
                if catalog_version is not None:
                    statement = statement.where(
                        FieldSemanticIndexVersionRow.catalog_version
                        == catalog_version.value
                    )
                if self._dialect_name == "postgresql":
                    statement = statement.with_for_update()
                rows = session.scalars(statement).all()
                now = self._now()
                valid_ids = tuple(
                    row.index_version_id for row in rows if row.is_valid
                )
                if valid_ids:
                    session.execute(
                        update(FieldSemanticIndexProjectionRow)
                        .where(
                            FieldSemanticIndexProjectionRow.tenant_id == tenant_id,
                            FieldSemanticIndexProjectionRow.index_version_id.in_(
                                valid_ids
                            ),
                            FieldSemanticIndexProjectionRow.status
                            != IndexProjectionStatus.INVALIDATED.value,
                        )
                        .values(
                            status=IndexProjectionStatus.INVALIDATED.value,
                            processing_started_at=None,
                            worker_id=None, lease_token=None, lease_expires_at=None,
                            invalidated_at=now,
                            updated_at=now,
                        )
                    )
                for row in rows:
                    if not row.is_valid:
                        continue
                    row.is_active = False
                    row.is_valid = False
                    row.invalidated_reason = reason
                    row.invalidated_at = now
                    if row.retired_at is None:
                        row.retired_at = now
                return tuple(IndexVersion(row.version) for row in rows)
        except WorkflowPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError(
                "Unable to invalidate field semantic index versions"
            ) from exc

    def _delete_tenant_sync(self, tenant_id: str) -> tuple[IndexVersion, ...]:
        self._require_text("tenant_id", tenant_id)
        try:
            with self._sessions.begin() as session:
                versions = tuple(
                    IndexVersion(value)
                    for value in session.scalars(
                        select(FieldSemanticIndexVersionRow.version).where(
                            FieldSemanticIndexVersionRow.tenant_id == tenant_id
                        )
                    ).all()
                )
                session.execute(
                    delete(FieldSemanticIndexVersionRow).where(
                        FieldSemanticIndexVersionRow.tenant_id == tenant_id
                    )
                )
                return versions
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to delete tenant field semantic index state"
            ) from exc

    def _version_record(
        self,
        session: Session,
        row: FieldSemanticIndexVersionRow,
    ) -> FieldSemanticIndexVersionRecord:
        dense = session.get(ModelVersionRow, row.dense_model_version_id)
        sparse = (
            session.get(ModelVersionRow, row.sparse_model_version_id)
            if row.sparse_model_version_id is not None
            else None
        )
        if dense is None:
            raise WorkflowPersistenceError("Field semantic dense model version is missing")
        counts = self._projection_counts(session, row.index_version_id)
        return FieldSemanticIndexVersionRecord(
            tenant_id=row.tenant_id,
            index_version=IndexVersion(row.version),
            schema_version=row.schema_version,
            catalog_version=FieldSemanticCatalogVersion(row.catalog_version),
            dense_model_version=ModelVersion(dense.version),
            sparse_model_version=ModelVersion(sparse.version) if sparse else None,
            is_active=row.is_active,
            is_valid=row.is_valid,
            pending_count=counts.get(IndexProjectionStatus.PENDING.value, 0),
            processing_count=counts.get(IndexProjectionStatus.PROCESSING.value, 0),
            indexed_count=counts.get(IndexProjectionStatus.INDEXED.value, 0),
            failed_count=counts.get(IndexProjectionStatus.FAILED.value, 0),
            invalidated_count=counts.get(IndexProjectionStatus.INVALIDATED.value, 0),
            created_at=self._aware(row.created_at),
            activated_at=self._optional_aware(row.activated_at),
            retired_at=self._optional_aware(row.retired_at),
            invalidated_at=self._optional_aware(row.invalidated_at),
            invalidated_reason=row.invalidated_reason,
        )

    def _get_version_row(
        self,
        session: Session,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> FieldSemanticIndexVersionRow:
        row = session.scalar(
            select(FieldSemanticIndexVersionRow).where(
                FieldSemanticIndexVersionRow.tenant_id == tenant_id,
                FieldSemanticIndexVersionRow.version == index_version.value,
            )
        )
        if row is None:
            raise WorkflowPersistenceError("Field semantic index version does not exist")
        if not row.is_valid:
            raise WorkflowPersistenceError("Field semantic index version is invalid")
        return row

    def _get_projection(
        self,
        session: Session,
        tenant_id: str,
        semantic_id: str,
        index_version: IndexVersion,
    ) -> FieldSemanticIndexProjectionRow:
        version = self._get_version_row(session, tenant_id, index_version)
        row = session.scalar(
            select(FieldSemanticIndexProjectionRow).where(
                FieldSemanticIndexProjectionRow.tenant_id == tenant_id,
                FieldSemanticIndexProjectionRow.semantic_id == semantic_id,
                FieldSemanticIndexProjectionRow.index_version_id
                == version.index_version_id,
            )
        )
        if row is None:
            raise WorkflowPersistenceError("Field semantic projection does not exist")
        return row

    @staticmethod
    def _projection_counts(session: Session, version_id: str) -> dict[str, int]:
        rows = session.execute(
            select(
                FieldSemanticIndexProjectionRow.status,
                func.count(),
            )
            .where(FieldSemanticIndexProjectionRow.index_version_id == version_id)
            .group_by(FieldSemanticIndexProjectionRow.status)
        ).all()
        return {str(status): int(count) for status, count in rows}

    @staticmethod
    def _task_from_row(
        row: FieldSemanticIndexProjectionRow,
        version: FieldSemanticIndexVersionRow,
    ) -> FieldSemanticProjectionTask:
        return FieldSemanticProjectionTask(
            projection_id=row.projection_id,
            semantic_id=row.semantic_id,
            tenant_id=row.tenant_id,
            document_type=row.document_type,
            canonical_field_path=row.canonical_field_path,
            schema_version=row.schema_version,
            catalog_version=FieldSemanticCatalogVersion(row.catalog_version),
            index_version=IndexVersion(version.version),
            display_name=row.display_name,
            description=row.description,
            value_type=row.value_type,
            approved_aliases=SQLAlchemyFieldSemanticProjectionRepository._string_list(
                "approved_aliases_json",
                row.approved_aliases_json,
            ),
            negative_aliases=SQLAlchemyFieldSemanticProjectionRepository._string_list(
                "negative_aliases_json",
                row.negative_aliases_json,
            ),
            context_anchors=SQLAlchemyFieldSemanticProjectionRepository._string_list(
                "context_anchors_json",
                row.context_anchors_json,
            ),
            source_fingerprint=row.source_fingerprint,
            status=IndexProjectionStatus(row.status),
            attempt_count=row.attempt_count,
        )

    @staticmethod
    def _validate_task(
        task: FieldSemanticProjectionTask,
        version: FieldSemanticIndexVersionRow,
    ) -> None:
        if task.status is not IndexProjectionStatus.PENDING or task.attempt_count != 0:
            raise ValueError("New field semantic tasks must be unattempted and pending")
        if (
            task.tenant_id != version.tenant_id
            or task.index_version.value != version.version
            or task.schema_version != version.schema_version
            or task.catalog_version.value != version.catalog_version
        ):
            raise ValueError("Field semantic task is outside its index version scope")

    @staticmethod
    def _version_definition(row: FieldSemanticIndexVersionRow) -> tuple[object, ...]:
        return (
            row.tenant_id,
            row.version,
            row.schema_version,
            row.catalog_version,
            row.dense_model_version_id,
            row.sparse_model_version_id,
        )

    @staticmethod
    def _projection_definition(
        row: FieldSemanticIndexProjectionRow,
    ) -> tuple[object, ...]:
        return (
            row.tenant_id,
            row.semantic_id,
            row.index_version_id,
            row.document_type,
            row.canonical_field_path,
            row.schema_version,
            row.catalog_version,
            row.display_name,
            row.description,
            row.value_type,
            SQLAlchemyFieldSemanticProjectionRepository._string_list(
                "approved_aliases_json",
                row.approved_aliases_json,
            ),
            SQLAlchemyFieldSemanticProjectionRepository._string_list(
                "negative_aliases_json",
                row.negative_aliases_json,
            ),
            SQLAlchemyFieldSemanticProjectionRepository._string_list(
                "context_anchors_json",
                row.context_anchors_json,
            ),
            row.source_fingerprint,
        )

    @staticmethod
    def _string_list(name: str, value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise WorkflowPersistenceError(
                f"Field semantic projection {name} has an invalid shape"
            )
        return tuple(value)

    def _ensure_model_version(
        self,
        session: Session,
        tenant_id: str,
        version: ModelVersion,
    ) -> str:
        row_id = sha256(
            f"model\0{tenant_id}\0{version.value}".encode()
        ).hexdigest()
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

    @staticmethod
    def _version_id(tenant_id: str, index_version: IndexVersion) -> str:
        return sha256(
            f"field-semantic-index\0{tenant_id}\0{index_version.value}".encode()
        ).hexdigest()

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
    def _require_text(name: str, value: str) -> None:
        if not value.strip() or value != value.strip():
            raise ValueError(f"{name} must be non-empty and normalized")

    @staticmethod
    def _require_aware(name: str, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @classmethod
    def _optional_aware(cls, value: datetime | None) -> datetime | None:
        return cls._aware(value) if value is not None else None
