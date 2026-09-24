# 隔离备份与恢复演练

本文是 Invoice Intelligence 的恢复演练入口。它描述可重复的隔离流程，不代表已经完成生产恢复验证。演练不得连接生产数据库、对象存储、Milvus 或 Keycloak。

## 事实边界

- 业务 PostgreSQL：审核事实、CorrectionEvent、发票结果、版本注册、投影状态和审计的唯一事实源。
- Checkpointer PostgreSQL：LangGraph 恢复状态，必须作为独立故障域单独备份和恢复。
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
.\scripts\backup-restore-drill.ps1 `
  -Action Backup `
  -ComposeProject invoice-intelligence-drill `
  -BackupDirectory .\artifacts\backup-drill `
  -Execute
```

恢复必须使用不同的 project 名称和独立 volumes：

```powershell
.\scripts\backup-restore-drill.ps1 `
  -Action Restore `
  -ComposeProject invoice-intelligence-drill `
  -RestoreProject invoice-intelligence-restore `
  -BackupDirectory .\artifacts\backup-drill `
  -Execute
```

脚本不会打印 DSN、密码、Token、Access Key、Secret Key 或对象内容；不会执行 `down -v`，也不会自动删除卷。生产环境禁止直接使用该脚本。

## 备份步骤

1. 使用 `pg_dump --format=custom --no-owner --no-privileges` 备份业务 PostgreSQL。
2. 对独立 Checkpointer 数据库执行同等 custom-format 备份，并在 manifest 中记录其校验结果。
3. auth profile 启用时，备份 `postgres-auth`；未启用时明确记录“未启用”。
4. 以只读方式归档对象存储卷，生成 SHA-256 checksum。原件、渲染图和派生文本仍保持独立 Bucket/前缀。
5. 生成不含凭据的 manifest，记录时间、Compose project、文件名、checksum、schema head 和演练操作者。

## 恢复与一致性校验

1. 停止隔离环境中的 API/Worker，保留旧环境和旧 Alias。
2. 恢复业务 PostgreSQL、Checkpointer 和（如启用）Keycloak PostgreSQL 到新卷。
3. 恢复对象存储并校验归档 checksum；逐项抽样核对 `StoredObject` 引用、媒体类型、大小、租户和状态。
4. 启动一次性 migration 容器，执行 `alembic upgrade head`；不得回滚 migration。记录 `alembic current` 与预期 head。
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

当前尚未执行真实隔离恢复；因此不能宣称 RPO/RTO、容器凭据连接或 Milvus 全量重建已经通过生产验收。
