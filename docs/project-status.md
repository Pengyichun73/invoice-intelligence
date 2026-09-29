# 项目实现状态

本文是 Invoice Intelligence 当前实现进度的唯一状态基线。README、方案与架构文档描述设计和使用方式；
当它们与本文的完成度判断冲突时，以本文和当前代码、migration 为准。

状态核对日期：`2026-09-28`。验收数据已迁入默认 Compose 项目 `invoice-intelligence`；
业务、Checkpoint、Keycloak 计数与源一致，案例投影清单比对通过。OCR 与提取 Worker
未在目标项目重验，记忆带来的识别收益仍无配对数据支持。
旧 `invoice-acceptance` 容器和七个原卷已删除；源与目标迁移前的 13 份卷归档及
SHA-256 清单保存在 Git 忽略的本地 `artifacts/acceptance-to-main-20260928/`，
包含敏感数据，不得上传或提交。Keycloak realm 名仍为 `invoice-acceptance`。

多票批次增量：批次、源文件及子票据持久化表、分段 Worker、五票上限、逐票 Run、
边界确认与前端逐票审核入口已加入代码；跨页 OCR 异值会触发字段审核，并保留原页区域与
派生页尺寸以供定位。`0050` migration 尚未在目标业务库执行，真实同页多票、跨页归并、
OCR 故障、重启恢复及页面审核闭环尚未隔离验收；当前不得宣称多票识别准确或生产可用。

记忆增强增量：值盲 Prompt 与 `field-pattern-v1-` 派生索引代码、默认关闭的 OCR 定向区域
重读、三人 19 字段标注的离线冻结脚本及逐样本三变体真实提取服务已加入；
新增四个只读效果查询端点、三人可信身份标注 API、私有对象/PG 冻结事实边界、
现有 Evaluation Job 队列的批量三变体入口、五类受控场景聚合、脱敏判定持久化及 `0046` 至 `0049`
migration 源码。标注 Bucket 尚未配置并验收，migration 未在目标库执行；
完整样本、索引重建激活和现场配对评估仍缺失，重读持久证据未接入；
shadow 差异与按租户/字段/模板/索引版本受控 `apply` 尚未完成。
离线 JSONL 身份仍非 OIDC 认证事实。目前只有本机 MinIO，独立异机 MinIO
加密备份及恢复演练未完成，生产门禁保持关闭。

## 首阶段目标：隔离环境发票与可信记忆闭环

验收目标是上传后立即获得可恢复的 `run_id`，由可信审核人形成字段审核事实，再由不同的
授权人员批准合格候选；仅合格案例投影到 Milvus，并在第二张同类发票中被受控召回。
财务、交易、训练、晋升和代码 Harness 不阻塞此阶段。

| 验收门禁 | 当前状态 | 尚需证据 |
|---|---|---|
| Compose、业务库 `0045`、共享 Checkpointer、API/Worker | 默认项目业务库、Checkpoint 与 API 已迁移并健康；提取 Worker 因 OCR 8077 未运行而保持停止 | 需目标项目长任务、租约过期和重启后的持久状态证据 |
| 长时提取及审核恢复 | 已新增 PostgreSQL 队列和 Worker，未隔离验收 | 超过 3 分钟、断线重读、租约过期、崩溃恢复和重复请求 |
| 两名可信审核人和 19 字段审核事实 | 两账号在目标项目的 OIDC PKCE、JWT 与角色隔离通过；用户报告原环境审核准入已完成 | 仍需目标项目同 Run 的字段、CorrectionEvent、ReviewedExample、Admission 关联及审计复核 |
| 批量准入与确定性门禁 | 代码已有，未闭环验收 | 逐项持久状态、revision、硬失败原因；旧 rejected 保持终态 |
| Milvus 投影与后续发票召回 | 目标项目 5 条 PostgreSQL-Milvus 案例投影 ID/checksum 清单一致，Alias 可读 | 第二张发票受控召回及识别收益仍需现场复核 |

按表中顺序推进。`/health`、`/ready`、返回 202、前端提示及 PostgreSQL 中的
`indexed` 字段都不能替代运行事实或 Milvus 清单验证。验收证据只保存脱敏标识和状态。

## 状态定义

- **完成**：主路径已有代码、持久化和必要边界，不依赖占位入口。
- **受限可用**：主体已实现，但启用条件、外部 Adapter 或已知安全/部署缺口限制生产使用。
- **部分实现**：已有 Domain、API 或 Worker 的一部分，关键闭环仍缺失。
- **未实现**：只有设计、占位入口或明确的 fail-closed 边界。

文件、类、路由或 Compose service 的存在不等于能力完成。

## 当前总览

| 模块 | 状态 | 当前事实与边界 |
|---|---|---|
| Vision Prompt 与 Schema Validation | 完成 | Prompt 分离并版本化；本地 Pydantic 校验、exact evidence coverage、脱敏 retry diagnostic 和可选 LangSmith Registry 已实现 |
| OIDC/JWT、可信租户与 RBAC | 受限可用 | OIDC/JWT、TrustedTenantContext、权限策略和审计基础已实现；Training 四个端点已绑定 `training:submit`，生产部署仍受数据库凭据链路等缺口限制 |
| Human Review Task Service | 完成 | 列表、详情、claim/release/reassign、lease 恢复、幂等提交、可信 Reviewer、跨租户 404 和 Workflow resume 已实现 |
| 对象存储 | 受限可用 | Local 与 S3-compatible Adapter、HEAD/checksum/presigned URL、生命周期和迁移 Worker 已实现；生产 Compose 仍需容器连接及其余安全配置验收 |
| 索引重建与投影 | 受限可用 | 两类 projection 已有 lease/fencing；激活门禁比对合格源、PostgreSQL 投影与 Milvus ID/checksum；双进程及真实隔离 Milvus 合成数据、恢复后新 Collection 重建与失配阻断验收通过，持续漂移和生产规模未验收 |
| 离线评估 | 受限可用 | 诊断型 Snapshot Job、冻结 Suite Job、续租/确认链、HTTPS Adapter 与独立只读服务边界已实现；缺真实变体引擎、隔离证据快照与环境验收时 Suite Job 隔离，无 Stub 回退或晋升 |
| Training Registry | 受限可用 | Registry、导出、产物、PostgreSQL claim/lease Worker、fail-closed Stub 和 MLflow-compatible Adapter 已实现；Training API 已绑定 RBAC，真实平台仍需显式凭据 |
| 模型晋升与回滚 | 受限可用 | 客户端不能声明门禁事实；同租户 Evaluation/Artifact/Version 重载、审批 CAS 与有效历史目标回滚已实现；仅切换 PostgreSQL 注册状态，非真实部署 |
| 财务 Domain | 受限可用 | AccountingCandidate、规则版本、汇率快照、posting proposal、幂等/CAS 和同租户完成 Run 校验已实现；仅提供 mock 外部财务 Adapter |
| 交易分析 Domain | 受限可用 | 候选由同租户 completed Result 派生，审核采用幂等与 revision/CAS；历史不可信候选仅可审计升级为 escalated，规则仍为开发 Mock |
| Code Generation and Self-Healing Harness | 部分实现 | 已有 Domain/Port、完整版本绑定、固定八阶段 Runner、Snapshot/结构化 Patch 校验、受控 create/delete Patch 契约、Python AST/结构化参数/精确检索适配器、caller/callee/dependency 结构化查询、Snapshot/Patch/Execution/Watchdog PostgreSQL 事实写入、Task/Postmortem/Repository Source Registry repository、Task/Attempt claim/lease fencing、通用 Worker、含 Repository/Snapshot/Task/Patch/Execution 来源的结构化 Postmortem source event、Postmortem 治理 Application/API、预算与可信 trace_id、共享隐私 Trace/低敏 Metrics Adapter、Grammar Registry、Harness API 和独立 Worker 入口、fail-closed Sandbox；Source Registry 注册/更新治理 API、运行时 reload、真实多语言 Parser、Code Model、代码 Milvus、MicroVM 和长期代码经验投影仍未完成 |
| Docker Compose | 受限可用 | profile、volume、resource limit、liveness/readiness、runtime worker probe 和 `.dockerignore` 已配置；原业务库已迁移至 `0044`，独立 project API/Index Worker 健康且重启恢复通过；旧项目服务名仍与工作树 Compose 不一致，原业务流量未切换 |
| Observability 与 DLP | 受限可用 | 低基数 Worker/Provider/audit failure metrics、熔断计数、受控 `/metrics`、development 本地 JSONL 自动落盘/轮转与限长 Trace 诊断包入口已实现；未接 Prometheus/Grafana、共享 Provider 配额、生产 DLP 或外部 Trace Collector |

## 已完成主路径

- Harness 已建立租户边界、版本契约、Repository Snapshot、结构化 Patch、Watchdog/Postmortem Domain，
  固定八阶段 Runner、Application Service 入口、Task claim/lease fencing、通用 Worker、低敏 Metrics
  Adapter、Attempt 完成 fencing、结构化 Postmortem source event、Snapshot/路径/范围校验和默认拒绝
  Sandbox；预算包括最大修改行数、Patch operation 数和模型 Token 数，可信 `trace_id` 已贯穿
  Task/Workflow/Postmortem；这些能力仍不代表真实外部依赖或生产执行能力。

- Harness Parser 已通过 `GrammarRegistry` 做语言注册门禁；Snapshot 对常见二进制内容执行保守分类，
  Parser 不读取二进制源码，结构化 Patch 默认拒绝二进制文件；代码上下文检索使用结构化
  `CodeQuery` Port，不退化为绕过 Port 的自由文本查询。

- 固定 `InvoiceExtraction` Schema 的 Vision 提取、Schema validation、字段 evidence 覆盖和有限重试。
- 确定性单一 LangGraph Workflow、人工中断/恢复和 Review Task 生命周期。
- PostgreSQL 审核事实、记忆准入、字段语义治理及 S3-compatible 存储主体。
- 冲突关闭请求的短事务消费者；仅明确因该冲突隔离的案例会重新进入准入队列，别名和其他原因仍需人工复核。
- 版本化 Training Registry 与独立 training worker；未配置 Provider 时明确 fail closed。
- 独立 Accounting Domain；识别结果不会直接入账，默认只连接 mock provider。
- 开发基线脚本 `scripts/verify-dev-baseline.ps1` 已建立：在 development 环境检查本地可信租户、
  应用组合根导入和 Python 编译，不连接外部依赖；该检查不替代生产 Compose 或隔离环境验收。
- Alembic 源码新增 `20260927_0045_extraction_work_queue`；原业务库此前已到 `0044`，本轮未执行 `0045` 迁移，不能宣称库与源码同 head。

## 已知阻塞与风险

1. 业务数据库 password file 已在原库 migration 与隔离 Compose API/Worker 验证；当前旧项目服务名与工作树 Compose 不一致，原业务 API/Worker 切换及完整生产连接仍未验证。
2. Promotion 只切换 PostgreSQL 注册状态，不执行真实模型部署；真实 Suite 评估 Runner 与晋升 Worker 仍未实现。
3. Transaction 规则仍为开发 Mock；历史客户端构造的候选与可信派生快照不一致时拒绝确认或驳回，授权 Reviewer 可审计升级，原候选仍不转成可信事实。
4. `evaluation-worker` 与 `scheduler` 现与 API 使用同一业务 PostgreSQL 评估 Job queue；Suite Job 已冻结绑定并可选装配 HTTPS 隔离 Runner Adapter，独立服务边界已实现，但尚缺真实变体引擎、只读隔离证据快照及 PostgreSQL 崩溃恢复验收。`training-worker` 是唯一训练执行入口，负责提交、刷新和取消，真实平台仍需显式凭据。
5. Index projection 已在隔离恢复库与真实 Milvus 从合格源重建两类新 Collection，双进程、清单与 Alias 失配门禁验收通过；持续漂移监测与生产规模仍未验收。
6. Compose runtime probe 已在隔离 API/Worker 容器核对 password file、稳定 worker ID 和数据库连通性；旧项目服务差异与缺少旧应用镜像仍阻止原业务流量切换。
7. `/api/v1/ready` 只做安全配置 readiness，不代表 Milvus、对象存储或远程 Provider 已可用。
8. 隔离 Compose 的业务库、独立 PostgreSQL Checkpointer 和 MinIO 卷已从 SHA-256 校验归档
   恢复；合成数据观察到丢失 0 条、恢复就绪 65.87 秒，Milvus 两类清单重建通过。
   生产 RPO/RTO、Keycloak 可选分支、持续写入和生产规模仍未验收。
9. 根目录已添加 Apache-2.0 `LICENSE`；版权主体为 `love-ovo73`，版权年份为 2026。

## 后续任务

后续 Codex 执行应使用 [`codex-next-target-feature-prompt.md`](codex-next-target-feature-prompt.md)，
每次只完成一个仍未验收的模块，并分别记录“已实现、隔离环境已验收、生产已启用、受限可用”。

### P0：安全与部署正确性

1. 在后续 production preflight 中完成业务数据库 password file 的容器连接和权限验收。

### P1：后台任务闭环

5. 提供并验收独立服务的全部真实变体引擎、脱敏只读证据快照与凭据；对续租、过期重领后的 Run fencing 执行真实 PostgreSQL 双 Worker 并发和数据泄漏演练。诊断型 Stub 报告不得充当晋升证据。
6. 核实并保留 `training-worker` 的单一执行路径；已移除误导性的 training-sync 占位 service。
7. 对两类 index projection 继续验收长期容器实际队列、持续漂移监测与容量；隔离恢复重建及破坏注入基线见验收记录。

### P2：生产运维

8. 在隔离环境演练业务级 healthcheck、容器凭据校验和 production preflight。
9. 在隔离环境实际执行 PostgreSQL、对象存储备份恢复及 Milvus 重建演练（runbook/helper 已准备）。
10. 在隔离环境接入并验证 Prometheus/Grafana 或等价告警接收端；当前项目已提供低基数 metrics 与规则示例。

### P3：质量与治理

11. 分批清理 Ruff 当前报告的 99 个问题；本轮已完成 `F401`、`F841` 和 import order，剩余以 `E501`、`UP012`、`UP035`、`UP040`、`UP046`、`UP047` 为主。
12. 已为新增安全边界、Evaluation Worker、事务、CAS、幂等和跨租户语义补齐定向回归测试；仍需在隔离 PostgreSQL/容器环境完成并发演练。
13. 已增加本地文档链接与状态声明检查脚本；远程 CI 接入仍需仓库环境。
14. 已完成：项目所有者选择 Apache-2.0，根级 `LICENSE` 已添加；第三方依赖和参考项目仍按各自许可证处理。
15. 前端已新增“运行治理 → 后台操作”聚合入口，覆盖 Evaluation Job 查询、Training Job 查询/取消/重试、Promotion Candidate 创建/审批/拒绝/回滚和 Transaction Candidate 分析/复核；仅展示脱敏状态与版本元数据，不代表后台 Worker 或生产部署已完成。
16. 为 Harness 增加长期代码经验派生投影和受控检索；在此之前，source event 与 `approved` 状态
    仅表示 PostgreSQL 基础事实已通过治理 CAS，不表示可检索长期记忆已生效。

## 本次盘点验证

- 代码、路由、Repository、Worker、migration、配置和 Compose 均按实现读取，而非按命名判断。
- 当前代码新增 `0045` 提取队列迁移；原业务库于 2026-09-25 迁移至 `0044`，`0045` 尚未在业务库执行。
- 使用 `.env.example` 补齐 Compose 变量后，`docker compose config --quiet` 可解析配置结构；这不代表服务可生产启动。
- Ruff 只读检查结果为 99 个问题：44 `UP012`、30 `E501`、12 `UP046`、9 `UP035`、3 `UP040`、1 `UP047`；本轮选定的 `F401`、`F841`、`I001` 已清零。
- 初始文档统一时未运行 pytest；后续各任务的定向验证见下方交付记录。未启动服务、未连接真实 Provider 或生产数据库。

## 后续交付记录

- `2026-09-24`：P0 部署静态核对确认源码 Alembic 单一 head 为 `0044`；业务 PostgreSQL
  password file 在 API、migration、Scheduler、Evaluation 与业务 Worker 中使用统一 Settings
  解析路径。修正 preflight 对合法末尾换行的误拒绝，并使 Compose API/生命周期 Worker 的
  S3 endpoint 可配置为 HTTPS，应用凭据可独立于 MinIO root secret 注入；S3 启动检查现拒绝
  Principal 数组中的公开授权。
  定向认证、凭据、队列、Worker health 与 S3 Policy 回归 `29 passed`；Compose 配置解析核对
  10 个业务数据库消费者和 2 个 S3 消费者，Alembic `heads` 返回单一 `0044`，文档与 Ruff 检查通过。
  `test_preflight.py` 的 pytest `tmp_path` 在本机因 WinError 5 无法运行，已用无文件系统依赖的
  定向断言覆盖合法末尾换行、空值、多行与超长内容。
  Docker daemon 当前不可连接，工作区仅有示例 secret；
  容器内连接、实际 migration、OIDC/TLS、private Bucket、checksum 与生命周期尚未隔离验收。

- `2026-09-24`：新增 `code_harness_sources` PostgreSQL 事实表、SQLAlchemy Repository 和
  Composition Root/Worker 装配；Harness Worker 启动时仅加载已启用的预登记 Source，缺少迁移、
  Source 或外部 Parser/Model/Index/MicroVM 时继续 fail closed。Source 注册/更新治理 API、
  运行时 reload 和真实 PostgreSQL 验收仍未完成。
- `2026-09-24`：新增 `scripts/verify-dev-baseline.ps1`，用于在 development 环境验证本地可信租户、
  开发认证、production-only preflight 分支、应用组合根导入和 Python 编译；脚本不连接 PostgreSQL、
  Milvus、S3、OIDC 或远程 Provider，不执行迁移，也不生成评估或晋升证据。开发逻辑可继续运行，
  真实生产与隔离环境验收仍保持为后续受限模块。
- `2026-09-24`：索引投影 Worker 不再静默丢弃两类 `verify_index_version()` 的失败结果；校验
  失败会写入低基数 Worker 指标和脱敏结构化日志，并保持版本不可激活。Worker 不切换 Alias，
  因此旧 Alias 继续保留。新增 Worker 行为回归；索引门禁与租约定向验证共 `17 passed`。
  真实 Milvus 与隔离 PostgreSQL 双 Worker 验收仍未执行。
- `2026-09-23`：Training 四个端点接入 `training:submit`；权限拒绝审计、Training 路由覆盖和
  跨租户 404 定向用例已通过。执行 `test_auth_security.py`、
  `test_training_registry_repository.py`、`test_training_api.py`：15 passed。未运行迁移或启动服务。
- `2026-09-23`：Docker secret 由 Settings 在进程内解析为业务 DSN，Alembic、API 与 Worker
  共享该解析路径；Docker entrypoint 不再将密码导出为环境变量。配置定向测试 25 passed，
  11 个业务 Compose service 的 password-file 契约检查通过。3 个对象存储测试因本机 pytest
  `tmp_path` 目录权限错误未能执行；未启动容器或连接数据库。
- `2026-09-23`：Transaction API 收窄为 `run_id`，仅从同租户 completed Result 派生候选；
  审核加入 revision/CAS，幂等重放使用事务内审计快照。新增迁移 `20260923_0032_transaction`，
  未执行迁移或启动服务；隔离 SQLite 交易、认证与财务定向测试 21 passed，变更文件 Ruff 检查通过。
- `2026-09-23`：Promotion 创建请求仅引用可信评估提案与产物；审批重载门禁，回滚在一个事务中
  恢复有效历史 Candidate。新增迁移 `20260923_0033_promotion`；隔离定向测试 23 passed，
  变更文件 Ruff 检查通过；未执行迁移、启动服务或连接生产部署控制面。
- `2026-09-23`：两类 Index Projection 加入有期限、可续租的 PostgreSQL 租约；Worker ID 接入
  Compose，完成/失败由 worker/token/expiry 条件更新保护，过期重领且有限重试；审核案例 Alias 激活
  需验证目标 Collection，并按租户切换，注册失败时尝试恢复先前 Alias。新增迁移
  `20260923_0034_index_lease`；隔离 SQLite 与 Settings 定向测试 11 passed，变更文件 Ruff 检查通过，
  Compose `core` profile 配置可解析。未执行迁移、
  启动服务或连接生产 Milvus。
- `2026-09-23`：新增脱敏 Dataset Snapshot、Evaluation Job/Schedule 表与 PostgreSQL claim/lease
  Repository；评估 API 只登记任务，独立 Worker 使用确定性 Stub 计算聚合指标，Scheduler 只登记到期
  Job；新增迁移 `20260923_0035_evaluation`。完成 Python 编译和导入校验，未执行迁移、启动容器或连接
  生产数据库。
- `2026-09-23`：核实 `training-worker` 已承担 Training Registry 的提交、刷新、取消和失败恢复；
  删除不存在队列对应的 `training-sync-worker` Compose service、占位模块及环境变量，避免将
  fail-closed 入口误认为生产能力。未执行迁移、启动容器或连接远程训练平台。
- `2026-09-23`：新增安全配置 preflight、`GET /api/v1/ready`、Worker runtime health probe、
  `.dockerignore`，并为主要 Worker 与 storage lifecycle 接入数据库/租约前置检查；移除明确禁用的
  `model-promotion-worker` Compose 入口。未启动容器、执行迁移或连接生产依赖。
- `2026-09-23`：新增 [`docs/backup-restore-drill.md`](backup-restore-drill.md) 和
  `scripts/backup-restore-drill.ps1`。脚本默认 dry-run，显式 `-Execute` 时仅面向隔离
  Compose project 执行 custom-format PostgreSQL、对象存储 checksum 和恢复步骤；Milvus
  仍按 PostgreSQL 事实重新 register/project/verify 后显式激活 Alias。本次未执行备份、
  恢复、迁移、容器或真实 Milvus。
- `2026-09-23`：新增低基数 `MetricsRegistry`、受控 `GET /api/v1/metrics`、Provider
  circuit-open/调用、Index/Evaluation Worker processed/retry/lease recovery 和 security audit
  write failure 指标；新增 [`docs/observability-alerts.md`](observability-alerts.md) 规则示例。
  未接入 Prometheus/Grafana，未执行真实故障注入或生产告警演练。
- `2026-09-24`：完成 Ruff 低风险清理：删除确认无用的 `Path` 导入和 memory admission 局部变量，
  修正 7 个模块的 import order；`F401`、`F841`、`I001` 定向检查通过，源码编译通过。
  全仓剩余 99 项，主要为长行和 Python 现代化建议，未做无关格式化。
- `2026-09-24`：补齐 Evaluation 回归矩阵：新增
  [`tests/test_evaluation_jobs.py`](../tests/test_evaluation_jobs.py)，覆盖 Snapshot 版本不可变、
  跨租户 404、Job 幂等语义、PostgreSQL queue claim/lease、迟到 Worker fencing、重试隔离、
  Scheduler 入队幂等和“只保存聚合指标”边界。定向测试 `6 passed`，Evaluation/Promotion/
  Training/Auth/Transaction 相关回归 `34 passed`；未完成真实 PostgreSQL 双 Worker 并发演练。
  本机对象存储测试仍受 pytest 临时目录权限（WinError 5）阻塞，非业务断言失败。
- `2026-09-24`：新增 [`scripts/check-docs.ps1`](../scripts/check-docs.ps1)，对 README、方案、
  架构及 `docs/*.md` 执行本地链接存在性、状态基线标记和已知过时声明检查。检查脚本通过；
  当前尚未接入远程 CI，不能据此宣称文档与实现已具备持续集成门禁。
- `2026-09-24`：前端新增“运行治理 → 后台操作”聚合视图及对应 API 方法，覆盖评估任务、
  训练任务、模型晋升候选和交易候选；前端测试 `18 passed`，Vite 构建通过。该视图不接收
  tenant/reviewer 覆盖字段，不执行后台任务或生产部署。
- `2026-09-24`：项目所有者选择 Apache-2.0；新增根级 [`LICENSE`](../LICENSE)，并在
  `pyproject.toml`、README、架构文档和状态基线中同步声明 `love-ovo73`/2026。未修改第三方
  依赖许可证边界，也未新增 NOTICE。
- `2026-09-24`：冲突重评估请求新增完成/人工复核状态、短事务消费者及 Compose 入口；
  仅冲突原因隔离的记忆案例重新入队，确定性硬失败和别名审批仍受人工门禁。Evaluation Job
  明确标记 `diagnostic_only`；修正容器健康检查读取业务数据库 password file 的路径。
  定向测试 11 passed，新文件 Ruff 检查通过，Alembic 单一 head 与文档检查通过；前端构建通过。
  Docker daemon 未运行，且正式 secret 未配置，容器连接与真实 PostgreSQL/Milvus 验收未执行。
- `2026-09-24`：内建 Milvus 两类索引激活前强一致读取 Collection 的 ID/checksum 全量清单，
  并与 PostgreSQL 已审批案例及版本化字段投影清单比对；差异阻断 Alias 切换。
  隔离 Mock 回归 10 passed；当时尚未比对字段目录源集合，也未使用真实 Milvus
  验证迭代查询和并发写入边界。字段目录源集合的后续补充见下方记录。
- `2026-09-24`：Promotion Evidence 增加冻结评估数据集内容、真实 Suite 报告版本与报告产物引用
  校验；无效数据集、诊断报告版本或缺少产物均拒绝晋升。相关安全边界定向回归 24 passed，
  变更文件 Ruff 检查通过；该校验不能替代尚未接入的真实 Suite Runner。
- `2026-09-24`：索引治理前端区分“未激活·待校验”和“已激活”，说明投影登记与 `/ready`
  不能证明生产验收；后台激活仍由服务端执行完整性门禁。Vite 构建通过。
- `2026-09-24`：字段语义索引激活前，从指定 Catalog 版本重新生成合格 semantic ID 与源指纹；
  与 PostgreSQL 投影快照逐项比对后，再比对 Milvus ID/checksum。漏登记或过期目录快照阻断激活。
  索引定向测试 12 passed，相关 Ruff 与 mypy 检查通过；真实 Milvus 和并发写入尚未验收。
- `2026-09-24`：修正 Evaluation Worker/Scheduler 与 API 队列使用不同 DSN 的断链；三者现在
  通过业务 PostgreSQL 的 password-file 解析读取同一评估 Job 表。真实 Suite 隔离数据源仍未接入；
  本次仅修复诊断队列连通契约。相关定向测试 9 passed，Compose 配置解析、Ruff 与文档检查通过；
  尚未进行容器内连接演练。
- `2026-09-24`：OfflineEvaluationService 改为先发布聚合报告并绑定产物引用，再持久化 `completed`
  Run；缺少 Publisher 或产物时 fail closed，避免完成状态早于证据。定向及隔离 SQLite 持久化测试
  5 passed；
  真实 Suite Runner、冻结数据集 Job 绑定和隔离证据读取仍未接入。
- `2026-09-24`：历史不可信交易候选可由授权 Reviewer 使用既有幂等与 revision/CAS 审核路径
  升级为 `escalated`，写入脱敏来源原因码和审计；确认/驳回仍拒绝，候选快照保持原样。
  隔离 SQLite 定向测试 6 passed，变更文件 Ruff 与 mypy 检查通过。
- `2026-09-24`：核验单一 Training Worker 的失败分类：未配置 Stub 进入永久失败/隔离，远程
  429/5xx 进入有限重试，400 与无效响应为永久失败；错误不带远程响应正文或凭据。MockTransport
  定向测试 7 passed；未连接真实训练平台。
- `2026-09-24`：修正文档中关于冲突重评估消费者及索引投影固定租约的过时描述；仅同步已存在的
  实现事实，未据此将容器连接或真实 Milvus 标记为已验收。
- `2026-09-24`：离线评估执行器默认要求同时配置两套 Suite 的全部规定变体 Runner，缺项在
  构造时 fail closed；两套 Suite 并集与可信记忆专属变体定向测试 2 passed。尚未实现真实 Runner、
  冻结数据集 Job 绑定或容器内隔离证据读取。
- `2026-09-24`：冻结 `EvaluationDataset` 现在要求训练侧和每个评估案例都提供模板指纹；缺失
  指纹的旧数据在反序列化与晋升证据重载时失效，不能把“无指纹”视为模板隔离成功。评估产物
  与晋升定向测试 17 passed，变更文件 Ruff 检查通过。Docker daemon 仍不可连接，仅有示例
  secret，隔离 Compose、真实 PostgreSQL/Milvus 和恢复演练尚未执行。
- `2026-09-24`：新增 `20260924_0037_evaluation_suite_jobs`，在既有 Job 队列中固化
  同租户冻结数据集、Suite、检索 Policy 与版本绑定；旧 Snapshot 诊断 Job 保持兼容。
  `/evaluations/suite-jobs` 要求 `evaluation:run`；无 Runner 时 Worker 以固定错误码隔离，不回退
  Stub。SQLite 迁移/绑定/RBAC 定向回归 23 passed；前端单进程测试 18 passed、Vite native
  config loader 构建通过。常规 npm 命令因沙箱 `spawn EPERM` 未直接通过；未运行真实数据库迁移。
- `2026-09-24`：新增 `20260924_0038_evaluation_job_run_link`，Suite Job 的每次领取使用独立
  Run ID，运行中续租，完成时由 PostgreSQL 当前租约重载并确认 Run/版本/报告；晋升仅接受已
  确认的完成 Suite Job，迟到或失配 Run 被拒。隔离 SQLite 定向测试覆盖完成、续租、缺报告、
  超时、迟到租约和晋升重载；容器仍未配置真实 Runner，未执行真实 PostgreSQL 并发与恢复演练。
- `2026-09-24`：Evaluation Worker 新增可选 HTTPS 隔离 Runner Adapter 与 `bootstrap.py` 装配；
  请求只含冻结训练/评估文档与模板清单、证据引用及版本，响应须绑定请求和清单 SHA-256 并通过
  Pydantic/Domain 校验。聚合报告写入 PostgreSQL `evaluation_report_artifacts`；未提供远端
  评估服务、token 或隔离证据账户，默认继续隔离 Suite Job。MockTransport 与 Suite Job 定向
  回归 16 passed；真实远端及 PostgreSQL/Compose 验收未执行。
- `2026-09-24`：新增独立 ASGI Evaluation Service 工厂与只读证据快照 Adapter；服务启动要求
  全部 11 个变体引擎、token 文件和按租户登记的证据 SHA-256，运行时校验冻结清单、案例绑定及
  脱敏证据内容。共享变体在请求中显式绑定 Suite；Worker Adapter 限制 `isolated://` 引用和响应大小。两套 Suite 全部变体的进程内契约测试
  使用诊断型假引擎，仅证明边界，不产生 Promotion Evidence。真实引擎、快照、TLS 和独立部署
  尚未提供，真实 Suite、PostgreSQL 双 Worker 与 Compose 验收未执行。
- `2026-09-24`：新增 `20260924_0039_evaluation_report_artifacts`，将 JSON/Markdown 聚合报告
  以不可变内容和 SHA-256 保存到 PostgreSQL；Suite Job 完成与 Promotion Evidence 重载均
  校验 Run、tenant、schema、内容和引用顺序，缺失或篡改时 fail closed。评估绑定与晋升定向
  回归 29 passed，迁移保留旧诊断行的等价 SQLite 验证已补齐；真实数据库迁移仍未执行。
- `2026-09-24`：Evaluation Dataset Repository 新增列元数据与不可变 JSON payload 的
  tenant、dataset、version、schema 一致性校验，并在 Evaluation Run 绑定时再次核对冻结数据集；
  漂移或失配直接抛出持久化错误，阻止形成可晋升评估事实。评估完整性与相关回归共
  `30 passed`，新增测试 Ruff 通过；真实 PostgreSQL、隔离 Runner 与生产迁移仍未执行。

- `2026-09-24`：在同一隔离 PostgreSQL 实例的专用库和真实隔离 Milvus 上，两个独立 Worker
  进程完成 Reviewed Example 与 Field Semantic 并发投影；4 条合格案例和 19 条字段语义清单
  的 ID/checksum/数量与 PostgreSQL 一致，拒绝状态案例未入索引。续租、过期重领、迟到
  token fencing、失败重试与幂等通过。新版本缺项、额外项、错租户/版本及 checksum 篡改均阻断
  激活，旧活动版本与旧 Milvus Alias 保留。实测修复 PyMilvus tuple upsert、集合初建时索引未就绪、fresh
  Alembic 版本列长度以及 Docker 构建包元数据。定向测试 `17 passed`，Ruff 和编译通过；
  命令、失败记录与剩余风险见 [`index-projection-acceptance-2026-09-24.md`](index-projection-acceptance-2026-09-24.md)。

- `2026-09-25`：原业务库实际 `0031`、15 MB、79 张表，独立 PostgreSQL Checkpointer 约 8 MB；
  业务库与 PostgreSQL Checkpointer custom-format 归档及当前 SQLite Checkpointer 文件已备份，
  分别通过新数据库恢复或 SQLite integrity_check。原库归档恢复的两个独立数据库先后用挂载
  migration 和固定 digest 镜像完成全部 13 个 revision 至 `0044`；关键事实计数不变、约束/索引
  有效。迁移进程约 3 秒、整体命令约 7 秒；无并发写入，不能据此保证实际停机时长。
  这是原库变更前的预演阶段；随后已按确认的命令迁移。旧应用镜像与 Compose 服务名差异仍是流量切换门禁。完整证据见
  [`business-db-migration-preflight-2026-09-25.md`](business-db-migration-preflight-2026-09-25.md)。

- `2026-09-25`：用户确认后，重新核对原库 `0031`、0 个其他会话及 0 个 processing 投影；
  新备份经独立恢复校验后，固定镜像执行 13 个 revision 至真实 `0044`。96 张表，未验证约束和
  无效索引均为 0；Reviewed Example 67、Review Task 13、Correction Event 4、对象 31、
  Document 31、Extraction Run 31，均与迁移前一致；四组租户关联异常计数均为 0。
  独立 Compose project 的 API/Index Worker、PostgreSQL、Milvus 均健康，API password file
  临时表写入后回滚，数据库/API/Worker 重启后恢复健康。旧项目容器和 Alias 未切换；新环境无
  合格投影队列，不能据此声称生产 claim/lease/fencing 与完整性已在长期容器重演。

- `2026-09-25`：隔离故障恢复脚本修复 Checkpointer 漏备、固定服务/卷名、二进制 dump
  管道和目标覆盖风险。新归档在独立 project 恢复业务库、4 表 1 条合成记录的 PostgreSQL
  Checkpointer 与 2 个 MinIO 合成对象；Document 7、CorrectionEvent 2、ReviewedExample 5，
  其中 4 条获准。恢复环境就绪 65.87 秒，观察到丢失 0 条；新 Milvus Collection 清单为
  Reviewed Example 4/4、Field Semantic 19/19。删除下一版本条目后，新 Alias 激活被阻止，
  合成旧 Alias 保留；原环境未触碰。可选 Keycloak 未启用，证据与风险见
  [`backup-restore-acceptance-2026-09-25.md`](backup-restore-acceptance-2026-09-25.md)。

## 当前不得对外宣称

- Docker Compose 已达到生产就绪。
- 真实 Suite 变体评估已在隔离环境执行；默认 Worker 仍只自动运行诊断型 Stub 聚合。
- 模型已自动晋升，或 Candidate 注册状态切换已实际修改部署流量/模型权重。
- 交易分析规则已达到生产裁决能力，或历史客户端构造的候选已完成可信迁移。
- 索引 Worker 已通过生产 PostgreSQL/Milvus 或生产规模与灾难恢复验收。
- 已接入真实财务系统、真实远程训练平台或生产流量控制面。
