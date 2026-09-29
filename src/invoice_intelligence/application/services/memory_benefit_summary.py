"""Deterministic, value-free paired benefit and scenario scoring."""

import json
import math
from collections import defaultdict
from collections.abc import Sequence
from hashlib import sha256
from typing import Any

from invoice_intelligence.domain.invoice import InvoiceExtraction

VARIANTS = frozenset({"vision", "vision_ocr", "vision_ocr_memory"})
VERSION_KEYS = (
    "schema_version", "model_version", "prompt_version", "catalog_version", "index_version"
)
FIELD_PATHS = frozenset(InvoiceExtraction.model_fields)


def summarize_paired_judgments(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Reject incomplete pairs before calculating any benefit claim."""

    cases: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        case_id, variant = row.get("case_id"), row.get("variant")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("Paired judgment has no case identifier")
        if variant not in VARIANTS or variant in cases[case_id]:
            raise ValueError("Paired judgment has a repeated or invalid variant")
        if row.get("gold_complete") is not True:
            raise ValueError("Paired judgment lacks frozen independent gold")
        if variant != "vision" and row.get("ocr_healthy") is not True:
            raise ValueError("Paired judgment has unavailable OCR")
        fields = row.get("fields")
        if not isinstance(fields, dict) or set(fields) != FIELD_PATHS:
            raise ValueError("Paired judgment requires all 19 fields")
        for judgment in fields.values():
            if not isinstance(judgment, dict) or any(
                type(judgment.get(key)) is not bool
                for key in ("correct", "review_required", "auto_accepted")
            ) or judgment["review_required"] == judgment["auto_accepted"]:
                raise ValueError("Paired judgment has an invalid field decision")
        elapsed = row.get("elapsed_ms")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
            raise ValueError("Paired judgment requires a finite elapsed time")
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("Paired judgment requires a finite elapsed time")
        for key in ("document_checksum", "template_group", *VERSION_KEYS):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError(f"Paired judgment lacks {key}")
        cases[case_id][variant] = row
    if not cases:
        raise ValueError("Paired judgment dataset is empty")

    checksums: set[str] = set()
    templates: set[str] = set()
    all_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    version_bindings: dict[str, str] | None = None
    for variants in cases.values():
        if set(variants) != VARIANTS:
            raise ValueError("Every case requires all three variants")
        reference = variants["vision"]
        checksum = reference["document_checksum"]
        if checksum in checksums:
            raise ValueError("Paired dataset contains repeated document bytes")
        checksums.add(checksum)
        templates.add(reference["template_group"])
        bindings = {key: reference[key] for key in VERSION_KEYS}
        if version_bindings is None:
            version_bindings = bindings
        elif version_bindings != bindings:
            raise ValueError("Paired cases have inconsistent runtime versions")
        for row in variants.values():
            if any(row[key] != reference[key] for key in (
                "document_checksum", "template_group", *VERSION_KEYS
            )):
                raise ValueError("Paired variants have inconsistent source bindings")
        all_pairs.append((variants["vision_ocr"], variants["vision_ocr_memory"]))

    coverage = len(cases) >= 20 and len(templates) >= 3
    metrics = _score(all_pairs, None, coverage=coverage, minimum_cases=20)
    scenarios: list[dict[str, Any]] = []
    for field_path in sorted(FIELD_PATHS):
        item = _score(all_pairs, field_path, coverage=coverage, minimum_cases=20)
        scenarios.append({
            "dimension": "field_path", "value": field_path,
            "field_path": field_path, "sample_count": len(all_pairs), **item,
        })
    for template in sorted(templates):
        pairs = [pair for pair in all_pairs if pair[0]["template_group"] == template]
        item = _score(pairs, None, coverage=coverage, minimum_cases=20)
        scenarios.append({
            "dimension": "template_group", "value": template,
            "field_path": None, "sample_count": len(pairs), **item,
        })
    for dimension in ("image_quality", "ocr_status", "error_category"):
        values = sorted({
            str(before[dimension]) for before, _ in all_pairs if dimension in before
        })
        for value in values:
            pairs = [
                pair for pair in all_pairs if pair[0].get(dimension) == value
            ]
            item = _score(pairs, None, coverage=coverage, minimum_cases=20)
            scenarios.append({
                "dimension": dimension, "value": value,
                "field_path": None, "sample_count": len(pairs), **item,
            })
    blockers: list[str] = []
    if not coverage:
        blockers.append("paired_coverage_below_minimum")
    if metrics["ocr_review_fields"] == 0:
        blockers.append("ocr_review_baseline_missing")
    elif metrics["review_reduction_vs_ocr"] < 0.20:
        blockers.append("review_reduction_below_minimum")
    if metrics["correct_field_delta"] < 0:
        blockers.append("correct_fields_regressed")
    if metrics["wrong_auto_passes"]:
        blockers.append("wrong_auto_passes_present")
    if metrics["p95_extra_ms"] > 10000:
        blockers.append("p95_extra_latency_exceeded")
    public_metrics = {key: metrics[key] for key in (
        "passed", "coverage_sufficient", "review_reduction_vs_ocr",
        "correct_field_delta", "wrong_auto_passes", "p95_extra_ms",
    )}
    canonical = json.dumps(
        sorted(rows, key=lambda row: (row["case_id"], row["variant"])),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return {
        "dataset_digest": sha256(canonical.encode()).hexdigest(),
        "case_count": len(cases), "template_group_count": len(templates),
        "versions": version_bindings,
        "metrics": public_metrics,
        "scenarios": scenarios,
        "blocker_codes": blockers,
    }


def _score(
    pairs: Sequence[tuple[dict[str, Any], dict[str, Any]]],
    field_path: str | None, *, coverage: bool, minimum_cases: int,
) -> dict[str, Any]:
    ocr_reviews = 0
    memory_reviews = 0
    correct_delta = 0
    wrong_auto = 0
    extra_latencies: list[float] = []
    for ocr, memory in pairs:
        paths = (field_path,) if field_path is not None else sorted(FIELD_PATHS)
        for path in paths:
            before, after = ocr["fields"][path], memory["fields"][path]
            ocr_reviews += int(before["review_required"])
            memory_reviews += int(after["review_required"])
            correct_delta += int(after["correct"]) - int(before["correct"])
            wrong_auto += int(after["auto_accepted"] and not after["correct"])
        extra_latencies.append(max(0.0, memory["elapsed_ms"] - ocr["elapsed_ms"]))
    reduction = (ocr_reviews - memory_reviews) / ocr_reviews if ocr_reviews else 0.0
    p95 = sorted(extra_latencies)[math.ceil(0.95 * len(extra_latencies)) - 1]
    enough = coverage and len(pairs) >= minimum_cases
    return {
        "passed": bool(enough and ocr_reviews and reduction >= 0.20
                       and correct_delta >= 0 and wrong_auto == 0 and p95 <= 10000),
        "coverage_sufficient": enough,
        "review_reduction_vs_ocr": round(reduction, 6),
        "correct_field_delta": correct_delta,
        "wrong_auto_passes": wrong_auto,
        "p95_extra_ms": round(p95, 3),
        "ocr_review_fields": ocr_reviews,
    }
