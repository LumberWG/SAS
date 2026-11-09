# SAS 选股与下单逻辑 · 公式备查

> 生成日期：2026-10-29；最近修订：2026-10-31（同步至 commit `bdb84a1` 之后的实现）。
> 代码出处：`backend/app/screener/scanner.py`、`analysis/recommender.py`、`analysis/factors.py`、
> `analysis/regime.py`、`analysis/policy_floor.py`、`data/breadth.py`、`strategies/builtin.py`、
> `data/scope.py`、`data/cache.py`、`data/minute.py`、`paper/engine.py`、`paper/live.py`、
> `backtest/pipeline.py`、`monitor/scheduler.py`、`main.py`、`config.py`。
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
- 风控层：静态 MA 弱市闸门（默认路径）→ 动态机制层（calm / weak / shock / crisis，可选）→
  硬性回撤熔断（优先级最高，可选）。三者都只压低总仓位上限，不会放大；详见 §4.10。
  涨跌家数广度（`data/breadth.py`）只做观测，**不参与任何判定**（见 `BACKTEST_LOG` #86）。

---

## 1. 选股逻辑（`screener/scanner.py`）

### 1.1 候选池构建（三选一）
- `codes`：直接指定一批代码；
- `top_amount:N`：按实时成交额取前 N 只（流动性优先，前端默认 `top_amount:500`）；
- `scope`（含 `"all"`）：`filter_pool_by_scope` 按板块过滤，再经范围过滤；
  - 注意 `"all"` 走全市场不过滤分支，会触发全市场扫描（候选可达 ~4392 只，很慢）。

> **候选池来源 `get_stock_pool()`（`data/service.py`）**：东财 push2 列表（按 `total` 并行翻页）
> → 新浪列表（并行分页）→ akshare → 本地快照代码表（上交所官方列表校验沪市）。最终结果少于
> `_POOL_MIN_ROWS = 4000`（全A股约 5500 只）视为**残缺**：继续换源，且残缺结果只缓存 2 分钟、
> **不写磁盘缓存**。**为什么必须有下限**：2026-11-05 东财把单页条数 `pz` 钳到 100（旧实现按
> 「本页条数 < page_size」停止翻页 → 只取第一页 100 只深市）叠加上交所只回科创板 405 只，
> 池被缩成 505 只 → 降级快照只剩 505 条 → 快照安全阀（5590→505）拒绝覆盖 → 账户估值停在
> 11-04 收盘价。池残缺会同时污染「全市场扫描」「成交额 TopN 池」「账户估值」三条链路。

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

> **只做多，不做空**：面向散户，无融券/卖空。下文所有「卖出 / sell」= 平掉已有多头批次
> （`reduce_paper_sellable()`，上限为可卖股数，不会为负）；无持仓时卖出信号跳过（`无持仓可卖`），
> 不会产生空头仓位。回测同口径，故「空仓」= 现金等待，不是反向持仓。
>
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
盘中可投预算 available_budget = max(0, 盘中上限 - max(0, 总资产 - 现金))
         盘中上限 = 总资产 × min(1.0, 熔断/动态/弱市 各自的硬上限)
         # ↑ 2026-11-02 起 `paper_max_total_exposure`(0.80) 是**收盘目标**，不是盘中硬上限：
         #   盘中允许满仓（等价 1.0，实际只受现金约束），由 §4.4b 收盘减仓压回 80%
目标金额 budget = min( available_budget × 加权占比, 总资产 × paper_single_position_max(0.20), 现金, available_budget )
股数     shares = floor(budget / price / 100) × 100      # 整手
加权占比 = (max(score,1) × 档位系数) / Σ权重
成本价   cost_price = (成交额 + 买入费用) / 股数          # 含费成本，止损/止盈都以它为基准
```
候选多于剩余名额时只取名额内的高分股参与分钱（避免预算被买不到的摊薄）。
若「成交额 + 费用 > 现金」，逐手回退（每次 −100 股）直到买得起。

> **`paper_max_positions`(10) 是上限不是门槛**：持仓可以少于 10 只，只是不允许超过。
> 轮动（§4.4）不要求先填满名额——原来「必须满 10 只才轮动」的门槛已删除（记录 #93）。
> 注意 20% 单票上限 × 10 只 = 200% 与 80% 收盘目标在资金上不兼容：实际能否到 10 只
> 取决于当时现金与候选数量，**不要把它当成「应该持有 10 只」的预期**。

> **总资产口径 `total_asset()`**：`现金 + Σ(股数 × 最新价)`。某只票取价失败（实时快照与 K 线
> 同时取不到，如停牌、数据源熔断）时**按成本价计入**并打 warning，与 `account_detail()` 同口径。
> 此前是「取不到价就跳过该持仓」＝市值按 0 算：一个 20% 仓位的票在数据源熔断的瞬间从总资产里
> 消失，8% 组合回撤熔断当场误触发，且熔断态落库跨重启保留。回退成本价对浮亏是中性的
> （不涨不跌），比把未实现亏损一次性放大的做法更接近真实。
>
> **取价优先级**：撮合传入的实时报价 > 内存快照 `quote_map()` > 日K收盘价。批量核算
> （`total_asset()` / `account_detail()`）会先把内存快照价合成一张映射再逐只取价，
> 等价于旧实现每只持仓内部各取一次快照，但只做一次整表读取——口径不变，只是不再
> 对每只持仓各做一次 `to_thread(get_snapshot)`。

### 4.3 评分分档
- `score ≥ paper_full_score(默认40)` → 系数 1.0；
- `paper_half_score(默认25) ~ full` → 系数 0.5；
- `score < half(25)` → 只记录不下单。

### 4.4 轮动替换（**不需要满仓**，默认开启，2026-11-02 起）

**语义（2026-11-02 使用者口径，记录 #93）**：`paper_max_positions(10)` 是**上限不是门槛**——
持仓可以少于 10 只；只要出现「明显更好」的票就把最弱的那只换掉，**不必等止损/止盈**；
一次调用可连换多只，**可以把持仓全换掉**。原来的「必须满 10 只才轮动」门槛已删除。

**选弱** `_weakest_rotatable(positions, today, keep=...)`：按建仓 `score` 取最低，但排除三类持仓：

- **手动建仓**（`strategy` 含「手动」）：不打分、代表用户意图，程序不卖；
- **当日买入**（`buy_date >= today`）：T+1 卖不掉；
- **本轮仍是买入建议的**（`keep` = 买入候选 ∪ 候补候选）：同一只票同时是「最弱持仓」和
  「最强候选」是自相矛盾，实际后果是「卖掉又买回」。2026-11-03 实盘：301314 科瑞思
  **建仓分 0**（0 = 未知，不是真的差）、**当前分 100**，被判为最弱卖掉 400 股，同一 tick
  又按 100 分买回 300 股——白付 24.12+5.14 元费用，持仓还少了一手。
  结论：**建仓分缺失不能当建仓分最差用**，只要本轮还在建议买入它，就不许当卖出对象。

> T+1 的判断放在「选弱」而不是「成交前」：旧实现先选最弱、再检查它是否当日买入，于是今天
> 恰好买了最弱那只时整次轮动被挡掉——哪怕另一只持仓明明可换。

**选强** `_pick_rotation_target(...)`：换入票必须**真能买进**，否则不卖（空转卖出＝白付摩擦 +
名额空着，等于凭空降仓）。逐条门槛：

1. 未持仓、代码去重；
2. `score ≥ paper_rotation_min_score(40)` **且** `score ≥ paper_half_score(25)`（低于半额分的票
   买入阶段本来就会被丢，为它卖持仓没有意义）；
3. 比最弱持仓建仓 `score` 高 `paper_rotation_gap(20)`；
4. 交易池闸门 `entry_gate(..., ignore_slots=True)`：板块范围 / 北交所 / 大市值 / 名称 / 成交额池。
   `ignore_slots` 只为轮动而设——轮动时刻可能正好满仓，名额检查会把所有候选一律拒成
   「持仓已满」（而轮动卖出本身就会腾出名额），导致「换入票买不买得进」永远无法预判；
5. 切入点闸门 `entry_price_ok`（取实时价）：不追高 / 待回踩；
6. 卖出所得（扣费后）≥ 换入票一手 `price × 100`；
7. 不在本轮已卖出的代码集合里（`exclude`：否则「换出去又买回来」，白付两次摩擦）。

**候选来源** = 本轮买入建议 ∪ `rotation_candidates`（实盘 `live.tick` 传入**候补队列**中未持仓的
买单，且只在开盘窗口内传）。满仓时 backlog 不会进 `recommendations`（只在有剩余名额时补位），
不单独传进来，「今天没买到、分还很高」的票就永远进不了轮动视野；窗口外传则会「立刻卖 + 立刻买」，
偏离「次日开盘价」口径（记录 #19）。

> 候补队列自 2026-11-03 起还包含**入队阶段被队列容量挡掉**的票（见 §5），因此轮动候选面比以前宽：
> 只要 ≥ `half_score`，低分票（例如 26 分）同样可能触发「换入」。换手突然变多时，
> 先看 `paper_rotation_gap`（默认 20）而不是去收窄候补。

**执行（循环）**：`while 当日已轮动只数 < paper_rotation_max_per_day` →
取当前最弱可卖持仓（被换掉的即退出 `positions`）→ 找「真能买进」的替换标的 →
**校验卖出所得真能投回去**（见下）→ 卖出（严格 T+1，`reduce_paper_sellable` 只卖可卖批次）
→ 把换入票补进本轮 `buy_candidates`（候补来源的票不在其中，不补就只剩「卖」这一半）
→ 买入阶段按评分优先与仓位规则建仓；循环内每一轮重新取最弱，
`exclude` = **已卖出的票 ∪ 已被占用的换入目标**。

两条 2026-11-03 补的硬约束（都是当日实盘暴露出来的）：

1. **一个换入目标只承接一次卖出**（`consumed`）。换入票要到买入阶段才建仓，循环里它一直
   「未持仓」，少了这条会被反复选中——2026-11-03 日志：三笔轮动的换入全是 301331，
   卖 3 只共 94,687 元，实际只买回恩威 600 股（13,986 元）。
2. **卖出的钱必须投得回去**：换入票能吸收的金额
   = `min(卖出所得, 总资产×paper_single_position_max(20%), 卖出后的仓位剩余额度)`，
   若 **< 卖出所得**（或不足一手）则**不卖**。理由：「换股」不等于「降仓」——弱市把总仓位
   压到 30% 时，卖一只大票的钱大部分投不回去，那不是换股而是降仓，而**降仓是风控的职责**
   （由买入端的 `exposure_cap` 自然实现）。不拦的话归因会完全错位：2026-11-03 卖 3 只
   94,687 元只投回 28,182 元，仓位 96% → 28.6%，之后每分钟的高分候选全部
   「可用资金不足一手」。日志写「不轮动 X（市值 M 元）：换入 Y 最多只投回 D 元」。

> `exposure_cap` 因此从「买入阶段」提前到轮动之前计算（两处共用同一个上限，
> 买入阶段只是按卖出后的现金重算剩余额度）。

计次用 `count_paper_trades("轮动替换", today, side="sell")`，语义是**每日轮动只数**
（默认 10 ≈ 不限；0 = 当日不轮动）。回测侧同名参数 `rotation_max_per_day`（`None` = 沿用 settings），
`metadata` 落 `rotation_count` / `rotation_max_per_day`，便于区分「压根没触发」与「触发但没用」。

**回测同口径**（`backtest/pipeline.py`）：同样不再要求满仓、按 `while` 连换多只；当日买入
（`buy_date >= date`）、**已在卖出队列**里的票（后者只是空动作，却会吃掉一次配额）与
**当日仍在买入建议里**的票（2026-11-03 同步，对应线上的 `keep`）都不参与选弱；条件 1~3 一致。

**卖回来的门槛（实盘）**：轮动卖出的票当日**可以**买回，但必须**确实值得**。
判据：**当前评分 ≥ 当初换掉它的那只票的分数**。

- 依据：轮动卖出的唯一理由是「它不如那只票」（今天 600960 渤海汽车 40 分 → 换入 67 分）。
  若买回时它仍不如，买回只是否认了当初的换、白付一次摩擦；若已涨到 ≥67 分，
  说明当初卖的判断被推翻，买回成立。
- 门槛来源：卖出记录的 `reason` 里的 `新票N分`（`_rotation_target_from_reason`），
  不新增参数、不新增表；同一只票当日被卖多次取最高门槛；解析不到时退回
  `paper_rotation_min_score`（既不误放行老记录，也不永久封杀）。
- **以 `paper_trades` 为准、每交易日回库灌一次**（`_rotation_rebuy_floor()`），内存只做
  当日增量缓存。不能只放内存——重启是常态（每交易日 15:30 的 `SASRestart`、每次部署、
  盘中手动重启）。2026-11-03 吃过一次亏：12:23 部署重启清空内存后，13:00:35 同一只票被
  按 41 分买回（< 67）。回库读还顺带补上「功能当天上线、上午的卖出发生在旧代码里」的空档。
- 对外：`GET /api/paper/live` 的 `rotation_sold_today`（代码）与
  `rotation_rebuy_floor`（代码 → 买回所需最低分）。

引擎内另有两道同向闸：选弱排除「本轮仍建议买入」的票、卖出代码不进本轮买入候选。
回测侧不需要这一条：买入委托 `pending_buys` 来自当日 `buys`，而选弱已经把这类票排除掉了。

> **默认 `paper_rotation_enabled=True`**（2026-11-02 使用者决策，记录 #92）。证据边界没变：回测实测
> 轮动在 Top300 池下**几乎不触发**，而它一旦触发就是强制卖出与换手（月换手约 2.2 个资本）的来源，
> 所以开着**不是**「被证明有效的 alpha」，而是「模拟盘把规则跑全、用真实成交观察」的选择。
> 判读方式：先看日志/`paper_trade` 里 `轮动替换` 是否真的出现——不出现则本轮开启对收益无影响；
> 若出现且伴随摩擦侵蚀，把开关改回 `false` 即可（一处配置）。
> 回测侧对应参数 `rotation_max_per_day=0` 可作「关轮动」对照组（§7.2）。

### 4.4b 收盘减仓 `eod_trim()`（80% 是收盘目标，不是盘中硬上限）

```
触发窗口：收盘(15:00) 前 paper_eod_trim_minutes(默认10) 分钟，且 paper_eod_trim_enabled=True
需减金额 need = (总资产 - 现金) - 总资产 × paper_max_total_exposure(0.80)
执行顺序：按建仓 score 从低到高，跳过「手动」与当日买入(T+1)，逐只卖出
每只股数：qty = min(该只持股, max(100, floor(need / price / 100) × 100))
退出条件：need ≤ 0（减够了）或没有可卖持仓（卖不动）
```

- **只减不买**：收盘减仓不会反向加仓，纯粹把仓位压回目标、给次日做T 留现金。
- **不强求**：全部是今仓（T+1 不可卖）就只记日志（`收盘减仓：…没有可卖持仓`），
  收盘仍超 80% 不算违规——这不是硬风控，是「尽量保持」。
- **与轮动/止损的关系**：止损止损优先（先跑 `check_stop_loss_target`），轮动与买入在同一轮
  之后执行，收盘减仓最后跑；同一只票若先被轮动卖出，减仓时已不在持仓里。
- **回测差异（已知）**：回测按日线在**次日开盘**成交，`max_total_exposure(0.80)` 仍直接作为
  买入上限，且**没有实现收盘减仓**。等价于「收盘 80%」的近似：回测里开盘买入即 ≤80%，
  持仓只在上涨时被动超配；线上盘中可到 100%，收盘再压回 80%。两者长期持仓分布接近，
  但线上午盘多出的 20% 额度会让收益/回撤略偏放大，跨口径对比时应记住这一层（记录 #93）。

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
- **卖出后同步内存 `positions`**：`execute_recommendations()` 在卖出成交后立刻更新本轮的内存持仓表
  （清仓 `pop`、部分卖出改 `shares`）。该字典决定本轮剩余名额 `max_positions - len(positions)` 与
  轮动触发，只改库不减内存会让「同批卖出的票腾不出名额」，本轮后续买单全部报「持仓已满」，
  要等下一个 tick 才恢复（轮动分支一直是对的，卖出分支此前漏了）；
- 卖出/止损/做T 都必须走 `reduce_paper_sellable`，不能直接改 `shares`；
- `ensure_lots_migrated()` 启动时把历史持仓（无批次）补建为一个批次（沿用原 `buy_date`），幂等。
- **加仓不覆盖建仓属性**（2026-11-02）：做T加仓/买回走同一个 `add_paper_lot`，但它们的
  `score` 默认 0、`stop_price`/`peak_price` 为 `None`。旧实现无条件覆盖汇总行，把「建仓 88 分」的
  票写成 0 分（轮动会把它当最弱换掉）、清空建仓时定死的止损线、重置移动止损高水位。
  现在只在**传了有效值**时覆盖（`score>0` / `strategy` 非空 / 价格非 `None`），否则保留原值。

### 4.9 做T引擎 `t_tick()`（真实 tick；`paper_t_active` 默认开启，2026-11-02 起）
总开关 `paper_t_active`（`config.py` 默认**已改为 True**；`.env` 与 `app_state` 覆盖层同为 true，三处一致
—— 2026-11-02 使用者决策：模拟盘先跑机制，记录 #92），仓位比例 `paper_t_size`（默认 0.25）。

**口径提醒（沿用 #1065）**：判定开关是否生效要看 `/settings` 的 `source` 字段，不能只看 `.env` 或 `config.py` 默认值——
`cfg:` 覆盖层优先级高于它们，会静默压过。2026-10-31 曾纠正一次误覆盖（改回 false）；2026-11-02 用户决策强制开启，
把覆盖层、`.env`、`config.py` 默认值三处统一为 true（记录 #92）。重开条件（已覆盖，留存备查）见 README §「做T引擎」：
先有实测滑点，再用 `t_minute_path=True` 重跑配对实验。
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
      → 预算再与「单票剩余额度」「总仓位剩余额度」取 min（见下），
        买入 floor(预算/price/100)×100 股，tag=t_add（今仓，次日可卖）
```
- 触发用快照的 `open/high/low/prev_close`，成交价用当 tick 的实时价 `price`；
- **加仓预算三道护栏（2026-11-03 定稿，线上 `t_tick` + 回测 `pipeline` 日K版/分钟版同口径）**：
  先按现金比例算出 `budget0`（满仓档 `cash×0.95`、浅档 `cash×t_down_add`），再与下面两者取 `min`，
  最后才整手估算——避免「单票打满、组合超配」（`#93` 已指出 `cash*0.95` 把单票顶到 73%、仓位 96%）：
  1) **单票上限**：`single_room = max(0, 总资产 × paper_single_position_max(0.20) − 该票当前市值)`；
  2) **总仓位上限**：`total_room = max(0, 总资产 × effective_exposure − 当前已投入)`，
     其中 `effective_exposure` 盘中常规 = 1.0（80% 是收盘目标、由 `eod_trim` 压回），
     弱市 / 熔断 / 动态机制按下限硬压（与买入、轮动共用同一套 `effective_exposure` 计算）。
  即下跌加仓**不会**把单票推过 20%、也不会把组合推过当时生效的总仓位上限；现金不足的整手回退循环照旧。
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
     · 分级加仓预算同样先现金比例、再与「单票剩余额度 / 总仓位剩余额度」取 min（同线上 t_tick，2026-11-03）
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
  做T 这一族据此不建议**调细采样**；线上 `paper_t_active` 自 2026-11-02 起默认开（记录 #92，
  属模拟盘取舍），因此归因时要把「做T 成交」与「基础策略成交」分开看。

### 4.9.1 开盘保护（跳空护栏 + 开盘延迟 + 开盘冷静期，2026-11-03 / 2026-11-04，记录 #98 / #108）

**起因（使用者提）**：开盘就交易，若隔夜外围利空导致开盘大盘猛跌，开盘瞬间买入会「接刀」；
原系统仅 `paper_t_crash_guard` 护做T，普通买入（队列撮合、轮动换入）无开盘跳空保护，
弱市判定是日级 MA、不防当日开盘跳空。使用者建议「交易往后移一段时间」。据此落地两道闸，
**只拦买入、不拦卖出**（卖出照常）：

1. **跳空护栏（扩展 crash guard 到买入端）**：复用 `paper_t_crash_guard`（默认 2%）——大盘当日跌幅
   超阈值时，暂停**普通买入 + 轮动换入**（不仅是做T），避免开盘猛跌接刀。阈值与做T 共用，不新增参数。
   `idx_open_drop` 取最弱主要指数当日涨跌幅的负值（与做T 同口径，非未来函数）。**量纲**：
   `peek_indices()` 的 `pct_change` 是**百分点**（`-0.47` 表示 −0.47%），比较前必须 `/100` 换算成小数，
   否则 `-0.47` 会被当成 −47% 跌幅、护栏自开盘起常开（2026-11-05 实盘零成交的根因，已修）。
2. **开盘延迟 `paper_buy_open_delay_minutes`（新增，默认 0=不改行为）**：**仅在大盘向下跳空时**，
   开盘后 N 分钟内禁止自动买入（含轮动换入、做T 买回/加仓），让跳空消化；午盘 13:00 起同样延迟。
   仅拦买入、不拦卖出。**利好日（外围出利好、大盘上涨）不延迟、不暂停**——保护只针对下行风险，
   不对上行收益设障，否则会踏空误伤（使用者 2026-11-03 指出「外围也可能利好」）。方向判定
   复用 `_idx_open_drop`（最弱主要指数当日涨跌幅），与跳空护栏同口径。
3. **开盘冷静期 `paper_open_cooldown_minutes`（2026-11-04 新增，默认 0=不改行为）**：**无条件**禁止各交易时段（09:30 / 13:00）开盘后 N 分钟内自动买入（含轮动换入、做T 买回/加仓），避开开盘瞬间波动/跳空（恩威 09:30:01 开盘即买、买价远高于开盘价）。不同于 ① ② 的「只对下行风险生效」，**冷静期对利好日也生效**——它是「不机械开盘买」的硬约束，与延迟互补。仅拦买入、不拦卖出。

**实现**：`engine.execute_recommendations` 计算 `buy_paused = weak_pause_all ∨ crash_pause_buy ∨ open_delayed ∨ open_cooldown`，
统一控制轮动分支与买入入口（含准确 skip 原因，区分「开盘跳空护栏 / 开盘延迟 / 大盘弱势」，
避免误归因）；`engine.t_tick` 的做T 买入（买回、下跌加仓）也受 `open_delayed` 约束，卖出（倒T/涨停卖半）不受影响。
**不采纳**：把延迟做成「只在开盘窗口内买」——那是 `paper_buy_open_window_minutes` 的语义（线上已置 0），
不等于「延迟到之后」；真正延迟需新参数（已加）。**测试**：`tests/test_paper.py::TestOpenProtection`
覆盖跳空护栏触发/不触发、开盘延迟触发/不触发、延迟只拦买不拦卖；受真实弱势日影响的执行规则用例
在 `_disable_pool_gate` / `test_paper_sim` 的 autouse 里已隔离择时闸门（弱市/crash/熔断/动态）。

### 4.10 机制层：动态降暴露 / 政策托底 / 硬性熔断（默认全关，记录 #84、#85）

三个开关**全部默认 false**；关闭时走 §4.6 的静态 MA 闸门，代码路径与改造前一致。
优先级（高 → 低）：**熔断 > 动态机制 > 静态弱市**。三者都只**压低**总仓位上限，不会放大。

**（1）动态机制 `detect_regime()`（`analysis/regime.py`）**

```
输入 series = 指数收盘，截至前一交易日（与 §4.6 同口径，无未来函数）
vol_ratio  = std(ret, 近20日) / std(ret, 近60日)
max_dd     = −min((close − rolling_max(120)) / rolling_max(120))，取近 120 日
trend_weak = close[-1] < MA(paper_regime_ma，默认 60)

crisis : max_dd ≥ 0.12 或 vol_ratio ≥ 2.0 → cap=crisis(0.15)，pause_all=True
shock  : vol_ratio ≥ 1.4 或 max_dd ≥ 0.06 → cap=shock(0.40)
weak   : trend_weak                       → cap=weak(0.30)
calm   : 其余                             → cap=calm(0.0 = 不降仓，回落 paper_max_total_exposure)

effective_exposure = min(paper_max_total_exposure, cap)      # 只降不升
```

- 指数不足 122 根、parquet 读取失败、任何异常 → 退化为 `normal / 不降仓`（等同关闭），
  绝不因数据抖动停交易；crisis 额外置 `weak_pause_all=True`（全暂停买入，卖出照常）。

**（2）政策托底 `detect_policy_floor()`（`analysis/policy_floor.py`）**

```
必要条件：近 120 日最大回撤 < 5% → score=0 / level=none（没有下行段就无「底」可谈）
分量（各 0~1）：
  vol_compression = clip((1 − std(ret,10)/std(ret,40)) / 0.5)      # 卖压衰竭
  stabilization   = 近20日最低点相对抬升 ≥0 → clip(抬升%/0.03 + 0.5)，否则 0
  reversal_days   = clip((近10日收阳占比 − 0.4) / 0.3)
  idx_score = 0.40·vol_compression + 0.35·stabilization + 0.25·reversal_days

可选直接信号（ETF 放量 / 期指升贴水 / 官媒情绪，当前部署均无）：
  score = 0.7·idx_score + 0.3·mean(直接信号)；无直接信号时 score = idx_score，available=False
level = strong(≥0.50) / weak(≥0.20) / none
```

叠加规则（`paper_dynamic_exposure` 与 `paper_policy_floor_enabled` 同开，且基础态 ∈ {weak, shock}）：

- `score ≥ min_score(0.50)` → 升级 `managed_downtrend`，cap=`harvest_cap(0.30)` 且**允许买入**
  （托底型阴跌：缩仓收割反转，而非纯撤退）；
- `score ≤ low_escalate(0.20)` 且（基础态为 shock 或回撤 ≥12%）→ 升级 `crisis`（自由落体型防御）。

**（3）硬性回撤熔断 `circuit_breaker_state()`（滞回）**

```
peak = max(历史峰值总资产, 当前总资产)；dd = 1 − 总资产 / peak
未触发：dd ≥ trigger(0.08) → 触发
已触发：dd ≤ release(0.04) → 解除          # 滞回，避免临界值反复抖
触发后：market_weak=True、weak_pause_all=True、effective_exposure = min(…, target(0.0))
```

- 峰值与触发态落 `app_state.paper_cb_state`（JSON），**跨重启保留**——否则重启后「忘了刚才跌过」
  就会立刻满仓接飞刀；
- 关闭开关或任何异常 → 未触发（零风险）。

**开关依赖与实现细节**（2026-10-31 代码复核）

- `paper_policy_floor_enabled` **必须与 `paper_dynamic_exposure` 同开才生效**。托底强度是用来
  修饰 weak/shock 的，而 weak/shock 由动态机制层产出：只开 floor 时引擎根本不调用
  `_dynamic_regime()`，`/api/paper/live` 的 `floor` 恒为 `null`。设置页该项 tooltip 已标注这个依赖。
- `detect_policy_floor` 的**必要条件阈值 `drop_thresh=5%` 目前不可配置**（代码内常量），
  要改必须动源码并回测——不要误以为设置页能调。
- 熔断的 `release` 若被填成 ≥ `trigger`（设置页逐字段校验，拦不住跨字段填反），引擎会静默收敛为
  `trigger × 0.9`；否则状态机会在触发线附近每个 tick 翻转一次，表现为买入被反复暂停又放开。
- 熔断状态**只在（峰值, 触发态）变化时落库**。tick 每 5 秒一次，每次都写会让 `sas.db` 的 WAL 无意义增长。
- 动态机制层开启时，`/api/paper/live` 的 `weak` 字段**恒为 `null`**：此时静态 MA 闸门不参与决策，
  若返回残留值，前端会同时显示「弱市压到 `weak_exposure`」和「机制层压到 `regime_cap`」两个矛盾数字。
- **均线口径差一天（已知差异，不改）**：静态路径用 `rolling(n).mean().shift(1)` 再取前一交易日，
  实际是「前两日价格算出的均线」；动态路径把截至前一日的序列整段喂给 `detect_regime` 重算，
  是「前一日算出的均线」。两者都在无未来函数一侧，但严格程度不同。统一会改变线上判定、
  需要重新回测，故只记录。

---

## 5. 撮合机制（`paper/live.py`，事件驱动）

- **周期**：默认 `paper_tick_interval = 5s` 一次 `tick()`；实时价新浪逐股行情优先，缺失用内存快照兜底。
- **tick 执行顺序**：清过期信号 → 非交易时段直接返回 → 批量取实时价 → 手动模式仅刷新报价 →
  ① `check_stop_loss_target`（止盈止损/盈利保护）→ ② `t_tick`（做T）→ ③ 队列撮合 → 候补补位 → 出队。
- **买入窗口**：`paper_buy_open_window_minutes`（默认 5 分钟）；窗口外只跑卖出，买单留在队列等下一个开盘窗口
  （对齐回测"次日开盘价"成交，避免盘中 13:01 一把买高）。
- **信号 TTL**：`paper_signal_ttl_minutes(30)`，过期直接丢弃；开盘窗口模式下买单的过期时间顺延到
  「下一个窗口起点 + 窗口长度 + TTL」，否则 13:01 产生的买单永远等不到次日开盘。
- **候补队列**：`paper_backlog_enabled`；两条路入候补，语义对下游等价——
  ① tick 时被判「持仓已满 / 总仓位已达上限 / 资金不足一手」（`_defer_skips`）；
  ② **入队阶段就被队列容量挡掉**（`_backlog_put`，2026-11-03 起）。
  按评分降序在有剩余名额时补位（同样受开盘窗口约束），有效到当日收盘（非交易时段顺延次日）。
  ②必须存在，否则被容量截断的票从未进过 `pending`，既补不了位也当不了轮动候选
  （`rotation_candidates` 只读候补），高分票会被 M 个名额**静默挡掉**。
- **卖出优先于买入**：队列按 `(action != "sell", -score)` 排序，卖完再按评分从高到低买。
- **交易队列容量（五段式③）**：`paper_trade_queue_top_m`（默认 5）——待执行**买单**最多同时挂 M 条，
  同一批按 14 因子总分降序择优入队；超额的不入队，原因写入 `queue_rejected`
  （`GET /api/paper/live` 暴露，前端「④ 交易池」页以「未入队 名称/分值」展示）。
  卖出信号不受限（释放风险优先）；同一只票更新建议不重复占位；`0` = 不限制（旧行为）。

  三条口径细节（2026-11-03 修正，见 `WORKLOG` 十一节）：

  1. **名额计数不含已持仓票**：`holding` 只数「未持仓的买单」。引擎对已持仓的买入建议下一步
     必然回「已持仓」并出队，让它占名额等于每批白扔一个坑。已持仓票**仍照常入队**——并行
     账本各有各的持仓，替它们决定就等于让影子账本丢信号。
  2. **超额的票转存候补**（`_backlog_put`），受 `paper_backlog_max` 限制；`queue_rejected[*].deferred`
     标注是否成功转存。**队列本体仍是 M 条**：候补不是第二条队列，只在剩余名额 > 0 时补位。
  3. **`queue_rejected` 只留最后一批**，故另有 `queue_rejected_total` 记累计条次
     （自本次启动，重启归零）；
     `交易队列已满` 日志只在名单变化时打印（同样的 18 条每分钟重复，一天 107 行）。

  为什么**不**用「调大 M」解决：M 是入口限流，真正的约束往往在下游（弱市 30% 仓位、现金不足）。
  2026-11-03 实测：18 条被拒（最高 83.3 分）而同时段账户只剩 1,389 元可用，放宽 M 一股也买不到，
  只是把 18 条挂单一起送进「总仓位已达上限」。
- **自动 / 手动模式**：手动模式只刷新报价与展示建议，不执行任何买卖（含止盈止损），由用户在模拟交易页手动下单。

---

## 5b. 并行模拟账本（`paper/ledger.py` + `data/cache.py` 账本作用域）

**目标**：同一份信号、N 套处置参数同时记账，盘中对比，替代「改配置 → 跑 21 个交易日 → 再改」的串行试错。

**路由**：`cache.ledger_scope(account_id)` 用 `ContextVar` 记录当前账本；`None` = 线上主账本（`paper_*` 表），
非 `None` = 该账本的 `sim_*` 表。所有 `db.paper_*` / `list_paper_*` 在内部按作用域拼表名与 `account_id` 条件，
因此 `PaperEngine` 的撮合规则仍是**一份代码**，不为账本分叉（漏改一处就会写脏主账本）。

**参数注入**：`OverlaySettings(base, overrides)` 代理全局 settings，`__getattr__` 先查覆盖再回落；
引擎里 `self.settings.paper_xxx` 的读取因此自动按账本生效，不需要改 20 余处调用。

**方案映射**（`PROFILE_TO_PAPER`，回测参数 → 线上字段）：

| 回测 key | paper 字段 |
| --- | --- |
| `stop_loss` | `paper_stop_loss` |
| `take_profit` | `paper_take_profit` |
| `max_positions` | `paper_max_positions` |
| `max_total_exposure` | `paper_max_total_exposure` |
| `single_position_ratio` | `paper_single_position_max` |
| `weak_exposure` | `paper_weak_exposure` |
| `regime_ma` | `paper_regime_ma` |
| `breakeven_trigger` | `paper_breakeven_pct` |
| `trail_pct` / `trail_arm` | `paper_trail_pct` / `paper_trail_arm` |
| `stop_atr_mult` / `stop_atr_max` | `paper_atr_stop_mult` / `paper_atr_stop_max` |

不在上表的键（`invert_score`、`scoring_mode`、`ml_*`、`strategies`、`factor_weights`、`buy_threshold`、
`regime_tiers` 等）一律视为**不可并行复现**：`sim_specs()` 跳过整个方案并返回原因。
理由：这些参数在**信号生成阶段**起作用，账本共享同一份信号；静默降级只会得到一条与方案名不符的曲线，
比不跑更误导。这类差异请用影子账户（日频、重跑整段）验证。

**队列**：每个账本有独立 `pending`（入队时 `dataclasses.replace` 复制）。主账本成交出队不影响账本继续重试；
「持仓已满 / 总仓位已达上限 / 资金不足一手 / 取价失败 / 等待回踩」留在队列等下一 tick，
其余（已持仓 / T+1 等）出队。买入同样受开盘窗口约束，与主账本同口径。

**结算**：`settle(record_regime=False)` —— 择时快照是全局每日一行，各账本 `paper_regime_ma` 可能不同，
写进去会覆盖主账本那行，因此账本结算不写。

**表结构**：`sim_account(account_id PK, tag UNIQUE, profile, overrides JSON, initial_capital, cash, enabled, note)`；
`sim_positions PK(account_id, code)`、`sim_lots(account_id, ...)`、`sim_trades(account_id, ...)`、
`sim_equity PK(account_id, date)`。线上 `paper_*` 表结构不变。

**重置**：`POST /api/paper/sim/reset?tag=` 只清该账本的持仓/批次/流水/净值，不影响主账本与其它账本。

**与做T的关系（边界）**：并行账本只复制主账本经 `submit → pending → tick` 撮合的**普通买卖信号**（用各自 overlay 独立成交）。做T（`t_tick`，见 §4.9）在引擎内部**直接成交、不经过 pending 队列**，因此**不会被复制进任何 sim 账本**。后果：当 `paper_t_active=true` 时，主账本会比 sim 多出做T维度的仓位变动（加仓/买回/倒T卖出）；sim 之间对比的仅是「信号处置参数」（止损/仓位/弱市/移动止损/ATR 倍数）差异，**不含做T维度**。看 sim 对比时应记住这一层差异，或临时关闭做T再对照，避免误判「sim 与主账本不一致」。

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
| `paper_max_positions` | 10 | 同时持仓**上限**（不是门槛：可以少于 10 只，不能超过） | engine |
| `paper_max_total_exposure` | 0.80 | **收盘目标**总仓位（盘中允许满仓，收盘前由 `eod_trim` 压回，留 20% 现金给次日做T） | engine |
| `paper_eod_trim_enabled / minutes` | True / 10 | 收盘减仓开关与窗口（收盘前 N 分钟内，14:50 起） | engine |
| `paper_stop_loss` | 0.08 | **固定止损**（相对含费成本；回测 `stop_loss` 同口径，可落盘） | engine |
| `paper_take_profit` | 0.20 | **固定止盈**（相对含费成本；回测 `take_profit` 同口径，可落盘） | engine |
| `paper_single_position_max` | 0.20 | **单票上限**（占总资产；回测 `single_position_ratio` 同口径，可落盘） | engine |
| `paper_full_score` | 40 | 评分全额系数阈值 | engine |
| `paper_half_score` | 25 | 评分半额系数阈值（低于只记录） | engine |
| `paper_rotation_enabled` | **True**（2026-11-02 起） | 满仓轮动替换开关；开着预期影响小（Top300 下几乎不触发），判读先看是否真出现「轮动替换」成交 | engine |
| `paper_rotation_min_score / gap / max_per_day` | 40 / 20 / **10** | 轮动门槛、分差、每日轮动**只数**（默认 10 ≈ 不限，0=当日不轮动） | engine |
| `paper_backlog_enabled / max` | True / 30 | 候补队列开关/上限（满仓时同时作为轮动候选） | live |
| `paper_trade_queue_top_m` | 5 | 交易队列**买入**容量 topM（0=不限，卖出不受限）；已持仓票不占名额，超额转候补 | live |
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
| `paper_t_active` | **True**（2026-11-02 起，config/`.env`/覆盖层三处一致） | 做T引擎总开关；对滑点极敏感，归因要与基础策略分开看（#66/#92） | engine |
| `paper_t_size` | 0.25 | 单次做T比例（相对总持仓） | engine |
| `paper_t_up_sell` | 0.05 | 开盘涨且盘中触此幅度 → 卖出 t_size | engine |
| `paper_t_up_buyback` | 0.03 | 回调买回线（相对 max(开盘, 昨收)） | engine |
| `paper_t_down_add / down_full` | 0.20 / 0.20 | 低开达此跌幅加仓（0.20≈关闭）；预算先现金比例、再与单票上限(20%)/总仓位上限取 min | engine |
| `paper_t_rebound_sell` | 0.02 | 反弹至跌幅小于此 → 减加仓批次 | engine |
| `paper_t_limit_sell_half` | True | 涨停（弱封板）卖 1/2 | engine |
| `paper_t_crash_guard` | 0.02 | 大盘当日跌幅超此暂停做T | engine |
| `paper_tick_interval` | 5 | 撮合周期（秒） | live |
| `paper_buy_open_window_minutes` | 5（**本机线上 0**） | 开盘买入窗口（分钟，0=不限）。线上 2026-11-02 起置 0 → 交易时段全时段可买，与回测「次日开盘」口径不一致 | live |
| `paper_buy_open_delay_minutes` | 0 | **开盘延迟**：仅在大盘**向下跳空**时，开盘后 N 分钟内禁止自动买入（含轮动换入、做T 买回/加仓），让外围利空导致的开盘跳空消化；0=不延迟。利好日（大盘上涨）不延迟、不暂停，避免踏空误伤 | engine |
| `paper_open_cooldown_minutes` | 0 | **开盘冷静期**：各交易时段（09:30 / 13:00）开盘后 N 分钟内**无条件**禁止自动买入（含轮动换入、做T 买回/加仓），避开开盘瞬间波动/跳空；0=不限制。仅拦买入、不拦卖出。区别于 `paper_buy_open_delay_minutes`（仅大盘向下跳空才延迟）。回测同口径见 `entry_open_delay_minutes` | engine |
| `paper_signal_ttl_minutes` | 30 | 信号有效期 | live |
| `scan_auto_watch_limit` | 10 | **二选**：从股票池按技术面评分取前 N 加自选（一选=signals 全量） | scanner |
| `paper_entry_max_chase` | **0（关闭）** | **⑤ 不追高**：现价高于信号价超此比例不买。2026-11-01 由 0.015 改为 0，依据 BACKTEST_LOG #89（6 段配对三条线全不过，且是买差不是少买） | engine |
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
- **成交时点**：实盘买入收敛到开盘窗口（`paper_buy_open_window_minutes`，config 默认 09:30 起 5 分钟内），与回测"次日开盘价成交"对齐。
  **例外**：本机线上 2026-11-02 起该值为 `0`（窗口关闭、交易时段全时段可买），属使用者拍板的实盘取舍——
  此时「成交时点」已不对齐，对账/归因时要按这个前提读（`WORKLOG` 2026-11-02）。
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
| `rotation_enabled / rotation_gap / rotation_min_score / rotation_max_per_day` | None / None / None / None | 满仓轮动：None = 沿用线上 `paper_rotation_*`；`rotation_max_per_day=0` = 当日不轮动（可作「关轮动」对照组）。回测口径见 §4.4：选弱排除当日买入与已在卖出队列的票，换入票需 ≥ `half_score`（记录 #91） |
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
| `entry_open_delay_minutes` | None（**`/backtest/defaults` 跟随实盘 `paper_open_cooldown_minutes`**） | ⑤ **开盘冷静期（回测侧）**：入场成交价从「开盘价」改为「开盘后 N 分钟那根 5m bar 收盘」（≈ open+N 分钟价），模拟实盘 `paper_open_cooldown_minutes`（记录 #108）。None/0=不延迟（历史开盘价口径）；设 N 后实盘与回测入场可比。无分钟数据回落 VWAP/开盘价。2026-11-07 起回测中心「五段式」组可调，落盘映射 `paper_open_cooldown_minutes` | pipeline / api / 前端 |
| `breakeven_trigger / trail_pct / trail_arm` | None | 保本止损 / 移动止损（回测侧） |

`_intraday_first_trigger(series, stop, tp, partial)` 返回首个触发的 `(type, price)`，
三者同时命中时取**序列中最先出现**者；日K 边界模式忽略返回价，改用 `min(开盘, 止损)` / `max(开盘, 目标)`。

### 7.3 已知偏差（对照时需注意）
- **结果页常驻「口径对照」（2026-11-07 起）**：回测中心结果区显示本次入场价（`0` = 次日开盘价，
  `>0` = 开盘 +N 分钟的 5m 收盘价）、实盘冷静期、本次池 `TopN` 与线上池，并附本节偏差摘要与
  `#70 §6` 判读门槛（−1pp ~ +5pp、不足 21 交易日不下结论）。**为什么必须上界面**：之前
  `entry_open_delay_minutes` 只在 `profiles.cur10` 里写死 5，`/backtest/defaults` 与
  `BacktestRequest` 都没有它，页面跑出来的是「次日开盘价」却常被拿来评价「09:35 后才买」的实盘；
  现在 `/defaults` 跟随 `paper_open_cooldown_minutes`，且落盘映射也指回该字段。
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
  实盘轮动会额外吃候补队列的票（见 §4.4），回测的轮动候选只有当轮 `buys`——因此
  **回测的轮动触发次数是实盘的下界**，两边不可直接比触发频次。
  2026-11-03 起实盘又加了两道闸（换入目标只用一次、卖出的钱必须投得回去），
  **回测只同步了前者**（`taken` 集合，本来就有）：回测的卖出与买入都在次日开盘、
  受 80% 与单票上限约束，会「卖多买少」地部分成交，没有这道校验，也没有弱市 30%
  这种严苛上限。结论：**两边轮动次数与仓位变化的差异，不能当成实盘策略优劣的证据**。
- **交易队列容量**：回测没有队列概念，买单数只受剩余名额（`max_positions - 持仓`）约束；线上额外限
  `paper_trade_queue_top_m`(5) 只买入候选。M 小于剩余名额时线上更保守，回测收益会略高估
  （满分票扎堆的日子尤甚），对照时需注意。2026-11-03 起被截断的票会进候补队列，
  因此它们**仍在线上视野内**（可补位、可当轮动候选），只是不能立刻成交——
  上一条「回测的轮动触发次数是实盘的下界」由此更强：回测既无候补、也没有被截断票。
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

### 7.4b 回测结果落库（2026-11-01）

「落盘」有两种含义，别混：§7.4 说的是**参数写入实盘配置**；这里是**回测结果本身入库**，
每次回测跑完自动写，不需要任何操作，也不影响实盘。

| 表 | 存什么 |
| --- | --- |
| `backtest_runs` | `params`（全量参数）、`metrics`、`equity_curve`（逐日 `equity/benchmark/cash/positions`，即过程数据）、`positions`（期末持仓）、`review`（归因 + 建议）、`job_id`（与 `backend.log` 对齐）、`elapsed` |
| `backtest_trades` | 逐笔成交：`code/name/buy_date/sell_date/buy_price/sell_price/shares/amount/fee/pnl/pnl_pct/reason/score` |

口径要点：
- 逐笔**单独成表**而不是塞进 `backtest_runs` 的 JSON 列，是为了能直接做 SQL 聚合（见下）；
  两表在同一事务内写入，不会出现「有 run 没明细」。
- `GET /api/backtest/{run_id}` 返回含 `trades` 的完整结果；`GET /api/backtest` 只返回摘要
  （刻意不取 `equity_curve/positions/review`，附带 `trade_count`）。
- 本功能上线前的历史记录只有参数与曲线，`trades` 为空；`positions/review/job_id/elapsed`
  由 `_migrate_backtest_extra` 给老库补列，历史值为 NULL，读取侧按空值降级、不回填。
- 内存任务表 `_JOBS` 仍只保留 **1 小时**（`_JOB_TTL`），过期后只能按 `run_id` 查库。

常用查询：

```sql
-- 按卖出原因统计：一眼看出「是否几乎全是止损/止盈」
SELECT reason, COUNT(*), ROUND(AVG(pnl_pct), 2) AS avg_pct
FROM backtest_trades WHERE run_id = ? GROUP BY reason ORDER BY COUNT(*) DESC;

-- 同一只票在多次回测里的表现（参数对比用）
SELECT run_id, sell_date, reason, pnl_pct FROM backtest_trades
WHERE code = '600519' ORDER BY run_id DESC;
```

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
- **`paper_equity` 的当日行是「最后一次快照」，不是「收盘值」（2026-11-05 起）**：盘中
  `tick` 每 `_EQUITY_SNAPSHOT_SECONDS`(30s) 按最新价 upsert 当日一行（有成交立即写、无成交也重估），
  15:40 结算用收盘价覆盖定稿。因此**盘中读净值/收益/21 日最大回撤取的是当时快照**（会话内会变），
  `live_review` / `weekly_review` 这类对账脚本应在收盘结算后跑才有可比性；同一交易日仍只有一行，
  不会因为盘中刷新而多出数据点、也不改变历史行。
- **`list_paper_equity(limit)` 取最近 N 天**（`ORDER BY date DESC LIMIT ?` 后反转为升序）：旧实现是
  `ORDER BY date ASC LIMIT ?`，快照超过 250 行（约一年）后曲线会停在最早那 250 天，最新点全被截掉。
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
- **影子账户口径（2026-11-07 起）**：`scripts/shadow.py::_snapshot` 把同一份 `ml_diagnostics`
  写进每条快照；`GET /api/ops/shadow` 据此给 `ml_state` —— `unknown`（改动前快照无该字段）
  / `fallback`（IC≤0 整段回退）/ `effective` / `factors`（方案本身是 14 因子）/ `absent`（无 `default_ml`）。
  `default` 与 `default_ml` 快照完全相同时，**必须同时看 `ml_state` 与样本长度**：ML 生效但窗口内
  `trade_count=0`（无平仓）也会得到相同曲线，不能推出「ML 无超额」；`unknown` 也不是回退
  （`BACKTEST_LOG` #109 / #117）。
- **默认跟随实盘**：`/backtest/defaults` 的 `scoring_mode` 由 `live_ml_scoring` 决定
  （开 = `ml`），避免「回测中心跑 ML、实盘还在 14 因子」这种口径漂移；`universe_size`
  同理跟随 `paper_pool_top_n`（300），不再写死 500——池子不同则候选集、选中票、
  超额基准全都不同，结论无法互相解释（`#79` §7）。
- **落盘折算**：`scoring_mode=ml|ml_blend` → `live_ml_scoring=True`；`ml_model` /
  `ml_label_horizon` / `ml_blend` / `ml_require_ic` / `industry_neutral` / `disable_limit_mask`
  都是**回测专属**，实盘固定 lightgbm + 行业中性 + IC 回退 + 涨跌停约束，不落盘。

**线上状态（2026-11-01 重新打开）**：`live_ml_scoring` 在**覆盖层与 `.env` 两处**都已置 true
（只改一处不够——「还原为 .env」会把开关改回去，这类「两处口径」问题同 §4.9 的做T 开关）。

重开依据（原关闭理由两条都站不住）：

1. **`#82` 是有效证据**：`ml_lgb` 18 段配对中位 **+7.16%**（基准 +1.11%），配对差 **+4.55pp**
   （超 3.5pp 门槛），最差段 −11.44% → **−5.07%**，超额>0 段占比 66.7% → 83.3%。
2. **原依据①（`#79` `effective=false`）是误读**：`#79` §3 自己写明，那是**12 个交易日的短区间回测**，
   只有 12 个截面导致 rank-IC 估不出来而回退，属 `#76` 设计内的纪律性降级，不能推论成
   「ML 分位当 score 用没有有效回测」。
3. **原依据②（首轮 109s）已不成立**：2026-11-01 实测 152 只自选股 **20.7s** 全量出分
   （`as_of=2026-10-30`、覆盖 152/152、**OOS IC 0.0654**），且按 `as_of` 缓存——
   一个交易日只训练一次，盘中每分钟的分析都命中缓存。

仍存在的已知局限（不因打开而消失）：

- `industry_map.csv` 不存在时 `industry_neutral=True` 静默退化为**板块中性**（4 组，主板约 140 只等于未中性化）；
- 训练/预测池是「当前自选 152 只」，存在 **selection bias**，OOS IC 0.065 偏乐观（与 10-30 的 0.0658 一致）；
- 买入判定用 `paper_half_score=25` 作 ML 分位门槛，与回测 `_rank_score` **完全同口径**（两边都是 25）。
  按上表，ML 分位 ≥25 的占比约 75%，所以「152 只里 113 只判 buy」是设计使然而非量纲错配——
  实际买入数仍由 `max_positions`（10）与总仓位/单票预算约束。

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
| ⑤ 成交 | ✅ 已补切入点：`paper_entry_max_chase`(0=关闭，原 1.5%，#89 证否)、`paper_entry_pullback`(0=不限) 回踩、`paper_entry_timeout_minutes`(0=沿用 TTL) 超时；配合开盘窗口 + TTL | 无分时强度/回踩确认；日线粒度下「切入点」只有价格带这一层 |

### 8.3 改造建议（按最小改动排序）

| # | 内容 | 参数/接口 | 影响面 |
| --- | --- | --- | --- |
| 1 | **队列收敛 topM** ✅ 已实现（2026-10-29）：每次分析后，买入建议按 14 因子总分降序只取前 M 入队，其余记 `queue_rejected`，前端 ④ 页展示「未入队 + 分值」 | `paper_trade_queue_top_m`（默认 5，0=不限）；卖出不受限；同股更新不重复占位 | 已上线，`LiveTrader.submit` 单点收口（开盘/盘中/选股联动三处调用全覆盖） |
| 2 | **交易池判定统一** ✅ 已实现：`entry_gate()` 前置闸 + `entry_price_ok()` 切入点 + `entry_check()` 组合入口；被拒原因经 `_entry_rejected` 暴露 | 无新参数，纯重构，237 项回归测试保证行为不变 | `engine` / `live` / ④ 页「切入点判定」卡片 |
| 3 | **切入点** ✅ 已实现：不追高 / 回踩 / 超时 | `paper_entry_max_chase`(0=关闭，原 0.015，#89 证否)、`paper_entry_pullback`(0)、`paper_entry_timeout_minutes`(0=沿用 TTL)；回测侧同名参数 `entry_max_chase/entry_pullback`（None=不启用） | 线上 `entry_price_ok` + 回测买入执行 |
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

## 9. 摆动影子：相对强弱（RS）极值口径（2026-11-08，只观测不交易）

> 实现：`app/analysis/swing.py` / `app/analysis/baseline.py` / `scripts/swing_shadow.py`，
> 实验记录 `BACKTEST_LOG` #120。**不参与任何选股、评分、撮合路径**。

### 9.1 为什么用 RS，而不是「两个极值对齐」

个股价格与指数点位量纲不同，无法直接比较「偏离多远」。定义

```
RS_t = (P_stock,t / P_index,t) / (P_stock,0 / P_index,0)
```

分母消掉大盘共同波动；在 RS 上找摆动点即「按基准找个股的相对极值」，且**只需一层确认滞后**。
分别找大盘极值与个股极值再对齐，会把两段滞后叠加到买点上。注意 **RS 低点 = 相对弱势结束**，
不是绝对低价：大盘 −2% / 个股 −1% 时 RS 上升但账户在亏，故必须前置 regime 闸门。

### 9.2 摆动点：尺度、确认、不可撤销

```
阈值_i     = max(swing_threshold_pct, swing_atr_mult × ATR_i / |v_i|)
候选 low   : v_i < min(v_{i-k..i-1}) 且 v_i < min(v_{i+1..i+k})     k = swing_confirm_bars
候选 high  : v_i > max(v_{i-k..i-1}) 且 v_i > max(v_{i+1..i+k})
确认时点   : confirmed_bar = i + k，滞后 lag_bars = k
ZigZag 筛选: 类型交替 + pivot 间隔 ≥ swing_min_gap
             + |v_i − v_last| / |v_last| ≥ 阈值_i（首个点用前缀跨度初始化）
```

**确认窗口只负责「右侧不破」，不负责走完整段波段。** 初版把 5% 波段门槛写成「3 根确认窗口内
必须反弹 5%」，指数 1200 根只识别出 1 个低点、锚窗形同虚设；改成相对上一摆动点的反转幅度后
为 14 个。另外点一旦输出**不回头替换**，因此

```
detect_swings(v[:t+1]) == [p for p in detect_swings(v) if p.confirmed_bar <= t]
```

（`tests/test_swing_shadow.py::test_no_lookahead_prefix_equivalence` 逐 bar 锁死）。
脚本虽只算一遍，语义仍是逐日重放；不能用「先标好全区间高低点再对齐买卖」的做法。

### 9.3 偏离度、剔除与波段幅度

```
z_t    = (RS_t − MA(RS, w)) / STD(RS, w)              w = swing_z_window
beta_t = cov(r_stock, r_index, w) / var(r_index, w)
corr_t = corr(r_stock, r_index, w)                    全部只用 T 日及之前数据
```

| 条件 | 判定 | 处理 |
| --- | --- | --- |
| `z`/`corr` 非有限 | `excluded_insufficient` | 数据不足，剔除 |
| `abs(z) > swing_extreme_z` | `extreme_positive` / `extreme_negative` | 严重背离，进观察池不交易 |
| `corr < swing_min_corr` | — | 与基准不同步，剔除 |
| 不在基准锚窗内 | `no_anchor` | 只记录，不算共振 |
| `abs(z) ≤ swing_band` 且落在锚窗内 | `resonance` | `resonance_buy` 观测信号 |

基准锚窗 = 基准**已确认低点**的 `[confirmed_bar, confirmed_bar + swing_anchor_bars]`，
只作时间筛选，不直接触发买卖。波段幅度统计区分 `gross`（极值到极值）与
`net`（从 `confirmed_bar` 的可买价到后续高点），`eaten = gross − net` 即确认滞后的成本。

### 9.4 评估口径（防止自我欺骗）

- 一律用**相对基准的超额收益**，不能用绝对收益：基准见底时大批个股同时见底只是 beta 的机械结果。
- 「共振度」若做成因子，须过 IC > 0.03 / IR > 0.5、分层收益单调、样本外 walk-forward；
  因子值只用 T 日及之前数据，收益取 T+1 起的前向超额。
- 弱市里 RS 会系统性失真（分母跌得慢、个股阴跌），不同 regime 要分层统计。
- 机制层 `detect_regime` 四态与线上 paper engine 的 MA60 弱市闸门**不是同一口径**，产物里
  同时给 `below_ma60` 便于对照，别把 `crisis` 当成线上仓位闸门的值。

## 10. 事前预估摆动转折：条件分布口径（2026-11-09，只观测不交易）

### 10.1 为什么不是「预估极值点」

`pivot` 的定义要求右侧 `k` 根不破，本身依赖未来路径，事前不可观测。单票摆动低点中位数
11 个（p10=5 / p90=15），摆动间隔 CV=0.964、幅度 CV=1.743，时点几乎不可预测。
实测买点收益中位数（Top300 / 2496 段）：

| 买点 | 到下一个摆动高点的收益 |
| --- | --- |
| 提前 3 / 2 / 1 根 | 4.613 / 5.478 / 6.647% |
| 极值当天（理想上界） | 8.363% |
| 确认日 +3（现行） | 4.979% |

**提前埋伏反而劣于等确认**；预估目标是「把买点从确认日拉回极值当天」，不是更早买。

### 10.2 输出是分布，不是点位

对 bar `s` 输出 `p_turn = P(未来 horizon 根内出现已确认摆动低点 | X_s)`，以及
`exp_upside` / `exp_downside`：

```
exp_net = p_turn · exp_upside − (1 − p_turn) · exp_downside − cost_pct
```

特征 `X_s` 五维分箱，全部因果：RS 滚动 Z、连续下跌根数、距最近**已可见**摆动点根数、
`|RS 日收益|` 波动代理、基准 MA60 上下。

### 10.3 三条因果红线

1. 标签（未来是否转折、波段幅度）可用未来数据，但样本带 `available_bar`：
   `y=1` 时为配对高点的 `confirmed_bar`（幅度用到了该高点的 pivot 值）；
   `y=0` 时为 `s + horizon + confirm_bars + 1`（确认窗口走完才能断言「没有低点」）。
   建表按 `available_bar ≤ t` 增量推进，不回头重训。
2. 全局分箱先验用 Laplace 平滑 `p_g = (n_pos + 1)/(n + 2)`；个股修正用带上限的 Beta 收缩
   `w = min(k, cap)/(min(k, cap) + α)`，`p = w·p_c + (1−w)·p_g`，幅度同理。
   `cap` 校正日线样本的自相关，避免单票几百个高度相关的 bar 把权重顶到 1。
3. 发信号阈值 `θ_t = base_rate_t × entry_lift`，`base_rate_t` 只取 `t` 时**已入库**样本
   （冷启动 ≈0.5 → θ≈1 → 天然不发信号）。**绝不能用全样本事后基础率**。

### 10.4 评估口径（防止自我欺骗）

- Brier `= mean((p − y)²)` 必须与常数基准 `mean((p̄ − y)²)` 比，
  `brier_skill = 1 − Brier/Brier_base`；基础率 8.9% 时 0.08 左右看着漂亮，其实几乎无信息量。
- 命中率对照动态门槛
  `breakeven = (median_confirmed + median_loss + cost)/(median_upside + median_loss)`；
  `verdict ∈ {insufficient_samples, below_breakeven, above_breakeven}`。
- **模型预估净 vs 实得期望净**并列看校准：本轮 0.519% vs 0.21%，过度自信约 2.5 倍。
  过度自信未修前，概率不可用于仓位。
- 一切用相对基准的超额收益，弱市须叠加 regime 闸门（同 §9.4）。

### 10.5 本轮实测（截至 2026-11-06）

113851 条样本 / 105881 次逐日重放 / 基础率 0.0889 / `p_turn` p99 = 0.2111 /
2713 条观测信号 / 命中率 0.3011 / 门槛 66.7% / Brier 0.0777（基准 0.0810，技巧 4.07%）/
命中收益 6.549% / 误报下探 1.584% / 实得期望净 0.21% vs 等确认 5.022%
→ `verdict = below_breakeven`，**不接评分、不接下单**。

---

## 11. 特征敏感性网格与校准口径（2026-11-09，只研究不交易）

### 11.1 三个新增的基准节奏 / 领先型特征（#122）

| 特征 | 计算 | 分箱 | 因果性 |
|---|---|---|---|
| `bench_mom` | `B_t / B_{t−5} − 1` | `<−1% / −1%~1% / >1%` | 只用 t 及之前；`np.roll` 绕回头部的前 5 根显式置 0 |
| `lead` | `(RS_t / RS_{t−5} − 1) − bench_mom_t` | 同上 | 「基准没转向、个股 RS 先转向」的量化代理 |
| `bench_z` | `(B_t − MA60_t) / SD60_t` | `<−1 / −1~0 / 0~1 / >1` | 60 根 trailing 均值与标准差，窗口不足填中间档 |

`z` 强制排在分箱矩阵第一列：它的 `-1` 同时承担「滚动 Z 窗口未走完，该 bar 不可用」的掩码，`build_samples_and_queries` 依赖这个约定。特征集由 `swing_est_features` 逗号分隔指定，`parse_features()` 拒绝未知名、重复与缺 `z`。

### 11.2 等频十分箱可靠性曲线

#121 用等宽 5 箱 `[0,0.2)…[0.8,1]`，而 `p_turn` p99 仅 0.2111 → 5 箱里 3 箱为空，曲线读不出来。改为把 `(p, y)` 按 `p` 排序后切成 10 个**等样本量**箱：

- 每箱 `predicted = mean(p)`、`actual = mean(y)`；`predicted > actual` 为过度自信，`<` 为低估；
- `calibration_ece = Σ(count_i × |predicted_i − actual_i|) / N`（拿概率定仓位前必看）；
- `calibration_bias = mean(p) − mean(y)`；`calibration_slope` 为 10 个箱点 `(predicted, actual)` 的 OLS 斜率，`>1` 表示**排序有信息但概率幅度被压缩**。

#121 默认口径实测：`bias = −0.0105`（低估）、`ECE = 0.0153`、`slope = 1.5821`，q10 预测 0.1656 对实际 0.2453。所以要分清两件事：**概率是低估**，而 #121 的「模型预估净 0.519% vs 实得 0.21%」是 `exp_upside / exp_downside` 的**条件幅度**被高估，两者不是同一个校准问题。

### 11.3 网格与时间切分（防过拟合）

- `build_dataset()` 先造好全部样本与查询；只有 `features / horizon / z_window / confirm_bars` 会影响其内容，`prior_strength / entry_lift / min_exp_net` 只影响重放。网格按 `(features, horizon)` 缓存 dataset，81 组参数只重建 9 次。
- 时间切分 `split = start_bar + (n − start_bar) × dev_frac`（默认 0.6）。`evaluate_dataset(..., bar_max=split)` 为开发期，`bar_min=split` 为样本外；**样本表始终全量**，靠 `available_bar ≤ q.bar` 推进，所以开发期看不到样本外标签，样本外可以用开发期历史。单测锁死 `dev.queries + oos.queries == 全量` 且分段 Brier 加权平均还原整段。
- 排序键 `(-brier_skill, calibration_ece, -estimated_queries)`。第三键是覆盖率：分箱维度越多，`min_global_samples=30` 滤掉的 bar 越多（8 维全特征集 33358 次重放 vs 5 维 60949 次），不同覆盖率的 skill 不可直接比。
- `entry_lift` **不影响 brier_skill**（只决定发不发信号），却能显著改变 `hit_rate`；因此按 `(features, horizon, prior_strength)` 去重、代表行取最接近默认门槛的一组，且**绝不**用命中率选参。样本外只对 top-3 与默认配置各评估一次。

### 11.4 三个判读坑

1. `brier_skill = 1 − Brier / Brier_base` 的分母是**该段自身**的常数基准；两段基础率不同，skill 跨段不可比。top1 样本外 skill 从 0.0464 升到 0.0497 的同时，绝对 Brier 从 0.1047 恶化到 0.1224，只是因为样本外基础率更高、`Brier_base` 从 0.1098 抬到 0.1288 —— 这不叫「没有过拟合」。
2. 抬高门槛能把命中率从 0.3398 刷到 0.4579，代价是信号从 3464 条骤减到 629 条（−82%），而实得净收益 0.415% → 0.635% → 0.619% **并不随命中率单调提升**。命中率必须与 `predicted_signals`、`mean_realized_net_pct` 一起看。
3. #121 的动态门槛 `breakeven_hit_pct` 会随分段的 `median_confirmed / median_loss` 变化（67.0% ~ 71.9%），所以「命中率提升了」不等于「跨过门槛了」。

## 12. 在线概率校准与滚动 walk-forward（2026-11-09，只研究不交易）

### 12.1 Platt 校准的形式与因果约束

`p' = sigmoid(a · logit(p) + b)`，其中 `p` 是 `TurnEstimator` 输出的原始分箱概率（全局先验 + 个股带上限 Beta 收缩）。拟合用**加权 Newton + 回溯步长**，对 `(a − 1)² + b²` 加 L2 惩罚，先验锚在恒等映射 `(a=1, b=0)`：

- `a > 1` 把低概率压得更低、高概率抬得更高，正好对应 #122 测出的 `slope > 1`（概率幅度被压缩）；
- `a` 恒为正（裁剪到 `[0.02, 20]`），所以**完全保留原排序**，不会悄悄改变选股顺序；
- 样本数 `< swing_est_calib_min_samples`（默认 200）时 `ready=False`，`apply()` 严格返回原值，等价于「不校准」。

三条因果约束：

1. 训练对 `(logit(p_raw), y)` 只能在该样本 `available_bar ≤ t` 之后进入校准器。实现上用最小堆按 `available_bar` 排队（`TurnEstimator.observe`），因为查询按 bar 递增产生、标签可用时间却可能更晚（配对高点后确认）。
2. 每 `swing_est_calib_refit_every`（默认 20）根 bar 重拟合一次，不逐 bar 重训。
3. 训练对按 `x` 取两位小数聚合后加权拟合：唯一 `x` 数量远少于样本数（10 万级样本 → 千级桶），重拟合才够快。

### 12.2 冷启动陷阱（#123 初版踩过，必须记住）

`evaluate_dataset(..., bar_min=split)` 初版只遍历**窗口内**的查询。但校准器的训练流完全靠 `observe()` 喂进来 —— 于是样本外评估时校准器被冷启动，只能拿窗口内那些标签已成熟的样本重学一遍，Platt 的样本外 skill 被算成 **−0.0389（5 折里只有 1 折为正）**，看起来「校准有害」。

修正：`cfg.calibration != "none"` 时，重放必须从**历史最早处**遍历全部查询（`observe` 照常喂），只是指标只在窗口内统计。修正后同一份数据的 platt 样本外 skill 变成 **0.0534（5 折全正）**。

一般化的教训：**任何带「在线学习状态」的评估，切窗口时切的是「统计口径」而不是「状态推进」**。`calibration="none"` 时可以安全跳过窗口外查询（估计量只由 `advance_to` 驱动），有状态时不行。单测 `test_window_evaluation_does_not_cold_start_calibrator` 专门锁这一条。

### 12.3 分折一致性与多重检验

- `walk_forward_dataset()` 把查询区间按**条数**等频切成 `folds` 段（默认 5），逐段独立算指标；边界若落在同一根 bar 内则后移，避免同一批查询被两折各算一次。估计器始终从历史最早处因果推进，所以第 i 折用的是「截至该折真实可见的全部样本」。
- 逐折 skill 的 `mean / sd / t = mean / (sd/√k)`；单边 p 用正态近似（`0.5·erfc(t/√2)`），5 折时 t 分布尾部更厚，故产物显式标注 `p_value_method = normal_approx`，不把 p 当精确值。
- `adjust_multiple_tests()` 用最保守的 Bonferroni：`α_adj = 0.05 / n_tests`，`n_tests` 为开发期实际比较过的配置数（81），并要求 `consistency ≥ 0.6`。它只约束「至少有一组是真的」，**不修正同一组在开发期被反复挑过**——后者只能靠样本外只验一次来防。

### 12.4 实测（Top300 / 2025-01-02 ~ 2026-11-06 / 257 只有效 / 113851 样本）

| 口径 | 开发 skill | 样本外 skill | 样本外 bias | 样本外 slope | 样本外命中 | 样本外实得净 |
|---|---|---|---|---|---|---|
| `calibration=none` | 0.0350 | 0.0445 | −0.0115 | 1.7353 | 0.3166 | −0.114% |
| `calibration=platt` | 0.0269 | **0.0534** | **−0.0028** | **1.2704** | 0.2825 | −0.364% |

- 校准**确实把概率读准了**：样本外 bias 从 −1.15pp 收到 −0.28pp，slope 从 1.74 降到 1.27，ECE 0.0199 → 0.0119，Brier 技巧 0.0445 → 0.0534。
- 但**没有让策略变能赚**：校准后 `p_turn` 上移使 `entry_lift × 基础率` 门槛放出更多信号（1336 → 3041 条），命中率反而 0.3166 → 0.2825，实得净 −0.114% → −0.364%。
- 开发期 platt 的 skill 更低（0.0269）是**冷启动成本**，不是校准有害：开发期前 200 条成熟样本之前校准器是恒等映射，之后才逐步生效。所以校准的收益只能在样本外/后段读。
- walk-forward（样本外 5 折）：`default` 0.0443（5/5 正）、`default_platt` 0.0533（5/5 正）、`top1` 均值 0.0500 / sd 0.0152 / t=7.365。多重检验：81 组试验、`α_adj=0.000617`、p < 1e−6、一致性 1.0 → `significant=True`。
- 但逐折命中率 0.4767 / 0.3449 / 0.3096 / 0.3975 / 0.4238 对应门槛 64.2% / 56.0% / 75.4% / 78.9% / 78.0%，**5 折全部低于门槛**；逐折实得净 +0.742 / −1.292 / −2.512 / +1.783 / +1.248%，符号来回翻。→ `conclusion = no_tradeable_edge`。

**结论**：概率信息量是**真实且稳定**的（连 5 折全正、过 Bonferroni），但它回答的是「哪个 bar 更可能转折」，不是「这笔交易扣掉成本还赚不赚」。命中率离门槛差的 25~35 个百分点，不是校准或特征工程能补的；`breakeven_hit_pct` 之所以高达 60~80%，是因为 `median_confirmed`（确认后剩余波段 3.5~6.0%）相对误报下探与双边成本太薄。要动这一层，只能改**标签定义**（转折改判相对基准的超额）或改**出场规则**，而不是继续修概率。

`swing_est_calibration` 默认仍为 `none`：校准改善的是校准指标，不是净收益，没有理由在线上默认改口语义；需要时可热更为 `platt`，设置页「概率校准」小节可回读 `a / b / samples / refits`。

---

## 13. 出场规则与可交易入场口径（2026-11-09，只研究不交易）

#123 的结论把问题指向「波段不够厚」，并明确下一步只有两条路：改标签定义或改出场规则。§13 同时做了这两件事，结论是否证的——但更要紧的是它暴露了 §9~§12 一直存在的一处口径偏差。

### 13.1 两种入场口径

设信号在 bar `t` 收盘产生（特征只用 `t` 及之前的数据），`N` 为序列长度。

| 口径 | 入场 bar | 含义 |
|---|---|---|
| `confirmed_low` | `entry = e`，`e` 为 `t` 之后首个摆动低点的 `confirmed_bar` | **事后口径**：#120~#123 一直用的。`e` 在 `t` 当时不可知，等于提前知道了哪根是低点 |
| `signal_next` | `entry = t + 1` | **可交易口径**：收盘后决策、次日收盘买入 |

两者对同一批信号的收益差距不是小数：`fixed3` 下事后口径净 +0.456%、可交易口径 −0.357%，**差 0.813 个百分点**。§11~§12 的所有「实得净」都是这两种口径的混合，必须按这一条重新解读。

### 13.2 三种出场规则

入场价 `P0 = close[entry]`，`h` 为持有上限：

- `fixed`：`exit = min(entry + h, N - 1)`，`r = close[exit] / P0 - 1`。
- `atr_trail`：入场时冻结回撤宽度
  `w = max(floor_pct/100, k · ATR[entry] / P0)`；
  之后 `peak_i = max(peak_{i-1}, close_i)`，首次满足 `close_i ≤ peak_i · (1 − w)` 即出场。
  **宽度必须用入场时的 ATR**：改用出场时的 ATR 等于用未来波动率反推止损，会系统性地把止损放宽。
- `swing_trail`：跟踪最近一个**已确认**的价格摆动高点
  `A_i = max{ p.value : p.kind = high, p.confirmed_bar ≤ i }`，
  首次 `close_i < A_i` 即出场。`confirmed_bar > i` 的前高当时看不见，不能提前用。

出场原因分 `trail`（峰值抬过入场价后的回撤）/ `stop`（没抬过就跌破）/ `take_profit` / `time_cap`，用于区分「趋势跟丢了」与「一进场就错」。

### 13.3 收益与判定

```
r_exit  = close[exit] / P0 - 1
r_bench = bench[exit] / bench[entry] - 1
excess  = r_exit - r_bench
net     = r_exit - cost              # cost 默认 0.3%（双边）
```

- `mean_net > 0` 且 `mean_excess - cost > 0` → `positive_expectancy`；
- `mean_net > 0` 但超额不够覆盖成本 → `beta_only`（赚的是大盘，不是这套方法）；
- 其余 → `negative_expectancy`。
- **选规则只在开发段（前 60%）按 `mean_net` 选**，样本外只验证一次，再按条数等频切 5 折看符号一致性（≥0.6 才算稳）。样本外不回头改选。

### 13.4 实测（257 只 / 2713 条信号 / 2025-01-02 ~ 2026-11-06）

可交易口径、全区间：

| 规则 | 净均值 | 超额均值 | 中位数 | 胜率 | 盈亏比 | p90 |
|---|---|---|---|---|---|---|
| `fixed3` | −0.357% | −0.258% | 0.000% | 0.4742 | 0.836 | 5.43% |
| `fixed10` | −0.337% | −0.417% | −0.633% | 0.4460 | 0.918 | 12.07% |
| `atr12` | +0.379% | +0.169% | −2.324% | 0.3670 | 1.106 | 12.12% |
| `atr25` | +0.020% | −0.540% | −3.015% | 0.3978 | 1.004 | 17.55% |
| `swing_trail` | −0.171% | +0.025% | −0.126% | 0.4252 | 0.898 | 3.89% |

- 开发段选出 `atr25`（净 +2.144%），**样本外净 −3.045%**，5 折 1/5 为正。样本外 8 种规则全负。
- 移动止盈把右尾抬了 3 倍（p90 5.43% → 17.55%），中位数却从 0.000% 掉到 −3.015%：**右尾变厚被中位数恶化完全抵消**，期望值没有改善。
- 样本外连 `confirmed_low`（事后口径）也全负（−0.35% ~ −3.23%）。**给完美入场点都赚不到**，说明问题不在入场滞后或出场规则，而在信号本身。

### 13.5 结论

§12 曾判断「要动这一层，只能改标签定义或改出场规则」。§13 把两条都试了：改度量（超额）与改出场（跟踪止盈）都只能改变收益分布的形状，不能改变期望的符号。**摆动低点信号在这段样本上没有可交易 alpha**，本系列（#120~#124）到此收口。

若还要继续，必须换**信号定义**（更短周期、加基准同步见底、加行业共振），并且沿用 §13.1 的 `signal_next` 入场口径与「误报计入」——否则会再次得到事后口径的假阳性。

---

## 14. 更短周期实验：30/60 分钟 RS 摆动（2026-11-09，只研究不交易）

### 14.1 数据聚合与基准代理
- 源：`data/minute5_fsdb/{code}.parquet`（5min bar，列 `date/open/high/low/close/volume/amount`）。
- 聚合：30min 用 `agg=6`（连续 6 根一组，午休/隔夜缺口落组边界）；60min 用 `agg=12`。聚合后 `high=max / low=min / close=last / volume=sum / amount=sum`。
- **基准代理**：上证指数分钟线未存盘（`minute5_fsdb` 只有 000001/000016/000688），故日内基准 = 扫描宇宙（Top300 ∩ 分钟库 = 243 只）等权均价。`baseline_kind="equal_weight_universe_mean"`，self-inclusion 在 243 只量级可忽略。
- 缺 OHLC 任一列（约 28/5564 的指数类文件只有 `date,close`）直接跳过该票。

### 14.2 参数按周期缩放
| 参数 | 30min | 60min |
|---|---|---|
| `bars_per_day` | 8 | 4 |
| `z_window` | 480 (60×8) | 240 (60×4) |
| `confirm_bars` | 16 (2×8) | 8 (2×4) |
| `horizon` | 24 (3×8) | 12 (3×4) |

其余（`entry_lift`/`cost`/`features`/`calibration`）沿用 #123 默认。评估链（detect_swings / relative_strength / build_dataset / simulate_exit）频率无关，原样复用。

### 14.3 出场预设（关键修正）
- **ATR 跟踪用紧止损**：`atr_k ∈ {2.0, 4.0}`，**不乘 `bars_per_day`**（早期 bug 版误写成 `2.0×bpd`/`3.0×bpd`，30min 即 16/24，止损宽到约 8%~16%，等于变相长持、偷换日内择时）。`hold_bars=10×bpd` 兜底。
- `fixed`：30min 用 `{8,16,32}`（=1/2/4×bpd）；60min 用 `{4,8,16}`。
- `swing_trail`：跌破最近已确认价格摆动高点。

### 14.4 实测（243 只 / 核心估计器）
| 周期 | 查询 | 信号 | 命中率 | brier_skill | 盈亏平衡命中 | 结论 |
|---|---|---|---|---|---|---|
| 30min | 734640 | 6320 | 29.21% | 0.0238 | 77.7% | below_breakeven |
| 60min | 367320 | 5835 | 34.41% | 0.0317 | 75.3% | below_breakeven |

样本外出场（可交易口径 `signal_next`）：
- 30min：`fixed8/16/32`、`swing_trail` 全负（−0.42%~−0.82%）；`atr2` 净 +0.633%/超额 +0.954%、`atr4` 净 +0.794%/超额 +1.122%（胜率 33%、盈亏比 1.37），5 折 (1.50, 1.20, 0.02, 3.12, −1.98) → 4/5 正、一致性 0.8 → `positive_expectancy_candidate`。
- 60min：所有规则全负（fixed4 −0.724% … atr2 −0.673% … swing_trail −0.556%），5 折全负 → `no_tradeable_edge`。

### 14.5 结论
主假设「更短周期提升转折点识别」不成立：命中率仍 ~30%，与日线同量级，概率模型自身 below_breakeven。唯一正的是 30min + 紧 ATR 跟踪吃到的周级漂移（出场层，非信号层），对周期极敏感、未计滑点，归 `positive_expectancy_candidate` 待独立区间复核。系统层面维持 `no_tradeable_edge`，不接评分/下单/不改线上默认。

---

*本表为快照，参数以代码当前实现为准；调整权重/阈值请同步 `tests/test_factors.py` 与前端"智能建议"展示。*
