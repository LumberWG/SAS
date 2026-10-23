/**
 * API 客户端：封装后端全部接口，统一处理 disclaimer 字段。
 */
import axios from 'axios'

const http = axios.create({
  baseURL: '/api',
  timeout: 30000,
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
  updated_at: string
  trading: boolean
  indices: IndexQuote[]
  breadth: {
    up: number
    down: number
    flat: number
    total: number
    limit_up: number
    limit_down: number
  }
  total_amount: number
  temperature: number
  sentiment: Sentiment
}

export interface KlineData {
  code: string
  dates: string[]
  kline: number[][] // [open, close, low, high]
  volumes: number[]
  amounts: (number | null)[]
  pct_changes: (number | null)[]
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
  batch: string
  created_at: string
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

export interface WatchItem {
  code: string
  name: string
  note: string
  created_at: string
  // 后端合并的实时行情字段（快照失败时为 null）
  price?: number | null
  pct_change?: number | null
}

export function getWatchlist(): Promise<{ items: WatchItem[] }> {
  return http.get('/watchlist').then((r) => r.data)
}

export function addWatch(code: string): Promise<{ added: boolean }> {
  return http.post(`/watchlist/${code}`).then((r) => r.data)
}

export function removeWatch(code: string): Promise<{ removed: boolean }> {
  return http.delete(`/watchlist/${code}`).then((r) => r.data)
}

export default http
