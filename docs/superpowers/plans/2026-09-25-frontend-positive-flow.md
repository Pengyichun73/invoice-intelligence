# Frontend Positive Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用当前 FastAPI 正式审核任务契约，打通前端发票上传、提取、结果展示和人工审核提交正向链路。

**Architecture:** 保留现有 Vue 3 单页工作台和集中 API Client。上传与提取继续使用既有接口；进入 `pending_review` 后读取 Review Task，调用 claim 获取内存 lease，再用 `expected_revision + lease_token + correction` 提交正式审核。所有业务状态以后端响应为准，前端不伪造 Workflow、OCR、Retrieval 或索引进度。

**Tech Stack:** Vue 3、Vite、原生 Fetch、`lucide-vue-next`、Node test runner。

---

## 文件职责

- Modify: `frontend/src/api/invoice.js`
- 增加正式审核任务 claim/submit API 方法。
  - 保留既有 run/result/review 读取方法。
- Modify: `frontend/src/views/ExtractionWorkbench.vue`
  - 完整替换现有 `state.review` 及其全部引用为正式 `state.reviewTask`，管理审核 task、lease token、claim/submitting 状态。
  - 将审核表单提交切换到正式 endpoint。
  - 处理 `received`、未知 Run 状态、409、租约缺失和写入后读取失败。
- Modify: `frontend/src/views/extractionReview.js`
  - 保持当前 19 字段和 correction 语义。
  - 如需要，仅抽取可测试的 submit envelope 构造辅助函数，不修改实体字段。
- Modify: `frontend/tests/extractionReview.test.js`
  - 覆盖正式 submit envelope 和审核状态重置所需的纯函数行为。
- Create: `frontend/tests/invoiceFlow.test.js`
  - 使用 mock `fetch` 验证 API 方法的路径、请求体和幂等 Header。
- Modify: `frontend/src/api/client.js`（仅必要时）
  - 若需要区分 claim 与可安全重试的 submit，增加显式写策略参数；默认不改变现有治理 API。

## Task 1: 补齐正式审核 API Client

**Files:**
- Modify: `frontend/src/api/invoice.js`
- Test: `frontend/tests/invoiceFlow.test.js`

- [ ] **Step 1: 编写 API Client 失败测试**

验证：

- `claimReview(identifier, expectedRevision)` 请求 `POST /api/v1/reviews/{identifier}/claim`。
- claim body 只有 `expected_revision`，可选 `lease_seconds` 由方法参数控制。
- `submitClaimedReview(identifier, expectedRevision, leaseToken, correction)` 请求
  `POST /api/v1/reviews/{identifier}/submit`。
- submit body 为 `{ expected_revision, lease_token, correction }`，而不是直接发送 correction。
- claim 和 submit 路径均对 identifier 做 `encodeURIComponent`。

- [ ] **Step 2: 运行单测确认失败**

Run:

```powershell
cd G:\work\ai\frontend
npm test -- --test-name-pattern "正式审核 API"
```

Expected: 新增断言失败，因为 API 方法尚未存在。

- [ ] **Step 3: 实现 API Client 方法**

使用现有 `get`、`write`，不直接调用 `fetch`。submit 继续使用统一 write 的幂等键；claim 不人为增加独立幂等语义。

- [ ] **Step 4: 运行单测确认通过**

Run:

```powershell
cd G:\work\ai\frontend
npm test -- --test-name-pattern "正式审核 API"
```

Expected: 相关测试通过。

## Task 2: 将工作台审核状态切换到正式 claim/submit

**Files:**
- Modify: `frontend/src/views/ExtractionWorkbench.vue`
- Modify: `frontend/src/views/extractionReview.js`
- Test: `frontend/tests/extractionReview.test.js`

- [ ] **Step 1: 增加可测试的审核提交 envelope 构造**

在 `extractionReview.js` 中保留现有 `buildReviewSubmission()` 返回的 `HumanCorrectionRequest`，增加一个小型纯函数或在调用侧明确构造：

```js
{
  expected_revision: reviewTask.revision,
  lease_token: leaseToken,
  correction: correctionPayload,
}
```

不得把 `tenant_id`、`reviewer_id` 或新字段加入 correction。

- [ ] **Step 2: 增加 claim 前后的状态字段**

在 `ExtractionWorkbench.vue` 中增加：

  - `state.reviewTask`（完整替换现有 `state.review`、`isReview`、`reviewEntries`、`reviewFields`、
    `reviewNotices`、`reviewBindings`、`hydrateReview()` 和 submit 逻辑的引用）
- `state.leaseToken`
- `state.reviewClaiming`
- `state.reviewSubmitting`
- `state.readOnlyReviewReason`

claim 成功后只从响应的 `task` 和 `lease_token` 更新状态。`task.revision`、`task.request`、`task.current_invoice` 覆盖旧 Review Task。

- [ ] **Step 3: 实现 `claimReviewTask()`**

流程：

1. 读取当前 task 的 `revision`。
2. 调用 `invoiceApi.claimReview(reviewId, revision)`。该调用可通过统一 Client 携带请求 Header，
   但 Header 不是服务端 claim 幂等契约；claim 超时不得由 Client 或页面自动重放。
3. 解包 `{ task, lease_token }`。
4. lease token 缺失时进入只读状态，不允许提交。
5. 使用新的 task request/current invoice hydrate 表单。

claim 网络错误、429、503 只允许用户显式重试；不能自动重复 claim。claim 409 后重新读取 Run 和 Review Task。

- [ ] **Step 4: 修改 `pollRun()` 的审核分支**

补齐状态：

- `received` 与 `processing`：继续轮询。
- `pending_review`：读取 task，随后尝试 claim。
- `completed`：读取 result。
- `failed`：展示后端状态。
- 未知状态：停止自动轮询，显示安全提示和 run_id 查询入口。

禁止使用前端 stage 文案推断具体后端节点进度。

- [ ] **Step 5: 实现正式 `submitClaimedReview()`**

提交前检查：

- 当前存在 `reviewTask`；
- 当前存在 64 字符 `leaseToken`；
- task revision 与表单快照对应。

请求使用统一的完整 body，API 方法签名固定为 `submitClaimedReview(identifier, body)`：

```js
invoiceApi.submitClaimedReview(
  state.reviewTask.review_id,
  {
    expected_revision: state.reviewTask.revision,
    lease_token: state.leaseToken,
    correction,
  },
)
```

成功后解包 `{ task, run }`，清除 lease 和本地审核表单状态，以 `run` 继续轮询。不得再次发送同一审核写请求。

- [ ] **Step 6: 实现 409 和租约不可恢复分支**

409 或租约失效时：

- 清除本地审核选择、task 和 lease；
- 重新读取 Run 与 Review Task；
- 若 task 为可领取状态，再允许 claim；
- 若 task 为 `claimed` 但当前页面没有 token，进入只读等待；
- 若 task 为 `submitted/cancelled` 或 Run 已完成，展示权威状态，不重放审核决定。
- `expired` 不是终态；重新读取后允许再次 claim，但必须基于最新 `revision`，不能复用旧 lease 或旧审核写请求。

页面刷新后不自动恢复旧 lease；详情接口不返回 lease token，因此必须按上述规则等待或重新领取。

- [ ] **Step 7: 保持成功后读取失败语义**

submit 返回成功但后续 Run/result 读取失败时，显示“审核已保存，最新状态未读取”，只提供重新读取动作。不得再次调用 submit。

- [ ] **Step 8: 更新审核纯函数测试**

覆盖：

- submit envelope 包含 revision、lease token 和 correction；
- correction 仍保持完整 `InvoiceExtraction`；
- 不出现 tenant/reviewer 字段；
- 现有日期、null、字段绑定验证继续通过。

## Task 3: 补充流程级 API/状态测试

**Files:**
- Create: `frontend/tests/invoiceFlow.test.js`
- Modify: `frontend/src/api/invoice.js`
- Modify: `frontend/src/api/client.js`（仅必要时）

- [ ] **Step 1: 测试请求体和响应形状**

使用 mock `fetch` 返回：

- claim `{ task, lease_token }`；
- submit `{ task, run }`；
- 409 错误并带 `X-Trace-ID`。

断言客户端不暴露远端完整 payload。

- [ ] **Step 2: 测试幂等边界**

验证：

- submit 网络失败后相同语义请求复用同一 `Idempotency-Key`；
- submit 成功后下一次相同语义请求使用新 key；
- claim 超时不会被 API Client 自动重放。

- [ ] **Step 3: 运行前端测试**

Run:

```powershell
cd G:\work\ai\frontend
npm test
```

Expected: 所有现有测试和新增测试通过。

## Task 4: 构建与运行时检查

**Files:**
- Modify: `frontend/src/views/ExtractionWorkbench.vue`（如构建错误需要）
- Modify: `frontend/src/api/invoice.js`（如构建错误需要）

- [ ] **Step 1: 构建前端**

Run:

```powershell
cd G:\work\ai\frontend
npm run build
```

Expected: Vite build 成功，无 Vue template/compiler 错误。

- [ ] **Step 2: 启动前端开发服务**

Run:

```powershell
cd G:\work\ai\frontend
npm run dev -- --host 127.0.0.1
```

Expected: Vite 在 `http://127.0.0.1:5173` 提供页面；不得修改或重启后端 Docker 容器。

- [ ] **Step 3: 手工检查正向链路**

使用当前运行 API 验证：

1. 打开发票提取页；
2. 选择支持格式的图片/PDF；
3. 启动提取并观察真实 Run 状态；
4. `completed` 时看到结果；
5. `pending_review` 时看到 task、claim 后的审核表单；
6. 提交审核后看到返回 Run 状态；
7. 409/读取失败提示不重复提交。

若后端模型或 OCR 未配置导致业务失败，只记录真实失败状态，不用 mock 数据宣称链路完成。

- [ ] **Step 4: 检查视口和可访问性**

检查 375px、768px、1440px：

- 无横向溢出；
- 审核控件和提交按钮不重叠；
- loading 时按钮禁用；
- 失败状态提供重新读取；
- `prefers-reduced-motion` 下无强制过渡。
