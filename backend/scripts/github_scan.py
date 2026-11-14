"""GitHub 开源 A 股量化项目：有边界的系统性补查（只研究，零生产写入）。

背景：2026-11-12 只按「创业板短线 / 指数择时」定向看过 6 个仓库，
用户 11-14 追问「GitHub 上都查完了吗」。本脚本用**固定查询集**做一次可复现的盘点，
回答「公开项目里到底有多少给出了、且经得起统一口径审查的可交易规则」。

**统一复现口径（PROTOCOL；任何候选进回测前必须满足，不因项目自称收益而放宽）**
- 数据：本地前复权日 K，**每个交易日**的成交额 Top300 池（不用当前快照，防幸存者偏差）；
- 撮合：T+1 开盘成交、整手、涨跌停不可成交、佣金 + 印花税 + 滑点；
- 风控：与 11-12 的 7 策略对比保持一致 —— MA30 趋势滤网、持仓 8、跟踪止损 6%、时间止损 10 日；
- 窗口：六段不重叠样本外（与七代前向验证基线同口径）；
- 判据：**相对等权基准的超额收益**，不是绝对收益（避免把 beta 当 alpha）；
- 门槛（``docs/analysis/ALPHA_CONCLUSIONS.md:22``）：OOS rank-IC > 0、配对超额中位 > 3.5pp/21 日、
  正占比过半、bootstrap CI 不含 0；
- 直接排除：只报绝对收益 / 无成本模型 / 挑选窗口 / 含未来函数 / 规则无法编码成
  ``Strategy.evaluate(df)`` 前缀切片。

用法：
    .venv\\Scripts\\python.exe scripts/github_scan.py --dry-run
    .venv\\Scripts\\python.exe scripts/github_scan.py --limit 40
产物：D:/Backup/SAS/data/logs/github_scan_2026-11.json
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date
from pathlib import Path

import requests

# 固定查询集：4 次搜索，避免匿名 search API 10 次/分钟限额，也避免「换关键词挑结果」
QUERIES = [
    '"A股" 量化 策略 stars:>30',
    '"A股" 回测 stars:>30',
    "china a-share quant strategy stars:>50",
    "ashare backtest strategy stars:>30",
]

# 11-12 已复现过的 6 个仓库，不再重复计数（但仍列出以便对照）
KNOWN_REPOS = {
    "baili-bl/wechat_-125-",
    "conquerer-yinn/kakaaaa-quant",
    "ling-0729/khunter",
    "percheung/chinaastockmarketrealtimemonitor",
    "robertquant/quant-research",
    "sqyangx/quantlab",
}

PROTOCOL = {
    "data": "本地前复权日K + 逐日成交额 Top300 池（不用当前快照）",
    "execution": "T+1 开盘成交 / 整手 / 涨跌停不可成交 / 佣金+印花税+滑点",
    "risk": "MA30 趋势滤网 / 持仓 8 / 跟踪止损 6% / 时间止损 10 日",
    "windows": "六段不重叠样本外（同七代前向验证基线）",
    "metric": "相对等权基准的超额收益（非绝对收益）",
    "threshold": "OOS rank-IC>0 且 配对超额中位>3.5pp/21日 且 正占比过半 且 bootstrap CI 不含 0",
    "exclude": "只报绝对收益 / 无成本 / 挑窗口 / 含未来函数 / 规则不可编码",
}

KW = {
    "entry": ["买入", "入场", "开仓", "信号", "金叉", "buy signal", "entry", "long when", "选股"],
    "exit": ["卖出", "出场", "平仓", "止损", "止盈", "死叉", "stop loss", "take profit", "exit", "sell when"],
    "cost": ["手续费", "印花税", "滑点", "佣金", "commission", "slippage", "transaction cost"],
    "oos": ["样本外", "前向验证", "walk forward", "out-of-sample", "out of sample", "滚动验证"],
    "framework": ["框架", "数据源", "sdk", "数据接口", "爬虫", "api wrapper", "data source", "可视化", "看板"],
}

RET_RE = re.compile(r"(?:年化|收益|超额|胜率|夏普|return|sharpe)[^\n]{0,30}?([+-]?\d{1,3}(?:\.\d+)?)\s*%", re.I)
OUT_FILE = Path("D:/Backup/SAS/data/logs/github_scan_2026-11.json")


def _hits(text: str, words: list[str]) -> list[str]:
    low = text.lower()
    return [w for w in words if w.lower() in low]


def _readme_text(full_name: str, branch: str, session: requests.Session) -> str:
    for b in (branch, "main", "master"):
        for name in ("README.md", "README.rst", "readme.md"):
            url = f"https://raw.githubusercontent.com/{full_name}/{b}/{name}"
            try:
                r = session.get(url, timeout=8)
                if r.ok and len(r.content) > 200:
                    return r.text[:60000]
            except requests.RequestException:
                continue
    return ""


def _classify(desc: str, readme: str) -> str:
    text = f"{desc}\n{readme}"
    fw = _hits(text, KW["framework"])
    has_entry = bool(_hits(text, KW["entry"]))
    has_exit = bool(_hits(text, KW["exit"]))
    if has_entry and has_exit and len(readme) > 800:
        return "strategy_rules"
    if fw and not (has_entry and has_exit):
        return "framework_or_data"
    if len(readme) < 500:
        return "no_readme"
    return "unclear"


def _snippets(text: str, words: list[str], limit: int = 2) -> list[str]:
    """留证据句，便于人工复核关键词命中的真伪（启发式不能当结论）。"""
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if 20 < len(s) < 240 and any(w.lower() in s.lower() for w in words):
            out.append(s[:220])
        if len(out) >= limit:
            break
    return out


def _audit(desc: str, readme: str) -> dict:
    text = f"{desc}\n{readme}"
    claims = RET_RE.findall(text)[:6]
    return {
        "has_entry_rule": bool(_hits(text, KW["entry"])),
        "has_exit_rule": bool(_hits(text, KW["exit"])),
        "has_cost_model": bool(_hits(text, KW["cost"])),
        "mentions_oos": bool(_hits(text, KW["oos"])),
        "entry_evidence": _snippets(text, KW["entry"]),
        "cost_evidence": _snippets(text, KW["cost"]),
        "oos_evidence": _snippets(text, KW["oos"]),
        "claimed_returns_pct": claims,
        "readme_chars": len(readme),
    }


def _search(q: str, session: requests.Session) -> list[dict]:
    r = session.get(
        "https://api.github.com/search/repositories",
        params={"q": q, "sort": "stars", "order": "desc", "per_page": 50},
        timeout=20,
    )
    if r.status_code == 403:
        raise RuntimeError(f"GitHub 搜索限额耗尽（reset={r.headers.get('X-RateLimit-Reset')}）")
    r.raise_for_status()
    return r.json().get("items", [])


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub 开源 A 股量化项目系统性盘点")
    ap.add_argument("--limit", type=int, default=40, help="最多盘点多少个仓库")
    ap.add_argument("--dry-run", action="store_true", help="只搜索，不抓 README")
    ap.add_argument("--out", default=str(OUT_FILE))
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update({"Accept": "application/vnd.github+json", "User-Agent": "SAS-research"})

    pool: dict[str, dict] = {}
    for i, q in enumerate(QUERIES):
        if i:
            time.sleep(7)  # 匿名 search 10 次/分钟
        try:
            items = _search(q, session)
        except Exception as exc:
            print(f"! 查询失败 {q}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        print(f"查询 {q!r} -> {len(items)} 条")
        for it in items:
            key = it["full_name"].lower()
            if key not in pool:
                pool[key] = {
                    "full_name": it["full_name"],
                    "stars": it["stargazers_count"],
                    "description": (it.get("description") or "")[:200],
                    "language": it.get("language"),
                    "pushed_at": it.get("pushed_at"),
                    "default_branch": it.get("default_branch") or "main",
                    "url": it["html_url"],
                    "already_reviewed": key in KNOWN_REPOS,
                }

    ranked = sorted(pool.values(), key=lambda x: -x["stars"])[: args.limit]
    print(f"合并去重 {len(pool)} 个，按 star 取前 {len(ranked)} 个")

    if not args.dry_run:
        for n, item in enumerate(ranked, 1):
            readme = _readme_text(item["full_name"], item["default_branch"], session)
            item["category"] = _classify(item["description"], readme)
            item["audit"] = _audit(item["description"], readme)
            if n % 10 == 0:
                print(f"  {n}/{len(ranked)}")
            time.sleep(0.15)
    else:
        for item in ranked:
            item["category"] = "not_scanned"
            item["audit"] = {}

    counts: dict[str, int] = {}
    for item in ranked:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    # 通过统一口径审查 = 有入场 + 有出场 + 有成本 + 有样本外，且不是框架/数据
    passed = [
        i for i in ranked
        if i["category"] == "strategy_rules"
        and i["audit"].get("has_cost_model")
        and i["audit"].get("mentions_oos")
    ]
    result = {
        "generated_at": date.today().isoformat(),
        "queries": QUERIES,
        "protocol": PROTOCOL,
        "search_pool_size": len(pool),
        "reviewed": len(ranked),
        "already_reviewed_before": sum(1 for i in ranked if i["already_reviewed"]),
        "category_counts": counts,
        "audit_passed_count": len(passed),
        "audit_passed": [i["full_name"] for i in passed],
        "repositories": ranked,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("分类：", counts)
    print(f"通过统一口径审查（入场+出场+成本+样本外）：{len(passed)} 个")
    for i in passed:
        print("  -", i["full_name"], i["stars"])
    print(f"已知 6 个仓库在榜内：{result['already_reviewed_before']} 个")
    print(f"产物 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
