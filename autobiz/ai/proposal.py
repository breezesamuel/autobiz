"""提案/投标文案生成（V2）：让 LLM 从客户 brief 里自己挑卖点，不硬塞能力。

核心改进：
- V1：prompt 里硬塞 "Capabilities to emphasize: scrap + llm + api" → 每个提案都在说 LLM pipeline
- V2：prompt 里只给完整客户 brief，让 LLM 自己读 → 客户提了什么就说什么（Scraping 项目说 Scraping，CRM 项目说 CRM）
"""
import re

from ..config import CFG
from . import llm

_SENDER = CFG["proposal"]["sender_name"] or "AutoBiz Studio"

# 能力词典：用于 fallback 模板，以及给 LLM 提供可选能力参考（但不硬塞）
_CAP_REF = {
    "scrap": "Production-grade Python scrapers (Playwright / Scrapy / requests) with retries, proxy rotation, session management and clean JSON/CSV output",
    "automat": "Scheduled automation jobs and API integrations that run unattended with error alerting",
    "playwright": "Playwright browser automation with headful fallback, anti-bot handling, and session persistence",
    "selenium": "Selenium-based browser automation with custom Chrome profiles",
    "beautifulsoup": "BeautifulSoup / lxml HTML parsing pipelines",
    "api": "REST API integrations, webhooks, OAuth flows and payment wiring (PayPal / Stripe)",
    "data": "Data cleaning, dedup and structured export from messy sources (Excel, HTML, PDF)",
    "llm": "LLM text pipelines: cleaning, extraction, classification, summarization and RAG-ready chunking",
    "n8n": "n8n workflow automation: trigger → action → conditional branches → output",
    "crm": "CRM customization and automation (Zoho / HubSpot / Salesforce / GHL)",
    "pos": "POS / payments integrations (Clover / Verifone / Stripe)",
    "frontend": "React / Next.js / Vue / Flutter mobile apps",
    "backend": "Node.js / Python FastAPI / Go / .NET APIs",
    "mt5": "MetaTrader 5 / MQL5 algorithmic trading bots",
    "solana": "Solana / EVM blockchain bots and integrations",
    "raspberry": "Raspberry Pi / hardware automation with Python",
    "payments": "Payment integrations and billing systems",
    "payment": "Payment integrations and billing systems",
}

_SYSTEM = f"""You are a senior Python engineer writing a 120–180 word Upwork proposal.

READING RULES (most important):
1. Read the client's brief below carefully. Pick exactly 2 or 3 specific requirements they MENTIONED (not things you want to sell them).
2. For each of those, show you can handle it — name the tool / approach. Be specific, no clichés.
3. If they mention authentication → say how you handle auth (sessions, cookies, .env).
4. If they mention anti-bot / CAPTCHA → say you use permitted approaches (no bypass).
5. If they mention confidentiality / security → mention .env, keyring, or encrypted storage.
6. If they mention delivering to another engineer → mention your README/docs + handoff checklist.
7. If they mention a specific tool/language (Playwright, Scrapy, n8n, Zoho, VB6, Solana...) — MENTION THAT EXACT TOOL FIRST.
8. NEVER mention something they didn't ask for. Do NOT bring up LLM pipelines, scraping, or API integrations unless they did.

FORMAT:
- Open with 1 sentence that echoes their core need (show you read the brief).
- 2–3 bullet points, each linking one client requirement → your specific approach.
- 1 sentence on delivery timeline (use the ETA hint).
- 1 scoping question that moves the conversation forward.
- Sign EXACTLY: Best,
{_SENDER}

Tone: confident, concise, no marketing fluff. No "I'm excited", no "extensive experience"."""


def _extract_client_keywords(title: str, desc: str) -> list:
    """从客户 brief 里提取明确提到的技术/需求关键词（给 LLM 做参考，不硬塞）。"""
    blob = f"{title} {desc}".lower()
    hits = []
    for k in _CAP_REF:
        if k in blob and _CAP_REF[k] not in hits:
            hits.append(_CAP_REF[k])
    return hits[:4]


def _eta(budget_max) -> str:
    v = budget_max or 0
    if v >= 5000: return "10–14 days"
    if v >= 1000: return "5–7 days"
    if v >= 300:  return "3–4 days"
    return "2–3 days"


def _polish(text: str, sender: str) -> str:
    lines = [ln for ln in text.strip().splitlines()
             if not re.match(r"^\s*subject\s*:", ln, re.I)]
    out = "\n".join(lines).strip()
    out = re.sub(r"\[YOUR NAME\]|\(YOUR NAME\)|\[Your Contact Information\]|\[[^\]]*name[^\]]*\]",
                 sender, out, flags=re.I)
    if sender not in out:
        out = f"{out.rstrip()}\n\nBest,\n{sender}"
    return out[:CFG["proposal"]["max_chars"]]


def _fallback(title: str, desc: str, budget_max) -> str:
    """LLM 挂了时的 fallback 模板（同样是 V2 风格：从客户 brief 提取）。"""
    keys = _extract_client_keywords(title, desc)
    if not keys:
        keys = [
            "Clean, tested Python deliverables with a short README for handoff",
            "Clear progress updates and a milestone demo before final delivery",
            "Post-delivery fixes until it works as agreed",
        ]
    lines = [f"Hi,",
             f"I read \"{title[:80]}\" — here's how I'd approach it.",
             "",
             f"- {keys[0]}",
             f"- {keys[1] if len(keys) > 1 else keys[0]}",
             f"- {keys[2] if len(keys) > 2 else keys[0]}",
             "",
             f"First working version in {_eta(budget_max)}. What's the target environment?",
             "",
             f"Best,\n{_SENDER}"]
    return "\n".join(lines)


def generate(lead: dict) -> tuple:
    """返回 (content, method)。"""
    title = lead.get("title") or "your project"
    desc = (lead.get("description") or "")[:3500]
    budget_max = lead.get("budget_max")
    sender = _SENDER

    if llm.available():
        try:
            # 给 LLM 客户 brief + 参考关键词（让它自己选，不硬塞）
            refs = _extract_client_keywords(title, desc)
            ref_line = ("Reference capabilities mentioned in your brief: "
                        + "; ".join(refs) + ". " if refs else "")
            prompt = (
                f"PROJECT TITLE: {title}\n\n"
                f"YOUR BRIEF (read carefully):\n{desc}\n\n"
                f"{ref_line}"
                f"Budget hint: ${budget_max} (Fixed). Delivery ETA hint: {_eta(budget_max)}.\n"
                f"Write the proposal now. Sign EXACTLY: Best,\n{sender}"
            )
            raw = llm.chat(_SYSTEM, prompt, temperature=0.4, max_tokens=600)
            if not raw or len(raw) < 40:
                raise RuntimeError("LLM returned too short")
            return _polish(raw, sender), "llm"
        except Exception as e:
            print(f"  [!] LLM 回退模板: {e}")

    return _fallback(title, desc, budget_max), "template"
