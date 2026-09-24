# 隔离故障恢复验收记录（2026-09-25）

## 范围与保护

- 源 project：`invoice-longrun-acceptance-20260925`；最终恢复 project：
  `invoice-restore-20260925-index`。两者的 PostgreSQL、Checkpointer、MinIO、Milvus、etcd
  卷均由各自 Compose 标签隔离。原 `invoice-intelligence` project 与原卷未执行恢复或清理。
- 源隔离业务库为 `invoice_longrun_acceptance`，API 实际 Checkpointer 为独立 PostgreSQL
  `postgres-checkpoint/checkpoint`。可选 `postgres-auth` 未启用，因此 Keycloak 未执行备份恢复。
- 仅写入合成两租户恢复哨兵和索引案例；没有真实发票、生产密钥或原业务库写入。

## 脚本缺口及修复

原 `scripts/backup-restore-drill.ps1` 固定工作树 Compose 的服务名、遗漏 Checkpointer、无条件
尝试认证库、假设不存在的 `object_storage_data` 卷；PowerShell 管道处理 binary dump、
未在恢复前校验业务归档 SHA-256，且使用可能覆盖现有数据的 `pg_restore --clean`。
现按实际容器/卷 Compose 标签、数据库名和 Checkpointer backend 定位；`docker cp` 保存
custom-format dump，manifest 记录逐文件 SHA-256，恢复前校验归档与目标 project/卷不存在，
仅向空数据库恢复。SQLite 分支会拒绝缺失的 checkpoint 文件；本轮使用独立 PostgreSQL 分支。
`Restore` 必须提供单独端口覆盖文件。脚本不执行 `down -v`、原库 downgrade 或 Alias 激活。

首次 SQLite 备份生成了 87-byte 空 Checkpointer 卷归档，未用于恢复验收。首次恢复目标
`invoice-restore-20260925-pgcheck` 因本机 Compose `create` 不支持 `--no-deps` 而停止，
仅创建并启动空 PostgreSQL；保留该失败目标，不作为成功证据。修正后使用全新目标完成
业务库、独立 Checkpointer 与 MinIO 恢复；最终再用含合格案例的新归档在全新目标重演。

## 最终命令与归档

在 `G:\work\ai` 执行，`-ComposeFile` 指向
`artifacts\longrun-acceptance-20260925\compose.yml`：

```powershell
pwsh -NoProfile -File scripts/backup-restore-drill.ps1 -Action Backup `
  -ComposeProject invoice-longrun-acceptance-20260925 `
  -ComposeFile G:\work\ai\artifacts\longrun-acceptance-20260925\compose.yml `
  -BackupDirectory G:\work\ai\artifacts\restore-drill-20260925-index -Execute
pwsh -NoProfile -File scripts/backup-restore-drill.ps1 -Action Restore `
  -ComposeProject invoice-longrun-acceptance-20260925 `
  -ComposeFile G:\work\ai\artifacts\longrun-acceptance-20260925\compose.yml `
  -BackupDirectory G:\work\ai\artifacts\restore-drill-20260925-index `
  -RestoreProject invoice-restore-20260925-index `
  -RestoreOverrideFile G:\work\ai\artifacts\restore-drill-fixture\restore-index-override.yml -Execute
```

恢复后在独立 API 容器中执行脱敏索引重建和故障注入：

```powershell
docker cp .\scripts\verify_index_projection_isolated.py `
  invoice-restore-20260925-index-api-1:/tmp/verify-index-recovery.py
docker exec -e INDEX_ACCEPTANCE_TAG=20260924_1650 -e INDEX_RECOVERY_DRILL=1 `
  -e INDEX_ACCEPTANCE_PHASE=rebuild invoice-restore-20260925-index-api-1 `
  python /tmp/verify-index-recovery.py
docker exec -e INDEX_ACCEPTANCE_TAG=20260924_1650 -e INDEX_RECOVERY_DRILL=1 `
  -e INDEX_ACCEPTANCE_PHASE=alias invoice-restore-20260925-index-api-1 `
  python /tmp/verify-index-recovery.py
docker exec -e INDEX_ACCEPTANCE_TAG=20260924_1650 -e INDEX_RECOVERY_DRILL=1 `
  -e INDEX_ACCEPTANCE_PHASE=alias_check invoice-restore-20260925-index-api-1 `
  python /tmp/verify-index-recovery.py
```

最终 manifest 的 `frozen_at_utc=2026-09-24T16:50:00.6819005Z`；
备份只在本地 Git 忽略目录，含合成业务数据和凭据相关存储元数据，不上传。

| 归档 | byte | SHA-256 |
|---|---:|---|
| `business.dump` | 340459 | `FB3D9118F60036FDFB1FB92C4A962A7DAE07DC4238FA5F6EC2957BF2373E6C60` |
| `checkpoint.dump` | 6307 | `86218381753C9B6266EAC4D32FFCD0664B8F29D5C2C27243C87655D670CF1EE2` |
| `object-storage.tgz` | 13289 | `57532C99D7A681D0DCC7B58682E90447B1CCC4812109135FBF014DB18E5E1DEE` |

恢复前全部 SHA-256 匹配；故意篡改 `business.dump` 后，脚本在创建目标容器/卷之前
报 checksum 失配。已存在的恢复 project 也被拒绝覆盖；原 project 名在命令入口被拒绝。

## 恢复与派生重建结果

- RPO：在本次冻结的合成写入中，源与恢复库逐项计数一致，**观察到丢失 0 条**；
  最近可恢复点是上述 `frozen_at_utc`。这不是连续备份的生产 RPO 保证。
- RTO：从全新 project 恢复开始到全部服务健康，**65.87 秒**；仅代表这组小规模合成数据。
- 业务库实际 Alembic head `20260924_0044_code_harness_repair_route`；Document 7、
  StoredObject 2、ReviewTask 2、CorrectionEvent 2、ReviewedExample 5，其中 admission
  `approved=4`、`rejected=1`。两租户对象归属及 Example→Document 租户失配均为 0；
  未验证约束和无效索引均为 0。
- Checkpointer 恢复后有 4 张表、1 条可读的空载荷合成 checkpoint。MinIO 两个对象从恢复
  存储读取后与 PostgreSQL `stored_objects.checksum` 逐项一致（2/2）；另一个租户的
  Document 下载入口返回 404。API `/ready` 返回 200。
- 从恢复后 PostgreSQL 的 4 条合格案例及字段目录，使用无外部模型请求的合成 Embedding，
  在**恢复环境自己的 Milvus** 新建 Collection 并完成双 Worker 投影：Reviewed Example 4/4、
  Field Semantic 19/19，ID、checksum、数量逐项与 PostgreSQL 清单一致；拒绝案例未入索引。
  续租、过期重领、迟到 fencing、重试和幂等亦在这组隔离进程中通过。
- 仅在恢复环境激活合成旧版本 Alias。下一版本完整投影后删除一条 Example 和一条 Field
  Semantic 项，`verify_index_version` 均失败；新版本激活被拒绝，旧 PostgreSQL 活动版本和
  旧 Milvus Alias 仍在。原环境和原 Alias 未被触及。

## 未执行项与剩余风险

- `auth_enabled=false`，没有 Keycloak 数据可恢复；脚本支持可选认证库，但该分支未实测。
- 生产 SQLite Checkpointer 分支未实测；原首次空卷归档证明缺文件时必须 fail closed。
- 此 MinIO 卷也承载隔离 Milvus 内部 bucket；业务对象与派生数据共卷限制了生产恢复粒度。
  Milvus 新 Collection 已从 PostgreSQL 重建，不能把 MinIO 中的派生 blob 当作事实源。
- 隔离 API 仍配置 LocalFileStorage；本轮通过 MinIO 客户端读取合成对象并与 PostgreSQL
  引用逐项对照，未验收应用 S3 Adapter 的同租户下载路径。
- 未进行生产规模、持续写入、跨主机备份、对象存储策略/生命周期、故障自动切流或生产
  Alias 激活演练；manifest 只有 checksum，没有签名或离站保存。保留旧环境与全部备份，
  失败时以新 project 重试，不在原库覆盖恢复。
- 首次失败目标与中间恢复目标的容器已停止，卷和归档保留；最终恢复 project 与源隔离
  project 仍健康运行。
