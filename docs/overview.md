# SAS 代码全景梳理（心智模型）

> 生成日期：2026-11-04。**参数默认值以 `backend/app/config.py` 为准，本文件只做索引与串联。**
> 定位：给"改动太多、记不清"时快速重建全局用。`README.md`=能做什么，`docs/flow.md`=一次买卖怎么走，
> `docs/sas_formulas.md`=公式口径，本文件=**把代码按层 + 按主线串起来**。
> 文档统一入口见 `docs/README.md`。

**目录**：[一、文档分工](#一文档分工) · [二、分层架构](#二分层架构) · [三、端到端主线](#三端到端主线) ·
[四、三套分数与排序](#四三套分数与排序) · [五、三层配置](#五三层配置) · [六、实盘子系统](#六实盘子系统paper) ·
[七、回测子系统](#七回测子系统backtest) · [八、近期改动](#八近期改动按主题) · [九、当前生效状态](#九当前实盘生效状态) ·
[十、口径红线](#十口径红线) · [十一、排错入口](#十一排错入口)

---

## 一、文档分工

| 文档 | 回答 |
|---|---|
| `README.md` | 能做什么（功能/开关/部署） |
| `docs/flow.md` | 一次买卖从哪来、被哪些判定/参数挡住（最该通读） |
| `docs/sas_formulas.md` | 公式是什么（因子/风控/撮合/回测参数表） |
| `WORKLOG.md` | 为什么这么改（策略变更逐条） |
| `BACKTEST_LOG.md` | 回测实验/归因结论（编号唯一，只追加） |
| **`docs/overview.md`（本文件）** | 代码分层 + 主线 + 近期改动 + 排错（全局地图） |

一句话总纲：**选股 → 打分 → 入队 → 撮合 → 引擎五段式 → 记账/复盘**；回测用同一套口径复刻。

---

## 二、分层架构

`backend/app/` 主要子包：

| 层 | 目录 | 职责 | 关键文件 / 符号 |
|---|---|---|---|
| 数据 | `data/` | 股票池/K线/分钟线/快照/SQLite | `cache.py`(Database + 表)、`service.py`(`get_kline_cached`)、`minute.py`、`snapshot_store.py`、`scope.py`(`is_tradable`)、`breadth.py`、`fallback.py`(直连源) |
| 指标 | `indicators/` | 纯 Pandas 指标 | `technical.py::add_all_indicators` |
| 分析/选股 | `analysis/` `strategies/` `screener/` | 14因子、技术面分、策略库、全市场扫描 | `factors.py::compute_factors`、`scoring.py::score_technical`、`recommender.py`、`scanner.py::rank_hits`、`builtin.py` |
| 交易 | `paper/` | 实盘决策 + 秒级撮合 + 并行账本 | `engine.py`(PaperEngine)、`live.py`(LiveTrader)、`ledger.py` |
| 回测 | `backtest/` | 全流水线回测 | `pipeline.py`(主线)、`profiles.py`、`apply.py`、`review.py`、`ml_scorer.py` |
| 配置 | 根 | 三层配置 + 参数中心 + 审计 | `config.py`(Settings)、`param_specs.py`、`audit.py`、`api/settings.py` |
| 接口/前端 | `api/` `frontend/` | REST + SPA | `api/*.py`；前端 **Vue3 + TS + Vite**，`src/views/Settings.vue`（参数）、`src/views/Ops.vue`（运维审计） |
| 调度/运维 | `monitor/` `scripts/` | 定时任务 + 数据/复盘脚本 | `monitor/scheduler.py`、`scripts/*` |

> 纠正：前端是 **Vue3**（非 React）；`cond_pair.py` 在 `backend/scripts/`（是脚本，不在 `app/backtest/`）。

---

## 三、端到端主线

```
触发(9:20 盘前 / 交易时段每 1min / 15:30 收盘扫描 / 15:35 盘后 / 15:40 结算 / 16:00 K线预热 / 20:30 K线回填 / 16:45 影子)
 → 数据(池 → get_kline_cached → add_all_indicators → 实时快照)
 → 扫描(is_tradable → 并发20 → 逐策略 evaluate → 技术面 ±8) → 第1次排序 signals.rank
 → 14因子打分(±100；buy≥+15 / sell≤−15) → 第2次排序 recommendations
 → 入队 LiveTrader.submit(hold 撤旧 / sell 不限额 / buy topM=5；TTL；窗口外顺延)
 → 撮合 tick(每 5s：清过期 → 交易时段门 → 取价 → 止盈止损 → 做T)
 → 第3次排序(卖永远排买前)
 → 引擎 execute_recommendations 五段式
 → 结果分流：成交 / 取价失败重试 / 名额·资金不足转候补 / 其余出队
 → 卖出风控(T+1 → 卖出信号 → 止损止盈 → 轮动 → 做T → 收盘减仓)
 → 15:40 结算净值 / 复盘
```

**买入五段式（`paper/engine.py::execute_recommendations`）**

1. **风控闸门**（优先级 熔断 > 动态机制 > 弱市 MA60）：只压总仓位上限，不抬高。
2. **准入五闸**（`entry_gate`）：北交所 / 板块 / 大市值 / 名称 / 成交额 TopN 池 + 已持仓 + 名额。
3. **切入点**（`entry_price_ok`）：取价 → 追高（默认关）→ 回踩（默认关，留队重试）。
4. **评分分档**：<25 只记录不下单；25–40 系数 0.5；≥40 系数 1.0。
5. **分钱整手**：`min(可投×权重, 总资产×20%, 现金, 可投)` → 整手 → 逐手回退费用 → 落批次（成本价含费 + 止损线一次定死）。

**卖出风控**：T+1 只卖 `buy_date<today` 的批次（FIFO）；止损线 `max(8%, 2×ATR)`（上限20%）建仓定死、盘中只抬不降（保本 10% / 移动回撤 10%）；轮动默认关闭；做T默认开；收盘前 10 分钟 `eod_trim` 把总仓位压回 80%。

---

## 四、三套分数与排序

| 分数 | 来源 | 区间 | 作用 | 落库 |
|---|---|---|---|---|
| 技术面分 | `analysis/scoring.py` | **±8** | **只用于排序**，不决定买卖 | `signals.rank` |
| 14因子 `total` | `analysis/factors.py` | **±100** | **判买卖**（±15）+ 仓位分档（25/40） | `recommendations.score` |
| ML 分位 | `analysis/live_ml.py` | 0–100 | **替换**因子分；`effective=false` 即整段回退=基线 | 同上 |

三个排序：①信号排序（技术面±8）②建议排序（因子/ML）③队列与分钱（沿用②）。**别把 ±8 当买卖依据。**

---

## 五、三层配置

优先级：**`app_state` 覆盖层 > `.env`/环境变量 > `config.py` 默认值**。

- 覆盖层键：`param_specs.RUNTIME_KEYS` 用裸键，其余带 `cfg:` 前缀（`OVERRIDE_PREFIX`）。
- 改配置正确姿势：**走设置页 / `POST /api/settings`**；启动时 `apply_persisted_overrides()` 重放。
- 判断是否真生效：看 `GET /api/settings` 每项 **`source`**（`app_state`/`env`/`default`）。
- 重要开关**两处都写**（覆盖层 + `.env`），否则一次"还原"就回弹（见 `61364f1` #114 配置生效铁律）。
- 新增参数三件套：`config.py` 字段 +（热改则）`param_specs.RUNTIME_KEYS` + 前端 `Settings.vue` 自动动态渲染（无需改前端）。

---

## 六、实盘子系统（`paper/`）

- **`engine.py`（PaperEngine）**：`execute_recommendations` 为决策核心（卖→闸门→轮动→买五段式）；开盘保护 `_open_delayed`/`_open_cooldown` 合成 `buy_paused`；止损止盈 `check_stop_loss_target`；做T `t_tick`；收盘 `eod_trim`；结算 `settle`。
- **`live.py`（LiveTrader）**：`submit` 入队（买 topM=5，卖不限额）；`tick` 撮合；`_in_buy_window` 开盘窗口；**开盘保护 `open_guard = _open_delayed or _open_cooldown`**；`_backlog_*` 候补；`_tick_sim` 并行账本。
- **`ledger.py`**：`PROFILE_TO_PAPER` 把回测方案映射成引擎参数（`OverlaySettings`），并行沙盘只做**参数敏感性对照，不是独立策略**。
- **数据表**：`paper_account/positions/position_lots/trades/equity/pending`；并行 `sim_*`；选股 `signals`、`recommendations`；运行时 `app_state`。

---

## 七、回测子系统（`backtest/`）

- **`pipeline.py`（主线）**：`PipelineParams=None` = 沿用线上 Settings（同口径关键）；universe 按**当日成交额 TopN**；信号只用 `df.iloc[:t+1]` 防未来函数；入场价 `exec_open_price`（`entry_open_delay_minutes` → VWAP → 开盘价）；`entry_confirm/entry_window` 分钟级切入点；`t_minute_path=True` 才与线上做T同序。
- **`profiles.py`**：`default`(基线)、`default_ml`、**`cur10`=线上镜像**（`max_positions=10`、轮动关、`entry_open_delay_minutes=5`）、`adapt4_10*` 等 100+ 方案。
- **`apply.py`**：回测参数"落盘"到实盘，只映射真有落点的键（避免"页面改了实盘没变"）。
- **`scripts/`**：`random_dist.py`(多段配对)、`accept.py`(一键验收)、`repro_run.py`(可复现复跑)、`cond_pair.py`(regime 段配对)、`live_review.py`、`daily_review.py`、`weekly_review.py`、`param_advisor.py`。

---

## 八、近期改动（按主题）

| 主题 | 实质 | 状态 |
|---|---|---|
| **开盘冷静期**(`fbee62b` / BACKTEST_LOG #108) | `paper_open_cooldown_minutes`：09:30/13:00 后 N 分钟**无条件禁买**（含轮动/做T买回），卖出照常；回测 `entry_open_delay_minutes` 同步 + `cur10=5` | 线上 **5** |
| **关实盘轮动** | `paper_rotation_enabled=false`（#99/#104 + 11-03 whipsaw 实证） | 线上 **False** |
| **#108-#113 六项机制**(`8de303b`) | ①分数优先轮动（`_rotation_liquidate` 弱市纯卖腾名额）②待买不过期（过期买单降级进 backlog，顺延次日开盘）③盘内错峰 `paper_buys_per_tick` ④全时段窗口(window=0)+冷却期 ⑤机制层止损收紧 `paper_regime_stop_tighten`(默认0.6) ⑥并行账本判读(`role/max_drawdown/ledger_interpretation`) | 代码已落 |
| **每日复盘市值口径修复** | `daily_review.py` 只计当前持仓，别把已清仓票按成交股数虚增 | 已修 |
| **配置生效铁律**(`61364f1` / #114) | app_state 压 .env，改配置走 API 并回读 source | 文档 |
| 更早 | 开盘保护(#98 跳空护栏/延迟)、T加仓套仓位上限、轮动三条口径纠正、队列丢单修复、撮合队列持久化、20:30 K线回填、影子账户等 | 已落 |

> `8de303b` 标题中的"#108-#113"是**改动计划编号**，不是 `BACKTEST_LOG` 记录号；`BACKTEST_LOG` 最后一条记录是 **#108（开盘冷静期）**。

---

## 九、当前实盘生效状态（2026-11-04 核对）

| 参数 | 值 | source |
|---|---|---|
| `paper_rotation_enabled` | **False** | app_state |
| `paper_open_cooldown_minutes` | **5** | app_state |
| `paper_buy_open_window_minutes` | **0**（全时段可买） | default |
| `paper_buy_open_delay_minutes` | 0（仅跳空才延迟） | default |
| `paper_t_active` | **True** | app_state |
| `live_ml_scoring` | **True** | app_state |
| `paper_weak_exposure` | **0.30** | app_state |
| 机制三件套(dynamic/policy/circuit) | 全 **False** | default |

真实成交模型 = **全时段可买 + 开盘后 5 分钟冷静期 + 弱市降到 30% 仓位 + 轮动关闭**。

---

## 十、口径红线

1. **14 因子必须喂 `add_all_indicators(df)`**，否则 `bias6/atr` 缺失 → `total` 被压进 ±20 → "买不进任何票"假象。
2. **线上池 300，`BacktestParams` 默认 500**：直接跑脚本要 `--universe 300`。
3. **只能做多 / T+1 / `max_positions` 是上限不是门槛 / 80% 是收盘目标（盘中可满仓）**。
4. **ML 看 `effective`**：`false` = 整段回退基线，不能当"ML 有效"证据。
5. **备份 WAL 库用 `scripts/backup_db.py`**，别 `Copy-Item`。
6. 本机 `.env` 的 `window=0` 会让 `test_exec_rules` 2 例失败——**环境口径，不是回归**。

---

## 十一、排错入口

1. **为什么没买/没卖**：`docs/flow.md` §5 skip 原因速查表 + `GET /api/settings` 的 `source` + `backend/logs/backend.log`。
2. **参数改了没变化**：先看 `source` 是否被 `app_state` 覆盖层压着；重要开关两处都写。
3. **实盘 vs 回测对不上**：确认 `cur10` + `entry_open_delay_minutes`、`--universe 300`、`t_minute_path`。
4. **复盘**：`daily_review.py`(单日) / `weekly_review.py`(周) / `live_review.py`(超额) / `param_advisor.py`(体检)。

---

## 十二、重建结构的阅读顺序

1. `docs/flow.md` 通读一遍（1 小时，建立主线）。
2. `backend/app/config.py` 扫字段（知道有哪些旋钮）。
3. `paper/engine.py::execute_recommendations` 走一遍（决策核心）。
4. `paper/live.py::tick` 走一遍（队列/窗口/开盘保护/并行账本）。
5. `backtest/pipeline.py` 只看 `exec_open_price` / `_simulate` / `entry_*`（同口径）。
6. 具体疑问按编号查 `BACKTEST_LOG.md`。
7. 维护约定：改选股/评分/交易/回测 → 同步 `README` + `docs/sas_formulas.md` + `WORKLOG.md`；回测实验 → `BACKTEST_LOG.md` 唯一编号。
