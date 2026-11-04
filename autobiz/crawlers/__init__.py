"""爬虫注册表与批量执行。"""
from ..config import CFG
from .base import BaseCrawler, Lead  # noqa: F401
from .dev_sources import GitHubBounty, HNCrawler, RedditCrawler, StackOverflowCrawler
from .job_boards import Arbeitnow, Himalayas, Jobicy, Remotive, RemoteOK
from .marketplaces import ContraCrawler, FiverrCrawler, UpworkCrawler

REGISTRY: dict = {}


def _register(*classes):
    for c in classes:
        REGISTRY[c.name] = c


_register(GitHubBounty, HNCrawler, StackOverflowCrawler, RedditCrawler,
          RemoteOK, Arbeitnow, Remotive, Himalayas, Jobicy,
          UpworkCrawler, FiverrCrawler, ContraCrawler)


def crawl_all(sources: list = None) -> tuple:
    """抓取全部渠道，返回 (leads, {source: error})。单源失败不影响其它源。"""
    from .. import db
    sources = sources or CFG["crawl"]["sources"]
    leads, errors = [], {}
    for name in sources:
        cls = REGISTRY.get(name)
        if cls is None:
            errors[name] = "未注册的渠道"
            continue
        try:
            got = cls().fetch()
            leads.extend(got)
        except Exception as e:
            msg = str(e)[:400]
            errors[name] = msg
            print(f"  [x] {name}: {msg[:120]}")
    new = 0
    for lead in leads:
        if db.upsert_lead({
            "uid": lead.uid, "source": lead.source, "title": lead.title,
            "url": lead.url, "description": lead.description,
            "budget_min": lead.budget_min, "budget_max": lead.budget_max,
            "currency": lead.currency, "posted_at": lead.posted_at,
            "raw": lead.raw,
        }):
            new += 1
    return leads, errors, new
