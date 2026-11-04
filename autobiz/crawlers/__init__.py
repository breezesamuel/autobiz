"""爬虫注册表与批量执行。"""
import re
from difflib import SequenceMatcher

from ..config import CFG
from .base import BaseCrawler, Lead  # noqa: F401
from .ats import Greenhouse
from .dev_sources import GitHubBounty, HNCrawler, RedditCrawler, StackOverflowCrawler
from .job_boards import Arbeitnow, Himalayas, Jobicy, Remotive, RemoteOK
from .marketplaces import ContraCrawler, FiverrCrawler, UpworkCrawler

REGISTRY: dict = {}


def _norm(title: str) -> str:
    # 保留 CJK 等 unicode 字母，否则中文标题会被清成空串而误判
    return re.sub(r"[\W_]+", " ", (title or "").lower(), flags=re.UNICODE).strip()


def _strip_digits(s: str) -> str:
    return re.sub(r"[0-9]+", "", s).strip()


def _url_key(url: str) -> str:
    """同站点的地区域名变体（arbeitnow.com/.fr/.co.uk）视为同一来源键。"""
    if not url:
        return ""
    m = re.match(r"^https?://(?:www\.)?([^/]+)(/[^?#]*)?", url)
    if not m:
        return url
    host, path = m.group(1), (m.group(2) or "").rstrip("/")
    label = host.split(".")[0].lower()
    return label + path


def _is_similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    # 仅数字/编号不同的标题视为同一活动的重复帖（时间戳、岗位编号变体）
    if _strip_digits(a) == _strip_digits(b) and _strip_digits(a):
        return True
    # 包含关系需较短方 >= 较长方 60%，避免 "Software Engineer" 吞掉所有岗位
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 0.6 * len(longer) and shorter in longer:
        return True
    if len(a) >= 30 and len(b) >= 30:
        return SequenceMatcher(None, a, b).ratio() >= 0.85
    return False


def _same_job(title_a: str, desc_a: str, url_a: str,
              title_b: str, desc_b: str, url_b: str) -> bool:
    """判重三重证据：URL 键一致，或标题一致/仅数字差异且描述吻合。

    标题有实质差异（职级/地区/语言）时一律不合并——公司模板会让不同岗位的
    描述高度相似，仅凭描述合并会误删真实线索。
    """
    ka, kb = _url_key(url_a), _url_key(url_b)
    if ka and ka == kb:
        return True
    variant = (title_a == title_b
               or (bool(_strip_digits(title_a))
                   and _strip_digits(title_a) == _strip_digits(title_b)))
    if not variant:
        return False
    da, db = _norm((desc_a or "")[:400]), _norm((desc_b or "")[:400])
    if len(da) >= 30 and len(db) >= 30:
        return SequenceMatcher(None, da, db).ratio() >= 0.5
    return True  # 描述缺失 + 同源同标题 -> 视为重发


def _register(*classes):
    for c in classes:
        REGISTRY[c.name] = c


_register(GitHubBounty, HNCrawler, StackOverflowCrawler, RedditCrawler,
          RemoteOK, Arbeitnow, Remotive, Himalayas, Jobicy, Greenhouse,
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
    # 跨仓库/跨轮模糊去重：同标题、包含关系、高相似度（防同活动刷屏农场）
    seen: dict = {}
    for row in db.all_leads():
        seen.setdefault(row["source"], []).append(
            (_norm(row.get("title")), row.get("description") or "",
             row.get("url") or ""))
    new = 0
    for lead in leads:
        norm = _norm(lead.title)
        pool = seen.setdefault(lead.source, [])
        if any(_is_similar(norm, t)
               and _same_job(norm, lead.description or "", lead.url or "",
                             t, d, u)
               for t, d, u in pool):
            continue
        if db.upsert_lead({
            "uid": lead.uid, "source": lead.source, "title": lead.title,
            "url": lead.url, "description": lead.description,
            "budget_min": lead.budget_min, "budget_max": lead.budget_max,
            "currency": lead.currency, "posted_at": lead.posted_at,
            "raw": lead.raw,
        }):
            new += 1
            pool.append((norm, lead.description or "", lead.url or ""))
    return leads, errors, new
