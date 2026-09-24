"""Bounded execution of explicitly configured offline evaluation pipelines."""

import asyncio
from collections.abc import Mapping, Sequence

from invoice_intelligence.application.ports.evaluation import EvaluationVariantRunner
from invoice_intelligence.domain.evaluation import (
    EvaluationBindings,
    EvaluationCaseObservation,
    EvaluationDataset,
    EvaluationSuite,
    EvaluationVariant,
    required_variants_for_suite,
)

ALL_EVALUATION_VARIANTS = tuple(dict.fromkeys(
    (*required_variants_for_suite(EvaluationSuite.CASE_RAG),
     *required_variants_for_suite(EvaluationSuite.TRUSTED_MEMORY_FIELD_BINDING))
))


class ConfiguredEvaluationVariantExecutor:
    """Run every required variant with bounded concurrency and stable case ordering."""

    def __init__(
        self,
        runners: Mapping[EvaluationVariant, EvaluationVariantRunner],
        *,
        maximum_concurrency: int,
        required_variants: tuple[EvaluationVariant, ...] = ALL_EVALUATION_VARIANTS,
    ) -> None:
        if not required_variants or len(required_variants) != len(set(required_variants)):
            raise ValueError("Offline evaluation required variants must be non-empty and unique")
        if set(runners) != set(required_variants):
            raise ValueError("Offline evaluation must configure every suite variant runner")
        if maximum_concurrency <= 0:
            raise ValueError("Offline evaluation concurrency must be greater than zero")
        self._runners = dict(runners)
        self._maximum_concurrency = maximum_concurrency

    async def evaluate(
        self,
        dataset: EvaluationDataset,
        variant: EvaluationVariant,
        bindings: EvaluationBindings,
        suite: EvaluationSuite,
    ) -> Sequence[EvaluationCaseObservation]:
        try:
            runner = self._runners[variant]
        except KeyError as exc:
            raise ValueError("Offline evaluation variant runner is unavailable") from exc
        if variant not in required_variants_for_suite(suite):
            raise ValueError("Offline evaluation variant is outside the selected suite")
        semaphore = asyncio.Semaphore(self._maximum_concurrency)

        async def evaluate_case(case_index: int) -> EvaluationCaseObservation:
            async with semaphore:
                observation = await runner.evaluate_case(
                    dataset.cases[case_index],
                    dataset,
                    bindings,
                    suite,
                )
            if observation.variant is not variant:
                raise ValueError("Evaluation runner returned the wrong variant identity")
            return observation

        return tuple(
            await asyncio.gather(
                *(evaluate_case(index) for index in range(len(dataset.cases)))
            )
        )
