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

export interface StrategyInfo {
  name: string
  description: string
  category: string
  min_bars: number
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
  total: number
  scanned: number
  progress: number
  skipped: number
  hit_count: number
  hits: ScanHit[]
  started_at: string
  finished_at: string
}

export function getStrategies(): Promise<StrategyInfo[]> {
  return http.get('/screening/strategies').then((r) => r.data.strategies)
}

export function startScan(strategies: string[], codes?: string[]): Promise<{ task_id: string }> {
  return http.post('/screening/scan', { strategies, codes }).then((r) => r.data)
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

export default http
