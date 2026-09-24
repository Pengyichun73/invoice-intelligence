# 审核日期与错误反馈设计

## 目标

统一人工审核中固定 Schema 的日期类型输入和 JSON 序列化，并把后端请求校验错误反馈到对应字段，避免用户提交后手动滚动查找错误。

## 设计

- `bookkeeping_datetime` 使用 `datetime-local`，仅接受 `YYYY-MM-DDTHH:mm[:ss]`；内部兼容严格 `YYYY-MM-DD` 并确定性补 `T00:00:00`。不调用本地时区/UTC 转换，输出固定到秒的无时区 ISO 8601。
- `invoice_date` 使用 `date`，仅接受并提交真实日历日期 `YYYY-MM-DD`；拒绝溢出日期、时区和日期时间混用。
- 所有字段保持 nullable；人工修正提供显式“设为空”，提交 JSON `null`，同时保留完整 19 字段对象。
- 其他字段维持现有 JSON/文本解析，不修改 `InvoiceExtraction` 或 API 契约。
- API Client 仅在 `code=request_validation_error` 时，从诊断消息严格提取 `body.corrected_invoice.<field>:<type>`；只保留固定 Schema 白名单字段和本地化错误类型。解析失败或诊断被截断时回退到安全摘要，不显示原始远端消息。
- 审核页为控件提供真实 `label`、稳定 `id`、`aria-invalid` 和 `aria-describedby`。提交失败后才自动滚动并聚焦首个错误；内联错误不重复使用 assertive 播报，提交摘要使用单一 `role=alert`。
- 字段值/action/置空状态变化时清除该字段错误；加载任务、重新提交和成功时清空全部旧错误。
- 不记录或持久化字段错误、字段值、图片、Base64 或完整响应。

## 验证

- Node 单元测试验证日期规范化、nullable、真实日历边界、后端字段路径白名单、错误聚焦和审核请求构造。
- Vite production build 验证 Vue 模板和打包。
