"""从 PostgreSQL 事实与 Milvus 派生数据比对完整投影清单。"""

import asyncio
from typing import cast

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.domain.examples import IndexVersion
from invoice_intelligence.infrastructure.indexing.milvus_examples import MilvusExampleIndexStore
from invoice_intelligence.infrastructure.indexing.milvus_field_semantics import (
    MilvusFieldSemanticIndexStore,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    ExampleIndexProjectionRow,
    FieldSemanticIndexProjectionRow,
    FieldSemanticIndexVersionRow,
    IndexVersionRow,
    MemoryAdmissionRecordRow,
    ReviewedExampleRow,
)


class SQLAlchemyMilvusIndexIntegrityVerifier:
    def __init__(
        self,
        engine: Engine,
        *,
        example_store: MilvusExampleIndexStore | None,
        field_store: MilvusFieldSemanticIndexStore | None,
    ) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
        self._examples = example_store
        self._fields = field_store

    async def verify_examples(self, tenant_id: str, index_version: IndexVersion) -> bool:
        if self._examples is None:
            return False
        expected = await asyncio.to_thread(self._example_manifest, tenant_id, index_version)
        if expected is None:
            return False
        actual = await self._examples.projection_manifest(tenant_id, index_version)
        return actual == expected

    async def verify_field_semantics(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        expected_sources: dict[str, str],
    ) -> bool:
        if self._fields is None or not expected_sources:
            return False
        expected = await asyncio.to_thread(
            self._field_manifest, tenant_id, index_version, expected_sources
        )
        if expected is None:
            return False
        actual = await self._fields.projection_manifest(tenant_id, index_version)
        return actual == expected

    def _example_manifest(
        self, tenant_id: str, index_version: IndexVersion
    ) -> dict[str, str] | None:
        with self._sessions() as session:
            version = session.scalar(
                select(IndexVersionRow).where(
                    IndexVersionRow.tenant_id == tenant_id,
                    IndexVersionRow.version == index_version.value,
                    IndexVersionRow.is_valid.is_(True),
                )
            )
            if version is None:
                return None
            eligible = set(session.scalars(
                select(ReviewedExampleRow.example_id)
                .join(
                    MemoryAdmissionRecordRow,
                    MemoryAdmissionRecordRow.example_id == ReviewedExampleRow.example_id,
                )
                .where(
                    ReviewedExampleRow.tenant_id == tenant_id,
                    ReviewedExampleRow.schema_version == version.schema_version,
                    ReviewedExampleRow.is_reviewed.is_(True),
                    ReviewedExampleRow.is_valid.is_(True),
                    MemoryAdmissionRecordRow.tenant_id == tenant_id,
                    MemoryAdmissionRecordRow.status == "approved",
                )
            ).all())
            projections = session.scalars(
                select(ExampleIndexProjectionRow).where(
                    ExampleIndexProjectionRow.tenant_id == tenant_id,
                    ExampleIndexProjectionRow.index_version_id == version.index_version_id,
                )
            ).all()
            if (
                {row.example_id for row in projections} != eligible
                or any(
                    row.status != "indexed"
                    or not row.projection_checksum
                    or row.tenant_id != tenant_id
                    for row in projections
                )
            ):
                return None
            return {row.example_id: cast(str, row.projection_checksum) for row in projections}

    def _field_manifest(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        expected_sources: dict[str, str],
    ) -> dict[str, str] | None:
        with self._sessions() as session:
            version = session.scalar(
                select(FieldSemanticIndexVersionRow).where(
                    FieldSemanticIndexVersionRow.tenant_id == tenant_id,
                    FieldSemanticIndexVersionRow.version == index_version.value,
                    FieldSemanticIndexVersionRow.is_valid.is_(True),
                )
            )
            if version is None:
                return None
            projections = session.scalars(
                select(FieldSemanticIndexProjectionRow).where(
                    FieldSemanticIndexProjectionRow.tenant_id == tenant_id,
                    FieldSemanticIndexProjectionRow.index_version_id == version.index_version_id,
                )
            ).all()
            if (
                len(projections) != len(expected_sources)
                or {row.semantic_id: row.source_fingerprint for row in projections}
                != expected_sources
                or any(
                row.status != "indexed"
                or not row.projection_checksum
                or row.tenant_id != tenant_id
                or row.schema_version != version.schema_version
                or row.catalog_version != version.catalog_version
                for row in projections
                )
            ):
                return None
            return {row.semantic_id: cast(str, row.projection_checksum) for row in projections}
