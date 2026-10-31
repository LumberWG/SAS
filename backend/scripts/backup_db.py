"""SQLite 热备份：用在线 backup API 复制 ``sas.db``，保留最近 N 份。

为什么不能直接 copy 文件：
库是 **WAL 模式**且服务一直在写，直接 `Copy-Item` 可能拿到「主库已改、-wal 未合并」
的半截状态，恢复时缺最近若干笔成交。``sqlite3.Connection.backup()`` 由 SQLite 自己
在页级加锁、产出一致性快照，**不需要停服务**，也不需要独占。

用法::

    python scripts/backup_db.py                       # 默认备份到 D:/Backup/SAS/backups，保留 30 份
    python scripts/backup_db.py --keep 60             # 改成保留 60 份
    python scripts/backup_db.py --out-dir E:/tmp/bk   # 换目录

计划任务 ``SASDbBackup`` 每交易日 15:50 跑一次（结算 15:40 之后，当天数据已落库）。
退出码：0 成功 / 1 失败（任务计划程序会记在「上次运行结果」里）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

#: 与线上保持一致的默认值（可用参数覆盖）
DEFAULT_SRC = Path("D:/Backup/SAS/data/sas.db")
DEFAULT_OUT = Path("D:/Backup/SAS/backups")
#: 名字前缀固定，清理时只动自己产出的文件，不误删别的备份
PREFIX = "sas-"


def backup(src: Path, out_dir: Path, keep: int) -> Path:
    """做一次一致性快照，返回新文件路径。"""
    if not src.is_file():
        raise FileNotFoundError(f"源库不存在: {src}")
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{PREFIX}{datetime.now():%Y%m%d-%H%M%S}.db"

    # 源用普通连接（不是 ro）：db 可能带未合并的 -wal，只读打开在部分场景会拒绝恢复；
    # 备份是只读语义，普通连接不会改数据。
    src_con = sqlite3.connect(str(src), timeout=30)
    try:
        dst_con = sqlite3.connect(str(dst))
        try:
            src_con.backup(dst_con)  # SQLite 官方热备份 API，页级一致
            dst_con.execute("PRAGMA integrity_check").fetchone()
        finally:
            dst_con.close()
    finally:
        src_con.close()

    # 只保留最近 keep 份（文件名含时间戳，字典序即时间序）
    old = sorted(out_dir.glob(f"{PREFIX}*.db"))
    for path in old[:-keep] if keep > 0 else []:
        try:
            path.unlink()
        except OSError as exc:  # noqa: BLE001 - 删不掉旧备份不算失败
            print(f"[warn] 删除旧备份失败 {path.name}: {exc}", file=sys.stderr)
    return dst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SAS 生产库热备份（SQLite online backup API）")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--keep", type=int, default=30, help="保留最近 N 份（0=不清理）")
    args = ap.parse_args(argv)

    try:
        dst = backup(args.src, args.out_dir, args.keep)
    except Exception as exc:  # noqa: BLE001 - 计划任务只看退出码
        print(f"[error] 备份失败: {exc}", file=sys.stderr)
        return 1
    size_mb = dst.stat().st_size / 1048576
    print(f"[ok] {args.src} -> {dst} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
