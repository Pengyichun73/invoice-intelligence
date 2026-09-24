"""Promotion worker entrypoint; automatic promotion is opt-in and disabled by default."""

import os


def main() -> None:
    if os.getenv("INVOICE_INTELLIGENCE_AUTOMATIC_PROMOTION_ENABLED", "false").lower() != "true":
        raise SystemExit("automatic promotion is disabled; use the approval API")
    raise SystemExit("automatic promotion worker is intentionally not enabled in this build")


if __name__ == "__main__":
    main()
