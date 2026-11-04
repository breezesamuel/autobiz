"""数据卫生：统计赏金来源仓库分布，删除黑名单仓库（刷 issue 农场）的污染线索。"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autobiz.config import CFG  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
conn = sqlite3.connect(str(ROOT / "data" / "autobiz.db"))
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT id, url, raw_json FROM leads WHERE source='github_bounty'").fetchall()
from collections import Counter  # noqa: E402

repos = Counter()
id_by_repo = {}
for r in rows:
    try:
        repo = json.loads(r["raw_json"] or "{}").get("repo", "")
    except Exception:
        repo = ""
    repo = repo.split("/repos/")[-1] or r["url"]
    repos[repo] += 1
    id_by_repo.setdefault(repo, []).append(r["id"])

print("=== github_bounty 仓库分布 ===")
for repo, n in repos.most_common(15):
    print(f"  {n:>3}  {repo}")

blocklist = [b.lower() for b in CFG["crawl"].get("github_repo_blocklist", [])]
# 镜像仓库的旧记录（解析前的链接）也强制重建
force_repos = [a.lower() for a in sys.argv[1:]]
to_delete = []
for repo, ids in id_by_repo.items():
    if any(b in repo.lower() for b in blocklist) or any(f in repo.lower() for f in force_repos):
        to_delete.extend(ids)

if to_delete:
    qmarks = ",".join("?" * len(to_delete))
    conn.execute(f"DELETE FROM proposals WHERE lead_id IN ({qmarks})", to_delete)
    conn.execute(f"DELETE FROM leads WHERE id IN ({qmarks})", to_delete)
    conn.commit()
    print(f"\n已删除农场污染线索 {len(to_delete)} 条（含其提案草稿）")
else:
    print("\n没有命中黑名单的线索")

print("剩余 github_bounty:",
      conn.execute("SELECT COUNT(*) FROM leads WHERE source='github_bounty'").fetchone()[0])
conn.close()
