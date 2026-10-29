# SAS 选股与下单逻辑 · 公式备查

> 生成日期：2026-10-29。
> 代码出处：`backend/app/screener/scanner.py`、`analysis/recommender.py`、`analysis/factors.py`、
> `strategies/builtin.py`、`paper/engine.py`、`paper/live.py`、`config.py`。
> 本文档用于归档当前实现的逻辑与公式，便于后续维护与回测对齐。

---

## 0. 系统总览

完整链路（**下单为模拟盘，非实盘**）：

```
选股扫描(scanner) ──► 命中 hits ──► 多因子建议(recommender + factors)
                                         │
                              ┌──────────┴──────────┐
                         程序自选(_auto_watch_hits)   实时交易队列(LiveTrader.submit)
                                                         │
                                                  5s tick 撮合(live.tick)
                                                         │
                                             模拟成交落库(PaperEngine)
```

- 选股层：规则型布尔触发（满足全部条件才入选）。
- 建议层：`factors.py` 多因子加权打分，决定 buy/sell/hold。
- 下单层：`paper/` 模拟盘，确定性公式计算费用、仓位、止盈止损。

---

## 1. 选股逻辑（`screener/scanner.py`）

### 1.1 候选池构建（三选一）
- `codes`：直接指定一批代码；
- `top_amount:N`：按实时成交额取前 N 只（流动性优先，前端默认 `top_amount:500`）；
- `scope`（含 `"all"`）：`filter_pool_by_scope` 按板块过滤，再经范围过滤；
  - 注意 `"all"` 走全市场不过滤分支，会触发全市场扫描（候选可达 ~4392 只，很慢）。

### 1.2 范围过滤 `is_tradable()`
剔除：科创板（688 开头）、北交所（920/43/83/87/4/8 开头）、ST、巨无霸（超大盘）。

### 1.3 逐股扫描与命中
对每只候选股并发跑所选策略 `_scan_stock` → 各策略 `evaluate(df)` 独立评估，
命中汇总成 `task.hits`（含 code/name/price/reason/技术面评分 `extra.score`）。

### 1.4 排序 `rank_hits()`
按技术面评分降序排，同分并列同一名次。

### 1.5 落盘与联动（在 `status=done` 之后）
- 落盘 `last_scan.json`（重启可恢复）；
- `_auto_watch_hits()`：命中 TopN 写入"程序自选"；
- `_trade_top_hits()`：命中 TopN 送入 `recommender.analyze_stock` 生成建议，再 `live_trader.submit` 入队。

---

## 2. 策略触发条件（布尔规则，无打分）

| 策略 | 文件类 | 触发条件（全部满足） | 默认参数 |
|---|---|---|---|
| 海龟突破 | `TurtleBreakout` | `close ≥ max(close[-N:])` 且 `amount > min_amount_yi×1e8` 且 `close > open`（阳线） | N=20, 最低成交额=1亿, 阳线=开 |
| 均线放量 | `MaVolumeBreakout` | `ma5 > ma10 > ma20` 且 `close > ma5` 且 `vol_ratio > 阈值` | 量比阈值=1.5 |
| MACD 金叉 | `MacdGoldenCross` | `prev.dif ≤ prev.dea` 且 `last.dif > last.dea`（金叉）且 `macd_hist > 0` | 红柱=开, 位置=any |
| KDJ 超卖金叉 | `KdjOversoldCross` | `kdj_j < 阈值`（超卖）且 `prev.k ≤ prev.d` 且 `last.k > last.d`（金叉） | 超卖J=20, 金叉=开 |
| 动量突破 | `MomentumBreakout` | `N日涨幅 ≥ 阈值%` 且 `close ≥ N日最高 × near_high%` | N=60, 涨幅≥30%, 贴近≥95% |
| 平台突破 | `PlatformBreakout` | `平台振幅 < 阈值%` 且 `close > 平台最高` 且 `volume > 平台均量` | 平台=20日, 振幅<15%, 放量=开 |

> 左側博弈/趋势策略。`stop_loss`/`target_price` 由 `default_stop_target`（10 日最低价 / +15%）给出。

---

## 3. 多因子评分公式（`analysis/factors.py`，核心）

### 3.1 总分公式
每个因子先归一化到 `sub ∈ [-1, 1]`，再加权求和：

```
total = Σ ( weight_i × sub_i )          # weight 合计 100，total ∈ [-100, 100]
action = buy   若 total ≥ +15
         sell  若 total ≤ -15
         hold  其他
```

单因子贡献 `contrib = weight × sub`（见 `FactorItem.contrib`）。

### 3.2 权重表（合计 100）

| 组 | 权重 | 因子（权重） |
|---|---|---|
| A 趋势 | 30 | 均线排列 `ma_trend`(12) / 中期斜率 `ma_slope`(10) / 长期位置 `long_position`(8) |
| B 动量 | 25 | MACD `macd`(10) / RSI `rsi`(8) / 20日涨幅 `gain20`(7) |
| C 量能 | 20 | 量比 `vol_ratio`(8) / 量能趋势 `vol_trend`(7) / 换手 `turnover`(5) |
| D 风险 | 15 | 波动率 `atr`(5) / 回撤 `drawdown`(5) / 乖离 `bias`(5) |
| E 位置 | 10 | 突破 `breakout`(6) / 布林位置 `boll`(4) |

`BUY_THRESHOLD = 15.0`，`SELL_THRESHOLD = -15.0`。

### 3.3 各因子 `sub` 算法（饱和到 [-1,1]）

- **ma_trend(12)**：5>10>20>60 → 1.0；5>10>20 → 0.6；5<10<20<60 → -1.0；5<10<20 → -0.6；否则 0。
- **ma_slope(10)**：`change = (MA20_now / MA20_5日前 - 1) × 100`；`sub = change / 2`（±2% 饱和）。
- **long_position(8)**：`dev = (close / ma60 - 1) × 100`；`sub = dev / 8`（±8% 饱和）。
- **macd(10)**：`strength = clip(hist / close / 0.01)`（柱体占股价 1% 满分）；`streak_sub = clip(连续同向放大天数 / 3) × sign(hist)`（最多 3 天）；`sub = 0.7×strength + 0.3×streak_sub`。
- **rsi(8)**：RSI12 >80 → -1；>70 → -0.5；≥55 → +1；≥50 → +0.2；≥45 → 0；≥40 → -0.2；≥30 → -0.5；<30 → +0.3（超跌反弹）。
- **gain20(7)**：`gain = (close / 20日前close - 1) × 100`；`sub = clip(gain / 20)`；`gain > 30` 额外 `sub = clip(sub - 0.3)`。
- **vol_ratio(8)**：>1.5 → 1；<0.7 → -1；否则 `clip((ratio - 1) / 0.5)`。
- **vol_trend(7)**：`ratio = 5日均量 / 20日均量`；`sub = clip((ratio - 1) / 0.5)`。
- **turnover(5)**：2%~8% → 1；1%~2% → 0.3；8%~15% → 0；15%~25% → -0.5；>25% → -1；<1% → -0.6（流动性差）。
- **atr(5)**：`ratio = atr / close × 100`；≤2% → 0.5；≥5% → -1；否则 `0.5 - (ratio-2)/3 × 1.5`。
- **drawdown(5)**：`dd = |20日最大回撤%|`；`sub = 1 - clip((dd-5)/15, 0, 1) × 2`（≤5% 满分，≥20% 扣满）。
- **bias(5)**：BIAS6 >6 → -1；<-6 → +1（超跌加分）；否则 `clip(-bias6 / 6)`。
- **breakout(6)**：`pos = close / 20日最高`；≥1.0 → 1；≥0.97 → 0.4；≥0.90 → 0；<0.90 → -1。
- **boll(4)**：`pos = (close - dn) / (up - dn)`；0.5~0.8 → 1；<0.5 → `clip((pos-0.5)/0.5)`；≤1.0 → -0.5；>1.0 → -1。

> 调用 `compute_factors(df, weights=None)` 用默认权重；传 `weights` 时按"缺省兜底→归一化到 100"重建（回测参数实验用）。

---

## 4. 模拟下单与风控公式（`paper/engine.py`）

> 账户：默认初始资金 10 万，单账户，状态全落 SQLite。`SINGLE_POSITION_MAX_RATIO=0.20`、`STOP_LOSS_RATIO=0.08`、`TAKE_PROFIT_RATIO=0.20`、`LOT_SIZE=100`。

### 4.1 费用（A 股标准费率）
```
佣金  = max(amount × 0.00025, 5)      # 万2.5，单笔最低5元，双向
印花税 = amount × 0.001               # 千1，仅卖出
过户费 = amount × 0.00001             # 万0.1，双向
```

### 4.2 仓位（评分加权，第 3 次排序）
```
可投预算 available_budget = max(0, 总仓位上限 - max(0, 总资产 - 现金))
         总仓位上限 = 总资产 × paper_max_total_exposure (默认 0.80)
目标金额 budget = min( available_budget × 加权占比, 总资产 × 0.20, 现金, available_budget )
股数     shares = floor(budget / price / 100) × 100      # 整手
加权占比 = (max(score,1) × 档位系数) / Σ权重
```
候选多于剩余名额时只取名额内的高分股参与分钱（避免预算被买不到的摊薄）。

### 4.3 评分分档
- `score ≥ paper_full_score(默认40)` → 系数 1.0；
- `paper_half_score(默认25) ~ full` → 系数 0.5；
- `score < half(25)` → 只记录不下单。

### 4.4 轮动替换（满仓时）
满足条件全部成立才换：开关开 + 持仓已满 `paper_max_positions(默认10)` + 存在未持仓买入候选 +
新票 `score ≥ paper_rotation_min_score` 且比最弱持仓 `score` 高 `paper_rotation_gap` +
最弱持仓非当日买入（T+1）+ 当日轮动次数 < `paper_rotation_max_per_day`。

### 4.5 买入准入 `_buy_allowed()`
三道闸（任一不满足则 skip）：
1. 北交所排除：`paper_exclude_bj` 且该代码属北交所；
2. 名称可信 `_name_ok()`：非空、不等于代码、无乱码（`\ufffd`/UTF-8 被当 GBK 解的反常）→ 否则拒绝（股票池可能不完整）；
3. 成交额 TopN 池：`paper_pool_top_n > 0` 时，代码须在当日成交额 TopN 集合内（与回测股票池口径对齐）。

### 4.6 止损 / 止盈
```
止损距离 _stop_distance = max(0.08, min(N × ATR/close, paper_atr_stop_max(默认0.2)))
                       N = paper_atr_stop_mult（0 表示关闭 ATR 放宽，固定 8%）
止损价 stop_price = 成本价 × (1 - _stop_distance)     # 建仓时一次定死
止盈触发：现价 / 成本价 - 1 ≥ 0.20
```
盘中逐 tick 检查 + 收盘 `settle()` 兜底检查。

### 4.7 T+1
当日买入（`buy_date >= 今天`）不可卖出。

---

## 5. 撮合机制（`paper/live.py`，事件驱动）

- **周期**：默认 `paper_tick_interval = 5s` 一次 `tick()`；实时价新浪行情优先，缺失用内存快照兜底。
- **买入窗口**：`paper_buy_open_window_minutes`（默认 0=不限制）；非窗口内只跑卖出，买单挂起等下一个开盘（对齐回测"次日开盘价"成交，避免盘中系统性买高）。
- **信号 TTL**：`paper_signal_ttl_minutes`；过期（价格漂移）直接丢弃。
- **候补队列**：`paper_backlog_enabled`；满仓/资金不足的高分买入票转入候补，等有名额优先补位（到当日收盘或下一开盘失效）。
- **自动 / 手动模式**：手动模式只刷新报价与展示建议，不执行任何买卖（含止盈止损），由用户在模拟交易页手动下单。
- 卖出优先于买入（释放资金与名额）；T+1 由 engine 内部拦截。

---

## 6. 关键可调参数清单

| 配置项 | 默认 | 含义 | 出处 |
|---|---|---|---|
| `paper_initial_capital` | 100000 | 模拟账户初始资金 | engine |
| `paper_auto_trade` | - | 模拟交易总开关 | engine/live |
| `paper_live_enabled` | - | 实时撮合引擎开关 | live |
| `paper_max_positions` | 10 | 最大持仓只数 | engine |
| `paper_max_total_exposure` | 0.80 | 总仓位上限（留 20% 现金） | engine |
| `paper_full_score` | 40 | 评分全额系数阈值 | engine |
| `paper_half_score` | 25 | 评分半额系数阈值（低于只记录） | engine |
| `paper_rotation_enabled/min_score/gap/max_per_day` | - | 轮动替换参数 | engine |
| `paper_exclude_bj` | - | 排除北交所 | engine |
| `paper_pool_top_n` | - | 成交额 TopN 池（对齐回测） | engine |
| `paper_atr_stop_mult/max` | 0 / 0.2 | ATR 止损放宽倍数/上限 | engine |
| `paper_tick_interval` | 5 | 撮合周期（秒） | live |
| `paper_buy_open_window_minutes` | 0 | 开盘买入窗口（分钟） | live |
| `paper_signal_ttl_minutes` | - | 信号有效期 | live |
| `paper_backlog_enabled/max` | - | 候补队列开关/上限 | live |
| `scan_auto_watch_limit` | - | 程序自选 TopN | scanner |
| `paper_scan_autotrade / _top_n / _overnight` | - | 选股联动自动交易 | scanner |
| `FACTOR_WEIGHTS` / `BUY_THRESHOLD` / `SELL_THRESHOLD` | 见 §3.2/3.1 | 评分权重与分级 | factors |

---

## 7. 与回测（`backtest/`）口径对齐的设计要点

- **成交时点**：实盘买入收敛到开盘窗口（默认 09:30 起 N 分钟内），与回测"次日开盘价成交"对齐，避免盘中 13:01 一把买入系统性买高。
- **股票池**：实盘买入准入强制"当日成交额 TopN 池"（`paper_pool_top_n`），与回测每日成交额 TopN 股票池同一口径，保证两边策略可比（见代码注释 BACKTEST_LOG #19）。
- **范围过滤**：实盘 `is_tradable` 剔除科创板/北交所/ST/巨无霸，回测池也需保持一致，否则小票被真实买入而回测选不到。
- **评分模型**：`compute_factors` 在选股建议与回测中复用同一套权重与阈值，外部 `weights` 仅做参数实验，自动归一化回 100 分制。

---

*本表为快照，参数以代码当前实现为准；调整权重/阈值请同步 `tests/test_factors.py` 与前端"智能建议"展示。*
