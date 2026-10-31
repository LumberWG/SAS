# 参与贡献

感谢有兴趣一起改。这个仓库最初是个人研究项目，所以下面这套规矩是**事后补的**——目的只有一个：
**别让「看起来变好了」的改动，把已经验证过的结论悄悄改坏。**

---

## 1. 动手前先读四份文件

| 文件 | 读它是为了 |
| --- | --- |
| `README.md` | 功能边界与用户可见行为（改了行为要同步这里） |
| `docs/flow.md` | 全链路流程图：一次买卖经过哪些闸门、被哪些参数挡住、skip 原因怎么对号 |
| `docs/sas_formulas.md` | 公式与实现口径。**代码和它对不上时，先确认谁才是对的** |
| `BACKTEST_LOG.md` | 哪些想法已经试过、结论是什么、为什么**不采纳**（避免重复试验） |
| `WORKLOG.md` 第十一节 | 策略变更的逐条记录（含「为什么」） |

`AGENTS.md` 是给 AI 编码工具看的交接约定，人也可以读，内容是下面「口径红线」的扩展版。

---

## 2. 开发环境

- Python **3.11+**（开发与 CI 用 3.12）
- Node **20+**（前端 Vite）
- Windows 是本项目实际部署平台（nssm 服务 + 计划任务）。代码本身跨平台，但「部署脚本」那套只保证 Windows。

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt        # 日常开发：下限
.venv\Scripts\pip install -r requirements.lock.txt   # 复现历史回测结论：锁定版本

cd ..\frontend
npm ci
```

## 3. 提交前必须本地跑通

```powershell
cd backend
.venv\Scripts\python.exe -m pytest      # 当前 324 例，约 35 秒，全部离线
.venv\Scripts\python.exe -m ruff check .   # 配置在 backend/pyproject.toml
```

CI（`.github/workflows/ci.yml`）跑的就是这两件事：

- `lint`（ubuntu-latest）：`ruff check` + `compileall`（语法层）；
- `test`（windows-latest）：用 `requirements.lock.txt` 装依赖后跑 pytest。
  **选 Windows 是因为 lock 文件从 Windows + CPython 3.12 冻结，锁版本只在同平台等价。**

> ruff 目前只开 `E9 / F63 / F7 / F82`（一定是 bug 的四类）。存量代码按默认规则还有 380+ 条
> 风格告警，**不要**在无关 PR 里顺手全量格式化——规则按模块逐步收紧，一次一类。

---

## 4. 口径红线（必读，几乎都是踩过的坑）

1. **14 因子必须喂加了指标的 frame**：`compute_factors(add_all_indicators(df))`。直接喂
   `get_kline_cached()` 的原始 df，`bias6` / `atr` 等预计算列缺失 → 多数因子退化为 0、
   `total` 被压进 ±20，会得出「线上买不进任何票」这类**假结论**。
2. **运行时开关有两层**：`app_state` 的 `cfg:<key>` 覆盖层优先级**高于** `.env` 与 `config.py`
   默认值。判断是否真的生效看 `GET /api/settings` 的 `source` 字段，别只看 `.env`。
   重要的开/关要**两处都写**（覆盖层 + `.env`），否则一次「还原」就回到旧值。
3. **线上与回测同口径才有意义**：线上 `paper_pool_top_n=300`，回测 `universe_size` 默认跟随它。
   不要写死 500 再互相比较——那是两个实验。
4. **ML 打分看 `effective`**：`effective=false` 表示整段回退 14 因子，此时结果与基线**完全相同**，
   不能当作「ML 有效」的证据。
5. **`param_specs._FIELD_RE` 要求行尾注释前 ≥2 个空格**（`\s{2,}#`）。只打 1 个空格会让设置页
   tooltip 变空，`tests/test_settings.py` 直接挂。
6. **测试会读到 `.env` 的 `API_TOKEN`**：`tests/conftest.py` 已在会话级把它置空。删掉这个夹具，
   接口用例会成片 401，看着像功能坏了。
7. **备份 WAL 库不能用 `Copy-Item`**：服务在写主库时 `-wal` 里还有未合并的提交，直接复制会丢
   最近几笔成交。用 `scripts/backup_db.py`（SQLite 在线 backup API）。
8. **单笔卖出/止损/做T 必须走 `reduce_paper_sellable`**，不能直接改 `shares`；
   卖出成交后要**同步内存 `positions`**，否则同批「卖 A 买 B」会报「持仓已满」等到下一个 tick。

---

## 5. 改代码必须同步文档（硬规则）

| 改动类型 | 必须同步 |
| --- | --- |
| 选股 / 评分 / 下单 / 交易规则 / 回测参数 | `README.md`（用户可见行为）+ `docs/flow.md`（流程与参数索引）+ `docs/sas_formulas.md`（公式与口径）+ `WORKLOG.md` 第十一节（逐条追加，写清「为什么」） |
| 回测 / 参数实验 | `BACKTEST_LOG.md`（**只追加不改写**；含条件/结果/归因/建议/人工判断/本轮变更/下轮计划；标题用 `## 记录 #NN — 日期 · 标题`，**编号必须唯一**） |

结论性的「不采纳」也要写清理由——否则半年后会有人把同一个实验再跑一遍。

## 6. 策略与参数的验收纪律

- 单窗口收益好看**不构成**采纳理由。至少：配对回测 + **不重叠随机分段**（`scripts/random_dist.py`）；
- 样本不足 **21 个交易日**只报方向，不下结论；
- 用 4 年数据里只剩 5 个样本的分组去调阈值 = 过拟合，不做（见 `BACKTEST_LOG` #68 / #86）；
- 新增开关**默认关闭**，且「关闭时行为与改造前逐行一致」。

## 7. 依赖变更

1. 改 `backend/requirements.txt`（下限写法）；
2. 重新生成锁定清单：`.venv\Scripts\pip freeze > requirements.lock.txt`，**补回文件头部注释**；
3. PR 里说明「为什么升」以及「是否影响已落盘的回测结论」（尤其 `lightgbm` / `scikit-learn` /
   `pandas`——换版本等于换模型/换口径，`#74` Ridge −0.33pp 与 `#76` LightGBM +4.55pp 就是两条路径）。

## 8. 提交与 PR

- 提交信息：`type(scope): 中文描述`，`type` 用 `feat / fix / docs / test / refactor / chore`。
  历史里已有这套风格（`fix(regime): ...`、`docs: ...`）。
- 一个 PR 只做一件事；**不要**把「顺手格式化 105 个文件」混进功能改动。
- 新行为必须有测试；修复 bug 要有能复现它的用例（先红后绿）。
- 不要提交：`.env`、`data/`、K 线 parquet、`*.db`、日志、`node_modules/`、`frontend/dist/`。

## 9. 不接受的改动

- 「美化净值」类改动（取价失败按 0 计市值、回测里剔除亏损样本等）；
- 没有回测支撑却上调仓位 / 放宽闸门 / 打开做T；
- 为了让测试变绿而改断言、放宽 `param_specs` 校验、或 `# noqa` 掉 `E9/F82`；
- 引入需要联网才能跑的单测（现有 324 例全部离线，请保持）。

## 10. 安全

- 发现令牌/密钥泄露请直接提 issue 说明「哪类文件暴露」，**不要在 issue 里贴真实值**；
- 涉及账户数据的截图/日志，先脱敏再贴。

---

许可：提交即表示同意以 [MIT License](LICENSE) 授权。
