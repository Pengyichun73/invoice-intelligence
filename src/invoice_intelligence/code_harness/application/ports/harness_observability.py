"""Privacy-safe Harness telemetry boundary."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Protocol

from ...domain.errors import require_text


class HarnessObservability(Protocol):
    def span(
        self,
        *,
        trace_id: str | None,
        tenant_id: str,
        stage: str,
        operation: str,
        attributes: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> AbstractContextManager[None]:
        ...


def validate_stage(stage: str) -> None:
    require_text("stage", stage, max_length=64)
    if not stage.startswith("code_harness."):
        raise ValueError("Harness telemetry stage must use code_harness prefix")
