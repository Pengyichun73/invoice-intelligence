# Frontend Interaction Feedback Implementation Plan

> **For agentic workers:** Execute each task in order and preserve existing API contracts.

**Goal:** 为现有 Vue 3 治理工作台增加统一、可恢复、可访问的操作反馈与页面导航体验。

**Architecture:** API Client 发布全局请求活动；无依赖通知 composable 管理消息；App Shell 承担请求进度与页面过渡；关键写操作页面调用统一反馈并在成功后重新读取后端状态。

**Tech Stack:** Vue 3 Composition API、Vite、lucide-vue-next、原生 CSS。

---

### Task 1: 全局反馈基础设施

**Files:**
- Create: `frontend/src/composables/useNotifications.js`
- Create: `frontend/src/components/NotificationCenter.vue`
- Create: `frontend/src/components/RequestProgress.vue`
- Modify: `frontend/src/api/client.js`

- [ ] 增加通知队列、自动关闭和后续动作。
- [ ] 为 API 请求使用 `try/finally` 发布并发计数，覆盖网络、解析和取消。
- [ ] 通知只消费前端白名单安全文案，未知错误泛化且不暴露响应体。
- [ ] 网络失败、429、503 后的相同语义写请求复用内存 Idempotency-Key。

### Task 2: App Shell 导航与动效

**Files:**
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/theme.css`

- [ ] 接入全局进度和消息中心。
- [ ] 支持 Hash 白名单、非法值回退、前进/后退、页面标题和焦点恢复。
- [ ] 增加减少动态效果与移动端适配。

### Task 3: 关键操作反馈

**Files:**
- Modify: `frontend/src/components/ActionDialog.vue`
- Modify: `frontend/src/views/ExtractionWorkbench.vue`
- Modify: `frontend/src/views/MemoryAdmissions.vue`
- Modify: `frontend/src/views/FieldSemantics.vue`
- Modify: `frontend/src/views/ConflictGovernance.vue`
- Modify: `frontend/src/views/ExampleGovernance.vue`
- Modify: `frontend/src/views/IndexGovernance.vue`

- [ ] 写操作成功后通知并刷新后端状态。
- [ ] 409 后清空陈旧选择并重新读取权威状态/revision；读取失败时禁止沿用旧快照。
- [ ] 写成功但刷新失败时提示“操作已保存、最新状态未读取”，后续只重试读取。
- [ ] 批量部分失败和网络失败使用对应反馈。
- [ ] 为可继续的业务流程提供页面跳转动作。
- [ ] 对话框实现 focus trap、Esc、busy 防关闭、关闭后焦点恢复和 live error。

### Task 4: 文档与验证

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Test: `frontend/tests/clientInteraction.test.js`
- Test: `frontend/tests/notifications.test.js`

- [ ] 更新交互反馈和数据真实性边界。
- [ ] 验证恶意错误内容不泄露、并发计数归零、重试 Key 复用、409 刷新及写成功/读失败分支。
- [ ] 运行全部前端测试与生产构建。
- [ ] 使用浏览器检查 375/768/1440 视口、Hash 前进后退、焦点、通知重叠和减少动态效果。
