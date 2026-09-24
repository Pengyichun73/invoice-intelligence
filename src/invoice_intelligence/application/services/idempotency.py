"""Protocol-neutral idempotency-key validation."""

import re

from invoice_intelligence.application.errors import BadRequestError

_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[\x21-\x7e]{1,200}$")


def normalize_idempotency_key(value: str | None) -> str:
    """Require a bounded visible-ASCII key suitable for durable indexing."""

    if value is None:
        raise BadRequestError("Idempotency-Key header is required")
    normalized = value.strip()
    if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized):
        raise BadRequestError(
            "Idempotency-Key must contain 1 to 200 visible ASCII characters"
        )
    return normalized
