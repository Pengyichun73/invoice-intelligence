# Human Review Task Service Design

## Scope

Add a production-oriented review-task aggregate around the existing deterministic
LangGraph interrupt/resume flow. `InvoiceExtraction`, workflow node names, and routing
remain unchanged.

## Architecture

- `domain.review_tasks` owns task status, priority, lease, revision, assignment, and
  transition invariants without FastAPI, SQLAlchemy, or LangGraph dependencies.
- `application.ports.review_tasks` defines tenant-scoped query, atomic claim/release/
  reassign, review-fact submission, and workflow-resume recovery boundaries.
- `ReviewTaskService` is the only API-facing use case. It derives the reviewer from
  `TrustedTenantContext`, applies RBAC at the HTTP trust boundary, and never accepts a
  client-supplied tenant or reviewer identity.
- The SQLAlchemy adapter stores task state and immutable audit facts in PostgreSQL.
- The existing `ExtractionWorkflowService` remains the only component allowed to call
  `WorkflowExecutionGateway`. Review submission delegates resume through that service.

## Lifecycle

Allowed states are `pending_review`, `claimed`, `submitted`, `expired`, and `cancelled`.
Claim is an atomic PostgreSQL update with a lease token and expiry. An expired claim is
made `expired` by recovery, then returned to `claimed` when reclaimed. Release returns a
claimed task to `pending_review`; reassign clears the old lease and creates a directed
`pending_review` assignment using revision/CAS. Only the target reviewer can claim that
assignment and receive a new lease token. Submitted and cancelled tasks are terminal.

Every mutation checks `tenant_id`, task identity, expected revision, current state, and
lease ownership. A mismatch is reported as 404 for tenant/resource mismatch and 409 for
stale revision or illegal transition.

## Submission And Recovery

`Idempotency-Key` is mandatory for submission. Its operation scope binds the tenant and
task, while its stored fingerprint covers the normalized correction request without
logging the key or values. During the existing Workflow resume, the authoritative review-
fact transaction writes the human correction, stable correction events, and recovery
record. The following task transition marks the task submitted and appends an audit event.
Stable event IDs and the idempotency binding prevent duplicate facts across retries.

Result persistence and memory admission remain separate from the review-fact transaction.
If resume fails after the facts commit, the stable facts are reused on retry and are not
duplicated. A submitted review must not be changed back to pending or mark an already
completed workflow failed. Memory admission continues through the existing
`memory_review_recoveries` mechanism.

## API

- `GET /reviews` supports reviewer, priority, status, creation-time, and cursor pagination.
- `GET /reviews/{review_id}` returns one tenant-scoped task and bounded review payload.
- `POST /reviews/{review_id}/claim`
- `POST /reviews/{review_id}/release`
- `POST /reviews/{review_id}/reassign`
- `POST /reviews/{review_id}/submit`
- `POST /reviews/recover-expired` is an administrative recovery operation.

Read operations require `review:read`; mutations require `review:submit`. Review submission
requires `Idempotency-Key`; reviewer identity always comes from the trusted context. Legacy
`POST /reviews/{run_id}` remains compatible during migration and delegates to the new
service.

## Data And Privacy

The task stores bounded review request JSON and current invoice payload already required
by the existing review UI. It never stores images, Base64, complete remote responses,
Chain-of-Thought, Authorization headers, or raw idempotency keys. Audit records contain
technical identifiers, action, actor, revision, trace, and safe reason codes.

## Verification

Targeted tests cover transition invariants, atomic claim contention, lease expiry and
recovery, tenant isolation, trusted reviewer enforcement, required idempotency, replay
without duplicate `CorrectionEvent`, RBAC mapping, and successful/failed workflow resume.
