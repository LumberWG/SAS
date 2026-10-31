"""聚合 ML A/B 回测结果（random_dist_mlab.json）。

用法：python scripts/agg_ml.py <tag>
输出：各方案分布 + 相对 default 的配对差值（含是否越过 3.5pp 显著线）。
"""
import json
import sys
import statistics as st
from pathlib import Path

TAG = sys.argv[1] if len(sys.argv) > 1 else "mlab"
P = Path(r"D:/Backup/SAS/data/logs")
f = P / f"random_dist_{TAG}.json"
d = json.load(open(f, encoding="utf-8"))
segs = d["segments"]
profs = d.get("profiles", {})
names = list(profs.keys())


def ex(name):
    return [s["profiles"][name]["excess"] for s in segs if name in s["profiles"]]


print(f"=== tag={TAG}  段数={len(segs)}  方案={names} ===\n")
print("--- 单方案分布（超额 %）---")
for n in names:
    v = ex(n)
    if not v:
        print(f"  {n}: 无数据")
        continue
    qs = st.quantiles(v, n=4) if len(v) >= 4 else (min(v), st.median(v), max(v))
    print(f"  {n:10s} n={len(v)} 中位={st.median(v):+.2f} 均值={st.mean(v):+.2f} "
          f" 25%={qs[0]:+.2f} 75%={qs[2]:+.2f} 最差={min(v):+.2f} 最好={max(v):+.2f} "
          f" >0={sum(1 for x in v if x > 0)/len(v):.0%}")

print("\n--- 配对差值（相对 default）---")
for n in names:
    if n == "default":
        continue
    diff = [s["profiles"][n]["excess"] - s["profiles"]["default"]["excess"]
            for s in segs if n in s["profiles"] and "default" in s["profiles"]]
    if not diff:
        continue
    med = st.median(diff)
    mean = st.mean(diff)
    win = sum(1 for x in diff if x > 0) / len(diff)
    flag = "✅ 越过显著线" if med > 3.5 else ("⚠️ 接近" if med > 1.0 else "❌ 未达")
    print(f"  {n:10s} 中位差={med:+.2f}pp 均值差={mean:+.2f}pp "
          f" 赢段占比={win:.0%} 最差配对={min(diff):+.2f}  {flag}")

# OOS IC 信息（若有）
print("\n--- 说明 ---")
print("  显著线 = 配对中位差 > 3.5pp（与项目验收一致）。")
print("  OOS IC<=0 的段 ML 自动回退 14 因子（不注入噪声），其配对差=0。")
