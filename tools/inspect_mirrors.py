"""检查库中镜像仓库线索的 URL 格式，用于对齐 MIRROR_RE。"""
import sqlite3

c = sqlite3.connect("data/autobiz.db")
c.row_factory = sqlite3.Row
rows = c.execute(
    "select title, description, url, raw_json from leads "
    "where source='github_bounty' and (description like '%URL%' or description like '%url%') "
    "limit 8"
).fetchall()
for r in rows:
    print("TITLE:", r["title"][:70])
    print("URL:", r["url"])
    for line in (r["description"] or "").splitlines():
        if "URL" in line or "url" in line:
            print("  LINE:", line[:160])
    print("---")
