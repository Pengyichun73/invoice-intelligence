"""Stable JSON and Markdown artifacts for completed offline evaluations."""

import asyncio
import json
import os
import tempfile
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from invoice_intelligence.domain.evaluation import (
    EvaluationBucketResult,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationVariantResult,
    ExtractionMetric,
    RetrievalMetric,
    TrustedMemoryFieldMetric,
)


class EvaluationReportSerializer:
    """Serialize aggregate-only reports without raw invoice or image content."""

    @classmethod
    def to_json_payload(cls, run: EvaluationRun) -> dict[str, Any]:
        cls._require_completed(run)
        return {
            "report_schema_version": run.report_schema_version,
            "evaluation_run_id": run.evaluation_run_id,
            "tenant_id": run.tenant_id,
            "status": run.status.value,
            "suite": run.suite.value,
            "dataset": {
                "dataset_id": run.dataset_id,
                "dataset_version": run.dataset_version,
                "schema_version": run.schema_version,
                "document_split_isolated": run.leakage_check_passed,
                "document_and_template_split_isolated": run.leakage_check_passed,
            },
            "versions": {
                "index": run.bindings.index_version.value,
                "model": run.bindings.model_version.value,
                "prompt": run.bindings.prompt_version.value,
                "retrieval_policy": run.bindings.retrieval_policy_version.value,
                "threshold": run.bindings.threshold_version,
                "catalog": run.bindings.catalog_version,
                "admission_policy": run.bindings.admission_policy_version,
                "field_binding_policy": run.bindings.field_binding_policy_version,
            },
            "timestamps": {
                "created_at": run.created_at.isoformat(),
                "started_at": cls._iso(run.started_at),
                "completed_at": cls._iso(run.completed_at),
            },
            "score_semantics": {
                "similarity_scores_are_probabilities": False,
                "quality_scores_are_probabilities": False,
                "positive_negative_separation": (
                    "mean(min positive ranking score - max hard-negative ranking score)"
                ),
                "undefined_zero_denominator_metrics": None,
            },
            "variants": [cls._variant(item) for item in run.results],
            "promotion_candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "tenant_id": item.tenant_id,
                    "baseline_variant": item.baseline_variant.value,
                    "candidate_variant": item.candidate_variant.value,
                    "metric_deltas": dict(sorted(item.metric_deltas.items())),
                    "rationale_codes": list(item.rationale_codes),
                    "status": item.status,
                    "requires_human_approval": item.requires_human_approval,
                    "may_modify_production": item.may_modify_production,
                    "created_at": item.created_at.isoformat(),
                }
                for item in run.promotion_candidates
            ],
            "production_configuration_modified": False,
        }

    @classmethod
    def to_markdown(cls, run: EvaluationRun) -> str:
        cls._require_completed(run)
        lines = [
            "# Invoice Intelligence Offline Evaluation",
            "",
            "## Run Scope",
            "",
            f"- Evaluation run: `{run.evaluation_run_id}`",
            f"- Tenant: `{run.tenant_id}`",
            f"- Dataset: `{run.dataset_id}` / `{run.dataset_version}`",
            f"- Entity Schema: `{run.schema_version}`",
            f"- Suite: `{run.suite.value}`",
            f"- Index: `{run.bindings.index_version.value}`",
            f"- Model: `{run.bindings.model_version.value}`",
            f"- Prompt: `{run.bindings.prompt_version.value}`",
            f"- Retrieval policy: `{run.bindings.retrieval_policy_version.value}`",
            f"- Thresholds: `{run.bindings.threshold_version}`",
            f"- Catalog: `{run.bindings.catalog_version or 'N/A'}`",
            f"- Admission policy: `{run.bindings.admission_policy_version or 'N/A'}`",
            f"- Field-binding policy: `{run.bindings.field_binding_policy_version or 'N/A'}`",
            f"- Document split isolated: `{str(run.leakage_check_passed).lower()}`",
            "",
            "## Overall Comparison",
            "",
            "| Variant | K | Recall@K | HitRate@K | MRR | nDCG@K | "
            "Pos/Neg Separation | Empty Rate | Field Accuracy | Missing Accuracy | "
            "Candidate Hit | Review Precision | Review Recall | Bad Auto-fill |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for result in run.results:
            extraction = result.overall.extraction_metric
            for retrieval in result.overall.retrieval_metrics:
                lines.append(
                    "| "
                    + " | ".join(
                        (
                            f"`{result.variant.value}`",
                            str(retrieval.k),
                            cls._ratio(retrieval.recall_at_k),
                            cls._ratio(retrieval.hit_rate_at_k),
                            cls._ratio(retrieval.mrr),
                            cls._ratio(retrieval.ndcg_at_k),
                            cls._number(retrieval.positive_negative_separation),
                            cls._ratio(retrieval.empty_retrieval_rate),
                            cls._ratio(extraction.field_accuracy),
                            cls._ratio(extraction.missing_recognition_accuracy),
                            cls._ratio(extraction.candidate_hit_rate),
                            cls._ratio(extraction.review_required_precision),
                            cls._ratio(extraction.review_required_recall),
                            str(extraction.erroneous_auto_filled_value_count),
                        )
                    )
                    + " |"
                )

        if any(item.overall.trusted_memory_field_metrics for item in run.results):
            lines.extend(
                (
                    "",
                    "## Trusted Memory and Field Binding",
                    "",
                    "| Variant | K | Approval Precision | Harmful Admission | "
                    "Quarantine | Reviewer Disagreement | Alias Accuracy | "
                    "Top-K Field Recall | Binding Ambiguity | Wrong-field Auto-fill | "
                    "Memory Helpfulness | Misleading Retrieval |",
                    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
                )
            )
            for result in run.results:
                for metric in result.overall.trusted_memory_field_metrics:
                    lines.append(
                        "| "
                        + " | ".join(
                            (
                                f"`{result.variant.value}`",
                                str(metric.k),
                                cls._ratio(metric.memory_approval_precision),
                                cls._ratio(metric.harmful_memory_admission_rate),
                                cls._ratio(metric.quarantine_rate),
                                cls._ratio(metric.reviewer_disagreement_rate),
                                cls._ratio(metric.alias_binding_accuracy),
                                cls._ratio(metric.top_k_field_recall),
                                cls._ratio(metric.field_binding_ambiguity_rate),
                                str(metric.wrong_field_auto_fill_count),
                                cls._ratio(metric.memory_helpfulness_rate),
                                cls._ratio(metric.misleading_retrieval_rate),
                            )
                        )
                        + " |"
                    )

        lines.extend(
            (
                "",
                "## Bucket Review",
                "",
                "Detailed bucket metrics are grouped by `document_type`, `field_path`, "
                "`vendor_template`, and `image_quality`. Values in vendor/template buckets "
                "are fingerprints, not raw invoice data.",
                "",
            )
        )
        for result in run.results:
            lines.extend((f"### `{result.variant.value}`", ""))
            lines.extend(cls._bucket_table(result))

        lines.extend(("", "## Promotion Candidates", ""))
        if not run.promotion_candidates:
            lines.append("No variant passed the configured promotion-candidate guardrails.")
        else:
            lines.extend(
                (
                    "| Candidate | Variant | Baseline | Rationale |",
                    "|---|---|---|---|",
                )
            )
            for item in run.promotion_candidates:
                lines.append(
                    f"| `{item.candidate_id}` | `{item.candidate_variant.value}` | "
                    f"`{item.baseline_variant.value}` | "
                    f"{', '.join(item.rationale_codes)} |"
                )

        lines.extend(
            (
                "",
                "## Review Constraints",
                "",
                "- Retrieval scores and separation margins are ranking signals, not probabilities.",
                "- Undefined metrics are reported as `N/A`; zero denominators are never imputed.",
                "- A Promotion Candidate requires human approval and cannot update production.",
                "- Results are tenant-scoped and must not tune another tenant automatically.",
                "- Historical cases remain priors and cannot override current image evidence.",
                "",
            )
        )
        return "\n".join(lines)

    @classmethod
    def _variant(cls, result: EvaluationVariantResult) -> dict[str, Any]:
        return {
            "variant": result.variant.value,
            "overall": cls._bucket(result.overall),
            "buckets": [cls._bucket(item) for item in result.buckets],
        }

    @classmethod
    def _bucket(cls, result: EvaluationBucketResult) -> dict[str, Any]:
        return {
            "dimension": result.bucket.dimension.value,
            "value": result.bucket.value,
            "retrieval_metrics": [
                cls._retrieval_metric(item) for item in result.retrieval_metrics
            ],
            "extraction_metric": cls._extraction_metric(result.extraction_metric),
            "trusted_memory_field_metrics": [
                cls._trusted_memory_field_metric(item)
                for item in result.trusted_memory_field_metrics
            ],
        }

    @staticmethod
    def _retrieval_metric(metric: RetrievalMetric) -> dict[str, Any]:
        return {
            "k": metric.k,
            "recall_at_k": metric.recall_at_k,
            "hit_rate_at_k": metric.hit_rate_at_k,
            "mrr": metric.mrr,
            "ndcg_at_k": metric.ndcg_at_k,
            "positive_negative_separation": metric.positive_negative_separation,
            "empty_retrieval_rate": metric.empty_retrieval_rate,
            "counts": {
                "evaluation_cases": metric.evaluation_case_count,
                "cases_with_expected_examples": metric.expected_example_case_count,
                "expected_examples": metric.expected_example_count,
                "relevant_hits": metric.relevant_hit_count,
                "empty_retrievals": metric.empty_retrieval_count,
                "separation_cases": metric.separation_case_count,
            },
        }

    @staticmethod
    def _extraction_metric(metric: ExtractionMetric) -> dict[str, Any]:
        return {
            "field_accuracy": metric.field_accuracy,
            "missing_recognition_accuracy": metric.missing_recognition_accuracy,
            "candidate_hit_rate": metric.candidate_hit_rate,
            "erroneous_auto_filled_value_count": (
                metric.erroneous_auto_filled_value_count
            ),
            "historical_override_violation_count": (
                metric.historical_override_violation_count
            ),
            "review_required_precision": metric.review_required_precision,
            "review_required_recall": metric.review_required_recall,
            "counts": {
                "evaluation_cases": metric.evaluation_case_count,
                "field_accuracy_denominator": metric.field_accuracy_denominator,
                "missing_recognition_correct": (
                    metric.missing_recognition_correct_count
                ),
                "candidate_cases": metric.candidate_case_count,
                "candidate_hits": metric.candidate_hit_count,
                "review_true_positives": metric.review_true_positive_count,
                "review_false_positives": metric.review_false_positive_count,
                "review_false_negatives": metric.review_false_negative_count,
            },
        }

    @staticmethod
    def _trusted_memory_field_metric(
        metric: TrustedMemoryFieldMetric,
    ) -> dict[str, Any]:
        ratio_names = (
            "memory_approval_precision",
            "harmful_memory_admission_rate",
            "quarantine_rate",
            "reviewer_disagreement_rate",
            "alias_binding_accuracy",
            "top_k_field_recall",
            "field_binding_ambiguity_rate",
            "memory_helpfulness_rate",
            "misleading_retrieval_rate",
        )
        count_names = tuple(
            name
            for name in metric.__dataclass_fields__
            if name not in {"k", *ratio_names}
        )
        return {
            "k": metric.k,
            **{name: getattr(metric, name) for name in ratio_names},
            "counts": {name: getattr(metric, name) for name in count_names},
        }

    @classmethod
    def _bucket_table(cls, result: EvaluationVariantResult) -> list[str]:
        lines = [
            "| Dimension | Bucket | K | Recall@K | nDCG@K | Field Accuracy | Review Recall |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
        for bucket in result.buckets:
            extraction = bucket.extraction_metric
            for retrieval in bucket.retrieval_metrics:
                escaped_value = bucket.bucket.value.replace("|", "\\|")
                lines.append(
                    f"| `{bucket.bucket.dimension.value}` | `{escaped_value}` | "
                    f"{retrieval.k} | {cls._ratio(retrieval.recall_at_k)} | "
                    f"{cls._ratio(retrieval.ndcg_at_k)} | "
                    f"{cls._ratio(extraction.field_accuracy)} | "
                    f"{cls._ratio(extraction.review_required_recall)} |"
                )
        trusted_buckets = tuple(
            bucket for bucket in result.buckets if bucket.trusted_memory_field_metrics
        )
        if trusted_buckets:
            lines.extend(
                (
                    "",
                    "| Dimension | Bucket | K | Approval Precision | Harmful Admission | "
                    "Alias Accuracy | Top-K Field Recall | Binding Ambiguity | "
                    "Wrong-field Auto-fill | Helpfulness | Misleading Retrieval |",
                    "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
                )
            )
            for bucket in trusted_buckets:
                escaped_value = bucket.bucket.value.replace("|", "\\|")
                for metric in bucket.trusted_memory_field_metrics:
                    lines.append(
                        f"| `{bucket.bucket.dimension.value}` | `{escaped_value}` | "
                        f"{metric.k} | {cls._ratio(metric.memory_approval_precision)} | "
                        f"{cls._ratio(metric.harmful_memory_admission_rate)} | "
                        f"{cls._ratio(metric.alias_binding_accuracy)} | "
                        f"{cls._ratio(metric.top_k_field_recall)} | "
                        f"{cls._ratio(metric.field_binding_ambiguity_rate)} | "
                        f"{metric.wrong_field_auto_fill_count} | "
                        f"{cls._ratio(metric.memory_helpfulness_rate)} | "
                        f"{cls._ratio(metric.misleading_retrieval_rate)} |"
                    )
        lines.append("")
        return lines

    @staticmethod
    def _require_completed(run: EvaluationRun) -> None:
        if run.status is not EvaluationRunStatus.COMPLETED:
            raise ValueError("Evaluation reports require a completed run")

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _ratio(value: float | None) -> str:
        return f"{value:.4f}" if value is not None else "N/A"

    @staticmethod
    def _number(value: float | None) -> str:
        return f"{value:.6f}" if value is not None else "N/A"


class FileEvaluationArtifactPublisher:
    """Atomically write derived JSON and Markdown reports to local storage."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()

    async def publish(self, run: EvaluationRun) -> tuple[str, ...]:
        return await asyncio.to_thread(self._publish_sync, run)

    def _publish_sync(self, run: EvaluationRun) -> tuple[str, ...]:
        payload = EvaluationReportSerializer.to_json_payload(run)
        markdown = EvaluationReportSerializer.to_markdown(run)
        tenant_directory = sha256_text(run.tenant_id)[:24]
        run_filename = sha256_text(run.evaluation_run_id)[:32]
        destination = self._root / tenant_directory
        destination.mkdir(parents=True, exist_ok=True)
        json_path = destination / f"{run_filename}.json"
        markdown_path = destination / f"{run_filename}.md"
        self._write_atomic(
            json_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        self._write_atomic(markdown_path, markdown)
        return (str(json_path), str(markdown_path))

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
