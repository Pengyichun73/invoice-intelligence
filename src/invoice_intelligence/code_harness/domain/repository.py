"""Immutable repository and snapshot facts."""

from dataclasses import dataclass
from enum import StrEnum

from .errors import require_sha256, require_text
from .versions import ExecutionVersionBinding


class FileKind(StrEnum):
    TEXT = "text"
    BINARY = "binary"
    SYMLINK = "symlink"


@dataclass(frozen=True, slots=True)
class RepositorySource:
    repository_id: str
    tenant_id: str
    root_path: str
    source_revision: str
    repository_version: str

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            require_text(name, getattr(self, name), max_length=1024)


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    path: str
    checksum_sha256: str
    size_bytes: int
    kind: FileKind
    mode: int | None = None

    def __post_init__(self) -> None:
        require_text("path", self.path, max_length=4096)
        require_sha256("checksum_sha256", self.checksum_sha256)
        if self.size_bytes < 0:
            raise ValueError("size_bytes must not be negative")
        if self.kind is FileKind.SYMLINK:
            raise ValueError("symlinks are not valid snapshot files")


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    snapshot_id: str
    tenant_id: str
    repository_id: str
    revision: int
    source_revision: str
    manifest_checksum_sha256: str
    files: tuple[SnapshotFile, ...]
    versions: ExecutionVersionBinding

    def __post_init__(self) -> None:
        for name in (
            "snapshot_id",
            "tenant_id",
            "repository_id",
            "source_revision",
        ):
            require_text(name, getattr(self, name), max_length=1024)
        require_sha256("manifest_checksum_sha256", self.manifest_checksum_sha256)
        if self.revision < 1:
            raise ValueError("snapshot revision must be positive")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("snapshot files must be unique and deterministically sorted")

