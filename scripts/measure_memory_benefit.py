"""Evaluate a privacy-safe, paired extraction acceptance manifest."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from invoice_intelligence.application.services.memory_benefit_summary import (
    summarize_paired_judgments,
)
from invoice_intelligence.domain.invoice import InvoiceExtraction

VARIANTS = ("vision", "vision_ocr", "vision_ocr_memory")
FIELD_PATHS = frozenset(InvoiceExtraction.model_fields)
VERSION_KEYS = (
    "schema_version",
    "model_version",
    "prompt_version",
    "catalog_version",
    "index_version",
)


def _read_manifest(path: Path) -> dict[str, dict[str, dict[str, object]]]:
    cases: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"Line {line_number} must be an object")
        case_id = row.get("case_id")
        variant = row.get("variant")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"Line {line_number} has no case_id")
        if variant not in VARIANTS or variant in cases[case_id]:
            raise ValueError(f"Line {line_number} has an invalid or repeated variant")
        if row.get("gold_complete") is not True:
            raise ValueError(f"Line {line_number} lacks complete independent review")
        fields = row.get("fields")
        if not isinstance(fields, dict) or set(fields) != FIELD_PATHS:
            raise ValueError(f"Line {line_number} must contain all 19 field paths")
        for path, result in fields.items():
            if not isinstance(result, dict) or any(
                type(result.get(key)) is not bool
                for key in ("review_required", "auto_accepted", "correct")
            ):
                raise ValueError(f"Line {line_number} has an incomplete field judgment")
            if result["review_required"] and result["auto_accepted"]:
                raise ValueError(f"Line {line_number} has conflicting field routes")
        if variant != "vision" and row.get("ocr_healthy") is not True:
            raise ValueError(f"Line {line_number} has unavailable OCR")
        if not isinstance(row.get("template_group"), str) or not row["template_group"]:
            raise ValueError(f"Line {line_number} has no template_group")
        for key in ("document_checksum", *VERSION_KEYS):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError(f"Line {line_number} has no {key}")
        cases[case_id][variant] = row
    if not cases:
        raise ValueError("Acceptance manifest is empty")
    return cases


def measure(path: Path) -> dict[str, object]:
    cases = _read_manifest(path)
    rows = [row for variants in cases.values() for row in variants.values()]
    summary = summarize_paired_judgments(rows)
    totals = {
        variant: {"review_fields": 0, "correct_fields": 0, "wrong_auto_passes": 0}
        for variant in VARIANTS
    }
    for row in rows:
        variant = row["variant"]
        for judgment in row["fields"].values():
            totals[variant]["review_fields"] += int(judgment["review_required"])
            totals[variant]["correct_fields"] += int(judgment["correct"])
            totals[variant]["wrong_auto_passes"] += int(
                judgment["auto_accepted"] and not judgment["correct"]
            )
    return {
        "contract_version": "memory-benefit-gate-v1",
        "case_count": summary["case_count"],
        "template_group_count": summary["template_group_count"],
        **summary["metrics"],
        "blocker_codes": summary["blocker_codes"],
        "variants": totals,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate paired, value-free invoice judgments")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    print(json.dumps(measure(args.manifest), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
