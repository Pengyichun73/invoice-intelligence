"""Multi-category hybrid retrieval for human-reviewed invoice examples."""

import json
import logging
import math
import time
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from invoice_intelligence.application.ports.admission import MemoryAdmissionRepository
from invoice_intelligence.application.ports.examples import (
    DenseEmbeddingProvider,
    ExampleIndexStore,
    ExampleRedactor,
    HybridSearchOptions,
    IndexProjectionRepository,
    QueryRewriteProvider,
    RerankingProvider,
    SparseEmbeddingProvider,
)
from invoice_intelligence.application.ports.governance import (
    RetrievalTelemetryContext,
    RetrievalTelemetryRepository,
)
from invoice_intelligence.application.ports.memory import CorrectionScopeResolver
from invoice_intelligence.application.services.field_semantic_catalog import (
    FieldSemanticCatalog,
)
from invoice_intelligence.domain.examples import (
    ExampleLabelType,
    ExampleScope,
    IndexVersion,
    ModelVersion,
    PromptVersion,
    RetrievalContext,
    RetrievalPolicyVersion,
    RetrievalRecallSource,
    RetrievalScore,
    RetrievedExample,
    ReviewedExamplePromptContext,
    ReviewedExamplePromptReference,
    SparseVector,
)
from invoice_intelligence.domain.extraction import FieldEvidence, Readability
from invoice_intelligence.domain.governance import (
    RetrievalStageMetrics,
    RetrievalTrace,
)

logger = logging.getLogger(__name__)

_RETRIEVAL_LABELS = (
    ExampleLabelType.CONFIRMED_CORRECT,
    ExampleLabelType.CORRECTED,
    ExampleLabelType.CONFIRMED_INCORRECT,
)
_RECALL_SOURCE_ORDER = (
    RetrievalRecallSource.DENSE,
    RetrievalRecallSource.SPARSE,
    RetrievalRecallSource.HYBRID,
)


@dataclass(frozen=True, slots=True)
class HybridRetrievalInput:
    """Current field observations used only to retrieve scoped historical priors."""

    tenant_id: str
    document_type: str
    field_path: str
    schema_version: str
    catalog_version: str
    field_evidence: FieldEvidence
    vendor_fingerprint: str | None = None
    template_fingerprint: str | None = None
    not_before: datetime | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("tenant_id", self.tenant_id),
            ("document_type", self.document_type),
            ("field_path", self.field_path),
            ("schema_version", self.schema_version),
            ("catalog_version", self.catalog_version),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        if self.field_evidence.field_path != self.field_path:
            raise ValueError("field_evidence must match the requested field_path")
        for name, optional_value in (
            ("vendor_fingerprint", self.vendor_fingerprint),
            ("template_fingerprint", self.template_fingerprint),
        ):
            if optional_value is not None and (
                not optional_value.strip()
                or optional_value != optional_value.strip()
            ):
                raise ValueError(f"{name} must be non-empty and normalized when supplied")
        if self.not_before is not None and (
            self.not_before.tzinfo is None or self.not_before.utcoffset() is None
        ):
            raise ValueError("not_before must be timezone-aware when supplied")

    @property
    def scope(self) -> ExampleScope:
        return ExampleScope(
            tenant_id=self.tenant_id,
            document_type=self.document_type,
            field_path=self.field_path,
            schema_version=self.schema_version,
            catalog_version=self.catalog_version,
        )


@dataclass(frozen=True, slots=True)
class CategoryRetrievalPolicy:
    """Bounded retrieval and rerank policy for one human-reviewed label."""

    candidate_k: int
    top_k: int
    min_relevance_score: float
    dense_weight: float
    sparse_weight: float

    def __post_init__(self) -> None:
        if self.candidate_k <= 0 or self.top_k <= 0:
            raise ValueError("Retrieval candidate_k and top_k must be greater than zero")
        if self.top_k > self.candidate_k:
            raise ValueError("Retrieval top_k must not exceed candidate_k")
        values = (
            self.min_relevance_score,
            self.dense_weight,
            self.sparse_weight,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Retrieval scores and weights must be finite")
        if self.dense_weight < 0 or self.sparse_weight < 0:
            raise ValueError("Retrieval weights must not be negative")
        if self.dense_weight + self.sparse_weight <= 0:
            raise ValueError("At least one retrieval weight must be greater than zero")


@dataclass(frozen=True, slots=True)
class HybridRetrievalPolicy:
    """Versioned defaults and exact field-path overrides for all label classes."""

    fusion_strategy: Literal["weighted", "rrf"]
    defaults: Mapping[ExampleLabelType, CategoryRetrievalPolicy]
    field_overrides: Mapping[
        str,
        Mapping[ExampleLabelType, CategoryRetrievalPolicy],
    ]
    version: RetrievalPolicyVersion

    def __post_init__(self) -> None:
        if self.fusion_strategy not in {"weighted", "rrf"}:
            raise ValueError("Unsupported retrieval fusion strategy")
        if set(self.defaults) != set(_RETRIEVAL_LABELS):
            raise ValueError("Retrieval defaults must configure all reviewed label classes")
        for field_path, overrides in self.field_overrides.items():
            if not field_path.strip() or field_path != field_path.strip():
                raise ValueError("Retrieval field override paths must be normalized")
            if not overrides:
                raise ValueError("Retrieval field overrides must not be empty")
            if any(label not in _RETRIEVAL_LABELS for label in overrides):
                raise ValueError("Retrieval field override contains an unsupported label")

    def for_field(
        self,
        field_path: str,
        label_type: ExampleLabelType,
    ) -> CategoryRetrievalPolicy:
        return self.field_overrides.get(field_path, {}).get(
            label_type,
            self.defaults[label_type],
        )


@dataclass(frozen=True, slots=True)
class _CategoryOutcome:
    results: tuple[RetrievedExample, ...]
    dense_candidate_count: int
    sparse_candidate_count: int
    rerank_candidate_count: int
    hybrid_search_ms: float
    rerank_ms: float


class HybridExampleRetrievalService:
    """Retrieve reviewed priors without choosing invoice values or workflow routes."""

    def __init__(
        self,
        *,
        projection_repository: IndexProjectionRepository,
        admission_repository: MemoryAdmissionRepository,
        index_store: ExampleIndexStore,
        redactor: ExampleRedactor,
        dense_embedding_provider: DenseEmbeddingProvider,
        sparse_embedding_provider: SparseEmbeddingProvider | None,
        reranking_provider: RerankingProvider,
        query_rewrite_provider: QueryRewriteProvider | None,
        policy: HybridRetrievalPolicy,
        dense_model_version: ModelVersion,
        sparse_model_version: ModelVersion | None,
        rerank_model_version: ModelVersion,
        prompt_version: PromptVersion,
        query_facts_resolver: CorrectionScopeResolver,
        field_semantic_catalog: FieldSemanticCatalog[Any],
        schema_version: str,
        feature_fingerprint_salt: str,
        max_fields: int,
        retention_days: int | None = None,
        telemetry_repository: RetrievalTelemetryRepository | None = None,
        telemetry_context: RetrievalTelemetryContext | None = None,
        threshold_version: str | None = None,
    ) -> None:
        if not schema_version.strip():
            raise ValueError("Example retrieval Schema version must not be empty")
        if not feature_fingerprint_salt:
            raise ValueError("Example retrieval feature fingerprint salt is required")
        if max_fields <= 0:
            raise ValueError("Example retrieval max_fields must be greater than zero")
        if retention_days is not None and retention_days <= 0:
            raise ValueError("retention_days must be greater than zero when configured")
        self._projection_repository = projection_repository
        self._admissions = admission_repository
        self._index_store = index_store
        self._redactor = redactor
        self._dense_embeddings = dense_embedding_provider
        self._sparse_embeddings = sparse_embedding_provider
        self._reranker = reranking_provider
        self._query_rewriter = query_rewrite_provider
        self._policy = policy
        self._dense_model_version = dense_model_version
        self._sparse_model_version = sparse_model_version
        self._rerank_model_version = rerank_model_version
        self._prompt_version = prompt_version
        self._query_facts_resolver = query_facts_resolver
        self._field_semantic_catalog = field_semantic_catalog
        self._schema_version = schema_version
        self._feature_fingerprint_salt = feature_fingerprint_salt
        self._max_fields = max_fields
        self._retention_days = retention_days
        self._telemetry_repository = telemetry_repository
        self._telemetry_context = telemetry_context
        self._threshold_version = threshold_version or policy.version.value

    async def retrieve(self, request: HybridRetrievalInput) -> RetrievalContext:
        """Return bounded positive examples and separately isolated hard negatives."""

        scope = request.scope
        trace_id = str(uuid4())
        created_at = datetime.now(UTC)
        started_at = time.perf_counter()
        index_version: IndexVersion | None = None
        stage = {
            "redaction_ms": 0.0,
            "index_lookup_ms": 0.0,
            "rewrite_ms": 0.0,
            "dense_embedding_ms": 0.0,
            "sparse_embedding_ms": 0.0,
            "hybrid_search_ms": 0.0,
            "rerank_ms": 0.0,
        }
        outcomes: dict[ExampleLabelType, _CategoryOutcome] = {}
        binding = (
            self._telemetry_context.bind(trace_id, scope.tenant_id)
            if self._telemetry_context is not None
            else nullcontext()
        )
        try:
            with binding:
                stage_started = time.perf_counter()
                redacted_query = self._redactor.redact_query(
                    scope,
                    request.field_evidence,
                    request.vendor_fingerprint,
                    request.template_fingerprint,
                )
                stage["redaction_ms"] = self._elapsed_ms(stage_started)

                stage_started = time.perf_counter()
                index_version = await self._projection_repository.get_active_version(
                    scope.tenant_id
                )
                stage["index_lookup_ms"] = self._elapsed_ms(stage_started)
                if index_version is None:
                    context = self._empty_context(trace_id, scope, None)
                    await self._persist_trace(
                        context,
                        outcomes,
                        stage,
                        created_at,
                        started_at,
                        succeeded=True,
                        error_code=None,
                    )
                    self._clear_trace(trace_id)
                    return context

                stage_started = time.perf_counter()
                query = await self._rewrite_or_original(redacted_query, scope)
                stage["rewrite_ms"] = self._elapsed_ms(stage_started)

                stage_started = time.perf_counter()
                dense_embedding = await self._embed_dense(query)
                stage["dense_embedding_ms"] = self._elapsed_ms(stage_started)

                stage_started = time.perf_counter()
                sparse_embedding = await self._embed_sparse(query)
                stage["sparse_embedding_ms"] = self._elapsed_ms(stage_started)

                for label_type in _RETRIEVAL_LABELS:
                    outcome = await self._retrieve_category(
                        scope=scope,
                        query=query,
                        dense_embedding=dense_embedding,
                        sparse_embedding=sparse_embedding,
                        index_version=index_version,
                        label_type=label_type,
                        template_fingerprint=request.template_fingerprint,
                        not_before=request.not_before,
                    )
                    outcomes[label_type] = outcome
                    stage["hybrid_search_ms"] += outcome.hybrid_search_ms
                    stage["rerank_ms"] += outcome.rerank_ms
        except Exception as exc:
            logger.warning(
                "Reviewed-example retrieval failed; continuing without historical context"
            )
            context = self._empty_context(trace_id, scope, index_version)
            await self._persist_trace(
                context,
                outcomes,
                stage,
                created_at,
                started_at,
                succeeded=False,
                error_code=self._safe_error_code(exc),
            )
            self._clear_trace(trace_id)
            return context

        positives = (
            outcomes[ExampleLabelType.CONFIRMED_CORRECT].results
            + outcomes[ExampleLabelType.CORRECTED].results
        )
        hard_negatives = outcomes[ExampleLabelType.CONFIRMED_INCORRECT].results
        context = RetrievalContext(
            trace_id=trace_id,
            scope=scope,
            examples=positives,
            hard_negatives=hard_negatives,
            hypotheses=(),
            index_version=index_version,
            dense_model_version=self._dense_model_version,
            sparse_model_version=self._sparse_model_version,
            rerank_model_version=self._rerank_model_version,
            prompt_version=self._prompt_version,
            retrieval_policy_version=self._policy.version,
            retrieved_at=datetime.now(UTC),
        )
        await self._persist_trace(
            context,
            outcomes,
            stage,
            created_at,
            started_at,
            succeeded=True,
            error_code=None,
        )
        self._clear_trace(trace_id)
        return context

    async def retrieve_for_extraction(
        self,
        tenant_id: str,
        invoice: object | None,
        field_evidence: Sequence[FieldEvidence],
    ) -> ReviewedExamplePromptContext | None:
        """Retrieve a bounded Prompt projection for current baseline field evidence."""

        normalized_tenant_id = tenant_id.strip()
        if not normalized_tenant_id or normalized_tenant_id != tenant_id:
            raise ValueError("tenant_id must be non-empty and normalized")
        try:
            facts = self._query_facts_resolver.current_facts(invoice)
            if facts is None:
                return None
            catalog_version = (
                await self._field_semantic_catalog.prompt_catalog(normalized_tenant_id)
            ).catalog_version.value
            not_before = (
                datetime.now(UTC) - timedelta(days=self._retention_days)
                if self._retention_days is not None
                else None
            )
            vendor_fingerprint = self._feature_fingerprint(
                normalized_tenant_id,
                "vendor",
                facts.vendor_features,
            )
            template_fingerprint = self._feature_fingerprint(
                normalized_tenant_id,
                "template",
                facts.template_features,
            )
            contexts: list[RetrievalContext] = []
            for evidence in self._select_evidence(field_evidence):
                if evidence.field_path not in facts.field_values:
                    continue
                context = await self.retrieve(
                    HybridRetrievalInput(
                        tenant_id=normalized_tenant_id,
                        document_type=facts.document_type,
                        field_path=evidence.field_path,
                        schema_version=self._schema_version,
                        catalog_version=catalog_version,
                        field_evidence=evidence,
                        vendor_fingerprint=vendor_fingerprint,
                        template_fingerprint=template_fingerprint,
                        not_before=not_before,
                    )
                )
                contexts.append(context)
            return self._to_prompt_context(tuple(contexts))
        except Exception:
            logger.warning(
                "Extraction example retrieval failed; continuing without reviewed cases"
            )
            return None

    async def _retrieve_category(
        self,
        *,
        scope: ExampleScope,
        query: str,
        dense_embedding: tuple[float, ...],
        sparse_embedding: SparseVector | None,
        index_version: IndexVersion,
        label_type: ExampleLabelType,
        template_fingerprint: str | None,
        not_before: datetime | None,
    ) -> _CategoryOutcome:
        policy = self._policy.for_field(scope.field_path, label_type)
        search_started = time.perf_counter()
        recalled = await self._index_store.hybrid_search(
            scope,
            query,
            dense_embedding,
            sparse_embedding,
            index_version,
            HybridSearchOptions(
                label_types=(label_type,),
                limit=policy.candidate_k,
                fusion_strategy=self._policy.fusion_strategy,
                dense_weight=policy.dense_weight,
                sparse_weight=policy.sparse_weight,
                template_fingerprint=template_fingerprint,
                not_before=not_before,
            ),
        )
        approved_ids = set(
            await self._admissions.filter_approved_example_ids(
                scope.tenant_id,
                tuple(item.candidate.example_id for item in recalled),
            )
        )
        recalled = tuple(
            item for item in recalled if item.candidate.example_id in approved_ids
        )
        hybrid_search_ms = self._elapsed_ms(search_started)
        dense_count = sum(
            RetrievalRecallSource.DENSE in item.recall_sources for item in recalled
        )
        sparse_count = sum(
            RetrievalRecallSource.SPARSE in item.recall_sources for item in recalled
        )
        candidates = self._deduplicate(recalled, label_type)
        if label_type is ExampleLabelType.CORRECTED:
            candidates = tuple(
                item
                for item in candidates
                if item.candidate.redacted_correction_reason is not None
            )
        if not candidates:
            return _CategoryOutcome(
                results=(),
                dense_candidate_count=dense_count,
                sparse_candidate_count=sparse_count,
                rerank_candidate_count=0,
                hybrid_search_ms=hybrid_search_ms,
                rerank_ms=0.0,
            )
        rerank_started = time.perf_counter()
        reranked = await self._reranker.rerank(
            query,
            tuple(item.candidate for item in candidates),
            len(candidates),
        )
        rerank_ms = self._elapsed_ms(rerank_started)
        by_id = {item.candidate.example_id: item for item in candidates}
        rerank_ids = tuple(example_id for example_id, _ in reranked)
        if len(rerank_ids) != len(set(rerank_ids)) or any(
            example_id not in by_id for example_id in rerank_ids
        ):
            raise ValueError("Reranker returned invalid reviewed-example identifiers")
        selected: list[RetrievedExample] = []
        for example_id, relevance_score in reranked:
            if relevance_score < policy.min_relevance_score:
                continue
            item = by_id[example_id]
            selected.append(
                replace(
                    item,
                    score=replace(item.score, rerank=relevance_score),
                    rank=len(selected) + 1,
                )
            )
            if len(selected) >= policy.top_k:
                break
        return _CategoryOutcome(
            results=tuple(selected),
            dense_candidate_count=dense_count,
            sparse_candidate_count=sparse_count,
            rerank_candidate_count=len(candidates),
            hybrid_search_ms=hybrid_search_ms,
            rerank_ms=rerank_ms,
        )

    def _select_evidence(
        self,
        evidence: Sequence[FieldEvidence],
    ) -> tuple[FieldEvidence, ...]:
        prioritized = sorted(
            evidence,
            key=lambda item: (
                0
                if (
                    item.ambiguous
                    or item.readability is not Readability.READABLE
                    or len(item.candidate_values) != 1
                )
                else 1,
                item.field_path,
            ),
        )
        return tuple(prioritized[: self._max_fields])

    def _feature_fingerprint(
        self,
        tenant_id: str,
        feature_kind: str,
        features: Mapping[str, object],
    ) -> str | None:
        if not features:
            return None
        canonical = json.dumps(
            dict(features),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = sha256(
            (
                f"{self._feature_fingerprint_salt}\0tenant:{tenant_id}"
                f"\0{feature_kind}\0{canonical}"
            ).encode("utf-8")
        ).hexdigest()
        return f"sha256:{digest}"

    def _to_prompt_context(
        self,
        contexts: tuple[RetrievalContext, ...],
    ) -> ReviewedExamplePromptContext | None:
        correct: list[ReviewedExamplePromptReference] = []
        corrected: list[ReviewedExamplePromptReference] = []
        negatives: list[ReviewedExamplePromptReference] = []
        for context in contexts:
            for item in context.examples:
                reference = self._prompt_reference(item)
                if reference.label_type is ExampleLabelType.CONFIRMED_CORRECT:
                    correct.append(reference)
                elif reference.label_type is ExampleLabelType.CORRECTED:
                    corrected.append(reference)
            negatives.extend(
                self._prompt_reference(item) for item in context.hard_negatives
            )
        if not contexts:
            return None
        return ReviewedExamplePromptContext(
            trace_ids=tuple(context.trace_id for context in contexts),
            verified_correct_examples=tuple(correct),
            reviewed_correction_examples=tuple(corrected),
            reviewed_negative_examples=tuple(negatives),
            conflicting_field_paths=self._conflicting_fields(contexts),
            retrieval_policy_version=self._policy.version,
        )

    @staticmethod
    def _prompt_reference(item: RetrievedExample) -> ReviewedExamplePromptReference:
        candidate = item.candidate
        return ReviewedExamplePromptReference(
            example_id=candidate.example_id,
            document_type=candidate.scope.document_type,
            field_path=candidate.scope.field_path,
            schema_version=candidate.scope.schema_version,
            label_type=candidate.label_type,
            model_value=candidate.redacted_model_value,
            reviewed_value=candidate.redacted_reviewed_value,
            correction_reason=candidate.redacted_correction_reason,
            index_version=candidate.index_version,
        )

    @classmethod
    def _conflicting_fields(
        cls,
        contexts: tuple[RetrievalContext, ...],
    ) -> tuple[str, ...]:
        conflicts: set[str] = set()
        for context in contexts:
            reviewed_by_model_value: dict[str, set[str]] = {}
            positive_values: set[str] = set()
            for item in context.examples:
                candidate = item.candidate
                model_key = cls._canonical(candidate.redacted_model_value)
                reviewed_key = cls._canonical(candidate.redacted_reviewed_value)
                reviewed_by_model_value.setdefault(model_key, set()).add(reviewed_key)
                positive_values.add(reviewed_key)
            negative_values = {
                cls._canonical(item.candidate.redacted_model_value)
                for item in context.hard_negatives
            }
            if (
                any(len(values) > 1 for values in reviewed_by_model_value.values())
                or not positive_values.isdisjoint(negative_values)
            ):
                conflicts.add(context.scope.field_path)
        return tuple(sorted(conflicts))

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    async def _rewrite_or_original(self, query: str, scope: ExampleScope) -> str:
        if self._query_rewriter is None:
            return query
        try:
            rewritten = await self._query_rewriter.rewrite(query, scope)
            if rewritten.redacted_source_query != query:
                raise ValueError("Query rewriter changed the source-query identity")
            return "\n".join(
                (
                    query,
                    "QUERY_REWRITE_HINT",
                    "REWRITTEN_RETRIEVAL_QUERY",
                    rewritten.rewritten_query,
                    "EXPANSION_TERMS",
                    json.dumps(
                        rewritten.expansion_terms,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            )
        except Exception:
            logger.warning(
                "Reviewed-example query rewrite failed; using redacted source query"
            )
            return query

    async def _embed_dense(self, query: str) -> tuple[float, ...]:
        embeddings = await self._dense_embeddings.embed((query,))
        if len(embeddings) != 1 or not embeddings[0]:
            raise ValueError("Dense embedding provider returned an invalid query vector")
        return tuple(float(value) for value in embeddings[0])

    async def _embed_sparse(self, query: str) -> SparseVector | None:
        if self._sparse_embeddings is None:
            return None
        embeddings = await self._sparse_embeddings.embed((query,))
        if len(embeddings) != 1:
            raise ValueError("Sparse embedding provider returned an invalid query vector")
        return tuple(
            sorted(
                ((int(index), float(weight)) for index, weight in embeddings[0]),
                key=lambda item: item[0],
            )
        )

    @staticmethod
    def _deduplicate(
        recalled: tuple[RetrievedExample, ...],
        label_type: ExampleLabelType,
    ) -> tuple[RetrievedExample, ...]:
        by_id: dict[str, RetrievedExample] = {}
        for item in recalled:
            if item.candidate.label_type is not label_type:
                raise ValueError("Index returned a reviewed example outside its label filter")
            example_id = item.candidate.example_id
            existing = by_id.get(example_id)
            if existing is None:
                by_id[example_id] = item
                continue
            if existing.candidate != item.candidate:
                raise ValueError("Duplicate index hits disagree on candidate content")
            sources = set(existing.recall_sources) | set(item.recall_sources)
            by_id[example_id] = replace(
                existing,
                score=RetrievalScore(
                    dense=_max_optional(existing.score.dense, item.score.dense),
                    sparse=_max_optional(existing.score.sparse, item.score.sparse),
                    fusion=_max_optional(existing.score.fusion, item.score.fusion),
                    rerank=None,
                ),
                rank=min(existing.rank, item.rank),
                recall_sources=tuple(
                    source for source in _RECALL_SOURCE_ORDER if source in sources
                ),
            )
        return tuple(sorted(by_id.values(), key=lambda item: item.rank))

    def _empty_context(
        self,
        trace_id: str,
        scope: ExampleScope,
        index_version: IndexVersion | None,
    ) -> RetrievalContext:
        return RetrievalContext(
            trace_id=trace_id,
            scope=scope,
            examples=(),
            hard_negatives=(),
            hypotheses=(),
            index_version=index_version,
            dense_model_version=self._dense_model_version,
            sparse_model_version=self._sparse_model_version,
            rerank_model_version=self._rerank_model_version,
            prompt_version=self._prompt_version,
            retrieval_policy_version=self._policy.version,
            retrieved_at=datetime.now(UTC),
        )

    async def _persist_trace(
        self,
        context: RetrievalContext,
        outcomes: Mapping[ExampleLabelType, _CategoryOutcome],
        stage: Mapping[str, float],
        created_at: datetime,
        started_at: float,
        *,
        succeeded: bool,
        error_code: str | None,
    ) -> None:
        if self._telemetry_repository is None:
            return
        positive_ids = tuple(item.candidate.example_id for item in context.examples)
        negative_ids = tuple(item.candidate.example_id for item in context.hard_negatives)
        result_count = len(positive_ids) + len(negative_ids)
        remote = (
            self._telemetry_context.snapshot(context.trace_id)
            if self._telemetry_context is not None
            else None
        )
        trace = RetrievalTrace(
            trace_id=context.trace_id,
            tenant_id=context.scope.tenant_id,
            document_type=context.scope.document_type,
            field_path=context.scope.field_path,
            schema_version=context.scope.schema_version,
            index_version=context.index_version,
            dense_model_version=context.dense_model_version,
            sparse_model_version=context.sparse_model_version,
            rerank_model_version=context.rerank_model_version,
            prompt_version=context.prompt_version,
            retrieval_policy_version=context.retrieval_policy_version,
            threshold_version=self._threshold_version,
            stage_metrics=RetrievalStageMetrics(
                redaction_ms=stage["redaction_ms"],
                index_lookup_ms=stage["index_lookup_ms"],
                rewrite_ms=stage["rewrite_ms"],
                dense_embedding_ms=stage["dense_embedding_ms"],
                sparse_embedding_ms=stage["sparse_embedding_ms"],
                hybrid_search_ms=stage["hybrid_search_ms"],
                rerank_ms=stage["rerank_ms"],
                total_ms=self._elapsed_ms(started_at),
            ),
            dense_candidate_count=sum(item.dense_candidate_count for item in outcomes.values()),
            sparse_candidate_count=sum(
                item.sparse_candidate_count for item in outcomes.values()
            ),
            rerank_candidate_count=sum(
                item.rerank_candidate_count for item in outcomes.values()
            ),
            positive_result_count=len(positive_ids),
            negative_result_count=len(negative_ids),
            positive_example_ids=positive_ids,
            negative_example_ids=negative_ids,
            empty_retrieval=result_count == 0,
            positive_hit_rate=(len(positive_ids) / result_count if result_count else 0.0),
            negative_hit_rate=(len(negative_ids) / result_count if result_count else 0.0),
            review_required=None,
            remote_model_error_count=remote.error_count if remote is not None else 0,
            input_tokens=remote.input_tokens if remote is not None else None,
            output_tokens=remote.output_tokens if remote is not None else None,
            estimated_cost=remote.estimated_cost if remote is not None else None,
            succeeded=succeeded,
            error_code=error_code,
            created_at=created_at,
            completed_at=datetime.now(UTC),
        )
        try:
            await self._telemetry_repository.save_trace(trace)
        except Exception:
            logger.warning("Unable to persist reviewed-example retrieval telemetry")
        else:
            logger.info(
                "reviewed_example_retrieval",
                extra={
                    "trace_id": trace.trace_id,
                    "tenant_id": trace.tenant_id,
                    "document_type": trace.document_type,
                    "field_path": trace.field_path,
                    "schema_version": trace.schema_version,
                    "index_version": (
                        trace.index_version.value if trace.index_version else None
                    ),
                    "dense_candidate_count": trace.dense_candidate_count,
                    "sparse_candidate_count": trace.sparse_candidate_count,
                    "empty_retrieval": trace.empty_retrieval,
                    "succeeded": trace.succeeded,
                },
            )

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return max(0.0, (time.perf_counter() - started_at) * 1000)

    @staticmethod
    def _safe_error_code(error: Exception) -> str:
        name = type(error).__name__.strip() or "RetrievalError"
        return f"retrieval_{name[:112]}"

    def _clear_trace(self, trace_id: str) -> None:
        if self._telemetry_context is not None:
            self._telemetry_context.clear(trace_id)


def _max_optional(left: float | None, right: float | None) -> float | None:
    values = tuple(value for value in (left, right) if value is not None)
    return max(values) if values else None
