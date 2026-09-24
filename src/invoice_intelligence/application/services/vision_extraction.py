"""Vision extraction orchestration use case."""

import asyncio
import logging
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from hashlib import sha256
from hmac import compare_digest
from typing import Generic, TypeVar
from uuid import uuid4

from invoice_intelligence.application.errors import DocumentIntegrityError
from invoice_intelligence.application.ports.document_processor import DocumentProcessor
from invoice_intelligence.application.ports.file_storage import FileStorage
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
)
from invoice_intelligence.domain.document import (
    DocumentProcessingLimits,
    DocumentReference,
    UploadDocument,
    VisionImage,
)
from invoice_intelligence.domain.extraction import (
    ExtractionAnomaly,
    ExtractionResult,
    OCRFieldObservation,
    OCRProviderStatus,
    PromptContextBudget,
    RawOCRResult,
    VisionPromptContext,
)
from invoice_intelligence.domain.field_semantics import FieldBindingEvidence

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

    async def extract(
        self,
        document: DocumentReference,
        output_schema: type[InvoiceT],
        tenant_id: str,
        prompt_context: VisionPromptContext | None = None,
        trace_id: str | None = None,
    ) -> ExtractionResult[InvoiceT]:
        """Verify stored bytes, normalize pages, and request structured extraction."""

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
            images = await self._document_processor.to_vision_images(
                content=content,
                inspected=inspected,
                limits=self._limits,
            )
            if self._artifact_service is not None:
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
            if self._ocr_provider is not None
            else None
        )
        raw_ocr_tasks = tuple(
            asyncio.create_task(
                self._observe_raw_ocr_traced(
                    provider, images, resolved_trace_id, tenant_id
                )
            )
            for provider in self._raw_ocr_providers
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
                legacy_ocr_task=legacy_ocr_task,
                raw_ocr_tasks=raw_ocr_tasks,
                tenant_id=tenant_id,
                document_id=document.document_id,
            )
        except BaseException:
            for task in ocr_tasks:
                task.cancel()
            await asyncio.gather(*ocr_tasks, return_exceptions=True)
            raise

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
        tenant_id: str,
        document_id: str,
    ) -> ExtractionResult[InvoiceT]:
        """Merge request-local OCR after Vision without changing source authority."""

        legacy_observations = result.ocr_observations
        if legacy_ocr_task is not None:
            legacy_observations = (
                *legacy_observations,
                *await legacy_ocr_task,
            )
        result = replace(result, ocr_observations=legacy_observations)

        if not raw_ocr_tasks:
            return result
        raw_results = tuple(await asyncio.gather(*raw_ocr_tasks))
        if self._artifact_service is not None:
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
            return await comparison_service.compare(
                tenant_id=tenant_id,
                document_id=document_id,
                document_type=facts.document_type,
                result=result,
                raw_results=raw_results,
            )
        except Exception as exc:
            return comparison_service.degrade(
                result=result,
                raw_results=raw_results,
                reason_code=f"ocr_comparison.{type(exc).__name__.lower()}",
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
