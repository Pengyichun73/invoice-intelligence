"""PostgreSQL source of truth for three-person gold annotation facts."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import (
    ResourceConflictError,
    ResourceNotFoundError,
)
from invoice_intelligence.application.ports.memory_gold import (
    GoldAnnotationFact,
    GoldCaseFact,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    DocumentRow,
    MemoryGoldAnnotationRow,
    MemoryGoldCaseRow,
)


class SQLAlchemyMemoryGoldRepository:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def get_case(self, tenant_id: str, document_id: str) -> GoldCaseFact | None:
        return await asyncio.to_thread(self._get_case_sync, tenant_id, document_id)

    async def add_annotation(
        self, tenant_id: str, document_id: str, document_checksum: str,
        template_group: str, versions: dict[str, str], actor_id: str,
        object_ref: str, checksum: str,
    ) -> GoldCaseFact:
        return await asyncio.to_thread(
            self._add_sync, tenant_id, document_id, document_checksum,
            template_group, versions, actor_id, object_ref, checksum,
        )

    async def freeze(
        self, tenant_id: str, document_id: str, expected_checksums: tuple[str, str],
        adjudicator_id: str, field_choices: dict[str, str],
        gold_ref: str, gold_checksum: str,
    ) -> GoldCaseFact:
        return await asyncio.to_thread(
            self._freeze_sync, tenant_id, document_id, expected_checksums,
            adjudicator_id, field_choices, gold_ref, gold_checksum,
        )

    def _get_case_sync(self, tenant_id: str, document_id: str) -> GoldCaseFact | None:
        with self._sessions() as session:
            row = session.scalar(select(MemoryGoldCaseRow).where(
                MemoryGoldCaseRow.tenant_id == tenant_id,
                MemoryGoldCaseRow.document_id == document_id,
            ))
            return self._to_fact(session, row) if row is not None else None

    def _add_sync(
        self, tenant_id: str, document_id: str, document_checksum: str,
        template_group: str, versions: dict[str, str], actor_id: str,
        object_ref: str, checksum: str,
    ) -> GoldCaseFact:
        try:
            with self._sessions.begin() as session:
                document = session.scalar(select(DocumentRow).where(
                    DocumentRow.tenant_id == tenant_id,
                    DocumentRow.document_id == document_id,
                ).with_for_update())
                if document is None:
                    raise ResourceNotFoundError("Document was not found")
                if document.checksum != document_checksum:
                    raise ResourceConflictError("Source document checksum changed")
                row = session.scalar(select(MemoryGoldCaseRow).where(
                    MemoryGoldCaseRow.tenant_id == tenant_id,
                    MemoryGoldCaseRow.document_id == document_id,
                ).with_for_update())
                if row is None:
                    row = MemoryGoldCaseRow(
                        tenant_id=tenant_id, document_id=document_id,
                        document_checksum=document_checksum, template_group=template_group,
                        versions_json=dict(versions), status="open", gold_ref=None,
                        gold_checksum=None, adjudicator_id=None,
                        field_choices_json=None,
                        created_at=datetime.now(UTC), frozen_at=None,
                    )
                    session.add(row)
                    session.flush()
                if row.status != "open" or row.template_group != template_group or (
                    row.versions_json != versions or row.document_checksum != document_checksum
                ):
                    raise ResourceConflictError("Gold case binding is fixed")
                annotations = self._annotations(session, tenant_id, document_id)
                previous = next((item for item in annotations if item.actor_id == actor_id), None)
                if previous is not None:
                    if previous.checksum != checksum:
                        raise ResourceConflictError(
                            "Annotator already submitted different evidence"
                        )
                    return self._to_fact(session, row)
                if len(annotations) >= 2:
                    raise ResourceConflictError("Gold case already has two annotators")
                session.add(MemoryGoldAnnotationRow(
                    annotation_id=str(uuid4()), tenant_id=tenant_id,
                    document_id=document_id, slot="first" if not annotations else "second",
                    actor_id=actor_id, object_ref=object_ref, checksum=checksum,
                    created_at=datetime.now(UTC),
                ))
                session.flush()
                return self._to_fact(session, row)
        except IntegrityError as exc:
            raise ResourceConflictError("Concurrent annotation changed the case") from exc

    def _freeze_sync(
        self, tenant_id: str, document_id: str, expected_checksums: tuple[str, str],
        adjudicator_id: str, field_choices: dict[str, str],
        gold_ref: str, gold_checksum: str,
    ) -> GoldCaseFact:
        with self._sessions.begin() as session:
            row = session.scalar(select(MemoryGoldCaseRow).where(
                MemoryGoldCaseRow.tenant_id == tenant_id,
                MemoryGoldCaseRow.document_id == document_id,
            ).with_for_update())
            if row is None:
                raise ResourceNotFoundError("Gold case was not found")
            annotations = self._annotations(session, tenant_id, document_id)
            if row.status != "open" or len(annotations) != 2 or (
                tuple(item.checksum for item in annotations) != expected_checksums
                or adjudicator_id in {item.actor_id for item in annotations}
            ):
                raise ResourceConflictError("Gold case changed before freeze")
            row.status = "frozen"
            row.gold_ref = gold_ref
            row.gold_checksum = gold_checksum
            row.adjudicator_id = adjudicator_id
            row.field_choices_json = dict(field_choices)
            row.frozen_at = datetime.now(UTC)
            session.flush()
            return self._to_fact(session, row)

    @staticmethod
    def _annotations(
        session: Session, tenant_id: str, document_id: str
    ) -> tuple[MemoryGoldAnnotationRow, ...]:
        return tuple(session.scalars(select(MemoryGoldAnnotationRow).where(
            MemoryGoldAnnotationRow.tenant_id == tenant_id,
            MemoryGoldAnnotationRow.document_id == document_id,
        ).order_by(MemoryGoldAnnotationRow.slot)))

    @classmethod
    def _to_fact(cls, session: Session, row: MemoryGoldCaseRow) -> GoldCaseFact:
        return GoldCaseFact(
            document_id=row.document_id, document_checksum=row.document_checksum,
            template_group=row.template_group, versions=dict(row.versions_json),
            status=row.status, annotations=tuple(
                GoldAnnotationFact(
                    actor_id=item.actor_id, slot=item.slot,
                    object_ref=item.object_ref, checksum=item.checksum,
                    created_at=item.created_at,
                )
                for item in cls._annotations(session, row.tenant_id, row.document_id)
            ),
            gold_ref=row.gold_ref, gold_checksum=row.gold_checksum,
            adjudicator_id=row.adjudicator_id,
            field_choices=(dict(row.field_choices_json)
                           if row.field_choices_json is not None else None),
            frozen_at=row.frozen_at,
        )
