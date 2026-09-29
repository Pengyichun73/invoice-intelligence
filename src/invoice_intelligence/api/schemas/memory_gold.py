"""Gold annotation requests and value-free status responses."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GoldVersionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(min_length=1, max_length=64)
    model_version: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=256)
    catalog_version: str = Field(min_length=1, max_length=128)
    index_version: str = Field(min_length=1, max_length=256)


class GoldAnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_group: str = Field(min_length=1, max_length=128)
    versions: GoldVersionsRequest
    fields: dict[str, dict[str, Any]]


class GoldAdjudicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_choices: dict[str, Literal["first", "second"]]


class GoldCaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    status: Literal["open", "frozen"]
    annotation_count: int
    annotator_ids: list[str]
    adjudicator_id: str | None
    document_checksum: str
    template_group: str
    versions: dict[str, str]
    gold_checksum: str | None
    frozen_at: datetime | None
