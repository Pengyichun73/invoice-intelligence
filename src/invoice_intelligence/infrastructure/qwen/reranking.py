"""Qwen text reranking adapter for redacted reviewed-example candidates."""

import math
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from invoice_intelligence.application.errors import RemoteInferenceError
from invoice_intelligence.application.ports.field_semantics import (
    FieldSemanticRerankCandidate,
)
from invoice_intelligence.domain.examples import ExampleCandidate
from invoice_intelligence.infrastructure.qwen.client import QwenRemoteClient, QwenRemoteError
from invoice_intelligence.infrastructure.qwen.redaction import (
    QwenPayloadGuard,
    QwenPayloadPolicyError,
)


class _RerankItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    relevance_score: float


class _RerankResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[_RerankItem]
    model: str
    id: str


class QwenRerankingProvider:
    """Rerank sanitized candidate text; scores remain uncalibrated relevance values."""

    def __init__(
        self,
        *,
        client: QwenRemoteClient,
        payload_guard: QwenPayloadGuard,
        model: str,
        instruct: str,
        max_documents: int,
    ) -> None:
        if not model.strip() or not instruct.strip():
            raise ValueError("Qwen rerank model and instruct must not be empty")
        if not 1 <= max_documents <= 500:
            raise ValueError("Qwen rerank max_documents must be between 1 and 500")
        self._client = client
        self._payload_guard = payload_guard
        self._model = model
        self._instruct = instruct
        self._max_documents = max_documents

    async def rerank(
        self,
        redacted_query_text: str,
        candidates: Sequence[ExampleCandidate],
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        return await self._rerank_documents(
            redacted_query_text,
            tuple(candidate.example_id for candidate in candidates),
            tuple(candidate.redacted_index_text for candidate in candidates),
            limit,
        )

    async def rerank_field_semantics(
        self,
        redacted_query_text: str,
        candidates: Sequence[FieldSemanticRerankCandidate],
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        """Rerank approved catalog metadata without selecting an invoice value."""

        return await self._rerank_documents(
            redacted_query_text,
            tuple(candidate.candidate_id for candidate in candidates),
            tuple(candidate.redacted_content for candidate in candidates),
            limit,
        )

    async def _rerank_documents(
        self,
        redacted_query_text: str,
        candidate_ids: tuple[str, ...],
        candidate_documents: tuple[str, ...],
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        if limit <= 0:
            raise RemoteInferenceError("Rerank limit must be greater than zero")
        if not candidate_ids:
            return ()
        if len(candidate_ids) != len(candidate_documents):
            raise RemoteInferenceError("Rerank candidate IDs and documents do not align")
        if len(candidate_ids) > self._max_documents:
            raise RemoteInferenceError("Rerank candidate count exceeds configuration")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise RemoteInferenceError("Rerank candidates contain duplicate identifiers")
        try:
            query = self._payload_guard.validate_redacted_text(
                redacted_query_text,
                field_name="Rerank query",
            )
            documents = [
                self._payload_guard.validate_redacted_text(
                    document,
                    field_name="Rerank candidate",
                )
                for document in candidate_documents
            ]
        except QwenPayloadPolicyError as exc:
            raise RemoteInferenceError(str(exc)) from exc
        top_n = min(limit, len(documents))
        try:
            response = await self._client.rerank(
                model=self._model,
                body={
                    "model": self._model,
                    "query": query,
                    "documents": documents,
                    "top_n": top_n,
                    "instruct": self._instruct,
                },
            )
        except QwenRemoteError as exc:
            raise RemoteInferenceError("Qwen rerank request failed") from exc
        payload = response.model_dump() if isinstance(response, BaseModel) else response
        try:
            parsed = _RerankResponse.model_validate(payload)
        except ValidationError as exc:
            raise RemoteInferenceError("Qwen rerank response Schema is invalid") from exc
        if parsed.model != self._model:
            raise RemoteInferenceError("Qwen rerank response model does not match configuration")
        indexes = tuple(item.index for item in parsed.results)
        if len(indexes) != len(set(indexes)) or len(indexes) > top_n:
            raise RemoteInferenceError("Qwen rerank response indexes are invalid")
        if any(index < 0 or index >= len(candidate_ids) for index in indexes):
            raise RemoteInferenceError("Qwen rerank response references an unknown candidate")
        if any(not math.isfinite(item.relevance_score) for item in parsed.results):
            raise RemoteInferenceError("Qwen rerank response contains a non-finite score")
        return tuple(
            (candidate_ids[item.index], float(item.relevance_score))
            for item in parsed.results
        )
