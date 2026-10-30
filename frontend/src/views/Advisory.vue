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
  ChevronDown,
  Lightbulb,
  Star,
} from 'lucide-vue-next'

import {
  getAdvisorySummary,
  getBuyRecommendations,
  getFocusList,
  getQueueFilter,
  getSellRecommendations,
  setQueueFilter,
  type QueueFilter,
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

/** 总分展示（±100 分制，取整避免列表太密） */
function scoreText(score: number): string {
  return `${score > 0 ? '+' : ''}${score.toFixed(0)}`
}

function contribClass(contrib: number): string {
  if (contrib > 0) return 'text-up'
  if (contrib < 0) return 'text-down'
  return 'text-content-faint'
}

/** 展开的因子分项（按股票代码记录） */
const expanded = ref<Set<string>>(new Set())
function toggleDetail(code: string): void {
  const next = new Set(expanded.value)
  if (next.has(code)) next.delete(code)
  else next.add(code)
  expanded.value = next
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

// ---- 交易队列准入：大盘股过滤（默认开，总市值 > 阈值不进队列） ----
const filter = ref<QueueFilter>({
  exclude_large_cap: true,
  large_cap_yi: 100,
  excluded_count: 0,
  universe: 0,
  basis: '总市值(total_mv)',
})
const capYiInput = ref(100)
const filterSaving = ref(false)

async function loadFilter() {
  try {
    const f = await getQueueFilter()
    filter.value = f
    capYiInput.value = f.large_cap_yi
  } catch {
    // 后端未就绪时保留前端默认展示，不阻塞建议列表
  }
}

/** 保存开关/阈值；输入非法（≤0）时回退到当前生效值 */
async function saveFilter() {
  const yi = Number(capYiInput.value)
  if (!Number.isFinite(yi) || yi <= 0) {
    capYiInput.value = filter.value.large_cap_yi
    return
  }
  filterSaving.value = true
  try {
    filter.value = await setQueueFilter({
      exclude_large_cap: filter.value.exclude_large_cap,
      large_cap_yi: yi,
    })
    capYiInput.value = filter.value.large_cap_yi
  } catch {
    await loadFilter()
  } finally {
    filterSaving.value = false
  }
}

async function toggleLargeCap() {
  filter.value.exclude_large_cap = !filter.value.exclude_large_cap
  await saveFilter()
}

onMounted(() => {
  load()
  loadFilter()
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
          class="flex h-10 w-10 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-primary-dark ring-1 ring-inset ring-white/15"
        >
          <Lightbulb class="h-5 w-5 text-white" />
        </div>
        <div>
          <h2 class="text-[17px] font-semibold text-content">智能盯盘建议</h2>
          <p class="mt-0.5 text-[11px] leading-relaxed text-content-faint">
            第 2 次排序：自选股按 14 因子加权评分（±100 分）排名，总分 ≥ +15 买入、≤ -15 卖出；
            建议产生后由模拟盘按评分加权仓位实时撮合。盘后 15:35 生成次日关注，收盘评分 Top
            {{ summary?.auto_watch_top_n ?? 10 }} 自动加入自选
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

    <!-- 交易队列准入：大盘股过滤（默认剔除，关闭后才允许大市值票进队列） -->
    <div class="glass-card flex flex-wrap items-center gap-3 px-4 py-2">
      <button
        type="button"
        class="flex items-center gap-2 disabled:opacity-60"
        :disabled="filterSaving"
        @click="toggleLargeCap"
      >
        <span
          class="relative h-4 w-8 shrink-0 rounded-full transition-colors"
          :class="filter.exclude_large_cap ? 'bg-primary' : 'bg-base-600'"
        >
          <span
            class="absolute top-0.5 h-3 w-3 rounded-full bg-white transition-all"
            :class="filter.exclude_large_cap ? 'left-[18px]' : 'left-0.5'"
          ></span>
        </span>
        <span
          class="text-[12px] font-medium"
          :class="filter.exclude_large_cap ? 'text-primary' : 'text-content-muted'"
        >
          排除大盘股
        </span>
      </button>

      <label class="flex items-center gap-1 text-[11px] text-content-muted">
        {{ filter.basis }} &gt;
        <input
          v-model.number="capYiInput"
          type="number"
          min="1"
          step="10"
          class="h-6 w-[64px] rounded border border-base-600 bg-base-700 px-1 text-right font-num text-[12px] text-content outline-none focus:border-primary"
          @keyup.enter="saveFilter"
          @blur="saveFilter"
        />
        亿 不进队列
      </label>

      <span
        v-if="filter.exclude_large_cap"
        class="rounded border border-base-600 px-1.5 py-0.5 font-num text-[10px] text-content-faint"
      >
        挡掉 {{ filter.excluded_count }} / {{ filter.universe }} 只
      </span>

      <span class="ml-auto text-[10px] text-content-faint">
        {{
          filter.exclude_large_cap
            ? '银行/石油等大市值票不进建议与队列'
            : '已放开：大市值票可以进入队列'
        }}
      </span>
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
                <span
                  v-if="item.rank"
                  class="rounded border border-up/40 bg-up/10 px-1.5 py-0.5 font-num text-[10px] font-semibold text-up"
                  title="第 2 次排序名次"
                >
                  #{{ item.rank }}
                </span>
                <span class="text-[14px] font-semibold text-content">{{ item.name }}</span>
                <span class="font-num text-[11px] text-content-faint">{{ item.code }}</span>
              </div>
              <span
                class="font-num text-[15px] font-bold"
                :class="scoreClass(item.score)"
                :title="`综合评分 ${item.score.toFixed(2)}（买卖阈值 ±15）`"
              >
                {{ scoreText(item.score) }}
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
            <div v-if="item.detail && item.detail.length > 0" class="mt-2">
              <button
                class="flex items-center gap-1 text-[11px] text-content-faint transition-colors hover:text-content"
                @click.stop="toggleDetail(item.code)"
              >
                <ChevronDown
                  class="h-3 w-3 transition-transform"
                  :class="expanded.has(item.code) ? 'rotate-180' : ''"
                />
                因子分项 {{ item.detail.length }} 项
              </button>
              <div
                v-if="expanded.has(item.code)"
                class="mt-1.5 space-y-1 rounded-lg bg-base-800/70 p-2"
              >
                <div
                  v-for="f in item.detail"
                  :key="f.key"
                  class="flex items-center justify-between gap-2 text-[11px]"
                >
                  <span class="text-content-muted">
                    <span class="text-content-faint">{{ f.group }} ·</span> {{ f.label }}
                    <span class="text-content-faint">（权重 {{ f.weight.toFixed(0) }}）</span>
                  </span>
                  <span class="flex items-center gap-2 font-num">
                    <span class="text-content-faint">{{ f.detail }}</span>
                    <span class="w-10 text-right" :class="contribClass(f.contrib)">
                      {{ f.contrib > 0 ? '+' : '' }}{{ f.contrib.toFixed(1) }}
                    </span>
                  </span>
                </div>
              </div>
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
                <span
                  v-if="item.rank"
                  class="rounded border border-down/40 bg-down/10 px-1.5 py-0.5 font-num text-[10px] font-semibold text-down"
                  title="第 2 次排序名次"
                >
                  #{{ item.rank }}
                </span>
                <span class="text-[14px] font-semibold text-content">{{ item.name }}</span>
                <span class="font-num text-[11px] text-content-faint">{{ item.code }}</span>
              </div>
              <span
                class="font-num text-[15px] font-bold"
                :class="scoreClass(item.score)"
                :title="`综合评分 ${item.score.toFixed(2)}（买卖阈值 ±15）`"
              >
                {{ scoreText(item.score) }}
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
            <div v-if="item.detail && item.detail.length > 0" class="mt-2">
              <button
                class="flex items-center gap-1 text-[11px] text-content-faint transition-colors hover:text-content"
                @click.stop="toggleDetail(item.code)"
              >
                <ChevronDown
                  class="h-3 w-3 transition-transform"
                  :class="expanded.has(item.code) ? 'rotate-180' : ''"
                />
                因子分项 {{ item.detail.length }} 项
              </button>
              <div
                v-if="expanded.has(item.code)"
                class="mt-1.5 space-y-1 rounded-lg bg-base-800/70 p-2"
              >
                <div
                  v-for="f in item.detail"
                  :key="f.key"
                  class="flex items-center justify-between gap-2 text-[11px]"
                >
                  <span class="text-content-muted">
                    <span class="text-content-faint">{{ f.group }} ·</span> {{ f.label }}
                    <span class="text-content-faint">（权重 {{ f.weight.toFixed(0) }}）</span>
                  </span>
                  <span class="flex items-center gap-2 font-num">
                    <span class="text-content-faint">{{ f.detail }}</span>
                    <span class="w-10 text-right" :class="contribClass(f.contrib)">
                      {{ f.contrib > 0 ? '+' : '' }}{{ f.contrib.toFixed(1) }}
                    </span>
                  </span>
                </div>
              </div>
            </div>
          </div>
          <div v-if="sells.length === 0" class="flex flex-col items-center gap-2 py-10 text-content-faint">
            <ArrowDownCircle class="h-8 w-8 opacity-30" />
            <p class="text-[12px]">暂无卖出建议</p>
            <p class="text-[11px]">自选股综合评分 ≤ -15（趋势/动量转弱）时出现在此列表</p>
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
