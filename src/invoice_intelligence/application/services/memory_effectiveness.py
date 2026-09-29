"""Tenant-scoped operational status and evidence-backed memory benefit queries."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from invoice_intelligence.application.errors import (
    BadRequestError,
    ForbiddenError,
    ResourceNotFoundError,
)
from invoice_intelligence.application.ports.memory_effectiveness import (
    EffectivenessFacts,
    MemoryEffectivenessRepository,
    PairedBenefitRun,
)
from invoice_intelligence.domain.governance import MemoryPermission, TrustedTenantContext
from invoice_intelligence.domain.invoice import InvoiceExtraction

_SCENARIO_DIMENSIONS = frozenset({
    "field_path", "template_group", "image_quality", "ocr_status", "error_category"
})
_PUBLIC_SCENARIO_VALUES = frozenset({
    "low", "medium", "high", "unknown", "available", "unavailable",
    "conflict", "vision_only", "ocr_only", "none",
})


@dataclass(frozen=True, slots=True)
class EffectivenessStage:
    name: str
    status: str
    observed_count: int
    blocker_codes: tuple[str, ...]
    count_scope: str
    since: datetime | None
    until: datetime | None
    version: str | None
    evidence_at: datetime | None


class MemoryEffectivenessService:
    """Keep operational observations separate from independent benefit evidence."""

    def __init__(self, repository: MemoryEffectivenessRepository) -> None:
        self._repository = repository

    async def overview(
        self, context: TrustedTenantContext, *, since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, object]:
        self._require_read(context)
        end = until or datetime.now(UTC)
        start = since or end - timedelta(days=30)
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or start >= end
            or end - start > timedelta(days=90)
            or end > datetime.now(UTC) + timedelta(minutes=1)
        ):
            raise BadRequestError("Effectiveness window must be ordered and at most 90 days")
        facts = await self._repository.facts(context.tenant_id, start, end)
        latest = await self._repository.latest_run(context.tenant_id)
        benefit_status = "insufficient_evidence"
        if (latest is not None and latest.status == "completed"
                and latest.metrics.get("coverage_sufficient") is True):
            benefit_status = (
                "demonstrated" if latest.metrics.get("passed") is True
                and not latest.blocker_codes
                else "not_demonstrated"
            )
        return {
            "since": start,
            "until": end,
            "operational": asdict(facts),
            "stages": [asdict(stage) for stage in self._stages(facts, latest, start, end)],
            "benefit_status": benefit_status,
            "latest_benefit_run_id": latest.run_id if latest else None,
            "benefit_blocker_codes": list(self._benefit_blockers(latest)),
        }

    async def stages(
        self, context: TrustedTenantContext, *, since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, object]:
        overview = await self.overview(context, since=since, until=until)
        return {key: overview[key] for key in ("since", "until", "stages")}

    async def scenarios(
        self, context: TrustedTenantContext, *, run_id: str | None = None,
        field_path: str | None = None,
    ) -> dict[str, object]:
        self._require_read(context)
        run = (
            await self._repository.get_run(context.tenant_id, run_id)
            if run_id is not None else await self._repository.latest_run(context.tenant_id)
        )
        if run_id is not None and run is None:
            raise ResourceNotFoundError("Memory benefit run was not found")
        scenarios = (
            [self._scenario_payload(
                item, context.tenant_id,
                run.metrics.get("coverage_sufficient") is True,
            ) for item in run.scenarios
             if item.get("dimension") in _SCENARIO_DIMENSIONS
             and (field_path is None or item.get("field_path") == field_path)]
            if run is not None and run.status == "completed" else []
        )
        return {
            "run_id": run.run_id if run else None,
            "benefit_status": (
                "insufficient_evidence" if run is None or run.status != "completed"
                or run.metrics.get("coverage_sufficient") is not True
                else "demonstrated" if run.metrics.get("passed") is True
                and not run.blocker_codes
                else "not_demonstrated"
            ),
            "scenarios": scenarios,
            "blocker_codes": list(self._benefit_blockers(run)),
        }

    async def run(
        self, context: TrustedTenantContext, run_id: str
    ) -> dict[str, object]:
        self._require_read(context)
        run = await self._repository.get_run(context.tenant_id, run_id)
        if run is None:
            raise ResourceNotFoundError("Memory benefit run was not found")
        return self._run_payload(run)

    @staticmethod
    def _run_payload(run: PairedBenefitRun) -> dict[str, object]:
        payload = asdict(run)
        payload.pop("tenant_id")
        payload["metrics"] = (
            MemoryEffectivenessService._metrics_payload(run.metrics)
            if run.status == "completed" and run.metrics else None
        )
        payload["scenarios"] = [
            MemoryEffectivenessService._scenario_payload(
                item, run.tenant_id, run.metrics.get("coverage_sufficient") is True
            )
            for item in run.scenarios
            if item.get("dimension") in _SCENARIO_DIMENSIONS
        ]
        return payload

    @staticmethod
    def _metrics_payload(metrics: dict[str, object]) -> dict[str, object] | None:
        keys = (
            "passed", "coverage_sufficient", "review_reduction_vs_ocr",
            "correct_field_delta", "wrong_auto_passes", "p95_extra_ms",
        )
        return {key: metrics[key] for key in keys} if all(key in metrics for key in keys) else None

    @staticmethod
    def _benefit_blockers(run: PairedBenefitRun | None) -> tuple[str, ...]:
        if run is None:
            return ("paired_gold_run_missing",)
        if run.status != "completed":
            return ("paired_gold_run_incomplete",)
        if run.metrics.get("coverage_sufficient") is not True:
            return tuple(sorted({*run.blocker_codes, "paired_coverage_below_minimum"}))
        if run.metrics.get("passed") is not True and not run.blocker_codes:
            return ("benefit_criteria_not_met",)
        return run.blocker_codes

    @staticmethod
    def _scenario_payload(
        scenario: dict[str, object], tenant_id: str, coverage_sufficient: bool
    ) -> dict[str, object]:
        keys = (
            "dimension", "value", "field_path", "sample_count",
            "review_reduction_vs_ocr", "correct_field_delta",
            "wrong_auto_passes", "p95_extra_ms", "passed",
        )
        payload = {key: scenario[key] for key in keys}
        dimension = payload["dimension"]
        raw_value = str(payload["value"])
        if dimension == "field_path" and raw_value in InvoiceExtraction.model_fields:
            payload["value"] = raw_value
        elif dimension in {"image_quality", "ocr_status", "error_category"} and (
            raw_value in _PUBLIC_SCENARIO_VALUES
        ):
            payload["value"] = raw_value
        else:
            digest = sha256(f"{tenant_id}:{raw_value}".encode()).hexdigest()
            payload["value"] = f"sha256:{digest}"
        if payload["field_path"] not in InvoiceExtraction.model_fields:
            payload["field_path"] = None
        payload["passed"] = payload["passed"] is True and coverage_sufficient
        return payload

    @staticmethod
    def _require_read(context: TrustedTenantContext) -> None:
        if not context.permits(MemoryPermission.READ_EVALUATION):
            raise ForbiddenError("Memory evaluation permission is required")

    @staticmethod
    def _stages(
        facts: EffectivenessFacts, latest: PairedBenefitRun | None,
        since: datetime, until: datetime,
    ) -> tuple[EffectivenessStage, ...]:
        stages = (
            ("documents", facts.document_count, "documents_missing", "window",
             None, facts.latest_document_at),
            ("extraction", facts.extraction_run_count, "extraction_runs_missing", "window",
             None, facts.latest_extraction_at),
            ("review", facts.reviewed_example_count, "reviewed_examples_missing", "window",
             None, facts.latest_review_at),
            ("admission", facts.approved_admission_count, "approved_examples_missing",
             "window", None, facts.latest_admission_at),
            ("projection", facts.indexed_example_count, "indexed_examples_missing",
             "current", facts.active_index_version, facts.latest_projection_at),
            ("retrieval", facts.nonempty_retrieval_count, "nonempty_retrieval_missing",
             "window", facts.active_index_version, facts.latest_retrieval_at),
            ("evaluation_jobs", facts.completed_evaluation_job_count,
             "completed_evaluation_jobs_missing", "window", None,
             facts.latest_evaluation_job_at),
            ("frozen_gold", facts.frozen_gold_case_count,
             "frozen_gold_missing", "window", None, facts.latest_frozen_gold_at),
        )
        result = [
            EffectivenessStage(
                name, "observed" if count else "not_observed", count,
                () if count else (blocker,), scope,
                since if scope == "window" else None,
                until if scope == "window" else None,
                version, evidence_at,
            )
            for name, count, blocker, scope, version, evidence_at in stages
        ]
        result.append(EffectivenessStage(
            "targeted_reread", "not_observed", 0,
            ("reread_persistent_evidence_missing",), "window", since, until,
            None, None,
        ))
        result.append(EffectivenessStage(
            "paired_benefit",
            "observed" if latest and latest.status == "completed" else "not_observed",
            latest.case_count if latest else 0,
            latest.blocker_codes if latest else ("paired_gold_run_missing",),
            "latest_run", None, None,
            latest.index_version if latest else None,
            latest.completed_at if latest else None,
        ))
        return tuple(result)
