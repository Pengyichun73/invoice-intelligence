"""Framework-independent contracts for versioned offline evaluation suites."""

import json
import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from invoice_intelligence.domain.admission import MemoryAdmissionStatus
from invoice_intelligence.domain.examples import (
    ExampleEvidenceReference,
    ExampleLabelType,
    IndexVersion,
    ModelVersion,
    PromptVersion,
    RetrievalPolicyVersion,
    RetrievalRecallSource,
    RetrievalScore,
)
from invoice_intelligence.domain.field_semantics import FieldBindingStatus
from invoice_intelligence.domain.workflow import JsonValue


class EvaluationSuite(StrEnum):
    """Versioned metric contract selected for one immutable evaluation run."""

    CASE_RAG = "case_rag"
    TRUSTED_MEMORY_FIELD_BINDING = "trusted_memory_field_binding"


class EvaluationVariant(StrEnum):
    """Required, independently measured offline comparison variants."""

    NO_MEMORY = "no_memory"
    PGVECTOR_LEGACY = "pgvector_legacy"
    DENSE_ONLY = "dense_only"
    SPARSE_ONLY = "sparse_only"
    HYBRID = "hybrid"
    HYBRID_RERANKER = "hybrid_reranker"
    HYBRID_POSITIVE_NEGATIVE_FEW_SHOT = "hybrid_positive_negative_few_shot"
    NO_FIELD_CATALOG = "no_field_catalog"
    STATIC_FIELD_DESCRIPTIONS = "static_field_descriptions"
    FIELD_DESCRIPTIONS_ALIASES = "field_descriptions_aliases"
    HYBRID_CONTEXT_ANCHORS = "hybrid_context_anchors"


CASE_RAG_EVALUATION_VARIANTS: tuple[EvaluationVariant, ...] = (
    EvaluationVariant.NO_MEMORY,
    EvaluationVariant.PGVECTOR_LEGACY,
    EvaluationVariant.DENSE_ONLY,
    EvaluationVariant.SPARSE_ONLY,
    EvaluationVariant.HYBRID,
    EvaluationVariant.HYBRID_RERANKER,
    EvaluationVariant.HYBRID_POSITIVE_NEGATIVE_FEW_SHOT,
)
TRUSTED_MEMORY_FIELD_BINDING_VARIANTS: tuple[EvaluationVariant, ...] = (
    EvaluationVariant.NO_FIELD_CATALOG,
    EvaluationVariant.STATIC_FIELD_DESCRIPTIONS,
    EvaluationVariant.FIELD_DESCRIPTIONS_ALIASES,
    EvaluationVariant.DENSE_ONLY,
    EvaluationVariant.SPARSE_ONLY,
    EvaluationVariant.HYBRID,
    EvaluationVariant.HYBRID_RERANKER,
    EvaluationVariant.HYBRID_CONTEXT_ANCHORS,
)
# Backward-compatible name for the original case-RAG suite.
REQUIRED_EVALUATION_VARIANTS = CASE_RAG_EVALUATION_VARIANTS


def required_variants_for_suite(
    suite: EvaluationSuite,
) -> tuple[EvaluationVariant, ...]:
    if suite is EvaluationSuite.CASE_RAG:
        return CASE_RAG_EVALUATION_VARIANTS
    return TRUSTED_MEMORY_FIELD_BINDING_VARIANTS


class EvaluationRunStatus(StrEnum):
    """Persisted lifecycle of one offline evaluation run."""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EvaluationBucketDimension(StrEnum):
    """Supported dimensions for deterministic metric slicing."""

    OVERALL = "overall"
    DOCUMENT_TYPE = "document_type"
    FIELD_PATH = "field_path"
    VENDOR_TEMPLATE = "vendor_template"
    IMAGE_QUALITY = "image_quality"


class MemoryEffectJudgment(StrEnum):
    """Human-adjudicated effect of showing one memory for the held-out evidence."""

    HELPFUL = "helpful"
    MISLEADING = "misleading"


@dataclass(frozen=True, slots=True)
class EvaluationReviewerJudgment:
    """One explicit human admission judgment used only as evaluation evidence."""

    reviewer_id: str
    status: MemoryAdmissionStatus

    def __post_init__(self) -> None:
        _require_text("Evaluation reviewer identifier", self.reviewer_id)
        if self.status not in {
            MemoryAdmissionStatus.APPROVED,
            MemoryAdmissionStatus.QUARANTINED,
            MemoryAdmissionStatus.REJECTED,
        }:
            raise ValueError("Evaluation reviewer judgment must be a terminal gate outcome")


@dataclass(frozen=True, slots=True)
class MemoryAdmissionGroundTruth:
    """Human-adjudicated memory quality label; model advice is never ground truth."""

    example_id: str
    expected_status: MemoryAdmissionStatus
    harmful_if_admitted: bool
    reviewer_judgments: tuple[EvaluationReviewerJudgment, ...]
    adjudicator_id: str
    adjudicated_at: datetime

    def __post_init__(self) -> None:
        _require_text("Admission ground-truth example identifier", self.example_id)
        _require_text("Admission ground-truth adjudicator", self.adjudicator_id)
        if self.expected_status not in {
            MemoryAdmissionStatus.APPROVED,
            MemoryAdmissionStatus.QUARANTINED,
            MemoryAdmissionStatus.REJECTED,
        }:
            raise ValueError("Admission ground truth must use a terminal gate outcome")
        if (
            self.harmful_if_admitted
            and self.expected_status is MemoryAdmissionStatus.APPROVED
        ):
            raise ValueError("A harmful memory cannot have an approved ground-truth status")
        if not self.reviewer_judgments:
            raise ValueError("Admission ground truth requires an explicit reviewer judgment")
        reviewer_ids = tuple(item.reviewer_id for item in self.reviewer_judgments)
        if len(reviewer_ids) != len(set(reviewer_ids)):
            raise ValueError("Admission ground-truth reviewers must be unique")
        if self.adjudicated_at.tzinfo is None:
            raise ValueError("Admission ground-truth time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class FieldBindingGroundTruth:
    """Human-adjudicated canonical field binding for current visual evidence."""

    evidence_id: str
    expected_status: FieldBindingStatus
    acceptable_field_paths: tuple[str, ...]
    is_alias_case: bool
    adjudicator_id: str
    adjudicated_at: datetime

    def __post_init__(self) -> None:
        _require_text("Field-binding ground-truth evidence", self.evidence_id)
        _require_text("Field-binding ground-truth adjudicator", self.adjudicator_id)
        if any(not item.strip() for item in self.acceptable_field_paths):
            raise ValueError("Acceptable field paths must not contain blanks")
        if len(self.acceptable_field_paths) != len(set(self.acceptable_field_paths)):
            raise ValueError("Acceptable field paths must be unique")
        if self.expected_status is FieldBindingStatus.ACCEPTED:
            if len(self.acceptable_field_paths) != 1:
                raise ValueError("Accepted field ground truth requires exactly one field path")
        elif self.expected_status is FieldBindingStatus.REVIEW_REQUIRED:
            if not self.acceptable_field_paths:
                raise ValueError("Ambiguous field ground truth requires candidate field paths")
        elif self.acceptable_field_paths:
            raise ValueError("Unresolved field ground truth cannot declare a field path")
        if self.adjudicated_at.tzinfo is None:
            raise ValueError("Field-binding ground-truth time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExpectedMemoryEffect:
    """Human label for whether a training-side memory helps this held-out case."""

    example_id: str
    source_document_id: str
    judgment: MemoryEffectJudgment

    def __post_init__(self) -> None:
        _require_text("Expected memory-effect example", self.example_id)
        _require_text("Expected memory-effect source document", self.source_document_id)


@dataclass(frozen=True, slots=True)
class ExpectedExample:
    """Human-reviewed example expected in the appropriate retrieval region."""

    example_id: str
    source_document_id: str
    label_type: ExampleLabelType
    relevance_grade: int = 1

    def __post_init__(self) -> None:
        _require_text("Expected example identifier", self.example_id)
        _require_text("Expected example source document", self.source_document_id)
        if not 1 <= self.relevance_grade <= 3:
            raise ValueError("Expected example relevance_grade must be between 1 and 3")


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """Human-adjudicated field-level ground truth for one held-out document."""

    case_id: str
    tenant_id: str
    document_id: str
    document_type: str
    field_path: str
    schema_version: str
    vendor_fingerprint: str | None
    template_fingerprint: str | None
    image_quality_bucket: str
    evidence_reference: ExampleEvidenceReference
    expected_examples: tuple[ExpectedExample, ...]
    expected_value: JsonValue
    expected_is_missing: bool
    expects_candidate: bool
    expected_review_required: bool
    reviewer_id: str
    created_at: datetime
    memory_admission_ground_truth: MemoryAdmissionGroundTruth | None = None
    field_binding_ground_truth: FieldBindingGroundTruth | None = None
    expected_memory_effects: tuple[ExpectedMemoryEffect, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("Evaluation case identifier", self.case_id),
            ("Evaluation tenant identifier", self.tenant_id),
            ("Evaluation document identifier", self.document_id),
            ("Evaluation document type", self.document_type),
            ("Evaluation field path", self.field_path),
            ("Evaluation Schema version", self.schema_version),
            ("Image quality bucket", self.image_quality_bucket),
            ("Evaluation reviewer identifier", self.reviewer_id),
        ):
            _require_text(name, value)
        for name, value in (
            ("Vendor fingerprint", self.vendor_fingerprint),
            ("Template fingerprint", self.template_fingerprint),
        ):
            if value is not None:
                _require_text(name, value)
        example_ids = tuple(item.example_id for item in self.expected_examples)
        if len(example_ids) != len(set(example_ids)):
            raise ValueError("Evaluation expected example identifiers must be unique")
        if self.expected_is_missing and self.expected_value is not None:
            raise ValueError("A genuinely missing field cannot define an expected value")
        if self.expected_is_missing and self.expects_candidate:
            raise ValueError("A genuinely missing field cannot require a candidate value")
        if self.expects_candidate and not self.expected_review_required:
            raise ValueError("Expected candidate cases must require human review")
        binding_truth = self.field_binding_ground_truth
        if (
            binding_truth is not None
            and binding_truth.acceptable_field_paths
            and self.field_path not in binding_truth.acceptable_field_paths
        ):
            raise ValueError("Field-binding ground truth must include the case field path")
        effect_ids = tuple(item.example_id for item in self.expected_memory_effects)
        if len(effect_ids) != len(set(effect_ids)):
            raise ValueError("Expected memory-effect example identifiers must be unique")
        helpful_ids = {
            item.example_id
            for item in self.expected_memory_effects
            if item.judgment is MemoryEffectJudgment.HELPFUL
        }
        misleading_ids = {
            item.example_id
            for item in self.expected_memory_effects
            if item.judgment is MemoryEffectJudgment.MISLEADING
        }
        if helpful_ids.intersection(misleading_ids):
            raise ValueError("A memory cannot be both helpful and misleading")
        if self.created_at.tzinfo is None:
            raise ValueError("Evaluation case created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    """Immutable tenant dataset with document-level train/evaluation isolation."""

    dataset_id: str
    tenant_id: str
    name: str
    version: str
    schema_version: str
    training_document_ids: tuple[str, ...]
    cases: tuple[EvaluationCase, ...]
    created_at: datetime
    is_frozen: Literal[True] = True
    training_template_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("Evaluation dataset identifier", self.dataset_id),
            ("Evaluation dataset tenant", self.tenant_id),
            ("Evaluation dataset name", self.name),
            ("Evaluation dataset version", self.version),
            ("Evaluation dataset Schema version", self.schema_version),
        ):
            _require_text(name, value)
        if not self.cases:
            raise ValueError("Evaluation dataset must contain at least one case")
        if self.is_frozen is not True:
            raise ValueError("Evaluation dataset versions must be frozen")
        if not self.training_document_ids:
            raise ValueError("Evaluation dataset must declare training document identifiers")
        if not self.training_template_fingerprints:
            raise ValueError("Evaluation dataset must declare training template fingerprints")
        if len(self.training_document_ids) != len(set(self.training_document_ids)):
            raise ValueError("Training document identifiers must be unique")
        if any(not document_id.strip() for document_id in self.training_document_ids):
            raise ValueError("Training document identifiers must not contain blanks")
        if any(not item.strip() for item in self.training_template_fingerprints):
            raise ValueError("Training template fingerprints must not contain blanks")
        if len(self.training_template_fingerprints) != len(
            set(self.training_template_fingerprints)
        ):
            raise ValueError("Training template fingerprints must be unique")
        case_ids = tuple(item.case_id for item in self.cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Evaluation case identifiers must be unique within a dataset")
        if any(item.tenant_id != self.tenant_id for item in self.cases):
            raise ValueError("Evaluation dataset cannot contain cross-tenant cases")
        if any(item.schema_version != self.schema_version for item in self.cases):
            raise ValueError("Evaluation dataset cases must use one Schema version")
        if any(item.template_fingerprint is None for item in self.cases):
            raise ValueError("Evaluation cases require template fingerprints for split isolation")
        evaluation_document_ids = {item.document_id for item in self.cases}
        training_document_ids = set(self.training_document_ids)
        leaked_documents = evaluation_document_ids & training_document_ids
        if leaked_documents:
            raise ValueError("Training and evaluation document_id sets must be disjoint")
        evaluation_templates = {
            item.template_fingerprint
            for item in self.cases
            if item.template_fingerprint is not None
        }
        if evaluation_templates.intersection(self.training_template_fingerprints):
            raise ValueError("Near-duplicate templates cannot cross evaluation splits")
        for case in self.cases:
            if any(
                expected.source_document_id not in training_document_ids
                for expected in case.expected_examples
            ):
                raise ValueError(
                    "Expected examples must originate from declared training documents"
                )
            if any(
                effect.source_document_id not in training_document_ids
                for effect in case.expected_memory_effects
            ):
                raise ValueError(
                    "Expected memory effects must originate from training documents"
                )
        if self.created_at.tzinfo is None:
            raise ValueError("Evaluation dataset created_at must be timezone-aware")

    @property
    def evaluation_document_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item.document_id for item in self.cases}))


@dataclass(frozen=True, slots=True)
class EvaluationBindings:
    """Rollback-capable versions fixed for an entire comparison run."""

    index_version: IndexVersion
    model_version: ModelVersion
    prompt_version: PromptVersion
    retrieval_policy_version: RetrievalPolicyVersion
    threshold_version: str
    catalog_version: str | None = None
    admission_policy_version: str | None = None
    field_binding_policy_version: str | None = None

    def __post_init__(self) -> None:
        _require_text("Evaluation threshold version", self.threshold_version)
        for name, value in (
            ("Evaluation Catalog version", self.catalog_version),
            ("Evaluation admission policy version", self.admission_policy_version),
            ("Evaluation field-binding policy version", self.field_binding_policy_version),
        ):
            if value is not None:
                _require_text(name, value)


@dataclass(frozen=True, slots=True)
class EvaluationRetrievedExample:
    """Minimal scored hit used by offline metrics; scores are never probabilities."""

    example_id: str
    source_document_id: str
    label_type: ExampleLabelType
    rank: int
    ranking_score: float
    scores: RetrievalScore
    recall_sources: tuple[RetrievalRecallSource, ...]
    is_reviewed: Literal[True] = True
    is_valid: Literal[True] = True
    admission_approved: Literal[True] = True

    def __post_init__(self) -> None:
        _require_text("Retrieved evaluation example identifier", self.example_id)
        _require_text(
            "Retrieved evaluation example source document",
            self.source_document_id,
        )
        if self.rank <= 0:
            raise ValueError("Retrieved evaluation rank must be greater than zero")
        if not math.isfinite(self.ranking_score):
            raise ValueError("Evaluation ranking_score must be finite")
        if not self.recall_sources:
            raise ValueError("Evaluation retrieval hit requires a recall source")
        if len(self.recall_sources) != len(set(self.recall_sources)):
            raise ValueError("Evaluation recall sources must be unique")
        if (
            self.is_reviewed is not True
            or self.is_valid is not True
            or self.admission_approved is not True
        ):
            raise ValueError(
                "Offline retrieval metrics only accept approved, reviewed, valid examples"
            )


@dataclass(frozen=True, slots=True)
class ExtractionEvaluationOutput:
    """Observed extraction behavior without modifying InvoiceExtraction."""

    actual_value: JsonValue
    predicted_missing: bool
    candidate_values: tuple[JsonValue, ...]
    review_required: bool
    current_evidence_sufficient: bool
    used_historical_prior_as_value: bool

    def __post_init__(self) -> None:
        canonical = tuple(_canonical_json(item) for item in self.candidate_values)
        if len(canonical) != len(set(canonical)):
            raise ValueError("Extraction candidate values must be unique")
        if self.predicted_missing and self.actual_value is not None:
            raise ValueError("A predicted-missing field cannot contain an actual value")
        if self.used_historical_prior_as_value and self.predicted_missing:
            raise ValueError("A missing result cannot also use a historical prior as its value")


@dataclass(frozen=True, slots=True)
class MemoryAdmissionEvaluationOutput:
    """Observed policy outcome; only approved means eligible for derived indexing."""

    example_id: str
    status: MemoryAdmissionStatus
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text("Evaluated admission example identifier", self.example_id)
        if any(not item.strip() for item in self.reason_codes):
            raise ValueError("Evaluated admission reason codes must not contain blanks")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("Evaluated admission reason codes must be unique")


@dataclass(frozen=True, slots=True)
class FieldBindingEvaluationOutput:
    """Observed binding result without authority to write an InvoiceExtraction value."""

    evidence_id: str
    status: FieldBindingStatus
    selected_canonical_field_path: str | None
    candidate_field_paths: tuple[str, ...]
    auto_fill_performed: bool

    def __post_init__(self) -> None:
        _require_text("Evaluated field-binding evidence", self.evidence_id)
        if self.selected_canonical_field_path is not None:
            _require_text(
                "Evaluated selected canonical field path",
                self.selected_canonical_field_path,
            )
        if any(not item.strip() for item in self.candidate_field_paths):
            raise ValueError("Evaluated field candidates must not contain blanks")
        if len(self.candidate_field_paths) != len(set(self.candidate_field_paths)):
            raise ValueError("Evaluated field candidates must be unique")
        if self.status is FieldBindingStatus.ACCEPTED:
            if self.selected_canonical_field_path is None:
                raise ValueError("Accepted field binding output requires a selected path")
            if self.selected_canonical_field_path not in self.candidate_field_paths:
                raise ValueError("Selected field path must be present in ranked candidates")
        elif self.selected_canonical_field_path is not None:
            raise ValueError("Non-accepted field binding output cannot select a field")
        if self.auto_fill_performed and self.status is not FieldBindingStatus.ACCEPTED:
            raise ValueError("Only an accepted binding can report an automatic fill")


@dataclass(frozen=True, slots=True)
class EvaluationCaseObservation:
    """One variant's retrieval and extraction output for one held-out case."""

    case_id: str
    tenant_id: str
    variant: EvaluationVariant
    retrieved_examples: tuple[EvaluationRetrievedExample, ...]
    extraction: ExtractionEvaluationOutput
    memory_admission: MemoryAdmissionEvaluationOutput | None = None
    field_binding: FieldBindingEvaluationOutput | None = None

    def __post_init__(self) -> None:
        _require_text("Evaluation observation case identifier", self.case_id)
        _require_text("Evaluation observation tenant identifier", self.tenant_id)
        example_ids = tuple(item.example_id for item in self.retrieved_examples)
        if len(example_ids) != len(set(example_ids)):
            raise ValueError("Evaluation observation cannot contain duplicate examples")
        ranks = tuple(item.rank for item in self.retrieved_examples)
        if len(ranks) != len(set(ranks)):
            raise ValueError("Evaluation observation ranks must be unique")
        if self.variant is EvaluationVariant.NO_MEMORY and self.retrieved_examples:
            raise ValueError("The no-memory variant cannot return historical examples")


@dataclass(frozen=True, slots=True)
class RetrievalMetric:
    """Aggregate retrieval metrics with explicit denominators."""

    k: int
    recall_at_k: float | None
    hit_rate_at_k: float | None
    mrr: float | None
    ndcg_at_k: float | None
    positive_negative_separation: float | None
    empty_retrieval_rate: float
    evaluation_case_count: int
    expected_example_case_count: int
    expected_example_count: int
    relevant_hit_count: int
    empty_retrieval_count: int
    separation_case_count: int

    def __post_init__(self) -> None:
        if self.k <= 0:
            raise ValueError("Retrieval metric K must be greater than zero")
        for name, value in (
            ("recall_at_k", self.recall_at_k),
            ("hit_rate_at_k", self.hit_rate_at_k),
            ("mrr", self.mrr),
            ("ndcg_at_k", self.ndcg_at_k),
            ("empty_retrieval_rate", self.empty_retrieval_rate),
        ):
            _require_ratio(name, value)
        if self.positive_negative_separation is not None and not math.isfinite(
            self.positive_negative_separation
        ):
            raise ValueError("Positive/negative separation must be finite")
        counts = (
            self.evaluation_case_count,
            self.expected_example_case_count,
            self.expected_example_count,
            self.relevant_hit_count,
            self.empty_retrieval_count,
            self.separation_case_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Retrieval metric counts must not be negative")
        if self.evaluation_case_count <= 0:
            raise ValueError("Retrieval metrics require at least one evaluation case")


@dataclass(frozen=True, slots=True)
class ExtractionMetric:
    """Aggregate field and review-routing metrics with auditable counts."""

    field_accuracy: float | None
    missing_recognition_accuracy: float
    candidate_hit_rate: float | None
    erroneous_auto_filled_value_count: int
    historical_override_violation_count: int
    review_required_precision: float | None
    review_required_recall: float | None
    evaluation_case_count: int
    field_accuracy_denominator: int
    missing_recognition_correct_count: int
    candidate_case_count: int
    candidate_hit_count: int
    review_true_positive_count: int
    review_false_positive_count: int
    review_false_negative_count: int

    def __post_init__(self) -> None:
        for name, value in (
            ("field_accuracy", self.field_accuracy),
            ("missing_recognition_accuracy", self.missing_recognition_accuracy),
            ("candidate_hit_rate", self.candidate_hit_rate),
            ("review_required_precision", self.review_required_precision),
            ("review_required_recall", self.review_required_recall),
        ):
            _require_ratio(name, value)
        counts = (
            self.erroneous_auto_filled_value_count,
            self.historical_override_violation_count,
            self.evaluation_case_count,
            self.field_accuracy_denominator,
            self.missing_recognition_correct_count,
            self.candidate_case_count,
            self.candidate_hit_count,
            self.review_true_positive_count,
            self.review_false_positive_count,
            self.review_false_negative_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Extraction metric counts must not be negative")
        if self.evaluation_case_count <= 0:
            raise ValueError("Extraction metrics require at least one evaluation case")


@dataclass(frozen=True, slots=True)
class TrustedMemoryFieldMetric:
    """Auditable trusted-memory and field-binding metrics for one Top-K cutoff."""

    k: int
    memory_approval_precision: float | None
    harmful_memory_admission_rate: float | None
    quarantine_rate: float | None
    reviewer_disagreement_rate: float | None
    alias_binding_accuracy: float | None
    top_k_field_recall: float | None
    field_binding_ambiguity_rate: float | None
    wrong_field_auto_fill_count: int
    memory_helpfulness_rate: float | None
    misleading_retrieval_rate: float | None
    evaluation_case_count: int
    memory_admission_case_count: int
    approved_prediction_count: int
    correct_approved_count: int
    harmful_ground_truth_count: int
    harmful_admitted_count: int
    quarantined_prediction_count: int
    multi_reviewer_case_count: int
    reviewer_disagreement_count: int
    alias_binding_case_count: int
    correct_alias_binding_count: int
    field_binding_case_count: int
    expected_field_path_count: int
    recalled_field_path_count: int
    ambiguous_prediction_count: int
    helpful_opportunity_count: int
    helpful_hit_count: int
    memory_effect_case_count: int
    misleading_hit_count: int

    def __post_init__(self) -> None:
        if self.k <= 0:
            raise ValueError("Trusted-memory field metric K must be greater than zero")
        for name, value in (
            ("memory_approval_precision", self.memory_approval_precision),
            ("harmful_memory_admission_rate", self.harmful_memory_admission_rate),
            ("quarantine_rate", self.quarantine_rate),
            ("reviewer_disagreement_rate", self.reviewer_disagreement_rate),
            ("alias_binding_accuracy", self.alias_binding_accuracy),
            ("top_k_field_recall", self.top_k_field_recall),
            ("field_binding_ambiguity_rate", self.field_binding_ambiguity_rate),
            ("memory_helpfulness_rate", self.memory_helpfulness_rate),
            ("misleading_retrieval_rate", self.misleading_retrieval_rate),
        ):
            _require_ratio(name, value)
        counts = (
            self.wrong_field_auto_fill_count,
            self.evaluation_case_count,
            self.memory_admission_case_count,
            self.approved_prediction_count,
            self.correct_approved_count,
            self.harmful_ground_truth_count,
            self.harmful_admitted_count,
            self.quarantined_prediction_count,
            self.multi_reviewer_case_count,
            self.reviewer_disagreement_count,
            self.alias_binding_case_count,
            self.correct_alias_binding_count,
            self.field_binding_case_count,
            self.expected_field_path_count,
            self.recalled_field_path_count,
            self.ambiguous_prediction_count,
            self.helpful_opportunity_count,
            self.helpful_hit_count,
            self.memory_effect_case_count,
            self.misleading_hit_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Trusted-memory field metric counts must not be negative")
        if self.evaluation_case_count <= 0:
            raise ValueError("Trusted-memory field metrics require evaluation cases")


@dataclass(frozen=True, slots=True)
class EvaluationBucket:
    """One non-sensitive aggregation slice."""

    dimension: EvaluationBucketDimension
    value: str

    def __post_init__(self) -> None:
        _require_text("Evaluation bucket value", self.value)
        if self.dimension is EvaluationBucketDimension.OVERALL and self.value != "all":
            raise ValueError("Overall evaluation bucket value must be 'all'")


@dataclass(frozen=True, slots=True)
class EvaluationBucketResult:
    """Metrics for one deterministic dataset bucket."""

    bucket: EvaluationBucket
    retrieval_metrics: tuple[RetrievalMetric, ...]
    extraction_metric: ExtractionMetric
    trusted_memory_field_metrics: tuple[TrustedMemoryFieldMetric, ...] = ()

    def __post_init__(self) -> None:
        k_values = tuple(item.k for item in self.retrieval_metrics)
        if not k_values or len(k_values) != len(set(k_values)):
            raise ValueError("Bucket retrieval K values must be non-empty and unique")
        trusted_k_values = tuple(item.k for item in self.trusted_memory_field_metrics)
        if len(trusted_k_values) != len(set(trusted_k_values)):
            raise ValueError("Trusted-memory field metric K values must be unique")
        if trusted_k_values and trusted_k_values != k_values:
            raise ValueError("Trusted-memory field and retrieval metrics must use the same K")


@dataclass(frozen=True, slots=True)
class EvaluationVariantResult:
    """Complete metrics for one comparison variant."""

    variant: EvaluationVariant
    overall: EvaluationBucketResult
    buckets: tuple[EvaluationBucketResult, ...]

    def __post_init__(self) -> None:
        if self.overall.bucket.dimension is not EvaluationBucketDimension.OVERALL:
            raise ValueError("Variant overall result must use the overall bucket")
        keys = tuple((item.bucket.dimension, item.bucket.value) for item in self.buckets)
        if len(keys) != len(set(keys)):
            raise ValueError("Evaluation variant bucket keys must be unique")
        if any(
            item.bucket.dimension is EvaluationBucketDimension.OVERALL
            for item in self.buckets
        ):
            raise ValueError("Overall metrics must not be duplicated in bucket results")


@dataclass(frozen=True, slots=True)
class PromotionCandidate:
    """Tenant-scoped proposal requiring human approval; it cannot mutate production."""

    candidate_id: str
    tenant_id: str
    evaluation_run_id: str
    baseline_variant: EvaluationVariant
    candidate_variant: EvaluationVariant
    metric_deltas: dict[str, float]
    rationale_codes: tuple[str, ...]
    created_at: datetime
    status: Literal["promotion_candidate"] = "promotion_candidate"
    requires_human_approval: Literal[True] = True
    may_modify_production: Literal[False] = False

    def __post_init__(self) -> None:
        for name, value in (
            ("Promotion candidate identifier", self.candidate_id),
            ("Promotion tenant identifier", self.tenant_id),
            ("Promotion evaluation run identifier", self.evaluation_run_id),
        ):
            _require_text(name, value)
        if self.baseline_variant is self.candidate_variant:
            raise ValueError("Promotion baseline and candidate variants must differ")
        if not self.metric_deltas:
            raise ValueError("Promotion candidate requires metric deltas")
        if any(not key.strip() for key in self.metric_deltas):
            raise ValueError("Promotion metric names must not be blank")
        if any(not math.isfinite(value) for value in self.metric_deltas.values()):
            raise ValueError("Promotion metric deltas must be finite")
        if not self.rationale_codes or any(
            not item.strip() for item in self.rationale_codes
        ):
            raise ValueError("Promotion candidate requires non-blank rationale codes")
        if self.created_at.tzinfo is None:
            raise ValueError("Promotion candidate created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    """Version-bound offline comparison; completion cannot alter production settings."""

    evaluation_run_id: str
    tenant_id: str
    dataset_id: str
    dataset_version: str
    schema_version: str
    bindings: EvaluationBindings
    variants: tuple[EvaluationVariant, ...]
    status: EvaluationRunStatus
    results: tuple[EvaluationVariantResult, ...]
    promotion_candidates: tuple[PromotionCandidate, ...]
    leakage_check_passed: bool
    report_schema_version: str
    artifact_references: tuple[str, ...]
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: str | None = None
    suite: EvaluationSuite = EvaluationSuite.CASE_RAG

    def __post_init__(self) -> None:
        for name, value in (
            ("Evaluation run identifier", self.evaluation_run_id),
            ("Evaluation run tenant", self.tenant_id),
            ("Evaluation run dataset identifier", self.dataset_id),
            ("Evaluation run dataset version", self.dataset_version),
            ("Evaluation run Schema version", self.schema_version),
            ("Evaluation report Schema version", self.report_schema_version),
        ):
            _require_text(name, value)
        if self.variants != required_variants_for_suite(self.suite):
            raise ValueError("Evaluation run must compare every suite variant in order")
        if self.suite is EvaluationSuite.TRUSTED_MEMORY_FIELD_BINDING and any(
            value is None
            for value in (
                self.bindings.catalog_version,
                self.bindings.admission_policy_version,
                self.bindings.field_binding_policy_version,
            )
        ):
            raise ValueError(
                "Trusted-memory field evaluation requires Catalog and policy versions"
            )
        result_variants = tuple(item.variant for item in self.results)
        if len(result_variants) != len(set(result_variants)):
            raise ValueError("Evaluation run result variants must be unique")
        if any(variant not in self.variants for variant in result_variants):
            raise ValueError("Evaluation run contains a result outside its Suite")
        result_buckets = tuple(
            bucket
            for result in self.results
            for bucket in (result.overall, *result.buckets)
        )
        if self.suite is EvaluationSuite.TRUSTED_MEMORY_FIELD_BINDING:
            if result_buckets and any(
                not bucket.trusted_memory_field_metrics for bucket in result_buckets
            ):
                raise ValueError(
                    "Trusted-memory field runs require Suite metrics in every bucket"
                )
        elif any(bucket.trusted_memory_field_metrics for bucket in result_buckets):
            raise ValueError("Case-RAG runs cannot contain trusted-memory field metrics")
        if self.status is EvaluationRunStatus.CREATED:
            if self.started_at is not None or self.completed_at is not None or self.results:
                raise ValueError("Created evaluation runs cannot contain execution state")
        elif self.status is EvaluationRunStatus.RUNNING:
            if self.started_at is None or self.completed_at is not None or self.results:
                raise ValueError("Running evaluation run state is inconsistent")
        elif self.status is EvaluationRunStatus.COMPLETED:
            if self.started_at is None or self.completed_at is None:
                raise ValueError("Completed evaluation runs require execution timestamps")
            if result_variants != self.variants:
                raise ValueError("Completed evaluation runs require every variant result")
            if not self.leakage_check_passed:
                raise ValueError("A run with unresolved data leakage cannot complete")
            if self.failure_code is not None:
                raise ValueError("Completed evaluation runs cannot contain a failure code")
        elif self.status is EvaluationRunStatus.FAILED:
            if self.started_at is None or self.completed_at is None:
                raise ValueError("Failed evaluation runs require execution timestamps")
            if self.failure_code is None:
                raise ValueError("Failed evaluation runs require a non-secret failure code")
        if self.status is not EvaluationRunStatus.FAILED and self.failure_code is not None:
            raise ValueError("Only failed evaluation runs may contain a failure code")
        if any(item.tenant_id != self.tenant_id for item in self.promotion_candidates):
            raise ValueError("Promotion candidates cannot cross tenant boundaries")
        if any(
            item.evaluation_run_id != self.evaluation_run_id
            for item in self.promotion_candidates
        ):
            raise ValueError("Promotion candidates must belong to their evaluation run")
        if len(self.artifact_references) != len(set(self.artifact_references)):
            raise ValueError("Evaluation artifact references must be unique")
        if any(not item.strip() for item in self.artifact_references):
            raise ValueError("Evaluation artifact references must not contain blanks")
        for name, value in (
            ("created_at", self.created_at),
            ("started_at", self.started_at),
            ("completed_at", self.completed_at),
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"Evaluation run {name} must be timezone-aware")


def _canonical_json(value: JsonValue) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _require_text(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_ratio(name: str, value: float | None) -> None:
    if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be between zero and one")
