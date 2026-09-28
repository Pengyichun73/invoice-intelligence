"""准入事实插入必须以 RETURNING 判断新建，不能依赖驱动的 rowcount。"""

from datetime import UTC, datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from invoice_intelligence.infrastructure.persistence.sqlalchemy_admission import (
    SQLAlchemyMemoryAdmissionRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import ModelVersionRow


def test_insert_do_nothing_distinguishes_create_from_replay() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ModelVersionRow.__table__.create(engine)
    repository = SQLAlchemyMemoryAdmissionRepository(engine)
    values = {
        "model_version_id": "model-1",
        "tenant_id": "tenant-a",
        "version": "v1",
        "created_at": datetime.now(UTC),
    }

    with Session(engine) as session, session.begin():
        assert repository._insert_do_nothing(session, ModelVersionRow, values)
        assert not repository._insert_do_nothing(session, ModelVersionRow, values)
        assert session.scalar(select(func.count()).select_from(ModelVersionRow)) == 1

    engine.dispose()
