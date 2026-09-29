import { get, getBlob, write } from './client.js'

const EXTRACTION_REQUEST_TIMEOUT_MS = 3 * 60 * 1000

export const invoiceApi = {
  health: (options = {}) => get('/api/v1/health', undefined, options),
  createBatch: (idempotencyKey) => write('/api/v1/invoice-batches', { idempotencyKey }),
  addBatchFile: (batchId, file, idempotencyKey) => {
    const body = new FormData()
    body.append('file', file)
    return write(`/api/v1/invoice-batches/${encodeURIComponent(batchId)}/files`, {
      body, idempotencyKey, timeoutMs: EXTRACTION_REQUEST_TIMEOUT_MS,
    })
  },
  submitBatch: (batchId, expectedRevision, idempotencyKey) => write(
    `/api/v1/invoice-batches/${encodeURIComponent(batchId)}/submit`,
    { body: { expected_revision: expectedRevision }, idempotencyKey },
  ),
  batch: (batchId) => get(`/api/v1/invoice-batches/${encodeURIComponent(batchId)}`),
  recentBatches: () => get('/api/v1/invoice-batches'),
  confirmBatchFile: (batchId, fileId, expectedRevision, groups) => write(
    `/api/v1/invoice-batches/${encodeURIComponent(batchId)}/files/${encodeURIComponent(fileId)}/confirm`,
    { body: { expected_revision: expectedRevision, groups } },
  ),
  retryBatchItem: (batchId, itemId, expectedRunId) => write(
    `/api/v1/invoice-batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(itemId)}/retry`,
    { body: { expected_run_id: expectedRunId } },
  ),
  batchPage: (batchId, fileId, page) => getBlob(
    `/api/v1/invoice-batches/${encodeURIComponent(batchId)}/files/${encodeURIComponent(fileId)}/pages/${page}`,
  ),
  upload: (file) => {
    const body = new FormData()
    body.append('file', file)
    return write('/api/v1/documents', { body, timeoutMs: EXTRACTION_REQUEST_TIMEOUT_MS })
  },
  extract: (documentId, idempotencyKey) => write(
    `/api/v1/documents/${encodeURIComponent(documentId)}/extract`,
    { timeoutMs: EXTRACTION_REQUEST_TIMEOUT_MS, idempotencyKey },
  ),
  run: (runId) => get(`/api/v1/runs/${encodeURIComponent(runId)}`),
  recentRuns: () => get('/api/v1/runs'),
  result: (runId) => get(`/api/v1/runs/${encodeURIComponent(runId)}/result`),
  review: (identifier) => get(`/api/v1/reviews/${encodeURIComponent(identifier)}`),
  claimReview: (identifier, expectedRevision, leaseSeconds) => write(
    `/api/v1/reviews/${encodeURIComponent(identifier)}/claim`,
    { body: { expected_revision: expectedRevision, ...(leaseSeconds ? { lease_seconds: leaseSeconds } : {}) } },
  ),
  submitClaimedReview: (identifier, body) => write(
    `/api/v1/reviews/${encodeURIComponent(identifier)}/submit`,
    { body, timeoutMs: EXTRACTION_REQUEST_TIMEOUT_MS },
  ),
}
