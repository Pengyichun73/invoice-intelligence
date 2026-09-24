"""Deterministic routing policy for the fixed Harness state machine."""

from dataclasses import dataclass

from ..domain.errors import HarnessErrorCode
from ..domain.execution import ExecutionBudget, HarnessTaskStatus
from ..domain.watchdog import WatchdogObservation, WatchdogOutcome


@dataclass(frozen=True, slots=True)
class RepairRoute:
    status: HarnessTaskStatus
    next_stage: str | None
    failure_code: HarnessErrorCode | None


def route_after_watchdog(
    *,
    observation: WatchdogObservation,
    attempt_count: int,
    budget: ExecutionBudget,
) -> RepairRoute:
    if observation.outcome is WatchdogOutcome.SUCCESS:
        return RepairRoute(HarnessTaskStatus.SUCCEEDED, None, None)
    if observation.outcome in {
        WatchdogOutcome.HARD_FAILURE,
        WatchdogOutcome.STALL,
        WatchdogOutcome.RESOURCE_EXHAUSTED,
    }:
        return RepairRoute(
            HarnessTaskStatus.QUARANTINED,
            None,
            observation.failure_code or HarnessErrorCode.HARD_FAILURE,
        )
    if attempt_count >= budget.max_attempts:
        return RepairRoute(
            HarnessTaskStatus.QUARANTINED,
            None,
            HarnessErrorCode.BUDGET_EXHAUSTED,
        )
    return RepairRoute(
        HarnessTaskStatus.REPAIR_PENDING,
        "retrieve_code_context",
        observation.failure_code,
    )
