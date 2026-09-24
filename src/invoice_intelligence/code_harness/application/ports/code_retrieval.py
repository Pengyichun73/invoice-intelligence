"""Controlled code-context retrieval port."""

from dataclasses import dataclass
from typing import Protocol

from .code_index import CodeIndexScope, CodeIndexHit


@dataclass(frozen=True, slots=True)
class CodeRetrievalRequest:
    scope: CodeIndexScope
    query: str
    limit: int = 8


class CodeContextRetriever(Protocol):
    async def retrieve(self, request: CodeRetrievalRequest) -> tuple[CodeIndexHit, ...]:
        ...

