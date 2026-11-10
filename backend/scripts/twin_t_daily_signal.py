# -*- coding: utf-8 -*-
"""持仓做T候选 · 日更截面信号（学习研究辅助工具，不接实盘、不自动下单）。

定位
----
#139–#156 的研究结论：机械 T+0 做T 信号族在「散户约束（无融券 + 真实成本 + 典型持仓规模）」
下**不存在可交易边缘**；边缘真实但极薄（截面排序信息有效），被最低佣金 + 印花税 + 滑点结构性锁死。
研究还发现：SAS 在线做T引擎用的是「开盘涨跌幅」价格阈值（paper_t_up_sell 等），而研究真正的
（薄）边缘来自**截面分位**——某只票在「全市场同一时刻」有多极端。

本工具把研究的截面方法论落地为一个**每日收盘后的观察清单**：
- 读你真实的持仓（paper_positions）；
- 对目标交易日，算每只持仓股相对全市场的 dev（价/日内累计VWAP−1）截面分位；
- 标出「当天最超卖（正T 候选：低买等回归）/ 最超买（反T 候选：卖底仓等回归）」的持仓；
- 附**成本临界判定**：按你的持仓规模与券商费率，算做一次做T往返的盈亏平衡 dev 幅度，
  并诚实指出「该极端幅度是否真能覆盖你的成本」。
- 它**不是交易指令**，是「今天组合里哪些名字在全市场视角下最极端」的减亏辅助/复盘清单。

数据
----
- 5 分钟线：app.data.minute.MINUTE5_DIR（默认 D:/Backup/SAS/data/minute5_fsdb，5559 只）。
- 持仓：settings.db_path 的 paper_positions 表（code/name/shares/cost_price/sellable_shares）。
- 费率/做T 比例：默认取自 app.config（trade_fee_* / paper_t_size），可用命令行覆盖。
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# 让脚本在 backend/ 下可直接 import app
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.config import get_settings  # noqa: E402
from app.data.minute import MINUTE5_DIR  # noqa: E402

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
DEV_CAP = 0.15          # |dev| > 15% 视为脏 bar（涨跌停/数据异常），置 NaN（与研究一致）
MIN_BARS = 30           # 当日有效 bar 不足半数则剔除出截面
BENCHMARKS = ["600519", "000001", "300750", "601318", "600036"]  # 用于探测最新交易日


def _auto_target_date(minute_dir: Path) -> str:
    """用基准流动性票的最大日期探测最新交易日（这些票每日更新，最可靠）。"""
    dates = []
    for code in BENCHMARKS:
        p = minute_dir / f"{code}.parquet"
        if not p.exists():
            continue
        try:
            d = pd.read_parquet(p, columns=["date"])["date"]
            dates.append(pd.to_datetime(d.max()).date())
        except Exception:
            pass
    if not dates:
        raise RuntimeError("无法从基准票探测交易日，请显式传 --date")
    return str(max(dates))


def _load_day_dev(path: Path, day: str, dev_cap: float = DEV_CAP):
    """读某只票目标交易日的 5 分钟线，返回 (dev数组, 时间字符串列表, 脏bar数) 或 None。

    dev = close / 日内累计VWAP − 1（只用当前及以前信息，无未来函数）。
    按日内位置索引对齐（第 k 根 5 分钟 bar = 同一交易时刻，全市场一致）。
    """
    if not path.exists():
        return None
    try:
        # 下推过滤：date 列物理存储为 'YYYY-MM-DD HH:MM:SS' 字符串，
        # 按日边界字符串过滤只取当日 ~48 根 5 分钟 bar（避免读全历史 2 万行）。
        df = pd.read_parquet(
            path,
            filters=[("date", ">=", day + " 00:00:00"), ("date", "<=", day + " 23:59:59")],
            columns=["date", "close", "volume", "amount"],
        )
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    if len(df) < MIN_BARS:
        return None
    df = df.sort_values("date").reset_index(drop=True)
    vol = df["volume"].astype(float).cumsum().to_numpy()
    amt = df["amount"].astype(float).cumsum().to_numpy()
    vwap = np.divide(amt, vol, out=np.ones_like(amt), where=vol > 0)
    dev = df["close"].astype(float).to_numpy() / vwap - 1.0
    n_invalid = int((np.abs(dev) > dev_cap).sum())
    dev[np.abs(dev) > dev_cap] = np.nan  # 脏值清洗
    times = [str(pd.Timestamp(t).time())[:5] for t in df["date"].to_numpy()]
    return dev, times, n_invalid


def build_universe(minute_dir: Path, day: str, observe_cap: int = 0):
    """构建目标交易日的全市场 dev 截面矩阵。

    返回 (matrix, codes, slot_times, invalid_frac)
      matrix: list[np.ndarray]，每个元素是一只票的 dev 数组（按位置索引对齐，缺 bar 为 NaN）
      slot_times: 参考票的第 k 根 bar 时刻字符串
    observe_cap>0 时只取前 N 只（按文件名排序）以加速；0 = 全市场。
    """
    files = sorted(glob.glob(str(minute_dir / "*.parquet")))
    if observe_cap and observe_cap > 0:
        files = files[: observe_cap]
    matrix: list[np.ndarray] = []
    codes: list[str] = []
    slot_times: list[str] = []
    n_finite = 0
    n_invalid = 0
    for f in files:
        code = os.path.basename(f)[:-8]
        res = _load_day_dev(Path(f), day)
        if res is None:
            continue
        dev, times, n_inv = res
        if not np.isfinite(dev).any():
            continue
        if not slot_times and len(times) >= MIN_BARS:
            slot_times = times
        matrix.append(dev)
        codes.append(code)
        n_finite += int(np.isfinite(dev).sum())
        n_invalid += n_inv
    invalid_frac = (n_invalid / (n_invalid + n_finite)) if (n_invalid + n_finite) else 0.0
    return matrix, codes, slot_times, invalid_frac


def holding_percentiles(hold_dev: np.ndarray, universe: np.ndarray):
    """对一只持仓股，逐截面槽算其在全市场的分位。

    universe: (N, M) 全市场 dev 矩阵（每槽一列）。hold_dev: (M,) 该持仓 dev。
    返回 (per_slot_pctile, per_slot_dev)：每槽的分位(%)与持仓 dev 值（NaN 槽忽略）。
    """
    M = universe.shape[1]
    pctile = np.full(M, np.nan)
    for k in range(M):
        col = universe[:, k]
        col = col[np.isfinite(col)]
        v = hold_dev[k]
        if len(col) == 0 or not np.isfinite(v):
            continue
        pctile[k] = float((col < v).mean() * 100.0)
    return pctile


def cost_break_even(notional: float, commission_rate: float, min_commission: float,
                    stamp_rate: float, slippage_bps: float) -> float:
    """做一次做T往返的盈亏平衡 dev 幅度（占名义额 %）。

    成本 = 2×max(佣金率×名义额, 最低佣金) + 印花税(卖出腿) + 滑点(往返)
    平衡幅度 = 成本 / 名义额 ×100。
    """
    comm = 2.0 * max(commission_rate * notional, min_commission)
    stamp = stamp_rate * notional
    slip = slippage_bps * 2.0 * notional
    cost = comm + stamp + slip
    return cost / notional * 100.0 if notional > 0 else float("inf")


def load_holdings(db_path: Path):
    """读 paper_positions 当前多头持仓（shares>0）。返回 list[dict]。"""
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT code, name, shares, cost_price, sellable_shares "
        "FROM paper_positions WHERE shares > 0"
    ).fetchall()
    conn.close()
    out = []
    for code, name, shares, cost, sellable in rows:
        out.append({
            "code": str(code).strip().zfill(6),
            "name": name or "",
            "shares": int(shares),
            "cost": float(cost or 0.0),
            "sellable": int(sellable or 0),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="持仓做T候选 · 日更截面信号")
    ap.add_argument("--date", default=None, help="目标交易日 YYYY-MM-DD（默认自动探测最新）")
    ap.add_argument("--out", default=None, help="输出 md 路径（默认 data/logs/twin_t_signal_<date>.md）")
    ap.add_argument("--observe-cap", type=int, default=0,
                    help="观察池上限（只）；0=全市场（约5500，约20s）；如 500 可加速")
    ap.add_argument("--quantile", type=float, default=0.05,
                    help="极端分位阈值：正T取分位<=Q、反T取分位>=1-Q（默认0.05=最极5%）")
    ap.add_argument("--tail-frac", type=float, default=0.10,
                    help="候选门槛：全天在极端尾部停留时间占比>=此值才算候选（默认0.10=10%时段），过滤一瞬噪声")
    ap.add_argument("--t-ratio", type=float, default=None,
                    help="单次做T 比例（占持仓市值）；默认取 paper_t_size")
    ap.add_argument("--commission-rate", type=float, default=None, help="佣金率（默认配置万2.5）")
    ap.add_argument("--min-commission", type=float, default=None, help="单笔最低佣金（默认5元）")
    ap.add_argument("--stamp-rate", type=float, default=None, help="印花税（卖出，默认千1）")
    ap.add_argument("--slippage-bps", type=float, default=None, help="单腿滑点（默认5bps=0.05%）")
    ap.add_argument("--topn", type=int, default=20, help="清单最多展示多少只持仓（默认20）")
    args = ap.parse_args()

    cfg = get_settings()
    day = args.date or _auto_target_date(MINUTE5_DIR)
    minute_dir = MINUTE5_DIR

    # 费率：命令行 > 配置
    commission_rate = args.commission_rate if args.commission_rate is not None else cfg.trade_fee_commission_rate
    min_commission = args.min_commission if args.min_commission is not None else cfg.trade_fee_commission_min
    stamp_rate = args.stamp_rate if args.stamp_rate is not None else cfg.trade_fee_stamp_rate
    slippage_bps = args.slippage_bps if args.slippage_bps is not None else 0.0005
    t_ratio = args.t_ratio if args.t_ratio is not None else cfg.paper_t_size

    print(f"[twin_t_daily_signal] 目标交易日={day}  5分钟目录={minute_dir}  observe_cap={args.observe_cap or '全市场'}")

    # 1) 观察池（全市场截面）
    import time as _t
    _t0 = _t.time()
    matrix, codes, slot_times, invalid_frac = build_universe(minute_dir, day, args.observe_cap)
    print(f"[twin_t_daily_signal] 观察池加载耗时 {_t.time()-_t0:.1f}s")
    if not matrix:
        print("[错误] 观察池为空，检查日期/目录")
        return 1
    U = np.array(matrix, dtype=float)  # (N, M)
    N, M = U.shape
    print(f"[twin_t_daily_signal] 观察池 N={N}  槽数 M={M}  脏dev占比={invalid_frac:.4f}  时刻样本={slot_times[0]}..{slot_times[-1]}")

    # 2) 持仓
    holdings = load_holdings(cfg.db_path)
    if not holdings:
        print("[警告] paper_positions 无多头持仓，仅输出观察池统计")
    print(f"[twin_t_daily_signal] 持仓数={len(holdings)}")

    # 预存每只持仓的 dev（按位置索引对齐）
    for h in holdings:
        res = _load_day_dev(minute_dir / f"{h['code']}.parquet", day)
        if res is None:
            h["_dev"] = None
            continue
        dev, _, _n = res
        # 对齐到 M 槽：截断/补 NaN
        d = np.full(M, np.nan)
        n = min(len(dev), M)
        d[:n] = dev[:n]
        h["_dev"] = d

    # 3) 逐持仓算截面分位 + 成本临界
    results = []
    for h in holdings:
        dev = h.get("_dev")
        if dev is None:
            results.append({**h, "status": "NO_DATA"})
            continue
        pctile = holding_percentiles(dev, U)
        valid = np.isfinite(pctile)
        if not valid.any():
            results.append({**h, "status": "NOPERS"})
            continue
        # 最超卖（分位最低）-> 正T 触发点；最超买（分位最高）-> 反T 触发点
        lo_k = int(np.nanargmin(pctile))
        hi_k = int(np.nanargmax(pctile))
        lo_dev, lo_pct = dev[lo_k], pctile[lo_k]
        hi_dev, hi_pct = dev[hi_k], pctile[hi_k]
        # 在极端尾部停留的时间占比：持续极端才算候选，过滤一瞬噪声
        frac_pos = float(np.mean(pctile[valid] <= args.quantile * 100.0))
        frac_neg = float(np.mean(pctile[valid] >= (1.0 - args.quantile) * 100.0))
        med_pct = float(np.median(pctile[valid]))

        # 名义额 / 成本临界：用该持仓当日最后有效收盘价作价格代理（轻量重读）
        last_px = np.nan
        try:
            fdf = pd.read_parquet(minute_dir / f"{h['code']}.parquet",
                                  columns=["date", "close"])
            dt = pd.to_datetime(fdf["date"])
            sub = fdf[dt.dt.date.astype(str) == day]["close"].astype(float)
            if len(sub):
                last_px = float(sub.iloc[-1])
        except Exception:
            last_px = np.nan
        h["_last_close_proxy"] = last_px

        position_value = h["shares"] * last_px if np.isfinite(last_px) else 0.0
        t_notional = t_ratio * position_value
        be = cost_break_even(t_notional, commission_rate, min_commission, stamp_rate, slippage_bps)

        results.append({
            **h, "status": "OK",
            "lo_k": lo_k, "lo_time": slot_times[lo_k] if lo_k < len(slot_times) else "?",
            "lo_dev": lo_dev, "lo_pct": lo_pct,
            "hi_k": hi_k, "hi_time": slot_times[hi_k] if hi_k < len(slot_times) else "?",
            "hi_dev": hi_dev, "hi_pct": hi_pct,
            "frac_pos": frac_pos, "frac_neg": frac_neg, "med_pct": med_pct,
            "last_px": last_px, "position_value": position_value,
            "t_notional": t_notional, "break_even": be,
        })

    # 4) 生成报告
    q = args.quantile
    tail = args.tail_frac
    cand_pos = []  # 正T 候选（全天底部尾部分位停留占比 >= tail）
    cand_neg = []  # 反T 候选（全天顶部尾部分位停留占比 >= tail）
    for r in results:
        if r.get("status") != "OK":
            continue
        if r["frac_pos"] >= tail:
            cand_pos.append(r)
        if r["frac_neg"] >= tail:
            cand_neg.append(r)
    cand_pos.sort(key=lambda r: (-r["frac_pos"], r["lo_pct"]))
    cand_neg.sort(key=lambda r: (-r["frac_neg"], -r["hi_pct"]))

    lines = []
    lines.append(f"# 持仓做T候选 · 截面信号（{day}）\n")
    lines.append("> **减亏辅助 / 复盘清单，不是交易指令。** 详见文末「诚实边界」。\n")
    lines.append(f"- 目标交易日：**{day}**")
    lines.append(f"- 观察池（全市场截面）：**{N}** 只；交易日有效槽数 M={M}（{slot_times[0]}–{slot_times[-1]}）")
    lines.append(f"- 脏 dev 占比（|dev|>15% 已剔除）：{invalid_frac:.4f}")
    lines.append(f"- 真实持仓：**{len(holdings)}** 只")
    lines.append(f"- 极端阈值：分位 ≤{q*100:.1f}%（正T）/ ≥{(1-q)*100:.1f}%（反T）；做T比例 t_ratio={t_ratio:.2f}")
    lines.append(f"- 候选门槛：全天在极端尾部停留占比 ≥{tail*100:.0f}%（过滤一瞬噪声，保留持续极端）")
    lines.append(f"- 费率：佣金万{commission_rate*1e4:.1f}（最低{min_commission:.0f}元）+ 印花税千{stamp_rate*1e3:.1f} + 单腿滑点{slippage_bps*1e4:.1f}bps\n")

    def fmt_money(x):
        if not np.isfinite(x):
            return "—"
        if x >= 1e8:
            return f"{x/1e8:.2f}亿"
        if x >= 1e4:
            return f"{x/1e4:.2f}万"
        return f"{x:.0f}元"

    if cand_pos:
        lines.append(f"## 一、正T 候选（当日持续最超卖 · 低买等回归）— {len(cand_pos)} 只\n")
        lines.append("| 代码 | 名称 | 尾部停留% | 最超卖时刻 | dev% | 分位% | 持仓市值 | 做T名义额 | 平衡% | 覆盖? |")
        lines.append("|------|------|---------|----------|------|------|---------|---------|-------|-------|")
        for r in cand_pos[:args.topn]:
            cover = "✅" if abs(r["lo_dev"]) * 100 > r["break_even"] else "❌"
            lines.append(
                f"| {r['code']} | {r['name']} | {r['frac_pos']*100:.0f} | {r['lo_time']} | {r['lo_dev']*100:+.2f} | "
                f"{r['lo_pct']:.1f} | {fmt_money(r['position_value'])} | {fmt_money(r['t_notional'])} | "
                f"{r['break_even']:.2f} | {cover} |"
            )
        lines.append("")
    else:
        lines.append("## 一、正T 候选：今日无持仓持续处于全市场最超卖尾部\n")

    if cand_neg:
        lines.append(f"## 二、反T 候选（当日持续最超买 · 卖底仓等回归）— {len(cand_neg)} 只\n")
        lines.append("| 代码 | 名称 | 尾部停留% | 最超买时刻 | dev% | 分位% | 可卖底仓 | 持仓市值 | 做T名义额 | 平衡% | 覆盖? |")
        lines.append("|------|------|---------|----------|------|------|---------|---------|---------|-------|-------|")
        for r in cand_neg[:args.topn]:
            cover = "✅" if abs(r["hi_dev"]) * 100 > r["break_even"] else "❌"
            sellable = r.get("sellable", 0)
            lines.append(
                f"| {r['code']} | {r['name']} | {r['frac_neg']*100:.0f} | {r['hi_time']} | {r['hi_dev']*100:+.2f} | "
                f"{r['hi_pct']:.1f} | {sellable} | {fmt_money(r['position_value'])} | "
                f"{fmt_money(r['t_notional'])} | {r['break_even']:.2f} | {cover} |"
            )
        lines.append("")
    else:
        lines.append("## 二、反T 候选：今日无持仓持续处于全市场最超买尾部\n")

    # 全部持仓明细（含未进入极端者）
    lines.append("## 三、全部持仓当日截面快照\n")
    lines.append("| 代码 | 名称 | 底部停留% | 顶部停留% | 中位分位% | 平衡% | 状态 |")
    lines.append("|------|------|---------|---------|----------|------|------|")
    for r in results:
        if r.get("status") == "OK":
            lines.append(
                f"| {r['code']} | {r['name']} | {r['frac_pos']*100:.0f} | {r['frac_neg']*100:.0f} | "
                f"{r['med_pct']:.1f} | {r['break_even']:.2f} | OK |"
            )
        else:
            lines.append(f"| {r['code']} | {r.get('name','')} | — | — | — | — | {r.get('status')} |")
    lines.append("")

    lines.append("## 四、诚实边界（务必读）\n")
    lines.append(
        "1. **这不是交易信号，是观察清单。** 它只告诉你「今天组合里哪些名字持续处于全市场视角下的极端尾部」"
        "（全天在底部/顶部 5% 尾部停留≥门槛才算候选，过滤一瞬噪声），由你自行决定是否盯盘做T；"
        "不接实盘、不自动下单。\n"
        "2. **边缘真实但极薄。** #139–#156 全链路回测（约50组对照）表明：截面排序信息有效（gross 尚可），"
        "但机械 T+0 做T 在散户约束（无融券 + 真实成本 + 典型持仓规模）下**不存在可交易边缘**；"
        "唯一曾幸存形态要求持10只/100万/单票30k，对散户不现实。\n"
        "3. **成本是第一杀手。** 表中「平衡幅度%」= 你做一次做T往返必须赚到的幅度才不亏；"
        "它由「最低佣金5元/腿」主导——**持仓越小、名义额越低，平衡幅度越高**。若「覆盖?」多为 ❌，"
        "说明以你的持仓规模，做T 几乎覆盖不了成本（这正是研究的核心结论）。\n"
        "4. **dev 幅度的局限。** 即使某票 dev 达到 −2%，你也通常只能吃到其中一部分回归，"
        "且还要承担逆向选择与滑点；表里「覆盖?」按「极端幅度>平衡幅度」近似，偏乐观。\n"
        "5. **方法学：** dev=close/日内累计VWAP−1，只用当前及以前信息（无未来函数）；"
        "|dev|>15% 按脏值剔除；截面分位在同交易时刻全市场横比。SAS 在线做T引擎用的是开盘涨跌幅阈值，"
        "本清单用研究认可的截面分位，二者互补。\n"
    )
    lines.append("---")
    lines.append(f"_生成于 twin_t_daily_signal.py · 数据 {minute_dir.name} · 观察池 {N} 只_")

    md = "\n".join(lines)
    out_path = args.out or str(cfg.db_path.parent / "logs" / f"twin_t_signal_{day}.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(f"[twin_t_daily_signal] 报告已写: {out_path}")
    print(f"[twin_t_daily_signal] 正T候选 {len(cand_pos)} 只 / 反T候选 {len(cand_neg)} 只")
    # 终端摘要
    print("\n=== 候选摘要（尾部停留% / 触发时刻 / dev% / 分位% / 平衡% / 覆盖?）===")
    for r in cand_pos[:args.topn]:
        print(f"  正T {r['code']} {r['name']}  尾部{r['frac_pos']*100:.0f}% @ {r['lo_time']} dev={r['lo_dev']*100:+.2f}% 分位={r['lo_pct']:.1f}% 平衡={r['break_even']:.2f}% {'覆盖' if abs(r['lo_dev'])*100>r['break_even'] else '不覆盖'}")
    for r in cand_neg[:args.topn]:
        print(f"  反T {r['code']} {r['name']}  尾部{r['frac_neg']*100:.0f}% @ {r['hi_time']} dev={r['hi_dev']*100:+.2f}% 分位={r['hi_pct']:.1f}% 平衡={r['break_even']:.2f}% {'覆盖' if abs(r['hi_dev'])*100>r['break_even'] else '不覆盖'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
