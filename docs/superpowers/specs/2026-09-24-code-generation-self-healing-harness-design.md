# Code Generation and Self-Healing Harness Design

**日期：** 2026-09-24  
**状态：** 已批准的实施设计  
**范围：** 在 Invoice Intelligence 中增加受控、可审计、可重建的代码生成与自愈调试 Harness 基础能力。

## 1. 设计目标

Harness 负责把代码理解、结构化补丁、确定性校验、隔离执行、有限自愈和失败经验治理组织成一条固定流水线。它不是开放式 Agent，不修改 `InvoiceExtraction`，也不改变现有发票识别 Workflow 的业务语义。

本阶段的目标是建立生产可演进的 Domain、Application Port、派生代码索引、Patch 验证、Watchdog、fail-closed Sandbox 边界和 Postmortem 事实模型。真实 Tree-sitter Grammar、MicroVM、远程模型和真实 Milvus 连接未配置时，系统必须明确返回受限或不可用状态，不能以 Stub 结果伪造成功。

Harness 使用独立的确定性 Application 状态机，不新增 LangGraph、Agent Graph 或第二个 Checkpointer。项目现有发票识别 Workflow 仍是唯一 LangGraph Workflow；Harness 通过 Application Service/Worker 入口运行固定节点序列。

## 2. 当前事实与差距

### 已有可复用能力

- `src/invoice_intelligence/domain`、`application/ports`、`application/services`、`infrastructure` 和 `workflow` 已形成依赖反转边界。
- 现有 Workflow 使用单一确定性 LangGraph，条件路由由 Python Policy 决定，并支持 `interrupt/resume`。
- `bootstrap.py` 是现有 Composition Root。
- PostgreSQL 保存业务事实、审核事实、治理状态、版本、幂等和审计。
- Milvus 是审核案例和字段语义的可重建派生索引，已有租户过滤、版本、投影、激活和完整性校验边界。
- 已有 `Observability`、低敏 Trace/Event、Worker claim/lease/fencing、重试和 quarantine 语义。
- 已有结构化 Provider Port、Pydantic 校验、可信租户上下文和跨租户 404 语义。

### 必须新增的能力

- 代码文件的容错 AST/CST 解析、符号和调用关系提取。
- 基于语义节点的 CAST 分块和版本化代码上下文查询。
- 代码索引的 PostgreSQL 事实登记和可重建派生投影。
- 结构化 Patch Proposal、路径安全、AST 变化和资源预算验证。
- 固定代码修复 Workflow 的独立运行状态与确定性路由。
- Watchdog 的稳定错误签名、循环检测和预算终止。
- `SandboxExecutor` Port 及 fail-closed Adapter。
- Postmortem 事实、准入状态和可重建检索投影。

### 明确不在本阶段承诺

- 未配置 Grammar 的语言的生产级解析。
- 在宿主机、本地任意子进程或普通 Docker 容器中执行不可信生成代码。
- 真实 Firecracker/MicroVM 运行时的安全验收。
- 真实远程模型、Milvus、PostgreSQL 或生产部署控制面连接。
- 自动修改生产代码、自动合并或自动部署。

## 3. 方案选择

### 方案 A：文本 RAG 加直接文件修改

实现成本最低，但无法可靠表达符号、调用关系、字节范围和结构变化；容易发生路径越权、错误替换和重复修复。弃用。

### 方案 B：AST/CST + 结构化 Patch + 确定性 Workflow

以 Tree-sitter/CST 为首选解析边界，使用语义块和调用关系构造受控上下文，LLM 只生成结构化 Patch，由 Python Policy 校验和路由。Sandbox 未配置时 fail closed。该方案与现有分层、事实源、治理和单一 Workflow 原则一致，作为推荐方案。

### 方案 C：独立多 Agent 编排平台

可以覆盖更多角色，但会引入 Supervisor、开放式循环、额外状态源和更复杂的权限边界，与现有架构不可变约束冲突。弃用。

## 4. 模块结构

新增模块使用独立命名空间，避免污染发票 Domain：

```text
src/invoice_intelligence/code_harness/
  __init__.py
  domain/
    __init__.py
    code_model.py
    patch_model.py
    execution.py
    watchdog.py
    postmortem.py
  application/
    __init__.py
    ports/
      parser.py
      code_model.py
      code_embedding.py
      code_reranker.py
      code_index.py
      code_retrieval.py
      patch_validator.py
      sandbox.py
      postmortem.py
      harness_observability.py
    services/
      repository_inspection.py
      code_context.py
      patch_validation.py
      self_healing.py
      postmortem_admission.py
  infrastructure/
    parsing/
    indexing/
    patching/
    sandbox/
    persistence/
    observability/
  workflow/
    nodes.py
    policy.py
    state.py
    runner.py
```

实际文件可在实施时合并过小模块，但依赖方向必须保持：

```text
domain
  <- application ports/services
  <- infrastructure adapters
  <- workflow
  <- API/worker composition root
```

Domain 不依赖 FastAPI、SQLAlchemy、Milvus、Tree-sitter、LangGraph 或模型 SDK。API 只调用 Application Service；Workflow 只调用 Application Port 或 Service；具体 Adapter 只在 Infrastructure 和 `bootstrap.py` 组装。

## 5. Domain 契约

所有 Task、Patch、Validation、Execution 和 Postmortem 使用统一 `ExecutionVersionBinding`：
`repository_revision`、`snapshot_checksum`、`schema_version`、`parser_version`、
`grammar_version`、`redaction_version`、`retrieval_index_version`、`model_version`、
`prompt_version`、`patch_policy_version`、`sandbox_runtime_version`、`sandbox_policy_version`
和 `watchdog_policy_version`。解析、检索、验证和执行必须按契约校验这些字段；版本缺失或不匹配即硬失败。

### 5.1 Code Artifact

`CodeArtifact` 表示一次受控读取的文件版本，至少包含：

- `artifact_id`
- `tenant_id`
- `repository_id`
- `revision`
- `file_path`
- `language`
- `content_sha256`
- `byte_length`
- `schema_version`

文件内容只在解析或验证所需的短生命周期内使用。State、日志和检索响应不得保存完整仓库或完整文件。`tenant_id` 是可信上下文派生的技术字段，不属于 `InvoiceExtraction`。

### 5.2 Code Symbol 和 Call Edge

`CodeSymbol` 保存：

- `symbol_id`
- `tenant_id`
- `repository_id`
- `snapshot_id`
- `revision`
- `file_path`
- `language`
- `symbol`
- `parent_symbol`
- `kind`
- `scope`
- `byte_range`
- `line_range`
- `signature_summary`
- `parse_status`
- `schema_version`

`CallEdge` 保存 caller、callee、关系类型、来源范围、解析置信状态和版本引用。无法解析的动态调用必须标记 `unresolved`，不得猜测为确定调用关系。
caller、callee 和来源范围必须绑定同一 `repository_id + snapshot_id + revision`；跨快照或跨 revision 的边不得写入同一索引版本。

### 5.3 CAST Chunk

CAST Chunk 以文件、模块、类、方法、函数和必要的语义上下文为边界，不使用单纯滑动窗口。至少保存：

- `chunk_id`
- `tenant_id`
- `repository_id`
- `snapshot_id`
- `file_path`
- `language`
- `symbol`
- `parent_symbol`
- `scope`
- `byte_range`
- `line_range`
- `content_sha256`
- `repository_revision`
- `revision`
- `schema_version`

索引中的代码内容必须按项目配置脱敏；原始业务事实仍由 PostgreSQL 和受控文件存储边界管理。

### 5.4 Patch Proposal

Patch Proposal 是唯一允许进入验证阶段的生成结果，至少包含：

- `patch_id`
- `tenant_id`
- `task_id`
- `base_revision`
- `target_files`
- `operations`
- `affected_symbols`
- `rationale_summary`
- `validation_requirements`
- `generator_version`
- `prompt_version`
- `source_trace_id`
- `repository_id`
- `snapshot_id`
- `source_revision`
- `patch_base_revision`
- `execution_revision`

`operations` 只能表达以下结构化操作。所有 byte offset 均表示 UTF-8 编码后的半开区间 `[byte_start, byte_end)`，换行统一按文件既有策略读取，写入时规范化为声明的 `LF` 或 `CRLF`，不得按字符索引解释。

- `replace_span`：文件路径、文件 base checksum、原始 `byte_start`/`byte_end`、原始内容 SHA-256、目标文本；
- `insert_before` 或 `insert_after`：文件路径、文件 base checksum、稳定符号定位、锚点 AST 指纹、目标文本；
- `delete_span`：文件路径、文件 base checksum、范围、原始内容 SHA-256；
- `create_file`：允许的源代码路径、语言、完整 UTF-8 内容载荷和内容 SHA-256；
- `delete_file`：文件路径、完整文件 SHA-256，并要求显式 Policy 许可。

每个操作必须包含 UTF-8 编码、换行策略、目标语言、符号定位或字节范围和前置指纹。相同文件的操作按 descending `byte_start` 应用；范围重叠、同一锚点重复或跨文件依赖未声明时拒绝 Patch。默认禁止创建/删除文件、跨文件重命名、二进制文件和工作区外路径。应用冲突、前置内容不匹配或 AST 指纹不匹配时拒绝 Patch。模型不能提供任意命令、任意路径或执行权限。

### 5.5 Execution 和 Postmortem

执行记录保存状态、资源预算、脱敏错误码、退出原因、attempt、lease 和 Trace 引用，不保存远程响应体或完整生成内容。

Postmortem 至少保存：

- `error_signature`
- `root_cause`
- `solution_pattern`
- `affected_language`
- `affected_symbol_kind`
- `patch_shape`
- `source_trace_id`
- `version_scope`
- `repository_id`
- `snapshot_id`
- `source_revision`
- `patch_base_revision`
- `execution_revision`
- `source_task_id`
- `source_patch_id`
- `source_execution_id`
- `fingerprint`
- `occurrence_count`
- `admission_status`
- `is_reviewed`
- `is_valid`

准入状态为 `pending`、`approved`、`quarantined`、`rejected`、`suspended`、`invalidated`。只有 `approved + is_reviewed=true + is_valid=true` 才允许进入长期检索。

相同 `tenant_id + fingerprint + version_scope` 的 Postmortem 合并为一个事实聚合；`source_task_id`、`source_patch_id`、`source_execution_id` 通过不可变来源表保留。`occurrence_count` 只在新来源事件插入成功时增加，来源事件使用稳定 `event_id` 去重。

## 6. 解析与索引

### 6.1 Parser Port

Application 定义 `CodeParser` Port，返回容错解析结果、符号、调用边、诊断和原始位置。Tree-sitter Adapter 位于 Infrastructure。解析不完整代码时允许返回 `parse_status=partial` 和有限节点，但必须保留诊断；未配置语言 Grammar 时返回明确的 `parser_not_configured`，不退化为不安全的文本 Patch。Port 输入和输出必须携带 `repository_id + snapshot_id + revision + parser_version + grammar_version + schema_version`，能力检查不得由 Adapter 静默降级。

### 6.2 受控 Repository Snapshot

Repository 不是任意宿主机目录。Application 只接受预先登记的 `RepositorySource`，包含可信 `tenant_id`、允许的绝对根目录或只读对象存储引用、来源类型、来源版本和访问 Policy。登记时拒绝符号链接逃逸、子模块边界外引用、设备文件、软链接目录和工作区外路径；忽略凭证、`.env`、密钥、二进制和配置黑名单文件。

每次任务创建不可变 `RepositorySnapshot`，保存 `snapshot_id`、`tenant_id`、`repository_id`、`revision`、文件清单 SHA-256、总字节数、来源 Trace 和 `snapshot_checksum`。Snapshot 的内容载体必须是只读对象包或受控临时副本，目录遍历、复制和使用前后都要复核真实路径、符号链接、文件类型、文件大小和 checksum，防止 TOCTOU。Git 来源必须保存 commit/tree hash 及子模块 commit；非 Git 来源使用固定排序的 manifest（路径、大小、内容 checksum、模式）计算 checksum，封存期间发现变化时按配置有限重试，仍变化则标记 `snapshot_capture_conflict` 并拒绝任务。解析、Patch 校验和 Sandbox 输入必须绑定同一个 `snapshot_id + revision + snapshot_checksum`，Sandbox 实际输入包在执行前再次校验。Snapshot 不能在任务运行中随意刷新；发现源文件变化时返回 `base_revision_conflict`。

### 6.3 索引事实与派生边界

PostgreSQL 保存：

- Repository、revision、索引版本和构建状态；
- tenant-scoped RepositorySource、RepositorySnapshot 和文件清单；
- 代码任务、Patch、验证和执行事实；
- Postmortem、准入、审计和来源事件；
- 派生投影登记、checksum、attempt、lease 和错误码。

Milvus 只保存经过脱敏的 CAST Chunk、符号摘要、调用关系特征和向量，是可删除、可重建的派生索引。代码索引使用独立的 Collection 前缀、Alias、Index Version 和 Partition Key，与审核案例 Collection、字段语义 Collection 物理隔离；代码索引不得复用业务记忆 Collection。代码查询必须先由 PostgreSQL 确认租户、Repository、revision、语言、路径和准入范围，再访问派生索引。
代码索引还必须使用独立 URI/database、凭据和可选部署故障域；若配置误指向审核案例或字段语义 Collection、Alias 或 database，Adapter 必须 fail closed 并返回 `code_index_misconfigured`。

代码 Collection 的最小 Scalar Schema 为 `tenant_id`、`repository_id`、`snapshot_id`、`revision`、`index_version`、`file_path`、`language`、`symbol`、`parent_symbol`、`scope`、`byte_start`、`byte_end`、`line_start`、`line_end`、`redaction_version` 和 `content_sha256`；向量模型、维度和 Reranker 必须通过独立的代码索引配置版本绑定，缺失 Provider 时只提供精确/结构化查询或返回受限能力。

索引构建采用版本化投影，不与 PostgreSQL 使用分布式事务。PostgreSQL 事实提交成功后登记投影任务；投影失败不回滚任务、Patch 或 Postmortem 事实。

### 6.3 受控检索

代码上下文 Service 暴露：

- 精确文件查询；
- 精确符号查询；
- caller/callee 查询；
- 结构化关键词查询；
- 受控 Dense/Sparse 查询；
- 按语言、revision、scope 和文件范围过滤。

返回内容必须包含来源、版本、字节/行范围和脱敏状态。代码精确查询必须通过 PostgreSQL 中的结构化符号/调用事实或只读 Snapshot Adapter 提供；Milvus 不可用、索引过期或版本不匹配时只能返回 `code_index_unavailable`/`code_index_stale` 的受限结果，不得把空结果解释为“没有匹配”，也不得继续生成 Patch。

### 6.4 Port 能力契约

Application 必须定义以下固定 Port，所有请求和结果都携带任务的 `tenant_id`、`repository_id`、`snapshot_id`、`revision` 及对应 provider/config/schema/index 版本：

- `CodeModel`：只接受受控上下文和结构化任务输入，返回经 Schema 校验的 `PatchProposal`；必须声明超时、重试、限流、错误分类和 `model_version/prompt_version/schema_version`。
- `CodeEmbedding`：只处理脱敏 CAST Chunk 或受控查询文本，返回维度和模型版本一致的向量；版本或维度不匹配时拒绝写入。
- `CodeReranker`：只接受已通过 Scope Filter 的候选引用，返回稳定排序结果和 `reranker_version`，不得扩大候选范围。
- `CodeIndex`：提供版本化投影登记、精确/结构化查询和受控向量查询；缺失或配置错误时返回稳定错误码，不以空结果伪造索引命中。
- `SandboxExecutor`：输入只能是已校验 Snapshot 包、已校验 Patch、命令白名单和资源预算，返回结构化执行观察；不得接受模型生成的任意命令。
- `CodeParser`：返回带 byte/line 范围的诊断、符号、调用边和 CAST Chunk；Parser 输入内容必须来源于 Snapshot，不接受任意宿主机路径。

所有 Port 必须有能力探测结果，至少区分 `available`、`not_configured`、`misconfigured`、`temporarily_unavailable` 和 `unsupported`。重试只适用于临时错误；版本不匹配、权限、安全和契约错误不得重试。

## 7. 固定自愈 Workflow

Harness 使用独立的确定性 Application 状态机，不增加现有唯一 LangGraph Workflow 的节点，也不创建新的 LangGraph/Agent Graph：

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

这些是 `HarnessNode` 的稳定阶段名称，由 `HarnessRunner` 按固定顺序调用；它们不是新的 LangGraph 节点。已有发票识别 Workflow 仍只处理发票提取、审核中断和恢复。

### 节点职责

- `prepare_task`：校验可信租户/Actor、任务、Repository、不可变 Snapshot、revision、预算和幂等键。
- `inspect_repository`：登记 Snapshot 文件清单、语言和 revision，生成有限结构摘要。
- `retrieve_code_context`：执行 Scope Filter 后获取有限 CAST 上下文。
- `generate_patch`：调用固定 Model Port，校验结构化 Patch；模型不能路由 Workflow。
- `validate_patch`：执行路径、范围、语法、AST/CST、base revision 和安全策略验证。
- `execute_in_sandbox`：调用 Sandbox Port；未配置强隔离运行时则进入 `sandbox_unavailable` 终态，不执行代码。
- `evaluate_result`：读取结构化执行观察，判断成功、临时失败、硬失败或需要修复。
- `repair_or_finish`：由 Python Policy 决定有限重试、人工审核、隔离或结束。

`HarnessState` 只保存任务 ID、tenant、Snapshot/revision 引用、版本、有限上下文引用、错误码、预算计数、attempt 和有界历史。不得保存图片、Base64、完整 Prompt、完整向量召回、完整仓库或 Chain-of-Thought。

### 7.1 状态机与路由

任务状态为：

```text
received
  -> inspecting
  -> context_ready
  -> patch_proposed
  -> patch_validated
  -> execution_pending
  -> succeeded
  -> repair_pending
  -> human_review
  -> quarantined
  -> failed
  -> cancelled
```

固定路由规则：

| 观察结果 | 路由 | 是否重试 |
| --- | --- | --- |
| Snapshot/租户/版本不匹配 | `failed` | 否 |
| Parser 未配置 | `quarantined`，错误码 `parser_not_configured` | 否 |
| 无合法 Patch | `human_review` 或 `failed`，由 Policy 按风险决定 | 否 |
| Patch 安全/路径/资源硬失败 | `quarantined` | 否 |
| Sandbox 未配置 | `quarantined`，错误码 `sandbox_not_configured` | 否 |
| 临时网络超时、429、5xx | `repair_pending` 或同阶段重试 | 按预算有限重试 |
| 执行成功且验证通过 | `succeeded` | 否 |
| 执行失败且 AST/错误有实质改善 | `repair_pending` | 按预算有限重试 |
| exact/jitter/stall/budget watchdog 命中 | `human_review` 或 `quarantined` | 否 |

`repair_pending` 必须携带 `next_stage`、`retry_at`、`reason_code` 和当前 attempt；默认 `next_stage=retrieve_code_context`，仅临时 Provider 错误可回到产生错误的阶段。创建新 Attempt 只能在旧 Attempt 以不可变终态提交后进行，并通过任务状态 CAS 和 lease fencing 防止并发执行。`sandbox_unavailable` 与 `sandbox_not_configured` 均固定进入 `quarantined`。只有执行观察中的 `error_signature` 改变且 AST 节点集合、受影响符号或结构化诊断至少一项发生变化，才算“实质改善”；否则触发 `stall_loop`。

`human_review` 只登记人工审查任务和状态，不让人工通过请求直接执行未经重新验证的 Patch；恢复时必须重载 Snapshot、Patch 和 revision，并重新执行确定性校验。

## 8. Patch 验证策略

验证顺序固定为：

1. 规范化路径并确认路径位于允许的 Repository 根目录内。
2. 校验 base revision，防止覆盖并发变更。
3. 检查目标文件数量、字节数、修改行数和操作数预算。
4. 拦截凭证、密钥、CI 权限、部署、安全配置和工作区外路径。
5. 应用到隔离临时副本，检查语法和 AST/CST 结构。
6. 检查受影响符号、调用边和文件范围是否与 Proposal 一致。
7. 生成稳定 Patch fingerprint，检测重复 Patch。
8. 输出不可覆盖的硬失败或可继续的验证结果。

不完整源码可以进入容错解析，但不能跳过路径、安全和资源校验。删除整个项目、路径逃逸、权限提升、访问宿主凭证、网络策略命中和资源超限立即终止并进入人工审核或 `quarantined`。

## 9. Watchdog

每个工具调用、Patch 和执行失败生成稳定 `error_signature`，输入只使用规范化错误码、工具名、参数摘要、目标 revision、Patch fingerprint 和受影响范围，不使用完整敏感内容。

Watchdog 检测：

- `exact_repeat`：相同工具、参数摘要、Patch 和错误重复；
- `jitter_loop`：Patch 有表面变化但错误签名和 AST 结果无实质改善；
- `stall_loop`：AST 结构、验证结果和执行观察连续无变化；
- `budget_exhausted`：循环、Patch、Token、执行时长或修改行数超限；
- `hard_error`：安全、路径、权限、版本冲突和不可恢复契约错误；
- `timeout` 和 `resource_exhausted`。

临时网络超时、429、5xx 只能按配置有限重试；相同错误不得无限重试。Watchdog 结果由确定性 Policy 路由为继续、人工审核、隔离或结束，模型不能覆盖。

## 10. Sandbox 边界

Application 定义 `SandboxExecutor` Port，输入为已验证 Patch、隔离 Repository Snapshot、命令白名单和资源预算，输出为结构化执行观察。

当前没有真实 MicroVM 时，仅提供：

- `FailClosedSandboxExecutor`：返回 `sandbox_not_configured`，不执行代码；
- 可选 `TestOnlySandboxExecutor`：必须显式启用、明确标注非生产，并且只接受无不可信输入的隔离验证。

禁止在主进程、宿主机任意子进程或工作区直接执行模型生成代码。生产 Adapter 必须实现网络默认拒绝、凭证不可见、工作区外不可写、CPU/内存/磁盘/进程/时长限制和执行身份隔离。普通 Docker 或本地子进程不能标记为 MicroVM 等价安全边界。

## 11. 可观测性与审计

复用现有 Observability Port，新增低基数阶段：

- `code_harness.prepare`
- `code_harness.inspect`
- `code_harness.retrieve`
- `code_harness.generate`
- `code_harness.validate_patch`
- `code_harness.sandbox`
- `code_harness.watchdog`
- `code_harness.postmortem`

事件只记录任务/资源技术 ID、Trace、模型/Prompt/Schema/Index 版本、耗时、Token 统计、状态、错误码和资源预算。默认不记录完整代码、完整 Prompt、完整 Completion、密钥、向量、远程响应体或 Chain-of-Thought。若未来需要保留 Prompt/Completion，必须经过脱敏、显式配置和审计门禁。

所有写入必须继承 `TrustedTenantContext`。客户端不能通过 Body、Query 或普通 Header 指定 `tenant_id`、Actor 或 Trace。跨租户资源统一表现为 404。

## 11.1 数据表、事务和入口

建议新增独立 migration 和前缀明确的表：

- `code_harness_repositories`
- `code_harness_snapshots`
- `code_harness_snapshot_files`
- `code_harness_tasks`
- `code_harness_task_attempts`
- `code_harness_patch_proposals`
- `code_harness_patch_operations`
- `code_harness_patch_validations`
- `code_harness_executions`
- `code_harness_watchdog_events`
- `code_harness_postmortems`
- `code_harness_postmortem_sources`
- `code_harness_postmortem_decisions`
- `code_harness_index_versions`
- `code_harness_index_projections`
- `code_harness_audits`

表名是设计边界，实施时可按现有 ORM 约定调整，但不得将 Harness 事实混入发票事实表。

事实事务保存任务/attempt/Patch/验证/执行观察和审计；记忆准入事务保存 Postmortem Decision；派生投影事务只登记 Projection 状态。三者不使用分布式事务。幂等键唯一作用域必须至少包含 `tenant_id + operation_type + client_scope`，任务和 Attempt 的状态迁移使用 `WHERE id=? AND expected_revision=? AND lease_token=?` 的 CAS/fencing 条件；终态只允许一次写入，重复请求返回原事实。所有写入使用 tenant-scoped stable ID、Idempotency-Key hash、revision/CAS 和来源 Trace；服务重放返回原事实和原审计。状态变更、claim、lease、fencing、终态和拒绝原因都必须写入审计。

初始 Application 入口为 `CodeHarnessService.create_task/get_task/resume_task`；若需要异步执行，独立 Worker 消费 PostgreSQL `code_harness_tasks` 中的可领取状态，使用 lease、attempt、next_attempt_at 和 fencing。API Router 不访问 ORM、Parser、Milvus、Sandbox 或 Workflow 内部。

## 12. Postmortem 治理

修复成功后只生成 `pending` Postmortem 候选。Application Service 先保存不可变来源事件和版本，再由确定性质量 Policy 与授权 Reviewer 决定准入。未审核或非 `approved` 案例不可进入长期检索、Few-shot、评估集或训练集。Decision 保存 `decision_id`、前后状态、reviewer/actor、reason_code、expected_revision、target_revision、policy_version、trace_id` 和时间。

Postmortem 索引投影遵循：

```text
PostgreSQL fact
  -> admission decision
  -> approved projection registration
  -> redacted Milvus projection
  -> explicit version activation
```

禁用、Schema 失效、租户删除和保留策略只使派生索引失效或删除，不删除原始 Postmortem、Patch、执行和审计事实。

## 13. 错误处理与恢复

- 业务任务、Patch、执行和 Postmortem 按稳定 ID 幂等。
- `run_id`/`task_id` write-once；不同内容不能覆盖已完成事实。
- Worker 使用 PostgreSQL 原子 claim、lease、attempt、next attempt 和 fencing。
- 进程恢复不得重复生成 Patch、重复执行、重复增加 Postmortem occurrence 或重复登记投影。
- 版本冲突、路径越权、Schema/租户不匹配和确定性硬失败不重试。
- 网络超时、429、5xx 和暂时不可用按有限指数退避重试。
- 重试耗尽进入 `quarantined` 或等价可审计状态。
- 记忆准入或投影失败不得把已经成功的代码任务伪装为执行失败；二者通过独立状态表达。

## 14. 实施阶段

### Phase 1：Domain、Application Port 和版本化契约

新增 Harness Domain 模型、Port、错误码、预算、Repository Snapshot、租户边界和版本契约；定义 Model Port、Parser Port、Embedding/Reranker Port、Index Port 和 Sandbox Port；不接入真实外部服务。

### Phase 2：解析、CAST 和代码上下文

实现 Parser Adapter 边界、Grammar 注册表、容错解析结果、语义分块、符号/调用图查询和索引版本登记。首期语言由配置显式列出；每个 Grammar 绑定 parser package/version、language、grammar_version 和 `schema_version`。当前 `pyproject.toml` 未配置 Tree-sitter 依赖，实施时必须先增加受控依赖并记录版本；未配置 Grammar 时 fail closed。

### Phase 3：单一确定性 Workflow

新增独立 Harness Application 状态机、`HarnessState`、Runner 和 Python Policy；不得新增 LangGraph、Checkpointer 或 Agent Graph；复用现有 Trace、可信租户和幂等边界。

### Phase 4：Patch、确定性验证和 Watchdog

实现结构化 Patch、路径/AST/资源校验、fingerprint、错误签名、循环检测和有限路由。

### Phase 5：SandboxExecutor fail-closed 边界

实现无运行时时的拒绝 Adapter、测试专用边界、执行状态机和版本化资源契约；`sandbox_not_configured` 固定进入 `quarantined`；不声称已具备 MicroVM。

### Phase 6：Trace、审计、Postmortem 和准入

接入已有 Observability Port、PostgreSQL 事实模型、审计、准入状态和可重建派生登记。

### Phase 7：文档和初始化模板

更新 `README.md`、`docs/architecture.md`、`docs/project-status.md`，并增加新项目可复用的 Harness 初始化规则。文档只声明实际完成能力。

## 15. 能力与配置矩阵

| 能力 | 必要配置 | 缺失时行为 | 允许降级 |
| --- | --- | --- | --- |
| Tree-sitter Parser | language、grammar package、grammar version | `parser_not_configured`，任务隔离 | 仅已批准的其他 AST/CST Adapter |
| Code Model Provider | Model Port、endpoint、凭据、schema/prompt version | `model_provider_not_configured`，任务隔离 | 不生成 Patch |
| Embedding/Reranker | 代码索引专属模型、维度、版本 | `index_provider_not_configured` | 精确文件/符号/结构化查询 |
| Code Milvus Index | 独立 URI/database、凭据、Collection prefix、Alias、Index Version 和独立故障域 | `code_index_unavailable`；误指向业务 Collection 时 `code_index_misconfigured` | PostgreSQL 精确/结构化查询，不长期双查 |
| MicroVM Sandbox | Sandbox Adapter、运行时版本、资源 Policy | `sandbox_not_configured`，`quarantined` | 不执行生成代码 |
| PostgreSQL | 业务 DSN、migration、可信租户上下文 | 服务不可用或拒绝任务 | 不使用内存事实替代 |

任何 Stub、Test-only Adapter 或配置解析成功都不能改变上述终态，也不能作为生产验收证据。

## 16. 验收与剩余风险

本设计阶段的事实验证为只读文档、源码、配置、Migration、API、Worker 和工作区状态核对；未启动服务、未连接真实 Provider/Milvus/PostgreSQL、未执行迁移，且未自动生成或运行测试。

实施完成前不得宣称：

- Tree-sitter 所有目标语言均已生产可用；
- Sandbox 已达到 MicroVM 安全等级；
- 代码已自动修改、合并或部署；
- Postmortem 已自动进入长期记忆；
- Milvus 或真实远程模型已完成生产验收。

主要风险为：语言 Grammar 覆盖不足、动态调用无法静态解析、结构化 Patch 与现有格式化工具不兼容、真实 MicroVM 接入复杂、代码脱敏可能损失检索质量，以及派生索引与 PostgreSQL 版本状态不一致。所有风险必须通过显式状态、版本、审计和 fail-closed 行为暴露。
