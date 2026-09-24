# 原业务库迁移预演与实际切换记录（2026-09-25）

## 本次边界与环境

- 以下记录是用户确认之前的预演阶段：当时只备份原库，并在独立命名数据库 `invoice_migration_preflight_20260925_0005` 上恢复、迁移；确认后的原库 DDL 结果见文末。
- 原库实际 Alembic current 为 `20260923_0031_storage_fk`；源码唯一 head 为 `20260924_0044_code_harness_repair_route`。执行前须重新读取，不能沿用本记录假定。
- 固定候选镜像通过容器内 password file 对原库执行只读 `alembic current`，再次返回 `0031`；同镜像 `alembic heads` 返回唯一 `0044`。这只验证读取路径，不表示原库已迁移。
- 当前运行的 Compose 项目为 `invoice-intelligence`，PostgreSQL 服务名分别为 `postgres-business`、`postgres-checkpoint`，卷分别为 `invoice-intelligence_postgres_business_data`、`invoice-intelligence_postgres_checkpoint_data`。当前工作树的 `docker-compose.yml` 却定义 `postgres`，没有 `postgres-checkpoint`；直接用当前 Compose `up` 可能创建另一套服务或卷。`docker compose config` 还因缺少 `MEMORY_ADMISSION_WORKER_ID` 而失败；`--no-interpolate --services` 只证实配置定义，不代表可启动。
- 当前只运行 PostgreSQL、Milvus、etcd、MinIO 等依赖，没有 API/Worker 容器。原业务库快照时其他会话、活动会话和锁等待均为 0。没有已识别的旧版应用镜像可作即时回退目标。
- `.env` 当前配置 `CHECKPOINT_BACKEND=sqlite`，本地 `.data/checkpoints.sqlite` 存在；另有独立 `postgres-checkpoint` 容器和数据库，快照时无其他连接。不能据容器存在推断应用正在使用 PostgreSQL Checkpointer；两种数据均已备份。配置文件中的凭据不写入本记录。
- 工作树有大量原有未提交变更，本次未覆盖或提交它们；迁移候选镜像来自当前工作树，而非 Git HEAD 单独内容。

## 备份与恢复验证

备份存于 Git 忽略的本地目录 `G:\work\ai\artifacts\migration-preflight-20260925-0005`，其中可能含原业务数据，禁止上传、提交或把归档内容放入日志。业务和 PostgreSQL Checkpointer 使用 `pg_dump --format=custom --no-owner --no-privileges`；从保存到本地的归档重新复制到容器，以 `pg_restore --list` 校验并分别恢复到独立数据库。SQLite 使用 `sqlite3.Connection.backup()`，副本 `PRAGMA integrity_check` 返回 `ok`。

| 归档 | 大小（byte） | SHA-256 | 可读性验证 |
|---|---:|---|---|
| `business.dump` | 436027 | `B64BB6529EF22581B1F95FF878D51A0E0F59D8FDCB98F45AB2995611CCE87ABD` | 恢复到 `invoice_migration_preflight_20260925_0005` 成功 |
| `checkpoint.dump` | 132432 | `BDF03957574D40285B2A92504A0DFE5559E1DBAA6DB842F0FC0E15FB192763E7` | 恢复到 `invoice_checkpoint_preflight_20260925_0005` 成功，4 张表 |
| `checkpoints-sqlite.backup` | 9109504 | `2420362F7E54D6F20449D46185002F280EB28E79E3306AB0063399274A1D8435` | SQLite `integrity_check=ok` |

原业务库约 15 MB、79 张表；PostgreSQL Checkpointer 约 8 MB。两个 PostgreSQL dump 分别取快照，不能宣称跨库原子一致；正式切换必须先停止业务写入再制作新备份。对象存储未在此次数据库迁移预演中备份，因此本记录不等于完整灾难恢复验收。

## 副本迁移结果

按原库归档恢复的副本迁移前 current 为 `0031`，79 张表，未验证约束 0。以同一 PostgreSQL 实例、独立数据库和容器内 password file 执行 `alembic upgrade head`，13 个 revision 全部成功；Alembic 进程约 3 秒，包含 Docker 启动的命令整体约 7 秒。迁移后 `alembic current` 为唯一 head `0044`，96 张表，416 个约束（其中 100 个外键）、576 个有效且 ready 的索引；无未验证约束或无效索引。`alembic_version.version_num` 长度为 64。

随后从同一保存的业务归档恢复第二个独立数据库 `invoice_migration_preflight_image_20260925_0017`，使用**不挂载源码**的固定镜像 `invoice-intelligence-migration-candidate@sha256:45f5dedb444b3150bed2e47159a8fdbee8aaa20b74c0f01d131dbea4320b2725` 再次完成 13 个 revision；Alembic 进程约 2.9 秒，命令整体约 7 秒。第二副本为 `0044`、96 张表，关键事实行数一致，未验证约束与无效索引均为 0。镜像由当前未提交工作树构建，未经提交或发布；正式切换前须再次确认其 digest 未变。

| 事实表 | 原库 / 副本迁移前 | 副本迁移后 |
|---|---:|---:|
| `documents` | 31 | 31 |
| `extraction_runs` | 31 | 31 |
| `review_tasks` | 13 | 13 |
| `correction_events` | 4 | 4 |
| `reviewed_examples` | 67 | 67 |
| `stored_objects` | 31 | 31 |
| `example_index_projections` | 0 | 0 |
| `field_semantic_index_projections` | 0 | 0 |
| `promotion_candidates` | 0 | 0 |
| `transaction_analyses` | 0 | 0 |

### Revision 影响

| Revision | 主要变化与迁移前条件 |
|---|---|
| `0032` | 交易分析 revision 默认 1、审计快照可空列。 |
| `0033` | 晋升候选证据列、成对约束、每租户唯一 active 部分索引；原库 active 候选为 0。 |
| `0034` | 两类投影增加租约期限、约束和索引；旧 `processing` 会重置为 `pending`。原库两类 `processing` 均为 0。 |
| `0035` | 新建评估快照、任务、计划、报告和产物表。 |
| `0036` | 冲突重评估完成时间和状态约束；先将 PostgreSQL Alembic 版本列扩至 64。 |
| `0037`–`0038` | 评估 Job 绑定冻结数据集/Suite，并增加 Run 链接及约束；旧 Job 默认诊断类。 |
| `0039` | 新建不可变评估报告产物表与索引。 |
| `0040`–`0041` | 新建 Harness 事实表及 Repository Source Registry。 |
| `0042` | Harness Execution 唯一约束从 Task 改为 Task+Attempt。 |
| `0043`–`0044` | Harness 来源引用、下一个阶段和重试原因可空列。 |

这些 revision 含非并发 `CREATE INDEX`、`ALTER TABLE`、`CHECK`/唯一约束，可能等待或阻塞并发写入。预演时原库没有应用连接，未测得竞争场景下的锁等待和实际停机时长。不能将约 3 秒直接作为生产停机承诺。

## 待审批的原库切换门禁

1. 固定迁移和应用镜像 digest、源代码状态及部署配置；解决当前 Compose 服务名/卷差异。找出旧应用镜像 digest，并在已升级的隔离副本上验证旧镜像是否能安全读取；当前没有可用的旧应用镜像，故**不能承诺直接回切镜像**。
2. 确定实际使用的 Checkpointer backend；同时保护 PostgreSQL 与 SQLite 快照，之后按选定 backend 规划部署连接。不要把 Checkpointer 表与业务 migration 混用。
3. 停止 API、Scheduler 和所有业务写入 Worker/外部客户端；通过 `pg_stat_activity`、队列 `processing` 状态及实际运行容器复核停写。若存在未知写入者或旧 `processing`，暂停切换并处理；不得仅靠当前 Compose `stop`，因为定义与运行容器不一致。
4. 停写后重新读取原库 current、源码 heads 和表计数；重新生成业务库、实际 Checkpointer 的带时间戳备份，计算 SHA-256，并再次向**另一个独立库**恢复验证。当前预演归档只证明 2026-09-25 当时可恢复，不可当作正式切换时点备份。
5. 用户最终确认后，才对已核实的原库执行下面的迁移命令；命令目标必须再次人工核对为 `invoice_intelligence`、主机 `postgres-business`、Compose 网络 `invoice-intelligence_default`。迁移镜像固定为已预演的 digest，不能在审批后悄悄重建。

```powershell
docker run --rm --env-file .env.compose --network invoice-intelligence_default `
  --entrypoint sh invoice-intelligence-migration-candidate@sha256:45f5dedb444b3150bed2e47159a8fdbee8aaa20b74c0f01d131dbea4320b2725 `
  -lc 'printf %s "$POSTGRES_BUSINESS_PASSWORD" > /tmp/business-password; export INVOICE_INTELLIGENCE_BUSINESS_DATABASE_URL="postgresql+psycopg://$POSTGRES_BUSINESS_USER@postgres-business:5432/invoice_intelligence"; export INVOICE_INTELLIGENCE_BUSINESS_DATABASE_PASSWORD_FILE=/tmp/business-password; cd /app; alembic upgrade head'
```

6. 迁移后用只读 SQL 和 `alembic current` 核对唯一 head、关键事实行数、租户归属、约束/索引有效性与应用连接；再逐步启动经过镜像 digest 核对的 API/Worker。Alias 由授权路径显式切换，migration 不自动激活。
7. 若迁移失败，保持停写，读取真实 Alembic current 和失败 revision；不得自动 downgrade，也不得在原库上直接 `pg_restore --clean`。保留原库与旧 Alias，从已验证归档恢复到**新数据库/独立服务**核验；旧应用镜像只有在对该恢复版本验证后才用于流量回切。缺少已验证旧镜像或流量切换路径时，停止切换并人工决定。

## 确认后的实际切换与验收

用户明确确认按预演方案迁移原库。执行前重新核对原库为 `invoice_intelligence`、current 为
`20260923_0031_storage_fk`、源码和固定镜像 head 为唯一 `20260924_0044_code_harness_repair_route`；
旧 Compose project 为 `invoice-intelligence`，仅有五个依赖容器，0 个其他业务库会话、0 个
processing 案例/字段投影。工作树仍有原有未提交修改，候选镜像 digest 未变；未运行旧项目 `up`。

新备份保存于 `G:\work\ai\artifacts\business-migration-20260925-0022`，含敏感业务归档，
不可提交或上传。`business.dump` 为 436027 byte、SHA-256
`3779D1169F2780B28009A618DA103CB80F290FFBCB08B298C8BA0C14B6971740`；
`checkpoint.dump` 为 132432 byte、SHA-256
`2FF2DBCD862AA1CF69FB3C37E3C9DEDF3BEF4F944DE2F5646258C79DC575B4C2`；
`checkpoints-sqlite.backup` 为 9109504 byte、SHA-256
`2420362F7E54D6F20449D46185002F280EB28E79E3306AB0063399274A1D8435`。
两份 custom 归档通过 `pg_restore --list`，分别从本地文件恢复到独立库
`invoice_migration_cutover_restore_20260925_0022` 与
`invoice_checkpoint_cutover_restore_20260925_0022`；SQLite 副本 `integrity_check=ok`。

按上文固定 digest 命令对原库执行 `alembic upgrade head`，退出码 0，13 个 revision；Alembic
进程约 2.48 秒，命令整体约 7.96 秒。容器内 password file 路径的 `alembic current` 与业务库
`alembic_version` 均返回唯一 `20260924_0044_code_harness_repair_route`。迁移后 96 张表，
未验证约束 0、无效索引 0。Reviewed Example 67、Review Task 13、Correction Event 4、
Stored Object 31、Document 31、Extraction Run 31，迁移前后相等；Reviewed Example/Document/
Stored Object 的 tenant 非空且各有一个租户，Example→Document、Example→Run、Run→Document、
Review Task→Run 四组租户或父级失配计数均为 0。

独立 Compose project `invoice-longrun-acceptance-20260925` 使用独立 PostgreSQL、Milvus、etcd、
MinIO 卷和仅回环开放的 API 端口；固定镜像、password file 与稳定 Worker ID
`longrun-worker-1` 运行。所有长期服务健康，API `/health` 与 `/ready` 返回 200；API 连接路径
在隔离库完成临时表 INSERT/SELECT 并回滚，随后 PostgreSQL、API、Index Worker 重启并恢复健康。
隔离环境没有合格投影队列，故此次不声称长期容器已重演 claim/lease/fencing 或 Milvus 全量
投影；双进程合成数据与失配门禁证据见索引验收记录。没有激活 Alias，旧项目五个依赖容器
仍健康。旧项目没有 API/Worker，且缺已验证旧应用镜像；原业务流量切换、OIDC/TLS/S3 生产
安全边界与故障时应用镜像回退仍未执行。失败恢复入口为本节新备份恢复至独立库并核对事实，
不得在原库自动 downgrade 或覆盖恢复。
