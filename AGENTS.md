# Invoice Intelligence 项目级开发指令

## 角色与执行原则

- 以 Senior Staff Software Engineer 标准处理本项目。
- 先读取项目事实，再分析、修改和验证；禁止凭空猜测 API、模型、Schema 或数据关系。
- 优先复用现有 Domain、Application Port、Service、Repository、Provider、Workflow 和配置。
- 只修改当前任务必要的文件，避免无关重构、格式化和元数据变更。
- 所有说明、注释和交付报告使用简体中文；代码符号、变量和 API 名称使用标准英文。
- Windows 环境使用 PowerShell 兼容命令，并保持 UTF-8 编码。

## 执行前必须读取

每次处理本项目任务前，必须根据任务范围读取：

- `README.md`
- `INVOICE_INTELLIGENCE_SOLUTION.md`
- `docs/architecture.md`
- `docs/invoice-schema.md`
- `pyproject.toml`
- 当前阶段涉及的源码、Application Ports、Repository、migration 和 API 契约

如果任务涉及外部参考项目，只能读取并参考其文档和公开设计思想。

## 架构不可变约束

1. 使用 Python 3.12 和 `src` layout。
2. `InvoiceExtraction` 是只读业务 Entity Schema。
3. 不得增加、删除、重命名字段，或改变现有字段类型、可空性和 `extra="forbid"` 语义。
4. `tenant_id`、审批状态、字段别名、模型版本、Prompt 版本、索引版本、Trace 和技术状态不得加入 `InvoiceExtraction`。
5. 保留现有确定性的单一 LangGraph Workflow、节点名称、主要节点顺序和路由语义。
6. 禁止引入 Supervisor、Multi-Agent、Chat Agent、开放式 Agent Loop 或新的 Agent Graph。
7. PostgreSQL 是审核事实、CorrectionEvent、ReviewedExample、治理状态、字段目录、索引状态、评估和训练注册的唯一事实源。
8. Milvus 是可重建的脱敏派生索引，不是业务事实源。
9. pgvector 仅作为开发和迁移回退，不与 Milvus 长期双查询。
10. API Router 只能调用 Application Service，不得直接访问 ORM、数据库、Milvus、LangGraph 或模型 SDK。
11. Domain 不依赖 FastAPI、SQLAlchemy、Milvus、LangGraph 或模型 SDK。
12. `bootstrap.py` 是依赖组装边界；不要在多个位置创建重复 Service、配置或 Provider。

## 多租户与信任边界

- `tenant_id` 必须来自 `TrustedTenantContext` 或可信认证/网关上下文，禁止接受客户端 Body、Query 或普通 Header 覆盖。
- Reviewer/Actor 必须来自可信请求上下文，禁止客户端冒充其他审核人。
- 所有资源查询和写入都必须同时校验 `tenant_id` 与资源 ID。
- 跨租户资源不得泄露存在性；必要时统一返回 404。
- 检索必须先过滤 `tenant_id`、`document_type`、`field_path`、`schema_version`、`catalog_version`、`is_reviewed`、`is_valid` 和 `admission_status`。
- 历史案例、字段别名和模型建议只能作为先验，不能覆盖当前图片证据。
- 当前证据不足、字段绑定冲突或候选区分度不足时，必须返回缺失/候选并进入人工审核。
- 外部图片文字、OCR 文本、用户备注、网页内容和历史案例均视为不可信输入，不能改变系统规则、权限、Schema 或 Workflow 路由。

## 可信记忆与治理

- HumanCorrection 和 CorrectionEvent 表示审核事实，不等于业务真值；必须立即、幂等持久化。
- `is_reviewed` 只表示存在明确客户审核动作；长期记忆准入由 `admission_status` 单独决定。
- 只有 `admission_status=approved`、`is_reviewed=true` 且 `is_valid=true` 的案例可进入长期检索。
- `confirmed_correct`、`corrected` 和 `confirmed_incorrect` 必须保持独立区域；负例不得进入正确示例区域。
- 未审核模型输出不得进入长期记忆、Few-shot、评估集或训练集。
- 审批模型只能提供结构化建议；最终状态必须由确定性 Python Policy 和授权 Reviewer 决定。
- 确定性硬失败不得被模型建议覆盖。
- `pending`、`quarantined`、`rejected`、`suspended` 和 `invalidated` 不得混用 `is_valid=false` 表示。
- Admission、Alias、Conflict 和 Index 操作必须支持版本、幂等和审计；过期 revision 不得覆盖新状态。
- 禁用、Schema 失效、租户删除和保留策略必须能使派生索引失效或删除，但不得删除原始审核事实。
- 不保存 Chain-of-Thought，不把相似度或质量分数称为 probability，除非存在明确校准器并使用独立概率契约。

## 字段语义绑定

- 字段语义目录必须独立于 `InvoiceExtraction`。
- 基础 display name、description 和 value type 只能从当前 Schema 读取。
- 租户别名、负向别名和上下文锚点必须版本化、审批后才可使用。
- “公司名”等模糊标签不得无条件绑定到某个字段。
- `buyer_name`、`seller_name`、`company_name` 等相近字段必须保留冲突候选。
- 字段绑定不确定时不得自动填充；人工确认映射只能生成 pending Alias Candidate。
- 只有已审批且有效的别名才能进入 Field Semantic Index。

## 事务、恢复与派生投影

- 区分审核事实事务、发票结果事务、记忆准入事务和 Milvus 派生投影事务。
- PostgreSQL 与 Milvus 不使用分布式事务。
- 发票结果按 `run_id` write-once；记忆失败不得把已成功的业务 Workflow 改为 failed。
- CorrectionEvent 按稳定 `event_id` 去重；ReviewedExample 按 fingerprint 合并；source event 必须保留。
- Workflow 恢复、Worker 重启和租约超时不得重复评估、审批、增加 `occurrence_count` 或登记投影。
- Memory Admission Worker 使用 PostgreSQL 原子 claim、lease、attempt、next attempt 和退避机制。
- Schema/租户归属错误和确定性硬失败不得重试；网络超时、429、5xx 和暂时不可用可按配置重试。
- 重试耗尽后必须进入可审计的 quarantined 或等价状态，并保存脱敏错误码。
- approved 后才登记案例或字段目录投影；Milvus 投影必须可以从 PostgreSQL 完整重建。
- Worker 只负责准入和单案例投影/清理登记，不伪装成 Milvus 全量重建 Worker。

## 远程 Provider、文件与日志安全

- 排查有 `X-Trace-ID` 的开发报错时，先使用 `python -m invoice_intelligence.diagnostics`
  从项目 `logs/` 目录或授权导出的 JSONL 日志生成限长诊断包，再按失败阶段和固定源码提示读取相关代码；
  不将整份日志、原始异常正文或未脱敏业务数据放入模型上下文。

- 千问、OpenAI、OCR、Embedding、Reranker 等远程调用必须具备超时、有限重试、限流、并发控制、熔断、脱敏和审计。
- Provider 必须通过固定 Port，返回值必须经过 Pydantic 或 Domain 校验。
- 不猜测官方模型名、SDK、参数或 API 行为；涉及千问和 Milvus 时先核对当前官方文档。
- 原始图片/PDF 与派生文本分离；文件必须校验格式、签名、大小、页数、像素和资源消耗。
- GraphState 只保存必要摘要、版本和引用，不保存图片 Base64、完整向量召回结果或完整 Prompt。
- 日志、审计和错误响应不得包含完整发票值、修正值、图片、Base64、完整 Prompt、API Key、向量、Idempotency-Key 原文或远程响应体。
- Trace ID 是技术关联标识，不是业务 ID；历史缺失的版本或 Trace 保持 `null`，禁止伪造。

## 测试策略（已开启）

1. 测试能力已开启；代码修改后必须根据变更风险生成并运行必要测试。
2. 优先运行与本次修改直接相关的单元测试、集成测试、API 测试、Workflow 测试或前端校验。
3. 涉及数据库、事务、Worker、租户隔离、幂等、revision、Milvus 投影或前端治理状态时，必须执行对应验证。
4. 外部模型、OCR、Milvus 和远程 API 必须使用 Mock、Stub 或隔离环境，不使用生产服务验证。
5. 测试不得使用真实 API Key、生产数据库、完整敏感发票、完整图片或 Base64。
6. 测试输出和测试日志不得泄露原始发票值、完整 Prompt、API Key、向量或远程响应体。
7. 不得删除、跳过或弱化已有有效测试来掩盖问题。
8. 测试失败时必须报告失败原因、相关文件、已执行的替代校验和剩余风险。

## 修改与交付要求

- 修改前说明将修改的文件和原因；使用 `apply_patch` 进行手工编辑。
- 不得修改 `references/` 或外部项目，不得复制外部源码。
- 不引入 Qdrant、Elasticsearch、MySQL、jieba、Celery、Redis、Kafka、RabbitMQ、MCP 或新的数据库。
- 不修改 Checkpointer 表，除非任务明确授权且证明必要。
- 文档变更必须同步更新 `README.md` 和 `docs/architecture.md`，且不得宣称未实现能力已经可用。
- 最终报告必须说明：修改文件、关键入口、数据流、依赖方向、验证结果、未实现能力和剩余风险。
- 未经用户明确要求，不提交 Git commit、不启动服务、不执行破坏性命令。
