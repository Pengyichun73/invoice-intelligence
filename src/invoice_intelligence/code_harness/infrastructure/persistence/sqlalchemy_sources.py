"""PostgreSQL-backed tenant-scoped Harness source registry."""

import asyncio

from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ...application.ports.source_registry import HarnessSourceRegistry
from ...domain.errors import HarnessError, HarnessErrorCode
from ...domain.repository import RepositorySource
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    CodeHarnessSourceRow,
)


class SQLAlchemyHarnessSourceRegistry(HarnessSourceRegistry):
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def list_enabled(self) -> tuple[RepositorySource, ...]:
        return await asyncio.to_thread(self._list_enabled)

    def _list_enabled(self) -> tuple[RepositorySource, ...]:
        try:
            with self._sessions() as session:
                rows = session.scalars(
                    select(CodeHarnessSourceRow)
                    .where(CodeHarnessSourceRow.enabled.is_(True))
                    .order_by(CodeHarnessSourceRow.repository_id)
                )
                return tuple(
                    RepositorySource(
                        repository_id=row.repository_id,
                        tenant_id=row.tenant_id,
                        root_path=row.root_path,
                        source_revision=row.source_revision,
                        repository_version=row.repository_version,
                    )
                    for row in rows
                )
        except SQLAlchemyError as exc:
            raise HarnessError(HarnessErrorCode.HARD_FAILURE) from exc
