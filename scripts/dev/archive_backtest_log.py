"""把 `BACKTEST_LOG.md` 的历史记录按月份归档到 `docs/backtest_log/`，并重建索引。

为什么
------
`BACKTEST_LOG.md` 曾涨到 570 KB / 8464 行 / 122 条记录（两个月），想回答
「这个方向以前试过吗、为什么不采纳」只能从头扫。拆分原则：

- **编号不重置**：`#NN` 全局唯一、跨文件连续（`AGENTS.md` / `WORKLOG.md` / 代码注释里
  已有大量 `#76`、`#108` 这类引用，分卷各自从 #1 开始会让历史引用全部失效）。
- **归档 = 物理搬迁**：正文逐字不动，只是换了存放位置（`只追加不改写` 指的是不改结论）。
- **主文件保留最近 N 条**（默认 10），日常读/追加都在这一个文件里完成。

用法（仓库根目录）
------------------
    python scripts/dev/archive_backtest_log.py            # 只看会做什么
    python scripts/dev/archive_backtest_log.py --apply    # 真的写盘
    python scripts/dev/archive_backtest_log.py --apply --keep 20

幂等：已归档的 `#NN` 不会重复写入分卷；重复运行只补新增部分。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "BACKTEST_LOG.md"
ARCHIVE_DIR = ROOT / "docs" / "backtest_log"

#: `## 记录 #12 · 2026-10-26 · 标题` / `## 记录 #134 — 2026-11-10 · 标题`
#: 历史上有两种残缺：编号带字母后缀（`#72b`）、整条没有日期（`#75`~`#78`）。
#: 编号保留原样（不重编号，否则会破坏既有引用）；缺日期的从正文里捞第一个日期。
REC = re.compile(
    r"^## 记录 #(\d+[A-Za-z]?)\s*[·—–]\s*(?:(\d{4}-\d{2}-\d{2})\s*[·—–]?\s*)?(.*)$")
DATE_ANY = re.compile(r"(\d{4}-\d{2}-\d{2})")

HEADER = """# 回测复盘与调参记录

> **本文件 = 索引 + 最近 {keep} 条记录**。更早的记录按月份归档在 `docs/backtest_log/`，
> 归档只做物理搬迁，正文逐字未改（用 `scripts/dev/archive_backtest_log.py` 复现/续跑）。
> `#NN` 编号**全局唯一、永不重置、跨文件连续**；新增记录追加到本文件末尾，号接着往下排。

## 记录规范

1. **标题格式**：`## 记录 #NN · YYYY-MM-DD · 标题`（NN 全局唯一递增，不要复用已归档的号）。
2. **合并优先**：同一天、同一主题的小实验写成**一条**，内部用 `### 第 N 轮` 分节；
   只有「改变了某个决策 / 参数 / 机制」才单开一条。反例：2026-11-10 一天开了 7 条（#134~#140），
   每条平均 69 行，索引被稀释。典型可合并场景：同一假设的多组参数、先证伪再换周期的连环验证。
3. **必备字段**：条件 / 结果 / 归因 / 建议 / **人工判断**（采纳与否 + 为什么）/ 本轮变更 / 下轮计划。
   结论性的「不采纳」必须写清理由，避免日后重复试验。
4. **只追加不改写**：历史记录的结论不得修改；要修正就新开一条并写明「修正 #NN」。
5. **归档**：每月初执行 `python scripts/dev/archive_backtest_log.py --apply`，
   把超出最近 {keep} 条的记录搬到 `docs/backtest_log/YYYY-MM.md`，索引自动重建。

## 归档

{archive_table}

## 索引

| # | 日期 | 标题 | 位置 |
| --- | --- | --- | --- |
{index_rows}

{notice}---

## 最近记录（{keep} 条）

"""


def _read() -> list[str]:
    return LOG.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")


def _parse(lines: list[str]) -> tuple[int | None, list[dict]]:
    """返回 (第一条记录的行号, 记录列表)。"""
    heads = [(i, REC.match(line)) for i, line in enumerate(lines) if REC.match(line)]
    if not heads:
        return None, []
    recs: list[dict] = []
    for k, (i, m) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        body = lines[i:end]
        while body and not body[-1].strip():      # 去掉段落尾部的空行
            body.pop()
        date = m.group(2) or ""
        if not date:                              # 标题没写日期：从正文捞
            for ln in body[1:40]:
                hit = DATE_ANY.search(ln)
                if hit:
                    date = hit.group(1)
                    break
        if not date and recs:                     # 整条都没有日期：沿用上一条（原文按时间顺序）
            date = recs[-1]["date"]
        recs.append({
            "num": m.group(1),
            "date": date,
            "title": m.group(3).strip(),
            "body": body,
        })
    return heads[0][0], recs


def _load_archived() -> dict[str, list[dict]]:
    """已归档分卷：{月份: recs}（编号按原样保存，含 `72b` 这类后缀）。"""
    out: dict[str, list[dict]] = {}
    if not ARCHIVE_DIR.exists():
        return out
    for path in sorted(ARCHIVE_DIR.glob("*.md")):
        _, recs = _parse(path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n"))
        out[path.stem] = recs
    return out


def _num_key(num: str) -> tuple[int, str]:
    """编号排序键：`72` < `72b` < `73`。"""
    m = re.match(r"(\d+)([A-Za-z]?)", num)
    return (int(m.group(1)), m.group(2)) if m else (0, num)


def _volume_head(month: str) -> str:
    return (
        f"# 回测记录归档 · {month}\n\n"
        f"> 从 `BACKTEST_LOG.md` 原样搬迁，**正文逐字未改**（只做物理搬迁）。\n"
        f"> 索引见 [BACKTEST_LOG.md](../../BACKTEST_LOG.md)；编号 `#NN` 全局唯一、跨文件连续。\n\n"
        "---\n\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="拆分 BACKTEST_LOG：索引 + 最近 N 条 + 按月归档")
    ap.add_argument("--apply", action="store_true", help="真的写盘（默认只看计划）")
    ap.add_argument("--keep", type=int, default=10, help="主文件保留最近多少条（默认 10）")
    args = ap.parse_args()

    lines = _read()
    first, recs = _parse(lines)
    if not recs:
        print("没有解析到任何 `## 记录 #NN` 段落，放弃。")
        return 1
    print(f"解析到 {len(recs)} 条记录（#{recs[0]['num']}~#{recs[-1]['num']}），"
          f"第一条在行 {first}")

    nums = [r["num"] for r in recs]
    dup = {n for n in nums if nums.count(n) > 1}
    if dup:
        print(f"!! 编号重复 {sorted(dup)}，先修正再归档。")
        return 1

    keep = max(0, args.keep)
    keep_set = {r["num"] for r in recs[-keep:]} if keep else set()
    to_archive = [r for r in recs if r["num"] not in keep_set]

    by_month: dict[str, list[dict]] = {}
    for r in to_archive:
        by_month.setdefault(r["date"][:7], []).append(r)

    archived = _load_archived()                     # 已有分卷：{month: recs}
    existing = {m: {r["num"] for r in v} for m, v in archived.items()}
    plan = []
    for month, items in sorted(by_month.items()):
        new = [r for r in items if r["num"] not in existing.get(month, set())]
        plan.append((month, items, new))
        print(f"  {month}: 本卷 {len(items)} 条，其中待写入 {len(new)} 条"
              f"（已存在 {len(items) - len(new)} 条）")

    # 全量视图 = 归档卷（含本次将写入的）+ 主文件保留的 —— 保证重复运行时索引不缩水
    merged: dict[str, dict] = {}
    for month, vol in archived.items():
        for r in vol:
            merged.setdefault(r["num"], r)
    for month, _items, new in plan:
        for r in new:
            merged.setdefault(r["num"], r)
    for r in recs:
        if r["num"] in keep_set:
            merged.setdefault(r["num"], r)
    full = sorted(merged.values(), key=lambda r: (r["date"], _num_key(r["num"])))

    # 只保留原来的「⚠️ 口径提醒」引用块，不要把文件开头的说明也搬下来
    notice = ""
    cut = lines[:first or 0]
    for i, line in enumerate(cut):
        if line.startswith(">") and "⚠️" in line:
            block: list[str] = []
            j = i
            while j < len(cut) and cut[j].startswith(">"):
                block.append(cut[j])
                j += 1
            notice = "\n".join(block) + "\n\n"
            break

    index_rows = []
    for r in full:
        if r["num"] in keep_set:
            where = "本文件"
        else:
            where = f"[{r['date'][:7]}.md](docs/backtest_log/{r['date'][:7]}.md)"
        title = r["title"].replace("|", "\\|")
        index_rows.append(f"| {r['num']} | {r['date']} | {title} | {where} |")

    vol_view: dict[str, list[dict]] = {m: list(v) for m, v in archived.items()}
    for month, _items, new in plan:
        vol_view.setdefault(month, []).extend(new)
    arch_rows = []
    for month in sorted(vol_view):
        v = sorted(vol_view[month], key=lambda r: _num_key(r["num"]))
        if not v:
            continue
        arch_rows.append(
            f"| {month} | #{v[0]['num']}~#{v[-1]['num']} | {len(v)} | "
            f"[docs/backtest_log/{month}.md](docs/backtest_log/{month}.md) |")
    archive_table = ("| 月份 | 首条 ~ 末条 | 条数 | 文件 |\n| --- | --- | --- | --- |\n"
                     + "\n".join(arch_rows)
                     + "\n\n> 编号有小幅跳跃：历史缺号（#45~#55 等）+ #75~#78 标题未写日期、"
                       "按正文首个日期归类。条数以「条数」列为准，检索用上面的索引。") \
        if arch_rows else "（暂无归档）"

    header = HEADER.format(keep=keep, archive_table=archive_table,
                           index_rows="\n".join(index_rows), notice=notice)
    tail = "\n\n".join("\n".join(r["body"]) for r in recs if r["num"] in keep_set)
    new_log = header + tail + "\n"

    if not args.apply:
        print(f"\n[dry-run] 主文件 {len(lines)} 行 → {new_log.count(chr(10)) + 1} 行；"
              f"索引 {len(index_rows)} 条（全量 {len(full)}），保留最近 {len(keep_set)} 条，"
              f"本次新归档 {sum(len(p[2]) for p in plan)} 条。加 --apply 执行。")
        return 0

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for month, _items, new in plan:
        path = ARCHIVE_DIR / f"{month}.md"
        if path.exists():
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
        else:
            text = _volume_head(month).rstrip("\n")
        if new:
            text += "\n\n" + "\n\n".join("\n".join(r["body"]) for r in new)
        path.write_text(text + "\n", encoding="utf-8")
        print(f"  写 {path.relative_to(ROOT)}：本次新增 {len(new)} 条")

    LOG.write_text(new_log, encoding="utf-8")
    print(f"  写 BACKTEST_LOG.md：{new_log.count(chr(10)) + 1} 行，"
          f"索引 {len(index_rows)} 条，保留最近 {len(keep_set)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
