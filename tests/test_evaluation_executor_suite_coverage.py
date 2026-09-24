"""真实离线评估执行器必须覆盖两套 Suite 的既定变体。"""

from types import SimpleNamespace
from typing import Any

import pytest

from invoice_intelligence.application.services.evaluation_execution import (
    ALL_EVALUATION_VARIANTS,
    ConfiguredEvaluationVariantExecutor,
)
from invoice_intelligence.domain.evaluation import (
    EvaluationSuite,
    EvaluationVariant,
    required_variants_for_suite,
)


class _Runner:
    def __init__(self, variant: EvaluationVariant) -> None:
        self.variant = variant

    async def evaluate_case(
        self, case: Any, dataset: Any, bindings: Any, suite: EvaluationSuite
    ) -> Any:
        return SimpleNamespace(case_id=case.case_id, variant=self.variant)


def test_default_executor_requires_both_suite_runner_sets() -> None:
    expected = set(required_variants_for_suite(EvaluationSuite.CASE_RAG)) | set(
        required_variants_for_suite(EvaluationSuite.TRUSTED_MEMORY_FIELD_BINDING)
    )
    assert set(ALL_EVALUATION_VARIANTS) == expected
    case_only = {
        variant: _Runner(variant)
        for variant in required_variants_for_suite(EvaluationSuite.CASE_RAG)
    }
    with pytest.raises(ValueError, match="every suite variant runner"):
        ConfiguredEvaluationVariantExecutor(case_only, maximum_concurrency=1)


@pytest.mark.asyncio
async def test_executor_runs_trusted_memory_only_variant() -> None:
    executor = ConfiguredEvaluationVariantExecutor(
        {variant: _Runner(variant) for variant in ALL_EVALUATION_VARIANTS},
        maximum_concurrency=1,
    )
    dataset = SimpleNamespace(cases=(SimpleNamespace(case_id="case-1"),))
    observations = await executor.evaluate(
        dataset,
        EvaluationVariant.HYBRID_CONTEXT_ANCHORS,
        SimpleNamespace(),
        EvaluationSuite.TRUSTED_MEMORY_FIELD_BINDING,
    )
    assert len(observations) == 1
    assert observations[0].variant is EvaluationVariant.HYBRID_CONTEXT_ANCHORS

    with pytest.raises(ValueError, match="outside the selected suite"):
        await executor.evaluate(
            dataset,
            EvaluationVariant.HYBRID_CONTEXT_ANCHORS,
            SimpleNamespace(),
            EvaluationSuite.CASE_RAG,
        )
