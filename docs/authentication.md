# OIDC、租户上下文与 RBAC

## 信任边界

除 `/api/v1/health` 外，API 请求必须提供经过验证的 Bearer JWT，或在 development 模式使用固定
demo identity。受信网关 middleware 也可直接安装强类型 `AuthContext`，但普通 Header 不能构造该
上下文。`subject`、`tenant_id`、`reviewer_id`、roles 和 scopes 只从已验证 Token/网关上下文读取。
`tenant_id`/`reviewer_id` Query 参数以及普通身份 Header 会被拒绝；请求 Body 由 `extra="forbid"`
Schema 拒绝未知身份字段。审核事实中的 reviewer 始终使用 Token 的 `reviewer_id`。

JWT 必须通过签名、允许算法、`exp`、`iat`、issuer 和 audience 校验。生产环境强制 HTTPS issuer、
HTTPS JWKS 和 TLS certificate verification。JWKS 仅按配置 TTL 缓存在进程内。

## Keycloak 本地配置

启动开发 Keycloak：

```powershell
docker compose --profile auth up -d keycloak
```

容器从 `deploy/keycloak/invoice-intelligence-realm.json` 导入 realm
`invoice-intelligence` 和 client `invoice-intelligence-api`。启动前设置：

```text
KEYCLOAK_ADMIN_USERNAME=<local-admin>
KEYCLOAK_ADMIN_PASSWORD=<local-password>
```

在 Keycloak 中为用户设置非空属性 `tenant_id` 和 `reviewer_id`，再分配
`invoice-intelligence-api` client roles。realm 配置包含 user-attribute mapper、API audience mapper
和全部权限 roles。该 public/direct-grant client 仅用于本地 profile；生产应使用受控 confidential
调用方 client，并保留相同 audience、claim mapper 和 client roles。

本地 OIDC 后端配置：

```text
INVOICE_INTELLIGENCE_AUTH_MODE=oidc
INVOICE_INTELLIGENCE_DEV_TENANT_ID=
INVOICE_INTELLIGENCE_OIDC_ISSUER=http://localhost:8081/realms/invoice-intelligence
INVOICE_INTELLIGENCE_OIDC_AUDIENCE=invoice-intelligence-api
INVOICE_INTELLIGENCE_OIDC_JWKS_URL=http://localhost:8081/realms/invoice-intelligence/protocol/openid-connect/certs
INVOICE_INTELLIGENCE_OIDC_CLIENT_ID=invoice-intelligence-api
```

## 权限矩阵

| 权限 | 当前 API 行为 |
|---|---|
| `document:read` | 读取 Run 和提取结果 |
| `document:extract` | 上传文档、启动提取 |
| `review:read` | 读取人工审核任务 |
| `review:submit` | 提交审核，Reviewer 来自 Token |
| `memory:read` | 读取案例、准入、审计、指标和字段语义 |
| `memory:admit` | 准入决策、冲突/别名治理、禁用与反馈 |
| `index:rebuild` | 注册、重建和执行索引投影 |
| `index:activate` | 激活索引版本 |
| `evaluation:run` | 已注册权限；当前无公开执行 Router |
| `training:submit` | 创建、读取、取消和重试 Training Job；`trainer` 角色可用，跨租户 Job 返回 404 |
| `model:promote` | 晋升候选与审批 Router；可信门禁来源尚待补齐 |
| `admin:manage` | 管理权限；隐含全部权限，当前无身份管理 Router |

内置组合角色为 `invoice-reader`、`invoice-extractor`、`invoice-reviewer`、`memory-governor`、
`index-operator`、`evaluator`、`trainer`、`model-governor` 和 `invoice-admin`。Token 也可直接携带与
权限同名的 client role 或 scope。

## 环境差异

- development：未提供 Token 且配置 `DEV_TENANT_ID` 时使用 `local-developer` demo identity；也可启用 OIDC。
- production：必须 `AUTH_MODE=oidc`，禁止 `DEV_TENANT_ID`，issuer/JWKS 必须 HTTPS，禁止关闭 TLS 验证。
- 所有环境：资源查询继续使用 `tenant_id + resource_id`；不存在与跨租户访问统一返回 404。
- 认证失败、权限拒绝、身份覆盖和 404/跨租户模糊事件写入 `security_audit_events`，不保存 Token、Authorization Header 或 API Key。

Training 的 `POST /api/v1/training/jobs`、`GET /api/v1/training/jobs/{job_id}`、
`POST /api/v1/training/jobs/{job_id}/cancel` 和 `/retry` 均要求 `training:submit`。
Job 查询先由可信上下文取得租户，再按 `tenant_id + job_id` 查找；权限不足返回 403 并记录
`authorization_denied`，不存在或跨租户返回 404 并记录模糊的
`resource_not_found_or_cross_tenant` 事件。读取暂复用 `training:submit`，不新增权限或角色。
