"""Dedicated Milvus hybrid index for approved field semantic catalog metadata."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
from threading import Lock
from typing import Any, cast

from invoice_intelligence.application.ports.field_semantics import (
    FieldSemanticIndexStore,
    FieldSemanticSearchOptions,
)
from invoice_intelligence.domain.examples import (
    IndexVersion,
    RetrievalRecallSource,
    RetrievalScore,
    SparseVector,
)
from invoice_intelligence.domain.field_semantics import (
    FieldSemanticCatalogVersion,
    FieldSemanticIndexDocument,
    FieldSemanticIndexScope,
    FieldSemanticVectorProjection,
    RetrievedFieldSemantic,
)
from invoice_intelligence.infrastructure.indexing.milvus_examples import (
    CollectionResolver,
    DefaultMilvusCollectionResolver,
    MilvusIndexError,
)

logger = logging.getLogger(__name__)


class MilvusFieldSemanticIndexStore(FieldSemanticIndexStore):
    """Store only approved, derived field metadata in a separate collection."""

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
        collection_prefix: str = "invoice_field_semantics",
        alias: str = "invoice_field_semantics_current",
        dense_dimension: int = 1536,
        hnsw_m: int = 64,
        hnsw_ef_construction: int = 100,
        search_ef: int = 128,
        batch_size: int = 64,
        sparse_enabled: bool = True,
        bm25_function_enabled: bool = True,
        sparse_metric_type: str = "BM25",
        max_text_length: int = 65_535,
    ) -> None:
        if not uri.strip():
            raise ValueError("Milvus URI must not be empty")
        if connect_timeout_seconds <= 0 or operation_timeout_seconds <= 0:
            raise ValueError("Milvus timeouts must be positive")
        if max_retries < 0:
            raise ValueError("Milvus retries must not be negative")
        if dense_dimension <= 0 or hnsw_m <= 0 or hnsw_ef_construction <= 0:
            raise ValueError("Milvus dense index parameters must be positive")
        if search_ef <= 0 or batch_size <= 0 or max_text_length <= 0:
            raise ValueError("Milvus search and batch parameters must be positive")
        if not sparse_enabled:
            raise ValueError("Field semantic index requires sparse retrieval")
        if sparse_metric_type not in {"BM25", "IP"}:
            raise ValueError("Milvus sparse metric must be BM25 or IP")
        if bm25_function_enabled and sparse_metric_type != "BM25":
            raise ValueError("Milvus BM25 Function requires the BM25 metric")
        if not alias.strip():
            raise ValueError("Field semantic Milvus alias must not be empty")

        self._uri = uri.strip()
        self._token = token.strip() if token else None
        self._connect_timeout = connect_timeout_seconds
        self._operation_timeout = operation_timeout_seconds
        self._max_retries = max_retries
        self._consistency_level = consistency_level
        self._resolver = collection_resolver or DefaultMilvusCollectionResolver(
            collection_prefix=collection_prefix
        )
        self._alias = alias.strip()
        self._dense_dimension = dense_dimension
        self._hnsw_m = hnsw_m
        self._hnsw_ef_construction = hnsw_ef_construction
        self._search_ef = search_ef
        self._batch_size = batch_size
        self._bm25_function_enabled = bm25_function_enabled
        self._sparse_metric_type = sparse_metric_type
        self._max_text_length = max_text_length
        self._client: Any | None = None
        self._client_init_lock = Lock()

    async def upsert(
        self,
        projections: Sequence[FieldSemanticVectorProjection],
    ) -> None:
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
                    "upsert_field_semantics",
                    lambda batch=batch, collection=collection: self._client_required().upsert(
                        collection_name=collection,
                        data=batch,
                        timeout=self._operation_timeout,
                    ),
                )

    async def hybrid_search(
        self,
        scope: FieldSemanticIndexScope,
        query_text: str,
        dense_embedding: tuple[float, ...],
        sparse_embedding: SparseVector | None,
        index_version: IndexVersion,
        options: FieldSemanticSearchOptions,
    ) -> tuple[RetrievedFieldSemantic, ...]:
        if len(dense_embedding) != self._dense_dimension:
            raise ValueError("Field semantic query vector dimension is invalid")
        if not query_text.strip():
            raise ValueError("Field semantic query text must not be empty")
        collection = self._resolver.resolve(scope.tenant_id, index_version)
        await self._ensure_collection(collection)
        expression = self._scope_filter(scope, index_version)
        sdk = self._load_sdk()
        dense_request = sdk["AnnSearchRequest"](
            data=[list(dense_embedding)],
            anns_field="dense_vector",
            param={"metric_type": "COSINE", "params": {"ef": self._search_ef}},
            limit=options.limit,
            expr=expression,
        )
        if sparse_embedding is not None and not self._bm25_function_enabled:
            sparse_data: list[Any] = [_sparse_payload(sparse_embedding)]
        elif self._bm25_function_enabled:
            sparse_data = [query_text]
        else:
            raise ValueError("Sparse query vector is required when BM25 is disabled")
        sparse_request = sdk["AnnSearchRequest"](
            data=sparse_data,
            anns_field="sparse_vector",
            param={
                "metric_type": self._sparse_metric_type,
                "params": {"drop_ratio_search": 0.0},
            },
            limit=options.limit,
            expr=expression,
        )
        ranker = self._build_ranker(sdk, options)
        hybrid_hits = await self._call(
            "hybrid_search_field_semantics",
            lambda: self._client_required().hybrid_search(
                collection_name=collection,
                reqs=[dense_request, sparse_request],
                ranker=ranker,
                limit=options.limit,
                output_fields=self._output_fields(),
                timeout=self._operation_timeout,
            ),
        )
        dense_hits, sparse_hits = await asyncio.gather(
            self._channel_search(
                collection,
                "dense_vector",
                [list(dense_embedding)],
                expression,
                options.limit,
                "COSINE",
                {"ef": self._search_ef},
            ),
            self._channel_search(
                collection,
                "sparse_vector",
                sparse_data,
                expression,
                options.limit,
                self._sparse_metric_type,
                {"drop_ratio_search": 0.0},
            ),
        )
        dense_scores = _score_map(dense_hits)
        sparse_scores = _score_map(sparse_hits)
        results: list[RetrievedFieldSemantic] = []
        for rank, hit in enumerate(_first_result_list(hybrid_hits)[: options.limit], start=1):
            semantic_id = _hit_id(hit)
            if not semantic_id:
                continue
            fusion_score = _hit_score(hit)
            if fusion_score is None:
                raise MilvusIndexError("Milvus field semantic hit has no fusion score")
            document = self._document_from_hit(hit, scope, index_version)
            results.append(
                RetrievedFieldSemantic(
                    document=document,
                    score=RetrievalScore(
                        dense=dense_scores.get(semantic_id),
                        sparse=sparse_scores.get(semantic_id),
                        fusion=fusion_score,
                        rerank=None,
                    ),
                    rank=rank,
                    recall_sources=tuple(
                        source
                        for source, score in (
                            (RetrievalRecallSource.DENSE, dense_scores.get(semantic_id)),
                            (RetrievalRecallSource.SPARSE, sparse_scores.get(semantic_id)),
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
        semantic_ids: Sequence[str],
        index_version: IndexVersion,
    ) -> None:
        ids = tuple(dict.fromkeys(item for item in semantic_ids if item.strip()))
        if not ids:
            return
        collection = self._resolver.resolve(tenant_id, index_version)
        if not await self._collection_exists(collection):
            return
        for batch in _chunks_ids(ids, self._batch_size):
            literals = ",".join(_expr_string(value) for value in batch)
            await self._call(
                "delete_field_semantics",
                lambda literals=literals: self._client_required().delete(
                    collection_name=collection,
                    filter=f"tenant_id == {_expr_string(tenant_id)} and "
                    f"index_version == {_expr_string(index_version.value)} and "
                    f"semantic_id in [{literals}]",
                    timeout=self._operation_timeout,
                ),
            )

    async def delete_tenant(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        collection = self._resolver.resolve(tenant_id, index_version)
        if not await self._collection_exists(collection):
            return
        await self._call(
            "delete_tenant_field_semantics",
            lambda: self._client_required().delete(
                collection_name=collection,
                filter=f"tenant_id == {_expr_string(tenant_id)} and "
                f"index_version == {_expr_string(index_version.value)}",
                timeout=self._operation_timeout,
            ),
        )

    async def ensure_collection(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> str:
        collection = self._resolver.resolve(tenant_id, index_version)
        await self._ensure_collection(collection)
        return collection

    async def projection_manifest(
        self, tenant_id: str, index_version: IndexVersion,
    ) -> dict[str, str]:
        """强一致读取目标字段语义 Collection 的标识与 checksum。"""

        collection = await self.ensure_collection(tenant_id, index_version)

        def read() -> dict[str, str]:
            iterator = self._client_required().query_iterator(
                collection_name=collection,
                batch_size=self._batch_size,
                filter="",
                output_fields=[
                    "semantic_id", "tenant_id", "index_version", "projection_checksum"
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
                        identifier = row.get("semantic_id")
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

        return cast(dict[str, str], await self._call("verify_field_semantic_manifest", read))

    async def switch_alias(
        self,
        tenant_id: str,
        index_version: IndexVersion,
    ) -> None:
        collection = await self.ensure_collection(tenant_id, index_version)
        alias = self._tenant_alias(tenant_id)
        client = self._client_required()
        try:
            await self._call(
                "create_field_semantic_alias",
                lambda: client.create_alias(
                    collection_name=collection,
                    alias=alias,
                    timeout=self._operation_timeout,
                ),
            )
        except MilvusIndexError:
            await self._call(
                "alter_field_semantic_alias",
                lambda: client.alter_alias(
                    collection_name=collection,
                    alias=alias,
                    timeout=self._operation_timeout,
                ),
            )

    async def close(self) -> None:
        client = self._client
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)
        self._client = None

    async def _ensure_collection(self, collection: str) -> None:
        if not await self._collection_exists(collection):
            schema, indexes = self._build_schema_and_indexes()
            try:
                await self._call(
                    "create_field_semantic_collection",
                    lambda: self._client_required().create_collection(
                        collection_name=collection,
                        schema=schema,
                        index_params=indexes,
                        consistency_level=self._consistency_level,
                        timeout=self._operation_timeout,
                    ),
                )
            except MilvusIndexError:
                if not await self._collection_exists(collection):
                    raise
        await self._call(
            "load_field_semantic_collection",
            lambda: self._client_required().load_collection(
                collection_name=collection,
                timeout=self._operation_timeout,
            ),
        )

    async def _collection_exists(self, collection: str) -> bool:
        return bool(
            await self._call(
                "has_field_semantic_collection",
                lambda: self._client_required().has_collection(
                    collection_name=collection,
                    timeout=self._operation_timeout,
                ),
            )
        )

    def _build_schema_and_indexes(self) -> tuple[Any, Any]:
        sdk = self._load_sdk()
        schema = self._client_required().create_schema(auto_id=False)
        schema.add_field(
            field_name="semantic_id",
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
        for field_name, max_length in (
            ("document_type", 256),
            ("canonical_field_path", 1024),
            ("schema_version", 128),
            ("catalog_version", 256),
            ("index_version", 256),
            ("display_name", 4096),
            ("description", self._max_text_length),
            ("value_type", 16_384),
            ("aliases_json", self._max_text_length),
            ("negative_aliases_json", self._max_text_length),
            ("context_anchors_json", self._max_text_length),
            ("dense_content", self._max_text_length),
            ("sparse_content", self._max_text_length),
            ("source_fingerprint", 128),
            ("projection_checksum", 128),
        ):
            field_options: dict[str, Any] = {}
            if field_name == "sparse_content":
                field_options["enable_analyzer"] = True
            schema.add_field(
                field_name=field_name,
                datatype=sdk["DataType"].VARCHAR,
                max_length=max_length,
                **field_options,
            )
        schema.add_field(field_name="is_valid", datatype=sdk["DataType"].BOOL)
        schema.add_field(
            field_name="dense_vector",
            datatype=sdk["DataType"].FLOAT_VECTOR,
            dim=self._dense_dimension,
        )
        schema.add_field(
            field_name="sparse_vector",
            datatype=sdk["DataType"].SPARSE_FLOAT_VECTOR,
        )
        if self._bm25_function_enabled:
            schema.add_function(
                sdk["Function"](
                    name="field_semantic_sparse_bm25",
                    input_field_names=["sparse_content"],
                    output_field_names=["sparse_vector"],
                    function_type=sdk["FunctionType"].BM25,
                )
            )
        indexes = self._client_required().prepare_index_params()
        indexes.add_index(
            field_name="dense_vector",
            index_name="field_semantic_dense_hnsw",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": self._hnsw_m, "efConstruction": self._hnsw_ef_construction},
        )
        indexes.add_index(
            field_name="sparse_vector",
            index_name="field_semantic_sparse_inverted",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type=self._sparse_metric_type,
            params={"inverted_index_algo": "DAAT_MAXSCORE"},
        )
        return schema, indexes

    def _entity(self, projection: FieldSemanticVectorProjection) -> dict[str, Any]:
        document = projection.document
        serialized = {
            "semantic_id": document.semantic_id,
            "tenant_id": document.scope.tenant_id,
            "document_type": document.scope.document_type,
            "canonical_field_path": document.canonical_field_path,
            "schema_version": document.scope.schema_version,
            "catalog_version": document.scope.catalog_version.value,
            "index_version": document.index_version.value,
            "is_valid": True,
            "display_name": document.display_name,
            "description": document.description,
            "value_type": document.value_type,
            "aliases_json": _canonical(document.approved_aliases),
            "negative_aliases_json": _canonical(document.negative_aliases),
            "context_anchors_json": _canonical(document.context_anchors),
            "dense_content": document.dense_index_text,
            "sparse_content": document.sparse_index_text,
            "dense_vector": list(projection.dense_embedding),
            "source_fingerprint": document.source_fingerprint,
            "projection_checksum": projection.projection_checksum,
        }
        if not self._bm25_function_enabled:
            if projection.sparse_embedding is None:
                raise ValueError("Field semantic sparse vector is required")
            serialized["sparse_vector"] = _sparse_payload(projection.sparse_embedding)
        if any(
            _contains_inline_image(value)
            for value in serialized.values()
            if isinstance(value, str)
        ):
            raise ValueError("Milvus field semantics cannot contain inline images")
        self._validate_lengths(serialized)
        return serialized

    def _document_from_hit(
        self,
        hit: Any,
        scope: FieldSemanticIndexScope,
        index_version: IndexVersion,
    ) -> FieldSemanticIndexDocument:
        fields = _hit_fields(hit)
        semantic_id = _hit_id(hit)
        hit_scope = FieldSemanticIndexScope(
            tenant_id=str(fields.get("tenant_id", "")),
            document_type=str(fields.get("document_type", "")),
            schema_version=str(fields.get("schema_version", "")),
            catalog_version=FieldSemanticCatalogVersion(
                str(fields.get("catalog_version", ""))
            ),
        )
        if (
            hit_scope != scope
            or str(fields.get("index_version", "")) != index_version.value
            or fields.get("is_valid") is not True
        ):
            raise MilvusIndexError("Milvus returned field semantics outside exact scope")
        return FieldSemanticIndexDocument(
            semantic_id=semantic_id,
            scope=hit_scope,
            canonical_field_path=str(fields.get("canonical_field_path", "")),
            display_name=str(fields.get("display_name", "")),
            description=str(fields.get("description", "")),
            value_type=str(fields.get("value_type", "")),
            approved_aliases=_json_string_tuple(fields.get("aliases_json")),
            negative_aliases=_json_string_tuple(fields.get("negative_aliases_json")),
            context_anchors=_json_string_tuple(fields.get("context_anchors_json")),
            dense_index_text=str(fields.get("dense_content", "")),
            sparse_index_text=str(fields.get("sparse_content", "")),
            index_version=index_version,
            source_fingerprint=str(fields.get("source_fingerprint", "")),
        )

    def _scope_filter(
        self,
        scope: FieldSemanticIndexScope,
        index_version: IndexVersion,
    ) -> str:
        return " and ".join(
            (
                f"tenant_id == {_expr_string(scope.tenant_id)}",
                f"document_type == {_expr_string(scope.document_type)}",
                f"schema_version == {_expr_string(scope.schema_version)}",
                f"catalog_version == {_expr_string(scope.catalog_version.value)}",
                f"index_version == {_expr_string(index_version.value)}",
                "is_valid == true",
            )
        )

    def _build_ranker(
        self,
        sdk: Mapping[str, Any],
        options: FieldSemanticSearchOptions,
    ) -> Any:
        if options.fusion_strategy == "rrf":
            params: dict[str, Any] = {"reranker": "rrf", "k": 100}
        else:
            total = options.dense_weight + options.sparse_weight
            params = {
                "reranker": "weighted",
                "weights": [
                    options.dense_weight / total,
                    options.sparse_weight / total,
                ],
                "norm_score": True,
            }
        return sdk["Function"](
            name="field_semantic_hybrid_reranker",
            input_field_names=[],
            function_type=sdk["FunctionType"].RERANK,
            params=params,
        )

    async def _channel_search(
        self,
        collection: str,
        field: str,
        data: list[Any],
        expression: str,
        limit: int,
        metric_type: str,
        params: Mapping[str, Any],
    ) -> Any:
        return await self._call(
            f"search_{field}",
            lambda: self._client_required().search(
                collection_name=collection,
                data=data,
                anns_field=field,
                search_params={"metric_type": metric_type, "params": dict(params)},
                filter=expression,
                limit=limit,
                output_fields=self._output_fields(),
                timeout=self._operation_timeout,
            ),
        )

    def _collection_for_projection(
        self,
        projection: FieldSemanticVectorProjection,
    ) -> str:
        document = projection.document
        if len(projection.dense_embedding) != self._dense_dimension:
            raise ValueError("Field semantic dense vector dimension is invalid")
        return self._resolver.resolve(document.scope.tenant_id, document.index_version)

    def _tenant_alias(self, tenant_id: str) -> str:
        digest = sha256(tenant_id.encode("utf-8")).hexdigest()[:12]
        normalized_base = _milvus_identifier(self._alias)
        return f"{normalized_base[:207]}_{digest}"

    def _validate_lengths(self, entity: Mapping[str, Any]) -> None:
        limits = {
            "semantic_id": 128,
            "tenant_id": 256,
            "document_type": 256,
            "canonical_field_path": 1024,
            "schema_version": 128,
            "catalog_version": 256,
            "index_version": 256,
            "display_name": 4096,
            "description": self._max_text_length,
            "value_type": 16_384,
            "aliases_json": self._max_text_length,
            "negative_aliases_json": self._max_text_length,
            "context_anchors_json": self._max_text_length,
            "dense_content": self._max_text_length,
            "sparse_content": self._max_text_length,
            "source_fingerprint": 128,
            "projection_checksum": 128,
        }
        for field_name, limit in limits.items():
            if len(str(entity.get(field_name, "")).encode("utf-8")) > limit:
                raise ValueError(f"Milvus field {field_name} exceeds max_length")

    @staticmethod
    def _output_fields() -> list[str]:
        return [
            "semantic_id",
            "tenant_id",
            "document_type",
            "canonical_field_path",
            "schema_version",
            "catalog_version",
            "index_version",
            "is_valid",
            "display_name",
            "description",
            "value_type",
            "aliases_json",
            "negative_aliases_json",
            "context_anchors_json",
            "dense_content",
            "sparse_content",
            "source_fingerprint",
            "projection_checksum",
        ]

    async def _call(self, operation: str, function: Callable[[], Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await asyncio.to_thread(function)
            except Exception as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    logger.warning("Milvus field semantic operation failed: %s", operation)
                    raise MilvusIndexError(
                        f"Milvus field semantic operation failed: {operation}"
                    ) from exc
                await asyncio.sleep(min(0.25 * (2**attempt), 4.0))
        raise MilvusIndexError(
            f"Milvus field semantic operation failed: {operation}"
        ) from last_error

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
        except ImportError as exc:
            raise MilvusIndexError("pymilvus is required for field semantics") from exc
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
        }


def _chunks(
    values: Sequence[dict[str, Any]],
    size: int,
) -> tuple[tuple[dict[str, Any], ...], ...]:
    return tuple(tuple(values[index : index + size]) for index in range(0, len(values), size))


def _milvus_identifier(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", value.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        raise ValueError("Milvus identifier must contain an alphanumeric character")
    if not re.match(r"^[A-Za-z_]", normalized):
        normalized = f"f_{normalized}"
    return normalized[:220]


def _chunks_ids(values: Sequence[str], size: int) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(values[index : index + size]) for index in range(0, len(values), size))


def _sparse_payload(vector: SparseVector) -> dict[int, float]:
    return {int(index): float(weight) for index, weight in vector}


def _expr_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _contains_inline_image(value: str) -> bool:
    lowered = value.casefold()
    return "base64," in lowered or "data:image" in lowered


def _first_result_list(results: Any) -> list[Any]:
    if isinstance(results, list) and results and isinstance(results[0], list):
        return results[0]
    return list(results) if isinstance(results, list) else []


def _hit_fields(hit: Any) -> Mapping[str, Any]:
    if isinstance(hit, Mapping):
        entity = hit.get("entity")
        return entity if isinstance(entity, Mapping) else hit
    fields = getattr(hit, "fields", None)
    return fields if isinstance(fields, Mapping) else {}


def _hit_id(hit: Any) -> str:
    fields = _hit_fields(hit)
    value = fields.get("semantic_id")
    if value is None:
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
        semantic_id: score
        for hit in _first_result_list(results)
        if (semantic_id := _hit_id(hit))
        if (score := _hit_score(hit)) is not None
    }


def _json_string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise MilvusIndexError("Milvus field semantic list is not serialized text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MilvusIndexError("Milvus field semantic list is invalid JSON") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise MilvusIndexError("Milvus field semantic list has an invalid shape")
    return tuple(parsed)
