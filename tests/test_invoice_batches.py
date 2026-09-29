"""Safety checks for bounded invoice segmentation and route permissions."""

import pytest
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO

from PIL import Image

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invoice_intelligence.api.security import permission_for_request
from invoice_intelligence.application.services.authorization import Permission
from invoice_intelligence.domain.invoice_batch import InvoiceGroup, InvoiceRegion, validate_groups
from invoice_intelligence.domain.document import (
    DocumentProcessingLimits, DocumentReference, InspectedDocument, VisionImage,
)
from invoice_intelligence.domain.extraction import (
    ExtractionResult, OCRFieldObservation, OCRProviderStatus, RawOCRObservation, RawOCRResult,
)
from invoice_intelligence.application.services.vision_extraction import VisionExtractionService
from invoice_intelligence.infrastructure.documents.processor import PillowMuPdfDocumentProcessor
from invoice_intelligence.workflow.nodes import InvoiceWorkflowNodes
from invoice_intelligence.infrastructure.persistence.sqlalchemy_invoice_batches import SQLAlchemyInvoiceBatchRepository
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base, DocumentRow, ExtractionRunRow, InvoiceBatchFileRow,
    InvoiceBatchItemRow, InvoiceBatchRow, StoredObjectRow,
)
from invoice_intelligence.application.errors import ResourceConflictError, ResourceNotFoundError


def test_invoice_regions_cover_pages_without_overlap() -> None:
    groups = (
        InvoiceGroup((InvoiceRegion(1, 0, 0, 0.5, 1), InvoiceRegion(2, 0, 0, 1, 1))),
        InvoiceGroup((InvoiceRegion(1, 0.5, 0, 1, 1),)),
    )
    validate_groups(groups, 2)
    with pytest.raises(ValueError, match="overlap"):
        validate_groups((groups[0], InvoiceGroup((InvoiceRegion(1, 0.4, 0, 1, 1),))), 2)
    with pytest.raises(ValueError, match="Every source page"):
        validate_groups((InvoiceGroup((InvoiceRegion(1, 0, 0, 1, 1),)),), 2)


def test_batch_routes_use_document_permissions() -> None:
    assert permission_for_request("POST", "/api/v1/invoice-batches", "/api/v1") is Permission.DOCUMENT_EXTRACT
    assert permission_for_request("GET", "/api/v1/invoice-batches/batch-1", "/api/v1") is Permission.DOCUMENT_READ
    assert permission_for_request("GET", "/api/v1/invoice-batches/batch-1/files/file-1/pages/1", "/api/v1") is Permission.DOCUMENT_READ


def test_cross_page_invoice_is_materialized_as_one_pdf() -> None:
    output = BytesIO()
    Image.new("RGB", (200, 200), "white").save(output, format="PNG")
    images = tuple(VisionImage(output.getvalue(), "image/png", index, 200, 200)
                   for index in (1, 2))
    content, mime, filename = PillowMuPdfDocumentProcessor._crop_invoice_sync(images, (
        InvoiceRegion(1, 0, 0, 1, 1), InvoiceRegion(2, 0, 0, 1, 1),
    ))
    assert mime == "application/pdf" and filename.endswith(".pdf")
    import pymupdf
    with pymupdf.open(stream=content, filetype="pdf") as document:
        assert document.page_count == 2


def test_batch_crop_uses_original_image_pixels() -> None:
    output = BytesIO()
    Image.new("RGB", (3000, 1000), "white").save(output, format="PNG")
    limits = DocumentProcessingLimits(10_000_000, 5, 200, 2048, 20_000_000)
    crop = PillowMuPdfDocumentProcessor._crop_invoice_from_source_sync(
        output.getvalue(), InspectedDocument("image/png", len(output.getvalue()), 1),
        (InvoiceRegion(1, 0, 0, 0.5, 1),), limits, 800, 600, 0.45,
    )
    assert crop.native_sizes == ((1500, 1000),)
    with Image.open(BytesIO(crop.content)) as image:
        assert image.size == (1500, 1000)


def test_two_regions_in_one_group_remain_one_child_pdf() -> None:
    import pymupdf
    output = BytesIO()
    Image.new("RGB", (200, 100), "white").save(output, format="PNG")
    content = output.getvalue()
    limits = DocumentProcessingLimits(10_000_000, 5, 200, 4096, 20_000_000)
    crop = PillowMuPdfDocumentProcessor._crop_invoice_from_source_sync(
        content, InspectedDocument("image/png", len(content), 1),
        (InvoiceRegion(1, 0, 0, 0.5, 1), InvoiceRegion(1, 0.5, 0, 1, 1)),
        limits, 800, 600, 0.45,
    )
    assert crop.native_sizes == ((100, 100), (100, 100))
    with pymupdf.open(stream=crop.content, filetype="pdf") as document:
        assert document.page_count == 2


def test_small_pdf_clip_gets_one_bounded_rerender() -> None:
    import pymupdf
    document = pymupdf.open()
    document.new_page(width=600, height=600)
    content = document.tobytes()
    document.close()
    limits = DocumentProcessingLimits(10_000_000, 5, 200, 4096, 20_000_000)
    crop = PillowMuPdfDocumentProcessor._crop_invoice_from_source_sync(
        content, InspectedDocument("application/pdf", len(content), 1),
        (InvoiceRegion(1, 0, 0, 0.5, 1),), limits, 1000, 600, 0,
    )
    assert crop.render_attempts == (2,)
    assert 1000 <= crop.native_sizes[0][0] <= 4096


@pytest.mark.asyncio
async def test_batch_source_ocr_rejects_other_invoice_and_boundary_text() -> None:
    source_content = b"source"
    source = DocumentReference("source", "memory://source", "image/png",
                               sha256(source_content).hexdigest())
    image = VisionImage(b"image", "image/png", 1, 100, 100)

    class Documents:
        async def get_document(self, document_id, tenant_id):
            return source if (document_id, tenant_id) == ("source", "tenant-a") else None

    class Storage:
        async def read(self, uri):
            return source_content

    class Processor:
        async def inspect(self, upload, limits):
            return InspectedDocument("image/png", len(upload.content), 1)

        async def to_vision_images(self, content, inspected, limits):
            return (image,)

    class OCR:
        async def observe_raw(self, images):
            observations = tuple(
                RawOCRObservation("source", "stub", "v1", "v1", 1, "", "", box, None)
                for box in ((10, 10, 20, 20), (60, 10, 70, 20), (49, 10, 51, 20), None)
            )
            return RawOCRResult(OCRProviderStatus.AVAILABLE, observations)

    service = object.__new__(VisionExtractionService)
    service._document_repository = Documents()
    service._file_storage = Storage()
    service._document_processor = Processor()
    service._limits = object()
    service._raw_ocr_providers = (OCR(),)
    result = await service._observe_batch_source({
        "source_document_id": "source",
        "regions": [{"page_number": 1, "left": 0, "top": 0, "right": 0.5, "bottom": 1}],
        "other_regions": [{"page_number": 1, "left": 0.5, "top": 0, "right": 1, "bottom": 1}],
    }, (VisionImage(b"crop", "image/png", 1, 50, 100),), "tenant-a", "child")
    assert result.status is OCRProviderStatus.AVAILABLE
    assert len(result.observations) == 1
    assert result.observations[0].bounding_box == (10, 10, 20, 20)


def test_cross_page_different_field_candidates_require_review() -> None:
    result = ExtractionResult(
        invoice=None, field_evidence=(), anomalies=(),
        ocr_observations=(
            OCRFieldObservation("invoice_number", 1, ("A001",)),
            OCRFieldObservation("invoice_number", 2, ("B002",)),
        ),
    )
    enriched = InvoiceWorkflowNodes._with_cross_page_field_conflicts(result)
    assert [(item.code, item.field_path) for item in enriched.anomalies] == [
        ("cross_page_field_conflict", "invoice_number"),
    ]
    same = ExtractionResult(
        invoice=None, field_evidence=(), anomalies=(),
        ocr_observations=(
            OCRFieldObservation("invoice_number", 1, ("A001",)),
            OCRFieldObservation("invoice_number", 2, ("A001",)),
        ),
    )
    assert not InvoiceWorkflowNodes._with_cross_page_field_conflicts(same).anomalies


@pytest.mark.asyncio
async def test_batch_limit_is_checked_before_items_are_created() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[
        StoredObjectRow.__table__, DocumentRow.__table__, ExtractionRunRow.__table__,
        InvoiceBatchRow.__table__, InvoiceBatchFileRow.__table__, InvoiceBatchItemRow.__table__,
    ])
    repo = SQLAlchemyInvoiceBatchRepository(engine)
    batch_id = await repo.create("tenant-a", "key-1")
    assert await repo.create("tenant-a", "key-1") == batch_id
    with pytest.raises(ResourceNotFoundError):
        await repo.get(batch_id, "tenant-b")
    now = datetime.now(UTC)
    with Session(engine) as session:
        for index in range(1, 3):
            session.add(DocumentRow(
                document_id=f"document-{index}", tenant_id="tenant-a",
                storage_uri=f"memory://{index}", mime_type="application/pdf",
                checksum=f"checksum-{index}", original_object_id=None,
                size_bytes=100, storage_status="available", created_at=now,
            ))
        session.commit()
    first = await repo.attach_file(batch_id, "tenant-a", "document-1", "a.pdf")
    second = await repo.attach_file(batch_id, "tenant-a", "document-2", "b.pdf")
    with pytest.raises(ResourceConflictError):
        await repo.submit(batch_id, "tenant-a", 1)
    await repo.submit(batch_id, "tenant-a", 3)
    for file_id, count in ((first, 3), (second, 3)):
        claim = await repo.claim()
        assert claim is not None and claim["file_id"] == file_id
        await repo.propose(claim, 1, [{"regions": [{
            "page_number": 1, "left": 0, "top": 0,
            "right": 1, "bottom": 1,
        }]} for _ in range(count)], False)
    assert (await repo.get(batch_id, "tenant-a"))["status"] == "too_many_invoices"
    assert await repo.prepare_items(batch_id, "tenant-a") is None
    assert (await repo.get(batch_id, "tenant-a"))["items"] == []


@pytest.mark.asyncio
async def test_child_source_is_visible_before_run_and_tenant_scoped() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[
        StoredObjectRow.__table__, DocumentRow.__table__, ExtractionRunRow.__table__,
        InvoiceBatchRow.__table__, InvoiceBatchFileRow.__table__, InvoiceBatchItemRow.__table__,
    ])
    repo = SQLAlchemyInvoiceBatchRepository(engine)
    batch_id = await repo.create("tenant-a", "source-key")
    now = datetime.now(UTC)
    with Session(engine) as session:
        for document_id in ("source", "child"):
            session.add(DocumentRow(
                document_id=document_id, tenant_id="tenant-a",
                storage_uri=f"memory://{document_id}", mime_type="image/png",
                checksum=document_id, original_object_id=None, size_bytes=100,
                storage_status="available", created_at=now,
            ))
        session.commit()
    await repo.attach_file(batch_id, "tenant-a", "source", "source.png")
    await repo.submit(batch_id, "tenant-a", 2)
    claim = await repo.claim()
    assert claim is not None
    region = {"page_number": 1, "left": 0, "top": 0, "right": 1, "bottom": 1}
    await repo.propose(claim, 1, [{"regions": [region]}], False)
    item = (await repo.prepare_items(batch_id, "tenant-a"))[0]
    await repo.set_item(item["item_id"], "tenant-a", "child", None, [
        {"width": 100, "height": 100, "native_width": 100,
         "native_height": 100, "render_attempts": 1},
    ])
    await repo.set_item(item["item_id"], "tenant-a", "child", None, [
        {"width": 100, "height": 100, "native_width": 100,
         "native_height": 100, "render_attempts": 1},
    ])
    assert (await repo.source_for_child("child", "tenant-a"))["regions"] == [region]
    assert await repo.source_for_child("child", "tenant-b") is None
    assert (await repo.get(batch_id, "tenant-a"))["status"] == "extracting"
