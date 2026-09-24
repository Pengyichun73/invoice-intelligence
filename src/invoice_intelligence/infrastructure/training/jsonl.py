"""Atomic local exporter for provider-neutral training JSONL artifacts."""

import asyncio
import json
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from invoice_intelligence.domain.training import (
    TrainingDatasetRecord,
    TrainingDatasetVersion,
    TrainingSplit,
)


class LocalTrainingDatasetExporter:
    """Publish immutable split JSONL files and a non-sensitive manifest."""

    def __init__(self, output_root: Path) -> None:
        self._output_root = output_root.resolve()

    async def export(
        self,
        dataset: TrainingDatasetVersion,
        records: Sequence[TrainingDatasetRecord],
    ) -> tuple[str, ...]:
        return await asyncio.to_thread(self._export_sync, dataset, tuple(records))

    def _export_sync(
        self,
        dataset: TrainingDatasetVersion,
        records: tuple[TrainingDatasetRecord, ...],
    ) -> tuple[str, ...]:
        if len(records) != dataset.record_count:
            raise ValueError("Export record count does not match frozen dataset version")
        self._output_root.mkdir(parents=True, exist_ok=True)
        directory_name = f"dataset-{dataset.fingerprint[:32]}"
        target = (self._output_root / directory_name).resolve()
        if target.parent != self._output_root:
            raise ValueError("Training export target escaped its configured root")
        filenames = {
            TrainingSplit.TRAIN: "train.jsonl",
            TrainingSplit.VALIDATION: "validation.jsonl",
            TrainingSplit.EVALUATION: "evaluation.jsonl",
        }
        artifact_paths = tuple(
            str(target / filename)
            for filename in (*filenames.values(), "manifest.json")
        )
        if target.exists():
            self._verify_existing(target, dataset)
            return artifact_paths

        staging = Path(tempfile.mkdtemp(prefix=".training-export-", dir=self._output_root))
        try:
            for split, filename in filenames.items():
                split_records = tuple(item for item in records if item.split is split)
                self._write_jsonl(staging / filename, split_records)
            manifest = self._manifest(dataset, records)
            self._write_json(staging / "manifest.json", manifest)
            os.replace(staging, target)
        except Exception:
            if staging.exists() and staging.parent == self._output_root:
                shutil.rmtree(staging)
            raise
        return artifact_paths

    @staticmethod
    def _write_jsonl(path: Path, records: Sequence[TrainingDatasetRecord]) -> None:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            for item in records:
                stream.write(
                    json.dumps(
                        _training_payload(item),
                        allow_nan=False,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(
                value,
                stream,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _manifest(
        dataset: TrainingDatasetVersion,
        records: Sequence[TrainingDatasetRecord],
    ) -> dict[str, Any]:
        counts = Counter(item.split.value for item in records)
        group_splits: dict[str, str] = {}
        for record in records:
            existing = group_splits.setdefault(
                record.group_fingerprint,
                record.split.value,
            )
            if existing != record.split.value:
                raise ValueError("A document/template group crosses dataset splits")
        return {
            "manifest_schema_version": "training-dataset-manifest.v1",
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "tenant_scope": dataset.tenant_scope,
            "tenant_count": len(dataset.source_tenant_ids),
            "cross_tenant": dataset.cross_tenant,
            "schema_version": dataset.schema_version,
            "generation_rule_version": dataset.generation_rule_version,
            "redaction_policy_version": dataset.redaction_policy_version,
            "split_rule_version": dataset.split_rule_version,
            "split_salt_version": dataset.split_salt_version,
            "dataset_fingerprint": dataset.fingerprint,
            "record_count": dataset.record_count,
            "split_counts": {
                split.value: counts.get(split.value, 0) for split in TrainingSplit
            },
            "group_count": len(group_splits),
            "record_fingerprints": sorted(item.fingerprint for item in records),
            "contains_raw_tenant_ids": False,
            "contains_raw_document_ids": False,
            "contains_image_base64": False,
            "training_executed": False,
        }

    @staticmethod
    def _verify_existing(target: Path, dataset: TrainingDatasetVersion) -> None:
        manifest_path = target / "manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Existing training artifact directory is invalid") from exc
        if payload.get("dataset_fingerprint") != dataset.fingerprint:
            raise ValueError("Existing training artifact has a different fingerprint")
        for filename in (
            "train.jsonl",
            "validation.jsonl",
            "evaluation.jsonl",
            "manifest.json",
        ):
            if not (target / filename).is_file():
                raise ValueError("Existing training artifact is incomplete")


def _training_payload(item: TrainingDatasetRecord) -> dict[str, object]:
    record = item.record
    return {
        "query": record.query,
        "positive": record.positive,
        "hard_negatives": list(record.hard_negatives),
        "scope": {
            "tenant_scope": record.scope.tenant_scope,
            "document_type": record.scope.document_type,
            "field_path": record.scope.field_path,
            "schema_version": record.scope.schema_version,
            "vendor_fingerprint": record.scope.vendor_fingerprint,
            "template_cluster": record.scope.template_cluster,
        },
        "source_event_ids": list(record.source_event_ids),
    }
