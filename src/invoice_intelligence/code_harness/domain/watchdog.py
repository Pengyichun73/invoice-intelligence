"""Deterministic execution and repair-loop observations."""

from dataclasses import dataclass
from enum import StrEnum

from .errors import HarnessErrorCode, require_text


class WatchdogOutcome(StrEnum):
    SUCCESS = "success"
    EXACT_REPEAT = "exact_repeat"
    JITTER = "jitter"
    STALL = "stall"
    BUDGET_EXHAUSTED = "budget_exhausted"
    HARD_FAILURE = "hard_failure"
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTED = "resource_exhausted"


@dataclass(frozen=True, slots=True)
class WatchdogObservation:
    attempt_id: str
    outcome: WatchdogOutcome
    error_signature: str | None
    changed_ast_fingerprint: bool
    changed_symbols: bool
    changed_diagnostics: bool
    failure_code: HarnessErrorCode | None = None

    def __post_init__(self) -> None:
        require_text("attempt_id", self.attempt_id)
        if self.error_signature is not None:
            require_text("error_signature", self.error_signature, max_length=512)
        if self.outcome is WatchdogOutcome.SUCCESS and self.failure_code is not None:
            raise ValueError("successful observation cannot have failure code")
        if self.outcome is not WatchdogOutcome.SUCCESS and self.failure_code is None:
            raise ValueError("failed observation requires failure code")

    @property
    def materially_improved(self) -> bool:
        return self.changed_ast_fingerprint or self.changed_symbols or self.changed_diagnostics

