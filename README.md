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
2. **信号**：选股结果记录扫描开始时的情绪快照；LLM 决策仪表盘在情绪权重 ≤ -0.5 时把规则结论「买入」自动降级为「持有」
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
- 分级：评分 ≥ +3 → 买入建议；≤ -3 → 卖出建议；其余持有观察
- **市场情绪修正**：情绪权重 ≤ -0.5（空头占优/极度悲观）时买入建议降级为持有
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
│ Vue3 前端（5 页面）  仪表盘 / 选股 / 个股 / 回测 / 信号自选   │
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

本项目面向**免费数据源**，针对国内网络环境实现了三级降级链：

```
东方财富(akshare) → 腾讯 → 新浪
```

- **熔断器**：某接口连续失败 3 次后进入 5 分钟冷却期，冷却期内直接降级，避免每只股票浪费重试时间
- **域名级节流**：同一域名请求间隔 ≥ 0.15s，降低触发风控（返回 501 JS 挑战页）的概率
- **新浪兜底**：该接口历史悠久、风控宽松，是腾讯被限流后的最终保障
- **快照 SWR 策略**：实时快照全量刷新一次约 90 秒（59 批顺序请求），采用
  stale-while-revalidate：TTL 内直接返回新鲜数据；TTL 外立即返回旧数据并后台单线程刷新
  （页面永不等待）；冷启动（无缓存）才同步刷新，接口层另有 15s/20s 超时降级
  （自选股列表退化为静态列表、概览返回 503）；刷新失败进入 60 秒冷却，防止数据源
  挂掉时每个请求重复走降级链。TTL 由 `snapshot_ttl` 配置（默认 30s），
  调度器每分钟主动刷新保持盘中数据新鲜

实测注意：
- K线成交量已统一换算为「手」；成交额为估算值（腾讯/新浪源不返回真实成交额）
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
  - 选股中心「预热K线」按钮可手动触发全市场预热（后台执行，不阻塞）
  - 预热完成后扫描直接读本地缓存，消除实时网络拉取与数据源限流等待
- 配置项：`kline_warmup_concurrency`（预热并发）、`kline_warmup_top_n`（开机预热只数，0=全市场）

### 7. 模拟交易（验证选股思路）

用模拟盘把「分析建议」变成可复盘的账本，验证选股思路的准确性。

- **初始资金 10 万元**，系统按智能建议 **自动执行买卖与仓位管理**
- **仓位规则**：等分资金，最多持有 10 只，单票不超过总资产 20%；买入向下取整到 100 股（整手）
- **交易规则（对齐 A 股）**：T+1（当日买入次日可卖）、佣金万 2.5（最低 5 元）+ 卖出印花税千 1 + 双边过户费万 0.1
- **成交价**：执行时刻最新价（快照优先，失败退化 K 线收盘价）
- **止盈止损**：默认止损 -8% / 止盈 +20%，每日收盘 15:40 检查触发
- **自动执行链路**：开盘 9:20 / 盘中每 5 分钟分析产生的买卖建议自动送入模拟盘；盘后 15:40 结算并记录净值
- **关闭自动执行**：环境变量 `PAPER_AUTO_TRADE=false`（仅记录信号不成交）

页面：导航「模拟交易」→ 账户收益率 / 资金分布 / 净值曲线 / 当前持仓 / 交易流水；`POST /api/paper/reset` 重置账户。

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
│   │   ├── views/             # Dashboard/Screening/StockDetail/Backtest/Signals
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
