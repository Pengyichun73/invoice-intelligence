import { get, write } from './client'

export const invoiceApi = {
  health: () => get('/api/v1/health'),
  upload: (file) => {
    const body = new FormData()
    body.append('file', file)
    return write('/api/v1/documents', { body })
  },
  extract: (documentId) => write(`/api/v1/documents/${encodeURIComponent(documentId)}/extract`),
  run: (runId) => get(`/api/v1/runs/${encodeURIComponent(runId)}`),
  result: (runId) => get(`/api/v1/runs/${encodeURIComponent(runId)}/result`),
  review: (runId) => get(`/api/v1/reviews/${encodeURIComponent(runId)}`),
  submitReview: (runId, body) => write(`/api/v1/reviews/${encodeURIComponent(runId)}`, { body }),
}
