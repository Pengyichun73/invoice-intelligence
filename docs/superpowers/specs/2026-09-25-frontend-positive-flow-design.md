# 前端发票提取正向链路设计

## 目标

基于当前 FastAPI OpenAPI 契约和运行中的 Docker 服务，打通一条可恢复的客户前端正向链路：

`上传文件 -> 创建提取 Run -> 轮询 Run -> 展示结果或进入人工审核 -> 提交审核 -> 继续轮询 -> 展示最终结果`

本次不修改后端、数据库、Docker、`InvoiceExtraction` Schema 或治理页面。

## 当前事实

- 前端已具备文件上传、提取启动、Run 轮询、结果展示和审核表单。
- 当前 API Client 的审核提交使用 legacy `POST /api/v1/reviews/{run_id}`。
- 当前 OpenAPI 同时提供正式审核任务接口：
  - `GET /api/v1/reviews/{identifier}`
  - `POST /api/v1/reviews/{identifier}/claim`
  - `POST /api/v1/reviews/{identifier}/submit`
- 审核任务提交契约要求 `expected_revision`、`lease_token` 和 `HumanCorrectionRequest`。
- 2026 年 9 月 25 日检查到本地 Docker Compose 环境中的 API、PostgreSQL、MinIO、Milvus 及相关 Worker
  容器报告 healthy；这只代表当前本地合成运行环境，不代表生产流量已切换或生产验收已完成。

## 方案

采用正式审核任务链路。提取页继续作为流程承载页面，审核进入 `pending_review` 后读取任务并领取租约；只有领取成功后才允许提交。

### API Client

在 `frontend/src/api/invoice.js` 增加：

- `claimReview(identifier, expectedRevision, leaseSeconds)`
- `submitClaimedReview(identifier, body)`

Submit 继续经过统一 `write()`，由 Client 生成并复用 `Idempotency-Key`。claim 接口当前没有
`Idempotency-Key` 契约，不能把 claim 当成可安全重试的幂等写操作；claim 请求超时只能重新读取
Review Task，确认当前状态后再决定是否重新领取。lease token 只存在当前页面内存。

上传响应不确定时，页面只能通过已知 document 标识保留上下文；不能假设重复上传一定被后端去重。
extract 响应不确定且没有拿到 `run_id` 时，当前后端没有按 `document_id` 查询 Run 的接口，也没有
extract 的服务端幂等契约，因此页面停止自动重试，提示用户联系后端运维或使用已知 `run_id` 查询，
不能用前端状态伪造 Run。本次不扩展后端去重或查询能力。

### ExtractionWorkbench 状态

增加审核任务运行态：

- `reviewTask`
- `leaseToken`
- `reviewClaiming`
- `reviewSubmitting`

后端返回的 `revision`、`status`、`request` 和 `current_invoice` 是唯一权威数据。提交后以返回的 `run` 为准继续轮询，不在前端预先改变业务状态。

### 正向数据流

1. 文件通过既有 MIME 白名单校验。
2. 调用 `POST /documents` 保存可信文档引用。
3. 调用 `POST /documents/{document_id}/extract` 创建 Run。
4. 轮询 `GET /runs/{run_id}`：
   - `received`：继续等待；
   - `processing`：继续等待，不能解释为具体 OCR、视觉、检索或 Milvus 进度；
   - `completed`：调用结果接口；
   - `pending_review`：读取 Review Task；
   - `failed`：展示后端稳定失败提示。
   - 未知状态：停止自动推进并提示“后端返回了暂不支持的运行状态”，允许用户按 run_id 重新读取。
5. Review Task 读取成功后调用 claim 接口。
6. claim 成功后解包 `{ task, lease_token }`，保存 `task.revision`、`task.status`、`task.request`、
   `task.current_invoice` 和 `lease_token`，加载审核项。
7. 使用现有 `buildReviewSubmission()` 构造 `HumanCorrectionRequest`，再包裹为：
   `{ expected_revision: task.revision, lease_token, correction }`，调用正式 submit 接口。
8. submit 成功后解包 `{ task, run }`，清理 lease 状态，以 `run` 继续轮询；不使用旧 Review Task
   的 revision 或本地猜测状态。

## 错误与恢复

- `409`：停止当前提交，清除本地审核选择、Review Task 和 lease，重新读取 Run 与 Review Task。
  若 Run 已 `completed`、任务已 `submitted/cancelled/expired` 或任务已被他人领取，页面以权威状态为准，
  不自动重放原审核决定；只有重新读取后任务仍可审核时，才允许再次 claim。
- `404/403`：使用白名单文案提示记录不可用或无权限，不泄露跨租户资源存在性。
- `429/503/网络错误`：保留当前编辑内容，依赖统一 Client 复用相同语义请求的幂等键。
- 写入成功但后续读取失败：提示“操作已保存，最新状态未读取”，只允许重新读取，不重复提交。
- lease 失效或缺失：不自动重建审核决定。当前任务接口未提供前端续租或 lease 恢复契约，本次不实现
  定时续租。页面刷新后若详情仍为 `claimed`，由于响应不返回原 lease token，页面只能进入只读等待，
  提供重新读取动作，直到任务变为 `expired` 或其他可解释终态；只有重新读取到可领取状态时才允许
  重新 claim。页面卸载不自动调用 release，避免在离线/卸载阶段发起不确定写请求。
- memory 失败可重试状态不影响已经持久化的发票结果。

## 修改范围

- `frontend/src/api/invoice.js`
- `frontend/src/views/ExtractionWorkbench.vue`
- 必要时补充 `frontend/src/views/extractionReview.js` 的契约适配，但不增加或修改 Schema 字段。
- 与上述行为直接相关的前端测试文件。

## 非目标

- 不创建或修改 tenant、reviewer、canonical field path。
- 不修改后端 API、数据库迁移、Docker Compose 或业务实体。
- 不实现未由后端提供的进度、下载、图片预览、治理操作或自动审批。
- 不解决 extract 已创建但响应丢失时的 Run 找回；这需要后端提供 document-to-run 查询或显式幂等契约。

## 验证

- 验证 claim 和 submit 的路径、嵌套响应解包、请求体、revision 与 lease token。
- 验证 409 后重新读取并清空过期审核选择。
- 验证成功提交后只轮询和读取，不重复写入。
- 验证 submit 网络失败、429、503 的幂等键复用；claim 超时只重新读取，不自动重复 claim。
- 验证 extract 响应不确定时不会自动重复创建 Run，并给出可执行的人工恢复提示。
- 运行前端现有测试与构建，并检查主要桌面和移动视口无溢出和遮挡。
