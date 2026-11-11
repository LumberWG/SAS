"""创业板「从底部起来」候选扫描。

数据来源：本地日线缓存 (kline_dir 下每只股票一个 parquet，列含 date/open/close/high/low...)。
全样本遍历创业板 (300/301) 股票，计算：
  - ret20        近 20 交易日涨幅 (last / close[-21] - 1)
  - start_pos    上涨起点位置：20 天前收盘价落在近 120 日价格区间的相对位置
                 (0=区间最低, 1=区间最高)；越低越说明「从底部起来」
  - gain_from_low 距近 120 日最低点的累计涨幅 (last / 120low - 1)
  - 分类标签: 底部反转(start_pos<=0.33) / 中位 / 高位延续(start_pos>=0.67)

结果可写入 data/bottom_reversal_cache.json，供 API / 面板每日读取，避免重复扫描。
"""
from __future__ import annotations

import os
import json
import glob
import datetime as dt

import pandas as pd

from app.config import get_settings
from app.indicators.technical import add_all_indicators
from app.analysis.factors import compute_factors
from app.analysis.board_index import classify_board, board_index_symbol, board_index_name

_CANDIDATE_CACHE = "bottom_reversal_cache.json"


def _code_name_map() -> dict:
    """code->name 映射，失败回退空字典（不影响数值计算）。"""
    try:
        from app.data.service import get_stock_pool
        pool = get_stock_pool()
        if hasattr(pool, "itertuples"):  # pandas DataFrame
            return {str(r.code): (r.name or "") for r in pool.itertuples()}
        if isinstance(pool, list):
            return {str(r.get("code", "")): (r.get("name") or "") for r in pool}
        return {}
    except Exception:
        return {}


def compute_all(lazy_factors: bool = True) -> list[dict]:
    """遍历日线缓存（按 bottom_boards 白名单多板块），返回每只股票的指标 dict 列表（已剔除 ST/退）。

    每只票按代码前缀分类到板块（创业板/上证主板/深证主板/科创板/北交所），
    并打上 ``board`` / ``board_index`` / ``board_index_name`` 标签，供后续「板块感知」趋势过滤。

    ``lazy_factors=True``（默认）：14 因子分只对通过 4 个几何门槛的票计算。因子分仅用于
    候选排序/软闸，未过门槛的票本就会被 ``scan_candidates`` 跳过，因此结果完全等价，
    但多板块扫描下可省掉数千次 ``add_all_indicators`` 重活（全量刷新从数分钟降到数十秒）。
    需要全市场因子分明细时传 ``False`` 回到旧行为。
    """
    s = get_settings()
    kdir = str(s.kline_dir)
    boards = {b.strip() for b in (s.bottom_boards or "").split(",") if b.strip()}
    names = _code_name_map()
    rows: list[dict] = []
    for f in glob.glob(os.path.join(kdir, "*.parquet")):
        code = os.path.splitext(os.path.basename(f))[0]
        board = classify_board(code)
        if board not in boards:
            continue
        try:
            df = pd.read_parquet(f)
        except Exception:
            continue
        if len(df) < 130:
            continue
        df = df.sort_values("date")
        close = df["close"].astype(float).to_numpy()
        dates = df["date"].to_numpy()
        if len(close) < 130:
            continue
        last = float(close[-1])
        base20 = float(close[-21])
        ret20 = last / base20 - 1.0 if base20 > 0 else None
        win = close[-120:]
        lmin = float(win.min())
        hmax = float(win.max())
        if hmax == lmin:
            start_pos = 0.5
        else:
            start_pos = (float(close[-21]) - lmin) / (hmax - lmin)
        gain_from_low = last / lmin - 1.0 if lmin > 0 else None
        # 新增：相对 20 日收盘高点的回撤（≤0 表示仍在创新高，>0 表示已从高位回落）
        roll_max20 = float(win[-20:].max()) if len(win) >= 20 else float(win.max())
        dd20 = (1.0 - last / roll_max20) if roll_max20 > 0 else None
        # 新增：当日换手率（成交活跃度）
        try:
            turnover = float(df["turnover_rate"].to_numpy()[-1])
        except Exception:
            turnover = None
        # 新增：20 日均成交额（成交量不能太低——过滤僵尸股）
        try:
            amt = df["amount"].astype(float).to_numpy()[-20:]
            avg_amount20 = float(amt.mean()) if len(amt) else None
        except Exception:
            avg_amount20 = None
        name = names.get(code, "")
        if ("ST" in name) or ("退" in name):
            continue
        # 14 因子分（二次过滤用）：对完整指标 df 算多因子加权总分（-100~100）。
        # 底反票因子分天然偏低，此处仅作"质量排序 + 软闸"，不要求转多。
        # 延迟计算：因子分只用于候选排序/软闸，而候选必须先过下面 4 个便宜门槛；
        # 未过门槛的票本就会被 scan_candidates 跳过，不必为它跑指标+因子重活。
        geo_pass = (
            dd20 is not None and turnover is not None and avg_amount20 is not None
            and start_pos <= float(s.bottom_start_pos_max)
            and dd20 >= float(s.bottom_dd20_min)
            and turnover >= float(s.bottom_turnover_min)
            and avg_amount20 >= float(s.bottom_min_avg_amount)
        )
        factor_score = None
        if not lazy_factors or geo_pass:
            try:
                d2 = add_all_indicators(df)
                factor_score = round(float(compute_factors(d2).total), 2)
            except Exception:
                factor_score = None
        rows.append({
            "code": code,
            "name": name,
            "board": board,
            "board_index": board_index_symbol(board),
            "board_index_name": board_index_name(board),
            "ret20": round(ret20, 4) if ret20 is not None else None,
            "start_pos": round(float(start_pos), 3),
            "gain_from_low": round(gain_from_low, 4) if gain_from_low is not None else None,
            "dd20": round(dd20, 4) if dd20 is not None else None,
            "turnover": round(turnover, 4) if turnover is not None else None,
            "avg_amount20": round(avg_amount20, 2) if avg_amount20 is not None else None,
            "factor_score": factor_score,
            "last": round(last, 2),
            "low120": round(lmin, 2),
            "date": str(dates[-1]),
        })
    return rows


def _classify(start_pos: float) -> str:
    if start_pos <= 0.33:
        return "底部反转"
    if start_pos >= 0.67:
        return "高位延续"
    return "中位"


def scan_candidates(
    rows: list[dict] | None = None,
    *,
    start_pos_max: float = 0.40,
    dd20_min: float = 0.15,
    turnover_min: float = 5.0,
    min_avg_amount: float = 50_000_000.0,
    min_factor_score: float = -100.0,
    board_uptrend: dict[str, set[str] | None] | None = None,
) -> list[dict]:
    """筛选「从底部起来」候选（收紧口径 + 14 因子二次过滤）。

    start_pos_max   起点位置上限：20 天前收盘价在近 120 日区间的相对位置 <= 此值（低位启动）
    dd20_min        相对 20 日收盘高点的回撤下限 >= 此值（已从高位回落、处在低位，
                    如 0.15 = 较 20 日高点跌 15% 以上）
    turnover_min    当日换手率下限 >= 此值（成交活跃，过滤冷门僵尸股，如 5.0 = 5%）
    min_avg_amount  20 日均成交额下限 >= 此值（成交量不能太低，默认 5000 万）
    min_factor_score 14 因子分软闸：候选需 因子分 >= 此值（默认 -100=关闭）。
                    底反票因子分天然偏低（实测均值≈-30），此闸只剔除"仍在深度恶化"的票，
                    切勿设为正值，否则候选几乎清空。
    """
    if rows is None:
        rows = compute_all()
    out = []
    for r in rows:
        sp = r.get("start_pos")
        dd = r.get("dd20")
        to = r.get("turnover")
        amt = r.get("avg_amount20")
        if sp is None or dd is None or to is None or amt is None:
            continue
        if sp > start_pos_max:
            continue
        if dd < dd20_min:
            continue
        if to < turnover_min:
            continue
        if amt < min_avg_amount:
            continue
        # 14 因子分软闸（默认 -100 表示关闭）
        if min_factor_score > -100:
            fs = r.get("factor_score")
            if fs is None or fs < min_factor_score:
                continue
        # ---- 板块感知趋势过滤（Layer1）：每只票用「自己板块的指数」判上升通道 ----
        # board_uptrend 为 None 表示未开启趋势过滤；非 None 时按个股所属板块过滤。
        # 板块无指数数据（如北交所）= None → 不阻断（available=False），有数据却不在上升通道 → 剔除。
        board = r.get("board")
        br_uptrend = None
        if board_uptrend is not None and board in board_uptrend:
            ups = board_uptrend[board]
            if ups is None:
                br_uptrend = None
            else:
                br_uptrend = (r.get("date") in ups)
                if not br_uptrend:
                    continue
        c = dict(r)
        c["cls"] = _classify(sp)
        c["board"] = board
        c["board_index"] = r.get("board_index")
        c["board_index_name"] = r.get("board_index_name")
        c["board_uptrend"] = br_uptrend
        c["start_pos_pct"] = round(sp * 100, 1)
        c["dd20_pct"] = round(dd * 100, 1)
        c["turnover_pct"] = round(to, 2)   # turnover_rate 已是百分比数值(如 8.61=8.61%)，不再乘100
        c["avg_amount20_wan"] = round(amt / 1e4, 1)   # 万元
        c["factor_score"] = r.get("factor_score")
        if r.get("ret20") is not None:
            c["ret20_pct"] = round(r["ret20"] * 100, 1)
        if r.get("gain_from_low") is not None:
            c["gain_from_low_pct"] = round(r["gain_from_low"] * 100, 1)
        out.append(c)
    # 优先按 14 因子分降序（质量/企稳信号最好在前），其次低位优先、回撤深优先
    out.sort(key=lambda x: (-(x.get("factor_score") or -1e9), x["start_pos"], -x["dd20"]))
    return out


def cache_path() -> str:
    s = get_settings()
    base = os.path.dirname(str(s.kline_dir).rstrip("/\\")) or "."
    return os.path.join(base, _CANDIDATE_CACHE)


def write_cache() -> dict:
    """实时扫描并写入缓存，返回 {generated_at, count, candidates, 趋势状态...}。

    趋势过滤：创业板指（默认官方 399006，见 bottom_trend_index）处于上升通道（> MA）的交易日才出候选。
    盘后扫描按当日收盘判定一次存进 payload（uptrend_today）；API 读时还会按「当前设置 + 盘中实时指数」
    再覆写一次（盘中翻多即解锁候选），遵循最新开关。
    """
    s = get_settings()
    kdir = str(s.kline_dir)
    ma = int(s.bottom_trend_ma)
    trend_index = (s.bottom_trend_index or "").strip()
    boards = [b.strip() for b in (s.bottom_boards or "").split(",") if b.strip()]

    rows = compute_all()

    # ---- 板块感知上升通道集合（每个白名单板块各算一份，Layer1）----
    board_uptrend: dict[str, set[str] | None] = {}
    board_trends: list[dict] = []
    if s.bottom_trend_filter:
        from app.backtest.engine import compute_cyb_uptrend_dates
        from app.analysis.board_index import board_regime
        for b in boards:
            if b == "cyb" and trend_index:
                # 用户指定官方创业板指（如 sz399006）：用官方指数
                up = compute_cyb_uptrend_dates(kdir, ma, index_symbol=trend_index)
            elif b == "cyb":
                # 默认：300/301 等权合成指数（回测验证更优，+8.49%）
                up = compute_cyb_uptrend_dates(kdir, ma, index_symbol=None)
            else:
                from app.analysis.board_index import board_uptrend_set
                up = board_uptrend_set(kdir, b, ma)
            board_uptrend[b] = up
            rg = board_regime(b, kdir, ma)
            as_of0 = rows[0]["date"] if rows else ""
            board_trends.append({
                "board": b,
                "name": rg.get("name") or b,
                "index": rg.get("index"),
                "available": rg.get("available", False),
                "state": rg.get("state"),
                "dev_pct": rg.get("dev_pct"),
                "uptrend_today": (as_of0 in up) if up is not None else None,
            })
    else:
        board_uptrend = None  # 趋势过滤关闭：不阻断任何板块

    cands = scan_candidates(
        rows,
        start_pos_max=s.bottom_start_pos_max,
        dd20_min=s.bottom_dd20_min,
        turnover_min=s.bottom_turnover_min,
        min_avg_amount=s.bottom_min_avg_amount,
        min_factor_score=s.bottom_min_factor_score,
        board_uptrend=board_uptrend,
    )
    as_of = cands[0]["date"] if cands else (rows[0]["date"] if rows else "")

    # 兼容字段：取首个有数据板块的 uptrend_today（面板横幅/旧逻辑用）
    uptrend_today = None
    for bt in board_trends:
        if bt.get("uptrend_today") is not None:
            uptrend_today = bt["uptrend_today"]
            break
    # trend_blocked：趋势过滤开启，且「有数据的板块全部处于下降通道」→ 候选被清空
    avail = [bt for bt in board_trends if bt.get("uptrend_today") is not None]
    all_down = bool(avail) and all(not bt["uptrend_today"] for bt in avail)
    trend_blocked = bool(s.bottom_trend_filter) and all_down

    payload = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "as_of": as_of,
        "count": len(cands),
        "candidates": cands,
        "criteria": {
            "start_pos_max": s.bottom_start_pos_max,
            "dd20_min": s.bottom_dd20_min,
            "turnover_min": s.bottom_turnover_min,
            "min_avg_amount": s.bottom_min_avg_amount,
            "min_factor_score": s.bottom_min_factor_score,
        },
        "trend_filter": bool(s.bottom_trend_filter),
        "trend_ma": int(s.bottom_trend_ma),
        "trend_index": trend_index,
        "bottom_boards": s.bottom_boards,
        "board_trends": board_trends,
        "time_stop_days": int(s.bottom_time_stop_days),
        "trailing_pct": float(s.bottom_trailing_pct),
        "uptrend_today": uptrend_today,
        "trend_blocked": trend_blocked,
    }
    path = cache_path()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return payload


def read_cache(force_refresh: bool = False, max_age_hours: int = 24) -> dict:
    """读取缓存；缺失/超龄/force_refresh 时实时重算。"""
    path = cache_path()
    if not force_refresh and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            gen = dt.datetime.strptime(data["generated_at"], "%Y-%m-%d %H:%M")
            if (dt.datetime.now() - gen).total_seconds() < max_age_hours * 3600:
                return data
        except Exception:
            pass
    return write_cache()
