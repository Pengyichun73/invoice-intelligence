"""Durable Snapshot, Patch, Execution and Watchdog fact boundary."""

from datetime import datetime
from typing import Protocol

from ...domain.patch_model import PatchProposal
from ...domain.repository import RepositorySnapshot
from ...domain.watchdog import WatchdogObservation


class HarnessFactRepository(Protocol):
    async def save_snapshot(self, snapshot: RepositorySnapshot, *, created_at: datetime) -> None:
        ...

    async def save_patch(self, proposal: PatchProposal, *, created_at: datetime) -> None:
        ...

    async def save_execution(
        self,
        *,
        execution_id: str,
        task_id: str,
        attempt_id: str,
        status: str,
        result_summary: str,
        error_signature: str | None,
        created_at: datetime,
    ) -> None:
        ...

    async def save_watchdog(
        self,
        *,
        observation_id: str,
        task_id: str,
        observation: WatchdogObservation,
        created_at: datetime,
    ) -> None:
        ...
