# Invoice Intelligence 后续目标功能执行提示词

将以下内容直接发送给 Codex，用于继续完成本项目的下一个生产验收模块。

---

你正在 `G:\work\ai` 继续维护 Invoice Intelligence。请以当前代码、migration、Compose
配置和 `docs/project-status.md` 为唯一事实来源，完成**一个尚未完成的目标模块**，并交付
可复核的代码、验证记录和状态更新。

## 目标模块选择

按以下顺序选择第一个仍未完成的模块；如果模块已完成，只核实事实并进入下一个：

1. **P0 隔离 Compose 与安全前置**
   - 使用真实隔离 Compose project 验证最新 Alembic head。
   - 验证 API、migration、Evaluation、Scheduler、Memory、Index、Training 和 Storage
     Worker 从同一个 password file 解析同一业务 DSN。
   - 验证最小权限、OIDC trusted tenant/reviewer、跨租户 404、TLS、S3 private bucket、
     checksum、短时 URL、对象迁移和生命周期。
   - 发现缺口时只补 fail-closed 逻辑、配置或验收脚本，不改变业务 Schema。

2. **P1 真实离线评估闭环**
   - 提供或接入独立、只读、隔离的 Suite Runner，覆盖 `case_rag` 和
     `trusted_memory_field_binding` 的全部既定变体。
   - 仅读取冻结 Dataset 的文档/模板清单、证据引用和版本绑定；不得发送 Ground Truth、
     Reviewer、完整发票值、图片、完整 Prompt 或向量。
   - 验证 HTTPS、Bearer token、请求/隔离清单 SHA-256、超时、有限重试、响应大小限制、
     版本绑定、数据集互斥和数据泄漏 fail-closed。
   - `SnapshotCase`、`deterministic_stub`、`diagnostic_only` 只能生成诊断报告，不能进入
     Promotion Evidence 或生产识别、审核、索引事实。

3. **P1 双 Worker 与 Milvus 完整性**
   - 用两个独立 Worker 连接同一隔离 PostgreSQL，演练 claim、续租、过期重领、worker
     fencing、迟到写入拒绝、有限重试和幂等。
   - 验证冲突关闭后的重评估只重新排队明确由该冲突隔离的 memory admission，不自动批准
     案例或别名。
   - 从 PostgreSQL 合格源集合生成两类投影清单，逐项核验 tenant、版本、ID、checksum、
     数量、重复和过期项。
   - 真实 Milvus 失配时阻止 Alias 切换并保留旧 Alias；Milvus 不得成为业务事实源。

4. **P2 备份恢复与运行治理**
   - 按 `docs/backup-restore-drill.md` 执行 PostgreSQL、对象存储和独立 Checkpointer
     恢复；从 PostgreSQL 重建 Milvus 后再显式激活 Alias。
   - 验证恢复失败时旧 Alias、旧对象引用和审核事实仍保留。
   - 将低基数 metrics 接入隔离告警接收端，注入 Worker、Provider、lease、projection、
     storage 和 audit failure，保存脱敏告警证据。

5. **P2 受限外部集成与前端事实展示**
   - 保持单一 Training Worker；Stub、429/5xx、400、凭据错误和无效产物使用既定失败语义。
   - 保持 Promotion 只切换 PostgreSQL 注册事实，不描述为流量部署或模型权重发布。
   - 保持 Transaction 规则和 Posting 为 mock/受限可用；历史不可信候选只能由授权 Reviewer
     审计升级为 `escalated`。
   - 检查前端按后端事实展示 evaluation type、integrity、registration status 和
     unavailable reason；不得将 `/ready`、`indexed` 或 Stub 报告解释为生产证明。

## 必须先读取

```text
AGENTS.md
README.md
INVOICE_INTELLIGENCE_SOLUTION.md
docs/architecture.md
docs/invoice-schema.md
docs/project-status.md
pyproject.toml
```

随后只读取本模块涉及的源码、Application Port、Repository、migration、Compose、配置、
API 契约和已有定向测试。先用 `rg` 定位实现与状态，再决定是否需要修改。

## 不可变约束

- 不修改固定 19 字段 `InvoiceExtraction`。
- 保留现有单一确定性 Workflow、节点顺序和路由语义。
- PostgreSQL 是审核、评估、训练注册和投影状态的唯一事实源；Milvus 是可重建派生索引。
- 不新增数据库、消息队列、Agent Graph、外部项目源码或新的后台执行入口。
- API Router 只能调用 Application Service；依赖统一由 `bootstrap.py` 组装。
- `tenant_id`、Reviewer、Trace、版本和权限必须来自可信后端上下文；跨租户资源统一 404。
- 使用 `apply_patch` 做编辑；不提交 Git commit，不执行破坏性清理，不连接生产服务。
- 日志、报告、错误和验收证据不得包含完整发票值、图片、Base64、完整 Prompt、向量、
  凭据、Idempotency-Key 原文或远程响应体。

## 执行规则

1. 先列出本模块的事实缺口、拟修改文件、验证命令和预期证据。
2. 只实现当前缺口；已有实现不要重复重构。
3. 若缺 Docker、隔离账号、真实 PostgreSQL、Milvus、S3、OIDC、Runner 或告警端：
   - 继续完成可在本地完成的 fail-closed 代码、Mock/SQLite/HTTP transport 验证和 runbook；
   - 明确记录“未执行的真实验收”，不得用 Compose 解析、`/ready`、`indexed` 或 Mock
     结果替代真实验收。
4. 数据库、租约、幂等、CAS、租户隔离、晋升门禁或报告完整性发生变化时，只运行相关定向
   测试、编译、Ruff/mypy 和文档检查；不要运行无关的全量测试。
5. 每个代码模块完成后同步：
   - `README.md`
   - `docs/architecture.md`
   - `docs/project-status.md`
   
   状态必须分别写清：
   - 已实现
   - 隔离环境已验收
   - 生产已启用
   - 受限可用/阻塞原因
6. 不得把“代码存在”写成“生产已验收”，不得把“注册状态切换”写成“模型部署”。

## 最终输出格式

用简体中文输出以下内容：

1. 本次选择的目标模块及选择依据；
2. 修改文件、关键入口和未修改但核实过的文件；
3. 数据流、依赖方向和 fail-closed 边界；
4. 已执行的定向验证、结果和未执行的真实环境验证；
5. `已实现 / 隔离环境已验收 / 生产已启用 / 受限可用` 状态矩阵；
6. 剩余阻塞、所需外部环境和下一步唯一建议。

如果本模块没有发现代码缺口，不要制造改动；只补充准确的验收记录或 runbook，并说明
为什么可以进入下一个模块。
