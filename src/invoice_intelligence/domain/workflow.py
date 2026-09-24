"""Framework-independent workflow, review, and correction value objects."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from invoice_intelligence.domain.field_semantics import (
    FieldBindingEvidence,
    FieldBindingReviewDecision,
)
from invoice_intelligence.domain.json_types import JsonValue


class WorkflowStatus(StrEnum):
    """Persisted deterministic workflow lifecycle."""

    RECEIVED = "received"
    PROCESSING = "processing"
    PENDING_REVIEW = "pending_review"
    COMPLETED = "completed"
    FAILED = "failed"


class ValidationRoute(StrEnum):
    """Only allowed deterministic validation-routing outcomes."""

    ACCEPTED = "accepted"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"


class SignalVerdict(StrEnum):
    """Outcome of one independently observable validation rule."""

    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


class HumanReviewAction(StrEnum):
    """Explicit human actions; absence of a change is never an action."""

    CONFIRM_CORRECT = "confirm_correct"
    CORRECT = "correct"
    CONFIRM_INCORRECT = "confirm_incorrect"


@dataclass(frozen=True, slots=True)
class WorkflowIdentity:
    """Keep checkpoint, execution, and business-document identities distinct."""

    thread_id: str
    run_id: str
    document_id: str

    def __post_init__(self) -> None:
        values = (self.thread_id.strip(), self.run_id.strip(), self.document_id.strip())
        if any(not value for value in values):
            raise ValueError("thread_id, run_id, and document_id must not be empty")
        if len(set(values)) != 3:
            raise ValueError("thread_id, run_id, and document_id must be different identifiers")


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """Deterministic reason that an extraction requires review."""

    code: str
    message: str
    field_path: str | None


@dataclass(frozen=True, slots=True)
class ValidationSignal:
    """One field-level signal; never represents model self-reported confidence."""

    field_path: str | None
    rule: str
    verdict: SignalVerdict
    score: float
    message: str
    observed_value: JsonValue
    expected: str | None
    failure_route: ValidationRoute

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("Validation signal score must be between zero and one")


@dataclass(frozen=True, slots=True)
class FieldDecision:
    """Deterministic decision composed from all available signals for one field."""

    field_path: str | None
    current_value: JsonValue
    candidate_values: tuple[str, ...]
    signals: tuple[ValidationSignal, ...]
    score: float
    route: ValidationRoute
    user_action: str | None

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("Field decision score must be between zero and one")


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """Result of Python-controlled extraction validation."""

    route: ValidationRoute
    field_decisions: tuple[FieldDecision, ...]
    issues: tuple[ValidationIssue, ...]

    @property
    def requires_review(self) -> bool:
        return self.route is ValidationRoute.REVIEW_REQUIRED

    @property
    def rejected(self) -> bool:
        return self.route is ValidationRoute.REJECTED


@dataclass(frozen=True, slots=True)
class ReviewField:
    """One field and the deterministic reasons requiring human confirmation."""

    field_path: str | None
    current_value: JsonValue
    candidate_values: tuple[str, ...]
    triggered_rules: tuple[str, ...]
    reasons: tuple[str, ...]
    user_action: str


@dataclass(frozen=True, slots=True)
class ReviewEvidenceSummary:
    """Bounded technical evidence shown to a reviewer without raw OCR lines."""

    field_path: str
    source_type: str
    source_id: str
    candidate_values: tuple[str, ...]
    page_number: int | None
    bounding_box: tuple[float, float, float, float] | None
    provider_name: str | None
    provider_version: str | None
    model_version: str | None
    provider_score: float | None
    source_reference: str | None
    comparison_outcome: str | None
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("field_path", self.field_path),
            ("source_type", self.source_type),
            ("source_id", self.source_id),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        if self.page_number is not None and self.page_number <= 0:
            raise ValueError("Review evidence page_number must be positive")
        if self.bounding_box is not None:
            left, top, right, bottom = self.bounding_box
            if any(not math.isfinite(item) for item in self.bounding_box):
                raise ValueError("Review evidence bounding_box must be finite")
            if left < 0 or top < 0 or right < left or bottom < top:
                raise ValueError("Review evidence bounding_box is invalid")
        if self.provider_score is not None and (
            not math.isfinite(self.provider_score)
            or not 0.0 <= self.provider_score <= 1.0
        ):
            raise ValueError("Review evidence provider_score is an invalid uncalibrated score")
        if self.source_reference is not None:
            if (
                not self.source_reference.strip()
                or self.source_reference != self.source_reference.strip()
            ):
                raise ValueError(
                    "Review evidence source_reference must be non-empty and normalized"
                )
            lowered_reference = self.source_reference.lower()
            if lowered_reference.startswith("data:") or "base64," in lowered_reference:
                raise ValueError("Review evidence source_reference must not contain Base64 data")
        for values, label in (
            (self.candidate_values, "candidate values"),
            (self.reason_codes, "reason codes"),
        ):
            if any(not item.strip() or item != item.strip() for item in values):
                raise ValueError(f"Review evidence {label} must be normalized")


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """Serializable content needed for a human review decision."""

    fields: tuple[ReviewField, ...]
    field_bindings: tuple[FieldBindingEvidence, ...] = ()
    evidence_sources: tuple[ReviewEvidenceSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class FieldReviewDecision:
    """One explicit, attributable field-level human review action."""

    field_path: str
    action: HumanReviewAction
    reason: str | None
    rejected_value: JsonValue

    def __post_init__(self) -> None:
        if not self.field_path.strip():
            raise ValueError("Review decision field_path must not be empty")
        if self.action in {
            HumanReviewAction.CORRECT,
            HumanReviewAction.CONFIRM_INCORRECT,
        } and (self.reason is None or not self.reason.strip()):
            raise ValueError("Corrected and rejected decisions require a reason")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("Review decision reason must not be blank")


@dataclass(frozen=True, slots=True)
class HumanCorrection:
    """Explicit human review submission carried by ``Command(resume=...)``."""

    corrected_invoice: Mapping[str, JsonValue] | None
    fields: tuple[FieldReviewDecision, ...]
    reviewer_id: str
    document_type: str | None
    field_bindings: tuple[FieldBindingReviewDecision, ...] = ()

    def __post_init__(self) -> None:
        if not self.reviewer_id.strip():
            raise ValueError("reviewer_id must not be empty")
        if self.document_type is not None and not self.document_type.strip():
            raise ValueError("document_type must not be blank")
        if not self.fields and not self.field_bindings:
            raise ValueError("Human review must include at least one explicit decision")
        paths = tuple(item.field_path for item in self.fields)
        if len(paths) != len(set(paths)):
            raise ValueError("Human review field paths must be unique")
        evidence_ids = tuple(item.evidence_id for item in self.field_bindings)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("Human review binding evidence IDs must be unique")
        if any(
            item.action is HumanReviewAction.CORRECT for item in self.fields
        ) and self.corrected_invoice is None:
            raise ValueError("Correct actions require a complete corrected invoice")
        if not any(
            item.action is HumanReviewAction.CORRECT for item in self.fields
        ) and self.corrected_invoice is not None:
            raise ValueError("corrected_invoice is only allowed for explicit correct actions")


@dataclass(frozen=True, slots=True)
class CorrectionEvent:
    """Persistable correction audit and memory entry."""

    document_type: str
    field_path: str
    model_value: JsonValue
    corrected_value: JsonValue
    correction_reason: str
    vendor_features: Mapping[str, JsonValue]
    template_features: Mapping[str, JsonValue]
    document_reference: str
    image_reference: str | None
    schema_version: str
    created_at: datetime
    is_reviewed: bool
    is_valid: bool

    def __post_init__(self) -> None:
        required = (
            self.document_type,
            self.field_path,
            self.correction_reason,
            self.document_reference,
            self.schema_version,
        )
        if any(not value.strip() for value in required):
            raise ValueError("Correction event identifiers and reason must not be empty")
        if self.created_at.tzinfo is None:
            raise ValueError("Correction event created_at must be timezone-aware")
