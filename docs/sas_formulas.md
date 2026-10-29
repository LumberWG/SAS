# SAS 选股与下单逻辑 · 公式备查

> 生成日期：2026-10-29（同步至 commit `c3d0c0c` 之后的实现）。
> 代码出处：`backend/app/screener/scanner.py`、`analysis/recommender.py`、`analysis/factors.py`、
> `strategies/builtin.py`、`data/scope.py`、`data/cache.py`、`data/minute.py`、
> `paper/engine.py`、`paper/live.py`、`backtest/pipeline.py`、`config.py`。
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
                                                  5s tick(live.tick)
                                                         │
                                 ┌───────────────────────┼───────────────────────┐
                        实时止盈止损/盈利保护      做T引擎(t_tick)         队列撮合(卖优先→评分降序)
                                 └───────────────────────┼───────────────────────┘
                                                         │
                                    模拟成交落库(PaperEngine + paper_position_lots 批次)
```

- 选股层：规则型布尔触发（满足全部条件才入选）。
- 建议层：`factors.py` 多因子加权打分，决定 buy/sell/hold。
- 下单层：`paper/` 模拟盘，确定性公式计算费用、仓位、止盈止损、做T。
- 持仓模型：**批次（lot）制**——每笔买入是一个带 `buy_date` 的批次，可卖股数由批次日期推导（严格 T+1）。

---

## 1. 选股逻辑（`screener/scanner.py`）

### 1.1 候选池构建（三选一）
- `codes`：直接指定一批代码；
- `top_amount:N`：按实时成交额取前 N 只（流动性优先，前端默认 `top_amount:500`）；
- `scope`（含 `"all"`）：`filter_pool_by_scope` 按板块过滤，再经范围过滤；
  - 注意 `"all"` 走全市场不过滤分支，会触发全市场扫描（候选可达 ~4392 只，很慢）。

### 1.2 范围过滤 `is_tradable(code, name)`（`data/scope.py`）
```
放行前缀：600 / 601 / 603 / 605 / 000 / 001 / 002 / 003 / 300 / 301
剔除：其余前缀（688 科创、920/43/83/87 北交所、900/200 B 股…）→ board_excluded
     GIANT_CODES 黑名单（银行/石油石化/保险/券商权重/白酒等 34 只）→ giant
     名称含 *ST / ST / 退 → st
```
> 该函数只按「代码 + 名称」判断，不查股票池，因此可脱库使用。
> 实战坑：`600000` 是浦发银行、在巨无霸黑名单里，用它做测试代码会被下单准入直接拒绝（曾导致 16 个单测红）。

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

> 左侧博弈/趋势策略。`stop_loss`/`target_price` 由 `default_stop_target`（10 日最低价 / +15%）给出。

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

> 账户：默认初始资金 10 万，单账户，状态全落 SQLite。常量：
> `SINGLE_POSITION_MAX_RATIO=0.20`、`STOP_LOSS_RATIO=0.08`、`TAKE_PROFIT_RATIO=0.20`、`LOT_SIZE=100`。

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
成本价   cost_price = (成交额 + 买入费用) / 股数          # 含费成本，止损/止盈都以它为基准
```
候选多于剩余名额时只取名额内的高分股参与分钱（避免预算被买不到的摊薄）。
若「成交额 + 费用 > 现金」，逐手回退（每次 −100 股）直到买得起。

### 4.3 评分分档
- `score ≥ paper_full_score(默认40)` → 系数 1.0；
- `paper_half_score(默认25) ~ full` → 系数 0.5；
- `score < half(25)` → 只记录不下单。

### 4.4 轮动替换（满仓时，**默认关闭**）
满足条件全部成立才换：`paper_rotation_enabled` + 持仓已满 `paper_max_positions(10)` + 存在未持仓买入候选 +
新票 `score ≥ paper_rotation_min_score(40)` 且比最弱持仓建仓 `score` 高 `paper_rotation_gap(20)` +
最弱持仓非当日买入（T+1）+ 当日轮动次数 < `paper_rotation_max_per_day(1)`。

> 默认 `paper_rotation_enabled=False`：回测实测轮动在 Top300 池下几乎不触发，却是强制卖出与
> 换手（月换手约 2.2 个资本）的主要来源，故线上关闭。

### 4.5 买入准入 `_buy_allowed()`（四道闸，任一不满足即 skip）
1. **北交所**：`paper_exclude_bj` 且代码属北交所（920/43/83/87/4/8）→ `北交所股票已排除`；
2. **板块范围**：`is_tradable(code, name)` 为假 → `不在沪深主板+创业板范围（scope 过滤）`；
3. **名称可信** `_name_ok()`：非空、不等于代码、无乱码（`\ufffd` / GBK-UTF8 往返不一致）→ 否则拒绝（股票池可能不完整）；
4. **成交额池**：`paper_pool_top_n(默认300) > 0` 时，代码须在当日成交额 TopN 集合内（与回测股票池口径对齐）；
   快照拿不到时本轮不限制（不让数据源抖动直接停交易）。

### 4.6 大盘择时买入闸门（弱市暂停买入）
```
ma      = close.rolling(N).mean().shift(1)        # N = paper_regime_ma（默认 60，<=0 关闭）
prev_c  = close[i-1]                              # i = 最后一个 date <= today 的行
weak    = prev_c < ma[i-1]                        # 即对比「前一日收盘」与「截至再前一日的 MA」
market_weak = paper_regime_weak_pause ∧ N>0 ∧ weak
market_weak → 全部买入候选返回 skip「大盘弱势，暂停买入」，轮动也暂停，卖出照常
```
- 指数文件 `data/parquet/idx_sh000001.parquet` 缺失或长度不足 → 判为不弱（不暂停）；
- MA 整体后移一日，保证「t 日盘中即可算出」，无未来函数（比回测 regime 口径略保守一日）。

### 4.7 止损 / 止盈 / 盈利保护
```
止损距离 _stop_distance = max(0.08, min(N × ATR/close, paper_atr_stop_max(0.20)))
                       N = paper_atr_stop_mult（默认 2.0；<=0 关闭 ATR 放宽 → 固定 8%）
建仓时一次定死：stop_price = round(cost_price × (1 - _stop_distance), 3)，peak_price = cost_price

逐 tick 检查（价格优先取实时报价）：
  1) peak = max(peak, price)                     # 持仓期最高价，落库
  2) base = stop_price（老持仓无该字段时退化为 cost × (1-0.08)）
  3) 保本：peak ≥ cost × (1 + paper_breakeven_pct 0.10) → base = max(base, cost)
  4) 移动：peak ≥ cost × (1 + paper_trail_arm 0.00)     → base = max(base, peak × (1 - paper_trail_pct 0.10))
  5) price ≤ base  → 止损卖出；price / cost - 1 ≥ 0.20 → 止盈卖出
```
- 止损/止盈都以**含费成本**为基准，止损线（含 ATR 放宽）在建仓时定死，便于复盘「当时定的止损是多少」；
- `paper_trail_arm=0` 表示移动止损**从建仓起就跟踪**：需 `peak × 0.9 > cost × 0.92`（即 peak > cost×1.022）才会抬升止损线；
- 保本与移动叠加后，实际走势常是「涨到 +10% 后回撤 10% 离场」，很少走到 +20% 硬止盈；
- 若该股当前无可卖批次（今仓），本轮直接跳过（T+1）。

### 4.8 批次持仓与严格 T+1（`data/cache.py`）
```
paper_position_lots(code, shares, cost_price, buy_date, tag)
  tag = base（开仓）/ t_add（做T加仓）/ t_buyback（做T买回）

汇总行 paper_positions 由批次聚合（_aggregate_paper_summary）：
  shares       = Σ lots.shares
  cost_price   = Σ(lots.shares × lots.cost_price) / Σ lots.shares     # 加权平均
  buy_date     = max(lots.buy_date)
  sellable_shares = Σ lots.shares where lots.buy_date < 今天          # 可卖股数（T+1）
  无批次 → 删除汇总行（持仓清零）

卖出 reduce_paper_sellable(code, want, today)：
  只在 sellable 批次内按 buy_date 升序 FIFO 减仓 → 返回实际卖出股数，并刷新汇总与可卖
```
- 今日买入的批次次日才计入 `sellable_shares`，因此「当日买当日卖」在数据层不可能发生；
- 卖出/止损/做T 都必须走 `reduce_paper_sellable`，不能直接改 `shares`；
- `ensure_lots_migrated()` 启动时把历史持仓（无批次）补建为一个批次（沿用原 `buy_date`），幂等。

### 4.9 做T引擎 `t_tick()`（真实 tick，默认全关）
总开关 `paper_t_active`（默认 False），仓位比例 `paper_t_size`（默认 0.25）。
每个 tick 按持仓逐只判断，**每只票每个 tick 只做一个动作**（命中分支后 `continue`）：

```
大盘护栏：idx_drop = -min(各主要指数当日涨跌幅)
        crash = paper_t_crash_guard(0.02) 且 idx_drop ≥ 0.02 → 该票本轮全部做T动作暂停（含买回）

① 买回（t_t_sold > 0 时优先处理；按 t_sold_date 区分「当日卖出」与「往日待买回」）
   当日卖出（t_sold_date == 今天）：
       买回线 = max(当日开盘价, 昨收) × (1 + paper_t_up_buyback 0.03)
       price ≤ 买回线 → 买入 (t_t_sold // 100) × 100 股，tag=t_buyback（今仓，次日可卖），
                       t_t_sold 清零、t_sold_date 清空
       遇 crash 护栏当日不买回（此时价格通常已低于买回线，照规则买回等于在最低点接刀），状态保留
   往日待买回（t_sold_date < 今天）：
       开盘直接恢复底仓，不看 crash 与买回线（回测 t_buyback_shares 就是次日开盘无条件买回）
       现金不足则告警并保留状态，下个 tick 再试

② 涨停卖半（paper_t_limit_sell_half）
   当日最高 ≥ 昨收 × 1.095 且 sellable ≥ 100 → 卖出 floor(sellable/2 /100)×100，t_t_sold = 卖出股数

③ 倒T（先卖后买，只动可卖底仓）
   open_chg = 开盘价 / 昨收 - 1
   open_chg > 0 且 当日最高 ≥ 开盘价 × (1 + paper_t_up_sell 0.05) 且 sellable ≥ 100
   → 卖出 min( floor(总持仓 × t_size /100)×100, sellable )，t_t_sold = 卖出股数

④ 仅当 open_chg < 0（低开）时：
   4a 反弹减仓：price ≥ 昨收 × (1 - paper_t_rebound_sell 0.02)
      → 只减 tag=t_add 的可卖批次（昨日及更早的加仓），减仓后本轮不再加仓
   4b 分级加仓（每票每日最多一次，t_down_armed 防重复）：
      最低价 ≤ 开盘价 × (1 - paper_t_down_full 0.20) → 预算 = 现金 × 0.95
      否则 最低价 ≤ 开盘价 × (1 - paper_t_down_add 0.20) → 预算 = 现金 × paper_t_down_add
      → 买入 floor(预算/price/100)×100 股，tag=t_add（今仓，次日可卖）
```
- 触发用快照的 `open/high/low/prev_close`，成交价用当 tick 的实时价 `price`；
- 每日跨交易日只重置 `t_down_armed`（每票每日最多加仓一次）；`t_t_sold` **跨日保留**直到买回完成，
  否则「当日没买回」会静默变成永久减仓，与回测口径不一致；
- 加仓预算按**现金比例**计算（`paper_t_down_add` 在 config 注释里写「相对总仓」，实现是相对现金）；
- `paper_t_down_add/down_full` 默认 0.20 ≈ 关闭下跌加仓（回测实测单边弱市加仓放大亏损）；
- crash 行为（已定稿）：crash 当日**不买回**（避免在最低点接刀），但待买回状态跨日保留，
  次日开盘无条件恢复底仓。旧实现每日重置时把 `t_t_sold` 清零，等于「crash / 未回落即永久减仓」，
  与回测（次日开盘一定买回）不可比，已修（新增 `paper_positions.t_sold_date` 列）。

---

## 5. 撮合机制（`paper/live.py`，事件驱动）

- **周期**：默认 `paper_tick_interval = 5s` 一次 `tick()`；实时价新浪逐股行情优先，缺失用内存快照兜底。
- **tick 执行顺序**：清过期信号 → 非交易时段直接返回 → 批量取实时价 → 手动模式仅刷新报价 →
  ① `check_stop_loss_target`（止盈止损/盈利保护）→ ② `t_tick`（做T）→ ③ 队列撮合 → 候补补位 → 出队。
- **买入窗口**：`paper_buy_open_window_minutes`（默认 5 分钟）；窗口外只跑卖出，买单留在队列等下一个开盘窗口
  （对齐回测"次日开盘价"成交，避免盘中 13:01 一把买高）。
- **信号 TTL**：`paper_signal_ttl_minutes(30)`，过期直接丢弃；开盘窗口模式下买单的过期时间顺延到
  「下一个窗口起点 + 窗口长度 + TTL」，否则 13:01 产生的买单永远等不到次日开盘。
- **候补队列**：`paper_backlog_enabled`；仅「持仓已满 / 总仓位已达上限 / 资金不足一手」三种 skip 入候补，
  按评分降序在有剩余名额时补位（同样受开盘窗口约束），有效到当日收盘（非交易时段顺延次日）。
- **卖出优先于买入**：队列按 `(action != "sell", -score)` 排序，卖完再按评分从高到低买。
- **自动 / 手动模式**：手动模式只刷新报价与展示建议，不执行任何买卖（含止盈止损），由用户在模拟交易页手动下单。

---

## 6. 关键可调参数清单

| 配置项 | 默认 | 含义 | 出处 |
|---|---|---|---|
| `paper_initial_capital` | 100000 | 模拟账户初始资金 | engine |
| `paper_auto_trade` | True | 模拟交易总开关 | engine/live |
| `paper_live_enabled` | True | 实时撮合引擎开关 | live |
| `paper_max_positions` | 10 | 最大持仓只数 | engine |
| `paper_max_total_exposure` | 0.80 | 总仓位上限（留 20% 现金） | engine |
| `paper_full_score` | 40 | 评分全额系数阈值 | engine |
| `paper_half_score` | 25 | 评分半额系数阈值（低于只记录） | engine |
| `paper_rotation_enabled` | **False** | 满仓轮动替换开关 | engine |
| `paper_rotation_min_score / gap / max_per_day` | 40 / 20 / 1 | 轮动门槛、分差、每日次数 | engine |
| `paper_backlog_enabled / max` | True / 30 | 候补队列开关/上限 | live |
| `paper_exclude_bj` | True | 排除北交所 | engine |
| `paper_pool_top_n` | 300 | 成交额 TopN 池（对齐回测，0=不限） | engine |
| `paper_atr_stop_mult / max` | 2.0 / 0.20 | ATR 止损放宽倍数/上限（0=固定 8%） | engine |
| `paper_breakeven_pct` | 0.10 | 浮盈达此比例止损上移到成本（保本） | engine |
| `paper_trail_pct` | 0.10 | 从持仓期最高价回撤此比例卖出（移动止损） | engine |
| `paper_trail_arm` | 0.00 | 移动止损启用门槛（浮盈达此比例后跟踪） | engine |
| `paper_regime_ma` | 60 | 大盘择时 MA 周期（<=0 关闭） | engine |
| `paper_regime_weak_pause` | True | 弱市（指数在 MA 下）暂停自动买入 | engine |
| `paper_t_active` | False | 做T引擎总开关 | engine |
| `paper_t_size` | 0.25 | 单次做T比例（相对总持仓） | engine |
| `paper_t_up_sell` | 0.05 | 开盘涨且盘中触此幅度 → 卖出 t_size | engine |
| `paper_t_up_buyback` | 0.03 | 回调买回线（相对 max(开盘, 昨收)） | engine |
| `paper_t_down_add / down_full` | 0.20 / 0.20 | 低开达此跌幅加仓（0.20≈关闭） | engine |
| `paper_t_rebound_sell` | 0.02 | 反弹至跌幅小于此 → 减加仓批次 | engine |
| `paper_t_limit_sell_half` | True | 涨停（弱封板）卖 1/2 | engine |
| `paper_t_crash_guard` | 0.02 | 大盘当日跌幅超此暂停做T | engine |
| `paper_tick_interval` | 5 | 撮合周期（秒） | live |
| `paper_buy_open_window_minutes` | 5 | 开盘买入窗口（分钟，0=不限） | live |
| `paper_signal_ttl_minutes` | 30 | 信号有效期 | live |
| `scan_auto_watch_limit` | 50 | 程序自选 TopN | scanner |
| `paper_scan_autotrade / _top_n / _overnight` | True / 10 / True | 选股联动自动交易 | scanner |
| `FACTOR_WEIGHTS` / `BUY_THRESHOLD` / `SELL_THRESHOLD` | 见 §3.2/3.1 | 评分权重与分级 | factors |

---

## 7. 与回测（`backtest/`）口径对齐的设计要点

### 7.1 已对齐
- **成交时点**：实盘买入收敛到开盘窗口（默认 09:30 起 5 分钟内），与回测"次日开盘价成交"对齐。
- **股票池**：实盘买入准入强制"当日成交额 TopN 池"（`paper_pool_top_n=300`），与回测每日成交额 TopN 同一口径。
- **范围过滤**：实盘 `_buy_allowed` 与回测 `board_prefixes` / `scope.is_tradable` 同源，回测选不到的票实盘也买不到。
- **评分模型**：`compute_factors` 在选股建议与回测中复用同一套权重与阈值，外部 `weights` 仅做参数实验，自动归一化回 100 分制。
- **止损口径**：回测止损用 `max(8%, 2×ATR%)` 建仓时定死，与 `_stop_distance` 一致。

### 7.2 回测侧新增的日内建模（均为可选，默认关闭=线上行为）
| 参数 | 默认 | 含义 |
|---|---|---|
| `intraday_minutes` | False | True 时对持仓日拉真实 5 分钟线（`data/minute.py`），按时间顺序判定止损/止盈/分段止盈首次触发，成交价取分钟价；False 时用日K 边界序列 `open→low→high→close`（low 在 high 前，天然实现「止损优先」） |
| `slippage` | 0.0 | 单边滑点：买价 ×(1+s)、卖价 ×(1−s)，涨跌停判定仍用原始价 |
| `t_active` 及 `t_*` | None | 做T引擎回测：涨停卖半（卖出后当日不再做T）/ 上涨倒T（成交价 = 开盘价×(1+t_up_sell)，买回按**次日开盘**）/ 下跌分级加仓（成交价 = 开盘价×(1−t_down_add)，预算 = 现金×t_down_add；触发满仓档时预算 = 现金×0.95）/ 反弹减仓（按收盘价，只卖之前交易日加的仓） |
| `partial_tp_at / partial_tp_ratio` | None / 0.5 | 分段止盈：首次达到该涨幅卖出一部分 |
| `breakeven_trigger / trail_pct / trail_arm` | None | 保本止损 / 移动止损（回测侧） |

`_intraday_first_trigger(series, stop, tp, partial)` 返回首个触发的 `(type, price)`，
三者同时命中时取**序列中最先出现**者；日K 边界模式忽略返回价，改用 `min(开盘, 止损)` / `max(开盘, 目标)`。

### 7.3 已知偏差（对照时需注意）
- **做T护栏用当日指数收盘**：`_t_engine` 的 crash guard 读 `idx_close[date]`（当日收盘）判断当日跌幅，
  属于未来函数；线上 `t_tick` 用的是盘中快照涨跌幅。做T结论只能看方向，不能当收益预测。
- **候补与轮动**：回测无候补队列，满仓/资金不足直接丢弃（实盘会顺延到窗口内补位）。
- **止损止盈触发**：回测用日线 `low/high`（或分钟线）判定，实盘每 5 秒用最新价判定，
  盘中被扫的止损在两端不完全等价。
- **指数 K 线新鲜度**：`regime_ma` 依赖 `idx_sh000001.parquet`，该文件由开机预热与 16:00 日更刷新；
  滞后时会静默退化为「不弱」档。

---

*本表为快照，参数以代码当前实现为准；调整权重/阈值请同步 `tests/test_factors.py` 与前端"智能建议"展示。*
