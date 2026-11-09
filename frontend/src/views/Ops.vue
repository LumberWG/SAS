<script setup lang="ts">
/**
 * 运维审计：调度器任务与下次执行时间、行情数据新鲜度、访客访问记录。
 *
 * 这些能力后端早就有了，但只能命令行/直接调接口才能看到。页面只做**只读展示**：
 * 不触发扫描、不重启服务、不修改任何参数；Windows 计划任务（备份/分钟线导出）不在
 * 进程内 APScheduler 里，因此只列口径与时刻，不伪造「运行状态」。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Clock3, Download, RefreshCw, Server, ShieldAlert, Users } from 'lucide-vue-next'

import { csvDateTag, downloadCsv } from '../lib/csv'
import { POLL } from '../lib/polling'

import {
  getAccessLog,
  getBreadth,
  getDataStatus,
  getEntryGuard,
  getMlStatus,
  getMonitorStatus,
  getReview,
  getSelfCheck,
  getShadow,
  type AccessRecord,
  type BreadthRow,
  type DataStatus,
  type EntryGuard,
  type MlStatus,
  type MonitorStatus,
  type ReviewDoc,
  type ReviewName,
  type SelfCheck,
  type ShadowStatus,
} from '../api'

const monitor = ref<MonitorStatus | null>(null)
const dataStatus = ref<DataStatus | null>(null)
const records = ref<AccessRecord[]>([])
const accessTotal = ref(0)
const uniqueVisitors = ref<number | null>(null)
const days = ref(7)
const excludeDev = ref(true)
const unique = ref(false)
const loading = ref(false)
const error = ref('')
const partialError = ref('')
const lastRefresh = ref('')
const mlStatus = ref<MlStatus | null>(null)
const shadow = ref<ShadowStatus | null>(null)
const shadowMl = computed(() => shadow.value?.profiles.find((p) => p.name === 'default_ml') ?? null)
const breadthRows = ref<BreadthRow[]>([])
const breadthNote = ref('')
const reviewDoc = ref<ReviewDoc | null>(null)
const reviewName = ref<ReviewName>('daily')
const reviewLoading = ref(false)
const selfCheck = ref<SelfCheck | null>(null)
const entryGuard = ref<EntryGuard | null>(null)
const reviewTabs: Array<{ key: ReviewName; label: string }> = [
  { key: 'daily', label: '每日复盘' },
  { key: 'param', label: '参数体检' },
  { key: 'position', label: '仓位盈亏' },
]
let timer: number | undefined

const JOB_LABELS: Record<string, string> = {
  snapshot_refresh: '行情快照刷新（60s）',
  alert_detect: '市场异动检测（5min）',
  auto_scan: '收盘自动选股（15:30）',
  pre_market: '开盘前分析（09:20）',
  intraday_monitor: '盘中自选股监控（1min）',
  post_market: '盘后分析（15:35）',
  paper_settle: '模拟盘结算（15:40）',
  paper_tick: '实时撮合（5s）',
  kline_warmup: 'K线预热（16:00）',
  kline_refill: '当日K线回填（20:30）',
  shadow_eval: '影子账户（16:45）',
}
function jobLabel(id: string): string {
  return JOB_LABELS[id] ?? id
}
/** APScheduler 的 next_run_time 带时区后缀，页面只留到分钟 */
function fmtTime(value: string | null): string {
  if (!value) return '—'
  return value.replace('T', ' ').slice(0, 16)
}
function fmtTs(ts: string): string {
  return (ts || '').replace('T', ' ').slice(0, 19) || '—'
}
function deviceOf(r: AccessRecord): string {
  const d = r.device
  return d ? `${d.os} · ${d.browser}` : (r.ua || '—').slice(0, 28)
}

async function loadAccess() {
  loading.value = true
  error.value = ''
  try {
    const res = await getAccessLog({
      limit: 200,
      days: days.value,
      exclude_dev: excludeDev.value,
      unique: unique.value,
    })
    records.value = res.records
    accessTotal.value = res.total
    uniqueVisitors.value = res.unique_visitors
  } catch (e) {
    error.value = `访问记录读取失败：${String(e)}`
  } finally {
    loading.value = false
  }
}

/** 导出当前筛选条件下的访客记录（所见即所得，不重新查库） */
function exportAccessCsv() {
  downloadCsv(
    `访客记录_${csvDateTag()}.csv`,
    ['时间', 'IP', '方法', '路径', '状态码', '浏览器', '系统', '开发者', 'API', 'UA', '来源'],
    records.value.map((r) => [
      r.ts, r.ip, r.method, r.path, r.status,
      r.device?.browser ?? '', r.device?.os ?? '',
      r.is_dev ? '是' : '否', r.is_api ? '是' : '否',
      r.ua ?? '', r.referer ?? '',
    ]),
  )
}

/** 切换复盘文档 tab（只读原文，不刷新整页其它卡片） */
async function loadReview(name: ReviewName = reviewName.value) {
  reviewName.value = name
  reviewLoading.value = true
  reviewDoc.value = await getReview(name).catch(() => null)
  reviewLoading.value = false
}

function fmtPct(v: number | null): string {
  return v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}
function fmtRatio(v: number | null): string {
  return v == null ? '—' : `${(v * 100).toFixed(1)}%`
}

async function loadAll() {
  // 任一子接口失败都不影响其它卡片，避免一个请求挂掉整页空白
  const [m, d, ml, sh, br, rv, ck] = await Promise.all([
    getMonitorStatus().catch(() => null),
    getDataStatus().catch(() => null),
    getMlStatus().catch(() => null),
    getShadow().catch(() => null),
    getBreadth(60).catch(() => null),
    getReview(reviewName.value).catch(() => null),
    getSelfCheck().catch(() => null),
  ])
  monitor.value = m
  selfCheck.value = ck
  dataStatus.value = d
  mlStatus.value = ml
  shadow.value = sh
  breadthRows.value = br?.rows ?? []
  breadthNote.value = br?.note ?? ''
  reviewDoc.value = rv
  entryGuard.value = await getEntryGuard().catch(() => null)
  const failed = [m, d, ml, sh, br, rv, ck].filter((x) => x === null).length
  partialError.value = failed
    ? `${failed} 个只读接口未返回数据，下方卡片可能仍是上一次的结果`
    : ''
  lastRefresh.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  await loadAccess()
}

const dataTone = computed(() => ({
  ok: 'text-up', warn: 'text-warn', error: 'text-down', unknown: 'text-content-faint',
}[dataStatus.value?.level ?? 'unknown']))
const mlStateLabel = computed(() => ({
  off: '未开启', effective: '已生效', pending: '待运行', fallback: '回退 14 因子',
}[mlStatus.value?.state ?? 'pending']))
const mlTone = computed(() => ({
  off: 'text-content-faint', effective: 'text-up', pending: 'text-content-faint', fallback: 'text-warn',
}[mlStatus.value?.state ?? 'pending']))
const checkChipClass = computed(() => ({
  ok: 'chip-up', warn: 'chip-warn', error: 'chip-down',
}[selfCheck.value?.level ?? 'warn']))
const checkLabel = computed(() => ({
  ok: '全部正常', warn: '有提示项', error: '存在故障',
}[selfCheck.value?.level ?? 'warn']))
function dotOf(level: string): string {
  return { ok: 'bg-up', warn: 'bg-warn', error: 'bg-down', info: 'bg-primary-light' }[level] ?? 'bg-content-faint'
}
function levelText(level: string): string {
  return { ok: '正常', warn: '提示', error: '故障', info: '说明' }[level] ?? level
}

onMounted(() => {
  loadAll()
  timer = window.setInterval(loadAll, POLL.defaultMs)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="space-y-4">
    <div
      v-if="entryGuard"
      class="flex flex-wrap items-center justify-between gap-3 rounded-lg border px-4 py-2 text-[12.5px]"
      :class="{
        'border-up/30 bg-up/10 text-up': entryGuard.regime.state === 'uptrend',
        'border-amber-400/30 bg-amber-400/10 text-amber-300': entryGuard.regime.state === 'choppy',
        'border-down/30 bg-down/10 text-down': entryGuard.regime.state === 'declining',
        'border-white/15 bg-white/5 text-content-faint': entryGuard.regime.state === 'unknown',
      }"
    >
      <span class="flex items-center gap-2">
        <span class="font-semibold">
          市场状态：
          {{
            entryGuard.regime.state === 'uptrend' ? '上升市 · 可正常开仓'
            : entryGuard.regime.state === 'choppy' ? '震荡市 · 控制仓位'
            : entryGuard.regime.state === 'declining' ? '下降市 · 慎开新仓'
            : '市场状态未知'
          }}
        </span>
        <template v-if="entryGuard.regime.as_of">
          （上证 {{ entryGuard.regime.close?.toFixed(0) }} · MA20 {{ entryGuard.regime.ma20?.toFixed(0) }} · 截至 {{ entryGuard.regime.as_of }}）
        </template>
      </span>
      <span class="opacity-80">下降市中动量信号 20 日超额转负，新开仓建议等待指数站回 MA20 上方</span>
      <span
        v-if="entryGuard?.block_state"
        class="rounded border px-2 py-0.5 text-[11.5px]"
        :class="entryGuard.block_state.enabled
          ? (entryGuard.block_state.active ? 'border-down/40 bg-down/15 text-down' : 'border-up/40 bg-up/15 text-up')
          : 'border-white/15 bg-white/5 text-content-faint'"
      >
        <template v-if="!entryGuard.block_state.enabled">硬屏蔽：未启用</template>
        <template v-else-if="entryGuard.block_state.active">硬屏蔽：已拦截新开仓</template>
        <template v-else>硬屏蔽：已布防（非下降市）</template>
      </span>
    </div>
    <!-- 入场护栏明细：逐信号 flags 标签（影子层，仅提示，不参与交易） -->
    <div
      v-if="entryGuard && entryGuard.signals && entryGuard.signals.length"
      class="glass-card p-5"
    >
      <div class="mb-2 flex items-center gap-2">
        <ShieldAlert class="h-4 w-4 text-amber-300" />
        <h3 class="text-subheading">入场护栏明细</h3>
        <span class="text-[12.5px] text-content-faint">{{ entryGuard.signals.length }} 条近期信号</span>
      </div>
      <p class="mb-3 text-[12.5px] leading-relaxed text-content-faint">
        市场状态滤网：上证站上 MA20 时动量信号 20 日超额 +0.74%，跌破后转 −1.22%，下降市标注「慎开新仓」；
        日内追高护栏：信号日高开 &gt;3% 或盘中冲高 &gt;5% 时提示用限价单在尾盘或回踩均线买入。均为只读提示，不参与交易。
      </p>
      <div class="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
        <div
          v-for="sig in entryGuard.signals"
          :key="sig.code + sig.scan_date"
          class="rounded-lg border border-base-600/60 bg-base-800/40 p-3"
        >
          <div class="flex items-center justify-between gap-2">
            <span class="text-[14px] font-semibold text-content">{{ sig.name }}</span>
            <span class="font-num text-[12px] text-content-faint">{{ sig.code }}</span>
          </div>
          <div class="mt-0.5 text-[11.5px] text-content-faint">{{ sig.strategy }} · {{ sig.scan_date }}</div>
          <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
            <span
              v-for="fl in sig.flags"
              :key="fl"
              class="rounded px-1.5 py-0.5 text-[11px] font-medium"
              :class="fl === '下降市慎开新仓' ? 'border border-down/40 bg-down/15 text-down' : 'border border-amber-400/40 bg-amber-400/15 text-amber-300'"
            >
              {{ fl }}
            </span>
            <span v-if="!sig.flags.length" class="text-[11px] text-content-faint">无护栏触发</span>
          </div>
          <div v-if="sig.intraday && sig.intraday.triggered && sig.intraday.note" class="mt-1 text-[11px] leading-relaxed text-amber-300/80">
            {{ sig.intraday.note }}
          </div>
        </div>
      </div>
    </div>
    <div class="page-hd">
      <div>
        <h2 class="text-subheading font-semibold tracking-tight text-content">运行监控</h2>
        <p class="page-desc">
          只读：调度任务、数据新鲜度、访客记录 · 每 30 秒刷新 · 最近更新 {{ lastRefresh || '—' }}
        </p>
      </div>
      <button class="btn btn-outline btn-sm" :disabled="loading" @click="loadAll">
        <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />
        刷新
      </button>
    </div>

    <p v-if="error" class="rounded-lg border border-down/30 bg-down/[0.08] px-3 py-2 text-[12.5px] text-down">
      {{ error }}
    </p>
    <p v-if="partialError" class="banner-warn rounded-lg px-3 py-2 text-[12.5px]">
      {{ partialError }}（保留已取到的数据，点右上角刷新重试）
    </p>

    <!-- 服务与数据 -->
    <div class="grid gap-4 lg:grid-cols-3">
      <div class="glass-card p-4">
        <div class="mb-2 flex items-center gap-2 text-[13px] font-medium text-content">
          <Server class="h-4 w-4 text-primary-light" /> 进程内调度器
        </div>
        <p class="text-[22px] font-semibold" :class="monitor?.running ? 'text-up' : 'text-down'">
          {{ monitor?.running ? '运行中' : '未运行 / 状态未知' }}
        </p>
        <p class="mt-1 text-[12.5px] text-content-faint">
          {{ monitor?.jobs.length ?? 0 }} 个任务 · 下次执行时间见下表
        </p>
      </div>
      <div class="glass-card p-4">
        <div class="mb-2 flex items-center gap-2 text-[13px] font-medium text-content">
          <Clock3 class="h-4 w-4 text-primary-light" /> 行情数据新鲜度
        </div>
        <p class="text-[22px] font-semibold" :class="dataTone">
          {{ dataStatus?.message ?? '状态未知' }}
        </p>
        <p class="mt-1 text-[12.5px] text-content-faint">
          快照 {{ dataStatus?.snapshot.rows ?? '—' }} 只 · 更新于 {{ dataStatus?.snapshot.updated_at ?? '—' }}
          · 已过 {{ dataStatus?.snapshot.age_seconds ?? '—' }}s
        </p>
      </div>
      <div class="glass-card p-4">
        <div class="mb-2 flex items-center gap-2 text-[13px] font-medium text-content">
          <Users class="h-4 w-4 text-primary-light" /> 真实访客
        </div>
        <p class="text-[22px] font-semibold font-num text-content">
          {{ unique ? (uniqueVisitors ?? '—') : accessTotal }}
        </p>
        <p class="mt-1 text-[12.5px] text-content-faint">
          {{ unique ? '独立 IP（勾选去重）' : `近 ${days} 天访问条数` }} · 分钟库最新 {{ dataStatus?.minute_export.latest_date ?? '—' }}
        </p>
      </div>
    </div>

    <!-- 系统自检：把「页面看着正常但某个环节已经静默坏掉」变成一屏红黄灯 -->
    <div class="glass-card p-5">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 class="text-subheading">系统自检</h3>
          <p class="mt-1 text-[12.5px] text-content-faint">
            只读汇总库结构 / 净值 / 快照 / 调度 / 分钟库 / 影子账户 / ML · {{ selfCheck?.server_time ?? '—' }}
          </p>
        </div>
        <span class="chip" :class="checkChipClass">{{ checkLabel }}</span>
      </div>
      <div class="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        <div
          v-for="c in selfCheck?.checks ?? []"
          :key="c.key"
          class="rounded-lg border border-base-600/60 px-3 py-2"
        >
          <div class="flex items-center gap-2 text-[13px]">
            <span class="h-1.5 w-1.5 shrink-0 rounded-full" :class="dotOf(c.level)"></span>
            <span class="font-medium text-content">{{ c.label }}</span>
            <span class="ml-auto text-[12px] text-content-faint">{{ levelText(c.level) }}</span>
          </div>
          <p class="mt-1 text-[12px] leading-relaxed text-content-muted">{{ c.detail }}</p>
          <p v-if="c.hint" class="mt-1 text-[12px] text-content-faint">{{ c.hint }}</p>
        </div>
        <div v-if="!selfCheck" class="text-[12.5px] text-content-faint">自检接口不可用</div>
      </div>
      <p class="mt-2 text-[12px] text-content-faint">{{ selfCheck?.note ?? '' }}</p>
    </div>

    <!-- 进程内调度任务 -->
    <div class="glass-card p-5">
      <h3 class="text-subheading">调度任务</h3>
      <p class="mt-1 text-[12.5px] text-content-faint">
        APScheduler（后端进程内）；「下次执行」为空通常表示任务被暂停或未注册。
      </p>
      <div class="mt-3 table-wrap">
        <table class="w-full text-left text-[13.5px]">
          <thead class="border-b border-base-600/60 text-content-faint">
            <tr>
              <th class="px-3 py-2 font-medium">任务</th>
              <th class="px-3 py-2 font-medium">ID</th>
              <th class="px-3 py-2 text-right font-medium">下次执行</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="job in monitor?.jobs ?? []" :key="job.id" class="border-b border-base-700/40">
              <td class="px-3 py-2 text-content">{{ jobLabel(job.id) }}</td>
              <td class="px-3 py-2 font-num text-content-faint">{{ job.id }}</td>
              <td class="px-3 py-2 text-right font-num text-content-muted">{{ fmtTime(job.next_run_time) }}</td>
            </tr>
            <tr v-if="!monitor?.jobs?.length">
              <td colspan="3" class="px-3 py-4 text-center text-content-faint">暂无调度任务</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="mt-3 flex flex-wrap gap-2 text-[12.5px]">
        <span class="chip chip-muted" title="由 Windows 计划任务执行，本页不查询其状态">SASDbBackup · 每日 15:50 备份</span>
        <span class="chip chip-muted" title="由 Windows 计划任务执行，本页不查询其状态">SASMinutesFsdb · 交易日 17:30 导出</span>
        <span class="chip chip-muted" title="手动触发">SASRestart / SASDeploy · 手动</span>
      </div>
    </div>

    <!-- 线上 ML + 复盘产物：这些内容原来只存在于日志/命令行/仓库文档里 -->
    <div class="glass-card p-5">
      <h3 class="text-subheading">ML 与复盘</h3>
      <p class="mt-1 text-[12.5px] text-content-faint">
        只读：线上 ML 是否真的生效、影子账户前向配对、脚本产出的复盘文档与广度历史
      </p>

      <div class="mt-3 grid gap-3 lg:grid-cols-2">
        <!-- 线上 ML 状态 -->
        <div class="rounded-lg border border-base-600/60 p-3">
          <div class="text-[13px] font-medium text-content">线上 ML 打分</div>
          <div class="mt-1 text-[18px] font-semibold" :class="mlTone">{{ mlStateLabel }}</div>
          <p class="mt-1 text-[12.5px] text-content-muted">{{ mlStatus?.note ?? '状态接口不可用' }}</p>
          <dl class="mt-3 grid grid-cols-1 gap-x-3 gap-y-1 text-[12.5px] xs:grid-cols-2">
            <dt class="text-content-faint">开关</dt><dd class="text-content">{{ mlStatus?.enabled ? '开' : '关' }}</dd>
            <dt class="text-content-faint">数据日</dt><dd class="font-num text-content">{{ mlStatus?.as_of ?? '—' }}</dd>
            <dt class="text-content-faint">覆盖只数</dt><dd class="font-num text-content">{{ mlStatus?.codes ?? '—' }}</dd>
            <dt class="text-content-faint">OOS rank-IC</dt><dd class="font-num text-content">{{ mlStatus?.oos_rank_ic ?? '—' }}</dd>
            <dt class="text-content-faint">最近判定</dt><dd class="font-num text-content">{{ mlStatus?.at ?? '—' }}</dd>
            <dt class="text-content-faint">缓存数据日</dt><dd class="font-num text-content">{{ mlStatus?.cache_days ?? '—' }}</dd>
          </dl>
          <p class="mt-2 text-[12px] leading-relaxed text-content-faint">
            开关打开 ≠ 生效；effective=false 时结果与 14 因子基线相同，不能当作「ML 有效」的证据（#82）。
            「待运行」只表示本进程重启后还没跑过一轮，不等于回退。
          </p>
        </div>

        <!-- 影子账户 -->
        <div class="rounded-lg border border-base-600/60 p-3">
          <div class="flex items-center justify-between gap-2">
            <div class="text-[13px] font-medium text-content">影子账户（16:45）</div>
            <span class="text-[12px] text-content-faint">{{ shadow?.updated_at ?? '—' }}</span>
          </div>
          <div v-if="shadow?.ml_pair" class="mt-2 rounded-lg border border-primary/30 bg-primary/[0.06] px-2.5 py-2 text-[12.5px]">
            <span class="font-medium text-primary-light">ML 前向配对</span>：
            default 超额 {{ fmtPct(shadow.ml_pair.base_excess) }} ·
            default_ml 超额 {{ fmtPct(shadow.ml_pair.ml_excess) }} ·
            差 <span :class="shadow.ml_pair.diff >= 0 ? 'text-up' : 'text-down'">{{ fmtPct(shadow.ml_pair.diff) }}</span>
          </div>
          <div class="mt-2 flex flex-wrap items-center gap-2 text-[12.5px]">
            <span
              class="chip"
              :class="shadow?.ml_state === 'effective' ? 'chip-up' : (shadow?.ml_state === 'fallback' || shadow?.ml_state === 'unknown') ? 'chip-warn' : 'chip-muted'"
            >{{ shadow?.ml_note ?? 'ML 判定不可用' }}</span>
            <span v-if="shadowMl?.ml_diagnostics_available" class="text-content-faint">
              OOS rank-IC <span class="font-num">{{ shadowMl.ml_oos_rank_ic?.toFixed(4) ?? '—' }}</span>
              · 预测对 <span class="font-num">{{ shadowMl.ml_pred_pairs ?? '—' }}</span>
              · 模式 {{ shadowMl.ml_mode ?? '—' }}
            </span>
          </div>
          <p
            class="mt-2 rounded-lg px-2.5 py-2 text-[12.5px]"
            :class="shadow?.pair_identical ? 'border border-warn/30 bg-warn/[0.08] text-warn' : 'text-content-muted'"
          >
            {{ shadow?.note ?? '影子账户状态不可用' }}
          </p>
          <div v-if="shadow?.profiles.length" class="mt-2 table-wrap">
            <table class="w-full text-left text-[12.5px]">
              <thead class="text-content-faint">
                <tr>
                  <th class="py-1 font-medium">方案</th>
                  <th class="py-1 text-right font-medium">收益</th>
                  <th class="py-1 text-right font-medium">超额</th>
                  <th class="py-1 text-right font-medium">快照</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="p in shadow.profiles" :key="p.name" class="border-t border-base-700/40">
                  <td class="py-1 text-content">{{ p.name }}</td>
                  <td class="py-1 text-right font-num text-content-muted">{{ fmtPct(p.total_return) }}</td>
                  <td class="py-1 text-right font-num text-content-muted">{{ fmtPct(p.excess) }}</td>
                  <td class="py-1 text-right font-num text-content-faint">{{ p.snapshots }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="mt-3 grid gap-3 lg:grid-cols-2">
        <!-- 复盘文档原文 -->
        <div class="rounded-lg border border-base-600/60 p-3">
          <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div class="seg !p-0.5">
              <button
                v-for="t in reviewTabs"
                :key="t.key"
                class="seg-item px-2 py-1"
                :class="reviewName === t.key ? 'seg-item-active' : ''"
                @click="loadReview(t.key)"
              >{{ t.label }}</button>
            </div>
            <span class="text-[12px] text-content-faint">{{ reviewDoc?.updated_at ?? '—' }}</span>
          </div>
          <pre class="max-h-[280px] overflow-auto whitespace-pre-wrap rounded-lg bg-base-900/60 p-3 text-[12px] leading-relaxed text-content-muted">{{ reviewLoading ? '加载中…' : (reviewDoc?.content || (reviewDoc?.note ?? '暂无内容')) }}</pre>
          <p class="mt-1 text-[12px] text-content-faint">
            {{ reviewDoc?.note ?? '' }}<span v-if="reviewDoc?.truncated"> · 内容已截断</span>
          </p>
        </div>

        <!-- 广度历史 -->
        <div class="rounded-lg border border-base-600/60 p-3">
          <div class="mb-2 flex items-center justify-between gap-2">
            <div class="text-[13px] font-medium text-content">涨跌家数广度（近 10 日）</div>
            <span class="text-[12px] text-content-faint">纯观测</span>
          </div>
          <div v-if="breadthRows.length" class="table-wrap">
            <table class="w-full text-left text-[12.5px]">
              <thead class="text-content-faint">
                <tr>
                  <th class="py-1 font-medium">日期</th>
                  <th class="py-1 text-right font-medium">涨</th>
                  <th class="py-1 text-right font-medium">跌</th>
                  <th class="py-1 text-right font-medium">上涨占比</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in breadthRows.slice(-10)" :key="r.date" class="border-t border-base-700/40">
                  <td class="py-1 font-num text-content-muted">{{ r.date }}</td>
                  <td class="py-1 text-right font-num text-up">{{ r.up ?? '—' }}</td>
                  <td class="py-1 text-right font-num text-down">{{ r.down ?? '—' }}</td>
                  <td class="py-1 text-right font-num text-content">{{ fmtRatio(r.up_ratio) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="text-[12.5px] text-content-muted">{{ breadthNote || '广度缓存尚未生成' }}</p>
          <p class="mt-2 text-[12px] leading-relaxed text-content-faint">
            4 年非重叠只有 5 个 shock 样本、方向不稳，广度不进任何交易判定（#86）。
          </p>
        </div>
      </div>
    </div>

    <!-- 访客访问记录 -->
    <div class="glass-card p-5">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 class="text-subheading">访客访问记录</h3>
          <p class="mt-1 text-[12.5px] text-content-faint">
            默认剔除本机 / 局域网 / dev_ips / X-Dev-Key 的调试流量；记录按天落 JSONL
          </p>
        </div>
        <div class="flex flex-wrap items-center gap-3 text-[12.5px] text-content-muted">
          <button class="btn btn-outline btn-sm" :disabled="!records.length" @click="exportAccessCsv">
            <Download class="h-3.5 w-3.5" />导出 CSV
          </button>
          <label class="flex items-center gap-1.5">
            <select v-model.number="days" class="field h-7 w-auto py-0" @change="loadAccess">
              <option :value="1">近 1 天</option>
              <option :value="7">近 7 天</option>
              <option :value="30">近 30 天</option>
            </select>
          </label>
          <label class="flex cursor-pointer items-center gap-1.5">
            <input v-model="excludeDev" type="checkbox" @change="loadAccess" /> 排除开发者
          </label>
          <label class="flex cursor-pointer items-center gap-1.5">
            <input v-model="unique" type="checkbox" @change="loadAccess" /> 按 IP 去重
          </label>
        </div>
      </div>

      <p class="mb-2 text-[12.5px] text-content-faint">
        共 {{ accessTotal }} 条<span v-if="uniqueVisitors != null"> · 独立访客 {{ uniqueVisitors }} 个 IP</span>
        ，最多展示 200 条
      </p>
      <div class="table-wrap">
        <table class="w-full text-left text-[13px]">
          <thead class="border-b border-base-600/60 text-content-faint">
            <tr>
              <th class="px-3 py-2 font-medium">时间</th>
              <th class="px-3 py-2 font-medium">IP</th>
              <th class="px-3 py-2 font-medium">设备</th>
              <th class="px-3 py-2 font-medium">路径</th>
              <th class="px-3 py-2 text-right font-medium">状态</th>
              <th class="px-3 py-2 font-medium">类型</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in records" :key="`${r.ts}-${r.ip}-${i}`" class="border-b border-base-700/40">
              <td class="whitespace-nowrap px-3 py-1.5 font-num text-content-muted">{{ fmtTs(r.ts) }}</td>
              <td class="whitespace-nowrap px-3 py-1.5 font-num text-content">{{ r.ip }}</td>
              <td class="px-3 py-1.5 text-content-muted" :title="r.ua || ''">{{ deviceOf(r) }}</td>
              <td class="max-w-[280px] truncate px-3 py-1.5 font-num text-content" :title="r.referer || r.path">
                {{ r.method }} {{ r.path }}
              </td>
              <td class="px-3 py-1.5 text-right font-num" :class="r.status >= 400 ? 'text-down' : 'text-content-muted'">
                {{ r.status }}
              </td>
              <td class="px-3 py-1.5 text-content-faint">
                {{ r.is_dev ? '开发者' : r.is_api ? 'API' : '页面' }}
              </td>
            </tr>
            <tr v-if="!records.length">
              <td colspan="6" class="px-3 py-4 text-center text-content-faint">
                {{ loading ? '加载中…' : '该条件下暂无访问记录' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
