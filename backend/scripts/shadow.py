"""影子账户：用**冻结参数**每天在真实行情上重跑一遍，只记录、不交易。

为什么需要它：`BACKTEST_LOG` 记录 #15~#17 的结论是「参数在样本内怎么调都不可信」，
唯一能判定 `e30_a15`（1.5×ATR 止损）那 +2.7 的样本外优势是真信号还是噪声的办法，
是**向前看**的样本外 —— 每天用当天收盘后的数据重算，逐日累积超额。

与线上模拟盘的关系：**完全隔离**。不读写 `paper_*` 表、不改 `app/config.py`、
不影响定时任务；参数从 `app/backtest/profiles.py` 取，结果只落
`data/logs/shadow_<tag>.json`。想停随时停，删文件即可。

做法：固定一个起始日 ``--start``，每天收盘后跑一次 ``--start ~ 今天``，
把当天的净值/超额/持仓写成一条快照（同一天重跑会覆盖，幂等）。
曲线本身由多次快照拼出来，等价于「参数冻结后的真实前向表现」。

用法（在 backend 目录）：
    # 每个交易日收盘后跑一次（建议放调度里，如 15:30）
    .venv\\Scripts\\python.exe scripts\\shadow.py --start 2026-11-02 --tag live
    # 只看当前累积结果
    .venv\\Scripts\\python.exe scripts\\shadow.py --start 2026-11-02 --tag live --report
    # 换一组对照方案
    .venv\\Scripts\\python.exe scripts\\shadow.py --start 2026-11-02 --tag live \\
        --profiles default,e30_a15,e30_a2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.pipeline import PipelineParams, _run_sync  # noqa: E402
from app.backtest.profiles import PROFILES  # noqa: E402
from app.config import get_settings  # noqa: E402

#: 默认对照：线上参数 vs 两个候选
#: - e30_a15：1.5×ATR 止损（4 段样本外全正，无衰减）
#: - e32_b  ：大盘分档 + 双线之下清仓（8 窗口绝对收益与超额同时改善，但 t≈1.5 不显著）
DEFAULT_PROFILES = "default,e30_a15,e32_b"


def _shadow_path(tag: str) -> str:
    log_dir = str(get_settings().kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"shadow_{tag}.json")


def _load(path: str) -> dict:
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"tag": "", "start": "", "universe": None, "profiles": {}}


def _save(path: str, data: dict) -> None:
    """原子写：中断不会留下半个 JSON。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=float)
    os.replace(tmp, path)


def _snapshot(name: str, result: dict, end: str) -> dict:
    m = result["metrics"]
    bench = float(m.get("benchmark_return") or 0)
    total = float(m.get("total_return") or 0)
    eq = result["equity_curve"]
    return {
        "as_of": eq[-1]["date"] if eq else end,
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_return": total,
        "benchmark_return": bench,
        "excess": round(total - bench, 2),
        "max_drawdown": float(m.get("max_drawdown") or 0),
        "trade_count": int(m.get("trade_count") or 0),
        "win_rate": float(m.get("win_rate") or 0),
        "equity": float(m.get("final_equity") or 0),
        "open_positions": int(m.get("open_positions") or 0),
        "positions": [
            {
                "code": code,
                "name": p.get("name"),
                "shares": p.get("shares"),
                "cost": p.get("cost"),
                "stop": round(float(p.get("stop") or 0), 3),
            }
            for code, p in (result.get("positions") or {}).items()
        ],
    }


def _check_start(data: dict, start: str, path: str) -> None:
    """起始日一旦定下就冻结：中途改起点会让曲线断裂、超额不可比。"""
    old = data.get("start")
    if old and old != start:
        raise SystemExit(
            f"该 tag 的影子账户起点已是 {old}，与本次 --start {start} 不一致；"
            f"换一个 --tag 或删掉 {path} 重开"
        )


def run(args: argparse.Namespace) -> None:
    path = _shadow_path(args.tag)
    data = _load(path)
    end = args.end or date.today().isoformat()
    _check_start(data, args.start, path)

    names = [n.strip() for n in args.profiles.split(",") if n.strip()]
    unknown = [n for n in names if n not in PROFILES]
    if unknown:
        raise SystemExit(f"未知方案: {unknown}，可选: {list(PROFILES)}")

    data.update({
        "tag": args.tag,
        "start": args.start,
        "universe": args.universe,
        "profiles_used": names,
    })

    print(f"影子账户 {args.tag}：{args.start} ~ {end}，Top{args.universe}，方案 {names}", flush=True)
    for name in names:
        t0 = time.time()
        params = PipelineParams(
            start_date=args.start, end_date=end, universe_size=args.universe, **PROFILES[name]
        )
        try:
            result = _run_sync(params)
        except Exception as exc:  # noqa: BLE001 - 单方案失败不影响其它方案
            print(f"  [{name}] 失败：{type(exc).__name__} {exc}", flush=True)
            continue
        snap = _snapshot(name, result, end)
        # 同一天重跑覆盖，不同天追加 → 一条随时间推进的曲线
        series = data["profiles"].setdefault(name, {"overrides": PROFILES[name], "snapshots": []})
        series["overrides"] = PROFILES[name]
        series["snapshots"] = [
            s for s in series["snapshots"] if s["as_of"] != snap["as_of"]
        ] + [snap]
        series["snapshots"].sort(key=lambda s: s["as_of"])
        print(
            f"  [{name}] {snap['as_of']} 收益 {snap['total_return']:+.2f}% | "
            f"基准 {snap['benchmark_return']:+.2f}% | 超额 {snap['excess']:+.2f}% | "
            f"回撤 {snap['max_drawdown']:.2f}% | {snap['trade_count']} 笔 | "
            f"持仓 {snap['open_positions']} 只 | {time.time() - t0:.0f}s",
            flush=True,
        )
    _save(path, data)
    print(f"\n已写入 {os.path.abspath(path)}", flush=True)
    report(data)


def report(data: dict) -> None:
    """打印累积曲线（每方案最后一条快照 + 与对照方案的差值）。"""
    profiles = data.get("profiles") or {}
    if not profiles:
        print("还没有快照", flush=True)
        return
    latest = {}
    print(f"\n影子账户 {data.get('tag')} | 起点 {data.get('start')} | 当前:", flush=True)
    print("\n| 方案 | 截至 | 收益 | 基准 | 超额 | 回撤 | 笔数 | 胜率 | 快照数 |", flush=True)
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |", flush=True)
    for name, series in profiles.items():
        snaps = series.get("snapshots") or []
        if not snaps:
            continue
        s = snaps[-1]
        latest[name] = s
        print(
            f"| {name} | {s['as_of']} | {s['total_return']:+.2f}% | "
            f"{s['benchmark_return']:+.2f}% | {s['excess']:+.2f}% | "
            f"{s['max_drawdown']:.2f}% | {s['trade_count']} | "
            f"{s['win_rate']:.1f}% | {len(snaps)} |",
            flush=True,
        )
    base = latest.get("default")
    if base and len(latest) > 1:
        print("\n相对 default 的超额差（同一影子起点下才有意义）:", flush=True)
        for name, s in latest.items():
            if name == "default":
                continue
            print(
                f"  {name}: {s['excess'] - base['excess']:+.2f} 个点"
                f"（回撤差 {s['max_drawdown'] - base['max_drawdown']:+.2f}）",
                flush=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="影子起点（冻结，之后不可改）")
    parser.add_argument("--end", default="", help="截止日，缺省=今天")
    parser.add_argument("--universe", type=int, default=300, help="每日成交额 TopN")
    parser.add_argument("--profiles", default=DEFAULT_PROFILES, help="逗号分隔的方案名")
    parser.add_argument("--tag", default="live", help="结果文件名后缀")
    parser.add_argument("--report", action="store_true", help="只打印当前累积结果，不重跑")
    args = parser.parse_args()

    if args.report:
        report(_load(_shadow_path(args.tag)))
        return
    run(args)


if __name__ == "__main__":
    main()
