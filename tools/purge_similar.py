"""清洗存量相似线索：同源内按标题模糊聚类 + 描述/URL 二次证据，双门槛才判重。

默认 dry-run（只打印将删除的条目），加 --apply 才真正落库。
"""
import sqlite3
import sys

sys.path.insert(0, ".")
from autobiz.crawlers import _is_similar, _norm, _same_job  # noqa: E402

DRY = "--apply" not in sys.argv

c = sqlite3.connect("data/autobiz.db")
c.row_factory = sqlite3.Row
rows = c.execute(
    "select id, uid, source, title, url, description, budget_min, budget_max, "
    "score from leads "
    "order by coalesce(budget_min,0) desc, coalesce(score,0) desc"
).fetchall()

clusters: dict = {}
removed = 0
for r in rows:
    norm = _norm(r["title"])
    bucket = clusters.setdefault(r["source"], [])
    hit = None
    for keep, dups in bucket:
        if _is_similar(norm, _norm(keep["title"])) and _same_job(
                norm, r["description"] or "", r["url"] or "",
                _norm(keep["title"]), keep["description"] or "",
                keep["url"] or ""):
            hit = (keep, dups)
            break
    if hit:
        hit[1].append(r)
    else:
        bucket.append((r, []))

for bucket in clusters.values():
    for keep, dups in bucket:
        for d in dups:
            removed += 1
            if not DRY:
                c.execute("delete from proposals where lead_id=?", (d["id"],))
                c.execute("delete from leads where id=?", (d["id"],))
            print(f"  [del] {d['title'][:70]}  (保留: {keep['title'][:50]})")

if not DRY:
    c.commit()
total = c.execute("select count(*) from leads").fetchone()[0]
mode = "DRY-RUN（未落库，加 --apply 执行）" if DRY else "已执行"
print(f"\n{mode}：判定重复 {removed} 条，当前库存 {total} 条")
