"""Read-only paired extraction evaluation against independent invoice gold."""

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from invoice_intelligence.application.services.example_retrieval import (
    HybridExampleRetrievalService,
)
from invoice_intelligence.application.services.extraction_validation import (
    EvidenceBasedExtractionValidator,
)
from invoice_intelligence.application.services.vision_extraction import VisionExtractionService
from invoice_intelligence.domain.document import DocumentReference
from invoice_intelligence.domain.examples import (
    ReviewedExamplePromptContext,
    ReviewedExamplePromptReference,
)
from invoice_intelligence.domain.extraction import (
    ExtractionResult,
    OCRComparisonOutcome,
    VisionPromptContext,
)
from invoice_intelligence.domain.invoice import InvoiceExtraction
from invoice_intelligence.domain.workflow import ValidationRoute

FIELD_PATHS = frozenset(InvoiceExtraction.model_fields)
VERSION_KEYS = (
    "schema_version", "model_version", "prompt_version",
    "catalog_version", "index_version",
)


@dataclass(frozen=True, slots=True)
class FrozenInvoiceGold:
    """Complete independent truth and an isolated reference to the same source bytes."""

    case_id: str
    document: DocumentReference
    template_group: str
    invoice: InvoiceExtraction
    versions: dict[str, str]

    @classmethod
    def from_row(
        cls, row: dict[str, Any], document: DocumentReference
    ) -> "FrozenInvoiceGold":
        if row.get("gold_complete") is not True:
            raise ValueError("Gold case must be independently complete")
        if row.get("document_checksum") != document.checksum:
            raise ValueError("Gold case document checksum does not match evidence")
        fields = row.get("fields")
        if not isinstance(fields, dict) or set(fields) != FIELD_PATHS:
            raise ValueError("Gold case must contain exactly 19 fields")
        case_id, template = row.get("case_id"), row.get("template_group")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("Gold case identifier is missing")
        if not isinstance(template, str) or not template.strip():
            raise ValueError("Gold case template group is missing")
        versions: dict[str, str] = {}
        for key in VERSION_KEYS:
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Gold case has incomplete version bindings")
            versions[key] = value
        values: dict[str, object] = {}
        for path, item in fields.items():
            if not isinstance(item, dict) or item.get("state") not in {
                "present", "absent", "unreadable"
            }:
                raise ValueError("Gold case has an invalid field state")
            value = item.get("value")
            if (item["state"] == "present") != (value is not None):
                raise ValueError("Gold case field state conflicts with value")
            if item["state"] == "present":
                page, box, observed = (
                    item.get("page_number"), item.get("bounding_box"),
                    item.get("observed_text"),
                )
                if (
                    type(page) is not int or page <= 0
                    or not isinstance(box, list) or len(box) != 4
                    or any(type(number) not in {int, float} for number in box)
                    or any(not math.isfinite(number) for number in box)
                    or box[0] < 0 or box[1] < 0
                    or box[2] <= box[0] or box[3] <= box[1]
                    or not isinstance(observed, str) or not observed.strip()
                ):
                    raise ValueError("Gold case present field lacks image evidence")
            values[path] = value
        return cls(
            case_id=case_id,
            document=document,
            template_group=template,
            invoice=InvoiceExtraction.model_validate(values),
            versions=versions,
        )


class MemoryBenefitEvaluationService:
    """Run three real extraction paths without persisting artifacts or retrieval traces."""

    def __init__(
        self,
        *,
        extraction_service: VisionExtractionService[InvoiceExtraction],
        validator: EvidenceBasedExtractionValidator,
        example_retrieval: HybridExampleRetrievalService,
        runtime_versions: Mapping[str, str],
    ) -> None:
        if set(runtime_versions) != set(VERSION_KEYS) or any(
            not value.strip() for value in runtime_versions.values()
        ):
            raise ValueError("Evaluation requires all runtime version bindings")
        self._extraction = extraction_service
        self._validator = validator
        self._retrieval = example_retrieval
        self._runtime_versions = dict(runtime_versions)

    async def run_case(
        self, *, tenant_id: str, gold: FrozenInvoiceGold
    ) -> tuple[dict[str, object], ...]:
        if not tenant_id.strip() or tenant_id != tenant_id.strip():
            raise ValueError("Evaluation requires a trusted tenant scope")
        if gold.versions["schema_version"] != "3.0.0":
            raise ValueError("Gold case Schema version is not supported")
        if not gold.versions["index_version"].startswith("field-pattern-v1-"):
            raise ValueError("Paired evaluation requires a value-blind index")
        if gold.versions != self._runtime_versions:
            raise ValueError("Runtime versions differ from frozen gold")
        active_index = await self._retrieval.active_index_version(tenant_id)
        if active_index is None or active_index.value != gold.versions["index_version"]:
            raise ValueError("Active index version differs from frozen gold")

        started = time.perf_counter()
        vision = await self._extraction.extract(
            gold.document, InvoiceExtraction, tenant_id,
            include_ocr=False, persist_artifacts=False,
            targeted_reread_mode="off",
        )
        vision_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        vision_ocr = await self._extraction.extract(
            gold.document, InvoiceExtraction, tenant_id,
            include_ocr=True, persist_artifacts=False,
            targeted_reread_mode="off",
        )
        ocr_ms = (time.perf_counter() - started) * 1000
        self._require_ocr(vision_ocr)

        started = time.perf_counter()
        reviewed = await self._retrieval.retrieve_for_extraction(
            tenant_id, vision_ocr.invoice, vision_ocr.field_evidence,
            persist_telemetry=False,
            fail_on_error=True,
        )
        focus = self._focus_paths(vision_ocr, reviewed)
        retrieved_ids = self._retrieved_ids(reviewed)
        if reviewed is not None and any(
            reference.index_version.value != gold.versions["index_version"]
            for reference in self._references(reviewed)
        ):
            raise ValueError("Retrieved case index version differs from frozen gold")
        if focus:
            memory = await self._extraction.extract(
                gold.document, InvoiceExtraction, tenant_id,
                prompt_context=VisionPromptContext(
                    reviewed_examples=reviewed,
                    focus_field_paths=focus,
                ),
                include_ocr=True, persist_artifacts=False,
                targeted_reread_mode="apply",
            )
            self._require_ocr(memory)
        else:
            memory = vision_ocr
        memory_ms = ocr_ms + (time.perf_counter() - started) * 1000

        return (
            self._judgments(gold, "vision", vision, vision_ms, ()),
            self._judgments(gold, "vision_ocr", vision_ocr, ocr_ms, ()),
            self._judgments(
                gold, "vision_ocr_memory", memory, memory_ms, retrieved_ids
            ),
        )

    @staticmethod
    def _require_ocr(result: ExtractionResult[InvoiceExtraction]) -> None:
        if (
            not result.raw_ocr_observations
            or not result.ocr_comparisons
            or any(
                item.outcome is OCRComparisonOutcome.UNAVAILABLE
                or "ocr_comparison.provider_partial_unavailable" in item.reason_codes
                for item in result.ocr_comparisons
            )
        ):
            raise ValueError("OCR is unavailable; paired benefit evaluation is invalid")

    def _focus_paths(
        self,
        baseline: ExtractionResult[InvoiceExtraction],
        reviewed: ReviewedExamplePromptContext | None,
    ) -> tuple[str, ...]:
        if reviewed is None:
            return ()
        review_paths = {
            item.field_path for item in self._validator.validate(baseline).field_decisions
            if item.route is ValidationRoute.REVIEW_REQUIRED and item.field_path is not None
        }
        case_paths = {item.field_path for item in self._references(reviewed)}
        return tuple(sorted(review_paths & case_paths))

    @staticmethod
    def _references(
        reviewed: ReviewedExamplePromptContext,
    ) -> tuple[ReviewedExamplePromptReference, ...]:
        return (
            *reviewed.verified_correct_examples,
            *reviewed.reviewed_correction_examples,
            *reviewed.reviewed_negative_examples,
        )

    @classmethod
    def _retrieved_ids(cls, reviewed: ReviewedExamplePromptContext | None) -> tuple[str, ...]:
        return tuple(sorted({
            item.example_id for item in cls._references(reviewed)
        })) if reviewed is not None else ()

    def _judgments(
        self,
        gold: FrozenInvoiceGold,
        variant: str,
        result: ExtractionResult[InvoiceExtraction],
        elapsed_ms: float,
        retrieved_ids: tuple[str, ...],
    ) -> dict[str, object]:
        invoice = result.invoice
        if not isinstance(invoice, InvoiceExtraction):
            raise ValueError("Variant has no complete InvoiceExtraction result")
        decisions = {
            item.field_path: item
            for item in self._validator.validate(result).field_decisions
            if item.field_path is not None
        }
        if set(decisions) != FIELD_PATHS:
            raise ValueError("Variant has incomplete field-level decisions")
        actual = invoice.model_dump(mode="json")
        expected = gold.invoice.model_dump(mode="json")
        evidence = {item.field_path: item for item in result.field_evidence}
        return {
            "case_id": gold.case_id,
            "variant": variant,
            "gold_complete": True,
            "document_checksum": gold.document.checksum,
            "template_group": gold.template_group,
            **gold.versions,
            "ocr_healthy": variant == "vision" or bool(result.raw_ocr_observations),
            "elapsed_ms": round(elapsed_ms, 3),
            "retrieved_example_ids": list(retrieved_ids),
            "fields": {
                path: {
                    "correct": actual[path] == expected[path],
                    "review_required": decisions[path].route is not ValidationRoute.ACCEPTED,
                    "auto_accepted": decisions[path].route is ValidationRoute.ACCEPTED,
                    "evidence_page_number": (
                        evidence[path].page_number if path in evidence else None
                    ),
                }
                for path in sorted(FIELD_PATHS)
            },
        }
