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

## 回测参数可调与做T引擎（心得策略）

回测中心（`/backtest`）已支持把 `PipelineParams` 的全套风控参数在 UI 上逐项调整（基础 / 选股买入 / 止损止盈 / 移动保本 / 大盘择时 / 仓位评分轮动 / 做T引擎 共 7 组），**留空 = 沿用线上默认**。并内置两组「一键预设」：

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

**A 股 T+1 约束**：做T 只动用**底仓**做「先卖后买」倒T（当日卖底仓可卖、买回次日可卖）；下跌加仓为隔夜行为（当日买、之后反弹时卖）。回测内部对「买回 / 减仓」按**次日开盘**处理，避免未来函数。

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
| 收盘选股 | 工作日 15:30 | 全市场扫描，命中信号持久化；**评分 Top N 自动加入自选股** |
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
- 安全：公网暴露期间任何人可访问；建议在 dashboard 配置访问认证，且不要在公网环境配置真实 LLM Key
- 数据源：服务器在本机，A股数据源直连不受穿透影响
- 调度任务：自动盯盘（盘中建议/收盘选股）依赖后端进程存活，关机或关闭后端后当天任务不执行

## 技术栈

- **后端**：Python 3.11+ / FastAPI / Pandas / akshare / SQLite + Parquet / APScheduler
- **前端**：Vue 3 + Vite + TypeScript + TailwindCSS + ECharts + Pinia
- **LLM**：OpenAI 兼容协议（DeepSeek / 通义 / 智谱 / OpenAI 等可配置），**未配置 API Key 时自动降级为技术面规则评分，其余功能完整可用**

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│ Vue3 前端（五段式流水线）  ①股票池→②自选→③队列→④交易池→⑤成交 │
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
Get-Content e:\Study\SAS\data\logs\sas-service.log -Tail 50   # 看日志
nssm remove SAS confirm                # 卸载服务
```

**模式区分**：
- **生产模式**（推荐，公网访问用）：只跑 `SAS` 服务一个进程，访问 http://127.0.0.1:8000
- **开发模式**（改代码调试用）：`python start_dev.py`（前端 Vite HMR + 后端 `--reload` 自动重启），二者不要同时使用（8000 端口冲突）

### 8. 扫描加速（默认范围 + K线预热）

网页选股扫描慢的主要瓶颈不是"没存历史数据"（K线已增量缓存到 `data/parquet/`），而是**默认全市场 5000 只** + **首次扫描时 K线尚未缓存**导致的实时网络拉取。两项优化：

- **默认扫描范围收窄**：选股中心默认 `成交额前 500（推荐）`，下拉可选全市场 / 成交额前 500/1000 / 各板块；后端 `top_amount:N` 按实时成交额取流动性最好的 N 只，冷缓存下最快出结果
- **K线预热**：
  - 开机后台低并发（默认 10）预热流动性前 1000 只到本地 Parquet
  - 每个交易日 16:00 调度全市场日K增量刷新，保持缓存新鲜
  - 同一轮预热会顺带刷新上证指数日线（`idx_sh000001`）：`regime_ma` / `regime_tiers` 只认这条序列，指数不跟着日更，最近几天就会静默退化成默认档；预热结果里的 `index` 字段（`ok/last/bars`）可在 `/api/screening/kline-status` 看到
  - 选股中心「预热K线」按钮可手动触发全市场预热（后台执行，不阻塞）
  - 预热完成后扫描直接读本地缓存，消除实时网络拉取与数据源限流等待
- 配置项：`kline_warmup_concurrency`（预热并发）、`kline_warmup_top_n`（开机预热只数，0=全市场）
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
- **仓位规则**：等分资金，最多持有 10 只，单票不超过总资产 20%；买入向下取整到 100 股（整手）
- **交易规则（对齐 A 股）**：T+1（当日买入次日可卖）、佣金万 2.5（最低 5 元）+ 卖出印花税千 1 + 双边过户费万 0.1
- **成交价**：执行时刻最新价（快照优先，失败退化 K 线收盘价）
- **止盈止损**：默认止损 -8% / 止盈 +20%，每日收盘 15:40 检查触发
- **自动执行链路**：开盘 9:20 / 盘中每 5 分钟分析产生的买卖建议自动送入模拟盘；盘后 15:40 结算并记录净值
- **关闭自动执行**：环境变量 `PAPER_AUTO_TRADE=false`（仅记录信号不成交）

页面：导航「④ 交易池」→ 交易模式 / 撮合状态 / 待执行信号 / 候补队列；「⑤ 持仓与成交」→ 账户收益率 / 资金分布 / 净值曲线 / 当前持仓 / 交易流水（手动模式下单也在这一页）；`POST /api/paper/reset` 重置账户。

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

106 个单元测试，覆盖：指标公式正确性、各策略命中/不命中场景、策略参数注入与
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
│   ├── tests/                 # 106 个单元测试
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

## 设计参考

本项目架构借鉴了以下成熟开源项目（均为 MIT 协议）：

- [Sequoia-X](https://github.com/silverhornz/Sequoia-X) — 收盘后自动选股流水线、策略工厂模式
- [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) — LLM 决策仪表盘结构化设计
- [tick-stock-panel](https://github.com/shy3130/tick-stock-panel) — 数据源可插拔能力路由
- [akshare](https://github.com/akfamily/akshare) — 免费 A 股数据源

---

**再次提醒：本系统仅供学习研究，不构成投资建议。**
