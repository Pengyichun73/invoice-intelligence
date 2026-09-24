"""Durable repository source registry boundary."""

from typing import Protocol

from ...domain.repository import RepositorySource


class HarnessSourceRegistry(Protocol):
    async def list_enabled(self) -> tuple[RepositorySource, ...]:
        ...
