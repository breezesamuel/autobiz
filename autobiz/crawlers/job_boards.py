"""免费公开职位板 JSON API：RemoteOK / Arbeitnow / Remotive / Himalayas / Jobicy。"""
from .. import http
from ..config import CFG
from .base import (BaseCrawler, Lead, has_exclude, keyword_hits, parse_budget,
                   strip_html)


def _keep(title: str, text: str) -> bool:
    kw = CFG["crawl"]["keywords"]
    blob = f"{title} {text}"
    if has_exclude(blob, CFG["crawl"]["exclude_keywords"]):
        return False
    return bool(keyword_hits(blob, kw))


class RemoteOK(BaseCrawler):
    name = "remoteok"

    def fetch(self) -> list:
        r = http.get("https://remoteok.com/api", min_interval=3.0)
        items = r.json()
        leads = []
        for it in items[1:]:  # 第 0 个是法律声明
            if not isinstance(it, dict) or not it.get("id"):
                continue
            title = it.get("position", "")
            desc = strip_html(it.get("description") or "")
            if not _keep(title, desc):
                continue
            bmin = it.get("salary_min") or None
            bmax = it.get("salary_max") or None
            if not bmin:
                pb = parse_budget(desc[:500]) or parse_budget(title)
                if pb:
                    bmin, bmax = pb[0], pb[1]
            leads.append(Lead(
                uid=f"ro_{it['id']}", source=self.name, title=title,
                url=it.get("url") or "", description=desc[:4000],
                budget_min=bmin, budget_max=bmax, currency="USD",
                posted_at=it.get("date", ""),
                raw={"company": it.get("company"), "tags": it.get("tags")},
            ))
        return leads


class Arbeitnow(BaseCrawler):
    name = "arbeitnow"

    def fetch(self) -> list:
        r = http.get("https://www.arbeitnow.com/api/job-board-api", min_interval=3.0)
        leads = []
        for it in r.json().get("data", []):
            title = it.get("title", "")
            desc = strip_html(it.get("description") or "")
            if not _keep(title, f"{desc} {' '.join(it.get('tags') or [])}"):
                continue
            pb = parse_budget(desc[:500])
            leads.append(Lead(
                uid=f"an_{it.get('slug') or it.get('url')}", source=self.name,
                title=title, url=it.get("url", ""), description=desc[:4000],
                budget_min=pb[0] if pb else None,
                budget_max=pb[1] if pb else None,
                currency=pb[2] if pb else "USD",
                posted_at=_unix(it.get("created_at")),
                raw={"company": it.get("company_name"), "tags": it.get("tags")},
            ))
        return leads


class Remotive(BaseCrawler):
    name = "remotive"

    def fetch(self) -> list:
        r = http.get("https://remotive.com/api/remote-jobs",
                     params={"limit": 50}, min_interval=4.0)
        leads = []
        for it in r.json().get("jobs", []):
            title = it.get("title", "")
            desc = strip_html(it.get("description") or "")
            if not _keep(title, f"{desc} {it.get('category','')}"):
                continue
            pb = parse_budget(it.get("salary") or "") or parse_budget(desc[:500])
            leads.append(Lead(
                uid=f"rm_{it.get('id')}", source=self.name, title=title,
                url=it.get("url", ""), description=desc[:4000],
                budget_min=pb[0] if pb else None,
                budget_max=pb[1] if pb else None,
                currency=pb[2] if pb else "USD",
                posted_at=it.get("publication_date", ""),
                raw={"company": it.get("company_name"),
                     "location": it.get("candidate_required_location")},
            ))
        return leads


class Himalayas(BaseCrawler):
    name = "himalayas"

    def fetch(self) -> list:
        r = http.get("https://himalayas.app/jobs/api",
                     params={"limit": 50}, min_interval=3.0)
        leads = []
        for it in r.json().get("jobs", []):
            title = it.get("title", "")
            desc = strip_html(it.get("description") or "")
            tags = " ".join(t if isinstance(t, str) else t.get("name", "")
                            for t in (it.get("tags") or []))
            if not _keep(title, f"{desc} {tags}"):
                continue
            company = it.get("company")
            leads.append(Lead(
                uid=f"hm_{it.get('id') or it.get('slug')}", source=self.name,
                title=title, url=it.get("url", ""), description=desc[:4000],
                posted_at=it.get("createdAt", ""),
                raw={"company": company.get("name") if isinstance(company, dict) else company,
                     "tags": tags},
            ))
        return leads


class Jobicy(BaseCrawler):
    name = "jobicy"

    def fetch(self) -> list:
        r = http.get("https://jobicy.com/api/v2/remote-jobs",
                     params={"count": 20}, min_interval=3.0)
        leads = []
        for it in r.json().get("jobs", []):
            title = it.get("jobTitle", "")
            desc = strip_html(it.get("jobDescription") or "")
            tags = " ".join(it.get("jobTags") or [])
            if not _keep(title, f"{desc} {tags}"):
                continue
            bmin = it.get("annualSalaryMin") or None
            bmax = it.get("annualSalaryMax") or None
            leads.append(Lead(
                uid=f"jc_{it.get('id') or it.get('jobUrl')}", source=self.name,
                title=title, url=it.get("jobUrl", ""), description=desc[:4000],
                budget_min=bmin, budget_max=bmax,
                currency=it.get("salaryCurrency") or "USD",
                posted_at=it.get("pubDate", ""),
                raw={"company": it.get("companyName"), "tags": tags,
                     "jobType": it.get("jobType")},
            ))
        return leads


def _unix(ts) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return str(ts or "")
