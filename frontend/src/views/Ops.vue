<script setup lang="ts">
/**
 * 运维审计：调度器任务与下次执行时间、行情数据新鲜度、访客访问记录。
 *
 * 这些能力后端早就有了，但只能命令行/直接调接口才能看到。页面只做**只读展示**：
 * 不触发扫描、不重启服务、不修改任何参数；Windows 计划任务（备份/分钟线导出）不在
 * 进程内 APScheduler 里，因此只列口径与时刻，不伪造「运行状态」。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Clock3, Download, RefreshCw, Server, Users } from 'lucide-vue-next'

import { csvDateTag, downloadCsv } from '../lib/csv'

import {
  getAccessLog,
  getDataStatus,
  getMonitorStatus,
  type AccessRecord,
  type DataStatus,
  type MonitorStatus,
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
const lastRefresh = ref('')
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

async function loadAll() {
  // 任一子接口失败都不影响其它卡片，避免一个请求挂掉整页空白
  const [m, d] = await Promise.all([
    getMonitorStatus().catch(() => null),
    getDataStatus().catch(() => null),
  ])
  monitor.value = m
  dataStatus.value = d
  lastRefresh.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  await loadAccess()
}

const dataTone = computed(() => ({
  ok: 'text-up', warn: 'text-warn', error: 'text-down', unknown: 'text-content-faint',
}[dataStatus.value?.level ?? 'unknown']))

onMounted(() => {
  loadAll()
  timer = window.setInterval(loadAll, 30_000)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div>
        <h2 class="text-[18px] font-semibold tracking-tight text-content">运维审计</h2>
        <p class="mt-1 text-[12.5px] text-content-faint">
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

    <!-- 进程内调度任务 -->
    <div class="glass-card p-5">
      <h3 class="text-subheading">调度任务</h3>
      <p class="mt-1 text-[12.5px] text-content-faint">
        APScheduler（后端进程内）；「下次执行」为空通常表示任务被暂停或未注册。
      </p>
      <div class="mt-3 overflow-x-auto">
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
      <div class="overflow-x-auto">
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
