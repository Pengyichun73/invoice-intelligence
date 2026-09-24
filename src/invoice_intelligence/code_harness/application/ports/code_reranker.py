"""Code retrieval reranking boundary."""

from typing import Protocol


class CodeReranker(Protocol):
    model_version: str

    async def rerank(
        self,
        query: str,
        candidate_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        ...

