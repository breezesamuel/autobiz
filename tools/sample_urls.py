"""列出指定仓库在库中的样例链接。"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
conn = sqlite3.connect(str(ROOT / "data" / "autobiz.db"))
conn.row_factory = sqlite3.Row
keyword = sys.argv[1] if len(sys.argv) > 1 else ""
rows = conn.execute(
    "SELECT url, raw_json FROM leads WHERE source='github_bounty'").fetchall()
for r in rows:
    repo = json.loads(r["raw_json"] or "{}").get("repo", "").split("/repos/")[-1]
    if keyword in repo:
        print(repo, "|", r["url"])
conn.close()
