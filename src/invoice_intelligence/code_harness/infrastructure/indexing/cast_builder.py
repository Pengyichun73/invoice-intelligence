"""Build semantic CAST chunks from parser output."""

from hashlib import sha256

from ...application.ports.parser import ParsedArtifact
from ...domain.code_model import CastChunk
from ...domain.repository import RepositorySnapshot


def build_cast_chunks(
    *,
    snapshot: RepositorySnapshot,
    parsed: tuple[ParsedArtifact, ...],
    contents: dict[str, bytes],
) -> tuple[CastChunk, ...]:
    chunks: list[CastChunk] = []
    for artifact in parsed:
        source = contents.get(artifact.path, b"")
        for symbol in artifact.symbols:
            text = source[symbol.byte_range.start : symbol.byte_range.end]
            chunks.append(
                CastChunk(
                    chunk_id=sha256(
                        f"{snapshot.snapshot_id}:{symbol.symbol_id}".encode("utf-8")
                    ).hexdigest(),
                    file_path=symbol.file_path,
                    language=symbol.language,
                    symbol=symbol.name,
                    parent_symbol=symbol.parent_symbol,
                    scope=symbol.scope,
                    byte_range=symbol.byte_range,
                    line_range=symbol.line_range,
                    text_checksum_sha256=sha256(text).hexdigest(),
                    schema_version=snapshot.versions.schema_version,
                    versions=snapshot.versions,
                )
            )
    return tuple(chunks)
