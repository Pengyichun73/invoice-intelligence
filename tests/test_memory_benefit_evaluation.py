"""Isolated paired evaluation safety checks."""

from dataclasses import dataclass

import pytest

from invoice_intelligence.application.services.memory_benefit_evaluation import (
    FrozenInvoiceGold,
    MemoryBenefitEvaluationService,
)
from invoice_intelligence.domain.document import DocumentReference
from invoice_intelligence.domain.examples import (
    ExampleLabelType,
    IndexVersion,
    RetrievalPolicyVersion,
    ReviewedExamplePromptContext,
    ReviewedExamplePromptReference,
)
from invoice_intelligence.domain.extraction import ExtractionResult, OCRComparisonOutcome
from invoice_intelligence.domain.field_semantics import FieldSemanticCatalogVersion
from invoice_intelligence.domain.invoice import InvoiceExtraction
from invoice_intelligence.domain.workflow import (
    FieldDecision,
    ValidationOutcome,
    ValidationRoute,
)

VERSIONS = {
    "schema_version": "3.0.0",
    "model_version": "test-model",
    "prompt_version": "test-prompt",
    "catalog_version": "test-catalog",
    "index_version": "field-pattern-v1-test",
}


def _gold() -> FrozenInvoiceGold:
    values = dict.fromkeys(InvoiceExtraction.model_fields)
    values["invoice_number"] = "PRIVATE-INVOICE-ANSWER"
    fields = {
        path: (
            {
                "state": "present", "value": value, "page_number": 1,
                "bounding_box": [0, 0, 10, 10], "observed_text": "PRIVATE-TEXT",
            }
            if value is not None else {"state": "absent", "value": None}
        )
        for path, value in values.items()
    }
    document = DocumentReference("doc", "isolated://doc", "image/png", "a" * 64)
    return FrozenInvoiceGold.from_row({
        "case_id": "case-1", "document_checksum": document.checksum,
        "template_group": "group-1", "gold_complete": True,
        "fields": fields, **VERSIONS,
    }, document)


@dataclass
class _Comparison:
    outcome: OCRComparisonOutcome = OCRComparisonOutcome.CORROBORATED
    reason_codes: tuple[str, ...] = ()


class _Extraction:
    def __init__(self, *, ocr_healthy: bool = True) -> None:
        self.calls: list[dict[str, object]] = []
        self.ocr_healthy = ocr_healthy

    async def extract(self, document, schema, tenant_id, prompt_context=None, **options):
        self.calls.append({"prompt_context": prompt_context, **options})
        return ExtractionResult(
            invoice=_gold().invoice,
            field_evidence=(), anomalies=(),
            raw_ocr_observations=(object(),) if options["include_ocr"] and self.ocr_healthy else (),
            ocr_comparisons=(_Comparison(),) if options["include_ocr"] and self.ocr_healthy else (),
        )


class _Validator:
    def validate(self, result):
        return ValidationOutcome(
            route=ValidationRoute.REVIEW_REQUIRED,
            field_decisions=tuple(
                FieldDecision(
                    field_path=path, current_value=None, candidate_values=(),
                    signals=(), score=0.0,
                    route=(ValidationRoute.REVIEW_REQUIRED if path == "invoice_number"
                           else ValidationRoute.ACCEPTED),
                    user_action=None,
                )
                for path in InvoiceExtraction.model_fields
            ),
            issues=(),
        )


class _Retrieval:
    def __init__(self, version: str = VERSIONS["index_version"]):
        self.version = version
        self.calls: list[bool] = []

    async def active_index_version(self, tenant_id):
        return IndexVersion(self.version)

    async def retrieve_for_extraction(
        self, tenant_id, invoice, evidence, *, persist_telemetry, fail_on_error
    ):
        self.calls.append(persist_telemetry)
        assert fail_on_error is True
        reference = ReviewedExamplePromptReference(
            example_id="example-1", document_type="invoice",
            field_path="invoice_number", schema_version="3.0.0",
            label_type=ExampleLabelType.CONFIRMED_CORRECT,
            model_value=None, reviewed_value=None, correction_reason=None,
            index_version=IndexVersion(self.version),
        )
        return ReviewedExamplePromptContext(
            trace_ids=(), verified_correct_examples=(reference,),
            reviewed_correction_examples=(), reviewed_negative_examples=(),
            conflicting_field_paths=(), retrieval_policy_version=RetrievalPolicyVersion("test"),
        )


class _Leakage:
    def __init__(self, found: bool = False) -> None:
        self.found = found

    async def has_indexed_source(self, tenant_id, index_version, document_checksum):
        return self.found


class _Catalog:
    def __init__(self, version: str = VERSIONS["catalog_version"]) -> None:
        self.version = version

    async def list_definitions(self, tenant_id):
        return (type("Definition", (), {
            "catalog_version": FieldSemanticCatalogVersion(self.version),
        })(),)


RUNTIME_VERSIONS = {
    key: VERSIONS[key] for key in ("schema_version", "model_version", "prompt_version")
}


@pytest.mark.asyncio
async def test_three_variants_use_real_extraction_without_persistent_artifacts():
    extraction, retrieval = _Extraction(), _Retrieval()
    service = MemoryBenefitEvaluationService(
        extraction_service=extraction, validator=_Validator(),
        example_retrieval=retrieval, leakage_repository=_Leakage(),
        field_semantic_catalog=_Catalog(), runtime_versions=RUNTIME_VERSIONS,
    )
    rows = await service.run_case(tenant_id="isolated-tenant", gold=_gold())
    assert [row["variant"] for row in rows] == [
        "vision", "vision_ocr", "vision_ocr_memory"
    ]
    assert [call["include_ocr"] for call in extraction.calls] == [False, True, True]
    assert [call["targeted_reread_mode"] for call in extraction.calls] == [
        "off", "off", "apply"
    ]
    assert all(call["persist_artifacts"] is False for call in extraction.calls)
    assert retrieval.calls == [False]
    assert rows[2]["retrieved_example_ids"] == ["example-1"]
    assert rows[1]["image_quality"] == "unknown"
    assert rows[1]["ocr_status"] == "available"
    assert rows[1]["error_category"] == "none"
    assert "PRIVATE-INVOICE-ANSWER" not in str(rows)
    assert "PRIVATE-TEXT" not in str(rows)


@pytest.mark.asyncio
async def test_ocr_failure_rejects_paired_result():
    extraction = _Extraction(ocr_healthy=False)
    service = MemoryBenefitEvaluationService(
        extraction_service=extraction, validator=_Validator(),
        example_retrieval=_Retrieval(), leakage_repository=_Leakage(),
        field_semantic_catalog=_Catalog(), runtime_versions=RUNTIME_VERSIONS,
    )
    with pytest.raises(ValueError, match="OCR is unavailable"):
        await service.run_case(tenant_id="isolated-tenant", gold=_gold())
    assert len(extraction.calls) == 2


@pytest.mark.asyncio
async def test_index_mismatch_stops_before_provider_calls():
    extraction = _Extraction()
    service = MemoryBenefitEvaluationService(
        extraction_service=extraction, validator=_Validator(),
        example_retrieval=_Retrieval("other-index"), leakage_repository=_Leakage(),
        field_semantic_catalog=_Catalog(), runtime_versions=RUNTIME_VERSIONS,
    )
    with pytest.raises(ValueError, match="Active index version"):
        await service.run_case(tenant_id="isolated-tenant", gold=_gold())
    assert extraction.calls == []


def test_gold_requires_current_image_evidence_and_runtime_versions():
    gold = _gold()
    with pytest.raises(ValueError, match="runtime version"):
        MemoryBenefitEvaluationService(
            extraction_service=_Extraction(), validator=_Validator(),
            example_retrieval=_Retrieval(), leakage_repository=_Leakage(),
            field_semantic_catalog=_Catalog(),
            runtime_versions={"schema_version": "3.0.0"},
        )
    assert gold.invoice.invoice_number == "PRIVATE-INVOICE-ANSWER"


@pytest.mark.asyncio
async def test_same_checksum_index_leak_stops_before_provider_calls():
    extraction = _Extraction()
    service = MemoryBenefitEvaluationService(
        extraction_service=extraction, validator=_Validator(),
        example_retrieval=_Retrieval(), leakage_repository=_Leakage(True),
        field_semantic_catalog=_Catalog(), runtime_versions=RUNTIME_VERSIONS,
    )
    with pytest.raises(ValueError, match="tested document checksum"):
        await service.run_case(tenant_id="isolated-tenant", gold=_gold())
    assert extraction.calls == []


@pytest.mark.asyncio
async def test_catalog_mismatch_stops_before_provider_calls():
    extraction = _Extraction()
    service = MemoryBenefitEvaluationService(
        extraction_service=extraction, validator=_Validator(),
        example_retrieval=_Retrieval(), leakage_repository=_Leakage(),
        field_semantic_catalog=_Catalog("other-catalog"),
        runtime_versions=RUNTIME_VERSIONS,
    )
    with pytest.raises(ValueError, match="Active catalog version"):
        await service.run_case(tenant_id="isolated-tenant", gold=_gold())
    assert extraction.calls == []
