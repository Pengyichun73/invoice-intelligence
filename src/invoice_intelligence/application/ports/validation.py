"""Independent image-quality and optional OCR validation boundaries."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.extraction import (
    OCRFieldObservation,
    PageQuality,
    RawOCRResult,
)
from invoice_intelligence.domain.workflow import JsonValue

InvoiceT = TypeVar("InvoiceT")


@dataclass(frozen=True, slots=True)
class SchemaInspection:
    """Framework-neutral view of a structured invoice Schema validation."""

    python_values: Mapping[str, object]
    json_values: Mapping[str, JsonValue]
    valid: bool


class InvoiceSchemaInspector(Protocol):
    """Validate and flatten a concrete business Schema behind an abstraction."""

    def inspect(self, invoice: object | None) -> SchemaInspection:
        """Return typed values, JSON values, and the Schema validation outcome."""

        ...


class ImageQualityAnalyzer(Protocol):
    """Measure page quality without using a model-provided confidence value."""

    async def analyze(self, images: Sequence[VisionImage]) -> tuple[PageQuality, ...]:
        """Return deterministic clarity and resolution observations per page."""

        ...


class OCRValidationProvider(Protocol):
    """Optional independent OCR adapter used only for cross-validation."""

    async def observe(
        self,
        images: Sequence[VisionImage],
        output_schema: type[InvoiceT],
    ) -> tuple[OCRFieldObservation, ...]:
        """Return field candidates without mutating or replacing Vision output."""

        ...


class RawOCRProvider(Protocol):
    """Optional provider returning unbound OCR text-line observations."""

    async def observe_raw(
        self,
        images: Sequence[VisionImage],
    ) -> RawOCRResult:
        """Return bounded raw observations without assigning canonical fields."""

        ...
