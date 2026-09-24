"""Framework-independent contracts for reviewed-example retrieval."""

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from invoice_intelligence.domain.json_types import JsonValue

SparseVector: TypeAlias = tuple[tuple[int, float], ...]


class ExampleLabelType(StrEnum):
    """Explicit customer-review labels; admission approval is a separate gate."""

    CONFIRMED_CORRECT = "confirmed_correct"
    CORRECTED = "corrected"
    CONFIRMED_INCORRECT = "confirmed_incorrect"


class IndexProjectionStatus(StrEnum):
    """PostgreSQL-owned lifecycle of one rebuildable index projection."""

    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    INVALIDATED = "invalidated"


class RetrievalRecallSource(StrEnum):
    """Observable retrieval channels; none of these imply probability."""

    DENSE = "dense"
    SPARSE = "sparse"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class IndexVersion:
    """Opaque, rollback-capable version of one derived retrieval index."""

    value: str

    def __post_init__(self) -> None:
        _require_text("Index version", self.value)


@dataclass(frozen=True, slots=True)
class ExampleIndexProjectionState:
    """Safe PostgreSQL status for one example/index-version projection."""

    projection_id: str
    tenant_id: str
    example_id: str
    index_version: IndexVersion
    status: IndexProjectionStatus
    attempt_count: int
    projection_checksum: str | None
    last_error_code: str | None
    processing_started_at: datetime | None
    created_at: datetime
    updated_at: datetime
    indexed_at: datetime | None
    invalidated_at: datetime | None

    def __post_init__(self) -> None:
        for name, value in (
            ("projection_id", self.projection_id),
            ("tenant_id", self.tenant_id),
            ("example_id", self.example_id),
        ):
            _require_text(name, value)
        if self.attempt_count < 0:
            raise ValueError("Projection attempt_count must not be negative")
        for name, optional_value in (
            ("projection_checksum", self.projection_checksum),
            ("last_error_code", self.last_error_code),
        ):
            if optional_value is not None:
                _require_text(name, optional_value)
        for name, timestamp in (
            ("processing_started_at", self.processing_started_at),
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
            ("indexed_at", self.indexed_at),
            ("invalidated_at", self.invalidated_at),
        ):
            if timestamp is not None and timestamp.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("Projection updated_at cannot precede created_at")


@dataclass(frozen=True, slots=True)
class ModelVersion:
    """Opaque deployed model identifier, independent of any provider SDK."""

    value: str

    def __post_init__(self) -> None:
        _require_text("Model version", self.value)


@dataclass(frozen=True, slots=True)
class PromptVersion:
    """Opaque, rollback-capable version of a retrieval or Few-shot Prompt."""

    value: str

    def __post_init__(self) -> None:
        _require_text("Prompt version", self.value)


@dataclass(frozen=True, slots=True)
class RetrievalPolicyVersion:
    """Rollback-capable version for retrieval thresholds and Top-K policy."""

    value: str

    def __post_init__(self) -> None:
        _require_text("Retrieval policy version", self.value)


@dataclass(frozen=True, slots=True)
class ExampleEvidenceReference:
    """Durable references to evidence; never contains image bytes or Base64."""

    document_reference: str
    image_reference: str | None = None
    page_number: int | None = None
    evidence_source: str | None = None
    candidate_values: tuple[str, ...] = ()
    readability: str | None = None
    validation_signals: tuple[str, ...] = ()
    ambiguous: bool = False

    def __post_init__(self) -> None:
        _require_text("Document evidence reference", self.document_reference)
        if self.image_reference is not None:
            _require_text("Image evidence reference", self.image_reference)
        if self.page_number is not None and self.page_number <= 0:
            raise ValueError("Evidence page_number must be greater than zero")
        if self.evidence_source is not None:
            _require_text("Evidence source", self.evidence_source)
        if self.readability is not None:
            _require_text("Evidence readability", self.readability)
        if any(not item.strip() for item in self.candidate_values):
            raise ValueError("Evidence candidates must not contain blank values")
        if any(not item.strip() for item in self.validation_signals):
            raise ValueError("Evidence validation signals must not contain blank values")


@dataclass(frozen=True, slots=True)
class ExampleScope:
    """Base fact filters; admission approval remains a separate mandatory gate."""

    tenant_id: str
    document_type: str
    field_path: str
    schema_version: str
    catalog_version: str
    is_reviewed: Literal[True] = True
    is_valid: Literal[True] = True

    def __post_init__(self) -> None:
        _require_text("Tenant identifier", self.tenant_id)
        _require_text("Document type", self.document_type)
        _require_text("Field path", self.field_path)
        _require_text("Schema version", self.schema_version)
        _require_text("Catalog version", self.catalog_version)
        if self.is_reviewed is not True or self.is_valid is not True:
            raise ValueError("Retrieval scope must require reviewed and valid examples")


@dataclass(frozen=True, slots=True)
class ReviewedExample:
    """PostgreSQL review fact and admission candidate, not an approved memory by itself.

    ``is_reviewed`` records an explicit customer action. ``is_valid`` records source
    lifecycle only. Neither flag grants long-term retrieval eligibility.
    """

    example_id: str
    tenant_id: str
    source_feedback_id: str
    source_event_id: str | None
    document_id: str
    run_id: str
    document_type: str
    field_path: str
    schema_version: str
    catalog_version: str
    model_version: ModelVersion
    prompt_version: PromptVersion
    label_type: ExampleLabelType
    model_value: JsonValue
    reviewed_value: JsonValue
    correction_reason: str | None
    vendor_fingerprint: str | None
    template_fingerprint: str | None
    evidence_reference: ExampleEvidenceReference
    reviewer_id: str
    is_reviewed: bool
    is_valid: bool
    created_at: datetime
    fingerprint: str
    occurrence_count: int
    last_seen_at: datetime
    invalidated_reason: str | None = None
    invalidated_at: datetime | None = None

    def __post_init__(self) -> None:
        required = {
            "example_id": self.example_id,
            "tenant_id": self.tenant_id,
            "source_feedback_id": self.source_feedback_id,
            "document_id": self.document_id,
            "run_id": self.run_id,
            "document_type": self.document_type,
            "field_path": self.field_path,
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "reviewer_id": self.reviewer_id,
            "fingerprint": self.fingerprint,
        }
        for name, required_value in required.items():
            _require_text(name, required_value)
        for name, optional_value in (
            ("source_event_id", self.source_event_id),
            ("correction_reason", self.correction_reason),
            ("vendor_fingerprint", self.vendor_fingerprint),
            ("template_fingerprint", self.template_fingerprint),
        ):
            if optional_value is not None:
                _require_text(name, optional_value)
        if self.label_type is ExampleLabelType.CORRECTED:
            if self.source_event_id is None:
                raise ValueError("Corrected examples require a source correction event")
            if self.correction_reason is None:
                raise ValueError("Corrected examples require a correction reason")
            if self.model_value == self.reviewed_value:
                raise ValueError("Corrected examples must change the model value")
        elif self.source_event_id is not None:
            raise ValueError(
                "Confirmed correct/incorrect examples require an independent review source"
            )
        if self.label_type is ExampleLabelType.CONFIRMED_CORRECT:
            if self.model_value != self.reviewed_value:
                raise ValueError("Confirmed-correct values must match")
        if self.label_type is ExampleLabelType.CONFIRMED_INCORRECT:
            if self.correction_reason is None:
                raise ValueError("Confirmed-incorrect examples require a rejection reason")
            if self.reviewed_value is not None:
                raise ValueError("Confirmed-incorrect examples do not imply a correct value")
        if not self.is_reviewed:
            raise ValueError("ReviewedExample cannot represent an unreviewed model output")
        if self.created_at.tzinfo is None:
            raise ValueError("Reviewed example created_at must be timezone-aware")
        if self.occurrence_count <= 0:
            raise ValueError("Reviewed example occurrence_count must be greater than zero")
        if self.last_seen_at.tzinfo is None:
            raise ValueError("Reviewed example last_seen_at must be timezone-aware")
        if self.last_seen_at < self.created_at:
            raise ValueError("Reviewed example last_seen_at cannot precede created_at")
        if self.invalidated_reason is not None:
            _require_text("Reviewed example invalidated reason", self.invalidated_reason)
        if self.invalidated_at is not None and self.invalidated_at.tzinfo is None:
            raise ValueError("Reviewed example invalidated_at must be timezone-aware")
        if self.is_valid and (
            self.invalidated_reason is not None or self.invalidated_at is not None
        ):
            raise ValueError("Valid reviewed examples cannot contain invalidation metadata")
        if not self.is_valid and (
            self.invalidated_reason is None or self.invalidated_at is None
        ):
            raise ValueError("Invalid reviewed examples require reason and timestamp")

    @property
    def scope(self) -> ExampleScope:
        """Return its exact scope; this does not grant retrieval eligibility."""

        if not self.is_valid:
            raise ValueError("Invalid reviewed examples cannot produce a retrieval scope")
        return ExampleScope(
            tenant_id=self.tenant_id,
            document_type=self.document_type,
            field_path=self.field_path,
            schema_version=self.schema_version,
            catalog_version=self.catalog_version,
        )


@dataclass(frozen=True, slots=True)
class ExampleCandidate:
    """Redacted Milvus projection kept separate from raw reviewed values."""

    example_id: str
    scope: ExampleScope
    label_type: ExampleLabelType
    redacted_model_value: JsonValue
    redacted_reviewed_value: JsonValue
    redacted_correction_reason: str | None
    vendor_fingerprint: str | None
    template_fingerprint: str | None
    evidence_reference: ExampleEvidenceReference
    redacted_index_text: str
    redaction_policy_version: str
    index_version: IndexVersion
    last_seen_at: datetime

    def __post_init__(self) -> None:
        _require_text("Example identifier", self.example_id)
        _require_text("Redacted index text", self.redacted_index_text)
        _require_text("Redaction policy version", self.redaction_policy_version)
        if self.redacted_correction_reason is not None:
            _require_text("Redacted correction reason", self.redacted_correction_reason)
        if self.vendor_fingerprint is not None:
            _require_text("Vendor fingerprint", self.vendor_fingerprint)
        if self.template_fingerprint is not None:
            _require_text("Template fingerprint", self.template_fingerprint)
        if self.last_seen_at.tzinfo is None or self.last_seen_at.utcoffset() is None:
            raise ValueError("Example candidate last_seen_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExampleVectorProjection:
    """Derived index payload kept separate from the redacted case candidate."""

    candidate: ExampleCandidate
    dense_embedding: tuple[float, ...]
    sparse_embedding: SparseVector | None
    projection_checksum: str

    def __post_init__(self) -> None:
        _require_text("Projection checksum", self.projection_checksum)
        if not self.dense_embedding:
            raise ValueError("Dense embedding must not be empty")
        if any(not math.isfinite(value) for value in self.dense_embedding):
            raise ValueError("Dense embedding must contain finite values")
        if self.sparse_embedding is not None:
            previous_index = -1
            for token_index, weight in self.sparse_embedding:
                if token_index < 0:
                    raise ValueError("Sparse token indexes must not be negative")
                if token_index <= previous_index:
                    raise ValueError("Sparse token indexes must be strictly increasing")
                if not math.isfinite(weight):
                    raise ValueError("Sparse embedding weights must be finite")
                previous_index = token_index


@dataclass(frozen=True, slots=True)
class RetrievalScore:
    """Independent retrieval scores; none of these values is a probability."""

    dense: float | None
    sparse: float | None
    fusion: float | None
    rerank: float | None

    def __post_init__(self) -> None:
        values = (self.dense, self.sparse, self.fusion, self.rerank)
        if all(value is None for value in values):
            raise ValueError("At least one retrieval score must be present")
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("Retrieval scores must be finite")


@dataclass(frozen=True, slots=True)
class RetrievedExample:
    """One redacted candidate with channel-specific retrieval scores."""

    candidate: ExampleCandidate
    score: RetrievalScore
    rank: int
    recall_sources: tuple[RetrievalRecallSource, ...]

    def __post_init__(self) -> None:
        if self.rank <= 0:
            raise ValueError("Retrieved example rank must be greater than zero")
        if not self.recall_sources:
            raise ValueError("Retrieved examples must retain at least one recall source")
        if len(self.recall_sources) != len(set(self.recall_sources)):
            raise ValueError("Retrieved example recall sources must be unique")


@dataclass(frozen=True, slots=True)
class CandidateHypothesis:
    """Historical prior only; it is never an authorized final invoice value."""

    field_path: str
    historical_prior_value: JsonValue
    supporting_example_ids: tuple[str, ...]
    supporting_labels: tuple[ExampleLabelType, ...]
    reason_codes: tuple[str, ...]
    is_historical_prior: Literal[True] = field(default=True, init=False)
    may_override_current_evidence: Literal[False] = field(default=False, init=False)

    def __post_init__(self) -> None:
        _require_text("Hypothesis field path", self.field_path)
        if not self.supporting_example_ids:
            raise ValueError("Historical hypothesis requires supporting reviewed examples")
        if len(self.supporting_example_ids) != len(set(self.supporting_example_ids)):
            raise ValueError("Historical hypothesis example identifiers must be unique")
        if not self.supporting_labels:
            raise ValueError("Historical hypothesis requires supporting reviewed labels")
        if len(self.supporting_labels) != len(self.supporting_example_ids):
            raise ValueError("Historical hypothesis labels must align with example identifiers")
        for example_id in self.supporting_example_ids:
            _require_text("Supporting example identifier", example_id)
        for reason_code in self.reason_codes:
            _require_text("Hypothesis reason code", reason_code)


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    """Bounded, versioned Few-shot context safe for Prompt construction."""

    trace_id: str
    scope: ExampleScope
    examples: tuple[RetrievedExample, ...]
    hard_negatives: tuple[RetrievedExample, ...]
    hypotheses: tuple[CandidateHypothesis, ...]
    index_version: IndexVersion | None
    dense_model_version: ModelVersion
    sparse_model_version: ModelVersion | None
    rerank_model_version: ModelVersion | None
    prompt_version: PromptVersion
    retrieval_policy_version: RetrievalPolicyVersion
    retrieved_at: datetime

    def __post_init__(self) -> None:
        _require_text("Retrieval trace identifier", self.trace_id)
        all_examples = self.examples + self.hard_negatives
        example_ids = tuple(item.candidate.example_id for item in all_examples)
        if len(example_ids) != len(set(example_ids)):
            raise ValueError("Retrieval context cannot contain duplicate examples")
        if any(item.candidate.scope != self.scope for item in all_examples):
            raise ValueError("All retrieved examples must match the exact retrieval scope")
        if self.index_version is None and all_examples:
            raise ValueError("Non-empty retrieval context requires an active index version")
        if self.index_version is not None and any(
            item.candidate.index_version != self.index_version for item in all_examples
        ):
            raise ValueError("All retrieved examples must use the active index version")
        positive_labels = {
            ExampleLabelType.CONFIRMED_CORRECT,
            ExampleLabelType.CORRECTED,
        }
        if any(item.candidate.label_type not in positive_labels for item in self.examples):
            raise ValueError("Positive example region contains a hard negative")
        if any(
            item.candidate.label_type is not ExampleLabelType.CONFIRMED_INCORRECT
            for item in self.hard_negatives
        ):
            raise ValueError("Hard-negative region contains a positive example")
        if any(
            item.candidate.label_type is ExampleLabelType.CORRECTED
            and item.candidate.redacted_correction_reason is None
            for item in self.examples
        ):
            raise ValueError("Corrected retrieval examples require a correction reason")
        if any(
            ExampleLabelType.CONFIRMED_INCORRECT in hypothesis.supporting_labels
            for hypothesis in self.hypotheses
        ):
            raise ValueError("Hard negatives cannot produce positive candidate hypotheses")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("Retrieval context retrieved_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ReviewedExamplePromptReference:
    """Minimal redacted case reference allowed in checkpoint state and Vision prompts."""

    example_id: str
    document_type: str
    field_path: str
    schema_version: str
    label_type: ExampleLabelType
    model_value: JsonValue
    reviewed_value: JsonValue
    correction_reason: str | None
    index_version: IndexVersion

    def __post_init__(self) -> None:
        for name, value in (
            ("example_id", self.example_id),
            ("document_type", self.document_type),
            ("field_path", self.field_path),
            ("schema_version", self.schema_version),
        ):
            _require_text(name, value)
        if self.label_type is ExampleLabelType.CORRECTED:
            if self.correction_reason is None:
                raise ValueError("Corrected Prompt references require a correction reason")
        elif self.correction_reason is not None:
            _require_text("correction_reason", self.correction_reason)


@dataclass(frozen=True, slots=True)
class ReviewedExamplePromptContext:
    """Bounded Prompt projection; historical values remain non-authoritative priors."""

    trace_ids: tuple[str, ...]
    verified_correct_examples: tuple[ReviewedExamplePromptReference, ...]
    reviewed_correction_examples: tuple[ReviewedExamplePromptReference, ...]
    reviewed_negative_examples: tuple[ReviewedExamplePromptReference, ...]
    conflicting_field_paths: tuple[str, ...]
    retrieval_policy_version: RetrievalPolicyVersion

    def __post_init__(self) -> None:
        if len(self.trace_ids) != len(set(self.trace_ids)):
            raise ValueError("Prompt context trace identifiers must be unique")
        for trace_id in self.trace_ids:
            _require_text("Prompt context trace identifier", trace_id)
        regions = (
            (
                self.verified_correct_examples,
                ExampleLabelType.CONFIRMED_CORRECT,
            ),
            (self.reviewed_correction_examples, ExampleLabelType.CORRECTED),
            (
                self.reviewed_negative_examples,
                ExampleLabelType.CONFIRMED_INCORRECT,
            ),
        )
        references = tuple(item for items, _ in regions for item in items)
        identifiers = tuple(item.example_id for item in references)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Prompt context cannot contain duplicate example references")
        for items, required_label in regions:
            if any(item.label_type is not required_label for item in items):
                raise ValueError("Prompt example is stored in the wrong label region")
        if len(self.conflicting_field_paths) != len(set(self.conflicting_field_paths)):
            raise ValueError("Prompt conflict field paths must be unique")
        for field_path in self.conflicting_field_paths:
            _require_text("conflicting_field_path", field_path)

    @property
    def has_examples(self) -> bool:
        return bool(
            self.verified_correct_examples
            or self.reviewed_correction_examples
            or self.reviewed_negative_examples
        )


def _require_text(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
