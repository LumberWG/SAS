/**
 * API 客户端：封装后端全部接口，统一处理 disclaimer 字段。
 */
import axios from 'axios'

const http = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

// ---- GET 响应缓存（TTL）：减少页面切换 / 轮询的重复请求与后端压力 ----
// 命中且未过期时通过 axios adapter 短路返回，不再发起网络请求；
// 仅缓存 GET，且 refresh=true 的主动刷新请求跳过缓存（保证拿到最新）。
const _getCache = new Map<string, { ts: number; data: unknown }>()
const GET_CACHE_TTL = 10_000

function _cacheKey(config: { url?: string; params?: unknown }): string {
  return (config.url ?? '') + '|' + JSON.stringify(config.params ?? {})
}

http.interceptors.request.use((config) => {
  const refresh = (config.params as { refresh?: boolean } | undefined)?.refresh
  if ((config.method ?? 'get').toLowerCase() === 'get' && config.url && !refresh) {
    const hit = _getCache.get(_cacheKey(config))
    if (hit && Date.now() - hit.ts < GET_CACHE_TTL) {
      config.adapter = () =>
        Promise.resolve({
          data: hit.data,
          status: 200,
          statusText: 'OK',
          headers: {},
          config,
          request: {},
        } as import('axios').AxiosResponse)
    }
  }
  return config
})

http.interceptors.response.use((response) => {
  const cfg = response.config
  const method = (cfg.method ?? 'get').toLowerCase()
  // 写操作（切换模式/下单/撮合/重置）会让之前的 GET 快照全部过期：
  // 若不清缓存，紧随其后的 load() 会读回旧数据，页面看起来「切换没生效」。
  if (method !== 'get') {
    _getCache.clear()
    return response
  }
  const refresh = (cfg.params as { refresh?: boolean } | undefined)?.refresh
  if (cfg.url && !refresh) {
    _getCache.set(_cacheKey(cfg), { ts: Date.now(), data: response.data })
  }
  return response
})

// 每个响应都带 disclaimer 字段（后端统一注入），页面顶栏展示

// ==================== 行情 ====================

export interface IndexQuote {
  symbol: string
  name: string
  price: number | null
  pct_change: number | null
  change: number | null
  amount: number | null
}

export interface Sentiment {
  level: string
  weight: number
  advice: string
  temperature: number
}

export interface MarketOverview {
  updated_at: string | null
  trading: boolean
  indices: IndexQuote[]
  breadth: {
    up: number
    down: number
    flat: number
    total: number
    // 未开盘/停牌（无有效成交价）的只数，不计入涨跌家数
    untraded?: number
    traded?: number
    limit_up: number
    limit_down: number
  }
  total_amount: number
  temperature: number
  sentiment: Sentiment
  // 数据源故障（东财/新浪不可达）时后端返回降级结构而非 503 全空
  degraded?: boolean
  message?: string
}

export interface KlineData {
  code: string
  dates: string[]
  kline: number[][] // [open, close, low, high]
  volumes: number[]
  amounts: (number | null)[]
  pct_changes: (number | null)[]
}

export interface IntradayData {
  code: string
  date: string | null
  prev_close: number | null
  times: string[]
  prices: (number | null)[]
  avg_prices: (number | null)[]
  volumes: number[]
}

export interface StockQuote {
  code: string
  name: string
  price: number | null
  pct_change: number | null
  change: number | null
  open: number | null
  high: number | null
  low: number | null
  prev_close: number | null
  volume: number | null
  amount: number | null
  turnover_rate: number | null
  volume_ratio: number | null
  total_mv: number | null
  float_mv: number | null
}

export function getOverview(): Promise<MarketOverview> {
  return http.get('/market/overview').then((r) => r.data)
}

export function getIndexKline(
  symbol: string,
  days = 60
): Promise<{ symbol: string; dates: string[]; closes: (number | null)[] }> {
  return http.get(`/market/indices/${symbol}/kline`, { params: { days } }).then((r) => r.data)
}

export function getStockKline(
  code: string,
  days = 120,
  refresh = false
): Promise<KlineData> {
  return http
    .get(`/market/stocks/${code}/kline`, { params: { days, refresh } })
    .then((r) => r.data)
}

export function getStockQuote(code: string): Promise<StockQuote> {
  return http.get(`/market/stocks/${code}`).then((r) => r.data)
}

/** 个股分时（逐分钟价格 / 均价 / 成交量）。 */
export function getStockIntraday(code: string): Promise<IntradayData> {
  return http.get(`/market/stocks/${code}/intraday`).then((r) => r.data)
}

// ==================== 选股 ====================

export interface ParamSpec {
  type: 'int' | 'float' | 'bool' | 'select'
  default: number | boolean | string
  label: string
  min?: number | null
  max?: number | null
  step?: number | null
  unit?: string
  options?: [string, string][] | null
}

export interface StrategyInfo {
  name: string
  description: string
  category: string
  min_bars: number
  custom: boolean
  parameters: Record<string, ParamSpec>
}

export interface StrategyDetail {
  name: string
  description: string
  category: string
  min_bars: number
  custom: boolean
  custom_meta: { base: string; params: Record<string, unknown> } | null
  parameters: Record<string, ParamSpec>
}

/**
 * 命中/信号行的当前实时行情。
 * 后端在列表接口里附带：DB/任务里存的是「记录时刻」的数据，展示用这一份。
 */
export interface HitRealtime {
  price: number
  pct_change: number | null
  volume_ratio: number | null
  amount_yi: number | null
}

export interface ScanHit {
  code: string
  name: string
  strategy: string
  price: number
  reason: string
  stop_loss: number | null
  target_price: number | null
  date: string
  extra: Record<string, unknown>
  /** 第 1 次排序名次（1 起，同股并列；历史信号来自 DB signals.rank） */
  rank?: number
  /** 技术面评分（= extra.score，落库字段） */
  score?: number | null
  /** 当前实时行情（快照不可用时缺省，前端回退到记录值） */
  realtime?: HitRealtime | null
}

export interface ScanTask {
  task_id: string
  status: 'pending' | 'running' | 'done' | 'error'
  strategies: string[]
  scope: string
  total: number
  scanned: number
  progress: number
  skipped: number
  hit_count: number
  hits: ScanHit[]
  started_at: string
  finished_at: string
  duration: number | null
  sentiment: Sentiment | null
  error?: string
}

export interface ScanScope {
  key: string
  label: string
}

export function getStrategies(): Promise<{
  strategies: StrategyInfo[]
  today_hits: Record<string, number>
  scopes: ScanScope[]
}> {
  return http.get('/screening/strategies').then((r) => r.data)
}

export function startScan(
  strategies: string[],
  codes?: string[],
  scope = 'all'
): Promise<{ task_id: string }> {
  return http.post('/screening/scan', { strategies, codes, scope }).then((r) => r.data)
}

export function getLatestScan(): Promise<{ task: ScanTask | null }> {
  return http.get('/screening/scan/latest').then((r) => r.data)
}

export function getScanTask(taskId: string): Promise<ScanTask> {
  return http.get(`/screening/scan/${taskId}`).then((r) => r.data)
}

export interface SignalRecord {
  id: number
  code: string
  name: string
  strategy: string
  price: number
  reason: string
  stop_loss: number | null
  target_price: number | null
  scan_date: string
  created_at: string
  /** 当前实时行情（后端按快照附带） */
  realtime?: HitRealtime | null
}

export function getSignals(code?: string, limit = 100): Promise<SignalRecord[]> {
  return http.get('/screening/signals', { params: { code, limit } }).then((r) => r.data.signals)
}

// ==================== 回测 ====================

/**
 * 回测请求：固定走「全策略流水线」（扫描 → 加自选 → 14因子排序 → 评分加权下单），
 * 只有时间段是必填，其余留空即用模拟盘默认配置。
 */
export interface BacktestRequest {
  start_date: string
  end_date: string
  initial_capital?: number
  max_positions?: number
  universe_size?: number
  board_prefixes?: string[]
  watch_cap?: number
  add_per_day?: number
  rebalance_days?: number
  buy_threshold?: number
  buy_top_pct?: number
  max_open_gain?: number
  max_gain_5d?: number
  min_volume_ratio?: number
  min_turnover_rate?: number
  invert_score?: boolean
  stop_loss?: number
  take_profit?: number
  max_hold_days?: number
  stop_atr_mult?: number
  stop_atr_min?: number
  stop_atr_max?: number
  stop_confirm_close?: boolean
  partial_tp_at?: number
  partial_tp_ratio?: number
  breakeven_trigger?: number
  trail_pct?: number
  trail_arm?: number
  regime_ma?: number
  regime_fast_ma?: number
  weak_exposure?: number
  regime_flat_below?: boolean
  max_total_exposure?: number
  single_position_ratio?: number
  full_score?: number
  half_score?: number
  rotation_enabled?: boolean
  rotation_gap?: number
  rotation_min_score?: number
  // 五段式③⑤：队列容量与切入点
  queue_top_m?: number
  entry_max_chase?: number
  entry_pullback?: number
  entry_window_minutes?: number
  // 做T引擎
  t_active?: boolean
  t_size?: number
  t_up_sell?: number
  t_up_buyback?: number
  t_down_add?: number
  t_down_full?: number
  t_rebound_sell?: number
  t_limit_sell_half?: boolean
  t_crash_guard?: number
}

export interface BacktestTrade {
  code: string
  name: string
  buy_date: string
  sell_date: string
  buy_price: number
  sell_price: number
  shares: number
  pnl: number
  pnl_pct: number
  reason: string
}

export interface BacktestResult {
  run_id?: number
  strategy: string
  start_date: string
  end_date: string
  params: Record<string, unknown>
  metrics: {
    total_return: number
    benchmark_return?: number
    annualized_return: number
    max_drawdown: number
    sharpe: number
    trade_count: number
    win_rate: number
    avg_pnl_pct?: number
    final_equity: number
    trading_days: number
    open_positions?: number
  }
  equity_curve: { date: string; equity: number; benchmark?: number }[]
  trades: BacktestTrade[]
  review?: BacktestReview
}

/** 归因分组统计（卖出原因 / 评分档 / 持有天数） */
export interface BacktestGroup {
  name: string
  count: number
  pnl: number
  avg_pnl_pct: number
  win_rate: number
}

/** 复盘用到的精简成交行 */
export interface BacktestReviewTrade {
  code: string
  name: string
  buy_date: string
  sell_date: string
  pnl_pct: number
  pnl: number
  reason: string
  score?: number
}

/** 参数调整建议 */
export interface BacktestSuggestion {
  target: string
  current: string | number
  suggest: string | number
  reason: string
  priority: string
}

/** 回测复盘：亏损归因 + 调参建议（由后端 app/backtest/review.py 生成） */
export interface BacktestReview {
  summary: string
  reasons: BacktestGroup[]
  score_buckets: BacktestGroup[]
  hold_buckets: BacktestGroup[]
  avg_hold_days: number
  cost: { fee_total: number; pnl_total: number; fee_vs_loss_pct: number }
  extremes: { worst: BacktestReviewTrade[]; best: BacktestReviewTrade[] }
  suggestions: BacktestSuggestion[]
}

/** 后端回测任务状态（提交后轮询，避免长请求撞上 30s 超时）。 */
interface BacktestJob {
  job_id?: string
  status: 'running' | 'done' | 'error'
  elapsed?: number
  error?: string
  result?: BacktestResult
}

/** 轮询间隔（毫秒）与最长等待（毫秒） */
const BACKTEST_POLL_MS = 1500
const BACKTEST_TIMEOUT_MS = 15 * 60 * 1000

/**
 * 提交回测并轮询结果。
 *
 * 回测要逐只股票逐日滚动评估策略，几百只 × 一年常需 1~3 分钟，
 * 单个长请求会被浏览器/代理超时掐断，所以后端改为任务化。
 */
export async function runBacktest(req: BacktestRequest): Promise<BacktestResult> {
  const { data } = await http.post<BacktestJob>('/backtest/run', req, { timeout: 0 })
  // 兼容旧后端：仍返回完整结果时直接使用
  if (!data.job_id) return data as unknown as BacktestResult

  const deadline = Date.now() + BACKTEST_TIMEOUT_MS
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, BACKTEST_POLL_MS))
    const res = await http.get<BacktestJob>(`/backtest/jobs/${data.job_id}`, {
      timeout: 0,
      // refresh 绕过 GET 缓存，否则轮询会一直读到第一次的 running 状态
      params: { refresh: true },
    })
    if (res.data.status === 'done' && res.data.result) return res.data.result
    if (res.data.status === 'error') throw new Error(res.data.error || '回测失败')
  }
  throw new Error('回测超时（15 分钟），请缩小股票池或时间区间后重试')
}

export function getBacktest(runId: number): Promise<BacktestResult> {
  return http.get(`/backtest/${runId}`).then((r) => r.data)
}

/** 系统设置总览：一项参数的当前生效值 + 来源 + 生效方式。 */
export interface ParamItem {
  key: string
  label: string
  value: string | number | boolean | null
  type: 'bool' | 'int' | 'float' | 'str'
  /** 值从哪来：app_state 覆盖 / 环境变量(.env) / 代码默认 / 硬编码常量 */
  source: 'app_state' | 'env' | 'default' | 'code'
  /** 改了是否必须重启后端才生效 */
  restart_required: boolean
  /** 是否允许页面修改（路径/列表类为 false） */
  writable: boolean
  /** 渲染控件：bool 开关 / number 数字框 / text 文本框 / readonly 只读 */
  kind: 'bool' | 'number' | 'text' | 'readonly'
  runtime: boolean
  /** 完整说明（来自 config.py 注释），只放在 tooltip 里 */
  desc?: string
  note: string
}

/** 组内小节：页面上把一个 tab 再切成语义小节，避免一组 50 项堆成一片 */
export interface ParamSubgroup {
  name: string
  desc: string
  items: ParamItem[]
}

/** 分组：左侧导航的一项；`items` 是小节的扁平串（搜索/计数用） */
export interface ParamGroup {
  name: string
  desc: string
  items: ParamItem[]
  subgroups: ParamSubgroup[]
}

export interface SettingsOverview {
  /** 只含**可编辑**参数；路径/列表类与硬编码常量不进清单（改不了就不展示） */
  groups: ParamGroup[]
  total: number
  runtime_overrides: number
  hint: string
}

export function getSettingsOverview(): Promise<SettingsOverview> {
  return http.get('/settings').then((r) => r.data)
}

/** 修改配置项：立即生效并落库（重启保留）。返回修改后的总览。 */
export function updateSettings(
  changes: Record<string, string | number | boolean>,
): Promise<SettingsOverview & { applied: Record<string, unknown> }> {
  return http.post('/settings', { changes }).then((r) => r.data)
}

/** 还原为 .env/环境变量值；keys 为空 = 还原全部可编辑项。 */
export function resetSettings(keys: string[] = []): Promise<SettingsOverview & { reset: string[] }> {
  return http.post('/settings/reset', { keys }).then((r) => r.data)
}

/** 交易队列的大盘股过滤设置（「③ 三选 · 交易队列」页开关）。 */
export interface QueueFilter {
  exclude_large_cap: boolean
  large_cap_yi: number
  /** 当前阈值下会被挡掉的股票数（全市场口径，含不可交易的板块） */
  excluded_count: number
  universe: number
  basis: string
}

export function getQueueFilter(): Promise<QueueFilter> {
  return http.get('/paper/queue-filter').then((r) => r.data)
}

export function setQueueFilter(payload: {
  exclude_large_cap: boolean
  large_cap_yi: number
}): Promise<QueueFilter> {
  return http.post('/paper/queue-filter', payload).then((r) => r.data)
}

/**
 * 回测参数的线上默认值（具体数字）。
 *
 * 后端把 `Settings.paper_*` 与 pipeline 里的常量统一算好返回，前端不再抄一份；
 * 约定「关闭/不限」的数值项返回 0，提交时值为 0 的字段会被略过（等价于 None）。
 */
export function getBacktestDefaults(): Promise<Record<string, number | boolean | string[]>> {
  return http.get('/backtest/defaults').then((r) => r.data.defaults)
}

/**
 * 回测参数 → 实盘配置的落盘映射。
 *
 * `mapping` 是能同步的项（回测参数 → Settings 字段），`skipped` 是实盘没有对应实现
 * 的项及原因，`current` 是这些字段当前生效值（前端用来算「落盘会改哪几项」）。
 */
export interface BacktestPersistMap {
  mapping: Record<string, string>
  skipped: Record<string, string>
  current: Record<string, number | boolean | string>
}

export function getBacktestPersistMap(): Promise<BacktestPersistMap> {
  return http.get('/backtest/persist-map').then((r) => r.data)
}

/** 落盘结果：写了哪些项、其中哪些真的变了、哪些跳过 */
export interface BacktestPersistResult {
  applied: Record<string, unknown>
  changed: { setting: string; old: unknown; new: unknown }[]
  skipped: { key: string; reason: string }[]
  /** 落盘后的默认值（前端直接用作新基线，颜色高亮归零） */
  defaults: Record<string, number | boolean | string[]>
}

/**
 * 将回测参数落盘为**实盘默认值**：写配置覆盖层 + 立即生效，重启保留。
 *
 * 只同步有实盘落点的子集（见 `getBacktestPersistMap`），其余项由后端返回原因。
 */
export function persistBacktestRules(req: BacktestRequest): Promise<BacktestPersistResult> {
  return http.post('/backtest/persist', req).then((r) => r.data)
}

// ==================== 监控 / 异动 ====================

export interface MarketAlert {
  type: string
  code: string
  name: string
  price: number | null
  pct_change: number
  volume_ratio: number | null
  time: string
}

export function getAlerts(
  limit = 30
): Promise<{
  alerts: MarketAlert[]
  trading_hours: boolean
  trading_day: boolean
}> {
  return http.get('/monitor/alerts', { params: { limit } }).then((r) => r.data)
}

// ==================== 策略管理（参数编辑 / 自定义策略） ====================

export function createCustomStrategy(payload: {
  name: string
  base_strategy: string
  params: Record<string, unknown>
  description?: string
}): Promise<{ created: boolean; name: string }> {
  return http.post('/strategies/custom', payload).then((r) => r.data)
}

export function deleteCustomStrategy(name: string): Promise<{ deleted: boolean }> {
  return http.delete(`/strategies/custom/${encodeURIComponent(name)}`).then((r) => r.data)
}

// ==================== 智能建议 ====================

export interface RecommendationItem {
  id: number
  code: string
  name: string
  action: 'buy' | 'sell' | 'hold' | 'focus'
  score: number
  reason: string
  price: number
  stop_loss: number | null
  target_price: number | null
  sentiment: string
  /** 第 2 次排序名次（1 起，0=未参与排序） */
  rank?: number
  /** 因子分项明细（第 2 次排序打分依据） */
  detail?: RecommendationFactor[] | null
  batch: string
  created_at: string
  /** 当前实时行情（price 为建议生成价，展示以本字段为准） */
  realtime?: HitRealtime | null
}

/** 单因子评分明细 */
export interface RecommendationFactor {
  key: string
  label: string
  group: string
  weight: number
  /** 归一化得分 [-1, 1] */
  sub: number
  /** 对总分的贡献（weight × sub） */
  contrib: number
  detail: string
}

export function getBuyRecommendations(): Promise<{
  items: RecommendationItem[]
  batch: string | null
  updated_at: string | null
}> {
  return http.get('/advisory/buy').then((r) => r.data)
}

export function getSellRecommendations(): Promise<{
  items: RecommendationItem[]
  batch: string | null
  updated_at: string | null
}> {
  return http.get('/advisory/sell').then((r) => r.data)
}

export function getFocusList(): Promise<{
  items: RecommendationItem[]
  batch: string | null
  for_date: string | null
  updated_at: string | null
}> {
  return http.get('/advisory/focus').then((r) => r.data)
}

export function getAdvisorySummary(): Promise<{
  buy_count: number
  sell_count: number
  focus_count: number
  focus_batch: string | null
  trading_hours: boolean
  trading_day: boolean
  auto_watch_enabled: boolean
  auto_watch_top_n: number
}> {
  return http.get('/advisory/summary').then((r) => r.data)
}

// ==================== 模拟交易 ====================

export interface PaperAccount {
  initial_capital: number
  cash: number
  market_value: number
  total_asset: number
  total_pnl: number
  return_pct: number
  position_count: number
  positions: Array<{
    code: string
    name: string
    shares: number
    cost_price: number
    price: number
    market_value: number
    pnl: number
    pnl_pct: number
    buy_date: string
    strategy: string
  }>
}

export interface PaperTrade {
  id: number
  code: string
  name: string
  side: 'buy' | 'sell'
  price: number
  shares: number
  amount: number
  fee: number
  reason: string
  trade_date: string
  created_at: string
}

export function getPaperAccount(): Promise<PaperAccount> {
  return http.get('/paper/account').then((r) => r.data)
}

/** 待执行的实时交易信号（下个 tick 成交） */
export interface PaperPendingSignal {
  code: string
  name: string
  action: 'buy' | 'sell'
  reason: string
  created_at: string
  expire_at: string
  waiting_seconds: number
  price_at_signal: number | null
  /** 排序分（第 3 次排序按评分降序执行、加权仓位） */
  score?: number
}

export interface PaperLiveTrade {
  code: string
  name: string
  side: 'buy' | 'sell'
  shares: number
  price: number
  fee: number
  reason: string
  time: string
}

/** 实时撮合引擎状态 */
export interface PaperLiveStatus {
  enabled: boolean
  engine_enabled: boolean
  /** true=自动交易（系统撮合，人工不干预）；false=手动交易 */
  auto_trade: boolean
  mode: 'auto' | 'manual'
  trading: boolean
  tick_interval: number
  signal_ttl_minutes: number
  last_tick: string | null
  last_tick_at: string | null
  ticks: number
  executed: number
  pending: PaperPendingSignal[]
  /** 候补队列：因满仓/资金不足没买成、等名额释放的高分票（按评分降序，最多 10 条） */
  backlog?: PaperPendingSignal[]
  backlog_count?: number
  /** 五段式③：交易队列买入容量上限（0=不限制） */
  queue_top_m?: number
  /** 因队列容量未入队的买入候选（最近一批分析） */
  queue_rejected?: Array<{ code: string; name: string; score: number; reason: string }>
  /** 五段式⑤：切入点参数（不追高比例 / 回踩要求 / 超时分钟） */
  entry_max_chase?: number
  entry_pullback?: number
  entry_timeout_minutes?: number
  /** 本轮被判定规则/切入点拒掉的买入信号 */
  entry_rejected?: Array<{ code: string; name: string; reason: string }>
  recent_trades: PaperLiveTrade[]
  last_error: string
  prices: Record<string, number>
}

export function getPaperLive(): Promise<PaperLiveStatus> {
  // 引擎状态（模式/报价/信号）属实时数据，跳过 10s GET 缓存
  return http.get('/paper/live', { params: { refresh: true } }).then((r) => r.data)
}

/** 立即撮合一次（不必等定时 tick） */
export function tickPaper(): Promise<Record<string, unknown>> {
  return http.post('/paper/tick').then((r) => r.data)
}

/** 切换自动 / 手动交易模式（持久化） */
export function setPaperMode(auto: boolean): Promise<PaperLiveStatus> {
  return http.post('/paper/mode', { auto }).then((r) => r.data)
}

export interface PaperOrderPayload {
  code: string
  side: 'buy' | 'sell'
  /** 买入/卖出股数（卖出留空=全部） */
  shares?: number
  /** 买入金额（元），优先于 shares */
  amount?: number
  name?: string
}

export interface PaperOrderResult {
  result: {
    code: string
    name: string
    side: string
    shares: number
    price: number
    amount: number
    fee: number
    reason: string
  }
  account: PaperAccount
}

/** 手动下单（按实时价立即成交） */
export function placePaperOrder(payload: PaperOrderPayload): Promise<PaperOrderResult> {
  return http.post('/paper/order', payload).then((r) => r.data)
}

export function getPaperTrades(
  limit = 100,
  code?: string
): Promise<{ trades: PaperTrade[] }> {
  return http
    .get('/paper/trades', { params: { limit, code } })
    .then((r) => r.data)
}

export function getPaperEquity(): Promise<{
  equity: Array<{
    date: string
    total_asset: number
    cash: number
    market_value: number
  }>
}> {
  return http.get('/paper/equity').then((r) => r.data)
}

export function resetPaperAccount(): Promise<{ reset: boolean }> {
  return http.post('/paper/reset').then((r) => r.data)
}

// ==================== K线预热 ====================

export function warmupKlines(): Promise<{ started: boolean; message: string }> {
  return http.post('/screening/warmup').then((r) => r.data)
}

export interface KlineStatus {
  total: number
  cached: number
  ratio: number
  last_warmup: {
    result?: {
      ok: number
      fail: number
      total: number
      /** 指数日线随预热一起刷新（择时 regime_ma / regime_tiers 的输入） */
      index?: { ok: boolean; last: string | null; bars: number; error?: string }
    }
    at?: string
  }
}

export function getKlineStatus(): Promise<KlineStatus> {
  return http.get('/screening/kline-status').then((r) => r.data)
}

// ==================== LLM 分析 ====================
export interface ChecklistItem {
  label: string
  status: 'pass' | 'warn' | 'fail'
  detail: string
}

export interface DecisionDashboard {
  code: string
  name: string
  conclusion: '买入' | '持有' | '卖出'
  summary: string
  entry_price: number | null
  stop_loss: number | null
  target_price: number | null
  checklist: ChecklistItem[]
  model: string
  degraded: boolean
  generated_at: string
  sentiment: Sentiment | null
}

export function getAnalysisStatus(): Promise<{ llm_available: boolean; model: string }> {
  return http.get('/analysis/status').then((r) => r.data)
}

export function analyzeStock(code: string, refresh = false): Promise<DecisionDashboard> {
  return http.post(`/analysis/${code}`, null, { params: { refresh } }).then((r) => r.data)
}

// ==================== 自选股 ====================

/** 自选股来源：auto=程序自选（扫描命中） / manual=手动自选（用户添加） */
export type WatchSource = 'auto' | 'manual'

export interface WatchItem {
  code: string
  name: string
  note: string
  source: WatchSource
  created_at: string
  // 后端合并的实时行情字段（快照失败时为 null）
  price?: number | null
  pct_change?: number | null
  /** 当日成交额（元），快照缺失时为 null */
  amount?: number | null
  /** 换手率（%），快照缺失时为 null */
  turnover_rate?: number | null
}

export interface WatchCounts {
  auto: number
  manual: number
  total: number
}

export function getWatchlist(
  source?: WatchSource
): Promise<{ items: WatchItem[]; counts: WatchCounts }> {
  return http
    .get('/watchlist', { params: source ? { source } : {} })
    .then((r) => r.data)
}

export function addWatch(
  code: string,
  source: WatchSource = 'manual'
): Promise<{ added: boolean }> {
  return http.post(`/watchlist/${code}`, null, { params: { source } }).then((r) => r.data)
}

/** 批量加入自选股（扫描命中 → 程序自选）。 */
export function addWatchBatch(
  codes: string[],
  source: WatchSource = 'auto',
  note = ''
): Promise<{ added: number; codes: string[] }> {
  return http.post('/watchlist/batch', { codes, source, note }).then((r) => r.data)
}

/** 把程序自选转入手动自选。 */
export function promoteWatch(codes: string[]): Promise<{ promoted: number }> {
  return http.post('/watchlist/promote', { codes }).then((r) => r.data)
}

export function removeWatch(code: string): Promise<{ removed: boolean }> {
  return http.delete(`/watchlist/${code}`).then((r) => r.data)
}

// ==================== 统一错误 / 数据源降级提示 ====================
// 后端在数据源故障时会返回 502（异常）或 503（超时降级），网络层错误则为
// 无 response。这类情况应统一提示一次「行情数据源暂不可用」，而不是让每个
// 页面各自写错误逻辑（且有些页面会因此永久卡在 loading）。
import { useNotifyStore } from '../stores/notify'

http.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status
    const isDown =
      status === 502 || status === 503 || status === 504 || !error.response
    if (isDown) {
      try {
        useNotifyStore().markDataSourceDown()
      } catch {
        // pinia 尚未激活（理论上不会在请求发生时触发）
      }
    }
    return Promise.reject(error)
  },
)

// ==================== 可交易范围规模 ====================

export interface ScopeStats {
  source_total: number
  in_scope: number
  mainboard: number
  cyb: number
  excluded: { board_excluded: number; st: number; giant: number; invalid: number }
}

/** 当前可交易范围规模（沪深主板+创业板，剔 ST/巨无霸），见后端 /api/scope/stats。 */
export function getScopeStats(): Promise<ScopeStats> {
  return http.get('/scope/stats').then((r) => r.data)
}

export default http
