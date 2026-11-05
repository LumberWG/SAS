"""一键导出「建仓理由」复盘报告（BACKTEST_LOG #115 配套工具）。

把持仓批次（lot）里记录的决策溯源 provenance 渲染成可读的 Markdown / JSON，
方便盘后复盘「这只票为什么在这个时点买入」「当时的 regime / 信号 / 止损是多少」。

provenance 由 engine._build_provenance 在建仓时一次性写入 paper_position_lots.provenance，
本脚本只读取与渲染，不改任何数据。

用法（在 backend 目录下，用项目的 venv python 运行）：
    # 导出全部持仓批次的建仓理由到 Markdown（打印到终端）
    .venv/Scripts/python.exe scripts/export_lot_reason.py

    # 只看某只票（可重复 --code）
    .venv/Scripts/python.exe scripts/export_lot_reason.py --code 600519 --code 000001

    # 导出成文件（复盘留存）
    .venv/Scripts/python.exe scripts/export_lot_reason.py --out 建仓理由_2026-11-05.md

    # 原始 JSON（给其他脚本/前端二次加工）
    .venv/Scripts/python.exe scripts/export_lot_reason.py --json

数据源：与线上模拟盘同一份 SQLite（settings.db_path，默认 D:/Backup/SAS/data/sas.db）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# 让脚本可在 backend 目录下直接运行（python scripts/export_lot_reason.py）
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.data.cache import Database  # noqa: E402

# ---- 标签映射（与前端 PaperTrading.vue 保持一致）----
TRIGGER_LABEL = {
    "fresh_buy": "自动买入",
    "rotation_buyin": "轮动换入",
    "manual": "手动买入",
    "t_buyback": "做T买回",
    "t_add": "做T加仓",
}
TAG_LABEL = {"base": "底仓", "t_add": "做T加仓", "t_buyback": "做T买回"}
REGIME_LABEL = {
    "normal": "正常",
    "calm": "平稳",
    "weak": "弱势",
    "shock": "震荡",
    "crisis": "危机",
    "managed_downtrend": "托底型阴跌",
}


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def render_lot(lot: dict) -> str:
    """把一个 lot 渲染成 Markdown 小节。"""
    code = lot.get("code", "?")
    name = lot.get("name") or ""
    tag = TAG_LABEL.get(lot.get("tag", "base"), lot.get("tag", "base"))
    buy_date = lot.get("buy_date", "")
    cost = lot.get("cost_price")
    shares = lot.get("shares")
    prov = lot.get("provenance")
    lines = [f"#### {tag} · 买入日 {buy_date}"]
    meta = []
    if cost is not None:
        meta.append(f"成本 {cost:.2f}")
    if shares is not None:
        meta.append(f"{shares} 股")
    if meta:
        lines.append("> " + " · ".join(meta))

    if not prov:
        lines.append("\n_该批次建仓时未记录溯源（provenance 为空，多见于 2026-11-05 之前的老批次）。_")
        return "\n".join(lines)

    if isinstance(prov, str):
        try:
            prov = json.loads(prov)
        except Exception:
            prov = None
    if not isinstance(prov, dict):
        lines.append("\n_该批次溯源数据无法解析。_")
        return "\n".join(lines)

    trigger = TRIGGER_LABEL.get(prov.get("trigger", ""), prov.get("trigger", ""))
    regime = prov.get("regime", {}) or {}
    state = REGIME_LABEL.get(regime.get("state", ""), regime.get("state", ""))
    lines.append(f"\n- **触发方式**：{trigger}")
    lines.append(f"- **建仓时机制态**：{state}")
    sd = prov.get("stop_distance")
    sp = prov.get("stop_price")
    if sd is not None:
        lines.append(f"- **止损距离**：{_fmt_pct(sd)}" + (f"（线 {sp}）" if sp is not None else ""))
    signals = prov.get("signals") or []
    if signals:
        lines.append("- **触发信号**：")
        for s in signals:
            lines.append(f"  - {s}")
    rationale = prov.get("rationale")
    if rationale:
        lines.append(f"- **一句话理由**：{rationale}")
    return "\n".join(lines)


def render_markdown(lots: list[dict]) -> str:
    """把一批 lot 按股票归并，渲染成完整报告。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(lots)
    # 按 code 归并
    by_code: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for l in lots:
        c = l.get("code", "?")
        by_code.setdefault(c, []).append(l)
        names[c] = l.get("name") or names.get(c) or ""

    out = [
        "# 模拟盘建仓理由复盘",
        "",
        f"> 生成时间：{now}  ·  共 {total} 个批次 / {len(by_code)} 只股票",
        "",
    ]
    if not lots:
        out.append("_当前没有任何建仓批次记录。_")
        return "\n".join(out)

    for code in sorted(by_code):
        nm = names.get(code, "")
        out.append(f"## {code} {nm}".rstrip())
        out.append("")
        for lot in by_code[code]:
            out.append(render_lot(lot))
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="导出模拟盘建仓理由复盘（Markdown / JSON）")
    ap.add_argument("--code", action="append", default=[], help="只看指定股票（可重复）。缺省=全部批次")
    ap.add_argument("--out", help="输出到文件（Markdown）。缺省=打印到终端")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON（含每个 lot 的 provenance）")
    args = ap.parse_args()

    db = Database()
    codes = args.code or [None]
    lots: list[dict] = []
    for c in codes:
        lots.extend(db.list_paper_lots(code=c))

    if args.json:
        text = json.dumps(lots, ensure_ascii=False, indent=2, default=str)
    else:
        text = render_markdown(lots)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已写出 {len(lots)} 个批次到 {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
