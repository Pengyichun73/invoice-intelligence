import { DEFAULT_REQUEST_TIMEOUT_MS, get, write } from './client.js'

const memory = '/api/v1/memory'
const semantics = '/api/v1/field-semantics'
const evaluations = '/api/v1/evaluations'
const training = '/api/v1/training/jobs'
const promotion = '/api/v1/model-promotion'
const transactions = '/api/v1/transactions'

export const governanceApi = {
  admissions: (params, options = {}) => get(`${memory}/admissions`, params, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ...options,
  }),
  admission: (id, options = {}) => get(`${memory}/admissions/${encodeURIComponent(id)}`, undefined, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ...options,
  }),
  decideAdmission: (id, action, reason, expectedRevision) => write(
    `${memory}/admissions/${encodeURIComponent(id)}/${action}`,
    { body: { reason, expected_revision: expectedRevision } },
  ),
  decideAdmissions: (action, reason, items) => write(
    `${memory}/admissions/batch/${action}`,
    { body: { reason, items } },
  ),
  examples: (params, options = {}) => get(`${memory}/examples`, params, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ...options,
  }),
  example: (id, options = {}) => get(`${memory}/examples/${encodeURIComponent(id)}`, undefined, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ...options,
  }),
  exampleProjections: (id, params, options = {}) => get(
    `${memory}/examples/${encodeURIComponent(id)}/projections`,
    params,
    { timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS, ...options },
  ),
  disableExample: (id, reason) => write(`${memory}/examples/${encodeURIComponent(id)}/disable`, { body: { reason } }),
  fieldSemantics: (params, options = {}) => get(semantics, params, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ...options,
  }),
  conflicts: (params, options = {}) => get(`${semantics}/conflicts`, params, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ...options,
  }),
  decideConflict: (id, action, body) => write(
    `${semantics}/conflicts/${encodeURIComponent(id)}/${action}`,
    { body },
  ),
  decideAlias: (id, action, reason, expectedRevision) => write(
    `${semantics}/aliases/${encodeURIComponent(id)}/${action}`,
    { body: { reason, expected_revision: expectedRevision } },
  ),
  rebuildIndex: (body) => write(`${memory}/indexes/rebuild`, { body }),
  index: (version) => get(
    `${memory}/indexes/${encodeURIComponent(version)}`,
    undefined,
    { timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS },
  ),
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
    undefined,
    { timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS },
  ),
  projectFieldSemanticIndex: (version, limit = null) => write(
    `${semantics}/indexes/${encodeURIComponent(version)}/project`,
    { body: { limit } },
  ),
  activateFieldSemanticIndex: (version) => write(
    `${semantics}/indexes/${encodeURIComponent(version)}/activate`,
    { body: {} },
  ),
  ocrMetrics: (options = {}) => get(`${memory}/ocr-metrics`, undefined, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ...options,
  }),
  evaluation: (id, options = {}) => get(`${memory}/evaluations/${encodeURIComponent(id)}`, undefined, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ...options,
  }),
  effectivenessOverview: (params, options = {}) => get(`${memory}/effectiveness/overview`, params, options),
  effectivenessStages: (params, options = {}) => get(`${memory}/effectiveness/stages`, params, options),
  effectivenessScenarios: (params, options = {}) => get(`${memory}/effectiveness/scenarios`, params, options),
  effectivenessRun: (id, options = {}) => get(`${memory}/effectiveness/runs/${encodeURIComponent(id)}`, undefined, options),
  goldCase: (documentId) => get(`${memory}/gold/${encodeURIComponent(documentId)}`),
  submitGoldAnnotation: (documentId, body) => write(`${memory}/gold/${encodeURIComponent(documentId)}/annotations`, { body }),
  createMemoryBenefitJob: (documentIds) => write(`${evaluations}/memory-benefit-jobs`, { body: { document_ids: documentIds } }),
  audits: (params, options = {}) => get(`${memory}/audits`, params, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ...options,
  }),
  evaluationJobs: (params, options = {}) => get(`${evaluations}/jobs`, params, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ...options,
  }),
  evaluationJob: (id, options = {}) => get(`${evaluations}/jobs/${encodeURIComponent(id)}`, undefined, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ...options,
  }),
  trainingJob: (id, options = {}) => get(`${training}/${encodeURIComponent(id)}`, undefined, {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    ...options,
  }),
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
