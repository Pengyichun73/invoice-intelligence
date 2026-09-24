# Human Review Task Service Implementation Plan

> **For agentic workers:** Execute inline in this task; repository instructions prohibit unrequested commits and multi-agent delegation is disabled.

**Goal:** Add a tenant-isolated, lease-based, idempotent human review task service around the existing LangGraph review flow.

**Architecture:** Extend the existing PostgreSQL `review_tasks` aggregate instead of creating a parallel source of truth. API routes call a new Application Service; only the existing `ExtractionWorkflowService` invokes LangGraph. Review facts remain transactionally separate from final result and memory projection.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2, PostgreSQL, Alembic, LangGraph.

---

### Task 1: Domain and Port

**Files:**
- Create: `src/invoice_intelligence/domain/review_tasks.py`
- Create: `src/invoice_intelligence/application/ports/review_tasks.py`
- Modify: `src/invoice_intelligence/application/ports/business_persistence.py`

- [ ] Define statuses, priority, task, lease, page, filter, and audit value objects.
- [ ] Define tenant-scoped list/get and CAS mutation repository contracts.
- [ ] Keep legacy review read records compatible.

### Task 2: PostgreSQL Schema and Repository

**Files:**
- Create: `migrations/versions/20260923_0024_human_review_tasks.py`
- Modify: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_models.py`
- Create: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_review_tasks.py`
- Modify: `src/invoice_intelligence/infrastructure/persistence/sqlalchemy_repository.py`

- [ ] Add tenant, assignment, priority, revision, lease, submission, cancellation, and audit columns/tables.
- [ ] Backfill legacy task status and tenant scope.
- [ ] Implement PostgreSQL `UPDATE ... WHERE revision/status/tenant` CAS and claim semantics.
- [ ] Persist transition audit in the same transaction.
- [ ] Make legacy task creation/reset populate the new lifecycle fields.

### Task 3: Application Service and Workflow Integration

**Files:**
- Create: `src/invoice_intelligence/application/services/review_tasks.py`
- Modify: `src/invoice_intelligence/application/services/extraction_workflow.py`
- Modify: `src/invoice_intelligence/workflow/nodes.py`

- [ ] Implement list/detail/claim/release/reassign/expire/cancel use cases.
- [ ] Require trusted actor and validate lease ownership.
- [ ] Delegate submit to `ExtractionWorkflowService` with mandatory idempotency.
- [ ] Mark submitted only after review facts are durable; preserve retryable workflow state.
- [ ] Keep memory recovery independent from workflow completion.

### Task 4: API, RBAC, and Composition

**Files:**
- Create: `src/invoice_intelligence/api/schemas/reviews.py`
- Modify: `src/invoice_intelligence/api/routes/reviews.py`
- Modify: `src/invoice_intelligence/api/dependencies.py`
- Modify: `src/invoice_intelligence/api/security.py`
- Modify: `src/invoice_intelligence/bootstrap.py`
- Modify: `src/invoice_intelligence/main.py`

- [ ] Add list/detail and mutation schemas with `extra=forbid`.
- [ ] Require `Idempotency-Key` on submit and state-changing routes.
- [ ] Apply `review:read` and `review:submit` to all new routes.
- [ ] Preserve cross-tenant 404 behavior and trusted reviewer identity.
- [ ] Wire the service only in `bootstrap.py`/application lifespan.

### Task 5: Documentation and Verification

**Files:**
- Create: `docs/human-review-api.md`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Create: `tests/test_review_task_service.py`
- Modify: `tests/test_auth_security.py`

- [ ] Document lifecycle, lease ownership, pagination, idempotency, errors, and recovery.
- [ ] Verify `InvoiceExtraction` schema is byte-for-byte unchanged.
- [ ] Run targeted Domain, repository, API/RBAC, and workflow review tests.
- [ ] Run Ruff and Mypy on changed backend modules.
