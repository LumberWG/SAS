"""参数搜索：在多窗口上批量跑 PipelineParams 组合，按「最差窗口超额」排序。

设计要点：
  - 信号（扫描/自选/14因子）与交易参数无关，`_prepare_cached` 每个窗口只算一次，
    之后每个参数组合只跑交易循环（秒级），因此可以一次试几十组。
  - 每个组合都要在**全部窗口**上跑，排序依据是 `min(超额)`——最差窗口的超额，
    避免挑出「某个窗口暴涨、其它窗口崩」的参数。
  - 结果增量落盘（data/logs/search_<tag>.json），中断后重跑会跳过已完成的组合。

用法（在 backend 目录）：
    .venv\\Scripts\\python.exe scripts\\search.py --configs 80 --tag s1
    .venv\\Scripts\\python.exe scripts\\search.py --configs 80 --tag s1 --seed 7
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.pipeline import PipelineParams, _prepare_cached, _simulate  # noqa: E402
from app.config import get_settings  # noqa: E402

#: 窗口名 -> (开始, 结束)。E/F 为 2024 年（K线已补到 2023-07，见记录 #9）。
WINDOWS: dict[str, tuple[str, str]] = {
    "E": ("2024-01-01", "2024-06-30"),
    "F": ("2024-07-01", "2024-12-31"),
    "A": ("2026-09-28", "2026-10-23"),
    "B": ("2026-06-01", "2026-10-23"),
    "C": ("2025-11-01", "2026-05-31"),
    "D": ("2025-08-01", "2025-10-31"),
    # G/H：2025 上半年。此前从未进过任何搜索/回归（D 从 2025-08 才开始），
    # 是唯一干净的样本外区间；连起来可把 2025-01 ~ 2026-10 铺成连续分段。
    "G": ("2025-01-01", "2025-03-31"),
    "H": ("2025-04-01", "2025-07-31"),
    # I/J：2023 年（K 线已回溯到 2022-09，见记录 #17）。同样没进过任何搜索。
    "I": ("2023-01-01", "2023-04-30"),
    "J": ("2023-05-01", "2023-08-31"),
}

#: 样本外检验集：只用来验收，不参与参数搜索。
#: 依据 BACKTEST_LOG 记录 #15——把检验集也拿去搜参等于没有检验集。
HOLDOUT: tuple[str, ...] = ("G", "H", "I", "J")

#: 搜索空间（None = 沿用线上值）
SPACE: dict[str, list] = {
    "buy_threshold": [None, 25, 35],
    "stop_loss": [0.05, 0.06, 0.08, 0.10],
    "take_profit": [0.12, 0.15, 0.20, 0.30],
    "max_positions": [4, 5, 8, 10],
    "single_position_ratio": [0.10, 0.15, 0.20, 0.25],
    "max_total_exposure": [0.60, 0.70, 0.80, 0.95],
    "max_hold_days": [0, 3, 5, 10],
    "breakeven_trigger": [None, 0.08, 0.12],
    "partial_tp_at": [None, 0.10],
    "min_volume_ratio": [None, 1.0],
    "weak_exposure": [(None, None), (20, 0.40), (60, 0.60)],
}

DEFAULT_CFG: dict = {k: None for k in SPACE}


def sample_configs(n: int, seed: int) -> list[dict]:
    """随机采样 + 可行性过滤 + 去重；第一组固定为线上默认。"""
    rng = random.Random(seed)
    out: list[dict] = [dict(DEFAULT_CFG)]
    guard = 0
    while len(out) < n and guard < n * 60:
        guard += 1
        cfg = {k: rng.choice(v) for k, v in SPACE.items()}
        reg, weak = cfg.pop("weak_exposure")
        cfg["regime_ma"] = reg
        cfg["weak_exposure"] = weak
        out.append(cfg)
    return dedupe(out)


def dedupe(cfgs: list[dict]) -> list[dict]:
    """去掉重复组合与不可行组合（持仓数 × 单票比例够不到目标仓位）。"""
    keys = list(SPACE) + ["regime_ma"]
    seen: set[tuple] = set()
    out: list[dict] = []
    for cfg in cfgs:
        sig = tuple("" if cfg.get(k) is None else str(cfg.get(k)) for k in keys)
        if sig in seen:
            continue
        pos = cfg.get("max_positions") or 10
        ratio = cfg.get("single_position_ratio") or 0.20
        expo = cfg.get("max_total_exposure") or 0.80
        if pos * ratio < expo * 0.5:
            continue
        seen.add(sig)
        out.append(cfg)
    return out


def neighbor_configs(tag_path: str, top_n: int) -> list[dict]:
    """取某一轮搜索的 TopN 组合，逐个旋钮做单变量扰动（爬山）。"""
    data = load_progress(tag_path)
    rows = _ranking(data)
    out: list[dict] = []
    for r in rows[:top_n]:
        base = dict(r["cfg"])
        for key, values in SPACE.items():
            if key == "weak_exposure":
                continue  # regime/仓位是成对的，下面单独处理
            for value in values:
                if value == base.get(key):
                    continue
                cfg = dict(base)
                cfg[key] = value
                out.append(cfg)
        for pair in SPACE["weak_exposure"]:
            if pair == (base.get("regime_ma"), base.get("weak_exposure")):
                continue
            cfg = dict(base)
            cfg["regime_ma"], cfg["weak_exposure"] = pair
            out.append(cfg)
    return dedupe(out)


def resolve_windows(
    spec: str, *, allow_holdout: bool = False
) -> dict[str, tuple[str, str]]:
    """解析 ``--windows A,B,C`` 为窗口子集；空串=全部（默认排除样本外窗口 G/H）。

    日常迭代只跑近端窗口（A/B/C/D）节省 1/3 时间，E/F（2024）留给定稿前的
    稳定性复验——它们的作用是覆盖「普跌/普涨」两种市况，不是持仓周期需要。

    ``G``/``H`` 是样本外检验集（见 ``HOLDOUT``），默认**不参与**任何批量运行；
    要用必须显式点名（``--windows G,H``）或传 ``allow_holdout=True``。
    """
    names = [n.strip().upper() for n in str(spec or "").split(",") if n.strip()]
    if not names:
        if allow_holdout:
            return dict(WINDOWS)
        return {k: v for k, v in WINDOWS.items() if k not in HOLDOUT}
    unknown = [n for n in names if n not in WINDOWS]
    if unknown:
        raise SystemExit(f"未知窗口 {unknown}，可选 {list(WINDOWS)}")
    return {n: WINDOWS[n] for n in names}


def _window_names(data: dict) -> list[str]:
    """本次（含历次续跑）跑过的窗口，用于排名时不卡没跑的窗口。

    优先读 ``data["windows"]``（每次 run 都会合并写入）；老结果文件没有这个字段时
    再从结果键里推断，避免把「老格式/别的批次」的窗口算进来导致排名全被跳过。
    """
    saved = data.get("windows") or {}
    names = [w for w in WINDOWS if w in saved]
    if names:
        return names
    done = {k.split("|", 1)[0] for k in (data.get("results") or {})}
    return [w for w in WINDOWS if w in done]


def cfg_id(idx: int, cfg: dict) -> str:
    parts = [f"c{idx:03d}"]
    for k, v in cfg.items():
        if v is None:
            continue
        parts.append(f"{k}={v}")
    return "|".join(parts)


def load_progress(path: str) -> dict:
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"tag": "", "windows": {}, "configs": {}, "results": {}}


def save_progress(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=float)
    os.replace(tmp, path)


def auto_jobs(count: int) -> int:
    """默认并行度：任务数、CPU//2、6 三者取小。

    每个进程要常驻一份 K 线+指标（实测 0.5~1GB），所以上限压到 6；
    16 核 / 32GB 的机器上 6 路正好把 6 个窗口一次跑完。
    """
    cpu = os.cpu_count() or 4
    return max(1, min(count, max(1, cpu // 2), 6))


def _run_batch(task: tuple) -> tuple[str, int, dict]:
    """单个「股票池 × 窗口」任务（可丢进子进程）：跑完该批全部组合。

    数据准备（读 5000+ parquet、算指标、算选股信号）是主要耗时，一个批次做一次；
    所以并行粒度选「股票池 × 窗口」而不是单个组合。

    Returns:
        ``(窗口名, 股票池, {结果键: 指标})``
    """
    wname, start, end, u, configs = task
    prepared = _prepare_cached(
        PipelineParams(start_date=start, end_date=end, universe_size=u)
    )
    out: dict[str, dict] = {}
    for cid, cfg in configs.items():
        key = f"{wname}|u{u}|{cid}"
        # None 一律省略：Optional 字段省略即 None，非 Optional 字段（止损/止盈/
        # 持仓天数）省略才回到 dataclass 默认值，直接传 None 会炸。
        kwargs = {k: v for k, v in cfg.items() if v is not None}
        params = PipelineParams(
            start_date=start, end_date=end, universe_size=u, **kwargs
        )
        try:
            res = _simulate(prepared, params)
        except Exception as exc:  # noqa: BLE001 - 单组失败不中断搜索
            out[key] = {"error": str(exc)}
            continue
        m = res["metrics"]
        out[key] = {
            "return": float(m.get("total_return") or 0),
            "benchmark": float(m.get("benchmark_return") or 0),
            "excess": round(
                float(m.get("total_return") or 0) - float(m.get("benchmark_return") or 0), 2
            ),
            "max_drawdown": float(m.get("max_drawdown") or 0),
            "sharpe": float(m.get("sharpe") or 0),
            "trades": int(m.get("trade_count") or 0),
            "win_rate": float(m.get("win_rate") or 0),
        }
    return wname, u, out


def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    path = str(settings.db_path.parent / "logs" / f"search_{args.tag}.json")
    data = load_progress(path)
    data["tag"] = args.tag
    data["space"] = {k: [str(v) for v in v] for k, v in SPACE.items()}

    if args.neighbors:
        prev = str(settings.db_path.parent / "logs" / f"search_{args.neighbors}.json")
        configs = neighbor_configs(prev, args.top)
        print(f"从 {args.neighbors} 的 Top{args.top} 生成邻居组合 {len(configs)} 个", flush=True)
    else:
        configs = sample_configs(args.configs, args.seed)
    data["configs"] = {cfg_id(i, c): c for i, c in enumerate(configs)}

    universes = [int(u) for u in str(args.universes).split(",") if u.strip()]
    # 断点续跑时允许分批传不同股票池：合并而不是覆盖，最后一次性排名
    data["universes"] = sorted(set(data.get("universes") or []) | set(universes))
    universes = data["universes"]
    windows = resolve_windows(getattr(args, "windows", ""))
    leaked = [w for w in windows if w in HOLDOUT]
    if leaked:
        print(
            f"警告：{leaked} 是样本外检验集，一旦拿来做搜索/调参就失去检验意义"
            f"（BACKTEST_LOG 记录 #15）",
            flush=True,
        )
    # 与股票池同理：分批跑不同窗口时合并，最后一次性排名
    data["windows"] = {
        **(data.get("windows") or {}), **{k: list(v) for k, v in windows.items()},
    }

    total_runs = len(windows) * len(universes) * len(configs)
    done = sum(
        1
        for w in windows
        for u in universes
        for cid in data["configs"]
        if f"{w}|u{u}|{cid}" in data["results"]
    )
    print(
        f"窗口 {list(windows)} × 股票池 {universes} × 组合 {len(configs)} "
        f"= {total_runs} 次；已完成 {done}",
        flush=True,
    )

    # 断点续跑：按「股票池 × 窗口」切任务，该批全做完就直接跳过（连数据准备都不做）
    tasks = []
    for u in universes:
        for wname, (start, end) in windows.items():
            pending = {
                cid: cfg for cid, cfg in data["configs"].items()
                if not (
                    data["results"].get(f"{wname}|u{u}|{cid}")
                    and "error" not in data["results"][f"{wname}|u{u}|{cid}"]
                )
            }
            if pending:
                tasks.append((wname, start, end, u, pending))

    jobs = getattr(args, "jobs", 0) or auto_jobs(len(tasks))
    jobs = max(1, min(jobs, len(tasks)))
    print(f"待跑批次 {len(tasks)}（每批=一个股票池×窗口），并行度 {jobs}", flush=True)

    t_all = time.time()
    if jobs == 1:
        for task in tasks:
            wname, u, batch = _run_batch(task)
            data["results"].update(batch)
            save_progress(path, data)
            print(f"[{wname}/Top{u}] 完成 {len(batch)} 组", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for wname, u, batch in pool.map(_run_batch, tasks):
                data["results"].update(batch)
                save_progress(path, data)
                print(f"[{wname}/Top{u}] 完成 {len(batch)} 组", flush=True)
    if tasks:
        print(f"全部批次完成，用时 {time.time() - t_all:.0f}s", flush=True)

    report(data)


def _ranking(data: dict) -> list[dict]:
    """按「最差(窗口×股票池)超额」→「平均收益」→「最差回撤」排序。

    要求组合在**所有窗口 × 所有股票池规模**上都有结果，否则不参与排名——
    只在某个股票池上有效的参数属于过拟合（见记录 #7 的 auto_v1）。
    """
    unis = data.get("universes") or [None]  # None = 老格式（单一股票池）
    windows = _window_names(data) or list(WINDOWS)
    rows = []
    for cid, cfg in data["configs"].items():
        per: dict[str, dict] = {}
        missing = False
        for u in unis:
            for w in windows:
                label = f"{w}/u{u}" if u else w
                key = f"{w}|u{u}|{cid}" if u else f"{w}|{cid}"
                r = data["results"].get(key)
                if not r or "error" in r:
                    missing = True
                    break
                per[label] = r
            if missing:
                break
        if missing:
            continue
        excess = [v["excess"] for v in per.values()]
        rets = [v["return"] for v in per.values()]
        dds = [v["max_drawdown"] for v in per.values()]
        rows.append({
            "cid": cid, "cfg": cfg, "cols": list(per),
            "min_excess": min(excess), "mean_excess": sum(excess) / len(excess),
            "mean_ret": sum(rets) / len(rets), "worst_dd": min(dds),
            "per": per,
        })
    rows.sort(key=lambda r: (-r["min_excess"], -r["mean_ret"], -r["worst_dd"]))
    return rows


def report(data: dict) -> None:
    """按「最差窗口超额」排序打印。"""
    rows = _ranking(data)
    if not rows:
        print("没有完成的组合", flush=True)
        return

    top = 15
    print("\n| 排名 | 最差超额 | 平均超额 | 平均收益 | 最差回撤 | 组合 |", flush=True)
    print("| --- | --- | --- | --- | --- | --- |", flush=True)
    for i, r in enumerate(rows[:top], 1):
        print(
            f"| {i} | {r['min_excess']:+.2f}% | {r['mean_excess']:+.2f}% "
            f"| {r['mean_ret']:+.2f}% | {r['worst_dd']:.2f}% | {r['cid']} |",
            flush=True,
        )

    cols = rows[0]["cols"]
    print("\n### 各窗口×股票池：收益 / 超额 / 回撤", flush=True)
    print("| 排名 | 组合 | " + " | ".join(cols) + " |", flush=True)
    print("| --- | --- | " + " | ".join("---" for _ in cols) + " |", flush=True)
    for i, r in enumerate(rows[:top], 1):
        cells = [
            f"{r['per'][c]['return']:+.2f}% / {r['per'][c]['excess']:+.2f}% / "
            f"{r['per'][c]['max_drawdown']:.1f}%"
            for c in cols
        ]
        print(f"| {i} | {r['cid']} | " + " | ".join(cells) + " |", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configs", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--universes", default="300,200", help="逗号分隔的股票池规模")
    parser.add_argument("--tag", default="s1")
    parser.add_argument("--neighbors", default="", help="从该 tag 的结果生成单变量邻居组合")
    parser.add_argument("--top", type=int, default=3, help="邻居模式取前 N 名")
    parser.add_argument(
        "--windows", default="",
        help="逗号分隔的窗口子集（如 A,B,C,D），缺省=全部但**排除样本外 G/H**",
    )
    parser.add_argument(
        "--jobs", type=int, default=0,
        help="并行批次（股票池×窗口）数，0=自动（见 auto_jobs），1=串行",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
