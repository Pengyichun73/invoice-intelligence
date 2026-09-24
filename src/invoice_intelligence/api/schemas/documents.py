"""Stable document API schemas."""

from pydantic import BaseModel, ConfigDict


class DocumentResponse(BaseModel):
    """Immutable trusted document reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    storage_uri: str
    mime_type: str
    checksum: str
    size_bytes: int | None = None
    storage_status: str = "available"


class DocumentDownloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    expires_in_seconds: int
