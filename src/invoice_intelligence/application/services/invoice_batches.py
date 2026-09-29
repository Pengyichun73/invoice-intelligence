"""Bounded invoice batch orchestration without changing the single-invoice workflow."""

from dataclasses import replace

from invoice_intelligence.application.errors import (
    BadRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from invoice_intelligence.application.ports.document_processor import DocumentProcessor
from invoice_intelligence.application.ports.document_repository import DocumentReferenceRepository
from invoice_intelligence.application.ports.file_storage import FileStorage
from invoice_intelligence.application.ports.invoice_batches import InvoiceBatchRepository
from invoice_intelligence.application.ports.invoice_segmentation import InvoiceSegmentationProvider
from invoice_intelligence.application.ports.validation import RawOCRProvider
from invoice_intelligence.application.services.document_ingestion import DocumentIngestionService
from invoice_intelligence.application.services.extraction_workflow import ExtractionWorkflowService
from invoice_intelligence.application.services.idempotency import normalize_idempotency_key
from invoice_intelligence.domain.document import DocumentProcessingLimits, UploadDocument
from invoice_intelligence.domain.extraction import OCRProviderStatus
from invoice_intelligence.domain.invoice_batch import InvoiceGroup, InvoiceRegion, validate_groups


class InvoiceBatchService:
    def __init__(
        self, repository: InvoiceBatchRepository,
        ingestion: DocumentIngestionService,
        documents: DocumentReferenceRepository,
        storage: FileStorage,
        processor: DocumentProcessor,
        limits: DocumentProcessingLimits,
        detector: InvoiceSegmentationProvider | None,
        extractor: ExtractionWorkflowService | None = None,
        raw_ocr: RawOCRProvider | None = None,
        min_width: int = 800, min_height: int = 600, min_clarity: float = 0.45,
    ) -> None:
        self._repository = repository
        self._ingestion = ingestion
        self._documents = documents
        self._storage = storage
        self._processor = processor
        self._limits = limits
        self._detector = detector
        self._extractor = extractor
        self._raw_ocr = raw_ocr
        self._min_width = min_width
        self._min_height = min_height
        self._min_clarity = min_clarity

    async def create(self, tenant_id: str, key: str, actor_id: str) -> dict:
        self._require_detector()
        batch_id = await self._repository.create(tenant_id, normalize_idempotency_key(key), actor_id)
        return await self.get(batch_id, tenant_id)

    async def list_recent(self, tenant_id: str, actor_id: str) -> list[dict]:
        return await self._repository.list_recent(tenant_id, actor_id, 30)

    async def add_file(self, batch_id: str, tenant_id: str, upload: UploadDocument,
                       key: str, trace_id: str | None) -> dict:
        batch = await self.get(batch_id, tenant_id)
        if batch["status"] != "open" or len(batch["files"]) >= 5:
            raise BadRequestError("Invoice batch accepts at most five files")
        document = await self._ingestion.ingest(
            upload, idempotency_key=f"{batch_id}-{normalize_idempotency_key(key)}",
            tenant_id=tenant_id, trace_id=trace_id,
        )
        await self._repository.attach_file(batch_id, tenant_id, document.document_id, upload.filename)
        return await self.get(batch_id, tenant_id)

    async def submit(self, batch_id: str, tenant_id: str, revision: int) -> dict:
        await self._repository.submit(batch_id, tenant_id, revision)
        return await self.get(batch_id, tenant_id)

    async def get(self, batch_id: str, tenant_id: str) -> dict:
        return await self._repository.get(batch_id, tenant_id)

    async def retry_item(self, batch_id: str, item_id: str, tenant_id: str,
                         expected_run_id: str, key: str) -> dict:
        await self._repository.retry_item(
            batch_id, item_id, tenant_id, expected_run_id,
            normalize_idempotency_key(key),
        )
        return await self.get(batch_id, tenant_id)

    async def confirm(self, batch_id: str, file_id: str, tenant_id: str,
                      revision: int, groups: tuple[InvoiceGroup, ...]) -> dict:
        batch = await self.get(batch_id, tenant_id)
        file = next((item for item in batch["files"] if item["file_id"] == file_id), None)
        if file is None:
            raise ResourceNotFoundError("Invoice batch file was not found")
        if file["status"] != "needs_review" or file["page_count"] is None:
            raise ResourceConflictError("Invoice boundaries are not awaiting confirmation")
        validate_groups(groups, file["page_count"])
        await self._repository.confirm(
            batch_id, file_id, tenant_id, revision, file["page_count"],
            self._encode_groups(groups),
        )
        return await self.get(batch_id, tenant_id)

    async def process_claim(self, claim: dict) -> None:
        detector = self._require_detector()
        document = await self._documents.get_document(claim["document_id"], claim["tenant_id"])
        if document is None:
            raise ValueError("Invoice batch source document was not found")
        content = await self._storage.read(document.storage_uri)
        inspected = await self._processor.inspect(
            UploadDocument(None, document.mime_type, content), self._limits,
        )
        images = await self._processor.to_vision_images(content, inspected, self._limits)
        groups, uncertain = await detector.detect(images)
        validate_groups(groups, inspected.page_count)
        if self._raw_ocr is None:
            uncertain = True
        else:
            try:
                ocr = await self._raw_ocr.observe_raw(images)
                if ocr.status is not OCRProviderStatus.AVAILABLE:
                    uncertain = True
                else:
                    for observation in ocr.observations:
                        label = observation.normalized_text.casefold()
                        if not any(anchor in label for anchor in (
                            "发票号码", "发票代码", "invoice no", "invoice number",
                        )) or observation.bounding_box is None:
                            continue
                        image = images[observation.page_number - 1]
                        left, top, right, bottom = observation.bounding_box
                        x = (left + right) / (2 * image.width)
                        y = (top + bottom) / (2 * image.height)
                        covered = any(
                            region.page_number == observation.page_number
                            and region.left <= x <= region.right
                            and region.top <= y <= region.bottom
                            for group in groups for region in group.regions
                        )
                        if not covered:
                            uncertain = True
            except Exception:
                uncertain = True
        await self._repository.propose(
            claim, inspected.page_count, self._encode_groups(groups), uncertain,
        )

    async def preview_page(self, batch_id: str, file_id: str,
                           tenant_id: str, page_number: int) -> bytes:
        batch = await self.get(batch_id, tenant_id)
        file = next((item for item in batch["files"] if item["file_id"] == file_id), None)
        if file is None:
            raise ResourceNotFoundError("Invoice batch file was not found")
        document = await self._documents.get_document(file["document_id"], tenant_id)
        if document is None:
            raise ResourceNotFoundError("Invoice batch source document was not found")
        content = await self._storage.read(document.storage_uri)
        inspected = await self._processor.inspect(
            UploadDocument(None, document.mime_type, content), self._limits,
        )
        if not 1 <= page_number <= inspected.page_count:
            raise BadRequestError("Source page is outside the document")
        images = await self._processor.to_vision_images(content, inspected, self._limits)
        return images[page_number - 1].content

    async def dispatch_ready(self) -> None:
        if self._extractor is None:
            raise ServiceUnavailableError("Invoice batch extraction is not configured")
        for batch_id, tenant_id in await self._repository.ready_batches():
            items = await self._repository.prepare_items(batch_id, tenant_id)
            if items is None:
                continue
            batch = await self.get(batch_id, tenant_id)
            files = {file["file_id"]: file for file in batch["files"]}
            for item in items:
                if item["run_id"]:
                    continue
                source = files[item["file_id"]]
                document = await self._documents.get_document(source["document_id"], tenant_id)
                if document is None:
                    raise ValueError("Invoice batch source document was not found")
                content = await self._storage.read(document.storage_uri)
                inspected = await self._processor.inspect(
                    UploadDocument(None, document.mime_type, content), self._limits,
                )
                regions = tuple(InvoiceRegion(**region) for region in item["regions"])
                crop = await self._processor.crop_invoice_from_source(
                    content, inspected, regions, self._limits,
                    self._min_width, self._min_height, self._min_clarity,
                )
                child_inspected = await self._processor.inspect(
                    UploadDocument(crop.filename, crop.mime_type, crop.content), self._limits,
                )
                derived_images = await self._processor.to_vision_images(
                    crop.content, child_inspected,
                    replace(self._limits, pdf_render_dpi=(
                        300 if 2 in crop.render_attempts
                        else min(self._limits.pdf_render_dpi, 300)
                    )),
                )
                derived_pages = [{
                    "width": page.width, "height": page.height,
                    "native_width": native[0], "native_height": native[1],
                    "render_attempts": attempts,
                    "native_clarity_milli": round(clarity * 1000),
                } for page, native, attempts, clarity in zip(
                    derived_images, crop.native_sizes, crop.render_attempts,
                    crop.clarity_scores, strict=True,
                )]
                child = await self._ingestion.ingest(
                    UploadDocument(crop.filename, crop.mime_type, crop.content),
                    idempotency_key=f"{item['item_id']}-document",
                    tenant_id=tenant_id,
                )
                await self._repository.set_item(
                    item["item_id"], tenant_id, child.document_id, None, derived_pages,
                )
                run = await self._extractor.extract_document(
                    child.document_id, tenant_id,
                    idempotency_key=f"{item['item_id']}-run-{item['run_attempt']}",
                )
                await self._repository.set_item(
                    item["item_id"], tenant_id, child.document_id, run.run_id,
                    derived_pages,
                )

    @staticmethod
    def _encode_groups(groups: tuple[InvoiceGroup, ...]) -> list[dict]:
        return [{"regions": [{
            "page_number": region.page_number, "left": region.left, "top": region.top,
            "right": region.right, "bottom": region.bottom,
        } for region in group.regions]} for group in groups]

    def _require_detector(self) -> InvoiceSegmentationProvider:
        if self._detector is None:
            raise ServiceUnavailableError("Invoice segmentation provider is not configured")
        return self._detector
