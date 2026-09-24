"""Project approved PostgreSQL field semantics into a dedicated hybrid index."""

import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import uuid4

from invoice_intelligence.application.errors import FieldSemanticIndexError
from invoice_intelligence.application.ports.field_semantics import (
    FieldSemanticDenseEmbeddingProvider,
    FieldSemanticIndexStore,
    FieldSemanticProjectionRepository,
    FieldSemanticSparseEmbeddingProvider,
)
from invoice_intelligence.application.ports.index_integrity import IndexIntegrityVerifier
from invoice_intelligence.application.ports.observability import PrivacyTelemetry, TraceStage
from invoice_intelligence.application.ports.projection_lease import ProjectionLease
from invoice_intelligence.application.services.field_semantic_catalog import (
    FieldSemanticCatalog,
)
from invoice_intelligence.application.services.projection_heartbeat import projection_heartbeat
from invoice_intelligence.domain.examples import (
    IndexProjectionStatus,
    IndexVersion,
    ModelVersion,
    SparseVector,
)
from invoice_intelligence.domain.field_semantics import (
    FieldAlias,
    FieldSemanticCatalogVersion,
    FieldSemanticDefinition,
    FieldSemanticIndexDocument,
    FieldSemanticIndexScope,
    FieldSemanticIndexVersionRecord,
    FieldSemanticProjectionTask,
    FieldSemanticVectorProjection,
)


@dataclass(frozen=True, slots=True)
class FieldSemanticProjectionBatchResult:
    tenant_id: str
    index_version: IndexVersion
    claimed: int
    indexed: int
    failed: int


class FieldSemanticIndexProjectionService[SchemaT]:
    """Coordinate short PostgreSQL transactions and idempotent Milvus writes."""

    def __init__(
        self,
        *,
        catalog: FieldSemanticCatalog[SchemaT],
        projection_repository: FieldSemanticProjectionRepository,
        index_store: FieldSemanticIndexStore,
        dense_embedding_provider: FieldSemanticDenseEmbeddingProvider,
        sparse_embedding_provider: FieldSemanticSparseEmbeddingProvider | None,
        batch_size: int = 32,
        privacy_telemetry: PrivacyTelemetry | None = None,
        integrity_verifier: IndexIntegrityVerifier | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("Field semantic projection batch_size must be positive")
        self._catalog = catalog
        self._projections = projection_repository
        self._index_store = index_store
        self._dense_embeddings = dense_embedding_provider
        self._sparse_embeddings = sparse_embedding_provider
        self._batch_size = batch_size
        self._privacy_telemetry = privacy_telemetry
        self._integrity_verifier = integrity_verifier

    async def register_index_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        dense_model_version: ModelVersion,
        sparse_model_version: ModelVersion | None,
    ) -> int:
        """Register an immutable build and idempotently schedule the composed catalog."""

        definitions = await self._catalog.list_definitions(
            tenant_id,
            catalog_version=catalog_version,
        )
        eligible = tuple(
            definition
            for definition in definitions
            if definition.is_valid
            and definition.schema_version == schema_version
            and definition.catalog_version == catalog_version
            and definition.tenant_scope == tenant_id
        )
        if not eligible:
            raise ValueError("Field semantic catalog contains no projectable definitions")
        await self._projections.register_version(
            tenant_id,
            index_version,
            schema_version,
            catalog_version,
            dense_model_version,
            sparse_model_version,
        )
        tasks = tuple(
            self._task_for_definition(tenant_id, definition, index_version)
            for definition in eligible
        )
        await self._projections.schedule(tenant_id, index_version, tasks)
        return len(tasks)

    async def project_pending(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        *,
        limit: int | None = None,
        trace_id: str | None = None,
        worker_id: str | None = None,
        lease_seconds: float = 300.0,
    ) -> FieldSemanticProjectionBatchResult:
        """Process one bounded retry batch without changing source catalog facts."""

        span = (
            self._privacy_telemetry.span(
                trace_id=trace_id or uuid4().hex,
                tenant_id=tenant_id,
                stage=TraceStage.INDEX_PROJECTION,
                operation="project_field_semantics",
                attributes={"index_version": index_version.value},
            )
            if self._privacy_telemetry is not None
            else nullcontext()
        )
        with span:
            return await self._project_pending(
                tenant_id, index_version, limit=limit,
                worker_id=worker_id or f"api-projection:{uuid4().hex}",
                lease_seconds=lease_seconds,
            )

    async def _project_pending(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        *,
        limit: int | None = None,
        worker_id: str,
        lease_seconds: float,
    ) -> FieldSemanticProjectionBatchResult:

        claim_limit = limit if limit is not None else self._batch_size
        if claim_limit <= 0:
            raise ValueError("Field semantic projection limit must be positive")
        indexed = 0
        failed = 0
        claimed = 0
        for _ in range(claim_limit):
            leases = await self._projections.claim_pending(
                tenant_id, index_version, 1, worker_id, lease_seconds,
            )
            if not leases:
                break
            lease = leases[0]
            claimed += 1
            try:
                await self._project_one(lease, lease_seconds)
            except Exception as exc:
                failed += 1
                await self._record_failure(lease, exc)
            else:
                indexed += 1
        return FieldSemanticProjectionBatchResult(
            tenant_id=tenant_id,
            index_version=index_version,
            claimed=claimed,
            indexed=indexed,
            failed=failed,
        )

    async def get_index_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> FieldSemanticIndexVersionRecord | None:
        return await self._projections.get_version(tenant_id, index_version)

    async def list_index_versions(self, tenant_id: str) -> tuple[IndexVersion, ...]:
        return await self._projections.list_versions(tenant_id)

    async def verify_index_version(self, tenant_id: str, index_version: IndexVersion) -> bool:
        record = await self._projections.get_version(tenant_id, index_version)
        if record is None or not record.ready_for_activation:
            return False
        ensure_collection = getattr(self._index_store, "ensure_collection", None)
        if callable(ensure_collection):
            await ensure_collection(tenant_id, index_version)
        if self._integrity_verifier is not None:
            try:
                definitions = await self._catalog.list_definitions(
                    tenant_id, catalog_version=record.catalog_version,
                )
                eligible = tuple(
                    definition for definition in definitions
                    if definition.is_valid
                    and definition.tenant_scope == tenant_id
                    and definition.schema_version == record.schema_version
                    and definition.catalog_version == record.catalog_version
                )
                tasks = tuple(
                    self._task_for_definition(tenant_id, definition, index_version)
                    for definition in eligible
                )
            except ValueError:
                return False
            expected_sources = {
                task.semantic_id: task.source_fingerprint for task in tasks
            }
            if not tasks or len(expected_sources) != len(tasks):
                return False
            return await self._integrity_verifier.verify_field_semantics(
                tenant_id, index_version, expected_sources
            )
        return True

    async def requeue_stale(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        stale_before: datetime,
    ) -> int:
        return await self._projections.requeue_stale(
            tenant_id,
            index_version,
            stale_before,
        )

    async def activate_index_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        """Switch the derived alias, then commit PostgreSQL as the authoritative version."""

        if not await self.verify_index_version(tenant_id, index_version):
            raise ValueError("Field semantic index version is not ready for activation")
        record = await self._projections.get_version(tenant_id, index_version)
        if record is None:
            raise ValueError("Field semantic index version is unavailable")
        previous = await self._projections.get_active_version(tenant_id, record.schema_version)
        await self._index_store.switch_alias(tenant_id, index_version)
        try:
            await self._projections.activate_version(tenant_id, index_version)
        except Exception:
            if previous is not None and previous.index_version != index_version:
                await self._index_store.switch_alias(tenant_id, previous.index_version)
            raise

    async def rebuild_index_version(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> int:
        """Reset a non-active build and remove its rebuildable Milvus documents."""

        count = await self._projections.reset_version(tenant_id, index_version)
        await self._index_store.delete_tenant(tenant_id, index_version)
        return count

    async def invalidate_catalog(
        self,
        tenant_id: str,
        schema_version: str,
        catalog_version: FieldSemanticCatalogVersion,
        reason: str,
    ) -> tuple[IndexVersion, ...]:
        versions = await self._projections.invalidate_catalog(
            tenant_id,
            schema_version,
            catalog_version,
            reason,
        )
        for version in versions:
            await self._index_store.delete_tenant(tenant_id, version)
        return versions

    async def invalidate_schema(
        self,
        tenant_id: str,
        schema_version: str,
        reason: str,
    ) -> tuple[IndexVersion, ...]:
        versions = await self._projections.invalidate_schema(
            tenant_id,
            schema_version,
            reason,
        )
        for version in versions:
            await self._index_store.delete_tenant(tenant_id, version)
        return versions

    async def delete_tenant(self, tenant_id: str) -> tuple[IndexVersion, ...]:
        versions = await self._projections.list_versions(tenant_id)
        for version in versions:
            await self._index_store.delete_tenant(tenant_id, version)
        return await self._projections.delete_tenant(tenant_id)

    async def _project_one(
        self, lease: ProjectionLease[FieldSemanticProjectionTask], lease_seconds: float,
    ) -> None:
        task = lease.item
        if task.status is not IndexProjectionStatus.PROCESSING:
            raise ValueError("Only claimed field semantic tasks may be projected")
        document = self._document_for_task(task)
        async with projection_heartbeat(
            lambda: self._projections.renew_lease(
                task.tenant_id, task.semantic_id, task.index_version,
                lease.worker_id, lease.token, lease_seconds,
            ), lease_seconds,
        ):
            dense = await self._embed_dense(document.dense_index_text)
            sparse = await self._embed_sparse(document.sparse_index_text)
            projection = FieldSemanticVectorProjection(
                document=document,
                dense_embedding=dense,
                sparse_embedding=sparse,
                projection_checksum=self._projection_checksum(document),
            )
            await self._index_store.upsert((projection,))
        await self._projections.mark_projected(
            task.tenant_id, task.semantic_id, task.index_version,
            projection.projection_checksum, lease.worker_id, lease.token,
        )

    async def _embed_dense(self, text: str) -> tuple[float, ...]:
        vectors = await self._dense_embeddings.embed((text,))
        if len(vectors) != 1 or not vectors[0]:
            raise ValueError("Dense embedding provider returned an invalid result")
        return tuple(float(value) for value in vectors[0])

    async def _embed_sparse(self, text: str) -> SparseVector | None:
        if self._sparse_embeddings is None:
            return None
        vectors = await self._sparse_embeddings.embed((text,))
        if len(vectors) != 1:
            raise ValueError("Sparse embedding provider returned an invalid result")
        return tuple(
            sorted(
                ((int(index), float(weight)) for index, weight in vectors[0]),
                key=lambda item: item[0],
            )
        )

    async def _record_failure(
        self,
        lease: ProjectionLease[FieldSemanticProjectionTask],
        error: Exception,
    ) -> None:
        task = lease.item
        error_name = type(error).__name__.strip() or "ProjectionError"
        try:
            await self._projections.mark_failed(
                task.tenant_id,
                task.semantic_id,
                task.index_version,
                f"field_semantic_projection_{error_name[:96]}",
                lease.worker_id,
                lease.token,
            )
        except Exception as persistence_error:
            raise FieldSemanticIndexError(
                "Unable to persist field semantic projection failure"
            ) from persistence_error

    @classmethod
    def _task_for_definition(
        cls,
        tenant_id: str,
        definition: FieldSemanticDefinition,
        index_version: IndexVersion,
    ) -> FieldSemanticProjectionTask:
        semantic_id = cls._semantic_id(tenant_id, definition)
        approved_aliases, negative_aliases, context_anchors = (
            cls._approved_projection_metadata(definition)
        )
        projection_identity = (
            f"field-semantic-projection\0{tenant_id}\0{semantic_id}\0"
            f"{index_version.value}"
        )
        projection_id = sha256(projection_identity.encode("utf-8")).hexdigest()
        return FieldSemanticProjectionTask(
            projection_id=projection_id,
            semantic_id=semantic_id,
            tenant_id=tenant_id,
            document_type=definition.document_type,
            canonical_field_path=definition.canonical_field_path,
            schema_version=definition.schema_version,
            catalog_version=definition.catalog_version,
            index_version=index_version,
            display_name=definition.display_name,
            description=definition.description,
            value_type=definition.value_type,
            approved_aliases=approved_aliases,
            negative_aliases=negative_aliases,
            context_anchors=context_anchors,
            source_fingerprint=cls._source_fingerprint(definition),
            status=IndexProjectionStatus.PENDING,
            attempt_count=0,
        )

    @classmethod
    def _document_for_task(
        cls,
        task: FieldSemanticProjectionTask,
    ) -> FieldSemanticIndexDocument:
        source_payload = cls._projection_source_payload(
            schema_version=task.schema_version,
            document_type=task.document_type,
            canonical_field_path=task.canonical_field_path,
            display_name=task.display_name,
            description=task.description,
            value_type=task.value_type,
            approved_aliases=task.approved_aliases,
            negative_aliases=task.negative_aliases,
            context_anchors=task.context_anchors,
            catalog_version=task.catalog_version,
            tenant_id=task.tenant_id,
        )
        if sha256(cls._canonical(source_payload).encode("utf-8")).hexdigest() != (
            task.source_fingerprint
        ):
            raise ValueError("PostgreSQL field semantic projection snapshot is inconsistent")
        dense_text = cls._sectioned_text(
            (
                ("DISPLAY_NAME", task.display_name),
                ("DESCRIPTION", task.description),
                ("APPROVED_ALIASES", task.approved_aliases),
                ("CONTEXT_ANCHORS", task.context_anchors),
                ("VALUE_TYPE", task.value_type),
                ("CANONICAL_FIELD_PATH", task.canonical_field_path),
            )
        )
        sparse_text = cls._sectioned_text(
            (
                (
                    "ORIGINAL_LABELS",
                    (
                        task.display_name,
                        task.canonical_field_path.rsplit(".", 1)[-1],
                    ),
                ),
                ("STANDARD_NAME", task.display_name),
                ("APPROVED_ALIASES", task.approved_aliases),
                ("CANONICAL_FIELD_PATH", task.canonical_field_path),
            )
        )
        return FieldSemanticIndexDocument(
            semantic_id=task.semantic_id,
            scope=FieldSemanticIndexScope(
                tenant_id=task.tenant_id,
                document_type=task.document_type,
                schema_version=task.schema_version,
                catalog_version=task.catalog_version,
            ),
            canonical_field_path=task.canonical_field_path,
            display_name=task.display_name,
            description=task.description,
            value_type=task.value_type,
            approved_aliases=task.approved_aliases,
            negative_aliases=task.negative_aliases,
            context_anchors=task.context_anchors,
            dense_index_text=dense_text,
            sparse_index_text=sparse_text,
            index_version=task.index_version,
            source_fingerprint=task.source_fingerprint,
        )

    @classmethod
    def _approved_projection_metadata(
        cls,
        definition: FieldSemanticDefinition,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        cls._require_approved_aliases(definition.aliases + definition.negative_aliases)
        approved_aliases = tuple(alias.alias_text for alias in definition.aliases)
        negative_aliases = tuple(alias.alias_text for alias in definition.negative_aliases)
        context_anchors = tuple(
            dict.fromkeys(
                anchor.text
                for alias in definition.aliases
                for anchor in alias.context_anchors
                if not anchor.is_negative
            )
        )
        return approved_aliases, negative_aliases, context_anchors

    @staticmethod
    def _require_approved_aliases(aliases: tuple[FieldAlias, ...]) -> None:
        if any(not alias.eligible_for_binding for alias in aliases):
            raise ValueError("Unapproved field aliases cannot enter the semantic index")

    @classmethod
    def _semantic_id(
        cls,
        tenant_id: str,
        definition: FieldSemanticDefinition,
    ) -> str:
        identity = (
            "field-semantic\0"
            f"{tenant_id}\0{definition.document_type}\0{definition.schema_version}\0"
            f"{definition.catalog_version.value}\0{definition.canonical_field_path}"
        )
        return sha256(identity.encode("utf-8")).hexdigest()

    @classmethod
    def _source_fingerprint(cls, definition: FieldSemanticDefinition) -> str:
        if definition.tenant_scope is None or not definition.is_valid:
            raise ValueError("Only valid tenant catalog definitions may be fingerprinted")
        aliases, negative_aliases, anchors = cls._approved_projection_metadata(
            definition
        )
        payload = cls._projection_source_payload(
            schema_version=definition.schema_version,
            document_type=definition.document_type,
            canonical_field_path=definition.canonical_field_path,
            display_name=definition.display_name,
            description=definition.description,
            value_type=definition.value_type,
            approved_aliases=aliases,
            negative_aliases=negative_aliases,
            context_anchors=anchors,
            catalog_version=definition.catalog_version,
            tenant_id=definition.tenant_scope,
        )
        return sha256(cls._canonical(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _projection_source_payload(
        *,
        schema_version: str,
        document_type: str,
        canonical_field_path: str,
        display_name: str,
        description: str,
        value_type: str,
        approved_aliases: tuple[str, ...],
        negative_aliases: tuple[str, ...],
        context_anchors: tuple[str, ...],
        catalog_version: FieldSemanticCatalogVersion,
        tenant_id: str,
    ) -> dict[str, object]:
        return {
            "schema_version": schema_version,
            "document_type": document_type,
            "canonical_field_path": canonical_field_path,
            "display_name": display_name,
            "description": description,
            "value_type": value_type,
            "approved_aliases": approved_aliases,
            "negative_aliases": negative_aliases,
            "context_anchors": context_anchors,
            "catalog_version": catalog_version.value,
            "tenant_id": tenant_id,
            "is_valid": True,
        }

    @classmethod
    def _projection_checksum(cls, document: FieldSemanticIndexDocument) -> str:
        payload = {
            "semantic_id": document.semantic_id,
            "source_fingerprint": document.source_fingerprint,
            "dense_index_text": document.dense_index_text,
            "sparse_index_text": document.sparse_index_text,
            "index_version": document.index_version.value,
        }
        return sha256(cls._canonical(payload).encode("utf-8")).hexdigest()

    @classmethod
    def _sectioned_text(cls, sections: tuple[tuple[str, object], ...]) -> str:
        text = "\n".join(
            item
            for name, value in sections
            for item in (name, cls._canonical(value))
        )
        lowered = text.casefold()
        if "base64," in lowered or "data:image" in lowered:
            raise ValueError("Field semantic index cannot contain inline image data")
        return text

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
