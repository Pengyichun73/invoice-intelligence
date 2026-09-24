# Human Review Task API

Human Review Task 是 PostgreSQL 审核任务事实。API 身份来自已验证 OIDC/网关上下文，
请求 Body、Query 和普通 Header 不能覆盖 `tenant_id` 或操作人身份。

## 状态

```text
pending_review -> claimed -> submitted
       |             |
       |             +-> expired -> claimed
       +---------------------------> cancelled
```

- `pending_review`：Workflow 已中断，等待领取。
- `claimed`：任务由一个可信 Reviewer 持有，具有有限 lease。
- `expired`：lease 已过期并由恢复操作确认，可重新领取。
- `submitted`：审核事实已持久化；Workflow 恢复及记忆准入分别处理。
- `cancelled`：任务被明确取消，不能再次提交。

所有状态写入使用 `expected_revision`。revision、状态或 lease 已变化时返回 `409`；
资源不存在或属于其他租户时统一返回 `404`。

## 查询

```http
GET /api/v1/reviews?reviewer_id=...&priority=80&status=claimed&limit=50&cursor=...
GET /api/v1/reviews/{review_id}
```

列表支持 `reviewer_id`、`priority`、`status`、`created_from`、`created_to` 和不透明 cursor。
详情包含有界审核证据和当前发票结构，不包含原图、Base64、完整远程响应或 Prompt。

## 领取和释放

```http
POST /api/v1/reviews/{review_id}/claim
{"expected_revision": 1, "lease_seconds": 900}

POST /api/v1/reviews/{review_id}/release
{"expected_revision": 2, "lease_token": "<64 hex characters>"}
```

claim 响应单独返回 `lease_token`。普通列表和详情不返回 token。调用方不得记录 token。

## 转派和取消

```http
POST /api/v1/reviews/{review_id}/reassign
{"expected_revision": 2, "lease_token": "...", "target_reviewer_id": "reviewer-b"}

POST /api/v1/reviews/{review_id}/cancel
{"expected_revision": 2, "reason": "duplicate review request"}
```

`target_reviewer_id` 表示任务受让人，不表示请求操作人；审计操作人始终来自可信上下文。
转派会清除原 lease 并进入定向 `pending_review`，只有受让人能再次 claim 并取得自己的 token。

## 提交

```http
POST /api/v1/reviews/{review_id}/submit
Idempotency-Key: <opaque client key>

{
  "expected_revision": 2,
  "lease_token": "...",
  "correction": {
    "fields": [
      {"field_path": "invoice_number", "action": "confirm_correct"}
    ]
  }
}
```

`Idempotency-Key` 必填。相同 key 与相同语义请求重放返回原业务结果，不重复写入
`HumanCorrection` 或 `CorrectionEvent`；相同 key 绑定不同请求返回 `409`。
`confirm_correct`、`correct`、`confirm_incorrect` 是独立审核动作，不通过任务状态混合表达。

## 过期恢复

```http
POST /api/v1/reviews/recover-expired?limit=100
```

该操作将当前租户已过期的 `claimed` lease 原子转换为 `expired`。`expired` 任务可再次 claim。

## 权限

| 操作 | 权限 |
|---|---|
| 列表、详情 | `review:read` |
| claim、release、reassign、cancel、recover、submit | `review:submit` |

## 事务与恢复

审核事实、最终提取结果、记忆准入是不同事务。Workflow 在审核事实成功持久化后才把任务标记为
`submitted`；最终结果随后按 `run_id` write-once。记忆准入通过现有
`memory_review_recoveries` claim/lease Worker 执行，失败不会撤销审核事实，也不会把已完成
Workflow 改为 failed。

旧 `POST /api/v1/reviews/{run_id}` 暂时保留并标记 deprecated；它会原子自领取可用任务后走同一
Application Service 和幂等提交路径。
