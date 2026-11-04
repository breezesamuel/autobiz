"""ATS 职位源：Greenhouse 公开 API（免授权、免 Cloudflare，替代被拦的 arbeitnow）。"""
import json

from .. import http
from ..config import CFG
from .base import BaseCrawler, Lead, has_exclude, keyword_hits, strip_html

# 实测有活跃招聘板且岗位与远程/技术相关的公司
BOARDS = [
    "gitlab", "cloudflare", "datadog", "elastic", "mongodb", "canonical",
    "databricks", "stripe", "anthropic", "scaleai", "figma", "twilio",
    "vercel", "discord", "dropbox", "asana", "gusto", "duolingo",
]
MAX_PER_BOARD = 12
DETAIL_TIMEOUT = 20


def _get_json(url: str) -> dict:
    try:
        return http.get(url, timeout=30, min_interval=0.5).json()
    except Exception:
        from curl_cffi import requests as cr
        r = cr.get(url, impersonate="chrome131", timeout=40)
        r.raise_for_status()
        return json.loads(r.text)


def _page_text(url: str) -> str:
    """岗位详情页正文；失败则返回空串（调用方降级为元数据描述）。"""
    try:
        import requests
        r = requests.get(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"),
        }, timeout=DETAIL_TIMEOUT)
        r.raise_for_status()
        txt = strip_html(r.text)
        i = txt.lower().find("description")
        return txt[i:] if i > 0 else txt
    except Exception:
        return ""


def _keep(title: str, text: str) -> bool:
    kw = CFG["crawl"]["keywords"]
    blob = f"{title} {text}"
    if has_exclude(blob, CFG["crawl"]["exclude_keywords"]):
        return False
    return bool(keyword_hits(blob, kw))


class Greenhouse(BaseCrawler):
    name = "greenhouse"

    def fetch(self) -> list:
        import concurrent.futures as cf

        cands = []
        for board in BOARDS:
            try:
                jobs = _get_json(
                    f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
                    "?content=false").get("jobs", [])
            except Exception as e:
                print(f"  [x] greenhouse/{board}: {str(e)[:80]}")
                continue
            picked = []
            for j in jobs:
                title = j.get("title") or ""
                loc = (j.get("location") or {}).get("name") or ""
                dept = ", ".join(d.get("name", "") for d in
                                 (j.get("departments") or []))
                if not _keep(title, f"{loc} {dept}"):
                    continue
                picked.append((board, j, loc, dept))
            picked.sort(key=lambda x: x[1].get("updated_at") or "", reverse=True)
            cands.extend(picked[:MAX_PER_BOARD])

        urls = [(b, j, loc, dept) for b, j, loc, dept in cands
                if (j.get("absolute_url") or "")]
        texts: dict = {}
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_page_text, j["absolute_url"]): j["id"]
                    for _, j, _, _ in urls}
            for f in cf.as_completed(futs):
                texts[futs[f]] = f.result()

        leads = []
        for board, j, loc, dept in cands:
            title = j.get("title") or ""
            url = j.get("absolute_url") or ""
            desc = texts.get(j.get("id")) or ""
            if not desc:
                desc = (f"{title} — {board} — {loc} {dept}. "
                        f"Updated {j.get('updated_at','')}")
            # 用完整描述做二次过滤，剔除标题沾关键词的销售/无关岗位
            if not _keep(title, desc):
                continue
            leads.append(Lead(
                uid=f"gh_{board}_{j.get('id')}", source=self.name,
                title=title, url=url, description=desc[:4000],
                posted_at=j.get("first_published") or j.get("updated_at") or "",
                raw={"company": j.get("company_name") or board,
                     "location": loc, "departments": dept},
            ))
        return leads
