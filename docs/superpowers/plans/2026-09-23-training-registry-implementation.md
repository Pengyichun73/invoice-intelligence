# Training Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现独立七状态 TrainingJob Registry、远程训练 Port/Adapter、独立 Worker、API 与可选 MLOps 部署配置，并兼容既有 TrainingRun/ModelArtifact 链路。

**Architecture:** PostgreSQL 保存 Job、Dataset Export、Artifact 与审计事实；FastAPI Application Service 只登记意图，Training Worker 通过 claim/lease 调用 Provider。成功结果在单事务中同步旧 TrainingRun 与 ModelArtifact，默认 Stub fail closed。

**Tech Stack:** Python 3.12、Pydantic/dataclass、FastAPI、SQLAlchemy 2、Alembic、httpx、PostgreSQL、Docker Compose。

---

### Task 1: Domain 与 Port

**Files:**
- Create: `src/invoice_intelligence/domain/training_registry.py`
- Create: `src/invoice_intelligence/application/ports/training_registry.py`
- Test: `tests/test_training_registry_domain.py`

- [ ] 定义七状态、完整状态转换、TrainingJob、DatasetExport、TrainingArtifact、Provider request/snapshot 和 lease。
- [ ] 定义 TrainingProvider、TrainingArtifactReader/Verifier 与 TrainingRegistryRepository Protocol，包含 create/get/request_cancel/retry/claim/reschedule/advance/commit_success。
- [ ] 增加领域测试，覆盖版本绑定、终态不可变、取消/成功竞态和敏感 payload 拒绝。

### Task 2: SQLAlchemy Registry 与 Migration

**Files:**
- Modify: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_models.py`
- Create: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_training_registry.py`
- Create: `migrations/versions/<unique-head>_training_registry.py`
- Modify: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_model_training.py`
- Modify: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_training.py`
- Test: `tests/test_training_registry_repository.py`
- Test: `tests/integration/test_training_registry_postgresql.py`

- [ ] 先解析当前 Alembic heads；现有并发 migration revision 冲突未解决时不改写他人文件，待 heads 唯一后使用唯一 revision，或创建显式 merge revision 后再追加 training registry。
- [ ] 新增 training job、training artifact、training audit event，以及与现有 `training_dataset_versions` 一对一 FK 的 dataset export 元数据表；禁止复制 Dataset 内容或形成第二事实源。
- [ ] 实现租户隔离、Idempotency 摘要、revision CAS、claim/lease、失败重调度、重试派生和终态不可变。
- [ ] 在共享 SQLAlchemy Session/Unit-of-Work 中实现 success transaction：校验 lease 后同步 Job/旧 TrainingRun，登记新 Artifact 与旧 ModelArtifact/audit；旧 Repository reader 无需旁路即可读取。
- [ ] 增加 SQLite 隔离测试覆盖 claim 竞争、过期 lease、跨租户 404 语义和幂等重放。
- [ ] 增加 PostgreSQL 集成测试覆盖 `FOR UPDATE SKIP LOCKED`、全状态映射、原子回滚/reconcile，以及旧评估/部署 reader 可见性。

### Task 2A: 现有 Dataset Export 扩展

**Files:**
- Modify: `src/invoice_intelligence/application/services/training_dataset_export.py`
- Modify: `src/invoice_intelligence/infrastructure/training/jsonl.py`
- Modify: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_training.py`
- Test: `tests/test_training_dataset_export_registry.py`

- [ ] 在现有 `TrainingDatasetVersion` 导出完成时登记一对一 manifest 元数据、SHA-256、正/负样本计数，不创建平行 Dataset。
- [ ] 强制 eligibility：`is_reviewed=true`、`is_valid=true`、`admission_status=approved`，Hard Negative Candidate 已审批有效。
- [ ] 测试 `confirmed_correct/corrected.reviewed_value` 仅进入正例，`confirmed_incorrect.model_value` 仅进入 hard-negative，任何未审核/未准入数据均被拒绝。

### Task 3: Provider Adapter 与 Worker Service

**Files:**
- Create: `src/invoice_intelligence/infrastructure/training/stub.py`
- Create: `src/invoice_intelligence/infrastructure/training/mlflow_compatible.py`
- Create: `src/invoice_intelligence/infrastructure/training/artifacts.py`
- Create: `src/invoice_intelligence/application/services/training_jobs.py`
- Create: `src/invoice_intelligence/application/services/training_worker.py`
- Create: `src/invoice_intelligence/workers/training.py`
- Test: `tests/test_training_worker.py`

- [ ] Stub 明确返回 unsupported/permanent error，不创建伪远程成功。
- [ ] MLflow-compatible Adapter 使用 httpx、凭据、timeout、host allowlist 和严格响应模型；不依赖 mlflow Python 包。
- [ ] Artifact Adapter 实现受控 `https`/可选 `file` 读取、redirect host 复验、最大字节数、timeout 与 SHA-256；Application Worker 只依赖 Artifact Reader/Verifier Port。
- [ ] Application Service 只创建/查询/取消/重试，不持有 Provider。
- [ ] Worker 实现 submit/refresh/cancel、动作域 operation id、失败分类、退避、checksum 校验、成功原子登记。
- [ ] 测试默认 Stub 隔离、暂时错误重调度、取消确认、成功校验与过期 lease 拒绝。

### Task 4: API 与 Composition Root

**Files:**
- Create: `src/invoice_intelligence/api/schemas/training.py`
- Create: `src/invoice_intelligence/api/routes/training.py`
- Modify: `src/invoice_intelligence/api/dependencies.py`
- Modify: `src/invoice_intelligence/api/main.py`
- Modify: `src/invoice_intelligence/bootstrap.py`
- Modify: `src/invoice_intelligence/config/settings.py`
- Modify: `.env.example`
- Test: `tests/test_training_api.py`

- [ ] 增加 create/get/cancel/retry API；tenant/actor 只来自 TrustedTenantContext，写操作要求 Idempotency-Key。
- [ ] 请求模型只暴露安全版本/目标配置，不接受 remote status、artifact 或 tenant。
- [ ] bootstrap 只在 composition root 创建 repository/provider/services；FastAPI 不启动 Worker。
- [ ] 配置默认 stub；MLflow-compatible 缺少 endpoint/credential 时 fail closed。
- [ ] API 测试断言请求只登记，不调用 Provider，且跨租户统一 Not Found。

### Task 5: Docker 与文档

**Files:**
- Modify: `docker-compose.yml`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `INVOICE_INTELLIGENCE_SOLUTION.md`

- [ ] 增加 `mlops` profile 的独立 Training Worker，不增加 MLflow 服务依赖。
- [ ] 记录环境变量、远程平台接入、LangSmith Prompt 引用边界、失败/取消/重试语义。
- [ ] 更新当前能力与 Not implemented，避免宣称任何具体远程平台已可用。

### Task 6: 验证

**Files:**
- Modify: `pyproject.toml`（仅在需要将新增模块纳入 mypy 时）

- [ ] 运行新增领域、Repository、Worker、API 测试。
- [ ] 运行相关既有测试、Ruff 与目标模块 mypy。
- [ ] 检查 migration head、OpenAPI 路由和 Docker Compose config；不启动服务、不访问真实远程平台。
