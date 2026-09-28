from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from invoice_intelligence.application.errors import VisionExtractionError
from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.examples import (
    ExampleLabelType,
    IndexVersion,
    RetrievalPolicyVersion,
    ReviewedExamplePromptContext,
    ReviewedExamplePromptReference,
)
from invoice_intelligence.domain.extraction import VisionPromptContext
from invoice_intelligence.domain.invoice import InvoiceExtraction
from invoice_intelligence.infrastructure.vision.structured import (
    build_extraction_result,
    build_vision_response_model,
    compile_prompt_context_sections,
    schema_retry_instruction,
    validation_error_diagnostics,
    vision_error_diagnostics,
)


def test_reviewed_prompt_context_never_contains_prior_invoice_values() -> None:
    reference = ReviewedExamplePromptReference(
        example_id="example-1",
        document_type="invoice",
        field_path="invoice_number",
        schema_version="3.0.0",
        label_type=ExampleLabelType.CORRECTED,
        model_value="PRIOR-ERROR-999",
        reviewed_value="PRIOR-TRUTH-999",
        correction_reason="previous value PRIOR-TRUTH-999",
        index_version=IndexVersion("legacy-v1"),
    )
    reviewed = ReviewedExamplePromptContext(
        trace_ids=("trace-1",),
        verified_correct_examples=(),
        reviewed_correction_examples=(reference,),
        reviewed_negative_examples=(),
        conflicting_field_paths=(),
        retrieval_policy_version=RetrievalPolicyVersion("v1"),
    )
    sections = compile_prompt_context_sections(
        VisionPromptContext(reviewed_examples=reviewed)
    )
    payload = "\n".join(section for _, section in sections)
    assert "invoice_number" in payload
    assert "PRIOR-ERROR-999" not in payload
    assert "PRIOR-TRUTH-999" not in payload
    assert "correction_reason" not in payload


def _invoice_payload() -> dict[str, object | None]:
    return {field_path: None for field_path in InvoiceExtraction.model_fields}


def _evidence(field_path: str) -> dict[str, object]:
    return {
        "field_path": field_path,
        "source": "visual",
        "page_number": None,
        "candidate_values": [],
        "readability": "missing",
        "validation_signals": ["not_visible"],
        "ambiguous": False,
    }


def _response_payload(paths: list[str]) -> dict[str, Any]:
    return {
        "invoice": _invoice_payload(),
        "evidence": [_evidence(path) for path in paths],
        "anomalies": [],
        "field_binding_evidence": [],
    }


def _image() -> VisionImage:
    return VisionImage(
        content=b"not-used-by-contract-validation",
        mime_type="image/png",
        page_number=1,
        width=1000,
        height=1000,
    )


def test_complete_null_invoice_has_exact_evidence_coverage() -> None:
    response_model = build_vision_response_model(InvoiceExtraction)
    field_paths = list(InvoiceExtraction.model_fields)
    parsed = response_model.model_validate(_response_payload(field_paths))

    result = build_extraction_result(
        parsed,
        (_image(),),
        document_id="document-1",
        provider_name="test",
    )

    assert result.invoice == InvoiceExtraction.model_validate(_invoice_payload())
    assert {item.field_path for item in result.field_evidence} == set(field_paths)


def test_missing_evidence_path_is_rejected_with_safe_diagnostic() -> None:
    response_model = build_vision_response_model(InvoiceExtraction)
    field_paths = list(InvoiceExtraction.model_fields)
    parsed = response_model.model_validate(_response_payload(field_paths[1:]))

    with pytest.raises(
        VisionExtractionError,
        match=r"^coverage:missing=invoice_unique_code$",
    ):
        build_extraction_result(
            parsed,
            (_image(),),
            document_id="document-1",
            provider_name="test",
        )


def test_duplicate_evidence_path_is_rejected_with_safe_diagnostic() -> None:
    response_model = build_vision_response_model(InvoiceExtraction)
    field_paths = list(InvoiceExtraction.model_fields)
    parsed = response_model.model_validate(
        _response_payload([*field_paths, field_paths[0]])
    )

    with pytest.raises(
        VisionExtractionError,
        match=r"^coverage:duplicate=invoice_unique_code$",
    ):
        build_extraction_result(
            parsed,
            (_image(),),
            document_id="document-1",
            provider_name="test",
        )


def test_extra_evidence_path_is_rejected_with_safe_diagnostic() -> None:
    response_model = build_vision_response_model(InvoiceExtraction)
    field_paths = list(InvoiceExtraction.model_fields)
    parsed = response_model.model_validate(
        _response_payload([*field_paths, "invoice_code"])
    )

    with pytest.raises(
        VisionExtractionError,
        match=r"^coverage:extra=invoice_code$",
    ):
        build_extraction_result(
            parsed,
            (_image(),),
            document_id="document-1",
            provider_name="test",
        )


def test_invalid_evidence_path_is_redacted_from_coverage_diagnostic() -> None:
    response_model = build_vision_response_model(InvoiceExtraction)
    field_paths = list(InvoiceExtraction.model_fields)
    parsed = response_model.model_validate(
        _response_payload([*field_paths, "sensitive value"])
    )

    with pytest.raises(VisionExtractionError, match=r"^coverage:extra=invalid_path$") as error:
        build_extraction_result(
            parsed,
            (_image(),),
            document_id="document-1",
            provider_name="test",
        )

    assert "sensitive value" not in str(error.value)


def test_coverage_reports_missing_extra_and_duplicate_in_one_failure() -> None:
    response_model = build_vision_response_model(InvoiceExtraction)
    field_paths = list(InvoiceExtraction.model_fields)
    parsed = response_model.model_validate(
        _response_payload([*field_paths[1:], "invoice_code", field_paths[1]])
    )

    with pytest.raises(VisionExtractionError) as captured:
        build_extraction_result(
            parsed,
            (_image(),),
            document_id="document-1",
            provider_name="test",
        )

    assert vision_error_diagnostics(captured.value) == (
        "coverage:missing=invoice_unique_code",
        "coverage:extra=invoice_code",
        "coverage:duplicate=company_name",
    )


def test_pydantic_diagnostics_classify_missing_extra_and_type_without_values() -> None:
    response_model = build_vision_response_model(InvoiceExtraction)
    payload = _response_payload(list(InvoiceExtraction.model_fields))
    invoice = payload["invoice"]
    assert isinstance(invoice, dict)
    invoice.pop("po_number")
    invoice["invoice_code"] = "must-not-leak"
    invoice["invoice_total_amount"] = "not-a-decimal"

    with pytest.raises(ValidationError) as captured:
        response_model.model_validate(payload)

    diagnostics = validation_error_diagnostics(captured.value)
    rendered = ";".join(diagnostics)
    assert "schema:missing=invoice.po_number" in diagnostics
    assert "schema:extra=invoice.invoice_code" in diagnostics
    assert "schema:type=invoice.invoice_total_amount" in rendered
    assert "must-not-leak" not in rendered
    assert "not-a-decimal" not in rendered


def test_current_failure_shape_reports_old_branch_fields_without_values() -> None:
    response_model = build_vision_response_model(InvoiceExtraction)
    payload = _response_payload(list(InvoiceExtraction.model_fields))
    invoice = payload["invoice"]
    assert isinstance(invoice, dict)
    for field_path in (
        "po_number",
        "bookkeeping_datetime",
        "attribute_1",
        "currency",
        "is_seal",
        "attribute_2",
    ):
        invoice.pop(field_path)
    invoice.update(
        {
            "invoice_code": "must-not-leak",
            "invoice_type_desc": "must-not-leak",
            "buyer_tax_number": "must-not-leak",
            "item_tax_rate": "not-a-decimal",
        }
    )

    with pytest.raises(ValidationError) as captured:
        response_model.model_validate(payload)

    diagnostics = validation_error_diagnostics(captured.value)
    rendered = ";".join(diagnostics)
    assert "schema:missing=invoice.po_number" in diagnostics
    assert "schema:missing=invoice.currency" in diagnostics
    assert "schema:extra=invoice.invoice_code" in diagnostics
    assert "schema:extra=invoice.item_tax_rate" in diagnostics
    assert "must-not-leak" not in rendered
    assert "not-a-decimal" not in rendered


def test_retry_instruction_separates_schema_and_coverage_failures() -> None:
    instruction = schema_retry_instruction(
        (
            "schema:missing=invoice.po_number",
            "schema:extra=invoice.invoice_code",
            "schema:type=invoice.invoice_total_amount:decimal_parsing",
            "coverage:missing=currency",
            "coverage:extra=invoice_code",
            "coverage:duplicate=invoice_number",
        )
    )

    assert "PROMPT_VERSION=invoice-vision-extraction-v2" in instruction
    assert "required invoice fields, using null when not visible: invoice.po_number" in instruction
    assert "undeclared invoice fields: invoice.invoice_code" in instruction
    assert "evidence item for these selected invoice fields: currency" in instruction
    assert "evidence paths outside the selected invoice fields: invoice_code" in instruction
    assert "duplicate paths: invoice_number" in instruction
    assert "decimal_parsing" in instruction
