# Invoice Intelligence Task Hub Implementation Plan

> **For agentic workers:** Implement this plan task-by-task in the current session. The workspace is not a Git repository, so commit steps are intentionally omitted.

**Goal:** 将现有 Vue 3 前端重构为顶部导航的客户任务中枢，并完整接入后端已存在的治理接口。

**Architecture:** 保留当前单页组件切换和集中 API Client，不引入 Vue Router、Pinia 或新依赖。应用壳负责一级/二级导航，页面负责 View Model 和交互，API Client 负责 HTTP、幂等键和稳定错误映射。所有英文枚举只在展示层转换为中文。

**Tech Stack:** Vue 3、Vite、lucide-vue-next、原生 CSS、FastAPI JSON API。

---

### Task 1: 重构应用壳与导航

**Files:**
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/styles.css`

- [x] 将 9 个页面按工作台、可信记忆、字段语义、运行治理分组。
- [x] 用桌面双层顶部导航替换常驻侧边栏。
- [x] 添加移动端顶部栏、底部核心导航和完整功能菜单。
- [x] 保留 `KeepAlive`、本地页面选择和现有页面事件导航。
- [x] 为键盘焦点、44px 触控目标和减少动态效果提供基础样式。

### Task 2: 重做客户任务总览

**Files:**
- Modify: `frontend/src/views/Dashboard.vue`
- Modify: `frontend/src/styles.css`

- [x] 保留健康、准入、案例、冲突和 OCR 指标的并行真实读取。
- [x] 生成基于真实计数的风险优先待办摘要。
- [x] 用“待人工处理、可信案例、多源识别、服务状态”替换技术导向摘要。
- [x] 将可信原则压缩为客户可读的证据优先提示。
- [x] 接口部分失败时显示未知状态，不以零代替。

### Task 3: 补齐治理接口接入

**Files:**
- Modify: `frontend/src/api/governance.js`
- Modify: `frontend/src/views/IndexGovernance.vue`

- [x] 增加字段语义索引登记、查询、投影和激活 Client 方法。
- [x] 将索引治理拆为“案例记忆索引”和“字段语义索引”两个分段视图。
- [x] 根据后端 Schema 分别呈现投影计数、版本绑定和激活条件。
- [x] 保持 PostgreSQL 投影登记状态与 Milvus 实时健康状态的语义边界。
- [x] 所有写请求继续通过集中 Client 自动携带 `Idempotency-Key`。

### Task 4: 统一全站视觉和信息层级

**Files:**
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/components/PageHeader.vue`
- Modify: `frontend/src/components/StatusBadge.vue`
- Modify: `frontend/src/components/ResourceState.vue`

- [x] 建立冷黑、深青、荧光黄绿、青绿、琥珀和危险红的设计 Token。
- [x] 统一页面标题、工具栏、表格、记录列表、详情区、对话框和空状态。
- [x] 技术元数据降低视觉权重，业务待办和主要动作保持高对比。
- [x] 使用 transform/opacity 实现 150-300ms 状态动画，禁止布局跳动。
- [x] 优化 375、768、1024、1440px 布局，避免文本和控件重叠。

### Task 5: 校验生产构建

**Files:**
- Verify: `frontend/src/**/*.vue`
- Verify: `frontend/src/**/*.js`
- Verify: `frontend/src/styles.css`

- [x] 运行 `npm run build`，预期 Vite 构建成功。
- [x] 检查构建输出不包含 Vue 模板编译错误或缺失导入。
- [x] 检查代码中不存在前端星号遮蔽和新增英文业务状态直出。
- [x] 不运行真实 OCR、模型、Milvus 或生产数据库。
