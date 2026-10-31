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

> `exclude_giant` 参数（默认 `True`）用于「③ 交易队列」的大盘股开关：开关关闭时传 `False`，
> 黑名单与 `total_mv > 100 亿` 阈值同时放开。回测侧不传该参数（口径不变）。

**范围过滤是三道，不只是候选池**（2026-10-30 补齐）：

| 环节 | 位置 | 说明 |
|---|---|---|
| 候选池 | `scanner` 扫描前 | 池子按 `is_tradable` 过滤（原有） |
| 写自选 | `scanner._auto_watch_hits` / `scheduler.auto_watch_top_hits` | 兜底再判一次，范围外的不写入（曾是漏洞：盘后选股路径可把 688 写进自选） |
| 分析 | `recommender.analyze_watchlist` | 范围外的票不参与分析（已持仓的例外，仍需卖出建议）；否则它们会长期占住「建议买入」「切入点判定」面板 |

> 名称在**入库时**就用 `clean_name()` 清洗（`db.add_watch` / `add_watch_many`），
> 乱码（GBK 误读）不会再写进 `watchlist`；接口另有回填逻辑，历史脏数据会被修。
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

生效阈值取 `Settings.factor_buy_threshold` / `factor_sell_threshold`（默认 `15.0` / `-15.0`，
设置页可改、回测中心可落盘；`factors.BUY_THRESHOLD` 只剩代码默认与脚本兜底的作用）。
两者必须满足 **buy > sell**，同批提交时由 `param_specs._CROSS_RULES` 校验。

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

> 账户：默认初始资金 10 万，单账户，状态全落 SQLite。常量 `LOT_SIZE=100`。
> 风控三项 2026-10-30 起从硬编码挪进 `Settings`（设置页可改，也可由回测中心「落盘」写入）：
> `paper_stop_loss=0.08`、`paper_take_profit=0.20`、`paper_single_position_max=0.20`；
> engine 里保留 `STOP_LOSS_RATIO / TAKE_PROFIT_RATIO / SINGLE_POSITION_MAX_RATIO` 作缺省回退。

### 4.1 费用（A 股标准费率）
```
佣金  = max(amount × trade_fee_commission_rate, trade_fee_commission_min)   # 默认 万2.5 / 最低5元，双向
印花税 = amount × trade_fee_stamp_rate        # 默认 千1，仅卖出
过户费 = amount × trade_fee_transfer_rate     # 默认 万0.1，双向

> 四个费率都在 `Settings` 里（设置页可改，免五则 `commission_min=0`），**回测 `pipeline.calc_fee`
> 与实盘 `engine.calc_fee` 共用这一份**；此前两边各写一份常量，佣金谈下来只改一边，
> 回测 PnL 与实盘就对不上。
```

### 4.2 仓位（评分加权，第 3 次排序）
```
可投预算 available_budget = max(0, 总仓位上限 - max(0, 总资产 - 现金))
         总仓位上限 = 总资产 × paper_max_total_exposure (默认 0.80)
目标金额 budget = min( available_budget × 加权占比, 总资产 × paper_single_position_max(0.20), 现金, available_budget )
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

### 4.5 买入准入 `_buy_allowed()`（五道闸，任一不满足即 skip）
1. **北交所**：`paper_exclude_bj` 且代码属北交所（920/43/83/87/4/8）→ `北交所股票已排除`；
2. **板块范围**：`is_tradable(code, name, exclude_giant=large_on)` 为假 → `不在沪深主板+创业板范围（scope 过滤）`；
3. **大市值**：`large_cap_rule()` 为开（默认开）时
   - `GIANT_CODES` 黑名单生效（即上一条的 `exclude_giant=True`）；
   - 且 `总市值(total_mv) > paper_large_cap_yi(默认 100) 亿` → `总市值 N亿 > M亿（大盘股已排除）`；
   - 市值取不到（快照抖动）时**不拦**，与成交额池同一原则；
   - 开关**关闭**时上述两条同时失效 → 银行/石油等大市值票才允许进队列；
4. **名称可信** `_name_ok()`：非空、不等于代码、无乱码（`\ufffd` / GBK-UTF8 往返不一致）→ 否则拒绝（股票池可能不完整）；
5. **成交额池**：`paper_pool_top_n(默认300) > 0` 时，代码须在当日成交额 TopN 集合内（与回测股票池口径对齐）；
   快照拿不到时本轮不限制（不让数据源抖动直接停交易）。

> 大盘股开关的运行时状态存 `app_state`（键 `paper_exclude_large_cap` / `paper_large_cap_yi`），
> 接口 `GET/POST /api/paper/queue-filter`，页面在「③ 三选 · 交易队列」顶部。改完对**下一轮扫描**生效
> （选股候选池同样按该规则剔除，见 §1.2），因此不需要重启服务。
>
> **单一写入层**：`POST /api/paper/queue-filter` 内部委托 `param_specs.update_settings()`，
> 与「系统设置」页共用同一份 `app_state` 键与同一套校验（数值下限在 `_MIN_EXCLUSIVE`）。
> 两个入口只是"快捷方式"，不会出现两套校验各说各话（曾有过：队列页拒绝阈值 ≤0、设置页却放行）。

> **闸门顺序**：先 `_buy_allowed`（永久性原因：范围 / 大市值 / 名称 / 池子），再「大盘弱势」（临时性）。
> 反过来会让弱市期间全部候选都显示「大盘弱势」，把 688 这类「本来就不该买」的票也伪装成临时拒绝
> ——2026-10-30 的「切入点判定」面板就是这个症状（5 只里 4 只是科创板，全部标着「大盘弱势」）。

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
- **判定按自然日缓存**：结论只依赖前一日收盘，同一交易日内恒定，因此 `_index_is_weak` 一天只读一次
  指数 parquet（此前 tick 每 5 秒读一次，当天弱市日志刷了 1999 条）。
- **可运行时切换**：`paper_regime_weak_pause` 在 `param_specs.RUNTIME_KEYS` 中，设置页改完立即生效
  （无需改 `.env` 重启）；当前值经 `GET /api/paper/live` 的 `regime` 字段返回：
  `{enabled, ma, weak, paused_count, day}`，`paused_count` 是**当日**累计拦截次数（跨日归零）。
- **收盘快照 `paper_regime_state:{date}`（2026-10-30）**：`settle()` 每日**无条件**写一条 JSON：
  `{date, enabled, ma, index_prev_close, index_prev_ma, weak, note, weak_exposure, paused_count,
  total_asset, market_value, exposure}`。与 `paper_regime_paused:{date}` 互补——后者只在**发生拦截**
  时才有值，「弱市但当天没有买入信号」不会留下任何痕迹，而「当日是否弱势」正是
  `BACKTEST_LOG #70 §8` 要对账的前提。`note` 非空表示判定退化（指数文件缺失/长度不足），
  此时 `weak=false`，与闸门「数据源抖动不停交易」的既有行为一致；数值明细由
  `_index_regime_detail()` 给出，布尔判定 `_index_is_weak()` 只是它的薄封装（口径一字未改）。
- **三档实测（记录 #59，A~D+G/H，2025 年后）**：不暂停均值超额 +5.68%、降仓 60% +1.71%、
  降仓 30% +6.92%（但涨市 C 段 −13.82pp）、全暂停（现状）+4.83%。
  配对中位：降仓 30% +3.29pp、全暂停 **−2.50pp**。**三档均未通过验收协议**，故线上默认值不动，
  取舍（少交易 vs 少亏）交由使用者按风险偏好在设置页自行切换。
- **弱市保留比例 `paper_weak_exposure`（默认 0）**：`0` = 弱市按上面的公式全暂停买入；
  `>0` = 不暂停买入，只把总仓位上限压到 `min(paper_max_total_exposure, paper_weak_exposure)`
  （回测 `day_exposure` 同口径）。记录 #60 的档位寻优显示：**6 窗口能通过、15 段不重叠随机段不通过**
  （配对中位 −0.25pp、胜段 40%），所以**代码默认值保持 0**；MA20 版本的高敏感在单边跌里会
  反复「反弹站上 MA20 → 买入 → 再跌」，与降仓初衷相悖。
  **本机运行时覆盖为 0.3**（记录 #63，`app_state` 写入，重启重放）：MA60+30% 的随机段为中位 −0.55pp、
  胜段 46.7%，但**日胜率中位 42.9% → 60%**、急跌月 −11.02% → −4.60%；属于风险偏好档位而非收益优化。
- **弱转强爬坡 `regime_ramp_days`（仅回测，默认 0，未采纳）**：线上**没有独立的「转强」判定**——
  `prev_close >= ma[i-1]` 即视为转强，当天立刻恢复正常买入（无确认天数、无幅度缓冲、无滞回）。
  回测侧可选的爬坡口径（记录 #64）：
```
k    = 连续「非弱」交易日数（弱市日重置 0）；D = regime_ramp_days（0=关闭）
frac = min(1, k / D)                                       # 转强第 D 天回到满仓
day_exposure = weak_exposure + (max_total_exposure − weak_exposure) × frac    # 仅 frac < 1
```
  实测（20 段不重叠随机段，配对）：在 `weak_exposure=0.3`（线上档位）上加 ramp 仅 **+0.12pp、t 1.10、
  4/20 段生效** —— **无效**，因为 `queue_top_m=5 × 单票 20%` 本身已经把「打满仓位」摊成好几天（自然爬坡）；
  在 `weak_exposure=0` 上 ramp3 有明确效果（H 段 11 笔/回撤 −16.48% → 13 笔/−7.57%，超额 +0.89 → +6.90），
  但该档位本身已被 #59/#60 否决，纯 ramp 贡献仅 +0.35pp（t 0.94）。
  **结论：不做进 `paper/engine.py`**，`apply.py` 标注实盘无对应项。
- **「怎么判定转强」这条线已关闭（记录 #64 §8）**：MA20 更敏感（#60：D 段 8→32 笔、假强反复）、
  双线四档（#21：t≈1.5 不显著）、ramp（#64：+0.35pp 不显著）三种改法都不改变期望——
  滞后是 MA60 的固有代价，换判定或换节奏只是把收益在不同市况间搬动。

### 4.7 止损 / 止盈 / 盈利保护

> **参数维度已关闭（记录 #61）**：`take_profit` 0.30/0.40、`stop_loss` 0.05/0.06、
> `stop_atr_mult` 1.5、`single_position_ratio` 0.25 及其组合（含 `tp40_sl5` 的 8:1 盈亏比），
> 在 A~D+G/H 上没有一个稳定胜出；15 段不重叠随机段里最优组合 `tp40_sl5` 配对中位 **−1.36pp**、
> 胜段 33%。机制：盈亏比提高会把胜率同步压低（日胜率 42.9% → 7.1%），期望不变；
> 收紧止损还会把「先回撤再上涨」的票提前踢出（同 `be8`/`tp10`）。因此**不要用调出场线的方式
> 追求「小亏换大赚」**；20% 止盈 / 8% 止损 / 单票 20% 继续作为默认值。
```
止损距离 _stop_distance = max(paper_stop_loss(0.08), min(N × ATR/close, paper_atr_stop_max(0.20)))
                       N = paper_atr_stop_mult（默认 2.0；<=0 关闭 ATR 放宽 → 只用 paper_stop_loss）
建仓时一次定死：stop_price = round(cost_price × (1 - _stop_distance), 3)，peak_price = cost_price

逐 tick 检查（价格优先取实时报价）：
  1) peak = max(peak, price)                     # 持仓期最高价，落库
  2) base = stop_price（老持仓无该字段时退化为 cost × (1 - paper_stop_loss)）
  3) 保本：peak ≥ cost × (1 + paper_breakeven_pct 0.10) → base = max(base, cost)
  4) 移动：peak ≥ cost × (1 + paper_trail_arm 0.00)     → base = max(base, peak × (1 - paper_trail_pct 0.10))
  5) price ≤ base  → 止损卖出；price / cost - 1 ≥ paper_take_profit(0.20) → 止盈卖出
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

**上线期口径（2026-10-31 复核）**：`config.py` 默认、`BACKTEST_LOG` #66、本节均要求关闭；某次「系统设置」操作
把 `app_state.cfg:paper_t_active` 置成了 `1`（覆盖层优先级高于 `.env`/默认值，页面显示「运行时」），已改回 `false`。
所以**判定某个开关是否生效要看 `/settings` 的 `source` 字段，不能只看 `.env` 或 `config.py` 默认值**——
`cfg:` 覆盖层会静默压过它们。重开条件见 README §「做T引擎」：先有实测滑点，再用 `t_minute_path=True` 重跑配对实验。
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

**回测口径 `t_minute_path=True`（记录 #65，2026-10-30 起做T 实验的唯一合法口径）**

```
取当日 5m 收盘序列（get_minute_day_bars，纯本地；无数据 → 当天不做T，不回落日K）
day_open = 首根 5m 的开盘（缺失用首根收盘）
逐点推进（与线上 t_tick 同序，每点只做一个动作）：
  ① 待买回（当日卖出）：px ≤ max(day_open, 昨收) × (1+t_up_buyback) → 买回；crash 当日不接
  ② 涨停卖半：px ≥ 昨收 × 1.095 且可卖底仓 ≥ 100 → 卖一半（t_half_date 按日重置）
  ③ 倒T：day_open > 昨收 且 px ≥ day_open × (1+t_up_sell) → 卖 t_size 底仓
  ④ 低开日（day_open < 昨收）：反弹减仓（只减昨日及更早的 t_add 批次）→ 分级加仓（每日一次）
收尾：仍待买回 → t_buyback_shares（次日开盘无条件恢复底仓）
```
- **crash guard 只用开盘跌幅**：`指数开盘 / 前一日收盘 ≤ −t_crash_guard`，不用当日收盘（未来函数）；
  代价是盘中闪崩捕捉不到（线上有实时快照，回测没有指数分钟数据）。
- **T+1**：当日加仓记 `t_today_added`，反弹减仓只减「昨日及更早」的批次；卖出按股数且不越底仓。
- **为什么必须用这个口径**：同参数下日K四价版（`t_minute_path=False`）在 C（涨市）段给出 **−14.31%** 超额，
  分钟路径版是 **+2.11%**，均值差 5pp —— 路径假设对做T 是量级性的，日K版结论不可引用。
- **采样周期 `t_minute_period`（"5"/"1"）**：1 分钟线每天 240 个价格点（5m 为 48）。同一套逻辑与参数下，
  5m → 1m 使配对均值 −0.54pp、**t 2.16 → 1.41**、胜段 66.7% → 55.6%（记录 #66）。
  结论**随采样变细单调下降**，且边际交易为负贡献（笔数 +6%、收益 −0.54pp）——
  做T 这一族据此关闭，线上 `paper_t_active` 保持 False。

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
- **交易队列容量（五段式③）**：`paper_trade_queue_top_m`（默认 5）——待执行**买单**最多同时挂 M 条，
  同一批按 14 因子总分降序择优入队；超额的不入队，原因写入 `queue_rejected`
  （`GET /api/paper/live` 暴露，前端「④ 交易池」页以「未入队 名称/分值」展示）。
  卖出信号不受限（释放风险优先）；同一只票更新建议不重复占位；`0` = 不限制（旧行为）。
- **自动 / 手动模式**：手动模式只刷新报价与展示建议，不执行任何买卖（含止盈止损），由用户在模拟交易页手动下单。

---

## 6. 关键可调参数清单

> 本表是**文档口径**的参考值。要查「当前实际在跑什么」、或直接改，用 `/settings` 页
> （`GET/POST /api/settings`）——它由 `app/param_specs.py` 从 `Settings.model_fields` + 源码注释生成，
> 标注每项当前生效值 / 来源（app_state 覆盖 > .env > 默认）。只列可编辑项：路径/列表类
> （`db_path`、`kline_dir`、`cors_origins` 等）与剩余硬编码常量（`LOT_SIZE`、板块前缀、
> 巨无霸名单、回测默认止盈止损）不返回、不显示——改不了的项摆出来只会让人以为能调。
>
> 写入路径：页面改 → `app_state` 覆盖层（`cfg:<key>`，大盘股那几项沿用既有无前缀键）
> → **就地 setattr 到 `Settings` 单例**（所以下一个 tick 生效）→ 重启时
> `apply_persisted_overrides()` 重放。路径/列表类（`READONLY_KEYS`）不可改，标「需重启」。
>
> 新增配置项时**只需在 `config.py` 写好注释**，设置页会自动带出中文标签与控件类型。

| 配置项 | 默认 | 含义 | 出处 |
|---|---|---|---|
| `paper_initial_capital` | 100000 | 模拟账户初始资金 | engine |
| `paper_auto_trade` | True | 模拟交易总开关 | engine/live |
| `paper_live_enabled` | True | 实时撮合引擎开关 | live |
| `paper_max_positions` | 10 | 最大持仓只数 | engine |
| `paper_max_total_exposure` | 0.80 | 总仓位上限（留 20% 现金） | engine |
| `paper_stop_loss` | 0.08 | **固定止损**（相对含费成本；回测 `stop_loss` 同口径，可落盘） | engine |
| `paper_take_profit` | 0.20 | **固定止盈**（相对含费成本；回测 `take_profit` 同口径，可落盘） | engine |
| `paper_single_position_max` | 0.20 | **单票上限**（占总资产；回测 `single_position_ratio` 同口径，可落盘） | engine |
| `paper_full_score` | 40 | 评分全额系数阈值 | engine |
| `paper_half_score` | 25 | 评分半额系数阈值（低于只记录） | engine |
| `paper_rotation_enabled` | **False** | 满仓轮动替换开关 | engine |
| `paper_rotation_min_score / gap / max_per_day` | 40 / 20 / 1 | 轮动门槛、分差、每日次数 | engine |
| `paper_backlog_enabled / max` | True / 30 | 候补队列开关/上限 | live |
| `paper_trade_queue_top_m` | 5 | 交易队列**买入**容量 topM（0=不限，卖出不受限） | live |
| `paper_exclude_bj` | True | 排除北交所 | engine |
| `paper_pool_top_n` | 300 | 成交额 TopN 池（对齐回测，0=不限） | engine |
| `paper_exclude_large_cap` | True | 大盘股开关（页面上可改，写 `app_state` 覆盖） | engine/scanner |
| `paper_large_cap_yi` | 100 | 总市值阈值（亿元），> 该值不进队列 | engine/scanner |
| `paper_atr_stop_mult / max` | 2.0 / 0.20 | ATR 止损放宽倍数/上限（0=只用 `paper_stop_loss`） | engine |
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
| `scan_auto_watch_limit` | 10 | **二选**：从股票池按技术面评分取前 N 加自选（一选=signals 全量） | scanner |
| `paper_entry_max_chase` | 0.015 | **⑤ 不追高**：现价高于信号价超此比例不买 | engine |
| `paper_entry_pullback` | 0.0 | **⑤ 回踩**：要求现价低于信号价此比例才买（0=不限） | engine |
| `paper_entry_timeout_minutes` | 0 | **⑤ 超时**：买单信号超时放弃（0=沿用 `paper_signal_ttl_minutes`） | live |
| `paper_scan_autotrade / _top_n / _overnight` | True / 10 / True | 选股联动自动交易 | scanner |
| `factor_buy_threshold` | 15.0 | **买入阈值**（总分 ≥ 即 buy；须 > 卖出阈值） | factors |
| `factor_sell_threshold` | -15.0 | **卖出阈值**（总分 ≤ 即 sell） | factors |
| `trade_fee_commission_rate` | 0.00025 | **佣金费率**（双向，万2.5） | engine + pipeline |
| `trade_fee_commission_min` | 5.0 | 单笔最低佣金（元，免五填 0） | engine + pipeline |
| `trade_fee_stamp_rate` | 0.001 | 印花税（仅卖出，千1） | engine + pipeline |
| `trade_fee_transfer_rate` | 1e-05 | 过户费（双向，万0.1） | engine + pipeline |
| `FACTOR_WEIGHTS` | 见 §3.2 | 评分权重（14 因子，改需改代码） | factors |

---

## 7. 与回测（`backtest/`）口径对齐的设计要点

### 7.1 已对齐
- **成交时点**：实盘买入收敛到开盘窗口（默认 09:30 起 5 分钟内），与回测"次日开盘价成交"对齐。
- **股票池**：实盘买入准入强制"当日成交额 TopN 池"（`paper_pool_top_n=300`），与回测每日成交额 TopN 同一口径。
- **范围过滤**：实盘 `_buy_allowed` 与回测 `board_prefixes` / `scope.is_tradable` 同源，回测选不到的票实盘也买不到。
- **大盘股过滤是实盘独有（回测无法建模）**：`total_mv` 只存在实时快照里，`data/klines/` 没有市值字段，
  回测池子不按市值剔除。所以线上开着 100 亿阈值时，实盘的可选集合比回测**窄**，
  两边的超额不能直接对读——对比时应把回测限制在「当日市值 ≤ 阈值」的子集，或临时关掉开关。
- **评分模型**：`compute_factors` 在选股建议与回测中复用同一套权重与阈值，外部 `weights` 仅做参数实验，自动归一化回 100 分制。
- **止损口径**：回测止损用 `max(stop_loss, N×ATR%)` 建仓时定死，与实盘 `_stop_distance`（`max(paper_stop_loss, N×ATR%)`）一致；两侧默认都是 8%，且都能改（回测走 `stop_loss`，实盘走 `paper_stop_loss` / 落盘）。

### 7.2 回测侧新增的日内建模（均为可选，默认关闭=线上行为）
| 参数 | 默认 | 含义 |
|---|---|---|
| `intraday_minutes` | False | True 时对持仓日读真实 5 分钟线（`data/minute.py` → `data/minute5_fsdb`，全市场 5563 只、2025-01-02 起、纯本地不联网，按 code 缓存「日期→切片下标 + close 数组」，首次 ~5ms、之后 ~0.02ms/次），按时间顺序判定止损/止盈/分段止盈首次触发，成交价取分钟价；False 时用日K 边界序列 `open→low→high→close`（low 在 high 前，天然实现「止损优先」，但成交价直接取止损价，**系统性偏乐观**，见 §7.3） |
| `slippage` | 0.0 | 单边滑点：买价 ×(1+s)、卖价 ×(1−s)，涨跌停判定仍用原始价 |
| `t_active` 及 `t_*` | None | 做T引擎回测：涨停卖半 / 上涨倒T / 下跌分级加仓（预算 = 现金×t_down_add，满仓档 现金×0.95）/ 反弹减仓（只卖昨日及更早的加仓批次）。**必须配 `t_minute_path=True`**（见下行）；`t_minute_path=False` 的日K四价版已证伪（C 段 −14.31%），仅供历史对照 |
| `t_minute_path` | False | 做T 走当日 5m 序列逐点推进（与线上 `t_tick` 同序，含「当日回落即买回」）；无本地分钟数据的票当天不做T。**记录 #65：做T 实验的唯一合法口径** |
| `exec_fill` | `"open"` | 开盘段成交价口径：`"open"` = 开盘价（历史口径）；`"vwap1m"` = 开盘后 `exec_fill_minutes` 分钟的 1m 成交量加权均价。只改「以开盘价成交」的动作（开盘段买入、开盘瞬间的止损/止盈成交价），盘中触发价与能否成交的判定不变。无 1m 数据回落开盘价。**记录 #67：`vwap1m` 使 18 段超额系统性 −1.4~−2.5pp（t −2.60），故不作默认** |
| `exec_fill_minutes` | 30 | VWAP 窗口分钟数（1/5/30 均测过，无单调性，说明差异来自路径依赖而非「买得更晚」） |
| `fill_noise_bps` | 0 | 成交价随机扰动 σ（bp），每笔成交价乘 `1 + N(0, σ/10000)`，双向、均值 0。用于稳健性检验（记录 #68） |
| `fill_noise_seed` | 0 | 扰动种子（同种子完全可复现） |
| `dd_pause_at` | None | 组合级回撤断路器：收盘净值相对峰值回撤 ≥ 该比例 → 次日起暂停新开仓（`buys=[]`，轮动同停；卖出/止损/止盈照常）。**记录 #69：18 段中位差 +0.00、胜段 2/18、最差段未改善，不采纳** |
| `dd_pause_days` | 10 | 暂停买入的交易日数 |
| `partial_tp_at / partial_tp_ratio` | None / 0.5 | 分段止盈：首次达到该涨幅卖出一部分 |
| `queue_top_m` | None | ③ 队列容量：每日买单笔数上限（按评分截断） |
| `entry_max_chase` | None | ⑤ 不追高：次日开盘相对信号日收盘的涨幅上限 |
| `entry_pullback` | None | ⑤ 回踩：要求次日开盘 ≤ 信号日收盘×(1−x)，即低开才买 |
| `entry_window_minutes` | None | ⑤ **分钟级切入点**：需开 `intraday_minutes`；买入日开盘后前 N 分钟的 5 分钟收盘价里等回踩，以首次触及价成交，等不到则当天不买。只读本地 `data/minute5_fsdb` 缓存（保证可复现） |
| `entry_confirm_minutes` | None | ⑤ **分钟级强势确认**（`#54` 的反向假设）：需开 `intraday_minutes`；只观察开盘后前 N 分钟（N=30 → 前 6 根 5m），满足 `entry_confirm_mode` 才以**窗口末收盘价**成交，否则当天不买。无分钟数据的票直接放弃（不回落开盘价）。**默认关闭**：2026-10-30 按验收协议证伪（记录 #57） |
| `entry_confirm_mode` | None | 确认口径：`hold_open`=窗口内 5m 收盘从未跌破当日开盘价；`above_avg`=窗口末收盘 ≥ 当日累计成交均价（Σamount/Σvolume，真 VWAP）；`both`=两者同时 |
| `intraday_entry` | False | ⑤ **全时段买入**：True 时除开盘成交外，还在 `intraday_entry_times` 各时点用**当日截断日线**重算 14 因子，仍判 buy 就以该时点 5m 收盘价成交（复刻实盘盘中分析）。候选只取前一交易日已在自选池判 buy 的票，历史部分只喂**原始 OHLCV**（剔除整表算的指标列）→ 无未来函数。**默认关闭**：2026-10-30 按验收协议证伪（记录 #58） |
| `intraday_entry_times` | 09:45/10:30/11:15/13:30/14:30 | 上述重算/尝试时点（`HH:MM`）。午休不产生 bar，`_intraday_bar_count` 按交易分钟折算（13:30 = 第 30 根） |
| `intraday_entry_top_n` | 10 | 每个时点最多重算多少只候选（按昨收评分降序），用于控制 CPU 成本 |
| `open_fill_minutes` | 0 | 开盘窗口「开板补单」：开盘一字涨停时，窗口内某根 5m 收盘 < 前收×1.095 就以它成交（对应线上窗口内每 5 秒撮合）。0=只用开盘价（历史口径）。无分钟数据返回 None（保守判买不到） |
| `breakeven_trigger / trail_pct / trail_arm` | None | 保本止损 / 移动止损（回测侧） |

`_intraday_first_trigger(series, stop, tp, partial)` 返回首个触发的 `(type, price)`，
三者同时命中时取**序列中最先出现**者；日K 边界模式忽略返回价，改用 `min(开盘, 止损)` / `max(开盘, 目标)`。

### 7.3 已知偏差（对照时需注意）
- **做T护栏（已于记录 #65 修复）**：旧实现读 `idx_close[date]`（当日收盘）判跌幅 = 未来函数，
  等于「提前知道今天大跌所以全天不做T」。现改为 `指数开盘 / 前一日收盘 ≤ −t_crash_guard`（开盘时已知）。
  修正后 C 段 `t_low_min` +7.62% → +1.49%、`t_dip_min` −1.74% → −19.52%，**旧口径的做T 结论一律作废**。
  残余偏差：回测抓不到盘中闪崩（无指数分钟数据），线上有实时快照。
- **做T 的摩擦敏感度（已量化为弹性）**：做T 把成交笔数抬到 2 倍以上（18 段 287 → 623 笔）。
  1m 口径下加 0.3% 单边滑点，均值 +0.92% → −1.87%，即 **每 0.1% 滑点 ≈ −0.93pp**，
  **滑点 ≥0.10% 收益即归零**；而 `#35` 实测开盘 30 分内非触发单已是 −6.7bp。
  5m 口径（+1.46% / t 2.16）与 1m 口径（+0.92% / t 1.41）对比表明：
  唯一的「显著」结果是「5m 采样 + 零滑点」的双重乐观组合，记录 #66 据此关闭做T 方向。
- **「按开盘价精确成交」是乐观假设（已量化，记录 #67）**：把开盘段成交价换成开盘后 1/5/30 分钟
  1m VWAP（偏离通常仅 0.1%~0.3%），18 段配对超额分别 **−2.48% / −1.37% / −2.00%**
  （t −2.60 / −1.69 / −2.14，胜段 16.7%~33.3%，**三档方向一致为负**）。
  基准是全池 buy&hold，不涉及开盘成交，所以这是**真实的信号损失**，不是基准被一起改了。
  含义：**所有「超额 +X pp」的绝对值系统性偏高约 1.4~2.5pp**；相对排序不受影响（同一口径内配对），
  但绝对值 <2.5pp 的结论（如 `t_low_min` +1.46、`ramp` +0.35、`pause_ramp3` +0.98）落在偏差区间内，
  不能当作 alpha 证据。窗口层面无单调性（C 段成交价差 0.1%、超额差 24pp），
  说明该策略对执行价极度敏感——收益由少数右尾票决定（同 #61）。
- **成交价扰动下的分布（记录 #68，验收基准）**：固定 `default`，只对成交价加 `N(0, σ)` 扰动，18 段中位超额：

  | 批次 | 均值 | 最小 | 最大 | 极差 |
  | --- | --- | --- | --- | --- |
  | 无扰动 | +4.12% | — | — | — |
  | ±10bp × 10 种子 | **+2.23%** | +0.66% | +4.10% | 3.44pp |
  | ±20bp × 10 种子 | **+2.10%** | +1.25% | +3.12% | 1.87pp |

  两条硬结论：① **20 个样本全部为正**（未跨 0）→ 方向性 alpha 真实存在；
  ② **baseline 4.12% 落在 ±10bp 分布的上沿**（最大值恰为 4.10%）→ **真实量级 ≈ 2.1pp / 21 天**，
  与 #67 的 VWAP 结论（−1.4~−2.5pp）独立互证。因此**验收门槛**：
  配对差必须 **>3.5pp / 21 天**才算超出度量噪声；`#59~#68` 所有参数改进（+0.35~+1.46pp）均低于此门槛。
- **候补与轮动**：回测无候补队列，满仓/资金不足直接丢弃（实盘会顺延到窗口内补位）。
- **交易队列容量**：回测没有队列概念，买单数只受剩余名额（`max_positions - 持仓`）约束；线上额外限
  `paper_trade_queue_top_m`(5) 只买入候选。M 小于剩余名额时线上更保守，回测收益会略高估
  （满分票扎堆的日子尤甚），对照时需注意。
- **止损止盈触发**：回测用日线 `low/high`（或分钟线）判定，实盘每 5 秒用最新价判定，
  盘中被扫的止损在两端不完全等价。
- **日K 边界模式的止损价偏乐观**：不开 `intraday_minutes` 时，止损按「触发价=止损价」成交，
  而真实分钟序列里穿越止损价的那根 5 分钟线收盘常更低。2026-10-30 A~D 四窗口实测（记录 #56）：
  开 `intraday_minutes` 后平均超额 5.05% → 4.28%、3/4 窗口更差、回撤普遍被低估 1~5pp，
  即**历史日K 边界口径的结果系统性偏乐观**。对比历史记录时需注意这一点。
- **分钟数据覆盖**：`minute5_fsdb` 覆盖 5563 只、2025-01-02 起；日K 有而分钟缺的 171 只中，
  69 只在 2026 年仍有日K（北交所 920xxx、B 股 200xxx 等），这部分票在 `intraday_minutes=True` 时
  会静默回落到日K 边界。2024 及更早窗口无分钟数据，该开关等于无效（不会报错）。
  **注意起点是导出脚本的常量而非数据源限制**：`scripts/fetch_minutes_fsdb.py` 顶部 `START="20250101"`
  写死，所有文件都是 440 天 × 48 根；原始 leveldb（`D:\Backup\SAS\fsdb\stockdb`，25.3GB）的时间深度
  另需探测。已支持 `--start/--end/--min-rows`，可把早期 5m 导到独立目录后用 `SAS_MINUTE5_DIR` 指过去
  （例如 `minute5_hist`），日K 侧 `klines` 已覆盖 2022-09-01 起，因此 2023/2024 窗口只要补齐分钟数据
  就能做分钟口径对照。
  **但按用户口径（不炒长线）已定稿不补**：凡依赖分钟线的实验，评估区间一律从 2025-01-02 起算；
  E/F（2024）只用于日K 口径的普跌/普涨稳健性复验，不参与分钟级实验（记录 #58 §9）。
- **指数 K 线新鲜度**：`regime_ma` 依赖 `idx_sh000001.parquet`，该文件由开机预热与 16:00 日更刷新；
  滞后时会静默退化为「不弱」档。
- **买入时点（线上窗口 vs 回测单点）**：回测把「次日开盘」压缩成一个成交价点；线上是
  09:30 起 `paper_buy_open_window_minutes`(5) 分钟内每 5 秒撮合一次，开盘一字板后开板仍有机会。
  `open_fill_minutes` 可复现这段窗口：2026-10-30 A~D+G/H 实测与 default **逐位相同**
  （仅 B 窗口多 2 笔、均值 −0.12pp）→ 说明「窗口太窄」不是收益瓶颈，建议按协议不采纳（记录 #58）。
- **盘中新信号无法回测**：线上盘中分析每分钟重算 14 因子并立刻成交，回测只能近似
  （`intraday_entry`：把当日日线截断到某时点后重算）。实测该近似整体更差（配对中位 −1.18pp、
  正占比 33%），且 `iad_0945` 与 `iad` 逐位相同——入场信息在开盘 15 分钟内已耗尽。
  因此线上「全时段可买」与回测口径**不可直接互证**，结论只能作为否决依据，不能当作增益承诺。

### 7.4 回测规则落盘（2026-10-30）

回测中心的两条路径必须分清：

| 操作 | 影响范围 | 落库 |
|---|---|---|
| 改参数 + 「开始回测」 | 只有本次回测（`PipelineParams` 覆盖） | 否 |
| 「将规则落盘保存到数据库」 | 选股 + 模拟盘交易（`Settings` 单例 + `app_state` 覆盖） | 是，重启保留，成为新默认值 |

映射表在 `app/backtest/apply.py` 的 `BACKTEST_TO_SETTING`，**只映射实盘真有落点的子集**：

```
stop_loss→paper_stop_loss            take_profit→paper_take_profit
single_position_ratio→paper_single_position_max
stop_atr_mult→paper_atr_stop_mult    stop_atr_max→paper_atr_stop_max
breakeven_trigger→paper_breakeven_pct   trail_pct/trail_arm→paper_trail_pct/trail_arm
max_positions→paper_max_positions    universe_size→paper_pool_top_n
buy_threshold→factor_buy_threshold
max_total_exposure/regime_ma/full_score/half_score/rotation_*→同名 paper_*
queue_top_m→paper_trade_queue_top_m  entry_max_chase/entry_pullback→paper_entry_*
t_*→paper_t_*
```

不映射的项一律在响应 `skipped` 里给出原因（前端原样展示），典型是：

- **选股侧回测专属**：`board_prefixes`（实盘只支持排北交所）、`rebalance_days`、`buy_top_pct`、
  `max_gain_5d`、`min_volume_ratio`、`min_turnover_rate`、`invert_score`；
- **卖出侧回测专属**：`max_hold_days`、`stop_atr_min`、`stop_confirm_close`、`partial_tp_at/ratio`；
- **择时分档**：`regime_fast_ma`、`weak_exposure`、`regime_flat_below`（实盘只有单均线 + 弱市暂停买入）；
- **账户级**：`initial_capital`（已有账户不会被重置）。

两条约束保证「落盘 = 所见即所改」：

1. `POST /backtest/persist` 只写调用方**显式提交**的字段（`model_fields_set`），
   不会被 Pydantic 默认值（如 `universe_size=500`）被动覆盖实盘配置；
2. 前端在确认框里列出「会变更的项：旧值 → 新值」，落盘后 `GET /backtest/defaults` 的基线随之刷新
   （止损/止盈/单票上限现在读 `Settings`，不再写死）。

---

### 7.5 实盘对账口径（`scripts/live_review.py`，2026-10-30）

回测的「超额」一直是相对**等权基准**的，而实盘账本只有绝对收益——两边口径不通，
`BACKTEST_LOG #70 §6` 那条判读规则（21 天超额 −1pp ~ +5pp）在补这层之前无法执行。

```
universe(d) = 当日成交额降序前 N（跳过 idx_* 指数文件，不做板块过滤）
bench_ret(t) = mean( close_i(t) / close_i(t-1) - 1 )，i ∈ universe(前一交易日)
bench_cum    = Π (1 + bench_ret(t))，从首个净值快照日起算
超额(pp)     = (策略区间收益 − 基准区间收益) × 100
```

- **滞后一天是防未来函数**，不是保守：当日成交额排名收盘后才知道，用当日池算当日收益
  等于拿成交额（与当日涨跌强相关）挑赢家。与 `pipeline.py` 的 `bench_codes = universe[dates[day_idx-1]]` 完全一致。
- **两个池都报**：回测 `PipelineParams.universe_size` 默认 **500**，线上 `paper_pool_top_n`
  默认 **300**。冻结的 ≈+2.1pp（#68）是相对 500 的，而实盘只买 300 内的票——差异本身是信息。
- **净值快照断档时逐日累乘**：`paper_equity` 缺某天不会让该天的基准收益消失，只把策略侧
  变成区间收益；否则会系统性漏掉断档期间的基准波动。
- 判读（`#70 §6` 冻结，先写死避免事后解释）：**−1pp ~ +5pp** = 与回测一致；
  **< −1pp** 先查执行价与成交可行性（#67 量化 1.4~2.5pp），不要调策略参数；
  **> +5pp** 视为短期运气（#68 噪声极差 3.44pp）；**< 21 个交易日不下结论**。

### 7.6 打分层（ML 截面排序）与回测中心口径（2026-10-31）

打分是**唯一被替换的环节**，信号阈值、风控、择时、下单窗口全部沿用，便于单变量对比。

```
scoring_mode = factors   14 因子启发式（基线，即 #72 的 default）
             = ml        段前训练 → 当日截面分位(0~100)，用分位替代总分
             = ml_blend  ML 分位与 14 因子归一分按 ml_blend 加权融合
```

- **训练/预测切分**：训练集 = klines 320 根尾部中 `start_date` **之前**的截面，
  预测集 = 段内日期。段前走前，无未来函数。模型默认 LightGBM（65 特征 Alpha158 风格 +
  Purged Group 时序 CV 选参），`ml_model=ridge` 为线性降级路径。
- **回退纪律**：`oos_rank_ic <= ml_require_ic`（默认 0）时**整段回退 14 因子**，
  `pred_pairs=0`，不注入噪声。这是设计而非故障。
- **`ml_diagnostics`**（结果里必看）：`mode / model / label_horizon / industry_neutral /
  oos_rank_ic / pred_pairs / effective`。**`effective=false` 说明这次跑的是 14 因子**，
  无论 `scoring_mode` 填的是什么——没有这个字段时二者在结果里完全无法区分。
- **区间长度**：短区间（如 12 个交易日）只有 12 个截面，rank-IC 估不出来，几乎必然回退。
  要看 ML 至少半年；随机段评估用 `scripts/random_dist.py`（`#76` 的 18 段 × 21 天）。
- **默认跟随实盘**：`/backtest/defaults` 的 `scoring_mode` 由 `live_ml_scoring` 决定
  （开 = `ml`），避免「回测中心跑 ML、实盘还在 14 因子」这种口径漂移；`universe_size`
  同理跟随 `paper_pool_top_n`（300），不再写死 500——池子不同则候选集、选中票、
  超额基准全都不同，结论无法互相解释（`#79` §7）。
- **落盘折算**：`scoring_mode=ml|ml_blend` → `live_ml_scoring=True`；`ml_model` /
  `ml_label_horizon` / `ml_blend` / `ml_require_ic` / `industry_neutral` / `disable_limit_mask`
  都是**回测专属**，实盘固定 lightgbm + 行业中性 + IC 回退 + 涨跌停约束，不落盘。

**线上状态（2026-10-31 复核后关闭）**：`live_ml_scoring` 在 **覆盖层与 `.env` 两处**都已置 false
（只改一处不够——「还原为 .env」会把开关重新打开，这类「两处口径」问题同 §4.9 的做T 开关）。
关闭依据：① `#79` 实测 `effective=false`，即「ML 分位直接当 score 用」**至今没有一次有效回测**；
② 实测线上首次构建 **109s**（152 只 K 线 + 4 超参 × 4 折 LightGBM），而 `get_kline_cached` 每次调用
都联网增量拉取、`get_live_ml_scores` 再读一遍全部 K 线 → 每轮分析请求翻倍；③ `industry_map.csv`
不存在时 `industry_neutral=True` 静默退化为**板块中性**（4 组，主板约 140 只等于未中性化）。
训练/预测池是「当前自选 152 只」，存在 selection bias，2026-10-30 实测 OOS IC 0.0658 偏乐观。

**分布对照（勿再误判成「量纲错配」）**：同日、同批 152 只实测——

| 打分 | min | p25 | p50 | p75 | max | ≥25 | ≥40 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 14 因子 `total` | −14.5 | 20.8 | 38.8 | 46.7 | 61.2 | 71.1% | 48.7% |
| ML 截面分位 | 0.0 | ≈25 | 50.4 | ≈75 | 100.0 | 75.0% | 60.0% |

两者同为 0~100 量纲、分布可比（ML 略宽松），`paper_half_score=25` / `paper_full_score=40`
**不需要为 ML 单独调整**。口径注意：14 因子必须作用在 `add_all_indicators()` 之后的 frame 上；
直接喂 `get_kline_cached()` 的原始 df，会让 `bias6`/`atr` 等预计算列缺失、多数因子退化为 0，
`total` 被压进 ±20——2026-10-31 曾据此误判「线上买不进任何票」，属口径错误而非策略缺陷。

## 8. 自动交易五段式（目标架构）与现状差距

> 2026-10-29 由用户明确的架构定义。**目前未实现**，本章先固化目标与差距，后续改造按此对齐，
> 改完须同步 §4/§5 与参数表。

### 8.1 目标链路

| 段 | 目标 | 产出 | 核心问题 |
| --- | --- | --- | --- |
| ① 一选 | 全市场扫描，选出合适股票进 **股票池** | 股票池（当日合格候选，宽口径） | 从 5000+ 只里筛出「形态成立」的 |
| ② 二选 | 池内按策略打分排序，按事先约定取 **topN** 进 **自选股** | 自选股（N 只） | 谁更值得盯 |
| ③ 三选 | 详细分析自选股，取 **topM** 进 **交易队列** | 交易队列（M 只） | 谁更值得买 |
| ④ 进池 | 按**判定规则**决定哪些进 **交易池** | 交易池（可下单候选） | 现在能不能买（环境/仓位/资格） |
| ⑤ 成交 | 满足**切入点 + 切入时间**才真正成交 | 成交 | 什么价格、什么时候切 |

**关键约束（⑤ 的含义）**：topM 进队 ≠ 一定成交。队列只是「候选」，还须通过交易池判定，
且切入点/切入时间不满足就不买（例如开盘已冲高 5% 不追、跌势未止不接、当日窗口已过）。

### 8.2 现状映射（`screener` / `analysis` / `paper`）

| 段 | 现状实现 | 差距 |
| --- | --- | --- |
| ① 一选 | `scanner` 全市场或 `top_amount:N` 扫描，命中全部写 `signals`（含 `score/rank`）= **股票池** | ✅ 2026-10-29 已分离：一选只落 `signals` |
| ② 二选 | ✅ 已分离：`_auto_watch_hits` 从池内按技术面评分取 Top`scan_auto_watch_limit`(**10**) 加自选 | 池 → 自选两级收口已完成 |
| ③ 三选 | 14 因子对**全部自选**打分 → 买卖建议 → `live_trader.submit` 入队；**2026-10-29 已补 topM 收口**：买入按总分降序只留前 `paper_trade_queue_top_m`(5) 条，超额记 `queue_rejected`；`paper_scan_autotrade_top_n`(10) 只限制「扫描后立即分析」的只数 | 自选仍可能一次全部分析（属计算成本，不入队），队列层已收敛 |
| ④ 进池 | ✅ 已统一：`entry_gate()`（择时/已持仓/`_buy_allowed`/名额）+ `entry_price_ok()`（价格/切入点）+ `entry_check()` 组合入口；评分分档与预算在分档阶段 | 规则仍分两段（取价前/取价后），但已有单一入口与统一 skip 原因 |
| ⑤ 成交 | ✅ 已补切入点：`paper_entry_max_chase`(1.5%) 不追高、`paper_entry_pullback`(0=不限) 回踩、`paper_entry_timeout_minutes`(0=沿用 TTL) 超时；配合开盘窗口 + TTL | 无分时强度/回踩确认；日线粒度下「切入点」只有价格带这一层 |

### 8.3 改造建议（按最小改动排序）

| # | 内容 | 参数/接口 | 影响面 |
| --- | --- | --- | --- |
| 1 | **队列收敛 topM** ✅ 已实现（2026-10-29）：每次分析后，买入建议按 14 因子总分降序只取前 M 入队，其余记 `queue_rejected`，前端 ④ 页展示「未入队 + 分值」 | `paper_trade_queue_top_m`（默认 5，0=不限）；卖出不受限；同股更新不重复占位 | 已上线，`LiveTrader.submit` 单点收口（开盘/盘中/选股联动三处调用全覆盖） |
| 2 | **交易池判定统一** ✅ 已实现：`entry_gate()` 前置闸 + `entry_price_ok()` 切入点 + `entry_check()` 组合入口；被拒原因经 `_entry_rejected` 暴露 | 无新参数，纯重构，237 项回归测试保证行为不变 | `engine` / `live` / ④ 页「切入点判定」卡片 |
| 3 | **切入点** ✅ 已实现：不追高 / 回踩 / 超时 | `paper_entry_max_chase`(0.015)、`paper_entry_pullback`(0)、`paper_entry_timeout_minutes`(0=沿用 TTL)；回测侧同名参数 `entry_max_chase/entry_pullback`（None=不启用） | 线上 `entry_price_ok` + 回测买入执行 |
| 4 | **一选/二选分离** ✅ 已实现：一选只落 `signals`，二选从池内取 Top`scan_auto_watch_limit`(**50→10**) 加自选 | 复用现有参数，仅改默认值与语义注释 | `scanner` |

> 回测结论见 `BACKTEST_LOG` 记录 #52：**三条切入点规则在样本外无稳定增益，不作为收益来源**；
> `queue_top_m` 对收益中性（作用是有界排队），`entry_max_chase` 保留为防追高的工程保护。
> 统一对照脚本：`backend/scripts/five_stage_backtest.py`。
>
> ⑤ 的**分钟级切入点**（`entry_window_minutes` + 回踩）已实现并可回测，结论见记录 #54：
> 等回踩再买表现为**逆向选择**（+20.63% → −12.74%/−20.42%/−9.59%），**默认关闭**
> （`paper_entry_pullback=0`、`entry_window_minutes=None`）。对照脚本 `scripts/minute_entry_backtest.py`。
> 线上回踩用 tick 累积「曾触及」置位（`PendingSignal.armed`），避免价格反弹一次就永久错过。

**回测一致性要求**：3 个切入点参数与 topM 都必须进 `PipelineParams`（默认 `None`=线上值），
否则线上改了、回测测不到，两边又不可比。采纳前须按「验收协议」做 ≥30 段不重叠抽样 + 两档滑点。

### 8.4 页面布局（2026-10-29 按五段式重排；2026-10-30 合并 ④⑤ 菜单项）

| 段 | 路由 | 页面 | 展示内容 |
| --- | --- | --- | --- |
| 概览 | `/dashboard` | 大盘概览 | 指数 / 涨跌家数 / 情绪 / 自选盯盘 |
| ① 一选 | `/screening` | 股票池 | 全市场扫描参数、策略选择、命中结果（`signals`）、评分名次；**自选列标注「已进/未进」**（★ 已进自选=二选选中，表头显示 `已进/池内` 计数） |
| ② 二选 | `/signals` | 自选股 | 程序自选（`source=auto`）+ 手动自选，14 因子排名入口 |
| ③ 三选 | `/advisory` | 交易队列 | 自选股详细分析、买卖建议、评分分项明细 |
| ④ 交易池 · 持仓 | `/paper`（query `tab=pool` 或 `tab=account`） | 交易池 + 持仓 | 一个页面两个 tab：**交易池**=自动/手动模式、撮合状态、待执行信号与候补队列、未入队/未成交原因（判定结果）；**持仓与成交**=账户总览、净值、当前持仓、交易流水、手动下单 |
| 复盘 | `/backtest` | 回测中心 | 7 组参数、一键预设、净值曲线、亏损归因 |
| 下钻 | `/stock/:code` | 个股详情 | K线 / 指标 / LLM 决策仪表盘（高亮归到 ②） |

左侧导航分三组：**概览 / 交易流水线（①~④）/ 复盘**。④交易池与⑤持仓本是同一页的两个 tab
（同一个 `PaperTrading.vue`、同一份数据），2026-10-30 起合并为一个菜单项，页内用 tab 切换
「判定与队列 / 账户复盘」，避免一屏混装两类信息；tab 状态写进 URL（`?tab=pool` / `?tab=account`），
旧链接 `/paper-pool` 自动重定向到 `/paper?tab=pool`。

### 8.5 五段与三次排序的对应关系（避免概念混淆）

- 三次排序是**代码里真实存在的三段**：扫描排序（±8 快评）→ 自选打分（14 因子）→ 评分加权下单；
- 五段式是**业务口径**：它把「三次排序」拆得更细，并补上代码里缺的「池 → 队列 → 交易池」三层收口
  与「切入点/切入时间」判定；
- 对齐后：①②=第 1 次排序（分两级收口），③=第 2 次排序，④⑤=第 3 次排序 + 执行择时。

---

*本表为快照，参数以代码当前实现为准；调整权重/阈值请同步 `tests/test_factors.py` 与前端"智能建议"展示。*
