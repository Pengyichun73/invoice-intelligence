# 隔离备份与恢复演练

本文是 Invoice Intelligence 的恢复演练入口。2026-09-25 的隔离执行证据见
[`backup-restore-acceptance-2026-09-25.md`](backup-restore-acceptance-2026-09-25.md)；
这不是生产恢复验证。演练不得连接生产数据库、对象存储、Milvus 或 Keycloak。

## 事实边界

- 业务 PostgreSQL：审核事实、CorrectionEvent、发票结果、版本注册、投影状态和审计的唯一事实源。
- Checkpointer：实际为独立 PostgreSQL 时单独 dump/restore；development SQLite 时备份其
  独立卷，文件缺失则拒绝备份，不把空卷当作可恢复 checkpoint。
- Keycloak PostgreSQL：仅在启用 `auth` profile 时备份；它不是业务事实源。
- 对象存储：原始文件、渲染图片和派生文本的受控对象；数据库只保存引用、checksum、租户和状态。
- Milvus/etcd：脱敏派生索引和元数据，不参与事实恢复；应从 PostgreSQL 事实重建。

## 运行方式

仅生成计划（默认，不连接服务）：

```powershell
.\scripts\backup-restore-drill.ps1 -Action Plan
```

在隔离 Compose project 执行备份：

```powershell
.\scripts\backup-restore-drill.ps1 -Action Backup `
  -ComposeProject invoice-longrun-acceptance-20260925 `
  -ComposeFile .\artifacts\longrun-acceptance-20260925\compose.yml `
  -BackupDirectory .\artifacts\restore-drill-20260925-index -Execute
```

恢复必须使用不同的 project 名称和独立 volumes：

```powershell
.\scripts\backup-restore-drill.ps1 -Action Restore `
  -ComposeProject invoice-longrun-acceptance-20260925 `
  -ComposeFile .\artifacts\longrun-acceptance-20260925\compose.yml `
  -BackupDirectory .\artifacts\restore-drill-20260925-index `
  -RestoreProject invoice-restore-20260925-index `
  -RestoreOverrideFile .\artifacts\restore-drill-fixture\restore-index-override.yml -Execute
```

脚本仅接受 `invoice-longrun-acceptance-*` 源 project 和 `invoice-restore-*` 新目标；
实际容器/卷 Compose 标签与隔离业务库名须匹配。每次恢复使用尚不存在的目标 project 和卷，
并提供独立 API 端口覆盖文件。不会打印 DSN、密码、Token、Access Key、Secret Key 或对象内容；
不会执行 `down -v`、`pg_restore --clean` 或自动删除卷。生产环境禁止直接使用该脚本。

## 备份步骤

1. 使用 `pg_dump --format=custom --no-owner --no-privileges` 备份业务 PostgreSQL。
2. 按 API 实际 backend 备份独立 PostgreSQL Checkpointer 或非空 SQLite checkpoint 文件；
   manifest 记录 backend 和归档 SHA-256。
3. `postgres-auth` 实际运行时才备份；未启用时记录 `auth_enabled=false`。
4. 停止隔离 API、Index Worker、Milvus、MinIO 后以只读挂载归档 MinIO 卷，再恢复源服务。
5. 生成不含凭据的 manifest，记录冻结时间、Compose project、各文件 SHA-256 和 schema head。

## 恢复与一致性校验

1. 校验全部归档 SHA-256，拒绝已经存在的目标 project/卷，保留旧环境和旧 Alias。
2. 恢复业务 PostgreSQL、实际 Checkpointer 和（如启用）Keycloak PostgreSQL 到新空卷。
3. 恢复对象存储卷并核对 `StoredObject` 引用、大小、租户和内容 checksum。
4. 启动一次性 migration 容器；在已恢复 head 上 `alembic upgrade head` 必须幂等。
   核对实际 `alembic current`，不得回滚 migration。
5. 核对审核事实、CorrectionEvent、ReviewTask、版本 Registry 和审计记录；跨租户查询必须仍返回 404。
6. 使用脱敏的 approved、`is_reviewed=true`、`is_valid=true` 投影事实，在新 Collection 中执行 register/project/verify。
7. 只有完整投影和验证通过，授权用户才能显式 activate 对应 Alias。禁止在旧活动 Collection 上原地重建。
8. 如果投影、校验、Alias 切换或 PostgreSQL 注册任一步失败，保持旧 Alias/旧环境，记录安全错误码并从 PostgreSQL 重新登记待处理任务。

## 验收记录

每次演练至少记录：

- RPO：最后一个可恢复备份时间点。
- RTO：从隔离卷恢复到可读业务数据的耗时。
- PostgreSQL/对象存储 checksum 与行数抽样结果。
- migration head、租户隔离、审核事实和 CorrectionEvent 校验结果。
- Milvus Collection project/verify 计数、脱敏检查和 Alias 激活结果。
- 失败步骤、保留的旧 Alias、重试入口和人工决策。

隔离恢复已执行，合成数据观察到丢失 0 条，恢复环境就绪耗时 65.87 秒；生产 RPO/RTO、
Keycloak 可选分支、生产规模和连续写入故障场景仍未验收。
