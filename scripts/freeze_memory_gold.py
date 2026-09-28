"""Freeze independently annotated, full-schema invoice extraction gold cases."""

import argparse
import json
import math
import re
from hashlib import sha256
from pathlib import Path

from invoice_intelligence.domain.invoice import InvoiceExtraction

FIELD_PATHS = frozenset(InvoiceExtraction.model_fields)
VERSION_KEYS = (
    "schema_version", "model_version", "prompt_version",
    "catalog_version", "index_version",
)
CASE_KEYS = ("document_checksum", "template_group", *VERSION_KEYS)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(path: Path) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"Line {number} must be an object")
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in rows:
            raise ValueError(f"Line {number} has an invalid or repeated case_id")
        rows[case_id] = row
    if not rows:
        raise ValueError("Annotation file is empty")
    return rows


def _validated_fields(row: dict[str, object]) -> dict[str, dict[str, object]]:
    fields = row.get("fields")
    if not isinstance(fields, dict) or set(fields) != FIELD_PATHS:
        raise ValueError("Every annotation must contain exactly 19 field paths")
    values: dict[str, object] = {}
    for path, raw in fields.items():
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must be an annotation object")
        state, value = raw.get("state"), raw.get("value")
        if state not in {"present", "absent", "unreadable"}:
            raise ValueError(f"{path} has an invalid state")
        if state == "present":
            page, box, text = (
                raw.get("page_number"), raw.get("bounding_box"),
                raw.get("observed_text"),
            )
            if (
                value is None or type(page) is not int or page <= 0
                or not isinstance(box, list) or len(box) != 4
                or any(type(item) not in {int, float} for item in box)
                or any(not math.isfinite(item) for item in box)
                or box[0] < 0 or box[1] < 0
                or box[2] <= box[0] or box[3] <= box[1]
                or not isinstance(text, str) or not text.strip()
            ):
                raise ValueError(f"{path} lacks current-image evidence")
        elif value is not None:
            raise ValueError(f"{path} cannot have a value when {state}")
        values[path] = value
    InvoiceExtraction.model_validate(values)
    return fields


def freeze(
    first_path: Path, second_path: Path, adjudication_path: Path, output_path: Path
) -> dict[str, object]:
    first, second, adjudication = (
        _load(first_path), _load(second_path), _load(adjudication_path)
    )
    data, summary = prepare_frozen_gold(first, second, adjudication)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(data)
    return summary


def prepare_frozen_gold(
    first: dict[str, dict[str, object]],
    second: dict[str, dict[str, object]],
    adjudication: dict[str, dict[str, object]],
) -> tuple[str, dict[str, object]]:
    """Validate independent labels without reading or writing business artifacts."""

    if not first:
        raise ValueError("Annotation cases cannot be empty")
    if set(first) != set(second) or set(first) != set(adjudication):
        raise ValueError("Case lists must match across both labels and adjudication")
    frozen: list[dict[str, object]] = []
    for case_id in sorted(first):
        a, b, decision = first[case_id], second[case_id], adjudication[case_id]
        for key in CASE_KEYS:
            if not isinstance(a.get(key), str) or not a[key]:
                raise ValueError(f"{case_id} lacks {key}")
            if a[key] != b.get(key) or a[key] != decision.get(key):
                raise ValueError(f"{case_id} has inconsistent {key}")
        if re.fullmatch(r"[0-9a-f]{64}", str(a["document_checksum"])) is None:
            raise ValueError(f"{case_id} has an invalid document checksum")
        first_actor, second_actor, adjudicator = (
            a.get("annotator_id"), b.get("annotator_id"),
            decision.get("adjudicator_id"),
        )
        if (
            not isinstance(first_actor, str) or not first_actor
            or not isinstance(second_actor, str) or not second_actor
            or first_actor == second_actor
            or adjudicator != second_actor
        ):
            raise ValueError(f"{case_id} requires two distinct trusted reviewers")
        fields_a, fields_b = _validated_fields(a), _validated_fields(b)
        choices = decision.get("field_choices")
        if not isinstance(choices, dict):
            raise ValueError(f"{case_id} lacks adjudication choices")
        disagreements = {
            path for path in FIELD_PATHS
            if _canonical(fields_a[path]) != _canonical(fields_b[path])
        }
        if set(choices) != disagreements or any(
            choice not in {"first", "second"} for choice in choices.values()
        ):
            raise ValueError(f"{case_id} must adjudicate every disagreement exactly once")
        selected = {
            path: (fields_a[path] if choices.get(path) != "second" else fields_b[path])
            for path in sorted(FIELD_PATHS)
        }
        _validated_fields({"fields": selected})
        frozen.append({
            "case_id": case_id,
            **{key: a[key] for key in CASE_KEYS},
            "gold_complete": True,
            "fields": selected,
        })
    data = "".join(_canonical(row) + "\n" for row in frozen)
    return data, {
        "case_count": len(frozen),
        "sha256": sha256(data.encode("utf-8")).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze independent invoice gold annotations")
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("adjudication", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(freeze(args.first, args.second, args.adjudication, args.output)))


if __name__ == "__main__":
    main()
