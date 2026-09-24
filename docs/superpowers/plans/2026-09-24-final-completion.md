# Invoice Intelligence 最终完整实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在不连接生产系统的前提下，完成 Invoice Intelligence 的隔离环境验收、后台队列闭环、索引投影完整性、运维恢复和质量门禁，使项目可以明确区分“已实现”“受限可用”和“未实现”。

**Architecture:** PostgreSQL 继续作为唯一事实源；Milvus、对象存储、评估和训练平台均通过固定 Port/Adapter 接入。Worker 只执行可恢复的 `project/verify`，Alias、Catalog、Admission 和模型状态仍由授权 API 显式改变。所有真实外部依赖只在隔离 Compose 项目中验证。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy/Alembic、PostgreSQL、Milvus standalone、Docker Compose profiles、pytest、Ruff、PowerShell runbook。

---

## 交付边界

- 不修改 `InvoiceExtraction` 字段、类型、可空性或 `extra="forbid"`。
- 不新增第二套 Workflow，不引入 Celery、Redis、Kafka、RabbitMQ 或 Agent 路由。
- 不连接生产数据库、真实财务系统、真实训练平台或生产流量控制面。
- 每阶段完成后同步 `README.md`、`docs/architecture.md`、`docs/project-status.md`，只声明已经验证的能力。

### Task 1: 生产 preflight 与数据库/Secret 连接验收

**Files:**
- Modify: `docs/docker-deployment.md`
- Modify: `docs/project-status.md`
- Modify: `.env.compose.example`
- Modify: `scripts/` 下现有 preflight/runbook（仅在缺少检查时）
- Test: `tests/test_settings.py`、Compose 契约定向测试

- [ ] 准备隔离 Compose project、非生产数据库、非生产 secret 文件和独立 volumes。
- [ ] 执行 `docker compose --env-file .env.compose --profile core config --quiet`，确认 required interpolation、secret mount、healthcheck 和 worker ID。
- [ ] 执行 Alembic migration 到单一 head，记录 revision、数据库连接和密码文件权限。
- [ ] 验证 API、migration、memory admission、index projection、evaluation、scheduler、training worker 均通过 `SELECT 1` 和 runtime probe。
- [ ] 验证 `/api/v1/health`、`/api/v1/ready` 的语义不混淆，失败时记录脱敏错误码。
- [ ] 仅在隔离环境运行相关配置/API 回归测试；保存命令、revision、容器状态和失败原因。
- [ ] 将“容器连接已验证”或“仍受阻”写入状态文档。

### Task 2: Index Projection Worker 重试封顶与恢复闭环

**Files:**
- Modify: `src/invoice_intelligence/workers/index_projection.py`
- Modify: `src/invoice_intelligence/application/services/example_index_projection.py`
- Modify: `src/invoice_intelligence/application/services/field_semantic_index_projection.py`
- Modify: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_examples.py`
- Modify: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_field_semantic_index.py`
- Modify: `src/invoice_intelligence/config/settings.py`
- Test: projection queue、claim/lease、retry exhaustion、fencing 定向测试

- [ ] 让 `index_projection_worker_max_attempts` 真正进入 claim policy。
- [ ] 当 `attempt_count >= max_attempts` 时停止 claim，清空 `next_attempt_at`，保留 `failed`、安全错误码和审计上下文。
- [ ] 保证 stale lease 可回收、旧 worker/token 不能迟到写入、新 worker 可重新领取。
- [ ] 检查 `project_pending`、`verify`、`activate`、`rollback` 的状态前置条件和 revision/CAS。
- [ ] 验证 Worker 永不调用 Alias 自动切换。

### Task 3: Milvus 完整投影验证与稳定 ID 核对

**Files:**
- Modify: `src/invoice_intelligence/application/ports/` 下索引 Store Port
- Modify: `src/invoice_intelligence/application/services/example_index_projection.py`
- Modify: `src/invoice_intelligence/application/services/field_semantic_index_projection.py`
- Modify: `src/invoice_intelligence/infrastructure/indexing/milvus_examples.py`
- Modify: `src/invoice_intelligence/infrastructure/indexing/milvus_field_semantics.py`
- Add/Modify: `src/invoice_intelligence/domain/` 下 `IndexVerificationResult` 契约
- Test: Milvus Adapter stub、stable ID missing/extra、collection/alias metadata 测试

- [ ] 定义不含原始值、Base64、完整向量的 `IndexVerificationResult`。
- [ ] 统计 PostgreSQL eligible count、投影成功 count、Milvus row count、stable ID missing/extra、collection metadata。
- [ ] 验证 tenant、document type、field path、schema/catalog/index version、reviewed/valid 过滤条件全部生效。
- [ ] 仅当完整性验证通过时允许授权 API 激活 Alias。
- [ ] 验证 rollback 只能切换到仍有效且完整投影的历史版本。

### Task 4: 隔离 PostgreSQL 双 Worker 与真实 Milvus 演练

**Files:**
- Modify: `docs/index-projection-worker-runbook.md`
- Modify: `docs/backup-restore-drill.md`
- Modify: `docs/project-status.md`

- [ ] 在隔离 PostgreSQL 启动两个不同 worker ID，制造同一批 projection claim。
- [ ] 记录 lease 过期、重领、迟到完成、重试耗尽和恢复结果。
- [ ] 启动隔离 Milvus/etcd/object storage，执行 register -> project -> verify。
- [ ] 确认旧 Alias 在新版本未验证前保持不变，授权 activate 后再切换。
- [ ] 记录 stable ID 集合核对结果、collection 状态、Alias 状态和回滚结果。

### Task 5: Evaluation、Training、Scheduler 隔离闭环

**Files:**
- Modify: `docs/project-status.md`
- Modify: `docs/docker-deployment.md`
- Test: evaluation/training/auth/tenant/queue 回归矩阵

- [ ] 为 evaluation 使用独立 PostgreSQL DSN，验证 Snapshot 不可变、Job 幂等、claim/lease、scheduler 入队和 retry。
- [ ] 验证 training-worker 是唯一执行入口，Stub 缺凭据时进入 fail-closed 状态。
- [ ] 验证 evaluation/training 不写入业务事实、不跨租户读取、不自动晋升模型。
- [ ] 记录“隔离 Stub 已验收，真实平台未接入”的明确边界。

### Task 6: 备份恢复、对象存储与 Milvus 重建演练

**Files:**
- Modify: `scripts/backup-restore-drill.ps1`（仅修复发现的问题）
- Modify: `docs/backup-restore-drill.md`
- Modify: `docs/project-status.md`

- [ ] 在隔离 Compose project 执行 PostgreSQL、Checkpointer、对象存储备份。
- [ ] 按 runbook 恢复 migration head、租户事实、对象 checksum 和审核状态。
- [ ] 从 PostgreSQL 重新 register/project/verify 新 Milvus Collection，不把 Milvus volume 当事实备份。
- [ ] 授权 activate 新 Alias，验证旧 Alias 保留策略和失败补偿路径。
- [ ] 记录 RPO/RTO、恢复命令、checksum 和未覆盖风险。

### Task 7: 可观测性与质量门禁

**Files:**
- Modify: `docs/observability-alerts.md`
- Modify: `docs/project-status.md`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Test: metrics endpoint、日志脱敏、tenant isolation、workflow non-failure 回归测试

- [ ] 验证 `/api/v1/metrics` 只暴露低基数指标，不包含发票值、Base64、Prompt、向量和跨租户标签。
- [ ] 为 OCR、projection、evaluation、training、security audit 核对成功/失败/重试/lease recovery 指标。
- [ ] 在隔离环境接入 Prometheus/Grafana 或等价接收端；未接入时明确标记为受限可用。
- [ ] 运行 Ruff、mypy、文档检查脚本和本次变更相关测试；Windows 临时目录权限问题单独记录，不删除测试。
- [ ] 统一 README 顶部状态日期为 `2026-09-24`，与 `docs/project-status.md` 一致。

## 最终验收顺序

1. 配置和 Compose 结构检查。
2. Migration head 与隔离 PostgreSQL preflight。
3. API、Worker runtime probe 和租户隔离回归。
4. 双 Worker lease/retry/fencing 演练。
5. Milvus register/project/verify/activate/rollback 演练。
6. Evaluation/Training 隔离队列演练。
7. 备份恢复与 Milvus 重建演练。
8. 文档、Ruff、mypy、测试和状态基线复核。

## 最终“完整实现”判据

只有同时满足以下条件，项目才可标记为“隔离环境验收完成”：

- PostgreSQL migration、secret/DSN、API 和全部启用 Worker 在隔离容器中可连接。
- Projection 达到 retry 封顶、lease recovery、双 Worker fencing 和稳定 ID 完整性验证。
- Milvus 新 Collection 完整 project/verify 后才允许 Alias activate，rollback 可恢复。
- Evaluation、Training、Backup/Restore 均有隔离演练记录。
- 文档状态、风险和未实现能力与代码事实一致。

即使上述条件全部满足，也不能宣称已接入真实财务系统、真实训练平台、生产部署控制面或生产流量。
