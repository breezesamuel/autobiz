"""爬虫基座：Lead 数据结构、预算解析、关键词过滤。"""
import re
from dataclasses import dataclass, field
from typing import Optional

AMT = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_CURRENCY_MAP = {
    "$": "USD", "us$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
    "¥": "CNY", "rmb": "CNY", "cny": "CNY",
    "aud": "AUD", "a$": "AUD",
}
_RATES = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CNY": 0.14, "AUD": 0.66}

_RANGE_RE = re.compile(
    rf"(?P<c1>\$|US\$|€|£|¥|USD|EUR|GBP|CNY|AUD|A\$)\s*(?P<a>{AMT})\s*"
    r"(?:-|–|—|to|~)\s*"
    rf"(?:(?P<c2>\$|US\$|€|£|¥|USD|EUR|GBP|CNY|AUD|A\$)\s*)?(?P<b>{AMT})",
    re.I,
)
_SUFFIX_RE = re.compile(rf"(?P<a>{AMT})\s*(?P<c1>USD|EUR|GBP|CNY|AUD)\b", re.I)
_PREFIX_RE = re.compile(
    rf"(?P<c1>\$|US\$|€|£|¥|USD|EUR|GBP|CNY|AUD|A\$)\s*(?P<a>{AMT})", re.I
)


def _cur(token: str) -> str:
    return _CURRENCY_MAP.get(token.lower(), "USD")


def to_usd(amount: float, currency: str) -> float:
    return round(amount * _RATES.get(currency, 1.0), 2)


def parse_budget(text: str) -> Optional[tuple]:
    """从自由文本提取预算，返回 (min, max, currency)；无预算返回 None。"""
    if not text:
        return None
    m = _RANGE_RE.search(text)
    if m:
        a, b = float(m.group("a").replace(",", "")), float(m.group("b").replace(",", ""))
        if a > b:
            a, b = b, a
        if 1 <= b <= 500000:
            return (a, b, _cur(m.group("c1")))
    for rx in (_SUFFIX_RE, _PREFIX_RE):
        m = rx.search(text)
        if m:
            a = float(m.group("a").replace(",", ""))
            if 1 <= a <= 500000:
                return (a, a, _cur(m.group("c1")))
    return None


@dataclass
class Lead:
    uid: str
    source: str
    title: str
    url: str
    description: str = ""
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    currency: str = "USD"
    posted_at: str = ""
    raw: dict = field(default_factory=dict)


def keyword_hits(text: str, keywords: list) -> list:
    low = (text or "").lower()
    return [k for k in keywords if k.lower() in low]


def has_exclude(text: str, excludes: list) -> bool:
    low = (text or "").lower()
    return any(k.lower() in low for k in excludes)


def strip_html(html: str) -> str:
    if not html:
        return ""
    txt = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"&nbsp;?", " ", txt)
    txt = re.sub(r"&amp;", "&", txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


class BaseCrawler:
    name = "base"

    def fetch(self) -> list:
        raise NotImplementedError
