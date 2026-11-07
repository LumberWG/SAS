# AGENTS.md — 给任何 AI 编码工具的入场须知

本文件是**跨工具交接约定**。不管是 CodeBuddy、WorkBuddy 还是别的工具，进仓库先读这里，
再读 `docs/README.md`（**文档统一入口**）/ `docs/overview.md`（**代码全景地图**）/ `README.md`（功能）/
`docs/flow.md`（全链路流程与参数）/ `WORKLOG.md`（策略）/ `BACKTEST_LOG.md`（实验）。
**文档统一目录**：参考/说明类文档都在 `docs/`（研究/历史报告在 `docs/analysis/`）；根目录只保留
`README/AGENTS/CONTRIBUTING/LICENSE/WORKLOG/BACKTEST_LOG` 这 6 个必须在根的文件。
**对外贡献规则**（含从第 4 节提升为硬约束的口径红线）见 `CONTRIBUTING.md`；
本仓库以 [MIT](LICENSE) 授权。

最后更新：2026-11-04

## 0. 根目录脚本不要随便搬

`serve_frontend.py`（nssm 服务 `SAS-frontend` 入口）、`deploy.ps1` / `restart-backend.ps1` /
`run-hidden.vbs`（计划任务 `SASDeploy` / `SASRestart` 的入口）、`install-silent-deploy.*`
（一次性注册上面两个任务）——这些文件的**绝对路径写死在服务与计划任务里**，挪走会静默失效。
其余运维脚本已收进 `scripts/ops/`，开发脚本在 `scripts/dev/`。

## 1. 只有一份工作区，不要在别处再 clone

本机项目唯一路径：`e:\Study\SAS`，远端 `https://github.com/LumberWG/SAS.git`（分支 `main`）。

所有工具都在**同一个目录**里读写，所以不存在「A 工具的改动要搬到 B 工具」这回事 ——
`git log` / `git status` 就是接力棒。另开一份副本只会让两边互相覆盖。

## 2. 提交身份（本机没有 git 全局身份）

每次提交显式指定，不要改 `.git/config` 或全局 `gitconfig`：

```powershell
git -c user.name=LumberWG -c user.email=663589316@qq.com commit -m "..."
```

## 3. 改代码必须同步文档（硬规则）

| 改动类型 | 必须同步 |
| --- | --- |
| 选股 / 评分 / 下单 / 交易规则 / 回测参数 | `README.md`（用户可见行为）＋ `docs/sas_formulas.md`（公式与口径）＋ `WORKLOG.md` 第十一节（逐条追加，写清「为什么」）|
| 回测 / 参数实验 | `BACKTEST_LOG.md`（只追加不改写；含条件/结果/归因/建议/人工判断/本轮变更/下轮计划；**标题用 `## 记录 #NN — 日期 · 标题`，编号必须唯一**）|

结论性的「不采纳」也要写清理由，避免以后重复试验。

## 4. 口径红线（都是踩过的坑）

- **14 因子必须喂加了指标的 frame**：`compute_factors(add_all_indicators(df))`。
  直接喂 `get_kline_cached()` 的原始 df，`bias6`/`atr` 等预计算列缺失 → 多数因子退化为 0、
  `total` 被压进 ±20，会得出「线上买不进任何票」这类**假结论**。
- **运行时开关有两层**：`app_state` 的 `cfg:<key>` 覆盖层**优先级高于** `.env` 与 `config.py` 默认值。
  判断是否真的生效，看 `GET /api/settings` 的 `source` 字段，别只看 `.env`。
  重要的关/开要**两处都写**（覆盖层 + `.env`），否则一次「还原」就回到旧值。
- **`param_specs._FIELD_RE` 要求行尾注释前 ≥2 个空格**（`\s{2,}#`）。只打 1 个空格会让设置页
  tooltip 变空，`tests/test_settings.py` 直接挂。
- **线上与回测同口径才有意义**：线上 `paper_pool_top_n=300`，`/api/backtest/defaults` 会把
  `universe_size` 设成 `paper_pool_top_n or 300`，不要写死 500 再互相比较。
  **但 `BacktestParams.universe_size` 数据类默认仍是 500**：直接调 pipeline 或跑 `scripts\*.py`
  时必须显式传 `--universe 300`，否则跑的是「Top500 里的策略」，与线上和 `#76` 都不是同一实验。
- **ML 打分看 `effective`**：`effective=false` 说明整段回退 14 因子，此时结果与基线一模一样，
  不能当作「ML 有效」的证据。
- **测试会读到 `.env` 的 `API_TOKEN`**：`tests/conftest.py` 已在会话级把它置空（就地改单例，
  不清 lru 缓存）。删掉这个夹具，接口用例会成片 401，看着像功能坏了。
- **备份 WAL 库不能用 `Copy-Item`**：服务在写主库时 `-wal` 里还有未合并的提交，直接复制会丢
  最近几笔成交。用 `scripts/backup_db.py`（SQLite 在线 backup API）。
- **轮动卖出前必须先确认换入票能买进**：`_pick_rotation_target` 用与买入同一套闸门
  （`entry_gate(ignore_slots=True)` + `entry_price_ok` + `≥ half_score`），还要校验「卖出所得够买一手」。
  只按评分就卖＝只卖不买、仓位凭空降一档，还会和弱市降仓混在一起无法归因（`BACKTEST_LOG` #91）。
  轮动候选还要把 `live` 的候补队列一起传进去，否则那些高分票永远进不了轮动视野。
- **`paper_max_positions` 是上限不是门槛**：不满仓也能轮动，一次可连换多只（`#93`）。
  任何"必须满 X 只才做某事"的写法都是回退到旧口径。`paper_max_total_exposure`(80%) 同理
  ——它是**收盘目标**，盘中买入上限是 100%（现金约束），收盘前 10 分钟由 `eod_trim` 压回；
  改回盘中也卡 80% 会让「钱满票不满」的僵局重现（`#93` 的起因）。
- **本机 `.env` 把 `PAPER_BUY_OPEN_WINDOW_MINUTES` 设成了 0**（2026-11-02 起，窗口关闭）。
  `test_exec_rules.py` 里测「开盘窗口时点规则」的 3 个用例已改为用 `window_on` 夹具**显式把窗口设成 5 分钟**，
  不再随本机 `.env` 漂移（2026-11-07 前这 2 例会挂，属环境口径不是回归）。
- **不要再接受「全量 4 例既有失败」这个说法**：2026-11-07 已全部清掉 ——
  `param_specs._SHORT_LABELS["paper_buy_open_window_minutes"]` 误塞了一整句说明（标签含「；」且 >10 字），
  改成「开盘成交窗口」；全量顺序污染来自 `update_settings()` 就地改 Settings 单例，已在
  `tests/conftest.py` 加 autouse 快照/还原夹具。**本机全量 `pytest -q` 现在应当全绿**，
  再出现失败先当回归查，不要归因为环境。

## 5. 常用命令

```powershell
cd e:\Study\SAS\backend
.\.venv\Scripts\python.exe -m pytest tests -q          # 全量单测（当前 404 例，全离线，约 35s；2026-11-07 起应全绿）
.\.venv\Scripts\python.exe -m ruff check .             # 代码检查（配置在 backend\pyproject.toml）
.\.venv\Scripts\python.exe scripts\weekly_review.py --md   # 周度复盘（零成交归因）
.\.venv\Scripts\python.exe scripts\live_review.py --md     # 实盘 vs 等权基准超额对账
schtasks /run /tn SASRestart                            # 重启后端（无需 UAC）
Get-Content e:\Study\SAS\backend\logs\backend.log -Tail 50  # 应用日志（业务判定在这）
.\.venv\Scripts\python.exe scripts\backup_db.py         # 手动备份库（SQLite 在线 backup API）
schtasks /run /tn SASDbBackup                           # 每日 15:50 的备份计划任务，立即触发
.\.venv\Scripts\python.exe scripts\run_minutes_all.py            # 分钟线补到最近交易日（只补增量）
.\.venv\Scripts\python.exe scripts\run_minutes_all.py --end 20261030  # 只补某一天
schtasks /run /tn SASMinutesFsdb                        # 每交易日 17:30 的分钟线导出，立即触发
```

服务：`SAS`（后端 :8000，nssm）、`SAS-frontend`（:8080）、`cpolar`（公网隧道）。

**接口已启用访问令牌**：除 `/api/health` 外，所有 `/api/*` 都要带 `X-API-Token`
（值在 `backend\.env` 的 `API_TOKEN`，不随仓库走）。不带会 **401**，别误判成服务挂了：

```powershell
Invoke-RestMethod -Headers @{'X-API-Token'='<令牌>'} http://127.0.0.1:8000/api/settings
```

## 6. 当前状态（2026-11-02 收盘后）

| 项 | 值 | 说明 |
| --- | --- | --- |
| `paper_t_active` | **true**（2026-11-02 开） | 用户决策强制开启（覆盖 #66 待满足条件）；`config.py` 默认、`.env`、`app_state` 覆盖层**三处一致**，还原不回退；监控见 README「做T引擎」 |
| `live_ml_scoring` | **true**（2026-11-01 重开） | `#82` 配对 +4.55pp 超 3.5pp 线；实测 152 只 20.7s、当日 `as_of` 缓存只训练一次，见 `docs` §7.6 |
| `paper_weak_exposure` | 0.3 | 弱市只降仓不全停（`#63`）；0 会让弱市零成交 |
| `paper_dynamic_exposure` / `paper_policy_floor_enabled` / `paper_circuit_breaker_enabled` | 全 false | 机制层（`#84`/`#85`）已落地但**均未经配对回测**，需单开满 21 个交易日再判读 |
| `paper_buy_open_window_minutes` | **0**（2026-11-02 起） | 线上把开盘窗口关了 → 交易时段全时段可买，**与回测「次日开盘价」口径不一致**；也是本机 `test_exec_rules` 2 例失败的来源 |
| `paper_rotation_enabled` 等 4 项 | **true** / 40 / 20 / **10** | 2026-11-02 起默认开（`#92`）。**口径已改（`#93`）**：`max_positions` 是上限不是门槛，不满仓也能换，可连换多只；`max_per_day` 现在是「每日轮动**只数**」（10 ≈ 不限）。判读先看有没有「轮动替换」成交 |
| `paper_max_total_exposure` / `paper_eod_trim_*` | 0.80 / true / 10 | **80% 是收盘目标**（`#93`）：盘中允许满仓（只受现金+单票 20%），收盘前 10 分钟按最弱优先压回 80%，今仓 T+1 卖不掉就跳过 |
| 影子策略 | 11-02 起每交易日 16:45 | `default` vs `default_ml` 落 `data/logs/shadow_live.json`，是「ML 能不能开」的证据来源 |
| `API_TOKEN` | 已启用（2026-10-31） | 值只在 `backend\.env`；前端 401 时弹框输入，`/api/health` 豁免 |
| `SASDbBackup` | 每日 15:50 | SQLite 在线 backup API → `D:\Backup\SAS\backups\`，保留 30 份 |
| `SASMinutesFsdb` | 每交易日 17:30 | 本地 fsdb → `minute1_fsdb` / `minute5_fsdb`，只补增量；个股详情分时图的数据来源 |
| 涨跌家数广度 | 每日 16:00 刷新，**仅观测** | `app/data/breadth.py` → `data\breadth_daily.parquet`。曾想用它区分 shock/自由落体，实测 4 年非重叠只有 5 个样本、方向还不稳（`#86`），**不进任何判定** |

## 7. 数据与产物

- 生产库：`D:\Backup\SAS\data\sas.db`（只读核对时用 `mode=ro`）。
- K 线与指数：`D:\Backup\SAS\data\klines`，指数文件 `idx_sh000001.parquet`。
- 分钟线：`D:\Backup\SAS\data\minute1_fsdb` / `minute5_fsdb`（本地 fsdb 导出，**不走 akshare**），
  进度清单 `_done_1m.txt` / `_done_5m.txt` 里存 `code|YYYYMMDD`。分时图读 1m 库，
  它落后日 K 一天时页面会显示「本地历史 · 某日」——那不是缓存 bug，是导出没跑。
- 应用日志：`backend\logs\backend.log`（10MB×5 轮转）；nssm 的 stderr 基本是空的，别只看那个。
- 库备份：`D:\Backup\SAS\backups\sas-YYYYMMDD-HHMMSS.db`（保留 30 份）。**别用 `Copy-Item` 备份 WAL 库**，
  要用 `scripts/backup_db.py`（在线 backup API）。
- `.env` 在 `.gitignore` 里，不进仓库；发布基线写在 `.env.example`。
