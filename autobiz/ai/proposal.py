"""提案/投标文案生成：LLM 可用则调 LLM，否则用高转化本地模板（英文，面向海外客户）。"""
from ..config import CFG
from ..crawlers.base import keyword_hits
from . import llm

_CAPABILITIES = {
    "scrap": ("Production-grade Python scrapers (requests / Playwright / Scrapy) "
              "with retries, proxy rotation and clean JSON/CSV output",
              "scraping"),
    "automat": ("Scheduled automation jobs and API integrations that run "
                "unattended with error alerting", "automation"),
    "ai": ("LLM text pipelines: cleaning, extraction, classification, "
           "summarization and RAG-ready chunking", "AI text"),
    "llm": ("LLM text pipelines: cleaning, extraction, classification, "
            "summarization and RAG-ready chunking", "AI text"),
    "data": ("Data cleaning, dedup and structured export from messy "
             "sources (Excel, HTML, PDF)", "data processing"),
    "dashboard": ("Lightweight dashboards and reporting scripts",
                  "dashboard"),
    "api": ("REST API integrations, webhooks and payment wiring "
            "(PayPal / Stripe)", "API work"),
}

_SYSTEM = ("You are a senior freelance developer writing a short, concrete "
           "proposal for a client. No fluff, no exaggeration, English, "
           "under 150 words, end with one sharp scoping question.")

_TEMPLATE = """Hi,

I read "{title}" — this sits right in my wheelhouse.

- {cap1}
- {cap2}
- {cap3}

I can start today and send a first working version within {eta}. Reply with a
sample input/output and I'll come back with a fixed price and a concrete plan
within a few hours.

Best,
{sender}
"""

_TEMPLATE_JOB = """Hi,

I saw "{title}" and my hands-on background maps straight onto the core of it.

- {cap1}
- {cap2}
- {cap3}

I'm available to start this week, and I'm happy to do a short paid trial task
or walk through a relevant sample before you decide.

Best,
{sender}
"""

_JOB_SOURCES = {"remoteok", "arbeitnow", "remotive", "himalayas", "jobicy"}


def _pick_capabilities(title: str, desc: str) -> list:
    blob = f"{title} {desc}".lower()
    caps, labels = [], []
    for key, (cap, label) in _CAPABILITIES.items():
        if key in blob and label not in labels:
            caps.append(cap)
            labels.append(label)
        if len(caps) == 3:
            break
    defaults = [
        "Clean, tested Python deliverables with a short README so anyone can rerun it",
        "Clear daily progress updates and a milestone demo before final delivery",
        "Free post-delivery fixes until the script behaves as agreed",
    ]
    while len(caps) < 3:
        caps.append(defaults[len(caps)])
    return caps


def _eta(budget_max) -> str:
    v = budget_max or 0
    return "5-7 days" if v >= 500 else "3-4 days" if v >= 100 else "2-3 days"


def generate(lead: dict) -> tuple:
    """返回 (content, method)。"""
    title = lead.get("title") or "your project"
    desc = (lead.get("description") or "")[:1500]
    caps = _pick_capabilities(title, desc)
    sender = CFG["proposal"]["sender_name"]
    is_job = lead.get("source") in _JOB_SOURCES

    if llm.available():
        try:
            style = ("This is a full-time/contract role posting: write a "
                     "concise cover letter, no delivery-time or fixed-price "
                     "language."
                     if is_job else
                     "This is a gig/bounty: emphasize fast fixed-price "
                     "delivery.")
            prompt = (f"Project title: {title}\n\nClient brief:\n{desc}\n\n"
                      f"Capabilities to emphasize: {'; '.join(caps)}\n"
                      f"Sender: {sender}. Delivery ETA: {_eta(lead.get('budget_max'))}.\n"
                      f"{style}")
            return llm.chat(_SYSTEM, prompt, temperature=0.7), "llm"
        except Exception as e:
            print(f"  [!] LLM 调用失败，回退模板: {e}")

    if is_job:
        content = _TEMPLATE_JOB.format(
            title=title[:120], cap1=caps[0], cap2=caps[1], cap3=caps[2],
            sender=sender)
    else:
        content = _TEMPLATE.format(
            title=title[:120], cap1=caps[0], cap2=caps[1], cap3=caps[2],
            eta=_eta(lead.get("budget_max")), sender=sender)
    return content[:CFG["proposal"]["max_chars"]], "template"
