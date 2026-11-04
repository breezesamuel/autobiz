"""接单平台适配层：Upwork 官方 API / Fiverr。

调研结论（2026）：
- Upwork：官方 API 需在 developers.upwork.com 申请 access token，无公开 RSS。
- Fiverr：无公开读取 API（只有 Affiliate 追踪 API）。
未配置凭据时抛出 NeedAuthError，流水线如实记录“待授权”，绝不伪造数据。
"""
from ..config import env
from .base import BaseCrawler, Lead, keyword_hits


class NeedAuthError(RuntimeError):
    """需要平台授权才能继续。"""


class UpworkCrawler(BaseCrawler):
    name = "upwork"
    KEY_ENV = "UPWORK_ACCESS_TOKEN"

    def fetch(self) -> list:
        token = env(self.KEY_ENV)
        if not token:
            raise NeedAuthError(
                "UPWORK 待授权：请到 https://developers.upwork.com 申请 API key，"
                "把 access token 写入 .env 的 UPWORK_ACCESS_TOKEN 后自动启用"
            )
        import json as _json
        from .. import http
        query = """
        query SearchJobs($query: String!, $first: Int) {
          marketplaceSearch(talent: BUYER, query: $query, first: $first) {
            nodes { title url postedOn }
          }
        }"""
        r = http.post_json(
            "https://www.upwork.com/api/graphql",
            {"query": query,
             "variables": {"query": "python scraper automation", "first": 30}},
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        nodes = (r.json().get("data", {}).get("marketplaceSearch", {})
                 .get("nodes") or [])
        leads = []
        for i, n in enumerate(nodes):
            if not n or not n.get("title"):
                continue
            leads.append(Lead(
                uid=f"up_{n.get('url') or i}", source=self.name,
                title=n.get("title", ""), url=n.get("url", ""),
                posted_at=str(n.get("postedOn") or ""),
                raw={"source": "upwork_official_api"},
            ))
        return leads


class FiverrCrawler(BaseCrawler):
    name = "fiverr"
    KEY_ENV = "FIVERR_AFFILIATE_KEY"

    def fetch(self) -> list:
        key = env(self.KEY_ENV)
        if not key:
            raise NeedAuthError(
                "FIVERR 无公开读取 API（仅 Affiliate 追踪接口）。"
                "同类需求已由 RemoteOK/HN/Reddit 等源覆盖；"
                "如需接 Fiverr 单，可申请 Affiliate key 写入 .env"
            )
        raise NeedAuthError("Fiverr Affiliate API 不提供 gig 搜索，暂无读取路径")


class ContraCrawler(BaseCrawler):
    """Contra：无官方 API，探测公开搜索页是否可解析（失败即如实上报）。"""
    name = "contra"

    def fetch(self) -> list:
        from .. import http
        r = http.get("https://contra.com/search/opportunities",
                     retries=1, min_interval=2.0)
        text = r.text
        import re
        hits = re.findall(r'"title":"([^"]{10,120})"', text)
        leads = []
        for i, t in enumerate(dict.fromkeys(hits)):
            if keyword_hits(t, ["python", "scrap", "automat", "script", "ai"]):
                leads.append(Lead(
                    uid=f"ct_{i}_{t[:40]}", source=self.name, title=t,
                    url="https://contra.com/search/opportunities",
                    raw={"probe": "public_search_page"},
                ))
        if r.status_code == 403:
            raise NeedAuthError("Contra 公开搜索页返回 403，需登录态")
        return leads
