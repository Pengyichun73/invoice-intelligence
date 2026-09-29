"""Document inspection and vision-image conversion boundary."""

from typing import Protocol

from invoice_intelligence.domain.document import (
    DocumentProcessingLimits,
    InspectedDocument,
    UploadDocument,
    VisionImage,
)
from invoice_intelligence.domain.invoice_batch import CroppedInvoice, InvoiceRegion


class DocumentProcessor(Protocol):
    """Validate documents and create bounded transient vision inputs."""

    async def inspect(
        self,
        document: UploadDocument,
        limits: DocumentProcessingLimits,
    ) -> InspectedDocument:
        """Validate content, media type, integrity, dimensions, and page count."""

        ...

    async def to_vision_images(
        self,
        content: bytes,
        inspected: InspectedDocument,
        limits: DocumentProcessingLimits,
    ) -> tuple[VisionImage, ...]:
        """Normalize an image or render PDF pages for a vision provider."""

        ...

    async def crop_invoice(
        self, images: tuple[VisionImage, ...], regions: tuple[InvoiceRegion, ...],
    ) -> tuple[bytes, str, str]:
        """Create a child document from bounded regions of the current source."""

        ...

    async def crop_invoice_from_source(
        self, content: bytes, inspected: InspectedDocument,
        regions: tuple[InvoiceRegion, ...], limits: DocumentProcessingLimits,
        min_width: int, min_height: int, min_clarity: float,
    ) -> CroppedInvoice:
        """Crop source pixels or PDF clips with bounded, quality-driven rendering."""
        ...
