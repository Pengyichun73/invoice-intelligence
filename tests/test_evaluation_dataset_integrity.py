"""冻结评估数据集的列元数据与不可变 JSON 载荷必须一致。"""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.errors import WorkflowPersistenceError
from invoice_intelligence.domain.evaluation import EvaluationCase, EvaluationDataset
from invoice_intelligence.domain.examples import ExampleEvidenceReference
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation import (
    SQLAlchemyEvaluationRepository,
    _dataset_key,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    EvaluationDatasetRow,
)

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def _dataset() -> EvaluationDataset:
    return EvaluationDataset(
        dataset_id="dataset-a",
        tenant_id="tenant-a",
        name="held-out",
        version="v1",
        schema_version="3.0.0",
        training_document_ids=("training-document",),
        training_template_fingerprints=("training-template",),
        cases=(
            EvaluationCase(
                case_id="case-a",
                tenant_id="tenant-a",
                document_id="held-out-document",
                document_type="invoice",
                field_path="invoice_number",
                schema_version="3.0.0",
                vendor_fingerprint=None,
                template_fingerprint="held-out-template",
                image_quality_bucket="readable",
                evidence_reference=ExampleEvidenceReference(
                    document_reference="isolated://held-out-document",
                ),
                expected_examples=(),
                expected_value=None,
                expected_is_missing=True,
                expects_candidate=False,
                expected_review_required=False,
                reviewer_id="reviewer-a",
                created_at=NOW,
            ),
        ),
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_dataset_metadata_drift_fails_closed() -> None:
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[EvaluationDatasetRow.__table__])
    repository = SQLAlchemyEvaluationRepository(engine)
    dataset = _dataset()
    await repository.add(dataset)

    with sa.orm.Session(engine) as session, session.begin():
        row = session.get(
            EvaluationDatasetRow,
            _dataset_key("tenant-a", "dataset-a", "v1"),
        )
        assert row is not None
        row.schema_version = "3.0.1"

    with pytest.raises(WorkflowPersistenceError, match="metadata"):
        await repository.get_dataset("tenant-a", "dataset-a", "v1")
