"""PayPal REST 收款。

实测结论（2026-11，live 凭据）：
- OAuth client_credentials ✅
- Invoicing API（开票→发客户邮箱→在线支付）✅ 本系统主收款轨道
- Checkout Payment-Resources / Orders ❌ 403 NOT_AUTHORIZED（应用未开通 Checkout）
因此 create_payment_link 失败时自动降级为发票轨道。
"""
import time

import requests

from ..config import env


class PayPal:
    def __init__(self):
        self.mode = env("PAYPAL_MODE", "live")
        self.base = ("https://api-m.paypal.com" if self.mode == "live"
                     else "https://api-m.sandbox.paypal.com")
        self.client_id = env("PAYPAL_CLIENT_ID")
        self.secret = env("PAYPAL_CLIENT_SECRET")
        self._tok = {"t": 0.0, "v": ""}

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.secret)

    def _token(self) -> str:
        if time.time() - self._tok["t"] < 3000 and self._tok["v"]:
            return self._tok["v"]
        r = requests.post(
            f"{self.base}/v1/oauth2/token",
            auth=(self.client_id, self.secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"}, timeout=30,
        )
        r.raise_for_status()
        self._tok = {"t": time.time(), "v": r.json()["access_token"]}
        return self._tok["v"]

    def _hdr(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json"}

    def health(self) -> dict:
        """能力探测：token + 发票 + 支付链接（只读 GET）。"""
        if not self.configured:
            return {"ok": False, "mode": self.mode,
                    "error": "PAYPAL_CLIENT_ID/SECRET 未配置"}
        try:
            self._token()
        except Exception as e:
            return {"ok": False, "mode": self.mode, "error": str(e)[:200]}
        caps = {}
        try:
            r = requests.get(f"{self.base}/v2/invoicing/invoices",
                             params={"page": 1, "page_size": 1,
                                     "total_required": "true"},
                             headers=self._hdr(), timeout=25)
            caps["invoicing"] = r.status_code == 200
        except Exception:
            caps["invoicing"] = False
        try:
            r = requests.get(f"{self.base}/v1/checkout/payment-resources",
                             params={"page": 1, "page_size": 1},
                             headers=self._hdr(), timeout=25)
            caps["payment_link"] = r.status_code == 200
        except Exception:
            caps["payment_link"] = False
        return {"ok": True, "mode": self.mode, "base": self.base,
                "capabilities": caps}

    # ---------- 轨道 1：发票（主轨道） ----------
    def create_invoice(self, amount: float, description: str = "",
                       currency: str = "USD", recipient_email: str = "",
                       invoice_number: str = "") -> dict:
        items = [{
            "name": (description or "AutoBiz service")[:60],
            "description": (description or "AutoBiz delivery")[:180],
            "unit_amount": {"currency_code": currency, "value": f"{amount:.2f}"},
            "quantity": "1",
        }]
        body = {
            "detail": {
                "invoice_number": invoice_number or f"AB-{int(time.time())}",
                "currency_code": currency,
                "note": "Thank you for your business.",
                "term": "Net 7",
            },
            "invoicer": {"name": {"given_name": "AutoBiz",
                                  "surname": "Studio"}},
            "items": items,
        }
        if recipient_email:
            body["primary_recipients"] = [{
                "billing_info": {"email_address": recipient_email}}]
        r = requests.post(f"{self.base}/v2/invoicing/invoices", json=body,
                          headers=self._hdr(), timeout=30)
        r.raise_for_status()
        data = r.json() if r.content else {}
        # PayPal 创建成功返回 201 + {"href": ".../invoices/INV2-..."}
        inv_id = data.get("id") or ""
        if not inv_id and isinstance(data, dict) and data.get("href"):
            inv_id = data["href"].rstrip("/").split("/")[-1]
        if not inv_id:
            loc = r.headers.get("Location") or ""
            inv_id = loc.rstrip("/").split("/")[-1]
        status = ""
        if inv_id:
            try:
                g = requests.get(f"{self.base}/v2/invoicing/invoices/{inv_id}",
                                 headers=self._hdr(), timeout=25)
                status = g.json().get("status", "")
            except Exception:
                status = ""
        return {"id": inv_id, "status": status,
                "invoice_number": invoice_number or "",
                "amount": amount, "currency": currency}

    def send_invoice(self, invoice_id: str, email: str = "") -> dict:
        """把发票发到客户邮箱（不填邮箱则发给发票上已有收件人）。"""
        payload = {"send_to_invoicer": False}
        if email:
            payload["subject"] = "Invoice from AutoBiz Studio"
            payload["note"] = "Payment link included, thank you!"
        r = requests.post(
            f"{self.base}/v2/invoicing/invoices/{invoice_id}/send",
            json=payload, headers=self._hdr(), timeout=30)
        r.raise_for_status()
        data = r.json()
        return {"ok": True, "status": data.get("status"),
                "detail": str(data.get("statusDetail", ""))[:200]}

    def list_invoices(self, page_size: int = 10) -> list:
        r = requests.get(f"{self.base}/v2/invoicing/invoices",
                         params={"page": 1, "page_size": page_size,
                                 "total_required": "true"},
                         headers=self._hdr(), timeout=30)
        r.raise_for_status()
        return r.json().get("items", [])

    # ---------- 轨道 2：Payment Link（当前应用未授权，失败自动降级） ----------
    def create_payment_link(self, amount: float, description: str = "",
                            currency: str = "USD",
                            reference: str = "") -> dict:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                "description": (description or "AutoBiz service")[:120],
                "custom_id": reference or "autobiz",
            }],
            "payment_source": {"paypal": {"experience_context": {
                "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                "return_url": env("PAYPAL_RETURN_URL", "http://localhost:8787/thanks"),
                "cancel_url": env("PAYPAL_RETURN_URL", "http://localhost:8787/thanks"),
            }}},
        }
        r = requests.post(
            f"{self.base}/v1/checkout/payment-resources", json=body,
            headers={**self._hdr(),
                     "PayPal-Request-Id": reference or f"autobiz-{int(time.time())}"},
            timeout=30)
        r.raise_for_status()
        data = r.json()
        links = {l.get("rel"): l.get("href") for l in data.get("links", [])}
        return {"id": data.get("id"), "status": data.get("status"),
                "approve_url": links.get("approve")}

    def charge(self, amount: float, description: str = "",
               currency: str = "USD", recipient_email: str = "",
               reference: str = "") -> dict:
        """最优可用轨道：优先 Payment Link，403 则自动降级为发票。"""
        try:
            link = self.create_payment_link(amount, description, currency,
                                            reference)
            return {"mode": "payment_link", **link}
        except requests.HTTPError as e:
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code in (401, 403):
                inv = self.create_invoice(amount, description, currency,
                                          recipient_email, reference)
                sent = None
                if recipient_email:
                    try:
                        sent = self.send_invoice(inv["id"], recipient_email)
                    except Exception as se:
                        sent = {"error": str(se)[:200]}
                return {"mode": "invoice", "invoice": inv, "sent": sent}
            raise
