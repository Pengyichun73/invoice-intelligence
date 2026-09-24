"""One-time verified migration from LocalFileStorage to configured S3 storage."""

import asyncio
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import StorageChecksumMismatchError
from invoice_intelligence.bootstrap import build_container, close_application_container
from invoice_intelligence.config.logging import configure_logging
from invoice_intelligence.domain.storage import ObjectKind, StorageWriteRequest
from invoice_intelligence.infrastructure.persistence.database import create_business_engine
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    DocumentRow,
    StoredObjectRow,
)
from invoice_intelligence.infrastructure.storage.local import LocalFileStorage


async def run() -> None:
    container = build_container(allow_legacy_storage_migration=True)
    configure_logging(container.settings, component="storage-migration")
    if container.settings.file_storage_backend != "s3":
        raise RuntimeError("Storage migration requires FILE_STORAGE_BACKEND=s3")
    source = LocalFileStorage(container.settings.file_storage_root)
    engine = create_business_engine(
        container.settings.resolved_business_database_url.get_secret_value()
    )
    sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    try:
        with sessions() as session:
            object_ids = tuple(
                session.scalars(
                    select(StoredObjectRow.object_id)
                    .where(StoredObjectRow.status == "migration_pending")
                    .order_by(StoredObjectRow.object_id)
                )
            )
        for object_id in object_ids:
            with sessions() as session:
                row = session.get(StoredObjectRow, object_id)
                if row is None or row.status != "migration_pending":
                    continue
                source_uri = row.storage_uri
                tenant_id = row.tenant_id
                media_type = row.media_type
                expected_checksum = row.checksum
            content = await source.read(source_uri)
            if sha256(content).hexdigest() != expected_checksum:
                raise StorageChecksumMismatchError("Legacy object checksum does not match")
            metadata = await container.file_storage.save_object(
                StorageWriteRequest(
                    object_id=object_id,
                    tenant_id=tenant_id,
                    kind=ObjectKind.ORIGINAL,
                    content=content,
                    media_type=media_type,
                    checksum=expected_checksum,
                )
            )
            verified = await container.file_storage.head(metadata.storage_uri)
            if verified.checksum != expected_checksum or verified.size_bytes != len(content):
                raise StorageChecksumMismatchError("Migrated object verification failed")
            with sessions.begin() as session:
                row = session.get(StoredObjectRow, object_id, with_for_update=True)
                document = session.get(DocumentRow, object_id, with_for_update=True)
                if row is None or document is None or row.status != "migration_pending":
                    continue
                row.storage_uri = metadata.storage_uri
                row.size_bytes = metadata.size_bytes
                row.status = "available"
                row.revision += 1
                document.storage_uri = metadata.storage_uri
                document.size_bytes = metadata.size_bytes
                document.storage_status = "available"
    finally:
        engine.dispose()
        await close_application_container(container)


if __name__ == "__main__":
    asyncio.run(run())
