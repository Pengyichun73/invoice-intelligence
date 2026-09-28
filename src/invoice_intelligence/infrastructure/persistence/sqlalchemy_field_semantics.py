"""SQLAlchemy adapter for tenant-scoped field semantic catalog facts."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from sqlalchemy import Engine, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import (
    IdempotencyConflictError,
    ResourceConflictError,
    ResourceNotFoundError,
    WorkflowPersistenceError,
)
from invoice_intelligence.domain.field_semantics import (
    FieldAlias,
    FieldAliasCandidate,
    FieldAliasCandidateDecision,
    FieldAliasCandidateDecisionAuthority,
    FieldAliasCandidateScope,
    FieldAliasCandidateSupport,
    FieldAliasStatus,
    FieldAliasSupportSummary,
    FieldContextAnchor,
    FieldContextRelation,
    FieldSemanticCatalogVersion,
    GlobalFieldAliasSupportSnapshot,
)
from invoice_intelligence.domain.governance import GovernanceAction, GovernanceAuditEvent
from invoice_intelligence.infrastructure.persistence.sqlalchemy_governance import (
    add_governance_audit,
    require_governance_audit,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    FieldAliasCandidateDecisionRow,
    FieldAliasCandidateRow,
    FieldAliasCandidateSourceRow,
    FieldAliasGlobalSupportSnapshotRow,
    FieldSemanticAliasDecisionRow,
    FieldSemanticAliasRow,
    FieldSemanticCatalogVersionRow,
    FieldSemanticContextAnchorRow,
    MemoryConflictFieldAliasCandidateRow,
    MemoryConflictRow,
)


class SQLAlchemyFieldAliasRepository:
    """Persist catalog versions, alias state, and append-only human decisions."""

    _ALLOWED_TRANSITIONS = {
        FieldAliasStatus.PENDING: frozenset(
            {
                FieldAliasStatus.APPROVED,
                FieldAliasStatus.REJECTED,
                FieldAliasStatus.INVALIDATED,
            }
        ),
        FieldAliasStatus.APPROVED: frozenset(
            {FieldAliasStatus.SUSPENDED, FieldAliasStatus.INVALIDATED}
        ),
        FieldAliasStatus.SUSPENDED: frozenset(
            {FieldAliasStatus.APPROVED, FieldAliasStatus.INVALIDATED}
        ),
        FieldAliasStatus.REJECTED: frozenset({FieldAliasStatus.INVALIDATED}),
        FieldAliasStatus.INVALIDATED: frozenset(),
    }

    def __init__(self, engine: Engine) -> None:
        self._dialect_name = engine.dialect.name
        self._sessions = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        )

    async def register_catalog_version(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        created_by: str,
        created_at: datetime,
    ) -> FieldSemanticCatalogVersion:
        return await asyncio.to_thread(
            self._register_catalog_version_sync,
            tenant_id,
            schema_version,
            catalog_version,
            created_by,
            created_at,
        )

    async def activate_catalog_version(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        approved_by: str,
        reason: str,
        idempotency_key: str,
        approved_at: datetime,
    ) -> FieldSemanticCatalogVersion:
        return await asyncio.to_thread(
            self._activate_catalog_version_sync,
            tenant_id,
            schema_version,
            catalog_version,
            approved_by,
            reason,
            idempotency_key,
            approved_at,
        )

    async def get_active_catalog_version(
        self,
        tenant_id: str,
        schema_version: str,
    ) -> FieldSemanticCatalogVersion | None:
        return await asyncio.to_thread(
            self._get_active_catalog_version_sync,
            tenant_id,
            schema_version,
        )

    async def invalidate_catalog_version(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        reason: str,
        invalidated_at: datetime,
    ) -> bool:
        return await asyncio.to_thread(
            self._invalidate_catalog_version_sync,
            tenant_id,
            schema_version,
            catalog_version,
            reason,
            invalidated_at,
        )

    async def is_catalog_version_valid(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
    ) -> bool:
        return await asyncio.to_thread(
            self._is_catalog_version_valid_sync,
            tenant_id,
            schema_version,
            catalog_version,
        )

    async def is_catalog_version_published(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
    ) -> bool:
        return await asyncio.to_thread(
            self._is_catalog_version_published_sync,
            tenant_id,
            schema_version,
            catalog_version,
        )

    async def submit_alias(self, alias: FieldAlias) -> FieldAlias:
        return await asyncio.to_thread(self._submit_alias_sync, alias)

    async def get_alias(self, tenant_id: str, alias_id: str) -> FieldAlias | None:
        return await asyncio.to_thread(self._get_alias_sync, tenant_id, alias_id)

    async def decide_alias(
        self,
        tenant_id: str,
        alias_id: str,
        target_status: FieldAliasStatus,
        reviewer_id: str,
        reason: str,
        idempotency_key: str,
        decided_at: datetime,
    ) -> FieldAlias:
        return await asyncio.to_thread(
            self._decide_alias_sync,
            tenant_id,
            alias_id,
            target_status,
            reviewer_id,
            reason,
            idempotency_key,
            decided_at,
        )

    async def list_approved_aliases(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        *,
        document_type: str | None = None,
        canonical_field_paths: Sequence[str] = (),
    ) -> tuple[FieldAlias, ...]:
        return await asyncio.to_thread(
            self._list_approved_aliases_sync,
            tenant_id,
            schema_version,
            catalog_version,
            document_type,
            tuple(canonical_field_paths),
        )

    def _register_catalog_version_sync(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        created_by: str,
        created_at: datetime,
    ) -> FieldSemanticCatalogVersion:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        self._require_text("created_by", created_by)
        self._require_aware("created_at", created_at)
        version_id = self._catalog_version_id(
            tenant_id,
            schema_version,
            catalog_version.value,
        )
        values = {
            "catalog_version_id": version_id,
            "tenant_id": tenant_id,
            "schema_version": schema_version,
            "catalog_version": catalog_version.value,
            "is_active": False,
            "is_valid": True,
            "created_by": created_by,
            "created_at": created_at,
            "activated_by": None,
            "activation_reason": None,
            "activation_idempotency_key_hash": None,
            "activated_at": None,
            "retired_at": None,
            "invalidated_at": None,
            "invalidated_reason": None,
        }
        try:
            with self._sessions.begin() as session:
                self._insert_do_nothing(
                    session,
                    FieldSemanticCatalogVersionRow,
                    values,
                )
                row = session.scalar(
                    select(FieldSemanticCatalogVersionRow).where(
                        FieldSemanticCatalogVersionRow.tenant_id == tenant_id,
                        FieldSemanticCatalogVersionRow.schema_version == schema_version,
                        FieldSemanticCatalogVersionRow.catalog_version
                        == catalog_version.value,
                    )
                )
                if row is None:
                    raise WorkflowPersistenceError("Catalog version was not persisted")
                if (
                    row.catalog_version_id != version_id
                    or row.created_by != created_by
                ):
                    raise WorkflowPersistenceError(
                        "Catalog version identity is bound to different metadata"
                    )
                return FieldSemanticCatalogVersion(row.catalog_version)
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to register catalog version") from exc

    def _activate_catalog_version_sync(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        approved_by: str,
        reason: str,
        idempotency_key: str,
        approved_at: datetime,
    ) -> FieldSemanticCatalogVersion:
        for name, value in (
            ("tenant_id", tenant_id),
            ("schema_version", schema_version),
            ("approved_by", approved_by),
            ("reason", reason),
            ("idempotency_key", idempotency_key),
        ):
            self._require_text(name, value)
        self._require_aware("approved_at", approved_at)
        idempotency_hash = self._hash_text(idempotency_key)
        try:
            with self._sessions.begin() as session:
                replay = session.scalar(
                    select(FieldSemanticCatalogVersionRow).where(
                        FieldSemanticCatalogVersionRow.tenant_id == tenant_id,
                        FieldSemanticCatalogVersionRow.activation_idempotency_key_hash
                        == idempotency_hash,
                    )
                )
                if replay is not None:
                    if not self._activation_matches(
                        replay,
                        schema_version,
                        catalog_version,
                        approved_by,
                        reason,
                        approved_at,
                    ):
                        raise WorkflowPersistenceError(
                            "Catalog activation idempotency key is bound to another action"
                        )
                    return FieldSemanticCatalogVersion(replay.catalog_version)

                statement = select(FieldSemanticCatalogVersionRow).where(
                    FieldSemanticCatalogVersionRow.tenant_id == tenant_id,
                    FieldSemanticCatalogVersionRow.schema_version == schema_version,
                    FieldSemanticCatalogVersionRow.catalog_version
                    == catalog_version.value,
                )
                if self._dialect_name == "postgresql":
                    statement = statement.with_for_update()
                target = session.scalar(statement)
                if target is None:
                    raise WorkflowPersistenceError("Catalog version does not exist")
                if not target.is_valid:
                    raise WorkflowPersistenceError("Invalid catalog version cannot be activated")
                if target.is_active:
                    raise WorkflowPersistenceError("Catalog version is already active")
                if target.activated_at is not None:
                    raise WorkflowPersistenceError(
                        "A retired catalog version must be reissued before rollback"
                    )
                pending_aliases = session.scalar(
                    select(func.count())
                    .select_from(FieldSemanticAliasRow)
                    .where(
                        FieldSemanticAliasRow.tenant_id == tenant_id,
                        FieldSemanticAliasRow.catalog_version_id
                        == target.catalog_version_id,
                        FieldSemanticAliasRow.status == FieldAliasStatus.PENDING.value,
                    )
                )
                if pending_aliases:
                    raise WorkflowPersistenceError(
                        "Catalog activation requires every alias review to be resolved"
                    )

                session.execute(
                    update(FieldSemanticCatalogVersionRow)
                    .where(
                        FieldSemanticCatalogVersionRow.tenant_id == tenant_id,
                        FieldSemanticCatalogVersionRow.schema_version == schema_version,
                        FieldSemanticCatalogVersionRow.is_active.is_(True),
                    )
                    .values(is_active=False, retired_at=approved_at)
                )
                target.is_active = True
                target.activated_by = approved_by
                target.activation_reason = reason
                target.activation_idempotency_key_hash = idempotency_hash
                target.activated_at = approved_at
                target.retired_at = None
                session.flush()
                return FieldSemanticCatalogVersion(target.catalog_version)
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to activate catalog version") from exc

    def _get_active_catalog_version_sync(
        self,
        tenant_id: str,
        schema_version: str,
    ) -> FieldSemanticCatalogVersion | None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(FieldSemanticCatalogVersionRow).where(
                        FieldSemanticCatalogVersionRow.tenant_id == tenant_id,
                        FieldSemanticCatalogVersionRow.schema_version == schema_version,
                        FieldSemanticCatalogVersionRow.is_active.is_(True),
                        FieldSemanticCatalogVersionRow.is_valid.is_(True),
                    )
                )
                return (
                    FieldSemanticCatalogVersion(row.catalog_version)
                    if row is not None
                    else None
                )
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read active catalog version") from exc

    def _invalidate_catalog_version_sync(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        reason: str,
        invalidated_at: datetime,
    ) -> bool:
        for name, value in (
            ("tenant_id", tenant_id),
            ("schema_version", schema_version),
            ("reason", reason),
        ):
            self._require_text(name, value)
        self._require_aware("invalidated_at", invalidated_at)
        try:
            with self._sessions.begin() as session:
                statement = select(FieldSemanticCatalogVersionRow).where(
                    FieldSemanticCatalogVersionRow.tenant_id == tenant_id,
                    FieldSemanticCatalogVersionRow.schema_version == schema_version,
                    FieldSemanticCatalogVersionRow.catalog_version
                    == catalog_version.value,
                )
                if self._dialect_name == "postgresql":
                    statement = statement.with_for_update()
                row = session.scalar(statement)
                if row is None:
                    raise WorkflowPersistenceError("Catalog version does not exist")
                if not row.is_valid:
                    if row.invalidated_reason != reason:
                        raise WorkflowPersistenceError(
                            "Catalog version is already invalidated for another reason"
                        )
                    return False
                row.is_active = False
                row.is_valid = False
                row.invalidated_at = invalidated_at
                row.invalidated_reason = reason
                if row.retired_at is None:
                    row.retired_at = invalidated_at
                return True
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to invalidate catalog version") from exc

    def _is_catalog_version_valid_sync(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
    ) -> bool:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        try:
            with self._sessions() as session:
                return session.scalar(
                    select(FieldSemanticCatalogVersionRow.catalog_version_id).where(
                        FieldSemanticCatalogVersionRow.tenant_id == tenant_id,
                        FieldSemanticCatalogVersionRow.schema_version == schema_version,
                        FieldSemanticCatalogVersionRow.catalog_version
                        == catalog_version.value,
                        FieldSemanticCatalogVersionRow.is_valid.is_(True),
                    )
                ) is not None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to inspect catalog version") from exc

    def _is_catalog_version_published_sync(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
    ) -> bool:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        try:
            with self._sessions() as session:
                return session.scalar(
                    select(FieldSemanticCatalogVersionRow.catalog_version_id).where(
                        FieldSemanticCatalogVersionRow.tenant_id == tenant_id,
                        FieldSemanticCatalogVersionRow.schema_version == schema_version,
                        FieldSemanticCatalogVersionRow.catalog_version
                        == catalog_version.value,
                        FieldSemanticCatalogVersionRow.is_valid.is_(True),
                        FieldSemanticCatalogVersionRow.activated_at.is_not(None),
                    )
                ) is not None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to inspect published catalog version"
            ) from exc

    def _submit_alias_sync(self, alias: FieldAlias) -> FieldAlias:
        if alias.status is not FieldAliasStatus.PENDING or not alias.is_valid:
            raise ValueError("New aliases must be pending and valid")
        try:
            with self._sessions.begin() as session:
                existing = session.get(FieldSemanticAliasRow, alias.alias_id)
                if existing is not None:
                    persisted = self._alias_from_row(session, existing)
                    if not self._alias_submission_matches(persisted, alias):
                        raise WorkflowPersistenceError(
                            "Alias identifier is bound to different immutable metadata"
                        )
                    return persisted
                version = self._require_catalog_version(
                    session,
                    alias.tenant_id,
                    alias.schema_version,
                    alias.catalog_version,
                )
                values = {
                    "alias_id": alias.alias_id,
                    "tenant_id": alias.tenant_id,
                    "catalog_version_id": version.catalog_version_id,
                    "schema_version": alias.schema_version,
                    "document_type": alias.document_type,
                    "canonical_field_path": alias.canonical_field_path,
                    "alias_text": alias.alias_text,
                    "normalized_alias": alias.normalized_alias,
                    "is_negative": alias.is_negative,
                    "status": alias.status.value,
                    "submitted_by": alias.submitted_by,
                    "submitted_at": alias.submitted_at,
                    "reviewed_by": None,
                    "reviewed_at": None,
                    "review_reason": None,
                    "is_valid": True,
                    "source_run_id": alias.source_run_id,
                    "source_document_id": alias.source_document_id,
                    "source_evidence_id": alias.source_evidence_id,
                    "source_binding_decision_id": alias.source_binding_decision_id,
                    "submission_reason": alias.submission_reason,
                }
                created = self._insert_do_nothing(
                    session,
                    FieldSemanticAliasRow,
                    values,
                )
                row = session.get(FieldSemanticAliasRow, alias.alias_id)
                if row is None:
                    raise WorkflowPersistenceError(
                        "Equivalent alias already exists under another identifier"
                    )
                if created:
                    for ordinal, anchor in enumerate(alias.context_anchors):
                        session.add(
                            FieldSemanticContextAnchorRow(
                                anchor_id=anchor.anchor_id,
                                tenant_id=alias.tenant_id,
                                alias_id=alias.alias_id,
                                ordinal=ordinal,
                                text=anchor.text,
                                normalized_text=anchor.normalized_text,
                                relation=anchor.relation.value,
                                max_distance=anchor.max_distance,
                                is_negative=anchor.is_negative,
                            )
                        )
                    session.flush()
                persisted = self._alias_from_row(session, row)
                if not self._alias_submission_matches(persisted, alias):
                    raise WorkflowPersistenceError(
                        "Alias identifier is bound to different immutable metadata"
                    )
                return persisted
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to submit field alias") from exc

    def _get_alias_sync(self, tenant_id: str, alias_id: str) -> FieldAlias | None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("alias_id", alias_id)
        try:
            with self._sessions() as session:
                row = session.get(FieldSemanticAliasRow, alias_id)
                if row is None or row.tenant_id != tenant_id:
                    return None
                return self._alias_from_row(session, row)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read field alias") from exc

    def _decide_alias_sync(
        self,
        tenant_id: str,
        alias_id: str,
        target_status: FieldAliasStatus,
        reviewer_id: str,
        reason: str,
        idempotency_key: str,
        decided_at: datetime,
    ) -> FieldAlias:
        for name, value in (
            ("tenant_id", tenant_id),
            ("alias_id", alias_id),
            ("reviewer_id", reviewer_id),
            ("reason", reason),
            ("idempotency_key", idempotency_key),
        ):
            self._require_text(name, value)
        self._require_aware("decided_at", decided_at)
        if target_status is FieldAliasStatus.PENDING:
            raise ValueError("Alias decisions cannot transition to pending")
        idempotency_hash = self._hash_text(idempotency_key)
        decision_id = self._hash_text(
            f"field-alias-decision\0{tenant_id}\0{idempotency_hash}"
        )
        try:
            with self._sessions.begin() as session:
                replay = session.scalar(
                    select(FieldSemanticAliasDecisionRow).where(
                        FieldSemanticAliasDecisionRow.tenant_id == tenant_id,
                        FieldSemanticAliasDecisionRow.idempotency_key_hash
                        == idempotency_hash,
                    )
                )
                if replay is not None:
                    if (
                        replay.alias_id != alias_id
                        or replay.status != target_status.value
                        or replay.reviewer_id != reviewer_id
                        or replay.reason != reason
                        or self._aware(replay.decided_at) != decided_at
                    ):
                        raise WorkflowPersistenceError(
                            "Alias decision idempotency key is bound to another action"
                        )
                    current = session.get(FieldSemanticAliasRow, alias_id)
                    if current is None or current.status != replay.status:
                        raise WorkflowPersistenceError(
                            "Alias decision replay has already been superseded"
                        )
                    return self._alias_from_row(session, current)

                statement = select(FieldSemanticAliasRow).where(
                    FieldSemanticAliasRow.tenant_id == tenant_id,
                    FieldSemanticAliasRow.alias_id == alias_id,
                )
                if self._dialect_name == "postgresql":
                    statement = statement.with_for_update()
                row = session.scalar(statement)
                if row is None:
                    raise WorkflowPersistenceError("Field alias does not exist")
                current_status = FieldAliasStatus(row.status)
                if target_status not in self._ALLOWED_TRANSITIONS[current_status]:
                    raise WorkflowPersistenceError("Invalid field alias status transition")
                version = session.get(
                    FieldSemanticCatalogVersionRow,
                    row.catalog_version_id,
                )
                if version is None or version.tenant_id != tenant_id:
                    raise WorkflowPersistenceError("Field alias catalog scope is invalid")
                if (
                    current_status is FieldAliasStatus.PENDING
                    and target_status is FieldAliasStatus.APPROVED
                    and version.activated_at is not None
                ):
                    raise WorkflowPersistenceError(
                        "Pending aliases must be approved before catalog activation"
                    )
                if not row.is_valid and target_status is not FieldAliasStatus.INVALIDATED:
                    raise WorkflowPersistenceError("Invalid field alias cannot be reviewed")
                revision = int(
                    session.scalar(
                        select(func.max(FieldSemanticAliasDecisionRow.revision)).where(
                            FieldSemanticAliasDecisionRow.tenant_id == tenant_id,
                            FieldSemanticAliasDecisionRow.alias_id == alias_id,
                        )
                    )
                    or 0
                ) + 1
                session.add(
                    FieldSemanticAliasDecisionRow(
                        decision_id=decision_id,
                        tenant_id=tenant_id,
                        alias_id=alias_id,
                        previous_status=current_status.value,
                        status=target_status.value,
                        reviewer_id=reviewer_id,
                        reason=reason,
                        idempotency_key_hash=idempotency_hash,
                        revision=revision,
                        decided_at=decided_at,
                    )
                )
                row.status = target_status.value
                row.reviewed_by = reviewer_id
                row.reviewed_at = decided_at
                row.review_reason = reason
                row.is_valid = target_status is not FieldAliasStatus.INVALIDATED
                session.flush()
                return self._alias_from_row(session, row)
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to decide field alias") from exc

    def _list_approved_aliases_sync(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        document_type: str | None,
        canonical_field_paths: tuple[str, ...],
    ) -> tuple[FieldAlias, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        if document_type is not None:
            self._require_text("document_type", document_type)
        for field_path in canonical_field_paths:
            self._require_text("canonical_field_path", field_path)
        if len(canonical_field_paths) != len(set(canonical_field_paths)):
            raise ValueError("canonical_field_paths must be unique")
        try:
            with self._sessions() as session:
                version = session.scalar(
                    select(FieldSemanticCatalogVersionRow).where(
                        FieldSemanticCatalogVersionRow.tenant_id == tenant_id,
                        FieldSemanticCatalogVersionRow.schema_version == schema_version,
                        FieldSemanticCatalogVersionRow.catalog_version
                        == catalog_version.value,
                        FieldSemanticCatalogVersionRow.is_valid.is_(True),
                    )
                )
                if version is None:
                    return ()
                statement = select(FieldSemanticAliasRow).where(
                    FieldSemanticAliasRow.tenant_id == tenant_id,
                    FieldSemanticAliasRow.catalog_version_id
                    == version.catalog_version_id,
                    FieldSemanticAliasRow.schema_version == schema_version,
                    FieldSemanticAliasRow.status == FieldAliasStatus.APPROVED.value,
                    FieldSemanticAliasRow.is_valid.is_(True),
                )
                if document_type is not None:
                    statement = statement.where(
                        FieldSemanticAliasRow.document_type == document_type
                    )
                if canonical_field_paths:
                    statement = statement.where(
                        FieldSemanticAliasRow.canonical_field_path.in_(
                            canonical_field_paths
                        )
                    )
                rows = session.scalars(
                    statement.order_by(
                        FieldSemanticAliasRow.document_type,
                        FieldSemanticAliasRow.canonical_field_path,
                        FieldSemanticAliasRow.normalized_alias,
                        FieldSemanticAliasRow.alias_id,
                    )
                ).all()
                return tuple(self._alias_from_row(session, row) for row in rows)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to list approved field aliases") from exc

    def _require_catalog_version(
        self,
        session: Session,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
    ) -> FieldSemanticCatalogVersionRow:
        row = session.scalar(
            select(FieldSemanticCatalogVersionRow).where(
                FieldSemanticCatalogVersionRow.tenant_id == tenant_id,
                FieldSemanticCatalogVersionRow.schema_version == schema_version,
                FieldSemanticCatalogVersionRow.catalog_version == catalog_version.value,
            )
        )
        if row is None:
            raise WorkflowPersistenceError("Field alias requires a registered catalog version")
        if not row.is_valid:
            raise WorkflowPersistenceError("Field alias catalog version is invalid")
        if row.activated_at is not None:
            raise WorkflowPersistenceError(
                "New field aliases require a never-activated draft catalog version"
            )
        return row

    def _alias_from_row(
        self,
        session: Session,
        row: FieldSemanticAliasRow,
    ) -> FieldAlias:
        version = session.get(
            FieldSemanticCatalogVersionRow,
            row.catalog_version_id,
        )
        if (
            version is None
            or version.tenant_id != row.tenant_id
            or version.schema_version != row.schema_version
        ):
            raise WorkflowPersistenceError("Persisted alias catalog scope is invalid")
        anchor_rows = session.scalars(
            select(FieldSemanticContextAnchorRow)
            .where(
                FieldSemanticContextAnchorRow.tenant_id == row.tenant_id,
                FieldSemanticContextAnchorRow.alias_id == row.alias_id,
            )
            .order_by(FieldSemanticContextAnchorRow.ordinal)
        ).all()
        anchors = tuple(
            FieldContextAnchor(
                anchor_id=anchor.anchor_id,
                text=anchor.text,
                normalized_text=anchor.normalized_text,
                relation=FieldContextRelation(anchor.relation),
                max_distance=anchor.max_distance,
                is_negative=anchor.is_negative,
            )
            for anchor in anchor_rows
        )
        return FieldAlias(
            alias_id=row.alias_id,
            tenant_id=row.tenant_id,
            schema_version=row.schema_version,
            document_type=row.document_type,
            canonical_field_path=row.canonical_field_path,
            alias_text=row.alias_text,
            normalized_alias=row.normalized_alias,
            is_negative=row.is_negative,
            context_anchors=anchors,
            catalog_version=FieldSemanticCatalogVersion(version.catalog_version),
            status=FieldAliasStatus(row.status),
            submitted_by=row.submitted_by,
            submitted_at=self._aware(row.submitted_at),
            reviewed_by=row.reviewed_by,
            reviewed_at=(
                self._aware(row.reviewed_at) if row.reviewed_at is not None else None
            ),
            review_reason=row.review_reason,
            is_valid=row.is_valid,
            source_run_id=row.source_run_id,
            source_document_id=row.source_document_id,
            source_evidence_id=row.source_evidence_id,
            source_binding_decision_id=row.source_binding_decision_id,
            submission_reason=row.submission_reason,
        )

    @staticmethod
    def _alias_submission_matches(persisted: FieldAlias, submitted: FieldAlias) -> bool:
        """Compare immutable submission identity while allowing later review transitions."""

        return (
            persisted.alias_id == submitted.alias_id
            and persisted.tenant_id == submitted.tenant_id
            and persisted.schema_version == submitted.schema_version
            and persisted.document_type == submitted.document_type
            and persisted.canonical_field_path == submitted.canonical_field_path
            and persisted.alias_text == submitted.alias_text
            and persisted.normalized_alias == submitted.normalized_alias
            and persisted.is_negative == submitted.is_negative
            and persisted.context_anchors == submitted.context_anchors
            and persisted.catalog_version == submitted.catalog_version
            and persisted.submitted_by == submitted.submitted_by
            and persisted.source_run_id == submitted.source_run_id
            and persisted.source_document_id == submitted.source_document_id
            and persisted.source_evidence_id == submitted.source_evidence_id
            and persisted.source_binding_decision_id
            == submitted.source_binding_decision_id
            and persisted.submission_reason == submitted.submission_reason
        )

    @staticmethod
    def _activation_matches(
        row: FieldSemanticCatalogVersionRow,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        approved_by: str,
        reason: str,
        approved_at: datetime,
    ) -> bool:
        activated_at = row.activated_at
        return (
            row.schema_version == schema_version
            and row.catalog_version == catalog_version.value
            and row.activated_by == approved_by
            and row.activation_reason == reason
            and activated_at is not None
            and SQLAlchemyFieldAliasRepository._aware(activated_at) == approved_at
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
        return session.execute(statement).rowcount == 1

    @staticmethod
    def _catalog_version_id(
        tenant_id: str,
        schema_version: str,
        catalog_version: str,
    ) -> str:
        return SQLAlchemyFieldAliasRepository._hash_text(
            f"field-semantic-catalog\0{tenant_id}\0{schema_version}\0{catalog_version}"
        )

    @staticmethod
    def _hash_text(value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()

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


class SQLAlchemyFieldAliasCandidateRepository:
    """Persist governed alias candidates and replay-safe support facts."""

    _ALLOWED_TRANSITIONS = {
        FieldAliasStatus.PENDING: frozenset(
            {
                FieldAliasStatus.APPROVED,
                FieldAliasStatus.REJECTED,
                FieldAliasStatus.INVALIDATED,
            }
        ),
        FieldAliasStatus.APPROVED: frozenset(
            {FieldAliasStatus.SUSPENDED, FieldAliasStatus.INVALIDATED}
        ),
        FieldAliasStatus.SUSPENDED: frozenset(
            {FieldAliasStatus.APPROVED, FieldAliasStatus.INVALIDATED}
        ),
        FieldAliasStatus.REJECTED: frozenset({FieldAliasStatus.INVALIDATED}),
        FieldAliasStatus.INVALIDATED: frozenset(),
    }

    def __init__(self, engine: Engine) -> None:
        self._dialect_name = engine.dialect.name
        self._sessions = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        )

    async def save_pending(
        self,
        candidate: FieldAliasCandidate,
        support: FieldAliasCandidateSupport | None,
        global_support: GlobalFieldAliasSupportSnapshot | None = None,
    ) -> FieldAliasCandidate:
        return await asyncio.to_thread(
            self._save_pending_sync,
            candidate,
            support,
            global_support,
        )

    async def get(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> FieldAliasCandidate | None:
        return await asyncio.to_thread(self._get_sync, tenant_id, candidate_id)

    async def get_global(self, candidate_id: str) -> FieldAliasCandidate | None:
        return await asyncio.to_thread(self._get_global_sync, candidate_id)

    async def list_by_ids_unscoped(
        self,
        candidate_ids: Sequence[str],
    ) -> tuple[FieldAliasCandidate, ...]:
        return await asyncio.to_thread(
            self._list_by_ids_unscoped_sync,
            tuple(candidate_ids),
        )

    async def list_competing(
        self,
        tenant_id: str,
        schema_version: str,
        document_type: str,
        normalized_alias: str,
    ) -> tuple[FieldAliasCandidate, ...]:
        return await asyncio.to_thread(
            self._list_competing_sync,
            tenant_id,
            schema_version,
            document_type,
            normalized_alias,
        )

    async def list_for_governance(
        self,
        tenant_id: str,
        statuses: Sequence[FieldAliasStatus],
        *,
        schema_version: str,
        document_type: str | None,
        canonical_field_path: str | None,
        limit: int,
        after_candidate_id: str | None = None,
    ) -> tuple[FieldAliasCandidate, ...]:
        return await asyncio.to_thread(
            self._list_for_governance_sync,
            tenant_id,
            tuple(statuses),
            schema_version,
            document_type,
            canonical_field_path,
            limit,
            after_candidate_id,
        )

    async def list_source_reviewer_ids(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> tuple[str, ...]:
        return await asyncio.to_thread(
            self._list_source_reviewer_ids_sync,
            tenant_id,
            candidate_id,
        )

    async def summarize_support(
        self,
        tenant_id: str,
        candidate_id: str,
        as_of: datetime,
    ) -> FieldAliasSupportSummary:
        return await asyncio.to_thread(
            self._summarize_support_sync,
            tenant_id,
            candidate_id,
            as_of,
        )

    async def get_global_support(
        self,
        candidate_id: str,
    ) -> GlobalFieldAliasSupportSnapshot | None:
        return await asyncio.to_thread(self._get_global_support_sync, candidate_id)

    async def decide(
        self,
        decision: FieldAliasCandidateDecision,
        *,
        tenant_id: str | None,
        expected_revision: int,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> FieldAliasCandidate:
        return await asyncio.to_thread(
            self._decide_sync,
            decision,
            tenant_id,
            expected_revision,
            audit_event,
        )

    async def get_decision_by_idempotency_hash(
        self,
        tenant_id: str | None,
        candidate_id: str,
        idempotency_key_hash: str,
    ) -> FieldAliasCandidateDecision | None:
        return await asyncio.to_thread(
            self._get_decision_by_idempotency_hash_sync,
            tenant_id,
            candidate_id,
            idempotency_key_hash,
        )

    async def delete_tenant(self, tenant_id: str) -> int:
        return await asyncio.to_thread(self._delete_tenant_sync, tenant_id)

    def _save_pending_sync(
        self,
        candidate: FieldAliasCandidate,
        support: FieldAliasCandidateSupport | None,
        global_support: GlobalFieldAliasSupportSnapshot | None,
    ) -> FieldAliasCandidate:
        if candidate.status is not FieldAliasStatus.PENDING:
            raise ValueError("New field alias candidates must be pending")
        if candidate.scope is FieldAliasCandidateScope.TENANT:
            if (
                support is None
                or global_support is not None
                or support.candidate_id != candidate.candidate_id
                or support.tenant_id != candidate.tenant_id
            ):
                raise ValueError("Tenant candidates require one matching support fact")
        elif support is not None or (
            global_support is None
            or global_support.candidate_id != candidate.candidate_id
        ):
            raise ValueError("Global candidates require one aggregate support snapshot")
        values = {
            "candidate_id": candidate.candidate_id,
            "scope": candidate.scope.value,
            "tenant_id": candidate.tenant_id,
            "schema_version": candidate.schema_version,
            "document_type": candidate.document_type,
            "canonical_field_path": candidate.canonical_field_path,
            "alias_text": candidate.alias_text,
            "normalized_alias": candidate.normalized_alias,
            "policy_version": candidate.policy_version,
            "context_anchors_json": self._anchors_payload(candidate.context_anchors),
            "status": candidate.status.value,
            "canonical_collision": candidate.canonical_collision,
            "support_window_days": candidate.support_window_days,
            "revision": candidate.revision,
            "submitted_at": candidate.submitted_at,
            "updated_at": candidate.updated_at,
            "reviewed_by": None,
            "reviewed_at": None,
            "review_reason": None,
            "promoted_catalog_version": None,
        }
        try:
            with self._sessions.begin() as session:
                candidate_created = self._insert_do_nothing(
                    session,
                    FieldAliasCandidateRow,
                    values,
                )
                row = session.get(FieldAliasCandidateRow, candidate.candidate_id)
                if row is None or not self._candidate_identity_matches(row, candidate):
                    raise WorkflowPersistenceError(
                        "Alias candidate identifier is bound to different metadata"
                    )
                support_created = False
                if support is not None:
                    support_created = self._save_support(session, support)
                if global_support is not None:
                    self._save_global_support(session, global_support)
                if support_created and not candidate_created:
                    session.execute(
                        update(FieldAliasCandidateRow)
                        .where(
                            FieldAliasCandidateRow.candidate_id
                            == candidate.candidate_id
                        )
                        .values(
                            revision=FieldAliasCandidateRow.revision + 1,
                            updated_at=candidate.updated_at,
                        )
                    )
                    session.expire(row)
                session.flush()
                return self._candidate_from_row(row)
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to persist field alias candidate") from exc

    def _get_sync(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> FieldAliasCandidate | None:
        self._require_text("tenant_id", tenant_id)
        self._require_text("candidate_id", candidate_id)
        try:
            with self._sessions() as session:
                row = session.get(FieldAliasCandidateRow, candidate_id)
                if (
                    row is None
                    or row.scope != FieldAliasCandidateScope.TENANT.value
                    or row.tenant_id != tenant_id
                ):
                    return None
                return self._candidate_from_row(row)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read field alias candidate") from exc

    def _get_global_sync(self, candidate_id: str) -> FieldAliasCandidate | None:
        self._require_text("candidate_id", candidate_id)
        try:
            with self._sessions() as session:
                row = session.get(FieldAliasCandidateRow, candidate_id)
                if row is None or row.scope != FieldAliasCandidateScope.GLOBAL.value:
                    return None
                return self._candidate_from_row(row)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read global alias candidate") from exc

    def _list_by_ids_unscoped_sync(
        self,
        candidate_ids: tuple[str, ...],
    ) -> tuple[FieldAliasCandidate, ...]:
        for candidate_id in candidate_ids:
            self._require_text("candidate_id", candidate_id)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate_ids must be unique")
        if not candidate_ids:
            return ()
        try:
            with self._sessions() as session:
                rows = session.scalars(
                    select(FieldAliasCandidateRow).where(
                        FieldAliasCandidateRow.candidate_id.in_(candidate_ids)
                    )
                ).all()
                by_id = {row.candidate_id: self._candidate_from_row(row) for row in rows}
                return tuple(by_id[item] for item in candidate_ids if item in by_id)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read alias candidate sources") from exc

    def _list_competing_sync(
        self,
        tenant_id: str,
        schema_version: str,
        document_type: str,
        normalized_alias: str,
    ) -> tuple[FieldAliasCandidate, ...]:
        for name, value in (
            ("tenant_id", tenant_id),
            ("schema_version", schema_version),
            ("document_type", document_type),
            ("normalized_alias", normalized_alias),
        ):
            self._require_text(name, value)
        try:
            with self._sessions() as session:
                rows = session.scalars(
                    select(FieldAliasCandidateRow)
                    .where(
                        FieldAliasCandidateRow.scope
                        == FieldAliasCandidateScope.TENANT.value,
                        FieldAliasCandidateRow.tenant_id == tenant_id,
                        FieldAliasCandidateRow.schema_version == schema_version,
                        FieldAliasCandidateRow.document_type == document_type,
                        FieldAliasCandidateRow.normalized_alias == normalized_alias,
                        FieldAliasCandidateRow.status.in_(
                            (
                                FieldAliasStatus.PENDING.value,
                                FieldAliasStatus.APPROVED.value,
                                FieldAliasStatus.SUSPENDED.value,
                            )
                        ),
                    )
                    .order_by(
                        FieldAliasCandidateRow.canonical_field_path,
                        FieldAliasCandidateRow.candidate_id,
                    )
                ).all()
                return tuple(self._candidate_from_row(row) for row in rows)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to list competing alias candidates") from exc

    def _list_for_governance_sync(
        self,
        tenant_id: str,
        statuses: tuple[FieldAliasStatus, ...],
        schema_version: str,
        document_type: str | None,
        canonical_field_path: str | None,
        limit: int,
        after_candidate_id: str | None,
    ) -> tuple[FieldAliasCandidate, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("schema_version", schema_version)
        if not statuses:
            raise ValueError("At least one field alias status is required")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        for name, value in (
            ("document_type", document_type),
            ("canonical_field_path", canonical_field_path),
            ("after_candidate_id", after_candidate_id),
        ):
            if value is not None:
                self._require_text(name, value)
        try:
            with self._sessions() as session:
                statement = select(FieldAliasCandidateRow).where(
                    FieldAliasCandidateRow.scope
                    == FieldAliasCandidateScope.TENANT.value,
                    FieldAliasCandidateRow.tenant_id == tenant_id,
                    FieldAliasCandidateRow.schema_version == schema_version,
                    FieldAliasCandidateRow.status.in_(
                        tuple(status.value for status in statuses)
                    ),
                )
                if document_type is not None:
                    statement = statement.where(
                        FieldAliasCandidateRow.document_type == document_type
                    )
                if canonical_field_path is not None:
                    statement = statement.where(
                        FieldAliasCandidateRow.canonical_field_path
                        == canonical_field_path
                    )
                if after_candidate_id is not None:
                    cursor_row = session.scalar(
                        select(FieldAliasCandidateRow).where(
                            FieldAliasCandidateRow.scope == FieldAliasCandidateScope.TENANT.value,
                            FieldAliasCandidateRow.tenant_id == tenant_id,
                            FieldAliasCandidateRow.candidate_id == after_candidate_id,
                        )
                    )
                    if cursor_row is None:
                        return ()
                    statement = statement.where(
                        tuple_(FieldAliasCandidateRow.updated_at, FieldAliasCandidateRow.candidate_id)
                        < (cursor_row.updated_at, after_candidate_id)
                    )
                rows = session.scalars(
                    statement.order_by(
                        FieldAliasCandidateRow.updated_at.desc(),
                        FieldAliasCandidateRow.candidate_id.desc(),
                    ).limit(limit)
                ).all()
                return tuple(self._candidate_from_row(row) for row in rows)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to list field alias candidates for governance"
            ) from exc

    def _list_source_reviewer_ids_sync(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> tuple[str, ...]:
        self._require_text("tenant_id", tenant_id)
        self._require_text("candidate_id", candidate_id)
        try:
            with self._sessions() as session:
                candidate = session.get(FieldAliasCandidateRow, candidate_id)
                if (
                    candidate is None
                    or candidate.scope != FieldAliasCandidateScope.TENANT.value
                    or candidate.tenant_id != tenant_id
                ):
                    return ()
                reviewers = session.scalars(
                    select(FieldAliasCandidateSourceRow.reviewer_id)
                    .where(
                        FieldAliasCandidateSourceRow.tenant_id == tenant_id,
                        FieldAliasCandidateSourceRow.candidate_id == candidate_id,
                    )
                    .distinct()
                    .order_by(FieldAliasCandidateSourceRow.reviewer_id)
                ).all()
                return tuple(reviewers)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError(
                "Unable to list field alias source reviewers"
            ) from exc

    def _summarize_support_sync(
        self,
        tenant_id: str,
        candidate_id: str,
        as_of: datetime,
    ) -> FieldAliasSupportSummary:
        self._require_text("tenant_id", tenant_id)
        self._require_text("candidate_id", candidate_id)
        self._require_aware("as_of", as_of)
        try:
            with self._sessions() as session:
                candidate = session.get(FieldAliasCandidateRow, candidate_id)
                if (
                    candidate is None
                    or candidate.scope != FieldAliasCandidateScope.TENANT.value
                    or candidate.tenant_id != tenant_id
                ):
                    raise WorkflowPersistenceError("Tenant alias candidate does not exist")
                window_start = as_of - timedelta(days=candidate.support_window_days)
                row = session.execute(
                    select(
                        func.count(FieldAliasCandidateSourceRow.support_id),
                        func.count(func.distinct(FieldAliasCandidateSourceRow.document_id)),
                        func.count(
                            func.distinct(
                                FieldAliasCandidateSourceRow.template_fingerprint
                            )
                        ),
                        func.count(func.distinct(FieldAliasCandidateSourceRow.reviewer_id)),
                    ).where(
                        FieldAliasCandidateSourceRow.tenant_id == tenant_id,
                        FieldAliasCandidateSourceRow.candidate_id == candidate_id,
                        FieldAliasCandidateSourceRow.occurred_at >= window_start,
                        FieldAliasCandidateSourceRow.occurred_at <= as_of,
                    )
                ).one()
                return FieldAliasSupportSummary(
                    candidate_id=candidate_id,
                    window_started_at=window_start,
                    window_ended_at=as_of,
                    source_count=int(row[0] or 0),
                    distinct_documents=int(row[1] or 0),
                    distinct_templates=int(row[2] or 0),
                    distinct_reviewers=int(row[3] or 0),
                )
        except WorkflowPersistenceError:
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to summarize alias support") from exc

    def _get_global_support_sync(
        self,
        candidate_id: str,
    ) -> GlobalFieldAliasSupportSnapshot | None:
        self._require_text("candidate_id", candidate_id)
        try:
            with self._sessions() as session:
                row = session.get(FieldAliasGlobalSupportSnapshotRow, candidate_id)
                if row is None:
                    return None
                fingerprints = tuple(row.tenant_fingerprints_json)
                return GlobalFieldAliasSupportSnapshot(
                    candidate_id=row.candidate_id,
                    salt_version=row.salt_version,
                    tenant_fingerprints=fingerprints,
                    source_count=row.source_count,
                    distinct_documents=row.distinct_documents,
                    distinct_templates=row.distinct_templates,
                    distinct_reviewers=row.distinct_reviewers,
                    created_at=self._aware(row.created_at),
                )
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read global alias support") from exc

    def _decide_sync(
        self,
        decision: FieldAliasCandidateDecision,
        tenant_id: str | None,
        expected_revision: int,
        audit_event: GovernanceAuditEvent | None,
    ) -> FieldAliasCandidate:
        if expected_revision <= 0:
            raise ValueError("expected_revision must be greater than zero")
        idempotency_hash = decision.idempotency_key_hash
        try:
            with self._sessions.begin() as session:
                replay = session.scalar(
                    select(FieldAliasCandidateDecisionRow).where(
                        FieldAliasCandidateDecisionRow.candidate_id
                        == decision.candidate_id,
                        FieldAliasCandidateDecisionRow.idempotency_key_hash
                        == idempotency_hash,
                    )
                )
                if replay is not None:
                    if not self._decision_matches(replay, decision):
                        raise IdempotencyConflictError(
                            "Alias candidate idempotency key is bound to another action"
                        )
                    current = session.get(FieldAliasCandidateRow, decision.candidate_id)
                    if current is None or not self._row_in_scope(current, tenant_id):
                        raise ResourceNotFoundError("Alias candidate does not exist")
                    if audit_event is not None:
                        require_governance_audit(session, audit_event)
                    return self._candidate_at_decision(current, replay)

                if decision.previous_revision != expected_revision:
                    raise ValueError(
                        "Decision previous_revision must match expected_revision"
                    )
                if decision.revision != expected_revision + 1:
                    raise ValueError(
                        "Decision revision must increment expected_revision once"
                    )

                predicates = [
                    FieldAliasCandidateRow.candidate_id == decision.candidate_id,
                    FieldAliasCandidateRow.scope == decision.scope.value,
                    FieldAliasCandidateRow.status == decision.previous_status.value,
                    FieldAliasCandidateRow.revision == expected_revision,
                ]
                if tenant_id is None:
                    predicates.append(FieldAliasCandidateRow.tenant_id.is_(None))
                else:
                    predicates.append(FieldAliasCandidateRow.tenant_id == tenant_id)
                if decision.status not in self._ALLOWED_TRANSITIONS[decision.previous_status]:
                    raise ResourceConflictError("Invalid alias candidate transition")
                values: dict[str, Any] = {
                    "status": decision.status.value,
                    "revision": FieldAliasCandidateRow.revision + 1,
                    "reviewed_by": decision.reviewer_id,
                    "reviewed_at": decision.decided_at,
                    "review_reason": decision.reason,
                    "updated_at": decision.decided_at,
                }
                if decision.promoted_catalog_version is not None:
                    values["promoted_catalog_version"] = (
                        decision.promoted_catalog_version.value
                    )
                result = session.execute(
                    update(FieldAliasCandidateRow).where(*predicates).values(**values)
                )
                if result.rowcount != 1:
                    concurrent_replay = session.scalar(
                        select(FieldAliasCandidateDecisionRow).where(
                            FieldAliasCandidateDecisionRow.candidate_id
                            == decision.candidate_id,
                            FieldAliasCandidateDecisionRow.idempotency_key_hash
                            == idempotency_hash,
                        )
                    )
                    if concurrent_replay is not None:
                        if not self._decision_matches(concurrent_replay, decision):
                            raise IdempotencyConflictError(
                                "Alias candidate idempotency key is bound to another action"
                            )
                        current = session.get(
                            FieldAliasCandidateRow,
                            decision.candidate_id,
                        )
                        if current is None or not self._row_in_scope(
                            current,
                            tenant_id,
                        ):
                            raise ResourceNotFoundError(
                                "Alias candidate does not exist"
                            )
                        return self._candidate_at_decision(
                            current,
                            concurrent_replay,
                        )
                    current = session.get(FieldAliasCandidateRow, decision.candidate_id)
                    if current is None or not self._row_in_scope(current, tenant_id):
                        raise ResourceNotFoundError("Alias candidate does not exist")
                    raise ResourceConflictError(
                        "Alias candidate revision or status changed concurrently"
                    )
                session.add(
                    FieldAliasCandidateDecisionRow(
                        decision_id=decision.decision_id,
                        candidate_id=decision.candidate_id,
                        scope=decision.scope.value,
                        previous_status=decision.previous_status.value,
                        status=decision.status.value,
                        authority=decision.authority.value,
                        reviewer_id=decision.reviewer_id,
                        reason=decision.reason,
                        idempotency_key_hash=idempotency_hash,
                        previous_revision=decision.previous_revision,
                        revision=decision.revision,
                        policy_version=decision.policy_version,
                        decided_at=decision.decided_at,
                        promoted_catalog_version=(
                            decision.promoted_catalog_version.value
                            if decision.promoted_catalog_version is not None
                            else None
                        ),
                    )
                )
                if audit_event is not None:
                    expected_action = {
                        FieldAliasStatus.APPROVED: GovernanceAction.APPROVE_FIELD_ALIAS,
                        FieldAliasStatus.SUSPENDED: GovernanceAction.DISABLE_FIELD_ALIAS,
                    }.get(decision.status)
                    if (
                        expected_action is None
                        or audit_event.action is not expected_action
                        or tenant_id is None
                        or audit_event.tenant_id != tenant_id
                        or audit_event.resource_type != "field_alias_candidate"
                        or audit_event.resource_id != decision.candidate_id
                        or audit_event.resource_version != str(decision.revision)
                        or audit_event.reviewer_id != decision.reviewer_id
                    ):
                        raise WorkflowPersistenceError(
                            "Field alias governance audit scope is inconsistent"
                        )
                    add_governance_audit(session, audit_event)
                session.flush()
                row = session.get(FieldAliasCandidateRow, decision.candidate_id)
                if row is None:
                    raise ResourceNotFoundError("Alias candidate does not exist")
                return self._candidate_from_row(row)
        except (
            IdempotencyConflictError,
            ResourceConflictError,
            ResourceNotFoundError,
            WorkflowPersistenceError,
        ):
            raise
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to decide alias candidate") from exc

    def _get_decision_by_idempotency_hash_sync(
        self,
        tenant_id: str | None,
        candidate_id: str,
        idempotency_key_hash: str,
    ) -> FieldAliasCandidateDecision | None:
        self._require_text("candidate_id", candidate_id)
        self._require_text("idempotency_key_hash", idempotency_key_hash)
        try:
            with self._sessions() as session:
                candidate = session.get(FieldAliasCandidateRow, candidate_id)
                if candidate is None or not self._row_in_scope(candidate, tenant_id):
                    return None
                row = session.scalar(
                    select(FieldAliasCandidateDecisionRow).where(
                        FieldAliasCandidateDecisionRow.candidate_id == candidate_id,
                        FieldAliasCandidateDecisionRow.idempotency_key_hash
                        == idempotency_key_hash,
                    )
                )
                return self._decision_from_row(row) if row is not None else None
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to read alias candidate decision") from exc

    def _delete_tenant_sync(self, tenant_id: str) -> int:
        self._require_text("tenant_id", tenant_id)
        try:
            with self._sessions.begin() as session:
                candidate_ids = tuple(
                    session.scalars(
                        select(FieldAliasCandidateRow.candidate_id).where(
                            FieldAliasCandidateRow.tenant_id == tenant_id
                        )
                    ).all()
                )
                if not candidate_ids:
                    return 0
                conflict_ids = tuple(
                    session.scalars(
                        select(MemoryConflictFieldAliasCandidateRow.conflict_id).where(
                            MemoryConflictFieldAliasCandidateRow.tenant_id == tenant_id,
                            MemoryConflictFieldAliasCandidateRow.candidate_id.in_(
                                candidate_ids
                            ),
                        )
                    ).all()
                )
                if conflict_ids:
                    session.execute(
                        delete(MemoryConflictRow).where(
                            MemoryConflictRow.tenant_id == tenant_id,
                            MemoryConflictRow.conflict_id.in_(conflict_ids),
                        )
                    )
                session.execute(
                    delete(FieldAliasCandidateSourceRow).where(
                        FieldAliasCandidateSourceRow.tenant_id == tenant_id,
                        FieldAliasCandidateSourceRow.candidate_id.in_(candidate_ids),
                    )
                )
                session.execute(
                    delete(FieldAliasCandidateDecisionRow).where(
                        FieldAliasCandidateDecisionRow.candidate_id.in_(candidate_ids)
                    )
                )
                session.execute(
                    delete(FieldAliasCandidateRow).where(
                        FieldAliasCandidateRow.tenant_id == tenant_id
                    )
                )
                return len(candidate_ids)
        except (ValueError, SQLAlchemyError) as exc:
            raise WorkflowPersistenceError("Unable to delete tenant alias candidates") from exc

    def _save_support(
        self,
        session: Session,
        support: FieldAliasCandidateSupport,
    ) -> bool:
        values = {
            "support_id": support.support_id,
            "candidate_id": support.candidate_id,
            "tenant_id": support.tenant_id,
            "document_id": support.document_id,
            "run_id": support.run_id,
            "evidence_id": support.evidence_id,
            "binding_decision_id": support.binding_decision_id,
            "reviewer_id": support.reviewer_id,
            "source_catalog_version": support.source_catalog_version.value,
            "reason": support.reason,
            "template_fingerprint": support.template_fingerprint,
            "occurred_at": support.occurred_at,
        }
        created = self._insert_do_nothing(
            session,
            FieldAliasCandidateSourceRow,
            values,
        )
        row = session.scalar(
            select(FieldAliasCandidateSourceRow).where(
                FieldAliasCandidateSourceRow.tenant_id == support.tenant_id,
                FieldAliasCandidateSourceRow.run_id == support.run_id,
                FieldAliasCandidateSourceRow.evidence_id == support.evidence_id,
                FieldAliasCandidateSourceRow.binding_decision_id
                == support.binding_decision_id,
            )
        )
        if row is None or any(
            (
                row.support_id != support.support_id,
                row.candidate_id != support.candidate_id,
                row.document_id != support.document_id,
                row.reviewer_id != support.reviewer_id,
                row.source_catalog_version != support.source_catalog_version.value,
                row.reason != support.reason,
                row.template_fingerprint != support.template_fingerprint,
            )
        ):
            raise WorkflowPersistenceError(
                "Field binding review source is bound to another alias candidate"
            )
        return created

    def _save_global_support(
        self,
        session: Session,
        support: GlobalFieldAliasSupportSnapshot,
    ) -> None:
        values = {
            "candidate_id": support.candidate_id,
            "salt_version": support.salt_version,
            "tenant_fingerprints_json": list(support.tenant_fingerprints),
            "source_count": support.source_count,
            "distinct_documents": support.distinct_documents,
            "distinct_templates": support.distinct_templates,
            "distinct_reviewers": support.distinct_reviewers,
            "created_at": support.created_at,
        }
        self._insert_do_nothing(session, FieldAliasGlobalSupportSnapshotRow, values)
        row = session.get(FieldAliasGlobalSupportSnapshotRow, support.candidate_id)
        if row is None or any(
            (
                row.salt_version != support.salt_version,
                tuple(row.tenant_fingerprints_json) != support.tenant_fingerprints,
                row.source_count != support.source_count,
                row.distinct_documents != support.distinct_documents,
                row.distinct_templates != support.distinct_templates,
                row.distinct_reviewers != support.distinct_reviewers,
            )
        ):
            raise WorkflowPersistenceError(
                "Global alias candidate support is bound to different aggregates"
            )

    @classmethod
    def _candidate_from_row(cls, row: FieldAliasCandidateRow) -> FieldAliasCandidate:
        return FieldAliasCandidate(
            candidate_id=row.candidate_id,
            scope=FieldAliasCandidateScope(row.scope),
            tenant_id=row.tenant_id,
            schema_version=row.schema_version,
            document_type=row.document_type,
            canonical_field_path=row.canonical_field_path,
            alias_text=row.alias_text,
            normalized_alias=row.normalized_alias,
            policy_version=row.policy_version,
            context_anchors=cls._anchors_from_payload(row.context_anchors_json),
            status=FieldAliasStatus(row.status),
            canonical_collision=row.canonical_collision,
            support_window_days=row.support_window_days,
            revision=row.revision,
            submitted_at=cls._aware(row.submitted_at),
            updated_at=cls._aware(row.updated_at),
            reviewed_by=row.reviewed_by,
            reviewed_at=(
                cls._aware(row.reviewed_at) if row.reviewed_at is not None else None
            ),
            review_reason=row.review_reason,
            promoted_catalog_version=(
                FieldSemanticCatalogVersion(row.promoted_catalog_version)
                if row.promoted_catalog_version is not None
                else None
            ),
        )

    @classmethod
    def _candidate_identity_matches(
        cls,
        row: FieldAliasCandidateRow,
        candidate: FieldAliasCandidate,
    ) -> bool:
        return (
            row.scope == candidate.scope.value
            and row.tenant_id == candidate.tenant_id
            and row.schema_version == candidate.schema_version
            and row.document_type == candidate.document_type
            and row.canonical_field_path == candidate.canonical_field_path
            and row.normalized_alias == candidate.normalized_alias
            and row.policy_version == candidate.policy_version
            and row.canonical_collision == candidate.canonical_collision
            and row.support_window_days == candidate.support_window_days
        )

    @staticmethod
    def _anchors_payload(
        anchors: tuple[FieldContextAnchor, ...],
    ) -> list[dict[str, object]]:
        return [
            {
                "anchor_id": anchor.anchor_id,
                "text": anchor.text,
                "normalized_text": anchor.normalized_text,
                "relation": anchor.relation.value,
                "max_distance": anchor.max_distance,
                "is_negative": anchor.is_negative,
            }
            for anchor in anchors
        ]

    @staticmethod
    def _anchors_from_payload(payload: object) -> tuple[FieldContextAnchor, ...]:
        if not isinstance(payload, list):
            raise WorkflowPersistenceError("Alias candidate anchors are malformed")
        anchors: list[FieldContextAnchor] = []
        for item in payload:
            if not isinstance(item, dict):
                raise WorkflowPersistenceError("Alias candidate anchor is malformed")
            anchors.append(
                FieldContextAnchor(
                    anchor_id=str(item.get("anchor_id", "")),
                    text=str(item.get("text", "")),
                    normalized_text=str(item.get("normalized_text", "")),
                    relation=FieldContextRelation(str(item.get("relation", ""))),
                    max_distance=(
                        int(item["max_distance"])
                        if item.get("max_distance") is not None
                        else None
                    ),
                    is_negative=bool(item.get("is_negative", False)),
                )
            )
        return tuple(anchors)

    @staticmethod
    def _decision_matches(
        row: FieldAliasCandidateDecisionRow,
        decision: FieldAliasCandidateDecision,
    ) -> bool:
        promoted = (
            decision.promoted_catalog_version.value
            if decision.promoted_catalog_version is not None
            else None
        )
        return (
            row.scope == decision.scope.value
            and row.previous_status == decision.previous_status.value
            and row.status == decision.status.value
            and row.authority == decision.authority.value
            and row.reviewer_id == decision.reviewer_id
            and row.reason == decision.reason
            and row.previous_revision == decision.previous_revision
            and row.revision == decision.revision
            and row.policy_version == decision.policy_version
            and row.promoted_catalog_version == promoted
        )

    @classmethod
    def _decision_from_row(
        cls,
        row: FieldAliasCandidateDecisionRow,
    ) -> FieldAliasCandidateDecision:
        return FieldAliasCandidateDecision(
            decision_id=row.decision_id,
            candidate_id=row.candidate_id,
            scope=FieldAliasCandidateScope(row.scope),
            previous_status=FieldAliasStatus(row.previous_status),
            status=FieldAliasStatus(row.status),
            authority=FieldAliasCandidateDecisionAuthority(row.authority),
            reviewer_id=row.reviewer_id,
            reason=row.reason,
            idempotency_key_hash=row.idempotency_key_hash,
            previous_revision=row.previous_revision,
            revision=row.revision,
            policy_version=row.policy_version,
            decided_at=cls._aware(row.decided_at),
            promoted_catalog_version=(
                FieldSemanticCatalogVersion(row.promoted_catalog_version)
                if row.promoted_catalog_version is not None
                else None
            ),
        )

    @classmethod
    def _candidate_at_decision(
        cls,
        current: FieldAliasCandidateRow,
        decision: FieldAliasCandidateDecisionRow,
    ) -> FieldAliasCandidate:
        candidate = cls._candidate_from_row(current)
        return FieldAliasCandidate(
            candidate_id=candidate.candidate_id,
            scope=candidate.scope,
            tenant_id=candidate.tenant_id,
            schema_version=candidate.schema_version,
            document_type=candidate.document_type,
            canonical_field_path=candidate.canonical_field_path,
            alias_text=candidate.alias_text,
            normalized_alias=candidate.normalized_alias,
            policy_version=candidate.policy_version,
            context_anchors=candidate.context_anchors,
            status=FieldAliasStatus(decision.status),
            canonical_collision=candidate.canonical_collision,
            support_window_days=candidate.support_window_days,
            revision=decision.revision,
            submitted_at=candidate.submitted_at,
            updated_at=cls._aware(decision.decided_at),
            reviewed_by=decision.reviewer_id,
            reviewed_at=cls._aware(decision.decided_at),
            review_reason=decision.reason,
            promoted_catalog_version=(
                FieldSemanticCatalogVersion(decision.promoted_catalog_version)
                if decision.promoted_catalog_version is not None
                else (
                    candidate.promoted_catalog_version
                    if FieldAliasStatus(decision.status)
                    in {FieldAliasStatus.APPROVED, FieldAliasStatus.SUSPENDED}
                    else None
                )
            ),
        )

    @staticmethod
    def _row_in_scope(row: FieldAliasCandidateRow, tenant_id: str | None) -> bool:
        if tenant_id is None:
            return (
                row.scope == FieldAliasCandidateScope.GLOBAL.value
                and row.tenant_id is None
            )
        return (
            row.scope == FieldAliasCandidateScope.TENANT.value
            and row.tenant_id == tenant_id
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
        return session.execute(statement).rowcount == 1

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
