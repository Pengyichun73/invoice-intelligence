# Pluggable Object Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为发票原件、渲染图片和派生文本提供 PostgreSQL 治理的可插拔 Local/S3-compatible 对象存储。

**Architecture:** Application 通过 `FileStorage`、`DownloadAccessIssuer`、`StoredObjectRepository` 和上传 Unit of Work 工作；Local 与 boto3 S3 Adapter 位于 Infrastructure。PostgreSQL 保存对象状态和恢复任务，MinIO/S3 仅保存可删除内容，生命周期 Worker 通过 claim/lease 完成删除和恢复。

**Tech Stack:** Python 3.12、Pydantic Settings、SQLAlchemy 2.x、PostgreSQL、boto3、FastAPI、Alembic、Docker Compose、MinIO。

---

### Task 1: Domain、Port 与错误契约

**Files:**
- Create: `src/invoice_intelligence/domain/storage.py`
- Modify: `src/invoice_intelligence/application/ports/file_storage.py`
- Create: `src/invoice_intelligence/application/ports/stored_objects.py`
- Modify: `src/invoice_intelligence/application/errors.py`
- Test: `tests/unit/test_storage_domain.py`

- [ ] 定义 `ObjectKind`、`StoredObjectStatus`、`StorageWriteRequest`、`StorageObjectMetadata`、`StoredObjectRecord`。
- [ ] 将下载 URL 签发拆为 `DownloadAccessIssuer` Protocol。
- [ ] 定义上传 begin/finalize、tenant 查询、claim/delete/retry Repository 契约。
- [ ] 增加五个稳定错误码对应的异常类型。
- [ ] 运行 `pytest tests/unit/test_storage_domain.py -q`，预期通过。

### Task 2: Local 与 S3-compatible Adapter

**Files:**
- Modify: `src/invoice_intelligence/infrastructure/storage/local.py`
- Create: `src/invoice_intelligence/infrastructure/storage/s3.py`
- Create: `src/invoice_intelligence/infrastructure/storage/download_access.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/test_file_storage_adapters.py`

- [ ] Local Adapter 按 kind 分目录并实现 save/read/head/delete、实际 SHA-256 校验和 traversal 防护。
- [ ] boto3 Adapter 固定 kind→Bucket，使用 conditional put、ChecksumSHA256、HEAD/GetObjectAttributes、有限标准重试和异常脱敏映射。
- [ ] S3 issuer 生成短期 GET URL；Local issuer 生成 HMAC API token URL。
- [ ] 运行 Adapter tests，S3 使用 Stubber/Mock，不连接真实 MinIO。

### Task 3: Settings

**Files:**
- Modify: `src/invoice_intelligence/config/settings.py`
- Modify: `.env.example`
- Test: `tests/unit/test_settings.py`

- [ ] 增加 backend、endpoint、region、Bucket、secret file、TLS、TTL、保留期和 Worker 设置。
- [ ] 校验 Bucket 互异、production 禁止 local/HTTP/空 HMAC、secret/env 冲突。
- [ ] 运行 settings tests。

### Task 4: 修复 Alembic DAG、Schema 与 Repository

**Files:**
- Modify: `migrations/versions/20260923_0025_*.py`
- Modify: `migrations/versions/20260923_0026_accounting_domain.py`
- Create: `migrations/versions/20260923_0029_object_storage_phase_one.py`
- Create: `migrations/versions/20260923_0030_object_storage_phase_two.py`
- Modify: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_models.py`
- Create: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_stored_objects.py`
- Modify: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_repository.py`
- Test: `tests/integration/test_stored_object_repository.py`

- [ ] 本地数据库当前为 `20260909_0022`；部署前确认其他环境均未应用这些开发期迁移。明确映射为 index queue `0025`、promotion `0026`、transaction `0027`、accounting `0028` 并线性设置 down_revision，不改业务 DDL；验证 `alembic heads` 只有一个 head。
- [ ] Phase one 创建 `stored_objects`、`artifact_recovery_tasks` 和 upload claim lease 字段及约束/索引，为全部历史 Document 建立 `migration_pending` 对象。
- [ ] 为 Document 增加 nullable `original_object_id`，保留旧列用于历史迁移阶段；Phase two 作为独立收口发布，不加入普通日常 `upgrade head`，先检查无 migration pending/failed，再设 NOT NULL 并删除旧列。
- [ ] 实现 begin/finalize UoW、tenant 查询、SKIP LOCKED claim、CAS 状态转换。
- [ ] 运行 PostgreSQL integration tests，覆盖复合 FK/CHECK、SKIP LOCKED、lease、revision/fencing CAS、并发 idempotency 和 phase-two gate。

### Task 5: 上传和下载 Application Service/API

**Files:**
- Modify: `src/invoice_intelligence/application/services/document_ingestion.py`
- Modify: `src/invoice_intelligence/application/services/memory_admission.py`
- Modify: `src/invoice_intelligence/application/services/vision_extraction.py`
- Create: `src/invoice_intelligence/application/services/document_access.py`
- Modify: `src/invoice_intelligence/api/routes/documents.py`
- Modify: `src/invoice_intelligence/api/schemas/documents.py`
- Modify: `src/invoice_intelligence/api/dependencies.py`
- Test: `tests/unit/test_document_ingestion.py`
- Test: `tests/api/test_document_routes.py`

- [ ] 保留现有文件安全检查，一次性迁移全部 FileStorage 消费者；使用 begin_upload 稳定 ID、写入、可信 checksum 检查和 finalize_upload。
- [ ] 增加 tenant-scoped 下载 URL 和 Local content service；Router 只调用 Service。
- [ ] 映射 404、409、422、503 和稳定存储错误码，不暴露签名 URL到日志。
- [ ] 运行 ingestion/API tests。

### Task 6: 派生产物和生命周期 Worker

**Files:**
- Create: `src/invoice_intelligence/application/services/document_artifacts.py`
- Create: `src/invoice_intelligence/application/services/storage_lifecycle.py`
- Create: `src/invoice_intelligence/workers/storage_lifecycle.py`
- Modify: `src/invoice_intelligence/application/services/vision_extraction.py`
- Modify: `src/invoice_intelligence/workflow/nodes.py`
- Modify: `src/invoice_intelligence/workflow/state.py`
- Modify: `src/invoice_intelligence/bootstrap.py`
- Modify: `src/invoice_intelligence/main.py`
- Modify: `src/invoice_intelligence/workers/__init__.py`
- Test: `tests/integration/test_storage_artifact_recovery.py`
- Test: `tests/unit/test_storage_lifecycle.py`

- [ ] rendered 使用 document/page/renderer version 幂等保存；版本化 recipe resolver 只接受代码内已知版本。
- [ ] 从 Workflow 传入 source run 引用；derived text 使用 generation/source run/supersedes 关系保存，非确定性重建不冒充原产物。
- [ ] 实现过期授权撤销、delete claim、lease recovery、重试分类、reconciliation 和优雅停止。
- [ ] 最后在 `bootstrap.py`/`main.py` 组装 Storage、Issuer、Repository、上传 UoW、下载和 Worker Service。
- [ ] PostgreSQL 集成验证产物/恢复任务同事务、rendered 重放、OCR 新 generation/supersedes、未知 recipe 永久失败及 Workflow 状态隔离。
- [ ] 运行 lifecycle tests。

### Task 7: MinIO development profile 与历史内容迁移

**Files:**
- Modify: `docker-compose.yml`
- Create: `deploy/minio/init-storage.sh`
- Create: `src/invoice_intelligence/workers/storage_migration.py`
- Create: `src/invoice_intelligence/infrastructure/storage/startup_validation.py`
- Modify: `src/invoice_intelligence/infrastructure/storage/s3.py`
- Modify: `src/invoice_intelligence/config/settings.py`
- Modify: `src/invoice_intelligence/bootstrap.py`
- Modify: `.env.compose.example`
- Modify: `scripts/manage-local.ps1`
- Modify: `docs/docker-deployment.md`
- Test: `tests/unit/test_storage_migration.py`

- [ ] 保留现有 Milvus 对 MinIO 的依赖，增加独立业务 Bucket 初始化容器和可选 `storage-lifecycle` Worker；Linux init image 运行 shell/mc，不执行 PowerShell。
- [ ] 创建三个 private Bucket、禁止匿名访问并配置仅清理 multipart/获准删除对象的规则。
- [ ] 启动时检查 private policy、conditional-write 能力、可信 checksum/fallback 和 production legacy 门禁。
- [ ] 实现 local→S3 copy/checksum verify/transaction switch/rollback-safe CLI。
- [ ] 部署编排固定为 `alembic upgrade 20260923_0029` → `python -m invoice_intelligence.workers.storage_migration` → 显式执行 phase-two 收口；普通开发启动停在 phase one，复制完成前不执行收口。
- [ ] 运行迁移/安全门禁测试；真实 MinIO 集成仅使用 development 容器，不使用云端。

### Task 8: 文档与回归验证

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `INVOICE_INTELLIGENCE_SOLUTION.md`

- [ ] 记录配置、Docker profile、Worker、迁移阶段、错误码、生命周期和生产门禁。
- [ ] 运行相关 pytest、PostgreSQL 集成、mypy、ruff 和 Alembic graph/upgrade；覆盖删除提交失败重放、Local token 状态二次检查、S3 checksum fallback 和启动 fail-closed。
- [ ] 报告未执行的真实 MinIO/S3 集成验证和剩余风险。
