import sqlite3
from collections import Counter

c = sqlite3.connect(r"D:/Backup/SAS/data/sas.db")
tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("ALL TABLES:", tabs)
for tb in tabs:
    if any(k in tb.lower() for k in ("signal", "recommend", "candi", "watch", "screen", "paper")):
        try:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({tb})")]
            n = c.execute(f"SELECT count(*) FROM {tb}").fetchone()[0]
            print(f"\n{tb}: rows={n}")
            print("  cols:", cols)
            codecols = [x for x in cols if "code" in x.lower()]
            if codecols:
                rows = c.execute(f"SELECT {codecols[0]} FROM {tb}").fetchall()
                pre = Counter((x[0][:2] if x[0] else "??") for x in rows)
                print("  code prefix:", dict(pre))
        except Exception as e:
            print(tb, "err", e)
