"""把云端工作流回写的 leads_export.json 并入本地 SQLite（按 uid 去重）。"""
import json
import subprocess
import sys
import time

sys.path.insert(0, ".")
from autobiz.db import upsert_lead  # noqa: E402

rev = sys.argv[1] if len(sys.argv) > 1 else "157934e"
import shutil  # noqa: E402

git = shutil.which("git") or "C:/Program Files/Git/cmd/git.exe"
raw = subprocess.run(
    [git, "show", f"{rev}:data/leads_export.json"],
    capture_output=True, cwd=".",
).stdout
if not raw:
    print("no export at", rev, "— fresh start")
    raise SystemExit(0)
data = json.loads(raw.decode("utf-8"))
leads = data.get("leads", []) if isinstance(data, dict) else data

added = 0
for L in leads:
    uid = L.get("uid")
    if not uid:
        continue
    try:
        item = dict(L)
        if "raw" not in item and "raw_json" in item:
            import json as _j
            item["raw"] = _j.loads(item["raw_json"] or "{}")
        r = upsert_lead(item)
        if r:
            added += 1
    except Exception as e:
        print("skip", uid, e)
print(f"cloud leads={len(leads)}, newly added={added}")
