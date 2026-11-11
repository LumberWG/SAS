<script setup lang="ts">
/**
 * 默认布局：左侧职能导航 + 顶栏（面包屑 + 市场状态灯 + 时钟 + 数据口径）。
 *
 * 视觉约定：侧栏/顶栏用半透明底色 + 1px 边框，不用发光阴影；导航激活态由
 * 「左侧 3px 色条 + 略亮的底色 + 主色图标 + 右侧圆点」四重提示，hover 只提亮文字。
 * 分组标题带延伸细线与序号，形成清晰的层级节奏。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { DEGRADE_NOTICE_MS, POLL } from '../lib/polling'
import { useRoute, useRouter } from 'vue-router'
import {
  Activity,
  ChevronRight,
  FlaskConical,
  LayoutDashboard,
  Menu,
  Radar,
  ScanSearch,
  Server,
  ShieldAlert,
  SlidersHorizontal,
  Target,
  Wallet,
} from 'lucide-vue-next'

import { navProgress } from '../composables/navProgress'
import { useMarketStore } from '../stores/market'
import {
  getDataStatus,
  getMlStatus,
  getScopeStats,
  onDataDegraded,
  type DataStatus,
  type MlStatus,
  type ScopeStats,
} from '../api'

const route = useRoute()
const router = useRouter()
const market = useMarketStore()
const scopeStats = ref<ScopeStats | null>(null)
// #181：可交易范围按板块真实拆分展示，不再把「非创业板」笼统叫作主板
const BOARD_ZH: Record<string, string> = {
  sh_main: '沪主', sz_main: '深主', cyb: '创业板', star: '科创板', bse: '北交所',
}
const scopeBoardText = computed(() => {
  const s = scopeStats.value
  if (!s) return ''
  if (!s.by_board) return `创业板 ${s.cyb}`
  const parts = Object.entries(s.by_board)
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${BOARD_ZH[k] ?? k} ${n}`)
  return parts.length ? parts.join(' / ') : `创业板 ${s.cyb}`
})
// 选股板块与交易板块不一致时（当前默认就是），顶栏给出可点的提示
const scopeWarning = computed(() => {
  const bc = scopeStats.value?.board_consistency
  return bc && !bc.ok ? bc.message : ''
})
// 手机（经 cpolar 公网访问）没有 224px 的横向空间：侧栏默认收起，点汉堡展开
const sidebarOpen = ref(false)
// 手机顶栏放不下 4 个状态灯，收进一个可点开的状态面板 —— 信息不能整块藏掉
const statusPanelOpen = ref(false)
watch(() => route.path, () => {
  sidebarOpen.value = false
  statusPanelOpen.value = false
})
// 抽屉展开时锁住背景滚动，否则手指在遮罩上滑动会滚到页面背后
watch(sidebarOpen, (open) => { document.body.style.overflow = open ? 'hidden' : '' })
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    sidebarOpen.value = false
    statusPanelOpen.value = false
  }
}

// 导航按职能划分（而非内部流程阶段）：工作台 → 投研分析 → 交易执行 → 系统管理。
// 业务上的「五段式」顺序仍完整保留在投研分析组内（选股 → 信号 → 建议 → 回测）。
const navGroups = [
  {
    title: '工作台',
    items: [
      { path: '/dashboard', title: '市场总览', icon: LayoutDashboard },
    ],
  },
  {
    title: '投研分析',
    items: [
      { path: '/screening', title: '全市场选股', icon: ScanSearch },
      { path: '/signals', title: '信号与自选', icon: Radar },
      { path: '/advisory', title: '决策建议', icon: Target },
      { path: '/backtest', title: '策略回测', icon: FlaskConical },
    ],
  },
  {
    title: '交易执行',
    items: [
      { path: '/paper', title: '模拟交易', icon: Wallet },
    ],
  },
  {
    title: '系统管理',
    items: [
      { path: '/ops', title: '运行监控', icon: Server },
      { path: '/settings', title: '系统设置', icon: SlidersHorizontal },
    ],
  },
]

// 顶栏时钟
const now = ref('')
// 市场阶段：未开盘 / 集合竞价 / 交易中 / 午间休市 / 已收盘 / 休市
const phase = ref('')
let timer: number | undefined
let dataTimer: number | undefined
let offDegraded: (() => void) | undefined

// 全局数据新鲜度：SWR 会静默返回旧快照，接口 200 也可能带 degraded=true。
// 状态条独立于任何页面，30s 轮询一次（refresh=true 绕过 GET 缓存）。
const dataStatus = ref<DataStatus | null>(null)
const mlStatus = ref<MlStatus | null>(null)
const degradeNotice = ref('')
let degradeAt = 0
/** 状态接口本身挂了：不能把「取不到」显示成「待运行」 */
const statusUnavailable = ref(false)

async function refreshDataStatus() {
  // 两个状态接口互相独立：任一挂掉都不影响另一个（更不能假装正常）
  const [s, m] = await Promise.all([
    getDataStatus().catch(() => null),
    getMlStatus().catch(() => null),
  ])
  dataStatus.value = s
  mlStatus.value = m
  statusUnavailable.value = !s || !m
  // 降级提示保留至少 60s，避免一次抖动就闪一下消失
  if (s?.level === 'ok' && Date.now() - degradeAt > DEGRADE_NOTICE_MS) degradeNotice.value = ''
}

const mlState = computed(() => mlStatus.value?.state ?? 'pending')
const mlChipClass = computed(() => ({
  effective: 'chip-up', pending: 'chip-muted', fallback: 'chip-warn', off: 'chip-muted',
}[mlState.value]))
const mlChipText = computed(() => {
  // 接口挂了不能显示成「待运行」——那是另一种完全不同的状态
  if (statusUnavailable.value && !mlStatus.value) return '状态未知'
  return {
    effective: '生效', pending: '待运行', fallback: '14因子', off: '关',
  }[mlState.value]
})
const mlTitle = computed(() => {
  const s = mlStatus.value
  if (!s) return '线上 ML 状态不可用'
  return [
    s.note,
    `开关：${s.enabled ? '开' : '关'} · 判定：${s.outcome} · 状态：${s.state}`,
    `数据日：${s.as_of ?? '—'} · 覆盖 ${s.codes} 只 · OOS rank-IC：${s.oos_rank_ic ?? '—'}`,
    `最近判定：${s.at ?? '—'} · 缓存 ${s.cache_days} 个数据日`,
    'effective=false 时结果与 14 因子基线相同，不能当作 ML 有效的证据',
  ].join('\n')
})

const dataLevel = computed(() => {
  if (degradeNotice.value && dataStatus.value?.level !== 'error') return 'warn'
  return dataStatus.value?.level ?? 'unknown'
})
const dataChipClass = computed(() => ({
  ok: 'chip-up', warn: 'chip-warn', error: 'chip-down', unknown: 'chip-muted',
}[dataLevel.value]))
const dataDotClass = computed(() => ({
  ok: 'bg-up', warn: 'bg-warn', error: 'bg-down', unknown: 'bg-content-faint',
}[dataLevel.value]))
const dataChipText = computed(() => {
  const s = dataStatus.value
  if (!s) return '数据状态未知'
  if (dataLevel.value === 'error') return '行情数据不可用'
  if (dataLevel.value === 'warn') {
    const age = s.snapshot.age_seconds
    return age == null ? '行情数据陈旧' : `数据陈旧 ${Math.round(age)}s`
  }
  return `数据 ${(s.snapshot.updated_at ?? s.server_time).slice(11, 19)}`
})
const dataTitle = computed(() => {
  const s = dataStatus.value
  if (!s) return '数据状态接口不可用，无法判断页面数据是否新鲜'
  const fails = Object.keys(s.recent_failures).join('、') || '无'
  const minute = s.minute_export.available
    ? `${s.minute_export.latest_date ?? '—'}（导出于 ${s.minute_export.exported_at ?? '—'}）`
    : '不可用'
  return [
    `状态：${s.message}`,
    `快照 ${s.snapshot.rows} 只 · 更新于 ${s.snapshot.updated_at ?? '—'} · 已过 ${s.snapshot.age_seconds ?? '—'}s（TTL ${s.ttl_seconds}s）`,
    `指数 ${s.indices.rows} 条 · 更新于 ${s.indices.updated_at ?? '—'}`,
    `后台刷新中：${s.refreshing.join('、') || '无'} · 近期失败：${fails}`,
    `分钟库最新：${minute} · 交易时段：${s.trading ? '是' : '否'}`,
    degradeNotice.value ? `降级上报：${degradeNotice.value}` : '',
  ].filter(Boolean).join('\n')
})
const bannerText = computed(() => {
  if (dataLevel.value === 'ok' || dataLevel.value === 'unknown') return ''
  return degradeNotice.value || dataStatus.value?.message || '行情数据异常，页面可能显示旧数据'
})

function computePhase(d: Date): string {
  const wd = d.getDay()
  if (wd === 0 || wd === 6) return '休市'
  const m = d.getHours() * 60 + d.getMinutes()
  if (m < 9 * 60 + 15) return '未开盘'
  if (m < 9 * 60 + 30) return '集合竞价'
  if (m <= 11 * 60 + 30) return '交易中'
  if (m < 13 * 60) return '午间休市'
  if (m <= 15 * 60) return '交易中'
  return '已收盘'
}

function tick() {
  const d = new Date()
  now.value = d.toLocaleTimeString('zh-CN', { hour12: false })
  phase.value = computePhase(d)
}

// 悬停/聚焦导航项即定向预取该路由 chunk：比空闲全量预取更确定——
// 鼠标移到「决策建议」上，点下去时它的包已经在内存里，切页零等待。
function prefetchRoute(path: string) {
  const r = router.getRoutes().find((x) => x.path === path)
  const comp = (r?.components?.default ?? (r as { component?: unknown })?.component) as
    | (() => Promise<unknown>)
    | undefined
  if (typeof comp === 'function') {
    try {
      comp()
    } catch {
      /* 预取失败静默，不影响正常导航 */
    }
  }
}

onMounted(() => {
  tick()
  timer = window.setInterval(tick, POLL.clockMs)
  market.fetchAnalysisStatus()
  getScopeStats().then((d) => (scopeStats.value = d)).catch(() => {})
  refreshDataStatus()
  dataTimer = window.setInterval(refreshDataStatus, POLL.defaultMs)
  offDegraded = onDataDegraded((e) => {
    degradeAt = Date.now()
    degradeNotice.value = e.message || `接口 ${e.path} 返回降级数据`
  })
  window.addEventListener('keydown', onKeydown)

  // 空闲时预取其余路由 chunk（含 echarts 706KB），使首次进入新页面零网络等待。
  // 本地/内网部署带宽充足，收益明显；用 requestIdleCallback 避免抢占首屏交互。
  const prefetchRoutes = () => {
    router.getRoutes().forEach((r) => {
      const comp = (r.components?.default ?? (r as { component?: unknown }).component) as
        | (() => Promise<unknown>)
        | undefined
      if (typeof comp === 'function') {
        try {
          comp()
        } catch {
          /* 预取失败静默，不影响正常导航 */
        }
      }
    })
  }
  const ric = (window as unknown as { requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => void })
    .requestIdleCallback
  if (typeof ric === 'function') ric(prefetchRoutes, { timeout: 3000 })
  else window.setTimeout(prefetchRoutes, 1500)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
  if (dataTimer) window.clearInterval(dataTimer)
  offDegraded?.()
  window.removeEventListener('keydown', onKeydown)
  document.body.style.overflow = ''
})

const statusText = computed(() => phase.value || (market.trading ? '交易中' : '已收盘'))

// 个股详情是投研链路里的下钻页，高亮到「信号与自选」入口
const activePath = computed(() =>
  route.path.startsWith('/stock/') ? '/signals' : route.path
)
// 顶栏面包屑的分组名，与侧栏共用同一份 navGroups，避免两处维护
const currentGroup = computed(() =>
  navGroups.find((g) => g.items.some((i) => i.path === activePath.value))?.title ?? '工作台'
)
</script>

<template>
  <div class="flex h-full">
    <!-- 路由切换进度条：首次进入需下载动态 chunk 时显示，keep-alive 命中页近乎无感 -->
    <div
      v-if="navProgress.active"
      class="fixed inset-x-0 top-0 z-[100] h-[2px] overflow-hidden"
    >
      <div
        class="h-full bg-primary shadow-[0_0_6px] shadow-primary/60 transition-[width] duration-200 ease-out"
        :style="{ width: navProgress.value + '%' }"
      ></div>
    </div>
    <!-- 左侧导航（固定） -->
    <div
      v-if="sidebarOpen"
      class="fixed inset-0 z-[80] bg-black/55 lg:hidden"
      aria-hidden="true"
      @click="sidebarOpen = false"
    ></div>
    <aside
      class="fixed left-0 top-0 z-[90] flex h-full w-56 flex-col border-r border-base-600/60 bg-base-800/90 backdrop-blur-xl transition-transform duration-200 lg:translate-x-0 lg:bg-base-800/70"
      :class="sidebarOpen ? 'translate-x-0' : '-translate-x-full'"
    >
      <!-- 品牌 -->
      <div class="flex items-center gap-2.5 border-b border-base-600/60 px-5 py-[15px]">
        <div
          class="flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-gradient-to-br from-primary to-primary-dark ring-1 ring-inset ring-white/20"
        >
          <Activity class="h-[18px] w-[18px] text-white" :stroke-width="2.2" />
        </div>
        <div class="min-w-0">
          <div class="text-[16px] font-semibold leading-tight tracking-[0.08em] text-content">SAS</div>
          <div class="truncate text-[11.5px] leading-tight text-content-faint">智能投研终端</div>
        </div>
      </div>

      <nav class="flex-1 space-y-6 overflow-y-auto px-3 py-5">
        <div v-for="group in navGroups" :key="group.title" class="space-y-1">
          <div class="mb-2 flex items-center gap-2 px-2.5">
            <span class="eyebrow shrink-0">{{ group.title }}</span>
            <span class="h-px flex-1 bg-base-600/60"></span>
            <span class="font-num text-[10.5px] leading-none text-content-faint">{{ String(group.items.length).padStart(2, '0') }}</span>
          </div>
          <router-link
            v-for="item in group.items"
            :key="item.path"
            :to="item.path"
            class="group relative flex items-center gap-2.5 rounded-control px-2.5 py-[9px] text-[13.5px] font-medium transition-colors duration-150"
            @click="sidebarOpen = false"
            @mouseenter="prefetchRoute(item.path)"
            @focus="prefetchRoute(item.path)"
            :class="
              activePath === item.path
                ? 'bg-primary/[0.12] text-content'
                : 'text-content-muted hover:bg-base-700/60 hover:text-content'
            "
          >
            <span
              class="absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-full transition-colors"
              :class="activePath === item.path ? 'bg-primary' : 'bg-transparent'"
            ></span>
            <component
              :is="item.icon"
              class="h-4 w-4 shrink-0 transition-colors"
              :class="activePath === item.path ? 'text-primary-light' : 'text-content-faint group-hover:text-content-muted'"
            />
            <span class="truncate">{{ item.title }}</span>
            <span
              v-if="activePath === item.path"
              class="ml-auto h-1 w-1 shrink-0 rounded-full bg-primary"
            ></span>
          </router-link>
        </div>
      </nav>

      <!-- 侧栏底部：仅保留运行环境标识（免责声明移到主内容区底部统一呈现，避免多处重复） -->
      <div class="border-t border-base-600/60 px-3 py-3">
        <div class="flex items-center justify-between px-1">
          <span class="inline-flex items-center gap-1.5 text-[10.5px] font-medium tracking-[0.1em] text-content-faint">
            <span class="h-1.5 w-1.5 rounded-full bg-warn/70"></span>
            PAPER MODE
          </span>
          <span class="font-num text-[10.5px] tracking-wide text-content-faint">SIM</span>
        </div>
      </div>
    </aside>

    <!-- 主内容区（为固定侧栏留出宽度） -->
    <div class="flex h-full flex-1 flex-col lg:ml-56">
      <!-- 顶栏（固定） -->
      <header
        class="fixed left-0 right-0 top-0 z-30 flex h-14 items-center justify-between gap-2 border-b border-base-600/60 bg-base-900/85 px-3 backdrop-blur-xl sm:px-5 lg:left-56"
      >
        <div class="flex min-w-0 items-center gap-2 sm:gap-3">
          <button
            class="btn btn-ghost btn-sm shrink-0 px-2 lg:hidden"
            aria-label="切换导航"
            :aria-expanded="sidebarOpen"
            @click="sidebarOpen = !sidebarOpen"
          >
            <Menu class="h-4 w-4" />
          </button>
          <div class="hidden min-w-0 items-center gap-1.5 sm:flex">
            <span class="shrink-0 text-[12.5px] text-content-faint">{{ currentGroup }}</span>
            <ChevronRight class="h-3.5 w-3.5 shrink-0 text-content-faint/60" />
          </div>
          <h1 class="min-w-0 truncate text-subheading font-semibold tracking-tight">{{ route.meta.title ?? '市场总览' }}</h1>
          <span
            class="chip shrink-0"
            :class="market.trading ? 'chip-up' : 'chip-muted'"
          >
            <span
              class="h-1.5 w-1.5 rounded-full"
              :class="market.trading ? 'animate-pulse-slow bg-up' : 'bg-content-faint'"
            />
            <span class="hidden xs:inline">{{ statusText }}</span>
          </span>
        </div>
        <div class="flex shrink-0 items-center gap-2">
          <span class="font-num hidden text-[14px] text-content-muted sm:inline">{{ now }}</span>
          <span class="mx-1 hidden h-4 w-px bg-base-600 sm:block"></span>
          <span
            v-if="scopeStats"
            class="chip chip-muted hidden sm:inline-flex"
            :title="`可交易范围 ${scopeStats.in_scope} 只（${scopeBoardText}）`"
          >
            范围 {{ scopeStats.in_scope }} · {{ scopeBoardText }}
          </span>
          <span
            v-if="scopeWarning"
            class="chip chip-muted hidden text-warn sm:inline-flex"
            :title="scopeWarning"
          >
            ⚠ 板块不一致
          </span>
          <span
            class="chip hidden sm:inline-flex"
            :class="dataChipClass"
            :title="dataTitle"
          >
            <span class="h-1.5 w-1.5 rounded-full" :class="dataDotClass" />
            {{ dataChipText }}
          </span>
          <span
            class="chip hidden md:inline-flex"
            :class="mlChipClass"
            :title="mlTitle"
          >ML: {{ mlChipText }}</span>
          <span class="chip chip-muted hidden md:inline-flex">
            LLM: {{ market.llmAvailable ? market.llmModel : '规则降级' }}
          </span>
          <!-- 手机上 4 个状态灯被断点全隐藏了，但「看到的是不是旧数据」不能没有入口 -->
          <button
            class="btn btn-ghost btn-sm shrink-0 gap-1.5 px-2 sm:hidden"
            :aria-expanded="statusPanelOpen"
            aria-label="查看数据状态"
            @click="statusPanelOpen = !statusPanelOpen"
          >
            <span class="h-1.5 w-1.5 rounded-full" :class="dataDotClass" />
            <Activity class="h-4 w-4" />
          </button>
        </div>

        <!-- 移动端状态面板：窄屏顶栏放不下，口径收进这里而不是整块消失 -->
        <div
          v-if="statusPanelOpen"
          class="absolute left-0 right-0 top-14 z-40 space-y-2 border-b border-base-600/60 bg-base-800/95 px-3 py-3 text-[13px] shadow-pop backdrop-blur-xl sm:hidden"
        >
          <div class="flex flex-wrap items-center gap-2">
            <span class="chip" :class="dataChipClass">
              <span class="h-1.5 w-1.5 rounded-full" :class="dataDotClass" />
              {{ dataChipText }}
            </span>
            <span class="chip" :class="mlChipClass">ML: {{ mlChipText }}</span>
            <span class="chip chip-muted">市场：{{ statusText }}</span>
            <span class="chip chip-muted">LLM: {{ market.llmAvailable ? market.llmModel : '规则降级' }}</span>
          </div>
          <div v-if="statusUnavailable" class="rounded-lg border border-warn/30 bg-warn/[0.08] px-2 py-1.5 text-warn">
            部分状态接口未返回数据，下面显示的可能不是最新结果
          </div>
          <div class="text-content-muted">{{ dataStatus?.message ?? '数据状态接口不可用，无法判断是否新鲜' }}</div>
          <div class="flex flex-wrap gap-x-3 gap-y-1 text-content-faint">
            <span class="font-num">{{ now }}</span>
            <span>快照 {{ dataStatus?.snapshot.updated_at ?? '—' }}</span>
            <span v-if="scopeStats">范围 {{ scopeStats.in_scope }} · {{ scopeBoardText }}</span>
            <span v-if="scopeWarning" class="text-warn">{{ scopeWarning }}</span>
          </div>
          <div class="text-content-faint">{{ mlStatus?.note ?? 'ML 状态不可用' }}</div>
          <button class="btn btn-ghost btn-sm w-full" @click="refreshDataStatus()">
            重新检查（保持面板打开）
          </button>
        </div>
      </header>

      <!-- 页面内容（顶栏高度补偿） -->
      <main class="flex-1 overflow-y-auto pt-14">
        <div class="app-main mx-auto max-w-[1560px] animate-fade-in px-3 py-4 sm:px-5 sm:py-5 xl:px-6">
          <!-- 数据降级/陈旧：全局横幅，别让「200 但给的是旧数据」静默过去 -->
          <div
            v-if="bannerText"
            class="mb-4 flex items-start gap-2 rounded-lg border px-3 py-2 text-[12.5px] leading-relaxed"
            :class="dataLevel === 'error'
              ? 'border-down/30 bg-down/[0.08] text-down'
              : 'border-warn/30 bg-warn/[0.08] text-warn'"
          >
            <ShieldAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <div class="min-w-0 flex-1">
              <span class="font-medium">{{ bannerText }}</span>
              <span class="ml-1.5 opacity-80">页面可能显示旧数据，勿当作实时行情；以顶栏状态条的时间为准。</span>
            </div>
            <button
              class="shrink-0 rounded border border-current/30 px-2 py-0.5 text-[12px] opacity-80 hover:opacity-100"
              @click="refreshDataStatus"
            >
              重新检查
            </button>
          </div>
          <router-view v-slot="{ Component, route }">
            <!-- 去掉 out-in 淡入淡出（约 0.44s 的「先出后进」空档是切换卡顿的主因），
                 仅保留 keep-alive：命中缓存的页面瞬时恢复，不再重拉数据/重建定时器。
                 key 用 route.path（而非 fullPath）：模拟交易 ?tab=pool/account 等 query 变化
                 不再触发整页重建，tab 切换由组件内 watch 处理。 -->
            <keep-alive :max="10">
              <component :is="Component" :key="route.path" />
            </keep-alive>
          </router-view>
        </div>
      </main>

      <!-- 全局免责声明：全站仅此一处，置于主内容区底部、靠右轻量展示 -->
      <footer
        class="shrink-0 border-t border-base-600/40 bg-base-900/60 px-3 py-1.5 text-right text-[11px] leading-tight text-content-faint sm:px-5"
      >
        {{ market.disclaimer }}
      </footer>
    </div>
  </div>
</template>
