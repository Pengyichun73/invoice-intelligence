"""PostgreSQL/pgvector implementation of filtered correction memory."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast

from sqlalchemy import Engine, and_, desc, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import CorrectionMemoryError
from invoice_intelligence.application.ports.memory import (
    CorrectionMemoryMatch,
    CorrectionMemoryScope,
    VectorMemoryUpsert,
)
from invoice_intelligence.domain.workflow import CorrectionEvent, JsonValue
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    CorrectionMemoryRow,
    CorrectionMemorySourceRow,
    MemoryAdmissionRecordRow,
    ReviewedExampleRow,
)


class PgVectorMemoryStore:
    """Legacy development fallback gated by PostgreSQL admission approval.

    The store remains a migration-only fallback when the reviewed-example index is
    unavailable.  Retrieval joins the canonical reviewed example and admission rows
    so pending or revoked facts cannot become Few-shot context through this path.
    """

    def __init__(self, engine: Engine, dimensions: int) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("PgVectorMemoryStore requires PostgreSQL")
        if dimensions != 1536:
            raise ValueError("Vector dimensions must match the 1536-column migration")
        self._dimensions = dimensions
        self._sessions = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        )

    async def list_scopes(
        self,
        tenant_id: str,
        document_types: tuple[str, ...],
        schema_version: str,
        limit: int,
    ) -> tuple[CorrectionMemoryScope, ...]:
        if not document_types or limit <= 0:
            return ()
        return await asyncio.to_thread(
            self._list_scopes_sync,
            tenant_id,
            document_types,
            schema_version,
            limit,
        )

    async def search(
        self,
        tenant_id: str,
        scope: CorrectionMemoryScope,
        query_embedding: tuple[float, ...],
        limit: int,
        min_similarity: float,
    ) -> tuple[CorrectionMemoryMatch, ...]:
        self._validate_embedding(query_embedding)
        if limit <= 0:
            raise ValueError("Vector search limit must be greater than zero")
        if not 0.0 <= min_similarity <= 1.0:
            raise ValueError("min_similarity must be between zero and one")
        return await asyncio.to_thread(
            self._search_sync,
            tenant_id,
            scope,
            query_embedding,
            limit,
            min_similarity,
        )

    async def upsert(self, records: Sequence[VectorMemoryUpsert]) -> None:
        for record in records:
            self._validate_embedding(record.embedding)
        await asyncio.to_thread(self._upsert_sync, tuple(records))

    async def disable(self, tenant_id: str, memory_id: str, reason: str) -> None:
        if not memory_id.strip() or not reason.strip():
            raise ValueError("memory_id and disable reason must not be empty")
        await asyncio.to_thread(self._disable_sync, tenant_id, memory_id, reason)

    async def invalidate_schema(
        self,
        tenant_id: str,
        document_type: str,
        schema_version: str,
        reason: str,
    ) -> int:
        if not document_type.strip() or not schema_version.strip() or not reason.strip():
            raise ValueError("Schema invalidation values must not be empty")
        return await asyncio.to_thread(
            self._invalidate_schema_sync,
            tenant_id,
            document_type,
            schema_version,
            reason,
        )

    def _list_scopes_sync(
        self,
        tenant_id: str,
        document_types: tuple[str, ...],
        schema_version: str,
        limit: int,
    ) -> tuple[CorrectionMemoryScope, ...]:
        statement = (
            select(
                CorrectionMemoryRow.document_type,
                CorrectionMemoryRow.field_path,
                CorrectionMemoryRow.schema_version,
            )
            .join(
                CorrectionMemorySourceRow,
                and_(
                    CorrectionMemorySourceRow.memory_id == CorrectionMemoryRow.memory_id,
                ),
            )
            .join(
                ReviewedExampleRow,
                and_(
                    ReviewedExampleRow.source_event_id
                    == CorrectionMemorySourceRow.source_event_id,
                    ReviewedExampleRow.tenant_id == CorrectionMemoryRow.tenant_id,
                ),
            )
            .join(
                MemoryAdmissionRecordRow,
                and_(
                    MemoryAdmissionRecordRow.tenant_id
                    == ReviewedExampleRow.tenant_id,
                    MemoryAdmissionRecordRow.example_id
                    == ReviewedExampleRow.example_id,
                ),
            )
            .where(
                CorrectionMemoryRow.tenant_id == tenant_id,
                CorrectionMemoryRow.document_type.in_(document_types),
                CorrectionMemoryRow.schema_version == schema_version,
                CorrectionMemoryRow.is_reviewed.is_(True),
                CorrectionMemoryRow.is_valid.is_(True),
                MemoryAdmissionRecordRow.status == "approved",
            )
            .group_by(
                CorrectionMemoryRow.document_type,
                CorrectionMemoryRow.field_path,
                CorrectionMemoryRow.schema_version,
            )
            .order_by(desc(func.max(CorrectionMemoryRow.updated_at)))
            .limit(limit)
        )
        try:
            with self._sessions() as session:
                return tuple(
                    CorrectionMemoryScope(
                        document_type=document_type,
                        field_path=field_path,
                        schema_version=row_schema_version,
                    )
                    for document_type, field_path, row_schema_version in session.execute(
                        statement
                    )
                )
        except SQLAlchemyError as exc:
            raise CorrectionMemoryError("Unable to list correction-memory scopes") from exc

    def _search_sync(
        self,
        tenant_id: str,
        scope: CorrectionMemoryScope,
        query_embedding: tuple[float, ...],
        limit: int,
        min_similarity: float,
    ) -> tuple[CorrectionMemoryMatch, ...]:
        distance = CorrectionMemoryRow.embedding.cosine_distance(list(query_embedding))
        similarity = (1.0 - distance).label("similarity")
        statement = (
            select(CorrectionMemoryRow, similarity)
            .join(
                CorrectionMemorySourceRow,
                and_(
                    CorrectionMemorySourceRow.memory_id == CorrectionMemoryRow.memory_id,
                ),
            )
            .join(
                ReviewedExampleRow,
                and_(
                    ReviewedExampleRow.source_event_id
                    == CorrectionMemorySourceRow.source_event_id,
                    ReviewedExampleRow.tenant_id == CorrectionMemoryRow.tenant_id,
                ),
            )
            .join(
                MemoryAdmissionRecordRow,
                and_(
                    MemoryAdmissionRecordRow.tenant_id
                    == ReviewedExampleRow.tenant_id,
                    MemoryAdmissionRecordRow.example_id
                    == ReviewedExampleRow.example_id,
                ),
            )
            .where(
                CorrectionMemoryRow.tenant_id == tenant_id,
                CorrectionMemoryRow.document_type == scope.document_type,
                CorrectionMemoryRow.field_path == scope.field_path,
                CorrectionMemoryRow.schema_version == scope.schema_version,
                CorrectionMemoryRow.is_reviewed.is_(True),
                CorrectionMemoryRow.is_valid.is_(True),
                MemoryAdmissionRecordRow.status == "approved",
                distance <= 1.0 - min_similarity,
            )
            .order_by(distance, desc(CorrectionMemoryRow.occurrence_count))
            .limit(limit)
        )
        try:
            with self._sessions() as session:
                return tuple(
                    CorrectionMemoryMatch(
                        memory_id=row.memory_id,
                        event=self._event_from_payload(row.event_json),
                        similarity=float(row_similarity),
                        occurrence_count=row.occurrence_count,
                    )
                    for row, row_similarity in session.execute(statement)
                )
        except (TypeError, ValueError, SQLAlchemyError) as exc:
            raise CorrectionMemoryError("Unable to search correction memory") from exc

    def _upsert_sync(self, records: tuple[VectorMemoryUpsert, ...]) -> None:
        if not records:
            return
        now = self._now()
        try:
            with self._sessions.begin() as session:
                for record in records:
                    memory_id = sha256(
                        f"correction-memory\0{record.fingerprint}".encode("utf-8")
                    ).hexdigest()
                    event = record.event
                    session.execute(
                        pg_insert(CorrectionMemoryRow)
                        .values(
                            memory_id=memory_id,
                            tenant_id=record.tenant_id,
                            fingerprint=record.fingerprint,
                            document_type=event.document_type,
                            field_path=event.field_path,
                            schema_version=event.schema_version,
                            event_json=self._event_payload(event),
                            embedding=list(record.embedding),
                            is_reviewed=event.is_reviewed,
                            is_valid=event.is_valid,
                            occurrence_count=0,
                            disabled_reason=None,
                            created_at=event.created_at,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing(index_elements=["fingerprint"])
                    )
                    inserted_source = session.scalar(
                        pg_insert(CorrectionMemorySourceRow)
                        .values(
                            source_event_id=record.source_event_id,
                            memory_id=memory_id,
                            created_at=now,
                        )
                        .on_conflict_do_nothing(index_elements=["source_event_id"])
                        .returning(CorrectionMemorySourceRow.source_event_id)
                    )
                    if inserted_source is None:
                        existing_source = session.get(
                            CorrectionMemorySourceRow,
                            record.source_event_id,
                        )
                        if existing_source is None or existing_source.memory_id != memory_id:
                            raise CorrectionMemoryError(
                                "Source event is bound to a different correction memory"
                            )
                        continue
                    session.execute(
                        update(CorrectionMemoryRow)
                        .where(CorrectionMemoryRow.memory_id == memory_id)
                        .values(
                            occurrence_count=CorrectionMemoryRow.occurrence_count + 1,
                            updated_at=now,
                        )
                    )
        except CorrectionMemoryError:
            raise
        except SQLAlchemyError as exc:
            raise CorrectionMemoryError("Unable to upsert correction memory") from exc

    def _disable_sync(self, tenant_id: str, memory_id: str, reason: str) -> None:
        try:
            with self._sessions.begin() as session:
                result = session.execute(
                    update(CorrectionMemoryRow)
                    .where(
                        CorrectionMemoryRow.tenant_id == tenant_id,
                        CorrectionMemoryRow.memory_id == memory_id,
                    )
                    .values(
                        is_valid=False,
                        disabled_reason=reason.strip(),
                        updated_at=self._now(),
                    )
                )
                if result.rowcount == 0:
                    raise CorrectionMemoryError("Correction memory does not exist")
        except CorrectionMemoryError:
            raise
        except SQLAlchemyError as exc:
            raise CorrectionMemoryError("Unable to disable correction memory") from exc

    def _invalidate_schema_sync(
        self,
        tenant_id: str,
        document_type: str,
        schema_version: str,
        reason: str,
    ) -> int:
        try:
            with self._sessions.begin() as session:
                result = session.execute(
                    update(CorrectionMemoryRow)
                    .where(
                        CorrectionMemoryRow.tenant_id == tenant_id,
                        CorrectionMemoryRow.document_type == document_type,
                        CorrectionMemoryRow.schema_version == schema_version,
                        CorrectionMemoryRow.is_valid.is_(True),
                    )
                    .values(
                        is_valid=False,
                        disabled_reason=reason.strip(),
                        updated_at=self._now(),
                    )
                )
                return int(result.rowcount or 0)
        except SQLAlchemyError as exc:
            raise CorrectionMemoryError("Unable to invalidate correction-memory Schema") from exc

    def _validate_embedding(self, embedding: tuple[float, ...]) -> None:
        if len(embedding) != self._dimensions:
            raise ValueError("Embedding dimension does not match pgvector column")

    @staticmethod
    def _event_payload(event: CorrectionEvent) -> dict[str, JsonValue]:
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
            "created_at": event.created_at.isoformat(),
            "is_reviewed": event.is_reviewed,
            "is_valid": event.is_valid,
        }

    @staticmethod
    def _event_from_payload(payload: dict[str, Any]) -> CorrectionEvent:
        try:
            created_at = datetime.fromisoformat(str(payload["created_at"]))
            return CorrectionEvent(
                document_type=str(payload["document_type"]),
                field_path=str(payload["field_path"]),
                model_value=cast(JsonValue, payload.get("model_value")),
                corrected_value=cast(JsonValue, payload.get("corrected_value")),
                correction_reason=str(payload["correction_reason"]),
                vendor_features=cast(dict[str, JsonValue], payload["vendor_features"]),
                template_features=cast(dict[str, JsonValue], payload["template_features"]),
                document_reference=str(payload["document_reference"]),
                image_reference=(
                    str(payload["image_reference"])
                    if payload.get("image_reference") is not None
                    else None
                ),
                schema_version=str(payload["schema_version"]),
                created_at=created_at,
                is_reviewed=bool(payload["is_reviewed"]),
                is_valid=bool(payload["is_valid"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CorrectionMemoryError("Persisted correction memory is invalid") from exc

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)
