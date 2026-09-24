"""Structured code-model boundary."""

from dataclasses import dataclass
from typing import Protocol

from ...domain.patch_model import PatchProposal
from ...domain.repository import RepositorySnapshot
from .code_index import CodeContext


@dataclass(frozen=True, slots=True)
class CodeGenerationRequest:
    task_id: str
    snapshot: RepositorySnapshot
    context_refs: tuple[str, ...]
    failure_signature: str | None
    max_model_tokens: int
    context_payloads: tuple[CodeContext, ...] = ()


class CodeModel(Protocol):
    async def generate_patch(self, request: CodeGenerationRequest) -> PatchProposal:
        ...
