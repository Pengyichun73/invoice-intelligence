# Observability 与告警边界

当前实现提供低基数、进程级运行指标；指标不是业务事实源，也不能替代 PostgreSQL
队列、审核事实或 Provider 审计。`GET /api/v1/metrics` 默认关闭，只有显式设置
`INVOICE_INTELLIGENCE_METRICS_ENDPOINT_ENABLED=true` 才暴露，并应通过受控网络或反向代理
限制访问。

## 指标

| 指标 | 类型 | 标签 |
|---|---|---|
| `invoice_worker_processed_total` | counter | `worker`, `outcome` |
| `invoice_worker_retries_total` | counter | `worker` |
| `invoice_worker_lease_expired_total` | counter | `worker` |
| `invoice_worker_backlog` | gauge | `worker` |
| `invoice_worker_oldest_task_age_seconds` | gauge | `worker` |
| `invoice_provider_calls_total` | counter | `provider`, `operation`, `outcome` |
| `invoice_provider_circuit_open_total` | counter | `provider`, `operation` |
| `invoice_audit_write_failures_total` | counter | `sink`, `error_code` |

标签不得包含 `tenant_id`、`document_id`、`run_id`、资源 ID、发票字段值或自由文本。
当前已接入通用 Provider 熔断和审计写入失败计数；Index Projection 与 Evaluation Worker
记录处理/重试/租约恢复。没有可查询队列深度的 Worker 不会伪造 backlog 或最老任务年龄为零。
PostgreSQL queue 仍是 backlog、lease 和 retry 的权威来源。

## 推荐告警规则

规则由部署环境中的 Prometheus 或兼容系统加载，项目不硬编码 webhook、Token 或接收端：

```yaml
groups:
  - name: invoice-intelligence
    rules:
      - alert: InvoiceWorkerBacklogGrowing
        expr: deriv(invoice_worker_backlog[15m]) > 0
        for: 15m
      - alert: InvoiceWorkerOldestTaskTooOld
        expr: invoice_worker_oldest_task_age_seconds > 900
        for: 10m
      - alert: InvoiceWorkerLeaseExpiredBurst
        expr: increase(invoice_worker_lease_expired_total[10m]) > 5
        for: 5m
      - alert: InvoiceWorkerRetryBurst
        expr: increase(invoice_worker_retries_total[10m]) > 20
        for: 10m
      - alert: InvoiceProviderCircuitOpen
        expr: increase(invoice_provider_circuit_open_total[5m]) > 0
        for: 5m
      - alert: InvoiceAuditWriteFailure
        expr: increase(invoice_audit_write_failures_total[5m]) > 0
        for: 1m
```

阈值必须结合部署规模和队列 SLA 调整。`audit_write_failures_total` 属于高优先级安全
告警；审计写入失败仍会重新抛出，不能被指标采集吞掉。Provider circuit open 只表示调用
在本进程被熔断，不表示远端服务的全局状态。

## 数据安全

指标、日志和告警不得包含完整发票值、图片、Base64、Prompt、密钥、向量、Idempotency-Key
原文或远程响应体。相似度、质量分数和 circuit 状态不是 probability。当前未接入
Prometheus/Grafana、外部告警接收端或跨副本共享配额；生产告警闭环仍需在隔离环境完成验证。
