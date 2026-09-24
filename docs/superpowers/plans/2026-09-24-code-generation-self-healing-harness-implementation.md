# Code Generation and Self-Healing Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有发票识别 Schema 和唯一 LangGraph Workflow 的前提下，建立受控、可审计、fail-closed 的代码生成与自愈 Harness 基础能力。

**Architecture:** Harness 使用独立的 Domain/Application 状态机和固定节点顺序，不新增 LangGraph、Agent Graph 或开放式 Agent Loop。PostgreSQL 保存 Repository、Snapshot、Task、Patch、Execution、Watchdog、Postmortem 和审计事实；代码 CAST/向量索引是带版本的可重建派生投影。所有外部解析器、模型、Embedding/Reranker、Milvus 和 Sandbox 均通过 Port 接入，未配置时返回显式受限状态。

**Tech Stack:** Python 3.12、Pydantic、现有 SQLAlchemy/Alembic、现有 TrustedTenantContext/Observability/lease 模式、Tree-sitter（仅在明确配置 Grammar 后启用）、现有 Milvus 边界。

---

## 文件边界

新增：

- `src/invoice_intelligence/code_harness/__init__.py`
- `src/invoice_intelligence/code_harness/domain/`
- `src/invoice_intelligence/code_harness/application/ports/`
- `src/invoice_intelligence/code_harness/application/services/`
- `src/invoice_intelligence/code_harness/workflow/`
- `src/invoice_intelligence/code_harness/infrastructure/`
- `migrations/versions/<timestamp>_code_harness.py`
- `docs/code-harness.md`
- `docs/code-harness-initialization-prompt.md`

按现有项目约定修改：

- `pyproject.toml`：仅增加已批准的解析依赖及版本约束；未配置 Grammar 时不得自动宣称可用。
- `src/invoice_intelligence/bootstrap.py`：只增加 Harness Port/Service/Adapter 的单一组装入口。
- `README.md`
- `docs/architecture.md`
- `docs/project-status.md`

不得修改：

- `InvoiceExtraction` 及其 Schema 文档中的既有字段契约。
- 既有发票 Workflow 节点、GraphState、Checkpointer 表。
- `references/` 和工作区中与本任务无关的文件。

## Task 1: Domain 与版本化契约

**Files:**
- Create: `src/invoice_intelligence/code_harness/domain/code_model.py`
- Create: `src/invoice_intelligence/code_harness/domain/patch_model.py`
- Create: `src/invoice_intelligence/code_harness/domain/execution.py`
- Create: `src/invoice_intelligence/code_harness/domain/watchdog.py`
- Create: `src/invoice_intelligence/code_harness/domain/postmortem.py`
- Create: `src/invoice_intelligence/code_harness/domain/errors.py`
- Create: `src/invoice_intelligence/code_harness/domain/tenant_boundary.py`

- [ ] 定义 `RepositorySource`、不可变 `RepositorySnapshot`、`CodeArtifact`、`CodeSymbol`、`CallEdge` 和 `CastChunk`。
- [ ] 所有代码对象统一绑定 `tenant_id`、`repository_id`、`snapshot_id`、`revision` 和 schema/parser/index 版本。
- [ ] 定义 Patch Operation 的 UTF-8 byte range、文件 base checksum、LF/CRLF 策略、create payload、操作排序、重叠拒绝和跨文件依赖声明。
- [ ] 定义 Task、Attempt、Execution、Budget、WatchdogObservation、Postmortem 和 Admission 状态。
- [ ] 对状态迁移、版本冲突、不可用能力、路径安全和租约 fencing 定义稳定错误码。
- [ ] 保证 Domain 不导入 FastAPI、SQLAlchemy、Milvus、Tree-sitter、LangGraph 或模型 SDK。

## Task 2: Application Ports 与确定性 Policy

**Files:**
- Create: `src/invoice_intelligence/code_harness/application/ports/parser.py`
- Create: `src/invoice_intelligence/code_harness/application/ports/code_model.py`
- Create: `src/invoice_intelligence/code_harness/application/ports/code_embedding.py`
- Create: `src/invoice_intelligence/code_harness/application/ports/code_reranker.py`
- Create: `src/invoice_intelligence/code_harness/application/ports/code_index.py`
- Create: `src/invoice_intelligence/code_harness/application/ports/code_retrieval.py`
- Create: `src/invoice_intelligence/code_harness/application/ports/patch_validator.py`
- Create: `src/invoice_intelligence/code_harness/application/ports/sandbox.py`
- Create: `src/invoice_intelligence/code_harness/application/ports/postmortem.py`
- Create: `src/invoice_intelligence/code_harness/application/ports/harness_observability.py`
- Create: `src/invoice_intelligence/code_harness/workflow/policy.py`

- [ ] 定义固定 Port 的输入/输出、版本字段、能力探测、超时/重试和错误分类。
- [ ] 定义 Scope Filter，检索前强制校验 tenant/repository/snapshot/revision/language/path 以及 schema/parser/grammar/redaction/index/config 版本。
- [ ] 定义有限预算和 `repair_pending.next_stage`。
- [ ] 定义实质改善指标：error signature 必须改变，且 AST 节点集合、受影响符号或结构化诊断至少一项变化。
- [ ] 定义 `sandbox_not_configured`、`sandbox_unavailable` 固定进入 `quarantined` 的 Policy。

## Task 3: Repository Snapshot、Parser 与 CAST

**Files:**
- Create: `src/invoice_intelligence/code_harness/application/services/repository_inspection.py`
- Create: `src/invoice_intelligence/code_harness/application/services/code_context.py`
- Create: `src/invoice_intelligence/code_harness/infrastructure/parsing/grammar_registry.py`
- Create: `src/invoice_intelligence/code_harness/infrastructure/parsing/tree_sitter_parser.py`
- Create: `src/invoice_intelligence/code_harness/infrastructure/parsing/redaction.py`
- Create: `src/invoice_intelligence/code_harness/infrastructure/indexing/cast_builder.py`
- Create: `src/invoice_intelligence/code_harness/infrastructure/indexing/code_index.py`

- [ ] 只接受预登记 `RepositorySource`，拒绝符号链接逃逸、设备文件、工作区外路径和黑名单文件。
- [ ] 创建只读对象包或受控临时副本，保存 Git commit/tree/submodule 信息或非 Git 文件清单 checksum。
- [ ] 在目录遍历、复制、解析、Patch 校验和 Sandbox 输入前后复核真实路径、文件类型、大小和 checksum，阻断 TOCTOU。
- [ ] 未配置 Grammar 返回 `parser_not_configured`，不退化为文本 Patch。
- [ ] 从 AST/CST 生成符号、调用边和 CAST Chunk，保留 byte/line 范围和 parse diagnostics。
- [ ] 代码索引只生成可重建投影，配置误指向业务 Collection/database 时 fail closed。

## Task 4: Patch Validator 与 Watchdog

**Files:**
- Create: `src/invoice_intelligence/code_harness/application/services/patch_validation.py`
- Create: `src/invoice_intelligence/code_harness/infrastructure/patching/structured_patch.py`
- Create: `src/invoice_intelligence/code_harness/infrastructure/patching/path_policy.py`
- Create: `src/invoice_intelligence/code_harness/infrastructure/patching/ast_validation.py`
- Create: `src/invoice_intelligence/code_harness/application/services/self_healing.py`

- [ ] 按固定顺序执行路径、revision、资源、安全、隔离副本、AST/CST、影响范围和 fingerprint 校验。
- [ ] 使用 UTF-8 半开 byte range；同文件操作按 descending byte start 应用，重叠或重复锚点直接拒绝。
- [ ] 默认拒绝删除/创建文件、二进制、凭证、CI 权限、安全配置、网络策略和工作区外路径。
- [ ] 生成不含敏感内容的稳定 Patch fingerprint 和 error signature。
- [ ] 实现 exact/jitter/stall/budget/hard error/timeout/resource exhausted 判定。
- [ ] 通过 CAS、attempt 和 lease token 避免重复执行和重复增加 occurrence count。

## Task 5: 固定 Harness 状态机与 Sandbox 边界

**Files:**
- Create: `src/invoice_intelligence/code_harness/workflow/state.py`
- Create: `src/invoice_intelligence/code_harness/workflow/nodes.py`
- Create: `src/invoice_intelligence/code_harness/workflow/runner.py`
- Create: `src/invoice_intelligence/code_harness/infrastructure/sandbox/fail_closed.py`
- Create: `src/invoice_intelligence/code_harness/infrastructure/sandbox/test_only.py`
- Create: `src/invoice_intelligence/code_harness/application/services/code_harness.py`

- [ ] 按 `prepare_task -> inspect_repository -> retrieve_code_context -> generate_patch -> validate_patch -> execute_in_sandbox -> evaluate_result -> repair_or_finish` 固定顺序运行。
- [ ] State 只保存技术 ID、版本引用、有限上下文引用、错误码、预算、attempt 和有界历史。
- [ ] `FailClosedSandboxExecutor` 不执行代码并返回 `sandbox_not_configured`。
- [ ] 测试专用 Adapter 必须显式启用、标注非生产且不能改变生产终态。
- [ ] 恢复人工审核时重新加载 Snapshot/Patch/revision 并重新做确定性校验。
- [ ] `CodeHarnessService` 提供 `create_task/get_task/resume_task`，API 只能调用该 Service。

## Task 6: PostgreSQL 事实模型、投影登记与 Worker

**Files:**
- Create: `src/invoice_intelligence/infrastructure/persistence/code_harness_models.py`
- Create: `src/invoice_intelligence/infrastructure/persistence/code_harness_repositories.py`
- Create: `migrations/versions/<timestamp>_code_harness.py`
- Create: `src/invoice_intelligence/workers/code_harness.py`
- Modify: `src/invoice_intelligence/bootstrap.py`

- [ ] 新增独立 `code_harness_*` 表，不混入发票事实表，不修改 Checkpointer 表。
- [ ] 保存 Repository/Snapshot 文件清单、Task/Attempt/Patch/Validation/Execution/Watchdog/Postmortem/Decision/Projection/Audit 事实，以及不可变 `PostmortemSourceEvent`、事件类型、摘要 checksum 和 `event_id` 唯一约束。
- [ ] 对 `tenant_id + fingerprint + version_scope` 建立 Postmortem 聚合唯一约束；仅在新 source event 插入成功的同一事务中原子递增 `occurrence_count`。
- [ ] 幂等键唯一作用域包含 `tenant_id + operation_type + client_scope`。
- [ ] 状态更新使用 expected revision、lease token 和 fencing 条件；终态 write-once。
- [ ] Claim 使用 `FOR UPDATE SKIP LOCKED`；续租必须匹配 worker/lease token/revision；迟到 Worker 的完成写入必须被 fencing 拒绝并记录审计。
- [ ] 区分事实事务、准入事务和派生投影登记事务，不使用分布式事务。
- [ ] Worker 只负责 claim/lease、有限执行、Postmortem 准入或单案例投影登记，不伪装成全量索引重建。
- [ ] 若暴露 API，新增 Router 只能从可信认证上下文取得 `TrustedTenantContext`、Actor、Trace 和幂等键，禁止客户端 Body/Query/Header 覆盖。

## Task 7: 文档、初始化模板与能力声明

**Files:**
- Create: `docs/code-harness.md`
- Create: `docs/code-harness-initialization-prompt.md`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/project-status.md`

- [ ] 记录已实现入口、数据流、依赖方向、状态路由、事实/派生边界和安全限制。
- [ ] 明确未配置 Tree-sitter Grammar、真实 Code Model、Embedding/Reranker、Milvus 和 MicroVM 时的受限行为。
- [ ] 写入可复制到新项目初始化阶段的 Codex Prompt：先读事实、建立 Design/Plan、固定状态机、Port 隔离、PostgreSQL 事实源、Milvus 派生索引、fail-closed Sandbox、禁止开放式 Agent Loop。
- [ ] 不宣称自动修改、自动合并、自动部署或 MicroVM 生产安全验收已完成。

## 验证与交付约束

- [ ] 仅执行与修改直接相关的静态导入、类型/语法检查和文档一致性检查；不启动服务、不连接真实 Provider/Milvus/PostgreSQL、不执行生成代码。
- [ ] 检查 `InvoiceExtraction` Schema、既有发票 Workflow 和 Checkpointer 表没有变更。
- [ ] 检查新增 API/Worker 只通过 Application Service/Port 访问基础设施。
- [ ] 检查工作区无关改动保持原状。
- [ ] 最终报告列出当前实现、受限能力、未实现外部依赖、验证结果和剩余风险。
