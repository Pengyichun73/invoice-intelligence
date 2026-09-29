"""Paired scoring enforces coverage and emits value-free scenario counts."""

from copy import deepcopy

import pytest

from invoice_intelligence.application.services.memory_benefit_summary import (
    summarize_paired_judgments,
)
from invoice_intelligence.domain.invoice import InvoiceExtraction


def _rows(count: int) -> list[dict[str, object]]:
    rows = []
    for number in range(count):
        for variant in ("vision", "vision_ocr", "vision_ocr_memory"):
            memory = variant == "vision_ocr_memory"
            rows.append({
                "case_id": f"case-{number}", "variant": variant,
                "gold_complete": True, "ocr_healthy": True,
                "image_quality": "low" if number < 10 else "high",
                "ocr_status": "available", "error_category": "conflict",
                "document_checksum": f"{number:064x}",
                "template_group": f"template-{number % 3}",
                "schema_version": "3.0.0", "model_version": "model-a",
                "prompt_version": "prompt-a", "catalog_version": "catalog-a",
                "index_version": "field-pattern-v1-a",
                "elapsed_ms": 1000 if not memory else 1500,
                "fields": {path: {
                    "correct": True, "review_required": not memory,
                    "auto_accepted": memory,
                } for path in InvoiceExtraction.model_fields},
            })
    return rows


def test_scenario_scores_require_real_coverage() -> None:
    insufficient = summarize_paired_judgments(_rows(8))
    assert insufficient["metrics"]["passed"] is False
    assert insufficient["scenarios"][0]["passed"] is False
    result = summarize_paired_judgments(_rows(20))
    assert result["metrics"]["passed"] is True
    assert result["case_count"] == 20
    assert result["template_group_count"] == 3
    assert len(result["scenarios"]) == 26
    assert {item["value"] for item in result["scenarios"]
            if item["dimension"] == "image_quality"} == {"low", "high"}
    assert all(item["passed"] is False for item in result["scenarios"]
               if item["dimension"] == "template_group")
    assert result["blocker_codes"] == []


def test_version_mismatch_rejects_entire_dataset() -> None:
    rows = deepcopy(_rows(2))
    rows[-1]["model_version"] = "different"
    with pytest.raises(ValueError, match="inconsistent"):
        summarize_paired_judgments(rows)


def test_p95_latency_blocks_otherwise_improved_result() -> None:
    rows = _rows(20)
    for row in rows:
        if row["variant"] == "vision_ocr_memory" and row["case_id"] in {
            "case-18", "case-19"
        }:
            row["elapsed_ms"] = 12000
    result = summarize_paired_judgments(rows)
    assert result["metrics"]["p95_extra_ms"] == 11000
    assert result["metrics"]["passed"] is False
    assert result["blocker_codes"] == ["p95_extra_latency_exceeded"]
