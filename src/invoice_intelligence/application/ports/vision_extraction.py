"""Vision extraction provider boundary."""

from typing import Protocol, Sequence, TypeVar

from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.extraction import ExtractionResult, VisionPromptContext

InvoiceT = TypeVar("InvoiceT")


class VisionExtractionProvider(Protocol):
    """Extract a caller-supplied immutable business schema from images."""

    async def extract(
        self,
        images: Sequence[VisionImage],
        output_schema: type[InvoiceT],
        prompt_context: VisionPromptContext,
        document_id: str,
    ) -> ExtractionResult[InvoiceT]:
        """Return structured business data, direct evidence, and anomalies."""

        ...
