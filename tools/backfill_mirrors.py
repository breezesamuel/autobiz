"""把已入库的镜像仓库线索 URL 回填为原始 issue 地址，并清理回填后重复的线索。"""
import json
import sqlite3
import sys

sys.path.insert(0, ".")
from autobiz.crawlers.dev_sources import GitHubBounty as GB  # noqa: E402

MIRROR_RE = GB.MIRROR_RE
MIRROR_HOSTS = ("vikingr2023/awesome-agent-bounties",)

c = sqlite3.connect("data/autobiz.db")
c.row_factory = sqlite3.Row

rows = c.execute(
    "select id, uid, title, url, description, raw_json from leads "
    "where source='github_bounty'"
).fetchall()

updated, skipped_dup = 0, 0
for r in rows:
    desc = r["description"] or ""
    if "awesome-agent-bounties" not in (r["url"] or "") and "mirror" not in desc.lower():
        continue
    m = MIRROR_RE.search(desc)
    if not m:
        continue
    real = m.group(1)
    dup = c.execute(
        "select id, description, budget_min, raw_json from leads where url=? and id!=?",
        (real, r["id"]),
    ).fetchone()
    raw = json.loads(r["raw_json"] or "{}")
    raw["mirror_of"] = real
    if dup:
        # 真源头已在库中：合并镜像侧的赏金与更丰富描述，再删除镜像副本
        d_raw = json.loads(dup["raw_json"] or "{}")
        if not dup["budget_min"] and r["description"]:
            for rx in GB.BOUNTY_PATTERNS:
                am = rx.search(r["description"] or "")
                if am:
                    c.execute(
                        "update leads set budget_min=?, budget_max=? where id=?",
                        (float(am.group(1).replace(",", "")),) * 2 + (dup["id"],),
                    )
                    break
        if len(r["description"] or "") > len(dup["description"] or ""):
            c.execute(
                "update leads set description=? where id=?",
                (r["description"], dup["id"]),
            )
        if d_raw.get("mirror_of") is None:
            d_raw["mirror_of"] = real
            c.execute(
                "update leads set raw_json=? where id=?",
                (json.dumps(d_raw, ensure_ascii=False), dup["id"]),
            )
        c.execute("delete from proposals where lead_id=?", (r["id"],))
        c.execute("delete from leads where id=?", (r["id"],))
        skipped_dup += 1
        print(f"  [del] 镜像副本已合并至真源头: {r['title'][:50]}")
        continue
    c.execute(
        "update leads set url=?, raw_json=? where id=?",
        (real, json.dumps(raw, ensure_ascii=False), r["id"]),
    )
    updated += 1
    print(f"  [fix] {r['title'][:50]} -> {real}")

c.commit()
print(f"\n回填 {updated} 条，删除重复镜像副本 {skipped_dup} 条")
