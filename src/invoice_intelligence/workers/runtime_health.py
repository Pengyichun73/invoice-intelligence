"""Business-level worker health probe used by Compose healthchecks."""

from __future__ import annotations

import argparse
import os

from sqlalchemy import text

from invoice_intelligence.config.settings import get_settings
from invoice_intelligence.infrastructure.persistence.database import create_business_engine
from invoice_intelligence.infrastructure.preflight import run_preflight


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id-env")
    parser.add_argument("--database-url-env", default="INVOICE_INTELLIGENCE_BUSINESS_DATABASE_URL")
    args = parser.parse_args()

    settings = get_settings()
    worker_id = os.getenv(args.worker_id_env) if args.worker_id_env else None
    result = run_preflight(settings, worker_id=worker_id)
    if not result.ready:
        raise SystemExit(1)

    database_url = (
        settings.resolved_business_database_url.get_secret_value()
        if args.database_url_env == "INVOICE_INTELLIGENCE_BUSINESS_DATABASE_URL"
        else os.getenv(args.database_url_env)
    )
    if not database_url:
        raise SystemExit(1)
    engine = create_business_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
