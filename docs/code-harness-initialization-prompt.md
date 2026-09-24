# 新项目初始化提示词：代码生成与自愈调试 Harness

你是本项目的 Senior Staff Software Engineer。请将“面向大语言模型的代码生成与自愈调试 Harness”以受控、可审计、可重建、fail-closed 的方式集成到当前项目。

## 执行协议

1. 先读取 `README.md`、架构文档、领域 Schema、`pyproject.toml`、现有 Workflow、Application Port、Infrastructure、Migration、API 和 Worker；同时检查 Git 工作区状态。
2. 不根据文件名猜测已有能力；先输出能力盘点、差距矩阵、复用边界和迁移风险。
3. 先写 Design，再写 Implementation Plan；保持以下固定边界。
4. 使用项目既有依赖、Composition Root、租户上下文、Observability、幂等和 lease/fencing 模式。
5. 只修改必要文件；手工编辑使用 `apply_patch`；不提交 Git、不启动服务、不连接真实生产依赖，除非明确授权。

## 不可变架构约束

- 不修改现有业务 Entity Schema 的字段、类型、可空性和校验语义。
- 不新增 Supervisor、Multi-Agent、Chat Agent、开放式 Agent Loop 或第二个 LangGraph/Agent Graph。
- Harness 使用独立的确定性 Application 状态机：

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

- Domain 不依赖 Web Framework、ORM、向量数据库、Parser、LangGraph 或模型 SDK。
- API 只能调用 Application Service；Workflow 只能调用 Port/Service；Adapter 只能在 Infrastructure 和 Composition Root 组装。
- PostgreSQL 保存 Repository、Snapshot、Task、Patch、Validation、Execution、Watchdog、Postmortem、准入和审计事实。
- 向量数据库只保存脱敏、带版本、可删除、可重建的代码索引派生数据。
- 代码索引必须和业务记忆索引使用独立 Collection、Alias、Database/URI、凭据和故障域；误指向业务索引时 fail closed。

## 必须实现的能力

### 代码理解

- 通过 `CodeParser` Port 接入 Tree-sitter 或已批准的等价 AST/CST Parser。
- 支持不完整代码的容错诊断，但未配置 Grammar 时返回 `parser_not_configured`，不得退化为文本 Patch。
- 提取文件、模块、类、函数、方法、调用边和 byte/line 范围。
- 以 AST/CST 语义节点生成 CAST Chunk，不使用单纯滑动窗口。
- `CodeArtifact`、`CodeSymbol`、`CallEdge` 和 CAST Chunk 必须绑定 `tenant_id`、`repository_id`、`snapshot_id`、`revision` 及版本字段。

### Repository Snapshot

- 只接受预登记的 Repository Source，不接受任意宿主机路径。
- 创建不可变 Snapshot；Git 来源保存 commit/tree hash 和子模块 commit，非 Git 来源保存文件清单 checksum。
- 使用只读对象包或受控临时副本；复制、解析、Patch 校验和 Sandbox 输入前后复核真实路径、符号链接、文件类型、大小和 checksum，防止 TOCTOU。
- 解析、Patch 和 Sandbox 必须使用同一个 `snapshot_id + revision + snapshot_checksum`。

### 结构化 Patch

- 模型只能返回 Pydantic/JSON Schema 约束的 Patch Proposal，不能返回任意命令、路径或执行权限。
- 支持 `replace_span`、`insert_before`、`insert_after`、`delete_span`、受控 `create_file` 和显式许可的 `delete_file`。
- UTF-8 byte offset 使用半开区间 `[byte_start, byte_end)`；每个操作必须携带文件 base checksum、前置指纹、语言和换行策略。
- 同文件操作按 descending byte start 应用；重叠、重复锚点、前置 checksum 不匹配、AST 指纹不匹配或未声明的跨文件依赖必须拒绝。
- 默认拒绝二进制、凭证、CI 权限、安全配置、网络策略、工作区外路径和任意文件删除。

### 确定性自愈与 Watchdog

- 只有 Python Policy 可以路由状态，模型不能决定下一节点。
- `repair_pending` 必须保存 `next_stage`、`retry_at`、`reason_code` 和 attempt。
- 新 Attempt 只能在旧 Attempt 以不可变终态提交后创建，并使用任务 CAS、lease token 和 fencing。
- 只有 error signature 改变且 AST 节点集合、受影响符号或结构化诊断至少一项变化，才算实质改善。
- 检测 `exact_repeat`、`jitter_loop`、`stall_loop`、`budget_exhausted`、`hard_error`、`timeout` 和 `resource_exhausted`。
- `sandbox_not_configured` 和 `sandbox_unavailable` 固定进入 `quarantined`；安全、路径、权限和版本硬失败不得重试。

### Sandbox

- 定义 `SandboxExecutor` Port。
- 没有真实 MicroVM 时只能提供 fail-closed Adapter；不执行生成代码。
- 普通 Docker、本地子进程或主进程执行不得标记为 MicroVM 等价安全边界。
- 生产 Adapter 必须实现网络默认拒绝、凭证不可见、工作区外不可写、资源限制和执行身份隔离。

### Postmortem Memory

- 修复成功后只生成 `pending` Postmortem 候选。
- 保存 `error_signature`、`root_cause`、`solution_pattern`、语言、符号类型、Patch 形状、版本范围、Repository/Snapshot/revision 来源和不可变来源事件。
- 只有 `approved + is_reviewed=true + is_valid=true` 才能进入长期检索。
- `pending`、`approved`、`quarantined`、`rejected`、`suspended`、`invalidated` 必须保持独立语义。

## 能力缺失时的行为

- Parser 缺失：`parser_not_configured`，任务隔离。
- Code Model 缺失：`model_provider_not_configured`，不生成 Patch。
- Embedding/Reranker 缺失：只允许精确文件/符号/结构化查询。
- 代码 Milvus 缺失：`code_index_unavailable`，不以空结果伪造命中。
- Sandbox 缺失：`sandbox_not_configured`，进入 `quarantined`。
- PostgreSQL 缺失：拒绝任务，不用内存状态替代事实源。

## 交付要求

最终报告必须列出：修改文件、关键入口、数据流、依赖方向、固定 Workflow 和路由、Watchdog、Sandbox 边界、事实/派生边界、已实现能力、未实现能力、验证结果和剩余风险。任何 Stub、Test-only Adapter 或配置解析成功都不能作为生产验收证据。
