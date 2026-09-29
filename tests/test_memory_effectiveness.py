"""Effectiveness reporting must not turn operational activity into claimed benefit."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from invoice_intelligence.api.schemas.memory_effectiveness import (
    EffectivenessOverviewResponse,
    EffectivenessRunResponse,
)
from invoice_intelligence.application.errors import ResourceNotFoundError
from invoice_intelligence.application.ports.memory_effectiveness import (
    EffectivenessFacts,
    PairedBenefitRun,
)
from invoice_intelligence.application.services.memory_effectiveness import (
    MemoryEffectivenessService,
)
from invoice_intelligence.domain.governance import MemoryPermission, TrustedTenantContext

NOW = datetime(2026, 9, 28, tzinfo=UTC)


class _Repository:
    def __init__(self, run: PairedBenefitRun | None = None) -> None:
        self.run = run
        self.tenants: list[str] = []

    async def facts(self, tenant_id: str, since: datetime, until: datetime) -> EffectivenessFacts:
        self.tenants.append(tenant_id)
        return EffectivenessFacts(
            document_count=8, extraction_run_count=8, reviewed_example_count=5,
            pending_admission_count=0, approved_admission_count=5,
            indexed_example_count=5, retrieval_count=4,
            nonempty_retrieval_count=2, review_required_retrieval_count=1,
            active_index_version="field-pattern-v1-test", latest_document_at=NOW,
            latest_extraction_at=NOW, latest_review_at=NOW,
            latest_admission_at=NOW, latest_projection_at=NOW,
            latest_retrieval_at=NOW, evaluation_job_count=0,
            completed_evaluation_job_count=0, latest_evaluation_job_at=None,
            gold_case_count=0, frozen_gold_case_count=0,
            frozen_template_group_count=0, latest_frozen_gold_at=None,
        )

    async def latest_run(self, tenant_id: str) -> PairedBenefitRun | None:
        self.tenants.append(tenant_id)
        return self.run if self.run and self.run.tenant_id == tenant_id else None

    async def get_run(self, tenant_id: str, run_id: str) -> PairedBenefitRun | None:
        self.tenants.append(tenant_id)
        return self.run if self.run and (
            self.run.tenant_id, self.run.run_id
        ) == (tenant_id, run_id) else None


def _context(tenant_id: str) -> TrustedTenantContext:
    return TrustedTenantContext(
        tenant_id=tenant_id, actor_id="evaluator",
        permissions=frozenset({MemoryPermission.READ_EVALUATION}),
    )


def _run() -> PairedBenefitRun:
    return PairedBenefitRun(
        run_id="run-1", tenant_id="tenant-a", status="completed",
        dataset_digest="a" * 64, schema_version="3.0.0",
        catalog_version="catalog-a", index_version="field-pattern-v1-test",
        model_version="model-a", prompt_version="prompt-a", case_count=8,
        template_group_count=2, metrics={"passed": False, "coverage_sufficient": False},
        scenarios=(), blocker_codes=(), created_at=NOW, completed_at=NOW,
    )


@pytest.mark.asyncio
async def test_operational_activity_does_not_claim_benefit() -> None:
    repository = _Repository(_run())
    result = await MemoryEffectivenessService(repository).overview(
        _context("tenant-a"), since=NOW - timedelta(days=1), until=NOW + timedelta(hours=1)
    )
    assert result["benefit_status"] == "insufficient_evidence"
    assert "paired_coverage_below_minimum" in result["benefit_blocker_codes"]
    assert result["operational"]["nonempty_retrieval_count"] == 2
    assert repository.tenants == ["tenant-a", "tenant-a"]
    EffectivenessOverviewResponse.model_validate(result)
    stages = {item["name"]: item for item in result["stages"]}
    assert stages["projection"]["count_scope"] == "current"
    assert stages["targeted_reread"]["status"] == "not_observed"


@pytest.mark.asyncio
async def test_cross_tenant_run_is_not_disclosed() -> None:
    service = MemoryEffectivenessService(_Repository(_run()))
    with pytest.raises(ResourceNotFoundError):
        await service.run(_context("tenant-b"), "run-1")


@pytest.mark.asyncio
async def test_pending_run_has_no_metrics() -> None:
    pending = replace(_run(), status="pending", metrics={}, completed_at=None)
    payload = await MemoryEffectivenessService(_Repository(pending)).run(
        _context("tenant-a"), "run-1"
    )
    assert payload["metrics"] is None
    assert "tenant_id" not in payload
    EffectivenessRunResponse.model_validate(payload)


@pytest.mark.asyncio
async def test_scenarios_redact_uncontrolled_template_values() -> None:
    scenario = {
        "dimension": "template_group", "value": "PRIVATE-SUPPLIER",
        "field_path": "invoice_number", "sample_count": 20,
        "review_reduction_vs_ocr": 0.25, "correct_field_delta": 1,
        "wrong_auto_passes": 0, "p95_extra_ms": 1000.0, "passed": True,
        "raw_invoice_value": "PRIVATE-INVOICE",
    }
    run = replace(_run(), scenarios=(scenario,))
    payload = await MemoryEffectivenessService(_Repository(run)).scenarios(
        _context("tenant-a")
    )
    assert "PRIVATE-SUPPLIER" not in str(payload)
    assert "PRIVATE-INVOICE" not in str(payload)
    assert payload["scenarios"][0]["value"].startswith("sha256:")
    assert payload["scenarios"][0]["passed"] is False
