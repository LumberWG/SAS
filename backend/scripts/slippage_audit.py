"""成交偏离审计：把模拟盘的真实成交价与**回测的成交假设**对齐。

为什么需要它（BACKTEST_LOG 记录 #29）：回测默认「次日开盘价成交 + 零滑点」，
而线上模拟盘由 `paper_tick` 每 5 秒按实时价成交。两者的差值 = 时点差异 + 冲击成本 +
报价延迟——这正是 `PipelineParams.slippage` 应该承载的量。不量这个数，
「中位超额 +1.x%/月」就只是纸面数字（0.3% 单边摩擦足以把正超额月份从 63% 打到 53%）。

口径（``cost_bps > 0`` 一律表示"我们比开盘价吃亏"）::

    买入: (成交价 / 当日开盘价 − 1) × 10000
    卖出: (当日开盘价 / 成交价 − 1) × 10000

按时段分组的意义：回测假设的是**开盘成交**，所以「开盘 30 分钟内」的成交最接近回测口径，
那一组的中位数是 `slippage` 的合理估计；越往后越混入"策略本就该晚点买"的选择效应。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts/slippage_audit.py
    .venv\\Scripts\\python.exe scripts/slippage_audit.py --since 2026-10-26 --tag oct
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from app.data.cache import db, kline_cache  # noqa: E402

#: 时段分桶：(下界含, 上界含, 名称)。覆盖 A 股连续竞价两段。
BUCKETS = (
    ("09:30", "09:59", "开盘30分"),
    ("10:00", "11:29", "上午盘中"),
    ("13:00", "14:29", "下午盘中"),
    ("14:30", "15:00", "尾盘"),
)


def cost_bps(side: str, fill: float, open_px: float) -> float | None:
    """成交价相对开盘价的成本（bp，>0 = 吃亏）；坏值返回 None。"""
    if not fill or not open_px or fill <= 0 or open_px <= 0:
        return None
    if side == "buy":
        return (fill / open_px - 1) * 10000
    return (open_px / fill - 1) * 10000


def bucket_of(hhmm: str) -> str:
    """按成交时刻（HH:MM）归桶，落在集合竞价/异常时刻归「其它」。"""
    for lo, hi, name in BUCKETS:
        if lo <= hhmm <= hi:
            return name
    return "其它"


def reason_group(reason: str) -> str:
    """把很长的成交原因归成几类，便于分组统计（原始字符串带评分/均线等细节）。"""
    text = str(reason or "")
    for prefix, label in (
        ("止损", "止损触发"), ("止盈", "止盈触发"), ("轮动", "轮动替换"),
        ("综合评分", "评分买入"), ("择时", "择时清仓"), ("时间止损", "时间止损"),
    ):
        if text.startswith(prefix):
            return label
    return "其它"


def _open_price(code: str, date: str) -> float | None:
    """取该股当日开盘价（走本地 K 线缓存；缺数据返回 None）。"""
    df = kline_cache.get(code)
    if df is None or df.empty or "open" not in df.columns:
        return None
    sub = df[df["date"].astype(str).str[:10] == date]
    if sub.empty:
        return None
    try:
        px = float(sub["open"].iloc[-1])
    except (TypeError, ValueError):
        return None
    return px if px == px and px > 0 else None


def _stats(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    n = len(ordered)
    return {
        "n": n,
        "median": round(statistics.median(ordered), 1),
        "mean": round(sum(ordered) / n, 1),
        "p90": round(ordered[min(n - 1, int(n * 0.9))], 1),
        "share_costly": round(sum(1 for v in ordered if v > 0) / n * 100, 1),
    }


def audit(since: str = "", until: str = "") -> dict:
    """逐笔计算偏离并按「方向 × 时段」汇总。"""
    rows = db.list_paper_trades(limit=100000)
    samples: list[dict] = []
    missing: list[str] = []
    for t in rows:
        date = str(t.get("trade_date") or "")[:10]
        if not date or (since and date < since) or (until and date > until):
            continue
        stamp = str(t.get("created_at") or "")
        hhmm = stamp[11:16] if len(stamp) >= 16 else ""
        open_px = _open_price(t["code"], date)
        bps = cost_bps(t["side"], float(t["price"] or 0), open_px or 0)
        if bps is None:
            missing.append(f"{date} {t['code']} {t['side']}")
            continue
        samples.append({
            "date": date,
            "time": hhmm,
            "code": t["code"],
            "name": t.get("name"),
            "side": t["side"],
            "price": float(t["price"]),
            "open": open_px,
            "cost_bps": round(bps, 1),
            "reason": t.get("reason"),
            "bucket": bucket_of(hhmm),
        })

    groups: dict[str, dict] = {}
    for key in ("buy", "sell"):
        vals = [s["cost_bps"] for s in samples if s["side"] == key]
        groups[key] = _stats(vals)
    groups["all"] = _stats([s["cost_bps"] for s in samples])
    for _lo, _hi, name in BUCKETS:
        vals = [s["cost_bps"] for s in samples if s["bucket"] == name]
        groups[f"bucket:{name}"] = _stats(vals)
    # 按成交原因再切一刀：止损/止盈是"触发价成交"，与开盘成交不可比（记录 #35）
    reasons: dict[str, list[float]] = {}
    for s in samples:
        s["reason_group"] = reason_group(s.get("reason") or "")
        reasons.setdefault(s["reason_group"], []).append(s["cost_bps"])
    for reason, vals in reasons.items():
        groups[f"reason:{reason}"] = _stats(vals)

    # 估计 slippage 只用「开盘 30 分内的非触发单」：
    # 止损/止盈是按触发价成交（天然远离开盘价），拿它算滑点会得出 +200bp 的假数。
    basis = [
        s["cost_bps"] for s in samples
        if s["bucket"] == "开盘30分" and s["reason_group"] not in ("止损触发", "止盈触发")
    ]
    groups["suggest_basis"] = _stats(basis)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "samples": len(samples),
        "missing_open": missing,
        "groups": groups,
        "suggested_slippage": _suggest(groups),
        "trades": samples,
    }


def _suggest(groups: dict) -> dict:
    """把「开盘 30 分」的中位成本当作 `slippage` 建议值（这一组最接近回测假设）。

    样本不足 30 笔时只给参考，不作为改默认值的依据——与验收协议同一条纪律。
    """
    base = groups.get("suggest_basis") or groups.get("bucket:开盘30分") or groups.get("all") or {}
    if not base.get("n"):
        return {"value": None, "note": "无样本"}
    value = round(max(0.0, base["median"]) / 10000, 5)
    note = "样本充足，可作为 slippage 默认值" if base["n"] >= 30 else (
        f"样本仅 {base['n']} 笔，只作参考；满 30 笔再定默认值"
    )
    return {"value": value, "basis": base, "note": note}


def _print_report(data: dict) -> None:
    print(f"\n成交偏离审计（{data['generated_at']}）｜样本 {data['samples']} 笔\n")
    print("| 分组 | 笔数 | 中位成本 | 均值 | 90 分位 | 吃亏占比 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for key, label in (
        ("all", "全部"), ("buy", "买入"), ("sell", "卖出"),
        ("bucket:开盘30分", "开盘30分"), ("bucket:上午盘中", "上午盘中"),
        ("bucket:下午盘中", "下午盘中"), ("bucket:尾盘", "尾盘"),
    ):
        st = data["groups"].get(key) or {}
        if not st.get("n"):
            print(f"| {label} | 0 | n/a | n/a | n/a | n/a |")
            continue
        print(
            f"| {label} | {st['n']} | {st['median']:+.1f} bp | {st['mean']:+.1f} bp "
            f"| {st['p90']:+.1f} bp | {st['share_costly']:.1f}% |"
        )
    total = (data["groups"].get("all") or {}).get("n") or 0
    opening = (data["groups"].get("bucket:开盘30分") or {}).get("n") or 0
    if total:
        print(
            f"\n成交时点：开盘 30 分内 {opening}/{total}（{opening / total * 100:.0f}%），"
            f"其余为延迟成交——这部分偏差是**时点差**（回测假设开盘成交），不是纯滑点"
        )

    reasons = {
        k.split(":", 1)[1]: v for k, v in data["groups"].items()
        if k.startswith("reason:") and (v.get("n") or 0) >= 2
    }
    if reasons:
        print("\n按成交原因（止损/止盈是触发价成交，与开盘成交不同口径）\n")
        print("| 原因 | 笔数 | 中位成本 | 均值 | 吃亏占比 |")
        print("| --- | --- | --- | --- | --- |")
        for reason, st in sorted(reasons.items(), key=lambda kv: -kv[1]["n"]):
            print(
                f"| {reason} | {st['n']} | {st['median']:+.1f} bp | {st['mean']:+.1f} bp "
                f"| {st['share_costly']:.1f}% |"
            )
    sug = data["suggested_slippage"]
    basis = sug.get("basis") or {}
    print(
        f"\n建议 `slippage`：{sug.get('value')}"
        f"（基数 = 开盘 30 分内的非触发单 {basis.get('n', 0)} 笔，中位 "
        f"{basis.get('median', 0):+.1f} bp；{sug.get('note')}）"
    )
    if data["missing_open"]:
        print(f"\n缺当日开盘价、已跳过 {len(data['missing_open'])} 笔："
              + ", ".join(data["missing_open"][:5]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="", help="起始交易日（含）")
    parser.add_argument("--until", default="", help="截止交易日（含）")
    parser.add_argument("--tag", default="", help="结果文件名后缀")
    args = parser.parse_args()

    data = audit(args.since, args.until)
    _print_report(data)

    log_dir = str(get_settings().kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    out = os.path.join(log_dir, f"slippage_audit{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print(f"\n结果已保存：{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
