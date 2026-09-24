import { get, write } from './client'

const memory = '/api/v1/memory'
const semantics = '/api/v1/field-semantics'
const evaluations = '/api/v1/evaluations'
const training = '/api/v1/training/jobs'
const promotion = '/api/v1/model-promotion'
const transactions = '/api/v1/transactions'

export const governanceApi = {
  admissions: (params) => get(`${memory}/admissions`, params),
  admission: (id) => get(`${memory}/admissions/${encodeURIComponent(id)}`),
  decideAdmission: (id, action, reason, expectedRevision) => write(
    `${memory}/admissions/${encodeURIComponent(id)}/${action}`,
    { body: { reason, expected_revision: expectedRevision } },
  ),
  decideAdmissions: (action, reason, items) => write(
    `${memory}/admissions/batch/${action}`,
    { body: { reason, items } },
  ),
  examples: (params) => get(`${memory}/examples`, params),
  example: (id) => get(`${memory}/examples/${encodeURIComponent(id)}`),
  exampleProjections: (id, params) => get(
    `${memory}/examples/${encodeURIComponent(id)}/projections`,
    params,
  ),
  disableExample: (id, reason) => write(`${memory}/examples/${encodeURIComponent(id)}/disable`, { body: { reason } }),
  fieldSemantics: (params) => get(semantics, params),
  conflicts: (params) => get(`${semantics}/conflicts`, params),
  decideConflict: (id, action, body) => write(
    `${semantics}/conflicts/${encodeURIComponent(id)}/${action}`,
    { body },
  ),
  decideAlias: (id, action, reason, expectedRevision) => write(
    `${semantics}/aliases/${encodeURIComponent(id)}/${action}`,
    { body: { reason, expected_revision: expectedRevision } },
  ),
  rebuildIndex: (body) => write(`${memory}/indexes/rebuild`, { body }),
  index: (version) => get(`${memory}/indexes/${encodeURIComponent(version)}`),
  projectIndex: (version, limit = null) => write(
    `${memory}/indexes/${encodeURIComponent(version)}/project`,
    { body: { limit } },
  ),
  activateIndex: (version) => write(
    `${memory}/indexes/${encodeURIComponent(version)}/activate`,
    { body: {} },
  ),
  rebuildFieldSemanticIndex: (body) => write(`${semantics}/indexes/rebuild`, { body }),
  fieldSemanticIndex: (version) => get(
    `${semantics}/indexes/${encodeURIComponent(version)}`,
  ),
  projectFieldSemanticIndex: (version, limit = null) => write(
    `${semantics}/indexes/${encodeURIComponent(version)}/project`,
    { body: { limit } },
  ),
  activateFieldSemanticIndex: (version) => write(
    `${semantics}/indexes/${encodeURIComponent(version)}/activate`,
    { body: {} },
  ),
  ocrMetrics: () => get(`${memory}/ocr-metrics`),
  evaluation: (id) => get(`${memory}/evaluations/${encodeURIComponent(id)}`),
  audits: (params) => get(`${memory}/audits`, params),
  evaluationJobs: (params) => get(`${evaluations}/jobs`, params),
  evaluationJob: (id) => get(`${evaluations}/jobs/${encodeURIComponent(id)}`),
  trainingJob: (id) => get(`${training}/${encodeURIComponent(id)}`),
  cancelTrainingJob: (id, reason) => write(
    `${training}/${encodeURIComponent(id)}/cancel`,
    { body: { reason } },
  ),
  retryTrainingJob: (id) => write(`${training}/${encodeURIComponent(id)}/retry`, { body: {} }),
  createPromotionCandidate: (body) => write(promotion, { body }),
  approvePromotionCandidate: (id, body) => write(
    `${promotion}/${encodeURIComponent(id)}/approve`,
    { body },
  ),
  rejectPromotionCandidate: (id, body) => write(
    `${promotion}/${encodeURIComponent(id)}/reject`,
    { body },
  ),
  rollbackPromotionCandidate: (id, body) => write(
    `${promotion}/${encodeURIComponent(id)}/rollback`,
    { body },
  ),
  analyzeTransaction: (runId) => write(
    `${transactions}/candidates/analyze`,
    { body: { run_id: runId } },
  ),
  reviewTransaction: (id, body) => write(
    `${transactions}/candidates/${encodeURIComponent(id)}/review`,
    { body },
  ),
}
