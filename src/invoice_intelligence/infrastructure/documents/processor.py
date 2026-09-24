"""Pillow and PyMuPDF document validation and image conversion."""

import asyncio
import math
import warnings
from io import BytesIO
from pathlib import Path

import pymupdf
from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError

from invoice_intelligence.application.errors import (
    DocumentPageLimitError,
    DocumentTooLargeError,
    EmptyDocumentError,
    InvalidDocumentError,
    UnsupportedMediaTypeError,
)
from invoice_intelligence.domain.document import (
    DocumentProcessingLimits,
    InspectedDocument,
    UploadDocument,
    VisionImage,
)

_SUPPORTED_MIME_TYPES = frozenset(
    {"application/pdf", "image/jpeg", "image/png", "image/webp"}
)
_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
}
_MIME_BY_EXTENSION = {
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".webp": "image/webp",
}
_MIME_BY_PIL_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


class PillowMuPdfDocumentProcessor:
    """Validate supported inputs and produce bounded PNG vision images."""

    async def inspect(
        self,
        document: UploadDocument,
        limits: DocumentProcessingLimits,
    ) -> InspectedDocument:
        """Validate the original document without retaining decoded content."""

        return await asyncio.to_thread(self._inspect_sync, document, limits)

    async def to_vision_images(
        self,
        content: bytes,
        inspected: InspectedDocument,
        limits: DocumentProcessingLimits,
    ) -> tuple[VisionImage, ...]:
        """Normalize an image or render each allowed PDF page to PNG."""

        return await asyncio.to_thread(
            self._to_vision_images_sync,
            content,
            inspected,
            limits,
        )

    def _inspect_sync(
        self,
        document: UploadDocument,
        limits: DocumentProcessingLimits,
    ) -> InspectedDocument:
        content = document.content
        if not content:
            raise EmptyDocumentError("Uploaded document is empty")
        if len(content) > limits.max_upload_bytes:
            raise DocumentTooLargeError("Uploaded document exceeds the configured size limit")

        declared_mime = self._normalize_declared_mime(document.declared_mime_type)
        detected_mime = self._detect_mime(content)
        if detected_mime is None:
            raise UnsupportedMediaTypeError("File signature is not a supported image or PDF")
        if declared_mime != detected_mime:
            raise InvalidDocumentError("Declared MIME type does not match file content")

        self._validate_filename(document.filename, detected_mime)
        if detected_mime == "application/pdf":
            page_count = self._inspect_pdf(content, limits)
        else:
            self._inspect_image(content, detected_mime, limits)
            page_count = 1

        return InspectedDocument(
            mime_type=detected_mime,
            size_bytes=len(content),
            page_count=page_count,
        )

    @staticmethod
    def _normalize_declared_mime(value: str | None) -> str:
        if value is None:
            raise UnsupportedMediaTypeError("A supported Content-Type is required")
        normalized = value.partition(";")[0].strip().lower()
        normalized = _MIME_ALIASES.get(normalized, normalized)
        if normalized not in _SUPPORTED_MIME_TYPES:
            raise UnsupportedMediaTypeError("Declared MIME type is not supported")
        return normalized

    @staticmethod
    def _detect_mime(content: bytes) -> str | None:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return "image/webp"
        if b"%PDF-" in content[:1024]:
            return "application/pdf"
        return None

    @staticmethod
    def _validate_filename(filename: str | None, detected_mime: str) -> None:
        if not filename:
            return
        suffix = Path(filename).suffix.lower()
        if not suffix:
            return
        extension_mime = _MIME_BY_EXTENSION.get(suffix)
        if extension_mime is None:
            raise UnsupportedMediaTypeError("Filename extension is not supported")
        if extension_mime != detected_mime:
            raise InvalidDocumentError("Filename extension does not match file content")

    @staticmethod
    def _inspect_image(
        content: bytes,
        expected_mime: str,
        limits: DocumentProcessingLimits,
    ) -> None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as image:
                    actual_mime = _MIME_BY_PIL_FORMAT.get(image.format or "")
                    if actual_mime != expected_mime:
                        raise InvalidDocumentError(
                            "Image decoder format does not match file signature"
                        )
                    width, height = image.size
                    if width <= 0 or height <= 0:
                        raise InvalidDocumentError("Image dimensions are invalid")
                    if width * height > limits.max_image_pixels:
                        raise InvalidDocumentError("Image exceeds the configured pixel limit")
                    if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
                        raise InvalidDocumentError("Animated images are not supported")
                    image.verify()
        except InvalidDocumentError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise InvalidDocumentError("Image exceeds safe decoder limits") from exc
        except (OSError, SyntaxError, UnidentifiedImageError, ValueError) as exc:
            raise InvalidDocumentError("Image is corrupt or cannot be decoded") from exc

    @staticmethod
    def _inspect_pdf(content: bytes, limits: DocumentProcessingLimits) -> int:
        try:
            with pymupdf.open(stream=content, filetype="pdf") as document:
                if document.needs_pass or document.is_encrypted:
                    raise InvalidDocumentError("Encrypted PDFs are not supported")
                if document.is_repaired:
                    raise InvalidDocumentError("PDF is damaged and required decoder repair")
                page_count = document.page_count
                if page_count <= 0:
                    raise InvalidDocumentError("PDF contains no pages")
                if page_count > limits.max_pdf_pages:
                    raise DocumentPageLimitError("PDF exceeds the configured page limit")
                for page_number in range(page_count):
                    page = document.load_page(page_number)
                    rect = page.rect
                    if (
                        not math.isfinite(rect.width)
                        or not math.isfinite(rect.height)
                        or rect.width <= 0
                        or rect.height <= 0
                    ):
                        raise InvalidDocumentError("PDF contains an invalid page geometry")
                return page_count
        except (DocumentPageLimitError, InvalidDocumentError):
            raise
        except Exception as exc:
            raise InvalidDocumentError("PDF is corrupt or cannot be decoded") from exc

    def _to_vision_images_sync(
        self,
        content: bytes,
        inspected: InspectedDocument,
        limits: DocumentProcessingLimits,
    ) -> tuple[VisionImage, ...]:
        if inspected.mime_type == "application/pdf":
            return self._render_pdf(content, inspected.page_count, limits)
        return (self._normalize_image(content, limits),)

    @staticmethod
    def _normalize_image(
        content: bytes,
        limits: DocumentProcessingLimits,
    ) -> VisionImage:
        try:
            with Image.open(BytesIO(content)) as source:
                source.load()
                oriented = ImageOps.exif_transpose(source)
                if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
                    rgba = oriented.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    normalized = Image.alpha_composite(background, rgba).convert("RGB")
                else:
                    normalized = oriented.convert("RGB")
                if limits.image_preprocessing_enabled:
                    normalized = PillowMuPdfDocumentProcessor._enhance_image(
                        normalized,
                        limits,
                    )
                normalized.thumbnail(
                    (limits.max_image_dimension, limits.max_image_dimension),
                    Image.Resampling.LANCZOS,
                )
                output = BytesIO()
                normalized.save(output, format="PNG", optimize=True)
                width, height = normalized.size
        except (OSError, SyntaxError, UnidentifiedImageError, ValueError) as exc:
            raise InvalidDocumentError("Image became unreadable during normalization") from exc

        return VisionImage(
            content=output.getvalue(),
            mime_type="image/png",
            page_number=1,
            width=width,
            height=height,
        )

    @staticmethod
    def _enhance_image(image: Image.Image, limits: DocumentProcessingLimits) -> Image.Image:
        """Apply bounded, deterministic enhancement without removing color evidence."""

        grayscale = ImageOps.grayscale(image)
        extrema = grayscale.getextrema()
        dynamic_range = extrema[1] - extrema[0]
        enhanced = image
        if dynamic_range < 180:
            enhanced = ImageOps.autocontrast(enhanced, cutoff=1)
        enhanced = ImageEnhance.Contrast(enhanced).enhance(
            limits.image_preprocessing_contrast
        )
        enhanced = ImageEnhance.Sharpness(enhanced).enhance(
            limits.image_preprocessing_sharpness
        )
        return enhanced

    @staticmethod
    def _render_pdf(
        content: bytes,
        expected_page_count: int,
        limits: DocumentProcessingLimits,
    ) -> tuple[VisionImage, ...]:
        rendered: list[VisionImage] = []
        total_rendered_pixels = 0
        try:
            with pymupdf.open(stream=content, filetype="pdf") as document:
                if document.page_count != expected_page_count:
                    raise InvalidDocumentError("PDF page count changed during processing")
                for index in range(expected_page_count):
                    page = document.load_page(index)
                    rect = page.rect
                    area = rect.width * rect.height
                    scale = min(
                        limits.pdf_render_dpi / 72.0,
                        limits.max_image_dimension / rect.width,
                        limits.max_image_dimension / rect.height,
                        math.sqrt(limits.max_image_pixels / area),
                    ) * 0.999
                    if not math.isfinite(scale) or scale <= 0:
                        raise InvalidDocumentError("PDF page cannot be rendered within limits")
                    estimated_pixels = max(1, math.ceil(rect.width * scale)) * max(
                        1,
                        math.ceil(rect.height * scale),
                    )
                    if (
                        total_rendered_pixels + estimated_pixels
                        > limits.max_total_rendered_pixels
                    ):
                        raise InvalidDocumentError(
                            "PDF exceeds the configured cumulative render pixel limit"
                        )
                    pixmap = page.get_pixmap(
                        matrix=pymupdf.Matrix(scale, scale),
                        colorspace=pymupdf.csRGB,
                        alpha=False,
                    )
                    if (
                        pixmap.width > limits.max_image_dimension
                        or pixmap.height > limits.max_image_dimension
                        or pixmap.width * pixmap.height > limits.max_image_pixels
                    ):
                        raise InvalidDocumentError(
                            "Rendered PDF page exceeds configured image limits"
                        )
                    total_rendered_pixels += pixmap.width * pixmap.height
                    if total_rendered_pixels > limits.max_total_rendered_pixels:
                        raise InvalidDocumentError(
                            "PDF exceeds the configured cumulative render pixel limit"
                        )
                    rendered_image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
                    if limits.image_preprocessing_enabled:
                        rendered_image = PillowMuPdfDocumentProcessor._enhance_image(
                            rendered_image,
                            limits,
                        )
                    output = BytesIO()
                    rendered_image.save(output, format="PNG", optimize=True)
                    rendered.append(
                        VisionImage(
                            content=output.getvalue(),
                            mime_type="image/png",
                            page_number=index + 1,
                            width=pixmap.width,
                            height=pixmap.height,
                        )
                    )
        except InvalidDocumentError:
            raise
        except Exception as exc:
            raise InvalidDocumentError("PDF failed during page rendering") from exc
        return tuple(rendered)
