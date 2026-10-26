<script setup lang="ts">
/**
 * 智能建议：建议买入 / 建议卖出 / 明日关注。
 * 数据由调度器自动生成：开盘 9:20、盘中每分钟、盘后 15:35。
 */
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowDownCircle,
  ArrowUpCircle,
  CalendarClock,
  Lightbulb,
  Star,
} from 'lucide-vue-next'

import {
  getAdvisorySummary,
  getBuyRecommendations,
  getFocusList,
  getSellRecommendations,
  type RecommendationItem,
} from '../api'

const router = useRouter()

const buys = ref<RecommendationItem[]>([])
const sells = ref<RecommendationItem[]>([])
const focus = ref<RecommendationItem[]>([])
const focusDate = ref<string | null>(null)
const summary = ref<Awaited<ReturnType<typeof getAdvisorySummary>> | null>(null)
const updatedAt = ref<string | null>(null)
let timer: number | undefined

async function load() {
  try {
    const [buyData, sellData, focusData, summaryData] = await Promise.all([
      getBuyRecommendations(),
      getSellRecommendations(),
      getFocusList(),
      getAdvisorySummary(),
    ])
    buys.value = buyData.items
    sells.value = sellData.items
    focus.value = focusData.items
    focusDate.value = focusData.for_date
    summary.value = summaryData
    updatedAt.value = buyData.updated_at ?? sellData.updated_at
  } catch {
    // 加载失败保留旧数据
  }
}

function goDetail(code: string) {
  router.push(`/stock/${code}`)
}

function scoreClass(score: number): string {
  if (score > 0) return 'text-up'
  if (score < 0) return 'text-down'
  return 'text-content'
}

/** 当前价：优先实时快照，缺失时退回建议生成价。 */
function currentPrice(item: RecommendationItem): number {
  return item.realtime?.price ?? item.price
}

function fmtPct(v: number | null | undefined): string {
  return v == null ? '--' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

function pctClass(v: number | null | undefined): string {
  if (v == null || v === 0) return 'text-content-faint'
  return v > 0 ? 'text-up' : 'text-down'
}

onMounted(() => {
  load()
  timer = window.setInterval(load, 30000)  // 30s 轮询
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="space-y-5">
    <!-- 顶部：机制说明 + 三项计数 -->
    <div class="glass-card flex flex-wrap items-center justify-between gap-4 p-5">
      <div class="flex items-center gap-3">
        <div
          class="flex h-10 w-10 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-primary-light shadow-glow"
        >
          <Lightbulb class="h-5 w-5 text-white" />
        </div>
        <div>
          <h2 class="text-[17px] font-semibold text-content">智能盯盘建议</h2>
          <p class="mt-0.5 text-[11px] leading-relaxed text-content-faint">
            开盘 9:20 / 盘中每分钟分析自选股，建议产生后由模拟盘实时撮合（5 秒内成交）；盘后 15:35
            生成次日关注，收盘选股评分 Top {{ summary?.auto_watch_top_n ?? 10 }} 自动加入自选
          </p>
        </div>
      </div>
      <div class="flex items-center gap-3">
        <div class="rounded-lg border border-up/30 bg-up/10 px-4 py-2 text-center">
          <div class="font-num text-[22px] font-bold leading-none text-up">
            {{ summary?.buy_count ?? 0 }}
          </div>
          <div class="mt-1 text-[11px] text-content-faint">建议买入</div>
        </div>
        <div class="rounded-lg border border-down/30 bg-down/10 px-4 py-2 text-center">
          <div class="font-num text-[22px] font-bold leading-none text-down">
            {{ summary?.sell_count ?? 0 }}
          </div>
          <div class="mt-1 text-[11px] text-content-faint">建议卖出</div>
        </div>
        <div class="rounded-lg border border-warn/30 bg-warn/10 px-4 py-2 text-center">
          <div class="font-num text-[22px] font-bold leading-none text-warn">
            {{ summary?.focus_count ?? 0 }}
          </div>
          <div class="mt-1 text-[11px] text-content-faint">明日关注</div>
        </div>
        <span v-if="updatedAt" class="text-[11px] text-content-faint">更新于 {{ updatedAt }}</span>
      </div>
    </div>

    <div class="grid grid-cols-1 gap-5 xl:grid-cols-2">
      <!-- 建议买入 -->
      <section class="glass-card flex flex-col p-5">
        <div class="mb-4 flex items-center justify-between">
          <div class="flex items-center gap-2">
            <ArrowUpCircle class="h-4 w-4 text-up" />
            <h3 class="text-subheading">建议买入</h3>
          </div>
          <span class="text-[11px] text-content-faint">{{ buys.length }} 只</span>
        </div>
        <div class="max-h-[480px] space-y-2 overflow-y-auto">
          <div
            v-for="item in buys"
            :key="item.code"
            class="cursor-pointer rounded-lg border border-up/20 bg-up/5 p-3 transition-all hover:border-up/50 hover:bg-up/10"
            @click="goDetail(item.code)"
          >
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="text-[14px] font-semibold text-content">{{ item.name }}</span>
                <span class="font-num text-[11px] text-content-faint">{{ item.code }}</span>
              </div>
              <span class="font-num text-[15px] font-bold" :class="scoreClass(item.score)">
                {{ item.score > 0 ? '+' : '' }}{{ item.score.toFixed(1) }}
              </span>
            </div>
            <p class="mt-1.5 text-[11px] leading-relaxed text-content-muted">{{ item.reason }}</p>
            <div class="mt-2 flex items-center gap-4 font-num text-[11px]">
              <span class="text-content" :title="`建议价 ${item.price.toFixed(2)}`">
                现价 {{ currentPrice(item).toFixed(2) }}
                <span v-if="item.realtime" :class="pctClass(item.realtime.pct_change)">
                  {{ fmtPct(item.realtime.pct_change) }}
                </span>
              </span>
              <span class="text-down">止损 {{ item.stop_loss?.toFixed(2) ?? '--' }}</span>
              <span class="text-warn">目标 {{ item.target_price?.toFixed(2) ?? '--' }}</span>
              <span v-if="item.sentiment" class="ml-auto text-content-faint">{{ item.sentiment }}</span>
            </div>
          </div>
          <div v-if="buys.length === 0" class="flex flex-col items-center gap-2 py-10 text-content-faint">
            <ArrowUpCircle class="h-8 w-8 opacity-30" />
            <p class="text-[12px]">暂无买入建议</p>
            <p class="text-[11px]">交易时段每分钟自动分析自选股，建议产生后即时撮合成交；未配置自选股时无建议</p>
          </div>
        </div>
      </section>

      <!-- 建议卖出 -->
      <section class="glass-card flex flex-col p-5">
        <div class="mb-4 flex items-center justify-between">
          <div class="flex items-center gap-2">
            <ArrowDownCircle class="h-4 w-4 text-down" />
            <h3 class="text-subheading">建议卖出</h3>
          </div>
          <span class="text-[11px] text-content-faint">{{ sells.length }} 只</span>
        </div>
        <div class="max-h-[480px] space-y-2 overflow-y-auto">
          <div
            v-for="item in sells"
            :key="item.code"
            class="cursor-pointer rounded-lg border border-down/20 bg-down/5 p-3 transition-all hover:border-down/50 hover:bg-down/10"
            @click="goDetail(item.code)"
          >
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="text-[14px] font-semibold text-content">{{ item.name }}</span>
                <span class="font-num text-[11px] text-content-faint">{{ item.code }}</span>
              </div>
              <span class="font-num text-[15px] font-bold" :class="scoreClass(item.score)">
                {{ item.score > 0 ? '+' : '' }}{{ item.score.toFixed(1) }}
              </span>
            </div>
            <p class="mt-1.5 text-[11px] leading-relaxed text-content-muted">{{ item.reason }}</p>
            <div class="mt-2 flex items-center gap-4 font-num text-[11px]">
              <span class="text-content" :title="`建议价 ${item.price.toFixed(2)}`">
                现价 {{ currentPrice(item).toFixed(2) }}
                <span v-if="item.realtime" :class="pctClass(item.realtime.pct_change)">
                  {{ fmtPct(item.realtime.pct_change) }}
                </span>
              </span>
              <span class="text-down">止损 {{ item.stop_loss?.toFixed(2) ?? '--' }}</span>
              <span v-if="item.sentiment" class="ml-auto text-content-faint">{{ item.sentiment }}</span>
            </div>
          </div>
          <div v-if="sells.length === 0" class="flex flex-col items-center gap-2 py-10 text-content-faint">
            <ArrowDownCircle class="h-8 w-8 opacity-30" />
            <p class="text-[12px]">暂无卖出建议</p>
            <p class="text-[11px]">自选股技术面转弱（评分 ≤ -3）时出现在此列表</p>
          </div>
        </div>
      </section>
    </div>

    <!-- 明日关注（盘后生成） -->
    <section class="glass-card p-5">
      <div class="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div class="flex items-center gap-2">
          <CalendarClock class="h-4 w-4 text-warn" />
          <h3 class="text-subheading">明日关注</h3>
          <span
            v-if="focusDate"
            class="rounded-md border border-warn/40 bg-warn/10 px-2 py-0.5 text-[11px] text-warn"
          >
            {{ focusDate }} 开盘重点关注
          </span>
        </div>
        <span class="text-[11px] text-content-faint">
          盘后 15:35 全市场扫描评分 Top {{ summary?.focus_count ?? 10 }}
        </span>
      </div>
      <div class="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
        <div
          v-for="item in focus"
          :key="item.code"
          class="flex cursor-pointer items-center justify-between rounded-lg border border-base-600/50 bg-base-700/30 p-3 transition-all hover:border-warn/50"
          @click="goDetail(item.code)"
        >
          <div class="flex items-center gap-2.5">
            <Star class="h-3.5 w-3.5 text-warn" />
            <div>
              <div class="text-[13px] font-medium text-content">{{ item.name }}</div>
              <div class="font-num text-[11px] text-content-faint">{{ item.code }}</div>
            </div>
          </div>
          <div class="text-right">
            <div class="font-num text-[14px] font-bold" :class="scoreClass(item.score)">
              {{ item.score > 0 ? '+' : '' }}{{ item.score.toFixed(1) }}
            </div>
            <div class="font-num text-[11px] text-content-muted" :title="`建议价 ${item.price.toFixed(2)}`">
              {{ currentPrice(item).toFixed(2) }}
              <span v-if="item.realtime" class="ml-1" :class="pctClass(item.realtime.pct_change)">
                {{ fmtPct(item.realtime.pct_change) }}
              </span>
            </div>
          </div>
        </div>
        <div
          v-if="focus.length === 0"
          class="col-span-full flex flex-col items-center gap-2 py-10 text-content-faint"
        >
          <CalendarClock class="h-8 w-8 opacity-30" />
          <p class="text-[12px]">暂无明日关注</p>
          <p class="text-[11px]">每个交易日 15:35 盘后自动生成（需后端保持运行）</p>
        </div>
      </div>
    </section>
  </div>
</template>
