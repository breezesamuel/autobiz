"""本地仪表盘：零第三方依赖的 HTTP 服务（KPI / 线索 / 提案 / 收款）。"""
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db
from .config import CFG, ROOT

STATIC = ROOT / "static" / "dashboard.html"
_PAY_CACHE = {"t": 0.0, "data": None}


def _payments_status() -> dict:
    if time.time() - _PAY_CACHE["t"] < 300 and _PAY_CACHE["data"]:
        return _PAY_CACHE["data"]
    from .payment.moltspay import MoltsPay
    from .payment.paypal import PayPal
    data = {"paypal": PayPal().health(), "moltspay": MoltsPay().status()}
    _PAY_CACHE.update(t=time.time(), data=data)
    return data


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _html(self, text: str, code: int = 200) -> None:
        self._send(code, text.encode("utf-8"), "text/html; charset=utf-8")

    def do_GET(self) -> None:
        u = urlparse(self.path)
        path = u.path
        if path in ("/", "/index.html"):
            if STATIC.exists():
                self._html(STATIC.read_text(encoding="utf-8"))
            else:
                self._html("<h1>dashboard.html missing</h1>", 404)
            return
        if path == "/api/summary":
            self._json({"summary": db.summary(),
                        "runs": db.recent_runs(12),
                        "payments": _payments_status()})
            return
        if path == "/api/leads":
            qs = parse_qs(u.query)
            status = (qs.get("status") or [""])[0]
            q = (qs.get("q") or [""])[0].lower()
            min_score = int((qs.get("min_score") or ["0"])[0])
            limit = int((qs.get("limit") or ["100"])[0])
            leads = db.all_leads()
            out = []
            for l in leads:
                if status and l["status"] != status:
                    continue
                if min_score and (l["score"] or 0) < min_score:
                    continue
                if q and q not in (l["title"] or "").lower() \
                        and q not in (l["description"] or "").lower():
                    continue
                out.append({k: l.get(k) for k in
                            ("id", "source", "title", "url", "score",
                             "status", "budget_min", "budget_max",
                             "currency", "posted_at", "score_reasons")})
                if len(out) >= limit:
                    break
            self._json({"leads": out, "total": len(out)})
            return
        m = re.match(r"^/api/lead/(\d+)$", path)
        if m:
            lead = db.get_lead(int(m.group(1)))
            if not lead:
                self._json({"error": "not found"}, 404)
                return
            lead.pop("raw_json", None)
            self._json({"lead": lead, "proposals": db.proposals_for(lead["id"])})
            return
        if path == "/api/payments":
            self._json(_payments_status())
            return
        if path == "/thanks":
            self._html("<h2 style='font-family:sans-serif'>Payment received. "
                       "Thank you!</h2>")
            return
        self._json({"error": "unknown endpoint"}, 404)

    def do_POST(self) -> None:
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            self._json({"error": "bad json"}, 400)
            return

        m = re.match(r"^/api/leads/(\d+)/status$", u.path)
        if m:
            status = body.get("status", "")
            if status not in ("new", "shortlisted", "proposed", "sent",
                              "won", "lost", "excluded"):
                self._json({"error": f"invalid status {status}"}, 400)
                return
            db.set_status(int(m.group(1)), status)
            self._json({"ok": True, "status": status})
            return

        if u.path == "/api/revenue":
            try:
                rid = db.add_revenue(
                    float(body["amount"]), body.get("channel", "manual"),
                    body.get("note", ""), body.get("currency", "USD"),
                    body.get("status", "confirmed"))
                self._json({"ok": True, "id": rid})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if u.path == "/api/paylink":
            from .payment.paypal import PayPal
            try:
                result = PayPal().charge(
                    float(body["amount"]), body.get("description", ""),
                    body.get("currency", "USD"),
                    body.get("email", ""),
                    body.get("reference", ""))
                self._json({"ok": True, **result})
            except Exception as e:
                self._json({"ok": False, "error": str(e)[:400]}, 502)
            return

        self._json({"error": "unknown endpoint"}, 404)

    def log_message(self, fmt, *args) -> None:
        pass


def serve(port: int = None) -> None:
    port = port or CFG["app"]["dashboard_port"]
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[dashboard] http://localhost:{port}  (Ctrl+C 退出)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] 已停止")
