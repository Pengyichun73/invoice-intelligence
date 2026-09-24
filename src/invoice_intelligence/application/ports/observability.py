"""Sensitive-value-free observability contracts."""

import math
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class TraceStage(StrEnum):
    """Stable, low-cardinality stages for privacy-safe operational traces."""

    INGESTION = "ingestion"
    DOCUMENT_PREPROCESSING = "document_preprocessing"
    VISION = "vision"
    OCR = "ocr"
    RETRIEVAL = "retrieval"
    VALIDATION = "validation"
    HUMAN_REVIEW = "human_review"
    MEMORY_ADMISSION = "memory_admission"
    FIELD_SEMANTIC_BINDING = "field_semantic_binding"
    INDEX_PROJECTION = "index_projection"
    GOVERNANCE_OPERATION = "governance_operation"
    BACKGROUND_RECOVERY = "background_recovery"
    CODE_HARNESS = "code_harness"


SafeTraceValue = str | int | float | bool | None


class PrivacyTelemetry(Protocol):
    """Record metadata-only spans without receiving business or model content."""

    def span(
        self,
        *,
        trace_id: str,
        tenant_id: str,
        stage: TraceStage,
        operation: str,
        attributes: Mapping[str, SafeTraceValue] | None = None,
    ) -> AbstractContextManager[None]:
        """Bind one stage span to the current async context."""

        ...


@dataclass(frozen=True, slots=True)
class OCRPageMetric:
    """One OCR page call without image or recognized text content."""

    page_number: int
    latency_ms: float
    status_code: int | None
    outcome: str
    text_box_count: int

    def __post_init__(self) -> None:
        if self.page_number <= 0:
            raise ValueError("OCR metric page_number must be positive")
        if not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ValueError("OCR metric latency must be finite and non-negative")
        if self.status_code is not None and not 100 <= self.status_code <= 599:
            raise ValueError("OCR metric status_code is invalid")
        if not self.outcome.strip() or self.outcome != self.outcome.strip():
            raise ValueError("OCR metric outcome must be non-empty and normalized")
        if self.text_box_count < 0:
            raise ValueError("OCR metric text_box_count must be non-negative")


@dataclass(frozen=True, slots=True)
class OCRProviderCallMetric:
    """One provider batch call with version labels and bounded page metrics."""

    trace_id: str
    provider_name: str
    provider_version: str
    model_version: str
    config_version: str
    page_count: int
    latency_ms: float
    pages: tuple[OCRPageMetric, ...]
    error_type: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("trace_id", self.trace_id),
            ("provider_name", self.provider_name),
            ("provider_version", self.provider_version),
            ("model_version", self.model_version),
            ("config_version", self.config_version),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"OCR metric {name} must be non-empty and normalized")
        if self.page_count < 0 or len(self.pages) > self.page_count:
            raise ValueError("OCR metric pages must not exceed page_count")
        if not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ValueError("OCR metric latency must be finite and non-negative")
        if self.error_type is not None and (
            not self.error_type.strip() or self.error_type != self.error_type.strip()
        ):
            raise ValueError("OCR metric error_type must be normalized when provided")


@dataclass(frozen=True, slots=True)
class OCRComparisonMetric:
    """Deterministic field binding and comparison counts for one extraction."""

    trace_ids: tuple[str, ...]
    raw_observation_count: int
    bound_field_count: int
    corroborated_count: int
    conflicting_count: int
    ocr_only_count: int
    vision_only_count: int
    unresolved_count: int
    unavailable_count: int
    conflict_review_required: bool

    def __post_init__(self) -> None:
        if any(not item.strip() or item != item.strip() for item in self.trace_ids):
            raise ValueError("OCR comparison trace IDs must be non-empty and normalized")
        counts = (
            self.raw_observation_count,
            self.bound_field_count,
            self.corroborated_count,
            self.conflicting_count,
            self.ocr_only_count,
            self.vision_only_count,
            self.unresolved_count,
            self.unavailable_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("OCR comparison metric counts must be non-negative")


class OCRTelemetry(Protocol):
    """Record OCR metrics without receiving invoice values or image content."""

    def record_provider_call(self, metric: OCRProviderCallMetric) -> None:
        """Record provider latency, outcomes, page box counts, and versions."""

        ...

    def record_comparison(self, metric: OCRComparisonMetric) -> None:
        """Record binding and deterministic comparison outcomes."""

        ...


@dataclass(frozen=True, slots=True)
class OCRProviderVersionSummary:
    provider_name: str
    provider_version: str
    model_version: str
    config_version: str
    call_count: int


@dataclass(frozen=True, slots=True)
class OCRMetricsSummary:
    provider_call_count: int
    page_call_count: int
    total_latency_ms: float
    average_call_latency_ms: float
    average_page_latency_ms: float
    success_count: int
    timeout_count: int
    circuit_open_count: int
    schema_error_count: int
    other_error_count: int
    text_box_count: int
    bound_field_count: int
    corroborated_count: int
    conflicting_count: int
    ocr_only_count: int
    vision_only_count: int
    unresolved_count: int
    unavailable_count: int
    empty_ocr_rate: float | None
    conflict_review_required_rate: float | None
    providers: tuple[OCRProviderVersionSummary, ...]


class OCRMetricsRepository(OCRTelemetry, Protocol):
    """Persist and aggregate sensitive-value-free OCR operational metrics."""

    async def summarize(self) -> OCRMetricsSummary:
        ...
