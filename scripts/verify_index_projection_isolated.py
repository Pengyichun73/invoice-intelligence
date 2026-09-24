"""仅在专用 PostgreSQL 库与 Milvus 前缀运行的脱敏双进程投影验收。"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from invoice_intelligence.application.ports.examples import TenantRedactionPolicy
from invoice_intelligence.application.services.example_index_projection import (
    ExampleIndexProjectionService,
)
from invoice_intelligence.application.services.field_semantic_catalog import FieldSemanticCatalog
from invoice_intelligence.application.services.field_semantic_index_projection import (
    FieldSemanticIndexProjectionService,
)
from invoice_intelligence.config.settings import get_settings
from invoice_intelligence.domain.examples import IndexVersion, ModelVersion, PromptVersion
from invoice_intelligence.domain.field_semantics import FieldSemanticCatalogVersion
from invoice_intelligence.domain.invoice import InvoiceExtraction
from invoice_intelligence.infrastructure.corrections.redaction import TenantExampleRedactor
from invoice_intelligence.infrastructure.corrections.scopes import PydanticCorrectionScopeResolver
from invoice_intelligence.infrastructure.indexing.integrity import (
    SQLAlchemyMilvusIndexIntegrityVerifier,
)
from invoice_intelligence.infrastructure.indexing.milvus_examples import MilvusExampleIndexStore
from invoice_intelligence.infrastructure.indexing.milvus_field_semantics import (
    MilvusFieldSemanticIndexStore,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_admission import (
    SQLAlchemyMemoryAdmissionRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_examples import (
    SQLAlchemyExampleRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_field_semantic_index import (
    SQLAlchemyFieldSemanticProjectionRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_field_semantics import (
    SQLAlchemyFieldAliasRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    DocumentRow,
    ExampleFeedbackRow,
    ExampleIndexProjectionRow,
    ExtractionRunRow,
    FieldSemanticIndexProjectionRow,
    MemoryAdmissionDecisionRow,
    MemoryAdmissionRecordRow,
    ModelVersionRow,
    PromptVersionRow,
    ReviewedExampleRow,
)

TAG = os.environ["INDEX_ACCEPTANCE_TAG"]
if not TAG.startswith("20260924_") or not TAG.replace("_", "").isdigit():
    raise RuntimeError("INDEX_ACCEPTANCE_TAG must identify a dedicated 20260924 database")
RECOVERY_DRILL = os.environ.get("INDEX_RECOVERY_DRILL") == "1"
DATABASE = (
    "invoice_longrun_acceptance" if RECOVERY_DRILL else f"invoice_index_acceptance_{TAG}"
)
EXPECTED_HOST = "postgres" if RECOVERY_DRILL else "postgres-business"
MILVUS_URI = "http://milvus:19530"
TENANT = f"index-acceptance-{TAG}"
EXAMPLE_VERSION = IndexVersion(f"examples-{TAG}")
FIELD_VERSION = IndexVersion(f"fields-{TAG}")
MODEL_VERSION = ModelVersion("synthetic-dense-v1")
PROMPT_VERSION = PromptVersion("synthetic-prompt-v1")
CATALOG_VERSION = FieldSemanticCatalogVersion("synthetic-base-v1")


class Dense:
    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if self.delay:
            await asyncio.sleep(self.delay)
        return () if self.fail else tuple((1.0, 0.0, 0.0, 0.0) for _ in texts)


class Sparse:
    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[tuple[int, float], ...], ...]:
        return tuple(((1, 1.0),) for _ in texts)


def _digest(*parts: str) -> str:
    return sha256("\0".join(parts).encode()).hexdigest()


def _engine() -> sa.Engine:
    url = get_settings().resolved_business_database_url.get_secret_value()
    parsed = sa.engine.make_url(url)
    if parsed.database != DATABASE or parsed.host != EXPECTED_HOST:
        raise RuntimeError("Acceptance script refuses non-isolated PostgreSQL")
    return sa.create_engine(url, pool_pre_ping=True)


def _services(engine: sa.Engine, *, fail: bool = False, delay: float = 0.0):
    examples = SQLAlchemyExampleRepository(engine)
    fields = SQLAlchemyFieldSemanticProjectionRepository(engine)
    example_store = MilvusExampleIndexStore(
        uri=MILVUS_URI, collection_prefix=f"accept_examples_{TAG}",
        alias=f"accept_examples_current_{TAG}", dense_dimension=4,
        bm25_function_enabled=False, sparse_metric_type="IP", max_retries=0,
    )
    field_store = MilvusFieldSemanticIndexStore(
        uri=MILVUS_URI, collection_prefix=f"accept_fields_{TAG}",
        alias=f"accept_fields_current_{TAG}", dense_dimension=4,
        bm25_function_enabled=False, sparse_metric_type="IP", max_retries=0,
    )
    verifier = SQLAlchemyMilvusIndexIntegrityVerifier(
        engine, example_store=example_store, field_store=field_store
    )
    catalog = FieldSemanticCatalog(
        schema_reader=PydanticCorrectionScopeResolver(
            vendor_feature_fields=(), template_feature_fields=()
        ),
        alias_repository=SQLAlchemyFieldAliasRepository(engine),
        output_schema=InvoiceExtraction,
        schema_version="3.0.0",
        base_catalog_version=CATALOG_VERSION,
    )
    dense = Dense(fail=fail, delay=delay)
    sparse = Sparse()
    example_service = ExampleIndexProjectionService(
        projection_repository=examples,
        admission_repository=SQLAlchemyMemoryAdmissionRepository(engine),
        redactor=TenantExampleRedactor(
            default_policy=TenantRedactionPolicy("mask", "synthetic-redaction-v1"),
            field_patterns=(".*",),
        ),
        index_store=example_store,
        dense_embedding_provider=dense,
        sparse_embedding_provider=sparse,
        redaction_policy_version="synthetic-redaction-v1",
        integrity_verifier=verifier,
    )
    field_service = FieldSemanticIndexProjectionService(
        catalog=catalog,
        projection_repository=fields,
        index_store=field_store,
        dense_embedding_provider=dense,
        sparse_embedding_provider=sparse,
        integrity_verifier=verifier,
    )
    return examples, fields, example_service, field_service, example_store, field_store


def _seed(engine: sa.Engine) -> None:
    now = datetime.now(UTC)
    model_id = _digest("model", TENANT, MODEL_VERSION.value)
    prompt_id = _digest("prompt", TENANT, PROMPT_VERSION.value)
    with Session(engine) as session, session.begin():
        if session.scalar(sa.select(sa.func.count()).select_from(ReviewedExampleRow)):
            raise RuntimeError("Acceptance database already contains Reviewed Examples")
        session.add(ModelVersionRow(
            model_version_id=model_id, tenant_id=TENANT,
            version=MODEL_VERSION.value, created_at=now,
        ))
        session.add(PromptVersionRow(
            prompt_version_id=prompt_id, tenant_id=TENANT,
            version=PROMPT_VERSION.value, created_at=now,
        ))
        session.flush()
        for index in range(5):
            document_id = str(uuid4())
            run_id = f"synthetic-run-{index}-{TAG}"
            feedback_id = f"synthetic-feedback-{index}-{TAG}"
            example_id = f"synthetic-example-{index}-{TAG}"
            source = {"document_reference": f"isolated://synthetic-{index}"}
            session.add(DocumentRow(
                document_id=document_id, tenant_id=TENANT,
                storage_uri=f"isolated://synthetic-{index}", mime_type="image/png",
                checksum=_digest("document", str(index)), created_at=now,
            ))
            session.flush()
            session.add(ExtractionRunRow(
                run_id=run_id, tenant_id=TENANT, thread_id=run_id,
                document_id=document_id, status="completed", created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(ExampleFeedbackRow(
                feedback_id=feedback_id, tenant_id=TENANT,
                replay_key=_digest("feedback", str(index)),
                semantic_fingerprint=_digest("feedback-fingerprint", str(index)),
                run_id=run_id, document_id=document_id, field_path="invoice_number",
                schema_version="3.0.0", label_type="confirmed_correct",
                reviewer_id="synthetic-reviewer", evidence_reference_json=source,
                model_version_id=model_id, prompt_version_id=prompt_id,
                is_valid=True, created_at=now,
            ))
            session.flush()
            session.add(ReviewedExampleRow(
                example_id=example_id, tenant_id=TENANT,
                replay_key=_digest("example", str(index)),
                semantic_fingerprint=_digest("example-fingerprint", str(index)),
                source_feedback_id=feedback_id, document_id=document_id, run_id=run_id,
                document_type="InvoiceExtraction", field_path="invoice_number",
                schema_version="3.0.0", catalog_version=CATALOG_VERSION.value,
                model_version_id=model_id, prompt_version_id=prompt_id,
                label_type="confirmed_correct", model_value_json="SYNTHETIC",
                reviewed_value_json="SYNTHETIC", evidence_reference_json=source,
                reviewer_id="synthetic-reviewer", is_reviewed=True, is_valid=True,
                occurrence_count=1, created_at=now, updated_at=now, last_seen_at=now,
            ))
            session.flush()
            status = "approved" if index < 4 else "rejected"
            decision_id = f"synthetic-decision-{index}-{TAG}"
            session.add(MemoryAdmissionDecisionRow(
                decision_id=decision_id, tenant_id=TENANT, example_id=example_id,
                previous_status="pending", status=status, authority="human_governor",
                decided_by="synthetic-reviewer", reason="synthetic-acceptance",
                reason_codes_json=[], assessment_ids_json=[], conflict_ids_json=[],
                policy_version="synthetic-policy-v1",
                idempotency_key_hash=_digest("decision", str(index)),
                revision=1, decided_at=now,
            ))
            session.flush()
            session.add(MemoryAdmissionRecordRow(
                example_id=example_id, tenant_id=TENANT, schema_version="3.0.0",
                status=status, current_decision_id=decision_id,
                policy_version="synthetic-policy-v1", revision=1,
                attempt_count=0, created_at=now, updated_at=now,
            ))


async def _register(engine: sa.Engine) -> int:
    _, _, example_service, field_service, _, _ = _services(engine)
    await example_service.register_index_version(
        TENANT, EXAMPLE_VERSION, "3.0.0", MODEL_VERSION, None, None, PROMPT_VERSION
    )
    return await field_service.register_index_version(
        TENANT, FIELD_VERSION, "3.0.0", CATALOG_VERSION, MODEL_VERSION, None
    )


def _worker(name: str, ready: multiprocessing.synchronize.Event) -> None:
    ready.wait(20)
    engine = _engine()
    try:
        _, _, examples, fields, _, _ = _services(engine, delay=0.2)

        async def run() -> None:
            example_result = await examples.project_pending(
                TENANT, EXAMPLE_VERSION, limit=1, worker_id=name, lease_seconds=5
            )
            field_result = await fields.project_pending(
                TENANT, FIELD_VERSION, limit=1, worker_id=name, lease_seconds=5
            )
            if (example_result.indexed, field_result.indexed) != (1, 1):
                raise RuntimeError("Independent worker failed to project both kinds")
            print(json.dumps({"worker": name, "examples": 1, "fields": 1}), flush=True)

        asyncio.run(run())
    finally:
        engine.dispose()


async def _verify(engine: sa.Engine, field_count: int) -> None:
    example_repo, field_repo, examples, fields, example_store, field_store = _services(engine)
    assert await examples.verify_index_version(TENANT, EXAMPLE_VERSION)
    assert await fields.verify_index_version(TENANT, FIELD_VERSION)
    example_manifest = await example_store.projection_manifest(TENANT, EXAMPLE_VERSION)
    field_manifest = await field_store.projection_manifest(TENANT, FIELD_VERSION)
    assert len(example_manifest) == 4
    assert len(field_manifest) == field_count
    assert f"synthetic-example-4-{TAG}" not in example_manifest
    with Session(engine) as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(ExampleIndexProjectionRow)
        ) == 4
        assert session.scalar(
            sa.select(sa.func.count()).select_from(FieldSemanticIndexProjectionRow)
        ) == field_count
        assert set(example_manifest.values()) == set(session.scalars(
            sa.select(ExampleIndexProjectionRow.projection_checksum)
        ).all())
        assert set(field_manifest.values()) == set(session.scalars(
            sa.select(FieldSemanticIndexProjectionRow.projection_checksum)
        ).all())
    print(json.dumps({
        "head": "20260924_0044_code_harness_repair_route",
        "workers": 2, "example_count": len(example_manifest),
        "field_count": len(field_manifest), "eligible_source_count": 4,
        "tenant": TENANT, "example_version": EXAMPLE_VERSION.value,
        "field_version": FIELD_VERSION.value, "checksums_match": True,
    }), flush=True)


async def _run(engine: sa.Engine, *, seed: bool = True) -> None:
    if seed:
        _seed(engine)
    field_count = await _register(engine)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    workers = [
        context.Process(target=_worker, args=(f"worker-{index}", ready))
        for index in (1, 2)
    ]
    for worker in workers:
        worker.start()
    ready.set()
    for worker in workers:
        worker.join(120)
        if worker.exitcode != 0:
            raise RuntimeError("Independent index projection worker failed")
    example_repo, field_repo, examples, fields, _, _ = _services(engine)
    bad_examples = _services(engine, fail=True)[2]
    bad_fields = _services(engine, fail=True)[3]
    assert (await bad_examples.project_pending(
        TENANT, EXAMPLE_VERSION, limit=1, worker_id="failure-example", lease_seconds=5
    )).failed == 1
    assert (await bad_fields.project_pending(
        TENANT, FIELD_VERSION, limit=1, worker_id="failure-field", lease_seconds=5
    )).failed == 1
    with Session(engine) as session, session.begin():
        past = datetime.now(UTC) - timedelta(days=1)
        session.execute(sa.update(ExampleIndexProjectionRow).where(
            ExampleIndexProjectionRow.status == "failed"
        ).values(next_attempt_at=past, updated_at=past))
        session.execute(sa.update(FieldSemanticIndexProjectionRow).where(
            FieldSemanticIndexProjectionRow.status == "failed"
        ).values(next_attempt_at=past, updated_at=past))
    assert (await examples.project_pending(
        TENANT, EXAMPLE_VERSION, limit=1, worker_id="retry-example", lease_seconds=5
    )).indexed == 1
    assert (await fields.project_pending(
        TENANT, FIELD_VERSION, limit=1, worker_id="retry-field", lease_seconds=5
    )).indexed == 1
    old_example = (await example_repo.claim_pending(
        TENANT, EXAMPLE_VERSION, 1, "old-example", 0.2
    ))[0]
    old_field = (await field_repo.claim_pending(
        TENANT, FIELD_VERSION, 1, "old-field", 0.2
    ))[0]
    await example_repo.renew_lease(
        TENANT, old_example.item.example_id, EXAMPLE_VERSION,
        "old-example", old_example.token, 0.2
    )
    await field_repo.renew_lease(
        TENANT, old_field.item.semantic_id, FIELD_VERSION,
        "old-field", old_field.token, 0.2
    )
    await asyncio.sleep(0.3)
    assert await field_repo.requeue_stale(TENANT, FIELD_VERSION, datetime.now(UTC)) == 1
    new_example = (await example_repo.claim_pending(
        TENANT, EXAMPLE_VERSION, 1, "new-example", 5
    ))[0]
    new_field = (await field_repo.claim_pending(
        TENANT, FIELD_VERSION, 1, "new-field", 5
    ))[0]
    assert new_example.token != old_example.token
    assert new_field.token != old_field.token
    for operation in (
        example_repo.mark_projected(
            TENANT, old_example.item.example_id, EXAMPLE_VERSION,
            "late-checksum", "old-example", old_example.token,
        ),
        field_repo.mark_projected(
            TENANT, old_field.item.semantic_id, FIELD_VERSION,
            "late-checksum", "old-field", old_field.token,
        ),
    ):
        try:
            await operation
        except Exception as exc:
            assert "no longer owned" in str(exc)
        else:
            raise RuntimeError("Late worker crossed fencing boundary")
    await examples._project_one(new_example, EXAMPLE_VERSION, 5)
    await fields._project_one(new_field, 5)
    await fields.project_pending(
        TENANT, FIELD_VERSION, limit=100, worker_id="drain", lease_seconds=5
    )
    await _verify(engine, field_count)


async def _alias_gate(engine: sa.Engine) -> None:
    example_repo, field_repo, examples, fields, example_store, field_store = _services(engine)
    assert (await examples.project_pending(
        TENANT, EXAMPLE_VERSION, limit=10, worker_id="idempotent-example", lease_seconds=5
    )).indexed == 0
    assert (await fields.project_pending(
        TENANT, FIELD_VERSION, limit=100, worker_id="idempotent-field", lease_seconds=5
    )).indexed == 0
    await examples.activate_index_version(TENANT, EXAMPLE_VERSION)
    await fields.activate_index_version(TENANT, FIELD_VERSION)
    next_example = IndexVersion(f"examples-{TAG}-next")
    next_field = IndexVersion(f"fields-{TAG}-next")
    await examples.register_index_version(
        TENANT, next_example, "3.0.0", MODEL_VERSION, None, None, PROMPT_VERSION
    )
    await fields.register_index_version(
        TENANT, next_field, "3.0.0", CATALOG_VERSION, MODEL_VERSION, None
    )
    assert (await examples.project_pending(
        TENANT, next_example, limit=10, worker_id="next-example", lease_seconds=5
    )).indexed == 4
    assert (await fields.project_pending(
        TENANT, next_field, limit=100, worker_id="next-field", lease_seconds=5
    )).indexed == 19
    assert await examples.verify_index_version(TENANT, next_example)
    assert await fields.verify_index_version(TENANT, next_field)
    example_manifest = await example_store.projection_manifest(TENANT, next_example)
    field_manifest = await field_store.projection_manifest(TENANT, next_field)
    await example_store.delete(TENANT, (next(iter(example_manifest)),), next_example)
    await field_store.delete(TENANT, (next(iter(field_manifest)),), next_field)
    assert not await examples.verify_index_version(TENANT, next_example)
    assert not await fields.verify_index_version(TENANT, next_field)
    for operation in (
        examples.activate_index_version(TENANT, next_example),
        fields.activate_index_version(TENANT, next_field),
    ):
        try:
            await operation
        except ValueError as exc:
            assert "not ready for activation" in str(exc)
        else:
            raise RuntimeError("Incomplete manifest activated")
    assert await example_repo.get_active_version(TENANT) == EXAMPLE_VERSION
    active_field = await field_repo.get_active_version(TENANT, "3.0.0")
    assert active_field is not None and active_field.index_version == FIELD_VERSION
    client = example_store._client_required()
    assert client.list_aliases(
        collection_name=example_store._resolver.resolve(TENANT, EXAMPLE_VERSION)
    )["aliases"]
    assert field_store._tenant_alias(TENANT) in field_store._client_required().list_aliases(
        collection_name=field_store._resolver.resolve(TENANT, FIELD_VERSION)
    )["aliases"]
    print(json.dumps({
        "idempotent_replay": True, "missing_manifest_blocks_activation": True,
        "old_postgres_versions_preserved": True, "old_milvus_aliases_preserved": True,
    }), flush=True)


async def _alias_check(engine: sa.Engine) -> None:
    example_repo, field_repo, examples, fields, example_store, field_store = _services(engine)
    next_example = IndexVersion(f"examples-{TAG}-next")
    next_field = IndexVersion(f"fields-{TAG}-next")
    assert not await examples.verify_index_version(TENANT, next_example)
    assert not await fields.verify_index_version(TENANT, next_field)
    assert await example_repo.get_active_version(TENANT) == EXAMPLE_VERSION
    active_field = await field_repo.get_active_version(TENANT, "3.0.0")
    assert active_field is not None and active_field.index_version == FIELD_VERSION
    assert example_store._client_required().list_aliases(
        collection_name=example_store._resolver.resolve(TENANT, EXAMPLE_VERSION)
    )["aliases"]
    assert field_store._tenant_alias(TENANT) in field_store._client_required().list_aliases(
        collection_name=field_store._resolver.resolve(TENANT, FIELD_VERSION)
    )["aliases"]
    print(json.dumps({
        "missing_manifest_blocks_activation": True,
        "old_postgres_versions_preserved": True,
        "old_milvus_aliases_preserved": True,
    }), flush=True)


async def _integrity_injection(engine: sa.Engine) -> None:
    example_repo, field_repo, examples, fields, example_store, field_store = _services(engine)
    example_version = IndexVersion(f"examples-{TAG}-integrity")
    field_version = IndexVersion(f"fields-{TAG}-integrity")
    await examples.register_index_version(
        TENANT, example_version, "3.0.0", MODEL_VERSION, None, None, PROMPT_VERSION
    )
    await fields.register_index_version(
        TENANT, field_version, "3.0.0", CATALOG_VERSION, MODEL_VERSION, None
    )
    assert (await examples.project_pending(
        TENANT, example_version, limit=10, worker_id="integrity-example", lease_seconds=5
    )).indexed == 4
    assert (await fields.project_pending(
        TENANT, field_version, limit=100, worker_id="integrity-field", lease_seconds=5
    )).indexed == 19
    stores = (
        (example_store, example_version, "example_id", examples),
        (field_store, field_version, "semantic_id", fields),
    )
    for store, version, id_field, service in stores:
        assert await service.verify_index_version(TENANT, version)
        collection = store._resolver.resolve(TENANT, version)
        client = store._client_required()
        original = client.query(
            collection_name=collection, filter="", output_fields=["*"], limit=1,
            consistency_level="Strong",
        )[0]
        extra = dict(original)
        extra[id_field] = f"synthetic-extra-{TAG}"
        client.upsert(collection_name=collection, data=[extra])
        assert not await service.verify_index_version(TENANT, version)
        client.delete(collection_name=collection, ids=[extra[id_field]])
        assert await service.verify_index_version(TENANT, version)
        corrupted = dict(original)
        corrupted["projection_checksum"] = "0" * 64
        client.upsert(collection_name=collection, data=[corrupted])
        assert not await service.verify_index_version(TENANT, version)
        client.upsert(collection_name=collection, data=[original])
        assert await service.verify_index_version(TENANT, version)
        wrong_scope = dict(original)
        wrong_scope[id_field] = f"synthetic-wrong-scope-{TAG}"
        wrong_scope["tenant_id"] = "synthetic-other-tenant"
        wrong_scope["index_version"] = "synthetic-other-version"
        client.upsert(collection_name=collection, data=[wrong_scope])
        try:
            await service.activate_index_version(TENANT, version)
        except Exception:
            pass
        else:
            raise RuntimeError("Wrong-scope manifest activated")
        assert await example_repo.get_active_version(TENANT) == EXAMPLE_VERSION
        active_field = await field_repo.get_active_version(TENANT, "3.0.0")
        assert active_field is not None and active_field.index_version == FIELD_VERSION
        client.delete(collection_name=collection, ids=[wrong_scope[id_field]])
        assert await service.verify_index_version(TENANT, version)
    print(json.dumps({
        "both_kinds": True, "extra_item_rejected": True,
        "checksum_tamper_rejected": True, "wrong_tenant_and_version_rejected": True,
        "old_versions_preserved": True,
    }), flush=True)


if __name__ == "__main__":
    database_engine = _engine()
    try:
        if os.environ.get("INDEX_ACCEPTANCE_PHASE") == "seed_only" and RECOVERY_DRILL:
            _seed(database_engine)
        elif os.environ.get("INDEX_ACCEPTANCE_PHASE") == "rebuild" and RECOVERY_DRILL:
            asyncio.run(_run(database_engine, seed=False))
        elif os.environ.get("INDEX_ACCEPTANCE_PHASE") == "integrity":
            asyncio.run(_integrity_injection(database_engine))
        elif os.environ.get("INDEX_ACCEPTANCE_PHASE") == "alias_check":
            asyncio.run(_alias_check(database_engine))
        elif os.environ.get("INDEX_ACCEPTANCE_PHASE") == "alias":
            asyncio.run(_alias_gate(database_engine))
        else:
            asyncio.run(_run(database_engine))
    finally:
        database_engine.dispose()
