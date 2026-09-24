# Docker Compose 部署

> 当前状态：Compose profiles 可用于配置结构验证和开发编排，但尚未达到生产就绪。PostgreSQL
> password secret 已通过统一 Settings 接入 API、migration 和 Worker，尚未在容器中做连接验证；
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
docker compose --profile object-storage up -d object-storage object-storage-init storage-lifecycle-worker
```

`object-storage` profile 使用固定 MinIO development image，并由一次性 init 容器创建三个 private
Bucket：原件、渲染图片和派生文本。设置 `FILE_STORAGE_BACKEND=s3` 后 API 才切换到 MinIO；默认
仍使用 Local development Adapter。Access/Secret Key 通过 Compose 环境与 Docker secret 文件传入，
不得写入镜像、日志或预签名 URL。生产使用 S3/OSS 时复用同一 Adapter，必须使用 HTTPS、private
Bucket 和最小权限凭据。

`migration` 是一次性容器，成功后 API 和 Worker 才会启动。`index-rebuild` 已接入
`lease_expires_at`、稳定 worker ID 和条件写入 fencing；需设置 `INDEX_PROJECTION_WORKER_ID`，
并完成 `20260923_0034_index_lease` 迁移后启用。当前尚缺隔离 PostgreSQL 双 Worker 与真实 Milvus
完整性演练，不能据此宣称生产就绪。评估 Job 由 API 登记在业务 PostgreSQL，
`evaluation-worker` 和 `scheduler` 使用相同的 password-file 解析路径领取任务；
真实 Suite 所需隔离数据和证据读取尚未接入，不会将诊断型 Job 当作真实评估。
`training-worker` 使用业务 PostgreSQL 的 Training Registry queue，并通过
Stub/远程 Adapter 执行提交、刷新和取消。调度必须由 PostgreSQL claim/lease 实现，Compose
不引入 Celery、Redis、Kafka 或 RabbitMQ。

业务容器通过无密码的 `postgresql+psycopg://...@postgres:5432/...` URL 与
`INVOICE_INTELLIGENCE_BUSINESS_DATABASE_PASSWORD_FILE=/run/secrets/postgres_business_password`
连接到 PostgreSQL；Settings 在进程内组合密码，禁止同时在 URL 中提供明文密码。production
必须配置可读的绝对路径 password file；development 可继续使用本地带凭据 DSN。`.env` 的
`INVOICE_INTELLIGENCE_ENVIRONMENT` 由所有容器读取，API 不再强制 development。Checkpointer
仍需单独数据库/DSN。当前未实际启动容器验证连接，不要把下列 `up` 命令解释为端到端可启动证明。
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

- [ ] 所有 `secrets/*.txt` 由 secret manager 生成且未进入 Git/镜像层；业务 URL 不含密码、password file 可读。
- [ ] OIDC、TLS、反向代理和可信 tenant/reviewer claim 已验证。
- [ ] S3/OSS 与 MinIO 凭据、加密、生命周期和备份已验证；生产不使用 LocalFileStorage。
- [ ] Milvus 仅保存脱敏派生数据，PostgreSQL 是唯一事实源。
- [ ] 每个 Worker 使用稳定唯一 `worker_id`，并确认对应 claim/lease migration 已部署。
- [ ] Compose profile、资源限制、重启策略和健康检查已通过变更门禁。
- [ ] 已演练 PostgreSQL、对象存储备份恢复和 Milvus 重建。
- [ ] LangSmith/PromptGenius 未作为运行时硬依赖；空配置仍能启动。
