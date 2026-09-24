"""Deterministic field binding over approved catalog metadata and hybrid retrieval."""

import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Generic, Literal, TypeVar

from invoice_intelligence.application.errors import HumanCorrectionError
from invoice_intelligence.application.ports.field_semantics import (
    FieldSemanticDenseEmbeddingProvider,
    FieldSemanticIndexStore,
    FieldSemanticProjectionRepository,
    FieldSemanticRerankCandidate,
    FieldSemanticRerankingProvider,
    FieldSemanticSearchOptions,
    FieldSemanticSparseEmbeddingProvider,
)
from invoice_intelligence.application.ports.memory import CorrectionScopeResolver
from invoice_intelligence.application.services.field_alias_learning import (
    FieldAliasLearningService,
)
from invoice_intelligence.application.services.field_semantic_catalog import (
    FieldSemanticCatalog,
)
from invoice_intelligence.domain.examples import IndexVersion, RetrievalScore, SparseVector
from invoice_intelligence.domain.field_semantics import (
    FieldBindingCandidate,
    FieldBindingDecision,
    FieldBindingDecisionAuthority,
    FieldBindingEvidence,
    FieldBindingReviewDecision,
    FieldBindingStatus,
    FieldContextAnchor,
    FieldContextObservation,
    FieldSemanticCatalogVersion,
    FieldSemanticDefinition,
    FieldSemanticIndexScope,
    RetrievedFieldSemantic,
    canonical_field_path_template,
    normalize_field_label,
)
from invoice_intelligence.domain.workflow import WorkflowIdentity

logger = logging.getLogger(__name__)
SchemaT = TypeVar("SchemaT")


@dataclass(frozen=True, slots=True)
class FieldSemanticBindingRequest:
    """Tenant-scoped current-image label evidence; no historical field value is accepted."""

    tenant_id: str
    document_type: str
    schema_version: str
    evidence: FieldBindingEvidence

    def __post_init__(self) -> None:
        for name, value in (
            ("tenant_id", self.tenant_id),
            ("document_type", self.document_type),
            ("schema_version", self.schema_version),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")


@dataclass(frozen=True, slots=True)
class FieldSemanticBindingPolicy:
    """Versioned thresholds and weights for non-probabilistic binding scores."""

    candidate_k: int
    result_limit: int
    fusion_strategy: Literal["weighted", "rrf"]
    dense_weight: float
    sparse_weight: float
    min_candidate_score: float
    acceptance_score: float
    min_score_margin: float
    min_rerank_score: float
    exact_weight: float
    retrieval_weight: float
    rerank_weight: float
    context_weight: float
    position_weight: float
    value_type_weight: float
    require_value_type_for_acceptance: bool
    version: str

    def __post_init__(self) -> None:
        if self.candidate_k < 2 or self.result_limit < 2:
            raise ValueError(
                "Field binding requires at least two candidates and two visible results"
            )
        if self.result_limit > self.candidate_k:
            raise ValueError("Field binding result_limit must not exceed candidate_k")
        if self.fusion_strategy not in {"weighted", "rrf"}:
            raise ValueError("Unsupported field binding fusion strategy")
        bounded = (
            self.min_candidate_score,
            self.acceptance_score,
            self.min_score_margin,
        )
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in bounded):
            raise ValueError("Field binding thresholds must be finite scores from zero to one")
        if self.min_candidate_score > self.acceptance_score:
            raise ValueError("Field binding candidate threshold must not exceed acceptance")
        if not math.isfinite(self.min_rerank_score):
            raise ValueError("Field binding rerank threshold must be finite")
        weights = self.component_weights
        if any(not math.isfinite(value) or value < 0.0 for value in weights):
            raise ValueError("Field binding component weights must be finite and non-negative")
        if sum(weights) <= 0.0:
            raise ValueError("Field binding requires at least one positive component weight")
        if self.dense_weight < 0.0 or self.sparse_weight < 0.0:
            raise ValueError("Field binding retrieval weights must be non-negative")
        if self.dense_weight + self.sparse_weight <= 0.0:
            raise ValueError("Field binding requires Dense or Sparse retrieval weight")
        if not self.version.strip() or self.version != self.version.strip():
            raise ValueError("Field binding policy version must be normalized")

    @property
    def component_weights(self) -> tuple[float, ...]:
        return (
            self.exact_weight,
            self.retrieval_weight,
            self.rerank_weight,
            self.context_weight,
            self.position_weight,
            self.value_type_weight,
        )


@dataclass(frozen=True, slots=True)
class _ContextAssessment:
    context_score: float
    position_score: float
    matched_anchor_ids: tuple[str, ...]
    negative_anchor_matched: bool
    required_alias_context_missing: bool


class FieldSemanticBindingService(Generic[SchemaT]):
    """Bind a current visual label to Schema paths without inferring its field value."""

    def __init__(
        self,
        *,
        catalog: FieldSemanticCatalog[SchemaT],
        projection_repository: FieldSemanticProjectionRepository,
        index_store: FieldSemanticIndexStore,
        dense_embedding_provider: FieldSemanticDenseEmbeddingProvider,
        sparse_embedding_provider: FieldSemanticSparseEmbeddingProvider | None,
        reranking_provider: FieldSemanticRerankingProvider,
        alias_learning_service: FieldAliasLearningService[SchemaT],
        query_facts_resolver: CorrectionScopeResolver,
        policy: FieldSemanticBindingPolicy,
        schema_version: str,
    ) -> None:
        if not schema_version.strip() or schema_version != schema_version.strip():
            raise ValueError("Field binding schema_version must be normalized")
        self._catalog = catalog
        self._projections = projection_repository
        self._index_store = index_store
        self._dense_embeddings = dense_embedding_provider
        self._sparse_embeddings = sparse_embedding_provider
        self._reranker = reranking_provider
        self._alias_learning = alias_learning_service
        self._query_facts_resolver = query_facts_resolver
        self._policy = policy
        self._schema_version = schema_version

    async def bind(self, request: FieldSemanticBindingRequest) -> FieldBindingDecision:
        """Return accepted, review_required, or unresolved with auditable score parts."""

        if request.schema_version != self._schema_version:
            raise ValueError("Field binding request does not use the current Schema version")
        active_index = await self._projections.get_active_version(
            request.tenant_id,
            request.schema_version,
        )
        requested_catalog_version = (
            active_index.catalog_version if active_index is not None else None
        )
        definitions = await self._catalog.list_definitions(
            request.tenant_id,
            document_type=request.document_type,
            catalog_version=requested_catalog_version,
        )
        definitions = self._validate_definitions(request, definitions)
        catalog_version = definitions[0].catalog_version
        evidence = self._normalize_evidence(request.evidence)
        if evidence is None:
            return self._decision(
                request=request,
                catalog_version=catalog_version,
                index_version=active_index.index_version if active_index else None,
                status=FieldBindingStatus.UNRESOLVED,
                candidates=(),
                selected_path=None,
                reason_codes=("field_binding.normalized_label_empty",),
            )

        exact_candidates = await self._catalog.find_binding_candidates(
            request.tenant_id,
            request.document_type,
            evidence,
            catalog_version=catalog_version,
        )
        exact_by_path = {
            item.canonical_field_path: item for item in exact_candidates
        }
        if active_index is None:
            return self._fallback_decision(
                request,
                evidence,
                definitions,
                exact_by_path,
                catalog_version,
                None,
                "field_binding.active_index_unavailable",
            )
        if (
            not active_index.is_valid
            or active_index.schema_version != request.schema_version
            or active_index.catalog_version != catalog_version
        ):
            return self._fallback_decision(
                request,
                evidence,
                definitions,
                exact_by_path,
                catalog_version,
                active_index.index_version,
                "field_binding.index_scope_mismatch",
            )

        query_text = self._query_text(evidence, definitions)
        try:
            dense_embedding = await self._embed_dense(query_text)
            sparse_embedding = await self._embed_sparse(query_text)
            search_scope = FieldSemanticIndexScope(
                tenant_id=request.tenant_id,
                document_type=request.document_type,
                schema_version=request.schema_version,
                catalog_version=catalog_version,
            )
            retrieved = await self._index_store.hybrid_search(
                search_scope,
                query_text,
                dense_embedding,
                sparse_embedding,
                active_index.index_version,
                FieldSemanticSearchOptions(
                    limit=self._policy.candidate_k,
                    fusion_strategy=self._policy.fusion_strategy,
                    dense_weight=self._policy.dense_weight,
                    sparse_weight=self._policy.sparse_weight,
                ),
            )
            retrieved_by_path = self._retrieved_by_path(
                retrieved,
                definitions,
                search_scope,
                active_index.index_version,
            )
            candidate_paths = self._candidate_paths(exact_candidates, retrieved)
            if not candidate_paths:
                return self._decision(
                    request=request,
                    catalog_version=catalog_version,
                    index_version=active_index.index_version,
                    status=FieldBindingStatus.UNRESOLVED,
                    candidates=(),
                    selected_path=None,
                    reason_codes=("field_binding.no_candidate",),
                )
            selected_definitions = tuple(
                self._definition_by_path(definitions, path) for path in candidate_paths
            )
            reranked = await self._rerank(query_text, selected_definitions)
            candidates = self._score_candidates(
                evidence=evidence,
                definitions=selected_definitions,
                exact_by_path=exact_by_path,
                retrieved_by_path=retrieved_by_path,
                reranked=reranked,
            )
        except Exception as exc:
            logger.warning(
                "Field semantic binding retrieval failed; automatic binding disabled",
                extra={
                    "tenant_id": request.tenant_id,
                    "document_type": request.document_type,
                    "schema_version": request.schema_version,
                    "error_type": type(exc).__name__,
                },
            )
            return self._fallback_decision(
                request,
                evidence,
                definitions,
                exact_by_path,
                catalog_version,
                active_index.index_version,
                "field_binding.retrieval_unavailable",
            )
        return self._materialize_runtime_decision(
            request,
            self._final_decision(
                request,
                catalog_version,
                active_index.index_version,
                candidates,
            ),
        )

    async def confirm_reviewed_binding(
        self,
        *,
        tenant_id: str,
        identity: WorkflowIdentity,
        reviewer_id: str,
        invoice: SchemaT | None,
        evidence: FieldBindingEvidence,
        review: FieldBindingReviewDecision,
    ) -> FieldBindingEvidence:
        """Apply a human mapping and idempotently submit a pending alias candidate."""

        summary = evidence.binding_decision
        if evidence.document_id != identity.document_id:
            raise HumanCorrectionError("Field binding evidence is outside this document")
        if review.evidence_id != evidence.evidence_id:
            raise HumanCorrectionError("Field binding review references different evidence")
        if summary is None:
            raise HumanCorrectionError("Field binding evidence has no reviewable decision")
        if summary.status is FieldBindingStatus.ACCEPTED:
            raise HumanCorrectionError("Accepted field bindings cannot be reviewed again")
        if (
            summary.status is FieldBindingStatus.REVIEW_REQUIRED
            and review.selected_canonical_field_path not in evidence.candidate_field_paths
        ):
            raise HumanCorrectionError(
                "Selected field path is not one of the review candidates"
            )
        if summary.schema_version != self._schema_version:
            raise HumanCorrectionError("Field binding review uses an inactive Schema version")
        definition_path = canonical_field_path_template(
            review.selected_canonical_field_path
        )
        definition = await self._catalog.get_definition(
            tenant_id,
            summary.document_type,
            definition_path,
            catalog_version=summary.catalog_version,
        )
        if definition is None or not definition.is_valid:
            raise HumanCorrectionError(
                "Selected field path is absent from the current Entity Schema"
            )

        facts = self._query_facts_resolver.current_facts(invoice)
        learning = await self._alias_learning.record_human_mapping(
            tenant_id=tenant_id,
            identity=identity,
            reviewer_id=reviewer_id,
            evidence=evidence,
            binding_decision_id=summary.decision_id,
            document_type=summary.document_type,
            canonical_field_path=definition_path,
            catalog_version=summary.catalog_version,
            reason=review.reason,
            template_features=facts.template_features if facts is not None else {},
        )
        human_decision_id = sha256(
            (
                f"human-field-binding\0{learning.support.support_id}\0{reviewer_id}"
            ).encode("utf-8")
        ).hexdigest()
        return replace(
            evidence,
            candidate_field_paths=tuple(
                dict.fromkeys(
                    (
                        *evidence.candidate_field_paths,
                        review.selected_canonical_field_path,
                    )
                )
            ),
            binding_decision=replace(
                summary,
                decision_id=human_decision_id,
                status=FieldBindingStatus.ACCEPTED,
                selected_canonical_field_path=review.selected_canonical_field_path,
                reason_codes=("field_binding.human_confirmed",),
                authority=FieldBindingDecisionAuthority.HUMAN_REVIEWER,
                requires_review=False,
            ),
        )

    def _final_decision(
        self,
        request: FieldSemanticBindingRequest,
        catalog_version: FieldSemanticCatalogVersion,
        index_version: IndexVersion,
        candidates: tuple[FieldBindingCandidate, ...],
    ) -> FieldBindingDecision:
        visible = candidates[: self._policy.result_limit]
        plausible = tuple(
            item
            for item in visible
            if item.combined_match_score >= self._policy.min_candidate_score
        )
        if not plausible:
            return self._decision(
                request=request,
                catalog_version=catalog_version,
                index_version=index_version,
                status=FieldBindingStatus.UNRESOLVED,
                candidates=visible,
                selected_path=None,
                reason_codes=("field_binding.no_qualified_candidate",),
            )
        top = plausible[0]
        second = plausible[1] if len(plausible) > 1 else None
        margin = (
            top.combined_match_score - second.combined_match_score
            if second is not None
            else top.combined_match_score
        )
        close_paths = tuple(
            item.canonical_field_path
            for item in plausible
            if top.combined_match_score - item.combined_match_score
            < self._policy.min_score_margin
        )
        if len(close_paths) > 1:
            close = set(close_paths)
            visible = tuple(
                replace(
                    item,
                    conflicting_field_paths=tuple(
                        path
                        for path in close_paths
                        if path != item.canonical_field_path
                    ),
                )
                if item.canonical_field_path in close
                else item
                for item in visible
            )
        accepted = (
            top.rules_passed
            and top.combined_match_score >= self._policy.acceptance_score
            and margin >= self._policy.min_score_margin
        )
        if accepted:
            return self._decision(
                request=request,
                catalog_version=catalog_version,
                index_version=index_version,
                status=FieldBindingStatus.ACCEPTED,
                candidates=visible,
                selected_path=top.canonical_field_path,
                reason_codes=(
                    "field_binding.accepted",
                    "field_binding.high_score_separation",
                ),
            )
        reasons: list[str] = []
        if len(close_paths) > 1:
            reasons.append("field_binding.ambiguous_candidates")
        if not top.rules_passed:
            reasons.extend(top.rule_failures)
        if top.combined_match_score < self._policy.acceptance_score:
            reasons.append("field_binding.score_below_acceptance")
        if margin < self._policy.min_score_margin:
            reasons.append("field_binding.margin_below_acceptance")
        return self._decision(
            request=request,
            catalog_version=catalog_version,
            index_version=index_version,
            status=FieldBindingStatus.REVIEW_REQUIRED,
            candidates=visible,
            selected_path=None,
            reason_codes=tuple(dict.fromkeys(reasons)),
        )

    def _score_candidates(
        self,
        *,
        evidence: FieldBindingEvidence,
        definitions: Sequence[FieldSemanticDefinition],
        exact_by_path: Mapping[str, FieldBindingCandidate],
        retrieved_by_path: Mapping[str, RetrievedFieldSemantic],
        reranked: tuple[tuple[str, float], ...],
    ) -> tuple[FieldBindingCandidate, ...]:
        rerank_scores = dict(reranked)
        rerank_ranks = {
            field_path: self._rank_score(rank, len(reranked))
            for rank, (field_path, _) in enumerate(reranked, start=1)
        }
        retrieved_order = tuple(retrieved_by_path)
        retrieval_ranks = {
            field_path: self._rank_score(rank, len(retrieved_order))
            for rank, field_path in enumerate(retrieved_order, start=1)
        }
        scored: list[FieldBindingCandidate] = []
        for definition in definitions:
            field_path = definition.canonical_field_path
            exact = exact_by_path.get(field_path)
            matched_alias_ids = exact.matched_alias_ids if exact is not None else ()
            exact_score = exact.alias_match_score if exact is not None else 0.0
            context = self._context_assessment(
                definition,
                evidence,
                matched_alias_ids,
            )
            value_type_score, value_type_known, value_type_compatible = (
                self._value_type_compatibility(
                    evidence.observed_value_type,
                    definition.value_type,
                )
            )
            retrieved = retrieved_by_path.get(field_path)
            raw_rerank = rerank_scores.get(field_path)
            failures: list[str] = []
            if self._negative_alias_matches(definition, evidence.normalized_label):
                failures.append("field_binding.negative_alias_match")
            if context.negative_anchor_matched:
                failures.append("field_binding.negative_context_anchor")
            if context.required_alias_context_missing:
                failures.append("field_binding.required_alias_context_missing")
            if (
                matched_alias_ids
                and exact is not None
                and exact.conflicting_field_paths
                and not any(
                    alias.context_anchors
                    for alias in definition.aliases
                    if alias.alias_id in matched_alias_ids
                )
            ):
                failures.append("field_binding.ambiguous_alias_requires_context")
            if not value_type_compatible and value_type_known:
                failures.append("field_binding.value_type_incompatible")
            elif self._policy.require_value_type_for_acceptance and not value_type_known:
                failures.append("field_binding.value_type_unavailable")
            if raw_rerank is None:
                failures.append("field_binding.rerank_result_missing")
            elif raw_rerank < self._policy.min_rerank_score:
                failures.append("field_binding.rerank_score_below_threshold")
            component_scores = (
                exact_score,
                retrieval_ranks.get(field_path, 0.0),
                rerank_ranks.get(field_path, 0.0),
                context.context_score,
                context.position_score,
                value_type_score,
            )
            combined = self._weighted_score(component_scores)
            retrieval_score = self._retrieval_score(retrieved, raw_rerank)
            scored.append(
                FieldBindingCandidate(
                    evidence_id=evidence.evidence_id,
                    schema_version=definition.schema_version,
                    document_type=definition.document_type,
                    canonical_field_path=field_path,
                    catalog_version=definition.catalog_version,
                    matched_alias_ids=matched_alias_ids,
                    matched_context_anchor_ids=context.matched_anchor_ids,
                    conflicting_field_paths=(),
                    alias_match_score=exact_score,
                    context_match_score=context.context_score,
                    combined_match_score=combined,
                    position_match_score=context.position_score,
                    value_type_match_score=value_type_score,
                    retrieval_rank_score=retrieval_ranks.get(field_path, 0.0),
                    rerank_rank_score=rerank_ranks.get(field_path, 0.0),
                    retrieval_score=retrieval_score,
                    rule_failures=tuple(dict.fromkeys(failures)),
                )
            )
        return tuple(
            sorted(
                scored,
                key=lambda item: (
                    -item.combined_match_score,
                    item.canonical_field_path,
                ),
            )
        )

    def _fallback_decision(
        self,
        request: FieldSemanticBindingRequest,
        evidence: FieldBindingEvidence,
        definitions: Sequence[FieldSemanticDefinition],
        exact_by_path: Mapping[str, FieldBindingCandidate],
        catalog_version: FieldSemanticCatalogVersion,
        index_version: IndexVersion | None,
        reason: str,
    ) -> FieldBindingDecision:
        candidates: list[FieldBindingCandidate] = []
        for field_path, exact in exact_by_path.items():
            definition = self._definition_by_path(definitions, field_path)
            context = self._context_assessment(
                definition,
                evidence,
                exact.matched_alias_ids,
            )
            value_score, _, _ = self._value_type_compatibility(
                evidence.observed_value_type,
                definition.value_type,
            )
            candidates.append(
                replace(
                    exact,
                    context_match_score=context.context_score,
                    position_match_score=context.position_score,
                    value_type_match_score=value_score,
                    combined_match_score=self._weighted_score(
                        (
                            exact.alias_match_score,
                            0.0,
                            0.0,
                            context.context_score,
                            context.position_score,
                            value_score,
                        )
                    ),
                    matched_context_anchor_ids=context.matched_anchor_ids,
                    conflicting_field_paths=(),
                    rule_failures=(reason,),
                )
            )
        visible_templates = tuple(
            sorted(
                candidates,
                key=lambda item: (-item.combined_match_score, item.canonical_field_path),
            )[: self._policy.result_limit]
        )
        visible = self._materialize_runtime_candidates(
            visible_templates,
            evidence.candidate_field_paths,
        )
        exact_matches = tuple(item for item in visible if item.alias_match_score == 1.0)
        missing_detail_instance = (
            len(exact_matches) == 1 and "*" in exact_matches[0].canonical_field_path
        )
        accepted = len(exact_matches) == 1 and not missing_detail_instance
        selected_path: str | None = None
        if accepted:
            selected_path = exact_matches[0].canonical_field_path
            visible = tuple(
                replace(item, rule_failures=())
                if item.canonical_field_path == selected_path
                else item
                for item in visible
            )
        status = (
            FieldBindingStatus.ACCEPTED
            if accepted
            else FieldBindingStatus.REVIEW_REQUIRED
            if visible
            else FieldBindingStatus.UNRESOLVED
        )
        return self._decision(
            request=request,
            catalog_version=catalog_version,
            index_version=index_version,
            status=status,
            candidates=visible,
            selected_path=selected_path if accepted else None,
            reason_codes=(
                ("field_binding.exact_catalog_match",)
                if accepted
                else ("field_binding.detail_instance_unavailable",)
                if missing_detail_instance
                else (reason,)
            ),
        )

    @staticmethod
    def _materialize_runtime_candidates(
        candidates: Sequence[FieldBindingCandidate],
        runtime_paths: Sequence[str],
    ) -> tuple[FieldBindingCandidate, ...]:
        materialized: list[FieldBindingCandidate] = []
        for candidate in candidates:
            matches = tuple(
                path
                for path in runtime_paths
                if canonical_field_path_template(path) == candidate.canonical_field_path
            )
            if matches:
                materialized.extend(
                    replace(
                        candidate,
                        canonical_field_path=path,
                    )
                    for path in matches
                )
            else:
                materialized.append(candidate)
        return tuple(
            sorted(
                materialized,
                key=lambda item: (-item.combined_match_score, item.canonical_field_path),
            )
        )

    def _materialize_runtime_decision(
        self,
        request: FieldSemanticBindingRequest,
        decision: FieldBindingDecision,
    ) -> FieldBindingDecision:
        candidates = self._materialize_runtime_candidates(
            decision.candidates,
            request.evidence.candidate_field_paths,
        )
        selected_template = decision.selected_canonical_field_path
        if candidates == decision.candidates and (
            selected_template is None or "*" not in selected_template
        ):
            return decision
        selected_matches = (
            tuple(
                item.canonical_field_path
                for item in candidates
                if "*" not in item.canonical_field_path
                if canonical_field_path_template(item.canonical_field_path)
                == selected_template
            )
            if selected_template is not None
            else ()
        )
        missing_detail_instance = (
            selected_template is not None
            and "*" in selected_template
            and not selected_matches
        )
        ambiguous_instance = len(selected_matches) > 1
        status = (
            FieldBindingStatus.REVIEW_REQUIRED
            if ambiguous_instance or missing_detail_instance
            else decision.status
        )
        selected_path = (
            selected_matches[0]
            if len(selected_matches) == 1
            else selected_template
            if not selected_matches and not missing_detail_instance
            else None
        )
        return self._decision(
            request=request,
            catalog_version=decision.catalog_version,
            index_version=decision.index_version,
            status=status,
            candidates=candidates,
            selected_path=selected_path,
            reason_codes=(
                ("field_binding.detail_instance_ambiguous",)
                if ambiguous_instance
                else ("field_binding.detail_instance_unavailable",)
                if missing_detail_instance
                else decision.reason_codes
            ),
        )

    async def _rerank(
        self,
        query_text: str,
        definitions: Sequence[FieldSemanticDefinition],
    ) -> tuple[tuple[str, float], ...]:
        candidates = tuple(
            FieldSemanticRerankCandidate(
                candidate_id=item.canonical_field_path,
                canonical_field_path=item.canonical_field_path,
                redacted_content=self._rerank_content(item),
            )
            for item in definitions
        )
        reranked = await self._reranker.rerank_field_semantics(
            query_text,
            candidates,
            len(candidates),
        )
        candidate_ids = {item.candidate_id for item in candidates}
        result_ids = tuple(candidate_id for candidate_id, _ in reranked)
        if len(result_ids) != len(set(result_ids)) or any(
            candidate_id not in candidate_ids for candidate_id in result_ids
        ):
            raise ValueError("Field semantic reranker returned invalid candidate IDs")
        if any(not math.isfinite(score) for _, score in reranked):
            raise ValueError("Field semantic reranker returned a non-finite score")
        return reranked

    async def _embed_dense(self, query_text: str) -> tuple[float, ...]:
        embeddings = await self._dense_embeddings.embed((query_text,))
        if len(embeddings) != 1 or not embeddings[0]:
            raise ValueError("Field semantic dense embedding is invalid")
        vector = tuple(float(value) for value in embeddings[0])
        if any(not math.isfinite(value) for value in vector):
            raise ValueError("Field semantic dense embedding contains non-finite values")
        return vector

    async def _embed_sparse(self, query_text: str) -> SparseVector | None:
        if self._sparse_embeddings is None:
            return None
        embeddings = await self._sparse_embeddings.embed((query_text,))
        if len(embeddings) != 1 or not embeddings[0]:
            raise ValueError("Field semantic sparse embedding is invalid")
        normalized: list[tuple[int, float]] = []
        previous_index = -1
        for index, weight in embeddings[0]:
            if isinstance(index, bool) or not isinstance(index, int):
                raise ValueError("Field semantic sparse token index must be an integer")
            normalized_weight = float(weight)
            if index < 0 or index <= previous_index:
                raise ValueError(
                    "Field semantic sparse token indexes must be strictly increasing"
                )
            if not math.isfinite(normalized_weight) or normalized_weight < 0.0:
                raise ValueError(
                    "Field semantic sparse weights must be finite and non-negative"
                )
            normalized.append((index, normalized_weight))
            previous_index = index
        return tuple(normalized)

    @staticmethod
    def _normalize_evidence(
        evidence: FieldBindingEvidence,
    ) -> FieldBindingEvidence | None:
        normalized_label = normalize_field_label(evidence.observed_label)
        if not normalized_label:
            return None
        nearby_text = tuple(
            dict.fromkeys(
                normalized
                for value in evidence.nearby_text
                if (normalized := normalize_field_label(value))
            )
        )
        observations_by_key: dict[
            tuple[str, object, int | None], FieldContextObservation
        ] = {}
        for item in evidence.context_observations:
            normalized = normalize_field_label(item.text)
            if not normalized:
                continue
            observation = replace(item, normalized_text=normalized)
            observations_by_key.setdefault(
                (normalized, observation.relation, observation.distance),
                observation,
            )
        return replace(
            evidence,
            normalized_label=normalized_label,
            nearby_text=nearby_text,
            context_observations=tuple(observations_by_key.values()),
        )

    @staticmethod
    def _validate_definitions(
        request: FieldSemanticBindingRequest,
        definitions: tuple[FieldSemanticDefinition, ...],
    ) -> tuple[FieldSemanticDefinition, ...]:
        if not definitions:
            raise ValueError("Current Schema has no field definitions for document_type")
        catalog_versions = {item.catalog_version for item in definitions}
        if len(catalog_versions) != 1:
            raise ValueError("Field semantic definitions mix catalog versions")
        paths = tuple(item.canonical_field_path for item in definitions)
        if len(paths) != len(set(paths)):
            raise ValueError("Current Schema contains duplicate canonical field paths")
        if any(
            not item.is_valid
            or item.tenant_scope != request.tenant_id
            or item.document_type != request.document_type
            or item.schema_version != request.schema_version
            for item in definitions
        ):
            raise ValueError("Field semantic definitions are outside the binding scope")
        return definitions

    @staticmethod
    def _retrieved_by_path(
        retrieved: Sequence[RetrievedFieldSemantic],
        definitions: Sequence[FieldSemanticDefinition],
        scope: FieldSemanticIndexScope,
        index_version: IndexVersion,
    ) -> dict[str, RetrievedFieldSemantic]:
        known = {item.canonical_field_path for item in definitions}
        by_path: dict[str, RetrievedFieldSemantic] = {}
        for item in retrieved:
            path = item.document.canonical_field_path
            if (
                item.document.scope != scope
                or item.document.index_version != index_version
            ):
                raise ValueError("Index returned field semantics outside the requested scope")
            if path not in known:
                raise ValueError("Index returned a path absent from the current Schema")
            existing = by_path.get(path)
            if existing is None or item.rank < existing.rank:
                by_path[path] = item
        return dict(sorted(by_path.items(), key=lambda pair: pair[1].rank))

    def _candidate_paths(
        self,
        exact: Sequence[FieldBindingCandidate],
        retrieved: Sequence[RetrievedFieldSemantic],
    ) -> tuple[str, ...]:
        ordered = tuple(
            dict.fromkeys(
                tuple(item.canonical_field_path for item in exact)
                + tuple(item.document.canonical_field_path for item in retrieved)
            )
        )
        return ordered[: self._policy.candidate_k]

    @staticmethod
    def _definition_by_path(
        definitions: Sequence[FieldSemanticDefinition],
        field_path: str,
    ) -> FieldSemanticDefinition:
        definition = next(
            (item for item in definitions if item.canonical_field_path == field_path),
            None,
        )
        if definition is None:
            raise ValueError("Binding candidate is absent from the current Schema")
        return definition

    @classmethod
    def _context_assessment(
        cls,
        definition: FieldSemanticDefinition,
        evidence: FieldBindingEvidence,
        matched_alias_ids: tuple[str, ...],
    ) -> _ContextAssessment:
        nearby = tuple(normalize_field_label(item) for item in evidence.nearby_text) + tuple(
            item.normalized_text for item in evidence.context_observations
        )
        observations = evidence.context_observations
        positive = tuple(
            anchor for anchor in definition.context_anchors if not anchor.is_negative
        )
        negative = tuple(
            anchor for anchor in definition.context_anchors if anchor.is_negative
        )
        matched_positive = tuple(
            anchor
            for anchor in positive
            if cls._anchor_text_matches(anchor, nearby, observations)
        )
        matched_negative = tuple(
            anchor
            for anchor in negative
            if cls._anchor_text_matches(anchor, nearby, observations)
        )
        positioned = tuple(
            anchor
            for anchor in matched_positive
            if cls._anchor_position_matches(anchor, observations)
        )
        if positive:
            context_score = len(matched_positive) / len(positive)
            position_score = (
                len(positioned) / len(positive)
                if observations
                else context_score * 0.5
            )
        else:
            context_score = 0.5
            position_score = 0.5
        required_alias_anchors = tuple(
            anchor
            for alias in definition.aliases
            if alias.alias_id in matched_alias_ids
            for anchor in alias.context_anchors
            if not anchor.is_negative
        )
        required_missing = bool(required_alias_anchors) and not any(
            anchor.anchor_id in {item.anchor_id for item in matched_positive}
            for anchor in required_alias_anchors
        )
        return _ContextAssessment(
            context_score=context_score,
            position_score=position_score,
            matched_anchor_ids=tuple(
                dict.fromkeys(
                    item.anchor_id for item in matched_positive + matched_negative
                )
            ),
            negative_anchor_matched=bool(matched_negative),
            required_alias_context_missing=required_missing,
        )

    @staticmethod
    def _anchor_text_matches(
        anchor: FieldContextAnchor,
        nearby: Sequence[str],
        observations: Sequence[FieldContextObservation],
    ) -> bool:
        normalized = normalize_field_label(anchor.text)
        return any(normalized in item for item in nearby) or any(
            normalized in item.normalized_text for item in observations
        )

    @staticmethod
    def _anchor_position_matches(
        anchor: FieldContextAnchor,
        observations: Sequence[FieldContextObservation],
    ) -> bool:
        normalized = normalize_field_label(anchor.text)
        return any(
            normalized in item.normalized_text
            and item.relation is anchor.relation
            and (
                anchor.max_distance is None
                or (item.distance is not None and item.distance <= anchor.max_distance)
            )
            for item in observations
        )

    @staticmethod
    def _negative_alias_matches(
        definition: FieldSemanticDefinition,
        normalized_label: str,
    ) -> bool:
        return any(
            normalize_field_label(alias.alias_text) == normalized_label
            for alias in definition.negative_aliases
        )

    @classmethod
    def _value_type_compatibility(
        cls,
        observed_type: str | None,
        expected_schema: str,
    ) -> tuple[float, bool, bool]:
        expected = cls._schema_types(expected_schema)
        observed = cls._observed_types(observed_type)
        if not observed:
            return 0.5, False, False
        observed_formats = frozenset(
            item for item in observed if item.startswith("format:")
        )
        compatible = (
            bool(expected.intersection(observed_formats))
            if observed_formats
            else bool(expected.intersection(observed))
            or ("integer" in observed and "number" in expected)
        )
        return (1.0 if compatible else 0.0), True, compatible

    @classmethod
    def _schema_types(cls, schema_text: str) -> frozenset[str]:
        try:
            payload = json.loads(schema_text)
        except json.JSONDecodeError as exc:
            raise ValueError("Field semantic value_type is not valid JSON Schema") from exc
        if not isinstance(payload, dict):
            raise ValueError("Field semantic value_type must be a JSON Schema object")
        values: set[str] = set()
        cls._collect_schema_types(payload, values)
        if not values:
            raise ValueError("Field semantic value_type declares no root JSON type")
        return frozenset(values)

    @classmethod
    def _collect_schema_types(
        cls,
        payload: Mapping[str, object],
        values: set[str],
    ) -> None:
        raw_type = payload.get("type")
        if isinstance(raw_type, str):
            values.add(raw_type)
        elif isinstance(raw_type, list):
            values.update(item for item in raw_type if isinstance(item, str))
        raw_format = payload.get("format")
        if isinstance(raw_format, str) and raw_format.strip():
            values.add(f"format:{raw_format.casefold()}")
        for keyword in ("anyOf", "oneOf"):
            variants = payload.get(keyword)
            if isinstance(variants, list):
                for variant in variants:
                    if isinstance(variant, dict):
                        cls._collect_schema_types(variant, values)

    @classmethod
    def _observed_types(cls, observed_type: str | None) -> frozenset[str]:
        if observed_type is None:
            return frozenset()
        normalized = normalize_field_label(observed_type)
        aliases = {
            "str": ("string",),
            "text": ("string",),
            "date": ("format:date",),
            "datetime": ("format:date-time",),
            "date time": ("format:date-time",),
            "float": ("number",),
            "decimal": ("number",),
            "int": ("integer",),
            "bool": ("boolean",),
            "none": ("null",),
        }
        aliased = aliases.get(normalized)
        if aliased is not None:
            return frozenset(aliased)
        if normalized in {"string", "number", "integer", "boolean", "array", "object", "null"}:
            return frozenset((normalized,))
        try:
            return cls._schema_types(observed_type)
        except ValueError:
            return frozenset()

    def _weighted_score(self, components: tuple[float, ...]) -> float:
        weights = self._policy.component_weights
        total = sum(weights)
        return sum(score * weight for score, weight in zip(components, weights)) / total

    @staticmethod
    def _rank_score(rank: int, count: int) -> float:
        if count <= 0 or rank <= 0 or rank > count:
            return 0.0
        return (count - rank + 1) / count

    @staticmethod
    def _retrieval_score(
        retrieved: RetrievedFieldSemantic | None,
        rerank_score: float | None,
    ) -> RetrievalScore | None:
        if retrieved is None:
            return (
                RetrievalScore(dense=None, sparse=None, fusion=None, rerank=rerank_score)
                if rerank_score is not None
                else None
            )
        return RetrievalScore(
            dense=retrieved.score.dense,
            sparse=retrieved.score.sparse,
            fusion=retrieved.score.fusion,
            rerank=rerank_score,
        )

    @staticmethod
    def _query_text(
        evidence: FieldBindingEvidence,
        definitions: Sequence[FieldSemanticDefinition],
    ) -> str:
        nearby = tuple(normalize_field_label(item) for item in evidence.nearby_text) + tuple(
            item.normalized_text for item in evidence.context_observations
        )
        matched_approved_anchors = tuple(
            dict.fromkeys(
                anchor.text
                for definition in definitions
                for anchor in definition.context_anchors
                if not anchor.is_negative
                and any(normalize_field_label(anchor.text) in item for item in nearby)
            )
        )
        return "\n".join(
            (
                "OBSERVED_FIELD_LABEL",
                json.dumps(evidence.normalized_label, ensure_ascii=False),
                "OBSERVED_VALUE_TYPE",
                json.dumps(
                    sorted(FieldSemanticBindingService._observed_types(
                        evidence.observed_value_type
                    )),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "PAGE_POSITION",
                json.dumps(
                    {
                        "page_number": evidence.page_number,
                        "bounding_box": evidence.bounding_box,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "MATCHED_APPROVED_CONTEXT_ANCHORS",
                json.dumps(
                    matched_approved_anchors,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )

    @staticmethod
    def _rerank_content(definition: FieldSemanticDefinition) -> str:
        payload = {
            "canonical_field_path": definition.canonical_field_path,
            "display_name": definition.display_name,
            "description": definition.description,
            "approved_aliases": tuple(alias.alias_text for alias in definition.aliases),
            "negative_aliases": tuple(
                alias.alias_text for alias in definition.negative_aliases
            ),
            "context_anchors": tuple(
                anchor.text
                for anchor in definition.context_anchors
                if not anchor.is_negative
            ),
            "value_type": definition.value_type,
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _decision(
        self,
        *,
        request: FieldSemanticBindingRequest,
        catalog_version: FieldSemanticCatalogVersion,
        index_version: IndexVersion | None,
        status: FieldBindingStatus,
        candidates: tuple[FieldBindingCandidate, ...],
        selected_path: str | None,
        reason_codes: tuple[str, ...],
    ) -> FieldBindingDecision:
        top1 = candidates[0].combined_match_score if candidates else None
        top2 = candidates[1].combined_match_score if len(candidates) > 1 else None
        margin = top1 - top2 if top1 is not None and top2 is not None else top1
        identity = {
            "tenant_id": request.tenant_id,
            "document_type": request.document_type,
            "schema_version": request.schema_version,
            "evidence_id": request.evidence.evidence_id,
            "catalog_version": catalog_version.value,
            "index_version": index_version.value if index_version is not None else None,
            "policy_version": self._policy.version,
        }
        decision_id = sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return FieldBindingDecision(
            decision_id=decision_id,
            tenant_id=request.tenant_id,
            evidence_id=request.evidence.evidence_id,
            document_type=request.document_type,
            schema_version=request.schema_version,
            status=status,
            selected_canonical_field_path=selected_path,
            candidates=candidates,
            catalog_version=catalog_version,
            index_version=index_version,
            policy_version=self._policy.version,
            authority=FieldBindingDecisionAuthority.DETERMINISTIC_POLICY,
            decided_by=f"field-semantic-binding:{self._policy.version}",
            reason_codes=reason_codes,
            requires_review=status is not FieldBindingStatus.ACCEPTED,
            top1_score=top1,
            top2_score=top2,
            score_margin=margin,
            decided_at=datetime.now(UTC),
        )
