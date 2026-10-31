"""抓取 A 股「代码 -> 行业」映射，供路线 A 行业内中性化使用。

数据源：akshare（免 token，东方财富行业板块口径）。本脚本**需要在有网络的机器上运行**
（如你的本地 Windows 机），沙箱/离线环境拉不到。产出 ``data/industry_map.csv``：

    code,industry,name
    600000,银行,浦发银行
    000001,银行,平安银行
    ...

用法（backend 目录）：:

    .venv\\Scripts\\python.exe scripts/fetch_industry_map.py
    .venv\\Scripts\\python.exe scripts/fetch_industry_map.py --limit 5   # 先拉前 5 个板块试水
    .venv\\Scripts\\python.exe scripts/fetch_industry_map.py --out D:/Backup/SAS/data/industry_map.csv

说明
----
- 行业分类会随时间微调（个别股票换板块），这里取**当前快照**即可，中性化对时点误差不敏感。
- 若遇代理/连接报错，先确认本机直连东方财富正常；必要时在命令前加 ``set NO_PROXY=*``
  （Windows）或 ``NO_PROXY='*'``（Linux/Mac）绕过代理。
- 幂等：每次整体覆盖输出文件。已抓到的板块失败会自动跳过并继续，不中断。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402


def fetch_boards():
    import akshare as ak
    return ak.stock_board_industry_name_em()


def fetch_cons(symbol: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_board_industry_cons_em(symbol=symbol)


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取 A 股代码->行业 映射")
    parser.add_argument("--out", default=None, help="输出 csv 路径（默认 <data_dir>/industry_map.csv）")
    parser.add_argument("--limit", type=int, default=0, help="只拉前 N 个板块（调试用，0=全部）")
    parser.add_argument("--sleep", type=float, default=0.2, help="板块间休眠秒数，避免被限频")
    args = parser.parse_args()

    settings = get_settings()
    out_path = args.out or str(settings.kline_dir.parent / "industry_map.csv")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    print(f"读取行业板块列表 …")
    boards = fetch_boards()
    names = boards["板块名称"].tolist()
    if args.limit:
        names = names[: args.limit]
    print(f"共 {len(boards)} 个板块，本次处理 {len(names)} 个 -> {out_path}")

    rows: list[dict] = []
    for i, name in enumerate(names, 1):
        try:
            cons = fetch_cons(name)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(names)}] {name} 失败：{e}")
            continue
        code_col = "代码" if "代码" in cons.columns else "code"
        name_col = "名称" if "名称" in cons.columns else "name"
        if code_col not in cons.columns:
            print(f"  [{i}/{len(names)}] {name} 无代码列，跳过")
            continue
        for _, r in cons.iterrows():
            code = str(r[code_col]).strip().zfill(6)
            nm = str(r[name_col]) if name_col in cons.columns else ""
            rows.append({"code": code, "industry": name, "name": nm})
        print(f"  [{i}/{len(names)}] {name}：+{len(cons)} 只（累计 {len(rows)}）")
        if args.sleep:
            time.sleep(args.sleep)

    df = pd.DataFrame(rows).drop_duplicates(subset=["code"])
    df = df.sort_values("code")
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n完成：写入 {len(df)} 只唯一股票到 {out_path}")
    print(f"覆盖行业数：{df['industry'].nunique()}")


if __name__ == "__main__":
    main()
