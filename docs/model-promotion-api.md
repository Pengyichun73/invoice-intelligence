# 模型晋升 API 契约

所有接口需要可信认证与 `model:promote` 权限，租户和审批人来自可信上下文。此 API 管理
PostgreSQL 注册状态，不调用真实模型部署控制面，也不修改模型权重。自动晋升保持禁用。

## 创建 Candidate

`POST /api/v1/model-promotion`：

```json
{"candidate_id":"<离线评估生成的提案 ID>","evaluation_run_id":"<Run ID>","artifact_id":"<Model Artifact ID>"}
```

不接受客户端提交指标、版本、hard failure 或 compatibility errors。Application Service 从同租户
completed EvaluationRun、唯一终态 ModelEvaluation、有效 ModelArtifact、成功 TrainingRun、已导出训练
数据集、Model/Prompt/Index 版本事实读取并交叉校验。Index 必须有效且曾完成激活；Schema、Prompt、
Threshold 与当前受控配置匹配。指标直接来自 ModelEvaluation；错误自动填充、历史覆盖或失败的
ModelEvaluation 优先阻止晋升。门禁不通过的有据候选保存为 `rejected` 并审计；缺证据或跨租户引用
返回 404/409，不伪造成功。旧 Candidate 无可信产物和评估引用时不能晋升。

## 审批、拒绝与回滚

- `POST /api/v1/model-promotion/{candidate_id}/approve`：
  `{"expected_revision":1,"target_status":"canary"}`。只允许 `shadow -> canary -> active`
  的阶段顺序，审批前重读证据；过期 revision 或证据失效返回 409。
- `POST /api/v1/model-promotion/{candidate_id}/reject`：
  `{"expected_revision":1,"reason":"POLICY_REJECTED"}`。reason 仅接受安全代码。
- `POST /api/v1/model-promotion/{active_candidate_id}/rollback`：
  `{"expected_revision":3,"target_candidate_id":"<曾激活且目前为 rollback 的历史候选>"}`。
  目标必须属于同租户、使用不同模型版本且证据仍有效。响应是重新激活的历史 Candidate。

审批和回滚使用 PostgreSQL revision/CAS；回滚的两个候选及审计在单一事务内切换。活动候选唯一
索引防止并发双 active。上线迁移前应检查同一租户是否已有多个 `active` 旧记录；若存在，先按
审计事实人工裁决，迁移会 fail closed，不自动删除或选择旧记录。Evaluation worker/scheduler、
真实部署 Controller 和独立 Threshold Registry 仍不在此交付内。
