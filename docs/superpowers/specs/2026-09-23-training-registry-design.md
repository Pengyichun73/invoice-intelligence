# Training Registry 与远程训练适配层设计

## 目标与边界

在不修改现有 `TrainingRun` 契约的前提下，引入独立 `TrainingJob` 控制面。FastAPI 只登记、查询、取消和重试训练意图；独立 Worker 才能调用训练 Provider、刷新远程状态并登记产物。PostgreSQL 是 Training Job、Dataset Export、Training Artifact 与审计信息的唯一事实源。

## 领域模型

- `TrainingJob` 使用 `planned/submitted/running/succeeded/failed/cancelled/quarantined` 七状态，绑定 `tenant_id`、`dataset_version`、`schema_version`、`index_version`、`model_version`、`prompt_version`。
- `TrainingDatasetExport` 是现有 `TrainingDatasetVersion` 的导出登记，不创建平行数据集事实。身份严格使用 `tenant_scope + dataset_id + dataset_version`，并保存 manifest checksum、记录数、正例/负例计数、来源和版本。单租户 Job 的可信 `tenant_id` 必须属于 `source_tenant_ids`；跨租户数据集仍要求现有显式授权，API 不接受客户端 tenant 覆盖。
- 数据准入必须同时满足 `ReviewedExample.is_reviewed=true`、`is_valid=true`、`admission_status=approved`，Hard Negative 还必须绑定已审批且有效的 `HardNegativeCandidate`。正例只允许 `confirmed_correct` 或 `corrected` 的 `reviewed_value`；`confirmed_incorrect` 只能使用 `model_value` 进入 hard-negative 区，不得成为 positive/target。
- `TrainingArtifact` 登记成功训练产生的不可变产物，保存 checksum、来源、Provider、版本绑定、审计主体和时间，不保存模型二进制、完整发票值、完整 Prompt 或 Chain-of-Thought。
- 现有 `TrainingRun` 保持原样，作为评估、部署与晋升流程引用的底层执行记录。每个 `TrainingJob` 必须一对一绑定一个由 Worker 创建的 `TrainingRun`；Job 请求同时提供或由配置解析旧契约要求的 target/provider/base model、validation/evaluation dataset、hyperparameters、code version 与 artifact references。七状态映射为：`planned -> created/export_ready`，`submitted -> submitted/queued`，`running -> running`，`succeeded -> succeeded`，`failed -> failed`，`cancelled -> canceled`，`quarantined -> unsupported/failed`。新 Registry 不改变旧枚举。
- 远程成功后，一个 PostgreSQL 事务原子推进 `TrainingJob`、`TrainingRun`，登记 `TrainingArtifact` 和现有 `ModelArtifact`；若外部成功后本地事务失败，Job 保持非终态并由相同 operation id 重放 reconcile，禁止先写 Job succeeded。

## Application Port 与服务

- `TrainingProvider`：`submit/get_job/cancel`，只接收已脱敏导出引用和版本元数据。
- `TrainingRegistryRepository`：创建、租户隔离读取、原子 claim/lease、状态推进、取消请求、重试派生，以及 TrainingJob/TrainingRun/TrainingArtifact/ModelArtifact 的成功原子提交。
- `TrainingJobService`：供 API 使用，只创建、读取、请求取消和创建重试任务，不调用 Provider。
- `TrainingWorkerService`：领取任务后提交或刷新 Provider；使用有限重试、lease、attempt 和脱敏错误码；远程成功必须同时提供可验证 artifact reference 与 checksum，否则隔离。所有更新以 `job_id + lease_token + revision` 条件写入，过期 Worker 不得覆盖新状态。

## Adapter

- deterministic stub 默认报告“不支持提交”，Worker 将任务隔离为安全错误，不伪造成功。
- remote Adapter interface 通过配置注入，不猜测具体云平台 SDK。
- MLflow-compatible HTTP Adapter 仅调用兼容 REST endpoint；MLflow 不是必选依赖，也不是业务事实源。
- LangSmith Prompt Registry 仅作为 `prompt_version` 的外部引用元数据；PostgreSQL 保存任务绑定和审计事实。

## API 与 Worker

- `POST /api/v1/training/jobs` 创建 `planned` 任务。
- `GET /api/v1/training/jobs/{job_id}` 查询租户内任务。
- `POST /api/v1/training/jobs/{job_id}/cancel` 仅登记取消请求。
- `POST /api/v1/training/jobs/{job_id}/retry` 从 `failed/cancelled/quarantined` 创建新任务并关联来源。
- `python -m invoice_intelligence.workers.training` 启动独立 Worker。FastAPI lifespan 不启动 Worker。

## 状态与恢复

- 允许转移为：`planned -> submitted|cancelled|failed|quarantined`；`submitted -> running|succeeded|failed|cancelled|quarantined`；`running -> succeeded|failed|cancelled|quarantined`。四个终态不可变，只能通过 retry 创建新 Job。
- `cancel_requested_at/by/reason` 独立保存，不额外增加状态。未提交任务由 Worker 确认后进入 `cancelled`；已提交任务只有 Provider 确认取消后进入 `cancelled`。若 Provider 已报告成功，成功优先，取消请求保留为审计事实。
- claim 只选择到期的 `planned/submitted/running` 或有取消请求的非终态任务；使用数据库行锁、稳定排序和 lease。claim 成功只递增诊断用 `claim_count`，设置 `lease_token/lease_expires_at/worker_id`。所有完成、延期和释放均条件匹配 lease token 与 revision。
- 网络超时、429、5xx 和暂时不可用可重试；只有失败重调度才递增 `failure_attempt_count`，设置 `next_attempt_at` 并指数退避。成功 submit 或成功 refresh 将 `failure_attempt_count` 重置为零，正常远程轮询不消耗错误预算。凭据缺失、Schema/tenant/dataset/version 不匹配、Provider 契约违规、artifact 校验失败为永久错误并隔离；远程明确训练失败进入 `failed`。可重试错误耗尽 `max_attempts` 后进入 `quarantined`。
- 重试创建新 `planned` Job，保留 `retry_of_job_id`，不复活终态记录。
- operation id 按动作域隔离并具组合唯一约束：`submit:{job_id}`、`cancel:{job_id}`、`commit:{job_id}`。同一动作重放复用原 ID，不同动作绝不共享幂等键。

## 产物校验与审计

- checksum 固定为下载后原始 artifact bytes 的 SHA-256 小写十六进制；Provider 返回 checksum 时必须恒定时间比较，不返回时由 Worker 在受控下载后计算。
- artifact URI 只允许配置 allowlist 中的 `https` host 或显式启用的 `s3`/`file` scheme；下载限制 timeout、最大字节数和重定向目标。默认 stub 不下载或生成产物。
- provenance 保存 `remote_job_id`、Provider、artifact URI、dataset manifest checksum、训练 dataset identity、schema/index/model/prompt/code version、规范化 hyperparameters、created_by/created_at、trace_id 和来源 Job/Run ID。
- API 写入使用 `Idempotency-Key` 的摘要和可信 Trace；审计不保存 Key 原文。创建、取消请求、重试、状态推进、失败分类和产物登记均产生不可变审计事件。
- 产物校验或 Registry 原子提交失败时不得标记 succeeded；保留远程 Job identity，由后续 claim/reconcile 重试。

## 配置与部署

- 默认 Provider 为 `stub`。
- MLflow-compatible Adapter 需要显式 endpoint、认证凭据引用和 timeout；缺失凭据时 fail closed。
- Docker Compose 新增 `mlops` profile，仅启动 Training Worker，不强制启动 MLflow 服务。
- README 与架构文档说明远程接入、失败/取消语义和未实现平台特定能力。

## 验证

增加领域状态机、Repository 租户隔离/claim/幂等、API 不执行训练、Worker 提交/刷新/取消/隔离、数据准入和 Adapter fail-closed 测试。外部平台全部使用 Stub，不访问生产服务。
