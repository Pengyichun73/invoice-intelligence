# Index Projection Worker 运维手册

`register` 创建新的 PostgreSQL index version 和 pending projection rows。Worker 只执行 `project` 和 `verify`，不执行 Alias 切换。只有全部 eligible rows 为 `indexed` 且 Milvus collection 校验通过后，授权用户才能调用 `activate`。`rollback` 只能切换到已验证且仍有效的旧版本。

先将业务库迁移到 `20260923_0034_index_lease`。配置 `INVOICE_INTELLIGENCE_INDEX_PROJECTION_WORKER_ENABLED=true`、稳定且不同实例唯一的 `INVOICE_INTELLIGENCE_INDEX_PROJECTION_WORKER_ID` 和明确的 `INVOICE_INTELLIGENCE_INDEX_PROJECTION_WORKER_TENANT_IDS=["tenant-id"]`，启动 `python -m invoice_intelligence.workers.index_projection`。Docker 使用 `core` profile 的兼容服务名：`docker compose --profile core up -d index-rebuild`；Compose 从 `INDEX_PROJECTION_WORKER_ID` 设置进程内 Worker ID。启用前还需配置可用的 Milvus 与 Embedding Provider，不能仅凭 Compose 可解析就视为已运行。

投影行由 PostgreSQL `FOR UPDATE SKIP LOCKED` claim。每次领取生成新的 `lease_token` 和 `lease_expires_at`，远程 Embedding/Upsert 期间按租期的三分之一续租。完成、失败和续租均由 PostgreSQL 条件更新校验 tenant、index version、worker、token 及未过期租约；旧 Worker 的迟到写入被拒绝，不得清除新 Worker 已写入的派生文档。过期行可重新领取，达到最大 attempt 后保持 `failed` 待人工处置。每次只领取一个任务，避免批量等待期间租约过期；失败写入安全 `last_error_code`、`attempt_count` 和指数退避 `next_attempt_at`。原始审核事实不会被 Worker 修改。

内建 Milvus Adapter 的激活会读取目标 Collection 全部 ID 和 projection checksum，与 PostgreSQL
合格事实逐项比对；这验证的是派生投影身份及内容 checksum，不重算向量数值。Alias 仍须授权用户
显式激活；未经隔离 PostgreSQL 双 Worker 与真实 Milvus 演练，不宣称整套索引部署已生产就绪。
两类 Alias 按租户隔离；切换后若 PostgreSQL 注册失败，会尝试恢复先前 Alias。首次激活或恢复 Milvus 失败时仍可能出现暂时不一致，需按 PostgreSQL 活动版本人工核对并重新切换。

Milvus 仅保存脱敏派生内容。日志只输出 tenant、index version、状态、计数和安全错误码，不输出原始值、图片、Base64、完整 Prompt、完整向量或远程响应。

恢复演练必须从 PostgreSQL approved/reviewed/valid 事实重新 register、project、verify 新
Collection；不得把 Milvus volume 当作事实备份。请按 [`backup-restore-drill.md`](backup-restore-drill.md)
记录 checksum、project/verify 计数、Alias 保留和失败恢复结果。
