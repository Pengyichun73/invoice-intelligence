"""Lease ownership for rebuildable, PostgreSQL-queued index projections."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ProjectionLease[ItemT]:
    item: ItemT
    worker_id: str
    token: str
    expires_at: datetime
