# Invoice Intelligence

## 当前项目状态

截至 `2026-09-25`，核心提取、Human Review Task、可信记忆治理、S3-compatible 存储主体、
Training Registry 和独立财务 Domain 已有实现；Evaluation Job 的 PostgreSQL 队列、Scheduler 与
诊断型 Stub Worker 已实现；两套 Suite 的 Runner 配置完整性门禁及 HTTPS 隔离 Runner Adapter
已具备；独立隔离评估服务的只读边界已实现，但真实变体引擎、脱敏证据快照、索引部署验证、模型晋升实际部署控制和生产 Docker 部署
仍未闭环。交易候选来源校验与审核 CAS 已补齐；历史客户端构造的候选不会自动升级为可信数据，
授权 Reviewer 仅可将其审计升级为 `escalated`。统一完成度、阻塞项和任务优先级见
[`docs/project-status.md`](docs/project-status.md)，不得仅根据类、路由或容器名称判断能力已完成。

P0 部署核对已确认源码 Alembic head 为 `20260924_0044_code_harness_repair_route`，
并修正了 password file 末尾换行的 preflight 判断；Compose 的对象存储 endpoint 可由
`OBJECT_STORAGE_ENDPOINT_URL` 指定 HTTPS 地址；S3 启动检查会拒绝 Principal 数组中的公开授权。
应用对象存储凭据可由 `OBJECT_STORAGE_APP_ACCESS_KEY` 与 `OBJECT_STORAGE_APP_SECRET_FILE`
独立提供；示例配置仍回退到开发 MinIO 凭据。
生命周期 Worker 可在完成业务 migration 后独立指向外部 S3，开发 MinIO 须先完成 Bucket 初始化。
当前 Docker daemon 可连接，旧项目依赖容器仍运行，运行容器的 PostgreSQL 服务名与工作树
Compose 定义不一致。经确认，原业务库已从实际 `0031` 升至唯一 head `0044`；切换前备份已在
独立数据库恢复验证，关键事实计数、租户关联及约束检查通过。独立 Compose project 的 API、
Index Worker、PostgreSQL 和 Milvus 已以合成配置运行并完成健康检查与重启演练；该环境不代表
原业务流量已切换，旧项目仍无 API/Worker。实际命令、备份校验及剩余门禁见
[`docs/business-db-migration-preflight-2026-09-25.md`](docs/business-db-migration-preflight-2026-09-25.md)。
独立 Compose 故障恢复已覆盖业务 PostgreSQL、独立 PostgreSQL Checkpointer 与 MinIO 卷：
合成事实和对象 checksum 恢复一致，实际恢复就绪 65.87 秒；从恢复的合格源重建两类
Milvus Collection，并验证缺项阻止新 Alias 激活。证据及未执行项见
[`docs/backup-restore-acceptance-2026-09-25.md`](docs/backup-restore-acceptance-2026-09-25.md)。
OIDC/TLS、S3 和生命周期的完整隔离验收仍未完成，生产部署仍受限。

后续按模块执行隔离验收和生产化补齐时，使用
[`docs/codex-next-target-feature-prompt.md`](docs/codex-next-target-feature-prompt.md)；
该提示词要求先核对当前事实，再按 P0→P2 顺序推进，不能把 Mock、Stub 或配置解析当成生产验收。

代码生成与自愈 Harness 当前为部分实现：已建立 Domain/Port 契约、完整版本绑定、固定八阶段 Runner、
Repository Snapshot、Python AST/结构化参数与本地精确检索适配器、结构化 Patch 校验、Grammar 显式注册、Snapshot/
Patch/Execution/Watchdog PostgreSQL 事实写入、PostgreSQL
首版事实迁移/Task/Postmortem/Repository Source Registry Repository、Task claim/lease fencing、
通用 Harness Worker、Harness API、共享隐私 Trace/低敏 Metrics Adapter 和默认拒绝 Sandbox；尚未完成真实多语言
Parser、Code Model、代码 Milvus 或 MicroVM Sandbox。设计见
[`docs/code-harness.md`](docs/code-harness.md)，新项目初始化提示词见
[`docs/code-harness-initialization-prompt.md`](docs/code-harness-initialization-prompt.md)；
任务预算、Patch 语法/AST 校验和可信 `trace_id` 持久化已接入；Patch 契约已覆盖受控
`create_file` 与显式许可的 `delete_file`，Postmortem 已具备独立治理
Application/API、可信 actor、租户边界和 revision CAS admission，但长期代码经验检索投影尚未接入。
低敏 Metrics 记录进程内计数器、阶段耗时和可选 Token 统计，但不持久化资源技术 ID。不得将已实现的契约、Stub、首版迁移或配置解析视为 Harness
生产验收。

## 隔离 Suite Runner 接入

Evaluation Worker 默认只运行 `diagnostic_only` Snapshot Job；Suite Job 在未配置 Runner 时
隔离，不回退到 Stub。独立 ASGI 服务工厂位于
`invoice_intelligence.evaluation_runner.entrypoint:create_app`；须在隔离部署中提供
`EVALUATION_SERVICE_TOKEN_FILE`、`EVALUATION_SERVICE_EVIDENCE_ROOT`、
`EVALUATION_SERVICE_EVIDENCE_MANIFEST_FILE` 与
`EVALUATION_SERVICE_ENGINE_FACTORY=module:function`，再使用
`uvicorn invoice_intelligence.evaluation_runner.entrypoint:create_app --factory` 启动。
引擎工厂须为全部 11 个 `EvaluationVariant` 提供真实只读实现；缺项拒绝启动。
证据以 `isolated://<key>` 指向 `<root>/<tenant_id>/<key>`，预登记清单格式为
`{"tenant-a":{"isolated://key":"<sha256>"}}`，仅允许脱敏只读快照；文件缺失、越界、过大或
checksum 不符会拒绝请求。实际部署须启用 TLS 并限制服务与证据源访问。
Worker 可通过环境变量设置
`INVOICE_INTELLIGENCE_EVALUATION_RUNNER_ENDPOINT`（HTTPS 完整 URL）、
`INVOICE_INTELLIGENCE_EVALUATION_RUNNER_TOKEN_FILE`（容器内 secret 文件路径）。
两项必须同时提供；凭据和隔离服务应由部署方独立提供。聚合 JSON/Markdown 报告直接写入
PostgreSQL `evaluation_report_artifacts`，不依赖本地报告目录。
可另设 `EVALUATION_RUNNER_TIMEOUT_SECONDS`、`EVALUATION_RUNNER_MAX_RETRIES`、
`EVALUATION_RUNNER_MAX_CONCURRENCY` 与 `EVALUATION_SUITE_TIMEOUT_SECONDS`，均使用
`INVOICE_INTELLIGENCE_` 前缀。Compose 未内置真实变体引擎、证据快照或 token。

Adapter 按 `isolated-evaluation-runner-v1` 发送 Suite、变体、冻结数据集的文档/模板清单、案例证据引用和
版本绑定，不发送 Ground Truth、Reviewer、完整发票值或图片；服务须返回请求 SHA-256、隔离
清单 SHA-256 和对应的结构化 Observation。服务校验冻结清单、案例身份及证据 checksum，Worker
校验响应后再由 Application Service 聚合。
该绑定校验不能证明远端确实使用了隔离只读数据源，须通过隔离环境的账号权限和执行记录验收。

## 可插拔对象存储

development 默认使用 `LocalFileStorage`；设置
`INVOICE_INTELLIGENCE_FILE_STORAGE_BACKEND=s3` 后使用 S3-compatible Adapter。业务对象固定分为
`invoice-originals`、`invoice-rendered`、`invoice-derived-text` 三个 private Bucket，名称可配置但
必须互不相同。production 禁止 Local、HTTP endpoint、关闭 TLS 和默认 HMAC key。

MinIO 使用 `minio/minio:RELEASE.2025-04-22T22-12-26Z`。启动业务 Bucket 和生命周期 Worker：

```powershell
docker compose --env-file .env.compose --profile object-storage up -d `
  object-storage object-storage-init storage-lifecycle-worker
python -m alembic upgrade head
python -m invoice_intelligence.workers.storage_lifecycle
```

从已有 Local 数据切换到 MinIO/S3 前，先将 backend 配为 `s3` 并执行：

```powershell
python -m invoice_intelligence.workers.storage_migration
```

命令逐个复制、校验 SHA-256/大小并以 PostgreSQL 事务切换引用。仍存在
`migration_pending/failed` 历史对象时，production API 会拒绝启动。

上传仍先执行 MIME、扩展名、签名、大小、PDF 页数、图片像素和解压炸弹校验。PostgreSQL 的
`stored_objects` 保存租户、对象引用、SHA-256、媒体类型、大小和生命周期状态；不保存文件内容或
预签名 URL。`GET /api/v1/documents/{document_id}/download-url` 只在 tenant 校验和 checksum
检查后签发短时 URL。Local URL 由 API HMAC 验证，S3 URL 由 SDK 签发。

生命周期 Worker 只消费 PostgreSQL 中到期/待删除记录，使用 `FOR UPDATE SKIP LOCKED`、lease、
revision 和幂等 DELETE。稳定错误码为 `storage.object_not_found`、
`storage.checksum_mismatch`、`storage.permission_denied`、
`storage.temporarily_unavailable` 和 `storage.invalid_reference`。

## 独立财务 Domain

财务处理不属于识别 Workflow。授权调用方使用 `POST /api/v1/accounting/candidates` 引用同租户、
已完成且已持久化结果的 `run_id`，显式幂等创建 `AccountingCandidate`。候选初始状态固定为
`pending_rule_review`；只有带税务规则、科目表和入账规则版本的确定性 Policy 结果或授权 Reviewer
决定才可进入 `approved`，之后才允许通过 `AccountingPostingProvider` 入账。Docker/本地组合根只装配
mock posting 与 mock exchange-rate Adapter，不连接真实财务系统。

所有财务写接口要求 `Idempotency-Key`，状态变更要求 `expected_revision`。跨租户读取统一表现为
404。外部 posting 失败以独立 attempt 和 audit 事实保存，不回滚候选、审核决定或识别结果。

## 模型晋升与回滚

模型发布使用独立 `PromotionCandidate`，状态为 `shadow`、`canary`、`active`、`rollback`、`rejected`。
Candidate 必须绑定 dataset/evaluation/model/prompt/schema/index/threshold 版本；PostgreSQL 通过
`revision` CAS 和 `promotion_candidate_audits` 保存审批、拒绝和回滚事实。创建 API 只接收离线评估
提案 ID、Evaluation Run ID 和 Artifact ID；指标、硬失败和兼容性从同租户已完成评估、有效模型产物、
Model Evaluation 和版本记录重载。审批再次校验，回滚要求曾激活且仍有效的历史 Candidate，并在单个
PostgreSQL 事务切换注册状态。门禁还要求冻结的同租户评估数据集、真实 Suite 报告版本和报告产物引用；
诊断型 Stub 与缺失报告产物均被拒绝。旧 Candidate 缺证据引用时 fail closed。此处只切换注册事实，
不调用真实部署控制面或修改模型权重；当前不提供模型晋升 Worker，详见
[`docs/model-promotion-api.md`](docs/model-promotion-api.md)。

企业级发票与图片信息智能提取项目。目标系统采用确定性的单一 LangGraph Workflow，
支持结构化提取、人工纠错和持续记忆。

面向客户的完整目标方案、当前实现状态、数据流、治理边界和生产化路线见
[`INVOICE_INTELLIGENCE_SOLUTION.md`](INVOICE_INTELLIGENCE_SOLUTION.md)。
后续前端交互迭代可直接复用 [`FRONTEND_INTERACTION_OPTIMIZATION_PROMPT.md`](FRONTEND_INTERACTION_OPTIMIZATION_PROMPT.md)。

## Trace、审计与隐私遥测

在项目根目录 `G:\work\ai` 启动 API 或 Worker 时，development 默认把应用 JSONL 日志
自动写入 `G:\work\ai\logs\<进程名>\<进程名>-<PID>.jsonl`，同时保留控制台输出。
单文件默认 20 MB、保留 5 个轮转副本；进程启动时清理该组件目录内超过 7 天且所属进程
已停止的旧文件。`logs/` 已由 Git 忽略。production 默认关闭本地落盘；可用
`INVOICE_INTELLIGENCE_LOG_FILE_ENABLED` 显式切换。当前 Compose 明确开启文件输出，业务 API/Worker
将 `/app/logs` 映射到项目 `./logs`；设 `LOG_FILE_ENABLED=false` 可关闭 Compose 文件输出。
应用日志是 JSONL；Uvicorn 或其他外部进程自己的非 JSON 输出不属于此文件流。

本地开发排障可直接从该日志目录生成限长诊断包：

```powershell
python -m invoice_intelligence.diagnostics --log .\logs `
  --trace-id <X-Trace-ID> --tenant-id <trusted-tenant-id> --max-chars 6000
```

可按 `--since`、`--until`（带时区的 ISO 8601）和 `--error-code` 收窄；本地授权运维人员
可加 `--with-db`，使用已有 Settings 只读查询同租户 Harness Task/Attempt/Postmortem 与治理审计。
输出只含白名单技术字段、目标错误或首个失败事件、邻近事件和固定源码入口提示，不含原始日志正文、
异常正文或发票值。该命令不调用大模型；按目录读取时只扫描各组件的 JSONL 主文件与轮转文件，
每条可匹配事件须含 `trace_id` 与 `tenant_id`。源码提示只是排查起点，不是根因结论。

每个 HTTP 请求由服务端生成或从受信任认证上下文继承 `trace_id`，客户端普通 Header、Body
和 Query 均不能指定可信 Trace。响应通过 `X-Trace-ID` 返回技术关联标识；治理写响应同时返回
对应 PostgreSQL 审计事实的 `audit_id`、`resource_version` 和 `trace_id`。相同
`Idempotency-Key` 的语义一致重放返回原审计和原 Trace，不创建第二条审计。

统一阶段名为 `ingestion`、`document_preprocessing`、`vision`、`ocr`、`retrieval`、
`validation`、`human_review`、`memory_admission`、`field_semantic_binding`、
`index_projection`、`governance_operation` 和 `background_recovery`。阶段 Span 只记录耗时、结果、
脱敏错误类型、版本及资源技术标识；不采集发票值、图片、Base64、完整 Prompt、API Key、向量、
Idempotency-Key 原文、远程响应体或 Chain-of-Thought。Trace ID 不是业务 ID。

PostgreSQL 仍是治理审计唯一事实源；历史审计缺失 `resource_version`/`trace_id` 时保持 `null`。
阶段日志、外部 Trace Collector 和 Milvus 均不参与治理事务。`indexed` 只表示 PostgreSQL 已记录
一次成功投影，不能证明查询时 Milvus 实体仍存在、Collection 已加载或远程服务健康。索引 Worker
启用校验时会记录校验失败并保持投影版本不可激活；Alias 切换仍只能由授权 API 执行，旧 Alias
不会因后台校验失败而被替换。

## 快速启动（Windows PowerShell）

### Docker Compose 部署

Docker 部署使用显式 profiles，默认不启动服务。业务 PostgreSQL password file 已接入 API、migration
和 Worker 的统一 Settings 解析路径，但生产连接和其余安全前置项尚未完成端到端验证，不能据此宣称
生产就绪。完整边界、
诊断型 Evaluation Job 队列由 API 登记在业务 PostgreSQL，Scheduler 与 Evaluation Worker
使用相同的 password-file DSN；真实 Suite 的隔离证据读取仍待接入。
auth/vector/mlops profile 和阻塞项见 [`docs/docker-deployment.md`](docs/docker-deployment.md)。
隔离备份与恢复演练流程见 [`docs/backup-restore-drill.md`](docs/backup-restore-drill.md)；默认
只生成计划，不能据此宣称已完成生产恢复验收。

前置环境：Python 3.12、Node.js/npm、Docker Desktop。项目根目录固定为 `G:\work\ai`。

### 1. 首次准备配置

```powershell
cd G:\work\ai

if (!(Test-Path .env.compose)) { Copy-Item .env.compose.example .env.compose }
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

如果两个文件已经存在，不要覆盖。Compose 的 PostgreSQL 密码必须存放在
`secrets/postgres_business_password.txt`，容器内只传 password-file 路径；本地开发可在 `.env`
中使用带凭据的业务 DSN。按 [`docs/docker-deployment.md`](docs/docker-deployment.md) 在开发环境
准备 `secrets/*.txt`，生产由 secret manager 提供，不能沿用示例占位值。Checkpointer 必须使用
独立数据库，不得与业务数据库同库。远程推理
选择千问时，还需设置 `INVOICE_INTELLIGENCE_VISION_PROVIDER=qwen` 对应的完整前缀配置、
`INVOICE_INTELLIGENCE_QWEN_API_KEY`、地域正确的
Compatible Base URL 及所选模型；变量完整名称以 [`.env.example`](.env.example) 为准。

### 2. 启动 PostgreSQL、Milvus、etcd 和 MinIO

```powershell
cd G:\work\ai
docker compose --env-file .env.compose --profile core --profile vector up -d `
  postgres etcd object-storage milvus
docker compose --env-file .env.compose ps
```

等待上述四个容器均为 `running` 或 `healthy`。Compose 使用命名 volumes，正常 `down` 不会删除数据；
不要执行 `docker compose down -v`。

### 3. 首次初始化后端

```powershell
cd G:\work\ai
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
alembic upgrade head
```

`.venv` 已存在时跳过创建；依赖已经安装且没有新 migration 时，只需激活环境并执行
`alembic upgrade head`，该命令可幂等重复执行。

### 4. 启动 FastAPI 后端（保持窗口运行）

```powershell
cd G:\work\ai
.\.venv\Scripts\Activate.ps1
uvicorn invoice_intelligence.main:app `
  --host 127.0.0.1 `
  --port 8000 `
  --loop asyncio:SelectorEventLoop
```

Windows 下必须使用 `SelectorEventLoop`：PostgreSQL Checkpointer 使用的 Psycopg 异步连接不支持
默认的 `ProactorEventLoop`。即使显式传入 `--loop asyncio`，Uvicorn 单进程模式仍会选择
`ProactorEventLoop`，因此不能省略上述完整 `--loop` 参数。

后端健康检查与 API 文档：

```text
http://127.0.0.1:8000/api/v1/health
http://127.0.0.1:8000/docs
```

### 4.1 启动记忆准入 Worker（另开窗口）

首次使用新增 Worker 前先执行第 3 步的 `alembic upgrade head`。Worker 必须连接 PostgreSQL，
不会由 FastAPI 自动启动：

```powershell
cd G:\work\ai
.\.venv\Scripts\Activate.ps1
python -m invoice_intelligence.workers.memory_admission
```

`Ctrl+C` 会停止领取新任务，并等待当前已领取批次完成。多副本可使用不同的
`INVOICE_INTELLIGENCE_MEMORY_ADMISSION_WORKER_ID`；留空时自动使用 hostname 与 PID。

冲突关闭后的重评估请求由独立 Worker 消费；先升级到
`20260924_0036_conflict_reevaluation`，再启动
`python -m invoice_intelligence.workers.conflict_reevaluation`。它只重排因该冲突被确定性隔离的
记忆案例；其他隔离原因和别名候选仍需治理人员复核，不会自动批准。

### 4.2 启动训练 Worker（可选，另开窗口）

训练 API 只登记 `planned` 任务，不在 HTTP 请求内提交训练或更新模型权重。执行 migration 后，
独立启动 Worker：

```powershell
cd G:\work\ai
.\.venv\Scripts\Activate.ps1
python -m invoice_intelligence.workers.training
```

默认 `INVOICE_INTELLIGENCE_TRAINING_PROVIDER=stub` 会确定性地将领取到的任务隔离为
`training_provider_not_configured`，不会伪造远程成功。接入兼容 REST 控制面时设置
`mlflow_compatible`、HTTPS Base URL、Bearer Token、三个可配置路径和产物 HTTPS host allowlist。
该 Adapter 不依赖 `mlflow` Python 包，也不要求本地 MLflow 服务；本地实验服务使用独立
`mlflow-local` Compose profile。`mlops` profile 只保证训练 Worker 与 PostgreSQL 可用。

训练状态固定为 `planned/submitted/running/succeeded/failed/cancelled/quarantined`。远程明确失败
进入 `failed`；缺少凭据、版本/租户不匹配、Provider 契约或产物 checksum 失败进入
`quarantined`；取消只有在 Worker 未提交或远程平台确认后才进入 `cancelled`。终态任务通过 retry
创建新 Job，不复活原记录。

### 5. 可选启动本地 PaddleOCR（另开窗口）

```powershell
cd G:\work\ai
.\.venv-ocr\Scripts\Activate.ps1
$env:PADDLE_PDX_CACHE_HOME = (Join-Path $PWD '.data\paddlex-cache')

paddlex --serve `
  --pipeline .\conf\ocr\ppocrv6_small_v1.yaml `
  --host 127.0.0.1 `
  --port 8188 `
  --device gpu:0
```

禁止改回 `--pipeline OCR`，否则会加载默认 `PP-OCRv6_medium`。当前 PaddleOCR HTTP Adapter
在 `INVOICE_INTELLIGENCE_OCR_ENABLED=true` 时由 Composition Root 接入主 Workflow；未启用
字段语义绑定时只保留行级技术观察，不执行 canonical 字段自动绑定。

### 6. 首次初始化并启动 Vue 3 前端（另开窗口）

```powershell
cd G:\work\ai\frontend
npm install
npm run dev
```

后续启动只需 `npm run dev`。浏览器访问 `http://127.0.0.1:5173`，Vite 将 `/api` 代理到
`http://127.0.0.1:8000`。

前端任务中枢按租户可信上下文提供九个视图，并通过顶部导航分为“工作台、可信记忆、字段语义、
运行治理”四个区域：总览、提取工作台、记忆准入、案例库、字段语义、冲突、索引、评估和审计。
移动端使用底部核心导航和完整功能抽屉，不保留常驻侧边栏。提取页保留上传、Workflow 状态、视觉/OCR/字段绑定/Retrieval
阶段提示、字段证据和显式人工审核；后端未提供的阶段状态统一显示为“未报告”，不模拟进度。
治理写操作均生成 `Idempotency-Key`，准入与别名审批/禁用操作提交当前 `revision`，危险操作必须填写原因。
前端提供统一请求进度、成功/警告/失败通知、页面 Hash 导航和可访问的治理确认对话框。请求进度只表示
当前 HTTP 活动，不代表后台 Workflow、Worker 或 Milvus 投影百分比。网络中断、429、503 后，当前
页面生命周期内的相同语义写请求复用原 `Idempotency-Key`；成功或确定性失败后才轮换。通知仅展示
前端白名单中文文案和受信 Trace，不展示远端 `message/detail` 或完整响应体。
人工审核请求不再接受前端 `reviewer_id`，审核人固定取自后端 `TrustedTenantContext.actor_id`。
人工修正按固定 Schema 使用日期或日期时间控件，支持显式提交 nullable 字段；请求校验失败时只将
白名单字段和本地化错误类型映射回对应控件，并自动定位首个错误，不展示完整远端诊断。
别名页面展示后端 revision；409 时重新查询候选并提示记录已被其他治理人员更新，前端不先行改状态。
治理写入成功但随后刷新失败时，页面明确显示“操作已保存、最新状态未读取”，后续只允许重新读取，
不会重复提交写操作。409 后清除陈旧选择，只有成功读取最新 revision 后才允许再次治理。
冲突页面支持从后端候选中 resolve 或将误报 dismiss，并在 409 时刷新最新状态，不进行乐观更新。
案例详情分页展示 PostgreSQL 中各 Index Version 的投影状态、尝试次数、checksum 和脱敏错误码；
页面明确区分“已登记 indexed”与“实时 Milvus 健康”。
页面不提供 `tenant_id` 输入。已授权的提取结果、人工审核值和案例值按业务需要完整展示，
不使用星号遮蔽或字符截断；图片 Base64、API Key、完整 Prompt、向量和跨租户数据仍禁止展示。

“总览”是面向客户的默认入口：第一屏以风险优先队列展示待准入、已隔离和开放字段冲突，
同时展示有效案例、多源识别和处理链路状态。总览数据只调用现有
健康、准入、案例、冲突和 OCR 聚合接口；任何接口不可用时显示真实的未连接/未报告状态，
不会用零待办或前端自定义数据伪造处理进度。技术人员仍可在折叠详情、索引、评估和审计页查看版本、
投影状态、指标和 Trace ID。

索引治理页使用“案例记忆索引/字段语义索引”分段视图，已接入两类索引的登记、查询、分批投影
和激活接口。内建 Milvus Adapter 的激活会强一致读取目标 Collection 的 ID/checksum 清单，
与 PostgreSQL 合格投影逐项比对；页面的普通状态查询仍不代表实时 Milvus 健康。

客户界面的状态、筛选项和指标名称统一使用中文，例如“结果冲突”“仅文字识别”“仅视觉识别”
和“多源一致”。后端继续使用稳定英文枚举作为 API 契约，前端只在展示层转换，不改变查询、
筛选和治理请求语义。待处理、隔离、冲突、失败和不可用状态使用更醒目的黄/红状态色；版本号、
标准字段路径等必须保留的技术值仅在治理详情中展示。

治理审批采用显式门禁：客户人工审核只形成审核事实；确定性质量评估产生不可覆盖的硬失败和风险
信号；审批模型只产生结构化 advisory；Python Policy 合并信号；最终状态变更必须由具备治理权限
且满足二级审批规则的 Reviewer 提交。Admission、Alias、Conflict 均使用 PostgreSQL 事务、
revision/CAS 和 Idempotency-Key；Conflict resolve/dismiss 只关闭冲突并登记重新评估，不修改
发票值，也不直接批准记忆或别名。只有 `approved` 案例或别名才允许登记 Milvus 派生投影。

### 7. 启动校验与停止

日常启动不需要再次手工粘贴数据库迁移命令。统一管理脚本会在启动后端前幂等执行
`alembic upgrade head`；数据库已是最新版本时不会重复变更，迁移失败则不会启动后端：

```powershell
cd G:\work\ai
.\scripts\manage-local.ps1 -Action start
```

需要同时启动本地 PaddleOCR 时：

```powershell
.\scripts\manage-local.ps1 -Action start -IncludeOCR
```

查看状态或停止由脚本启动的进程：

```powershell
.\scripts\manage-local.ps1 -Action status
.\scripts\manage-local.ps1 -Action stop -IncludeOCR
```

```powershell
Invoke-WebRequest http://127.0.0.1:8000/api/v1/health
docker compose --env-file G:\work\ai\.env.compose -f G:\work\ai\docker-compose.yml ps
```

前端、后端、Memory Admission Worker 和 PaddleOCR 使用各自窗口的 `Ctrl+C` 停止。
停止中间件但保留数据：

```powershell
cd G:\work\ai
docker compose --env-file .env.compose down
```

日常重新启动时依次执行第 2、4、4.1、可选第 5、6 步，无需重新创建虚拟环境或执行
`npm install`。

也可使用幂等脚本统一管理后端、Memory Admission Worker 与前端；`-IncludeOCR` 额外管理独立
`.venv-ocr` 进程。OCR 状态只检查 `127.0.0.1:8188` TCP 可达性，不猜测健康检查接口。

```powershell
.\scripts\manage-local.ps1 start -IncludeOCR
.\scripts\manage-local.ps1 status -IncludeOCR
.\scripts\manage-local.ps1 stop -IncludeOCR
```

### 回归验证

以下门禁不会启动后端、前端、OCR 或中间件服务：

```powershell
cd G:\work\ai
.\scripts\verify-regression.ps1
```

当前回归覆盖固定 19 字段 Entity Schema、多 OCR Provider 确定性顺序与安全降级、Vision/OCR
复用同一批规范化图片、OCR 指标持久化、历史 Schema 记忆失效，以及审核案例和字段语义索引的
激活门禁。`quarantined` 准入记录可在前端单条或批量选择“重新评估”，该操作只将记录以可审计
决定恢复为 `pending`；独立 Memory Admission Worker 随后生成当前 Policy 的确定性评估，仍不会
绕过批准门禁。

Evaluation Job 的隔离回归另有专门矩阵，覆盖 Dataset Snapshot 不可变性、租户隔离、幂等语义、
queue claim/lease、迟到 Worker fencing、重试隔离和 Scheduler 只入队边界。当前已通过
`tests/test_evaluation_jobs.py` 的 6 项定向测试及 Evaluation/Promotion/Training/Auth/Transaction
相关 34 项回归；这些测试使用隔离 SQLite/Stub，不代表真实 PostgreSQL 并发或生产评估演练已完成。

文档链接与状态声明可在本地检查：

```powershell
.\scripts\check-docs.ps1
```

该检查覆盖核心 Markdown 的本地链接、状态基线标记和已知过时声明；当前尚未接入远程 CI。

## 能力白名单与不可信输入边界

Infrastructure Adapter 采用代码内静态白名单，启动组装时校验内建实现的正向能力。能力声明不能
来自 HTTP 请求、Prompt、OCR 文本、图片或租户配置，也不是动态 Tool Registry。

| Adapter 类型 | 允许能力 | 明确禁止 |
|---|---|---|
| OpenAI/Qwen Vision | 结构化视觉提取 | 数据库写入、Workflow 路由、审批、Schema/Tenant 修改 |
| PaddleX OCR | 原始 OCR observation | canonical 字段赋值、审批、持久化业务事实 |
| OpenAI/Qwen Embedding | 对 Application 已脱敏文本生成 Dense 向量 | 接收图片、推断当前字段值、写事实库 |
| Qwen Reranker/Query Rewrite | 对已脱敏候选排序或改写检索文本 | 改写 Scope Filter、填充发票、改变路由 |
| Qwen Memory Quality | 结构化 advisory suggestion | 单独批准记忆、覆盖确定性硬失败 |
| Milvus Index Store | 读写可重建派生索引 | 作为事实源、写入未审批或未脱敏数据 |

`Domain` 只包含业务值对象与规则；`Application` 定义 Port、用例、Scope Filter 和确定性 Policy；
`Infrastructure` 实现文件、模型、OCR、PostgreSQL 和 Milvus Adapter；`API` 只解析可信上下文并调用
Application Service。Provider 没有治理 Repository、Workflow Router 或数据库会话，因此模型输出
必须先经过 Pydantic/Domain 校验，再由 Application 决策。

图片文字、OCR 文本、用户备注、历史案例、字段目录说明和外部错误文本全部按不可信数据处理。
Vision Prompt 的数据区块名由固定枚举产生，内容使用 JSON 封套并标记
`trusted_instructions=false`；伪造的 `BEGIN_UNTRUSTED_DATA/END_UNTRUSTED_DATA` 标记会被移除。
数据区指令不能修改 `InvoiceExtraction`、Workflow、Tenant/Reviewer、审批 Policy 或索引过滤条件。
当前图片事实仍高于 Schema/业务规则之外的所有历史信息，证据不足时不自动填充。

文件入口仅接受 PDF/JPEG/PNG/WEBP，并同时检查声明 MIME、扩展名、文件签名和解码器格式；限制
上传字节、PDF 页数、单页尺寸、单页像素、DPI、累计 PDF 渲染像素和动画/解压炸弹。原始文件
保存在 `FileStorage`，规范化图片仅在 Provider 调用期间存在，业务 Entity、技术元数据和审计记录
分别持久化，GraphState 不保存 Base64。

OpenAI、Qwen 和 PaddleX 均具有超时、有限重试、并发限制、RPM 限流、熔断及元数据审计。日志
白名单只允许技术标识、阶段、耗时、计数、模型版本和脱敏错误码；禁止原始发票值、OCR 全文、
图片、Base64、完整 Prompt、密钥、向量、远程响应体和 Idempotency-Key 原文。

关键配置：

```text
INVOICE_INTELLIGENCE_MAX_TOTAL_RENDERED_PIXELS=120000000
INVOICE_INTELLIGENCE_OPENAI_MAX_CONCURRENCY=8
INVOICE_INTELLIGENCE_OPENAI_REQUESTS_PER_MINUTE=60
INVOICE_INTELLIGENCE_OPENAI_CIRCUIT_FAILURE_THRESHOLD=5
INVOICE_INTELLIGENCE_OPENAI_CIRCUIT_RECOVERY_SECONDS=30
INVOICE_INTELLIGENCE_OPENAI_VISION_SCHEMA_MAX_RETRIES=2
INVOICE_INTELLIGENCE_QWEN_VISION_SCHEMA_MAX_RETRIES=2
INVOICE_INTELLIGENCE_VISION_PROMPT_VERSION=invoice-vision-extraction-v2
```

## 当前边界

### Human Review Task Service

后端已提供独立的 PostgreSQL 审核任务生命周期：`pending_review`、`claimed`、`submitted`、
`expired`、`cancelled`。列表支持审核人、优先级、状态、创建时间和 cursor 分页；claim、release、
reassign、cancel 与过期恢复使用 revision/CAS，claim 使用有限 lease。转派清除原 lease，只有
受让人能重新 claim。提交强制
`Idempotency-Key`，Reviewer 只来自可信认证上下文，跨租户读取统一返回 404。

审核事实、最终结果和记忆准入保持不同事务；记忆准入失败不会撤销已写入的审核事实。
审核后台调用契约见 [`docs/human-review-api.md`](docs/human-review-api.md)。

OIDC/JWT、可信租户上下文和 RBAC 的部署说明见
[`docs/authentication.md`](docs/authentication.md)。除健康检查外，API 身份只来自已验证 Token；
development 可使用显式 demo tenant，production 禁止 `DEV_TENANT_ID` 并强制 HTTPS issuer/JWKS
及 TLS 校验。认证和权限拒绝写入脱敏安全审计，跨租户资源继续统一返回 404。

- Python 3.12，使用 `src` layout。
- `domain` 除业务 Schema 使用 Pydantic 外，不依赖编排、API、数据库或模型 SDK。
- `application` 保存用例和抽象接口。
- `workflow` 只负责编排，不定义业务实体。
- `infrastructure` 实现文件/数据库/向量索引以及 OpenAI、千问远程推理 Adapter。
- `api` 只负责 HTTP 协议适配。
- `bootstrap.py` 是唯一依赖组装位置，`main.py` 只管理进程入口和 API lifespan。
- `references/` 是只读参考源，不属于主项目。

系统新增独立交易分析 Domain，不扩展 `InvoiceExtraction` 或直接产生最终风险结论。分类、重复匹配、
可疑规则与风险评估保存在 PostgreSQL 租户事实表；模型分数仅为 advisory，最终 `confirmed`、
`dismissed`、`escalated` 状态必须由确定性规则或可信 Reviewer 决定。当前
`/api/v1/transactions/candidates/analyze` 只接收 `run_id`，由 Application Service 校验同租户
completed Run 与持久化 Result 后派生脱敏候选；审核要求 `expected_revision`，并由 PostgreSQL
条件更新保护，并在审核前重新核验候选来源。幂等键绑定操作、租户和规范化请求摘要，重放不重复写审计。规则仍为开发 Mock，
因此该 Domain 仍属受限可用；接口契约见 [`docs/transaction-analysis-api.md`](docs/transaction-analysis-api.md)。

当前已支持 PNG、JPG、JPEG、WEBP、PDF 的上传、签名/MIME/完整性/大小/页数校验，
以及 PDF 页面受限渲染。上传原件保存到 `FileStorage`，Workflow 只保留
`document_id`、`storage_uri`、`mime_type`、`checksum`。

视觉请求前会对输入做确定性质量处理：读取 EXIF 方向、统一为 RGB/PNG、限制最大边长和像素
数；低动态范围图片执行轻度自动对比度与锐化，PDF 默认按 300 DPI 渲染后再应用同一处理链。
处理不会改变原始文件，也不会生成额外业务字段；每页仅记录宽高和清晰度分数，供验证路由和日志
定位问题。相关参数可由 `.env` 中的 `PDF_RENDER_DPI`、`MAX_IMAGE_DIMENSION`、
`MAX_IMAGE_PIXELS` 和 `IMAGE_PREPROCESSING_*` 配置。

视觉 Provider 可选 OpenAI Responses Structured Outputs 或千问 OpenAI-compatible JSON
Schema。`InvoiceExtraction` 固定为 19 个业务字段，字段均 required-but-nullable，并设置
`extra="forbid"`；Provider 不得输出 VAT 扩展字段、明细数组或其他未声明字段。无法可靠完成
结构时，Provider 返回 `invoice=null`、字段级直接候选/可读性和异常，不保存 Chain-of-Thought。
Prompt v2 将 system、user instruction、强制规则、JSON Schema 和 retry instruction 分离；非空
invoice 的 19 个字段必须各有且仅有一条 evidence，包含值为 `null` 的字段。Provider 返回先经
本地 Pydantic 与 coverage 校验；重试只携带白名单化的 missing/extra/type/coverage 诊断，不携带
字段值或完整响应体。`VisionPromptRegistry` 是可选 Application Port，默认使用本地版本化 Prompt；
可注入 LangSmith pull adapter，未配置 LangSmith 凭据时不影响运行，也不会向 Registry 上传图片、
Base64、发票值或请求响应。PromptGenius 不属于运行时依赖。

已实现单一确定性 LangGraph `StateGraph`：

```text
prepare_document
  -> extract_invoice (baseline current-image facts)
  -> retrieve_correction_context
     -> enterprise Milvus reviewed examples OR pgvector legacy fallback
     -> no match -> validate_extraction
     -> scoped matches -> extract_invoice (dynamic Few-shot) -> validate_extraction
     -> accepted -> persist_result -> save_correction_memory -> END
     -> review_required -> request_human_review -> interrupt()
                  -> Command(resume=HumanCorrection)
                  -> apply_human_correction -> validate_extraction
     -> rejected -> FAILED -> END
```

验证不读取或依赖模型自报 confidence。每个字段生成 `ValidationSignal` 和
`FieldDecision`，组合页面清晰度/分辨率、字段缺失、Pydantic Schema、金额与明细关系、
日期/币种/编号格式、视觉候选/歧义、Provider anomaly，以及可选 OCR 与 Vision 一致性。
审核请求包含字段路径、当前值、候选值、触发规则、原因和用户操作；OCR 比对触发审核时还包含
有界的来源、页码、位置和 reason codes。验证器不会用候选值自动覆盖低可信字段，OCR-only
候选也只能由人工选择。人工明确确认的字段不会被相同图像、OCR、格式或业务规则重复送审，
但仍必须通过完整 Entity Schema；人工明确确认的 `null` 保持为 `null`，不进行猜测填充。

路由全部由 Python 条件函数决定。`thread_id` 仅用于 LangGraph Checkpoint，`run_id` 标识一次
业务执行，`document_id` 标识文档，三者必须不同。Workflow 状态、最终结果与纠错记忆写入
PostgreSQL 业务数据库；LangGraph Checkpointer 使用独立存储。开发环境可显式关闭向量记忆
后使用 SQLite，生产环境通过
`INVOICE_INTELLIGENCE_CHECKPOINT_BACKEND=postgres` 和 PostgreSQL DSN 切换。

节点按恢复重放设计：最终结果按 `run_id` write-once，纠错事件使用稳定事件 ID 去重。
`request_human_review` 在 `interrupt()` 前无外部副作用。
人工审核在一个 PostgreSQL 事务中写入 `human_corrections`、`correction_events` 和
`memory_review_recoveries`。`persist_result` 成功后，Workflow 才尝试幂等创建待准入案例和
`pending` 审批记录；失败只把恢复记录标为 `failed_retryable`，Run 仍保持 `completed`。
`GET /api/v1/runs/{run_id}` 通过独立技术字段返回 `memory_status`、脱敏 `memory_error_code` 和
`memory_trace_id`，不污染 `InvoiceExtraction`。Workflow 不同步执行质量评估、远程审批建议或
索引投影，也不再向旧 pgvector 写入新记忆；pgvector 仅在 Milvus
案例检索未配置时作为租户隔离的已有数据开发/迁移回退。Prompt 将当前图片事实、三类已准入案例
和强制业务规则分区，历史值不得覆盖当前图片证据。

独立 Memory Admission Worker 先恢复未生成案例的 `memory_review_recoveries`，再使用 PostgreSQL
`FOR UPDATE SKIP LOCKED` 原子领取到期准入任务，记录
`worker_id`、随机 lease token、租约时间、尝试次数、下次执行时间和脱敏错误码。它恢复案例、
CorrectionEvent、document/run/result 与证据引用，调用既有 `MemoryAdmissionService.process_admission`。
确定性硬失败不重试；远程超时、429、5xx、暂时不可用和投影协调失败使用指数退避，耗尽次数后
由 Python Service 隔离。模型只产生建议，不能直接批准。`approved` 仅登记幂等案例投影；非准入
状态登记并协调派生数据清理。该进程不执行 Milvus 全量重建，也不修改 `InvoiceExtraction`。
准入 claim 只选择 `status=pending`；已批准、拒绝、隔离、暂停或失效记录不会因租约窗口再次进入
评估。恢复 Outbox 在重试耗尽后保留终态 `failed_retryable` 供治理处理，已创建的准入任务则由
Application Service 写入可审计的 `quarantined`。
恢复记录和准入记录复用现有 `MEMORY_ADMISSION_WORKER_*` 批量、并发、租约、轮询、最大尝试次数
及指数退避配置，不新增第二套 Worker 配置。

Migration `20260909_0020` 将非当前 Schema `3.0.0` 的 ReviewedExample 标记无效、对应 Admission
追加确定性 `invalidated` Decision、清空 Worker 租约与 `next_attempt_at`，并失效派生投影。历史事实
保留但不会再次被 Worker 领取，因此旧 Schema 不再持续产生 `permanent.workflowerror`。

审核案例另由 `ExampleIndexProjectionService` 从 PostgreSQL 投影到 `ExampleIndexStore`。
投影只接收 `admission_status=approved`、`is_reviewed=true` 且 `is_valid=true` 的案例，按租户策略
`mask`、`hash` 或 `drop` 脱敏，构造带 `DOCUMENT_CONTEXT`、`FIELD_PATH`、`MODEL_VALUE`、
`REVIEWED_VALUE`、`CORRECTION_REASON`、`VENDOR_TEMPLATE_FEATURES`、`LABEL_TYPE` 分区的
检索文本，并分别生成 Dense/Sparse 载荷。`MilvusExampleIndexStore` 是可选的真实
Dense+Sparse Adapter；默认不开启连接，PostgreSQL 投影状态仍是可重试事实，Milvus 只保存
可由 PostgreSQL 完全重建的脱敏派生索引。

单案例投影查询 `GET /api/v1/memory/examples/{example_id}/projections` 只读取 PostgreSQL：先按
`tenant_id + example_id` 验证 ReviewedExample，再读取同租户 Admission 与
`example_index_projections`，支持 Index Version、状态和游标筛选。没有投影时返回空列表，不伪造
pending。响应中的 `indexed` 仅表示 PostgreSQL 已记录一次成功的派生投影，不表示该 HTTP 请求
实时查询或验证了 Milvus 实体。投影队列持久化固定 `lease_expires_at`，领取状态可返回当前租约
时间；非领取状态返回 `null`。该时间不代表 Milvus 实体健康。

`none` 仅保留为非 Milvus 开发兼容值；启用 Milvus 时，启动配置会拒绝默认或租户覆盖使用
`none`，生产派生索引必须使用 `mask`、`hash` 或 `drop`。

### 事实事件与派生投影边界

`HumanCorrection`、`CorrectionEvent`、`ReviewedExample`、准入决定、治理审计和字段目录是
PostgreSQL 中的事实记录；`example_index_projections` 与
`field_semantic_index_projections` 只保存可重建的投影状态。投影处理顺序固定为：
PostgreSQL 记录 `pending` -> 原子领取 -> 脱敏/向量化 -> Milvus 幂等写入 -> PostgreSQL
记录 `indexed`。Milvus 写入失败只留下可重试的投影失败状态，不回滚已提交的发票结果、审核事实或
治理审计。全量重建只遍历 PostgreSQL 中 `approved + is_reviewed + is_valid` 的事实，不从 Milvus
反向恢复。

当真实 Milvus 不可用时，pgvector 仅作为开发/迁移回退。其检索还必须联结
`correction_memory_sources`、`reviewed_examples` 和 `memory_admission_records`，并满足
`admission_status=approved`；`is_reviewed/is_valid` 不能单独授予长期检索资格。

租户删除、保留清理和 Schema 失效属于显式运维生命周期：应先在 PostgreSQL 记录失效事实，再登记
派生投影清理。当前物理 purge 接口是不可逆的数据保留策略例外，调用前必须由合规流程确认；它不应
被用于准入失败、投影失败或普通治理撤销。

## 多租户与案例生命周期

所有文档、运行、结果、审核和记忆 API 都从上游认证/网关写入的
`request.state.trusted_tenant_context` 获取 `tenant_id` 与 Actor，不接受客户端
`X-Tenant-ID`。`tenant_id` 只存在于技术状态、业务记录和检索 Scope，不进入
`InvoiceExtraction`。文档与运行 Repository 查询必须同时匹配资源 ID 和租户；人工提交的
`reviewer_id` 必须等于可信 Actor。原始纠错事件和审核案例写入时还会重新校验
`tenant_id + run_id + document_id` 归属。Migration `20260901_0009` 将历史核心记录隔离到
`__legacy_unassigned__`，上线前必须由管理员明确归属后才可访问。

案例只由明确人工动作生成：模型值被确认产生 `confirmed_correct`，被修正产生 `corrected`，
候选被明确否定产生 `confirmed_incorrect`；“未修改”不等于确认。三类案例及来源事件、字段证据、
页面引用、Schema/Model/Prompt 版本先写 PostgreSQL，并默认创建独立的 `pending` 准入记录。
确定性验证、审批模型建议和 Python Policy 在发票 Workflow 外独立处理；只有 `approved` 才创建
索引投影。禁用、Schema 失效和租户删除通过 PostgreSQL 投影状态传播，Milvus 可随时丢弃并重建。

`FieldSemanticCatalog` 独立于 `InvoiceExtraction`：基础显示名称、描述和类型只读自当前 Pydantic
Schema；租户别名、负向别名和上下文锚点在 PostgreSQL 中按 Catalog Version 保存，并需明确
人工审批后才参与字段绑定候选。字段目录只返回候选及冲突，不修改 Entity Schema，也不允许
“公司名”等模糊标签无条件绑定 `company_name` 或覆盖当前图片证据。

`FieldSemanticIndexProjectionService` 将合成目录投影到独立于审核案例的 Milvus Collection。
PostgreSQL 的 `field_semantic_index_versions` 与 `field_semantic_index_projections` 保存版本、重建、
租约、失败和失效状态，并在版本登记时固化完整的已审批合成目录快照；重试和重建只读取该
PostgreSQL 快照，不重新读取可能已变化的当前目录。快照首次生成时只读取 Schema 基础定义与指定
Catalog Version 中 `approved + is_valid` 的租户覆盖。Dense 文本包含 description、display name、
approved aliases 和正向 context anchors；Sparse/BM25 文本包含原始标签、标准名称和 approved
aliases。负向别名只作排除元数据，不进入正向召回文本。每个 ANN 请求先过滤 tenant/document
type/schema/catalog/index version 与 `is_valid`，索引结果仍只是字段绑定先验，不能覆盖图片证据。

`FieldSemanticBindingService` 对当前图片标签做 NFKC、空格和 Unicode 标点规范化，先匹配 Schema
标准名称与已审批别名，再在严格租户/文档类型/Schema/Catalog/Index Scope 内执行 Dense+Sparse
Hybrid Retrieval 和 Rerank。最终分数由精确匹配、召回/重排顺序、已审批上下文锚点、页面位置与
值类型兼容性共同形成，并显式计算 Top-1/Top-2 margin。只有高区分度且确定性规则全部通过时返回
`accepted`；候选接近或规则不足返回 `review_required`，无合格候选返回 `unresolved`。模糊租户
别名缺少上下文锚点时不能自动接受；远程检索、Embedding 或 Rerank 失败时同样禁止自动绑定。
所有分数均为未校准匹配/相关性分数，不是 probability，服务不读取或推断当前字段值。

现有 `extract_invoice` 节点会把只含 Schema 定义及 `approved + is_valid` 别名的版本化
`FIELD_SEMANTIC_CATALOG` 注入 Vision Prompt。Provider 的业务输出仍只能是 canonical
`InvoiceExtraction`；原始标签、页码、可选 bounding box 和上下文观察写入独立
`FieldBindingEvidence`。启用字段绑定后，非 `accepted` 结果沿原 `validate_extraction ->
request_human_review` 路由进入审核。审核接口分别展示字段值问题与字段映射候选，人工映射不会改写
当前发票值，只会幂等追加带 run/document/evidence/decision/Reviewer/Catalog 来源的 `pending`
`FieldAliasCandidate` 支持事实。该候选经来源多样性、canonical 冲突和同标签多字段冲突门禁后，
才可由租户治理人员审批并生成新的未激活 Catalog Version；在 Catalog 与索引均显式发布前，不会
进入 Prompt 或 Milvus。GraphState 只保存
页面引用、有限文本观察、候选路径和决策摘要，不保存图片 bytes/Base64 或完整向量召回结果。

`FieldAliasCandidate.revision` 初始为 1。仅新增唯一支持来源或成功治理状态迁移时原子加 1；相同
来源重放不增加 revision。审批与禁用请求必须同时提交 `expected_revision` 和 `Idempotency-Key`：
相同 Key/相同语义优先返回原决定，不同语义或 CAS 失败返回 409。候选决定追加保存前后 revision、
前后状态、Reviewer、Reason、Policy Version 与时间。审批生成的 draft Catalog Version 由
`candidate_id + target revision` 唯一确定，因此同一候选 revision 的并发审批不会创建多个版本。

别名支持度按配置时间窗口统计不同 document、template 和 Reviewer。相同规范化标签被映射到不同
canonical field path 时写入 `MemoryConflictRecord`，冲突关闭前所有相关候选均禁止晋升。禁用已
发布别名会先暂停 PostgreSQL alias，再失效该 Catalog 的活动派生索引。Catalog 回滚通过克隆历史
有效版本形成新的未激活版本，不原地复活旧版本。全局候选要求更高权限与多个已发布、仍有效的
租户 Catalog 来源；跨租户来源身份只保存 HMAC-SHA256 tenant fingerprints 和聚合计数，语义键仅
保留 Schema、document type、canonical field path 和规范化标签。全局审批不会直接注入租户 Catalog。

## Hybrid Retrieval 与 Dynamic Few-shot

Milvus 共享 Collection 将 `tenant_id` 声明为 Partition Key，每个 Dense/Sparse
`AnnSearchRequest` 在向量搜索前强制过滤 `tenant_id + document_type + field_path +
schema_version + is_reviewed + is_valid + label_type + index_version`。Dense 使用 HNSW/COSINE，
Sparse 默认使用 Milvus BM25 Function 与 `SPARSE_INVERTED_INDEX`；同类结果用 weighted/RRF
融合、按 `example_id` 去重后再经千问 Rerank。分数只表示未校准相关性，不称为概率。

`confirmed_correct`、`corrected`、`confirmed_incorrect` 分别进入
`VERIFIED_CORRECT_EXAMPLES`、`REVIEWED_CORRECTION_EXAMPLES`、
`REVIEWED_NEGATIVE_EXAMPLES`；负例不会进入正确答案区域。历史文本按不可信数据处理，不能成为
Prompt 指令。图片证据始终优先；证据不足或案例冲突只能产生候选并进入人工审核。空检索不触发
第二次 Vision 调用，检索故障降级为无历史上下文的确定性验证。

### Prompt 上下文预算与优先级

Vision 请求按固定顺序编译：`CURRENT_IMAGE_FACTS` -> `MANDATORY_BUSINESS_RULES` ->
`FIELD_SEMANTIC_CATALOG` -> 已审批正确案例 -> 已审批纠错案例 -> 已审批负例。目录和历史
区块统一标记为 `BEGIN_UNTRUSTED_DATA/END_UNTRUSTED_DATA`，其中的 OCR、备注和案例文本
不能改变 Schema、权限或 Workflow 路由。`PromptContextBudget` 通过
`INVOICE_INTELLIGENCE_VISION_PROMPT_MAX_*` 配置案例总数、分区数量、目录定义数、单区字符数
和总字符数；超出预算时按上述优先级丢弃低优先级区块，不截断成不可解析 JSON。检索为空、
检索/Embedding/Reranker 异常或上下文编译超限时，保留当前图片事实并使用空历史上下文，
不得自动填充字段。GraphState 仅保存裁剪后的脱敏引用、版本和 trace，不保存图片、向量或
完整 Prompt。

## 评估、训练与晋升

离线评估数据契约覆盖 Retrieval 的 Recall@K、HitRate@K、MRR、nDCG@K、正负分离和空召回率，
以及字段准确率、缺失识别、候选命中、错误自动填充、Review Precision/Recall。可比较无记忆、
pgvector、Dense、Sparse、Hybrid、Hybrid+Rerank 和正负 Few-shot；数据按 `document_id` 与模板
近重复分组隔离，结果绑定 tenant/dataset/index/model/Prompt/threshold 版本，只能产生
Promotion Candidate。

同一 `OfflineEvaluationService` 还支持 `trusted_memory_field_binding` 套件，固定比较无字段目录、
仅静态描述、描述+已审批别名、Dense-only、Sparse-only、Hybrid、Hybrid+Reranker 和
Hybrid+上下文锚点。版本绑定额外包含 Catalog、记忆准入 Policy 与字段绑定 Policy。Dataset 只
接收人工裁决的 `MemoryAdmissionGroundTruth`、`FieldBindingGroundTruth` 和
`ExpectedMemoryEffect`；人工初审分歧与最终裁决分开保存，模型建议不能作为 Ground Truth。
指标包括 Memory Approval Precision、Harmful Memory Admission Rate、Quarantine Rate、
Reviewer Disagreement Rate、Alias Binding Accuracy、Top-K Field Recall、Field Binding
Ambiguity Rate、Wrong-field Auto-fill Count、Memory Helpfulness Rate 与 Misleading Retrieval
Rate。每项比率同时输出分子/分母，零分母为 `null`；排名、质量和区分度分数均不是 probability。
PostgreSQL 继续通过 `evaluation_datasets/evaluation_runs` 保存完整版本化契约，并在
`evaluation_report_artifacts` 中保存不可变的 JSON/Markdown 聚合报告及 SHA-256；报告只包含
聚合值、版本和不可逆 fingerprint，不包含发票原值、图片或完整 Prompt。冻结数据集必须同时记录训练侧模板指纹和每个评估案例的模板指纹；缺失
指纹无法证明按模板隔离，会被拒绝，旧数据也不能作为晋升证据。
Repository 读取、写入和 Evaluation Run 绑定时还会校验数据库列元数据与不可变 JSON
payload 的 tenant、dataset、version 和 schema 一致性；发现漂移即 fail closed，不产生可晋升证据。

Hard Negative 导出只接收人工标签，租户默认不可混合，导出前脱敏，并按文档/模板分组切分。
Training Registry、版本化导出、产物校验和独立 PostgreSQL claim/lease Worker 已实现。默认
deterministic Stub 在未配置真实平台时 fail closed；可选 MLflow-compatible REST Adapter 只有在
显式提供 HTTPS endpoint、凭据和 artifact allowlist 后才提交远程任务。Training Job 的创建、读取、
取消和重试均要求 `training:submit`，跨租户 Job 返回 404；部署 Controller 仍为 registry-only，
不改变生产流量。

Evaluation 已增加脱敏 Dataset Snapshot、Evaluation Job/Schedule API、PostgreSQL claim/lease
任务表、确定性 Stub Worker 与 scheduler；诊断 Job 队列使用业务 PostgreSQL，真实 Suite 的
隔离证据读取尚未接入，也未完成生产环境演练。
`POST /api/v1/evaluations/suite-jobs` 可登记同租户冻结 `EvaluationDataset`、Suite 和完整版本绑定；
旧 Snapshot Job 保持 `diagnostic_only`。Suite Job 使用同一 PostgreSQL 租约队列，当前 Worker
未配置真实 Runner 时以 `evaluation.runner_unavailable` 隔离，不执行 Stub，也不生成可晋升 Run。
Application Service 已能在注入完整 Runner 后按领取 attempt 派生独立 Run ID、续租并执行全部
Suite 变体。`20260924_0039_evaluation_report_artifacts` 要求当前租约在 PostgreSQL 中确认完成 Run
与聚合报告；晋升门禁重载 Job→Run、数据集、Suite、版本与报告一致性。失去租约的迟到 Run
即使留下完成事实，也不能通过晋升门禁。Suite 执行有总时限，超时进入有限重试且不确认 Run。
Worker 已可选装配 HTTPS Runner Adapter；独立服务代码已提供，真实引擎、隔离证据账户和 token 未提供。
Promotion Candidate 的门禁输入已改为从可信 PostgreSQL 评估、产物与版本事实读取，但这不等于已部署模型或已具备自动晋升。
独立 OfflineEvaluationService 现在要求报告 Publisher 返回产物引用后才持久化 `completed`；
发布失败会记录为失败 Run。Job→Run 的续租与确认链已有隔离数据库等价测试；真实 Suite Runner
和隔离证据读取仍未在隔离环境验收，也未完成真实 PostgreSQL 并发验收。

## HITL 生命周期

1. 新建运行后状态从 `received` 进入 `processing`。
2. 验证结果为 `review_required` 时，先持久化 `pending_review` 和 `review_tasks`，再执行
   `interrupt()`；中断节点在 `interrupt()` 前不产生副作用。
3. `POST /reviews/{run_id}` 仅接受处于 `pending_review` 的运行，并要求 `Idempotency-Key`。
4. Service Layer 使用原 `thread_id` 和 `Command(resume=HumanCorrection)` 恢复 Checkpoint。
5. 完整人工修正通过 `InvoiceExtraction` 校验后写入审计记录、关闭 review task、重新验证；
   仍有问题则再次进入审核，通过则持久化结果和纠错记忆。
6. 最终状态仅为 `completed` 或 `failed`；`run_id`、`thread_id`、`document_id` 始终分离。

## 数据存储边界

| 数据 | 存储 | 隔离规则 |
|---|---|---|
| 原始文件 | `FileStorage`：development 为 Local，production 目标为 S3/OSS compatible | PostgreSQL 保存对象引用和 checksum；GraphState 不保存 Base64/bytes |
| 业务数据 | SQLAlchemy/Alembic，生产 PostgreSQL | 唯一事实源；核心资源和案例均带租户归属 |
| Workflow Checkpoint | 独立 SQLite 文件或独立 PostgreSQL database | 配置拒绝与业务库同库，也拒绝落入文件存储目录 |
| 向量纠错记忆 | `VectorMemoryStore`，PostgreSQL/pgvector 派生表 | 仅作租户隔离的开发/迁移回退，不与 Milvus 长期双查询 |
| 审核案例索引 | `ExampleIndexStore`，生产独立 Milvus Dense+Sparse | 由 PostgreSQL `example_index_projections` 重建；只保存脱敏派生数据 |
| 字段语义索引 | `FieldSemanticIndexStore`，独立 Milvus Dense+Sparse Collection | 由 PostgreSQL `field_semantic_index_projections` 中固化的已审批合成目录快照完整重建 |
| 离线评估事实 | PostgreSQL `evaluation_datasets` / `evaluation_runs` / `evaluation_report_artifacts` | 冻结 Dataset、Run 与报告产物均绑定 tenant 和完整版本；报告内容不可变且以 checksum 校验 |

向量表默认与业务表共用一个 PostgreSQL database，但使用独立 Port、表和事务；这是逻辑隔离，
不是独立数据库级故障域。若合规或容量要求物理隔离，需要后续单独的数据迁移设计。

## 本地启动

完整的首次初始化、日常启动、PaddleOCR、健康检查和停止顺序见本文顶部
“快速启动（Windows PowerShell）”。`docker-compose.yml` 已包含 API、migration、Worker、OIDC、
Milvus、MinIO 和 MLOps 等显式 profile；默认不启动任何服务，前端仍需单独启动。

开发流程不要求先完成生产验收。可使用以下命令检查本地配置、应用组合根导入和 Python 编译；
该检查不连接 PostgreSQL、Milvus、S3、OIDC 或远程 Provider，也不会把开发结果标记为生产就绪：

```powershell
.\scripts\verify-dev-baseline.ps1
```

脚本要求 `INVOICE_INTELLIGENCE_ENVIRONMENT=development`、开发认证模式和显式
`INVOICE_INTELLIGENCE_DEV_TENANT_ID`。真实 Suite Runner、生产 password file、OIDC、S3、
Milvus 完整性、备份恢复和告警验收仍由后续隔离环境任务单独执行；缺少这些条件时相关路径继续
保持 fail closed。

开发环境 `.env` 中的 `INVOICE_INTELLIGENCE_DEV_TENANT_ID=demo-tenant` 仅用于本地测试，
会安装一个明确的本地可信租户上下文；切换到 staging/production 前必须删除该配置并接入
真实认证/RBAC 中间件。

端点：

- `GET /api/v1/health`
- `GET /api/v1/ready`：生产配置 readiness；不代表外部 Provider 或派生索引已健康
- `GET /api/v1/metrics`：默认关闭；显式启用后仅暴露低基数 Worker、Provider 熔断和审计失败指标
- `POST /api/v1/documents`：上传文件，必须携带 `Idempotency-Key`
- `POST /api/v1/documents/{document_id}/extract`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/result`
- `GET /api/v1/reviews`：按审核人、优先级、状态、创建时间和 cursor 分页查询
- `GET /api/v1/reviews/{review_id}`：读取租户范围内的审核任务详情
- `POST /api/v1/reviews/{review_id}/claim`：原子领取并返回有限期 lease token
- `POST /api/v1/reviews/{review_id}/release`：释放本人持有的有效 lease
- `POST /api/v1/reviews/{review_id}/reassign`：定向转派，受让人需重新 claim
- `POST /api/v1/reviews/{review_id}/cancel`：使用 revision/CAS 明确取消任务
- `POST /api/v1/reviews/{review_id}/submit`：提交审核，必须携带 `Idempotency-Key`
- `POST /api/v1/reviews/recover-expired`：将本租户过期 lease 恢复为可重新领取状态
- `POST /api/v1/reviews/{run_id}`：deprecated 兼容提交接口，必须携带 `Idempotency-Key`
- `GET /api/v1/memory/admissions`：按状态分页查询租户记忆准入记录
- `GET /api/v1/memory/admissions/{admission_id}`：查询质量信号与完整准入决策链
- `POST /api/v1/memory/admissions/{admission_id}/approve`：人工批准，必须携带当前 revision 与 `Idempotency-Key`
- `POST /api/v1/memory/admissions/{admission_id}/reject`：人工拒绝，必须携带当前 revision 与 `Idempotency-Key`
- `POST /api/v1/memory/admissions/{admission_id}/quarantine`：人工隔离，必须携带当前 revision 与 `Idempotency-Key`
- `GET /api/v1/memory/audits`：按可信租户读取统一治理审计，支持 operation、resource、Trace、时间范围和游标筛选
- `GET /api/v1/memory/examples`
- `GET /api/v1/memory/examples/{example_id}`
- `GET /api/v1/memory/examples/{example_id}/projections`：分页查询单案例 PostgreSQL 投影状态，可按 `index_version` 和 `status` 筛选
- `POST /api/v1/memory/examples/{example_id}/disable`：必须携带 `Idempotency-Key`
- `POST /api/v1/memory/schema-versions/{schema_version}/invalidate`：必须携带 `Idempotency-Key`
- `POST /api/v1/memory/indexes/rebuild`：登记可重试重建，必须携带 `Idempotency-Key`
- `GET /api/v1/memory/indexes/{index_version}`
- `POST /api/v1/memory/feedback`：必须携带 `Idempotency-Key`
- `GET /api/v1/memory/evaluations/{evaluation_run_id}`：返回评估 Suite、完整版本绑定、聚合/分桶指标和只读 Promotion Candidate
- `GET /api/v1/field-semantics`：查询 Schema 字段目录、已审批别名和待治理别名候选
- `GET /api/v1/field-semantics/conflicts`：分页查询字段绑定冲突
- `POST /api/v1/field-semantics/conflicts/{conflict_id}/resolve`：从现有候选中确认字段映射并关闭冲突，必须携带 `Idempotency-Key`
- `POST /api/v1/field-semantics/conflicts/{conflict_id}/dismiss`：将误报或不再适用的冲突关闭，必须携带 `Idempotency-Key`
- `POST /api/v1/field-semantics/aliases/{alias_id}/approve`：审批租户别名候选，Body 必须包含 `reason + expected_revision`，并携带 `Idempotency-Key`
- `POST /api/v1/field-semantics/aliases/{alias_id}/disable`：禁用已审批租户别名，Body 必须包含 `reason + expected_revision`，并携带 `Idempotency-Key`

FastAPI Router 只调用 `DocumentIngestionService`、`ExtractionWorkflowService` 或
`MemoryGovernanceService`，不访问
Graph 节点、Checkpointer、模型或 ORM。恢复由 Service 通过 Runner 执行
`Command(resume=...)`。错误响应统一为 `{code, message}`，明确映射 400、404、409、422
和 500；治理端点另外区分 403、429 和 503。除健康检查外，所有端点都不接受客户端指定
`tenant_id`，只读取可信认证或网关中间件写入 `request.state.trusted_tenant_context` 的租户和
Actor；缺少该上下文时返回 403，不回退到客户端 Header。
`INVOICE_INTELLIGENCE_MEMORY_ADMISSION_REQUIRE_DISTINCT_SECOND_REVIEWER=true` 时，记忆准入
审批人不得是原案例 Reviewer，别名审批人不得是任一来源映射 Reviewer。准入批准还要求当前
确定性质量评估存在且无硬失败、案例仍为 reviewed/valid 且没有开放冲突；模型建议不能绕过这些
门禁。别名审批生成未激活 Catalog Version，只有后续显式 Catalog 激活和索引发布后才参与绑定。

冲突写接口只允许 `open -> resolved|dismissed`。字段映射冲突执行 `resolve` 时，
`selected_canonical_field_path` 必须同时属于冲突候选和当前 `InvoiceExtraction` Schema；Reviewer
固定取自可信上下文。一次数据库事务会锁定冲突、写入不可变解决决定、关闭冲突，并为关联的
Memory Admission 或 Field Alias 登记 `pending` 重新评估请求。该动作不修改发票值，也不会直接
批准 Admission/Alias。相同租户下重复 `Idempotency-Key` 与相同请求返回原决定；Key 重用、状态
竞争或 stale `expected_status` 返回 409。跨租户 ID 始终按 404 处理。

统一治理审计覆盖 Admission 批准/拒绝/隔离、租户 Alias 批准/禁用、Conflict resolve/dismiss，
并保留案例禁用、Schema 失效、Index 重建和 Retrieval Feedback。每个新治理写请求从可信
`TrustedTenantContext` 继承 Trace；没有上游 Trace 时由服务端生成，客户端不能指定可信 Trace。
Admission/Alias 的 `resource_version` 是提交后的 revision，Conflict 使用稳定
`resolution_decision_id`，Index/Schema 使用对应版本，缺少明确版本的 Example/Feedback 保持
`null`。权威 PostgreSQL 状态、不可变 Decision 与统一 Audit 在同一数据库事务提交；Milvus
清理或投影不参与该事务，也不能删除已提交审计。历史审计不会猜测回填，版本和 Trace 可为 `null`。
相同 `Idempotency-Key` 重放读取原 Audit，因此返回原 `audit_id` 和原 Trace，不生成第二条记录。
审计 reason 会拒绝图片/Base64 载荷并脱敏明显邮箱、电话和长编号；查询不返回幂等键哈希或请求 payload。
若进程在 Audit 提交后、幂等响应完成前退出，重放会从 Audit 恢复响应，不重复执行治理状态变更。

业务库使用 SQLAlchemy 2.x 与 Alembic，默认使用 `postgresql+psycopg://...`。pgvector 仅在
Milvus 未启用且 `CORRECTION_MEMORY_BACKEND=postgres_pgvector` 时构造为回退 Adapter。
开发若使用 `sqlite+pysqlite:///.data/business.sqlite`，必须同时设置
`INVOICE_INTELLIGENCE_CORRECTION_MEMORY_BACKEND=disabled`。业务表为
`documents`、`extraction_runs`、`extraction_results`、`review_tasks`、
`human_corrections`、`correction_events`，PostgreSQL 另有 `correction_memories` 和
`correction_memory_sources`、`reviewed_examples`、`example_index_projections`、
`field_semantic_catalog_versions`、`field_semantic_aliases`、
`field_semantic_alias_decisions`、`field_semantic_context_anchors`、
`field_alias_candidates`、`field_alias_candidate_sources`、
`field_alias_candidate_decisions`、`field_alias_global_support_snapshots`、
`memory_conflict_field_alias_candidates`、
`memory_conflict_resolution_decisions`、`memory_conflict_reevaluation_requests`、
`field_semantic_index_versions`、`field_semantic_index_projections`、
`memory_governance_audits`、`retrieval_traces` 和 `memory_retrieval_feedback`；协议幂等表为
`idempotency_requests`。
业务引擎不会自动建表，启动前必须执行 Alembic migration。

设置 `INVOICE_INTELLIGENCE_OPENAI_API_KEY` 后才构造 OpenAI Provider；模型、超时、重试、
图片 detail、上传大小、PDF 页数/DPI 和图片尺寸限制均可通过 `.env` 配置。
验证的 accepted/rejected 阈值、最小清晰度/分辨率、OCR 相似度和金额误差同样可配置；
`INVOICE_INTELLIGENCE_VALIDATION_FIELD_OVERRIDES` 接受以精确字段路径为键的 JSON 覆盖。
OCR Adapter 默认不配置；未配置 OCR 时不会因此扣分。
本地 PaddleOCR 的固定 GPU/端口/轻量模型运行基线见
[`docs/local-ocr.md`](docs/local-ocr.md)，HTTP 服务必须使用版本化 Pipeline 配置
`conf/ocr/ppocrv6_small_v1.yaml`，否则 `--pipeline OCR` 会回到 PaddleX 默认的
`PP-OCRv6_medium`。多源比对的 Provider 分数阈值、字段覆盖、字段风险等级、金额误差和 Policy
版本均可配置；不同 Provider 的未校准分数不会相加或直接比较。
Vision 与 OCR 复用同一批规范化临时页面并可有界并发；结果按固定顺序合并。OCR 不可用时记录
`unavailable` 且不扣分，Workflow 继续使用 Vision 结果。Checkpoint 不保存 Base64、完整 OCR
响应或原始 OCR 行文本，只保存审核所需的有界技术摘要。
`INVOICE_INTELLIGENCE_OCR_ENABLED=true` 只启用固定本地 Provider；
`INVOICE_INTELLIGENCE_OCR_REMOTE_PROVIDERS` 可声明零个或多个独立、与 PaddleX 官方 `/ocr`
契约兼容的远程 Provider。启用项按 `provider_name` 确定性排序后与本地结果聚合；系统不猜测其他
厂商的请求或响应 Schema。

OCR 可观测性通过 `OCRTelemetry` Application Port 输出到 Infrastructure 结构化日志：
`ocr_provider_metrics` 记录整批耗时、最终逐页成功/超时/熔断/Schema 错误计数和 Empty OCR Rate；
`ocr_page_metrics` 记录每页耗时与文本框数量；`ocr_comparison_metrics` 记录成功绑定字段数、
corroborated/conflicting/OCR-only/Vision-only 数量及 OCR 冲突审核率。事件只含 trace、计数、状态和
Provider/模型/Pipeline 配置版本，不含字段值、图片、Base64 或完整响应。Pipeline 版本通过
`INVOICE_INTELLIGENCE_OCR_PIPELINE_CONFIG_VERSION` 配置，并应与实际 YAML 文件版本同步。
指标同时写入 PostgreSQL 的敏感值隔离事件表；`GET /api/v1/memory/ocr-metrics` 返回累计聚合，
索引治理页显示调用、延迟、错误、文本框、绑定、比对 outcome 和 Provider 版本。

运行级指标由进程内 `MetricsRegistry` 暴露为 Prometheus text format，默认不启用。Worker
指标只使用 `worker`、`outcome` 等低基数标签；PostgreSQL 仍是队列 backlog、lease 和 retry
事实源。Provider 熔断和安全审计写入失败会分别计数，审计失败仍会抛错，不会被指标采集吞掉。
告警规则示例见 [`docs/observability-alerts.md`](docs/observability-alerts.md)；当前未接入
Prometheus/Grafana 或外部告警接收端，不能宣称生产告警闭环已完成。

纠错记忆的 Schema 版本、检索数量/阈值、Embedding model、vendor/template 特征字段和
脱敏策略均可配置。默认 `mask` 会在 Embedding、向量派生 JSON 和 Few-shot Prompt 前脱敏；
PostgreSQL 原始事件不被改写。`hash` 策略必须配置独立 salt。相同纠错 fingerprint 合并，
`correction_memory_sources` 保证重放不重复计数；Store 端口支持禁用错误记忆和按 Schema
版本失效。该能力仅服务发票字段纠错，不提供开放式 Agent Memory。

审核案例索引可通过 `INVOICE_INTELLIGENCE_MILVUS_ENABLED=true` 启用；生产配置强制启用
Milvus、Dense Embedding 与 Rerank。Adapter 使用版本化 Collection、Tenant Partition Key、
Scalar Filter、Dense HNSW/COSINE 和 Sparse Inverted Index（默认由 Milvus
BM25 Function 从 `redacted_content` 生成），并支持 WeightedRanker/RRF、批量 Upsert、租户
过滤删除、Alias 切换和高合规租户独立 Collection Resolver。融合使用 Milvus 2.6+ 官方
`FunctionType.RERANK` 的 weighted/RRF 策略。连接 URI、Token、超时、重试、Consistency、
批量大小、向量维度、HNSW 参数和融合权重均配置化；未配置 URI 时不会创建
Milvus 客户端。实现依据 [Milvus Hybrid Search](https://milvus.io/docs/zh/multi-vector-search.md)、
[Partition Key](https://milvus.io/docs/zh/use-partition-key.md)、
[Alias](https://milvus.io/docs/zh/manage-aliases.md) 和
[HNSW](https://milvus.io/docs/zh/hnsw.md)。

字段语义投影默认关闭；设置 `INVOICE_INTELLIGENCE_FIELD_SEMANTIC_INDEX_ENABLED=true` 后复用同一
Milvus 连接、HNSW/BM25、超时、重试、Consistency 和 batch 配置，但使用独立的
`FIELD_SEMANTIC_INDEX_COLLECTION_PREFIX` 与 `FIELD_SEMANTIC_INDEX_ALIAS`。服务不会在 FastAPI
启动时自动建版本或重建。审核案例索引通过 `/api/v1/memory/indexes/{version}/project` 与
`.../activate` 执行；字段语义索引通过 `/api/v1/field-semantics/indexes/rebuild`、
`.../{version}/project` 与 `.../{version}/activate` 执行，激活仍受完整投影门禁约束。
字段绑定也默认关闭；只有同时启用字段语义索引、Dense Embedding 与 Rerank 后，才可设置
`INVOICE_INTELLIGENCE_FIELD_SEMANTIC_BINDING_ENABLED=true`。候选数、返回数、Dense/Sparse 权重、
候选阈值、接受阈值、Top-1/Top-2 margin、Rerank 阈值、各规则分量权重和策略版本均可配置。
人工字段映射的别名学习窗口以及 document/template/Reviewer 最小支持度由
`FIELD_ALIAS_*` 配置控制。跨租户全局聚合默认不可用；只有配置独立
`FIELD_ALIAS_GLOBAL_HASH_SALT` 后，高权限治理调用才能生成不含原始 tenant ID 的聚合快照。

千问实现于 2026-09-01 对照当前官方文档核验：Vision 使用
`chat.completions.create` 与兼容的 JSON Schema，Query Rewrite 使用
`chat.completions.parse`，Embedding 使用 `embeddings.create`，文本 Rerank 使用独立
`compatible-api/v1/reranks` 的 `client.post`。默认模型
`qwen3.8-max`、`qwen3.7-text-embedding`、`qwen3-rerank`、`qwen3.8-flash` 均存在于当前官方
目录；Base URL 必须按地域/Workspace 配置，代码不内置猜测地址。依据：
[结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)、
[Embedding](https://help.aliyun.com/zh/model-studio/embedding)、
[Rerank](https://help.aliyun.com/zh/model-studio/rerank)。

`INVOICE_INTELLIGENCE_QWEN_MAX_RETRIES` 仅处理网络和可重试 HTTP 错误；返回 `200 OK` 但未通过
本地 JSON Schema、Pydantic 或证据字段归属校验时，由
`INVOICE_INTELLIGENCE_QWEN_VISION_SCHEMA_MAX_RETRIES` 控制独立的有限重试。

未配置 OpenAI API key 时，上传、查询和健康检查仍可用，提取/恢复端点返回 HTTP 503。具体
Workflow 在 FastAPI lifespan 中打开独立 Checkpointer，并绑定固定 `InvoiceExtraction`。

## 结构化记忆维护与版本治理

长期记忆不是自由文本或 Agent Memory，而是 PostgreSQL 中的 `CorrectionEvent`、`HumanCorrection`、
`ReviewedExample`、字段绑定证据、`FieldAliasCandidate`、质量评估、Admission/Alias Decision、Conflict、
Catalog/Index Version 和投影状态。人工提交先形成不可变审核事实；案例默认 `pending`，只有
`is_reviewed=true + is_valid=true + admission_status=approved` 才能进入长期检索。`confirmed_correct`、
`corrected` 与 `confirmed_incorrect` 分别进入正确、纠错和独立负例区域。

案例检索 Scope 固定包含 `tenant_id + document_type + field_path + schema_version + catalog_version`，
并在可获得模板指纹时执行 `template_fingerprint` 精确过滤；配置
`INVOICE_INTELLIGENCE_REVIEWED_EXAMPLE_RETENTION_DAYS` 后，还会按 `last_seen_at` 应用实时检索窗口。
Milvus 案例 Collection 保存对应 Catalog、模板指纹和时间戳 Scalar 字段，但只保存脱敏文本与向量。
查询仍会回查 PostgreSQL Admission、审核和有效状态，历史值永远不能覆盖当前图片证据。

`20260909_0022_reviewed_example_catalog_scope.py` 不猜测历史案例的 Catalog：旧记录标记为
`legacy-unversioned` 并 fail-closed 失效，同时追加确定性 Admission invalidation Decision、失效旧投影
和旧案例 Index Version。升级后必须通过治理入口创建并投影新的 Index Version。新审核案例优先沿用
提取时字段绑定证据中的 Catalog Version；无绑定证据时才读取当前已发布 Catalog。

失效顺序固定为 PostgreSQL 先行：案例禁用、Schema 失效、保留期到期和租户删除先更新或删除授权
范围内的案例/投影事实，再幂等清理 Milvus；别名禁用和 Catalog 失效先停止 PostgreSQL 授权，再
清理字段语义 Collection。保留期清理不会删除 `CorrectionEvent` 或原始人工审核事实。物理租户删除
仍必须由外部合规流程明确授权并确认法定保留要求，不能由后台 Worker 自行触发。

Catalog 回滚不会重新激活已 retired 的可变快照，而是把仍有效的历史快照重新签发为新的未激活
Catalog Version，经审批、投影和发布后生效；案例/字段索引回滚只允许切换到 PostgreSQL 中仍有效、
完整构建的历史 Index Version。Milvus 可随时从 PostgreSQL 的 approved/reviewed/valid 版本快照重建。
现有 Memory Admission Worker 只消费准入与恢复任务并登记单案例投影/清理，不负责全量重建、自动
训练或模型晋升；本阶段未新增开放式维护 Worker。

## 剩余工作

Training RBAC、业务数据库 secret/DSN 解析、Transaction 来源/幂等/CAS 和 Promotion 可信输入
P1 的 Evaluation queue/worker/scheduler、training sync 占位清理和 Index lease/recovery
闭环已完成；当前剩余工作、生产限制和验证证据以状态基线为准。Ruff 本轮已清理
`F401`、`F841` 和 import order，尚余 99 项长行及 Python 现代化建议。
前端“运行治理 → 后台操作”已提供评估任务查询、训练任务控制、模型晋升候选门禁和交易候选复核入口；
该入口只展示脱敏状态/版本元数据，不代表真实训练、模型部署或生产晋升已完成。
索引治理页面将未激活版本标为“未激活·待校验”，激活版本标为“已激活”；投影登记、页面查询与
`/ready` 均不构成生产验收证据。
字段语义索引激活时，服务端从指定版本的已审批字段目录重新生成 semantic ID 和源指纹，
与 PostgreSQL 投影快照及 Milvus ID/checksum 清单逐项比对；缺失或过期快照会阻断激活。
完整完成度、证据、P2/P3 任务及不得对外宣称的能力统一维护在
[`docs/project-status.md`](docs/project-status.md)。

实现依据：[OpenAI Images and vision](https://developers.openai.com/api/docs/guides/images-vision)、
[OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)、
[LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、
[LangGraph Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)。

## 参考项目

- [financial-intelligence-agent](https://github.com/leojg/financial-inteligence-agent)：Apache 2.0
  架构参考。上游没有 `NOTICE` 文件；当前主项目未复制其源码，因此没有待转录的 NOTICE
  内容。未来若复制 Apache 2.0 源码，必须遵守许可证第 4 节的 LICENSE、归属和修改声明要求。
- [ai-workflow-engine](https://github.com/cimulink/ai-workflow-engine)：仓库 README 声称 MIT，
  但实际没有 `LICENSE` 文件，故按许可证不明确处理；本项目仅重新实现 interrupt/resume、
  pending review 和崩溃恢复思想，不复制其源码。

本项目由 `love-ovo73` 于 2026 年选择 Apache License 2.0，完整文本见根目录
[`LICENSE`](LICENSE)。该许可证仅适用于本项目自身受版权保护的内容；参考项目的许可证和归属
仍按各自上游条款处理，本项目未复制其源码。

架构说明见 `docs/architecture.md`，字段决策见 `docs/invoice-schema.md`。
# Index Projection Worker

索引重建使用独立的 PostgreSQL 队列。`python -m invoice_intelligence.workers.index_rebuild`（兼容入口）或 `python -m invoice_intelligence.workers.index_projection` 只执行 `project/verify`；Alias 的 `activate/rollback` 必须由具备 `INDEX_ACTIVATE` 权限的用户显式调用。
两类队列使用 `lease_expires_at`、稳定 Worker ID 和随机 token 进行领取、续租、超时恢复及迟到写入隔离。Docker 服务为 `core` profile 的 `index-rebuild`；必须在迁移 `20260923_0034_index_lease` 后启用并配置 `INDEX_PROJECTION_WORKER_ID`。完整启动与风险见 [`docs/index-projection-worker-runbook.md`](docs/index-projection-worker-runbook.md)。

2026-09-24 在专用 PostgreSQL 数据库及真实隔离 Milvus 中完成两进程合成数据验收：两类投影的领取、续租、过期重领、迟到写入 fencing、失败重试、幂等和清单比对通过；删除新版本派生项后，激活被拒且旧 Alias 保留。复核命令、修复和未覆盖项见 [`docs/index-projection-acceptance-2026-09-24.md`](docs/index-projection-acceptance-2026-09-24.md)。该结果不代表生产环境验收。
