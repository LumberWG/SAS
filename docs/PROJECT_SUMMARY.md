# SAS 项目前期工作总结

> **截至**：2026-11-12 · **范围**：本项目定位与同类对比 / 全流程与公式 / 已完成回测结论 / 后续方向
> **资料来源**：`README.md`、`AGENTS.md`、`docs/overview.md`、`docs/flow.md`、`docs/sas_formulas.md`、
> `WORKLOG.md`、`BACKTEST_LOG.md`、`docs/backtest_log/*`、`docs/analysis/*`、`RESEARCH_SUMMARY.md`，
> 并逐条与 `backend/app/` 代码核对。
>
> **口径声明**：`docs/flow.md` / `docs/sas_formulas.md` 是 10 月底快照。本文所有「当前生效值」以
> `backend/app/config.py` + 实测 `get_settings()` + `GET /api/settings` 的 `source` 为准（2026-11-12 核对）。
> 已发现的快照漂移举例：旧文档写「线上池 300」，实际 `paper_pool_top_n = 0`（不限制）；旧文档写
> 「开盘窗口默认 5 / 冷静期 0」，实际 `paper_buy_open_window_minutes = 0`、`paper_open_cooldown_minutes = 5`。
>
> **免责声明**：本系统仅供学习研究，全部为模拟盘，不接真实券商；所有数据与结论不构成投资建议。

---

## 一、本项目特点，及与同类项目的优缺点对比

### 1.1 一句话定位

**A 股（沪深京）个人用的「端到端模拟交易 + 研究闭环」**：本地日线/分钟线数据 → 全市场扫描 →
多策略命中 → 因子/ML 打分 → 信号队列 → 5 秒 tick 模拟撮合 → 批次制 T+1 持仓 →
多触发源卖出 → 按 A 股真实费率结算 → 净值/复盘；回测用同一套参数复刻，并把结论逐条编号归档。

它不是「回测框架」，也不是「行情看板」，而是把**一天真实要跑的事**串成可观测、可复现、
可回滚的一条链。

### 1.2 六个结构性特点

| # | 特点 | 具体体现 |
|---|---|---|
| 1 | **完整生命周期闭环** | 覆盖大盘→板块→选股→评分→入队→撮合→持仓管理→卖出→结算→复盘，而非只做「信号」或只做「回测」；卖出优先于买入，T+1、涨跌停、整手、印花税都在撮合层真实建模 |
| 2 | **三套分数严格分层 + 三次排序** | 技术面分 `±8`（只排序，不判买卖）→ 14 因子 `±100`（判买卖 + 仓位分档）→ ML 截面分位 `0~100`（替换因子分，`effective=false` 整段回退）；混入任一层都会得出错误结论 |
| 3 | **A 股微观结构原生内建** | `board_rules.py` 单一来源统一板块前缀、涨跌停（主板 10% / 创业板·科创 20% / 北交所 30% / ST 5%）、最小申报股数（科创 200 起 1 股递增）；批次账本严格 T+1 FIFO；卖出才收印花税；开盘 5 分钟冷静期 |
| 4 | **三层配置 + 参数中心** | `app_state` 覆盖层 > `.env` > `config.py`；设置页由 `param_specs._LAYOUT` 动态渲染（改 `config.py` 不必改前端）；`source` 字段明示每项生效来源；回测「落盘」可写回实盘并成为新默认值 |
| 5 | **回测与实盘严格 1:1（本期新增）** | `app/backtest/alignment.py` 是契约单一来源：41 项「回测默认 ← 实盘字段」用双哨兵运行时探针验证（零污染代理，不碰 live 单例，含策略集跟随 `AUTO_SCAN_STRATEGIES`）；新增参数未登记即报漂移；37 项落盘映射反向一致性校验；另有股票池 ST 过滤口径的行为探针；`check_backtest_alignment.py` 一键校验、回测页顶部实时徽标 |
| 6 | **研究纪律与留痕** | `BACKTEST_LOG.md` 编号唯一、只追加、结论性「不采纳」也写清理由；验收门槛 >3.5pp；18/31 段随机不重叠窗口配对；`WORKLOG.md` 记「为什么改」；研究资产与 live 明确隔离（A-Only 决策） |
| 7 | **运维可观测** | `/ops` 自检（库结构/净值/快照/调度/分钟库/影子账户/ML 状态）、影子并行账本、ML 三态灯、每日复盘与参数体检、SQLite 在线热备份、失败态不静默降级 |

### 1.3 与 6 个已实际试跑的同类开源项目对比

| 项目 | 定位 / 它更擅长 | 相对 SAS 的短板或被 SAS 证伪之处 | 本项目处置 |
|---|---|---|---|
| `Conquerer-yinn/Kakaaaa-quant` | 轻量「研究工作台」闭环：盘后复盘→情绪指标→历史主表→推送→网页→策略验证；工程规范（CI/Ruff）好 | 「涨停后 5 日」B 策略在随机一个月配对中净负拖累（月均 −0.32% / 三月 −3.04%），A+B 合并劣于单 A | 仅留研究，已从 live 剔除 |
| `PerCheung/ChinaAStockMarketRealtimeMonitor` | 实时监控，25-60（MA5/MA25 + VOL5/VOL60）信号极简、直观 | 无风控/成本/样本外验证，收益宣称缺严谨回测；单信号无仓位与退出体系 | **唯一采纳进 live**：三窗全正 + 随机月唯一正期望；由 SAS 补齐板块、止损、仓位、T+1 与成本 |
| `sqyangx/quantlab` | 工业级：Qlib Alpha158 数据层 + Kronos K 线 Transformer + RD-Agent LLM 因子发现；有 agentic 复核层与板块资金流 | 重、依赖链长；其 Model-A 的动量阈值是本项目代理、与原文不符；校准后 T+3 硬出场/跳过开盘涨停仅在研究侧复现 | 仅研究，未接 live |
| `ling-0729/KHunter` | 策略库丰富（12 选股 / 8 择时）、ADX 当日仓位档位、回测完全离线可复现、版本文档化 | 底反参数其实来自 SAS 内置 `builtin.py`，唯一增量是 ADX 闸门；校准后全样本 0 信号、几乎无增量 | 仅研究 |
| `robertquant/quant-research` | ETF 轮动 v4（30 日动量 + MA60 过滤 + −8% 硬止损），11 年回测并披露夏普/回撤/胜率 | 叠加到 A+MA30 之上月均 +0.24% → +0.10%，成交数 20→13，**无增量** | 未接 live |
| `Baili-BL/wechat_-125-` | 广发研报 125 指标择时的体系化实现，8 个指标 + 双指数对照 | 其允许交易日集合与 SAS 现有 MA30 高度重合，分层回测几乎等同 base | 未接 live |

**横向结论**：这 6 个项目的共同短板是「**有信号、缺闭环**」——大多止步于选股或择时信号，
缺少 A 股交易约束、成本、仓位与退出的完整建模，也缺少「结论性否决」的留痕机制。
SAS 的优势不在某个 alpha 更聪明，而在**把信号放进可执行的全生命周期并用配对回测判定去留**；
用户据此拍板 **A-Only**（live 只跑 `量价共振2560`），其余全部留在研究资产。

### 1.4 与通用量化框架（Qlib / vnpy / backtrader / 聚宽类）的架构层对比

| 维度 | SAS | 通用框架 | 谁更优 / 代价 |
|---|---|---|---|
| 数据层 | 本地 parquet + SQLite + 分钟库 fsdb，增量回填、离线可跑 | Qlib 二进制数据层 / vnpy 多数据源适配 | 通用框架更规范、可插拔；SAS 更轻、离线自洽但无插件生态 |
| A 股交易约束 | 内建 T+1 批次、板块涨跌停、整手/科创 200 起、印花税、开盘冷静期 | 多需自行实现或靠券商接口 | **SAS 明显更贴合 A 股现货** |
| 执行 | 5 秒 tick 模拟撮合、实时报价、队列/候补/TTL、并行账本 | backtrader 事件驱动回测；vnpy 实盘 CTP | 通用框架能接真实券商，SAS 目前纯模拟 |
| 策略表达 | 6 内置策略 + 前端可编辑自定义策略（持久化） | 通用 API，表达力更强 | 通用框架灵活；SAS 上手快、但扩展需改代码 |
| 参数治理 | 三层覆盖 + 动态渲染 + `source` 溯源 + 回测落盘反向写回 | 多为代码/配置文件 | **SAS 更工程化**，但也带来「覆盖层压住 .env」的踩坑成本 |
| 验证纪律 | 随机多段配对、>3.5pp 验收线、编号日志、1:1 对齐护栏 | 由使用者自建 | **SAS 显著更严**，代价是研究速度慢 |
| 生态/协作 | 单仓库、Vue3+FastAPI、Windows 服务化部署 | 社区大、文档/插件丰富 | 通用框架生态占优 |

### 1.5 优点 / 缺点直白清单

**优点**：① 端到端闭环、每笔可追溯（信号→队列→批次→成交→净值）；② A 股约束原生建模，
回测结论不会被「T+0 / 无涨跌停 / 无费用」虚高；③ 参数可热改且来源可查；④ 回测与实盘 1:1
有自动护栏而非靠自觉；⑤ 研究纪律严格，大量「证否」结论被归档，避免反复造轮子；
⑥ 失败态可见、每日复盘/自检/热备份齐全。

**缺点 / 现状边界**：① **纯模拟盘，无真实券商接口与实盘冲击成本**；② 回测主路径仍是
**日线四价**，实盘是 **5 秒 tick**，粒度不等价（参数 1:1 但执行层不等价）；
③ 主回测 **无滑点/最低佣金主导的真实成本模型**（真实成本模型目前只在做 T 研究 #155/#156 里），
会系统性高估薄边缘策略；④ 文档快照与 `config.py` 存在漂移，个别注释与实现不一致；
⑤ live 单策略（A-Only）集中度风险，缺「策略健康度」监控与自动降级；
⑥ 参数/开关数量多（107 项），认知与维护成本高，多层优先级易误判；
⑦ 多板块支持尚不完整（北证 50 指数常缺、切换板块前需重建等权合成指数并重跑）。

---

## 二、全流程：从大盘/板块分析到交易卖出（含公式）

### 2.0 总纲与交易日时间轴

```
触发(盘前9:20 / 盘中每1min / 15:30收盘扫描 / 15:35盘后 / 15:40结算 / 16:00预热 / 20:30回填)
 → 数据(股票池 → get_kline_cached → add_all_indicators → 实时快照)
 → 扫描(is_tradable → 并发20 → 逐策略evaluate → 技术面±8) → 排序① signals.rank
 → 打分(14因子±100 或 ML分位0~100) → 排序② recommendations.rank
 → 入队 LiveTrader.submit(买 topM=5 / 卖不限额 / TTL=30min / 窗口外顺延)
 → tick 每5秒(清过期 → 交易时段门 → 取价 → 止损止盈 → 做T)
 → 排序③ (卖出永远排买入之前)
 → 引擎 execute_recommendations 五段式 → 结果分流(成交/重试/候补/出队)
 → 卖出风控(T+1 → 信号 → 止损止盈 → 做T → 轮动 → 收盘减仓)
 → 15:40 settle 净值定稿 / 复盘
```

**三层配置优先级**：`app_state 覆盖层 > .env / 环境变量 > config.py 默认值`。
本文数值均为 2026-11-12 实测生效值。

---

### 2.1 第一层：大盘环境判定

**输入**：主要指数日线 parquet（上证/深成/创业板/沪深300）、外围指数、组合总资产与历史峰值。
**输出**：`market_weak`、`weak_pause_all`、`effective_exposure`、`regime_state`、是否硬拦新开仓。

实盘有 4 条并行链，**都只压低总仓位上限、不抬高**；优先级：熔断 > 动态机制 > 静态 MA 弱市 > 统一四档 cap。

```
【静态 MA 弱市闸门（默认路径）】paper/engine.py::_index_regime_detail()
n           = paper_regime_ma = 60 (日)
ma_series   = close.rolling(n).mean().shift(1)        # shift(1) 防未来函数
prev_close  = close.iloc[i-1]；prev_ma = ma_series.iloc[i-1]   # i = 最后一根 <= today
weak        = prev_close < prev_ma
market_weak = paper_regime_weak_pause(True) AND n>0 AND weak
weak_pause_all = market_weak AND paper_weak_exposure <= 0
当前值：paper_weak_exposure = 0.3 → 弱市「不暂停买入，只压仓位」；若设 0 则全天零成交（设计行为）

【动态机制层（默认关）】analysis/regime.py::detect_regime()
ret         = close.pct_change()
vol_ratio   = std(ret.tail(20)) / std(ret.tail(60))
runmax      = close.rolling(120, min_periods=1).max()
max_dd      = -min((close - runmax) / runmax 近120日)
trend_weak  = close.iloc[-1] < MA(60)
crisis : max_dd >= 0.12 OR vol_ratio >= 2.0  → cap = 0.15, pause_all = True
shock  : vol_ratio >= 1.4 OR max_dd >= 0.06  → cap = 0.40
weak   : trend_weak                          → cap = 0.30
calm   : 其余                                → cap = 0.80（不降仓）
数据不足 122 根或异常 → calm / 不降仓

【Layer2 统一宏观风险（展示常驻；进下单需开关）】analysis/market_risk.py
dom_weakness_i      = max(0, (MA60_i - last_i) / MA60_i)       # 国内四大盘
us_prev_weakness    = max(0, (MA60 - cur) / MA60)                # 美股前收
asia_same_weakness  = max(0, -(最新价/今开 - 1))                  # 亚太当日
dom_score = mean(有效国内分量)；ov_score = mean(有效外围分量)
score     = 0.6*dom_score + 0.4*ov_score   （两者都有）；仅国内 → score = dom_score
tier      = crisis 若 score>=0.12；shock 若 >=0.06；weak 若 >=0.02；否则 calm
cap       = calm 0.0(不压) / weak 0.30 / shock 0.20 / crisis 0.10
should_block_buy() = (entry_guard_block_declining_market=True AND tier == "crisis")
注：config.py 注释写「上证 MA20 declining 硬拦」，实现是「crisis 档」，以代码为准

【硬性回撤熔断（默认关）】analysis/policy_floor.py::circuit_breaker_state()
peak = max(历史峰值总资产, 当前总资产)      # 落 app_state，跨重启
dd   = 1 - total_asset / peak
未触发: dd >= 0.08 → 触发；已触发: dd > 0.04 → 维持（回落 ≤4% 才解除）
触发后 effective_exposure = 0，全暂停买入

【总仓位上限合成】engine.py::execute_recommendations()
total_asset       = cash + Σ(股数 × 最新价)（取价失败按成本价）
effective_exposure = 1.0                    # 盘中基线 100%；0.80 是「收盘目标」
if cb_tripped:              effective_exposure = min(effective_exposure, 0.0)
elif dyn_enabled:           effective_exposure = min(effective_exposure, dyn.exposure_cap)
elif market_weak AND weak_exposure>0:
                            effective_exposure = min(effective_exposure, weak_exposure)  # → 0.3
if unified_risk_cap_enabled AND tier in (weak,shock,crisis):
                            effective_exposure = min(effective_exposure, cap)
exposure_cap      = total_asset × effective_exposure
available_budget  = max(0, exposure_cap - max(0, total_asset - cash))
```

> **易错点**：`paper_max_total_exposure = 0.8` 在买入路径**不是硬上限**，只由 14:50 的 `eod_trim()`
> 压回；回测侧 `day_exposure = min(exposure, weak_exposure)` 直接把 0.8 当买入上限，两者口径不同。

---

### 2.2 第二层：板块规则与板块趋势

**输入**：6 位代码、名称、`twin_t_boards`、指数 parquet。
**输出**：板块键、涨跌停价、申报取整、趋势状态、买入是否放行。

```
【代码前缀 → 板块键】app/board_rules.py::BOARD_PREFIXES
sh_main = 600,601,603,605    sz_main = 000,001,002,003,004
cyb     = 300,301            star = 688,689    bse = 43,83,87,88,920
中文名：主板→(sh_main,sz_main) / 创业板→cyb / 科创板→star / 北交所→bse
当前 twin_t_boards = "创业板" → 白名单前缀 ("300","301")，选股与交易共用同一处设置

【涨跌停与申报规则】_BOARD_RULE = (涨跌停幅度%, 最小申报股数, 递增步长)
sh_main/sz_main = (10.0, 100, 100)；cyb = (20.0, 100, 100)
star = (20.0, 200, 1)；bse = (30.0, 100, 1)；ST/退 → limit_pct = 5.0
LIMIT_TOLERANCE_PCT = 0.5（百分点）
limit_up_ratio   = 1 + (limit_pct - 0.5)/100  → 主板1.095 / 双创1.195 / 北交1.295 / ST1.045
limit_down_ratio = 1 - (limit_pct - 0.5)/100  → 主板0.905 / 双创0.805 / 北交0.705 / ST0.955
涨停判定价 = prev_close × limit_up_ratio(code, name)
round_buy_qty(code, qty)：n = int(qty)；n < min_qty → 0；step>1 → n = (n // step) × step
exit_scale_factor(code) = clamp(limit_pct(code)/20.0, 0.25, 2.0)
    → 主板 0.5 / 创业板·科创 1.0 / 北交所 1.5（止盈按板块缩放，默认关）

【板块 / 全市场趋势】analysis/board_index.py
BOARD_INDEX: cyb→sz399006, sh_main→sh000001, sz_main→sz399001,
             star→sh000688, bse→bj899050（本地常缺 parquet）
board_regime(board, ma=60):
    last = close.iloc[-1]；m = close.rolling(ma).mean().iloc[-1]
    dev_pct = (last/m - 1) × 100
    state = "uptrend" 若 last > m，否则 "downtrend"；数据不足 → available=False
index_regime("sh000985", ma=60)：同式，中证全指 = 全市场口径

【趋势闸门三个接入点】
A 底反选股（默认开）：bottom_trend_filter=True, bottom_trend_ma=60, bottom_trend_index="sh000985"
    候选 date 不属于「close > MA60」日期集合 → 剔除
    trend_blocked = 所有有数据板块都 downtrend → 不给候选
B 入场（默认关）：paper_board_trend_gate=True 且该股所属板块 downtrend → 拒绝买入
C 回测镜像：board_trend_gate = None → 沿用 settings.paper_board_trend_gate
```

> 当前全市场（中证全指）低于 60 日 MA 约 −4%，底反候选被正确屏蔽。这体现本项目的分工：
> **趋势分析可看全市场，选股与交易严格限定 `twin_t_boards` 目标板块**。

---

### 2.3 第三层：股票池与选股

**输入**：扫描范围 `scope`、全市场股票池、实时快照、日 K parquet。
**输出**：`signals`（命中 + 技术面分）、自动自选、直连交易队列的建议。

```
【范围优先级】codes > top_amount_all:N（全市场）> top_amount:N（板块内）> 具名板块 / all
【板块内成交额 TopN（本期修正）】screener/scanner.py::_top_amount_pool()
prefixes = _allowed_board_prefixes()          # 由 twin_t_boards 解析 → ("300","301")
snap_b   = snapshot[snapshot.code.startswith(prefixes)]
top      = snap_b.nlargest(N, "amount")       # 只在目标板块内按成交额降序
pool     = pool[pool.code.isin(set(top.code))]
top_amount_all:N → 不筛板块，直接 snapshot.nlargest(N, "amount")
快照失败 → 降级 pool.head(N)

【统一可交易性】data/scope.py::is_tradable()
is_tradable = (6位数字) AND (前缀 ∈ 板块白名单) AND is_st_name(name) = False
            is_st_name() 是 ST/*ST/退 判定的单一来源，实盘与回测共用
大市值闸门：large_limit = paper_large_cap_yi × 1e8 = 100 × 1e8 = 1e10 元
            过滤条件 mv.isna() OR mv <= large_limit（取不到市值不拦）

【回测镜像（本期修正）】backtest/pipeline.py::_build_universe_by_date(..., names)
            names 来自 get_stock_pool() 名称快照；先用同一个 is_st_name() 剔 ST/退，
            再按**当日**成交额排名取 TopN（先剔后排，容量不变）；板块另由 board_prefixes 控制，
            不重复套用 is_tradable 的板块白名单。取不到名称快照则中止回测，绝不放行
            「少一层过滤」的结果。口径是当前名称快照（与实盘同源），不是逐日 ST 状态回放。
当前 paper_pool_top_n = 0 → 实盘买入池「不限制成交额 TopN」（注意：与扫描侧 N=500 不是一回事）

【逐股扫描】跳过 len(df)<30 或 len(df)<strategy.min_bars（多数 60，底反 130）
命中 = strategy.evaluate(add_all_indicators(df)) 非 None
技术面分 score_technical(df) ∈ [-8, +8]（只排序，不判买卖）：
    均线多头 +2 / 空头 -2；MACD 金叉红柱 +1.5 / 死叉绿柱 -1.5
    KDJ J<20 +1.5 / J>80 -1.5；量比>1.5 +1 / <0.7 -1；BIAS6<-6 +1 / >6 -1
排序①：按 (技术面分, price) 降序，同股多策略并列同一 rank

【内置策略（布尔条件全部满足才命中）】strategies/builtin.py
海龟突破   : close >= max(close[-20:]) AND amount > 1e8 AND close > open
均线放量   : ma5>ma10>ma20 AND close>ma5 AND vol_ratio>1.5
MACD 金叉  : prev.dif<=prev.dea AND last.dif>last.dea AND macd_hist>0
KDJ 超卖金叉: kdj_j<20 AND prev.k<=prev.d AND last.k>last.d
动量突破   : 60日涨幅>=30% AND close >= 60日最高×0.95
平台突破   : 20日振幅<15% AND close>平台最高 AND volume>平台均量
当前自动扫描只启用「量价共振2560」（A-Only，`app/strategies/github_strategies.py::VolumePriceResonance2560`）：
    MA5 上穿 MA25 + VOL5 上穿 VOL60 双金叉（策略库仍保留 6 个内置策略供手动扫描）

【底部反转（观察面板，默认不进交易链）】screener/bottom_reversal.py
start_pos    = (close[-21] - min(close[-120:])) / (max(close[-120:]) - min(close[-120:]))
               (区间振幅为 0 时取 0.5)
dd20         = 1 - close[-1] / max(close[-20:])
turnover     = turnover_rate[-1]                    # 百分制，5.0 = 5%
avg_amount20 = mean(amount[-20:])
四重几何闸：start_pos <= 0.40 AND dd20 >= 0.15 AND turnover >= 5.0 AND avg_amount20 >= 5e7 元
排序键：(-(factor_score or -1e9), start_pos, -dd20)
止盈：stop = min(low[-12:])；target = close[-1] × (1 + 0.25 × exit_scale_factor)

【副产物】
scan_auto_watch_limit = 10；watch_auto_max = 30（= 10×3）
    本轮命中 Top10 + 现有 auto 统一按技术面分降序，保留前 30，其余 auto 淘汰
paper_scan_autotrade_top_n = 10 → recommender.analyze_stock() → live_trader.submit()
```

---

### 2.4 第四层：因子与打分

**输入**：`add_all_indicators(df)` 之后的 K 线（**红线**：不加指标会导致 `bias6/atr` 缺失、
`total` 被压进 ±20，产生「买不进任何票」的假结论）。
**输出**：`total ∈ [-100,100]`、`action ∈ {buy,sell,hold}`、建议表 `score / rank`。

```
【14 因子总分】analysis/factors.py
total = Σ (weight_i × sub_i)，sub_i = clip(原始值, -1, +1)，Σweight_i = 100
权重（合计 100）：
  A 趋势 30 : ma_trend 12 / ma_slope 10 / long_position 8
  B 动量 25 : macd 10 / rsi 8 / gain20 7
  C 量能 20 : vol_ratio 8 / vol_trend 7 / turnover 5
  D 风险 15 : atr 5 / drawdown 5 / bias 5
  E 位置 10 : breakout 6 / boll 4
代表性子项：
  ma_slope      : change = (MA20_now / MA20_5日前 - 1)×100；sub = clip(change/2)   （±2% 饱和）
  long_position : dev = (close/ma60 - 1)×100；sub = clip(dev/8)                （±8% 饱和）
  vol_ratio     : >1.5→1；<0.7→-1；否则 clip((ratio-1)/0.5)
  atr           : ratio = atr/close×100；≤2%→0.5；≥5%→-1；否则 0.5-(ratio-2)/3×1.5
  drawdown      : dd = |min(close/cummax(close[-20:]) - 1)×100|；sub = 1 - clip((dd-5)/15,0,1)×2
  breakout      : pos = close/max(high[-20:])；≥1→1；≥0.97→0.4；≥0.90→0；<0.90→-1

【分级】factor_buy_threshold = 15.0；factor_sell_threshold = -15.0
action = buy 若 total >= 15；sell 若 total <= -15；否则 hold

【ML 替换（当前 live_ml_scoring = True）】analysis/live_ml.py + recommender.py
ml_score = LightGBM 当日截面分位(0~100)，horizon=15、行业中性、按 as_of 一天缓存
    无信号条件：OOS rank IC <= 0 / 样本 <5 只 / 构建失败 → 返回 {} → 整段回退 14 因子
若 ml_score 非空：
    score  = ml_score
    action = buy 若 ml_score >= paper_half_score(25)；否则沿用因子 sell / hold
否则 score = total，action = 因子 action
排序②：recommendations 按 -score 降序，rank 从 1 起
```

> 三个阈值各管一段，别混用：`15` 判因子 buy/sell（但 ML 生效时买入改看 `25`）；
> `paper_half_score = 25` 是「连分钱资格都没有」的下线；`paper_full_score = 40` 决定仓位权重系数。

---

### 2.5 第五层：入场（五段式）

**输入**：建议表、实时报价、内存队列/候补、现金与持仓。
**输出**：成交批次（含成本价与一次定死的止损线）或 skip 原因。

```
【入队】paper/live.py::submit()
top_m = paper_trade_queue_top_m = 5（0=不限）；卖出信号不受限
buy → 按 score 降序；holding = 队列中买单 且 未被持仓的 code
      code 不在 holding、不在 held、且 len(holding) >= top_m → 转入 _backlog 候补
TTL = paper_signal_ttl_minutes = 30 分钟；过期买单不丢弃，降级 backlog（上限 30）
窗口 paper_buy_open_window_minutes = 0 → _in_buy_window() 恒 True（全时段可买）
开盘保护 paper_open_cooldown_minutes = 5：
      09:30 <= cur < 09:35 或 13:00 <= cur < 13:05 无条件禁买（卖出照常）
buy_open = in_window AND NOT (open_delayed OR open_cooldown)
      不在 buy_open → 买单摘出留队，本轮只执行卖出

【① 风控闸门】见 2.1，只压 available_budget，不抬高
【② 准入五闸】engine.py::entry_gate()，顺序固定（永久性原因优先于弱市）
  1 _buy_allowed(): is_tradable / 总市值 <= 1e10 / 名称 / 成交额池 / 板块趋势闸门
  2 timing_pause（弱市全暂停档）→「大盘弱势，暂停买入」
  3 code 已持仓 →「已持仓」
  4 len(positions) >= paper_max_positions(10) →「持仓已满」

【③ 切入点】engine.py::entry_price_ok()
price 无效 →「取价失败」（留队重试）
涨停禁买：price >= prev_close × limit_up_ratio(code,name) → 放弃
不追高：chase = paper_entry_max_chase = 0（已关闭）；开启时
        price > signal_price × (1+chase) → 放弃
等回踩：pullback = paper_entry_pullback = 0（已关闭）；开启时
        未 armed 且 price > signal_price × (1-pullback) → 「等待回踩」留队
        armed 置位：任一 tick 的 px <= signal_price × (1-pullback)

【④ 评分分档】score < 25 → 仅记录不下单
factor_i = 1.0 若 score >= 40，否则 0.5（25 <= score < 40）

【⑤ 分钱整手 + 落批次】
slots     = max(0, paper_max_positions(10) - len(positions))
eligible  = 按 score 降序取前 slots（避免预算被买不到的候选摊薄）
per_tick  = paper_buys_per_tick = 2（本 tick 超限留队重试）
weight_i  = max(score_i, 1.0) × factor_i
share_i   = weight_i / Σ(eligible 的 weight)
budget_i  = min(available_budget × share_i,
                total_asset × paper_single_position_max(0.20), cash, available_budget)
shares_i  = round_buy_qty(code, budget_i / price)
while price×shares_i + calc_fee("buy", price×shares_i) > cash:
      shares_i = round_buy_qty(code, shares_i - step)     # 逐手/逐股回退含费总额
amount    = price × shares_i；fee = calc_fee("buy", amount)
cost_price = (amount + fee) / shares_i                    # 含费成本，止损以此为基准
stop_distance = max(0.08, min(2.0 × ATR / close, 0.20))   # 详见 2.6
stop_price = round(cost_price × (1 - stop_distance), 3)；peak_price = cost_price
```

---

### 2.6 第六层：持仓管理

```
【批次账本 + 严格 T+1】
shares          = Σ lots.shares
cost_price      = Σ(lots.shares × lots.cost_price) / Σ lots.shares   # 加权平均
sellable_shares = Σ lots.shares WHERE lots.buy_date < today
卖出按 buy_date 升序 FIFO 扣减；无批次则删除汇总行
建仓 score / stop_price / peak_price 一次定死，加仓(tag=t_add/t_buyback)不覆盖

【ATR 自适应止损距离（易错点）】engine.py::_stop_distance()
base = paper_stop_loss = 0.08；mult = paper_atr_stop_mult = 2.0
若 mult <= 0 → stop_distance = base
否则取本地缓存 K 线算 atr_indicator(df)（返回整表，新增 atr 列）：
    atr_val = frame["atr"].iloc[-1]        # ATR 绝对值，单位 元/股
    close   = frame["close"].iloc[-1]
    dist    = mult × atr_val / close       # 无量纲 = 2.0 × ATR%
    stop_distance = max(base, min(dist, paper_atr_stop_max = 0.20))  ∈ [0.08, 0.20]
建仓叠加机制层收紧（当前 paper_regime_stop_tighten = 0.6，仅 shock/crisis 生效）：
    d0 = stop_distance × 0.6；stop_distance = max(d0, paper_regime_stop_min = 0.03)

【止损线只抬不降】engine.py::check_stop_loss_target()
cost = pos.cost_price（含费）；change = price/cost - 1；peak = max(peak_price, price)
base = stop_price（老持仓无该字段 → cost × (1-0.08)）
保本：peak >= cost × (1 + paper_breakeven_pct=0.10) → base = max(base, cost)
移动：peak >= cost × (1 + paper_trail_arm=0.0)
      → base = max(base, peak × (1 - paper_trail_pct=0.10))
触发：price <= base → 止损卖出；change >= paper_take_profit=0.20 → 止盈卖出
时间止损（默认关）：held = (today - MIN(lots.buy_date)).days + 1 >= paper_max_hold_days(0=关)
实际常见路径：涨到 +10% 后回撤 10% 离场，很少走到 +20% 硬止盈

【止损口径单一来源（2026-11-12 修复）】app/risk/stops.py::resolve_stop_price(cost, base, peak, ...)
实盘 check_stop_loss_target 与回测 pipeline._simulate 物理共用这一个纯函数，
按顺序取「固定 → 保本 → 移动」中更紧的一条，签名不接受任何 regime / 大盘强弱参数。
回测曾用「大盘弱于 MA60 才防守」把固定止损一并门控，250 天里 134 天完全不止损
（#45 的 45 笔止损全部挤在 116 个弱市日），实盘却是每日无条件检查；
alignment.audit_stop_parity() 用行为 + 源码探针守住这条口径。

【做 T（当前 paper_t_active = True）】engine.py::t_tick()，每票每 tick 只做一个动作
开盘护栏 idx_drop >= paper_t_crash_guard(0.02) → 当日不做 T
① 买回：当日卖出的 buyback_line = max(今开, 昨收) × (1 + 0.03)，price <= line → 买回
        往日待买回 → 开盘无条件恢复底仓
② 涨停卖半：high >= 涨停价 且 可卖 >= min_qty → 卖一半
③ 倒 T：open_chg > 0 且 high >= open×(1+0.05) → 卖 round_sell_qty(总持仓×0.25)
④ 低开：反弹到 昨收×(1-0.02) 只减 t_add 批次；分级加仓阈值 t_down_add/down_full = 0.20（≈关闭）
         budget = min(现金×0.20 或 0.95, 单票剩余额度, 总仓剩余额度)

【收盘减仓】14:50~15:00，target = min(1.0, 0.80)
need = max(0, total_asset - cash) - total_asset × 0.80
按建仓 score 升序（最弱优先）卖出，排除手动单与今仓（T+1 卖不掉就跳过）
```

---

### 2.7 第七层：卖出

**卖出永远优先于买入**（tick 内顺序：止损止盈 → 做 T → 撮合建议 → 收盘减仓；
排序键 `(action != "sell", -score)`）。只能平多、不做空，卖出上限 = `sellable_shares`。

```
成交价 = 本 tick 实时报价（新浪逐股 > 内存快照 > 日 K 收盘）
amount = price × sold；fee = calc_fee("sell", amount)
现金 += amount - fee；实盘无滑点、无随机扰动

触发源与判定：
① 卖出信号 : action=="sell" 且有持仓 → sold = reduce_paper_sellable(...)，T+1 今仓返回「不可卖」
② 止损     : price <= base（见 2.6 的只抬不降止损线）
③ 止盈     : change >= 0.20
④ 时间止损 : held >= paper_max_hold_days（默认 0=关）
⑤ 做 T     : 涨停卖半 / 倒 T 卖出 / 反弹减仓
⑥ 轮动     : 当前关（paper_rotation_enabled=False）；开启时卖最弱换最强
⑦ 收盘减仓 : 14:50 起压回 80%
⑧ 清非交易板块 / 手动卖出 : 默认关 / place_order(side="sell")

exit_reason 结构化编码：TIME_STOP / STOP_LOSS / TAKE_PROFIT / ROTATION / OFFBOARD /
                        EOD_TRIM / T_SELL / MANUAL / SIGNAL / OTHER
15:40 settle() 再兜底跑一次止损止盈，用收盘价定稿当日净值
```

> **实盘没有「收盘确认止损」「分批止盈」**——那是回测专属实验旋钮
> （`stop_confirm_close` / `partial_tp_at`），默认关闭时与实盘一致。

---

### 2.8 费用与成交撮合（实盘 + 回测共用同一份 `Settings.trade_fee_*`）

```
【费用】engine.py::calc_fee() / pipeline.py::calc_fee()
佣金 commission = max(amount × 0.00025, 5.0 元)              # 双边，万2.5、最低 5 元
印花税 stamp    = amount × 0.001 若 side=="sell" 否则 0     # 仅卖出，千1
过户费 transfer = amount × 0.00001                          # 双边，万0.1
fee = round(commission + stamp + transfer, 2)  元

例：买入 20,000 元 → max(5.0,5.0)=5.0 + 0 + 0.2 = 5.20 元
    卖出 20,000 元 → 5.0 + 20.0 + 0.2 = 25.20 元

【实盘撮合】5 秒 tick；price = 实时报价；无滑点；cost_price 含费；
            有成交立即更新当日净值，无成交也每 30 秒按最新价重估
【回测专属（默认关）】slippage=0 / fill_noise_bps=0 / exec_fill="open"；
            entry_open_delay_minutes>0 → 开盘后 N 分钟 5m 收盘价（对齐实盘冷静期）；
            完整涨跌停 mask（涨停买不到、跌停卖不出）、VWAP1m、分钟级首次触发
```

---

### 2.9 贯穿数值例（把上面公式串起来算一遍）

**假设**：本金 100,000 元全现金、无持仓、非弱市、创业板票 `300xxx` 现价 20.00 元、
当日 ATR = 0.60 元、ML 分位 `score = 45`、本批只有这 1 只 eligible。

| 步 | 计算 | 结果 |
|---|---|---|
| 1 预算 | `effective_exposure = 1.0` → `exposure_cap = 100000`；`available_budget = 100000 - max(0, 100000-100000) = 100000` | 100,000 |
| 2 分档 | `score 45 >= 40` → `factor = 1.0`；`weight = 45×1.0 = 45`；只有 1 只 → `share = 1` | 1.0 |
| 3 单票上限 | `budget = min(100000×1, 100000×0.20, 100000, 100000)` | 20,000 |
| 4 整手 | `shares = round_buy_qty(创业板, 20000/20 = 1000)`（min 100 / step 100） | 1,000 股 |
| 5 含费校验 | `amount = 20000`；`fee = max(5,5) + 0 + 0.2 = 5.20`；`20005.20 <= cash` ✓ | 通过 |
| 6 成本价 | `cost_price = (20000 + 5.20)/1000` | **20.0052 元** |
| 7 ATR 止损 | `dist = 2.0 × 0.60 / 20.00 = 0.06`；`stop_distance = max(0.08, min(0.06, 0.20)) = 0.08` | 8% |
| 8 初始止损价 | `stop_price = round(20.0052 × 0.92, 3)` | **18.405 元** |
| 9 保本抬线 | 若 `peak >= 20.0052×1.10 = 22.006` → `base = max(18.405, 20.0052)` | **20.0052** |
| 10 移动抬线 | 若 `peak = 25.00` → `base = max(20.0052, 25×0.90 = 22.50)` | **22.50** |
| 11 止盈触发 | `price >= 20.0052×1.20 = 24.006`；若直接涨到 24.01 → `24.01 > base 22.50` → 止盈卖出 | 24.01 |
| 12 卖出结算 | `amount = 24.01×1000 = 24010`；`fee = max(5, 6.0025) + 24.01 + 0.2401 = 30.25`；`现金 += 24010−30.25 = 23979.75` | 23,979.75 |
| 13 净收益 | `(23979.75 − 20005.20) / 20005.20` | **≈ +19.87%** |

> 若价格先冲到 25.00 再回落到 22.50，则第 10 步的移动止损先触发（不是 +20% 止盈）——
> 这正是「实际常见路径：+10% 后回撤 10% 离场」的来源。

---

### 2.10 实盘 / 回测口径差异与红线

| 能力 | 实盘 | 回测 |
|---|---|---|
| 止损止盈 | 5 秒 tick 逐次判定，止损线建仓定死 | 日线 `open→low→high→close` 或分钟序列首次触发 |
| 止损生效日 | 每个交易日无条件 | 每个交易日无条件（共用 `resolve_stop_price`；曾漏 134/250 天，已修） |
| 涨跌停 | 仅「涨停禁买」+「涨停卖半」 | 完整 mask（涨停买不到 / 跌停卖不出） |
| 弱市降仓 | `min(1.0, weak_exposure)`，0.8 由收盘减仓压回 | `day_exposure = min(exposure, weak_exposure)`，无收盘减仓 |
| 成交价 | 实时报价，无滑点 | 开盘价 / VWAP1m / 开盘+N 分钟 5m 收盘；可加滑点扰动 |
| 队列/候补 | topM=5、TTL=30min、backlog=30 | 无队列，仅按评分截断每日买单 |
| 收盘确认止损 / 分批止盈 / 调仓周期 | **无** | 有（默认关闭 = 与实盘一致） |
| 策略集 | `.env` 的 `AUTO_SCAN_STRATEGIES`（只读，当前 A-Only「量价共振2560」） | 默认跟随同一字段；空数组两侧都等于「全部已注册策略」 |
| ST / 退市过滤 | scanner 与买入池走 `is_tradable → is_st_name` | 建池走同一个 `is_st_name`（先剔再排名） |

**红线**：① 14 因子必须喂 `add_all_indicators(df)`；② 只能做多 / T+1 /
`max_positions` 是上限不是门槛 / 0.8 是收盘目标（盘中可满仓）；
③ ML 看 `effective`，`false` 是整段回退 14 因子，不能当「ML 有效」证据；
④ 展示用参考位（10 日最低 / `price×1.15`）**不是执行位**，执行止损走
`max(8%, 2×ATR/close)`；⑤ 线上真实值只看 `GET /api/settings` 的 `source`；
⑥ 回测默认策略集必须跟随 `AUTO_SCAN_STRATEGIES`（回测 #44 曾因该键缺失跑成 14 策略全开，
结论无法解释 A-Only 实盘），且股票池 ST 过滤必须与实盘共用 `scope.is_st_name`；
⑦ 止损判定两侧必须共用 `app.risk.stops.resolve_stop_price` 且不得带大盘门控
（回测 #45 曾漏掉 134/250 天的止损，让「移动止损吃掉全部盈利」成为假象）；
任何一侧改动后跑 `check_backtest_alignment.py`，必须零漂移且
「股票池 ST 口径 = 一致」「止损门控口径 = 一致」。

---

## 三、目前已完成的回测结论（只列目的与结论）

> 约定：只写「测试目的 → 结论」。过程、样本、归因见 `BACKTEST_LOG.md` 与 `docs/backtest_log/`。

### 3.1 验收基线与统计方法

| 目的 | 结论 |
|---|---|
| 18 段不重叠随机段（各 21 交易日）验基准 | 中位超额 +1.11%、67% 段为正，alpha 方向真实；但 ±10bp 扰动极差 3.44pp，真实量级仅 ≈2.1pp → **验收门槛提高到 >3.5pp 并停止参数搜索** |
| 31 段大样本收紧置信带 | 中位 +1.85%、均值 95% CI [+0.20%, +4.48%]，期望为正已稳；2 个窗口即可初判 |
| 全历史净值曲线（2025-01~2026-10） | 策略 −12.96% vs 等权基准 −47.97%、超额 +35.01%、回撤 −19.15%；属单条路径，不作预期收益 |
| 终版 6 策略横向评测 | MACD 金叉是唯一负配对、均线放量/KDJ 最强，但无一越过 3.5pp → **维持 6 策略集成**；KDJ 单独为彩票型，不宜独立用 |
| 随机三个月短线回测 | 4/5 窗口完全空仓、全年盈利 93% 来自 2025-08 单月；查出预热截断幸存者偏差，全年基线 +8.50% 应修正为 **+0.48%** |
| 随机一个月周期回测（N=30/12） | 放大样本后结论翻转：**A（量价共振2560）是唯一正期望**（月均 +0.51% / 三月 +2.73%），B（涨停后5日）净负拖累，A+B 劣于单 A → **live 改只跑 A** |
| 两个择时层分层回测（N=12） | base +0.24%；+指数择时与 base 几乎相同（与 MA30 重合），+ETF 轮动降到 +0.10% → **增量边际/偏负，不接 live** |
| 7 个 GitHub 策略三窗回测 | 量价共振2560 三窗全正、风报最佳、最可落地；动量 T3 盈利但 422 笔换手不可执行；底反 ADX ≈ 基线 |
| B+A 解除底反「买不上」 | 确实解除踏空，但四种 MA 设置三窗全亏（BA −25.59%）→ **MA 是参与度旋钮不是盈利旋钮，策略本身负期望** |
| 正式多板块短线回测（5373 只） | −49.88%（单创业板 +8.47%），亏损全来自深主+科创 → **默认仅创业板是有益保护** |
| 严格对齐年度回测（#45，A-Only + 剔 ST，2025-10-09~2026-10-20） | −1.95% vs 等权基准 −6.47%（超额 +4.52pp）、回撤 −13.56%、140 笔；明显优于同区间「14 策略全开 + 未剔 ST」的 #44（−5.17%／−18.87%／−57% 尾部单笔）→ **A-Only 决策成立，ST 过滤消除极端尾部亏损**。注：该轮仍带「弱市才止损」门控缺陷（134/250 天不止损），只可用于策略集 / ST 的横向对照，**不能当作实盘收益预期** |
| 止损门控修复后复跑（#47，与 #45 参数逐项零差异、同一基准） | **+13.28%** vs 基准 −6.47%（超额 +19.75pp）、回撤 −10.04%、夏普 0.78、150 笔；强市日补回 35 笔止损，最差单笔 −17.93%→−10.36%，盈亏比 0.65→0.88 → **实盘规则一直是对的，是回测少执行了 134 天止损而系统性低估策略**；#44/#45 一类悲观结论需重估。仍属单条路径，不作收益预期 |
| 修复口径后的多段前向验证（6 段不重叠、450 交易日、267 笔，参数与实盘 84 项零差异，#48~#53） | 正收益 4/6、跑赢基准 4/6；平均超额 **+1.61pp**、中位 +3.15pp，但标准差 9.44pp、极差 31.32pp（−15.08~+16.24pp）→ **#47 的 +19.75pp 不是普遍 alpha，而是「跌市抗跌」在下跌年被放大**（基准下跌段 2 段平均 +9.96pp；上涨段 4 段平均 −2.56pp）。与历史「alpha 量级薄」结论一致；不作收益预期，按纪律不调参 |

### 3.2 参数与形态校准（绝大多数为「证否」）

| 目的 | 结论 |
|---|---|
| 等回踩买入 / 分钟级确认强势 | 均不采纳：等回踩是逆向选择、确认类无增益 |
| 全时段买入 + 盘中多时点重确认 | 不采纳，配对中位 −1.18pp；入场信息在开盘 15 分钟内已耗尽 |
| 止损止盈/盈亏比结构调参 | 随机段全否决（中位 −1.36pp），「小亏换大赚」不靠调出场线实现 |
| 更短周期 `fast` | 明确拒绝，无净增益且单段 −22.60% 尾部不可接受 |
| 移动止盈 / 时间止损 / 主动离场（8 种出场） | 对均值回归策略是减分项，样本外全负，时间止损默认关闭 |
| 还原线上真实出场（ATR+保本+移动） | 期望中位由 +1.11% 校准到约 0%，属**预期校准**而非策略失效，参数冻结 |
| 做 T 系统化回归 / 5m vs 1m 敏感度 | 零滑点首次过线，但 0.3% 滑点转负、1m 加滑点 −1.87% → 该族关闭 |
| 机械 T+0 做 T 信号族（约 50 组） | 散户约束（无融券+真实成本）下**不存在可交易边缘**，最优形态仍 −1.874% |
| 满仓轮动是否贡献换手/收益 | Top300 下几乎不触发，轮动不是 alpha 来源 |
| 大盘分档择时 / 弱市降仓档位寻优 | 三档均不过验收；**没有最优比例，只有「少参与少亏」，择时不产生 alpha** |
| 弱市保留 30% 仓位（#63） | **采纳**，但定性为风险偏好档位：−0.55pp/月换日胜率 +17pp、急跌月少亏 6.4pp |
| 弱转强爬坡 ramp | 不采纳（+0.35pp、t=0.94），`topM=5 × 单票20%` 已有自然爬坡 |
| ML 截面排序验收 | LightGBM +4.55% / 行业中性 +3.95% 过 3.5pp 线（采纳为候选+监控）；Ridge、朴素融合未过线 |
| 复核 `live_ml_scoring` 该不该开 | 重新打开：原 `effective=false` 是短窗按纪律回退而非无效，判读窗口 2026-12-01 |
| 分位门槛 / 20 日反转方向 | 门槛不是瓶颈；反转方向换种子不复现，方向这条线关闭 |
| 降频调仓 / 集中持股 / 池宽 | 中枢改善但配对不稳、集中是杠杆不是 alpha；30+ 方案无一通过随机段 |
| 组合回撤断路器 | 不通过（中位 0.00、胜段 2/18），形态维度至此全部关闭 |
| 「不追高 1.5%」入场过滤 | 涨市中位 −4.54pp 且是「买差」不是「少买」→ **已关闭为 0** |
| 剔除高位 20% / 压缩持有期 | 均不采纳（样本内 +7.1pp / 样本外 −2.6pp；t=−2.62） |
| 创业板底部反转、超跌低阈值、纯净超卖抄底 | 全部不采纳：Edge 在风控不在方向；「大涨股 2/3 从底部起来」是幸存者偏差 |
| 开盘冷静期（#108） | 用户拍板实盘 + 回测同步为开盘后 5 分钟，保持可比 |

### 3.3 6 个同类项目与 A-Only 决策

| 目的 | 结论 |
|---|---|
| #1 Kaka Quant 涨停后5日 | 接入后随机月证伪为净负拖累 → 从 live 剔除 |
| #2 PerCheung 25-60 量价共振 | 三窗全正 + 随机月唯一正期望 → **唯一 live 策略** |
| #3 QuantLab Model-A | 入场实为 Kronos+LLM 重排，动量阈值是本项目代理 → 校准后仅研究 |
| #4 KHunter 底反 ADX | 底反参数来自本项目，ADX 闸门全样本 0 信号 → 仅研究 |
| #5 ETF 轮动择时层 | 月均 +0.24%→+0.10%，无增益 → 未接 live |
| #6 广发 125 指标择时层 | 允许交易日与 MA30 高度重合，几乎等同 base → 未接 live |
| 6 repo 整体去留 | 用户拍板 **A-Only**：`AUTO_SCAN_STRATEGIES=["量价共振2560"]`，其余只留研究资产 |

### 3.4 Alpha / 探针类研究

| 目的 | 结论 |
|---|---|
| 学界共识与实测对齐 | C1（GBDT>线性）、C2、C3、C5 均被支持 → 方向性结论采用，具体实现自行验证 |
| 「突然加速股」启动前特征 | 活跃度可筛、幅度不可预测（R² 3~25% 且跨月不稳）→ 不作因子 |
| 大涨前 1~5 日盘面特征 | 分钟线 20+ 特征全是噪声；查准率 2.42x 但扣成本收益为负 |
| 大涨前 1~2 月「资金埋伏」 | **没有提前埋伏证据**，强特征都是股性基线 |
| 大跌前 1~2 月「资金出逃」 | 只有「先涨后放量跌 + 大单由买转卖」弱迹象（分位 0.58） |
| 「缩量下跌→次日大涨」/ 多周期方向 IC | 假设不成立且方向相反；日频不可交易，5~20 日反转稳定但落地前须配对回测 |
| 摆动影子 / 事前预估转折系列 | 概率校准信息量真实但命中率远低于 71% 门槛、8 种出场 OOS 全负 → `no_tradeable_edge` 收口 |
| 机制层 / 熔断 / 政策托底 | 仅「弱确认」、零前向样本，默认全关，判读同 2026-12-01 |
| 涨跌家数广度区分震荡/自由落体 | 去重叠后 4 年仅 5 样本且方向相反，降为纯观测指标 |

### 3.5 一句话现状

- **已落 live**：14 因子/ML 打分 + 标准风控 + 大盘择时降仓；弱市 30% 仓位；开盘冷静期 5 分钟；
  `entry_max_chase=0`；板块统一为创业板、趋势看全市场中证全指；成交额 TopN 改为板块内取；
  回测与实盘 1:1 对齐护栏；**A-Only**（自动扫描只跑量价共振2560，策略库仍保留 6 个内置策略）。
  注：「维持 6 策略集成」是基线阶段的结论，A-Only 是后续随机月回测后的 live 决策，两者属不同阶段、不冲突。
- **仅研究资产、未接 live**：两个择时层（ETF 轮动 / 125 指标）、涨停后5日、动量 T3、底反 ADX、
  机械 T+0 信号族、摆动影子系列、三份大行情探针、机制层/熔断/政策托底、轮动与做 T 的 alpha 证据
  （轮动/做 T 虽按「把规则跑全」默认开启，但**无 alpha 证据**）。
- **方法学总判断**：alpha 方向真实但量级薄（≈2pp/21 天），**成本与执行摩擦是第一杀手**，
  因此已停止参数搜索，转入前向验证。

---

## 四、后续改善与完善方向

### P0 · 一致性与正确性（先做，否则后面结论都不可信）

| # | 事项 | 为什么 / 具体做法 | 验收标准 |
|---|---|---|---|
| 1 | ~~**重启后端，加载本轮累积代码**~~ ✅ **已完成（2026-11-12 晚实测）** | 运行中的服务已加载全部新代码：`/api/backtest/alignment` → `ok=True` / 零漂移（41 项绑定、ST 口径一致、止损门控一致）；`/api/backtest/defaults` → `strategies=['量价共振2560']`、`board_prefixes=['cyb']`；`/api/settings/selection` → 3 段参数组。选股面板端点、板块内 TopN、双向落盘映射、阶段进度上报、策略集跟随 `AUTO_SCAN_STRATEGIES`、ST 过滤同源、`app.risk.stops` 共用止损均已生效 | 原验收标准逐项通过；后续只做回归复检 |
| 2 | **把真实成本模型前置到主回测** | #155/#156 已证明「最低佣金 5 元/腿 + 印花 + 滑点」能把薄边缘从 +0.06%/笔打到 −2%~−8%，但真实成本目前只在做 T 研究里；主回测仍无滑点 → 系统性高估 | 所有新实验默认跑 0 / 0.1% / 0.3% 三档滑点；回测成本口径与实盘逐项可对照 |
| 3 | **消除日线回测 vs 5 秒 tick 实盘的粒度差** | 目前「参数 1:1」但执行层不等价（涨跌停 mask、收盘减仓、成交价、队列/候补都不同）；应显式标注哪些结论只在日线口径成立，并把 tick 级路径做成可选 | 对齐自检增加「执行层差异清单」，每条注明影响方向；关键结论在两种口径下各跑一次 |
| 4 | **治理文档/注释漂移** | `flow.md`/`sas_formulas.md` 是旧快照（如「池 300」实为 0、`window=5` 实为 0）；另有 3 处注释与实现不一致（crisis 硬拦 vs MA20、做 T 默认、底反软闸双默认） | 加一个 `config.py ↔ 文档` 自动比对脚本，纳入重启自检；修掉已列出的不一致 |

### P1 · 验证纪律与策略健康度

| # | 事项 | 为什么 / 具体做法 | 验收标准 |
|---|---|---|---|
| 5 | **前向验证，不提前调参** | ML 有效性、机制层三开关、政策托底的判读窗口定为 2026-12-01；在此之前只记录不动参数。**首轮已完成**：`backend/scripts/forward_validation.py` 按时间前向切 6 个互不重叠段（450 交易日），参数一律由 `build_params()` 从实盘生成、逐字段断言零差异，结果落库 #48~#53；结论见 3.1 末行 | 判读日按 `effective` / OOS rank-IC / 前向超额配对出结论，写新编号记录。首轮 6 段 ML 全部生效（OOS IC 0.0118~0.2085），平均超额 +1.61pp、下跌段 +9.96pp / 上涨段 −2.56pp；仍按纪律不动参数 |
| 6 | **A-Only 单策略集中度监控** | live 只剩一个策略，缺「策略健康度」指标；需监控命中数、连续空仓天数、单月收益贡献占比、信号衰减 | 每日复盘增加策略健康度卡；设降级/暂停阈值（如连续 N 日无命中、单月贡献 >90% 告警） |
| 7 | **放开多板块的前置条件** | #182 已证多板块 −49.88%；切板块前须为各板块建等权合成趋势指数、补齐北证 50 指数 parquet、重跑多板块配对 | 每个板块单独回测为正、且趋势指数覆盖率 >95% 才允许改 `twin_t_boards` |
| 8 | **成本敏感度成为实验硬门槛** | 多次出现「零滑点过线、加 0.3% 转负」（做 T、分钟切入点） | 验收脚本强制输出三档滑点结果，任一档转负即不采纳 |
| 9 | **把「研究资产」与 live 的隔离自动化** | 目前靠人工记忆维持 A-Only 与「仅研究」清单 | 加一个 live 白名单校验：启动/落盘时若出现非白名单策略或研究旋钮被写进实盘，直接告警 |

### P2 · 工程、数据与可复现性

| # | 事项 | 为什么 / 具体做法 |
|---|---|---|
| 10 | **数据质量治理** | 分钟线存在 `close=0/volume=0` 脏 bar（dev 可达 −100%~+2850%），已有 `|dev|>15%` 清洗但应前置到入库；行业映射、指数 parquet 覆盖（bse 常缺）、快照失败的降级路径统一 |
| 11 | **回测 run_id 自动绑定参数快照** | 现在复跑依赖 `profiles.py` 与人工记 `--universe`；应把 `get_settings()` 全量 + 覆盖层 source + 数据日期区间 + 随机种子随每次回测落库，`repro_run.py` 一键复现 |
| 12 | **把 21 项选股参数与对齐自检接进每日体检** | `param_advisor.py` 目前只查回撤/波动/收益/胜率；应加上「对齐 ok / 板块一致性 / 数据新鲜度 / ML 三态」 |
| 13 | **参数中心减负** | 107 项 + 三层优先级认知成本高；给「仅研究/已废弃」项加视觉分组与只读标记，把常用项收敛成少量预设档位（保守 / 标准 / 进攻），同时保留高级展开 |
| 14 | **真实券商桥接（若将来需要）** | 目前纯模拟盘；若接入实盘，须先补冲击成本、部分成交、撤单、盘口 L2、涨跌停排队等，且不可直接复用现有撮合假设 |
| 15 | **补齐数据源层次** | 三份大行情探针一致指出：分钟级价量特征是噪声，缺的是 L2 逐笔/筹码分布/事件驱动；这些是比继续调 5 分钟信号更可能的 alpha 来源 |

### 当前待办（最短路径）

> 重启已于 **2026-11-12 23:03 远程完成**：`Start-ScheduledTask -TaskName 'SASRestart'`（`LastTaskResult=0`），前后端健康检查 OK，新代码已加载，验收见 P0-1。**不需要人在电脑前双击**；用户侧等价入口仍是 `E:\study\sas\scripts\ops\restart-backend.vbs`。此前「AI 环境无法自行重启」是触发方式选错（Bash 里 `schtasks` 被安全策略拦、`Stop-Service` 权限不足），已更正。

1. 浏览器硬刷新一次；跑 `scripts/check_code_sync.py`（退出码 0 = 磁盘代码与运行服务同步）与 `scripts/check_backtest_alignment.py`（零漂移）——两项均已验证；
2. 跑一次 live 校验：`/api/backtest/alignment`、`/api/paper/exit-audit`、`/api/settings/selection`、`/api/scope/stats`；
3. 明早开盘后留意第一笔卖出是否正常落流水（验证锁死锁修复在真实链路上生效）；
4. 之后按 P0-2 / P0-3 推进成本与口径，2026-12-01 再做 ML/机制层判读。

---

## 附：资料索引

| 想查 | 文件 |
|---|---|
| 代码全景 / 端到端主线 | `docs/overview.md` |
| 一次买卖链路与 skip 原因 | `docs/flow.md` |
| 全部公式与参数总表 | `docs/sas_formulas.md` |
| 回测实验（编号唯一、只追加） | `BACKTEST_LOG.md`、`docs/backtest_log/2026-10.md`、`2026-11.md` |
| 为什么这么改 | `WORKLOG.md` 第十一节 |
| 策略终版 / Alpha 结论 / 文献共识 | `docs/analysis/STRATEGY_REPORT.md`、`ALPHA_CONCLUSIONS.md`、`LITERATURE_CONSENSUS.md` |
| 做 T 信号族研究 | `RESEARCH_SUMMARY.md` |
| 每日复盘 / 参数体检 / 持仓盈亏 | `docs/daily_trade_review.md`、`docs/param_advisor.md`、`docs/position_pnl.md` |
| 回测↔实盘对齐契约 | `backend/app/backtest/alignment.py`、`backend/scripts/check_backtest_alignment.py` |
