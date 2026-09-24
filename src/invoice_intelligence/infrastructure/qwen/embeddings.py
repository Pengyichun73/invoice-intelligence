"""Qwen dense embedding adapter isolated behind application ports."""

import math
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from invoice_intelligence.application.errors import CorrectionMemoryError
from invoice_intelligence.infrastructure.qwen.client import QwenRemoteClient, QwenRemoteError
from invoice_intelligence.infrastructure.qwen.redaction import (
    QwenPayloadGuard,
    QwenPayloadPolicyError,
)


class _EmbeddingItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int
    embedding: list[float]


class _EmbeddingResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: list[_EmbeddingItem]


class QwenDenseEmbeddingProvider:
    """Create deterministic-order dense vectors through Qwen Embeddings."""

    def __init__(
        self,
        *,
        client: QwenRemoteClient,
        payload_guard: QwenPayloadGuard,
        model: str,
        dimensions: int,
        batch_size: int,
    ) -> None:
        if not model.strip():
            raise ValueError("Qwen embedding model must not be empty")
        if dimensions <= 0:
            raise ValueError("Qwen embedding dimensions must be greater than zero")
        if not 1 <= batch_size <= 10:
            raise ValueError("Qwen text embedding batch_size must be between 1 and 10")
        self._client = client
        self._payload_guard = payload_guard
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        try:
            redacted_texts = tuple(
                self._payload_guard.validate_redacted_text(
                    text,
                    field_name="Embedding input",
                )
                for text in texts
            )
        except QwenPayloadPolicyError as exc:
            raise CorrectionMemoryError(str(exc)) from exc

        vectors: list[tuple[float, ...]] = []
        for offset in range(0, len(redacted_texts), self._batch_size):
            batch = redacted_texts[offset : offset + self._batch_size]
            vectors.extend(await self._embed_batch(batch))
        return tuple(vectors)

    async def _embed_batch(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        try:
            response = await self._client.create_embeddings(
                model=self._model,
                texts=texts,
                dimensions=self._dimensions,
            )
        except QwenRemoteError as exc:
            raise CorrectionMemoryError("Qwen dense embedding request failed") from exc
        payload = response.model_dump() if isinstance(response, BaseModel) else response
        try:
            parsed = _EmbeddingResponse.model_validate(payload)
        except ValidationError as exc:
            raise CorrectionMemoryError("Qwen embedding response Schema is invalid") from exc
        ordered = sorted(parsed.data, key=lambda item: item.index)
        if [item.index for item in ordered] != list(range(len(texts))):
            raise CorrectionMemoryError("Qwen embedding response indexes are incomplete")
        embeddings = tuple(tuple(float(value) for value in item.embedding) for item in ordered)
        if any(len(vector) != self._dimensions for vector in embeddings):
            raise CorrectionMemoryError(
                "Qwen embedding response dimension does not match configuration"
            )
        if any(not math.isfinite(value) for vector in embeddings for value in vector):
            raise CorrectionMemoryError("Qwen embedding response contains a non-finite value")
        return embeddings
