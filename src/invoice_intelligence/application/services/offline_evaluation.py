"""Offline comparison orchestration and deterministic metric calculation."""

import json
import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256

from invoice_intelligence.application.errors import (
    ResourceConflictError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from invoice_intelligence.application.ports.evaluation import (
    EvaluationArtifactPublisher,
    EvaluationDatasetRepository,
    EvaluationRunRepository,
    EvaluationVariantExecutor,
)
from invoice_intelligence.domain.admission import MemoryAdmissionStatus
from invoice_intelligence.domain.evaluation import (
    EvaluationBindings,
    EvaluationBucket,
    EvaluationBucketDimension,
    EvaluationBucketResult,
    EvaluationCase,
    EvaluationCaseObservation,
    EvaluationDataset,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationSuite,
    EvaluationVariant,
    EvaluationVariantResult,
    ExtractionMetric,
    MemoryEffectJudgment,
    PromotionCandidate,
    RetrievalMetric,
    TrustedMemoryFieldMetric,
    required_variants_for_suite,
)
from invoice_intelligence.domain.examples import ExampleLabelType
from invoice_intelligence.domain.field_semantics import FieldBindingStatus
from invoice_intelligence.domain.json_types import JsonValue


@dataclass(frozen=True, slots=True)
class OfflineEvaluationPolicy:
    """Versioned run policy; production thresholds remain outside this service."""

    retrieval_k_values: tuple[int, ...] = (1, 3, 5)
    promotion_primary_k: int = 5
    minimum_recall_gain: float = 0.0
    minimum_field_accuracy_gain: float = 0.0
    maximum_review_recall_drop: float = 0.0
    minimum_alias_binding_accuracy_gain: float = 0.0
    minimum_top_k_field_recall_gain: float = 0.0
    maximum_harmful_admission_rate_increase: float = 0.0
    maximum_wrong_field_auto_fill_increase: int = 0
    maximum_misleading_retrieval_rate_increase: float = 0.0

    def __post_init__(self) -> None:
        if not self.retrieval_k_values:
            raise ValueError("Offline evaluation requires at least one K value")
        if len(self.retrieval_k_values) != len(set(self.retrieval_k_values)):
            raise ValueError("Offline evaluation K values must be unique")
        if tuple(sorted(self.retrieval_k_values)) != self.retrieval_k_values:
            raise ValueError("Offline evaluation K values must be sorted")
        if any(value <= 0 for value in self.retrieval_k_values):
            raise ValueError("Offline evaluation K values must be greater than zero")
        if self.promotion_primary_k not in self.retrieval_k_values:
            raise ValueError("Promotion primary K must be one of the evaluated K values")
        for name, value in (
            ("minimum_recall_gain", self.minimum_recall_gain),
            ("minimum_field_accuracy_gain", self.minimum_field_accuracy_gain),
            ("maximum_review_recall_drop", self.maximum_review_recall_drop),
            (
                "minimum_alias_binding_accuracy_gain",
                self.minimum_alias_binding_accuracy_gain,
            ),
            ("minimum_top_k_field_recall_gain", self.minimum_top_k_field_recall_gain),
            (
                "maximum_harmful_admission_rate_increase",
                self.maximum_harmful_admission_rate_increase,
            ),
            (
                "maximum_misleading_retrieval_rate_increase",
                self.maximum_misleading_retrieval_rate_increase,
            ),
        ):
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.maximum_wrong_field_auto_fill_increase < 0:
            raise ValueError(
                "maximum_wrong_field_auto_fill_increase must be non-negative"
            )


@dataclass(frozen=True, slots=True)
class OfflineEvaluationRequest:
    """Operator-supplied identity and immutable version bindings for one run."""

    evaluation_run_id: str
    tenant_id: str
    dataset_id: str
    dataset_version: str
    bindings: EvaluationBindings
    suite: EvaluationSuite = EvaluationSuite.CASE_RAG

    def __post_init__(self) -> None:
        for name, value in (
            ("Evaluation run identifier", self.evaluation_run_id),
            ("Evaluation tenant identifier", self.tenant_id),
            ("Evaluation dataset identifier", self.dataset_id),
            ("Evaluation dataset version", self.dataset_version),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")


class OfflineEvaluationService:
    """Compare fixed variants without training or modifying production configuration."""

    def __init__(
        self,
        *,
        dataset_repository: EvaluationDatasetRepository,
        run_repository: EvaluationRunRepository,
        variant_executor: EvaluationVariantExecutor,
        artifact_publisher: EvaluationArtifactPublisher | None,
        policy: OfflineEvaluationPolicy,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._datasets = dataset_repository
        self._runs = run_repository
        self._executor = variant_executor
        self._artifacts = artifact_publisher
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(UTC))

    async def evaluate(self, request: OfflineEvaluationRequest) -> EvaluationRun:
        """Execute a held-out comparison when explicitly invoked by an operator."""

        if self._artifacts is None:
            raise ServiceUnavailableError("Offline evaluation report publisher is unavailable")

        dataset = await self._datasets.get_dataset(
            request.tenant_id,
            request.dataset_id,
            request.dataset_version,
        )
        if dataset is None:
            raise ResourceNotFoundError("Evaluation dataset was not found")
        self._validate_dataset_scope(dataset, request)

        existing = await self._runs.get_run(
            request.tenant_id,
            request.evaluation_run_id,
        )
        if existing is not None:
            self._validate_replay(existing, request, dataset)
            if existing.status in {
                EvaluationRunStatus.COMPLETED,
                EvaluationRunStatus.FAILED,
            }:
                return existing
            raise ResourceConflictError("Evaluation run is already in progress")

        created_at = self._now()
        variants = required_variants_for_suite(request.suite)
        self._validate_suite_dataset(dataset, request.suite)
        run = EvaluationRun(
            evaluation_run_id=request.evaluation_run_id,
            tenant_id=request.tenant_id,
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.version,
            schema_version=dataset.schema_version,
            bindings=request.bindings,
            variants=variants,
            status=EvaluationRunStatus.CREATED,
            results=(),
            promotion_candidates=(),
            leakage_check_passed=self._has_document_isolation(dataset),
            report_schema_version="invoice-offline-evaluation-v2",
            artifact_references=(),
            created_at=created_at,
            suite=request.suite,
        )
        await self._runs.save(run)
        run = replace(
            run,
            status=EvaluationRunStatus.RUNNING,
            started_at=self._now(),
        )
        await self._runs.save(run)
        running = run

        try:
            results: list[EvaluationVariantResult] = []
            for variant in variants:
                raw_observations = await self._executor.evaluate(
                    dataset,
                    variant,
                    request.bindings,
                )
                observations = self._validate_observations(
                    dataset,
                    request.suite,
                    variant,
                    raw_observations,
                )
                results.append(
                    self._variant_result(
                        dataset,
                        request.suite,
                        variant,
                        observations,
                    )
                )
            completed_at = self._now()
            promotion_candidates = self._promotion_candidates(
                request,
                tuple(results),
                completed_at,
            )
            run = replace(
                run,
                status=EvaluationRunStatus.COMPLETED,
                results=tuple(results),
                promotion_candidates=promotion_candidates,
                completed_at=completed_at,
            )
            artifact_references = await self._artifacts.publish(run)
            if not artifact_references:
                raise ServiceUnavailableError("Offline evaluation report artifacts are unavailable")
            run = replace(run, artifact_references=artifact_references)
            await self._runs.save(run)
        except Exception:
            failed = replace(
                running,
                status=EvaluationRunStatus.FAILED,
                completed_at=self._now(),
                failure_code="OFFLINE_EVALUATION_FAILED",
            )
            await self._runs.save(failed)
            raise

        return run

    def _variant_result(
        self,
        dataset: EvaluationDataset,
        suite: EvaluationSuite,
        variant: EvaluationVariant,
        observations: tuple[EvaluationCaseObservation, ...],
    ) -> EvaluationVariantResult:
        overall = self._bucket_result(
            EvaluationBucket(EvaluationBucketDimension.OVERALL, "all"),
            dataset.cases,
            observations,
            suite,
        )
        cases_by_id = {item.case_id: item for item in dataset.cases}
        grouped: dict[
            tuple[EvaluationBucketDimension, str],
            list[EvaluationCaseObservation],
        ] = defaultdict(list)
        for observation in observations:
            case = cases_by_id[observation.case_id]
            for bucket in self._case_buckets(case):
                grouped[(bucket.dimension, bucket.value)].append(observation)

        case_ids_by_bucket: dict[
            tuple[EvaluationBucketDimension, str],
            set[str],
        ] = defaultdict(set)
        for case in dataset.cases:
            for bucket in self._case_buckets(case):
                case_ids_by_bucket[(bucket.dimension, bucket.value)].add(case.case_id)

        buckets: list[EvaluationBucketResult] = []
        for key in sorted(grouped, key=lambda item: (item[0].value, item[1])):
            dimension, value = key
            bucket_cases = tuple(
                item for item in dataset.cases if item.case_id in case_ids_by_bucket[key]
            )
            buckets.append(
                self._bucket_result(
                    EvaluationBucket(dimension, value),
                    bucket_cases,
                    tuple(grouped[key]),
                    suite,
                )
            )
        return EvaluationVariantResult(
            variant=variant,
            overall=overall,
            buckets=tuple(buckets),
        )

    def _bucket_result(
        self,
        bucket: EvaluationBucket,
        cases: tuple[EvaluationCase, ...],
        observations: tuple[EvaluationCaseObservation, ...],
        suite: EvaluationSuite,
    ) -> EvaluationBucketResult:
        return EvaluationBucketResult(
            bucket=bucket,
            retrieval_metrics=tuple(
                self._retrieval_metric(cases, observations, k)
                for k in self._policy.retrieval_k_values
            ),
            extraction_metric=self._extraction_metric(cases, observations),
            trusted_memory_field_metrics=tuple(
                self._trusted_memory_field_metric(cases, observations, k)
                for k in self._policy.retrieval_k_values
            )
            if suite is EvaluationSuite.TRUSTED_MEMORY_FIELD_BINDING
            else (),
        )

    @staticmethod
    def _trusted_memory_field_metric(
        cases: tuple[EvaluationCase, ...],
        observations: tuple[EvaluationCaseObservation, ...],
        k: int,
    ) -> TrustedMemoryFieldMetric:
        observations_by_case = {item.case_id: item for item in observations}
        memory_admission_case_count = 0
        approved_prediction_count = 0
        correct_approved_count = 0
        harmful_ground_truth_count = 0
        harmful_admitted_count = 0
        quarantined_prediction_count = 0
        multi_reviewer_case_count = 0
        reviewer_disagreement_count = 0
        alias_binding_case_count = 0
        correct_alias_binding_count = 0
        field_binding_case_count = 0
        expected_field_path_count = 0
        recalled_field_path_count = 0
        ambiguous_prediction_count = 0
        wrong_field_auto_fill_count = 0
        helpful_opportunity_count = 0
        helpful_hit_count = 0
        memory_effect_case_count = 0
        misleading_hit_count = 0

        for case in cases:
            observation = observations_by_case[case.case_id]
            admission_truth = case.memory_admission_ground_truth
            if admission_truth is not None:
                admission_output = observation.memory_admission
                if admission_output is None:
                    raise ValueError("Admission ground truth requires an admission observation")
                memory_admission_case_count += 1
                is_approved = admission_output.status is MemoryAdmissionStatus.APPROVED
                approved_prediction_count += int(is_approved)
                correct_approved_count += int(
                    is_approved
                    and admission_truth.expected_status is MemoryAdmissionStatus.APPROVED
                )
                harmful_ground_truth_count += int(admission_truth.harmful_if_admitted)
                harmful_admitted_count += int(
                    admission_truth.harmful_if_admitted and is_approved
                )
                quarantined_prediction_count += int(
                    admission_output.status is MemoryAdmissionStatus.QUARANTINED
                )
                judgments = admission_truth.reviewer_judgments
                if len(judgments) >= 2:
                    multi_reviewer_case_count += 1
                    reviewer_disagreement_count += int(
                        len({item.status for item in judgments}) > 1
                    )

            binding_truth = case.field_binding_ground_truth
            if binding_truth is not None:
                binding_output = observation.field_binding
                if binding_output is None:
                    raise ValueError("Field ground truth requires a binding observation")
                field_binding_case_count += 1
                top_k_paths = set(binding_output.candidate_field_paths[:k])
                acceptable = set(binding_truth.acceptable_field_paths)
                expected_field_path_count += len(acceptable)
                recalled_field_path_count += len(top_k_paths.intersection(acceptable))
                ambiguous_prediction_count += int(
                    binding_output.status is FieldBindingStatus.REVIEW_REQUIRED
                )
                if binding_truth.is_alias_case:
                    alias_binding_case_count += 1
                    correct_alias_binding_count += int(
                        binding_output.status is FieldBindingStatus.ACCEPTED
                        and binding_output.selected_canonical_field_path in acceptable
                    )
                wrong_field_auto_fill_count += int(
                    binding_output.auto_fill_performed
                    and binding_output.selected_canonical_field_path not in acceptable
                )

            effects = case.expected_memory_effects
            if effects:
                memory_effect_case_count += 1
                retrieved_ids = {
                    item.example_id
                    for item in sorted(
                        observation.retrieved_examples,
                        key=lambda item: item.rank,
                    )[:k]
                }
                helpful_ids = {
                    item.example_id
                    for item in effects
                    if item.judgment is MemoryEffectJudgment.HELPFUL
                }
                misleading_ids = {
                    item.example_id
                    for item in effects
                    if item.judgment is MemoryEffectJudgment.MISLEADING
                }
                if helpful_ids:
                    helpful_opportunity_count += 1
                    helpful_hit_count += int(bool(retrieved_ids.intersection(helpful_ids)))
                misleading_hit_count += int(
                    bool(retrieved_ids.intersection(misleading_ids))
                )

        return TrustedMemoryFieldMetric(
            k=k,
            memory_approval_precision=_ratio_or_none(
                correct_approved_count,
                approved_prediction_count,
            ),
            harmful_memory_admission_rate=_ratio_or_none(
                harmful_admitted_count,
                harmful_ground_truth_count,
            ),
            quarantine_rate=_ratio_or_none(
                quarantined_prediction_count,
                memory_admission_case_count,
            ),
            reviewer_disagreement_rate=_ratio_or_none(
                reviewer_disagreement_count,
                multi_reviewer_case_count,
            ),
            alias_binding_accuracy=_ratio_or_none(
                correct_alias_binding_count,
                alias_binding_case_count,
            ),
            top_k_field_recall=_ratio_or_none(
                recalled_field_path_count,
                expected_field_path_count,
            ),
            field_binding_ambiguity_rate=_ratio_or_none(
                ambiguous_prediction_count,
                field_binding_case_count,
            ),
            wrong_field_auto_fill_count=wrong_field_auto_fill_count,
            memory_helpfulness_rate=_ratio_or_none(
                helpful_hit_count,
                helpful_opportunity_count,
            ),
            misleading_retrieval_rate=_ratio_or_none(
                misleading_hit_count,
                memory_effect_case_count,
            ),
            evaluation_case_count=len(cases),
            memory_admission_case_count=memory_admission_case_count,
            approved_prediction_count=approved_prediction_count,
            correct_approved_count=correct_approved_count,
            harmful_ground_truth_count=harmful_ground_truth_count,
            harmful_admitted_count=harmful_admitted_count,
            quarantined_prediction_count=quarantined_prediction_count,
            multi_reviewer_case_count=multi_reviewer_case_count,
            reviewer_disagreement_count=reviewer_disagreement_count,
            alias_binding_case_count=alias_binding_case_count,
            correct_alias_binding_count=correct_alias_binding_count,
            field_binding_case_count=field_binding_case_count,
            expected_field_path_count=expected_field_path_count,
            recalled_field_path_count=recalled_field_path_count,
            ambiguous_prediction_count=ambiguous_prediction_count,
            helpful_opportunity_count=helpful_opportunity_count,
            helpful_hit_count=helpful_hit_count,
            memory_effect_case_count=memory_effect_case_count,
            misleading_hit_count=misleading_hit_count,
        )

    @staticmethod
    def _retrieval_metric(
        cases: tuple[EvaluationCase, ...],
        observations: tuple[EvaluationCaseObservation, ...],
        k: int,
    ) -> RetrievalMetric:
        observations_by_case = {item.case_id: item for item in observations}
        recalls: list[float] = []
        hit_rates: list[float] = []
        reciprocal_ranks: list[float] = []
        ndcgs: list[float] = []
        separations: list[float] = []
        expected_example_count = 0
        relevant_hit_count = 0
        empty_retrieval_count = 0

        for case in cases:
            observation = observations_by_case[case.case_id]
            ordered = tuple(sorted(observation.retrieved_examples, key=lambda item: item.rank))
            if not ordered:
                empty_retrieval_count += 1
            top_k = ordered[:k]
            expected = {
                (item.example_id, item.label_type, item.source_document_id): item
                for item in case.expected_examples
            }
            if expected:
                expected_example_count += len(expected)
                hits = tuple(
                    item
                    for item in top_k
                    if (
                        item.example_id,
                        item.label_type,
                        item.source_document_id,
                    )
                    in expected
                )
                hit_keys = {
                    (item.example_id, item.label_type, item.source_document_id)
                    for item in hits
                }
                relevant_hit_count += len(hit_keys)
                recalls.append(len(hit_keys) / len(expected))
                hit_rates.append(1.0 if hit_keys else 0.0)
                relevant_ranks = tuple(
                    item.rank
                    for item in ordered
                    if (
                        item.example_id,
                        item.label_type,
                        item.source_document_id,
                    )
                    in expected
                )
                reciprocal_ranks.append(
                    1.0 / min(relevant_ranks) if relevant_ranks else 0.0
                )
                dcg = sum(
                    (
                        2
                        ** expected[
                            (
                                item.example_id,
                                item.label_type,
                                item.source_document_id,
                            )
                        ].relevance_grade
                        - 1
                    )
                    / math.log2(rank + 2)
                    for rank, item in enumerate(top_k)
                    if (
                        item.example_id,
                        item.label_type,
                        item.source_document_id,
                    )
                    in expected
                )
                ideal_grades = sorted(
                    (item.relevance_grade for item in expected.values()),
                    reverse=True,
                )[:k]
                ideal_dcg = sum(
                    (2**grade - 1) / math.log2(rank + 2)
                    for rank, grade in enumerate(ideal_grades)
                )
                ndcgs.append(dcg / ideal_dcg if ideal_dcg else 0.0)

            positive_scores = tuple(
                item.ranking_score
                for item in ordered
                if item.label_type
                in {
                    ExampleLabelType.CONFIRMED_CORRECT,
                    ExampleLabelType.CORRECTED,
                }
            )
            negative_scores = tuple(
                item.ranking_score
                for item in ordered
                if item.label_type is ExampleLabelType.CONFIRMED_INCORRECT
            )
            if positive_scores and negative_scores:
                separations.append(min(positive_scores) - max(negative_scores))

        return RetrievalMetric(
            k=k,
            recall_at_k=_mean_or_none(recalls),
            hit_rate_at_k=_mean_or_none(hit_rates),
            mrr=_mean_or_none(reciprocal_ranks),
            ndcg_at_k=_mean_or_none(ndcgs),
            positive_negative_separation=_mean_or_none(separations),
            empty_retrieval_rate=empty_retrieval_count / len(cases),
            evaluation_case_count=len(cases),
            expected_example_case_count=len(recalls),
            expected_example_count=expected_example_count,
            relevant_hit_count=relevant_hit_count,
            empty_retrieval_count=empty_retrieval_count,
            separation_case_count=len(separations),
        )

    @staticmethod
    def _extraction_metric(
        cases: tuple[EvaluationCase, ...],
        observations: tuple[EvaluationCaseObservation, ...],
    ) -> ExtractionMetric:
        observations_by_case = {item.case_id: item for item in observations}
        field_correct = 0
        field_denominator = 0
        missing_correct = 0
        candidate_cases = 0
        candidate_hits = 0
        erroneous_auto_fills = 0
        historical_override_violations = 0
        review_true_positive = 0
        review_false_positive = 0
        review_false_negative = 0

        for case in cases:
            extraction = observations_by_case[case.case_id].extraction
            values_match = _json_equal(extraction.actual_value, case.expected_value)
            if not case.expected_is_missing:
                field_denominator += 1
                field_correct += int(values_match)
            missing_correct += int(extraction.predicted_missing == case.expected_is_missing)
            if case.expects_candidate:
                candidate_cases += 1
                expected = _canonical_json(case.expected_value)
                candidate_hits += int(
                    any(
                        _canonical_json(candidate) == expected
                        for candidate in extraction.candidate_values
                    )
                )
            if extraction.used_historical_prior_as_value:
                erroneous_auto_fills += int(not values_match)
                historical_override_violations += int(
                    not extraction.current_evidence_sufficient
                )
            if extraction.review_required and case.expected_review_required:
                review_true_positive += 1
            elif extraction.review_required:
                review_false_positive += 1
            elif case.expected_review_required:
                review_false_negative += 1

        review_precision_denominator = review_true_positive + review_false_positive
        review_recall_denominator = review_true_positive + review_false_negative
        return ExtractionMetric(
            field_accuracy=(
                field_correct / field_denominator if field_denominator else None
            ),
            missing_recognition_accuracy=missing_correct / len(cases),
            candidate_hit_rate=(
                candidate_hits / candidate_cases if candidate_cases else None
            ),
            erroneous_auto_filled_value_count=erroneous_auto_fills,
            historical_override_violation_count=historical_override_violations,
            review_required_precision=(
                review_true_positive / review_precision_denominator
                if review_precision_denominator
                else None
            ),
            review_required_recall=(
                review_true_positive / review_recall_denominator
                if review_recall_denominator
                else None
            ),
            evaluation_case_count=len(cases),
            field_accuracy_denominator=field_denominator,
            missing_recognition_correct_count=missing_correct,
            candidate_case_count=candidate_cases,
            candidate_hit_count=candidate_hits,
            review_true_positive_count=review_true_positive,
            review_false_positive_count=review_false_positive,
            review_false_negative_count=review_false_negative,
        )

    def _promotion_candidates(
        self,
        request: OfflineEvaluationRequest,
        results: tuple[EvaluationVariantResult, ...],
        created_at: datetime,
    ) -> tuple[PromotionCandidate, ...]:
        by_variant = {item.variant: item for item in results}
        variants = required_variants_for_suite(request.suite)
        baseline_variant = variants[0]
        baseline = by_variant[baseline_variant]
        candidates: list[PromotionCandidate] = []
        for variant in variants[1:]:
            candidate = by_variant[variant]
            rationale_codes: tuple[str, ...]
            if request.suite is EvaluationSuite.TRUSTED_MEMORY_FIELD_BINDING:
                baseline_trusted = self._trusted_at_primary_k(baseline)
                candidate_trusted = self._trusted_at_primary_k(candidate)
                if not self._passes_trusted_memory_field_guards(
                    baseline_trusted,
                    candidate_trusted,
                ):
                    continue
                deltas = self._trusted_memory_field_metric_deltas(
                    baseline_trusted,
                    candidate_trusted,
                )
                rationale_codes = (
                    "alias_binding_accuracy_guard_passed",
                    "top_k_field_recall_guard_passed",
                    "harmful_memory_admission_guard_passed",
                    "wrong_field_autofill_guard_passed",
                    "misleading_retrieval_guard_passed",
                )
            else:
                baseline_retrieval = self._retrieval_at_primary_k(baseline)
                baseline_extraction = baseline.overall.extraction_metric
                candidate_retrieval = self._retrieval_at_primary_k(candidate)
                candidate_extraction = candidate.overall.extraction_metric
                if not self._passes_promotion_guards(
                    baseline_retrieval,
                    baseline_extraction,
                    candidate_retrieval,
                    candidate_extraction,
                ):
                    continue
                deltas = self._metric_deltas(
                    baseline_retrieval,
                    baseline_extraction,
                    candidate_retrieval,
                    candidate_extraction,
                )
                rationale_codes = (
                    "retrieval_recall_guard_passed",
                    "field_accuracy_guard_passed",
                    "review_recall_guard_passed",
                    "no_additional_history_autofill_errors",
                )
            digest = sha256(
                (
                    f"{request.tenant_id}\0{request.evaluation_run_id}\0{variant.value}"
                ).encode()
            ).hexdigest()
            candidates.append(
                PromotionCandidate(
                    candidate_id=f"promotion-{digest[:32]}",
                    tenant_id=request.tenant_id,
                    evaluation_run_id=request.evaluation_run_id,
                    baseline_variant=baseline_variant,
                    candidate_variant=variant,
                    metric_deltas=deltas,
                    rationale_codes=rationale_codes,
                    created_at=created_at,
                )
            )
        return tuple(candidates)

    def _passes_trusted_memory_field_guards(
        self,
        baseline: TrustedMemoryFieldMetric,
        candidate: TrustedMemoryFieldMetric,
    ) -> bool:
        required = (
            baseline.alias_binding_accuracy,
            candidate.alias_binding_accuracy,
            baseline.top_k_field_recall,
            candidate.top_k_field_recall,
            baseline.harmful_memory_admission_rate,
            candidate.harmful_memory_admission_rate,
            baseline.misleading_retrieval_rate,
            candidate.misleading_retrieval_rate,
        )
        if any(value is None for value in required):
            return False
        return (
            _required_float(candidate.alias_binding_accuracy)
            - _required_float(baseline.alias_binding_accuracy)
            >= self._policy.minimum_alias_binding_accuracy_gain
            and _required_float(candidate.top_k_field_recall)
            - _required_float(baseline.top_k_field_recall)
            >= self._policy.minimum_top_k_field_recall_gain
            and _required_float(candidate.harmful_memory_admission_rate)
            <= _required_float(baseline.harmful_memory_admission_rate)
            + self._policy.maximum_harmful_admission_rate_increase
            and candidate.wrong_field_auto_fill_count
            <= baseline.wrong_field_auto_fill_count
            + self._policy.maximum_wrong_field_auto_fill_increase
            and _required_float(candidate.misleading_retrieval_rate)
            <= _required_float(baseline.misleading_retrieval_rate)
            + self._policy.maximum_misleading_retrieval_rate_increase
        )

    @staticmethod
    def _trusted_memory_field_metric_deltas(
        baseline: TrustedMemoryFieldMetric,
        candidate: TrustedMemoryFieldMetric,
    ) -> dict[str, float]:
        deltas = {
            "harmful_memory_admission_rate": _required_float(
                candidate.harmful_memory_admission_rate
            )
            - _required_float(baseline.harmful_memory_admission_rate),
            "alias_binding_accuracy": _required_float(candidate.alias_binding_accuracy)
            - _required_float(baseline.alias_binding_accuracy),
            "top_k_field_recall": _required_float(candidate.top_k_field_recall)
            - _required_float(baseline.top_k_field_recall),
            "wrong_field_auto_fill_count": float(
                candidate.wrong_field_auto_fill_count
                - baseline.wrong_field_auto_fill_count
            ),
            "misleading_retrieval_rate": _required_float(
                candidate.misleading_retrieval_rate
            )
            - _required_float(baseline.misleading_retrieval_rate),
        }
        for name, baseline_value, candidate_value in (
            (
                "memory_approval_precision",
                baseline.memory_approval_precision,
                candidate.memory_approval_precision,
            ),
            (
                "memory_helpfulness_rate",
                baseline.memory_helpfulness_rate,
                candidate.memory_helpfulness_rate,
            ),
        ):
            if baseline_value is not None and candidate_value is not None:
                deltas[name] = candidate_value - baseline_value
        return deltas

    def _passes_promotion_guards(
        self,
        baseline_retrieval: RetrievalMetric,
        baseline_extraction: ExtractionMetric,
        candidate_retrieval: RetrievalMetric,
        candidate_extraction: ExtractionMetric,
    ) -> bool:
        values = (
            baseline_retrieval.recall_at_k,
            candidate_retrieval.recall_at_k,
            baseline_extraction.field_accuracy,
            candidate_extraction.field_accuracy,
        )
        if any(value is None for value in values):
            return False
        baseline_recall = _required_float(baseline_retrieval.recall_at_k)
        candidate_recall = _required_float(candidate_retrieval.recall_at_k)
        baseline_accuracy = _required_float(baseline_extraction.field_accuracy)
        candidate_accuracy = _required_float(candidate_extraction.field_accuracy)
        baseline_review_recall = baseline_extraction.review_required_recall
        candidate_review_recall = candidate_extraction.review_required_recall
        if (baseline_review_recall is None) != (candidate_review_recall is None):
            return False
        review_recall_guard = True
        if baseline_review_recall is not None and candidate_review_recall is not None:
            review_recall_guard = (
                candidate_review_recall
                >= baseline_review_recall - self._policy.maximum_review_recall_drop
            )
        return (
            candidate_recall - baseline_recall
            >= self._policy.minimum_recall_gain
            and candidate_accuracy - baseline_accuracy
            >= self._policy.minimum_field_accuracy_gain
            and review_recall_guard
            and candidate_extraction.erroneous_auto_filled_value_count
            <= baseline_extraction.erroneous_auto_filled_value_count
            and candidate_extraction.historical_override_violation_count
            <= baseline_extraction.historical_override_violation_count
        )

    @staticmethod
    def _metric_deltas(
        baseline_retrieval: RetrievalMetric,
        baseline_extraction: ExtractionMetric,
        candidate_retrieval: RetrievalMetric,
        candidate_extraction: ExtractionMetric,
    ) -> dict[str, float]:
        deltas = {
            "recall_at_k": _required_float(candidate_retrieval.recall_at_k)
            - _required_float(baseline_retrieval.recall_at_k),
            "field_accuracy": _required_float(candidate_extraction.field_accuracy)
            - _required_float(baseline_extraction.field_accuracy),
            "erroneous_auto_filled_value_count": float(
                candidate_extraction.erroneous_auto_filled_value_count
                - baseline_extraction.erroneous_auto_filled_value_count
            ),
            "historical_override_violation_count": float(
                candidate_extraction.historical_override_violation_count
                - baseline_extraction.historical_override_violation_count
            ),
        }
        if (
            baseline_extraction.review_required_recall is not None
            and candidate_extraction.review_required_recall is not None
        ):
            deltas["review_required_recall"] = (
                candidate_extraction.review_required_recall
                - baseline_extraction.review_required_recall
            )
        return deltas

    def _retrieval_at_primary_k(
        self,
        result: EvaluationVariantResult,
    ) -> RetrievalMetric:
        return next(
            item
            for item in result.overall.retrieval_metrics
            if item.k == self._policy.promotion_primary_k
        )

    def _trusted_at_primary_k(
        self,
        result: EvaluationVariantResult,
    ) -> TrustedMemoryFieldMetric:
        return next(
            item
            for item in result.overall.trusted_memory_field_metrics
            if item.k == self._policy.promotion_primary_k
        )

    @staticmethod
    def _case_buckets(case: EvaluationCase) -> tuple[EvaluationBucket, ...]:
        vendor_template = (
            f"vendor={case.vendor_fingerprint or 'none'};"
            f"template={case.template_fingerprint or 'none'}"
        )
        return (
            EvaluationBucket(
                EvaluationBucketDimension.DOCUMENT_TYPE,
                case.document_type,
            ),
            EvaluationBucket(EvaluationBucketDimension.FIELD_PATH, case.field_path),
            EvaluationBucket(
                EvaluationBucketDimension.VENDOR_TEMPLATE,
                vendor_template,
            ),
            EvaluationBucket(
                EvaluationBucketDimension.IMAGE_QUALITY,
                case.image_quality_bucket,
            ),
        )

    @staticmethod
    def _validate_observations(
        dataset: EvaluationDataset,
        suite: EvaluationSuite,
        variant: EvaluationVariant,
        observations: Sequence[EvaluationCaseObservation],
    ) -> tuple[EvaluationCaseObservation, ...]:
        by_case = {item.case_id: item for item in observations}
        if len(by_case) != len(observations):
            raise ValueError("Variant executor returned duplicate case observations")
        expected_case_ids = {item.case_id for item in dataset.cases}
        if set(by_case) != expected_case_ids:
            raise ValueError("Variant executor must return exactly one observation per case")
        training_document_ids = set(dataset.training_document_ids)
        evaluation_document_ids = set(dataset.evaluation_document_ids)
        for observation in observations:
            if observation.tenant_id != dataset.tenant_id:
                raise ValueError("Variant executor returned a cross-tenant observation")
            if observation.variant is not variant:
                raise ValueError("Variant executor returned an observation for another variant")
            if any(
                hit.source_document_id not in training_document_ids
                or hit.source_document_id in evaluation_document_ids
                for hit in observation.retrieved_examples
            ):
                raise ValueError("Retrieved examples violate document-level split isolation")
            if suite is EvaluationSuite.TRUSTED_MEMORY_FIELD_BINDING:
                case = next(
                    item for item in dataset.cases if item.case_id == observation.case_id
                )
                if case.memory_admission_ground_truth is not None:
                    admission = observation.memory_admission
                    if admission is None:
                        raise ValueError(
                            "Trusted-memory evaluation requires an admission observation"
                        )
                    if admission.example_id != case.memory_admission_ground_truth.example_id:
                        raise ValueError(
                            "Admission observation does not match its ground truth"
                        )
                elif observation.memory_admission is not None:
                    raise ValueError("Admission observation has no human ground truth")
                if case.field_binding_ground_truth is not None:
                    binding = observation.field_binding
                    if binding is None:
                        raise ValueError(
                            "Field-binding evaluation requires a binding observation"
                        )
                    if binding.evidence_id != case.field_binding_ground_truth.evidence_id:
                        raise ValueError(
                            "Field-binding observation does not match its ground truth"
                        )
                elif observation.field_binding is not None:
                    raise ValueError("Field-binding observation has no human ground truth")
        return tuple(by_case[item.case_id] for item in dataset.cases)

    @staticmethod
    def _validate_suite_dataset(
        dataset: EvaluationDataset,
        suite: EvaluationSuite,
    ) -> None:
        if suite is EvaluationSuite.CASE_RAG:
            return
        if not any(item.memory_admission_ground_truth is not None for item in dataset.cases):
            raise ValueError("Trusted-memory evaluation requires admission ground truth")
        if not any(item.field_binding_ground_truth is not None for item in dataset.cases):
            raise ValueError("Field-binding evaluation requires binding ground truth")
        if not any(item.expected_memory_effects for item in dataset.cases):
            raise ValueError("Trusted-memory evaluation requires memory-effect judgments")
        if (
            any(item.template_fingerprint is not None for item in dataset.cases)
            and not dataset.training_template_fingerprints
        ):
            raise ValueError(
                "Template-aware evaluation requires training template fingerprints"
            )

    @staticmethod
    def _validate_dataset_scope(
        dataset: EvaluationDataset,
        request: OfflineEvaluationRequest,
    ) -> None:
        if dataset.tenant_id != request.tenant_id:
            raise ResourceConflictError("Evaluation dataset tenant does not match the request")
        if dataset.dataset_id != request.dataset_id or dataset.version != request.dataset_version:
            raise ResourceConflictError("Evaluation dataset identity does not match the request")
        if not dataset.is_frozen:
            raise ResourceConflictError("Evaluation requires a frozen dataset version")

    @staticmethod
    def _validate_replay(
        existing: EvaluationRun,
        request: OfflineEvaluationRequest,
        dataset: EvaluationDataset,
    ) -> None:
        expected_identity = (
            request.tenant_id,
            request.dataset_id,
            request.dataset_version,
            dataset.schema_version,
            request.bindings,
            request.suite,
        )
        actual_identity = (
            existing.tenant_id,
            existing.dataset_id,
            existing.dataset_version,
            existing.schema_version,
            existing.bindings,
            existing.suite,
        )
        if actual_identity != expected_identity:
            raise ResourceConflictError(
                "Evaluation run identifier was reused with different immutable bindings"
            )

    @staticmethod
    def _has_document_isolation(dataset: EvaluationDataset) -> bool:
        training = set(dataset.training_document_ids)
        evaluation = set(dataset.evaluation_document_ids)
        training_templates = set(dataset.training_template_fingerprints)
        evaluation_templates = {
            item.template_fingerprint
            for item in dataset.cases
            if item.template_fingerprint is not None
        }
        return training.isdisjoint(evaluation) and training_templates.isdisjoint(
            evaluation_templates
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Offline evaluation clock must return timezone-aware values")
        return value


def _mean_or_none(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _ratio_or_none(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _canonical_json(value: JsonValue) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_equal(left: JsonValue, right: JsonValue) -> bool:
    return _canonical_json(left) == _canonical_json(right)


def _required_float(value: float | None) -> float:
    if value is None:
        raise ValueError("A required evaluation metric is undefined")
    return value
