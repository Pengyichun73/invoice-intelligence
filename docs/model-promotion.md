# 模型灰度与回滚

1. 评估完成后由受信服务创建绑定完整版本的 Candidate。Policy 按 hard failure、兼容性和指标门禁顺序判定；任何 hard failure 都不可被指标覆盖。
2. 审批人使用 `POST /api/v1/model-promotion/{candidate_id}/approve`，携带 `expected_revision` 和目标 `shadow`、`canary` 或 `active`。`active` 仅映射既有 `production`，不会修改 `ModelArtifact` Schema。
3. 灰度顺序固定为 `shadow -> canary -> active`。部署控制面只接收已审批引用；LangSmith/MLflow 不参与状态决定。
4. 发现生产指标失败时，审批人调用 rollback API。服务只允许仍有效、已验证且有历史部署事实的目标版本；当前状态、目标状态和审计在 PostgreSQL 事务内 CAS 更新。
5. 迁移 `20260923_0025` 创建 Candidate 与审计事实表。所有拒绝、审批、回滚均记录 actor、状态、revision 和 trace_id，禁止写入完整 Prompt、模型响应、权重或 API Key。

自动晋升默认关闭。当前 Compose 不提供模型晋升 Worker；Promotion API 只维护
PostgreSQL 注册事实，不提供无人值守晋升或真实部署流量切换。
