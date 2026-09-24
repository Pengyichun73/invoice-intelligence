# Codex 后续生产验收执行提示词

请在 `G:\work\ai` 继续完成 Invoice Intelligence 的生产可验收闭环。先读取
`AGENTS.md`、`README.md`、`INVOICE_INTELLIGENCE_SOLUTION.md`、`docs/architecture.md`、
`docs/invoice-schema.md`、`docs/project-status.md`、`pyproject.toml`，再读取本次涉及的
源码、Application Port、Repository、migration、Compose 和 API 契约。以当前代码和
`docs/project-status.md` 为事实基线，不重复已完成工作。

## 不可变约束

- 不修改固定 19 字段 `InvoiceExtraction`。
- 保留单一确定性 Workflow、现有节点顺序和路由语义。
- PostgreSQL 是审核、评估、训练注册和投影状态的唯一事实源；Milvus 是可重建派生索引。
- 不新增数据库、消息队列、Agent Graph 或外部参考项目代码。
- API Router 只能调用 Application Service；依赖统一由 `bootstrap.py` 组装。
- tenant、Reviewer、Trace 和版本必须来自可信后端上下文；跨租户资源统一表现为 404。
- 使用 `apply_patch` 修改；不提交 Git commit，不执行破坏性清理，不连接生产服务。
- 所有说明、注释、验收记录使用简体中文；不得记录完整发票值、图片、Base64、向量、
  完整 Prompt、凭据或远程响应体。

## 执行顺序

### 1. 真实离线评估隔离环境

1. 提供一个独立、只读、隔离的 Suite Runner，覆盖 `case_rag` 和
   `trusted_memory_field_binding` 的全部既定变体。
2. 使用与业务不同的隔离账号和证据源；只允许按 tenant、dataset、document_id、
   template fingerprint 和 evidence reference 读取。
3. 通过 HTTPS Adapter 执行真实 Runner；验证请求和 isolation manifest SHA-256 回显、
   版本绑定、超时、有限重试、响应大小限制及 Pydantic/Domain 校验。
4. 证明训练文档与评估文档、document_id 与 template fingerprint 均互斥；缺失、重复、
   过期或跨租户证据必须 fail closed。
5. 验证 PostgreSQL 中 `evaluation_datasets`、`evaluation_runs`、
   `evaluation_report_artifacts` 的不可变绑定、JSON/Markdown checksum 和报告引用。
6. 证明 `SnapshotCase`、`deterministic_stub`、`diagnostic_only` 不能创建 Promotion Evidence，
   不能写生产识别、审核或索引事实。

### 2. 可信记忆和索引

1. 使用两个独立 Worker 连接同一隔离 PostgreSQL，验证 claim、lease renewal、过期重领、
   worker fencing、迟到写入拒绝、有限重试和幂等。
2. 验证冲突关闭后的重评估请求只重新排队明确由该冲突隔离的 memory admission；
   确定性硬失败、field alias 和其他人工门禁必须保持人工复核。
3. 从 PostgreSQL 合格源集合生成 reviewed-example 与 field-semantic 投影清单，逐项校验
   tenant、catalog/index version、ID、checksum、数量、重复项和过期项。
4. 读取真实 Milvus Collection 的 metadata、实体 ID 和 checksum；任何缺失、额外、错租户、
   错版本或 checksum 不一致都必须阻止 Alias 切换并保留旧 Alias。
5. 记录脱敏的 claim/lease、集合完整性和 Alias 保留证据。

### 3. 身份、文件、部署和恢复

1. 在隔离 Compose project 中执行最新 migration，记录 `alembic current` 和预期 head。
2. 验证 password file 被 API、migration、Evaluation/Scheduler/Memory/Index/Training/
   Storage Worker 解析为同一业务 DSN；不得把密码导出为环境变量。
3. 使用最小权限账号验证 API、Worker、migration 角色边界；验证 OIDC tenant/reviewer
   可信上下文、跨租户 404 和 TLS。
4. 验证 S3-compatible private bucket、HEAD/checksum、短时 URL、对象迁移、生命周期、
   checksum mismatch、旧引用保留和失败重试。
5. 按 `docs/backup-restore-drill.md` 执行 PostgreSQL、对象存储和独立 Checkpointer 恢复；
   从 PostgreSQL 重建 Milvus，验证失败时旧 Alias 和旧数据引用仍保留。
6. 将低基数 metrics 接入隔离告警接收端，注入 Worker、Provider、lease、projection、
   storage 和 audit failure，保存不含敏感内容的告警证据。

### 4. 晋升、训练、财务、交易和前端

1. 证明 Promotion 只接受真实完成的 Suite Evaluation Run、有效模型产物、报告产物和
   完整版本证据；诊断 Stub、缺报告、失配 checksum、失租约 Run 必须拒绝。
2. 验证人工审批、revision/CAS、幂等重放和注册状态回滚；将注册切换描述为事实注册，
   不描述为流量部署或模型权重发布。
3. 验证单一 Training Worker：未配置 Stub 永久隔离；429/5xx 有限重试；400、无效响应
   和凭据错误永久失败；日志不得包含远程响应体。
4. 验证历史不可信 Transaction Candidate 只能通过授权 Reviewer 审计升级为
   `escalated`，不得直接确认、驳回或伪装成可信派生候选；保存原因码和审计事实。
5. 检查前端完全按后端事实展示 evaluation type、integrity、registration status、
   unavailable reason；不得把 `/ready`、`indexed`、Stub 报告或配置解析当成生产证明。

## 代码与文档要求

- 只实现当前事实缺口，不做无关重构。
- 每个实现模块同步更新 `README.md`、`docs/architecture.md` 和
  `docs/project-status.md`，明确“已实现 / 隔离环境已验收 / 生产已启用”。
- 新增或修改 migration 必须有 SQLite 等价迁移验证；涉及数据库、租约、租户隔离、
  晋升门禁或报告完整性必须运行定向测试。
- Docker、隔离账号、secret 或真实 Milvus 不可用时，继续完成代码和 Mock 验证，并明确
  未执行的验收步骤、缺失环境和剩余风险；禁止用 Compose 配置解析、`/ready` 或
  `indexed` 状态替代真实验收。

## 最终交付格式

按模块列出：

1. 修改文件和关键入口；
2. 数据流和依赖方向；
3. 定向测试、隔离集成测试及其结果；
4. 已实现、隔离环境已验收、生产已启用三类状态；
5. 未完成项、阻塞原因、所需环境和剩余风险。
