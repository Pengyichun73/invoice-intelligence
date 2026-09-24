"""Local filesystem implementation of the file storage boundary."""

import asyncio
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from invoice_intelligence.application.errors import (
    StorageChecksumMismatchError,
    StorageError,
    StorageInvalidReferenceError,
    StorageObjectNotFoundError,
)
from invoice_intelligence.domain.storage import (
    ObjectKind,
    StorageObjectMetadata,
    StorageWriteRequest,
)

_EXTENSION_BY_MIME = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_STORED_NAME_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"\.(?:jpg|json|pdf|png|webp)$"
)


class LocalFileStorage:
    """Store immutable originals below one configured local directory."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()

    async def save(self, document_id: str, content: bytes, mime_type: str) -> str:
        """Atomically persist bytes and return a traversal-safe local URI."""

        return await asyncio.to_thread(self._save_sync, document_id, content, mime_type)

    async def read(self, storage_uri: str) -> bytes:
        """Read bytes from a URI owned by this adapter."""

        return await asyncio.to_thread(self._read_sync, storage_uri)

    async def save_object(self, request: StorageWriteRequest) -> StorageObjectMetadata:
        return await asyncio.to_thread(self._save_object_sync, request)

    async def head(self, storage_uri: str) -> StorageObjectMetadata:
        return await asyncio.to_thread(self._head_sync, storage_uri)

    async def delete(self, storage_uri: str) -> None:
        await asyncio.to_thread(self._delete_sync, storage_uri)

    def _save_sync(self, document_id: str, content: bytes, mime_type: str) -> str:
        try:
            normalized_id = str(UUID(document_id))
        except ValueError as exc:
            raise StorageError("document_id must be a valid UUID") from exc

        extension = _EXTENSION_BY_MIME.get(mime_type)
        if extension is None:
            raise StorageError("Cannot store an unsupported media type")

        filename = f"{normalized_id}{extension}"
        target = self._safe_path(filename)
        self._root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise StorageError("A document with the same identifier already exists")

        temporary_path: Path | None = None
        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                dir=self._root,
                prefix=".upload-",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                raise StorageError("A document with the same identifier already exists")
            os.replace(temporary_path, target)
            temporary_path = None
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError("Unable to persist the uploaded document") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

        return f"local://{filename}"

    def _read_sync(self, storage_uri: str) -> bytes:
        target, _ = self._resolve_uri(storage_uri)
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise StorageObjectNotFoundError("Stored object does not exist") from exc
        except OSError as exc:
            raise StorageError("Unable to read the stored document") from exc

    def _save_object_sync(self, request: StorageWriteRequest) -> StorageObjectMetadata:
        if sha256(request.content).hexdigest() != request.checksum:
            raise StorageChecksumMismatchError("Content checksum does not match request")
        extension = _EXTENSION_BY_MIME.get(request.media_type, ".json")
        filename = f"{request.object_id}{extension}"
        relative = f"{request.kind.value}/{filename}"
        target = self._safe_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = target.read_bytes()
            if sha256(existing).hexdigest() != request.checksum:
                raise StorageChecksumMismatchError("Immutable object key has different content")
        else:
            temporary_path: Path | None = None
            try:
                fd, name = tempfile.mkstemp(dir=target.parent, prefix=".upload-", suffix=".tmp")
                temporary_path = Path(name)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(request.content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, target)
                temporary_path = None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        return StorageObjectMetadata(
            storage_uri=f"local://{request.kind.value}/{filename}",
            kind=request.kind,
            media_type=request.media_type,
            size_bytes=len(request.content),
            checksum=request.checksum,
            native_checksum_verified=True,
        )

    def _head_sync(self, storage_uri: str) -> StorageObjectMetadata:
        target, kind = self._resolve_uri(storage_uri)
        try:
            content = target.read_bytes()
        except FileNotFoundError as exc:
            raise StorageObjectNotFoundError("Stored object does not exist") from exc
        return StorageObjectMetadata(
            storage_uri=storage_uri,
            kind=kind,
            media_type=self._mime_for_suffix(target.suffix),
            size_bytes=len(content),
            checksum=sha256(content).hexdigest(),
            native_checksum_verified=True,
        )

    def _delete_sync(self, storage_uri: str) -> None:
        target, _ = self._resolve_uri(storage_uri)
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("Unable to delete stored object") from exc

    def _resolve_uri(self, storage_uri: str) -> tuple[Path, ObjectKind]:
        parsed = urlsplit(storage_uri)
        if parsed.scheme != "local" or not parsed.netloc or parsed.query or parsed.fragment:
            raise StorageInvalidReferenceError("Invalid local storage URI")
        if parsed.path in ("", "/") and _STORED_NAME_PATTERN.fullmatch(parsed.netloc):
            return self._safe_path(parsed.netloc), ObjectKind.ORIGINAL
        try:
            kind = ObjectKind(parsed.netloc)
        except ValueError as exc:
            raise StorageInvalidReferenceError("Invalid local object kind") from exc
        filename = parsed.path.removeprefix("/")
        if not _STORED_NAME_PATTERN.fullmatch(filename):
            raise StorageInvalidReferenceError("Invalid local object name")
        return self._safe_path(f"{kind.value}/{filename}"), kind

    @staticmethod
    def _mime_for_suffix(suffix: str) -> str:
        return {
            ".pdf": "application/pdf",
            ".jpg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".json": "application/json",
        }.get(suffix.lower(), "application/octet-stream")

    def _safe_path(self, filename: str) -> Path:
        target = (self._root / filename).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise StorageError("Storage path escapes the configured root") from exc
        return target
