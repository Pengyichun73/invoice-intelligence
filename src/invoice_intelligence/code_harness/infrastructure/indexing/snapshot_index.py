"""Read-only exact index over a controlled repository snapshot."""

from dataclasses import dataclass
from typing import Mapping

from ...application.ports.code_index import (
    CodeContext,
    CodeIndexHit,
    CodeIndexScope,
    CodeQuery,
)
from ...application.ports.parser import ParsedArtifact
from ...domain.code_model import CallEdge, CodeSymbol
from ...domain.repository import RepositorySnapshot
from ...domain.versions import ExecutionVersionBinding


@dataclass(frozen=True, slots=True)
class _IndexedSymbol:
    symbol: CodeSymbol
    text: bytes


class SnapshotCodeIndex:
    """Deterministic fallback index; no result means only no local match."""

    def __init__(
        self,
        *,
        snapshot: RepositorySnapshot,
        parsed: tuple[ParsedArtifact, ...],
        contents: Mapping[str, bytes],
    ) -> None:
        self._snapshot = snapshot
        self._contents = contents
        self._symbols: tuple[_IndexedSymbol, ...] = tuple(
            _IndexedSymbol(symbol, contents.get(symbol.file_path, b"")[symbol.byte_range.start : symbol.byte_range.end])
            for artifact in parsed
            for symbol in artifact.symbols
        )
        self._edges: tuple[CallEdge, ...] = tuple(
            edge for artifact in parsed for edge in artifact.call_edges
        )

    async def search(
        self,
        *,
        scope: CodeIndexScope,
        query: str,
        limit: int,
    ) -> tuple[CodeIndexHit, ...]:
        return await self.search_structured(
            scope=scope,
            query=CodeQuery(keyword=query),
            limit=limit,
        )

    async def search_structured(
        self,
        *,
        scope: CodeIndexScope,
        query: CodeQuery,
        limit: int,
    ) -> tuple[CodeIndexHit, ...]:
        _assert_scope(scope, self._snapshot)
        if limit <= 0:
            return ()
        matches: list[CodeIndexHit] = []
        edges_by_caller: dict[str, tuple[CallEdge, ...]] = {}
        for edge in self._edges:
            edges_by_caller[edge.caller_symbol_id] = (
                *edges_by_caller.get(edge.caller_symbol_id, ()),
                edge,
            )
        for item in self._symbols:
            symbol = item.symbol
            if query.symbol and symbol.name != query.symbol:
                continue
            if query.file_path and symbol.file_path != query.file_path:
                continue
            if query.keyword and query.keyword.lower().encode("utf-8") not in item.text.lower():
                continue
            outgoing = edges_by_caller.get(symbol.symbol_id, ())
            if query.caller and (symbol.name != query.caller or not outgoing):
                continue
            if query.callee and not (
                symbol.name == query.callee
                or any(edge.callee_name == query.callee for edge in outgoing)
            ):
                continue
            if query.callee and query.caller:
                if symbol.name != query.caller or not any(
                    edge.callee_name == query.callee for edge in outgoing
                ):
                    continue
            if query.dependency and not (
                symbol.name == query.dependency
                or any(edge.callee_name == query.dependency for edge in outgoing)
            ):
                continue
            if query.dependency and query.symbol and symbol.name != query.symbol:
                continue
            matches.append(_hit(scope, symbol))
        return tuple(matches[:limit])

    async def register_projection(
        self,
        *,
        scope: CodeIndexScope,
        versions: ExecutionVersionBinding,
        chunk_ids: tuple[str, ...],
    ) -> None:
        _assert_scope(scope, self._snapshot)
        del versions, chunk_ids

    async def read_context(
        self,
        *,
        scope: CodeIndexScope,
        hit: CodeIndexHit,
        max_bytes: int,
        context_lines: int,
    ) -> CodeContext:
        _assert_scope(scope, self._snapshot)
        if (
            hit.tenant_id != scope.tenant_id
            or hit.repository_id != scope.repository_id
            or hit.snapshot_id != scope.snapshot_id
            or hit.snapshot_revision != scope.snapshot_revision
        ):
            raise ValueError("code context scope does not match hit")
        if max_bytes <= 0 or context_lines < 0:
            raise ValueError("context bounds are invalid")
        source = self._contents.get(hit.path, b"")
        start, end = _bounded_range(
            source,
            start=hit.start_byte,
            end=hit.end_byte,
            context_lines=context_lines,
            max_bytes=max_bytes,
        )
        return CodeContext(
            chunk_id=hit.chunk_id,
            path=hit.path,
            start_byte=start,
            end_byte=end,
            text_utf8=source[start:end].decode("utf-8", errors="replace"),
        )


def _assert_scope(scope: CodeIndexScope, snapshot: RepositorySnapshot) -> None:
    expected = (
        scope.tenant_id,
        scope.repository_id,
        scope.snapshot_id,
        scope.snapshot_revision,
        scope.source_revision,
        scope.schema_version,
    )
    actual = (
        snapshot.tenant_id,
        snapshot.repository_id,
        snapshot.snapshot_id,
        snapshot.revision,
        snapshot.source_revision,
        snapshot.versions.schema_version,
    )
    if expected != actual:
        raise ValueError("code index scope does not match snapshot")


def _hit(scope: CodeIndexScope, symbol: CodeSymbol) -> CodeIndexHit:
    return CodeIndexHit(
        chunk_id=f"{scope.snapshot_id}:{symbol.symbol_id}",
        tenant_id=scope.tenant_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        snapshot_revision=scope.snapshot_revision,
        source_revision=scope.source_revision,
        path=symbol.file_path,
        start_byte=symbol.byte_range.start,
        end_byte=symbol.byte_range.end,
        score=1.0,
        index_version=scope.index_version,
        schema_version=scope.schema_version,
        parser_version=scope.parser_version,
        grammar_version=scope.grammar_version,
        redaction_version=scope.redaction_version,
    )


def _bounded_range(
    source: bytes,
    *,
    start: int,
    end: int,
    context_lines: int,
    max_bytes: int,
) -> tuple[int, int]:
    start = max(0, min(start, len(source)))
    end = max(start, min(end, len(source)))
    for _ in range(context_lines):
        line_start = source.rfind(b"\n", 0, start) + 1
        line_end = source.find(b"\n", end)
        line_end = len(source) if line_end < 0 else line_end + 1
        start, end = line_start, line_end
    if end - start <= max_bytes:
        return start, end
    left = max(0, min(start, len(source) - max_bytes))
    return left, min(len(source), left + max_bytes)
