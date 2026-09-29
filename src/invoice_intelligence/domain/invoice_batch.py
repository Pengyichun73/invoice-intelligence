"""A bounded group of source documents and independently reviewed invoices."""

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite


class BatchStatus(StrEnum):
    OPEN = "open"
    SEGMENTING = "segmenting"
    NEEDS_BOUNDARY_REVIEW = "needs_boundary_review"
    TOO_MANY_INVOICES = "too_many_invoices"
    EXTRACTING = "extracting"
    DISPATCHED = "dispatched"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class InvoiceRegion:
    page_number: int
    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        if not all(isfinite(value) for value in (
            self.left, self.top, self.right, self.bottom,
        )):
            raise ValueError("Invoice region coordinates must be finite")
        if self.page_number < 1 or not (0 <= self.left < self.right <= 1):
            raise ValueError("Invalid invoice region horizontal boundary")
        if not (0 <= self.top < self.bottom <= 1):
            raise ValueError("Invalid invoice region vertical boundary")


@dataclass(frozen=True, slots=True)
class InvoiceGroup:
    regions: tuple[InvoiceRegion, ...]

    def __post_init__(self) -> None:
        if not self.regions:
            raise ValueError("Invoice group must contain at least one region")


@dataclass(frozen=True, slots=True)
class CroppedInvoice:
    content: bytes
    mime_type: str
    filename: str
    native_sizes: tuple[tuple[int, int], ...]
    render_attempts: tuple[int, ...]
    clarity_scores: tuple[float, ...]


def validate_groups(groups: tuple[InvoiceGroup, ...], page_count: int) -> None:
    if not 1 <= len(groups) <= 100:
        raise ValueError("Invoice count must be between one and one hundred")
    all_regions = [region for group in groups for region in group.regions]
    if len(all_regions) > 100:
        raise ValueError("Invoice boundary proposal contains too many regions")
    if any(region.page_number > page_count for region in all_regions):
        raise ValueError("Invoice region refers to an absent page")
    for index, first in enumerate(all_regions):
        for second in all_regions[index + 1:]:
            if first.page_number != second.page_number:
                continue
            width = max(0.0, min(first.right, second.right) - max(first.left, second.left))
            height = max(0.0, min(first.bottom, second.bottom) - max(first.top, second.top))
            if width * height > 0.001:
                raise ValueError("Invoice regions overlap")
    if {region.page_number for region in all_regions} != set(range(1, page_count + 1)):
        raise ValueError("Every source page must be assigned to an invoice")
