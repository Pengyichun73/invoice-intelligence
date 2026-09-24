"""Rebuildable code index projection boundary."""

from dataclasses import dataclass
from typing import Protocol

from ...domain.versions import ExecutionVersionBinding


@dataclass(frozen=True, slots=True)
class CodeIndexScope:
    tenant_id: str
    repository_id: str
    snapshot_id: str
    snapshot_revision: int
    source_revision: str
    schema_version: str
    parser_version: str
    grammar_version: str
    redaction_version: str
    index_version: str


@dataclass(frozen=True, slots=True)
class CodeIndexHit:
    chunk_id: str
    tenant_id: str
    repository_id: str
    snapshot_id: str
    snapshot_revision: int
    source_revision: str
    path: str
    start_byte: int
    end_byte: int
    score: float
    index_version: str
    schema_version: str
    parser_version: str
    grammar_version: str
    redaction_version: str
    source: str = "code_cast"


@dataclass(frozen=True, slots=True)
class CodeContext:
    """Bounded, snapshot-bound source context for one model request."""

    chunk_id: str
    path: str
    start_byte: int
    end_byte: int
    text_utf8: str


@dataclass(frozen=True, slots=True)
class CodeQuery:
    """Structured query; adapters must apply every supplied scope constraint."""

    symbol: str | None = None
    caller: str | None = None
    callee: str | None = None
    dependency: str | None = None
    file_path: str | None = None
    keyword: str | None = None

    def __post_init__(self) -> None:
        values = (
            self.symbol,
            self.caller,
            self.callee,
            self.dependency,
            self.file_path,
            self.keyword,
        )
        if not any(value and value.strip() for value in values):
            raise ValueError("at least one code query selector is required")


class CodeIndex(Protocol):
    async def search(
        self,
        *,
        scope: CodeIndexScope,
        query: str,
        limit: int,
    ) -> tuple[CodeIndexHit, ...]:
        ...

    async def search_structured(
        self,
        *,
        scope: CodeIndexScope,
        query: CodeQuery,
        limit: int,
    ) -> tuple[CodeIndexHit, ...]:
        ...

    async def read_context(
        self,
        *,
        scope: CodeIndexScope,
        hit: CodeIndexHit,
        max_bytes: int,
        context_lines: int,
    ) -> CodeContext:
        ...

    async def register_projection(
        self,
        *,
        scope: CodeIndexScope,
        versions: ExecutionVersionBinding,
        chunk_ids: tuple[str, ...],
    ) -> None:
        ...
