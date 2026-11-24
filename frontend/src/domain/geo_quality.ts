/** 轨迹质量工作流的前端数据层：多批重算、巡检单纠偏、异常段提交与命中出处。 */
import { request } from '@/api/client'

export type RecomputeBatch = {
  batch_key: string
  status: string
  sampling_date: string
  previous_ticket_id: string | null
  hit_ids: string[]
  hits_count: number
  pending_backfill_ranges: RangeItem[]
  failed_ranges: RangeItem[]
  error: string | null
  disposition_confirmed: boolean
  confirmed_correction_id: string | null
  misjudged_hit_ids: string[]
}

export type RangeItem = {
  start_index: number
  end_index: number
  start_point_id: number
  end_point_id: number
  reason: string
}

export type RecomputeRound = {
  ok: boolean
  round_key: string
  request_key: string | null
  status: string
  fence_id: number
  created_at: number
  batch_keys: string[]
  batches: RecomputeBatch[]
  pending_backfill_ranges: (RangeItem & { batch_key: string })[]
  failed_batches: { batch_key: string; error: string }[]
  missing_evidence_batches: string[]
  corrections: string[]
  duplicate?: boolean
  message?: string
}

export type CorrectionItem = {
  batch_key: string
  ok: boolean
  error?: string | null
  segments: {
    segment_id: string
    point_ids: string[]
    hit_ids: string[]
    usable_point_ids: string[]
  }[]
  hits_to_withdraw: string[]
  usable_point_ids: string[]
  previous_ticket_id?: string | null
}

export type Correction = {
  ok: boolean
  correction_id: string
  round_key: string
  status: string
  duplicate: boolean
  items: CorrectionItem[]
  failed_batches: { batch_key: string; error: string }[]
  signed_versions: { version: number; signer: string; note: string }[]
  withdrawals: { withdrawal_id: string; ticket_id: string; recipient: string }[]
  settled_tickets: string[]
  refused_settlements: { batch_key: string; ticket_id: string; error: string }[]
  message?: string
  withdrawals_sent?: number
  per_batch?: {
    batch_key: string
    ok: boolean
    error?: string | null
    withdrawn_hits: string[]
    hits_kept_by_review: string[]
    ticket: { ticket_id: string; action: string; message?: string } | null
  }[]
}

export type InspectionTicket = {
  ticket_id: string
  batch_key: string
  recipient: string
  status: string
  version: number
  source: string
  withdrawals: { withdrawal_id: string; recipient: string; correction_id: string }[]
  settled_by: string | null
  settled_version: number | null
}

export type Review = {
  review_id: string
  batch_key: string
  point_id: number | string
  hit_id: string
  verdict: string
  kind: string
  approved: boolean
  applied: boolean
  error: string | null
  late_point: Record<string, unknown> | null
}

export type HitProvenance = {
  ok: boolean
  batch_key: string
  point_id: string | null
  current_hit: {
    hit_id: string
    point_id: number | string
    sampling_date: string
    previous_ticket_id: string | null
    status: string
    eligible: boolean
    current_hit_owner: string
  } | null
  current_hit_eligible: boolean
  valid_source: Record<string, unknown> | null
  provenance: Record<string, unknown>[]
  ticket_dispositions: {
    ticket_id: string
    status: string
    recipient: string
    withdrawals: { withdrawal_id: string; recipient: string }[]
    settled_by: string | null
    settled_version: number | null
    version: number
  }[]
  hits: Record<string, unknown>[]
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.detail ?? '操作未成立')
  }
  return payload as T
}

async function get<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error('数据读取失败')
  return (await response.json()) as T
}

export function recomputeBatches(
  batchKeys: string[],
  options: { fenceId?: number; dates?: Record<string, string>; requestId?: string } = {},
): Promise<RecomputeRound> {
  return post<RecomputeRound>('/api/geo_quality/recompute', {
    batch_keys: batchKeys,
    fence_id: options.fenceId ?? 1,
    batches: batchKeys.map((key) => ({
      batch_key: key,
      sampling_date: options.dates?.[key] || null,
    })),
    request_id: options.requestId ?? null,
  })
}

export function listRounds(): Promise<{ items: RecomputeRound[]; total: number }> {
  return get('/api/geo_quality/recompute')
}

export function getRound(roundKey: string): Promise<RecomputeRound> {
  return get(`/api/geo_quality/recompute/${roundKey}`)
}

export function interruptRound(roundKey: string, batchKey: string, reason = ''): Promise<unknown> {
  return post(`/api/geo_quality/recompute/${roundKey}/interrupt`, { batch_key: batchKey, reason })
}

export function resumeRound(roundKey: string): Promise<{ round: RecomputeRound; resumed: unknown[]; skipped: unknown[]; message: string }> {
  return post(`/api/geo_quality/recompute/${roundKey}/resume`, {})
}

export function dispatchInspection(
  roundKey: string,
  batchKey: string,
  recipient = '巡检一组',
  requestId?: string,
): Promise<{ ok: boolean; duplicate: boolean; message: string; ticket: InspectionTicket }> {
  return post(`/api/geo_quality/recompute/${roundKey}/dispatch`, {
    batch_key: batchKey,
    recipient,
    request_id: requestId ?? null,
  })
}

export function submitCorrection(
  roundKey: string,
  items: { batch_key: string; segments: Record<string, unknown>[] }[],
  idempotencyKey?: string,
): Promise<Correction> {
  return post<Correction>('/api/geo_quality/corrections', {
    round_key: roundKey,
    items,
    idempotency_key: idempotencyKey ?? null,
  })
}

export function listCorrections(roundKey?: string): Promise<{ items: Correction[]; total: number }> {
  const query = roundKey ? `?round_key=${encodeURIComponent(roundKey)}` : ''
  return get(`/api/geo_quality/corrections${query}`)
}

export function signCorrection(correctionId: string, version: number, note = ''): Promise<{ ok: boolean; message: string; correction: Correction }> {
  return post(`/api/geo_quality/corrections/${correctionId}/sign`, { version, note })
}

export function dispatchCorrection(
  correctionId: string,
  mode: 'withdraw' | 'settle',
  version?: number,
): Promise<Correction> {
  return post<Correction>(`/api/geo_quality/corrections/${correctionId}/dispatch`, {
    mode,
    version: version ?? null,
  })
}

export function listTickets(batchKey?: string): Promise<{ items: InspectionTicket[]; total: number }> {
  const query = batchKey ? `?batch_key=${encodeURIComponent(batchKey)}` : ''
  return get(`/api/geo_quality/tickets${query}`)
}

export function withdrawTicket(ticketId: string, authorizationId = ''): Promise<{ ok: boolean; message: string; withdrawals_sent: number }> {
  return post(`/api/geo_quality/tickets/${ticketId}/withdraw`, {
    authorization_id: authorizationId || null,
  })
}

export function settleTicket(ticketId: string, correctionId: string, version: number): Promise<{ ok: boolean; message: string }> {
  return post(`/api/geo_quality/tickets/${ticketId}/settle`, {
    correction_id: correctionId,
    version,
  })
}

export function signReview(
  batchKey: string,
  pointId: number | string,
  verdict: string,
  latePoint?: Record<string, unknown> | null,
  approved = false,
): Promise<{ ok: boolean; message: string; review: Review }> {
  return post('/api/geo_quality/reviews', {
    batch_key: batchKey,
    point_id: pointId,
    verdict,
    approved,
    late_point: latePoint ?? null,
  })
}

export function listReviews(batchKey?: string): Promise<{ items: Review[]; total: number }> {
  const query = batchKey ? `?batch_key=${encodeURIComponent(batchKey)}` : ''
  return get(`/api/geo_quality/reviews${query}`)
}

export function getProvenance(batchKey: string, pointId?: number | string): Promise<HitProvenance> {
  const query = pointId !== undefined ? `?point_id=${encodeURIComponent(String(pointId))}` : ''
  return get(`/api/geo_quality/provenance/${encodeURIComponent(batchKey)}${query}`)
}
