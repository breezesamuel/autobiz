"""抽查：列出评分最高的线索，用于人工核验 URL 真伪。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

conn = sqlite3.connect(str(Path(__file__).resolve().parents[1] / "data" / "autobiz.db"))
rows = conn.execute(
    "SELECT score, source, title, url FROM leads ORDER BY score DESC LIMIT 8"
).fetchall()
for r in rows:
    print(f"{r[0]:>3} | {r[1]:<15} | {(r[2] or '')[:64]:<64} | {r[3]}")
conn.close()
