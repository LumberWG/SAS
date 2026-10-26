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

export interface BacktestRequest {
  strategy: string
  start_date: string
  end_date: string
  initial_capital?: number
  max_positions?: number
  commission?: number
  stamp_tax?: number
  codes?: string[]
  universe_size?: number
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
    annualized_return: number
    max_drawdown: number
    sharpe: number
    trade_count: number
    win_rate: number
    final_equity: number
    trading_days: number
  }
  equity_curve: { date: string; equity: number }[]
  trades: BacktestTrade[]
}

export function runBacktest(req: BacktestRequest): Promise<BacktestResult> {
  return http.post('/backtest/run', req).then((r) => r.data)
}

export function getBacktest(runId: number): Promise<BacktestResult> {
  return http.get(`/backtest/${runId}`).then((r) => r.data)
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
  last_warmup: { result?: { ok: number; fail: number; total: number }; at?: string }
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

export default http
