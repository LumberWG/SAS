"""卖出出口巡检（只读，不改库、不下单、不碰策略参数）。

用法（在 backend 目录下，用项目 venv）：

    .venv/Scripts/python.exe scripts/check_exit_paths.py

逐只持仓核对六条卖出出口：每 tick 止损止盈/时间止损、信号卖出、收盘减仓、
轮动换出、清非交易板块、人工下单。存在「可卖批次已到期却没有任何出口」的
僵尸仓时退出码 1；否则 0。行情价按本地 K 线末根收盘价判定（与引擎
``_latest_price`` 的 K 线回退同源），纯离线也能跑。

与 ``GET /api/paper/exit-audit`` 共用 ``app.paper.exit_audit.audit_positions``，
保证接口与脚本同一口径。本脚本属于只读巡检，不是策略规则，回测侧无需同步。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.data.scope import _allowed_board_prefixes  # noqa: E402
from app.paper.exit_audit import audit_positions  # noqa: E402

_OVERRIDE_KEYS = (
    "twin_t_boards", "paper_auto_trade", "paper_eod_trim_enabled",
    "paper_rotation_enabled", "paper_liquidate_offboard",
    "paper_max_hold_days", "paper_stop_loss", "paper_take_profit",
)


def _coerce(raw: str, default):
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    return str(raw)


def main() -> int:
    s = get_settings()
    db_file: Path = s.db_path
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # 复现线上 app_state 覆盖层（只读查询，不实例化会建表的 cache 单例）
        for key in _OVERRIDE_KEYS:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key=?", ("cfg:" + key,)
            ).fetchone()
            if row and row[0] not in (None, ""):
                try:
                    setattr(s, key, _coerce(row[0], getattr(s, key)))
                except (TypeError, ValueError):
                    pass
        mode = conn.execute(
            "SELECT value FROM app_state WHERE key='paper_trade_mode'"
        ).fetchone()
        auto_trade = (
            bool(s.paper_auto_trade) if mode is None else mode[0] == "auto"
        )

        positions = [dict(r) for r in conn.execute("SELECT * FROM paper_positions")]
        lots_by_code: dict[str, list[dict]] = {}
        for lot in conn.execute("SELECT * FROM paper_position_lots"):
            d = dict(lot)
            lots_by_code.setdefault(str(d["code"]).zfill(6), []).append(d)
        watch_codes = {
            str(r[0]).zfill(6) for r in conn.execute("SELECT code FROM watchlist")
        }
    finally:
        conn.close()

    prices: dict[str, float | None] = {}
    for p in positions:
        code = str(p["code"]).zfill(6)
        f = s.kline_dir / f"{code}.parquet"
        px = None
        if f.exists():
            try:
                df = pd.read_parquet(f)
                if not df.empty and "close" in df.columns:
                    px = float(df["close"].iloc[-1])
            except Exception:  # noqa: BLE001
                px = None
        prices[code] = px

    cfg = {
        "auto_trade": auto_trade,
        "eod_trim": bool(s.paper_eod_trim_enabled),
        "rotation": bool(s.paper_rotation_enabled),
        "liquidate_offboard": bool(s.paper_liquidate_offboard),
        "max_hold_days": int(s.paper_max_hold_days or 0),
        "stop_loss": float(s.paper_stop_loss or 0),
        "take_profit": float(s.paper_take_profit or 0),
    }
    report = audit_positions(
        positions=positions,
        lots_by_code=lots_by_code,
        watch_codes=watch_codes,
        prices=prices,
        today=datetime.now().strftime("%Y-%m-%d"),
        board_prefixes=_allowed_board_prefixes(),
        cfg=cfg,
    )

    print("=" * 78)
    print(
        "卖出出口自检 %s | 自动交易=%s | 持仓 %d 只"
        % (report["date"], "开" if auto_trade else "关", report["count"])
    )
    print("=" * 78)
    print("代码     名称       股数  可卖  建仓日     持有  行情价  自选  出口  状态")
    for i in report["items"]:
        print(
            "%s %-9s %4d %5d  %s  %3d天  %7s  %-3s  %-2d  %s"
            % (
                i["code"], i["name"][:9], i["shares"], i["sellable_shares"],
                i["buy_date"] or "-", i["held_days"],
                f'{i["price"]:.2f}' if i["price"] is not None else "无",
                "是" if i["in_watchlist"] else "否",
                len(i["available_exits"]), i["status"],
            )
        )
        for r in i["risks"]:
            print("        · " + r)
    print("-" * 78)
    for n in report["notes"]:
        print("提示：" + n)
    print(
        "结论：%s | blocked=%s | no_quote=%s"
        % (
            "每只持仓都留有卖出出口" if report["ok"] else "存在无出口持仓",
            report["blocked"] or "无",
            report["no_quote"] or "无",
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
