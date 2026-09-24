"""Durable document-reference registry boundary."""

from typing import Protocol

from invoice_intelligence.domain.document import DocumentReference


class DocumentReferenceRepository(Protocol):
    """Persist and resolve immutable workflow-safe document references."""

    async def save_document(self, document: DocumentReference, tenant_id: str) -> None:
        """Write a document reference once by document_id."""

        ...

    async def get_document(
        self,
        document_id: str,
        tenant_id: str,
    ) -> DocumentReference | None:
        """Resolve a trusted reference without accepting client storage metadata."""

        ...
