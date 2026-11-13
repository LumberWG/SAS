"""一键校验「回测 ↔ 实盘」是否严格对齐（零漂移时退出码 0）。

用法（在 backend 目录下，务必用项目 venv）：

    .venv/Scripts/python.exe scripts/check_backtest_alignment.py

约定（2026-11-12）：**后续所有更改，回测部分和实盘部分都必须同步。**
因此每次改动 `Settings` / `PipelineParams` / `_defaults_payload()` / `BACKTEST_TO_SETTING`
之后都跑一遍；有漂移时退出码 1，可直接接进重启脚本自检或 CI。
"""
from __future__ import annotations

import os
import sys

# 允许从任意工作目录直接执行（脚本自身在 backend/scripts/ 下）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.alignment import audit_alignment  # noqa: E402


def main() -> int:
    report = audit_alignment()
    s = report["summary"]
    print("=" * 62)
    print("回测 ↔ 实盘 对齐自检")
    print("=" * 62)
    print(
        f"  跟随实盘的参数 : {s['bound']} 项（逐项运行时探针验证）\n"
        f"  回测默认值字段 : {s['defaults_fields']} 项\n"
        f"  PipelineParams : {s['pipeline_fields']} 项\n"
        f"  运行时自动兜底 : {s['auto_fallback']} 项\n"
        f"  回测专属(豁免) : {s['research_only']} 项\n"
        f"  可双向落盘     : {s['persist_mapped']} 项\n"
        f"  股票池 ST 口径 : {'一致' if s['scope_parity'] == 'ok' else '不一致'}\n"
        f"  止损门控口径   : {'一致' if s['stop_parity'] == 'ok' else '不一致'}\n"
        f"  P1 上下文闸口径 : {'一致' if s['context_parity'] == 'ok' else '不一致'}\n"
        f"  漂移           : {s['drifts']} 项"
    )
    cov = s.get("context_coverage") or {}
    if cov and not cov.get("error"):
        print(
            f"  ── 上下文快照（策略实际读到的数据，非实时接口）──\n"
            f"     事件 {cov.get('events', 0)} 条 / 舆情 {cov.get('news', 0)} 条 / "
            f"热度 {cov.get('heat_days', 0)} 天"
            f"（{cov.get('heat_from')} ~ {cov.get('heat_to')}）"
        )
        if not cov.get("heat_days"):
            print("     ⚠ 热度分位为空：过热子闸两侧一致地不触发，"
                  "先跑 scripts/fetch_context_snapshots.py --build-heat")
    print("-" * 62)
    if report["ok"]:
        print("  ✅ 零漂移：打开回测不改任何项，跑的就是实盘那套规则。")
        print("=" * 62)
        return 0

    print(f"  ⚠ 发现 {len(report['drifts'])} 处漂移，必须修复后再上线：")
    for d in report["drifts"]:
        print(f"  [{d['kind']}] {d['key']}")
        print(f"      {d['detail']}")
    print("=" * 62)
    print("修复顺序：补 _defaults_payload() + alignment.BINDINGS / 白名单，"
          "再补 apply.BACKTEST_TO_SETTING，最后重跑本脚本。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
