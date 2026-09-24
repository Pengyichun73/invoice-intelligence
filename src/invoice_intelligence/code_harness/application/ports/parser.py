"""Parser boundary; no parser SDK types may cross this port."""

from dataclasses import dataclass
from collections.abc import Mapping
from typing import Protocol

from ...domain.code_model import CallEdge, CodeSymbol
from ...domain.repository import SnapshotFile
from ...domain.versions import ExecutionVersionBinding


@dataclass(frozen=True, slots=True)
class ParseDiagnostic:
    code: str
    severity: str
    start_byte: int
    end_byte: int


@dataclass(frozen=True, slots=True)
class ParsedArtifact:
    path: str
    symbols: tuple[CodeSymbol, ...]
    call_edges: tuple[CallEdge, ...]
    diagnostics: tuple[ParseDiagnostic, ...]
    ast_fingerprint: str
    versions: ExecutionVersionBinding
    parse_status: str = "complete"


class CodeParser(Protocol):
    async def parse(
        self,
        *,
        tenant_id: str,
        repository_id: str,
        snapshot_id: str,
        snapshot_revision: int,
        source_revision: str,
        files: tuple[SnapshotFile, ...],
        contents: Mapping[str, bytes],
        versions: ExecutionVersionBinding,
    ) -> tuple[ParsedArtifact, ...]:
        ...
