"""Framework-independent structured extraction result metadata."""

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

from invoice_intelligence.domain.examples import ReviewedExamplePromptContext
from invoice_intelligence.domain.field_semantics import (
    FieldBindingEvidence,
    FieldBindingStatus,
    FieldSemanticPromptCatalog,
)
from invoice_intelligence.domain.workflow import CorrectionEvent


class EvidenceSource(StrEnum):
    """Direct source from which a field candidate was observed."""

    VISUAL = "visual"
    HUMAN_CORRECTION = "human_correction"


class Readability(StrEnum):
    """Observed legibility of a field without inferred confidence."""

    READABLE = "readable"
    PARTIALLY_READABLE = "partially_readable"
    UNREADABLE = "unreadable"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class FieldEvidence:
    """Field-level observations kept separate from invoice business data."""

    field_path: str
    source: EvidenceSource
    page_number: int | None
    candidate_values: tuple[str, ...]
    readability: Readability
    validation_signals: tuple[str, ...]
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class PromptContextBudget:
    """Deterministic limits for historical and semantic Prompt context."""

    max_examples_total: int = 12
    max_examples_per_region: int = 6
    max_correction_events: int = 6
    max_catalog_definitions: int = 64
    max_section_chars: int = 12_000
    max_total_chars: int = 32_000

    def __post_init__(self) -> None:
        for name, value in (
            ("max_examples_total", self.max_examples_total),
            ("max_examples_per_region", self.max_examples_per_region),
            ("max_correction_events", self.max_correction_events),
            ("max_catalog_definitions", self.max_catalog_definitions),
            ("max_section_chars", self.max_section_chars),
            ("max_total_chars", self.max_total_chars),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.max_total_chars < self.max_section_chars:
            raise ValueError("max_total_chars must cover one section")


@dataclass(frozen=True, slots=True)
class VisionPromptContext:
    """Historical Prompt inputs kept separate from current-image business data."""

    correction_events: tuple[CorrectionEvent, ...] = ()
    reviewed_examples: ReviewedExamplePromptContext | None = None
    field_semantic_catalog: FieldSemanticPromptCatalog | None = None
    budget: PromptContextBudget = PromptContextBudget()

    @property
    def has_history(self) -> bool:
        return bool(
            self.correction_events
            or (
                self.reviewed_examples is not None
                and self.reviewed_examples.has_examples
            )
        )


@dataclass(frozen=True, slots=True)
class ExtractionAnomaly:
    """A non-secret extraction issue safe to persist or expose."""

    code: str
    message: str
    field_path: str | None
    page_number: int | None


@dataclass(frozen=True, slots=True)
class PageQuality:
    """Deterministic image-quality observations, separate from business data."""

    page_number: int
    width: int
    height: int
    clarity_score: float

    def __post_init__(self) -> None:
        if self.page_number <= 0:
            raise ValueError("page_number must be greater than zero")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("page dimensions must be greater than zero")
        if not 0.0 <= self.clarity_score <= 1.0:
            raise ValueError("clarity_score must be between zero and one")


BoundingBox = tuple[float, float, float, float]


class OCRComparisonOutcome(StrEnum):
    """Deterministic relationship between Vision and OCR evidence."""

    CORROBORATED = "corroborated"
    VISION_ONLY = "vision_only"
    OCR_ONLY = "ocr_only"
    UNRESOLVED = "unresolved"
    UNAVAILABLE = "unavailable"
    # Retained for backward-compatible checkpoint deserialization.
    CONSISTENT = "consistent"
    CONFLICTING = "conflicting"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class OCRProviderStatus(StrEnum):
    """Availability of an independent OCR provider for one observation batch."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RawOCRObservation:
    """One provider text-line observation before canonical field binding."""

    source_id: str
    provider_name: str
    provider_version: str
    model_version: str
    page_number: int
    observed_text: str
    normalized_text: str
    bounding_box: BoundingBox | None
    provider_score: float | None
    source_reference: str | None = None
    anomalies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("source_id", self.source_id),
            ("provider_name", self.provider_name),
            ("provider_version", self.provider_version),
            ("model_version", self.model_version),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        if self.page_number <= 0:
            raise ValueError("page_number must be greater than zero")
        if not isinstance(self.observed_text, str) or not isinstance(
            self.normalized_text, str
        ):
            raise TypeError("OCR text observations must be strings")
        _validate_bounding_box(self.bounding_box)
        _validate_provider_score(self.provider_score)
        _validate_source_reference(self.source_reference)
        if any(not item.strip() or item != item.strip() for item in self.anomalies):
            raise ValueError("OCR anomaly codes must be non-empty and normalized")


@dataclass(frozen=True, slots=True)
class RawOCRResult:
    """Bounded raw OCR observations plus an explicit provider availability status."""

    status: OCRProviderStatus
    observations: tuple[RawOCRObservation, ...] = ()
    anomalies: tuple[str, ...] = ()
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if self.trace_id is not None and not self.trace_id.strip():
            raise ValueError("trace_id must be non-empty when provided")
        if any(not item.strip() or item != item.strip() for item in self.anomalies):
            raise ValueError("OCR anomaly codes must be non-empty and normalized")
        if self.status is OCRProviderStatus.AVAILABLE and self.anomalies:
            raise ValueError("Available OCR results cannot contain provider anomalies")


@dataclass(frozen=True, slots=True)
class OCRFieldObservation:
    """OCR candidates bound to a canonical field, kept outside InvoiceExtraction."""

    field_path: str
    page_number: int | None
    candidate_values: tuple[str, ...]
    source_id: str = "legacy"
    provider_name: str = "unknown"
    provider_version: str = "unknown"
    model_version: str = "unknown"
    observed_text: str = ""
    normalized_text: str = ""
    bounding_box: BoundingBox | None = None
    provider_score: float | None = None
    source_reference: str | None = None
    anomalies: tuple[str, ...] = ()
    candidate_field_paths: tuple[str, ...] = ()
    binding_status: FieldBindingStatus = FieldBindingStatus.ACCEPTED
    binding_reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.field_path.strip():
            raise ValueError("field_path must be non-empty")
        if self.page_number is not None and self.page_number <= 0:
            raise ValueError("page_number must be greater than zero when provided")
        for name, value in (
            ("source_id", self.source_id),
            ("provider_name", self.provider_name),
            ("provider_version", self.provider_version),
            ("model_version", self.model_version),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        if not isinstance(self.binding_status, FieldBindingStatus):
            raise TypeError("binding_status must use FieldBindingStatus")
        _validate_bounding_box(self.bounding_box)
        _validate_provider_score(self.provider_score)
        _validate_source_reference(self.source_reference)
        for values, label in (
            (self.candidate_field_paths, "candidate field paths"),
            (self.anomalies, "OCR anomaly codes"),
            (self.binding_reason_codes, "binding reason codes"),
        ):
            if any(not item.strip() or item != item.strip() for item in values):
                raise ValueError(f"{label} must be non-empty and normalized")


@dataclass(frozen=True, slots=True)
class OCRVisionComparison:
    """Deterministic comparison summary for one canonical field."""

    canonical_field_path: str
    vision_candidates: tuple[str, ...]
    ocr_candidates: tuple[str, ...]
    supporting_sources: tuple[str, ...]
    conflicting_sources: tuple[str, ...]
    outcome: OCRComparisonOutcome
    reason_codes: tuple[str, ...]
    review_required: bool

    def __post_init__(self) -> None:
        if not self.canonical_field_path.strip():
            raise ValueError("canonical_field_path must be non-empty")
        for values, label in (
            (self.supporting_sources, "supporting sources"),
            (self.conflicting_sources, "conflicting sources"),
            (self.reason_codes, "reason codes"),
        ):
            if any(not item.strip() or item != item.strip() for item in values):
                raise ValueError(f"{label} must be non-empty and normalized")
        review_outcomes = {
            OCRComparisonOutcome.CONFLICTING,
            OCRComparisonOutcome.OCR_ONLY,
            OCRComparisonOutcome.UNRESOLVED,
        }
        if self.outcome in review_outcomes and not self.review_required:
            raise ValueError(f"{self.outcome.value} OCR/Vision evidence requires review")
        if self.outcome is OCRComparisonOutcome.UNAVAILABLE and self.review_required:
            raise ValueError("Unavailable OCR must degrade without requiring review")


def _validate_bounding_box(value: BoundingBox | None) -> None:
    if value is None:
        return
    if len(value) != 4 or any(not math.isfinite(item) for item in value):
        raise ValueError("bounding_box must contain four finite coordinates")
    left, top, right, bottom = value
    if left < 0 or top < 0 or right < left or bottom < top:
        raise ValueError("bounding_box coordinates must be ordered and non-negative")


def _validate_provider_score(value: float | None) -> None:
    if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0):
        raise ValueError("provider_score must be a finite uncalibrated score from zero to one")


def _validate_source_reference(value: str | None) -> None:
    if value is None:
        return
    if not value.strip() or value != value.strip():
        raise ValueError("source_reference must be non-empty and normalized when provided")
    lowered = value.lower()
    if lowered.startswith("data:") or "base64," in lowered:
        raise ValueError("source_reference must not contain Base64 data")


InvoiceT = TypeVar("InvoiceT")


@dataclass(frozen=True, slots=True)
class ExtractionResult(Generic[InvoiceT]):
    """Business extraction plus evidence; never contains model reasoning traces."""

    invoice: InvoiceT | None
    field_evidence: tuple[FieldEvidence, ...]
    anomalies: tuple[ExtractionAnomaly, ...]
    page_quality: tuple[PageQuality, ...] = ()
    raw_ocr_observations: tuple[RawOCRObservation, ...] = ()
    ocr_observations: tuple[OCRFieldObservation, ...] = ()
    ocr_comparisons: tuple[OCRVisionComparison, ...] = ()
    field_binding_evidence: tuple[FieldBindingEvidence, ...] = ()
