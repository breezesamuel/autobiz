"""线索评分：预算 35 + 关键词 30 + 新鲜度 15 + 渠道 15 + 详情 5 = 100 分制。"""
from datetime import datetime, timezone

from ..config import CFG
from ..crawlers.base import has_exclude, keyword_hits, to_usd
from ..db import all_leads, save_scores, set_status

def _parse_dt(s: str):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def deal_value_usd(lead: dict) -> float:
    cur = (lead.get("currency") or "USD").upper()
    raw = lead.get("budget_max") or lead.get("budget_min") or 0
    if not raw:
        return 0.0
    usd = to_usd(float(raw), cur)
    if usd > 20000:  # 超 2 万视为年薪（职位板惯例），折成月值
        usd = usd / 12.0
    return round(usd, 2)


def score_lead(lead: dict) -> tuple:
    cw, sc = CFG["crawl"], CFG["score"]
    text = f"{lead.get('title') or ''} {lead.get('description') or ''}"
    if has_exclude(text, cw["exclude_keywords"]):
        return 0, ["命中排除词"]

    reasons = []
    # 1) 预算价值（0-35）
    dv = deal_value_usd(lead)
    budget_pts = 0
    for th, pts in ((1000, 35), (500, 30), (200, 24), (100, 18), (50, 12), (20, 6)):
        if dv >= th:
            budget_pts = pts
            break
    if dv:
        reasons.append(f"项目价值 ${dv:.0f} (+{budget_pts})")

    # 2) 关键词匹配（0-30）
    title_hits = set(keyword_hits(lead.get("title") or "", cw["keywords"]))
    desc_hits = set(keyword_hits(lead.get("description") or "", cw["keywords"])) - title_hits
    kw_pts = min(30, len(title_hits) * 6 + len(desc_hits) * 3)
    if kw_pts:
        reasons.append(f"关键词 {len(title_hits)+len(desc_hits)} 个 (+{kw_pts})")

    # 3) 新鲜度（0-15）
    fresh_pts = 0
    dt = _parse_dt(lead.get("posted_at") or "")
    if dt:
        age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        if age_h <= 24:
            fresh_pts = 15
        elif age_h <= 72:
            fresh_pts = 11
        elif age_h <= 168:
            fresh_pts = 7
        elif age_h <= 336:
            fresh_pts = 4
        elif age_h <= 720:
            fresh_pts = 2
        if fresh_pts:
            reasons.append(f"发布 {int(age_h)}h 内 (+{fresh_pts})")

    # 4) 渠道权重（0-15）
    src_pts = sc["source_bonus"].get(lead.get("source"), 5)
    reasons.append(f"渠道 {lead.get('source')} (+{src_pts})")

    # 5) 描述详情度（0-5）
    dlen = len(lead.get("description") or "")
    detail_pts = 5 if dlen >= 500 else 3 if dlen >= 200 else 1 if dlen >= 80 else 0
    if detail_pts:
        reasons.append(f"需求描述 {dlen} 字 (+{detail_pts})")

    return budget_pts + kw_pts + fresh_pts + src_pts + detail_pts, reasons


def score_all() -> dict:
    leads = all_leads()
    shortlist_th = CFG["score"]["shortlist_threshold"]
    n_short = n_ex = 0
    for lead in leads:
        score, reasons = score_lead(lead)
        save_scores(lead["id"], score, reasons)
        if score == 0 and lead["status"] == "new":
            set_status(lead["id"], "excluded")
            n_ex += 1
        elif score >= shortlist_th and lead["status"] == "new":
            set_status(lead["id"], "shortlisted")
            n_short += 1
    return {"scored": len(leads), "new_shortlisted": n_short, "excluded": n_ex}
