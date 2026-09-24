# Code Generation and Self-Healing Harness

本项目的 Harness 设计用于在受控边界内完成代码理解、结构化 Patch、确定性校验、隔离执行、有限自愈和失败经验治理。它不是开放式 Agent，也不改变发票识别业务 Schema 或既有唯一 LangGraph Workflow。

## 状态流

```text
prepare_task
-> inspect_repository
-> retrieve_code_context
-> generate_patch
-> validate_patch
-> execute_in_sandbox
-> evaluate_result
-> repair_or_finish
```

任务、Snapshot、Patch、Execution、Watchdog 和 Postmortem 事实由 PostgreSQL 保存；CAST Chunk 和代码向量仅作为脱敏、带版本、可重建的派生索引。解析器、代码模型、Embedding/Reranker、索引和 Sandbox 通过 Application Port 接入。

当前实现已落地 Domain/Port 契约、固定八阶段 Runner、不可变 Snapshot 绑定、Python 标准库 AST
适配器、Snapshot 精确检索适配器、结构化 Patch 校验、实际绑定的 Grammar Registry、结构化函数参数、
PostgreSQL 首版
事实模型/迁移/Task、Snapshot、Patch、Execution、Watchdog 与 Postmortem Repository、Task
claim/lease fencing、Attempt 完成 fencing、
通用 Harness Worker、共享 `PrivacyTelemetry` 与低敏 Metrics Adapter，以及默认拒绝 Sandbox。外部能力未配置时继续
fail closed：未配置 Grammar 不生成文本 Patch，未配置模型不生成 Patch；未配置外部代码索引时
仅使用同一 Snapshot 生成的本地精确/结构化索引，不伪造向量检索命中；未配置 MicroVM 不执行
代码。普通 Docker 或本地子进程不等同于 MicroVM。

任务预算目前显式约束最大尝试次数、墙钟时间、Patch 字节数、修改文件数、修改行数、
Patch operation 数和模型 Token 数；Patch 校验还会执行快照版本、文件 checksum、路径、
范围、重叠、锚点唯一性、保护路径和 Python 语法/AST 变更检查。`trace_id` 从可信租户上下文
进入 Task、Workflow、Postmortem source event 和低敏 Span，不使用 `task_id` 冒充 Trace ID。
当前 Metrics Adapter 记录进程内低基数计数器、阶段耗时和可选 Token 统计，不持久化资源技术 ID。
Snapshot 捕获会对 UTF-8/空字节及常见二进制扩展名执行保守分类；二进制文件可进入清单但不会
被 Parser 当作源码，Patch 校验也会拒绝对其执行文本操作。

`bootstrap.py` 已提供显式 `build_harness_worker_service` 组合入口，默认组装 Python AST、
Metrics 和 fail-closed Sandbox；Parser、Code Index、Code Model、MicroVM 等真实能力仍要求调用方
显式注入。当前已提供 Harness API：

- `POST /api/v1/code-harness/tasks`：创建幂等任务；
- `GET /api/v1/code-harness/tasks/{task_id}`：按可信租户上下文查询任务；
- `POST /api/v1/code-harness/tasks/{task_id}/resume`：使用 `expected_revision` 做 CAS 恢复。
- `GET /api/v1/code-harness/postmortems/{postmortem_id}`：按可信租户上下文读取脱敏 Postmortem；
- `POST /api/v1/code-harness/postmortems/{postmortem_id}/admission`：由可信 actor 使用
  `expected_revision`、状态和 reason code 执行 CAS 准入治理。

当前已提供独立 Worker 入口 `invoice_intelligence.workers.code_harness`，仅接受
PostgreSQL 业务库，并在启动时从 `code_harness_sources` 事实表加载已启用的 Repository Source。
未完成迁移、没有启用 Source、Code Model、Code Index 或 MicroVM 时保持 fail-closed；当前尚未
提供 Source Registry 的完整注册/更新治理 API、运行时 reload 或生产级多仓库调度验收。当前 Postmortem
source event 首次写入会保存错误签名、根因类别、解决模式、解析结果推导的影响语言/符号类型、
实际 Patch operation 形状、Repository/Snapshot/source revision、Task/Patch/Execution 来源和版本范围；
未提供可信 Trace Provider 时 `source_trace_id` 保持
`null`，不会使用任务 ID 冒充 Trace ID。

Python Parser 运行前必须通过已批准的 `GrammarRegistry` 注册检查。解析失败时，标准库 `tokenize` 仅恢复可确认的模块/类/函数声明、调用边和 UTF-8
范围，并将 `ParsedArtifact.parse_status` 标记为 `partial`，同时保留 `syntax_error` 诊断。
该结果可用于受限精确检索，但不代表完整 AST/CST，也不代表代码已成功解析；真实 Tree-sitter/CST
容错 Parser 仍未接入。

Postmortem source event 已能幂等写入，并通过独立的 Postmortem Application Service 和
HTTP/RBAC 路由执行可信 actor、租户边界、reason code 与 revision CAS admission；但尚未
接入长期代码经验检索投影，因此 `approved` 状态只表示治理事实已批准，不代表代码经验
已经进入可检索长期记忆。

代码上下文读取只允许从当前 Snapshot 的命中范围读取，最多保留有限字节和上下文行数；
`HarnessState` 只保存 `chunk_id` 等稳定引用，受控片段仅存于单次 Workflow 进程内缓存，
不会写入 PostgreSQL 或 `InvoiceExtraction`。

Watchdog 现在依据 AST 变化、受影响文件摘要和诊断签名变化区分 `jitter` 与 `stall`；
错误重复且三类变化均不存在时进入 `stall` 并隔离，不能依靠重复调用继续消耗预算。

Snapshot/Patch/Execution/Watchdog 已在 Workflow 阶段写入 PostgreSQL；相同 Snapshot/Patch/
observation 重放不会重复创建事实，Execution 按 `task_id + attempt_id` 保存不可变 Sandbox
结果，不覆盖历史 Attempt；Postmortem 来源事件由 `20260924_0043` migration 增加不可变来源列，不覆盖历史
来源事件。当前仍未完成生产接入：
Source Registry 注册/更新治理 API、运行时 reload、真实 Tree-sitter/CST
多语言 Parser、代码模型、Embedding/Reranker、Milvus 派生投影和真实 MicroVM。迁移文件已生成，
但本次未连接或执行真实 PostgreSQL；Source Registry 和 Harness Repository 仍需完成数据库级验证。

详细设计见 `docs/superpowers/specs/2026-09-24-code-generation-self-healing-harness-design.md`，实施计划见 `docs/superpowers/plans/2026-09-24-code-generation-self-healing-harness-implementation.md`。上述设计和计划描述的是目标边界，不代表所有外部 Provider、Milvus 或 MicroVM 已完成生产验收。
