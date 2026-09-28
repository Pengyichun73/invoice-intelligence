# Architecture

## Code Generation and Self-Healing Harness

Harness 采用独立的确定性 Application 状态机，不新增 LangGraph、Agent Graph 或开放式 Agent Loop，
也不改变现有发票识别 Workflow 和 `InvoiceExtraction` Schema。固定阶段为：

```text
prepare_task -> inspect_repository -> retrieve_code_context -> generate_patch
-> validate_patch -> execute_in_sandbox -> evaluate_result -> repair_or_finish
```

Repository Snapshot、Patch、Execution、Watchdog、Postmortem 和审计事实由 PostgreSQL 保存；代码 CAST
和向量索引是绑定 `repository_id`、`snapshot_id`、`revision` 的脱敏可重建派生数据。Parser、Code Model、
Embedding/Reranker、Code Index 和 Sandbox 均只能通过 Application Port 接入。

当前 Harness 已完成 Domain/Port 契约、版本绑定、固定 Runner、Snapshot/结构化 Patch 校验、Python
AST/结构化参数与本地精确检索适配器、PostgreSQL 首版事实迁移与 Task/Postmortem/Repository Source Registry
Repository、Snapshot/Patch/Execution/Watchdog 事实写入、Task claim/lease fencing、通用 Harness
Worker、API/Composition Root 装配、共享隐私 Trace/低敏 Metrics
Adapter，以及默认拒绝 Sandbox 边界；尚未完成 Source Registry 注册/更新治理 API、运行时 reload、
真实 Tree-sitter/CST 多语言 Adapter、代码模型、代码 Milvus 或 MicroVM。首版迁移和 Repository
尚未在真实 PostgreSQL 上执行验收。缺少这些能力时
必须返回稳定受限状态并 fail closed，不得执行生成代码或伪造检索结果。详细边界见
`docs/superpowers/specs/2026-09-24-code-generation-self-healing-harness-design.md`。

执行预算包含最大尝试次数、墙钟时间、Patch bytes、文件数、修改行数、Patch operation 数和模型
Token 数。结构化 Patch 只能基于同一 Snapshot 的原始 UTF-8 byte 坐标一次性重建；校验拒绝路径越界、
保护文件、checksum/version 不一致、重叠区间、重复锚点和 Python 语法失败，并记录 AST 结构变化。
可信 `trace_id` 贯穿 Task、Workflow、Postmortem source event 和低敏 Span；Metrics Adapter
额外记录阶段耗时和可选 Token 统计，但仍只是进程内低基数指标，不是资源技术 ID 的持久化审计存储。

Postmortem source event 已具备幂等事实写入、独立治理 Application Service、HTTP 路由、
可信 actor/RBAC 和 revision CAS admission Repository；长期代码经验检索投影仍未实现，不能将
`approved` 字段存在误认为经验已可检索。

## 对象存储边界

`DocumentIngestionService -> FileStorage` 是唯一上传调用方向；API Router 只调用 Application
Service。`bootstrap.py` 根据 Pydantic Settings 组装 Local 或 S3-compatible Adapter。原件、渲染图
和派生文本通过固定 `ObjectKind` 映射到三个独立 private Bucket，客户端不能指定 Bucket/key。
隔离 Compose 使用 Local Adapter 时，API 和提取 Worker 必须将同一 `invoice_documents` 卷
作为相同的绝对存储根目录；否则上传成功后 Worker 仍可能找不到原件。
S3 启动检查读取 Bucket ACL 与 Policy；`Allow` 的 Principal 数组包含通配符或使用
`NotPrincipal` 时拒绝启动。该代码检查不能替代真实 S3 权限与匿名访问验收。

上传安全校验完成后计算 SHA-256，Adapter 使用不可覆盖的稳定对象键写入并执行 HEAD/checksum
验证。Document 与 `stored_objects` 在同一 PostgreSQL 事务登记；MinIO/S3 不参与数据库事务。
`stored_objects` 是生命周期事实源，Bucket 不能反向决定业务状态。预签名 URL 仅作为瞬时响应，
不写数据库、日志或审计。

`StorageLifecycleService` 通过 PostgreSQL claim/lease 处理删除。到期对象先撤销读取授权，再执行
幂等对象删除；不存在按成功处理，权限错误不重试，临时网络错误保留待重试状态。Phase One
migration 保留历史 `local://` 引用并标记 `migration_pending`；生产切换 S3 前必须完成内容复制和
checksum 校验，Phase Two 收口不得猜测历史文件大小或伪造迁移完成。

## Dependency rules

```text
domain          -> Python standard library + Pydantic business schemas
application     -> domain + application ports
workflow        -> domain + application ports/use cases + LangGraph
infrastructure  -> domain + application ports + external adapters
api             -> application-facing contracts + FastAPI
bootstrap       -> config + application + workflow + infrastructure
main            -> api + bootstrap + config (process entry/lifespan only)
```

`bootstrap.py` 是 Composition Root，负责构造具体实现和 application service；`main.py`
只将这些依赖注入 `api.main.create_app()` 并管理 lifespan。`api` 不依赖 `bootstrap`、
`workflow`、ORM、Checkpointer 或模型 SDK。除 Composition Root 外，协议层不得直接实例化
模型客户端、数据库连接、文件存储或向量数据库。

## System boundary

核心发票 Workflow 边界是 invoice/image/PDF extraction、字段证据、确定性验证、HITL、结果持久化和
受限纠错 Few-shot；独立 Accounting 与 Transaction Domain 不直接改写该 Workflow 或
`InvoiceExtraction`。不存在 Insights/Chat Agent、Supervisor/Multi-Agent、客户支持或情绪分析模块。LLM 只执行结构化视觉提取，不决定 Graph
路由，不调用工具，也不维护开放式记忆。
历史交易候选与当前同租户已完成 Run 派生快照不一致时，确认和驳回仍被阻断；授权 Reviewer
仅可通过现有幂等、revision/CAS 审核路径升级为 `escalated`，附固定脱敏来源原因码并保留原候选。

## Authentication and authorization

隔离验收使用 `compose.acceptance.yml` 中独立 Keycloak realm `invoice-acceptance`。
浏览器端采用 Authorization Code + PKCE，令牌仅保留在会话存储；API 仍通过
`OIDCJWTAuthContextProvider` 校验签名和 claim，并以既有 RBAC 决定权限。
OIDC 模式禁止缺少 Bearer Token 时回退到固定 `local-developer` 身份。
角色成员关系由 Keycloak 管理，PostgreSQL 只保存业务/审计事实。
隔离环境已核验两名独立账号的 PKCE 登录、JWT 签名及角色隔离；这不等于审核、准入或检索闭环验收。

HTTP 安全链固定为 `Bearer Token -> OIDCJWTAuthContextProvider -> AuthContext ->
AuthorizationPolicy -> TrustedTenantContext -> Application Service`。OIDC Adapter 位于
Infrastructure，JWT SDK 不进入 Domain；API middleware 只安装可信上下文和执行路由权限检查，
Router 仍只调用 Application Service。既有 Memory Service 权限检查继续作为第二道门禁。

Token 的签名、算法、过期时间、签发时间、issuer 和 audience 必须通过验证，Keycloak
`realm_access`/目标 client `resource_access` roles 与 scope 由确定性 Policy 映射为权限。
JWKS 只导入允许算法的签名密钥，忽略同一文档中的加密密钥；单个不支持的密钥不得
阻断有效签名密钥的加载。
`tenant_id` 和 `reviewer_id` 只读自可信 claim，普通 Header、Query 和 Body 无权覆盖。
生产配置禁止 demo tenant、HTTP issuer/JWKS 或关闭 TLS 验证。

资源 Repository 保持 `tenant_id + resource_id` 联合过滤。系统不会执行 tenant-agnostic 存在性
探测；不存在与跨租户访问均返回 404，并写入同一类脱敏安全审计事件，避免资源存在性侧信道。
身份/RBAC 注册事实和安全审计属于 PostgreSQL；Token、Authorization Header 和 API Key 不落库、
不进入日志。

## Deterministic workflow

记忆增强沿用现有单一 Workflow：召回的合格案例仅形成值盲字段模式，已批准字段目录提供
正负别名；历史字段值和自由文本纠错原因不进入第二次 Vision Prompt。新建
`field-pattern-v1-` 索引版本时派生 Milvus 投影不含历史字段值，审核事实仍只在 PostgreSQL。
OCR 当前页唯一绑定区域可触发一次受限裁剪重读，默认 `off`，`shadow` 不改结果，`apply`
须再通过当前证据核对、OCR 重比较和原确定性 Validator。该代码入口尚未通过真实样本收益
验收，生产路由不得启用。隔离标注 JSONL 的身份字段只是声明，不替代 OIDC/RBAC 审计。
生产目标暂为独立 Linux 单机，异机加密备份未就绪前禁止生产灰度。

隔离 Compose 的 HTTP API 只在业务 PostgreSQL 同一事务登记 `extraction_runs` 与
`extraction_work_items`，立即返回 `received` 和 `run_id`。独立 `extraction-worker` 通过
PostgreSQL claim/lease 领取 start/resume 任务，使用共享 PostgreSQL Checkpointer 执行下方
同一个 Graph；重启时按 `thread_id` 的现有 checkpoint 继续。队列租约更新与完成采用 claim token
条件写入，审核恢复还固定提交时的 checkpoint ID，不能把旧决定送入下一轮 interrupt；
业务结果仍由原有 `run_id` write-once 规则保护。审核提交先验证可信审核人和任务租约，
并在入队事务中再次锁定审核任务校验 revision、租约与 Reviewer；待执行恢复期间禁止取消、
转交、重新领取或过期回收。入队后返回 `execution_status=queued`；页面按 `run_id` 查询权威结果。开发机默认
同步路径不变。该异步路径的迁移和双进程恢复尚未隔离环境验收。

```text
prepare_document
  -> extract_invoice (baseline)
  -> retrieve_correction_context(current extracted facts)
     -> reviewed-example Milvus OR tenant-scoped pgvector fallback
     -> no match -> validate_extraction
     -> scoped matches -> extract_invoice(dynamic Few-shot) -> validate_extraction
     -> accepted -> persist_result -> save_correction_memory -> END
     -> review_required -> request_human_review
                           -> interrupt()
                           -> Command(resume=HumanCorrection)
                           -> apply_human_correction
                           -> validate_extraction
     -> rejected -> FAILED -> END
```

该流程使用一个 `StateGraph`。条件边全部调用 Python 路由函数，不使用 Supervisor、
Multi-Agent 或由 LLM 决定路由。歧义、缺失、不可读字段和 validation anomaly 均进入
`pending_review`；状态先写入业务库和 Checkpoint，再进入 `interrupt()` 节点。

恢复必须使用相同 `thread_id` 调用 `Command(resume=...)`。LangGraph 会从中断节点开头
重放，因此该节点在 `interrupt()` 前没有副作用。最终结果按 `run_id` write-once；相同内容
重放视为成功，不同内容拒绝覆盖。纠错事件 ID 不含易变创建时间，由 run/document 和稳定
事件内容计算；向量 source ID 唯一，恢复重放不会重复增加纠错模式计数。

Milvus 审核案例检索启用时，`retrieve_correction_context` 只走
`ReviewedExampleContextProvider`，不会同时查询旧 pgvector 记忆；Workflow 不再向旧 pgvector
写入新记忆，pgvector 只在企业案例 Provider 不存在时作为已有数据迁移/开发回退。两条路径不会
长期双查询。

## Multi-tenant boundary

```text
trusted authentication/gateway
  -> request.state.trusted_tenant_context(tenant_id, actor_id, permissions)
  -> FastAPI dependency
  -> Application Service
  -> tenant-scoped Repository / Retrieval Scope
```

除健康检查外，HTTP Adapter 不读取 `X-Tenant-ID`，也不允许 Body/Path 覆盖租户。`documents`、
`extraction_runs` 与所有案例/索引状态均保存技术字段 `tenant_id`；查询资源时必须同时匹配租户和
资源 ID。原始纠错事件和审核案例写入 PostgreSQL 时还会校验租户、运行和文档归属，人工审核
请求中的 `reviewer_id` 必须等于可信 Actor。`tenant_id`、Index/Model/Prompt 版本均不属于
发票业务字段，禁止加入 `InvoiceExtraction`。

旧记录升级到 migration `20260901_0009` 时先归入 `__legacy_unassigned__` 隔离租户。系统不会
猜测历史归属；管理员完成显式映射前，普通租户无法访问这些记录。

## HITL lifecycle

### Human Review Task Service

`ReviewTask` 是独立于 LangGraph Checkpoint 的 PostgreSQL 业务聚合。Workflow 进入
`pending_review` 时幂等创建或重开任务；API 通过 `ReviewTaskService` 执行 tenant-scoped 查询、
原子 claim/lease、release、reassign、cancel 和 expiry recovery。每次状态变更均递增 revision，
CAS 失败返回冲突，跨租户资源不暴露存在性。reassign 清除原审核人的 lease 并创建定向
`pending_review` 分配，只有目标 Reviewer 能重新 claim 并获取新 token。

提交路径为：可信 Reviewer + lease 校验 -> `Idempotency-Key` 绑定 -> 现有 LangGraph resume ->
HumanCorrection/CorrectionEvent 与 memory recovery outbox 事务 -> task `submitted` -> 最终结果
write-once -> 记忆准入 Worker。审核事实事务、结果事务和记忆事务互不回滚。

```text
received
  -> processing
  -> review_required
  -> persist WorkflowStatus.pending_review + review_tasks
  -> interrupt()
  -> POST review + Idempotency-Key
  -> Command(resume=HumanCorrection) with original thread_id
  -> schema-valid correction + CorrectionEvent + resolve review
  -> validate again
     -> pending_review (repeat) | completed | failed
```

`thread_id` 只定位 Checkpoint，`run_id` 只定位业务执行，`document_id` 只定位原始文档。恢复
接口不接受客户端提供存储 URI、checksum 或替代 thread ID。无效人工修正不会覆盖提取结果，
而是更新 review reason 并继续保持 `pending_review`。

## Field-level validation

验证层只使用可观察或可重算信号，不读取模型 confidence。页面图像经 Pillow 独立计算
清晰度与分辨率；字段结合缺失状态、Pydantic Schema、金额/税额一致性、日期/币种/编号
格式、Vision 候选和显式歧义、Provider anomaly，并可选比较独立 OCR 候选。

每字段输出 `ValidationSignal[]`、合成分数和 `FieldDecision`，路由值严格为
`accepted`、`review_required`、`rejected`。全局阈值由配置提供，字段路径可通过 JSON 做
稀疏覆盖。`rejected` 仅用于质量信号达到拒绝阈值的不可用文档；业务/格式/候选冲突进入
人工审核。审核请求明确返回当前值、候选、触发规则、原因和所需操作，不自动选择候选值。
人工执行 `confirm_correct` 或 `correct` 后，该字段以 `HUMAN_CORRECTION` 证据终止图像、OCR、
格式和业务规则的重复审核，但完整 `InvoiceExtraction` Schema 校验始终保留。

OCR 是 application port，Composition Root 在显式启用时构造 Infrastructure Adapter；默认关闭，
此时不会产生 OCR 信号或扣分。同一批规范化临时页面同时提供给 Vision 与 OCR，请求可有界并发，
合并顺序固定为 Vision、Vision 字段绑定、OCR、集中比对。原始 OCR 行只在单次请求内存在；
Checkpoint 仅保留字段候选、来源引用、页码、位置、未校准分数和原因码等技术摘要，不保存图片、
Base64、完整 OCR 响应或原始行文本，也不把技术元数据写入 `InvoiceExtraction`。

隔离 Compose 的 `compose.ocr-gpu.yml` 可单独部署 GPU OCR，沿用版本化 YAML、`gpu:0`
和容器内 8077；Worker 通过 `http://ocr:8077` 调用，不向主机发布 OCR 端口。
未启用覆盖文件时保持 `http://host.docker.internal:8077`，以兼容现有宿主 PaddleX。
`scripts/manage-acceptance.ps1` 负责选择一种路径及统一启动/停止 API、Worker 和依赖；
不能同时运行两份 GPU OCR。Docker 镜像使用官方 PaddleX 基础镜像并升级到项目固定的
PaddleX/PaddleOCR 版本；新镜像尚须现场构建及 OCR 文本框验收，配置存在不等于服务可用。

本地 PaddleOCR 运行基线固定为独立 `.venv-ocr`、`gpu:0`、回环地址
`http://127.0.0.1:8188`、PP-OCRv6 Small 检测/识别模型及版本化 Pipeline 配置
`conf/ocr/ppocrv6_small_v1.yaml`，详见 [`local-ocr.md`](local-ocr.md)。不要使用
`--pipeline OCR`，因为该注册名会加载 PaddleX 默认的 `PP-OCRv6_medium`。Infrastructure
`PaddleXOCRHttpAdapter` 通过 `RawOCRProvider` 输出未绑定的行级技术观察；
`DeterministicMultiSourceOCRComparisonService` 使用当前 `FieldSemanticCatalog` 和
`FieldSemanticBindingService` 做字段对齐，再按 canonical field path 聚合并形成确定性多源判断。
OCR 不得直接写入业务 Entity 或覆盖当前图片证据；比对冲突和绑定不确定只通过既有验证器进入
人工审核。审核请求展示有界的 Vision/OCR 来源、候选、页码、位置和 reason codes；OCR-only
候选只供人工选择。OCR 不可用形成 `unavailable` 摘要且不扣分，集中绑定或比对异常形成
`unresolved` 并进入审核，两者均不会因 OCR 故障把 Workflow 标记为 failed。

历史审核案例的模板相似度不是发票身份；当前二次 Vision Prompt 仍含历史脱敏值，后续应改为
已审批的值盲字段模式提示、当前图片区域定向重读和逐字段独立验收，详见
[`memory-extraction-roadmap.md`](memory-extraction-roadmap.md)。
Composition Root 可同时注入固定本地 Adapter 与
`INVOICE_INTELLIGENCE_OCR_REMOTE_PROVIDERS` 中启用的 PaddleX-compatible Adapter；远程配置按
唯一 `provider_name` 排序，各 Provider 独立限流、重试与熔断，Application Service 按配置序列
收集后再由集中比对服务稳定排序。一个 Provider 不可用不会取消其他 Provider 或 Vision。

业务 Schema `3.0.0` 固定为单一 `InvoiceExtraction`，只包含已确认的 19 个
required-but-nullable 字段，并拒绝所有额外字段。字段索引不可用时，仅唯一精确 Catalog 匹配
可由确定性策略接受；多义标签仍进入人工审核。Schema 2.x 的 VAT 分支和明细数组不属于当前
Entity，新运行不会读取或生成这些字段。

OCR 可观测性遵循依赖反转：Application 定义 `OCRTelemetry`，PaddleX Adapter 与集中比对服务只
调用该 Port，Infrastructure 的 `StructuredLoggingOCRTelemetry` 输出聚合友好的结构化指标事件。
Provider 事件记录整批/逐页耗时、最终逐页结果计数、每页文本框数、Empty OCR Rate 及
Provider/模型/Pipeline 配置版本；比对事件记录成功绑定字段数、各 outcome 数量及 OCR 冲突导致
审核的 rate 分子分母。Telemetry 不接收发票字段值、原始 OCR 文本、图片、Base64 或完整响应，
且上报异常被隔离，不能改变提取结果或 Workflow 路由。
Infrastructure 的 `SQLAlchemyOCRTelemetry` 先写相同结构化日志，再把仅含计数、耗时、outcome 和
版本标签的事件持久化到 PostgreSQL。`MemoryGovernanceService.get_ocr_metrics()` 通过
`OCRMetricsRepository` 聚合，HTTP Router 不访问 ORM；指标存储不含租户字段值、OCR 文本或图片。

```text
PaddleXOCRHttpAdapter (infrastructure) -> OCRTelemetry (application port)
DeterministicMultiSourceOCRComparisonService -> OCRTelemetry
StructuredLoggingOCRTelemetry (infrastructure) -> structured operational logs
OCR comparison -> EvidenceBasedExtractionValidator -> existing Python route
```

## Field semantic catalog

```text
InvoiceExtraction Pydantic Schema (read-only)
  -> FieldSemanticSchemaReader (display name / description / value type)
  -> FieldSemanticCatalog
  -> approved tenant aliases + negative aliases + context anchors (PostgreSQL)
  -> FieldSemanticIndexProjectionService
  -> dedicated Milvus Dense + Sparse collection (derived, rebuildable)
  -> FieldSemanticBindingService (exact -> hybrid -> rerank -> deterministic policy)
  -> FieldBindingCandidate[]
  -> accepted | review_required | unresolved
  -> existing validate_extraction / request_human_review route
  -> explicit human mapping -> pending FieldAliasCandidate + support source
```

字段语义目录是版本化技术元数据，不属于发票 Entity。人工映射先生成独立的 `pending` 候选，
人工决策以追加式审计记录保存；只有当前 Catalog Version 中 `approved + is_valid` 的正式别名参与候选生成。
目录不会修改 Pydantic 字段，也不把模型或匹配分数作为最终绑定权威。`company_name`、
`buyer_name`、`seller_name` 等同后缀字段保留冲突候选，因此“公司名”必须结合当前文档上下文和
人工审批，不能无条件选择字段。候选是历史/语义先验，不能覆盖当前图片证据。

字段目录索引与 ReviewedExample Collection 物理分离。每个租户的投影视图由只读 Schema 基础字段
和 PostgreSQL 中当前 Catalog Version 的已审批覆盖合成；未审批、rejected、suspended、invalidated
别名不会进入索引。Dense 使用 display name、description、approved aliases 和正向 context
anchors；Sparse/BM25 使用原始标签、标准名称、approved aliases 和 canonical field path。负向别名
保留为排除元数据，不作为正向检索文本。

### Explicit governance gate

治理写入遵循固定顺序：

```text
客户审核事实
  -> 确定性质量评估（硬失败不可覆盖）
  -> 可选审批模型 advisory（结构化、无 Chain-of-Thought）
  -> Python Policy 合并
  -> 授权 Reviewer + 二级审批人校验
  -> PostgreSQL revision/CAS + Idempotency-Key 事务
  -> 审计与投影登记
```

Admission 状态为 `pending -> approved|quarantined|rejected`，已批准案例可进入
`suspended|invalidated`；隔离案例只能由治理人员重新进入评估或作出最终决定。Alias 使用独立
Catalog Version 和 revision；Conflict 只允许 `open -> resolved|dismissed`，resolved 必须选择
当前 Schema 中已有的候选路径。Conflict 关闭不会修改 `InvoiceExtraction`、发票值、Admission
或 Alias 状态，只写入重新评估任务。Admission、Alias、Conflict 的状态更新和 GovernanceAudit
必须在同一 PostgreSQL 事务中提交；Milvus 清理/投影在事务外幂等执行。

准入列表的可选 `run_id`、`field_path`，案例列表的 `run_id`、`field_path`，字段目录的标准字段路径
及冲突列表的 `field_path` 均在 Repository 的租户约束和游标分页前应用。案例和准入的 Run ID 从
保留的 `example_feedback` 来源事实匹配；合并案例的 canonical `run_id` 仍可能是首次来源。
治理列表以业务更新时间
或最近出现时间降序、资源 ID 降序稳定分页，游标只在同租户内解析。前端治理写入后保留筛选并重新
读取权威列表与单条详情，不把批量响应中的失败项误当作已批准事实。

所有需要二级审批的操作都拒绝原始客户审核人再次执行，包括关联案例或别名来源 Reviewer。相同
Idempotency-Key 且语义一致时优先返回原决定和审计记录；语义不同返回 409；revision/CAS 过期
同样返回 409。模型建议、历史案例和字段别名不能绕过当前图片证据、确定性硬失败或权限门禁。

绑定输入只包含当前图片的标签、页面/位置、上下文观察和值类型，不包含历史字段值。Service 对标签
执行 NFKC、空格和 Unicode 标点规范化，先匹配标准名称与已审批别名，再使用活动索引执行严格 Scope
Filter 下的 Dense+Sparse Hybrid Retrieval，最后由 Reranker 排序。Python Policy 组合精确匹配、
召回/重排顺序、已审批上下文锚点、位置和值类型兼容性，并计算 Top-1/Top-2 margin。只有高区分度且
所有规则通过时选择当前 Schema 中的 canonical field path；接近候选、模糊别名缺少上下文、类型
冲突或远程能力失败均禁止自动选择。返回分数是未校准相关性，不是 probability，绑定决策无权修改
当前发票字段值。

`extract_invoice` 在每次 Vision 调用前构造版本化 `FIELD_SEMANTIC_CATALOG` Prompt 投影；投影只含
Schema 基础定义及当前 Catalog 中已审批、有效的租户别名，不携带租户审批元数据。Vision 技术
Envelope 可返回原始标签、页面、可选 bounding box、有限上下文和值类型观察，但业务结果仍只能是
canonical `InvoiceExtraction`。Application Service 随后执行字段绑定，完整 Hybrid/Rerank 结果在
请求内丢弃，GraphState 仅保留候选 canonical paths 和决定摘要。

非 `accepted` 绑定不会修改或清空当前图片事实，而是将原始标签和候选附加到现有 ReviewRequest。
Reviewer 的字段映射决定与字段值修正分离，并校验 evidence、document、Schema 及候选 Scope。确认后
以稳定 tenant-scoped HMAC fingerprint 创建或复用 `pending FieldAliasCandidate`，并追加独立
`FieldAliasCandidateSupport`；来源绑定 tenant、run、document、evidence、原绑定决定、Reviewer、
原 Catalog、原因和 template fingerprint。来源唯一约束保证 Workflow 重放不增加支持度；此阶段
不创建正式 alias、不注册 Catalog、不调度 Milvus 投影。

候选使用独立正整数 `revision`（初始为 1）进行乐观并发控制。只有插入新的唯一支持来源才增加
revision；相同来源重放不增加。治理决策通过 tenant/candidate/status/revision 条件执行单条 CAS
UPDATE，并在同一 PostgreSQL 事务追加包含 previous revision、target revision、前后状态、Reviewer、
Reason、Policy Version 和决定时间的不可变 Decision。CAS 影响 0 行时重新按 tenant 查询：不存在为
404，revision 或状态已改变为 409。

```text
human field binding
  -> pending FieldAliasCandidate
  -> idempotent support source
  -> windowed document/template/Reviewer diversity
  -> same-label/different-field conflict detection
  -> explicit tenant-governor approval
  -> new inactive Catalog Version containing approved aliases
  -> explicit field-semantic index build
  -> explicit Catalog/Index alias activation
```

Python Policy 在审批前硬性检查支持度、canonical label collision 和未解决
`MemoryConflictRecord`。单次映射和模型输出都没有审批权。已发布别名禁用后，PostgreSQL alias 先
进入 `suspended`，关联 Catalog 的活动索引版本随后失效，Milvus 数据可重试删除。Catalog 回滚
通过 reissue 历史有效版本形成新 draft，不原地激活 retired 版本。全局候选只接受多个已发布、仍
有效的租户 Catalog 来源，由更高权限调用生成；跨租户来源身份仅持久化 HMAC-SHA256 tenant
fingerprints 和聚合计数，语义键保留 Schema、document type、canonical field path 与规范化标签。
全局审批不会直接进入租户 Catalog 或 FieldSemanticIndex。

## Storage and identity boundaries

```text
thread_id   -> LangGraph checkpoint thread
run_id      -> one business workflow execution
document_id -> one stored document
```

三种标识均非空且互不相同。WorkflowStatus 同时存在于 Checkpoint State 和独立业务库，业务
状态包括 `received`、`processing`、`pending_review`、`completed`、`failed`。

业务库使用 SQLAlchemy 2.x；默认及生产使用 PostgreSQL，开发可在关闭向量记忆后使用
SQLite。Alembic 管理
`documents`、`extraction_runs`、`extraction_results`、`review_tasks`、
`human_corrections`、`correction_events`、PostgreSQL `correction_memories`、
`correction_memory_sources` 和 API 幂等表，不包含 Checkpoint 表。

开发环境使用另一 SQLite 文件保存 Checkpoint；生产使用与业务数据库不同的 PostgreSQL
Checkpoint database。配置按 host/port/database 比较，禁止仅通过更换账号或 DSN 文本规避
隔离；SQLite checkpoint/business 文件也不得放进 `file_storage_root`。业务库不承担
LangGraph 崩溃恢复。

```text
FileStorage
  -> immutable original bytes

BusinessRepository
  -> tenant-owned documents / extraction_runs / extraction_results
  -> review_tasks / human_corrections / correction_events / idempotency_requests

FieldSemanticCatalog
  -> read-only InvoiceExtraction Schema metadata
  -> tenant catalog versions / aliases / decisions / context anchors (PostgreSQL)

FieldAliasLearningService
  -> pending candidates / idempotent source facts / human decisions (PostgreSQL)
  -> windowed support aggregates / alias conflict links (PostgreSQL)
  -> approved candidates create inactive tenant Catalog versions only

FieldSemanticIndexProjectionService
  -> field_semantic_index_versions / complete approved projection snapshots (PostgreSQL)
  -> FieldSemanticIndexStore (dedicated Milvus collection)

VectorMemoryStore (fallback only)
  -> tenant-scoped correction_memories / correction_memory_sources

ExampleIndexProjectionService
  -> ExampleIndexStore (optional independent Milvus Dense+Sparse derived index)
  -> example_index_projections (PostgreSQL-owned queue and lifecycle)

LangGraph Checkpointer
  -> separate SQLite file or separate PostgreSQL database
```

审核案例与字段语义索引均提供显式的 register/project/activate Application Service 操作和 HTTP
适配入口。投影每次只领取有界批次；激活继续由 PostgreSQL `ready_for_activation`/完整投影检查
门禁，不能只因“已登记”就切换 Milvus Alias。自动循环消费 Worker 仍不属于当前实现。

文件、Checkpoint 和业务数据物理分离。pgvector 回退由独立 Port、派生表和事务逻辑隔离，
与业务表共用 PostgreSQL database；审核案例索引由 `ExampleIndexStore` 隔离，生产使用独立
Milvus Dense+Sparse。`example_index_projections` 只保存投影状态、checksum 和错误码，
Milvus 数据可从 PostgreSQL 审核案例完全重建。需要独立故障域时必须另行拆库和迁移，不能只
更换 Port 实现而忽略 source-event 一致性。未启用配置时不会建立 Milvus 客户端。

字段语义索引同样以 PostgreSQL Catalog 审批事实、索引版本和投影状态为准；Milvus 不反向写回
目录。基础 display name、description 与 value type 始终由对应版本的不可变 Entity Schema 读取，
租户可变事实只存在 PostgreSQL。版本化 Collection 可重建、失效并通过租户专属 Alias 回滚。
`field_alias_candidates` 与来源、决策、冲突关联同样只存在 PostgreSQL；FieldSemanticIndex 构建仍
只读取 Catalog 中 `approved + is_valid` 的正式 tenant aliases，pending/global 候选没有投影路径。

## Document ingestion and vision extraction

```text
HTTP upload
  -> DocumentIngestionService
  -> idempotency claim
  -> DocumentProcessor.inspect
  -> FileStorage.save(original bytes)
  -> DocumentReferenceRepository.save_document
  -> DocumentReference (four fields only)

DocumentReference + concrete InvoiceExtraction type
  -> checksum verification
  -> DocumentProcessor.to_vision_images
  -> approved versioned FieldSemanticCatalog prompt projection
  -> VisionExtractionProvider baseline
  -> FieldBindingEvidence observations
  -> optional FieldSemanticBindingService
  -> filtered reviewed-example retrieval or pgvector fallback
  -> optional VisionExtractionProvider dynamic Few-shot pass
  -> ExtractionResult[InvoiceExtraction]
```

`DocumentProcessor` 在 Vision 调用前执行统一的输入质量链：校正 EXIF 方向，转换为 RGB/PNG，
按最大边长和总像素数裁限，并在开启时使用轻度自动对比度与锐化。PDF 页面先以配置的 DPI
渲染，再经过相同处理。原始上传字节保持不变；处理后的临时图像只存在于单次 Provider 请求，
不会写入 GraphState 或业务表。每页的尺寸和确定性清晰度分数作为验证信号保存，便于区分图像
质量问题与远程模型超时/Schema 错误。

`GraphState` 中的文档只由 `document_id`、`storage_uri`、`mime_type`、`checksum` 引用。
State 还保存三种身份标识、WorkflowStatus、结构化提取/审核元数据和幂等标记，但不保存
文件字节。图片 Base64 只在 OpenAI Adapter 发起单次请求时临时生成，不进入 Workflow
State 或业务持久化。

OpenAI Adapter 使用官方 Responses API：`responses.parse(..., text_format=Schema)`。动态响应
Envelope 将业务结果、字段证据和异常分离；调用方业务 Schema 的每一层 object 必须
`extra="forbid"`，所有字段必须 required，缺失语义使用 required nullable 字段表达。
OpenAI 与 Qwen Adapter 使用同一 Prompt v2 和本地响应契约。system、user instruction、强制规则、
JSON Schema 与 retry instruction 独立组装；非空 invoice 的 Schema 叶子字段与 evidence 路径必须
一一对应。Pydantic missing/extra/type 和 coverage missing/extra/duplicate 被归类为不含字段值的
结构化诊断，只有这些脱敏诊断可进入有限 Schema retry；完整远程响应体不会记录或回传。

`VisionPromptRegistry` 位于 Application Port，默认 `LocalVisionPromptRegistry`。可选
`LangSmithVisionPromptRegistry` 只通过注入的 puller 拉取指定版本的静态 Prompt 文本，版本不匹配或
远程失败时回退本地模板；图片、Base64、发票值、历史上下文和响应体不进入 Registry。当前未把
LangSmith SDK 或 PromptGenius 加入运行时依赖。

baseline 结果只提供当前文档可观察事实。生产案例检索先按租户和完整 Scope 过滤，再分别召回
三类人工审核案例；只有已审核、有效且达到阈值的少量记录进入第二次 Vision 调用。无匹配时不
进行第二次调用。pgvector 回退同样先按租户、`document_type`、`field_path`、Schema 过滤，再
联结对应 `ReviewedExample` 与 `MemoryAdmissionRecord`，只允许 `approved`、已审核且有效的
案例按 cosine similarity 排序返回。

`CorrectionEvent` 保存模型值、人工值、原因、vendor/template 特征、文档/页面引用、Schema
版本、时间和审核/有效标记。原始事件留在 PostgreSQL；进入 Embedding、派生向量 JSON 和
Prompt 的副本先经过 `SensitiveDataRedactor`。重复 fingerprint 共享一条 memory，独立 source
表防止同一事件重放重复计数。`VectorMemoryStore` 还提供 disable 和 Schema 版本失效操作。

Prompt 明确分为 `CURRENT_IMAGE_FACTS`、`VERIFIED_CORRECT_EXAMPLES`、
`REVIEWED_CORRECTION_EXAMPLES`、`REVIEWED_NEGATIVE_EXAMPLES` 和
`MANDATORY_BUSINESS_RULES`。历史内容按不可信数据处理，不能充当指令；负例不能进入正确示例
区。历史记录只能提示既往模式，任何历史值都不能覆盖当前图片证据。该边界不是开放式 Agent
Memory，也不允许模型自行选择工具或路由。

具体 `InvoiceExtraction` 是固定的 19 字段对象。Provider 要求 evidence 唯一且完整覆盖当前
Schema 的字段路径，不得增加 VAT 扩展字段或其他未声明字段。

## HTTP workflow boundary

```text
POST /documents + Idempotency-Key
  -> DocumentReference
  -> POST /documents/{document_id}/extract
  -> completed | pending_review | failed

pending_review
  -> POST /reviews/{run_id} + Idempotency-Key
  -> ExtractionWorkflowService
  -> Command(resume=HumanCorrection)

GET /runs/{run_id}
GET /runs/{run_id}/result
GET /reviews/{run_id}
  -> SQLAlchemy business read models
```

API 生成 `thread_id` 与 `run_id`；客户端启动时只提供上传产生的 `document_id`。可信的
`storage_uri`、MIME 和 checksum 从 application document registry 读取，不接受客户端回传。
`thread_id` 不对外作为业务查询键。Router 只调用 application services；只有
`ExtractionWorkflowService` 通过 `WorkflowExecutionGateway` 启动或恢复 Graph。查询读取
业务 Repository，不读取 Checkpoint。400、403、404、409、422、429、500、503 使用稳定错误
Schema。

## Operational boundary

所有业务 Router 只调用 Application Service。租户和 Reviewer 必须来自可信认证或网关中间件
注入的 `request.state.trusted_tenant_context`；Body、Path 和普通 Header 均不能指定
`tenant_id`。`/memory/*` 公开治理能力包括准入分页/详情/人工决策、案例分页/详情、案例禁用、
Schema 失效、索引重建登记、索引状态、人工检索反馈和离线评估查询；`/field-semantics/*`
公开字段目录、租户别名候选、字段冲突查询/解决以及别名审批/禁用。所有写操作要求
`Idempotency-Key`；准入与别名状态写请求还必须提交当前 `expected_revision`。PostgreSQL 不可变
决策记录保存 Reviewer、Reason、Policy/Catalog Version、前后 Revision 和审计时间。

### Unified governance audit

```text
trusted request context(trace_id optional)
  -> FastAPI dependency generates a server trace when absent
  -> MemoryGovernanceService builds sensitive-value-free GovernanceAuditEvent
  -> authoritative repository transaction
       -> state CAS / immutable domain decision
       -> memory_governance_audits append
  -> response(audit_id, resource_version, trace_id)
```

统一审计动作覆盖 Admission approve/reject/quarantine、Field Alias approve/disable、Conflict
resolve/dismiss、Example disable、Schema invalidate、Index rebuild 和 Retrieval Feedback。Trace
只能来自可信上下文或服务端生成，不读取客户端自定义关联 Header；相同 Idempotency-Key 重放读取
原审计并返回原 `audit_id + trace_id`。Admission/Alias 的 `resource_version` 是提交后的 revision，
Conflict 使用稳定 `resolution_decision_id`，Index/Schema 使用对应版本；没有明确治理 revision 的
Example 与 Feedback 保持 `null`。迁移前历史行的 `resource_version/trace_id` 同样保持 `null`，
禁止猜测回填。

若进程在权威状态与 Audit 提交后、Idempotency 响应落库前退出，重放会以已存在 Audit 恢复响应并
将幂等记录补为 completed，不再次执行治理状态变更或追加第二条 Audit。

Audit 只保存治理元数据：reason 在写入前拒绝图片/Base64，并脱敏明显邮箱、电话和长编号；API
不返回 Idempotency-Key 哈希、原始请求 payload、发票值、Prompt、向量或带签名的图片引用。

Admission、Alias、Conflict、Example、Schema、Index 和 Feedback 的权威 PostgreSQL 写入与对应
Audit append 共享同一 Repository 事务。Alias 审批在最终 Candidate CAS 之前可能幂等准备不可见
Draft Catalog，但只有 Candidate 决策与 Audit 原子提交后才构成完成的治理动作。Milvus 删除、投影
和 Alias 切换不加入 PostgreSQL 事务；派生索引失败不能回滚、删除或改写审计事实。

`GET /api/v1/memory/audits` 始终显式过滤 tenant，并可按 operation、resource type/ID、Trace 和
时间范围筛选。返回仅含 actor、reason、resource identity/version、trace 和时间，不返回
Idempotency-Key hash、请求 payload、发票值、Prompt、图片签名参数或向量。

别名治理的处理顺序是先按 candidate 与 Idempotency-Key hash 查找历史 Decision，再判断
`expected_revision`。相同 Key 与相同语义返回原 Decision 快照；相同 Key 绑定不同 reason、动作、
Reviewer、Policy Version 或 expected revision 时返回 409。没有 replay 时才执行状态校验和 CAS。
租户审批 draft Catalog Version 使用 `candidate_id + target revision` 生成稳定标识；Catalog 注册、
alias 填充及 Decision 均幂等，同一候选 revision 的并发审批最多对应一个 Catalog Version。

人工准入批准由 `MemoryGovernanceService` 强制执行二级门禁：当前确定性评估必须存在且没有
failed 信号，ReviewedExample 必须保持 `is_reviewed=true/is_valid=true`，并且不存在开放冲突。
`MEMORY_ADMISSION_REQUIRE_DISTINCT_SECOND_REVIEWER=true` 时，治理 Actor 不能等于原审核人；
租户别名审批同样不能由任何来源映射 Reviewer 完成。Router 只调用该 Application Service，
不会直接访问 ORM、Milvus、Graph 或模型。

### Conflict resolution boundary

`MemoryGovernanceService` 是冲突关闭的唯一应用入口，Router 只传递可信上下文、冲突 ID、
`expected_status=open`、治理原因和可选脱敏备注。Reviewer 始终取
`TrustedTenantContext.actor_id`，跨租户查询返回 404。字段映射冲突的 `resolve` 只能选择冲突已有
候选且当前 Schema 声明的 canonical field path；`dismiss` 不接受字段选择。

```text
POST resolve | dismiss + Idempotency-Key
  -> tenant-scoped conflict read
  -> current Schema/candidate validation
  -> SELECT ... FOR UPDATE memory_conflicts
  -> immutable memory_conflict_resolution_decisions insert
  -> pending memory_conflict_reevaluation_requests insert
  -> open -> resolved | dismissed
```

上述写入位于同一 PostgreSQL 事务。`tenant_id + idempotency_key_hash` 唯一约束保证重放；相同 Key
和相同语义返回原决定，不同语义、并发抢先关闭或 expected status 失配返回 409。解决决定仅解除
开放冲突 blocker，并为关联 `memory_admission`/`field_alias` 登记 pending 重新评估目标；不会修改
`CorrectionEvent`、`ReviewedExample`、`FieldAliasCandidate` 或发票 Entity，也不会直接写 approved。
Milvus 不参与事务。现有短事务 Worker 幂等消费重新评估请求：仅将明确因该冲突隔离的
记忆案例重新入队，其他隔离原因和别名候选进入人工复核，不自动批准。

审核案例索引由 `ExampleIndexProjectionService` 按数据库队列批量投影。单条状态为
`pending -> processing -> indexed`，脱敏、embedding 或索引写入失败转为 `failed`，租约超时
可重新排队；案例禁用或 Schema 失效转为 `invalidated`。服务先写派生索引，再以稳定 checksum
确认 PostgreSQL 状态，重放只执行幂等 upsert，不增加案例计数，也不修改原始审核事实。

单案例投影可观察性由 `MemoryGovernanceService` 提供，只读查询顺序固定为 tenant-scoped
ReviewedExample 归属校验、MemoryAdmission 状态读取、`example_index_projections` 与
`index_versions` 联表分页。Repository 的基础条件始终包含 `tenant_id + example_id`，跨租户 ID
表现为 404。响应不包含向量、Sparse 词项、索引文本、原始值或异常详情；历史异常码再次经过安全
字符校验。Admission 非 approved 时仍可查看历史/清理投影，但明确标记当前不可参与长期检索。

`indexed` 的唯一语义是 PostgreSQL 已记录一次成功的派生 Upsert；查询路径不调用 Milvus，也不能
证明实体仍存在、Collection 已加载、Alias 已切换或 Milvus 当前健康。两类投影表均持久化
`lease_expires_at`；只有当前领取状态才返回租约时间，其余状态返回 `null`。

Milvus Adapter 使用版本化 Collection；普通租户共享 Collection，`tenant_id` 是 Partition
Key，并在每个 AnnSearchRequest 中应用 `tenant_id`、文档类型、字段路径、Schema 版本、
`is_reviewed`、`is_valid` 和
`index_version` Scalar Filter，禁止先全库搜索再过滤。Dense 使用 HNSW/COSINE，Sparse
使用 `SPARSE_INVERTED_INDEX`，默认由 BM25 Function 生成稀疏向量；融合支持 WeightedRanker
和 RRF（通过 Milvus 2.6+ 官方 `FunctionType.RERANK`）。Upsert 按 `example_id` 幂等，
删除使用主键或租户+版本过滤，不使用 `item_name`。
新版本 Collection 创建并加载后才切换 Alias；高合规租户可通过 Resolver 使用独立 Collection。
启用 Milvus 时配置层拒绝 `none` 脱敏策略，默认及租户覆盖只能选择 `mask`、`hash` 或 `drop`。

字段语义投影使用相同状态机，但独立保存于 `field_semantic_index_projections`。绑定搜索前强制应用
`tenant_id + document_type + schema_version + catalog_version + index_version + is_valid` Scalar
Filter；主键为稳定 `semantic_id`，重放只执行幂等 Upsert。活动版本禁止原地重建；新版本完整
投影后切换租户专属 Alias，再由 PostgreSQL 标记活动版本。重新激活旧的有效版本即为回滚。
版本登记时，Schema 基础定义与该租户已审批覆盖会固化为 PostgreSQL 投影快照；后续重试、重建
均从该快照生成向量，目录变化必须创建新版本，Milvus 不承担目录事实。

别名学习与发票值修正共享人工审核入口，但使用独立生命周期。人工字段映射提交只原子写入候选与
单个支持来源；仅实际插入新来源时原子增加候选 revision，冲突记录单独幂等写入，失败可由相同审核重放补齐。租户审批分阶段创建未激活 Catalog
及其已审批 alias，最后使用 expected revision 提交候选 CAS 审批决定；中途失败只留下不可见 draft，可用相同 Idempotency Key
恢复。别名禁用或 Catalog 失效先使 PostgreSQL 活动版本不可检索，再尝试删除 Milvus 派生数据，
因此外部索引失败不会重新授权失效别名。

每次字段级 Hybrid Retrieval 生成独立 `trace_id`，`retrieval_traces` 只保存 Scope、案例 ID、
Dense/Sparse/Rerank 候选数、各阶段耗时、Empty Retrieval、正负案例返回比例、首次
Review Required 路由和 index/model/Prompt/threshold 版本，不保存原始字段值、Base64 或完整
Prompt。千问响应存在官方 `usage` 时记录 Token；调用成本只按运维人员显式配置的当前费率
计算，未配置时保持 `null`，禁止猜测价格。`memory_retrieval_feedback` 保存可归因的人工相关性
判断，但不会直接改变案例标签或 `InvoiceExtraction`。

## Reviewed-example lifecycle

```text
explicit human field action
  -> confirmed_correct | corrected | confirmed_incorrect
  -> review-fact transaction
     -> human_corrections + correction_events + memory_review_recoveries(pending)
  -> persist_result(run_id write-once)
  -> recovery materialization
  -> PostgreSQL reviewed_examples + immutable source event/feedback
  -> memory_admission_records(pending)
  -> deterministic quality assessment
  -> optional model advisory + Python admission policy
  -> approved | quarantined | rejected
  -> approved only
  -> example_index_projections(pending)
  -> tenant redaction(mask | hash | drop; none is non-Milvus development fallback only)
  -> Dense embedding + Milvus BM25 projection
  -> Milvus idempotent upsert
  -> PostgreSQL projection state(indexed | failed | invalidated)
```

发票 Workflow 的审核节点只调用 `record_review_facts`，在单个 PostgreSQL 事务中保存
HumanCorrection、CorrectionEvent 与稳定 `memory_review_recoveries` Outbox。发票结果由后续
`persist_result` 按 `run_id` write-once；`save_correction_memory` 先提交 Run `completed`，再尝试
幂等物化 ReviewedExample/pending admission。物化失败只写
`failed_retryable + memory_error_code + memory_trace_id`，不会把 Run 改为 `failed`。质量评估、
模型建议、Policy 决策和索引协调由独立 Memory
Admission Worker 在 Workflow 外调用 `process_admission`；其失败不会改变已持久化的发票结果或
Workflow 完成状态。FastAPI 主进程不运行该后台循环。

### Memory Admission Worker

Worker 首先从 `memory_review_recoveries` 领取到期恢复任务。恢复记录绑定 tenant/run/document、
稳定 correction/event IDs 和 PostgreSQL 内的原始/审核后 ExtractionResult 快照，不保存图片
Base64。Worker 仅在最终结果存在时生成案例，随后按 fingerprint/replay key 幂等合并并创建
pending admission。恢复 Outbox 的不可恢复错误或重试耗尽后保留终态 `failed_retryable` 且停止
自动领取，供治理排查；已经创建的准入记录达到最大尝试次数则由准入 Service 写入可审计的
`quarantined` 决策。

`memory_admission_records` 同时保存准入当前状态和 PostgreSQL 队列控制字段。Worker 在短事务内以
`SELECT ... FOR UPDATE SKIP LOCKED` 领取 `status='pending'`、`next_attempt_at <= now` 且未租用或租约已过期的记录，
原子写入 `worker_id + lease_token + lease_expires_at` 并增加 `attempt_count`。完成、重试释放只能在
tenant、example、worker 和 lease token 全部匹配时更新，过期 Worker 因此不能覆盖新租约。

```text
memory_admission_records(next_attempt_at due)
  -> PostgreSQL atomic claim + lease
  -> ReviewedExample + optional CorrectionEvent + document/run/result/evidence ownership
  -> DeterministicMemoryQualityValidator
  -> hard failure: rejected | quarantined
  -> optional MemoryQualityAssessmentProvider (advisory only)
  -> DeterministicMemoryAdmissionPolicy
  -> immutable assessments/signals/decision + current status
  -> approved: ExampleIndexProjectionService.schedule_approved_example
  -> quarantined/rejected/suspended/invalidated: projection invalidation/cleanup coordination
```

相同 example、Policy、assessment source、输入 fingerprint 的评估由 PostgreSQL 唯一约束去重；
相同模型与 Prompt 版本的已保存 advisory 会直接复用。准入阶段只读取案例；前置恢复阶段按稳定
source feedback/replay key 幂等 upsert，因此 Worker 重启、Workflow 恢复和租约超时都不会重复
增加 `occurrence_count`。永久 Scope/Schema/
tenant/证据事实错误不重试；远程网络、429/5xx、存储暂时不可用和投影协调失败按配置指数退避。
达到最大次数由 Application Service 写入可审计 `quarantined` 决策。隔离态只能由人工治理再次
决策，Worker 不会自动把它改回 `approved`；已批准记录若持续无法登记投影，也会安全转入隔离，
避免无法协调派生索引的案例继续保持可用状态。

Worker 的职责到单案例准入与幂等投影/清理登记为止。实际 projection queue 消费、Collection
构建、Alias 切换和全量 PostgreSQL -> Milvus 重建属于独立索引生命周期，不由该 Worker 执行。

### 事务与失败恢复边界

1. 审核事实事务：HumanCorrection、CorrectionEvent、Memory Recovery Outbox 原子提交；任一失败
   时审核任务保持 `pending_review`，客户端使用相同 Idempotency-Key 重试。
2. 发票结果事务：`extraction_results.run_id` write-once；成功后记忆子系统无权删除、覆盖或把
   Run 改为 `failed`。
3. 记忆候选/准入事务：Outbox 物化 ReviewedExample 与 pending admission，失败独立重试。
4. Milvus 派生投影事务：仅消费 PostgreSQL `approved` 案例，失败只更新投影状态。

PostgreSQL 与 Milvus 不使用分布式事务。最终一致性由 Recovery Outbox、admission queue 和
projection queue 串联实现；PostgreSQL 始终是可完整重建 Milvus 的唯一事实源。

备份恢复同样遵循该边界：业务 PostgreSQL、独立 Checkpointer、对象存储和（如启用）Keycloak
数据库分别备份；Milvus/etcd 只作为可丢弃派生数据。恢复时先校验 PostgreSQL 事实和对象
checksum，再在新 Collection 中完整 project/verify，最后由授权 API 显式切换 Alias。任一步骤失败
都保留旧 Alias，不执行原地重建或破坏性卷清理。隔离演练入口见
[`docs/backup-restore-drill.md`](backup-restore-drill.md)。

开发/迁移环境的 pgvector 回退不构成第二事实源。它的查询通过
`correction_memory_sources -> reviewed_examples -> memory_admission_records` 关联，并显式要求
`memory_admission_records.status='approved'`、案例 `is_reviewed=true` 且 `is_valid=true`；因此
pending、quarantined、suspended、rejected 或 invalidated 案例不会因 Milvus 不可用而进入 Few-shot
上下文。新代码不得把 pgvector 与 Milvus 作为生产双查询路径。

失效、暂停、拒绝和回滚均先追加 PostgreSQL 状态/审计事实，再执行 Milvus 删除或投影清理。删除
操作是幂等的，Milvus 清理失败不会回滚 PostgreSQL；后续由投影治理任务按 PostgreSQL 状态重试。
租户物理删除和超期 purge 是受合规授权的不可逆保留策略，不属于准入失败恢复路径；在执行前必须
确认原始审核事实的保留要求与适用的法定删除义务。

没有修改字段不构成确认。`confirmed_correct` 必须有显式确认；`corrected` 同时保存模型错误值、
人工值和原因；`confirmed_incorrect` 保存被否定值和原因，不强制正确值。每条案例绑定租户、
document/run、字段证据、页面引用、Schema/Model/Prompt 版本和 Reviewer。fingerprint 合并重复
模式，但 source event 单独保留且 Workflow 重放不增加 occurrence count。

PostgreSQL 是案例、标签、版本、审计与投影状态的唯一事实源。Milvus 只接收脱敏文本、向量、
fingerprint 和文件/页面引用，不接收原始 JSON 值、图片 bytes 或 Base64。禁用案例、Schema
失效和租户删除先改变 PostgreSQL 状态，再产生索引失效/删除操作；全量重建只遍历有效、已审核且
准入状态为 `approved` 的事实，因此不依赖 Milvus 反向恢复。

## Hybrid retrieval and Dynamic Few-shot

每个字段分别检索 `confirmed_correct`、`corrected`、`confirmed_incorrect`。每类执行 Dense +
Sparse Hybrid，使用 versioned weighted/RRF 融合，按 `example_id` 去重并保留召回来源，再由
Reranker 排序和阈值裁剪。`confirmed_incorrect` 只进入 Hard Negative 区；正例与纠错例进入
独立正向区域。Dense、Sparse、fusion、rerank 分数均为相关性信号，不是概率；除非未来增加
显式校准器，否则 API 不输出 `probability`。

Retrieval 失败返回空上下文，Workflow 继续使用 baseline 图片事实；案例冲突给相关字段追加
歧义并进入人工审核。当前图片不可读时禁止从历史值自动补齐。GraphState 仅保存案例 ID、脱敏
摘要、trace/index/policy 引用，不保存完整 Milvus 记录。

上下文编译器使用固定优先级：当前图片事实、强制 Schema/业务规则、当前有效字段语义目录、
已审批正确案例、已审批纠错案例、已审批负例。目录和历史数据均以不可信数据区块包裹，
其中的 OCR、用户备注和案例文本不得改变系统规则。`PromptContextBudget` 由配置控制各区块
案例数、目录定义数、单区及总字符预算；超限时按优先级丢弃低优先级历史区块并保持合法 JSON。
OpenAI 与 Qwen 共用同一编译器；检索、Embedding、Reranker 或编译失败均 fail-closed，保留
当前图片事实而不自动填充。状态中只保留裁剪后的脱敏引用、版本和 trace。

## Evaluation boundary

离线评估复用一个 `OfflineEvaluationService`，由 `EvaluationSuite` 选择固定契约：

- `case_rag`：无记忆、pgvector、Dense-only、Sparse-only、Hybrid、Hybrid+Reranker、Hybrid+
  正负 Few-shot。指标包含 Recall@K、HitRate@K、MRR、nDCG@K、正负分离、空召回率、字段准确率、
  缺失识别、候选命中、错误自动填充数和 Review Required Precision/Recall。
- `trusted_memory_field_binding`：无字段目录、仅静态描述、描述+已审批别名、Dense-only、
  Sparse-only、Hybrid、Hybrid+Reranker、Hybrid+上下文锚点。指标包含 Memory Approval
  Precision、Harmful Memory Admission Rate、Quarantine Rate、Reviewer Disagreement Rate、
  Alias Binding Accuracy、Top-K Field Recall、Field Binding Ambiguity Rate、Wrong-field
  Auto-fill Count、Memory Helpfulness Rate 和 Misleading Retrieval Rate。

可信评估 Dataset 只接收人工裁决 Ground Truth：记忆准入标签保存最终 adjudicator 与多个初审
judgment，字段绑定标签保存允许的 canonical field paths 和预期绑定状态，记忆效果标签明确区分
helpful/misleading。Variant Observation 只保存准入状态、排序后的字段候选、是否发生自动填充及
案例引用，不保存图片或完整 Prompt。Memory Approval Precision 的分母是实际批准数；Harmful
Memory Admission Rate 的分母是人工标记 harmful 的案例数；Helpfulness Rate 的分母是存在
helpful Ground Truth 的检索机会；Misleading Retrieval Rate 的分母是具有效果标注的检索案例。
所有指标保存显式计数，零分母输出 `null`，任何模型或召回分数均不解释为 probability。

结果按 document type、field path、vendor/template 与图片质量分桶，并绑定 tenant、dataset、
Schema、Catalog、index、model、Prompt、准入 Policy、字段绑定 Policy、retrieval Policy 与
threshold 版本。`evaluation_datasets` 和 `evaluation_runs` 仍是 PostgreSQL 唯一事实源，扩展契约
写入原 JSON 列，因此不新增 migration；旧 `case_rag` JSON 缺少新字段时按兼容默认值读取。
Dataset Repository 在读取、写入及 Evaluation Run 绑定时同时校验列元数据与不可变 JSON
payload 的 tenant、dataset、version 和 schema 绑定；任一漂移均拒绝继续形成评估事实。

文档状态由 `docs/project-status.md` 统一维护。可使用 `scripts/check-docs.ps1` 对核心 Markdown
执行本地链接和已知过时状态声明检查；该脚本尚未接入远程 CI，也不替代代码、迁移或生产部署验收。

训练、验证、评估按 `document_id` 分组隔离，同模板近重复不得跨集合。评估只能创建
Promotion Candidate，可信套件的 Gate 至少阻止 harmful admission、wrong-field auto-fill 和
misleading retrieval 回归；候选仍需人工审批，不能修改生产配置。当前已完成隔离 SQLite/Stub
任务队列与报告边界回归，但未执行真实 PostgreSQL 并发评估或生产数据评估。

```text
frozen human-adjudicated EvaluationDataset (PostgreSQL)
  -> tenant-scoped Suite Job with immutable dataset/Suite/version binding
  -> runner unavailable: quarantined without diagnostic fallback
  -> configured HTTPS isolated Runner Adapter (optional Worker wiring)
  -> separate ASGI evaluation service + tenant-scoped read-only evidence snapshot
  -> fixed Suite variants through EvaluationVariantRunner Ports
  -> deterministic aggregate and bucket metrics
  -> immutable EvaluationRun (PostgreSQL)
  -> derived JSON + Markdown reports
  -> Promotion Candidate only (no production mutation)
```

`EvaluationDataset` 的训练文档集合与评估文档集合必须互斥，训练侧及每个评估案例均须具有
模板指纹且两侧指纹集合互斥。缺失指纹不是隔离成功的证据；读取旧数据和晋升证据校验均须
fail closed。
Suite Job 与旧 Snapshot 诊断 Job 共用 PostgreSQL 队列表；`evaluation_jobs` 中
`snapshot_id` 与冻结数据集外键互斥，Suite/检索 Policy 与模型、Prompt、索引、阈值版本随 Job
固化。Application Service 在注入完整 Runner 后为每次领取生成独立 Run ID、续租并调用
`OfflineEvaluationService`；Repository 只有在当前租约仍有效、Run 已完成且数据集/Suite/版本/
报告产物一致时，才能原子地确认 Job→Run。JSON/Markdown 报告保存于 PostgreSQL
`evaluation_report_artifacts`，以不可变 SHA-256 和 Run/tenant/schema 绑定校验；晋升门禁重载该确认链，拒绝失去租约的迟到 Run。
Suite 运行超过总时限时取消当前执行，并用固定错误码进入有限重试；未确认的 Run 不具备晋升资格。
Worker 已通过 `bootstrap.py` 选择性装配 HTTPS 隔离 Runner Adapter，缺少 endpoint 或 token
file 时 fail closed，不调用 Stub。Port 接收冻结 Dataset、Case 与版本绑定；Adapter
只发送 Suite/变体、训练/评估文档与模板清单、证据引用及版本，省略 Ground Truth 和 Reviewer。
服务拒绝不属于所选 Suite 的变体。响应须回显
请求与隔离清单的 SHA-256，并由 Pydantic/Domain 校验 Observation 身份及结构；Application
Service 继续校验召回来源属于训练集。聚合报告写入 PostgreSQL `evaluation_report_artifacts`；
独立服务通过 token 文件、冻结清单 SHA-256、案例与租户绑定及租户目录中的证据 SHA-256 校验，
在启动时要求全部变体引擎；引擎以 Port 接收脱敏证据 bytes，不接收 Ground Truth。服务不连接业务
PostgreSQL，也不写报告或晋升事实。隔离部署须提供真实只读引擎、证据快照和 TLS；Compose 未提供
这些资源或 token。
真实变体执行、PostgreSQL 双 Worker 并发、崩溃恢复和真实报告发布演练仍待完成；
本地隔离测试只证明门禁逻辑，不代表真实 Compose 或外部服务已验收。

## Training, promotion and rollback

Hard Negative Mining 只把人工审核标签作为最终标签；LLM 建议不能自行批准。导出数据在租户内
脱敏，默认禁止跨租户训练，且按 document/template 分组切分，版本绑定 Dataset、Schema 与生成
规则。在线请求不能更新模型权重。

训练编排与 FastAPI 主进程隔离，优先级为 Reranker、Embedding、最后才是 Vision/生成模型。
独立 `TrainingJob` 使用 `planned/submitted/running/succeeded/failed/cancelled/quarantined` 七状态，
绑定 tenant、dataset/schema/index/model/prompt version，并一对一关联保留的 `TrainingRun`。API 只登记、
查询、请求取消或创建 retry；独立 Worker 通过 PostgreSQL claim/lease 调用 Provider。默认
deterministic stub 明确拒绝提交，MLflow-compatible Adapter 只有在显式配置 HTTPS endpoint 与凭据
后才可调用，且不要求 MLflow 服务作为业务依赖或事实源。
Training Job 的创建、读取、取消和重试均由统一安全中间件要求 `training:submit`；Repository
以可信租户和 Job ID 联合查询，跨租户返回 404，拒绝与模糊 404 均写安全审计。

`TrainingDatasetExport` 通过一对一外键扩展现有 `TrainingDatasetVersion`。导出仍只读取已审核、有效、
`admission_status=approved` 的案例与已审批 Hard Negative；`confirmed_incorrect` 只进入负例。远程成功
必须完成受控 artifact 下载与 SHA-256 校验，才在同一 PostgreSQL 事务中推进 Job/Run 并登记
Training Artifact 和既有 Model Artifact。LangSmith 只可提供不可变 Prompt Registry 引用，
PostgreSQL 仍保存版本绑定和审计事实。`RegistryOnlyDeploymentController` 继续拒绝实际流量变更。
晋升顺序固定为 offline evaluation -> shadow -> canary -> human approval -> production，禁止跳级，
Production 必须有 Reviewer。主指标下降、Review Required Rate 上升或错误自动填充增加会拒绝
晋升并允许回滚到前一个 Production deployment；没有任何自动晋升 Production 的路径。

## Provider compatibility

千问 Adapter 于 2026-09-01 对照官方当前文档核验：Vision 和 Query Rewrite 使用
OpenAI-compatible `chat.completions.parse` + Pydantic JSON Schema，Dense Embedding 使用
`embeddings.create`，Rerank 使用独立 `compatible-api/v1/reranks` 的 `client.post`。默认
`qwen3.8-max`、`qwen3.7-text-embedding`、`qwen3-rerank`、`qwen3.8-flash` 均为当前官方模型。
调用统一经过超时、并发限制、速率限制、指数退避、熔断、脱敏与无敏感 payload 审计。

Milvus Adapter 对照当前 v3.0.x 文档，使用 `AnnSearchRequest`、BM25 Function、
`SPARSE_INVERTED_INDEX`、HNSW/COSINE、`FunctionType.RERANK`、Scalar Filter、Partition Key 和
Alias。实现保持 `pymilvus>=2.6,<3.0` 约束；升级 Client major version 前必须重新执行兼容审计。

官方依据：

- [千问结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)
- [千问 Embedding](https://help.aliyun.com/zh/model-studio/embedding)
- [千问 Rerank](https://help.aliyun.com/zh/model-studio/rerank)
- [Milvus Hybrid Search](https://milvus.io/docs/zh/multi-vector-search.md)
- [Milvus Partition Key](https://milvus.io/docs/zh/use-partition-key.md)
- [Milvus Alias](https://milvus.io/docs/zh/manage-aliases.md)

## Unified Trace and privacy telemetry

### 开发诊断包

`configure_logging` 在 development 默认同时配置标准输出与按组件/PID 隔离的本地 JSONL
`RotatingFileHandler`。日志目录默认是工作目录下的 `logs/`，启动时创建；Compose 将 `/app/logs`
绑定到项目 `./logs`。文件大小、轮转份数和旧文件保留天数由 Settings 控制，清理仅在进程启动时
执行且跳过仍在运行的 PID。production 默认仅输出标准输出，部署方可显式启用文件输出。
当前 Compose 显式开启文件输出并绑定宿主机目录，可用 `LOG_FILE_ENABLED=false` 关闭。
文件输出复用现有字段白名单和 JSON formatter；它不是 PostgreSQL 审计事实源。
应用日志时间戳以 `+08:00` 上海时间输出，诊断器按 ISO 8601 时区偏移比较时间；
API 异常处理器记录的 5xx 仅附加可信 Trace/租户、HTTP 方法与固定路由模板、状态码、耗时、包装和驱动异常类型、
连接失效标记及合法 SQLSTATE；安全审计持久化失败使用同一低敏分类。日志和诊断包均不输出
SQL 语句、参数或异常正文。422 只附加错误数量和首个错误类型。SQLSTATE 缺失不等于依赖正常；
旧日志不会回填。
Compose PostgreSQL 仅在内部网络提供 5432；主机进程不能用 `localhost:5432` 访问该容器，
除非运维明确配置仅本机可见的端口映射。

`invoice_intelligence.diagnostics` 是本地只读诊断入口。它从单个有大小上限的 JSONL 日志文件中
或项目 `logs/` 下各组件的主文件/轮转文件中按 Trace、租户和可选时间窗筛选事件，
只接受字段白名单和有界技术标识；不能把日志正文、
Prompt、异常正文或远程响应透传给开发 Agent。输出选择首个失败事件与邻近少量事件，并限制
序列化 JSON 的长度；提供 `--error-code` 时优先定位该错误，否则定位首个失败。
固定阶段到源码路径映射仅提供初始定位线索。
可选 PostgreSQL 读取仅查询同租户 Trace 的 Harness Task、最近 Attempt、Postmortem 和治理审计
技术事实；不访问发票值、Patch 内容或原始原因文本。该入口不更改业务事实、不向模型发送内容，
也不是外部 Trace Collector；缺少结构化日志文件或数据库连接时不会伪造诊断结果。

### 生命周期与传播

HTTP 边界忽略客户端自定义 Trace 输入，只从已类型化的 `TrustedTenantContext.trace_id` 继承；
没有受信任上游 Trace 时生成 32 字符随机 ID，并写入 `request.state.server_trace_id`。同一 ID
通过 Application Service 进入 `GraphState`，在恢复旧 Checkpoint 时使用 `run_id` 的不可逆技术
后缀作为稳定兼容值。响应统一返回 `X-Trace-ID`。`trace_id` 不进入 `InvoiceExtraction`，也不能
用于资源授权、租户查找或业务主键查询。

`PrivacyTelemetry` 是 Application Port，默认 Adapter 使用 `contextvars` 隔离并发请求，生成
嵌套 `span_id`/`parent_span_id`。它不依赖 OpenTelemetry SDK，也不传入 Prompt、发票对象、
图片、OCR 文本或向量。固定阶段为：

```text
ingestion
document_preprocessing
vision
ocr
retrieval
validation
human_review
memory_admission
field_semantic_binding
index_projection
governance_operation
background_recovery
```

Workflow 的节点名称、顺序和路由未改变。Vision Service 在同一根 Trace 下拆分预处理、Vision、
并发 OCR 和字段绑定；治理请求、案例/字段目录投影以及 Worker 恢复使用相同 Port。历史独立的
Retrieval/OCR 指标记录继续保留，不创建第二套事实表。

### 日志白名单与隐私边界

结构化日志只放行低敏元数据：`trace_id`、`span_id`、`parent_span_id`、`stage`、`operation`、
`outcome`、`duration_ms`、Provider/模型/配置版本、HTTP 状态、脱敏错误类型/错误码、候选/页面/
尝试计数，以及 tenant/run/document/example/index/recovery 等技术标识。第三方 Logger 的原始
消息统一替换为固定事件名；异常仅记录类型，不记录 traceback 或异常正文。禁止字段包括完整
发票值、OCR 原文、图片、Base64、完整 Prompt、API Key、向量、Idempotency-Key 原文、远程
响应体和 Chain-of-Thought。

### 审计与事务边界

`memory_governance_audits` 是不可变、tenant-scoped 的治理审计事实，已保存 `audit_id`、
`tenant_id`、`action`、actor/reviewer、脱敏 reason、资源类型/ID、`resource_version`、
`trace_id` 和时间。Admission、Alias、Conflict 等权威状态与对应审计由同一 PostgreSQL 事务
提交；幂等重放读取原 `audit_id` 和原 `trace_id`。历史 NULL 不回填。现有
`20260904_0019_governance_audit_trace.py` 已提供字段与 `(tenant_id, trace_id, created_at)` 索引，
本阶段无需 migration。

阶段 Span 是运行遥测，不是治理事实，不参与 PostgreSQL/Milvus 分布式事务。Milvus 投影失败
不会删除审计或回滚发票结果。PostgreSQL 中 `indexed` 只证明一次投影成功记录，不能证明当前
Milvus 实体存在、Collection 已加载、Alias 已切换或远程 Collector 已收到 Span。

## Capability allowlist and untrusted-content boundary

### 静态能力矩阵

`application/ports/capabilities.py` 定义正向 `ProviderCapability` 和只读
`ProviderCapabilityManifest`；`infrastructure/security/capabilities.py` 保存内建 Adapter 类型到能力
声明的不可变映射。`bootstrap.py` 只对代码内建实例执行 fail-closed 绑定校验。该映射不是运行时
Tool Registry，不接受配置、请求或 Prompt 增删能力；显式注入的测试/组合根替身属于受信任代码
边界，不能由 API 创建。

| 边界 | 可执行 | 不可执行 |
|---|---|---|
| Vision Port | 图片到严格 Extraction envelope | 审批、持久化、路由、Schema/Tenant 修改 |
| Raw OCR Port | 图片到未绑定文本 observation | 写 canonical 字段、确认业务真值 |
| Embedding/Rerank/Rewrite Port | 已脱敏检索表示与排序建议 | Scope Filter 修改、历史值自动填充 |
| Memory Quality Port | 结构化审批建议 | approved 决策、硬失败覆盖 |
| Milvus Store Port | approved 脱敏投影的读写/删除 | 保存事实、接收未审批原值 |

Provider 构造函数只得到远程 Client、Payload Guard、模型配置或派生索引配置，不得到治理
Repository、业务数据库 Session、LangGraph Router、Reviewer 或 Tenant 变更能力。Router 依赖
Application Service；Application Port 不暴露 ORM、Milvus Client、LangGraph 或模型 SDK；Domain
保持 Framework-independent。

### Prompt Injection 防护

所有图片文字、OCR 文本、备注、历史案例、字段别名/描述和外部文本均为 data-only。系统指令固定
声明当前图片证据优先级，以及 Schema、Tenant、Reviewer、Policy、Workflow 和检索过滤条件不可由
数据区修改。`PromptDataSection` 只允许字段目录、已审核正确案例、纠错案例和负例四种历史区块；
编译器使用 JSON envelope，写入 `trusted_instructions=false`，限制单区/总字符数，拒绝 inline
Base64，并中和伪造的区块边界标记。负例保持独立区，不进入正例区。

Provider 输出只能进入固定 Pydantic response envelope，`extra="forbid"`，再经过
`InvoiceExtraction` 与证据归属 Domain 校验。模型自报 confidence、隐藏推理和历史值都不获得决策
权；图片不可读、字段冲突或 Schema 无法真实满足时返回缺失/歧义并进入人工审核。

### 文件与数据分层

`DocumentProcessingLimits` 同时限制上传字节、页数、DPI、单边、单页像素和
`max_total_rendered_pixels`。PDF 在分配下一页 pixmap 前预估累计像素，渲染后再次用实际像素校验；
图片解码还拒绝动画、加密/修复 PDF、异常几何、格式不一致与 decompression bomb。原件由
`FileStorage` 保存；规范化页面是瞬态 `VisionImage`；OCR/检索文本是派生数据；
`InvoiceExtraction`、技术版本/状态和不可变审计分别建模，GraphState 只放引用与摘要。

### Remote Provider 与脱敏遥测

Qwen/PaddleX 使用既有 timeout、retry/backoff、semaphore、rate limiter、circuit breaker、Payload
Guard 和安全 telemetry。OpenAI Vision/Embedding 现在同样经过共享 `RemoteCallSafety`：SDK 负责
超时和有限重试，安全门负责进程内并发、RPM 和熔断，审计只写 provider、operation、model、
outcome、latency 和 error type。Embedding 额外拒绝空文本、超长文本和 inline binary，调用者仍
必须通过 Application Redactor 提供已脱敏内容。

`MetricsRegistry` 以低基数 Prometheus text format 暴露 Worker processed/retry/lease recovery、
Provider call/circuit-open 和 security audit write failure 指标。`/api/v1/metrics` 默认关闭，开启
后也不读取 ORM 或业务数据；没有可靠查询接口的队列不会伪造 backlog/oldest-age 数值。审计写入
失败会计数并继续抛错，不能以“指标成功”替代安全审计事实。告警接收端和阈值由部署环境配置，
详见 [`docs/observability-alerts.md`](observability-alerts.md)。

日志 formatter 采用字段白名单，第三方日志正文被固定事件名替换。任何日志、错误响应或治理审计
都不得保存发票/OCR 原值、图片、Base64、Prompt、API Key、向量、Idempotency-Key 原文、远程
响应体或 Chain-of-Thought。

## 结构化记忆维护与版本治理

### 前端治理操作边界

控制台的“后台治理操作”是现有 API 的薄客户端聚合视图，覆盖评估任务读取、训练任务控制、模型晋升候选门禁和交易候选复核。它只展示状态、版本、revision、错误码及时间等脱敏元数据；租户和 Reviewer 始终由可信后端上下文解析，前端不提供覆盖字段。所有写操作经统一客户端写入封装生成 `Idempotency-Key`，409 仅提示刷新后重试，不能伪造后台成功。该视图不执行评估、训练、模型权重更新或生产流量切换。
索引治理查询只读取 PostgreSQL 状态；未激活版本在页面上标为待校验，激活 API 才执行实时完整性门禁，页面查询和 `/ready` 均不代表生产验收。
字段语义索引的激活门禁从版本化 Catalog 重新生成合格定义的 semantic ID 与源指纹，
要求它们与 PostgreSQL 版本投影快照完全一致，再比较 Milvus ID/checksum 清单。
诊断型 Snapshot Job 与冻结数据集 Suite Job、Scheduler、Evaluation Worker 共用业务 PostgreSQL
中的评估队列；
真实 Suite 运行时对冻结数据集与证据的读取需要另行接入隔离数据源，不能把队列位置等同于评估隔离。
独立 OfflineEvaluationService 只有在全部 Suite 变体完成、聚合报告发布成功并取得产物引用后才提交
`completed` Run；报告 Publisher 不可用或发布失败时保留脱敏失败事实，不产生可晋升完成证据。

## 独立交易分析 Domain

`InvoiceExtraction` 保持固定 19 字段。Application 只接受 `run_id`，从同租户 completed Run 和
持久化 Result 派生脱敏、不可变的 `TransactionCandidate`，并使用租户范围稳定 fingerprint 幂等合并；
Repository 写入前再次校验来源。幂等键绑定租户、操作及规范化请求摘要；审核用 revision 条件更新，
同键重放读取审计快照，不重复写入。审核前同样重新核验候选来源；历史客户端构造的候选如果与可信派生快照不一致则 fail closed。
`TransactionClassification`、
`DuplicateTransactionCase`、`SuspiciousTransactionCase` 和 `RiskAssessment` 是独立事实，规则
通过 `TransactionRuleEngine` Port 注入并携带版本。模型输出和 score 只提供 advisory；最终状态
由确定性 Policy 或来自可信认证上下文的 Reviewer 决定。所有 Repository 查询包含 `tenant_id`，
不访问 Milvus、原始图片、Base64 或完整 Prompt，也不引入新的 Graph/Agent Loop。
规则引擎目前仍为开发 Mock，不可将 advisory score 当作 probability。接口契约见
[`transaction-analysis-api.md`](transaction-analysis-api.md)。

```text
InvoiceExtraction result -> TransactionCandidate -> versioned rule advisory
  -> classification / duplicate / suspicious cases -> deterministic or reviewer risk status
```

开发与生产验收分离：开发基线可先通过
`scripts/verify-dev-baseline.ps1` 检查 Settings、production-only preflight 的开发分支、
应用组合根导入和 Python 编译。该脚本不连接外部依赖，不执行迁移，不验证 Milvus/S3/OIDC，
也不产生可用于晋升的评估证据。生产 password file、真实 Suite Runner、双 Worker、
备份恢复和告警仍必须在隔离环境单独验收；缺少条件时保持 fail closed。

本地启动脚本 `scripts/manage-local.ps1 -Action start` 会在 FastAPI 启动前执行一次幂等的
`alembic upgrade head`。迁移失败会阻止后端进程启动，避免 ORM 已升级而 PostgreSQL 结构落后的
情况下产生 500；数据库已经处于 Head 时不会重复修改数据。该步骤只负责 Schema migration，
不启动 Memory Admission Worker，也不执行 Milvus 全量重建。

### 生命周期与 Scope

```text
人工审核事实（只追加）
  -> 绑定版本的 ReviewedExample（pending）
  -> 确定性质量信号
  -> 可选模型建议
  -> Python 准入策略 / 授权治理决定
  -> approved PostgreSQL 投影任务
  -> 脱敏 Milvus 案例索引
```

持久记忆边界只包含结构化案例、语义别名、证据引用、质量评估、冲突、治理决定和版本记录。人工
操作只证明发生过审核，不证明值正确。正确、纠错和确认错误案例保持独立检索区域，负例区域不能
生成正向字段值假设。

`ExampleScope` 现在精确包含 `tenant_id`、`document_type`、`field_path`、`schema_version` 和
`catalog_version`。`template_fingerprint` 保留为案例元数据，不参与硬过滤；`not_before` 是从
`last_seen_at` 派生的配置化时间窗口，两者都不得从历史值推断。Query Rewrite 接收同一 Catalog
Scope，但无权修改。Milvus 召回后 PostgreSQL 再次校验 `approved + is_reviewed + is_valid`。
默认最多检索固定的 19 个发票字段；Qwen 重排适配器按模型选择兼容或原生接口及响应 Schema。
在现有 Vision 提取服务内，字段级 OCR 比对之后可受限核对当前图片的视觉候选与独立 OCR 候选：
仅单一可读候选、同页、已接受字段绑定、OCR 定位存在、无冲突且 Schema 可解析时补齐 `null`，
再交由原确定性 Validator 执行格式和业务规则。OCR 不可用或仅历史案例命中时保持原结果。
有审核案例的第二次 Vision 调用只增加基线待审且召回案例的字段路径提示，不增 Workflow 节点，
不将历史值当作本次字段证据。记忆收益需相对无记忆的 Vision+OCR 变体验证，不能用召回数代替。

### 事实与派生数据职责

| 数据 | 唯一事实源 | 生命周期 |
|---|---|---|
| HumanCorrection / CorrectionEvent | PostgreSQL | 只追加审核事实；准入或投影失败时继续保留 |
| ReviewedExample / Admission / Conflict | PostgreSQL | 版本化结构化记忆候选与治理状态 |
| FieldAlias / Catalog Version | PostgreSQL | 租户范围、已审批、可回滚的语义元数据 |
| 案例/字段投影记录 | PostgreSQL | 重试、Checksum、失效和重建状态 |
| Milvus 实体 | 仅派生数据 | 已审批、有效、脱敏且可完整重建 |

Migration `20260909_0022` 为 `reviewed_examples` 增加非空 `catalog_version`。历史 Catalog 归属
无法证明，因此旧记录使用明确哨兵 `legacy-unversioned`、标记失效、追加不可变的确定性 Admission
失效决定，并失效对应投影和 Index Version；不会猜测填入当前活动 Catalog。Milvus 案例 Schema
增加 `catalog_version` 与 `last_seen_at_epoch`，治理人员创建新的 Index Version 后才会建立兼容
Collection，不会复用旧 Schema。

### 失效、保留与租户删除

案例禁用、Schema 失效和保留期到期先移除 PostgreSQL 检索授权并失效投影记录。别名禁用和
Catalog 失效先暂停/失效 PostgreSQL 别名、Catalog 与字段语义 Index Version。Milvus 删除是后续
幂等操作，删除失败不能恢复授权。配置保留期后，检索时还会实时排除过期案例，关闭异步清理完成
前的时间窗口。

保留期 purge 只在 PostgreSQL 失效和 Milvus 清理后删除派生 `ReviewedExample`，继续保留
CorrectionEvent 和原始人工审核事实。租户物理删除是显式合规操作，不能由 Worker 决定。调用方
必须证明租户权限、保留期到期或合法删除依据，并提供需要清理的 Index Version；当前项目不开放
无人值守租户 purge 接口。

### 版本切换、回滚与重建

Catalog 回滚把仍有效的历史快照重新签发为新的未激活版本，保留可审计的线性历史；新版本仍须
经过常规审批、投影完整性和发布门禁。Index 回滚只允许把 Alias 切换到 PostgreSQL 中仍有效且
完整投影的版本，失效版本不能重新激活。

全量重建按租户与 Schema 枚举 PostgreSQL 中 `approved + is_reviewed + is_valid` 的案例，生成脱敏
Dense/Sparse 投影并记录 Checksum，完成后才允许激活 Index。字段语义重建只读取一个不可变的已
审批 Catalog 快照；pending 或 disabled 别名不存在投影路径。Milvus 不参与事实或治理状态恢复。

### Worker 边界与剩余人工治理

现有 Memory Admission Worker 继续使用受限的 claim/lease/retry，并登记单案例投影或清理任务；
本阶段没有新增通用记忆提取或维护 Agent。未来定时维护进程也只能创建评估、准入或恢复任务，不能
批准记忆、训练模型、激活 Catalog/Index 或晋升部署。租户删除授权、Catalog 发布、Index Alias
激活、冲突裁决、模型训练和生产晋升仍是显式人工治理操作。
代码质量治理按风险分批执行；当前 Ruff 未处理项主要是长行和 Python 现代化建议，
不改变 API、Domain 或数据模型语义。

### 客户前端信息架构

Vue 3 前端采用顶部双层导航，不使用桌面常驻侧边栏。一级区域为“工作台、可信记忆、字段语义、
运行治理”，二级导航承载总览、发票提取、准入、案例、字段目录、冲突、索引、评估和审计。
移动端使用底部核心导航和完整功能抽屉。

“总览”作为默认入口，优先呈现待准入、隔离案例和开放字段冲突组成的真实待办队列，并展示
有效案例、多源识别和处理链路状态。任一聚合接口失败时对应数据保持“未读取”，不能用零待办
或前端模拟进度替代。

提取、准入、案例、字段语义、冲突、索引、评估和审计页面继续保留独立职责。内部资源 ID、
模型/Prompt/Schema 版本、投影 attempt 和脱敏错误码仅在需要治理或排障的详情区域显示，
首页不暴露这些实现细节。索引页通过分段视图分别治理案例索引与字段语义索引，并消费两类索引
的登记、查询、分批投影和激活接口。所有页面通过集中 API Client 访问后端，写请求自动携带
`Idempotency-Key`，租户不由前端输入；人工审核的 Reviewer 由 API Adapter 从
`TrustedTenantContext.actor_id` 注入，请求 Schema 不再接受 `reviewer_id`。加载、空结果、
429/503/409 等状态统一使用可读中文提示，并在冲突、准入和别名决定成功后展示后端返回的
Trace ID。人工修正根据固定 Schema 使用日期/日期时间控件并支持显式 `null`；422 诊断只对白名单
canonical 字段和已知错误类型做本地化映射，页面自动定位首个错误且不展示完整远端诊断。
Run 字段弹窗只读现有租户授权的提取结果和准入列表，不生成 ReviewedExample；未审核字段只能标注
为提取结果。日期仅在界面格式化为 `YYYY-MM-DD HH:mm:ss`，API 保持 ISO 8601 时区契约。

API Client 通过并发计数向 App Shell 发布真实 HTTP 活动，顶部不确定进度条不得解释为 Workflow、
Worker 或 Milvus 投影进度。全局通知只消费前端白名单错误文案和受信 `X-Trace-ID`，不展示后端任意
`message/detail`、完整 payload 或远程响应体。网络失败、429、503 造成写结果不确定时，相同方法、
路径和语义请求在当前页面生命周期内复用内存中的 `Idempotency-Key`；Key 不进入日志、通知或持久化。
409 后页面清除陈旧快照并重新读取权威状态与 revision；写成功而刷新失败时保留成功事实，只提供
“重新读取”动作，不重复发起治理写请求。

App Shell 使用内置视图白名单维护 Hash 与浏览器历史，页面切换后更新标题并把焦点移动到主标题。
治理对话框限制键盘焦点，提交期间禁止 Esc/遮罩关闭，结束后恢复触发控件焦点。所有过渡均支持
`prefers-reduced-motion`，移动端保留 44px 以上的核心触控区域。

本地 Vue 开发服务器的 `/api` 代理默认指向 Docker API 发布的 `http://[::1]:8000`，
避免主机上另行运行的 `127.0.0.1:8000` Uvicorn 抢占同端口后导致请求未进入 Docker；
`VITE_API_PROXY_TARGET` 可显式覆盖该目标。

展示层通过集中 `displayLabel` 映射把后端稳定枚举转换为中文，不修改 Domain 或 HTTP 契约。
状态筛选、案例类型、冲突类型、OCR 结果来源、投影状态、评估方案和审计操作均显示中文；
无法稳定翻译且仅用于排障的原因码不直接展示给客户，只显示风险依据数量和后端提供的可读说明。
原始枚举仍作为 `select` 的 value 和 API 参数，避免本地化影响持久化与幂等语义。

提取结果、待审核字段和案例详情直接展示当前可信租户请求已经授权返回的完整业务字段值，
前端不再用星号或字符截断处理这些值。该展示授权不扩展到日志、审计、错误响应或跨租户查询；
图片 Base64、完整 Prompt、API Key、向量和远程响应体继续禁止进入页面与日志。

## 财务 Domain 边界与状态流转

```text
InvoiceExtraction（只读 Schema）
  -> 已完成 ExtractionResult（识别事实）
  -> POST /accounting/candidates（显式、同租户、幂等）
  -> AccountingCandidate(pending_rule_review)
  -> 版本化 TaxAssessment + 确定性 Policy/授权 Reviewer PostingProposal
  -> approved
  -> AccountingPostingProvider
     -> posted
     -> provider failure: 保留 approved，追加 attempt/audit，revision + 1
```

依赖方向固定为 `API -> AccountingService -> Domain/Ports <- SQLAlchemy 与 mock Adapter`。
`InvoiceExtraction`、Workflow 和 GraphState 均不依赖财务 Domain；财务 Domain 只通过 Application
Service 查询已持久化的同租户完成结果，不向识别 Domain 回写。税务规则版本、科目表版本和入账规则
版本在审批时同时固化；任一版本或可执行规则缺失时保持 `pending_rule_review`，模型 advisory 无状态
决定权。PostgreSQL 保存候选、税务评估、入账提案、汇率快照、posting attempt 和不可变 audit；
外部系统不参与本地事实事务。

## Not implemented

- 冲突关闭后的请求由短事务 Worker 幂等消费：仅将明确因该冲突隔离的记忆案例追加确定性
  `pending` 决策并重新入队；其他隔离原因及别名候选标记为 `requires_review`，不自动批准。
- MCP、n8n、开放式 RAG/Agent Memory、Supervisor/Multi-Agent。
- Chat Agent、Insights Agent 和真实财务系统集成；Accounting 仅有 mock Provider，Transaction
  Analysis 已具备同租户完成 Run 来源校验和幂等/CAS，规则仍是开发 Mock。
- 生产对象存储的密钥轮换、恶意文件扫描、PDF 主动内容移除和隔离区；S3-compatible Adapter、
  checksum、presigned URL、生命周期与 Local 迁移 Worker 已实现。
- Remote Provider 的限流和熔断是单进程状态，多副本没有共享全局配额；供应商侧数据保留、删除
  完成和跨境处理状态不能由本项目日志单独证明。
- 治理 reason 等自由文本仍依赖入口长度限制与 Redactor，无法阻止已获授权用户主动粘贴所有类型
  的敏感信息；生产环境仍需要 DLP 规则和审计抽查。
- 索引 projection 的长期容器实际队列、持续漂移监测与生产规模故障恢复、Prometheus/Grafana
  外部告警接入和向量记忆独立数据库；双进程与真实隔离 Milvus 合成数据及清单破坏注入已验收。
- OTLP Exporter、外部 Trace Collector 的交付确认和跨服务 W3C Trace Context；当前实现只接受
  受信任上下文并输出内容关闭的本地结构化 Span。
- 完整的 Milvus 无人值守全量重建与独立监控告警；现有 index worker 只能视为部分实现。
- 具体厂商训练/微调 SDK、生产凭据与真实模型部署控制面；当前仅提供 fail-closed Stub、Provider Port
  和可配置 MLflow-compatible REST Adapter。
- 远端真实 Suite 变体执行服务、自动阈值调参和任何自动 Production 晋升；执行器已要求完整配置两套
  Suite 的既定变体 Runner，Worker 已可选装配 HTTPS Adapter，但远端服务与隔离证据尚未验收。现有诊断型
  Snapshot/Job API、PostgreSQL 队列与可运行 Worker/Scheduler 只对诊断 Job 计算预提交判断值的
  聚合指标；Suite Job 缺真实 Runner 时直接隔离，
  不能代替真实评估执行平台，也不能作为 Promotion Evidence。
- Promotion 的门禁事实已改为从 PostgreSQL Evaluation/Artifact/Version Registry 重载；仍缺自动评估平台与真实生产部署控制面。
- 现有 `__legacy_unassigned__` 数据的租户归属工具；迁移后需管理员显式处理。

## Reference provenance

`financial-intelligence-agent` 的有效许可证为 Apache 2.0，但上游没有 `NOTICE`。本项目只采用
其 Service Layer、Workflow 边界等架构思想，没有复制源码，因此当前无需传播上游 NOTICE；
如未来复制源码，必须同时履行 Apache 2.0 第 4 节的 LICENSE、版权/专利/归属保留和修改声明。

`ai-workflow-engine` 的 README 声称 MIT，但仓库缺少实际 `LICENSE`，故不得据此复制代码。
本项目的 `interrupt()`、`Command(resume=...)`、`pending_review` 和 Checkpointer 恢复均为
基于公开框架 API 的独立实现。

主项目根目录 [`LICENSE`](../LICENSE) 已采用 Apache License 2.0，版权主体为 `love-ovo73`，
版权年份为 2026。该许可证只覆盖本项目自身内容；第三方依赖和参考项目仍按其各自许可证与归属
要求处理，本项目未因采用该许可证而取得第三方代码的授权。
## Docker Compose 部署边界

Compose 只通过显式 profiles 启动 core/auth/vector/mlops 组件，服务间使用 Compose service name，
不使用 host network。PostgreSQL、Milvus、etcd、对象存储和模型产物使用独立 volumes，`migration`
为一次性容器。PostgreSQL password file 由统一 Settings 在进程内解析，API、migration 和 Worker
共享无密码环境变量 URL 与只读 secret 路径；entrypoint 不再把密码导出到环境变量。Checkpointer
继续使用独立 DSN，不复用业务密码解析。evaluation/scheduler 与 API 使用同一业务 PostgreSQL
评估 Job queue；training-worker 使用业务 Training Registry queue。preflight 与 Settings 均接受
password file 的末尾 CR/LF，同时拒绝空值、内部换行和超长内容。Compose 的 API 与生命周期 Worker
可通过 `OBJECT_STORAGE_ENDPOINT_URL` 指向 HTTPS S3 endpoint；默认内部 MinIO HTTP 仅用于开发。
应用 S3 凭据由独立的 `object_storage_app_secret_key` Compose secret 挂载，示例默认引用开发
MinIO 密钥文件；生产应提供最小权限凭据文件与独立 Access Key。生命周期 Worker 不依赖
MinIO 初始化容器，开发 MinIO 场景仍须先创建 private Bucket。
原业务 PostgreSQL 已在 9 月 25 日从实际 `20260923_0031_storage_fk` 升至当时源码 head
`20260924_0044_code_harness_repair_route`；本轮新增 `0045` 尚未迁移。切换前归档再次恢复到独立库，关键事实、租户关联及
约束检查通过。另以独立 Compose project 运行 API、Index Worker、PostgreSQL 和 Milvus，
验证 password file、健康检查与重启恢复；随后独立恢复 project 从校验归档恢复业务事实、
独立 PostgreSQL Checkpointer 和 MinIO 对象，按 PostgreSQL 合格源重建两类 Milvus
Collection 并验证清单及 Alias 失配门禁。完整证据见
[`backup-restore-acceptance-2026-09-25.md`](backup-restore-acceptance-2026-09-25.md)。
这些均为合成数据，不包含原业务流量或生产规模。
当前运行容器的 `postgres-business`/`postgres-checkpoint` 服务名与工作树 Compose 的 `postgres`
定义不一致，不能直接以新配置替换旧项目。实际副本、备份和停写门禁见
[`business-db-migration-preflight-2026-09-25.md`](business-db-migration-preflight-2026-09-25.md)。
生产连接与安全配置尚未端到端验收。
因此 Compose 只适合结构验证和受控开发编排，
不能宣称生产就绪；详见 `docs/docker-deployment.md`。

API 的 `/api/v1/health` 仅表示进程存活，`/api/v1/ready` 只返回脱敏的生产配置
readiness。主要 Worker 通过 runtime probe 校验稳定 `worker_id`、生产配置和 PostgreSQL
连通性；这些检查不等价于 Milvus、对象存储或远程 Provider 的业务可用性。

## Deterministic model promotion

`PromotionCandidateService` 的创建请求只引用离线评估提案、Evaluation Run 和 Model Artifact；
`PromotionEvidenceRepository` 从 PostgreSQL 校验同租户 completed Run、唯一终态 Model Evaluation、
冻结且按文档/模板隔离的评估数据集、真实 Suite 报告版本与产物引用、成功训练产物、已登记的
Model/Prompt 版本及有效且曾激活的 Index。缺少报告产物或数据集内容不合法时 fail closed；
指标与硬失败由这些事实推导，
并与当前受控 Schema、Prompt、Threshold 配置比较；不存在独立 Threshold Registry 时不接受客户端
声明其有效性。审批重新读取证据，并在候选 revision/CAS 事务中检查关键有效性；历史无证据引用的
Candidate 不可晋升。回滚要求目标曾为 active、当前为 rollback，且目标证据仍有效；两个候选状态
及审计在同一 PostgreSQL 事务切换，并以唯一活动候选索引防止双 active。注册状态切换并非真实
模型部署或权重更新；自动 Promotion Worker 保持禁用。接口见
[`model-promotion-api.md`](model-promotion-api.md)。LangSmith/MLflow 只提供外部观测或注册适配。

## 索引投影边界

Reviewed-example 和 field-semantic 索引均以 PostgreSQL 为唯一事实源，Milvus 只保存脱敏派生
projection。两类队列支持 `FOR UPDATE SKIP LOCKED`、`lease_expires_at`、稳定 Worker ID、随机 token、
续租、超时重领、有限重试与条件写入 fencing；旧 Worker 不能提交迟到完成/失败。Worker
不修改审核事实，也不自动切换 Alias；新版本必须完成投影和验证后由授权 API 显式激活。
两类 Alias 使用租户隔离名称；Repository 注册失败时 Application Service 尝试恢复先前 Alias。
内建 Milvus Adapter 的激活门禁在 PostgreSQL 队列完成和 Collection 可访问后，强一致遍历
Collection 的租户、版本、ID 与 projection checksum，并与 PostgreSQL 当前合格案例或字段投影
清单完全比对；缺失、额外、错租户、错版本或 checksum 失配均阻断激活。索引 Worker 的校验
结果会记录为低基数失败事件；它不会自行切换 Alias，失败时继续保留旧 Alias。2026-09-24
使用专用 PostgreSQL 库、两个独立进程和真实隔离 Milvus 对两类投影完成合成数据并发及
缺项阻断 Alias 验收；证据见 [`index-projection-acceptance-2026-09-24.md`](index-projection-acceptance-2026-09-24.md)。
此结果不覆盖生产规模、断网恢复或备份恢复，不能宣称生产索引部署已完成。

完整完成度与后续任务统一见 [`project-status.md`](project-status.md)。
记忆收益逐样本评估由 `MemoryBenefitEvaluationService` 调用既有视觉提取、OCR、
值盲案例检索和确定性 Validator；不写业务 Run、渲染/OCR 产物或检索 Trace。
冻结真值必须覆盖当前图片的 19 字段，并与文档 checksum、运行版本及活动值盲索引一致。
该服务只产出脱敏配对判定，尚未连接隔离批量调度或真实标注数据，不能作为生产晋升证据。
后续模块化执行提示词见 [`codex-next-target-feature-prompt.md`](codex-next-target-feature-prompt.md)；
它只用于编排验收和缺口修复，不改变架构边界或实体 Schema。
