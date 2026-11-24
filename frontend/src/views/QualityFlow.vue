<template>
  <section class="quality-flow" data-testid="quality-flow">
    <header class="flow-head">
      <div>
        <h3>围栏重算与巡检纠偏</h3>
        <p class="page-desc">
          多选批次发起围栏重算，按已确认异常段一次提交；待补点与处理失败分开保留，旧单可撤回或以新版纠正记录结清。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="flow-refresh" type="button" @click="refreshAll">刷新</button>
        <button class="btn" data-testid="flow-resume" type="button" :disabled="!activeRound" @click="resume">
          续做中断批次
        </button>
        <button class="btn primary" data-testid="flow-recompute" type="button" :disabled="!selected.length || busy" @click="recompute">
          重算选中批次（{{ selected.length }}）
        </button>
      </div>
    </header>

    <p v-if="flowMessage" class="flow-message" data-testid="flow-message">{{ flowMessage }}</p>
    <p v-if="flowError" class="error-text" data-testid="flow-error">{{ flowError }}</p>

    <div class="flow-grid">
      <section class="flow-panel">
        <h4>批次选择</h4>
        <table class="data-table">
          <thead>
            <tr><th></th><th>批次编号</th><th>原采样日期</th><th>最近巡检单</th><th>重算状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="batch in batches" :key="batch.batch_key" :data-testid="`flow-batch-${batch.batch_key}`">
              <td>
                <input
                  type="checkbox"
                  :data-testid="`flow-select-${batch.batch_key}`"
                  :checked="selected.includes(batch.batch_key)"
                  :disabled="batch.disposition_confirmed"
                  @change="toggleSelection(batch.batch_key)"
                />
              </td>
              <td>{{ batch.batch_key }}</td>
              <td>{{ batch.sampling_date }}</td>
              <td>{{ batch.previous_ticket_id ?? '—' }}</td>
              <td>
                <span :data-status="batch.status">{{ batch.status }}</span>
                <span v-if="batch.disposition_confirmed" class="badge">已确认处置</span>
                <button
                  v-if="batch.status === '围栏已算'"
                  class="link"
                  :data-testid="`flow-dispatch-inspection-${batch.batch_key}`"
                  type="button"
                  @click="dispatchInspectionFor(batch.batch_key)"
                >
                  下派巡检
                </button>
                <button
                  v-if="batch.status === '围栏已算' && !batch.disposition_confirmed"
                  class="link"
                  :data-testid="`flow-interrupt-${batch.batch_key}`"
                  type="button"
                  @click="interrupt(batch.batch_key)"
                >
                  标记中断
                </button>
              </td>
            </tr>
            <tr v-if="!batches.length">
              <td colspan="5" class="empty-state">先在上方列表登记批次，或选择已有批次发起重算</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="flow-panel">
        <h4>异常段（勾选后一次提交）</h4>
        <p class="flow-hint">勾选已确认异常段，提交后逐批列出可撤回命中与仍可用点位。</p>
        <div v-for="batch in selectableBatches" :key="batch.batch_key" class="segment-group">
          <label class="segment-batch">
            <input
              type="checkbox"
              :data-testid="`flow-batch-correction-${batch.batch_key}`"
              :checked="correctionSelection[batch.batch_key]?.length > 0"
              @change="toggleBatchCorrection(batch.batch_key)"
            />
            <strong>{{ batch.batch_key }}</strong>
            <span class="flow-hint">命中 {{ batch.hits_count }} 个</span>
          </label>
          <div v-if="batch.pending_backfill_ranges.length" class="range-list">
            <label
              v-for="(range, index) in batch.pending_backfill_ranges"
              :key="`${batch.batch_key}-pending-${index}`"
              class="range-item"
            >
              <input
                type="checkbox"
                :data-testid="`flow-seg-${batch.batch_key}-${index}`"
                :checked="isSegmentChecked(batch.batch_key, range)"
                @change="toggleSegment(batch.batch_key, range)"
              />
              待补点 {{ range.start_point_id }}–{{ range.end_point_id }}
            </label>
          </div>
          <p v-else class="flow-hint">该批无待补点区间</p>
          <div v-if="batch.failed_ranges.length" class="range-list failed">
            <span class="flow-hint">处理失败 {{ batch.failed_ranges.length }} 段：</span>
            <span v-for="(range, index) in batch.failed_ranges" :key="`f-${index}`">
              {{ range.start_point_id }}–{{ range.end_point_id }}
            </span>
          </div>
        </div>
        <button
          class="btn primary"
          data-testid="flow-submit-correction"
          type="button"
          :disabled="!correctionPayload.length || busy"
          @click="submitSegments"
        >
          提交已确认异常段（{{ correctionPayload.length }} 批）
        </button>
      </section>
    </div>

    <section v-if="latestCorrection" class="flow-panel" data-testid="flow-correction">
      <h4>纠正单 {{ latestCorrection.correction_id }}（{{ latestCorrection.status }}）</h4>
      <p v-if="latestCorrection.duplicate" class="badge">同一批异常段重复提交，只计一次修正</p>
      <table class="data-table">
        <thead>
          <tr><th>批次</th><th>结果</th><th>可撤回命中</th><th>仍可用点</th><th>旧巡检单</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in latestCorrection.items" :key="item.batch_key" :data-testid="`flow-correction-item-${item.batch_key}`">
            <td>{{ item.batch_key }}</td>
            <td>
              <span v-if="item.ok">已列出</span>
              <span v-else class="error-text">失败：{{ item.error }}</span>
            </td>
            <td>{{ item.hits_to_withdraw.join('、') || '—' }}</td>
            <td>{{ item.usable_point_ids.join('、') || '—' }}</td>
            <td>{{ item.previous_ticket_id ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="latestCorrection.failed_batches.length" class="error-text">
        失败批次：<span v-for="item in latestCorrection.failed_batches" :key="item.batch_key">
          {{ item.batch_key }}（{{ item.error }}）
        </span>
      </p>
      <div class="page-actions flow-actions">
        <label class="flow-inline">
          补签版本
          <input v-model.number="signVersion" data-testid="flow-sign-version" type="number" min="2" />
        </label>
        <button class="btn" data-testid="flow-sign" type="button" @click="signLatest">补签新版纠正记录</button>
        <button
          class="btn"
          data-testid="flow-dispatch-withdraw"
          type="button"
          :disabled="latestCorrection.status !== '待发出'"
          @click="dispatchLatest('withdraw')"
        >
          发出并撤回旧单
        </button>
        <button
          class="btn primary"
          data-testid="flow-dispatch-settle"
          type="button"
          :disabled="latestCorrection.status !== '待发出' || !latestCorrection.signed_versions.length"
          @click="dispatchLatest('settle')"
        >
          发出并以新版结清旧单
        </button>
      </div>
      <ul v-if="latestCorrection.signed_versions.length" class="flow-list">
        <li v-for="item in latestCorrection.signed_versions" :key="item.version">
          已签 v{{ item.version }}（{{ item.signer }}）{{ item.note }}
        </li>
      </ul>
      <div v-if="dispatchResult" class="flow-result" data-testid="flow-dispatch-result">
        <p>{{ dispatchResult.message }}</p>
        <ul>
          <li v-for="batch in dispatchResult.per_batch ?? []" :key="batch.batch_key">
            {{ batch.batch_key }}：撤回 {{ batch.withdrawn_hits.length }} 个命中；
            {{ batch.ticket ? batch.ticket.ticket_id + ' ' + (batch.ticket.message ?? '') : '无关联巡检单' }}
            <span v-if="batch.hits_kept_by_review.length">
              （复核恢复保留 {{ batch.hits_kept_by_review.join('、') }}）
            </span>
          </li>
        </ul>
      </div>
    </section>

    <section class="flow-panel">
      <h4>旧巡检单</h4>
      <table class="data-table">
        <thead>
          <tr><th>巡检单</th><th>批次</th><th>接收方</th><th>版本</th><th>状态</th><th>处置</th></tr>
        </thead>
        <tbody>
          <tr v-for="ticket in tickets" :key="ticket.ticket_id" :data-testid="`flow-ticket-${ticket.ticket_id}`">
            <td>{{ ticket.ticket_id }}</td>
            <td>{{ ticket.batch_key }}</td>
            <td>{{ ticket.recipient }}</td>
            <td>v{{ ticket.version }}</td>
            <td>{{ ticket.status }}</td>
            <td class="row-actions">
              <button
                class="link"
                :data-testid="`flow-ticket-withdraw-${ticket.ticket_id}`"
                type="button"
                @click="doWithdrawTicket(ticket.ticket_id)"
              >
                单独撤回
              </button>
              <button
                class="link"
                :data-testid="`flow-ticket-settle-${ticket.ticket_id}`"
                type="button"
                :disabled="!latestCorrection || !latestCorrection.signed_versions.length"
                @click="doSettleTicket(ticket.ticket_id)"
              >
                以新版结清
              </button>
            </td>
          </tr>
          <tr v-if="!tickets.length">
            <td colspan="6" class="empty-state">暂无巡检单</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="flow-panel">
      <h4>复核与现行命中出处</h4>
      <form class="flow-inline" @submit.prevent="submitReview">
        <label>
          批次
          <input v-model="reviewForm.batch_key" data-testid="flow-review-batch" placeholder="批次编号" />
        </label>
        <label>
          点位
          <input v-model="reviewForm.point_id" data-testid="flow-review-point" placeholder="点 ID" />
        </label>
        <label>
          结论
          <select v-model="reviewForm.verdict" data-testid="flow-review-verdict">
            <option value="误判">误判</option>
            <option value="恢复">恢复</option>
          </select>
        </label>
        <label>
          晚报点坐标
          <input v-model="reviewForm.lat" data-testid="flow-review-lat" placeholder="lat" />
        </label>
        <label>
          晚报点经度
          <input v-model="reviewForm.lon" data-testid="flow-review-lon" placeholder="lon" />
        </label>
        <label>
          晚报点时间
          <input v-model="reviewForm.timestamp" data-testid="flow-review-ts" placeholder="timestamp" />
        </label>
        <button class="btn primary" data-testid="flow-review-submit" type="submit">
          签署复核
        </button>
      </form>
      <p class="flow-hint">「误判」进入待撤回清单；「恢复」必须带有效晚报点才能恢复该点现行命中资格。</p>

      <form class="flow-inline" @submit.prevent="loadProvenance">
        <label>
          回查批次
          <input v-model="provenanceQuery.batch_key" data-testid="flow-prov-batch" placeholder="批次编号" />
        </label>
        <label>
          点位（可空）
          <input v-model="provenanceQuery.point_id" data-testid="flow-prov-point" placeholder="点 ID" />
        </label>
        <button class="btn" data-testid="flow-prov-query" type="submit">回查现行命中出处</button>
      </form>

      <div v-if="provenance" class="flow-result" data-testid="flow-provenance">
        <p>
          现行命中：{{ provenance.current_hit?.hit_id ?? '无' }}
          （{{ provenance.current_hit_eligible ? '有效' : '未生效' }}），
          采样日期 {{ provenance.current_hit?.sampling_date ?? '—' }}，
          处置班组 {{ provenance.current_hit?.current_hit_owner ?? '—' }}
        </p>
        <p>有效出处：{{ describeSource(provenance.valid_source) }}</p>
        <ul>
          <li v-for="ticket in provenance.ticket_dispositions" :key="ticket.ticket_id">
            {{ ticket.ticket_id }}：{{ ticket.status }}
            <span v-if="ticket.withdrawals.length">
              ，已向 {{ ticket.withdrawals.map((item) => item.recipient).join('、') }} 送出撤回归还
            </span>
            <span v-if="ticket.settled_by">，由 {{ ticket.settled_by }} v{{ ticket.settled_version }} 结清</span>
          </li>
        </ul>
      </div>
      <ul v-if="reviews.length" class="flow-list" data-testid="flow-reviews">
        <li v-for="review in reviews" :key="review.review_id">
          {{ review.review_id }}：{{ review.batch_key }} 点 {{ review.point_id }} {{ review.verdict }}
          {{ review.approved ? '（已核准）' : '' }}
          <span v-if="review.error" class="error-text">{{ review.error }}</span>
        </li>
      </ul>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import {
  dispatchCorrection,
  dispatchInspection,
  getProvenance,
  interruptRound,
  listCorrections,
  listReviews,
  listRounds,
  listTickets,
  recomputeBatches,
  resumeRound,
  settleTicket,
  signCorrection,
  signReview,
  submitCorrection,
  withdrawTicket,
} from '@/domain/geo_quality'
import type {
  Correction,
  HitProvenance,
  InspectionTicket,
  RangeItem,
  RecomputeBatch,
  RecomputeRound,
  Review,
} from '@/domain/geo_quality'

type RangeKey = string

const batches = ref<RecomputeBatch[]>([])
const rounds = ref<RecomputeRound[]>([])
const tickets = ref<InspectionTicket[]>([])
const reviews = ref<Review[]>([])
const latestCorrection = ref<Correction | null>(null)
const dispatchResult = ref<Correction | null>(null)
const provenance = ref<HitProvenance | null>(null)
const selected = ref<string[]>([])
const correctionSelection = reactive<Record<string, RangeItem[]>>({})
const signVersion = ref(2)
const flowMessage = ref('')
const flowError = ref('')
const busy = ref(false)
const reviewForm = reactive({
  batch_key: '',
  point_id: '',
  verdict: '误判',
  lat: '',
  lon: '',
  timestamp: '',
})
const provenanceQuery = reactive({ batch_key: '', point_id: '' })

const activeRound = computed(() => rounds.value[rounds.value.length - 1] ?? null)
const selectableBatches = computed(() => batches.value.filter((item) => item.status === '围栏已算'))
const correctionPayload = computed(() =>
  Object.entries(correctionSelection)
    .filter(([, ranges]) => ranges.length > 0)
    .map(([batch_key, ranges]) => ({
      batch_key,
      segments: ranges.map((range) => ({
        start_point_id: range.start_point_id,
        end_point_id: range.end_point_id,
      })),
    })),
)

function rangeKey(range: RangeItem): RangeKey {
  return `${range.start_point_id}-${range.end_point_id}`
}

function isSegmentChecked(batchKey: string, range: RangeItem): boolean {
  return (correctionSelection[batchKey] ?? []).some((item) => rangeKey(item) === rangeKey(range))
}

function toggleSelection(batchKey: string) {
  selected.value = selected.value.includes(batchKey)
    ? selected.value.filter((item) => item !== batchKey)
    : [...selected.value, batchKey]
}

function toggleBatchCorrection(batchKey: string) {
  if ((correctionSelection[batchKey] ?? []).length) {
    correctionSelection[batchKey] = []
    return
  }
  const batch = batches.value.find((item) => item.batch_key === batchKey)
  correctionSelection[batchKey] = batch ? [...batch.pending_backfill_ranges] : []
}

function toggleSegment(batchKey: string, range: RangeItem) {
  const current = correctionSelection[batchKey] ?? []
  correctionSelection[batchKey] = current.some((item) => rangeKey(item) === rangeKey(range))
    ? current.filter((item) => rangeKey(item) !== rangeKey(range))
    : [...current, range]
}

function describeSource(source: Record<string, unknown> | null): string {
  if (!source) return '暂无出处'
  const event = String(source.event ?? '')
  const roundKey = String(source.round_key ?? '')
  const date = String(source.sampling_date ?? '')
  if (event === 'restored') {
    return `复核 ${String(source.review_id ?? '')} 用晚报点恢复（原 ${String(source.previous_status ?? '')}）`
  }
  if (event === 'withdrawn') {
    return `已撤回（纠正单 ${String(source.correction_id ?? '')}）`
  }
  return `重算 ${roundKey} 生成，采样日期 ${date}`
}

function fail(error: unknown) {
  flowError.value = error instanceof Error ? error.message : '操作失败'
}

async function refreshAll() {
  flowError.value = ''
  try {
    const [roundPayload, ticketPayload, reviewPayload, correctionPayloadList, batchPayload] = await Promise.all([
      listRounds(),
      listTickets(),
      listReviews(),
      listCorrections(),
      listEntries('geo_source', {}),
    ])
    rounds.value = roundPayload.items
    tickets.value = ticketPayload.items
    reviews.value = reviewPayload.items
    latestCorrection.value = correctionPayloadList.items[correctionPayloadList.items.length - 1] ?? null
    const latest = activeRound.value
    batches.value = latest?.batches ?? []
    for (const batch of batches.value) {
      if (!(batch.batch_key in correctionSelection)) correctionSelection[batch.batch_key] = []
    }
    if (!batches.value.length) {
      batches.value = batchPayload.items
        .filter((row) => row.status === '已接入')
        .map((row) => ({
          batch_key: String(row['批次编号']),
          status: '待重算',
          sampling_date: String(row['时间跨度'] ?? '').slice(0, 10),
          previous_ticket_id: null,
          hit_ids: [],
          hits_count: 0,
          pending_backfill_ranges: [],
          failed_ranges: [],
          error: null,
          disposition_confirmed: false,
          confirmed_correction_id: null,
          misjudged_hit_ids: [],
        }))
    }
  } catch (error) {
    fail(error)
  }
}

async function recompute() {
  busy.value = true
  flowError.value = ''
  flowMessage.value = ''
  try {
    const result = await recomputeBatches(selected.value)
    flowMessage.value = result.duplicate
      ? (result.message ?? '同一申请已重算过')
      : `重算完成：${result.batches.map((item) => `${item.batch_key}=${item.status}`).join('，')}`
    selected.value = []
    await refreshAll()
  } catch (error) {
    fail(error)
  } finally {
    busy.value = false
  }
}

async function resume() {
  if (!activeRound.value) return
  busy.value = true
  flowError.value = ''
  try {
    const result = await resumeRound(activeRound.value.round_key)
    flowMessage.value = result.message
    await refreshAll()
  } catch (error) {
    fail(error)
  } finally {
    busy.value = false
  }
}

async function dispatchInspectionFor(batchKey: string) {
  if (!activeRound.value) return
  flowError.value = ''
  try {
    const result = await dispatchInspection(activeRound.value.round_key, batchKey)
    flowMessage.value = result.message
    await refreshAll()
  } catch (error) {
    fail(error)
  }
}

async function interrupt(batchKey: string) {  if (!activeRound.value) return
  try {
    await interruptRound(activeRound.value.round_key, batchKey, '手工中断')
    await refreshAll()
  } catch (error) {
    fail(error)
  }
}

async function submitSegments() {
  if (!activeRound.value) return
  busy.value = true
  flowError.value = ''
  try {
    const result = await submitCorrection(activeRound.value.round_key, correctionPayload.value)
    latestCorrection.value = result
    dispatchResult.value = null
    flowMessage.value = result.duplicate
      ? (result.message ?? '同一批异常段只计一次修正')
      : `已提交 ${result.items.filter((item) => item.ok).length} 批，失败 ${result.failed_batches.length} 批`
    await refreshAll()
  } catch (error) {
    fail(error)
  } finally {
    busy.value = false
  }
}

async function signLatest() {
  if (!latestCorrection.value) return
  try {
    const result = await signCorrection(
      latestCorrection.value.correction_id,
      Number(signVersion.value),
      '补签新版纠正记录',
    )
    flowMessage.value = result.message
    latestCorrection.value = result.correction
  } catch (error) {
    fail(error)
  }
}

async function dispatchLatest(mode: 'withdraw' | 'settle') {
  if (!latestCorrection.value) return
  busy.value = true
  flowError.value = ''
  try {
    const result = await dispatchCorrection(
      latestCorrection.value.correction_id,
      mode,
      mode === 'settle' ? Number(signVersion.value) || undefined : undefined,
    )
    dispatchResult.value = result
    latestCorrection.value = result
    flowMessage.value = result.message ?? '纠正单已发出'
    provenance.value = await getProvenance(result.items[0]?.batch_key ?? '')
    await refreshAll()
  } catch (error) {
    fail(error)
  } finally {
    busy.value = false
  }
}

async function doWithdrawTicket(ticketId: string) {
  try {
    const result = await withdrawTicket(ticketId, '手工授权')
    flowMessage.value = result.message
    await refreshAll()
  } catch (error) {
    fail(error)
  }
}

async function doSettleTicket(ticketId: string) {
  if (!latestCorrection.value) return
  try {
    const version = Math.max(...latestCorrection.value.signed_versions.map((item) => item.version))
    const result = await settleTicket(ticketId, latestCorrection.value.correction_id, version)
    flowMessage.value = result.message
    await refreshAll()
  } catch (error) {
    fail(error)
  }
}

async function submitReview() {
  flowError.value = ''
  try {
    const latePoint = reviewForm.lat && reviewForm.lon && reviewForm.timestamp
      ? {
        lat: Number(reviewForm.lat),
        lon: Number(reviewForm.lon),
        timestamp: Number(reviewForm.timestamp),
      }
      : null
    const result = await signReview(
      reviewForm.batch_key,
      reviewForm.point_id,
      reviewForm.verdict,
      latePoint,
    )
    flowMessage.value = result.message
    await refreshAll()
  } catch (error) {
    fail(error)
  }
}

async function loadProvenance() {
  flowError.value = ''
  try {
    provenance.value = await getProvenance(
      provenanceQuery.batch_key,
      provenanceQuery.point_id || undefined,
    )
  } catch (error) {
    fail(error)
  }
}

onMounted(refreshAll)
</script>

<style scoped>
.quality-flow { margin-top: 20px; border-top: 1px solid var(--border); padding-top: 16px; }
.flow-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.flow-head h3 { margin: 0; }
.flow-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.flow-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.flow-panel h4 { margin: 0 0 8px; font-size: 14px; }
.flow-hint { color: var(--muted); font-size: 12px; margin: 4px 0; }
.flow-message { color: #15803d; font-size: 13px; }
.flow-actions { margin-top: 8px; flex-wrap: wrap; }
.flow-inline { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-end; margin: 8px 0; font-size: 12px; color: var(--muted); }
.flow-inline input, .flow-inline select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.flow-inline label { display: flex; flex-direction: column; gap: 4px; }
.flow-list { font-size: 12px; color: var(--muted); padding-left: 16px; }
.flow-result { background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 10px; font-size: 13px; }
.segment-group { border-bottom: 1px dashed var(--border); padding: 6px 0; }
.segment-batch { display: flex; gap: 8px; align-items: center; font-size: 13px; }
.range-list { display: flex; flex-wrap: wrap; gap: 10px; padding-left: 20px; font-size: 12px; }
.range-list.failed { color: #b42318; }
.badge { display: inline-block; margin-left: 6px; background: #eef2ff; color: #1f6feb; border-radius: 10px; padding: 1px 8px; font-size: 11px; }
@media (max-width: 960px) {
  .flow-grid { grid-template-columns: 1fr; }
}
</style>
