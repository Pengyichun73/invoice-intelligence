import { get, write } from './client.js'

const EXTRACTION_REQUEST_TIMEOUT_MS = 3 * 60 * 1000

export const invoiceApi = {
  health: (options = {}) => get('/api/v1/health', undefined, options),
  upload: (file) => {
    const body = new FormData()
    body.append('file', file)
    return write('/api/v1/documents', { body, timeoutMs: EXTRACTION_REQUEST_TIMEOUT_MS })
  },
  extract: (documentId) => write(
    `/api/v1/documents/${encodeURIComponent(documentId)}/extract`,
    { timeoutMs: EXTRACTION_REQUEST_TIMEOUT_MS },
  ),
  run: (runId) => get(`/api/v1/runs/${encodeURIComponent(runId)}`),
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
