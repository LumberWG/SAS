# SAS — A股智能分析系统

面向国内 A 股（沪深京）的个人学习研究工具：实时行情监控、技术指标策略全市场选股、历史回测，并可选接入大模型生成结构化买卖建议。

> **免责声明：本系统仅供学习研究使用，所有数据与结论不构成任何投资建议。股市有风险，投资需谨慎。**

## 功能总览

| 模块 | 能力 |
| --- | --- |
| 实时行情 | 三大指数概览、涨跌家数、成交额统计、市场情绪分级（5 档权重）、自选股盯盘 |
| 自选股 | 个股详情页/扫描结果一键加自选（⭐按钮），Signals 页管理列表，实时价盯盘 |
| 智能建议 | 收盘自动选股评分 TopN 自动加自选；盘中 5 分钟分析自选股生成买入/卖出建议；盘后生成明日关注 |
| 策略选股 | 6 种内置经典策略 + 自定义策略，全市场 5000+ 股票并发扫描，命中信号自动持久化 |
| 策略编辑 | 所有策略参数前端可调，支持另存为自定义策略（持久化，重启自动加载） |
| LLM 智能分析 | 接入任意 OpenAI 兼容大模型，生成决策仪表盘（结论 + 入场价/止损价/目标价 + 六维检查清单） |
| 历史回测 | 信号驱动回放，输出收益率/胜率/最大回撤/夏普与净值曲线 |
| 定时监控 | 盘中快照刷新、异动检测（涨跌停/放量突破/急涨急跌）、收盘后自动选股推送 |

## 系统设置

`/settings` 页把散在四处的配置拉成一张表：`.env`/`config.py`（88 项）、`app_state` 运行时覆盖、页面上各业务开关、以及少量硬编码常量（可交易板块前缀、巨无霸黑名单、回测默认止损止盈、整手股数）。

页面上**只列可编辑的 78 项**。以下 10 项都**完全不显示**（`READONLY_KEYS`）：`api_token`（访问令牌，回显等于把钥匙挂在门上）、`db_path`、`kline_dir`、`cors_origins`、`data_no_proxy_domains`、`auto_scan_strategies` 与 `app_name`（只在启动时用于 `FastAPI(title=)`，改它连前端标题都不变）、`debug`（全仓库无人读取的历史字段）、`disclaimer`（接口 JSON 统一注入的文案常量，不是配置）、`notify_email`（`EmailNotifier` 目前只打日志并返回 False，开了反而多记一条渠道失败），以及剩余硬编码常量（`LOT_SIZE`、板块前缀、巨无霸名单、回测默认止盈止损）。理由一致：改不了或改了没效果，展示了只会让人以为能调。

> 评分阈值（`factor_buy_threshold` / `factor_sell_threshold`）与四项费率（`trade_fee_*`）**已从硬编码挪进配置**：前者决定 buy/sell 判级，后者被回测与实盘共用。整手股数（`LOT_SIZE=100`）仍固定——改了会下出非法委托。

- **外观主题（顶栏右侧下拉）**：位于「未保存」指标右边、分隔线之前；石墨黑 / 深海蓝 / 象牙白 / 暖沙，**默认象牙白（浅色）**，选完即时生效。按钮与每个菜单项左侧都保留四格预览色（页面底 / 卡片 / 主色 / 文字），收起时也能看出当前配色。
  这是**纯前端偏好**（存 `localStorage`），不进 `app_state`、不影响后端与其它电脑 —— 显示习惯不该混进业务参数表
- **条件可见**：子参数依赖父开关时默认隐藏（例：`paper_t_active` 关着时，8 个做T 参数不显示；
  `llm_enabled` 关着时，接口地址/密钥/模型/超时都不显示）。关系在 `param_specs._DEPENDS_ON` 里声明，
  父项为开关则「开=显示」、为文本则「非空=显示」，支持套娃；前端按**草稿值**实时显隐
  （打开做T 开关的那一下，下面参数立刻出现，不用等保存），分组标题会标「随开关隐藏 N」
- **两级布局（均衡分组）**：左侧竖排导航 8 个分组——连接与扫描 9 / 选股与自选 11 / 账户与费率 7 / 交易执行 13 / 仓位与轮动 8 / 风控与止损 11 / 做T 与择时 11 / 调度与 LLM 8，每项标该组总数与已覆盖数。之所以不做平铺 tab：按 `paper_` 前缀天然会变成「一组 50 项、另一组 1 项」。布局由 `param_specs._LAYOUT` 按业务语义声明（分组 → 组内小节 → 键），新增字段没收录时按前缀兜底进对应组的「其他」小节
- **组内小节**：每个分组再切成 2~5 个语义小节（如「风控与止损」= 买点切入 / 固定止损止盈 / ATR 与移动保护），小节标题右侧带一句说明；搜索时跨全部分组按小节分段展示命中结果
- **可编辑**：开关项点一下、数字项直接输，改完点顶栏的「保存 (N)」（改动后页面底部也会浮出「有 N 项未保存 · 保存并生效 / 放弃」条，往下翻几屏也能保存）；每行是「固定列宽（104px）右对齐的短标签 + 紧挨着的控件 + 靠右的来源胶囊」，标签与输入框之间不再撑出一大段空白；完整 key / 说明 / 当前值都在行 tooltip 里，**来源**（`运行时` = app_state 覆盖 > `.env` > `默认`）以胶囊标记，改动行高亮，可单行撤销、整组「本组还原」或全局「还原」为 `.env` 值
- **短标签 + tooltip**：卡片里只放标准短名（`固定止损`、`买单队列上限`、`ATR 止损倍数`，见 `param_specs._SHORT_LABELS`，最长 10 字），`config.py` 里那整句注释作为 `desc` 放进行悬停提示，连同完整 key、当前值、待保存值一起显示；有跨页补充说明的行在标签右侧带一个黄色提示点
- **写模型**：改 `app_state` 覆盖层 + 就地改 `Settings` 单例，**下一个 tick / 下一轮扫描即生效**，重启由启动流程重放，不会丢
- **只读项**：路径与列表类（`kline_dir`、`db_path`、`cors_origins` 等）在进程启动时就被静态挂载 / CORS / 缓存目录捕获，`POST /api/settings` 直接拒绝（400），`GET /api/settings` 也不返回；要改请改 `.env` 后重启。`app_name`、`debug` 同理不进清单
- **还原（二次确认）**：顶栏「还原」= 还原**全部**可编辑项，组头「本组还原」= 只还原本组；
  两者都弹确认框，写明「N 项 + 目标值来自本机 `.env`（无 `.env` 时等于代码默认）+ 不可撤销」。
  早先「有未保存改动就只还原那几项、否则还原全部」的隐式分支已去掉 —— 同一个按钮两种行为容易误点
- **敏感项脱敏**：`llm_api_key`、`notify_webhook_url` 在接口里只返回掩码（`sk-****abcd`），行尾带「密钥」标记；
  前端把掩码原样回传时后端视为「未修改」，输入新值才覆盖，传空串才是清空
- **审计**：每次保存/还原追加一行 JSON 到 `data/logs/settings_audit_YYYYMMDD.jsonl`
  （`ts / action / keys / values（密钥只留掩码）/ ip / ua`）—— 多环境部署后「谁还原了什么」只能靠它复盘
- 支持按标签 / key / 值 / 说明搜索（有筛选条件时跨全部分组，左栏同步显示每组命中数），筛「只看已覆盖」；接口 `GET /api/settings`、`POST /api/settings`、`POST /api/settings/reset`

## 回测参数可调与做T引擎（心得策略）

回测中心（`/backtest`）已支持把 `PipelineParams` 的全套风控参数在 UI 上逐项调整（共 9 组，见下一段的分组说明）。

参数框里显示的是**具体默认值**（后端 `GET /api/backtest/defaults` 统一算出线上配置，前端不再显示「默认」占位），并且按改动方向着色：**比默认大 = 红、比默认小 = 绿、等于默认 = 常态色**；鼠标悬停可看默认值。「关闭/不限」类参数默认就是 `0`，提交时值为 `0` 即略过该字段（等价于 `None`）。顶部有「重置」一键回到默认。

参数面板按 9 个 tab 分组（基础 / 选股买入 / 五段式切入 / 止损止盈 / 移动保本 / 大盘择时 / 仓位评分轮动 / 做T），每个参数一张卡片（标签一行、输入框占满一行），不再把标签和 60px 输入框挤在同一行。

**打分层（2026-10-31 新增，`BACKTEST_LOG` #79）**：「选股/买入」组顶部有 `打分层` 下拉——`14因子` / `ML分位` / `ML+因子融合`，**默认跟随实盘开关**（`live_ml_scoring` 开则为 ML），避免「回测跑 ML、实盘还在 14 因子」的口径漂移。同组可调 `ML模型`（LightGBM，缺依赖自动回退 Ridge）、`ML标签前瞻`、`融合权重`、`ML行业中性化`。**结果里的 `ml_diagnostics` 必须看**：`effective=false` 表示 OOS rank-IC ≤ 0 已**整段回退 14 因子**，这次跑的其实不是 ML（与基线数字完全相同）。短区间（十几个交易日）只有十几个截面、IC 估不出来，几乎必然回退——要看 ML 请跑 **≥ 半年**，或直接用 `scripts/random_dist.py` 那套 18 段 × 21 天（`#76`）。落盘时只有 `打分层` 能同步到实盘 `live_ml_scoring`，模型/前瞻/融合比例/中性化都是回测专属。**股票池默认 300**（跟随线上 `paper_pool_top_n`；2026-10-31 由写死的 500 改回），与 `#76` 验收口径一致——填 500 跑出来的是「Top500 里的 ML」，与线上和 `#76` 都不是同一实验。

**两套参数必须分清**（面板顶部有常驻说明）：
- 改参数 + 「开始回测」——只影响**本次回测**，不碰实盘；
- 「将规则落盘保存到数据库」——把这套规则写进配置库（`app_state` 覆盖层），立即对**选股 + 模拟盘交易**生效，重启保留，并成为新的默认值。只同步实盘有落点的子集（止损/止盈/单票上限/仓位/买入阈值/评分档/择时/买入切入点/做T 等，共 30 项），其余（板块组合、调仓周期、分位门槛、反转选股、分段止盈、时间止损、分钟级切入点）逐条列出「未同步」原因。落盘前有确认框展示「会变更哪几项：旧值 → 新值」。接口 `GET /api/backtest/persist-map`、`POST /api/backtest/persist`；映射表见 `app/backtest/apply.py`。

并内置三组「一键预设」：

- **创业板超卖半仓（心得）**：只创业板 + 半仓 + 超卖不追高 + 大盘择时 + 2×ATR 保本移动。2025~2026 回测 **-4.22%**，同期买入持有基准 **-59.97%**（跑赢约 55 个点），最大回撤 **-13.6%**。（完整条件、run id 与复跑方式见 `BACKTEST_LOG` 记录 #48；单窗口结论，未走验收协议，仅供存档）
- **创业板超卖·狠版（微调）**：更狠超卖 + 分段止盈，回测 **-13.54%**（反更差——过度超卖会接飞刀、过早止盈削弱利润奔跑，故原始版更优）。

### 做T引擎（参数化主动交易）

对应「次日开盘→收盘做T / 分级网格」的实战心得，字段均以 `t_` 前缀暴露，`t_active=None` 时完全关闭、沿用被动持有：

| 参数 | 含义 | 心得映射 |
| --- | --- | --- |
| `t_active` | 总开关 | 启用做T |
| `t_size` | 单次做T比例（相对总仓） | 1/4 仓 |
| `t_up_sell` | 开盘涨幅超此先卖锁利 | 涨超卖 1/4 |
| `t_up_buyback` | 从高点回调超此买回 | 回调 >3% 买回 |
| `t_down_add` | 开盘跌幅达此加 1/4 仓 | 跌 5% 加仓 |
| `t_down_full` | 跌幅达此加满仓 | 跌 7% 满仓 |
| `t_rebound_sell` | 反弹至跌幅小于此减仓 | 反弹 <2% 减仓 |
| `t_limit_sell_half` | 涨停弱封板卖 1/2 | 涨停开板前卖半 |
| `t_crash_guard` | 大盘当日跌幅超此暂停 | 大盘极速下跌不操作 |

**A 股 T+1 约束**：做T 只动用**底仓**做「先卖后买」倒T（当日卖底仓可卖、买回次日可卖）；下跌加仓为隔夜行为（当日买、之后反弹时卖）。

**回测口径（2026-10-30 起）**：勾选 `t_minute_path`（`PipelineParams.t_minute_path=True`）后，做T 按**当日 5 分钟序列逐点推进**，
与线上 `t_tick` 同序同条件（含「当日回落到买回线就买回」），无本地分钟数据的票当天不做T。
不勾时是旧的日K四价近似（买回/减仓按次日开盘、倒T 以阈值价成交），**结论不可引用**——
同参数下它在涨市窗口给出 −14.31% 超额，分钟版是 +2.11%。

**采样周期**：`t_minute_period` 可选 `"5"`（默认）或 `"1"`。1 分钟线每天 240 个价格点，用于检验结论对采样精度的敏感度。

**成交价口径**：`exec_fill` 可选 `"open"`（默认）或 `"vwap1m"`（开盘后 `exec_fill_minutes` 分钟的 1m 成交量加权均价）。
默认按开盘价成交——`BACKTEST_LOG` 记录 #67 实测这**乐观了约 1.4~2.5pp / 21 天**：换成 1m VWAP（偏离仅 0.1%~0.3%）后，
18 段配对超额为 −2.48%（t −2.60）、胜段仅 3/18。所以本仓库所有「超额 +X pp」的绝对值都要打这个折扣，
绝对值低于 2.5pp 的结论不构成 alpha 证据。

**成交价扰动检验**：`fill_noise_bps`（默认 0）对每笔成交价加 `N(0, σ)` 双向扰动，用于判断「超额」是否只是
执行价的抽样噪声。实测（`BACKTEST_LOG` 记录 #68）：`default` 的中位超额 +4.12% 落在 ±10bp 扰动分布的**上沿**
（10 个种子：+0.66%~+4.10%，均值 +2.23%；±20bp 均值 +2.10%）。20 个样本**全部为正**，说明方向性 alpha 真实存在，
但**真实量级约 2.1pp / 21 天**。据此，任何改动的配对差必须 **>3.5pp** 才算超出度量噪声——过去所有参数优化
（+0.35~+1.46pp）都低于该门槛。

**回测结论（`BACKTEST_LOG` 记录 #65 / #66，做T 方向已关闭）**：18 段不重叠随机段配对，`t_low_min`
（低吸真的打开：跌 5% 加 1/4 现金、跌 7% 加满 + 5 分钟路径）零滑点下 **均值 +1.46%、t 2.16、胜段 66.7%、
最差回撤 −12.80%（基准 −18.09%）**——本策略研究里第一个 t>2 的改动；但：

| 口径 | 均值 | t | 胜段 | 结论 |
| --- | --- | --- | --- | --- |
| 5m 路径，零滑点 | +1.46% | 2.16 | 66.7% | 唯一的「通过」组合，双重乐观 |
| 1m 路径，零滑点 | +0.92% | 1.41 | 55.6% | 采样细 5 倍 → 显著性被削掉一半 |
| 1m 路径，0.3% 滑点 | −1.87% | −2.41 | 27.8% | 摩擦吃掉全部 |

滑点弹性约 **每 0.1% ≈ −0.93pp**（1m 口径下滑点 ≥0.10% 收益即归零）。故判定 **做T 的 alpha 小于其摩擦**，
线上 `paper_t_active` 保持 **False**，也不再开线上模拟盘去采滑点（1m 数据已能反推）。
回测能力（分钟路径 + 滑点档）保留，后续用于给「开盘价成交」建模更真实的执行价。

**上线期状态（2026-10-31 复核锁定）**：`app_state` 覆盖层里 `cfg:paper_t_active` 曾被置为 `1`（当日 09:07），
与本节及 `docs/sas_formulas.md` §4.9 的口径冲突，已通过「系统设置」接口改回 `false`（即时生效、落库、留审计记录）。
首周实盘**不做T**，理由：`paper_trades` 至今 0 笔，做T 在纸面从未跑出成交；做T 会把成交笔数抬到 2 倍以上，
而实盘滑点尚未实测，等于把一个「滑点 ≥0.10% 即归零」的策略挂在未知摩擦上，且与基础策略变量混在一处无法归因。
**重开条件**：基础策略先跑出 ≥30 笔成交、`slippage_audit` 给出实测滑点后，用该实测滑点重跑
`t_minute_path=True` 的配对实验，显著为正再开；在那之前该开关的默认状态是关。

## 市场情绪分级（5 档）

市场整体情绪量化为 5 档离散等级，每档有默认权重（-1 ~ +1），为选股信号与 LLM 分析提供「市场环境」上下文。

| 情绪等级 | 温度区间 | 权重 | 操作建议 |
| --- | --- | --- | --- |
| 极度亢奋 | ≥ 80 | **+1** | 赚钱效应极致，注意波动加剧，追高需谨慎 |
| 多头占优 | 60–80 | **+0.5** | 顺势环境，趋势突破类策略有效性较高 |
| 中性 | 40–60 | **0** | 结构性机会为主，严选个股、控制仓位 |
| 空头占优 | 20–40 | **-0.5** | 防守为主，降低盈利预期，回避弱势股 |
| 极度悲观 | < 20 | **-1** | 恐慌区域，可关注超跌反弹机会或观望 |

**映射依据**：市场温度计（0-100）= 上涨占比 × 60 + 涨停占比（封顶 5%）× 40，按等宽阈值 [80, 60, 40, 20] 划分 5 档。阈值与权重定义见 `backend/app/market/sentiment.py`（调整需同步本文档与 `tests/test_sentiment.py`）。

**权重的三层用途**：
1. **展示**：Dashboard「市场情绪」卡片显示等级徽标 + 权重 + 操作建议
2. **信号**：选股结果记录扫描开始时的情绪快照；情绪仅作为建议理由中的上下文，**不再否决买入**
   （弱市常伴更好赔率，「大落大机会」由因子模型里的超跌/乖离/KDJ 分项表达）
3. **LLM 上下文**：个股分析时注入当前情绪等级与权重，AI 结论结合市场环境（如亢奋期提示追高风险、悲观期提示逆环境信号）

## 智能盯盘体系

调度器驱动的全自动盯盘闭环（需后端保持运行，交易日生效）：

| 任务 | 时间 | 行为 |
| --- | --- | --- |
| 开盘分析 | 工作日 9:20 | 分析全部自选股，生成本日买卖建议，并读取昨日「明日关注」 |
| 盘中监控 | 9:35–15:00 每 5 分钟 | 重新分析自选股，实时更新「建议买入 / 建议卖出」列表 |
| 收盘选股 | 工作日 15:30 | 全市场扫描，命中信号全部持久化为**股票池**；再按技术面评分取 Top 10（`scan_auto_watch_limit`）加入自选股（一选→二选） |
| 盘后分析 | 工作日 15:35 | 全市场扫描 + 自选股分析，生成次日「明日关注」TopN |

**建议引擎**（`app/analysis/`）：
- 技术面共享评分（`scoring.py`，±8 分：均线 ±2 / MACD ±1.5 / KDJ ±1.5 / 量能 ±1 / 乖离 ±1）
- 分级：多因子总分 ≥ +15 → 买入建议；≤ -15 → 卖出建议；其余持有观察
- **市场情绪**：只写入建议理由作为环境上下文，不再一票否决买入（弱市常有更好赔率，
  超跌机会交由因子模型中的 KDJ/乖离等分项表达）
- 止损/目标价：10 日最低价 / 信号价 +15%

**相关配置**（`backend/.env`）：`AUTO_WATCH_ENABLED`（自动加自选开关）、
`AUTO_WATCH_TOP_N`（每日自动加自选数量，默认 10）、`INTRAADAY_ANALYSIS_INTERVAL`
（盘中分析间隔分钟）、`FOCUS_TOP_N`（明日关注数量）。

建议数据按批次持久化（`recommendations` 表：`intraday_/premarket_/focus_` + 日期），
买卖建议只展示当日批次，明日关注按生成日期对应次日开盘。

## 公网部署（cpolar 内网穿透）

面向个人使用的公网访问方案：前端构建为静态文件由 FastAPI 托管（单端口），再用 cpolar 穿透到公网。

### 1. 构建前端（代码更新后重新执行）

```bash
cd frontend && npm run build
```

`dist/` 产物由后端自动托管，无需 Nginx。

### 2. 启动后端（单端口提供页面 + API）

```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

此时 http://127.0.0.1:8000 即可访问完整系统（页面 + `/api` + `/docs`）。`dist/` 不存在时后端仍可作为纯 API 服务运行（开发模式不受影响）。

### 3. 配置 cpolar 隧道

**固定域名（推荐，需 cpolar 付费版）**：先在 [cpolar 后台](https://dashboard.cpolar.com) → 预留 → 创建 HTTP 隧道（如子域名 `mysas`，本地地址 `8000`），创建后会自动同步到本地配置。之后编辑 `C:\Users\<你的用户名>\.cpolar\cpolar.yml`，确认 `tunnels:` 下包含：

```yaml
tunnels:
  mysas:
    id: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx   # dashboard 创建时自动生成，勿改动
    subdomain: mysas
    proto: http
    addr: "8000"
    bind_tls: both
    region: cn_top
    start_type: enable    # 开机/服务重启后自动恢复
```

**随机域名（免费版）**：yml 中不写 id 与 subdomain，由 cpolar 每次分配：

```yaml
tunnels:
  sas:
    proto: http
    addr: "8000"
    bind_tls: both
    start_type: enable
```

### 4. 重启 cpolar 服务使配置生效

修改 yml 后 **service 不会热加载**，需重启（管理员权限）：

```powershell
Restart-Service cpolar   # 管理员 PowerShell
```

重启后 service 自动拉起 `start_type: enable` 的隧道，此时在 **cpolar Web UI（http://127.0.0.1:9200）与 dashboard 隧道列表中都能看到**。

### 5. 访问验证

固定域名直接访问 https://mysas.vip.cpolar.cn（http/https 双协议）；
随机域名在日志中查找（`cpolar start <隧道名> -log=stdout` 会输出
`Tunnel established at https://xxxx.r10.cpolar.top`）。

### 注意事项

- **CLI 进程 vs service 隧道**：`cpolar start <name>` 命令行启动的是独立进程，
  能连通但**不在 Web UI/dashboard 列表中显示**，进程结束即断。要持久化+列表可见，
  必须用 yml + service 方式（本文档方式）
- 安全：公网暴露期间任何人可访问 —— 必须按下一节配 `API_TOKEN`，否则等同于把设置接口开放给公网
- 数据源：服务器在本机，A股数据源直连不受穿透影响
- 调度任务：自动盯盘（盘中建议/收盘选股）依赖后端进程存活，关机或关闭后端后当天任务不执行

### 6. 访问令牌（公网部署必配）

`backend/.env` 里设 `API_TOKEN=<随机串>` 后，所有 `/api/*` 请求都要带 `X-API-Token`
（或 `Authorization: Bearer <token>`），`/api/health` 例外（部署脚本与监控探活用）。
**留空 = 不校验**，本机开发零负担；但经 cpolar/云主机公开的实例必须配 ——
否则任何拿到地址的人都能读配置、改交易开关、一键还原全部参数。

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"   # 生成一个
```

- 前端：任意请求收到 401 会弹出令牌输入框，令牌只存本机浏览器 `localStorage`，换电脑重新输入
- 部署脚本：先 `$env:SAS_API_TOKEN = '<token>'` 再跑 `deploy.ps1`，否则业务探活会显示 401
- 令牌列在 `READONLY_KEYS`，不会被 `GET /api/settings` 回显；改它需要改 `.env` 并重启

## 技术栈

- **后端**：Python 3.11+ / FastAPI / Pandas / akshare / SQLite + Parquet / APScheduler
- **前端**：Vue 3 + Vite + TypeScript + TailwindCSS + ECharts + Pinia
- **LLM**：OpenAI 兼容协议（DeepSeek / 通义 / 智谱 / OpenAI 等可配置），**未配置 API Key 时自动降级为技术面规则评分，其余功能完整可用**

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│ Vue3 前端（五段式流水线）  ①股票池→②自选→③队列→④交易池/持仓  │
└──────────────────────────┬──────────────────────────────────┘
                           │ REST (/api)
┌──────────────────────────▼──────────────────────────────────┐
│ FastAPI 后端                                                 │
│  market · screening · backtest · analysis · watchlist ·       │
│  monitor · strategies（策略管理）                             │
│  ┌──────────┐  ┌──────────┐  ┌────────┐  ┌──────┐            │
│  │ 扫描器    │  │ 回测引擎 │  │ LLM    │  │ 调度 │            │
│  │ asyncio  │  │ 向量化   │  │ 分析器 │  │  APS │            │
│  └────┬─────┘  └────┬─────┘  └───┬────┘  └──┬───┘            │
│  ┌────▼─────────────▼────────────▼─────────▼───┐            │
│  │ 指标引擎(自研) + 策略注册表(工厂+参数化)      │            │
│  └────────────────────┬────────────────────────┘            │
│  ┌────────────────────▼────────────────────────┐            │
│  │ 数据源抽象层（可插拔降级链）                  │            │
│  │  东方财富 →(不可达) 腾讯 →(风控501) 新浪     │            │
│  │  + 接口熔断器 + 域名级节流 + 失败重试         │            │
│  └────────────────────┬────────────────────────┘            │
│  ┌────────────────────▼────────────────────────┐            │
│  │ 存储：Parquet K线缓存（按代码增量）+ SQLite   │            │
│  │ （自选股/信号/回测/自定义策略）              │            │
│  └─────────────────────────────────────────────┘            │
└─────────────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 后端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/Mac

pip install -r requirements.txt

# 可选：复制 env 样例并按需修改（LLM / Webhook / 调度开关）
copy .env.example .env          # Windows
# cp .env.example .env          # Linux/Mac

# 启动（默认 8000 端口，启动时自动预热股票池、加载自定义策略并开启调度器）
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后访问交互式文档：http://127.0.0.1:8000/docs

### 2. 前端

```bash
cd frontend
npm install
npm run dev        # 开发服务器，默认 http://localhost:5173
# npm run build    # 生产构建
```

前端开发服务器已配置 `/api` 代理到 `http://127.0.0.1:8000`，无需额外配置。

### 3. 配置 LLM（可选）

编辑 `backend/.env`：

```ini
LLM_ENABLED=true
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxxxxxx
LLM_MODEL=deepseek-chat
```

任意 OpenAI 兼容服务均可（DeepSeek / 通义千问 / 智谱 / OpenAI / Gemini）。

### 4. 配置通知（可选）

```ini
NOTIFY_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
```

支持企业微信 / 飞书 / 钉钉机器人；未配置时仅控制台输出。

## 内置策略与策略编辑

| 策略 | 核心逻辑 |
| --- | --- |
| 海龟突破 | 收盘价创 N 日新高 + 成交额过阈 + 阳线（周期/阈值可调） |
| 均线放量 | MA5>MA10>MA20 多头排列 + 放量突破 + 站上 5 日线 |
| MACD金叉 | DIF 上穿 DEA 且 MACD 柱 > 0（可过滤零轴位置） |
| KDJ超卖金叉 | J < 阈值 超卖区 + K 上穿 D（阈值可调） |
| 平台突破 | N 日窄幅横盘 + 放量突破平台上沿 |
| 动量突破 | N 日涨幅 ≥ 阈值 + 贴近新高（欧奈尔 RPS 思想） |

**策略可编辑**：在「选股中心」点击策略卡片的 ✏️ 按钮打开参数编辑器
（数字输入+滑块 / 开关 / 下拉三类控件），调整后可**另存为自定义策略**。
自定义策略持久化在 SQLite（`custom_strategies` 表），重启自动加载，
可像内置策略一样用于选股扫描与回测，也可随时删除；
内置策略不可修改、不可删除。

新增内置策略只需继承 `Strategy` 实现 `evaluate` 并声明 `parameters`
（见 `app/strategies/builtin.py`）。

## 数据源说明（重要）

本项目面向**免费数据源**，针对国内网络环境实现了多级降级链：

```
K线：东方财富(akshare) → 雪球(0.07s，真实成交额) → 腾讯(proxy.finance.qq.com → ifzq.gtimg.cn) → 新浪
股票池：东财 push2 列表 → 新浪列表(分页全量) → akshare → 本地快照代码表（上交所官方数据校验沪市）
快照：东方财富 → 新浪批量实时行情
```

- **连接池复用**：所有备用源共用一个 `requests.Session`（32 连接），并关闭系统代理
  （`trust_env=False`）。实测新浪首连需 7-8 秒 TLS 握手、复用连接仅 0.05 秒，
  不复用会让全市场扫描慢一个数量级
- **熔断器**：某接口每次尝试失败即计数，连续失败 3 次后熔断；冷却时间随失败次数
  指数增长（5 分钟 → 最长 30 分钟），避免数据源长期不可用时每轮扫描都重踩超时
- **域名级节流**：同一域名请求间隔 ≥ 0.15s，降低触发风控（返回 501 JS 挑战页）的概率
- **腾讯多主机**：`web.ifzq.gtimg.cn/appstock/app/fqkline/get` 在部分网络返回 501，
  已改用 `proxy.finance.qq.com/ifzqgtimg/...`（同格式 qfqday），并以
  `ifzq.gtimg.cn/appstock/app/kline/kline` 作为第二备选
- **雪球源**：需先访问 `https://xueqiu.com/hq` 取 `xq_a_token`（只访问首页只会下发风控
  cookie），之后 `stock.xueqiu.com/v5/stock/chart/kline.json` 可复用连接（约 0.07s/只），
  且返回**真实成交额**；token 失效（400/401）时自动重新握手并重试一次
- **股票池缓存**：主源结果缓存 1 小时；降级来源（快照代码表覆盖可能不全）只缓存 2 分钟，
  以便尽快重试主源
- **沪市校验**：股票池取到后用上交所官方列表（主板 `stockType=1` + 科创板 `stockType=8`，
  GBK 编码）核对沪市覆盖，缺失的沪市代码自动补齐
- **新浪兜底**：该接口历史悠久、风控宽松，是被限流后的最终保障
- **快照 SWR 策略**：实时快照全量刷新一次约 90 秒（59 批顺序请求），采用
  stale-while-revalidate：TTL 内直接返回新鲜数据；TTL 外立即返回旧数据并后台单线程刷新
  （页面永不等待）；冷启动（无缓存）才同步刷新，接口层另有 15s/20s 超时降级
  （自选股列表退化为静态列表、概览返回 503）；刷新失败进入 60 秒冷却，防止数据源
  挂掉时每个请求重复走降级链。TTL 由 `snapshot_ttl` 配置（默认 30s），
  调度器每分钟主动刷新保持盘中数据新鲜

实测注意：
- K线成交量已统一换算为「手」
- 成交额：东方财富与雪球源为**真实成交额**；腾讯/新浪源不返回成交额，
  按 `成交量(手)×100×(最高+最低+收盘)/3` 估算，仅用于指标筛选
- 交易日判断不含法定节假日（个人使用场景可接受）

### 6. 注册为 Windows 服务（开机自启 + 崩溃自动重启）

生产环境建议把后端注册为系统服务，实现开机自启与进程崩溃自动重启：

```powershell
# 管理员 PowerShell（或右键 install_service.ps1 → 使用 PowerShell 运行）
powershell -ExecutionPolicy Bypass -File e:\Study\SAS\install_service.ps1
```

脚本自动完成：
- 通过 NSSM 注册 `SAS` 服务（uvicorn 生产模式，托管前端 dist，单端口 8000）
- 启动类型 = 自动（开机自启）
- 进程退出自动重启（`AppExit Default Restart` + 5 秒节流防崩溃循环）
- 日志输出到 `data/logs/`（10MB 自动轮转）
- 同时给 cpolar 服务配置失败恢复策略（1/2/5 分钟三级重启）

常用运维命令：

```powershell
Restart-Service SAS                    # 重启
Get-Content e:\Study\SAS\backend\logs\backend.log -Tail 50    # 看应用日志（推荐）
nssm remove SAS confirm                # 卸载服务
```

**日志有两层，看业务问题只认后者**：
- nssm 的 stdout/stderr（`data\logs\`）——只兜异常栈，进程被隐藏窗口启动时经常是空的；
- **应用日志 `backend\logs\backend.log`**（`app/main.py` 的 `RotatingFileHandler`，10MB×5 轮转，
  `uvicorn/uvicorn.error/uvicorn.access` 一并收进来）。「评分 xx<25，仅记录不下单」「大盘弱势，
  总仓位压到 30%」「live ML 无信号，回退 14 因子」这类**决定性判定只写在这里**——
  2026-10-31 复盘「为什么零成交」时就是缺这一层，只能靠重跑脚本反推。

**访问令牌（2026-10-31 起启用）**

站点经 cpolar 隧道公网可达，而 `POST /api/settings` 能改止损/交易开关、`/reset` 能还原全部参数、
`/api/paper/*` 能下单——没有令牌时这些接口**任何人可调**。现在 `backend\.env` 里配了 `API_TOKEN`：

- 所有 `/api/*` 需带 `X-API-Token` 头，**只有 `/api/health` 豁免**（部署脚本/监控探活不受影响）；
- 前端自动携带（`api/index.ts` 请求拦截器），收到 **401** 会弹「需要访问令牌」输入框，
  令牌存本机 `localStorage`（换设备/换浏览器要重输一次）；
- 令牌只存在 `backend\.env`（被 `.gitignore`，且任何接口都不回显）。**忘了就回文件里看那一行**，
  改完重启 `SAS` 服务生效；
- 手工调接口要带 header：
  `Invoke-RestMethod -Headers @{'X-API-Token'='<令牌>'} http://127.0.0.1:8000/api/settings`

**数据库备份（每日 15:50，计划任务 `SASDbBackup`）**

```powershell
cd e:\Study\SAS\backend
.\.venv\Scripts\python.exe scripts\backup_db.py            # 手动备份一次
.\.venv\Scripts\python.exe scripts\backup_db.py --keep 60  # 改保留份数（默认 30）
schtasks /run /tn SASDbBackup                              # 立即触发计划任务
```

- 用 SQLite **在线 backup API**（页级一致快照），**不是 `Copy-Item`**：库是 WAL 模式且服务一直在写，
  直接复制文件可能拿到「主库已改、`-wal` 未合并」的半截状态，恢复时缺最近若干笔成交；
- 落 `D:\Backup\SAS\backups\sas-YYYYMMDD-HHMMSS.db`，保留最近 **30** 份（每天一份 ≈ 一个月），
  只清理自己产出的 `sas-*.db`，不碰目录里别的文件；
- 为什么必须有：账本（`paper_account` / `paper_trades` / `paper_equity`）是唯一的实盘记录，
  丢了就等于 `scripts/live_review.py` 的对账永久断档。

**模式区分**：
- **生产模式**（推荐，公网访问用）：只跑 `SAS` 服务一个进程，访问 http://127.0.0.1:8000
- **开发模式**（改代码调试用）：`python start_dev.py`（前端 Vite HMR + 后端 `--reload` 自动重启），二者不要同时使用（8000 端口冲突）

### 8. 扫描加速（默认范围 + K线预热）

网页选股扫描慢的主要瓶颈不是"没存历史数据"（K线已增量缓存到 `data/parquet/`），而是**默认全市场 5000 只** + **首次扫描时 K线尚未缓存**导致的实时网络拉取。两项优化：

- **默认扫描范围收窄**：选股中心默认 `成交额前 500（推荐）`，下拉可选全市场 / 成交额前 500/1000 / 各板块；后端 `top_amount:N` 按实时成交额取流动性最好的 N 只，冷缓存下最快出结果
- **K线预热**：
  - 开机后台低并发（默认 10）**只补落后的股票**：扫本地 Parquet 的最后日期，仅对落后于最新交易日的代码回源（停牌/退市超 30 天的自动跳过）
  - 每个交易日 16:00 调度全市场日K增量刷新，保持缓存新鲜
  - 同一轮预热会顺带刷新上证指数日线（`idx_sh000001`）：`regime_ma` / `regime_tiers` 只认这条序列，指数不跟着日更，最近几天就会静默退化成默认档；预热结果里的 `index` 字段（`ok/last/bars`）可在 `/api/screening/kline-status` 看到
  - 选股中心「预热K线」按钮可手动触发全市场预热（后台执行，不阻塞）
  - 预热完成后扫描直接读本地缓存，消除实时网络拉取与数据源限流等待
- 配置项：`kline_warmup_concurrency`（预热并发）、`kline_warmup_top_n`（16:00 收盘预热只数，**默认 0=全市场**；只热前 N 只会让其余股票长期停在旧日期）
- 手动补刷历史遗漏：`python backend/scripts/backfill_klines.py`（`--dry-run` 只看清单，`--include-dead` 连停牌股一起补）
- 预热进度可见：选股中心操作栏显示「本地K线 已缓存/总数 (百分比)」，点「预热K线」后实时轮询刷新；后端接口 `GET /api/screening/kline-status`

### 8b. 影子账户（冻结参数的前向验证）

参数在历史数据上怎么调都容易过拟合（见 `BACKTEST_LOG` 记录 #15/#34），唯一能判定"候选参数是真信号还是噪声"的办法是**向前看**：固定一个起点，每天用当天收盘后的数据重跑一遍，逐日累积超额。

- 启用：`backend/.env` 里设 `SHADOW_START=2026-11-02`（留空 = 不启用），可选 `SHADOW_TAG`（默认 `live`）、`SHADOW_UNIVERSE`、`SHADOW_PROFILES`（默认四方案 `default,e30_a15,e32_b,rev_inv`）
- 调度：每交易日 **16:45** 自动跑「起点 ~ 今天」（排在 16:00 K线预热之后，确保当日 bar 已补齐），日内重跑幂等
- 结果：`data/logs/shadow_<tag>.json`（按 `as_of` 追加快照），盘中不下单、不读写 `paper_*` 表、不改配置
- 查看：`.venv\Scripts\python.exe scripts\shadow.py --start 2026-11-02 --tag live --report`
- 注意：**起点写入后即冻结**，中途改起点会让曲线断裂、超额不可比（要换起点请换一个 `--tag`）

### 7. 模拟交易（验证选股思路）

用模拟盘把「分析建议」变成可复盘的账本，验证选股思路的准确性。

- **初始资金 10 万元**，系统按智能建议 **自动执行买卖与仓位管理**
- **仓位规则**：等分资金，最多持有 10 只（`paper_max_positions`），单票不超过总资产 20%（`paper_single_position_max`）；买入向下取整到 100 股（整手）
- **交易规则（对齐 A 股）**：T+1（当日买入次日可卖）、佣金万 2.5（最低 5 元）+ 卖出印花税千 1 + 双边过户费万 0.1；四项费率都在设置里（`trade_fee_commission_rate` / `_commission_min` / `_stamp_rate` / `_transfer_rate`），**回测与实盘共用同一份**，佣金谈下来只改一处
- **成交价**：卖出 / 止盈止损 = 执行时刻最新价（快照优先，失败退化 K 线收盘价）；**买入只在开盘后
  `paper_buy_open_window_minutes`（默认 5）分钟内成交**（与回测「次日开盘价」口径对齐），窗口外的买单留在
  队列等下一个开盘，有效期顺延。2026-10-30 用 `intraday_entry` 回测验证过「放开到全时段」：
  配对中位 −1.18pp、正占比 33%，**不采纳**（`BACKTEST_LOG` 记录 #58）
- **止盈止损**：默认止损 -8%（`paper_stop_loss`）/ 止盈 +20%（`paper_take_profit`），每 tick（默认 5 秒）用最新价检查；收盘 15:40 结算并记净值。三项风控参数可在设置页改，也可由回测中心「落盘」写入
- **自动执行链路**：开盘 9:20、盘中每 `intraday_analysis_interval`（默认 1 分钟）分析产生的买卖建议自动送入模拟盘队列，由 `paper_tick`（默认 5 秒）撮合——**卖出与止盈止损全天生效，买入受上面的开盘窗口约束**；盘中信号若在窗口外产生，顺延到下一个交易日的开盘窗口；盘后 15:40 结算并记录净值。注：2026-10-30 起「大盘择时弱市闸门」（`paper_regime_weak_pause`，指数在 MA60 下方）会让**全部自动买入**返回 skip，这是当天零成交的直接原因（同日弱市日志 1999 条）——注意那时的
  `paper_weak_exposure` 还是 0（0 = 全暂停）；**10-30 22:17 已改为 0.3，此后弱市只降仓、不再全停**。该开关已在
  「系统设置 → 做T 与择时」支持**运行时切换（改完立即生效，不用重启）**，并会在模拟交易页顶部显示
  「大盘弱势 · 今日已拦截 N 次」；`GET /api/paper/live` 的 `regime` 字段可直接读到判定与计数。
  旁边的「弱市保留仓位」（`paper_weak_exposure`，默认 0）可把它从「全暂停」改成「弱市只保留 X% 总仓位」，
  填 0.3 即弱市留三成仓继续参与。**本机当前已设为 0.3**（`BACKTEST_LOG` 记录 #63）：这是风险偏好取舍，
  不是收益优化——随机段实测月期望约 −0.55pp，换来**日胜率 42.9% → 60%、急跌月少亏约 6.4pp**（尾月差异不大）
- **机制层三开关（默认全关，`BACKTEST_LOG` #84 / #85）**：设置页新增 13 项，全部在 `RUNTIME_KEYS` 里
  ——**改完立即生效、不用重启**；关掉任一项，行为与改造前完全一致。
  - `paper_dynamic_exposure` + `paper_regime_cap_{calm,weak,shock,crisis}`（「做T 与择时 → 大盘择时」）：
    用「20/60 日波动率比 + 120 日最大回撤 + MA60 趋势」把市场分成 **calm / weak / shock / crisis**，
    动态压低总仓位上限（只降不升）。crisis 档同时全暂停买入。
  - `paper_policy_floor_enabled` + `min_score / low_escalate / harvest_cap`（「风控与止损」）：
    把「国家不会让股市崩、有个看不见的底」量化成 0~1 的托底强度，让两种下行**分治**——
    托底证据强的阴跌 → `managed_downtrend`（缩仓 30% 且允许买入，收割反转）；
    托底证据极弱 + 高波动 → 升级 `crisis`（自由落体型，撤退）。
    **诚实降级**：ETF 放量 / 期指升贴水 / 官媒情绪这三类直接信号当前部署都没有，
    只用「指数底部特征代理」，所以强度分是**弱确认**而不是硬信号（`available=False`）。
  - `paper_circuit_breaker_enabled` + `dd / release / target`（「风控与止损」）：
    组合回撤 ≥8% 触发，总仓位压到 target 且全暂停买入；回落到 ≤4% 才解除（滞回，避免临界抖动）。
    峰值与触发态落 `app_state.paper_cb_state`，**跨重启保留**。
  - **优先级**：熔断 > 动态机制 > 静态弱市闸门；三者都只压上限，不会加杠杆。
  - 观测：`GET /api/paper/live` 的 `regime` 字段已透出 `dynamic / regime_state / regime_cap /
    regime_metrics / circuit_breaker / floor`。**前端暂未消费这些字段**（页面只显示原有的
    弱市判断与拦截计数），当前只能从接口或 `backend\logs\backend.log` 里看当日处于哪一态；
    要做可视化需另改前端。
  - 建议：默认全关期间先让 14 因子基线跑满 21 个交易日（`#70 §6` 的判读纪律），
    再逐项单开对照——三项同时开会互相掩盖，也说不清是谁的贡献。
- **交易队列容量**：待执行**买单**最多同时挂 5 只（`paper_trade_queue_top_m`，按 14 因子总分择优），超出不入队并在「④ 交易池 · 持仓」页的交易池 tab 标注「未入队」原因；卖出不受限
- **大盘股过滤**（「③ 三选 · 交易队列」页顶部开关）：**默认开启**，**总市值 > 100 亿**（可改阈值）的票不进队列，银行/石油这类巨无霸也不会出现在候选池里；关掉开关才放开（此时旧的 `GIANT_CODES` 黑名单同时失效）。设置写进 `app_state`，重启保留。回测侧无法建模（`klines` 没有市值字段），对照时需注意实盘可选集合比回测窄
- **切入点**（进队≠成交）：现价高于信号价 1.5% 不追（`paper_entry_max_chase`）、可要求回踩（`paper_entry_pullback`，默认 0=不限）、信号超时放弃（`paper_entry_timeout_minutes`，0=沿用 30 分钟 TTL）；被拒原因同样在「④ 交易池 · 持仓」页的交易池 tab 展示。**注意**：这组规则经回测无稳定增益（BACKTEST_LOG #52），属于防追高的工程保护，不是收益来源
- **关闭自动执行**：环境变量 `PAPER_AUTO_TRADE=false`（仅记录信号不成交）
- **周度复盘**：`cd backend && .venv\Scripts\python.exe scripts\weekly_review.py [--days 14] [--md]` —— 只读汇总区间内的净值/收益/最大回撤、成交笔数与费用、每日弱市拦截次数，最后给一句诊断（**零成交时区分「被弱市闸门拦住」和「本来就没信号」**，不用再翻日志）。数据来自 `paper_equity` / `paper_trades` / `app_state` 的 `paper_regime_paused:日期` 键（引擎每 60 次 tick 落库一次，重启不丢；2026-10-30 之前的日子没有该键）；按日表另有两列「收盘仓位」「弱势」，来自收盘结算写入的 `paper_regime_state:日期`（见下条）
- **涨跌家数广度（观测用，不参与交易）**：`cd backend && .venv\Scripts\python.exe -m app.data.breadth [--since 2024-01-01]` —— 遍历本地 5734 只**前复权**日线回算每日 `up/down/flat/up_ratio`，落 `data\breadth_daily.parquet`（全市场约 **10 秒**），每交易日 16:00 K线预热后自动刷新一次。用途是复盘时回答「那天是普跌还是结构性下跌」。**不进任何交易判定**：曾想用它区分 shock / 自由落体，但 4 年非重叠样本只有 5 个、方向还不稳定，见 `BACKTEST_LOG` #86
- **实盘对账**：`cd backend && .venv\Scripts\python.exe scripts\live_review.py [--since 2026-11-02] [--until ...] [--md]` —— 把账本净值换算成**相对等权基准的超额**。此前 `weekly_review` 只有绝对收益，回答不了「到底有没有跑赢基准」。基准口径与回测一致：**前一交易日成交额 TopN 等权，持有到当日收盘**（滞后一天，防未来函数），同时输出两套：**Top500**（回测 `universe_size` 默认）与 **Top300**（线上 `paper_pool_top_n`）。判读按 `BACKTEST_LOG` #70 §6 冻结：**−1pp ~ +5pp** 视为与回测一致、低于 −1pp 先查执行价与成交可行性、高于 +5pp 视为短期运气，**不足 21 个交易日只报方向不下结论**。依赖每个交易日收盘写入的 `paper_regime_state:日期`（`PaperEngine.settle()` 落 `app_state`：指数前收盘 / MA / 是否弱势 / `weak_exposure` / 当日拦截次数 / 总资产与仓位），因此**弱市但当天没有买入信号**的日子也有痕迹，不再与「不是弱市」混在一起

页面：导航只有一项「④ 交易池 · 持仓」（`/paper`），页内两个 tab —— **交易池**：交易模式 / 撮合状态 / 待执行信号 / 候补队列 / 队列容量 / 切入点判定；**持仓与成交**：账户收益率 / 资金分布 / 净值曲线 / 当前持仓 / 交易流水 / 手动下单。tab 状态写在 URL（`?tab=pool|account`），旧链接 `/paper-pool` 自动跳转到交易池 tab；`POST /api/paper/reset` 重置账户。

## 代码更新流程（重要）

改动代码后**不会自动生效**，按改动类型执行：

| 改动 | 需要的动作 |
| --- | --- |
| 前端 `.vue` / `.ts` | 重新构建：`cd frontend && npm run build` |
| 后端 `.py` / 策略 / 配置 | 重启服务：`Restart-Service SAS` |
| 两者都改 | 一起做 |

**一键部署**（管理员，自动完成 构建前端 → 重启服务 → 验证本地+公网）：

```powershell
powershell -ExecutionPolicy Bypass -File e:\Study\SAS\deploy.ps1
```

**推送到 GitHub 不会自动发生**，需手动：

```bash
git add -A && git commit -m "..." && git push
```

**开发调试期**（不想反复构建/重启）：用 `python start_dev.py`——前端保存即热更新，后端保存自动重启（`--reload`）。调试完成后再跑 `deploy.ps1` 发布到生产。

## 测试

```bash
cd backend
.venv\Scripts\python.exe -m pytest tests/ -q
```

265 个单元测试，覆盖：指标公式正确性、各策略命中/不命中场景、策略参数注入与
自定义策略全链路（注册/持久化/启动加载）、回测止盈/止损/绩效指标、市场情绪
分级（阈值边界/权重/快照计算/逆环境降级）、交易时段判断、异动检测与去重、
通知管道、快照存储（TTL/SWR/并发去重/失败冷却）、智能建议（评分分级/情绪降级/批次持久化）。

## 目录结构

```
SAS/
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI 入口、lifespan（预热+自定义策略加载+调度器）
│   │   ├── config.py          # Pydantic 配置（.env 可覆盖）
│   │   ├── api/               # market/screening/backtest/analysis/watchlist/monitor/strategies
│   │   ├── data/              # 数据源抽象、akshare 实现、降级链、缓存
│   │   ├── indicators/        # 自研技术指标（含公式注释）
│   │   ├── strategies/        # 基类(参数化)/注册表/内置策略/自定义策略加载
│   │   ├── market/            # 市场情绪分级（5 档 + 权重）
│   │   ├── analysis/          # 技术面评分 + 智能建议引擎
│   │   ├── screener/          # 全市场并发扫描器（含扫描范围）
│   │   ├── backtest/          # 向量化回测引擎
│   │   ├── llm/               # OpenAI 兼容客户端 + 决策仪表盘
│   │   ├── monitor/           # APScheduler 调度器
│   │   └── notify/            # 通知管道（控制台/Webhook/邮件预留）
│   ├── tests/                 # 265 个单元测试
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── views/             # Dashboard/Screening/Signals/Advisory/PaperTrading/Backtest/StockDetail
│   │   ├── components/        # KLineChart/MetricCard/ConclusionBadge/StrategyPanel/StrategyEditor/ScanResultTable
│   │   ├── api/               # 接口封装
│   │   ├── layouts/           # 侧边导航 + 顶栏布局
│   │   ├── stores/            # Pinia 全局状态
│   │   └── router/
│   └── package.json
├── start_dev.py               # 一键启动前后端（含端口清理与健康检查）
└── data/                      # 运行时生成：klines/*.parquet + sas.db（不入库）
```

## 界面与视觉规范

配色/字体/组件样式都走令牌：**改主题只改 `style.css` 里的变量块**。

- **主题（四套，设置页下拉切换）**：`light` 象牙白（默认）/ `dark` 石墨黑 / `midnight` 深海蓝 / `sand` 暖沙。实现方式是 `src/style.css` 里按 `[data-theme=...]` 定义 `--c-*`（RGB 三元组），`tailwind.config.js` 把颜色接成 `rgb(var(--c-*) / <alpha-value>)` —— 所以 `bg-base-800`、`text-content-muted` 这些语义类**换主题自动跟着变**，模板里不出现色值字面量。切换入口是「系统设置」顶栏的下拉框，选择存 `localStorage`（显示偏好，不进后端配置）；`:root` 就是浅色（默认），`index.html` 里有一小段内联脚本在首帧前把 `data-theme` 补上，避免先闪默认色再切。
- **色板**：三级表面 `base-900`（页面底）→ `base-800`（卡片）→ `base-700`（悬浮/输入）→ `base-600`（边框），文字三阶 `content / muted / faint`，强调色只有 `primary` 一支；红涨绿跌 `up`/`down` 只用于数据本身。层次靠明度与 1px 边框，不叠加光晕。浅色主题下 `primary-light` 用作强调文字，所以取的是比主色更深的蓝。
- **字体**（`index.html` + `fontFamily`）：Inter（拉丁）+ Noto Sans SC（中文）+ JetBrains Mono（数字，`font-num` 走 `tabular-nums`，行情跳动时列宽不抖）。字号阶梯：`display 28` / `heading 22` / `subheading 16` / `body 14` / `label 13` / `caption 12`；模板里其余字号（`text-[13.5px]` 这类）2026-10 已整体上调约 12%，避免满屏 11px 小字。
- **组件类**（`src/style.css`）：`.glass-card`（卡片，hover 只亮边框，不再整页上浮）、`.glass-card-interactive`（确实可点的卡片才位移）、`.panel/.panel-hd`、`.btn` + `.btn-primary/-outline/-ghost/-danger/-primary-soft/-sm/-xs`、`.chip*`、`.field`、`.seg/.seg-item`（页内 tab）、`.table-shell/.table-head/.table-cell`、`.eyebrow`。
- **图表**（`src/lib/chartTheme.ts`）：ECharts 吃不到 CSS 类，所以 `chartTheme()` 运行时读同一批 `--c-*` 变量并按主题重算；调用方写在 `computed` 里即自动跟随主题。新增图表不要写 `#111A2E` 这类字面色。
- **Toast**：主题底色 + 语义色描边（旧版是浅色 alert 块，在深色界面里很刺眼）。

视觉取向参考 shadcn/ui 的 dark 中性灰阶与 Tremor / Vercel Dashboard 的表面分层，只借鉴色阶与排版思路，未引入其组件依赖。

## 设计参考

本项目架构借鉴了以下成熟开源项目（均为 MIT 协议）：

- [Sequoia-X](https://github.com/silverhornz/Sequoia-X) — 收盘后自动选股流水线、策略工厂模式
- [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) — LLM 决策仪表盘结构化设计
- [tick-stock-panel](https://github.com/shy3130/tick-stock-panel) — 数据源可插拔能力路由
- [akshare](https://github.com/akfamily/akshare) — 免费 A 股数据源

---

**再次提醒：本系统仅供学习研究，不构成投资建议。**
