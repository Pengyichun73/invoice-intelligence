"""Framework-independent document ingestion value objects."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DocumentReference:
    """Durable document identity safe to place in workflow state."""

    document_id: str
    storage_uri: str
    mime_type: str
    checksum: str
    size_bytes: int | None = None
    storage_status: str = "available"
    retention_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class UploadDocument:
    """Transient upload content passed into the ingestion use case."""

    filename: str | None
    declared_mime_type: str | None
    content: bytes


@dataclass(frozen=True, slots=True)
class InspectedDocument:
    """Validated document metadata derived from file content."""

    mime_type: str
    size_bytes: int
    page_count: int


@dataclass(frozen=True, slots=True)
class VisionImage:
    """Transient normalized image supplied to a vision provider."""

    content: bytes
    mime_type: str
    page_number: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class DocumentProcessingLimits:
    """Safety and rendering limits shared by document processors."""

    max_upload_bytes: int
    max_pdf_pages: int
    pdf_render_dpi: int
    max_image_dimension: int
    max_image_pixels: int
    max_total_rendered_pixels: int = 80_000_000
    image_preprocessing_enabled: bool = True
    image_preprocessing_contrast: float = 1.12
    image_preprocessing_sharpness: float = 1.18

    def __post_init__(self) -> None:
        for field_name in (
            "max_upload_bytes",
            "max_pdf_pages",
            "pdf_render_dpi",
            "max_image_dimension",
            "max_image_pixels",
            "max_total_rendered_pixels",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be greater than zero")
        if self.image_preprocessing_contrast <= 0:
            raise ValueError("image_preprocessing_contrast must be greater than zero")
        if self.image_preprocessing_sharpness <= 0:
            raise ValueError("image_preprocessing_sharpness must be greater than zero")
