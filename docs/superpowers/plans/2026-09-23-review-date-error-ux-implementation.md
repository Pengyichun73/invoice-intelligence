# Review Date And Error UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一审核日期类型并让提交错误自动定位到对应字段。

**Architecture:** 固定 Schema 的前端输入元数据和规范化逻辑集中在纯 JavaScript 模块；API Client 仅解析稳定错误诊断；Vue 页面负责呈现、滚动和聚焦。后端 Domain/API/Workflow 不变。

**Tech Stack:** Vue 3、原生 JavaScript、Node test runner、Vite

---

### Task 1: 日期类型与错误解析

**Files:**
- Modify: `frontend/src/views/extractionReview.js`
- Modify: `frontend/src/api/client.js`
- Test: `frontend/tests/extractionReview.test.js`

- [x] 增加日期字段输入元数据、ISO 规范化和无效值错误。
- [x] 增加显式 nullable 修正，保持完整 19 字段对象。
- [x] 仅对白名单字段从 `request_validation_error` 提取诊断，原始消息不进入 UI。
- [x] 测试非法日历日期、闰年、非法时分、时区/类型混用、nullable 和回退摘要。
- [x] 运行 `npm test` 验证日期和错误映射。

### Task 2: 审核页错误体验

**Files:**
- Modify: `frontend/src/views/ExtractionWorkbench.vue`
- Modify: `frontend/src/styles.css`

- [x] 日期字段使用对应原生输入类型。
- [x] 增加真实 label、显式置空、字段内错误、`aria-invalid` 和 `aria-describedby`。
- [x] 首个错误自动滚动并聚焦，顶部错误吸顶，提交区保留安全摘要。
- [x] 字段编辑/action/置空切换、任务加载、重试和成功时按范围清除错误。
- [x] 用可注入 DOM resolver 测试首错选择、scroll 和 focus 行为。
- [x] 运行 `npm run build` 验证生产构建。

### Task 3: 同步说明

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`

- [x] 只记录已实现的日期输入和字段错误反馈能力。
