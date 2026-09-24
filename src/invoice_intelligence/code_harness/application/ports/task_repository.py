"""Durable task fact boundary for the Harness."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ...domain.execution import HarnessAttempt, HarnessTask


@dataclass(frozen=True, slots=True)
class ClaimedHarnessTask:
    task: HarnessTask
    attempt: HarnessAttempt


class HarnessTaskRepository(Protocol):
    async def create_task(self, task: HarnessTask, *, idempotency_key_hash: str) -> HarnessTask:
        ...

    async def get_task(self, *, tenant_id: str, task_id: str) -> HarnessTask | None:
        ...

    async def save_attempt(self, attempt: HarnessAttempt) -> None:
        ...

    async def complete_attempt(
        self,
        *,
        attempt_id: str,
        task_id: str,
        revision: int,
        worker_id: str,
        lease_token: str,
        completed_at: datetime,
    ) -> None:
        ...

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> ClaimedHarnessTask | None:
        ...

    async def renew_claim(
        self,
        *,
        task_id: str,
        expected_revision: int,
        worker_id: str,
        lease_token: str,
        lease_seconds: int,
        now: datetime,
    ) -> HarnessTask:
        ...

    async def update_task(
        self,
        task: HarnessTask,
        *,
        expected_revision: int,
        lease_token: str | None,
    ) -> HarnessTask:
        ...
