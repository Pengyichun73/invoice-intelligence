"""Value-blind invoice boundary detection boundary."""

from typing import Protocol

from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.invoice_batch import InvoiceGroup


class InvoiceSegmentationProvider(Protocol):
    async def detect(self, images: tuple[VisionImage, ...]) -> tuple[tuple[InvoiceGroup, ...], bool]:
        """Return proposed invoice groups and whether boundaries need confirmation."""
