"""从导出快照恢复线索到主库（按 uid/url 去重），用于误删回滚或跨机器合并。"""
import json
import sqlite3
import sys

sys.path.insert(0, ".")

SRC = sys.argv[1] if len(sys.argv) > 1 else "data/leads_export.json"

c = sqlite3.connect("data/autobiz.db")
c.row_factory = sqlite3.Row
have_uid = {r[0] for r in c.execute("select uid from leads")}
have_url = {r[0] for r in c.execute("select url from leads where url != ''")}

data = json.load(open(SRC, encoding="utf-8"))
rows = data if isinstance(data, list) else data.get("leads", [])
added = 0
for r in rows:
    uid, url = r.get("uid"), r.get("url") or ""
    if not uid or uid in have_uid or (url and url in have_url):
        continue
    c.execute(
        "insert or ignore into leads (uid,source,title,url,description,budget_min,"
        "budget_max,currency,posted_at,fetched_at,score,status,raw_json) "
        "values (?,?,?,?,?,?,?,?,?,?,?,'new',?)",
        (uid, r.get("source"), r.get("title"), url, r.get("description"),
         r.get("budget_min"), r.get("budget_max"), r.get("currency"),
         r.get("posted_at"), r.get("fetched_at") or "", r.get("score", 0),
         json.dumps(r.get("raw") or r.get("raw_json"), ensure_ascii=False)),
    )
    have_uid.add(uid)
    if url:
        have_url.add(url)
    added += 1
c.commit()
total = c.execute("select count(*) from leads").fetchone()[0]
print(f"restore {added} -> total {total}")
