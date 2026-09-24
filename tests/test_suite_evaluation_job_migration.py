"""评估 Job 扩展迁移保留旧诊断行并约束 Suite 绑定。"""

from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError


def test_suite_job_migration_preserves_diagnostic_rows() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    old = import_module("migrations.versions.20260923_0035_evaluation_queue")
    new = import_module("migrations.versions.20260924_0037_evaluation_suite_jobs")
    link = import_module("migrations.versions.20260924_0038_evaluation_job_run_link")
    artifacts = import_module("migrations.versions.20260924_0039_evaluation_report_artifacts")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE evaluation_datasets (dataset_key VARCHAR(64) PRIMARY KEY)"
        ))
        old.op = Operations(MigrationContext.configure(connection))
        old.upgrade()
        connection.execute(sa.text(
            "INSERT INTO evaluation_snapshots "
            "(snapshot_id, tenant_id, dataset_version, schema_version, "
            "content_sha256, cases_json, created_at) VALUES "
            "('snapshot-a', 'tenant-a', 'v1', '3.0.0', :checksum, '[]', :now)"
        ), {"checksum": "a" * 64, "now": "2026-09-24T00:00:00+00:00"})
        connection.execute(sa.text(
            "INSERT INTO evaluation_jobs "
            "(job_id, tenant_id, snapshot_id, dataset_version, schema_version, "
            "index_version, model_version, prompt_version, threshold_version, "
            "request_sha256, status, attempt_count, created_at, updated_at) VALUES "
            "('diagnostic-a', 'tenant-a', 'snapshot-a', 'v1', '3.0.0', "
            "'index-v1', 'model-v1', 'prompt-v1', 'threshold-v1', :checksum, "
            "'pending', 0, :now, :now)"
        ), {"checksum": "b" * 64, "now": "2026-09-24T00:00:00+00:00"})
        new.op = Operations(MigrationContext.configure(connection))
        new.upgrade()
        assert connection.scalar(sa.text(
            "SELECT evidence_class FROM evaluation_jobs WHERE job_id = 'diagnostic-a'"
        )) == "diagnostic_only"
        connection.execute(sa.text(
            "INSERT INTO evaluation_datasets (dataset_key) VALUES ('dataset-key-a')"
        ))
        columns = {
            "job_id": "suite-a", "tenant_id": "tenant-a", "snapshot_id": None,
            "dataset_key": "dataset-key-a", "dataset_id": "dataset-a",
            "dataset_version": "v1", "schema_version": "3.0.0",
            "index_version": "index-v1", "model_version": "model-v1",
            "prompt_version": "prompt-v1", "threshold_version": "threshold-v1",
            "request_sha256": "c" * 64, "status": "pending", "attempt_count": 0,
            "created_at": "2026-09-24T00:00:00+00:00",
            "updated_at": "2026-09-24T00:00:00+00:00",
            "evidence_class": "suite_run", "suite": "case_rag",
            "retrieval_policy_version": "retrieval-v1",
        }
        connection.execute(sa.text(
            "INSERT INTO evaluation_jobs (" + ", ".join(columns) + ") VALUES (" +
            ", ".join(f":{name}" for name in columns) + ")"
        ), columns)
        assert connection.scalar(sa.text(
            "SELECT snapshot_id FROM evaluation_jobs WHERE job_id = 'suite-a'"
        )) is None
        with pytest.raises(IntegrityError):
            connection.execute(sa.text(
                "UPDATE evaluation_jobs SET dataset_key = NULL WHERE job_id = 'suite-a'"
            ))
        link.op = Operations(MigrationContext.configure(connection))
        link.upgrade()
        assert connection.scalar(sa.text(
            "SELECT evaluation_run_id FROM evaluation_jobs WHERE job_id = 'suite-a'"
        )) is None
        with pytest.raises(IntegrityError):
            connection.execute(sa.text(
                "UPDATE evaluation_jobs SET status = 'completed' WHERE job_id = 'suite-a'"
            ))
        connection.execute(sa.text(
            "UPDATE evaluation_jobs SET status = 'completed', "
            "evaluation_run_id = 'run-a' WHERE job_id = 'suite-a'"
        ))
        assert connection.scalar(sa.text(
            "SELECT evaluation_run_id FROM evaluation_jobs WHERE job_id = 'suite-a'"
        )) == "run-a"
        connection.execute(sa.text(
            "CREATE TABLE evaluation_runs ("
            "evaluation_run_key VARCHAR(64) PRIMARY KEY)"
        ))
        artifacts.op = Operations(MigrationContext.configure(connection))
        artifacts.upgrade()
        columns = {
            "artifact_id": "artifact-a",
            "evaluation_run_key": "run-a",
            "tenant_id": "tenant-a",
            "report_kind": "json",
            "report_schema_version": "evaluation-report-v1",
            "content_sha256": "d" * 64,
            "content_text": "{}\n",
            "created_at": "2026-09-24T00:00:00+00:00",
        }
        connection.execute(sa.text(
            "INSERT INTO evaluation_runs (evaluation_run_key) VALUES ('run-a')"
        ))
        connection.execute(sa.text(
            "INSERT INTO evaluation_report_artifacts (" + ", ".join(columns) + ") VALUES (" +
            ", ".join(f":{name}" for name in columns) + ")"
        ), columns)
        assert connection.scalar(sa.text(
            "SELECT content_sha256 FROM evaluation_report_artifacts "
            "WHERE artifact_id = 'artifact-a'"
        )) == "d" * 64
        with pytest.raises(IntegrityError):
            connection.execute(sa.text(
                "INSERT INTO evaluation_report_artifacts (" + ", ".join(columns) + ") "
                "VALUES (" + ", ".join(f":{name}" for name in columns) + ")"
            ), columns)
