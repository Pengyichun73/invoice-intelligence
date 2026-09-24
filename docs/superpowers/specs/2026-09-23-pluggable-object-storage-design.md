# 可插拔对象存储设计

## 目标与边界

在不修改 `InvoiceExtraction`、Workflow 路由或 API 分层的前提下，将现有仅支持本地原件的
`FileStorage` 扩展为可插拔对象存储。PostgreSQL 保存对象引用和生命周期事实；MinIO/S3/OSS
只保存对象内容。development 可使用本地文件系统或指定版本 MinIO，production 禁止本地存储。

## 存储分区

采用三个独立 Bucket：

- `originals`：上传的原始 PDF、JPEG、PNG、WEBP；生命周期由业务保留策略控制。
- `rendered`：PDF 渲染页和规范化图片；短期生命周期。
- `derived-text`：OCR 等派生文本；独立生命周期，不保存业务 Entity、Prompt 或向量。

Bucket 名称通过 Pydantic Settings 配置，必须互不相同。`object_kind` 到 Bucket 的映射是
Application/Adapter 内的固定枚举，不接受 HTTP 或 Prompt 覆盖。对象键中的租户段使用服务端密钥
生成 HMAC-SHA256，不使用可枚举的普通 hash；其余部分由对象类型、稳定对象 ID、页码和处理版本
组成。客户端不能指定 Bucket、对象键或 `storage_uri`。

现有规范化页面仍以瞬态数据进入 Vision/OCR Provider；为了满足可重用渲染产物的存储边界，
`DocumentArtifactService` 在渲染完成后按“文档 + 页码 + renderer_version”保存一份 rendered
对象，Provider 继续只接收内存 bytes。OCR 比对完成后，同一服务按“文档 + provider_version +
page_number”保存经过长度限制的派生 JSON 文本。保存失败只登记可恢复对象任务，不把已完成的
发票 Workflow 改为 failed。派生内容不进入 `InvoiceExtraction` 或 GraphState。

## Port 与 Adapter

`FileStorage` 提供受控的 `save`、`read`、`head` 和 `delete`。
`save` 接收结构化 `StorageWriteRequest`，包含固定 kind、稳定 key、expected checksum 和媒体类型；
`read` 接收 expected checksum 并在返回前验证实际 bytes。返回值和输入使用结构化对象引用/元数据；
预签名 URL 仅即时返回，不持久化、不记录日志。

- `DownloadAccessIssuer` 是独立统一 Port。S3 实现生成 presigned GET URL；Local 实现生成带 HMAC
  短时令牌的同域 API content URL。Application 只依赖该 Port，不按 concrete backend 分支。
- `LocalFileStorage`：development Adapter，实现读写/HEAD/删除；对应 LocalDownloadAccessIssuer
  指向受控 API endpoint，Storage Adapter 不伪造对象存储 URL。
- `S3FileStorage`：基于 boto3 的 S3-compatible Adapter，用于 development MinIO，并保留生产
  AWS S3/兼容 OSS 的实现边界。
- `bootstrap.py`：唯一 Adapter 选择和依赖组装位置。

## PostgreSQL 对象事实

新增 `stored_objects`，它是所有对象元数据的唯一事实表：

- 身份与范围：`object_id`、`tenant_id`、不可变 `stable_key VARCHAR(512)`、可空
  `parent_document_id`、`object_kind`、
  `page_number`、`producer_version`；
- 引用与完整性：`storage_uri`、`checksum`、`media_type`、`size_bytes`；
- 生命周期：`status`、`retention_until`、`delete_after`；
- 并发：`revision`、`claim_token`、`worker_id`、`lease_expires_at`、`attempt_count`、`next_attempt_at`、
  `last_error_code`、`last_error_at`；
- 时间：`created_at`、`updated_at`、`deleted_at`。

`stable_key` 是不以 `/` 开头、不含空段/`.`/`..`、只含受控 ASCII 字符的规范化相对 key，创建后
不得更新。对象状态为 `migration_pending`、`pending`、`available`、`delete_pending`、`deleting`、
`deleted`、`failed`。数据库
CHECK 约束状态、正 revision/size/attempt/page，以及删除时间组合；唯一约束覆盖
`tenant_id + object_kind + stable_key`，另建 `UNIQUE(tenant_id, object_id)`。
`documents` 建 `UNIQUE(tenant_id, document_id)`，新增唯一 `original_object_id`，并以
`(tenant_id, original_object_id)` 复合外键指向 `stored_objects(tenant_id, object_id)`。
`stored_objects` 的 `(tenant_id, parent_document_id)` 复合外键指向
`documents(tenant_id, document_id)`；原件的 parent 保持 NULL，派生对象必须非空，从而数据库
阻止跨租户或悬空关联。最终迁移完成后删除 `documents.storage_uri/mime_type/checksum` 重复列，Repository
通过 join 投影现有 `DocumentReference`，不会形成两个 PostgreSQL 真相。

历史记录先建立 `status=migration_pending` 对象记录，`size_bytes` 允许仅对该状态为 NULL，
不得直接标为 `available`。独立迁移命令逐项执行 local read → 实际 SHA-256/size 校验 → S3
conditional put → HEAD/下载抽样校验 → 单事务切换 `original_object_id` 和 `available`。源文件在
宽限期内保留以支持回滚；切换失败保持旧引用。production 启动在存在 legacy 记录时 fail closed，
不会通过运行时双读掩盖未完成迁移。唯一允许的迁移转换是 `migration_pending -> available|failed`。
迁移分两阶段：第一阶段允许 `documents.original_object_id` 为 NULL 并保留旧三列；全部内容复制
成功且不存在 `migration_pending/failed` legacy 后，第二个显式收口 migration 将
`original_object_id` 改为 NOT NULL，再删除旧三列。任何失败 legacy 都阻止收口 migration。

## 上传与一致性

上传固定顺序：

1. 复用 `PillowMuPdfDocumentProcessor` 完成 MIME、扩展名、签名、大小、页数、像素、动画和
   解压炸弹防护；
2. 计算 SHA-256；
3. `DocumentUploadUnitOfWork.begin_upload` 在同一 PostgreSQL 事务中创建或读取 durable
   Idempotency claim、分配稳定 `document_id/object_id` 并插入 `pending` 对象；claim owner、token
   和 lease 保存在 idempotency row，对象 revision 保存在 `stored_objects`。如果重放发现 claim
   已存在但 pending 因旧版本/异常缺失，则只允许在同事务按 claim 中的稳定 ID 补建；反向孤立
   pending 由相同 request hash 和唯一约束重新关联，语义冲突返回 409；
4. 上传到 `originals` Bucket；
5. HEAD 验证大小、媒体类型和服务端对象元数据中的 SHA-256；
6. `DocumentUploadUnitOfWork.finalize_upload` 使用 claim token/revision CAS，在同一 PostgreSQL
   事务中将对象改为 `available`、插入 Document 并把 Idempotency 状态改为 `completed`；
7. 返回同一 Document 引用。

并发请求只能由持有未过期 claim 的 owner 上传。每次外部写入前必须再次校验 claim fencing token
仍是当前值。进程崩溃后，重放可在 lease 到期后 CAS 接管：
若对象为 pending 且 HEAD 与 expected metadata 一致则直接执行 finalize；不存在则重新上传；不一致
则标记 checksum mismatch 并拒绝覆盖。S3 put 必须使用原子 `If-None-Match: *` 或后端等价条件，
禁止 HEAD/PUT 组合；不支持条件写的兼容后端使用唯一临时 key 写入，并通过后端原子 copy/promote
到稳定 key。无法提供任一原子语义的后端启动失败。对象存储与 PostgreSQL 不使用分布式事务；
stale pending 由 Worker 在宽限期后清理。

rendered/derived-text 使用 `artifact_recovery_tasks` 保存重建 recipe，而不保存 bytes：tenant、
parent document、kind、page、renderer/provider/config version、目标 object ID、状态、claim/lease、
attempt 和脱敏 error code。首次产物登记与 recovery task 在同一 PostgreSQL 事务；上传成功后原子
完成对象和任务。失败由独立生命周期 Worker 的 artifact recovery 分支重新读取 original，并使用
明确版本的 renderer/OCR Provider 重建。rendered 是确定性版本化处理，可复用原目标 checksum；
远程 OCR 视为非确定性：首次上传失败的 generation 标记 `failed`，恢复调用创建新的 generation、
object_id、stable_key 和 checksum，并保存 `source_run_id`、`supersedes_object_id`、provider/config
版本，绝不冒充 Workflow 当时使用的产物。缺失版本标记永久失败并进入人工治理。任务以 recipe +
generation fingerprint 幂等，重建失败不修改发票结果或 Workflow 状态。

## 下载与访问控制

预签名下载由 Application Service 先按 `tenant_id + document_id` 查询 PostgreSQL，再检查对象
状态和可信内容 checksum，最后向对象存储请求短时 GET URL。上传必须提交 S3/MinIO 原生
`ChecksumSHA256`，签发前通过 HEAD/GetObjectAttributes 验证服务端 checksum；不支持可信原生
checksum 的兼容后端必须先受控流式读取并计算 SHA-256，不能只信任用户 metadata。Local backend 返回同域 API content URL，
令牌包含 object_id、tenant HMAC、expiry 和 nonce；API 再次验证可信 tenant、状态和签名后流式
读取。Router 只调用 Application Service，不接触 boto3 或文件系统。响应不得包含 Access Key、
Secret Key 或持久签名；日志不得记录 URL。

## 生命周期 Worker

独立进程先在 PostgreSQL 事务中把到期 `available` CAS 为 `delete_pending`，立即撤销新下载授权；
活跃下载令牌短时有效，但 content endpoint 在真正读取前再次检查状态，因此不会开始新读取。
Worker 使用 `FOR UPDATE SKIP LOCKED` 将 `delete_pending` 或超过 pending grace 的孤立对象 claim 为
`deleting`，支持 revision、租约回收、有限重试、指数退避和 SIGINT/SIGTERM 优雅停止。删除成功但
数据库提交失败时，重放 DELETE；对象不存在视为幂等成功并标记 `deleted`。权限错误不重试；网络
超时、限流、5xx 和暂时不可用可重试；重试耗尽标记 `failed` 并只保存脱敏错误码。业务读取只
接受 `available`，读删除竞态由状态二次检查和短时 token 限制，不能提供跨系统强一致承诺。

MinIO development 初始化任务创建三个 Bucket。Bucket 原生 lifecycle 只清理未完成 multipart
upload 和带 `physical-delete-approved=true` 标签的对象；业务对象不得仅凭时间绕过 PostgreSQL
删除。三个对象类型的独立保留期由 PostgreSQL Worker 执行，删除完成后才记录最终状态。
Reconciliation 使用相同的可信原生 checksum，或在兼容后端受控流式读取实际 bytes，周期性检查
`available` 记录：缺失转 `failed/storage.object_not_found`，checksum 异常转
`failed/storage.checksum_mismatch`，禁止继续签发下载 URL。

## 异常契约

- `storage.object_not_found`：对象不存在；
- `storage.checksum_mismatch`：HEAD/读取结果与 PostgreSQL checksum 不一致；
- `storage.permission_denied`：对象存储认证或授权失败；
- `storage.temporarily_unavailable`：超时、限流或服务端临时失败；
- `storage.invalid_reference`：URI、Bucket 或对象键不属于当前 Adapter。

异常和日志不包含对象内容、预签名 URL、签名查询参数、Access Key、Secret Key 或远程响应体。

## 配置与秘密

Pydantic Settings 增加 backend、endpoint、region、三个 Bucket、TLS、presign TTL、HMAC key、
各类型保留期、重试和 Worker 参数。三个 Bucket 必须互异；production 对 `backend=local`、HTTP
endpoint、关闭 TLS 或空 HMAC key fail closed。Access Key/Secret Key 使用 `SecretStr`，支持环境
变量和 `*_FILE` Docker secret 路径：同时设置时 fail closed，文件必须非空且不可被其他用户写入，
读取后去除单个行尾但保留内部字符。IAM 权限限于三个 Bucket 的 Get/Put/Head/Delete、必要的
multipart 操作和只读 Bucket 属性；不得授予全局管理权限。三个 Bucket 必须为 private，禁止
public ACL、anonymous access 和允许公共 principal 的 Bucket policy；MinIO 初始化显式关闭匿名
访问，production 启动通过 Bucket policy/public-access 检查 fail closed。SSE/KMS 不在本阶段实现，
但生产部署必须由平台侧启用服务端加密，该外部状态只作为部署前置条件，不伪造验证结果。

## 验证

新增/更新 Port、Local Adapter、S3 Adapter、配置、Repository、上传一致性、预签名服务、Worker
和 API 测试。S3 使用 Stub/Mock，不访问真实 MinIO、云存储或生产数据库；现有文档安全校验测试
继续作为上传前门禁。
