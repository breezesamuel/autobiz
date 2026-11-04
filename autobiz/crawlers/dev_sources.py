"""开发者需求源：GitHub Bounty / Hacker News / Stack Overflow / Reddit。"""
import re
from datetime import datetime, timezone

from .. import http
from ..config import CFG, env
from .base import BaseCrawler, Lead, parse_budget, strip_html


def _iso(ts) -> str:
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return str(ts)
    except Exception:
        return ""


class GitHubBounty(BaseCrawler):
    """GitHub 上挂真金白银赏金的 issue —— 最直接的付费需求。"""
    name = "github_bounty"
    QUERIES = [
        "is:issue is:open label:bounty sort:created-desc",
        "is:issue is:open label:algora sort:created-desc",
        "is:issue is:open label:reward sort:created-desc",
        'is:issue is:open "bounty" python in:title sort:created-desc',
    ]
    BOUNTY_PATTERNS = [
        re.compile(r"(?:bounty|reward|prize)[^\n$]{0,50}\$\s*([\d,]+)", re.I),
        re.compile(r"\$\s*([\d,]+)[^\n]{0,30}(?:bounty|reward)", re.I),
        re.compile(r"(?:algora\.dev[^\s]*?)/(\d+)"),
    ]
    MIRROR_RE = re.compile(
        r"(?:原 URL|Original URL|Source URL)\s*\|?\s*"
        r"(https://github\.com/[\w.\-]+/[\w.\-]+/issues/\d+)")

    def fetch(self) -> list:
        headers = {"Accept": "application/vnd.github+json"}
        token = env("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        cw = CFG["crawl"]
        blocklist = [b.lower() for b in cw.get("github_repo_blocklist", [])]
        cap = int(cw.get("max_per_repo_per_round", 2))
        leads, seen = [], set()
        seen_titles, seen_urls, repo_count = set(), set(), {}
        for q in self.QUERIES:
            r = http.get("https://api.github.com/search/issues",
                         params={"q": q, "sort": "created", "order": "desc",
                                 "per_page": 30},
                         headers=headers, min_interval=6.5)
            for item in r.json().get("items", []):
                if item["id"] in seen:
                    continue
                repo_url = item.get("repository_url", "")
                repo_short = repo_url.split("/repos/")[-1] if repo_url else ""
                if any(b in repo_short.lower() for b in blocklist):
                    continue  # 已知刷 issue 农场
                if repo_count.get(repo_short, 0) >= cap:
                    continue  # 单仓库单轮上限，防刷屏农场
                norm = re.sub(r"[^a-z0-9]+", " ",
                              (item.get("title") or "").lower()).strip()
                if norm and norm in seen_titles:
                    continue  # 同轮同名赏金去重
                body = item.get("body") or ""
                # 镜像仓库（external-mirror）：解析出原始 issue，链接指向真源头
                mirror = self.MIRROR_RE.search(body)
                final_url = mirror.group(1) if mirror else item.get("html_url", "")
                if final_url in seen_urls:
                    continue
                seen.add(item["id"])
                seen_titles.add(norm)
                seen_urls.add(final_url)
                repo_count[repo_short] = repo_count.get(repo_short, 0) + 1
                amount = None
                for rx in self.BOUNTY_PATTERNS:
                    m = rx.search(body)
                    if m:
                        amount = float(m.group(1).replace(",", ""))
                        break
                lead = Lead(
                    uid=f"gh_{item['id']}",
                    source=self.name,
                    title=item.get("title", ""),
                    url=final_url,
                    description=body[:6000],
                    budget_min=amount, budget_max=amount,
                    posted_at=_iso(item.get("created_at")),
                    raw={"repo": repo_url,
                         "mirror_of": mirror.group(1) if mirror else None,
                         "labels": [l["name"] for l in item.get("labels", [])],
                         "comments": item.get("comments", 0)},
                )
                leads.append(lead)
        return leads


class HNCrawler(BaseCrawler):
    """Hacker News：招聘/外包/自由职业帖（Algolia 公开 API，1万次/小时）。"""
    name = "hn"
    QUERIES = ["who is hiring", "freelance", "hiring python",
               "looking for a developer", "contract work", "paying"]

    def fetch(self) -> list:
        leads = []
        cutoff = int(datetime.now(timezone.utc).timestamp()) - 30 * 86400
        for q in self.QUERIES:
            r = http.get("https://hn.algolia.com/api/v1/search",
                         params={"query": q, "tags": "story", "hitsPerPage": 25,
                                 "numericFilters": f"created_at_i>{cutoff}"},
                         min_interval=1.5)
            for hit in r.json().get("hits", []):
                oid = hit.get("objectID")
                if not oid:
                    continue
                title = hit.get("title") or ""
                desc = hit.get("story_text") or hit.get("comment_text") or ""
                url = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
                leads.append(Lead(
                    uid=f"hn_{oid}", source=self.name, title=title, url=url,
                    description=strip_html(desc)[:4000],
                    posted_at=_iso(hit.get("created_at_i")),
                    raw={"points": hit.get("points"), "author": hit.get("author")},
                ))
        return leads


class StackOverflowCrawler(BaseCrawler):
    """Stack Overflow 上疑似外包/委托的提问（SE 公开 API，300次/天）。"""
    name = "stackoverflow"
    QUERIES = ["freelance", "hiring", "contract", "paid project", "someone to write"]

    def fetch(self) -> list:
        leads = []
        for q in self.QUERIES:
            r = http.get("https://api.stackexchange.com/2.3/search/advanced",
                         params={"site": "stackoverflow", "order": "desc",
                                 "sort": "creation", "pagesize": 20,
                                 "tagged": "python", "q": q, "filter": "withbody"},
                         min_interval=2.0)
            data = r.json()
            if data.get("error_id"):
                raise RuntimeError(f"StackExchange: {data.get('error_message')}")
            for item in data.get("items", []):
                leads.append(Lead(
                    uid=f"so_{item['question_id']}", source=self.name,
                    title=item.get("title", ""), url=item.get("link", ""),
                    description=strip_html(item.get("body", ""))[:4000],
                    posted_at=_iso(item.get("creation_date")),
                    raw={"score": item.get("score"), "answers": item.get("answer_count")},
                ))
        return leads


class RedditCrawler(BaseCrawler):
    """Reddit r/forhire 等版块。2026-05 起未认证 .json 已 403，需官方 OAuth。"""
    name = "reddit"
    SUBS = ["forhire", "slavelabour", "freelance"]

    def fetch(self) -> list:
        leads = []
        last_err = None
        for sub in self.SUBS:
            try:
                r = http.get(
                    f"https://www.reddit.com/r/{sub}/search.json",
                    params={"q": "hiring", "restrict_sr": 1, "sort": "new",
                            "limit": 25, "t": "month"},
                    min_interval=3.0, retries=2,
                    headers={"Accept": "application/json"},
                )
                for child in r.json().get("data", {}).get("children", []):
                    p = child.get("data", {})
                    leads.append(Lead(
                        uid=f"rd_{p.get('id')}", source=self.name,
                        title=p.get("title", ""),
                        url=f"https://www.reddit.com{p.get('permalink', '')}",
                        description=(p.get("selftext") or "")[:4000],
                        posted_at=_iso(p.get("created_utc")),
                        raw={"sub": sub, "score": p.get("score")},
                    ))
            except Exception as e:
                last_err = e
        if not leads and last_err:
            raise RuntimeError(
                f"Reddit 未认证 JSON 接口已失效(403)，需申请官方 OAuth：{last_err}"
            )
        return leads
