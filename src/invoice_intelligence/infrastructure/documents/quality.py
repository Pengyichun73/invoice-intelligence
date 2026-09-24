"""Pillow-based deterministic image-quality analysis."""

import asyncio
import math
from collections.abc import Sequence
from io import BytesIO

from PIL import Image, ImageFilter, ImageStat, UnidentifiedImageError

from invoice_intelligence.application.errors import InvalidDocumentError
from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.extraction import PageQuality


class PillowImageQualityAnalyzer:
    """Estimate edge clarity while retaining exact normalized image dimensions."""

    async def analyze(self, images: Sequence[VisionImage]) -> tuple[PageQuality, ...]:
        return await asyncio.to_thread(self._analyze_sync, images)

    @classmethod
    def _analyze_sync(cls, images: Sequence[VisionImage]) -> tuple[PageQuality, ...]:
        quality: list[PageQuality] = []
        for image in images:
            try:
                with Image.open(BytesIO(image.content)) as source:
                    grayscale = source.convert("L")
                    grayscale.thumbnail((512, 512), Image.Resampling.BILINEAR)
                    edges = grayscale.filter(ImageFilter.FIND_EDGES)
                    edge_variance = ImageStat.Stat(edges).var[0]
            except (OSError, SyntaxError, UnidentifiedImageError, ValueError) as exc:
                raise InvalidDocumentError(
                    "Normalized image cannot be analyzed for quality"
                ) from exc

            clarity_score = min(1.0, math.sqrt(max(0.0, edge_variance)) / 64.0)
            quality.append(
                PageQuality(
                    page_number=image.page_number,
                    width=image.width,
                    height=image.height,
                    clarity_score=clarity_score,
                )
            )
        return tuple(quality)
