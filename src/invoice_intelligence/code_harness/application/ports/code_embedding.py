"""Code-only embedding boundary."""

from typing import Protocol


class CodeEmbedding(Protocol):
    model_version: str
    dimensions: int

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        ...

