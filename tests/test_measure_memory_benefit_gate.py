"""The paired gate rejects slow memory even when field accuracy improves."""

import json

from invoice_intelligence.domain.invoice import InvoiceExtraction
from scripts.measure_memory_benefit import measure


def test_p95_extra_latency_blocks_benefit(tmp_path) -> None:
    manifest = tmp_path / "judgments.jsonl"
    rows = []
    for number in range(20):
        for variant in ("vision", "vision_ocr", "vision_ocr_memory"):
            memory = variant == "vision_ocr_memory"
            rows.append({
                "case_id": f"case-{number}",
                "variant": variant,
                "gold_complete": True,
                "document_checksum": f"{number:064x}",
                "template_group": f"group-{number % 3}",
                "schema_version": "3.0.0",
                "model_version": "model-a",
                "prompt_version": "prompt-a",
                "catalog_version": "catalog-a",
                "index_version": "field-pattern-v1-a",
                "ocr_healthy": True,
                "elapsed_ms": 12000 if memory and number >= 18 else 1000,
                "fields": {
                    path: {
                        "correct": True,
                        "review_required": not memory,
                        "auto_accepted": memory,
                    }
                    for path in InvoiceExtraction.model_fields
                },
            })
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = measure(manifest)
    assert result["coverage_sufficient"] is True
    assert result["review_reduction_vs_ocr"] == 1.0
    assert result["p95_extra_ms"] == 11000
    assert result["passed"] is False
    assert "p95_extra_latency_exceeded" in result["blocker_codes"]
