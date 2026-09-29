"""Tenant-scoped invoice-batch state and fenced segmentation claims."""

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import IdempotencyConflictError, ResourceConflictError, ResourceNotFoundError
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    ExtractionRunRow, InvoiceBatchFileRow, InvoiceBatchItemRow, InvoiceBatchRow,
)


class SQLAlchemyInvoiceBatchRepository:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    async def create(self, tenant_id: str, key: str, actor_id: str) -> str:
        return await asyncio.to_thread(self._create, tenant_id, key, actor_id)

    def _create(self, tenant_id: str, key: str, actor_id: str) -> str:
        digest = sha256(f"{tenant_id}\0invoice-batch\0{key}".encode()).hexdigest()
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(select(InvoiceBatchRow).where(
                    InvoiceBatchRow.tenant_id == tenant_id,
                    InvoiceBatchRow.idempotency_hash == digest,
                ))
                if existing is not None:
                    if existing.created_by != actor_id:
                        raise IdempotencyConflictError("Invoice batch key belongs to another actor")
                    return existing.batch_id
                now = datetime.now(UTC)
                batch_id = f"batch_{uuid4().hex}"
                session.add(InvoiceBatchRow(
                    batch_id=batch_id, tenant_id=tenant_id, created_by=actor_id, idempotency_hash=digest,
                    status="open", revision=1, created_at=now, updated_at=now,
                ))
                return batch_id
        except IntegrityError:
            with self._sessions() as session:
                existing = session.scalar(select(InvoiceBatchRow).where(
                    InvoiceBatchRow.tenant_id == tenant_id,
                    InvoiceBatchRow.idempotency_hash == digest,
                ))
                if existing is None:
                    raise
                if existing.created_by != actor_id:
                    raise IdempotencyConflictError("Invoice batch key belongs to another actor")
                return existing.batch_id

    async def list_recent(self, tenant_id: str, actor_id: str, limit: int) -> list[dict]:
        return await asyncio.to_thread(self._list_recent, tenant_id, actor_id, limit)

    def _list_recent(self, tenant_id: str, actor_id: str, limit: int) -> list[dict]:
        with self._sessions() as session:
            rows = session.scalars(select(InvoiceBatchRow).where(
                InvoiceBatchRow.tenant_id == tenant_id,
                InvoiceBatchRow.created_by == actor_id,
            ).order_by(InvoiceBatchRow.created_at.desc(), InvoiceBatchRow.batch_id.desc()).limit(limit)).all()
            if not rows:
                return []
            statuses: dict[str, list[str | None]] = {row.batch_id: [] for row in rows}
            for batch_id, run_status in session.execute(
                select(InvoiceBatchItemRow.batch_id, ExtractionRunRow.status)
                .outerjoin(ExtractionRunRow, ExtractionRunRow.run_id == InvoiceBatchItemRow.run_id)
                .where(InvoiceBatchItemRow.tenant_id == tenant_id,
                       InvoiceBatchItemRow.batch_id.in_(statuses))
            ):
                statuses[batch_id].append(run_status)
            return [{"batch_id": row.batch_id,
                     "status": self._visible_status(row.status, statuses[row.batch_id]),
                     "created_at": row.created_at} for row in rows]

    @staticmethod
    def _visible_status(stage: str, run_statuses: list[str | None]) -> str:
        if stage != "dispatched" or not run_statuses:
            return stage
        if any(status == "failed" for status in run_statuses):
            return "failed"
        if any(status == "pending_review" for status in run_statuses):
            return "pending_review"
        if all(status == "completed" for status in run_statuses):
            return "completed"
        if any(status in ("received", "processing") for status in run_statuses):
            return "extracting"
        return stage

    async def attach_file(self, batch_id: str, tenant_id: str, document_id: str, filename: str | None) -> str:
        return await asyncio.to_thread(self._attach_file, batch_id, tenant_id, document_id, filename)

    def _attach_file(self, batch_id: str, tenant_id: str, document_id: str, filename: str | None) -> str:
        with self._sessions.begin() as session:
            batch = self._get_locked(session, batch_id, tenant_id)
            existing = session.scalar(select(InvoiceBatchFileRow).where(
                InvoiceBatchFileRow.batch_id == batch_id,
                InvoiceBatchFileRow.document_id == document_id,
            ))
            if existing is not None:
                return existing.file_id
            if batch.status != "open":
                raise ResourceConflictError("Invoice batch is already submitted")
            files = session.scalars(select(InvoiceBatchFileRow).where(
                InvoiceBatchFileRow.batch_id == batch_id,
            )).all()
            if len(files) >= 5:
                raise ResourceConflictError("Invoice batch accepts at most five files")
            now = datetime.now(UTC)
            file_id = f"file_{uuid4().hex}"
            session.add(InvoiceBatchFileRow(
                file_id=file_id, batch_id=batch_id, tenant_id=tenant_id,
                document_id=document_id, ordinal=len(files) + 1,
                filename=(filename or "")[:256] or None, status="queued", revision=1,
                page_count=None, groups_json=None, error_code=None, lease_token=None,
                lease_expires_at=None, attempt_count=0, created_at=now, updated_at=now,
                next_attempt_at=None,
            ))
            batch.revision += 1
            batch.updated_at = now
            return file_id

    async def submit(self, batch_id: str, tenant_id: str, revision: int) -> None:
        await asyncio.to_thread(self._submit, batch_id, tenant_id, revision)

    def _submit(self, batch_id: str, tenant_id: str, revision: int) -> None:
        with self._sessions.begin() as session:
            batch = self._get_locked(session, batch_id, tenant_id)
            if batch.status == "segmenting":
                return
            if batch.status != "open" or batch.revision != revision:
                raise ResourceConflictError("Invoice batch revision is stale")
            if session.scalar(select(InvoiceBatchFileRow.file_id).where(
                InvoiceBatchFileRow.batch_id == batch_id,
            )) is None:
                raise ResourceConflictError("Invoice batch has no files")
            batch.status = "segmenting"
            batch.revision += 1
            batch.updated_at = datetime.now(UTC)

    async def get(self, batch_id: str, tenant_id: str) -> dict:
        return await asyncio.to_thread(self._get, batch_id, tenant_id)

    def _get(self, batch_id: str, tenant_id: str) -> dict:
        with self._sessions() as session:
            batch = self._get_locked(session, batch_id, tenant_id)
            files = session.scalars(select(InvoiceBatchFileRow).where(
                InvoiceBatchFileRow.batch_id == batch_id,
            ).order_by(InvoiceBatchFileRow.ordinal)).all()
            items = session.scalars(select(InvoiceBatchItemRow).where(
                InvoiceBatchItemRow.batch_id == batch_id,
            ).order_by(InvoiceBatchItemRow.ordinal)).all()
            run_statuses = list(session.scalars(select(ExtractionRunRow.status).where(
                ExtractionRunRow.tenant_id == tenant_id,
                ExtractionRunRow.run_id.in_([item.run_id for item in items if item.run_id]),
            )).all())
            if len(run_statuses) < len(items):
                run_statuses.extend([None] * (len(items) - len(run_statuses)))
            return {
                "batch_id": batch.batch_id,
                "status": self._visible_status(batch.status, run_statuses),
                "revision": batch.revision,
                "files": [{
                    "file_id": file.file_id, "document_id": file.document_id,
                    "filename": file.filename, "ordinal": file.ordinal, "status": file.status,
                    "revision": file.revision, "page_count": file.page_count,
                    "groups": file.groups_json or [], "error_code": file.error_code,
                } for file in files],
                "items": [{
                    "item_id": item.item_id, "file_id": item.file_id,
                    "ordinal": item.ordinal, "regions": item.regions_json,
                    "document_id": item.document_id, "run_id": item.run_id,
                    "run_attempt": item.run_attempt,
                    "derived_pages": item.derived_pages_json or [],
                } for item in items],
            }

    async def claim(self, lease_seconds: int = 900) -> dict | None:
        return await asyncio.to_thread(self._claim, lease_seconds)

    def _claim(self, lease_seconds: int) -> dict | None:
        with self._sessions.begin() as session:
            now = datetime.now(UTC)
            row = session.scalar(select(InvoiceBatchFileRow).join(
                InvoiceBatchRow, InvoiceBatchRow.batch_id == InvoiceBatchFileRow.batch_id,
            ).where(
                InvoiceBatchRow.status.in_(("segmenting", "needs_boundary_review")),
                InvoiceBatchFileRow.status.in_(("queued", "processing")),
                (InvoiceBatchFileRow.lease_expires_at.is_(None)) |
                (InvoiceBatchFileRow.lease_expires_at < now),
                InvoiceBatchFileRow.attempt_count < 3,
                (InvoiceBatchFileRow.next_attempt_at.is_(None)) |
                (InvoiceBatchFileRow.next_attempt_at <= now),
            ).order_by(InvoiceBatchFileRow.created_at).with_for_update(skip_locked=True))
            if row is None:
                return None
            token = uuid4().hex
            row.status = "processing"
            row.lease_token = token
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.attempt_count += 1
            row.next_attempt_at = None
            row.updated_at = now
            return {"file_id": row.file_id, "batch_id": row.batch_id,
                    "tenant_id": row.tenant_id, "document_id": row.document_id,
                    "lease_token": token}

    async def propose(self, claim: dict, page_count: int, groups: list[dict], uncertain: bool) -> None:
        await asyncio.to_thread(self._propose, claim, page_count, groups, uncertain)

    async def renew(self, claim: dict, lease_seconds: int = 900) -> bool:
        return await asyncio.to_thread(self._renew, claim, lease_seconds)

    def _renew(self, claim: dict, lease_seconds: int) -> bool:
        with self._sessions.begin() as session:
            row = session.get(InvoiceBatchFileRow, claim["file_id"], with_for_update=True)
            if row is None or row.lease_token != claim["lease_token"]:
                return False
            row.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
            return True

    def _propose(self, claim: dict, page_count: int, groups: list[dict], uncertain: bool) -> None:
        with self._sessions.begin() as session:
            row = session.get(InvoiceBatchFileRow, claim["file_id"], with_for_update=True)
            if row is None or row.lease_token != claim["lease_token"]:
                raise ResourceConflictError("Invoice segmentation lease was lost")
            row.page_count = page_count
            row.groups_json = groups
            row.status = "needs_review" if uncertain else "proposed"
            row.revision += 1
            row.lease_token = None
            row.lease_expires_at = None
            row.next_attempt_at = (
                None if row.status == "failed" else
                datetime.now(UTC) + timedelta(seconds=min(60, 2 ** row.attempt_count))
            )
            row.updated_at = datetime.now(UTC)
            batch = self._get_locked(session, row.batch_id, row.tenant_id)
            files = session.scalars(select(InvoiceBatchFileRow).where(
                InvoiceBatchFileRow.batch_id == row.batch_id,
            )).all()
            if sum(len(file.groups_json or []) for file in files) > 5:
                batch.status = "too_many_invoices"
                batch.revision += 1
            elif uncertain:
                batch.status = "needs_boundary_review"
                batch.revision += 1

    async def fail_claim(self, claim: dict, code: str) -> None:
        await asyncio.to_thread(self._fail_claim, claim, code)

    def _fail_claim(self, claim: dict, code: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(InvoiceBatchFileRow, claim["file_id"], with_for_update=True)
            if row is None or row.lease_token != claim["lease_token"]:
                return
            row.error_code = code[:128]
            row.status = "failed" if row.attempt_count >= 3 else "queued"
            row.lease_token = None
            row.lease_expires_at = None
            if row.status == "failed":
                batch = self._get_locked(session, row.batch_id, row.tenant_id)
                batch.status = "failed"
                batch.revision += 1

    async def confirm(self, batch_id: str, file_id: str, tenant_id: str,
                      revision: int, page_count: int, groups: list[dict]) -> None:
        await asyncio.to_thread(self._confirm, batch_id, file_id, tenant_id,
                                revision, page_count, groups)

    def _confirm(self, batch_id: str, file_id: str, tenant_id: str,
                 revision: int, page_count: int, groups: list[dict]) -> None:
        with self._sessions.begin() as session:
            batch = self._get_locked(session, batch_id, tenant_id)
            row = session.get(InvoiceBatchFileRow, file_id, with_for_update=True)
            if row is None or row.batch_id != batch_id or row.tenant_id != tenant_id:
                raise ResourceNotFoundError("Invoice batch file was not found")
            if batch.status != "needs_boundary_review" or row.status != "needs_review":
                raise ResourceConflictError("Invoice boundaries cannot be changed now")
            if row.revision != revision or row.page_count != page_count:
                raise ResourceConflictError("Invoice boundary revision is stale")
            row.groups_json = groups
            row.status = "proposed"
            row.revision += 1
            batch.revision += 1
            batch.updated_at = datetime.now(UTC)

    async def prepare_items(self, batch_id: str, tenant_id: str) -> list[dict] | None:
        return await asyncio.to_thread(self._prepare_items, batch_id, tenant_id)

    def _prepare_items(self, batch_id: str, tenant_id: str) -> list[dict] | None:
        with self._sessions.begin() as session:
            batch = self._get_locked(session, batch_id, tenant_id)
            if batch.status not in ("segmenting", "needs_boundary_review", "extracting"):
                return None
            files = session.scalars(select(InvoiceBatchFileRow).where(
                InvoiceBatchFileRow.batch_id == batch_id,
            ).order_by(InvoiceBatchFileRow.ordinal).with_for_update()).all()
            if not files or any(file.status != "proposed" for file in files):
                return None
            groups = [(file, group) for file in files for group in (file.groups_json or [])]
            if len(groups) > 5:
                batch.status = "too_many_invoices"
                batch.revision += 1
                return None
            if not groups:
                batch.status = "failed"
                return None
            existing = session.scalars(select(InvoiceBatchItemRow).where(
                InvoiceBatchItemRow.batch_id == batch_id,
            )).all()
            if not existing:
                now = datetime.now(UTC)
                for ordinal, (file, group) in enumerate(groups, 1):
                    session.add(InvoiceBatchItemRow(
                        item_id=f"item_{uuid4().hex}", batch_id=batch_id,
                        file_id=file.file_id, tenant_id=tenant_id, ordinal=ordinal,
                        regions_json=group["regions"], document_id=None,
                        derived_pages_json=None,
                        run_id=None, run_attempt=1, retry_key_hash=None,
                        created_at=now,
                    ))
                session.flush()
            if batch.status != "extracting":
                batch.status = "extracting"
                batch.revision += 1
            items = session.scalars(select(InvoiceBatchItemRow).where(
                InvoiceBatchItemRow.batch_id == batch_id,
            ).order_by(InvoiceBatchItemRow.ordinal)).all()
            return [{"item_id": item.item_id, "file_id": item.file_id,
                     "regions": item.regions_json, "document_id": item.document_id,
                     "run_id": item.run_id, "run_attempt": item.run_attempt} for item in items]

    async def retry_item(self, batch_id: str, item_id: str, tenant_id: str,
                         expected_run_id: str, key: str) -> None:
        await asyncio.to_thread(self._retry_item, batch_id, item_id, tenant_id,
                                expected_run_id, key)

    def _retry_item(self, batch_id: str, item_id: str, tenant_id: str,
                    expected_run_id: str, key: str) -> None:
        digest = sha256(f"{tenant_id}\0{key}".encode()).hexdigest()
        with self._sessions.begin() as session:
            batch = self._get_locked(session, batch_id, tenant_id)
            item = session.get(InvoiceBatchItemRow, item_id, with_for_update=True)
            if item is None or item.batch_id != batch_id or item.tenant_id != tenant_id:
                raise ResourceNotFoundError("Invoice batch item was not found")
            if item.retry_key_hash == digest:
                return
            if item.run_id != expected_run_id:
                raise ResourceConflictError("Invoice batch item run changed")
            run = session.scalar(select(ExtractionRunRow).where(
                ExtractionRunRow.run_id == expected_run_id,
                ExtractionRunRow.tenant_id == tenant_id,
            ))
            if run is None or run.status != "failed":
                raise ResourceConflictError("Only a failed invoice run can be retried")
            item.run_id = None
            item.run_attempt += 1
            item.retry_key_hash = digest
            batch.status = "extracting"
            batch.revision += 1
            batch.updated_at = datetime.now(UTC)

    async def ready_batches(self) -> list[tuple[str, str]]:
        return await asyncio.to_thread(self._ready_batches)

    def _ready_batches(self) -> list[tuple[str, str]]:
        with self._sessions() as session:
            rows = session.scalars(select(InvoiceBatchRow).where(
                InvoiceBatchRow.status.in_(("segmenting", "needs_boundary_review", "extracting")),
            ).order_by(InvoiceBatchRow.created_at).limit(20)).all()
            return [(row.batch_id, row.tenant_id) for row in rows]

    async def set_item(self, item_id: str, tenant_id: str,
                       document_id: str, run_id: str | None,
                       derived_pages: list[dict[str, int]]) -> None:
        await asyncio.to_thread(self._set_item, item_id, tenant_id, document_id,
                                run_id, derived_pages)

    def _set_item(self, item_id: str, tenant_id: str,
                  document_id: str, run_id: str | None,
                  derived_pages: list[dict[str, int]]) -> None:
        with self._sessions.begin() as session:
            item = session.get(InvoiceBatchItemRow, item_id, with_for_update=True)
            if item is None or item.tenant_id != tenant_id:
                raise ResourceNotFoundError("Invoice batch item was not found")
            if item.document_id is not None and item.document_id != document_id:
                raise ResourceConflictError("Invoice batch item already has a different document")
            if item.run_id is not None and run_id is not None and item.run_id != run_id:
                raise ResourceConflictError("Invoice batch item already has a different run")
            item.document_id = document_id
            if run_id is not None:
                item.run_id = run_id
            item.derived_pages_json = derived_pages
            if run_id is None:
                return
            session.flush()
            pending = session.scalar(select(InvoiceBatchItemRow.item_id).where(
                InvoiceBatchItemRow.batch_id == item.batch_id,
                InvoiceBatchItemRow.run_id.is_(None),
            ))
            if pending is None:
                batch = self._get_locked(session, item.batch_id, tenant_id)
                batch.status = "dispatched"
                batch.revision += 1

    async def source_for_child(self, document_id: str, tenant_id: str) -> dict | None:
        return await asyncio.to_thread(self._source_for_child, document_id, tenant_id)

    def _source_for_child(self, document_id: str, tenant_id: str) -> dict | None:
        with self._sessions() as session:
            item = session.scalar(select(InvoiceBatchItemRow).where(
                InvoiceBatchItemRow.document_id == document_id,
                InvoiceBatchItemRow.tenant_id == tenant_id,
            ))
            if item is None:
                return None
            source = session.scalar(select(InvoiceBatchFileRow).where(
                InvoiceBatchFileRow.file_id == item.file_id,
                InvoiceBatchFileRow.tenant_id == tenant_id,
            ))
            if source is None:
                return None
            siblings = session.scalars(select(InvoiceBatchItemRow).where(
                InvoiceBatchItemRow.file_id == item.file_id,
                InvoiceBatchItemRow.tenant_id == tenant_id,
                InvoiceBatchItemRow.item_id != item.item_id,
            )).all()
            return {
                "source_document_id": source.document_id,
                "regions": item.regions_json,
                "other_regions": [
                    region for sibling in siblings for region in sibling.regions_json
                ],
                "derived_pages": item.derived_pages_json or [],
            }

    @staticmethod
    def _get_locked(session: Session, batch_id: str, tenant_id: str) -> InvoiceBatchRow:
        row = session.scalar(select(InvoiceBatchRow).where(
            InvoiceBatchRow.batch_id == batch_id,
            InvoiceBatchRow.tenant_id == tenant_id,
        ).with_for_update())
        if row is None:
            raise ResourceNotFoundError("Invoice batch was not found")
        return row
