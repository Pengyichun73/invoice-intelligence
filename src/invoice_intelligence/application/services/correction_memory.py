"""Bounded, reviewed correction-memory retrieval and persistence."""

import asyncio
import json
from hashlib import sha256
from typing import TypeVar

from invoice_intelligence.application.errors import CorrectionMemoryError
from invoice_intelligence.application.ports.memory import (
    CorrectionEventRepository,
    CorrectionMemoryRepository,
    CorrectionMemoryScope,
    CorrectionQueryFacts,
    CorrectionScopeResolver,
    EmbeddingProvider,
    SensitiveDataRedactor,
    VectorMemoryStore,
    VectorMemoryUpsert,
)
from invoice_intelligence.domain.document import DocumentReference
from invoice_intelligence.domain.workflow import CorrectionEvent, WorkflowIdentity

InvoiceT = TypeVar("InvoiceT")


class CorrectionMemoryService(CorrectionMemoryRepository):
    """Persist raw audit events and expose only filtered derived Few-shot context."""

    def __init__(
        self,
        *,
        event_repository: CorrectionEventRepository,
        vector_store: VectorMemoryStore | None,
        embedding_provider: EmbeddingProvider | None,
        redactor: SensitiveDataRedactor,
        scope_resolver: CorrectionScopeResolver,
        schema_version: str,
        min_similarity: float,
        per_scope_limit: int,
        max_scopes: int,
    ) -> None:
        if vector_store is None and embedding_provider is not None:
            raise ValueError("Embedding provider requires a vector store")
        if vector_store is not None and embedding_provider is None:
            raise ValueError("Vector store requires an embedding provider")
        if not 0.0 <= min_similarity <= 1.0:
            raise ValueError("min_similarity must be between zero and one")
        if per_scope_limit <= 0 or max_scopes <= 0:
            raise ValueError("Correction-memory limits must be greater than zero")
        if not schema_version.strip():
            raise ValueError("schema_version must not be empty")
        self._event_repository = event_repository
        self._vector_store = vector_store
        self._embedding_provider = embedding_provider
        self._redactor = redactor
        self._scope_resolver = scope_resolver
        self._schema_version = schema_version
        self._min_similarity = min_similarity
        self._per_scope_limit = per_scope_limit
        self._max_scopes = max_scopes

    async def retrieve(
        self,
        tenant_id: str,
        document: DocumentReference,
        output_schema: type[InvoiceT],
        current_invoice: object | None,
        limit: int,
    ) -> tuple[CorrectionEvent, ...]:
        if limit <= 0:
            raise ValueError("Correction context limit must be greater than zero")
        normalized_tenant_id = tenant_id.strip()
        if not normalized_tenant_id or normalized_tenant_id != tenant_id:
            raise ValueError("tenant_id must be non-empty and normalized")
        if self._vector_store is None or self._embedding_provider is None:
            return ()
        current_facts = self._scope_resolver.current_facts(current_invoice)
        if current_facts is None:
            return ()
        sanitized_facts = self._redactor.redact_query_facts(current_facts)
        allowed_scopes = self._scope_resolver.resolve(
            output_schema,
            self._schema_version,
        )
        allowed_scopes = tuple(
            scope
            for scope in allowed_scopes
            if scope.document_type == current_facts.document_type
        )
        allowed = set(allowed_scopes)
        document_types = tuple(sorted({scope.document_type for scope in allowed_scopes}))
        stored_scopes = await self._vector_store.list_scopes(
            normalized_tenant_id,
            document_types,
            self._schema_version,
            self._max_scopes,
        )
        scopes = tuple(scope for scope in stored_scopes if scope in allowed)
        if not scopes:
            return ()

        query_texts = tuple(
            self._query_text(document, scope, sanitized_facts) for scope in scopes
        )
        embeddings = await self._embedding_provider.embed(query_texts)
        if len(embeddings) != len(scopes):
            raise CorrectionMemoryError("Embedding provider returned an invalid result count")
        batches = await asyncio.gather(
            *(
                self._vector_store.search(
                    normalized_tenant_id,
                    scope,
                    embedding,
                    self._per_scope_limit,
                    self._min_similarity,
                )
                for scope, embedding in zip(scopes, embeddings, strict=True)
            )
        )
        matches = sorted(
            (match for batch in batches for match in batch),
            key=lambda item: (
                -item.similarity,
                -item.occurrence_count,
                -item.event.created_at.timestamp(),
                item.memory_id,
            ),
        )
        unique: list[CorrectionEvent] = []
        seen: set[str] = set()
        for match in matches:
            if match.memory_id in seen:
                continue
            seen.add(match.memory_id)
            unique.append(match.event)
            if len(unique) == limit:
                break
        return tuple(unique)

    async def save(
        self,
        tenant_id: str,
        identity: WorkflowIdentity,
        document: DocumentReference,
        events: tuple[CorrectionEvent, ...],
    ) -> None:
        if identity.document_id != document.document_id:
            raise CorrectionMemoryError(
                "Correction-memory document_id does not match workflow identity"
            )
        normalized_tenant_id = tenant_id.strip()
        if not normalized_tenant_id or normalized_tenant_id != tenant_id:
            raise ValueError("tenant_id must be non-empty and normalized")
        stored = await self._event_repository.save_correction_events(
            normalized_tenant_id,
            identity,
            document,
            events,
        )
        if not stored or self._vector_store is None or self._embedding_provider is None:
            return
        sanitized = tuple(
            (item.event_id, item.event, self._redactor.redact(item.event))
            for item in stored
            if item.event.is_reviewed and item.event.is_valid
        )
        if not sanitized:
            return
        texts = tuple(self._memory_text(redacted_event) for _, _, redacted_event in sanitized)
        embeddings = await self._embedding_provider.embed(texts)
        if len(embeddings) != len(sanitized):
            raise CorrectionMemoryError("Embedding provider returned an invalid result count")
        records = tuple(
            VectorMemoryUpsert(
                source_event_id=source_event_id,
                tenant_id=normalized_tenant_id,
                fingerprint=self._fingerprint(normalized_tenant_id, raw_event),
                event=redacted_event,
                embedding=embedding,
            )
            for (source_event_id, raw_event, redacted_event), embedding in zip(
                sanitized,
                embeddings,
                strict=True,
            )
        )
        await self._vector_store.upsert(records)

    async def disable_memory(
        self,
        tenant_id: str,
        memory_id: str,
        reason: str,
    ) -> None:
        if self._vector_store is None:
            raise CorrectionMemoryError("Vector correction memory is disabled")
        await self._vector_store.disable(tenant_id, memory_id, reason)

    async def invalidate_schema_version(
        self,
        tenant_id: str,
        document_type: str,
        schema_version: str,
        reason: str,
    ) -> int:
        if self._vector_store is None:
            raise CorrectionMemoryError("Vector correction memory is disabled")
        return await self._vector_store.invalidate_schema(
            tenant_id,
            document_type,
            schema_version,
            reason,
        )

    @staticmethod
    def _query_text(
        document: DocumentReference,
        scope: CorrectionMemoryScope,
        facts: CorrectionQueryFacts,
    ) -> str:
        payload = {
            "current_document_facts": {
                "mime_type": document.mime_type,
                "field_value": facts.field_values.get(scope.field_path),
                "vendor_features": dict(facts.vendor_features),
                "template_features": dict(facts.template_features),
            },
            "document_type": scope.document_type,
            "field_path": scope.field_path,
            "schema_version": scope.schema_version,
        }
        return CorrectionMemoryService._canonical(payload)

    @staticmethod
    def _memory_text(event: CorrectionEvent) -> str:
        payload = {
            "document_type": event.document_type,
            "field_path": event.field_path,
            "model_value": event.model_value,
            "corrected_value": event.corrected_value,
            "correction_reason": event.correction_reason,
            "vendor_features": dict(event.vendor_features),
            "template_features": dict(event.template_features),
            "schema_version": event.schema_version,
        }
        return CorrectionMemoryService._canonical(payload)

    @classmethod
    def _fingerprint(cls, tenant_id: str, event: CorrectionEvent) -> str:
        payload = f"{tenant_id}\0{cls._memory_text(event)}"
        return sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
