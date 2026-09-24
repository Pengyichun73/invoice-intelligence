"""Language-neutral code structure facts used by the Harness."""

from dataclasses import dataclass

from .errors import require_text, require_sha256
from .versions import ExecutionVersionBinding


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("byte range is invalid")


@dataclass(frozen=True, slots=True)
class LineRange:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 1 or self.end < self.start:
            raise ValueError("line range is invalid")


@dataclass(frozen=True, slots=True)
class CodeSymbol:
    symbol_id: str
    file_path: str
    language: str
    name: str
    symbol_kind: str
    parent_symbol: str | None
    scope: str
    byte_range: ByteRange
    line_range: LineRange
    signature: str | None
    versions: ExecutionVersionBinding
    tenant_id: str
    repository_id: str
    snapshot_id: str
    snapshot_revision: int
    source_revision: str
    parameters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "symbol_id",
            "file_path",
            "language",
            "name",
            "symbol_kind",
            "scope",
            "tenant_id",
            "repository_id",
            "snapshot_id",
            "source_revision",
        ):
            require_text(name, getattr(self, name), max_length=1024)
        if self.snapshot_revision < 1:
            raise ValueError("snapshot_revision must be positive")
        if self.parent_symbol is not None:
            require_text("parent_symbol", self.parent_symbol, max_length=1024)
        if self.signature is not None:
            require_text("signature", self.signature, max_length=4096)
        for parameter in self.parameters:
            require_text("parameter", parameter, max_length=1024)


@dataclass(frozen=True, slots=True)
class CallEdge:
    caller_symbol_id: str
    callee_name: str
    file_path: str
    byte_range: ByteRange
    versions: ExecutionVersionBinding
    tenant_id: str
    repository_id: str
    snapshot_id: str
    snapshot_revision: int
    source_revision: str

    def __post_init__(self) -> None:
        for name in (
            "caller_symbol_id",
            "callee_name",
            "file_path",
            "tenant_id",
            "repository_id",
            "snapshot_id",
            "source_revision",
        ):
            require_text(name, getattr(self, name), max_length=1024)
        if self.snapshot_revision < 1:
            raise ValueError("snapshot_revision must be positive")


@dataclass(frozen=True, slots=True)
class CastChunk:
    chunk_id: str
    file_path: str
    language: str
    symbol: str
    parent_symbol: str | None
    scope: str
    byte_range: ByteRange
    line_range: LineRange
    text_checksum_sha256: str
    schema_version: str
    versions: ExecutionVersionBinding
    tenant_id: str
    repository_id: str
    snapshot_id: str
    snapshot_revision: int
    source_revision: str

    def __post_init__(self) -> None:
        for name in (
            "chunk_id",
            "file_path",
            "language",
            "symbol",
            "scope",
            "schema_version",
            "tenant_id",
            "repository_id",
            "snapshot_id",
            "source_revision",
        ):
            require_text(name, getattr(self, name), max_length=1024)
        if self.snapshot_revision < 1:
            raise ValueError("snapshot_revision must be positive")
        if self.parent_symbol is not None:
            require_text("parent_symbol", self.parent_symbol, max_length=1024)
        require_sha256("text_checksum_sha256", self.text_checksum_sha256)
