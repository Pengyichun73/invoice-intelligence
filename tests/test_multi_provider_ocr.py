import asyncio
from dataclasses import replace
from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image

from invoice_intelligence.application.services.vision_extraction import (
    VisionExtractionService,
)
from invoice_intelligence.domain.document import (
    DocumentProcessingLimits,
    DocumentReference,
    InspectedDocument,
    VisionImage,
)
from invoice_intelligence.domain.extraction import (
    EvidenceSource,
    ExtractionResult,
    FieldEvidence,
    OCRComparisonOutcome,
    OCRFieldObservation,
    OCRProviderStatus,
    OCRVisionComparison,
    RawOCRObservation,
    RawOCRResult,
    Readability,
    VisionPromptContext,
)
from invoice_intelligence.domain.field_semantics import FieldBindingStatus
from invoice_intelligence.domain.invoice import InvoiceExtraction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,seen,expected",
    [
        ("shadow", "CURRENT-123", None),
        ("apply", "CURRENT-123", "CURRENT-123"),
        ("apply", "DIFFERENT-456", None),
    ],
)
async def test_targeted_reread_uses_current_region_only(
    mode: str, seen: str, expected: str | None
) -> None:
    with BytesIO() as output:
        Image.new("RGB", (200, 150), "white").save(output, format="PNG")
        page = VisionImage(output.getvalue(), "image/png", 1, 200, 150)
    path = "invoice_number"
    initial_evidence = FieldEvidence(
        path, EvidenceSource.VISUAL, 1, (), Readability.MISSING, ()
    )
    reread_evidence = FieldEvidence(
        path, EvidenceSource.VISUAL, 1, (seen,), Readability.READABLE, ()
    )
    comparison = OCRVisionComparison(
        path, (), ("CURRENT-123",), (), (),
        OCRComparisonOutcome.OCR_ONLY, (), True,
    )
    ocr = OCRFieldObservation(
        field_path=path, page_number=1, candidate_values=("CURRENT-123",),
        bounding_box=(20.0, 20.0, 80.0, 45.0),
        binding_status=FieldBindingStatus.ACCEPTED,
    )
    empty = InvoiceExtraction.model_validate(
        {name: None for name in InvoiceExtraction.model_fields}
    )
    initial = ExtractionResult(
        empty, (initial_evidence,), (),
        ocr_observations=(ocr,), ocr_comparisons=(comparison,),
    )

    class Provider:
        async def extract(self, *, images, **kwargs):
            assert images[0].width < page.width
            return ExtractionResult(
                empty.model_copy(update={path: seen}),
                (reread_evidence,), (),
            )

    class Comparison:
        async def compare(self, *, result, **kwargs):
            return replace(
                result,
                ocr_comparisons=(OCRVisionComparison(
                    path, ("CURRENT-123",), ("CURRENT-123",), (), (),
                    OCRComparisonOutcome.CORROBORATED, (), False,
                ),),
            )

    service = object.__new__(VisionExtractionService)
    service._provider = Provider()
    service._targeted_reread_mode = mode
    actual = await service._targeted_reread(
        initial, (page,), InvoiceExtraction,
        VisionPromptContext(focus_field_paths=(path,)),
        "tenant-a", "document-a", "invoice", (), Comparison(),
    )
    assert actual.invoice.invoice_number == expected
    if expected is None:
        assert actual is initial
    else:
        assert "targeted_region_reread" in actual.field_evidence[0].validation_signals


def test_targeted_crop_rejects_unbounded_ocr_regions() -> None:
    with BytesIO() as output:
        Image.new("RGB", (200, 150), "white").save(output, format="PNG")
        page = VisionImage(output.getvalue(), "image/png", 1, 200, 150)
    with pytest.raises(ValueError, match="outside"):
        VisionExtractionService._crop_ocr_region(page, (180.0, 10.0, 220.0, 30.0))
    with pytest.raises(ValueError, match="localized"):
        VisionExtractionService._crop_ocr_region(page, (1.0, 1.0, 199.0, 149.0))


class _Facts:
    document_type = "invoice"


class _ScopeResolver:
    def current_facts(self, invoice: object) -> _Facts:
        return _Facts()


class _Comparison:
    raw_results: tuple[RawOCRResult, ...] = ()

    async def compare(self, *, result: ExtractionResult[object], raw_results, **kwargs):
        self.raw_results = tuple(raw_results)
        return replace(
            result,
            raw_ocr_observations=tuple(
                observation
                for raw_result in self.raw_results
                for observation in raw_result.observations
            ),
        )


class _Provider:
    def __init__(self, name: str, delay: float, *, fail: bool = False) -> None:
        self.name = name
        self.delay = delay
        self.fail = fail

    async def observe_raw(self, images) -> RawOCRResult:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise TimeoutError("provider unavailable")
        return RawOCRResult(
            status=OCRProviderStatus.AVAILABLE,
            trace_id=f"trace-{self.name}",
            observations=(
                RawOCRObservation(
                    source_id=f"source-{self.name}",
                    provider_name=self.name,
                    provider_version="1",
                    model_version="1",
                    page_number=1,
                    observed_text=self.name,
                    normalized_text=self.name,
                    bounding_box=(0.0, 0.0, 1.0, 1.0),
                    provider_score=0.8,
                ),
            ),
        )


class _Storage:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.read_count = 0

    async def read(self, storage_uri: str) -> bytes:
        self.read_count += 1
        return self.content


class _Processor:
    def __init__(self, images: tuple[VisionImage, ...]) -> None:
        self.images = images
        self.inspect_count = 0
        self.render_count = 0

    async def inspect(self, document, limits) -> InspectedDocument:
        self.inspect_count += 1
        return InspectedDocument("image/png", len(document.content), len(self.images))

    async def to_vision_images(self, *, content, inspected, limits):
        self.render_count += 1
        return self.images


class _Quality:
    async def analyze(self, images):
        return ()


class _Catalog:
    async def prompt_catalog(self, tenant_id: str):
        return ()


class _VisionProvider:
    def __init__(self) -> None:
        self.images = None
        self.invoice = {"invoice_number": "VISION-001"}

    async def extract(self, *, images, **kwargs):
        self.images = images
        return ExtractionResult(invoice=self.invoice, field_evidence=(), anomalies=())


class _CapturingRawProvider(_Provider):
    def __init__(self, name: str, *, fail: bool = False) -> None:
        super().__init__(name, 0.0, fail=fail)
        self.images = None

    async def observe_raw(self, images) -> RawOCRResult:
        self.images = images
        return await super().observe_raw(images)


@pytest.mark.asyncio
async def test_multi_provider_merge_order_is_configuration_order_and_failure_degrades() -> None:
    slow = _Provider("z-provider", 0.02)
    fast = _Provider("a-provider", 0.0)
    failed = _Provider("failed-provider", 0.0, fail=True)
    comparison = _Comparison()
    service = VisionExtractionService(
        file_storage=object(),
        document_processor=object(),
        provider=object(),
        image_quality_analyzer=object(),
        limits=object(),
        field_semantic_catalog=object(),
        correction_scope_resolver=_ScopeResolver(),
        schema_version="3.0.0",
        raw_ocr_providers=(slow, fast, failed),
        ocr_comparison_service=comparison,
    )
    tasks = tuple(
        asyncio.create_task(service._observe_raw_ocr(provider, ()))
        for provider in (slow, fast, failed)
    )
    result = await service._add_ocr_evidence(
        result=ExtractionResult(invoice=object(), field_evidence=(), anomalies=()),
        legacy_ocr_task=None,
        raw_ocr_tasks=tasks,
        tenant_id="tenant-a",
        document_id="document-a",
    )

    assert tuple(item.trace_id for item in comparison.raw_results) == (
        "trace-z-provider",
        "trace-a-provider",
        None,
    )
    assert comparison.raw_results[-1].status is OCRProviderStatus.UNAVAILABLE
    assert result.invoice is not None


@pytest.mark.asyncio
async def test_extract_shares_normalized_images_and_ocr_cannot_overwrite_vision() -> None:
    content = b"normalized-source"
    images = (
        VisionImage(b"page-1", "image/png", 1, 800, 600),
        VisionImage(b"page-2", "image/png", 2, 800, 600),
    )
    storage = _Storage(content)
    processor = _Processor(images)
    vision = _VisionProvider()
    available = _CapturingRawProvider("available")
    unavailable = _CapturingRawProvider("unavailable", fail=True)
    comparison = _Comparison()
    service = VisionExtractionService(
        file_storage=storage,
        document_processor=processor,
        provider=vision,
        image_quality_analyzer=_Quality(),
        limits=DocumentProcessingLimits(1024, 2, 300, 2048, 4_000_000),
        field_semantic_catalog=_Catalog(),
        correction_scope_resolver=_ScopeResolver(),
        schema_version="3.0.0",
        raw_ocr_providers=(available, unavailable),
        ocr_comparison_service=comparison,
    )

    result = await service.extract(
        DocumentReference(
            document_id="document-a",
            storage_uri="memory://document-a",
            mime_type="image/png",
            checksum=sha256(content).hexdigest(),
        ),
        object,
        "tenant-a",
    )

    assert storage.read_count == 1
    assert processor.inspect_count == 1
    assert processor.render_count == 1
    assert vision.images is images
    assert available.images is images
    assert unavailable.images is images
    assert result.invoice is vision.invoice
    assert tuple(item.status for item in comparison.raw_results) == (
        OCRProviderStatus.AVAILABLE,
        OCRProviderStatus.UNAVAILABLE,
    )


@pytest.mark.asyncio
async def test_evaluation_extract_disables_ocr_and_artifact_writes() -> None:
    content = b"isolated-source"
    images = (VisionImage(b"page", "image/png", 1, 800, 600),)
    raw_provider = _CapturingRawProvider("raw")

    class Artifacts:
        async def store_rendered(self, *args):
            raise AssertionError("Evaluation must not persist rendered pages")

    service = VisionExtractionService(
        file_storage=_Storage(content),
        artifact_service=Artifacts(),
        document_processor=_Processor(images),
        provider=_VisionProvider(),
        image_quality_analyzer=_Quality(),
        limits=DocumentProcessingLimits(1024, 2, 300, 2048, 4_000_000),
        field_semantic_catalog=_Catalog(),
        correction_scope_resolver=_ScopeResolver(),
        schema_version="3.0.0",
        raw_ocr_providers=(raw_provider,),
        ocr_comparison_service=_Comparison(),
    )
    result = await service.extract(
        DocumentReference("doc", "isolated://doc", "image/png", sha256(content).hexdigest()),
        object, "isolated-tenant",
        include_ocr=False, persist_artifacts=False, targeted_reread_mode="off",
    )
    assert result.raw_ocr_observations == ()
    assert raw_provider.images is None
