# 交易分析 API 契约

所有接口要求可信认证上下文；`tenant_id` 和 Reviewer 不从 Body/Query/Header 接受覆盖。
API Router 只调用 Application Service。规则引擎仍为开发 Mock，结果只提供 advisory。

## 派生候选

`POST /api/v1/transactions/candidates/analyze`，Header：`Idempotency-Key`，Body：
`{"run_id":"<同租户 completed 提取 Run ID>"}`。服务读取 PostgreSQL 中持久化的 Result，
使用 `seller_name`、`invoice_number` 的租户范围 hash、`invoice_date`、`invoice_total_tax_price`
和 `currency` 生成候选。缺失字段保留 `null`；不接受客户端提供金额、商户、单据号、schema version
或 `document_id`。分析和审核都会核验派生快照；历史客户端构造的候选与之不符时返回 409，
不自动信任旧事实。

## 人工审核

`POST /api/v1/transactions/candidates/{candidate_id}/review`，Header：`Idempotency-Key`，
Body：`{"decision":"confirmed","expected_revision":1}`。`decision` 可为 `confirmed`、
`dismissed`、`escalated`，不可提交 `pending`。Reviewer 来自可信上下文。响应含当前 `revision`；
成功审核将其递增。同 Key/同语义重放返回第一次审核快照，不增加审计事件；同 Key/不同语义或
过期 revision 返回 409。跨租户、未完成或不存在的来源返回 404。

审核写入和审计快照在同一 PostgreSQL 事务内。事务已提交而协议幂等完成标记未写入时，同 Key
重放可从审计快照恢复。当前不自动修复历史不可信候选；上线前需单独盘点与治理。
