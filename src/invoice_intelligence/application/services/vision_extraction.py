"""Vision extraction orchestration use case."""

import asyncio
import logging
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from hashlib import sha256
from hmac import compare_digest
from io import BytesIO
from statistics import median
from typing import Generic, Literal, TypeVar, cast
from uuid import uuid4

from PIL import Image
from pydantic import BaseModel, TypeAdapter, ValidationError

from invoice_intelligence.application.errors import DocumentIntegrityError
from invoice_intelligence.application.ports.document_processor import DocumentProcessor
from invoice_intelligence.application.ports.document_repository import DocumentReferenceRepository
from invoice_intelligence.application.ports.file_storage import FileStorage
from invoice_intelligence.application.ports.invoice_batches import InvoiceBatchRepository
from invoice_intelligence.application.ports.memory import CorrectionScopeResolver
from invoice_intelligence.application.ports.observability import PrivacyTelemetry, TraceStage
from invoice_intelligence.application.ports.validation import (
    ImageQualityAnalyzer,
    OCRValidationProvider,
    RawOCRProvider,
)
from invoice_intelligence.application.ports.vision_extraction import VisionExtractionProvider
from invoice_intelligence.application.services.document_artifacts import DocumentArtifactService
from invoice_intelligence.application.services.field_semantic_binding import (
    FieldSemanticBindingRequest,
    FieldSemanticBindingService,
)
from invoice_intelligence.application.services.field_semantic_catalog import (
    FieldSemanticCatalog,
)
from invoice_intelligence.application.services.multi_source_ocr_comparison import (
    DeterministicMultiSourceOCRComparisonService,
    normalize_ocr_text,
)
from invoice_intelligence.domain.document import (
    DocumentProcessingLimits,
    DocumentReference,
    UploadDocument,
    VisionImage,
)
from invoice_intelligence.domain.extraction import (
    EvidenceSource,
    ExtractionAnomaly,
    ExtractionResult,
    FieldEvidence,
    OCRComparisonOutcome,
    OCRFieldObservation,
    OCRProviderStatus,
    PromptContextBudget,
    RawOCRObservation,
    RawOCRResult,
    Readability,
    VisionPromptContext,
)
from invoice_intelligence.domain.field_semantics import FieldBindingEvidence, FieldBindingStatus

InvoiceT = TypeVar("InvoiceT")
_LOGGER = logging.getLogger(__name__)


class VisionExtractionService(Generic[InvoiceT]):
    """Load a validated document and delegate bounded images to a provider."""

    def __init__(
        self,
        file_storage: FileStorage,
        document_processor: DocumentProcessor,
        provider: VisionExtractionProvider,
        image_quality_analyzer: ImageQualityAnalyzer,
        limits: DocumentProcessingLimits,
        field_semantic_catalog: FieldSemanticCatalog[InvoiceT],
        correction_scope_resolver: CorrectionScopeResolver,
        schema_version: str,
        field_semantic_binding_service: FieldSemanticBindingService[InvoiceT] | None = None,
        ocr_provider: OCRValidationProvider | None = None,
        raw_ocr_provider: RawOCRProvider | None = None,
        raw_ocr_providers: Sequence[RawOCRProvider] = (),
        ocr_comparison_service: (
            DeterministicMultiSourceOCRComparisonService[InvoiceT] | None
        ) = None,
        prompt_context_budget: PromptContextBudget | None = None,
        privacy_telemetry: PrivacyTelemetry | None = None,
        artifact_service: DocumentArtifactService | None = None,
        targeted_reread_mode: Literal["off", "shadow", "apply"] = "off",
        batch_repository: InvoiceBatchRepository | None = None,
        document_repository: DocumentReferenceRepository | None = None,
        batch_min_width: int = 800,
        batch_min_height: int = 600,
        batch_min_clarity: float = 0.45,
    ) -> None:
        if not schema_version.strip() or schema_version != schema_version.strip():
            raise ValueError("schema_version must be non-empty and normalized")
        self._file_storage = file_storage
        self._document_processor = document_processor
        self._provider = provider
        self._image_quality_analyzer = image_quality_analyzer
        self._limits = limits
        self._ocr_provider = ocr_provider
        providers = tuple(raw_ocr_providers)
        if raw_ocr_provider is not None:
            providers = (raw_ocr_provider, *providers)
        if len({id(provider) for provider in providers}) != len(providers):
            raise ValueError("raw_ocr_providers must not contain duplicate instances")
        self._raw_ocr_providers = providers
        self._ocr_comparison_service = ocr_comparison_service
        self._field_semantic_catalog = field_semantic_catalog
        self._correction_scope_resolver = correction_scope_resolver
        self._schema_version = schema_version
        self._field_semantic_binding_service = field_semantic_binding_service
        self._prompt_context_budget = prompt_context_budget or PromptContextBudget()
        self._privacy_telemetry = privacy_telemetry
        self._artifact_service = artifact_service
        if targeted_reread_mode not in {"off", "shadow", "apply"}:
            raise ValueError("targeted_reread_mode must be off, shadow, or apply")
        self._targeted_reread_mode = targeted_reread_mode
        self._batch_repository = batch_repository
        self._document_repository = document_repository
        self._batch_min_width = batch_min_width
        self._batch_min_height = batch_min_height
        self._batch_min_clarity = batch_min_clarity

    async def extract(
        self,
        document: DocumentReference,
        output_schema: type[InvoiceT],
        tenant_id: str,
        prompt_context: VisionPromptContext | None = None,
        trace_id: str | None = None,
        *,
        include_ocr: bool = True,
        persist_artifacts: bool = True,
        targeted_reread_mode: Literal["off", "shadow", "apply"] | None = None,
    ) -> ExtractionResult[InvoiceT]:
        """Verify stored bytes, normalize pages, and request structured extraction."""

        if targeted_reread_mode not in {None, "off", "shadow", "apply"}:
            raise ValueError("targeted_reread_mode must be off, shadow, or apply")
        resolved_trace_id = trace_id or uuid4().hex
        with self._span(
            resolved_trace_id,
            tenant_id,
            TraceStage.DOCUMENT_PREPROCESSING,
            "prepare_vision_input",
            {"document_id": document.document_id},
        ):
            content = await self._file_storage.read(document.storage_uri)
            actual_checksum = sha256(content).hexdigest()
            if not compare_digest(actual_checksum, document.checksum):
                raise DocumentIntegrityError(
                    "Stored document checksum does not match ingestion metadata"
                )

            inspected = await self._document_processor.inspect(
                UploadDocument(
                    filename=None,
                    declared_mime_type=document.mime_type,
                    content=content,
                ),
                self._limits,
            )
            source_context = (
                await self._batch_repository.source_for_child(document.document_id, tenant_id)
                if self._batch_repository is not None else None
            )
            render_limits = (
                replace(self._limits, pdf_render_dpi=(
                    300 if any(page.get("render_attempts") == 2
                               for page in source_context["derived_pages"])
                    else min(self._limits.pdf_render_dpi, 300)
                )) if source_context is not None else self._limits
            )
            images = await self._document_processor.to_vision_images(
                content=content,
                inspected=inspected,
                limits=render_limits,
            )
            if persist_artifacts and self._artifact_service is not None:
                try:
                    await self._artifact_service.store_rendered(
                        tenant_id,
                        document.document_id,
                        images,
                        "pillow-mupdf-render-v1",
                    )
                except Exception as exc:
                    _LOGGER.warning(
                        "rendered_artifact_storage_degraded",
                        extra={"error_type": type(exc).__name__},
                    )
            page_quality = await self._image_quality_analyzer.analyze(images)
            if source_context is not None:
                native = source_context["derived_pages"]
                if len(native) != len(page_quality):
                    raise DocumentIntegrityError("Batch source page metadata is incomplete")
                page_quality = tuple(replace(
                    page, width=min(page.width, int(meta.get("native_width", page.width))),
                    height=min(page.height, int(meta.get("native_height", page.height))),
                    clarity_score=min(
                        page.clarity_score,
                        float(meta.get("native_clarity_milli", 1000)) / 1000,
                    ),
                ) for page, meta in zip(page_quality, native, strict=True))
        batch_quality_blockers = tuple(
            code for code, failed in (
                ("batch_native_resolution_low", any(
                    page.width < self._batch_min_width or page.height < self._batch_min_height
                    for page in page_quality
                )),
                ("batch_clarity_low", any(
                    page.clarity_score < self._batch_min_clarity for page in page_quality
                )),
            ) if source_context is not None and failed
        )
        source_ocr_task = (
            asyncio.create_task(self._observe_batch_source(
                source_context, images, tenant_id, document.document_id,
            ))
            if source_context is not None and batch_quality_blockers
            and include_ocr and self._raw_ocr_providers and self._document_repository is not None
            else None
        )
        if source_context is not None:
            _LOGGER.info("batch_crop_quality", extra={
                "document_id": document.document_id,
                "blocker_codes": batch_quality_blockers,
                "native_sizes": tuple((page.width, page.height) for page in page_quality),
                "render_attempts": tuple(
                    item.get("render_attempts", 1) for item in source_context["derived_pages"]
                ),
            })
        _LOGGER.info(
            "vision_input_quality",
            extra={
                "document_id": document.document_id,
                "page_count": len(images),
                "page_quality": tuple(
                    {
                        "page_number": item.page_number,
                        "width": item.width,
                        "height": item.height,
                        "clarity_score": item.clarity_score,
                    }
                    for item in page_quality
                ),
            },
        )
        legacy_ocr_task = (
            asyncio.create_task(
                self._observe_legacy_ocr_traced(
                    images, output_schema, resolved_trace_id, tenant_id
                )
            )
            if include_ocr and self._ocr_provider is not None
            else None
        )
        raw_ocr_tasks = tuple(
            asyncio.create_task(
                self._observe_raw_ocr_traced(
                    provider, images, resolved_trace_id, tenant_id
                )
            )
            for provider in (self._raw_ocr_providers if include_ocr else ())
        )
        ocr_tasks = tuple(
            task
            for task in (legacy_ocr_task, *raw_ocr_tasks)
            if task is not None
        )
        try:
            catalog = await self._field_semantic_catalog.prompt_catalog(tenant_id)
            context = replace(
                prompt_context or VisionPromptContext(),
                field_semantic_catalog=catalog,
                budget=self._prompt_context_budget,
            )
            with self._span(
                resolved_trace_id,
                tenant_id,
                TraceStage.VISION,
                "extract_invoice_vision",
                {"document_id": document.document_id, "page_count": len(images)},
            ):
                result = await self._provider.extract(
                    images=images,
                    output_schema=output_schema,
                    prompt_context=context,
                    document_id=document.document_id,
                )
            result = replace(
                result,
                page_quality=page_quality,
                anomalies=(*result.anomalies, *(
                    ExtractionAnomaly(code, "批次票据区域图像质量不足，需核对原件", None, None)
                    for code in batch_quality_blockers
                )),
            )
            with self._span(
                resolved_trace_id,
                tenant_id,
                TraceStage.FIELD_SEMANTIC_BINDING,
                "bind_observed_field_labels",
                {"document_id": document.document_id},
            ):
                result = await self._bind_field_labels(result, tenant_id)
            return await self._add_ocr_evidence(
                result=result,
                images=images,
                output_schema=output_schema,
                prompt_context=context,
                persist_artifacts=persist_artifacts,
                targeted_reread_mode=targeted_reread_mode,
                legacy_ocr_task=legacy_ocr_task,
                raw_ocr_tasks=raw_ocr_tasks,
                source_ocr_task=source_ocr_task,
                tenant_id=tenant_id,
                document_id=document.document_id,
            )
        except BaseException:
            for task in ocr_tasks:
                task.cancel()
            if source_ocr_task is not None:
                source_ocr_task.cancel()
            await asyncio.gather(*ocr_tasks, return_exceptions=True)
            if source_ocr_task is not None:
                await asyncio.gather(source_ocr_task, return_exceptions=True)
            raise

    async def _observe_batch_source(
        self, context: dict, images: Sequence[VisionImage], tenant_id: str,
        child_document_id: str,
    ) -> RawOCRResult:
        try:
            assert self._document_repository is not None
            source = await self._document_repository.get_document(
                context["source_document_id"], tenant_id,
            )
            if source is None:
                raise DocumentIntegrityError("Batch source document is unavailable")
            content = await self._file_storage.read(source.storage_uri)
            if not compare_digest(sha256(content).hexdigest(), source.checksum):
                raise DocumentIntegrityError("Batch source checksum does not match")
            inspected = await self._document_processor.inspect(
                UploadDocument(None, source.mime_type, content), self._limits,
            )
            source_images = await self._document_processor.to_vision_images(
                content, inspected, self._limits,
            )
            result = await self._raw_ocr_providers[0].observe_raw(source_images)
            if result.status is not OCRProviderStatus.AVAILABLE:
                return result
            selected: list[RawOCRObservation] = []
            heights: list[float] = []
            excluded = 0
            for observation in result.observations:
                box = observation.bounding_box
                if box is None or observation.page_number > len(source_images):
                    excluded += 1
                    continue
                source_image = source_images[observation.page_number - 1]
                matches = []
                for index, region in enumerate(context["regions"]):
                    if region["page_number"] != observation.page_number:
                        continue
                    bounds = (
                        region["left"] * source_image.width,
                        region["top"] * source_image.height,
                        region["right"] * source_image.width,
                        region["bottom"] * source_image.height,
                    )
                    if not (bounds[0] <= box[0] < box[2] <= bounds[2]
                            and bounds[1] <= box[1] < box[3] <= bounds[3]):
                        continue
                    matches.append((index, bounds))
                ambiguous = any(
                        other["page_number"] == observation.page_number
                        and other["left"] * source_image.width < box[2]
                        and box[0] < other["right"] * source_image.width
                        and other["top"] * source_image.height < box[3]
                        and box[1] < other["bottom"] * source_image.height
                        for other in context["other_regions"]
                )
                if len(matches) != 1 or ambiguous:
                    excluded += 1
                    continue
                index, bounds = matches[0]
                target = images[index]
                scale_x = target.width / (bounds[2] - bounds[0])
                scale_y = target.height / (bounds[3] - bounds[1])
                selected.append(replace(
                    observation,
                    source_id=f"{observation.source_id}:batch-region:{index + 1}",
                    page_number=index + 1,
                    bounding_box=(
                        (box[0] - bounds[0]) * scale_x,
                        (box[1] - bounds[1]) * scale_y,
                        (box[2] - bounds[0]) * scale_x,
                        (box[3] - bounds[1]) * scale_y,
                    ),
                ))
                heights.append(box[3] - box[1])
            _LOGGER.info("batch_source_ocr_filter", extra={
                "document_id": child_document_id,
                "included_count": len(selected), "excluded_count": excluded,
                "median_text_height_px": median(heights) if heights else None,
            })
            return RawOCRResult(status=OCRProviderStatus.AVAILABLE,
                                observations=tuple(selected))
        except Exception as exc:
            _LOGGER.warning("batch_source_ocr_unavailable", extra={
                "document_id": child_document_id,
                "error_type": type(exc).__name__,
            })
            return RawOCRResult(status=OCRProviderStatus.UNAVAILABLE,
                                anomalies=("batch_source_ocr_unavailable",))

    async def _observe_legacy_ocr_traced(
        self,
        images: Sequence[VisionImage],
        output_schema: type[InvoiceT],
        trace_id: str,
        tenant_id: str,
    ) -> tuple[OCRFieldObservation, ...]:
        with self._span(
            trace_id,
            tenant_id,
            TraceStage.OCR,
            "observe_legacy_ocr",
            {"page_count": len(images)},
        ):
            return await self._observe_legacy_ocr(images, output_schema)

    async def _observe_raw_ocr_traced(
        self,
        provider: RawOCRProvider,
        images: Sequence[VisionImage],
        trace_id: str,
        tenant_id: str,
    ) -> RawOCRResult:
        with self._span(
            trace_id,
            tenant_id,
            TraceStage.OCR,
            "observe_raw_ocr",
            {"page_count": len(images)},
        ):
            return await self._observe_raw_ocr(provider, images)

    def _span(
        self,
        trace_id: str,
        tenant_id: str,
        stage: TraceStage,
        operation: str,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> AbstractContextManager[None]:
        if self._privacy_telemetry is None:
            return nullcontext()
        return self._privacy_telemetry.span(
            trace_id=trace_id,
            tenant_id=tenant_id,
            stage=stage,
            operation=operation,
            attributes=attributes,
        )

    async def _add_ocr_evidence(
        self,
        *,
        result: ExtractionResult[InvoiceT],
        legacy_ocr_task: asyncio.Task[tuple[OCRFieldObservation, ...]] | None,
        raw_ocr_tasks: Sequence[asyncio.Task[RawOCRResult]],
        source_ocr_task: asyncio.Task[RawOCRResult] | None = None,
        tenant_id: str,
        document_id: str,
        images: Sequence[VisionImage] = (),
        output_schema: type[InvoiceT] | None = None,
        prompt_context: VisionPromptContext | None = None,
        persist_artifacts: bool = True,
        targeted_reread_mode: Literal["off", "shadow", "apply"] | None = None,
    ) -> ExtractionResult[InvoiceT]:
        """Merge request-local OCR after Vision without changing source authority."""

        legacy_observations = result.ocr_observations
        if legacy_ocr_task is not None:
            legacy_observations = (
                *legacy_observations,
                *await legacy_ocr_task,
            )
        result = replace(result, ocr_observations=legacy_observations)

        if not raw_ocr_tasks and source_ocr_task is None:
            return result
        raw_results = tuple(await asyncio.gather(*raw_ocr_tasks))
        if source_ocr_task is not None:
            source_result = await source_ocr_task
            if source_result.status is OCRProviderStatus.AVAILABLE:
                raw_results = (*raw_results, source_result)
        if persist_artifacts and self._artifact_service is not None:
            try:
                await self._artifact_service.store_ocr(
                    tenant_id,
                    document_id,
                    raw_results,
                    uuid4().hex,
                )
            except Exception as exc:
                _LOGGER.warning(
                    "ocr_artifact_storage_degraded",
                    extra={"error_type": type(exc).__name__},
                )
        comparison_service = self._ocr_comparison_service
        facts = self._correction_scope_resolver.current_facts(result.invoice)
        if comparison_service is None:
            return replace(
                result,
                raw_ocr_observations=tuple(
                    observation
                    for raw_result in raw_results
                    for observation in raw_result.observations
                ),
            )
        if facts is None:
            return comparison_service.degrade(
                result=result,
                raw_results=raw_results,
                reason_code="ocr_comparison.document_type_unavailable",
            )
        try:
            compared = await comparison_service.compare(
                tenant_id=tenant_id,
                document_id=document_id,
                document_type=facts.document_type,
                result=result,
                raw_results=raw_results,
            )
            mode = targeted_reread_mode or self._targeted_reread_mode
            if (
                mode != "off"
                and output_schema is not None
                and prompt_context is not None
            ):
                compared = await self._targeted_reread(
                    compared, images, output_schema, prompt_context,
                    tenant_id, document_id, facts.document_type, raw_results,
                    comparison_service, mode,
                )
            return self._reconcile_current_evidence(compared)
        except Exception as exc:
            return comparison_service.degrade(
                result=result,
                raw_results=raw_results,
                reason_code=f"ocr_comparison.{type(exc).__name__.lower()}",
            )

    async def _targeted_reread(
        self,
        result: ExtractionResult[InvoiceT],
        images: Sequence[VisionImage],
        output_schema: type[InvoiceT],
        context: VisionPromptContext,
        tenant_id: str,
        document_id: str,
        document_type: str,
        raw_results: Sequence[RawOCRResult],
        comparison_service: DeterministicMultiSourceOCRComparisonService[InvoiceT],
        mode: Literal["off", "shadow", "apply"] | None = None,
    ) -> ExtractionResult[InvoiceT]:
        invoice = result.invoice
        if not isinstance(invoice, BaseModel) or not context.focus_field_paths:
            return result
        conflicting = (
            set(context.reviewed_examples.conflicting_field_paths)
            if context.reviewed_examples is not None else set()
        )
        for path in context.focus_field_paths:
            if path in conflicting or path not in type(invoice).model_fields:
                continue
            if getattr(invoice, path) is not None:
                continue
            comparisons = [
                item for item in result.ocr_comparisons
                if item.canonical_field_path == path
            ]
            observations = [
                item for item in result.ocr_observations if item.field_path == path
            ]
            if len(comparisons) != 1 or len(observations) != 1:
                continue
            comparison, ocr = comparisons[0], observations[0]
            if (
                comparison.outcome is not OCRComparisonOutcome.OCR_ONLY
                or comparison.conflicting_sources
                or "ocr_comparison.provider_partial_unavailable" in comparison.reason_codes
                or ocr.binding_status is not FieldBindingStatus.ACCEPTED
                or ocr.bounding_box is None
                or ocr.page_number is None
                or len(ocr.candidate_values) != 1
                or ocr.anomalies
            ):
                continue
            page = next((item for item in images if item.page_number == ocr.page_number), None)
            if page is None:
                continue
            try:
                crop = self._crop_ocr_region(page, ocr.bounding_box)
                focused = await self._provider.extract(
                    images=(crop,), output_schema=output_schema,
                    prompt_context=VisionPromptContext(
                        focus_field_paths=(path,),
                        field_semantic_catalog=context.field_semantic_catalog,
                        budget=context.budget,
                    ),
                    document_id=document_id,
                )
                value = getattr(focused.invoice, path) if focused.invoice is not None else None
                evidence = next(
                    (item for item in focused.field_evidence if item.field_path == path),
                    None,
                )
                if value is None or evidence is None or not self._reread_agrees(
                    path, value, evidence, ocr, type(invoice)
                ):
                    continue
                _LOGGER.info("targeted_reread_corrob", extra={"field_path": path})
                if (mode or self._targeted_reread_mode) == "shadow":
                    return result
                payload = invoice.model_dump(mode="python")
                payload[path] = value
                amended = replace(
                    result,
                    invoice=cast(InvoiceT, type(invoice).model_validate(payload)),
                    field_evidence=tuple(
                        replace(
                            item,
                            candidate_values=evidence.candidate_values,
                            page_number=evidence.page_number,
                            readability=evidence.readability,
                            ambiguous=False,
                            validation_signals=(
                                *evidence.validation_signals, "targeted_region_reread"
                            ),
                        ) if item.field_path == path else item
                        for item in result.field_evidence
                    ),
                )
                verified = await comparison_service.compare(
                    tenant_id=tenant_id, document_id=document_id,
                    document_type=document_type, result=amended,
                    raw_results=raw_results,
                )
                reread_comparison = next(
                    (item for item in verified.ocr_comparisons
                     if item.canonical_field_path == path),
                    None,
                )
                if (
                    reread_comparison is None
                    or reread_comparison.outcome is not OCRComparisonOutcome.CORROBORATED
                    or reread_comparison.review_required
                    or reread_comparison.conflicting_sources
                ):
                    return result
                return verified
            except Exception as exc:
                _LOGGER.warning(
                    "targeted_reread_degraded",
                    extra={"field_path": path, "error_type": type(exc).__name__},
                )
                return result
        return result

    @staticmethod
    def _crop_ocr_region(
        page: VisionImage, box: tuple[float, float, float, float]
    ) -> VisionImage:
        left, top, right, bottom = box
        if right <= left or bottom <= top:
            raise ValueError("OCR region has no area")
        with Image.open(BytesIO(page.content)) as image:
            if image.size != (page.width, page.height):
                raise ValueError("OCR region image dimensions do not match")
            if left < 0 or top < 0 or right > page.width or bottom > page.height:
                raise ValueError("OCR region is outside the current page")
            margin = max(32, min(128, int(max(right - left, bottom - top))))
            bounds = (
                max(0, int(left) - margin), max(0, int(top) - margin),
                min(page.width, int(right) + margin),
                min(page.height, int(bottom) + margin),
            )
            if bounds[2] - bounds[0] < 32 or bounds[3] - bounds[1] < 32:
                raise ValueError("OCR region is outside the current page")
            if (
                (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
                > page.width * page.height * 0.5
            ):
                raise ValueError("OCR region is not sufficiently localized")
            region = image.crop(bounds)
            with BytesIO() as output:
                region.save(output, format="PNG")
                return VisionImage(
                    content=output.getvalue(), mime_type="image/png",
                    page_number=page.page_number,
                    width=region.width, height=region.height,
                )

    @staticmethod
    def _reread_agrees(
        path: str,
        value: object,
        evidence: object,
        ocr: OCRFieldObservation,
        invoice_type: type[BaseModel],
    ) -> bool:
        if (
            not isinstance(evidence, FieldEvidence)
            or evidence.source is not EvidenceSource.VISUAL
            or evidence.readability is not Readability.READABLE
            or evidence.ambiguous
            or evidence.page_number != ocr.page_number
            or len(evidence.candidate_values) != 1
        ):
            return False
        adapter: TypeAdapter[object] = TypeAdapter(invoice_type.model_fields[path].annotation)
        try:
            visual = adapter.validate_python(evidence.candidate_values[0])
            observed = adapter.validate_python(ocr.candidate_values[0])
        except (TypeError, ValueError, ValidationError):
            return False
        if visual != value:
            return False
        if isinstance(value, str):
            return normalize_ocr_text(value).casefold() == normalize_ocr_text(
                str(observed)
            ).casefold()
        return observed == value

    @staticmethod
    def _reconcile_current_evidence(
        result: ExtractionResult[InvoiceT],
    ) -> ExtractionResult[InvoiceT]:
        invoice = result.invoice
        if not isinstance(invoice, BaseModel):
            return result
        comparisons = {
            item.canonical_field_path: item for item in result.ocr_comparisons
        }
        observations: dict[str, list[OCRFieldObservation]] = {}
        for item in result.ocr_observations:
            observations.setdefault(item.field_path, []).append(item)
        updates: dict[str, object] = {}
        reconciled_paths: set[str] = set()
        for evidence in result.field_evidence:
            path = evidence.field_path
            if (
                path not in type(invoice).model_fields
                or getattr(invoice, path) is not None
                or evidence.source is not EvidenceSource.VISUAL
                or evidence.readability is not Readability.READABLE
                or evidence.ambiguous
                or evidence.page_number is None
                or len(evidence.candidate_values) != 1
            ):
                continue
            candidate = evidence.candidate_values[0].strip()
            if not candidate or candidate.casefold() in {"null", "none", "n/a"}:
                continue
            comparison = comparisons.get(path)
            if (
                comparison is None
                or comparison.outcome is not OCRComparisonOutcome.CORROBORATED
                or comparison.review_required
                or comparison.conflicting_sources
                or len(comparison.vision_candidates) != 1
                or len(comparison.ocr_candidates) != 1
                or "ocr_comparison.provider_partial_unavailable" in comparison.reason_codes
            ):
                continue
            bound = observations.get(path, [])
            if len(bound) != 1:
                continue
            ocr = bound[0]
            if (
                ocr.binding_status is not FieldBindingStatus.ACCEPTED
                or ocr.page_number != evidence.page_number
                or ocr.bounding_box is None
                or len(ocr.candidate_values) != 1
                or ocr.anomalies
            ):
                continue
            adapter: TypeAdapter[object] = TypeAdapter(type(invoice).model_fields[path].annotation)
            try:
                value = adapter.validate_python(candidate)
                ocr_value = adapter.validate_python(ocr.candidate_values[0])
            except (TypeError, ValueError, ValidationError):
                continue
            if value is None or ocr_value is None:
                continue
            if isinstance(value, str):
                if normalize_ocr_text(value).casefold() != normalize_ocr_text(
                    str(ocr_value)
                ).casefold():
                    continue
            elif value != ocr_value:
                continue
            updates[path] = value
            reconciled_paths.add(path)
        if not updates:
            return result
        try:
            payload = invoice.model_dump(mode="python")
            payload.update(updates)
            corrected = type(invoice).model_validate(payload)
        except ValidationError:
            return result
        return replace(
            result,
            invoice=cast(InvoiceT, corrected),
            field_evidence=tuple(
                replace(
                    item,
                    validation_signals=(*item.validation_signals, "dual_source_reconciled"),
                )
                if item.field_path in reconciled_paths
                else item
                for item in result.field_evidence
            ),
        )

    async def _observe_legacy_ocr(
        self,
        images: Sequence[VisionImage],
        output_schema: type[InvoiceT],
    ) -> tuple[OCRFieldObservation, ...]:
        provider = self._ocr_provider
        if provider is None:
            return ()
        try:
            return await provider.observe(images, output_schema)
        except Exception:
            return ()

    async def _observe_raw_ocr(
        self,
        provider: RawOCRProvider,
        images: Sequence[VisionImage],
    ) -> RawOCRResult:
        try:
            return await provider.observe_raw(images)
        except Exception as exc:
            return RawOCRResult(
                status=OCRProviderStatus.UNAVAILABLE,
                anomalies=(f"ocr_{type(exc).__name__.lower()}",),
            )

    async def _bind_field_labels(
        self,
        result: ExtractionResult[InvoiceT],
        tenant_id: str,
    ) -> ExtractionResult[InvoiceT]:
        binding_service = self._field_semantic_binding_service
        if binding_service is None or not result.field_binding_evidence:
            return result
        facts = self._correction_scope_resolver.current_facts(result.invoice)
        if facts is None:
            return result

        bound: list[FieldBindingEvidence] = []
        anomalies = list(result.anomalies)
        runtime_field_paths = tuple(sorted(facts.field_values))
        for evidence in result.field_binding_evidence:
            try:
                decision = await binding_service.bind(
                    FieldSemanticBindingRequest(
                        tenant_id=tenant_id,
                        document_type=facts.document_type,
                        schema_version=self._schema_version,
                        evidence=replace(
                            evidence,
                            candidate_field_paths=runtime_field_paths,
                        ),
                    )
                )
                bound.append(
                    replace(
                        evidence,
                        candidate_field_paths=tuple(
                            item.canonical_field_path for item in decision.candidates
                        ),
                        binding_decision=decision.to_summary(),
                    )
                )
            except Exception as exc:
                _LOGGER.warning(
                    "field_semantic_binding_failed",
                    extra={
                        "tenant_id": tenant_id,
                        "document_id": evidence.document_id,
                        "evidence_id": evidence.evidence_id,
                        "error_type": type(exc).__name__,
                    },
                )
                bound.append(evidence)
                anomalies.append(
                    ExtractionAnomaly(
                        code="field_binding_unavailable",
                        message="字段语义绑定暂不可用，必须人工确认字段映射。",
                        field_path=None,
                        page_number=evidence.page_number,
                    )
                )
        return replace(
            result,
            field_binding_evidence=tuple(bound),
            anomalies=tuple(anomalies),
        )
