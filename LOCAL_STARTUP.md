# Invoice Intelligence 本地启动手册

本手册适用于 Windows PowerShell，项目根目录为 G:\work\ai。
当前开发环境建议使用 4 个终端窗口；启用本地 PaddleOCR 时增加第 5 个终端。

## 服务与终端分工

| 终端 | 进程 | 是否必须 | 地址/作用 |
|---|---|---|---|
| 终端 1 | Docker Compose 中间件 | 必须 | PostgreSQL、Milvus、etcd、MinIO |
| 终端 2 | FastAPI | 必须 | http://127.0.0.1:8000 |
| 终端 3 | Memory Admission Worker | 生产化记忆流程建议开启 | 消费 PostgreSQL pending 准入任务 |
| 终端 4 | Vue 3 前端 | 必须 | http://127.0.0.1:5173 |
| 终端 5 | PaddleOCR HTTP 服务 | 本地 OCR 开启时必须 | http://127.0.0.1:8188 |

FastAPI 不会自动启动 Memory Admission Worker。Worker 必须单独运行，且只负责记忆准入和单案例投影/清理登记，不负责 Milvus 全量重建。

## 首次准备

### 1. 检查项目目录

    Set-Location G:\work\ai
    Get-ChildItem

### 2. 创建配置文件

只在文件不存在时执行，避免覆盖已有密钥和数据库配置：

    if (!(Test-Path .env.compose)) { Copy-Item .env.compose.example .env.compose }
    if (!(Test-Path .env)) { Copy-Item .env.example .env }

修改 .env.compose：

- PostgreSQL 业务库用户名、密码、数据库名和端口；
- PostgreSQL Checkpointer 用户名、密码、数据库名和端口；
- MinIO 用户名和密码；
- Milvus 对外端口。

修改 .env：

- INVOICE_INTELLIGENCE_BUSINESS_DATABASE_URL 必须与业务 PostgreSQL 配置一致；
- Checkpointer 使用 SQLite 时配置 INVOICE_INTELLIGENCE_CHECKPOINT_BACKEND=sqlite；
- 使用 PostgreSQL Checkpointer 时填写独立 Checkpointer DSN；
- 千问调用需要配置 API Key、Compatible Base URL 和模型；
- 启用本地 OCR 时设置 INVOICE_INTELLIGENCE_OCR_ENABLED=true；
- 启用 Milvus 案例检索时设置 INVOICE_INTELLIGENCE_MILVUS_ENABLED=true 并配置 URI；
- 脱敏策略必须使用 mask、hash 或 drop，生产环境不能使用 none。

## 终端 1：启动 Docker 中间件

在终端 1 执行：

    Set-Location G:\work\ai
    docker compose --env-file .env.compose up -d
    docker compose --env-file .env.compose ps

确认以下服务为 running 或 healthy：

- postgres-business
- postgres-checkpoint
- etcd
- minio
- milvus

如果 Docker 容器已经启动，只需要执行状态检查：

    docker compose --env-file .env.compose ps

正常停止中间件但保留数据：

    docker compose --env-file .env.compose down

不要执行 docker compose down -v，否则会删除命名 Volume 中的数据。

## 首次安装和数据库迁移

可以在终端 2 执行，也可以单独打开一次性终端：

    Set-Location G:\work\ai
    if (!(Test-Path .venv)) { py -3.12 -m venv .venv }
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    pip install -e .
    alembic upgrade head

后续启动不需要重复创建虚拟环境或安装依赖，但新增 migration 后必须重新执行：

    Set-Location G:\work\ai
    .\.venv\Scripts\Activate.ps1
    alembic upgrade head

## 终端 2：启动 FastAPI 后端

    Set-Location G:\work\ai
    .\.venv\Scripts\Activate.ps1
    uvicorn invoice_intelligence.main:app `
      --host 127.0.0.1 `
      --port 8000 `
      --loop asyncio:SelectorEventLoop

Windows 下必须使用 SelectorEventLoop，因为 PostgreSQL Checkpointer 使用的 Psycopg 异步连接不兼容默认的 Proactor Event Loop。

后端检查地址：

- 健康检查：http://127.0.0.1:8000/api/v1/health
- OpenAPI 文档：http://127.0.0.1:8000/docs

PowerShell 检查命令：

    Invoke-WebRequest http://127.0.0.1:8000/api/v1/health

## 终端 3：启动 Memory Admission Worker

Worker 使用与 FastAPI 相同的 .env 和 PostgreSQL 业务库：

    Set-Location G:\work\ai
    .\.venv\Scripts\Activate.ps1
    python -m invoice_intelligence.workers.memory_admission

Worker 行为：

1. 恢复未完成的 memory_review_recoveries；
2. 使用 PostgreSQL FOR UPDATE SKIP LOCKED 原子领取 pending 准入任务；
3. 执行确定性质量验证；
4. 必要时调用审批模型生成结构化建议；
5. 由 Python Policy 决定 approved、quarantined 或 rejected；
6. approved 后登记脱敏索引投影；
7. 网络超时、429、5xx 等临时错误按配置重试；
8. Schema、租户归属和确定性硬失败不重试；
9. 达到最大尝试次数后进入 quarantined。

Worker 不会自动批准记忆，不会修改 InvoiceExtraction，也不会把记忆错误写成发票 Workflow 失败。按 Ctrl+C 可优雅停止；已领取任务会等待当前处理结束。

多 Worker 部署时，为每个进程设置不同的 INVOICE_INTELLIGENCE_MEMORY_ADMISSION_WORKER_ID。

## 终端 4：启动 Vue 3 前端

首次安装：

    Set-Location G:\work\ai\frontend
    npm install

启动前端：

    Set-Location G:\work\ai\frontend
    npm run dev

浏览器访问：

    http://127.0.0.1:5173

Vite 会把 /api 请求代理到 http://127.0.0.1:8000。

前端包含：

- 提取工作台；
- 记忆准入；
- 案例库；
- 字段语义；
- 冲突；
- 索引；
- 评估；
- 审计。

前端不接受用户输入 tenant_id，不直接访问 PostgreSQL、Milvus、LangGraph 或模型服务。

## 终端 5：可选启动本地 PaddleOCR

只有在 .env 中启用本地 OCR 时才需要终端 5：

    cd G:\work\ai
    .\scripts\manage-local.ps1 -Action start -IncludeOCR

也可以在独立 PowerShell 终端中直接启动并查看 PaddleX 日志：

```powershell
Set-Location G:\work\ai
.\.venv-ocr\Scripts\Activate.ps1
$env:PADDLE_PDX_CACHE_HOME = (Join-Path $PWD '.data\paddlex-cache')
paddlex --serve `
  --pipeline .\conf\ocr\ppocrv6_small_v1.yaml `
  --host 127.0.0.1 `
  --port 8188 `
  --device gpu:0
```

必须使用版本化 YAML：

    conf/ocr/ppocrv6_small_v1.yaml

不要使用 --pipeline OCR，该参数会加载默认的 PP-OCRv6_medium，不会继承 Python 中的 Small 模型配置。

本地 OCR 服务实际使用：

- PP-OCRv6_small_det；
- PP-OCRv6_small_rec。

OCR 只提供独立观察，不能覆盖 Vision 结果、修改 InvoiceExtraction 或自动填充字段。

## 推荐启动顺序

    1. Docker Compose 中间件
    2. 数据库迁移（首次启动或有新 migration 时）
    3. FastAPI
    4. Memory Admission Worker
    5. PaddleOCR（可选）
    6. Vue 3 前端

## 一键管理方式

如果不需要观察每个进程的实时输出，可以使用项目已有脚本管理 FastAPI、Worker 和前端：

    Set-Location G:\work\ai
    .\scripts\manage-local.ps1 start
    .\scripts\manage-local.ps1 status
    .\scripts\manage-local.ps1 stop

同时管理本地 OCR：

    .\scripts\manage-local.ps1 start -IncludeOCR
    .\scripts\manage-local.ps1 status -IncludeOCR
    .\scripts\manage-local.ps1 stop -IncludeOCR

该脚本不会替代 Docker Compose；中间件仍需单独使用终端 1 启动。

## 上传图片后的最小校验流程

1. 打开 http://127.0.0.1:5173。
2. 上传 PNG、JPG、JPEG、WEBP 或 PDF。
3. 查看提取工作台中的 Workflow 阶段状态。
4. 检查 Vision、OCR、字段绑定、Retrieval 和 Validation 状态。
5. 如果需要人工审核，确认字段候选、证据和原因后提交审核事实。
6. 查看 Run 状态；记忆失败时 Run 仍应保持业务 completed，记忆状态显示为 pending 或可重试状态。
7. 在记忆准入页面由授权 Reviewer 处理 pending 案例。
8. approved 后在案例详情查看 PostgreSQL 登记的索引投影状态。

## 常见问题

### 后端无法连接 PostgreSQL

检查：

    docker compose --env-file .env.compose ps
    Get-Content .env | Select-String "BUSINESS_DATABASE_URL|CHECKPOINT"

确认业务 DSN 的用户名、密码、数据库名和端口与 .env.compose 一致。

### Qwen 返回 400

查看 FastAPI 日志中的脱敏字段：

- remote_status_code；
- remote_error_code；
- remote_error_message；
- remote_request_id。

不得在日志中记录 API Key、完整 Prompt、图片 Base64 或完整发票值。

### 本地 OCR 显存不足

确认使用的是 conf/ocr/ppocrv6_small_v1.yaml，并保持 --device gpu:0。

不要改回默认 --pipeline OCR，也不要把 Small 配置与 Medium 默认配置混用。

### 前端页面无法访问 API

确认终端 2 正在运行，并检查：

    Invoke-WebRequest http://127.0.0.1:8000/api/v1/health

## 停止顺序

1. 前端终端按 Ctrl+C；
2. PaddleOCR 终端按 Ctrl+C；
3. Memory Admission Worker 按 Ctrl+C，等待当前任务结束；
4. FastAPI 终端按 Ctrl+C；
5. Docker 中间件按需执行 docker compose --env-file .env.compose down。

正常 down 不会删除数据；禁止使用 down -v，除非明确要清空全部开发数据。

## 数据与安全边界

- PostgreSQL 是审核事实、业务结果、案例、准入、目录、投影状态和审计的唯一事实源。
- Milvus 只保存 approved、有效且脱敏的派生数据。
- pgvector 仅作为开发/迁移回退，不与 Milvus 长期双查询。
- 历史案例只能作为先验，不能覆盖当前图片证据。
- confirmed_incorrect 只能进入负例区域。
- tenant_id、版本、审批状态和 Trace 不属于 InvoiceExtraction。
- 日志不得记录完整发票值、修正值、图片、Base64、Prompt、API Key、向量或 Idempotency-Key 原文。
