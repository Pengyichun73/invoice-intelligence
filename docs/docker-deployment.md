# Docker Compose 部署

## 隔离 OIDC 双人验收

日常启动隔离后端、Keycloak、PostgreSQL、Milvus、相关 Worker 和 OCR：

```powershell
cd G:\work\ai
.\scripts\manage-acceptance.ps1 -Action start
.\scripts\manage-acceptance.ps1 -Action status
.\scripts\manage-acceptance.ps1 -Action stop
```

`Auto` 模式检测到宿主机 PaddleX `8077` 的 `/ocr` 契约时复用该服务；否则使用
`compose.ocr-gpu.yml` 启动 Docker GPU OCR。明确使用 Docker：

```powershell
.\scripts\manage-acceptance.ps1 -Action start -OcrMode Docker
```

在显式 Docker 模式下，若宿主 OCR 已运行，脚本拒绝同时占用 GPU；请先自行停止宿主 OCR。
容器 OCR 内部端口 8077，不发布到主机；Worker 使用 `http://ocr:8077`。
首次构建 OCR 镜像和下载模型可能较慢；服务成功启动后由 `restart: unless-stopped`
随 Docker Desktop 恢复，无需保留 PowerShell 终端。已有 API 镜像按需显式加 `-Build`
重建。脚本不会关闭宿主 OCR，也不会删除数据库卷。前端验收页仍独立通过 15173 启动。

`compose.acceptance.yml` 使用独立 Compose 项目和数据卷，不修改默认项目。
本地 HTTP Keycloak 仅绑定 `127.0.0.1:18080`，仅用于隔离验收，不是生产认证部署。
登录前，确保 `secrets/*.txt` 中现有密码文件都是普通文件，并在当前项目根目录运行：

```powershell
docker compose -p invoice-acceptance --env-file .env.compose -f docker-compose.yml -f compose.acceptance.yml --profile core --profile auth up -d --build postgres-auth keycloak postgres postgres-checkpoint migration api extraction-worker memory-admission
.\scripts\provision-acceptance-users.ps1
.\.venv\Scripts\python.exe scripts\verify-acceptance-auth.py
if (-not (Test-Path frontend/.env.acceptance)) { Copy-Item frontend/.env.acceptance.example frontend/.env.acceptance }
cd frontend
npm install
npm run dev -- --mode acceptance --host 127.0.0.1 --port 15173 --strictPort
```

访问 `http://127.0.0.1:15173`。两个本地验收账号分别为 `reviewer-a`
（`invoice-reviewer`）和 `governor-b`（`memory-governor`），随机密码只保存在
`secrets/acceptance-users.txt`，不得提交或发送。A 同时具有 `invoice-extractor`
权限用于上传提取。验收账号使用隔离环境的占位资料，以避免 Keycloak 首次登录要求补全资料。Keycloak 中调整角色后需重新登录；
业务数据库不存储账号密码。API 使用签名、issuer、audience、租户和 reviewer claim
校验；OIDC 模式无 Token 时不得退回开发身份。

切换角色：在 `http://127.0.0.1:18080/admin/` 使用本地
`secrets/keycloak_admin_password.txt` 管理员凭据登录，选择 `invoice-acceptance`
realm，在 Users 中选择账号并通过 Role mapping 分配或移除 `invoice-reviewer`、
`memory-governor`。保持两人 `reviewer_id` 不同；角色变化后退出应用并重新登录获取新 Token。
生产环境不使用此 HTTP 管理入口或验收账号。

此处仅验证身份、权限与页面入口。`reviewer-a` 的总览会因缺少记忆读取权限显示部分数据未读取；应从“发票提取”进入其工作区，不将此提示视为后端故障。实际发票提取、审核、准入、Milvus 投影和第二张召回
仍应按下文独立逐项验收，不能由账号登录成功推定业务闭环通过。

> 当前状态：隔离 Compose 已启动并迁移至 `0045`，API、提取 Worker 与 Keycloak 健康；
> 两个账号的 OIDC PKCE、JWT 校验和 API 角色隔离已通过，浏览器可进入各自工作页面。
> 长时提取、审核、准入、投影及第二张发票召回仍未端到端验收，更不代表生产就绪。
> `evaluation-worker`、`scheduler` 已接入业务 PostgreSQL 的评估 Job queue；`training-worker` 负责训练任务
> 的提交、刷新和取消；model promotion worker 明确禁用。完整阻塞清单见
> [`project-status.md`](project-status.md)。

Compose 默认不启动任何服务，必须显式选择 profile。先准备配置和 secrets：

```powershell
Copy-Item .env.example .env
Get-ChildItem secrets\*.example | ForEach-Object { Copy-Item $_ ($_.FullName -replace '\.example$','.txt') }
docker compose --profile core config
```

常用启动方式：

```powershell
docker compose --profile core up -d --build
docker compose --profile auth up -d
docker compose --profile vector up -d
docker compose --profile vector-dev up -d attu
docker compose --profile mlops up -d
docker compose --profile object-storage up -d object-storage storage-lifecycle-worker
```

`object-storage` profile 使用固定 MinIO development image；首次启动需手工创建三个 private
Bucket：原件、渲染图片和派生文本。设置 `FILE_STORAGE_BACKEND=s3` 后 API 才切换到 MinIO；默认
仍使用 Local development Adapter。Access/Secret Key 通过 Compose 环境与 Docker secret 文件传入，
不得写入镜像、日志或预签名 URL。生产使用 S3/OSS 时复用同一 Adapter，必须使用 HTTPS、private
Bucket 和最小权限凭据。

`migration` 是一次性容器，成功后 API 和 Worker 才会启动。`index-rebuild` 已接入
`lease_expires_at`、稳定 worker ID 和条件写入 fencing；需设置 `INDEX_PROJECTION_WORKER_ID`，
并完成 `20260923_0034_index_lease` 迁移后启用。两类索引已完成隔离 PostgreSQL 双进程与真实 Milvus
合成数据完整性验收，但长期运行服务和故障恢复尚未验收，不能据此宣称生产就绪。评估 Job 由 API 登记在业务 PostgreSQL，
`evaluation-worker` 和 `scheduler` 使用相同的 password-file 解析路径领取任务；
真实 Suite 所需隔离数据和证据读取尚未接入，不会将诊断型 Job 当作真实评估。
`training-worker` 使用业务 PostgreSQL 的 Training Registry queue，并通过
Stub/远程 Adapter 执行提交、刷新和取消。调度必须由 PostgreSQL claim/lease 实现，Compose
不引入 Celery、Redis、Kafka 或 RabbitMQ。

`core` profile 还需要独立 `postgres-checkpoint` 和
`secrets/postgres_checkpoint_password.txt`。API 与 `extraction-worker` 共享该 Checkpointer，
并通过业务 PostgreSQL 的 `extraction_work_items` 领取提取及审核恢复任务。`0045` 未迁移、
任一 secret 缺失或 Worker 不运行时，不得把返回的 `run_id` 解释为已完成处理。开发机直接运行
Uvicorn 默认仍为同步模式；隔离 Compose 显式设置 `EXTRACTION_ASYNC_ENABLED=true`。

业务容器通过无密码的 `postgresql+psycopg://...@postgres:5432/...` URL 与
`INVOICE_INTELLIGENCE_BUSINESS_DATABASE_PASSWORD_FILE=/run/secrets/postgres_business_password`
连接到 PostgreSQL；Settings 在进程内组合密码，禁止同时在 URL 中提供明文密码。production
必须配置可读的绝对路径 password file；development 可继续使用本地带凭据 DSN。`.env` 的
`INVOICE_INTELLIGENCE_ENVIRONMENT` 由所有容器读取，API 不再强制 development。Checkpointer
仍需单独数据库/DSN。preflight 与 Settings 均接受 password file 的末尾换行；空值、内部换行与超长
内容会被拒绝。生产 S3 endpoint 通过 `.env.compose` 的 `OBJECT_STORAGE_ENDPOINT_URL` 指向
HTTPS 地址；示例值 `http://object-storage:9000` 仅用于开发 MinIO，生产 Settings 会拒绝 HTTP。
应用凭据通过 `.env.compose` 的 `OBJECT_STORAGE_APP_ACCESS_KEY` 和
`OBJECT_STORAGE_APP_SECRET_FILE` 单独配置；示例空值回退到开发 MinIO root 凭据，生产必须提供
独立最小权限凭据文件，不能沿用该默认值。MinIO 服务自身仍使用 `minio_root_password`。
生命周期 Worker 不依赖 MinIO 初始化容器，可单独对已配置的外部 S3 endpoint 运行；使用开发
MinIO 时应先在 MinIO Console 手工创建三个 private Bucket
（`invoice-originals`、`invoice-rendered`、`invoice-derived-text`），再启动 Worker。
S3 启动校验会读取 ACL/Policy 并拒绝公开 Principal；仍需在隔离 S3 中验证实际权限与匿名访问。
历史记录已验证临时迁移容器与独立 project 连接；本轮新提取 Worker 和 Checkpointer 尚未验收，
不要把下列 `up` 命令解释为端到端可启动证明。
MLflow 进程也必须同时获得 Access Key 与 Secret Key；
现有配置未完成该凭据闭环。API 提供 `/api/v1/health`（liveness）和 `/api/v1/ready`
（安全配置 readiness）；主要 Worker healthcheck 会通过 runtime probe 校验稳定 worker ID、
生产配置和 PostgreSQL `SELECT 1`。`storage-lifecycle-worker` 已加入同类检查。根目录
`.dockerignore` 已收敛 build context。
`/api/v1/metrics` 默认关闭；如需 Prometheus 抓取，显式设置
`INVOICE_INTELLIGENCE_METRICS_ENDPOINT_ENABLED=true`，并只通过受控网络或反向代理暴露。
该端点不代表业务事实源健康，也不提供租户、文档或资源级标签。

所有容器通过 Compose service name 通信；没有 host network。生产环境必须设置
`INVOICE_INTELLIGENCE_ENVIRONMENT=production`、`INVOICE_INTELLIGENCE_AUTH_MODE=oidc`、OIDC
issuer/audience/JWKS、TLS 证书、S3/OSS endpoint/credentials、Milvus 脱敏策略，并禁止
`LocalFileStorage`。LangSmith/PromptGenius 变量为空时不影响启动。

## 升级与回滚

原业务库 `0031` 至唯一源码 head `0044` 的备份恢复、双副本预演与已确认切换记录见
[`business-db-migration-preflight-2026-09-25.md`](business-db-migration-preflight-2026-09-25.md)。
当前运行项目的 `postgres-business`/`postgres-checkpoint` 服务与工作树 Compose 的 `postgres`
定义不一致；先解决此差异再使用下面的 Compose 升级命令。原业务库已迁移，但原业务 API/Worker 未切换。

```powershell
docker compose --profile core pull
docker compose --profile core build --no-cache
docker compose --profile core up -d
docker compose --profile core ps
```

回滚到已验证镜像 tag 后重新执行 `up -d`。不要回滚数据库 migration；需要先按应用提供的
向前兼容策略处理，禁止 `down -v`。

## 备份与恢复

隔离演练入口为 [`backup-restore-drill.md`](backup-restore-drill.md)，脚本默认只生成计划：

```powershell
.\scripts\backup-restore-drill.ps1 -Action Plan
```

脚本支持显式 `-Execute` 的隔离备份/恢复，但不会覆盖原 Compose project、执行 `down -v`
或打印凭据。恢复顺序固定为：PostgreSQL/Checkpointer/Keycloak（如启用）→ 对象存储
checksum → migration head → 事实与租户校验 → 新 Milvus Collection 完整 project/verify →
授权 API 显式 activate Alias。Milvus/etcd 是派生数据，失败时保持旧 Alias 和旧环境。

## 生产检查清单

- [ ] 所有 `secrets/*.txt` 由 secret manager 生成且未进入 Git/镜像层；业务与 Checkpointer URL 不含密码、两份 password file 可读。
- [ ] 业务库升级至 `0045`，API 与提取 Worker 指向同一独立 Checkpointer；审核恢复和重启后原 `run_id` 可继续查询。
- [ ] OIDC、TLS、反向代理和可信 tenant/reviewer claim 已验证。
- [ ] S3/OSS 与 MinIO 凭据、加密、生命周期和备份已验证；生产不使用 LocalFileStorage。
- [ ] Milvus 仅保存脱敏派生数据，PostgreSQL 是唯一事实源。
- [ ] 每个 Worker 使用稳定唯一 `worker_id`，并确认对应 claim/lease migration 已部署。
- [ ] Compose profile、资源限制、重启策略和健康检查已通过变更门禁。
- [ ] 已演练 PostgreSQL、对象存储备份恢复和 Milvus 重建。
- [ ] LangSmith/PromptGenius 未作为运行时硬依赖；空配置仍能启动。

## 隔离环境发票记忆闭环验收

仅在隔离 Compose project、脱敏样例和两个可信审核身份就绪后执行；禁止对现有业务库直接
运行本节命令。先备份隔离业务库，准备 `postgres_business_password.txt`、
`postgres_checkpoint_password.txt`、`.env` 和 `.env.compose`，核对端口及项目名不会覆盖现有容器。

```powershell
docker compose -p invoice-intelligence-acceptance --env-file .env.compose --profile core config --quiet
docker compose -p invoice-intelligence-acceptance --env-file .env.compose --profile core up -d --build
docker compose -p invoice-intelligence-acceptance --env-file .env.compose --profile core ps postgres postgres-checkpoint migration api extraction-worker memory-admission index-rebuild
docker compose -p invoice-intelligence-acceptance --env-file .env.compose --profile core exec api alembic current
```

`alembic current` 必须是 `20260927_0045_extraction_work_queue`，API 与提取 Worker 必须连接同一
Checkpointer。之后用两个可信身份按顺序保留脱敏证据：上传第一张样例并确认立即取得 `run_id`；
观察 `received -> processing -> pending_review|completed`；需要审核时由第一人提交、第二人
治理合格 Admission；逐项核对审核事实、revision、审计和投影；激活合格索引后上传第二张样例，
核对受控召回和当前图片证据优先。再分别注入 API/Worker 重启、超时、数据库暂不可用、重复
Idempotency-Key、过期 revision 和 Milvus 缺项，记录预期状态与实际状态。任何门禁未通过时
不得把首阶段标记为已验收；`rejected` 历史记录不重新批准。
`core` profile 本身不证明 Milvus 可用；投影及第二张发票召回前还必须在隔离 project 启动
`vector` profile，并配置真实的脱敏索引、Embedding/Reranker Provider 与索引激活门禁。
