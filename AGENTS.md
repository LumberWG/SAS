# AGENTS.md — 给任何 AI 编码工具的入场须知

本文件是**跨工具交接约定**。不管是 CodeBuddy、WorkBuddy 还是别的工具，进仓库先读这里，
再读 `README.md`（功能）/ `WORKLOG.md`（策略）/ `BACKTEST_LOG.md`（实验）。

最后更新：2026-10-31

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
- **线上与回测同口径才有意义**：线上 `paper_pool_top_n=300`，回测 `universe_size` 默认跟随它，
  不要写死 500 再互相比较。
- **ML 打分看 `effective`**：`effective=false` 说明整段回退 14 因子，此时结果与基线一模一样，
  不能当作「ML 有效」的证据。

## 5. 常用命令

```powershell
cd e:\Study\SAS\backend
.\.venv\Scripts\python.exe -m pytest tests -q          # 全量单测（当前 310 passed）
.\.venv\Scripts\python.exe scripts\weekly_review.py --md   # 周度复盘（零成交归因）
.\.venv\Scripts\python.exe scripts\live_review.py --md     # 实盘 vs 等权基准超额对账
schtasks /run /tn SASRestart                            # 重启后端（无需 UAC）
Get-Content e:\Study\SAS\backend\logs\backend.log -Tail 50  # 应用日志（业务判定在这）
.\.venv\Scripts\python.exe scripts\backup_db.py         # 手动备份库（SQLite 在线 backup API）
schtasks /run /tn SASDbBackup                           # 每日 15:50 的备份计划任务，立即触发
```

服务：`SAS`（后端 :8000，nssm）、`SAS-frontend`（:8080）、`cpolar`（公网隧道）。

**接口已启用访问令牌**：除 `/api/health` 外，所有 `/api/*` 都要带 `X-API-Token`
（值在 `backend\.env` 的 `API_TOKEN`，不随仓库走）。不带会 **401**，别误判成服务挂了：

```powershell
Invoke-RestMethod -Headers @{'X-API-Token'='<令牌>'} http://127.0.0.1:8000/api/settings
```

## 6. 当前状态（2026-10-31 收盘后）

| 项 | 值 | 说明 |
| --- | --- | --- |
| `paper_t_active` | false | 做T 按 `#66` 结论关闭，重开条件写在 README「做T引擎」 |
| `live_ml_scoring` | false | 无有效回测支撑（`#79` `effective=false`）+ 首轮构建 109s，见 `docs` §7.6 |
| `paper_weak_exposure` | 0.3 | 弱市只降仓不全停（`#63`）；0 会让弱市零成交 |
| `paper_dynamic_exposure` / `paper_policy_floor_enabled` / `paper_circuit_breaker_enabled` | 全 false | 机制层（`#84`/`#85`）已落地但**均未经配对回测**，需单开满 21 个交易日再判读 |
| 影子策略 | 11-02 起每交易日 16:45 | `default` vs `default_ml` 落 `data/logs/shadow_live.json`，是「ML 能不能开」的证据来源 |
| `API_TOKEN` | 已启用（2026-10-31） | 值只在 `backend\.env`；前端 401 时弹框输入，`/api/health` 豁免 |
| `SASDbBackup` | 每日 15:50 | SQLite 在线 backup API → `D:\Backup\SAS\backups\`，保留 30 份 |

## 7. 数据与产物

- 生产库：`D:\Backup\SAS\data\sas.db`（只读核对时用 `mode=ro`）。
- K 线与指数：`D:\Backup\SAS\data\klines`，指数文件 `idx_sh000001.parquet`。
- 应用日志：`backend\logs\backend.log`（10MB×5 轮转）；nssm 的 stderr 基本是空的，别只看那个。
- 库备份：`D:\Backup\SAS\backups\sas-YYYYMMDD-HHMMSS.db`（保留 30 份）。**别用 `Copy-Item` 备份 WAL 库**，
  要用 `scripts/backup_db.py`（在线 backup API）。
- `.env` 在 `.gitignore` 里，不进仓库；发布基线写在 `.env.example`。
