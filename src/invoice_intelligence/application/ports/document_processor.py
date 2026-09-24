"""Document inspection and vision-image conversion boundary."""

from typing import Protocol

from invoice_intelligence.domain.document import (
    DocumentProcessingLimits,
    InspectedDocument,
    UploadDocument,
    VisionImage,
)


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
