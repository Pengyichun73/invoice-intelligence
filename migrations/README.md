# Migrations

该目录只管理 SQLAlchemy 业务表，不包含任何 LangGraph Checkpoint 表。

```powershell
alembic upgrade head
```

数据库由 `INVOICE_INTELLIGENCE_BUSINESS_DATABASE_URL` 指定。容器中 URL 不带密码，并设置
`INVOICE_INTELLIGENCE_BUSINESS_DATABASE_PASSWORD_FILE` 指向只读 Docker secret；Alembic 与 API、
Worker 使用同一个 Settings 解析器，不向 Alembic Config 写入带密码 URL。默认及生产环境使用
`postgresql+psycopg://...`；`20260901_0002` 会创建 `vector` extension、纠错向量表和 HNSW
cosine 索引。执行 migration 的 PostgreSQL 角色需要 extension 权限。开发环境可改用 SQLite，
但必须设置 `INVOICE_INTELLIGENCE_CORRECTION_MEMORY_BACKEND=disabled`，此时只保留原始纠错
事件，不创建向量表。Checkpoint 继续由独立 SQLite 文件或 PostgreSQL DSN 管理。
