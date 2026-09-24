# 索引投影隔离验收记录（2026-09-24）

## 边界与环境

- 使用 Compose 项目 `invoice-intelligence` 中已运行的 `postgres-business`（PostgreSQL 16）与 `milvus`（Milvus 3.0.0）；没有连接远程模型或生产环境。
- 原业务库 `invoice_intelligence` 的实际 Alembic head 为 `20260923_0031_storage_fk`，未迁移。验收最终库 `invoice_index_acceptance_20260924_1553` 是同一 PostgreSQL 实例中的独立数据库，迁移至源码 head `20260924_0044_code_harness_repair_route`。
- 合成租户 `index-acceptance-20260924_1553`、专用 Collection 前缀和 Alias 与现有数据隔离。5 条虚构审核案例中 4 条 `approved`、1 条 `rejected`；只使用 `SYNTHETIC` 值和 `isolated://` 引用。
- 两个 `multiprocessing spawn` Worker 同时启动，分别创建数据库 engine 和 Milvus client。它们在一个临时验收容器内，不是生产服务副本。

## 可复核执行

入口：[`scripts/verify_index_projection_isolated.py`](../scripts/verify_index_projection_isolated.py)。脚本强制数据库名为 `invoice_index_acceptance_<INDEX_ACCEPTANCE_TAG>` 且主机为 `postgres-business`；需要先创建独立数据库并执行 `alembic upgrade head`。临时容器从 `.env.compose` 读取业务凭据，在容器内写入临时 password file；命令和输出均不打印密码。

最终运行参数：`INDEX_ACCEPTANCE_TAG=20260924_1553`、`PYTHONPATH=/workspace/src`，只读挂载 `src` 与 `scripts`，网络 `invoice-intelligence_default`，镜像 `invoice-intelligence-index-acceptance:20260924`。主阶段运行 `python /workspace/scripts/verify_index_projection_isolated.py`；同一库的后续阶段设置 `INDEX_ACCEPTANCE_PHASE=alias`，失配后的只读复核设置 `INDEX_ACCEPTANCE_PHASE=alias_check`。阶段具有顺序依赖，不可在同一库重复执行初始 seed。

主阶段实际输出：

```json
{"worker":"worker-2","examples":1,"fields":1}
{"worker":"worker-1","examples":1,"fields":1}
{"head":"20260924_0044_code_harness_repair_route","workers":2,"example_count":4,"field_count":19,"eligible_source_count":4,"tenant":"index-acceptance-20260924_1553","example_version":"examples-20260924_1553","field_version":"fields-20260924_1553","checksums_match":true}
```

脚本在主阶段还执行两类投影的故障注入与 PostgreSQL 退避后重试、租约续租、到期重领、旧 token 迟到提交拒绝，然后逐项比较 PostgreSQL 合格源、投影表与 Milvus manifest。`rejected` 案例 ID 未出现在 manifest。后续阶段确认重复领取无新增投影，先激活完整旧版本，再构建完整新版本，各删除一条新版本 Milvus 项；两类 `verify_index_version` 均返回 false、`activate_index_version` 均拒绝。后续阶段曾因验收脚本误把 `list_aliases()` 返回的字典当列表而在最终断言失败；前面的拒绝激活与 PostgreSQL 活动版本断言已通过。修正脚本后只读复核输出：

```json
{"missing_manifest_blocks_activation":true,"old_postgres_versions_preserved":true,"old_milvus_aliases_preserved":true}
```

第三阶段 `INDEX_ACCEPTANCE_PHASE=integrity` 对两类独立新版本各投影完整清单，再依次注入额外 ID、篡改 checksum、写入错租户和错版本项；每次门禁均拒绝，恢复原始项后重新通过。错租户/版本项存在时尝试激活亦被拒，旧 PostgreSQL 活动版本未变。实际输出：

```json
{"both_kinds":true,"extra_item_rejected":true,"checksum_tamper_rejected":true,"wrong_tenant_and_version_rejected":true,"old_versions_preserved":true}
```

## 实测缺口与修复

1. Docker 镜像构建曾因 `pyproject.toml` 的旧式 `license` 写法不符合当前 setuptools 校验、且缺少 `LICENSE` 文件而失败；改为 SPDX 字符串并复制许可证后构建通过。
2. Fresh PostgreSQL 迁移在 revision `20260924_0036_conflict_reevaluation` 处因 `alembic_version.version_num` 的 32 字符长度不足失败；该迁移在 PostgreSQL 上先扩为 64，隔离库迁移至 head 通过。
3. 两个 Worker 同时初建 Milvus Collection 时，`load_collection` 曾返回 code 700（索引尚未就绪）；两类 Adapter 仅对该错误增加最多 20 次、每次 0.25 秒的有界等待。
4. 随后真实 PyMilvus 对两类 `upsert` 均拒绝 tuple 批次：`expected 'Dict' or list of 'Dict', got 'tuple'`；调用边界改为 list 后完整实测通过。

本地定向回归：`17 passed`；两个 Milvus Adapter 和验收脚本的 Ruff、脚本 `py_compile` 均通过。

## 未执行与剩余风险

- 破坏注入针对单条合成项及两个新版本；未执行大规模数据、并发注入或持续漂移监测。
- 未执行真实生产部署的两个长期运行容器、网络故障/重启、备份恢复、容量压力、外部告警或实际租户数据验收。
- 失败诊断阶段留下专用数据库 `invoice_index_acceptance_20260924_1535`、`..._1548`、`..._1550` 及对应测试 Collection；最终验收库和 Collection 也保留供复核。清理这些隔离资源需单独确认使用方后进行。
- 原业务库仍停留在 `20260923_0031_storage_fk`；现行源码要求升级到 `20260924_0044_code_harness_repair_route`，本次未对原业务库执行迁移。
