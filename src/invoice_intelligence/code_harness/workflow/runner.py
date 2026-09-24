"""A small deterministic runner for the fixed Harness stages."""

from collections.abc import Awaitable, Callable

from .state import HarnessStage, HarnessState


StageHandler = Callable[[HarnessState], Awaitable[HarnessState]]

FIXED_STAGE_ORDER = (
    HarnessStage.PREPARE_TASK,
    HarnessStage.INSPECT_REPOSITORY,
    HarnessStage.RETRIEVE_CODE_CONTEXT,
    HarnessStage.GENERATE_PATCH,
    HarnessStage.VALIDATE_PATCH,
    HarnessStage.EXECUTE_IN_SANDBOX,
    HarnessStage.EVALUATE_RESULT,
    HarnessStage.REPAIR_OR_FINISH,
)


class HarnessRunner:
    def __init__(self, handlers: dict[HarnessStage, StageHandler]) -> None:
        missing = set(FIXED_STAGE_ORDER) - set(handlers)
        if missing:
            raise ValueError(f"missing fixed Harness stage handlers: {sorted(missing)}")
        self._handlers = handlers

    async def run(self, state: HarnessState) -> HarnessState:
        current = state
        while True:
            next_state = await self._handlers[current.stage](current)
            if next_state.stage is current.stage:
                return next_state
            current = next_state

