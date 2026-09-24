"""OpenAI Embeddings adapter isolated behind the application port."""

import math
import re
from collections.abc import Sequence

from openai import AsyncOpenAI, OpenAIError

from invoice_intelligence.application.errors import CorrectionMemoryError
from invoice_intelligence.infrastructure.remote.safety import (
    RemoteCallSafety,
    RemoteCircuitOpenError,
)

_INLINE_BINARY = re.compile(
    r"(?:data:(?:image|application/pdf)/[^;,]+;base64,|base64,)",
    re.IGNORECASE,
)
_MAX_EMBEDDING_TEXT_CHARS = 65_535


class OpenAIEmbeddingProvider:
    """Create deterministic-order float embeddings for correction-memory text."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int,
        timeout_seconds: float,
        max_retries: int,
        max_concurrency: int,
        requests_per_minute: int,
        circuit_failure_threshold: int,
        circuit_recovery_seconds: float,
    ) -> None:
        if not model.strip():
            raise ValueError("Embedding model must not be empty")
        if dimensions <= 0:
            raise ValueError("Embedding dimensions must be greater than zero")
        self._model = model
        self._dimensions = dimensions
        self._client = AsyncOpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )
        self._remote_safety = RemoteCallSafety(
            max_concurrency=max_concurrency,
            requests_per_minute=requests_per_minute,
            circuit_failure_threshold=circuit_failure_threshold,
            circuit_recovery_seconds=circuit_recovery_seconds,
        )

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        if any(not text.strip() for text in texts):
            raise CorrectionMemoryError("Embedding input must not be empty")
        if any(len(text) > _MAX_EMBEDDING_TEXT_CHARS for text in texts):
            raise CorrectionMemoryError("Embedding input exceeds the configured text limit")
        if any(_INLINE_BINARY.search(text) for text in texts):
            raise CorrectionMemoryError("Embedding input contains prohibited inline binary data")
        try:
            async with self._remote_safety.call(
                provider="openai",
                operation="dense_embedding",
                model=self._model,
            ):
                response = await self._client.embeddings.create(
                    model=self._model,
                    input=list(texts),
                    dimensions=self._dimensions,
                    encoding_format="float",
                )
        except RemoteCircuitOpenError as exc:
            raise CorrectionMemoryError(
                "OpenAI correction-memory embedding circuit is open"
            ) from exc
        except OpenAIError as exc:
            raise CorrectionMemoryError("OpenAI correction-memory embedding failed") from exc
        ordered = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in ordered] != list(range(len(texts))):
            raise CorrectionMemoryError("Embedding response indexes are incomplete")
        embeddings = tuple(tuple(float(value) for value in item.embedding) for item in ordered)
        if any(len(vector) != self._dimensions for vector in embeddings):
            raise CorrectionMemoryError("Embedding response dimension does not match configuration")
        if any(not math.isfinite(value) for vector in embeddings for value in vector):
            raise CorrectionMemoryError("Embedding response contains a non-finite value")
        return embeddings
