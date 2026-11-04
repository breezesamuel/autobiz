"""合并 git 冲突中的 leads_export.json：取云端与本地并集，按 uid/url 去重。"""
import json
import subprocess

ours = subprocess.run(
    ["git", "show", ":2:data/leads_export.json"], capture_output=True
).stdout
theirs = subprocess.run(
    ["git", "show", ":3:data/leads_export.json"], capture_output=True
).stdout


def parse(raw: bytes) -> list:
    try:
        d = json.loads(raw.decode("utf-8"))
        return d if isinstance(d, list) else d.get("leads", [])
    except Exception:
        return []


items, seen = [], set()
for src in (parse(theirs), parse(ours)):  # 云端更新，先入
    for x in src:
        k = x.get("uid") or x.get("url")
        if k and k not in seen:
            seen.add(k)
            items.append(x)

with open("data/leads_export.json", "w", encoding="utf-8") as f:
    json.dump(items, f, ensure_ascii=False, indent=1)
print(f"merged {len(items)} leads")
