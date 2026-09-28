import json
from hashlib import sha256

import pytest

from invoice_intelligence.domain.invoice import InvoiceExtraction
from scripts.freeze_memory_gold import prepare_frozen_gold


def _row(actor: str, value: str) -> dict[str, object]:
    fields = {
        path: {"state": "absent", "value": None}
        for path in InvoiceExtraction.model_fields
    }
    fields["invoice_number"] = {
        "state": "present", "value": value,
        "page_number": 1, "bounding_box": [10, 10, 90, 30],
        "observed_text": value,
    }
    return {
        "case_id": "case-1",
        "document_checksum": "a" * 64,
        "template_group": "template-a",
        "schema_version": "3.0.0",
        "model_version": "model-a",
        "prompt_version": "prompt-a",
        "catalog_version": "catalog-a",
        "index_version": "field-pattern-v1-a",
        "annotator_id": actor,
        "fields": fields,
    }


def test_freeze_requires_two_labels_and_explicit_adjudication() -> None:
    first = _row("reviewer-a", "A-001")
    second = _row("reviewer-b", "B-002")
    decision = {
        "case_id": "case-1",
        **{key: first[key] for key in (
            "document_checksum", "template_group", "schema_version",
            "model_version", "prompt_version", "catalog_version", "index_version",
        )},
        "adjudicator_id": "reviewer-b",
        "field_choices": {"invoice_number": "first"},
    }
    data, summary = prepare_frozen_gold(
        {"case-1": first}, {"case-1": second}, {"case-1": decision}
    )
    gold = json.loads(data)
    assert gold["fields"]["invoice_number"]["value"] == "A-001"
    assert gold["gold_complete"] is True
    assert summary["sha256"] == sha256(data.encode("utf-8")).hexdigest()


def test_freeze_rejects_unadjudicated_difference() -> None:
    first = _row("reviewer-a", "A-001")
    second = _row("reviewer-b", "B-002")
    decision = {
        "case_id": "case-1",
        **{key: first[key] for key in (
            "document_checksum", "template_group", "schema_version",
            "model_version", "prompt_version", "catalog_version", "index_version",
        )},
        "adjudicator_id": "reviewer-b",
        "field_choices": {},
    }
    with pytest.raises(ValueError, match="adjudicate"):
        prepare_frozen_gold(
            {"case-1": first}, {"case-1": second}, {"case-1": decision}
        )


def test_freeze_rejects_same_reviewer_or_missing_current_evidence() -> None:
    first = _row("reviewer-a", "A-001")
    second = _row("reviewer-a", "A-001")
    decision = {
        "case_id": "case-1",
        **{key: first[key] for key in (
            "document_checksum", "template_group", "schema_version",
            "model_version", "prompt_version", "catalog_version", "index_version",
        )},
        "adjudicator_id": "reviewer-a",
        "field_choices": {},
    }
    with pytest.raises(ValueError, match="distinct"):
        prepare_frozen_gold(
            {"case-1": first}, {"case-1": second}, {"case-1": decision}
        )
    second["annotator_id"] = "reviewer-b"
    decision["adjudicator_id"] = "reviewer-b"
    first["fields"]["invoice_number"].pop("bounding_box")
    with pytest.raises(ValueError, match="current-image evidence"):
        prepare_frozen_gold(
            {"case-1": first}, {"case-1": second}, {"case-1": decision}
        )
