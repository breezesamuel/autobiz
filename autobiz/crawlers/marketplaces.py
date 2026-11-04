"""接单平台适配层：Upwork（浏览器游客态）/ Fiverr / Contra。

Upwork 调研结论（实测 2026-11-04）：
- API 密钥路径：developers.upwork.com → GraphQL endpoint
- **无需密钥路径**：真实浏览器游客态（headless chromium）可直接读职位卡片
- 搜索页 URL：https://www.upwork.com/nx/search/jobs/?q=<keywords>&sort=recency&page=N
- DOM：article.job-tile → innerText + <a href="/jobs/xxx_~<id>/">
- 每页 10 条，&page=N 翻页
- requests 直抓会返回 403 Challenge 页（需 JavaScript 渲染），Playwright 可过
"""
import re

from ..config import env
from .base import BaseCrawler, Lead, keyword_hits, strip_html


class NeedAuthError(RuntimeError):
    """需要平台授权才能继续。"""


class UpworkCrawler(BaseCrawler):
    """Upwork 职位抓取：requests 直抓优先，Playwright 游客态兜底。"""
    name = "upwork"
    # 与其他 crawler 共享的关键词组合（复用 config.keywords）
    DEFAULT_QUERIES = ["python scraper", "python automation", "python ai agent",
                       "playwright selenium", "data extraction", "web scraping"]
    MAX_PAGES = 3  # 每 query 抓 3 页 = 30 条

    def fetch(self) -> list:
        try:
            return self._fetch_browser()
        except Exception as e:
            raise NeedAuthError(
                f"UPWORK 浏览器抓取失败：{e}。"
                "若未安装 chromium，运行: python -m playwright install chromium"
            )

    # ---- Playwright 游客态路径（主路径） ----
    def _fetch_browser(self) -> list:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("playwright 未安装")

        from ..config import CFG
        cfg_queries = CFG["crawl"].get("keywords", [])
        keywords = cfg_queries[:] if cfg_queries else self.DEFAULT_QUERIES
        # 把 keywords 拆成 4 个组合 query（避免单词过短）
        queries = []
        for i in range(0, min(len(keywords), 12), 3):
            chunk = " ".join(keywords[i:i+3])
            if len(chunk) > 5:
                queries.append(chunk)
        if not queries:
            queries = self.DEFAULT_QUERIES[:2]

        leads, seen_uids, seen_urls = [], set(), set()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"),
                locale="en-US",
                viewport={"width": 1280, "height": 720},
            )
            page = ctx.new_page()
            for q in queries:
                for pageno in range(1, self.MAX_PAGES + 1):
                    url = f"https://www.upwork.com/nx/search/jobs/?q={'+'.join(q.split())}&sort=recency&page={pageno}"
                    try:
                        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        if resp.status != 200:
                            break
                        page.wait_for_timeout(4000)  # 动态渲染
                    except Exception:
                        break
                    cards = page.query_selector_all("article.job-tile")
                    for card in cards:
                        lead = self._parse_card(card)
                        if not lead:
                            continue
                        if lead.uid in seen_uids or lead.url in seen_urls:
                            continue
                        seen_uids.add(lead.uid)
                        seen_urls.add(lead.url)
                        leads.append(lead)
                    # 简单去重：下一页没新卡就翻下一个 query
                    if not cards:
                        break
            browser.close()
        return leads

    def _parse_card(self, card) -> Lead | None:
        import re
        # 精准选择器：h2.job-tile-title 内的 <a> 拿标题和 URL
        title_el = card.query_selector("h2.job-tile-title a") or card.query_selector("a")
        if not title_el:
            return None
        title = (title_el.inner_text() or "").strip()
        # Upwork 把高亮 span 的 class 属性渲染进了 href slug，污染 title/url
        title = re.sub(r"span-class-[-\w]+-span-", "", title).strip()
        href = title_el.get_attribute("href") or ""
        if not title or "/jobs/" not in href:
            return None
        # 解析 job id
        m = re.search(r"~(\d+)", href)
        job_id = m.group(1) if m else href.split("/")[-2][:40]
        full_url = f"https://www.upwork.com{href.split('?')[0]}"

        # 整张卡 inner_text 扫预算和 posted 时间
        text = card.inner_text() or ""
        posted_at = ""
        pm = re.search(r"Posted\s+(.+?)\s+ago", text)
        if pm:
            posted_at = pm.group(0)
        # 预算：Fixed price $xx 或 $xxx,xxx.xx
        budget_min = budget_max = None
        currency = "USD"
        # 先找 "Est. budget:\n$xxx" 格式
        bm = re.search(r"Est\.\s+budget[^\$]*\$\s*([\d,]+(?:\.\d+)?)", text)
        if bm:
            budget_min = budget_max = float(bm.group(1).replace(",", ""))
        else:
            # 兜底：找第一个 $ 数字
            bm2 = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", text)
            if bm2:
                v = float(bm2.group(1).replace(",", ""))
                budget_min = budget_max = v if v >= 30 else None  # 低于 $30 可能是单价说明

        # 描述：标题行之后、预算之后的段落（去掉 Posted/Hourly/标签）
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        desc_parts = []
        skip = {"Posted", "Hourly", "Fixed", "price", "Intermediate", "Expert",
                "Entry", "Est.", "Less"}
        for line in lines:
            if line.startswith("Posted") or line == title or line in ("Hourly", "Fixed price"):
                continue
            if any(line.startswith(s) for s in ("Est.", "Less than", "More than")):
                continue
            if line.startswith("$"):
                continue
            desc_parts.append(line)
            if len(desc_parts) > 4 and len(line.split()) <= 2:
                break  # 标签行
        description = " ".join(desc_parts).strip()[:3000]

        return Lead(
            uid=f"up_{job_id}",
            source=self.name,
            title=title,
            url=full_url,
            description=description,
            budget_min=budget_min,
            budget_max=budget_max,
            currency=currency,
            posted_at=posted_at,
            raw={"job_href": href, "raw_text": text[:1000]},
        )


class FiverrCrawler(BaseCrawler):
    name = "fiverr"

    def fetch(self) -> list:
        raise NeedAuthError(
            "FIVERR 无公开读取 API（仅 Affiliate 追踪接口）。"
            "同类需求已由 RemoteOK/HN/Stack Overflow 等源覆盖。"
        )


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
