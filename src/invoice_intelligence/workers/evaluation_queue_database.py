"""评估队列与 API 共用业务 PostgreSQL 事实库。"""

from sqlalchemy.engine import make_url

from invoice_intelligence.config.settings import get_settings


def evaluation_queue_database_url() -> str:
    database_url = get_settings().resolved_business_database_url.get_secret_value()
    if make_url(database_url).get_backend_name() != "postgresql":
        raise RuntimeError("Evaluation queue requires the business PostgreSQL database")
    return database_url
