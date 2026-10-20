<script setup lang="ts">
/**
 * 个股详情：K线图 + 指标面板 + LLM 决策仪表盘。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { CheckCircle2, AlertTriangle, XCircle, Sparkles } from 'lucide-vue-next'

import {
  analyzeStock,
  getStockKline,
  getStockQuote,
  type DecisionDashboard,
  type StockQuote,
} from '../api'
import ConclusionBadge from '../components/ConclusionBadge.vue'
import KLineChart from '../components/KLineChart.vue'

const route = useRoute()
const code = computed(() => String(route.params.code ?? '600519'))

const quote = ref<StockQuote | null>(null)
const kline = ref<{ dates: string[]; kline: number[][]; volumes: number[] } | null>(null)
const dashboard = ref<DecisionDashboard | null>(null)
const analyzing = ref(false)

async function loadKline() {
  const data = await getStockKline(code.value, 120)
  kline.value = { dates: data.dates, kline: data.kline, volumes: data.volumes }
}

async function loadQuote() {
  try {
    quote.value = await getStockQuote(code.value)
  } catch {
    quote.value = null
  }
}

async function loadAnalysis() {
  analyzing.value = true
  try {
    dashboard.value = await analyzeStock(code.value)
  } catch {
    dashboard.value = null
  } finally {
    analyzing.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadKline(), loadQuote(), loadAnalysis()])
})

const pctTone = computed(() => {
  const p = quote.value?.pct_change
  if (p === null || p === undefined || p === 0) return 'text-content'
  return p > 0 ? 'text-up' : 'text-down'
})

function statusIcon(status: string) {
  if (status === 'pass') return CheckCircle2
  if (status === 'fail') return XCircle
  return AlertTriangle
}

function statusColor(status: string) {
  if (status === 'pass') return 'text-up'
  if (status === 'fail') return 'text-down'
  return 'text-warn'
}
</script>

<template>
  <div class="space-y-5">
    <!-- 股票头部信息 -->
    <div class="glass-card flex flex-wrap items-center justify-between gap-4 p-5">
      <div class="flex items-center gap-5">
        <div>
          <div class="flex items-center gap-2.5">
            <h2 class="text-[22px] font-semibold text-content">{{ quote?.name ?? code }}</h2>
            <span class="font-num rounded-md bg-base-700 px-2 py-0.5 text-[12px] text-content-muted">{{ code }}</span>
          </div>
          <div class="mt-2 flex items-baseline gap-3">
            <span class="font-num text-[32px] font-bold leading-none" :class="pctTone">
              {{ quote?.price?.toFixed(2) ?? '--' }}
            </span>
            <span class="font-num text-[15px]" :class="pctTone">
              {{ quote?.pct_change != null ? (quote.pct_change > 0 ? '+' : '') + quote.pct_change.toFixed(2) + '%' : '--' }}
            </span>
          </div>
        </div>
      </div>
      <div class="grid grid-cols-2 gap-x-8 gap-y-2 text-[12px] sm:grid-cols-4">
        <div><div class="text-content-faint">今开</div><div class="font-num text-content">{{ quote?.open?.toFixed(2) ?? '--' }}</div></div>
        <div><div class="text-content-faint">最高</div><div class="font-num text-up">{{ quote?.high?.toFixed(2) ?? '--' }}</div></div>
        <div><div class="text-content-faint">最低</div><div class="font-num text-down">{{ quote?.low?.toFixed(2) ?? '--' }}</div></div>
        <div><div class="text-content-faint">昨收</div><div class="font-num text-content">{{ quote?.prev_close?.toFixed(2) ?? '--' }}</div></div>
        <div><div class="text-content-faint">成交量</div><div class="font-num text-content">{{ quote?.volume ? (quote.volume / 1e4).toFixed(1) + ' 万手' : '--' }}</div></div>
        <div><div class="text-content-faint">成交额</div><div class="font-num text-content">{{ quote?.amount ? (quote.amount / 1e8).toFixed(2) + ' 亿' : '--' }}</div></div>
        <div><div class="text-content-faint">换手率</div><div class="font-num text-content">{{ quote?.turnover_rate != null ? quote.turnover_rate.toFixed(2) + '%' : '--' }}</div></div>
        <div><div class="text-content-faint">量比</div><div class="font-num text-content">{{ quote?.volume_ratio?.toFixed(2) ?? '--' }}</div></div>
      </div>
    </div>

    <div class="grid grid-cols-1 gap-5 xl:grid-cols-4">
      <!-- K线图 -->
      <div class="glass-card p-4 xl:col-span-3">
        <div class="mb-2 flex items-center justify-between">
          <h3 class="text-subheading">日K线走势</h3>
          <span class="text-[11px] text-content-faint">MA5 天蓝 · MA10 黄 · MA20 紫</span>
        </div>
        <KLineChart
          v-if="kline"
          :dates="kline.dates"
          :kline="kline.kline"
          :volumes="kline.volumes"
          height="440px"
        />
        <div v-else class="flex h-[440px] items-center justify-center text-content-faint">
          K线加载中...
        </div>
      </div>

      <!-- LLM 决策仪表盘 -->
      <div class="glass-card flex flex-col p-5">
        <div class="mb-3 flex items-center gap-2">
          <Sparkles class="h-4 w-4 text-primary-light" />
          <h3 class="text-subheading">AI 决策仪表盘</h3>
        </div>

        <div v-if="analyzing" class="flex flex-1 items-center justify-center text-content-faint">
          <div class="flex flex-col items-center gap-2">
            <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary-light border-t-transparent" />
            <span class="text-[12px]">分析中...</span>
          </div>
        </div>

        <template v-else-if="dashboard">
          <div class="mb-4 flex items-center gap-3">
            <ConclusionBadge :conclusion="dashboard.conclusion" size="lg" />
            <span class="text-[11px] text-content-faint">
              {{ dashboard.degraded ? '规则降级' : dashboard.model }}
            </span>
          </div>
          <p class="mb-4 text-[13px] leading-relaxed text-content-muted">{{ dashboard.summary }}</p>

          <!-- 三档价格 -->
          <div class="mb-4 grid grid-cols-3 gap-2 text-center">
            <div class="rounded-lg border border-up/30 bg-up/10 p-2">
              <div class="text-[10px] text-content-faint">入场价</div>
              <div class="font-num text-[15px] font-semibold text-up">{{ dashboard.entry_price?.toFixed(2) ?? '--' }}</div>
            </div>
            <div class="rounded-lg border border-down/30 bg-down/10 p-2">
              <div class="text-[10px] text-content-faint">止损价</div>
              <div class="font-num text-[15px] font-semibold text-down">{{ dashboard.stop_loss?.toFixed(2) ?? '--' }}</div>
            </div>
            <div class="rounded-lg border border-warn/30 bg-warn/10 p-2">
              <div class="text-[10px] text-content-faint">目标价</div>
              <div class="font-num text-[15px] font-semibold text-warn">{{ dashboard.target_price?.toFixed(2) ?? '--' }}</div>
            </div>
          </div>

          <!-- 检查清单 -->
          <div class="space-y-2">
            <div
              v-for="item in dashboard.checklist"
              :key="item.label"
              class="flex items-start gap-2 rounded-lg bg-base-700/40 p-2"
            >
              <component :is="statusIcon(item.status)" class="mt-0.5 h-3.5 w-3.5 shrink-0" :class="statusColor(item.status)" />
              <div class="min-w-0 flex-1">
                <div class="flex items-center justify-between text-[12px]">
                  <span class="font-medium text-content">{{ item.label }}</span>
                </div>
                <div class="text-[11px] text-content-faint">{{ item.detail }}</div>
              </div>
            </div>
          </div>

          <div class="mt-4 border-t border-base-600/50 pt-3 text-[10px] leading-relaxed text-content-faint">
            {{ dashboard.generated_at }} 生成 · 仅供学习研究，不构成投资建议
          </div>
        </template>

        <div v-else class="flex flex-1 items-center justify-center text-content-faint">
          分析暂不可用
        </div>
      </div>
    </div>
  </div>
</template>
