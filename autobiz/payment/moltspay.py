"""MoltsPay 适配器（x402/CDP 协议，支持 USDC/USDT 与支付宝/微信法币）。

文档: https://moltspay.com/docs/dev-guide/
能力: 生成数字产品货架 manifest（npx moltspay start 即可开卖）、
      连通性验证、凭据就绪检查。
"""
import json
import re
from pathlib import Path

from .. import http
from ..config import ROOT, env

SERVICES_FILE = ROOT / "services" / "moltspay.services.json"

# 产品货架：与本系统交付能力一一对应的数字产品（AI 文本处理脚本/爬虫交付物）
PRODUCTS = [
    {"id": "python-scraper-delivery", "type": "file_download",
     "name": "Custom Python Scraper (any site -> clean JSON/CSV)",
     "description": "Targeted crawler with retries, proxy rotation, docs. 3-day delivery.",
     "price": 99.00, "currency": "USDC", "acceptedCurrencies": ["USDC", "USDT"]},
    {"id": "ai-text-pipeline", "type": "file_download",
     "name": "AI Text Processing Pipeline",
     "description": "Cleaning, extraction, classification, summarization on your data.",
     "price": 149.00, "currency": "USDC", "acceptedCurrencies": ["USDC", "USDT"]},
    {"id": "automation-script", "type": "file_download",
     "name": "Automation Script (API / scheduler / webhook)",
     "description": "Unattended daily job with alerting. 48-hour delivery.",
     "price": 79.00, "currency": "USDC", "acceptedCurrencies": ["USDC", "USDT"]},
    {"id": "dashboard-report", "type": "file_download",
     "name": "Lightweight Dashboard / Report Generator",
     "description": "One-file HTML dashboard fed by your API or CSV.",
     "price": 129.00, "currency": "USDC", "acceptedCurrencies": ["USDC", "USDT"]},
]

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class MoltsPay:
    def __init__(self):
        self.api_key = env("MOLTSPAY_API_KEY")
        self.secret = env("MOLTSPAY_SECRET")
        self.wallet = env("MOLTSPAY_WALLET")

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.secret)

    @property
    def key_format_ok(self) -> bool:
        """文档要求 CDP_API_KEY_ID 为 UUID 或 organizations/... 格式。"""
        return bool(_UUID_RE.match(self.api_key)
                    or self.api_key.startswith("organizations/"))

    def connectivity(self) -> dict:
        """验证与 moltspay.com 服务目录的连通性（公开 JSON 注册表）。"""
        try:
            r = http.get("https://moltspay.com/registry/services",
                         retries=1, min_interval=0, timeout=20,
                         headers={"Accept": "application/json"})
            data = r.json()
            services = data.get("services", []) if isinstance(data, dict) else data
            return {"ok": True, "services_listed": len(services)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def write_manifest(self) -> Path:
        """生成/更新数字产品货架清单，配合 `npx moltspay start` 即可对外售卖。"""
        SERVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "provider": {
                "name": env("MOLTSPAY_PROVIDER_NAME", "AutoBiz Studio"),
                "wallet": self.wallet or "0xSET_YOUR_WALLET_ADDRESS",
            },
            "services": PRODUCTS,
        }
        SERVICES_FILE.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return SERVICES_FILE

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "key_format_ok": self.key_format_ok if self.configured else None,
            "wallet_set": bool(self.wallet),
            "connectivity": self.connectivity(),
            "manifest": str(self.write_manifest().relative_to(ROOT))
            if self.configured else None,
            "docs": "https://moltspay.com/docs/dev-guide/",
            "note": ("凭证为 CDP 格式(UUID+secret)，与官方文档一致；"
                     "设置 MOLTSPAY_WALLET 后即可 npx moltspay start 开卖"
                     if self.configured else "未配置 MOLTSPAY_API_KEY/SECRET"),
        }
