# Invoice Intelligence 企业案例 RAG 与不规则图片查询方案

## 文件与对象存储方案

系统提供 Local development 与 S3-compatible production Adapter。原始文件、渲染图片、派生文本
使用三个独立 private Bucket；PostgreSQL 保存对象引用、租户、媒体类型、大小、checksum 和状态，
对象存储只保存可删除内容。下载使用 tenant-scoped 短时 URL，生命周期清理由独立 Worker 执行。
生产禁止本地文件 Adapter，MinIO 仅用于 development，生产 S3/OSS 继续通过相同 Port 接入。

## 独立财务处理边界

财务能力通过独立 `AccountingCandidate`、`TaxAssessment`、`PostingProposal` 和
`ExchangeRateSnapshot` 建模，不修改 `InvoiceExtraction`，也不增加 LangGraph 节点。授权调用方
显式引用同租户已完成 `run_id` 创建候选；缺少版本化税务、科目表或入账规则时状态保持
`pending_rule_review`。只有确定性 Policy 或授权 Reviewer 可批准科目映射，模型仅能提供结构化
advisory。入账通过 `AccountingPostingProvider`，汇率通过 `ExchangeRateProvider`；当前组合根仅提供
mock Adapter。外部失败追加 PostgreSQL attempt/audit，不回滚识别或审核事实。

本文是面向客户的系统方案和当前实现边界说明。它同时描述现有代码已经提供的能力、需要受控运营才能启用的能力，以及尚未实现的生产化能力。未实现部分不会被当作当前系统已具备的功能对外承诺。

## 1. 目标与范围

系统用于处理企业发票图片、扫描件和 PDF，并在多租户边界内完成：

- 将当前图片证据提取为固定的 `InvoiceExtraction` 结构；
- 对图片质量、字段类型、业务格式和字段冲突做确定性验证；
- 在低可信或冲突时进入人工审核，而不是使用历史值强行填充；
- 将明确审核过的案例转化为可治理、可回滚的长期记忆；
- 使用字段语义目录处理“公司名”“购方名称”等不稳定标签；
- 使用 Dense + Sparse Hybrid Retrieval 提供受控 Dynamic Few-shot；
- 通过离线评估观察记忆是否帮助识别，并为模型或索引晋升提供证据。

发票识别 Workflow 不直接负责交易入账、税务裁决、汇率换算、交易分类或最终风险结论。当前项目已
提供独立 Accounting Domain 基础实现和受限可用的 Transaction Analysis Domain：前者仅连接 mock 外部
系统，后者已校验可信完成提取来源并具备幂等/CAS，但仍使用开发 Mock 规则。开放式聊天和自动训练/自动生产晋升不在系统边界内。

## 2. 不可变架构原则

1. Python 3.12 和 `src` layout。
2. `InvoiceExtraction` 是只读业务 Entity Schema，字段、类型和两个发票分支不能被技术元数据污染。
3. `tenant_id`、模型版本、索引版本、Prompt 版本、审批状态和字段别名属于技术元数据。
4. 使用一个确定性的 LangGraph Workflow，所有路由由 Python 条件决定。
5. PostgreSQL 是审核事实、案例、目录、索引状态、评估和训练注册信息的唯一事实源。
6. Milvus 是可重建的脱敏派生索引，不是业务事实源。
7. 未明确人工审核的数据不能进入长期记忆、Few-shot、评估集或训练集。
8. 历史案例只提供先验和错误模式，当前图片证据拥有最高优先级。
9. 审批模型只能给出结构化建议，不能单独建立业务真值或批准记忆。
10. API Router 只调用 Application Service，不访问 ORM、Milvus、Graph 或模型 SDK。

## 3. 在线处理流程

```text
可信租户上下文
  -> 文件摄取与幂等校验
  -> 原始文件持久化
  -> 图片/PDF 确定性预处理
  -> prepare_document
  -> extract_invoice：当前图片事实
  -> retrieve_correction_context：严格 Scope Filter 后检索审核案例
       -> 无合格案例：直接验证
       -> 有合格案例：一次受控 Dynamic Few-shot 提取
  -> validate_extraction：确定性质量与业务规则
       -> accepted：持久化结果
       -> review_required：LangGraph interrupt，等待人工审核
       -> rejected/failed：安全结束并记录原因
  -> save_correction_memory：只幂等创建候选和审批任务
  -> END
```

### 3.1 文件摄取

系统接受 PNG、JPG、JPEG、WEBP 和 PDF。摄取阶段校验扩展名、MIME、文件签名、完整性、大小和 PDF 页数；PDF 按配置 DPI 渲染。原始文件进入 `FileStorage`，GraphState 只保存 `document_id`、`storage_uri`、`mime_type` 和 checksum，不保存 Base64 或原始 bytes。

### 3.2 图片质量处理

处理链读取 EXIF 方向，统一 RGB/PNG，限制最大边长和像素数；低动态范围图片可执行轻度对比度和锐化。每页记录宽高和清晰度分数，供验证和审计使用。原图不被修改，质量元数据不进入 `InvoiceExtraction`。

### 3.3 Vision Extraction

Vision Provider 返回严格 Pydantic/JSON Schema 校验后的 `InvoiceExtraction`。Schema 来自 `docs/invoice-schema.md` 对应的业务字段定义，增票和非增票为互斥分支，字段采用 required-but-nullable。视觉证据不足时可以返回 `null`、候选和异常，不能用历史值填补。

当前支持 OpenAI Provider 和千问兼容 Provider。Provider 只在 `infrastructure` 层实现，支持超时、有限指数退避、临时错误重试、限流、熔断、脱敏和请求审计，不保存 Chain-of-Thought。

### 3.4 确定性验证与人工审核

验证器不使用模型自报 confidence，组合以下信号：Schema 类型、字段存在性、缺失、页面可读性和分辨率、金额/日期/编号格式、明细关系、候选歧义、可选 OCR 一致性、字段绑定冲突、异常文本和租户归属。

- `accepted`：规则和阈值通过；
- `review_required`：图片不足、候选接近、字段冲突或业务风险需要人工判断；
- `rejected`：确定性硬失败，例如无法解析或资源归属不一致。

审核动作必须明确记录 `reviewer_id`、字段动作、原因、原始候选和可选字段映射。用户没有修改不等于 `confirmed_correct`；人工确认的 `null` 仍保持为 `null`。

## 4. 可信记忆生命周期

### 4.1 什么数据才是有用记忆

有用记忆必须同时具备：

- 明确人工审核动作和 reviewer；
- 绑定原始 document、run、field evidence、页面和位置引用；
- 能通过当前 Schema、字段类型和确定性业务规则；
- 具有清晰的租户、文档类型、字段路径和 Schema 版本 Scope；
- 不存在未解决的同证据冲突；
- 在不同文档、模板、Reviewer 和时间窗口中有可解释的支持来源；
- 内容经过脱敏，能够被检索而不会泄露原始敏感值；
- 规则、模型、Prompt 和评估版本可追溯、可回滚。

孤立的一次修改、没有证据的客户输入、模型自报高置信度、与当前图片冲突的历史多数值，都不能直接成为长期记忆。

### 4.2 案例标签

| 标签 | 产生条件 | 作用 |
|---|---|---|
| `confirmed_correct` | 模型候选被人工明确确认正确 | 正确场景示例 |
| `corrected` | 模型值被人工修改 | 同时保存 `model_value`、`reviewed_value`、`correction_reason` |
| `confirmed_incorrect` | 模型候选被人工明确否定 | Hard Negative，禁止进入正确示例区域 |

原始 `HumanCorrection` 和 `CorrectionEvent` 立即保存。`ReviewedExample` 默认 `admission_status=pending`，不代表已可检索。

### 4.3 记忆准入状态机

```text
pending -> approved
pending -> quarantined -> approved
pending -> rejected
approved -> quarantined
approved -> suspended
approved -> invalidated
quarantined -> rejected
```

状态含义：

- `pending`：已形成候选，等待质量评估和二级审批；
- `approved`：满足策略、可投影到 Milvus；
- `quarantined`：证据不足、风险高或存在冲突，暂不检索；
- `rejected`：确定性门禁失败；
- `suspended`：已批准案例被治理操作临时停用；
- `invalidated`：Schema 版本或业务规则失效。

`is_reviewed` 只表示存在客户审核动作，`admission_status` 才表示长期记忆准入状态，二者不能混用。审批失败不会删除原始纠错事件。记忆审批失败也不会使发票业务 Workflow 失败。

### 4.4 准入决策

`MemoryAdmissionService` 按以下顺序运行：保存事实、执行确定性质量验证、处理硬失败、调用独立审批模型获取结构化建议、由 Python Policy 合并信号、产生最终 Admission Decision。模型建议不能绕过确定性硬失败、二次审核、冲突和字段风险门禁。高风险或冲突案例进入隔离，只有低风险且质量、Reviewer 可靠性和多样性满足配置的案例才可批准。

批准后才生成索引投影；禁用、暂停、拒绝或 Schema 失效时删除对应的 Milvus 派生数据。事务使用稳定事件 ID、fingerprint 和 revision，Workflow 恢复不会重复评估、增加 occurrence count 或重复投影。

## 5. 字段语义绑定

字段目录 `FieldSemanticCatalog` 独立于 `InvoiceExtraction`，基础 `display_name`、`description` 和 `value_type` 只从 Schema 读取。目录额外维护已审批别名、负别名、上下文锚点、目录版本、租户 Scope 和有效性。

### 5.1 绑定流程

```text
原始图片标签
  -> Unicode/空格/标点规范化
  -> 标准名称和已审批别名精确匹配
  -> Dense + Sparse 字段目录检索
  -> Reranker
  -> 位置、上下文锚点、文档类型和值类型兼容性打分
  -> Top-1/Top-2 区分度判断
       -> accepted：选择当前 Schema canonical field_path
       -> review_required：候选接近或冲突
       -> unresolved：没有合格候选
```

“公司名”不能无条件绑定 `company_name`。`buyer_name`、`seller_name` 和 `company_name` 必须保留冲突候选；字段绑定不确定时不填值。审核界面展示原始标签、规范化标签、页面位置、上下文和候选路径。人工确认只生成 `pending FieldAliasCandidate`，单次映射不会成为全局别名。

### 5.2 别名学习

租户别名按不同 document、template、Reviewer 和时间窗口统计支持来源。相同标签映射到不同字段时生成 `MemoryConflictRecord`。只有审批后的租户别名才能进入 Field Semantic Index；全局别名需要更高权限、跨租户不可逆脱敏统计和独立审批。别名不能覆盖 canonical field path，且支持禁用、目录版本失效和回滚。

## 6. Hybrid Retrieval 与 Dynamic Few-shot

### 6.1 检索边界

每次检索首先过滤：

`tenant_id`、`document_type`、`field_path`、`schema_version`、`is_reviewed=true`、`is_valid=true`、`admission_status=approved`。

案例按 `confirmed_correct`、`corrected`、`confirmed_incorrect` 分别检索，每类独立配置候选数、Top-K、Dense/Sparse 权重和阈值。各路结果使用 Milvus Weighted Fusion 或 RRF 合并，按 `example_id` 去重并保留召回来源，再由 Reranker 重新排序。无合格结果返回空 `RetrievalContext`；Milvus 失败安全降级为无历史上下文的 Vision 提取，不能自动改写发票字段。

### 6.2 Milvus 派生索引

PostgreSQL 保存案例事实和投影状态；独立 Milvus Collection 只保存脱敏后的文本和向量。索引支持 Dense HNSW、Sparse/BM25、Tenant Partition Key、Scalar Filter、版本化 Collection、Alias 切换、批量 Upsert、重建、失效和回滚。pgvector 仅作为开发/迁移回退，不与 Milvus 长期双查询。

索引文本按区域组织：`DOCUMENT_CONTEXT`、`FIELD_PATH`、`MODEL_VALUE`、`REVIEWED_VALUE`、`CORRECTION_REASON`、`VENDOR_TEMPLATE_FEATURES` 和 `LABEL_TYPE`。敏感字段使用 `mask`、`hash` 或 `drop` 策略，Milvus 不保存原始 Base64 或未经脱敏的完整发票内容。

### 6.3 Prompt 优先级

Dynamic Few-shot Prompt 固定分为：

1. `CURRENT_IMAGE_FACTS`：当前图片事实，最高优先级；
2. `VERIFIED_CORRECT_EXAMPLES`：类似场景的历史审核结果；
3. `REVIEWED_CORRECTION_EXAMPLES`：以前错误的位置、人工修正值和原因；
4. `REVIEWED_NEGATIVE_EXAMPLES`：禁止重复的错误模式；
5. `MANDATORY_BUSINESS_RULES`：Schema 和业务硬规则。

历史案例不能覆盖当前图片。图片不清晰、字段绑定冲突或历史案例互相冲突时，只能返回候选并要求人工审核。RetrievalContext 为空时不执行第二次模型调用，除非现有架构明确需要。

## 7. 多租户与安全边界

租户来自可信认证/网关写入的 `request.state.trusted_tenant_context`，不接受客户端提交的租户 Header 或 Body 字段。所有文档、运行、结果、审核、案例、准入、目录和索引查询都同时匹配资源 ID 与租户 ID。

远程请求具备超时、指数退避、临时错误重试、限流、并发上限、熔断、脱敏和审计。日志不记录完整发票、Base64、API Key、完整 Prompt 或模型内部推理。每次检索生成 trace ID，并记录索引、模型、Prompt、阈值版本和阶段耗时。

生产环境还需要接入真实认证/RBAC、二级审批人、租户删除和数据保留策略。开发环境的 `INVOICE_INTELLIGENCE_DEV_TENANT_ID` 只能用于本机测试。

## 8. 数据存储边界

| 数据 | 当前存储 | 事实/派生属性 |
|---|---|---|
| 原始图片/PDF | FileStorage（当前本地目录） | 原始业务输入 |
| 文档、运行、结果、审核、案例 | PostgreSQL | 唯一事实源 |
| LangGraph Checkpoint | 独立 SQLite 或 PostgreSQL | Workflow 恢复状态，不是业务查询源 |
| 旧纠错向量 | PostgreSQL/pgvector | 开发和迁移回退 |
| 审核案例检索向量 | 独立 Milvus | 可重建的脱敏派生索引 |
| 字段语义索引 | 独立 Milvus Collection | 可重建的脱敏派生索引 |
| 评估、训练数据集和模型注册 | PostgreSQL；JSON/JSONL 报告 | 事实版本和可审阅派生物 |

主要 PostgreSQL 事实包括 `documents`、`extraction_runs`、`extraction_results`、`review_tasks`、`human_corrections`、`correction_events`、`reviewed_examples`、`memory_admission_records`、质量评估和信号、字段目录与别名、索引投影、评估、训练注册和审计表。Checkpointer 不与业务库复用同一存储边界。

## 9. API 与客户操作

### 发票处理

- `POST /api/v1/documents`：上传文件，要求 `Idempotency-Key`；
- `POST /api/v1/documents/{document_id}/extract`：启动提取；
- `GET /api/v1/runs/{run_id}`：查询运行状态；
- `GET /api/v1/runs/{run_id}/result`：查询结构化结果；
- `GET /api/v1/reviews/{run_id}`：读取审核任务；
- `POST /api/v1/reviews/{run_id}`：提交审核事实并恢复 Workflow。

### 记忆治理

- `GET /api/v1/memory/admissions`、`/{admission_id}`：查询准入记录和决策链；
- `POST /api/v1/memory/admissions/{id}/approve|reject|quarantine`：二级准入决策；
- `GET/POST /api/v1/memory/examples`、`disable`、Schema 失效、索引重建和检索反馈；
- `GET /api/v1/memory/evaluations/{evaluation_run_id}`：读取只读评估结果和 Promotion Candidate。

### 字段语义治理

- `GET /api/v1/field-semantics`：查询目录、别名和候选；
- `GET /api/v1/field-semantics/conflicts`：查询字段冲突；
- `POST /api/v1/field-semantics/aliases/{alias_id}/approve|disable`：审批或禁用租户别名。

写接口使用 `Idempotency-Key`，审批记录 reviewer、reason、版本和时间。稳定错误 Schema 区分 400、403、404、409、422、429、500 和 503。

## 10. 评估与训练治理

### 10.1 离线评估

评估数据按 `document_id` 隔离训练集、验证集和评估集，并按租户、document type、field path、vendor/template、图片质量分桶。检索指标包括 Recall@K、HitRate@K、MRR、nDCG@K、正负分离和空检索率；提取指标包括字段准确率、缺失识别、候选命中、错误自动填充数量、Review Required Precision/Recall。

评估至少比较：无记忆、旧 pgvector、Dense-only、Sparse-only、Hybrid、Hybrid + Reranker、Hybrid + 正负 Few-shot，以及字段目录的不同配置。结果绑定 Dataset、Index、Model、Prompt、Threshold 版本，只产生人工审阅的 Promotion Candidate，不自动修改生产配置。当前项目已提供评估数据契约、指标计算、持久化和报告结构，但不自动运行评估。

### 10.2 Hard Negative 与训练数据

正样本来自 `confirmed_correct` 和 `corrected` 的 `reviewed_value`。Hard Negative 来自 `confirmed_incorrect`、人工判错的高召回案例、Reranker 排名过高的错误案例、同模板混淆字段和同文档语义近似但路径不同的案例。最终标签必须来自人工审核；LLM 只能提出候选。

训练导出格式为 `query`、`positive`、`hard_negatives`、`scope`、`source_event_ids`。导出前脱敏，默认按租户隔离，数据按 document/template 分组切分防止近重复泄漏。当前实现支持版本化导出和 Registry，不执行训练。

### 10.3 远程训练与模型晋升

训练边界优先考虑 Reranker，其次 Embedding，最后才考虑 Vision/生成式模型。FastAPI 只创建
`TrainingJob`，独立 Worker 才可提交、刷新和取消远程训练，在线请求不能更新权重。七状态固定为
`planned/submitted/running/succeeded/failed/cancelled/quarantined`；任务绑定 tenant、Dataset、Schema、
Index、Model 和 Prompt 版本，并一对一关联保留的 `TrainingRun`。默认 Stub 不会伪造成功；可配置
MLflow-compatible REST Adapter 不强制依赖 MLflow 服务。产物必须通过 SHA-256 校验后才登记来源、
版本和审计元数据；LangSmith Prompt 引用不是业务事实源。

晋升状态为：

```text
registered -> offline_evaluated -> shadow -> canary -> human_approved -> production
                                             \-> rolled_back
```

指标下降、审核率上升、错误自动填充增加、远程服务不稳定或人工否决时必须回滚。禁止自动晋升；远程服务不支持训练时只保留数据导出和 Model Registry，不伪造训练接口。

## 11. 当前能力状态

本节只保留方案级摘要；逐项代码证据、阻塞项和优先级以
[`docs/project-status.md`](docs/project-status.md) 为唯一状态基线。

| 能力 | 当前状态 | 说明 |
|---|---|---|
| 文件摄取、PDF 渲染、图片质量处理 | 已实现 | 有大小、页数、像素和清晰度限制 |
| 单一确定性 LangGraph Workflow | 已实现 | 节点和主要路由稳定，支持 interrupt/resume |
| OpenAI/千问 Vision Structured Extraction | 已实现 | Provider 可配置，响应 Pydantic 校验 |
| 确定性验证和人工审核 | 已实现 | 当前图片优先，不自动填充历史值 |
| PostgreSQL 事实源与租户隔离 | 已实现 | Alembic migration 已覆盖核心表 |
| Milvus Dense + Sparse 案例索引 | 已实现为可选 Adapter | 需配置连接、模型和受控投影/激活 |
| pgvector 回退 | 已实现 | 仅在 Milvus 未启用时使用 |
| 三类审核案例和准入状态机 | 已实现 | 新案例默认 pending，approved 后才可索引 |
| 独立记忆准入 Worker | 已实现 | PostgreSQL claim/lease、退避、隔离和投影登记；需单独启动 |
| 记忆质量验证与审批模型建议 | 已实现为受控边界 | 模型建议不能单独批准 |
| 字段语义目录、绑定和别名治理 | 已实现为受控边界 | 目录投影和激活需显式治理操作 |
| Dynamic Few-shot | 已实现 | 正例、纠错例、负例分区，当前图片优先 |
| 本地 PaddleOCR Small YAML | 已实现 | `conf/ocr/ppocrv6_small_v1.yaml`，独立 HTTP 服务 |
| PaddleOCR HTTP Adapter 与多源比对 | 已实现 | 可选接入主 Workflow；OCR observation 不直接覆盖 Vision |
| OIDC/JWT、可信租户与 RBAC | 受限可用 | 基础链路及 Training 的 `training:submit` 规则已实现；生产部署仍需独立验收 |
| Human Review Task Service | 已实现 | claim/lease、转派、恢复、幂等提交和 Workflow resume 已具备 |
| 索引重建与投影 Worker | 受限可用 | 两类队列已有租约到期、稳定 worker ID、续租、超时重领和迟到写入 fencing；尚缺隔离 PostgreSQL 双 Worker 与真实 Milvus 完整性演练 |
| S3-compatible 对象存储 | 受限可用 | Adapter、checksum、presign、生命周期已实现；Compose DB 凭据链路仍阻塞生产启动 |
| 自动离线评估调度 | 受限可用 | 诊断型 Snapshot/Job/Schedule、PostgreSQL queue、Stub Worker 与可选 HTTPS Suite Runner Adapter 已实现；远端执行及隔离证据未验收，诊断指标不能作为晋升证据 |
| 远程训练控制面 | 已实现为可选 Adapter | 默认 Stub fail closed；MLflow-compatible REST 需显式 endpoint、凭据和 artifact allowlist |
| 模型晋升与回滚 | 受限可用 | 门禁事实从 Evaluation/Artifact/Version Registry 重载，审批/CAS/历史目标回滚受控；真实部署与自动晋升仍禁用 |
| 独立财务 Domain | 受限可用 | 同租户完成 Run 校验和状态机已实现；仅提供 mock 外部 Provider |
| 交易分析 Domain | 受限可用 | 同租户 completed Result 派生候选、幂等/CAS 已实现；规则仍是开发 Mock，历史候选需重新核验 |
| Docker Compose | 受限可用 | 数据库 secret/DSN 已统一解析，Evaluation Worker 与 Scheduler 已接入队列；容器连接和生产安全演练尚未完成 |

## 12. 生产化分阶段计划

### 阶段 A：稳定在线提取

Training RBAC、Compose 数据库 secret/DSN、Transaction 来源/幂等/CAS 与 Promotion 可信输入已修复；下一步提供并验收真实隔离 Suite 变体执行服务；
再完成正式密钥管理、备份恢复演练、共享限流和告警。保持当前 Workflow 和业务 Schema 不变。

### 阶段 B：启用可信记忆

由治理人员处理 `pending` 准入任务，建立二级审批和 Reviewer 可靠性流程；启用 Milvus 投影、版本 Alias、重建和回滚；观察空检索、正负命中和误导检索指标。

### 阶段 C：启用字段语义绑定

冻结基础 Catalog Version，审批租户别名和上下文锚点；接入审核界面中的标签、位置和候选路径；冲突和 unresolved 必须进入人工审核。

### 阶段 D：接入多源 OCR

固化已实现的 PaddleOCR HTTP Adapter、超时/降级、指标和字段级对齐回归；OCR 只提供独立观察，
不覆盖 Vision 结果或改变 `InvoiceExtraction` Schema。

### 阶段 E：离线评估和受控训练

现有诊断型 Dataset Snapshot、Evaluation Job/API、PostgreSQL queue、Worker 与 scheduler 不能替代真实评估；先接入冻结审核数据集和全部 Suite 变体 Runner，再在隔离外部训练
环境执行实验。Promotion Gate 必须从可信 Registry 重载事实，经 shadow、canary 和人工批准后才可切换。

## 13. 客户最终可获得的能力

目标状态完成后，客户上传一张清晰、模糊、裁切、手写或版式变化较大的发票图片，系统将：

1. 在租户边界内保存原件并进行可解释的质量判断；
2. 用当前图片事实提取固定发票字段，不因历史案例覆盖当前内容；
3. 将“公司名”等自然语言标签绑定到 Schema 候选，并在歧义时显示候选而不是误填；
4. 在有足够证据时检索同租户已审批的正确例、纠错例和负例；
5. 在证据不足或来源冲突时请求人工确认；
6. 将明确审核事实沉淀为待准入记忆，由确定性规则和二级审批控制是否长期使用；
7. 通过索引、模型、Prompt 和阈值版本追踪每次结果；
8. 通过离线指标判断记忆是否真正减少错误，并支持回滚到上一版本。

这套流程的核心承诺是：系统可以持续学习，但任何历史记忆、别名或模型建议都不能绕过当前图片证据、租户隔离和人工治理门禁。

## 14. 主要剩余风险

- OIDC/RBAC 及 Training 路由权限已实现，但生产身份与部署仍需独立验收；
- Compose 数据库 password secret 已接入应用 DSN，但尚未验证容器端到端连接，当前部署不能称为生产就绪；
- Promotion 仅切换 PostgreSQL 注册状态，尚未接入真实模型部署；Transaction 规则仍为开发 Mock，历史候选需重新核验；
- Evaluation Worker/Scheduler 可运行诊断型队列，但尚不能自动运行真实 Suite 变体评估；
- Milvus 是派生索引，必须持续验证 PostgreSQL 投影状态与 Alias 一致；
- 租户删除、保留策略、密钥轮换和审计归档需要运营制度配套；
- 远程模型的可用模型名、地域 Endpoint 和价格应以客户账户当时的官方目录为准；
- 任何自动训练或自动晋升都不在当前系统允许范围内。

## 15. 启动入口

开发环境的 Docker、中间件、FastAPI、Vue 3 和可选 PaddleOCR 启动顺序见 [README.md](README.md) 顶部“快速启动（Windows PowerShell）”。本地 Small OCR Pipeline 位于 [`conf/ocr/ppocrv6_small_v1.yaml`](conf/ocr/ppocrv6_small_v1.yaml)。
