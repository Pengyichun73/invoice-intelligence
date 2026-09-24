"""Milvus Dense + Sparse adapter for redacted reviewed invoice examples.

PostgreSQL remains the canonical source.  This adapter owns only a rebuildable
derived index and never receives ``ReviewedExample`` instances or raw document
bytes.  The Milvus SDK is intentionally loaded lazily so deployments that use
the PostgreSQL fallback do not establish a Milvus connection at import time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from threading import Lock
from typing import Any, Protocol, cast

from invoice_intelligence.application.ports.examples import (
    ExampleIndexStore,
    HybridSearchOptions,
)
from invoice_intelligence.domain.examples import (
    ExampleCandidate,
    ExampleEvidenceReference,
    ExampleLabelType,
    ExampleScope,
    ExampleVectorProjection,
    IndexVersion,
    RetrievalRecallSource,
    RetrievalScore,
    RetrievedExample,
    SparseVector,
)

logger = logging.getLogger(__name__)


class MilvusIndexError(RuntimeError):
    """Safe adapter error; provider details are kept out of API responses."""


class CollectionResolver(Protocol):
    """Resolve a tenant and index version to a Milvus collection name."""

    def resolve(self, tenant_id: str, index_version: IndexVersion) -> str:
        """Return a versioned shared or tenant-dedicated collection name."""


class DefaultMilvusCollectionResolver:
    """Shared collection resolver with an opt-in dedicated collection map.

    ``dedicated_collections`` maps a high-compliance tenant to a collection
    prefix.  The index version is still appended, so a rebuild never mutates an
    older collection in place.
    """

    def __init__(
        self,
        *,
        collection_prefix: str,
        dedicated_collections: Mapping[str, str] | None = None,
    ) -> None:
        self._collection_prefix = _collection_component(collection_prefix, "collection_prefix")
        self._dedicated = {
            tenant_id.strip(): _collection_component(prefix, "dedicated_collection")
            for tenant_id, prefix in (dedicated_collections or {}).items()
        }

    def resolve(self, tenant_id: str, index_version: IndexVersion) -> str:
        normalized_tenant_id = tenant_id.strip()
        if not normalized_tenant_id:
            raise ValueError("tenant_id must not be empty")
        prefix = self._dedicated.get(normalized_tenant_id, self._collection_prefix)
        version = _collection_component(index_version.value, "index_version")
        # A short digest prevents distinct punctuation-heavy versions from
        # collapsing to the same Milvus identifier after normalization.
        digest_input = (
            f"{normalized_tenant_id}\0{index_version.value}"
            if normalized_tenant_id in self._dedicated
            else index_version.value
        )
        digest = sha256(digest_input.encode("utf-8")).hexdigest()[:10]
        return _collection_component(f"{prefix}_{version}_{digest}", "collection_name")


class MilvusExampleIndexStore(ExampleIndexStore):
    """Async application-port adapter backed by ``pymilvus.MilvusClient``.

    Every operation is executed in a worker thread because ``MilvusClient`` is
    synchronous.  Retries are bounded and do not change the projection
    checksum, making a replay safe after a partial network failure.
    """

    def __init__(
        self,
        *,
        uri: str,
        token: str | None = None,
        connect_timeout_seconds: float = 10.0,
        operation_timeout_seconds: float = 30.0,
        max_retries: int = 2,
        consistency_level: str = "Bounded",
        collection_resolver: CollectionResolver | None = None,
        collection_prefix: str = "invoice_examples",
        alias: str | None = None,
        dense_dimension: int = 1536,
        hnsw_m: int = 64,
        hnsw_ef_construction: int = 100,
        search_ef: int = 128,
        batch_size: int = 64,
        sparse_enabled: bool = True,
        bm25_function_enabled: bool = True,
        sparse_metric_type: str = "BM25",
        dense_weight: float = 0.7,
        sparse_weight: float = 0.3,
        ranker: str = "weighted",
        max_text_length: int = 65_535,
    ) -> None:
        if not uri.strip():
            raise ValueError("Milvus URI must not be empty")
        if connect_timeout_seconds <= 0 or operation_timeout_seconds <= 0:
            raise ValueError("Milvus timeouts must be greater than zero")
        if max_retries < 0:
            raise ValueError("Milvus max_retries must not be negative")
        if dense_dimension <= 0:
            raise ValueError("Milvus dense_dimension must be greater than zero")
        if hnsw_m <= 0 or hnsw_ef_construction <= 0 or search_ef <= 0:
            raise ValueError("Milvus HNSW parameters must be greater than zero")
        if batch_size <= 0:
            raise ValueError("Milvus batch_size must be greater than zero")
        if sparse_metric_type not in {"BM25", "IP"}:
            raise ValueError("Milvus sparse_metric_type must be BM25 or IP")
        if sparse_enabled and bm25_function_enabled and sparse_metric_type != "BM25":
            raise ValueError("Milvus BM25 Function requires BM25 sparse_metric_type")
        if ranker not in {"weighted", "rrf"}:
            raise ValueError("Milvus ranker must be weighted or rrf")
        if dense_weight < 0 or sparse_weight < 0 or dense_weight + sparse_weight <= 0:
            raise ValueError("Milvus fusion weights must be non-negative and non-zero")
        if max_text_length <= 0:
            raise ValueError("Milvus max_text_length must be greater than zero")

        self._uri = uri.strip()
        self._token = token.strip() if token else None
        self._connect_timeout = connect_timeout_seconds
        self._operation_timeout = operation_timeout_seconds
        self._max_retries = max_retries
        self._consistency_level = consistency_level
        self._resolver = collection_resolver or DefaultMilvusCollectionResolver(
            collection_prefix=collection_prefix
        )
        self._alias = alias.strip() if alias and alias.strip() else None
        self._dense_dimension = dense_dimension
        self._hnsw_m = hnsw_m
        self._hnsw_ef_construction = hnsw_ef_construction
        self._search_ef = search_ef
        self._batch_size = batch_size
        self._sparse_enabled = sparse_enabled
        self._bm25_function_enabled = bm25_function_enabled and sparse_enabled
        self._sparse_metric_type = sparse_metric_type
        self._dense_weight = dense_weight
        self._sparse_weight = sparse_weight
        self._ranker = ranker
        self._max_text_length = max_text_length
        self._client: Any | None = None
        self._client_init_lock = Lock()

    async def upsert(self, projections: Sequence[ExampleVectorProjection]) -> None:
        """Idempotently upsert redacted projections grouped by collection."""

        if not projections:
            return
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for projection in projections:
            collection = self._collection_for_projection(projection)
            grouped[collection].append(self._entity(projection))
        for collection, entities in grouped.items():
            await self._ensure_collection(collection)
            for batch in _chunks(entities, self._batch_size):
                await self._call(
                    "upsert",
                    lambda batch=batch, collection=collection: self._client_required().upsert(
                        collection_name=collection,
                        data=batch,
                        timeout=self._operation_timeout,
                    ),
                )

    async def rebuild(
        self,
        projections: Sequence[ExampleVectorProjection],
        index_version: IndexVersion,
    ) -> None:
        """Replace only the supplied tenants in one versioned collection.

        The source projection queue remains PostgreSQL-owned.  This helper is
        intended for bounded rebuild workers: it removes stale derived rows for
        the tenants represented by the batch, then performs the same idempotent
        batched Upsert as :meth:`upsert`.
        """

        if not projections:
            return
        if any(
            projection.candidate.index_version != index_version for projection in projections
        ):
            raise ValueError("Rebuild projections must all match the requested index version")
        tenants = {
            projection.candidate.scope.tenant_id
            for projection in projections
        }
        if len(tenants) == 0:
            raise ValueError("Rebuild projections must match the requested index version")
        for tenant_id in tenants:
            await self.delete_tenant(tenant_id, index_version)
        await self.upsert(projections)

    async def hybrid_search(
        self,
        scope: ExampleScope,
        redacted_query_text: str,
        dense_embedding: tuple[float, ...],
        sparse_embedding: SparseVector | None,
        index_version: IndexVersion,
        options: HybridSearchOptions,
    ) -> tuple[RetrievedExample, ...]:
        """Filter exact scope in every AnnSearchRequest before vector search."""

        limit = options.limit
        if scope.tenant_id != scope.tenant_id.strip():
            raise ValueError("Search tenant_id must be normalized")
        if len(dense_embedding) != self._dense_dimension:
            raise ValueError("Dense query dimension does not match Milvus configuration")
        if not redacted_query_text.strip():
            raise ValueError("redacted_query_text must not be empty")
        collection = self._resolver.resolve(scope.tenant_id, index_version)
        await self._ensure_collection(collection)
        expr = self._scope_filter(scope, index_version, options)
        sdk = self._load_sdk()
        dense_request = sdk["AnnSearchRequest"](
            data=[list(dense_embedding)],
            anns_field="dense_vector",
            param={
                "metric_type": "COSINE",
                "params": {"ef": self._search_ef},
            },
            limit=limit,
            expr=expr,
        )
        requests: list[Any] = [dense_request]
        if self._sparse_enabled:
            sparse_data: list[Any]
            if sparse_embedding is not None and not self._bm25_function_enabled:
                sparse_data = [_sparse_payload(sparse_embedding)]
            elif self._bm25_function_enabled:
                # The built-in BM25 Function converts query text to a sparse
                # vector inside Milvus; no remote sparse SDK is required.
                sparse_data = [redacted_query_text]
            else:
                sparse_data = []
            if sparse_data:
                requests.append(
                    sdk["AnnSearchRequest"](
                        data=sparse_data,
                        anns_field="sparse_vector",
                        param={
                            "metric_type": self._sparse_metric_type,
                            "params": {"drop_ratio_search": 0.0},
                        },
                        limit=limit,
                        expr=expr,
                    )
                )

        ranker = self._build_ranker(sdk, len(requests), options)
        hybrid_hits = await self._call(
            "hybrid_search",
            lambda: self._client_required().hybrid_search(
                collection_name=collection,
                reqs=requests,
                ranker=ranker,
                limit=limit,
                output_fields=self._output_fields(),
                timeout=self._operation_timeout,
            ),
        )
        dense_hits = await self._call_channel_search(
            collection,
            "dense_vector",
            [list(dense_embedding)],
            expr,
            limit,
            metric_type="COSINE",
            params={"ef": self._search_ef},
        )
        sparse_hits: Any = []
        if len(requests) > 1:
            sparse_query = (
                [_sparse_payload(sparse_embedding)]
                if sparse_embedding is not None and not self._bm25_function_enabled
                else [redacted_query_text]
            )
            sparse_hits = await self._call_channel_search(
                collection,
                "sparse_vector",
                sparse_query,
                expr,
                limit,
                metric_type=self._sparse_metric_type,
                params={"drop_ratio_search": 0.0},
            )
        dense_scores = _score_map(dense_hits)
        sparse_scores = _score_map(sparse_hits)
        hits = _first_result_list(hybrid_hits)
        results: list[RetrievedExample] = []
        for rank, hit in enumerate(hits[:limit], start=1):
            example_id = _hit_id(hit)
            if not example_id:
                continue
            candidate = self._candidate_from_hit(hit, scope, index_version)
            if candidate.label_type not in options.label_types:
                raise MilvusIndexError("Milvus returned a hit outside the requested labels")
            fusion_score = _hit_score(hit)
            if fusion_score is None:
                raise MilvusIndexError("Milvus returned a hit without a fusion score")
            results.append(
                RetrievedExample(
                    candidate=candidate,
                    score=RetrievalScore(
                        dense=dense_scores.get(example_id),
                        sparse=sparse_scores.get(example_id),
                        fusion=fusion_score,
                        rerank=None,
                    ),
                    rank=rank,
                    recall_sources=tuple(
                        source
                        for source, score in (
                            (RetrievalRecallSource.DENSE, dense_scores.get(example_id)),
                            (RetrievalRecallSource.SPARSE, sparse_scores.get(example_id)),
                            (RetrievalRecallSource.HYBRID, fusion_score),
                        )
                        if score is not None
                    ),
                )
            )
        return tuple(results)

    async def delete(
        self,
        tenant_id: str,
        example_ids: Sequence[str],
        index_version: IndexVersion,
    ) -> None:
        """Delete explicit primary keys from one tenant/version collection."""

        ids = tuple(dict.fromkeys(item for item in example_ids if item.strip()))
        if not ids:
            return
        collection = self._resolver.resolve(tenant_id, index_version)
        if not await self._collection_exists(collection):
            return
        # Restrict explicit primary keys by tenant and version even in a shared
        # collection; no caller-defined item field can broaden this deletion.
        for batch in _chunks_ids(ids, self._batch_size):
            id_literals = ",".join(_expr_string(item) for item in batch)
            await self._call(
                "delete",
                lambda id_literals=id_literals: self._client_required().delete(
                    collection_name=collection,
                    filter=f"tenant_id == {_expr_string(tenant_id)} and "
                    f"index_version == {_expr_string(index_version.value)} and "
                    f"example_id in [{id_literals}]",
                    timeout=self._operation_timeout,
                ),
            )

    async def delete_tenant(self, tenant_id: str, index_version: IndexVersion) -> None:
        """Delete only one tenant's derived rows in one versioned collection."""

        collection = self._resolver.resolve(tenant_id, index_version)
        if not await self._collection_exists(collection):
            return
        await self._call(
            "delete_tenant",
            lambda: self._client_required().delete(
                collection_name=collection,
                filter=f"tenant_id == {_expr_string(tenant_id)} and "
                f"index_version == {_expr_string(index_version.value)}",
                timeout=self._operation_timeout,
            ),
        )

    async def ensure_collection(
        self, tenant_id: str, index_version: IndexVersion,
    ) -> str:
        """Create/load a versioned collection and return its name."""

        collection = self._resolver.resolve(tenant_id, index_version)
        await self._ensure_collection(collection)
        return collection

    async def projection_manifest(
        self, tenant_id: str, index_version: IndexVersion,
    ) -> dict[str, str]:
        """强一致读取目标 Collection 的全部脱敏投影标识和 checksum。"""

        collection = await self.ensure_collection(tenant_id, index_version)

        def read() -> dict[str, str]:
            iterator = self._client_required().query_iterator(
                collection_name=collection,
                batch_size=self._batch_size,
                filter="",
                output_fields=[
                    "example_id", "tenant_id", "index_version", "projection_checksum"
                ],
                consistency_level="Strong",
                timeout=self._operation_timeout,
            )
            manifest: dict[str, str] = {}
            try:
                while batch := iterator.next():
                    for row in batch:
                        if (
                            row.get("tenant_id") != tenant_id
                            or row.get("index_version") != index_version.value
                        ):
                            raise MilvusIndexError("Collection contains another projection scope")
                        identifier = row.get("example_id")
                        checksum = row.get("projection_checksum")
                        if (
                            not isinstance(identifier, str)
                            or not isinstance(checksum, str)
                            or identifier in manifest
                        ):
                            raise MilvusIndexError("Collection projection manifest is invalid")
                        manifest[identifier] = checksum
            finally:
                iterator.close()
            return manifest

        return cast(dict[str, str], await self._call("verify_projection_manifest", read))

    async def switch_alias(
        self,
        tenant_id: str,
        index_version: IndexVersion,
        *,
        alias: str | None = None,
    ) -> None:
        """Atomically move an alias after the target collection is loaded."""

        collection = self._resolver.resolve(tenant_id, index_version)
        await self._ensure_collection(collection)
        alias_base = alias or self._alias
        if not alias_base:
            raise ValueError("An alias is required for alias switching")
        alias_name = (
            f"{_collection_component(alias_base, 'alias')[:200]}_"
            f"{sha256(tenant_id.encode()).hexdigest()[:12]}"
        )
        client = self._client_required()
        try:
            await self._call(
                "create_alias",
                lambda: client.create_alias(
                    collection_name=collection,
                    alias=alias_name,
                    timeout=self._operation_timeout,
                ),
            )
        except MilvusIndexError:
            # create_alias is expected to fail only when the alias exists;
            # alter_alias then provides the atomic collection pointer switch.
            await self._call(
                "alter_alias",
                lambda: client.alter_alias(
                    collection_name=collection,
                    alias=alias_name,
                    timeout=self._operation_timeout,
                ),
            )

    async def close(self) -> None:
        """Close the SDK client when the application lifespan ends."""

        client = self._client
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)
        self._client = None

    async def _ensure_collection(self, collection: str) -> None:
        exists = await self._collection_exists(collection)
        if not exists:
            schema, index_params = self._build_schema_and_indexes()
            try:
                await self._call(
                    "create_collection",
                    lambda: self._client_required().create_collection(
                        collection_name=collection,
                        schema=schema,
                        index_params=index_params,
                        consistency_level=self._consistency_level,
                        timeout=self._operation_timeout,
                    ),
                )
            except MilvusIndexError:
                # Another worker may have won the create race.  Confirm the
                # collection exists before surfacing the error.
                if not await self._collection_exists(collection):
                    raise
        await self._call(
            "load_collection",
            lambda: self._client_required().load_collection(
                collection_name=collection,
                timeout=self._operation_timeout,
            ),
        )

    async def _collection_exists(self, collection: str) -> bool:
        return bool(
            await self._call(
                "has_collection",
                lambda: self._client_required().has_collection(
                    collection_name=collection,
                    timeout=self._operation_timeout,
                ),
            )
        )

    def _build_schema_and_indexes(self) -> tuple[Any, Any]:
        sdk = self._load_sdk()
        schema = self._client_required().create_schema(
            auto_id=False,
        )
        schema.add_field(
            field_name="example_id",
            datatype=sdk["DataType"].VARCHAR,
            is_primary=True,
            max_length=128,
        )
        schema.add_field(
            field_name="tenant_id",
            datatype=sdk["DataType"].VARCHAR,
            max_length=256,
            is_partition_key=True,
        )
        schema.add_field(
            field_name="document_type", datatype=sdk["DataType"].VARCHAR, max_length=256
        )
        schema.add_field(field_name="field_path", datatype=sdk["DataType"].VARCHAR, max_length=1024)
        schema.add_field(
            field_name="schema_version", datatype=sdk["DataType"].VARCHAR, max_length=128
        )
        schema.add_field(
            field_name="catalog_version", datatype=sdk["DataType"].VARCHAR, max_length=128
        )
        schema.add_field(field_name="label_type", datatype=sdk["DataType"].VARCHAR, max_length=64)
        schema.add_field(
            field_name="vendor_fingerprint", datatype=sdk["DataType"].VARCHAR, max_length=512
        )
        schema.add_field(
            field_name="template_fingerprint", datatype=sdk["DataType"].VARCHAR, max_length=512
        )
        schema.add_field(field_name="is_reviewed", datatype=sdk["DataType"].BOOL)
        schema.add_field(field_name="is_valid", datatype=sdk["DataType"].BOOL)
        schema.add_field(
            field_name="index_version",
            datatype=sdk["DataType"].VARCHAR,
            max_length=256,
        )
        schema.add_field(
            field_name="dense_vector",
            datatype=sdk["DataType"].FLOAT_VECTOR,
            dim=self._dense_dimension,
        )
        schema.add_field(field_name="sparse_vector", datatype=sdk["DataType"].SPARSE_FLOAT_VECTOR)
        schema.add_field(
            field_name="redacted_content",
            datatype=sdk["DataType"].VARCHAR,
            max_length=self._max_text_length,
            enable_analyzer=True,
        )
        # Keep the redacted value fragments and references queryable without
        # ever projecting the canonical PostgreSQL JSON values.
        for field_name, max_length in (
            ("redacted_model_value", self._max_text_length),
            ("redacted_reviewed_value", self._max_text_length),
            ("redacted_correction_reason", self._max_text_length),
            ("document_reference", 2048),
            ("image_reference", 2048),
            ("redaction_policy_version", 128),
            ("projection_checksum", 128),
        ):
            schema.add_field(
                field_name=field_name,
                datatype=sdk["DataType"].VARCHAR,
                max_length=max_length,
            )
        schema.add_field(field_name="page_number", datatype=sdk["DataType"].INT64)
        schema.add_field(field_name="last_seen_at_epoch", datatype=sdk["DataType"].INT64)
        if self._bm25_function_enabled:
            schema.add_function(
                sdk["Function"](
                    name="redacted_content_bm25",
                    input_field_names=["redacted_content"],
                    output_field_names=["sparse_vector"],
                    function_type=sdk["FunctionType"].BM25,
                )
            )
        index_params = self._client_required().prepare_index_params()
        index_params.add_index(
            field_name="dense_vector",
            index_name="dense_vector_hnsw",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": self._hnsw_m, "efConstruction": self._hnsw_ef_construction},
        )
        if self._sparse_enabled:
            index_params.add_index(
                field_name="sparse_vector",
                index_name="sparse_vector_inverted",
                index_type="SPARSE_INVERTED_INDEX",
                metric_type=self._sparse_metric_type,
                params={"inverted_index_algo": "DAAT_MAXSCORE"},
            )
        return schema, index_params

    def _build_ranker(
        self,
        sdk: Mapping[str, Any],
        request_count: int,
        options: HybridSearchOptions,
    ) -> Any:
        strategy = options.fusion_strategy or self._ranker
        dense_weight = (
            options.dense_weight
            if options.dense_weight is not None
            else self._dense_weight
        )
        sparse_weight = (
            options.sparse_weight
            if options.sparse_weight is not None
            else self._sparse_weight
        )
        if request_count == 1:
            params: dict[str, Any] = {
                "reranker": "weighted",
                "weights": [1.0],
                "norm_score": True,
            }
        elif strategy == "rrf":
            params = {"reranker": "rrf", "k": 100}
        else:
            total = dense_weight + sparse_weight
            if total <= 0:
                raise ValueError("At least one hybrid search weight must be greater than zero")
            params = {
                "reranker": "weighted",
                "weights": [
                    dense_weight / total,
                    sparse_weight / total,
                ],
                "norm_score": True,
            }
        return sdk["Function"](
            name="invoice_example_reranker",
            input_field_names=[],
            function_type=sdk["FunctionType"].RERANK,
            params=params,
        )

    async def _call_channel_search(
        self,
        collection: str,
        anns_field: str,
        data: list[Any],
        expr: str,
        limit: int,
        *,
        metric_type: str,
        params: Mapping[str, Any],
    ) -> Any:
        return await self._call(
            f"search_{anns_field}",
            lambda: self._client_required().search(
                collection_name=collection,
                data=data,
                anns_field=anns_field,
                search_params={"metric_type": metric_type, "params": dict(params)},
                filter=expr,
                limit=limit,
                output_fields=self._output_fields(),
                timeout=self._operation_timeout,
            ),
        )

    def _collection_for_projection(self, projection: ExampleVectorProjection) -> str:
        candidate = projection.candidate
        if candidate.scope.tenant_id != candidate.scope.tenant_id.strip():
            raise ValueError("Projection tenant_id must be normalized")
        if len(projection.dense_embedding) != self._dense_dimension:
            raise ValueError("Projection dense dimension does not match Milvus configuration")
        return self._resolver.resolve(candidate.scope.tenant_id, candidate.index_version)

    def _entity(self, projection: ExampleVectorProjection) -> dict[str, Any]:
        candidate = projection.candidate
        if not candidate.scope.is_reviewed or not candidate.scope.is_valid:
            raise ValueError("Only reviewed and valid examples may be indexed")
        content = candidate.redacted_index_text
        evidence = candidate.evidence_reference
        serialized_values = (
            content,
            self._canonical(candidate.redacted_model_value),
            self._canonical(candidate.redacted_reviewed_value),
            candidate.redacted_correction_reason or "",
            candidate.vendor_fingerprint or "",
            candidate.template_fingerprint or "",
            evidence.document_reference,
            evidence.image_reference or "",
        )
        if any(_contains_inline_image(value) for value in serialized_values):
            raise ValueError("Milvus projection cannot contain inline image data")
        entity: dict[str, Any] = {
            "example_id": candidate.example_id,
            "tenant_id": candidate.scope.tenant_id,
            "document_type": candidate.scope.document_type,
            "field_path": candidate.scope.field_path,
            "schema_version": candidate.scope.schema_version,
            "catalog_version": candidate.scope.catalog_version,
            "label_type": candidate.label_type.value,
            "vendor_fingerprint": candidate.vendor_fingerprint or "",
            "template_fingerprint": candidate.template_fingerprint or "",
            "is_reviewed": True,
            "is_valid": True,
            "index_version": candidate.index_version.value,
            "dense_vector": list(projection.dense_embedding),
            "redacted_content": content,
            "redacted_model_value": self._canonical(candidate.redacted_model_value),
            "redacted_reviewed_value": self._canonical(candidate.redacted_reviewed_value),
            "redacted_correction_reason": candidate.redacted_correction_reason or "",
            "document_reference": evidence.document_reference,
            "image_reference": evidence.image_reference or "",
            "page_number": evidence.page_number or 0,
            "last_seen_at_epoch": int(candidate.last_seen_at.timestamp()),
            "redaction_policy_version": candidate.redaction_policy_version,
            "projection_checksum": projection.projection_checksum,
        }
        if self._sparse_enabled and not self._bm25_function_enabled:
            if projection.sparse_embedding is None:
                raise ValueError("Sparse embedding is required when BM25 Function is disabled")
            entity["sparse_vector"] = _sparse_payload(projection.sparse_embedding)
        self._validate_entity_lengths(entity)
        return entity

    def _validate_entity_lengths(self, entity: Mapping[str, Any]) -> None:
        limits = {
            "example_id": 128,
            "tenant_id": 256,
            "document_type": 256,
            "field_path": 1024,
            "schema_version": 128,
            "catalog_version": 128,
            "label_type": 64,
            "vendor_fingerprint": 512,
            "template_fingerprint": 512,
            "index_version": 256,
            "redacted_content": self._max_text_length,
            "redacted_model_value": self._max_text_length,
            "redacted_reviewed_value": self._max_text_length,
            "redacted_correction_reason": self._max_text_length,
            "document_reference": 2048,
            "image_reference": 2048,
            "redaction_policy_version": 128,
            "projection_checksum": 128,
        }
        for field_name, limit in limits.items():
            value = str(entity.get(field_name, ""))
            if len(value.encode("utf-8")) > limit:
                raise ValueError(f"Milvus field {field_name} exceeds its configured max_length")

    def _candidate_from_hit(
        self,
        hit: Any,
        scope: ExampleScope,
        index_version: IndexVersion,
    ) -> ExampleCandidate:
        fields = _hit_fields(hit)
        example_id = _hit_id(hit)
        if not example_id:
            raise MilvusIndexError("Milvus returned a hit without example_id")
        hit_scope = ExampleScope(
            tenant_id=str(fields.get("tenant_id", scope.tenant_id)),
            document_type=str(fields.get("document_type", scope.document_type)),
            field_path=str(fields.get("field_path", scope.field_path)),
            schema_version=str(fields.get("schema_version", scope.schema_version)),
            catalog_version=str(fields.get("catalog_version", "")),
        )
        if (
            hit_scope != scope
            or str(fields.get("index_version", index_version.value)) != index_version.value
        ):
            raise MilvusIndexError("Milvus returned a hit outside the requested scope")
        return ExampleCandidate(
            example_id=example_id,
            scope=hit_scope,
            label_type=ExampleLabelType(str(fields.get("label_type", "confirmed_correct"))),
            redacted_model_value=_json_value(fields.get("redacted_model_value")),
            redacted_reviewed_value=_json_value(fields.get("redacted_reviewed_value")),
            redacted_correction_reason=_optional_text(fields.get("redacted_correction_reason")),
            vendor_fingerprint=_optional_text(fields.get("vendor_fingerprint")),
            template_fingerprint=_optional_text(fields.get("template_fingerprint")),
            evidence_reference=ExampleEvidenceReference(
                document_reference=str(fields.get("document_reference", "milvus:unknown")),
                image_reference=_optional_text(fields.get("image_reference")),
                page_number=_optional_page(fields.get("page_number")),
            ),
            redacted_index_text=str(fields.get("redacted_content", "")),
            redaction_policy_version=str(fields.get("redaction_policy_version", "unknown")),
            index_version=index_version,
            last_seen_at=datetime.fromtimestamp(
                int(fields.get("last_seen_at_epoch", 0)),
                UTC,
            ),
        )

    def _scope_filter(
        self,
        scope: ExampleScope,
        index_version: IndexVersion,
        options: HybridSearchOptions,
    ) -> str:
        label_types = options.label_types
        if not label_types:
            raise ValueError("At least one reviewed-example label is required")
        if len(label_types) == 1:
            label_filter = f"label_type == {_expr_string(label_types[0].value)}"
        else:
            labels = ",".join(_expr_string(label.value) for label in label_types)
            label_filter = f"label_type in [{labels}]"
        filters = [
            f"tenant_id == {_expr_string(scope.tenant_id)}",
            f"document_type == {_expr_string(scope.document_type)}",
            f"field_path == {_expr_string(scope.field_path)}",
            f"schema_version == {_expr_string(scope.schema_version)}",
            f"catalog_version == {_expr_string(scope.catalog_version)}",
            "is_reviewed == true",
            "is_valid == true",
            label_filter,
            f"index_version == {_expr_string(index_version.value)}",
        ]
        if options.template_fingerprint is not None:
            filters.append(
                "template_fingerprint == "
                f"{_expr_string(options.template_fingerprint)}"
            )
        if options.not_before is not None:
            filters.append(
                f"last_seen_at_epoch >= {int(options.not_before.timestamp())}"
            )
        return " and ".join(filters)

    def _output_fields(self) -> list[str]:
        return [
            "example_id",
            "tenant_id",
            "document_type",
            "field_path",
            "schema_version",
            "catalog_version",
            "label_type",
            "vendor_fingerprint",
            "template_fingerprint",
            "is_reviewed",
            "is_valid",
            "index_version",
            "redacted_content",
            "redacted_model_value",
            "redacted_reviewed_value",
            "redacted_correction_reason",
            "document_reference",
            "image_reference",
            "page_number",
            "last_seen_at_epoch",
            "redaction_policy_version",
            "projection_checksum",
        ]

    @staticmethod
    def _canonical(value: object) -> str:
        return _canonical(value)

    async def _call(self, operation: str, function: Callable[[], Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await asyncio.to_thread(function)
            except Exception as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    logger.warning("Milvus operation failed: %s", operation)
                    raise MilvusIndexError(f"Milvus operation failed: {operation}") from exc
                await asyncio.sleep(min(0.25 * (2**attempt), 4.0))
        raise MilvusIndexError(f"Milvus operation failed: {operation}") from last_error

    def _client_required(self) -> Any:
        if self._client is None:
            self._load_sdk()
        if self._client is None:
            raise MilvusIndexError("Milvus client is not initialized")
        return self._client

    def _load_sdk(self) -> dict[str, Any]:
        try:
            from pymilvus import (
                AnnSearchRequest,
                DataType,
                Function,
                FunctionType,
                MilvusClient,
            )
        except ImportError as exc:  # pragma: no cover - depends on deployment extras
            raise MilvusIndexError("pymilvus is required for the Milvus adapter") from exc
        if self._client is None:
            with self._client_init_lock:
                if self._client is None:
                    kwargs: dict[str, Any] = {
                        "uri": self._uri,
                        "timeout": self._connect_timeout,
                    }
                    if self._token is not None:
                        kwargs["token"] = self._token
                    self._client = MilvusClient(**kwargs)
        return {
            "AnnSearchRequest": AnnSearchRequest,
            "DataType": DataType,
            "Function": Function,
            "FunctionType": FunctionType,
            "MilvusClient": MilvusClient,
        }


def _collection_component(value: str, name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", value.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        raise ValueError(f"{name} must contain an alphanumeric character")
    if not re.match(r"^[A-Za-z_]", normalized):
        normalized = f"c_{normalized}"
    return normalized[:220]


def _chunks(values: Sequence[dict[str, Any]], size: int) -> tuple[tuple[dict[str, Any], ...], ...]:
    return tuple(tuple(values[index : index + size]) for index in range(0, len(values), size))


def _chunks_ids(values: Sequence[str], size: int) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(values[index : index + size]) for index in range(0, len(values), size))


def _sparse_payload(vector: SparseVector) -> dict[int, float]:
    return {int(index): float(weight) for index, weight in vector}


def _expr_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _first_result_list(results: Any) -> list[Any]:
    if isinstance(results, list) and results and isinstance(results[0], list):
        return results[0]
    return list(results) if isinstance(results, list) else []


def _hit_fields(hit: Any) -> Mapping[str, Any]:
    if isinstance(hit, Mapping):
        entity = hit.get("entity")
        if isinstance(entity, Mapping):
            return entity
        return hit
    fields = getattr(hit, "fields", None)
    return fields if isinstance(fields, Mapping) else {}


def _hit_id(hit: Any) -> str:
    fields = _hit_fields(hit)
    value = fields.get("example_id")
    if not value:
        value = hit.get("id") if isinstance(hit, Mapping) else getattr(hit, "id", None)
    return str(value) if value is not None else ""


def _hit_score(hit: Any) -> float | None:
    raw = hit.get("distance") if isinstance(hit, Mapping) else getattr(hit, "distance", None)
    if raw is None:
        raw = hit.get("score") if isinstance(hit, Mapping) else getattr(hit, "score", None)
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _score_map(results: Any) -> dict[str, float]:
    return {
        example_id: score
        for hit in _first_result_list(results)
        if (example_id := _hit_id(hit))
        if (score := _hit_score(hit)) is not None
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _optional_page(value: Any) -> int | None:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def _contains_inline_image(value: str) -> bool:
    lowered = value.casefold()
    return "base64," in lowered or "data:image" in lowered


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
