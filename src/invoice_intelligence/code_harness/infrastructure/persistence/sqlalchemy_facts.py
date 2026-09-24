"""PostgreSQL persistence for immutable Harness execution facts."""

import asyncio
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ...application.ports.facts import HarnessFactRepository
from ...domain.errors import HarnessError, HarnessErrorCode
from ...domain.patch_model import PatchProposal
from ...domain.repository import RepositorySnapshot
from ...domain.watchdog import WatchdogObservation
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    CodeHarnessExecutionRow,
    CodeHarnessPatchRow,
    CodeHarnessSnapshotRow,
    CodeHarnessWatchdogRow,
)


class SQLAlchemyHarnessFactRepository(HarnessFactRepository):
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def save_snapshot(self, snapshot: RepositorySnapshot, *, created_at: datetime) -> None:
        await asyncio.to_thread(self._save_snapshot, snapshot, created_at)

    def _save_snapshot(self, snapshot: RepositorySnapshot, created_at: datetime) -> None:
        values = {
            "snapshot_id": snapshot.snapshot_id,
            "tenant_id": snapshot.tenant_id,
            "repository_id": snapshot.repository_id,
            "revision": snapshot.revision,
            "source_revision": snapshot.source_revision,
            "manifest_checksum_sha256": snapshot.manifest_checksum_sha256,
            "files_json": [
                {
                    "path": item.path,
                    "checksum_sha256": item.checksum_sha256,
                    "size_bytes": item.size_bytes,
                    "kind": item.kind.value,
                    "mode": item.mode,
                }
                for item in snapshot.files
            ],
            "versions_json": snapshot.versions.as_dict(),
            "created_at": created_at,
        }
        try:
            with self._sessions.begin() as session:
                existing = session.get(CodeHarnessSnapshotRow, snapshot.snapshot_id)
                if existing is not None:
                    if not _same_snapshot(existing, values):
                        raise HarnessError(HarnessErrorCode.SNAPSHOT_CAPTURE_CONFLICT)
                    return
                session.add(CodeHarnessSnapshotRow(**values))
        except HarnessError:
            raise
        except IntegrityError as exc:
            raise HarnessError(HarnessErrorCode.SNAPSHOT_CAPTURE_CONFLICT) from exc
        except SQLAlchemyError as exc:
            raise HarnessError(HarnessErrorCode.HARD_FAILURE) from exc

    async def save_patch(self, proposal: PatchProposal, *, created_at: datetime) -> None:
        await asyncio.to_thread(self._save_patch, proposal, created_at)

    def _save_patch(self, proposal: PatchProposal, created_at: datetime) -> None:
        values = {
            "patch_id": proposal.patch_id,
            "tenant_id": proposal.tenant_id,
            "repository_id": proposal.repository_id,
            "snapshot_id": proposal.snapshot_id,
            "snapshot_revision": proposal.snapshot_revision,
            "operations_json": [
                {
                    "path": item.path,
                    "kind": item.kind.value,
                    "start_byte": item.start_byte,
                    "end_byte": item.end_byte,
                    "payload_utf8": item.payload_utf8.decode("utf-8"),
                    "base_checksum_sha256": item.base_checksum_sha256,
                    "anchor": item.anchor,
                }
                for item in proposal.operations
            ],
            "patch_checksum_sha256": proposal.patch_checksum_sha256,
            "fingerprint": proposal.fingerprint,
            "created_at": created_at,
        }
        try:
            with self._sessions.begin() as session:
                existing = session.get(CodeHarnessPatchRow, proposal.patch_id)
                if existing is not None:
                    if not _same_patch(existing, values):
                        raise HarnessError(HarnessErrorCode.PATCH_INVALID)
                    return
                session.add(CodeHarnessPatchRow(**values))
        except HarnessError:
            raise
        except IntegrityError as exc:
            raise HarnessError(HarnessErrorCode.PATCH_INVALID) from exc
        except SQLAlchemyError as exc:
            raise HarnessError(HarnessErrorCode.HARD_FAILURE) from exc

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
        await asyncio.to_thread(
            self._save_execution,
            execution_id,
            task_id,
            attempt_id,
            status,
            result_summary,
            error_signature,
            created_at,
        )

    def _save_execution(
        self,
        execution_id: str,
        task_id: str,
        attempt_id: str,
        status: str,
        result_summary: str,
        error_signature: str | None,
        created_at: datetime,
    ) -> None:
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(CodeHarnessExecutionRow).where(
                        CodeHarnessExecutionRow.execution_id == execution_id
                    )
                )
                if existing is None:
                    session.add(
                        CodeHarnessExecutionRow(
                            execution_id=execution_id,
                            task_id=task_id,
                            attempt_id=attempt_id,
                            status=status,
                            result_summary=result_summary[:4096],
                            error_signature=error_signature,
                            created_at=created_at,
                        )
                    )
                    return
                if (
                    existing.task_id != task_id
                    or existing.attempt_id != attempt_id
                    or existing.status != status
                    or existing.result_summary != result_summary[:4096]
                    or existing.error_signature != error_signature
                ):
                    raise HarnessError(HarnessErrorCode.IDEMPOTENCY_CONFLICT)
        except IntegrityError as exc:
            raise HarnessError(HarnessErrorCode.IDEMPOTENCY_CONFLICT) from exc
        except SQLAlchemyError as exc:
            raise HarnessError(HarnessErrorCode.HARD_FAILURE) from exc

    async def save_watchdog(
        self,
        *,
        observation_id: str,
        task_id: str,
        observation: WatchdogObservation,
        created_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._save_watchdog,
            observation_id,
            task_id,
            observation,
            created_at,
        )

    def _save_watchdog(
        self,
        observation_id: str,
        task_id: str,
        observation: WatchdogObservation,
        created_at: datetime,
    ) -> None:
        values = {
            "observation_id": observation_id,
            "task_id": task_id,
            "attempt_id": observation.attempt_id,
            "outcome": observation.outcome.value,
            "error_signature": observation.error_signature,
            "changed_ast_fingerprint": observation.changed_ast_fingerprint,
            "changed_symbols": observation.changed_symbols,
            "changed_diagnostics": observation.changed_diagnostics,
            "failure_code": observation.failure_code.value if observation.failure_code else None,
            "created_at": created_at,
        }
        try:
            with self._sessions.begin() as session:
                existing = session.get(CodeHarnessWatchdogRow, observation_id)
                if existing is not None:
                    if not _same_watchdog(existing, values):
                        raise HarnessError(HarnessErrorCode.IDEMPOTENCY_CONFLICT)
                    return
                session.add(CodeHarnessWatchdogRow(**values))
        except HarnessError:
            raise
        except IntegrityError as exc:
            raise HarnessError(HarnessErrorCode.IDEMPOTENCY_CONFLICT) from exc
        except SQLAlchemyError as exc:
            raise HarnessError(HarnessErrorCode.HARD_FAILURE) from exc


def _same_snapshot(row: CodeHarnessSnapshotRow, values: dict[str, Any]) -> bool:
    return all(
        getattr(row, key) == value
        for key, value in values.items()
        if key != "created_at"
    )


def _same_patch(row: CodeHarnessPatchRow, values: dict[str, Any]) -> bool:
    return all(
        getattr(row, key) == value
        for key, value in values.items()
        if key != "created_at"
    )


def _same_watchdog(row: CodeHarnessWatchdogRow, values: dict[str, Any]) -> bool:
    return all(
        getattr(row, key) == value
        for key, value in values.items()
        if key != "created_at"
    )
