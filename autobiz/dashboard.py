"""本地仪表盘：零第三方依赖的 HTTP 服务（KPI / 线索 / 提案 / 收款）。"""
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

from . import db
from .config import CFG, ROOT

STATIC = ROOT / "static" / "dashboard.html"
_PAY_CACHE = {"t": 0.0, "data": None}
_start_time = time.time()


def _payments_status() -> dict:
    """支付通道健康检查，300s 缓存。1.5s 硬超时——PayPal health 冷启动要 6.8s 不能让 API 卡死."""
    import concurrent.futures
    if time.time() - _PAY_CACHE["t"] < 300 and _PAY_CACHE["data"]:
        return _PAY_CACHE["data"]
    data = {"paypal": {"ok": False, "error": "未检查"},
            "moltspay": {"ok": False, "error": "未检查"}}

    # 线程池 + 1.5s 硬超时 —— 任何通道慢都先返回降级
    def _paypal_health():
        from .payment.paypal import PayPal
        return PayPal().health()

    def _molts_status():
        from .payment.moltspay import MoltsPay
        return MoltsPay().status()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futs = {
            pool.submit(_paypal_health): "paypal",
            pool.submit(_molts_status): "moltspay",
        }
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=1.5):
                key = futs[fut]
                try:
                    data[key] = fut.result()
                except Exception as e:
                    data[key] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        except concurrent.futures.TimeoutError:
            # 超时了先返回当前已完成的（剩下的保持 ok=False）
            pass

    # 后台继续查一次——下次 cache 就有了
    try:
        from threading import Thread
        def _bg_check():
            try:
                data2 = dict(data)
                from .payment.paypal import PayPal
                data2["paypal"] = PayPal().health()
                from .payment.moltspay import MoltsPay
                data2["moltspay"] = MoltsPay().status()
                _PAY_CACHE.update(t=time.time(), data=data2)
            except Exception:
                pass
        Thread(target=_bg_check, daemon=True).start()
    except Exception:
        pass

    _PAY_CACHE.update(t=time.time(), data=data)
    return data


class RobustServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with address reuse + daemon threads + cap."""
    allow_reuse_address = True
    daemon_threads = True
    max_threads = 64

    def server_close(self):
        try:
            super().server_close()
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    def handle(self):
        """Global exception guard — never let one request kill the server."""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass  # client disconnected — normal, ignore
        except Exception:
            import traceback
            traceback.print_exc()

    def log_message(self, fmt, *args):
        _sys.stderr.write("[dashboard] " + (fmt % args) + "\n")

    def _send(self, code: int, body: bytes, ctype: str, extra_headers: dict = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass  # 客户端提前断了, 忽略

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
        if path == "/api/health":
            """Docker healthcheck + 系统自检."""
            t0 = time.time()
            checks = {}
            # DB 连通 (db 已在模块顶部 import，这里不要再 from . import db —— 会遮蔽！)
            try:
                n = db.get_conn().execute("SELECT COUNT(*) FROM leads").fetchone()[0]
                checks["db"] = {"ok": True, "leads_count": n}
            except Exception as e:
                checks["db"] = {"ok": False, "error": str(e)}
            # PayPal —— 走 _payments_status() 的线程池 + 1.5s 超时 + cache
            try:
                pay = _payments_status()
                checks["paypal"] = pay.get("paypal", {"ok": False, "error": "no data"})
            except Exception as e:
                checks["paypal"] = {"ok": False, "error": str(e)}
            # IMAP (不真连，只验 import)
            checks["imap"] = {"ok": True, "configured": bool(
                os.environ.get("IMAP_HOST") and os.environ.get("IMAP_USER"))}
            # 总状态
            all_ok = all(c.get("ok", True) for c in checks.values())
            self._json({
                "ok": all_ok, "uptime_sec": time.time() - _start_time,
                "checks": checks, "latency_ms": (time.time() - t0) * 1000,
            })
            return

        if path == "/api/summary":
            """系统总览 — 各组件独立 try/except + 8s 硬超时，慢就降级返回部分结果。"""
            import concurrent.futures as _cf
            base: dict = {}

            def _core():
                """必须在 8s 内完成的核心部分。"""
                b = {}
                try:
                    b["summary"] = db.summary()
                except Exception as e:
                    b["summary"] = {"error": f"db.summary: {e}"}
                try:
                    b["runs"] = db.recent_runs(12)
                except Exception as e:
                    b["runs"] = {"error": f"db.recent_runs: {e}"}
                try:
                    b["payments"] = _payments_status()
                except Exception as e:
                    b["payments"] = {"error": f"payments: {e}"}
                # invoicer / crm / product — 慢或 import 失败都不致命
                try:
                    from . import invoicer
                    b["invoicer"] = invoicer.pipeline_value()
                except Exception:
                    pass
                try:
                    from . import crm
                    b["crm"] = crm.full_stats()
                except Exception:
                    pass
                try:
                    from . import product
                    scan = product.frequency_scan(min_threshold=2)
                    b["product_addressable"] = {
                        "total": scan.get("total_addressable_leads", 0),
                        "revenue": scan.get("total_addressable_revenue", 0),
                        "top_skill": (scan.get("by_skill") or [None])[0],
                    }
                except Exception:
                    pass
                # tracker — 独立表，读失败不影响主数据
                try:
                    from . import tracker
                    b["tracker"] = tracker.funnel_stats(days=30)
                except Exception:
                    b["tracker"] = {"error": "tracker unavailable"}
                return b

            executor = _cf.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(_core)
            try:
                base = future.result(timeout=8)
            except _cf.TimeoutError:
                # 超时 — 标记 degraded，返回已完成部分（如果有）
                base = {"timeout": True, "degraded": True,
                        "note": "summary took >8s, partial result only"}
                future.cancel()
            except Exception as e:
                base = {"ok": False, "error": str(e)}
            finally:
                executor.shutdown(wait=False)

            self._json(base)
            return
        if path == "/api/proposals":
            conn = db.get_conn()
            rows = conn.execute("""
                SELECT p.id, p.content, p.method, p.generated_at, p.sent_at,
                       l.id as lead_id, l.source, l.title, l.url,
                       l.score, l.budget_min, l.budget_max, l.status
                FROM proposals p JOIN leads l ON p.lead_id = l.id
                WHERE p.sent_at IS NULL
                ORDER BY COALESCE(l.budget_max, l.budget_min, 0) DESC, l.score DESC
                LIMIT 50
            """).fetchall()
            grouped = {}
            for r in rows:
                d = dict(r)
                grouped.setdefault(d["source"], []).append(d)
            self._json({"total": len(rows), "by_source": grouped})
            return

        if path == "/api/quick-money":
            """最快变现路径：Upwork + 有预算 + 高分 → 按预算 DESC 前 20。"""
            conn = db.get_conn()
            rows = conn.execute("""
                SELECT l.*, p.content as proposal, p.id as proposal_id
                FROM leads l LEFT JOIN proposals p ON p.lead_id = l.id AND p.sent_at IS NULL
                WHERE l.source = 'upwork'
                  AND COALESCE(l.budget_max, l.budget_min, 0) > 0
                  AND l.score >= 30
                ORDER BY COALESCE(l.budget_max, l.budget_min, 0) DESC, l.score DESC
                LIMIT 20
            """).fetchall()
            self._json({"count": len(rows), "leads": [dict(r) for r in rows]})
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
        if path == "/api/tracker":
            from . import tracker
            qs = parse_qs(u.query)
            mode = (qs.get("mode") or ["funnel"])[0]
            days = int((qs.get("days") or ["30"])[0])
            if mode == "funnel":
                self._json({"tracker": tracker.funnel_stats(days=days)})
            elif mode == "channels":
                self._json({"tracker": tracker.top_channels()})
            elif mode == "pending":
                pending = tracker.pending_sends(limit=int((qs.get("limit") or ["50"])[0]))
                # 只返回轻量字段
                self._json({"tracker": [
                    {"id": p["id"], "source": p["source"], "title": p["title"],
                     "budget_max": p.get("budget_max"), "score": p.get("score"),
                     "url": p.get("url")}
                    for p in pending]})
            else:
                self._json({"error": f"unknown mode {mode}"}, 400)
            return
        m = re.match(r"^/api/tracker/lead/(\d+)$", path)
        if m:
            from . import tracker
            self._json({"events": tracker.lead_timeline(int(m.group(1)))})
            return
        m = re.match(r"^/api/lead/(\d+)/enrich$", path)
        if m:
            lead = db.get_lead(int(m.group(1)))
            if not lead:
                self._json({"error": "not found"}, 404)
                return
            from .enrich import enrich_lead
            result = enrich_lead(lead, scrape_homepage=True)
            # 写回
            import json as _j
            raw = _j.loads(lead.get("raw_json") or "{}")
            raw["enrich"] = result
            conn = db.get_conn()
            conn.execute("UPDATE leads SET raw_json=? WHERE id=?",
                         (_j.dumps(raw, ensure_ascii=False)[:50000], lead["id"]))
            conn.commit()
            self._json({"ok": True, "enrichment": result})
            return
        if path == "/api/quick-money":
            from .console import get_quick_money
            qs = parse_qs(u.query)
            lim = int((qs.get("limit") or ["15"])[0])
            leads = get_quick_money(limit=lim)
            for l in leads:
                l.pop("raw_json", None)
            self._json({"count": len(leads), "leads": leads})
            return

        if u.path == "/api/console/quick-money":
            """变现总控：按渠道返回 Pipeline + CSV 路径 + 发送建议。"""
            from .csv_export import export_proposals_csv
            from .config import env as _env
            smtp_host = bool(_env("SMTP_HOST"))
            smtp_from = _env("SMTP_FROM") or ""
            uw_authed = Path(r"F:\2026\diqi\data\.upwork_profile\.authed").exists()

            conn = db.get_conn()
            rows = conn.execute("""
                SELECT l.source,
                       COUNT(*) cnt,
                       SUM(CASE WHEN p.sent_at IS NULL THEN 1 ELSE 0 END) unsent,
                       COALESCE(SUM(CASE WHEN p.sent_at IS NULL
                           THEN COALESCE(l.budget_max,l.budget_min,0) ELSE 0 END),0) unsent_val,
                       SUM(CASE WHEN p.sent_at IS NOT NULL THEN 1 ELSE 0 END) sent
                FROM leads l JOIN proposals p ON p.lead_id = l.id
                GROUP BY l.source ORDER BY unsent_val DESC
            """).fetchall()
            channels = []
            for r in rows:
                d = dict(r)
                try:
                    csv_path = export_proposals_csv(
                        format="mailchimp", source=d["source"], limit=500)
                    d["csv_path"] = csv_path
                    d["csv_size"] = Path(csv_path).stat().st_size
                except Exception:
                    d["csv_path"] = ""
                    d["csv_size"] = 0
                # 发件建议
                if d["source"] == "upwork":
                    d["channel"] = "browser" if uw_authed else "manual_copy"
                    d["advice"] = "复制粘贴到 Upwork Apply 页"
                elif d["source"] in ("hn_show", "producthunt", "hn"):
                    d["channel"] = "cold_email"
                    d["advice"] = "冷触达 — 用 CSV 导入 Mailchimp/Lemlist"
                else:
                    d["channel"] = "cold_email"
                    d["advice"] = "冷触达 — CSV + Mailchimp"
                channels.append(d)

            total_val = sum(c["unsent_val"] for c in channels)
            self._json({
                "total_pipeline_value": total_val,
                "channels": channels,
                "smtp": {"host": smtp_host, "from": smtp_from},
                "upwork_authed": uw_authed,
                "send_ready": smtp_host and smtp_from and uw_authed,
            })
            return

        if path == "/api/pipeline-overview":
            from .console import pipeline_overview
            self._json(pipeline_overview())
            return

        if path == "/api/console/proposals":
            from .console import get_proposal_queue
            qs = parse_qs(u.query)
            src = (qs.get("source") or [""])[0]
            lim = int((qs.get("limit") or ["50"])[0])
            mb = float((qs.get("min_budget") or ["0"])[0])
            items = get_proposal_queue(limit=lim, source=src, min_budget=mb)
            for it in items:
                it.pop("content", None)  # 列表页不返回完整内容
            self._json({"count": len(items), "items": items})
            return

        if path == "/thanks":
            self._html("<h2 style='font-family:sans-serif'>Payment received. "
                       "Thank you!</h2>")
            return

        # ========== PAYWALL / 订阅墙 GET ==========
        if path == "/api/plans":
            from . import paywall as _pw, i18n as _i18n
            qs = parse_qs(u.query)
            lang = (qs.get("lang") or ["en"])[0]
            raw_plans = _pw.list_plans()
            plans = []
            for p in raw_plans:
                tp = _i18n.translate_plan(p, lang)
                tp["features"] = _i18n.translate_features_list(tp.get("features") or [], lang)
                plans.append(tp)
            self._json({"ok": True, "plans": plans, "lang": lang})
            return

        if path == "/api/subscription":
            from . import paywall as _pw, i18n as _i18n
            qs = parse_qs(u.query)
            lang = (qs.get("lang") or ["en"])[0]
            plan = _pw.get_plan()
            limits = _pw.get_limits()
            from . import db as _db
            today_sent = _db.get_conn().execute("""
                SELECT COUNT(*) FROM outreach_events 
                WHERE event='sent' AND DATE(created_at)=DATE('now')
            """).fetchone()[0]
            today_leads = _db.get_conn().execute("""
                SELECT COUNT(*) FROM leads WHERE DATE(fetched_at)=DATE('now')
            """).fetchone()[0]
            all_features = list(_pw.FEATURE_REQUIREMENTS.keys())
            unlocked = [{"key": f, "label": _i18n.feature_name(f, lang),
                         "label_en": _i18n.feature_name(f, "en"),
                         "label_zh": _i18n.feature_name(f, "zh")} for f in all_features if _pw.check(f)]
            locked = [{"key": f, "label": _i18n.feature_name(f, lang),
                       "label_en": _i18n.feature_name(f, "en"),
                       "label_zh": _i18n.feature_name(f, "zh")} for f in all_features if not _pw.check(f)]
            plan_display = {
                "code": plan.get("plan_code") or plan.get("code"),
                "name": _i18n.t(f"pw.{plan.get('plan_code') or plan.get('code','free')}", lang=lang,
                               default=plan.get("name", "Free")),
                "name_zh": _i18n.t(f"pw.{plan.get('plan_code') or plan.get('code','free')}", lang="zh",
                                   default=plan.get("name", "Free")),
                "name_en": _i18n.t(f"pw.{plan.get('plan_code') or plan.get('code','free')}", lang="en",
                                   default=plan.get("name", "Free")),
                "price_monthly": plan.get("price_monthly", 0),
                "price_yearly": plan.get("price_yearly", 0),
            }
            self._json({
                "ok": True,
                "plan": plan_display,
                "limits": limits,
                "usage": {
                    "emails_sent_today": today_sent,
                    "leads_fetched_today": today_leads,
                    "csv_rows_limit": limits.get("max_csv_rows", 0),
                },
                "features": {"unlocked": unlocked, "locked": locked, "all": all_features},
                "lang": lang,
            })
            return

        if path == "/api/subscription/check":
            from . import paywall as _pw
            qs = parse_qs(u.query)
            feature = (qs.get("feature") or [""])[0]
            self._json({"feature": feature, "unlocked": _pw.check(feature)})
            return

        # ========== GLOBAL API REGISTRY ==========
        if path == "/api/apis/global":
            """全球免费 API 总览 — 分类统计 + 来源 + 零 key 计数。"""
            try:
                from .api_global_registry import (
                    API_REGISTRY, NO_KEY_APIS, BY_CATEGORY, BY_SOURCE, FREE_ONLY
                )
                cats = [{"name": c, "count": len(v)} for c, v in sorted(
                    BY_CATEGORY.items(), key=lambda x: -len(x[1]))]
                srcs = [{"name": s, "count": len(v)} for s, v in sorted(
                    BY_SOURCE.items(), key=lambda x: -len(x[1]))]
                self._json({
                    "ok": True,
                    "total": len(API_REGISTRY),
                    "no_key": len(NO_KEY_APIS),
                    "free": len(FREE_ONLY),
                    "categories": cats,
                    "sources": srcs,
                })
            except ImportError as e:
                # FREE_ONLY / stats 缺失 — 降级：用已有符号 + 在线推导
                try:
                    from .api_global_registry import (
                        API_REGISTRY, NO_KEY_APIS, BY_CATEGORY, BY_SOURCE, _is_free
                    )
                    free = [a for a in API_REGISTRY if _is_free(a)]
                    cats = [{"name": c, "count": len(v)} for c, v in sorted(
                        BY_CATEGORY.items(), key=lambda x: -len(x[1]))]
                    self._json({
                        "ok": True, "degraded": True,
                        "note": f"FREE_ONLY/stats import failed: {e}",
                        "total": len(API_REGISTRY),
                        "no_key": len(NO_KEY_APIS),
                        "free": len(free),
                        "categories": cats[:20],
                        "sources": [{"name": s, "count": len(v)}
                                    for s, v in sorted(
                                        BY_SOURCE.items(), key=lambda x: -len(x[1]))[:20]],
                    })
                except Exception as e2:
                    self._json({"ok": False, "error": str(e),
                                "fallback_error": str(e2)}, 500)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/apis/stats":
            """API Registry 全局统计."""
            try:
                from .api_global_registry import stats
                self._json({"ok": True, **stats()})
            except ImportError:
                try:
                    from .api_global_registry import (
                        API_REGISTRY, NO_KEY_APIS, BY_CATEGORY, BY_SOURCE, _is_free
                    )
                    free = [a for a in API_REGISTRY if _is_free(a)]
                    self._json({
                        "ok": True, "degraded": True,
                        "total": len(API_REGISTRY),
                        "no_key": len(NO_KEY_APIS),
                        "free": len(free),
                        "categories": len(BY_CATEGORY),
                        "sources": len(BY_SOURCE),
                        "top_categories": [
                            {"name": c, "count": len(v)}
                            for c, v in sorted(
                                BY_CATEGORY.items(), key=lambda x: -len(x[1]))[:10]
                        ],
                        "top_sources": [
                            {"name": s, "count": len(v)}
                            for s, v in sorted(
                                BY_SOURCE.items(), key=lambda x: -len(x[1]))[:10]
                        ],
                    })
                except Exception as e:
                    self._json({"ok": False, "error": str(e)}, 500)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/apis/free":
            """只看免费 API."""
            try:
                from .api_global_registry import FREE_ONLY
                items = []
                for a in FREE_ONLY[:100]:
                    items.append({
                        "name": a.get("name", "")[:60],
                        "url": a.get("link") or a.get("url", ""),
                        "auth": a.get("auth", ""),
                        "category": a.get("category", "general"),
                    })
                self._json({"ok": True, "total": len(FREE_ONLY), "items": items})
            except ImportError:
                try:
                    from .api_global_registry import API_REGISTRY, _is_free
                    free = [a for a in API_REGISTRY if _is_free(a)]
                    items = []
                    for a in free[:100]:
                        items.append({
                            "name": a.get("name", "")[:60],
                            "url": a.get("link") or a.get("url", ""),
                            "auth": a.get("auth", ""),
                            "category": a.get("category", "general"),
                        })
                    self._json({"ok": True, "degraded": True,
                                "total": len(free), "items": items})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)}, 500)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        m = re.match(r"^/api/apis/category/([^/]+)$", path)
        if m:
            """按分类列出 API."""
            from .api_global_registry import BY_CATEGORY
            cat = unquote(m.group(1))
            # 模糊匹配
            matched = None
            for c in BY_CATEGORY:
                if cat.lower() in c.lower():
                    matched = c
                    break
            if not matched:
                self._json({"ok": False, "error": f"Category not found: {cat}",
                            "available": sorted(BY_CATEGORY.keys())}, 404)
                return
            items = []
            for a in BY_CATEGORY[matched][:100]:
                items.append({
                    "name": a.get("name", "")[:60],
                    "url": a.get("link") or a.get("url", ""),
                    "auth": a.get("auth", ""),
                    "category": a.get("category", matched),
                })
            self._json({"ok": True, "category": matched, "total": len(BY_CATEGORY[matched]),
                        "items": items})
            return

        if path == "/api/apis/search":
            """在全球 API 库中搜索 — ?q=weather&no_key=1&cat=Weather&limit=20."""
            from .api_global_registry import search, BY_CATEGORY
            qs = parse_qs(u.query)
            q = (qs.get("q") or [""])[0].strip()
            cat = (qs.get("cat") or [""])[0].strip()
            no_key = (qs.get("no_key") or ["0"])[0] in ("1", "true", "yes")
            free = (qs.get("free") or ["0"])[0] in ("1", "true", "yes")
            lim = int((qs.get("limit") or ["20"])[0])
            # 字段兼容：新 JSON 用 url, 老硬编码用 link
            results = search(q, no_key_only=no_key, free_only=free, cat=cat or None)[:lim]
            slim = []
            for a in results:
                url = a.get("link") or a.get("url", "")
                auth = a.get("auth", "")
                no_key_flag = auth.lower() in ("no", "none", "")
                slim.append({
                    "name": a.get("name", "")[:60],
                    "category": a.get("category", "general"),
                    "no_key": no_key_flag,
                    "link": url[:100],
                    "auth": auth[:30],
                    "https": a.get("https", False),
                    "free": a.get("free", True),
                })
            self._json({
                "ok": True, "query": q, "category": cat, "no_key_only": no_key,
                "total_matched": len([a for a in results if q.lower() in a.get("name","").lower() or q.lower() in a.get("description","").lower()]),
                "items": slim[:lim],
                "available_categories": sorted(BY_CATEGORY.keys()),
            })
            return

        if path == "/api/apis/no-key":
            """返回所有零 key 免费 API — ?cat=Weather&limit=50."""
            from .api_global_registry import NO_KEY_APIS, BY_CATEGORY
            qs = parse_qs(u.query)
            cat = (qs.get("cat") or [""])[0].strip()
            lim = int((qs.get("limit") or ["50"])[0])
            pool = NO_KEY_APIS
            if cat:
                pool = [a for a in pool if cat.lower() in a["category"].lower()]
            items = [{
                "name": a["name"], "category": a["category"],
                "link": a["link"][:80], "https": a["https"], "cors": a["cors"],
            } for a in pool[:lim]]
            self._json({
                "ok": True, "total": len(pool), "items": items,
                "available_categories": sorted(BY_CATEGORY.keys()),
            })
            return

        # ========== VECTOR / GEOMETRY / CLUSTERING ==========
        if path == "/api/vector/cluster":
            """对 pipeline 里的 shortlisted + score>=40 做向量聚类 + 关键词 + 离群点。"""
            try:
                from .ai.vector import analyze_leads
                from . import db as _db
                qs = parse_qs(u.query)
                k = int((qs.get("k") or ["5"])[0])
                limit = int((qs.get("limit") or ["100"])[0])
                rows = _db.get_conn().execute("""
                    SELECT * FROM leads
                    WHERE status='shortlisted' OR score >= 40
                    ORDER BY score DESC LIMIT ?
                """, (limit,)).fetchall()

                # analyze_leads 可能阻塞（sklearn fit 慢 / tfidf 大矩阵）→ 线程超时 6s
                import concurrent.futures as _cf
                def _run_cluster():
                    return analyze_leads([dict(r) for r in rows], top_k_clusters=k)
                with _cf.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(_run_cluster)
                    try:
                        result = future.result(timeout=6)
                    except _cf.TimeoutError:
                        self._json({
                            "ok": False, "degraded": True,
                            "error": "vector/cluster 超时 (6s) — 向量聚类慢，可能因 sklearn/tfidf 矩阵大",
                            "hint": "缩小 limit 参数 / 或等待 sklearn warmup",
                        }, 504)
                        return
                    except Exception as e:
                        self._json({
                            "ok": False, "degraded": True,
                            "error": f"vector/cluster: {e}",
                        }, 500)
                        return

                # 补充每条 lead 的摘要
                if result.get("ok"):
                    rows_list = [dict(r) for r in rows]
                    diverse = []
                    for idx in result["diverse_indices"]:
                        l = rows_list[idx]
                        diverse.append({
                            "idx": idx, "id": l.get("id"), "score": l.get("score"),
                            "budget": l.get("budget_max") or l.get("budget_min"),
                            "source": l.get("source"), "title": (l.get("title") or "")[:60],
                            "dist": round(result["centroid_distances"][idx], 3),
                        })
                    cluster_leads = {}
                    for i, l in enumerate(rows_list):
                        lbl = result.get("_labels", [])[i] if "_labels" in result else None
                        if lbl is not None:
                            cluster_leads.setdefault(str(lbl), []).append({
                                "id": l.get("id"), "score": l.get("score"),
                                "budget": l.get("budget_max") or l.get("budget_min"),
                                "source": l.get("source"),
                                "title": (l.get("title") or "")[:50],
                            })
                    dists = result["centroid_distances"]
                    if dists:
                        mean_d = sum(dists) / len(dists)
                        outliers = [
                            {"idx": i, "id": rows_list[i].get("id"),
                             "score": rows_list[i].get("score"),
                             "dist": round(d, 3),
                             "title": (rows_list[i].get("title") or "")[:50]}
                            for i, d in enumerate(dists) if d > mean_d * 1.3
                        ]
                    else:
                        outliers = []
                    self._json({
                        "ok": True, "total_analyzed": len(rows_list),
                        "k_clusters": k, "inertia": result["inertia"],
                        "cluster_counts": result["cluster_counts"],
                        "cluster_top_keywords": result["cluster_top_keywords"],
                        "most_diverse": diverse[:10],
                        "outliers": outliers[:5],
                        "vocab_size": result["vocab_size"],
                    })
                else:
                    self._json({"ok": False, "error": result.get("error")})
            except ImportError as e:
                self._json({
                    "ok": False, "degraded": True,
                    "error": f"vector/cluster import: {e}",
                    "hint": "ai.vector 缺依赖 (numpy/sklearn)",
                }, 500)
            except Exception as e:
                self._json({
                    "ok": False, "degraded": True,
                    "error": f"vector/cluster: {e}",
                }, 500)
            return

        if path == "/api/vector/quantiles":
            """真实 DB 预算分位数 (替代手调阶梯权重)。"""
            from .ai.vector import percentile_buckets, quantile_rank
            from . import db as _db
            rows = _db.get_conn().execute(
                "SELECT budget_max, budget_min FROM leads WHERE COALESCE(budget_max, budget_min, 0) > 0 LIMIT 500"
            ).fetchall()
            vals = sorted(float(r[0] or r[1] or 0) for r in rows)
            buckets = percentile_buckets(vals)
            q_50 = [b for b in buckets if b[1] >= 0.50][0] if buckets else (0, 0.50)
            q_65 = [b for b in buckets if b[1] >= 0.65][0] if buckets else (0, 0.65)
            q_80 = [b for b in buckets if b[1] >= 0.80][0] if buckets else (0, 0.80)
            q_90 = [b for b in buckets if b[1] >= 0.90][0] if buckets else (0, 0.90)
            q_97 = [b for b in buckets if b[1] >= 0.97][0] if buckets else (0, 0.97)
            self._json({
                "ok": True, "sample_size": len(vals),
                "range": f"${min(vals):.0f} – ${max(vals):.0f}",
                "quantiles": {
                    "50%": round(q_50[0], 2), "65%": round(q_65[0], 2),
                    "80%": round(q_80[0], 2), "90%": round(q_90[0], 2),
                    "97%": round(q_97[0], 2),
                },
                "buckets": [{"threshold": round(t, 2), "quantile": round(p, 2)}
                            for t, p in buckets],
            })
            return

        if path == "/api/vector/pareto":
            """Pareto 前沿 + 对数效用模型 — 对指定 lead 算最优报价集合。
            ?lead_id=123&budget=3000
            """
            from .ai.vector import pareto_frontier, log_utility
            from .pricing import price_for_lead
            from . import db as _db
            qs = parse_qs(u.query)
            lead_id = int((qs.get("lead_id") or ["0"])[0])
            row = _db.get_conn().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            if not row:
                self._json({"ok": False, "error": "lead not found"}, 404)
                return
            lead = dict(row)
            budget = lead.get("budget_max") or lead.get("budget_min") or 0
            if budget <= 0:
                # 用定价引擎估算
                p = price_for_lead(lead, mode="bid_full")
                budget = p["industry_estimate"]
            options = [
                (round(budget * (1 + x), -2), round(budget * (-x), -2), f"mode={m} ({x*100:+.0f}%)")
                for x, m in [(-0.25, "tight"), (-0.15, "aggressive"), (-0.05, "bid_tight"),
                             (0.0, "bid_full"), (0.10, "premium"), (0.20, "retainer")]
            ]
            pareto = pareto_frontier(options)
            results = []
            for p in pareto:
                u = log_utility(p[0], budget, risk=0.1)
                results.append({
                    "price": p[0], "mode_label": p[2],
                    "profit": p[0], "remaining_customer": max(budget - p[0], 0),
                    "util_us": u["util_us"], "util_customer": u["util_customer"],
                    "util_total": u["util_total"], "direction": u["direction"],
                    "sensitivity": u["sensitivity"],
                })
            self._json({
                "ok": True, "lead_id": lead_id, "budget": budget,
                "pareto_count": len(results), "options_total": len(options),
                "recommendation": results[0] if results else None,
                "pareto_set": results,
            })
            return

        if path == "/api/vector/deep":
            """深度全景分析: 图社区 + PageRank + Isolation Forest + LOF + Pareto 排序。
            ?limit=50&threshold=0.30&trees=50
            """
            from .ai.vector import deep_analyze_leads
            from . import db as _db
            qs = parse_qs(u.query)
            lim = int((qs.get("limit") or ["50"])[0])
            lim = min(lim, 80)  # 硬限 80，防 O(N²) 过载
            thresh = float((qs.get("threshold") or ["0.30"])[0])
            trees = int((qs.get("trees") or ["50"])[0])
            trees = min(trees, 100)

            rows = _db.get_conn().execute(
                "SELECT * FROM leads WHERE score >= 30 "
                "ORDER BY score DESC LIMIT ?", (lim,)
            ).fetchall()
            leads = [dict(r) for r in rows]
            if not leads:
                self._json({"ok": False, "error": "no leads with score>=30"}, 404)
                return

            # 先预计算 budget_usd
            for l in leads:
                l["budget_usd"] = float(l.get("budget_max") or l.get("budget_min") or 0)

            t0 = time.time()
            result = deep_analyze_leads(
                leads,
                label_prop_threshold=thresh,
                iforest_trees=trees,
            )
            elapsed = time.time() - t0

            if not result.get("ok"):
                self._json(result, 500)
                return

            # 精简返回 (不要传原始 _graph)
            result.pop("_graph", None)

            # 补充 top leads 详情
            top_pr = sorted(range(len(leads)), key=lambda i: -result["pagerank"][i])[:5]
            top_anom = sorted(range(len(leads)), key=lambda i: -result["anomaly_iforest"][i])[:3]
            top_bridge = sorted(range(len(leads)), key=lambda i: -result["bridge_scores"][i])[:3]

            result["top_pagerank"] = [
                {"idx": i, "id": leads[i].get("id"),
                 "pr": result["pagerank"][i],
                 "score": leads[i].get("score"),
                 "budget": leads[i].get("budget_usd"),
                 "source": leads[i].get("source"),
                 "title": (leads[i].get("title") or "")[:50]}
                for i in top_pr
            ]
            result["top_anomalies"] = [
                {"idx": i, "id": leads[i].get("id"),
                 "iforest": result["anomaly_iforest"][i],
                 "lof": result["anomaly_lof"][i],
                 "score": leads[i].get("score"),
                 "budget": leads[i].get("budget_usd"),
                 "source": leads[i].get("source"),
                 "title": (leads[i].get("title") or "")[:50]}
                for i in top_anom
            ]
            result["top_bridges"] = [
                {"idx": i, "id": leads[i].get("id"),
                 "bridge": result["bridge_scores"][i],
                 "comm": result["communities"][i],
                 "score": leads[i].get("score"),
                 "source": leads[i].get("source"),
                 "title": (leads[i].get("title") or "")[:50]}
                for i in top_bridge
            ]
            result["elapsed_ms"] = round(elapsed * 1000, 1)

            self._json(result)
            return

        if path == "/api/vector/bayesian":
            """贝叶斯关键词转化率 —— 用 DB 真实历史数据 (outreach → paid) 更新。"""
            from .ai.vector import bayesian_keyword_update
            from . import db as _db

            conn = _db.get_conn()

            # 拿到所有发过的提案 (作为曝光)
            proposals = conn.execute("""
                SELECT p.id, p.content, p.sent_at,
                       l.id as lead_id, l.status, l.source
                FROM proposals p JOIN leads l ON p.lead_id = l.id
                WHERE p.sent_at IS NOT NULL OR l.status IN ('sent','replied','paid','won')
                LIMIT 500
            """).fetchall()

            if not proposals:
                self._json({"ok": True, "note": "no proposals yet", "bayesian": {}})
                return

            # 用 scorer 的 implied + explicit keywords 做 hits
            from .ai.scorer import IMPLIED_PATTERNS
            from autobiz.config import CFG

            cw = CFG["crawl"]
            explicit_kws = set(cw["keywords"])

            keyword_hits_list = []
            outcomes_list = []

            for p in proposals:
                content = (p["content"] or "") + " " + (p["status"] or "") + " " + (p["source"] or "")
                low = content.lower()

                hits = set()
                # Layer 1: 显式
                for kw in explicit_kws:
                    if kw.lower() in low:
                        hits.add(kw)
                # Layer 2: implied patterns
                for pseudo, patterns in IMPLIED_PATTERNS.items():
                    for rx in patterns:
                        if re.search(rx, low):
                            hits.add(pseudo)
                            break

                if not hits:
                    continue

                keyword_hits_list.append(list(hits))
                # outcome: paid/won = 1, 其他 = 0
                outcome = 1 if p["status"] in ("paid", "won") else 0
                outcomes_list.append(outcome)

            if not keyword_hits_list:
                self._json({"ok": True, "note": "no keyword matches in proposals", "bayesian": {}})
                return

            bayes = bayesian_keyword_update(keyword_hits_list, outcomes_list)

            # 排序
            sorted_bayes = sorted(bayes.items(), key=lambda kv: -kv[1]["posterior_mean"])

            self._json({
                "ok": True,
                "total_proposals": len(proposals),
                "with_keywords": len(keyword_hits_list),
                "conversions": sum(outcomes_list),
                "bayesian": {k: v for k, v in sorted_bayes},
                "top_converters": [
                    {"keyword": k, **v} for k, v in sorted_bayes[:5]
                ],
                "bottom_converters": [
                    {"keyword": k, **v} for k, v in sorted_bayes[-3:]
                ],
            })
            return

        if path == "/api/paypal-webhook":
            self._json({"note": "POST endpoint only"})
            return

        # ========== 审计发现 404 的端点 ==========
        if path == "/api/tracker/funnel":
            """漏斗追踪: 各阶段计数 + 转化率。"""
            from . import tracker as _tr
            conn = db.get_conn()
            stages = ["new", "shortlisted", "proposed", "sent", "replied", "paid"]
            counts = {}
            for s in stages:
                try:
                    cnt = conn.execute(
                        "SELECT COUNT(*) FROM leads WHERE status=?", (s,)).fetchone()[0]
                except Exception:
                    cnt = 0
                counts[s] = cnt
            # outreach_events 漏斗
            try:
                funnel_rows = conn.execute("""
                    SELECT action, COUNT(*) as n
                    FROM outreach_events
                    GROUP BY action ORDER BY n DESC
                """).fetchall()
                outreach = {r[0]: r[1] for r in funnel_rows}
            except Exception:
                outreach = {}
            self._json({
                "ok": True, "lead_status": counts, "outreach": outreach,
                "total_leads": sum(counts.values()),
            })
            return

        # ========== AI 销售 Agent / 采购 / 外骨骼 新端点 ==========
        if path == "/api/funnel-simulate":
            """转化漏斗蒙特卡洛模拟 — AI 销售 Agent 核心可视化。
            Query: n=10&seed=4&source=expo
            """
            from . import tracker as _tr
            u = urlparse(self.path)
            q = parse_qs(u.query)
            n = int(q.get("n", [10])[0])
            seed = int(q.get("seed", [4])[0])
            src_filter = q.get("source", [None])[0]

            rates = dict(_tr.DEFAULT_CONVERSION_RATES)
            if src_filter == "expo":
                rates.update(send_to_reply=0.20, reply_to_want=0.35, want_to_paid=0.40)

            sim = _tr.funnel_simulate(n=n, rates=rates, seed=seed)
            # 精简输出（去掉 ASCII visual 省 payload）
            self._json({
                "ok": True,
                "params": sim["params"],
                "stages": sim["stages"],
                "conversion_rate_pct": sim["conversion_rate_pct"],
                "revenue_range": sim["revenue_range"],
                "bottleneck": sim["bottleneck"],
            })
            return

        if path == "/api/procurement-compare":
            """AI 采购智能比价。Query: ?item=exoskeleton | radar | supplies"""
            from . import procurement as _pr
            u = urlparse(self.path)
            q = parse_qs(u.query)
            item = q.get("item", ["exoskeleton"])[0]

            ranks = _pr.compare_suppliers(item)
            summary = _pr.summary()
            self._json({
                "ok": True, "category": item,
                "suppliers": ranks,
                "weights": _pr.PRICING_WEIGHTS,
                "summary": summary,
            })
            return

        if path == "/api/exoskeleton/status":
            """外骨骼控制器状态 + HAK v2.0 注册信息。"""
            from .hardware.exoskeleton import (
                ExoskeletonController, register_with_hak_kernel,
                _leg_fk, inverse_kinematics, IMUReading,
                JOINT_NAMES, STANDING_THETA, SEATED_THETA,
            )
            ctl = ExoskeletonController(mode="mock")
            # 快速模拟站姿
            ctl.set_all_joints(STANDING_THETA)
            fk_x, fk_y = _leg_fk(*STANDING_THETA[:2])
            hak = register_with_hak_kernel()
            self._json({
                "ok": True,
                "mode": ctl.mode,
                "joint_names": JOINT_NAMES,
                "standing_height_m": round(fk_y, 3),
                "seated_height_m": round(_leg_fk(*SEATED_THETA[:2])[1], 3),
                "estop_active": ctl.estop_active,
                "imu": {
                    "roll_deg": ctl.imu.roll_deg,
                    "pitch_deg": ctl.imu.pitch_deg,
                    "unstable": ctl.imu.is_unstable(),
                },
                "hak_registration": hak,
                "ik_sample": {
                    "target": [0.30, 0.55],
                    "solution": inverse_kinematics([0.30, 0.55, 0.55]),
                },
                "standing_trajectory_steps": 10,
                "total_device_classes": hak.get("total_devices"),
            })
            return

        if path == "/api/top-lead-five-dim":
            """Top lead 五维评分 — 雷达图数据源。"""
            from . import db as _db
            from .ai.scorer import score_five_dim
            _c = _db.get_conn()
            rows = _c.execute(
                "SELECT * FROM leads WHERE score > 0 ORDER BY score DESC LIMIT 1"
            ).fetchall()
            if not rows:
                rows = _c.execute(
                    "SELECT * FROM leads WHERE source='expo' ORDER BY id DESC LIMIT 1"
                ).fetchall()
            if not rows:
                self._json({"ok": False, "error": "no leads"})
                return
            lead = dict(rows[0])
            fv = score_five_dim(lead)
            self._json({
                "ok": True,
                "lead": {"id": lead["id"], "uid": lead["uid"], "title": lead["title"][:60],
                         "source": lead["source"], "score": lead["score"]},
                "five_dim": fv,
            })
            return

        if path == "/api/sse/runs":
            """SSE 实时推送 — 后端推送 runs 表新增记录。"""
            import json as _j
            from . import db as _db_sse
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                _c = _db_sse.get_conn()
                last_id = _c.execute(
                    "SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
                last_id = last_id[0] if last_id else 0
                for _ in range(60):
                    import time as _t
                    _t.sleep(2)
                    try:
                        row = _c.execute(
                            "SELECT * FROM runs WHERE id > ? ORDER BY id ASC LIMIT 1",
                            (last_id,)).fetchone()
                        if row:
                            d = dict(row)
                            last_id = d["id"]
                            payload = _j.dumps({
                                "phase": d["phase"], "found": d["found"],
                                "new": d["new"], "errors": bool(d["errors"]),
                                "elapsed": 0, "id": d["id"],
                            }, ensure_ascii=False)
                            self.wfile.write(f"data: {payload}\n\n".encode())
                            self.wfile.flush()
                    except Exception:
                        pass
            except Exception:
                pass
            return

        # ========== tick-stock-panel 真实行情 ==========
        if path == "/api/market/list":
            """监控品种列表 + 实时报价。"""
            from .market_data import list_symbols, _provider_ok
            self._json({"ok": True, "provider": os.environ.get("AUTOZONE_MARKET_PROVIDER","auto"),
                        "provider_ok": {k: {"ok": v[0], "ts": v[1]} for k, v in _provider_ok.items()},
                        "symbols": list_symbols()})
            return

        if path.startswith("/api/market/ticker"):
            u = urlparse(self.path); q = parse_qs(u.query)
            sym = q.get("symbol", ["BTCUSDT"])[0].upper()
            from .market_data import quote
            qd, meta = quote(sym)
            self._json({"ok": "error" not in qd, "symbol": sym,
                        "quote": qd, "meta": meta.__dict__})
            return

        if path.startswith("/api/market/klines"):
            u = urlparse(self.path); q = parse_qs(u.query)
            sym = q.get("symbol", ["BTCUSDT"])[0].upper()
            interval = q.get("interval", ["1m"])[0]
            limit = int(q.get("limit", [100])[0])
            from .market_data import klines as _kl
            candles, meta = _kl(sym, interval, limit)
            self._json({"ok": True, "symbol": sym, "interval": interval,
                        "candles": candles, "count": len(candles),
                        "meta": meta.__dict__})
            return

        if path.startswith("/api/market/indicators"):
            u = urlparse(self.path); q = parse_qs(u.query)
            sym = q.get("symbol", ["BTCUSDT"])[0].upper()
            interval = q.get("interval", ["1m"])[0]
            limit = int(q.get("limit", [120])[0])
            from .market_data import klines as _kl, compute_indicators
            candles, meta = _kl(sym, interval, limit)
            ind = compute_indicators(candles) if len(candles) >= 20 else {}
            self._json({"ok": True, "symbol": sym, "interval": interval,
                        "candles": candles, "indicators": ind,
                        "meta": meta.__dict__})
            return

        if path == "/api/sse/market":
            """SSE — tick-stock-panel 实时 tick 推送。"""
            import json as _j
            from .market_data import pop_tick
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                for _ in range(180):  # 3min * 1s
                    import time as _t
                    _t.sleep(1)
                    tick = pop_tick()
                    if tick:
                        self.wfile.write(
                            f"data: {_j.dumps(tick, ensure_ascii=False)}\n\n".encode())
                        self.wfile.flush()
            except Exception:
                pass
            return

        if path.startswith("/api/csv/export"):
            """CSV 导出: ?source=upwork 或 ?status=shortlisted 或全量。"""
            from . import csv_export as _ce
            import io, csv as _csv_mod
            qs = parse_qs(u.query)
            source = (qs.get("source") or [""])[0]
            status = (qs.get("status") or [""])[0]
            try:
                if source and status:
                    rows = conn.execute(
                        "SELECT * FROM leads WHERE source=? AND status=? ORDER BY score DESC LIMIT 2000",
                        (source, status)).fetchall()
                elif source:
                    rows = conn.execute(
                        "SELECT * FROM leads WHERE source=? ORDER BY score DESC LIMIT 2000",
                        (source,)).fetchall()
                elif status:
                    rows = conn.execute(
                        "SELECT * FROM leads WHERE status=? ORDER BY score DESC LIMIT 2000",
                        (status,)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM leads ORDER BY score DESC LIMIT 2000").fetchall()
            except Exception as e:
                rows = []
            buf = io.StringIO()
            cols = ["id", "source", "title", "url", "score", "status",
                    "budget_min", "budget_max", "currency", "posted_at", "score_reasons"]
            w = _csv_mod.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: (v if v is not None else "") for k, v in dict(r).items() if k in cols})
            fname = f"leads_{source or 'all'}_{status or 'all'}.csv"
            self._send(200, buf.getvalue().encode("utf-8"),
                       "text/csv; charset=utf-8",
                       extra_headers={"Content-Disposition": f'attachment; filename="{fname}"'})
            return

        # ========== HARDWARE KERNEL (11 模块真实可用) ==========
        if path == "/api/hardware/simulator":
            """模拟硬件列表 + 内核自检."""
            from .hardware import kernel as _K, descriptor as _D, sandbox as _SB, learning as _L
            from .hardware.transports import mock as _MT
            sensors = [
                _D.make_mock_descriptor("sensor", caps=[
                    {"name":"temperature","type":"read","unit":"C","range":[-40,85]},
                    {"name":"humidity","type":"read","unit":"%","range":[0,100]}]),
                _D.make_mock_descriptor("switch", caps=[
                    {"name":"relay","type":"write","unit":"bool"}]),
                _D.make_mock_descriptor("rgb-led", caps=[
                    {"name":"intensity","type":"write","unit":"%","range":[0,100]},
                    {"name":"color","type":"write","unit":"hex"}]),
            ]
            dev_list = []
            for i, d in enumerate(sensors):
                dev_list.append({
                    "name": f"mock-{d['device_class']}-{i}",
                    "class": d["device_class"],
                    "caps": [c["name"] for c in d["capabilities"]],
                    "sn": d["serial_number"],
                    "pinout_count": len(d["pinout"]),
                })
            self._json({
                "ok": True, "devices": dev_list,
                "learning_adaptive_base": _L.adaptive_poll_interval(0, 1000),
                "learning_exponential_3": _L.adaptive_poll_interval(3, 1000),
                "modules": ["descriptor","registry","semantic","sandbox","learning",
                            "handshake","kernel","transports/mock","transports/serial",
                            "transports/tcp","discovery"],
                "total_modules": 11,
            })
            return

        if path == "/api/hardware/registry":
            """引脚分配表 + 已注册设备."""
            from .hardware import registry as _R
            reg = _R.Registry()
            entries_list = list(reg.entries.values()) if hasattr(reg, "entries") else []
            entries = []
            for e in entries_list[:20]:
                entries.append({
                    "sn": e.sn, "transport": e.last_transport,
                    "class": e.descriptor.get("device_class") if e.descriptor else "?",
                    "pins": len(e.pin_assignments) if e.pin_assignments else 0,
                })
            self._json({
                "ok": True, "path": str(reg.path),
                "entries_count": len(entries_list), "entries": entries,
            })
            return

        if path == "/api/hardware/sandbox-test":
            """安全边界测试: ?cmd=erase_flash&perm=normal"""
            from .hardware import descriptor as _D, sandbox as _SB
            qs = parse_qs(u.query)
            cmd = (qs.get("cmd") or ["erase_flash"])[0]
            perm = (qs.get("perm") or ["normal"])[0]
            d = _D.make_mock_descriptor("sensor")
            blocked = False; detail = ""
            try: _SB.validate_command(d, cmd, perm=perm)
            except _SB.SandboxError as e:
                blocked = True; detail = str(e)[:120]
            self._json({"ok": True, "command": cmd, "permission": perm,
                        "blocked": blocked, "reason": detail})
            return

        # ---- 售后工单 GET ----
        m = re.match(r"^/api/tickets/(\d+)$", path)
        if m:
            from . import crm
            t = crm.get_ticket(int(m.group(1)))
            if t: self._json(t)
            else: self._json({"error": "not found"}, 404)
            return

        if path == "/api/tickets/stats":
            from . import crm
            self._json(crm.ticket_stats())
            return

        if path == "/api/tickets":
            from . import crm
            q = parse_qs(u.query)
            st = (q.get("status") or [""])[0]
            ct = (q.get("category") or [""])[0]
            pr = (q.get("priority") or [""])[0]
            lm = int((q.get("limit") or ["50"])[0])
            self._json({"tickets": crm.list_tickets(
                status=st, category=ct, priority=pr, limit=lm),
                "stats": crm.ticket_stats()})
            return

        # ---- 商品 GET ----
        if path == "/api/products":
            conn = db.get_conn()
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM digital_products ORDER BY price_usd ASC").fetchall()]
            self._json({"products": rows, "total": len(rows)})
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

        # ---- 工单 POST ----
        if u.path == "/api/tickets":
            from . import crm
            try:
                r = crm.create_support_ticket(
                    title=body.get("title", ""),
                    body=body.get("body", ""),
                    category=body.get("category", "question"),
                    priority=body.get("priority", "normal"),
                    email=body.get("email", ""),
                    customer_id=int(body.get("customer_id") or 0) or 0,
                    invoice_id=int(body.get("invoice_id") or 0) or 0,
                    assigned_to=body.get("assigned_to", ""))
                self._json(r, 201)
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        m = re.match(r"^/api/tickets/(\d+)/reply$", u.path)
        if m:
            from . import crm
            r = crm.add_ticket_reply(int(m.group(1)),
                                     body.get("body", ""),
                                     body.get("role", "agent"))
            self._json(r)
            return

        m = re.match(r"^/api/tickets/(\d+)/resolve$", u.path)
        if m:
            from . import crm
            r = crm.resolve_ticket(int(m.group(1)),
                                    resolution=body.get("resolution", ""),
                                    status=body.get("status", "resolved"))
            self._json(r)
            return

        m = re.match(r"^/api/tickets/(\d+)/refund$", u.path)
        if m:
            from . import crm
            r = crm.process_refund_ticket(int(m.group(1)),
                                           amount=float(body.get("amount", 0)),
                                           paypal_refund_id=body.get("paypal_refund_id", ""),
                                           resolution=body.get("resolution", ""))
            self._json(r)
            return

        m = re.match(r"^/api/tickets/(\d+)/escalate$", u.path)
        if m:
            from . import crm
            r = crm.escalate_ticket(int(m.group(1)),
                                    to=body.get("to", ""),
                                    reason=body.get("reason", ""))
            self._json(r)
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

        # --- tracker 手动事件 ---
        m = re.match(r"^/api/tracker/lead/(\d+)/event$", u.path)
        if m:
            from . import tracker
            lead_id = int(m.group(1))
            ev = body.get("event", "")
            channel = body.get("channel", "manual")
            if ev not in ("sent", "replied", "closed", "lost"):
                self._json({"error": "event must be sent/replied/closed/lost"}, 400)
                return
            amount = float(body.get("amount", 0)) if ev == "closed" else 0
            detail = body.get("detail") or {}
            try:
                if ev == "closed":
                    rid = tracker.record_closed(lead_id, channel, amount,
                                                 body.get("currency", "USD"), detail)
                elif ev == "lost":
                    rid = tracker.record_lost(lead_id, channel,
                                               body.get("reason", ""))
                elif ev == "replied":
                    rid = tracker.record_replied(lead_id, channel, detail)
                else:  # sent
                    rid = tracker.record_sent(lead_id, channel,
                                               proposal_id=int(body.get("proposal_id", 0)),
                                               remote_id=body.get("remote_id", ""))
                self._json({"ok": True, "event_id": rid})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        # --- 手动操作中心（console） ---
        if u.path == "/api/console/mark-sent":
            from .console import mark_sent
            try:
                result = mark_sent(
                    proposal_id=int(body["proposal_id"]),
                    channel=body.get("channel", "manual"),
                    remote_id=body.get("remote_id", ""))
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if u.path == "/api/console/mark-closed":
            from .console import mark_closed
            try:
                result = mark_closed(
                    lead_id=int(body["lead_id"]),
                    amount=float(body["amount"]),
                    channel=body.get("channel", "manual"),
                    currency=body.get("currency", "USD"),
                    note=body.get("note", ""))
                self._json({"ok": True, **result})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if u.path == "/api/console/paylink":
            from .console import create_paylink
            try:
                result = create_paylink(
                    amount=float(body["amount"]),
                    description=body.get("description", ""),
                    email=body.get("email", ""),
                    reference=body.get("reference", ""))
                self._json(result)
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if u.path == "/api/console/send-email":
            """手动发一封邮件（指定 proposal_id）。"""
            from .outreach import EmailChannel
            from . import db as _db
            import json as _json
            try:
                lead_row = _db.get_lead(int(body["lead_id"]))
                prop_row = _db.proposals_for(int(body["proposal_id"]))
                prop_row = prop_row[0] if isinstance(prop_row, list) and prop_row else prop_row
                if not lead_row or not prop_row:
                    self._json({"error": "lead/proposal not found"}, 404); return
                ch = EmailChannel()
                result = ch.send(lead_row, {"content": prop_row.get("content", "")})
                if result.ok:
                    conn = _db.get_conn()
                    conn.execute("UPDATE proposals SET sent_at=? WHERE id=?", (result.sent_at, int(body["proposal_id"])))
                    conn.execute("UPDATE leads SET status='sent' WHERE id=?", (int(body["lead_id"]),))
                    conn.commit()
                self._json(result.to_dict())
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if u.path == "/api/paypal-webhook":
            """PayPal Webhook / IPN — 自动标记 paid_at + 入账 + 启动交付。"""
            from . import invoicer as _inv
            try:
                # PayPal webhook 发来的事件名
                event_type = body.get("event_type", "")
                resource = body.get("resource") or body  # PayPal API v1 vs v2
                invoice_id = resource.get("invoice_id", "") or resource.get("id", "")
                status = resource.get("status", "") or resource.get("state", "")
                
                # 找到对应 invoice
                conn = db.get_conn()
                row = conn.execute(
                    "SELECT id FROM invoices WHERE paypal_invoice_id=? OR reference LIKE ?",
                    (invoice_id, f"%{invoice_id}%")).fetchone()
                if not row:
                    self._json({"ok": True, "note": f"no invoice match for {invoice_id}"})
                    return
                
                inv_id = row["id"]
                
                # 已付 → 自动触发
                if "PAID" in status.upper() or "COMPLETE" in status.upper() \
                        or event_type in ("INVOICE.PAID", "PAYMENT.CAPTURE.COMPLETED"):
                    result = _inv.mark_paid(inv_id)
                    # 启动交付里程碑
                    try:
                        from . import deliver
                        d = deliver.init_for_invoice(inv_id)
                        result["deliverables"] = d
                    except Exception as e:
                        result["deliver_error"] = str(e)[:200]
                    # CRM 自动更新
                    try:
                        from . import crm
                        inv = conn.execute("SELECT lead_id FROM invoices WHERE id=?", (inv_id,)).fetchone()
                        if inv:
                            crm.touch_lead(inv["lead_id"], "paid",
                                          {"invoice_id": inv_id, "amount": result.get("amount", 0)})
                    except Exception:
                        pass
                    self._json({"ok": True, "paid": True, "invoice_id": inv_id, **result})
                else:
                    self._json({"ok": True, "note": f"event {event_type} status {status}"})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if u.path == "/api/paypal-webhook-test":
            """本地测试 webhook（不用真付钱）。"""
            from . import invoicer as _inv, deliver as _del, crm as _crm
            try:
                inv_id = int(body.get("invoice_id", 0))
                if not inv_id:
                    inv_id = _inv.create_invoice(
                        lead_id=int(body.get("lead_id", 0)),
                        amount=float(body.get("amount", 0)),
                        channel="paypal",
                        client_email=body.get("email", "test@test.com"),
                    )
                    _inv.send_paypal_invoice(inv_id)
                
                # 手动模拟 paid
                paid = _inv.mark_paid(inv_id)
                # 交付里程碑
                try:
                    d = _del.init_for_invoice(inv_id)
                except Exception:
                    d = {"note": "deliver init skipped"}
                # CRM
                conn = db.get_conn()
                inv = conn.execute("SELECT lead_id FROM invoices WHERE id=?", (inv_id,)).fetchone()
                if inv:
                    try: _crm.touch_lead(inv["lead_id"], "paid", {"invoice_id": inv_id})
                    except Exception: pass
                
                self._json({"ok": True, "invoice_id": inv_id, "paid": paid, "deliver": d})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        # ========== PAYWALL / 订阅墙 ==========
        if u.path == "/api/plans":
            from . import paywall as _pw, i18n as _i18n
            qs = parse_qs(u.query)
            lang = (qs.get("lang") or ["en"])[0]
            plans = _pw.list_plans()
            for p in plans:
                p = _i18n.translate_plan(p, lang)
                p["features"] = _i18n.translate_features_list(p.get("features") or [], lang)
                p["features_zh"] = _i18n.translate_features_list(
                    [f.get("key") if isinstance(f, dict) else f for f in p.get("features_zh", [])], "zh")
            self._json({"ok": True, "plans": plans, "lang": lang})
            return

        if u.path == "/api/subscription":
            from . import paywall as _pw, i18n as _i18n
            qs = parse_qs(u.query)
            lang = (qs.get("lang") or ["en"])[0]
            plan = _pw.get_plan()
            limits = _pw.get_limits()
            # 当前使用量
            from . import db as _db
            today_sent = _db.get_conn().execute("""
                SELECT COUNT(*) FROM outreach_events 
                WHERE event='sent' AND DATE(created_at)=DATE('now')
            """).fetchone()[0]
            today_leads = _db.get_conn().execute("""
                SELECT COUNT(*) FROM leads WHERE DATE(fetched_at)=DATE('now')
            """).fetchone()[0]
            features_list = limits.get("features", [])
            # 分类：已解锁 / 未解锁（双语）
            all_features = list(_pw.FEATURE_REQUIREMENTS.keys())
            unlocked = [{"key": f, "label": _i18n.feature_name(f, lang),
                         "label_en": _i18n.feature_name(f, "en"),
                         "label_zh": _i18n.feature_name(f, "zh")} for f in all_features if _pw.check(f)]
            locked = [{"key": f, "label": _i18n.feature_name(f, lang),
                       "label_en": _i18n.feature_name(f, "en"),
                       "label_zh": _i18n.feature_name(f, "zh")} for f in all_features if not _pw.check(f)]
            
            # 翻译 plan 名称
            plan_display = {
                "code": plan.get("plan_code") or plan.get("code"),
                "name": _i18n.t(f"pw.{plan.get('plan_code') or plan.get('code','free')}", lang=lang,
                               default=plan.get("name", "Free")),
                "name_zh": _i18n.t(f"pw.{plan.get('plan_code') or plan.get('code','free')}", lang="zh",
                                   default=plan.get("name", "Free")),
                "name_en": _i18n.t(f"pw.{plan.get('plan_code') or plan.get('code','free')}", lang="en",
                                   default=plan.get("name", "Free")),
                "price_monthly": plan.get("price_monthly", 0),
                "price_yearly": plan.get("price_yearly", 0),
            }
            
            self._json({
                "ok": True,
                "plan": plan_display,
                "limits": limits,
                "usage": {
                    "emails_sent_today": today_sent,
                    "leads_fetched_today": today_leads,
                    "csv_rows_limit": limits.get("max_csv_rows", 0),
                },
                "features": {
                    "unlocked": unlocked,
                    "locked": locked,
                    "all": all_features,
                },
                "lang": lang,
                "i18n": _i18n.t("nav.subscription", lang=lang),
            })
            return

        if u.path == "/api/subscription/upgrade":
            from . import paywall as _pw
            try:
                plan_code = body.get("plan", body.get("plan_code", ""))
                if plan_code not in ("pro", "enterprise"):
                    self._json({"error": "plan must be pro or enterprise"}, 400); return
                result = _pw.upgrade(
                    target_plan=plan_code,
                    channel=body.get("channel", "paypal"),
                    cycle=body.get("cycle", "monthly"),
                    client_email=body.get("email", ""),
                )
                self._json(result)
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if u.path == "/api/subscription/activate":
            """Webhook 触发：付款到账 → 激活订阅。"""
            from . import paywall as _pw
            try:
                inv_id = int(body.get("invoice_id", 0))
                self._json(_pw.activate_subscription(inv_id))
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return

        if u.path == "/api/subscription/check":
            """前端快速检查某个 feature 是否已解锁。"""
            from . import paywall as _pw
            qs = parse_qs(u.query)
            feature = (qs.get("feature") or [""])[0]
            self._json({"feature": feature, "unlocked": _pw.check(feature)})
            return

        if u.path == "/api/csv/export":
            """导出提案 CSV（Mailchimp 格式）。

            订阅墙硬拦：proposal_bilingual = pro，无权限的用户不能导出全量提案。
            """
            from .csv_export import export_proposals_csv
            from . import paywall as _pw
            qs = parse_qs(u.query)
            fmt = (qs.get("format") or ["mailchimp"])[0]
            lim = int((qs.get("limit") or ["200"])[0])
            # 订阅墙：CSV 行数上限跟随 max_csv_rows，且 free plan 不允许导出（需 pro 的 csv_export_all）
            limits = _pw.get_limits()
            csv_limit = limits.get("max_csv_rows", 50) or 50
            lim = min(lim, csv_limit)
            if lim <= 0:
                self._json({"error": f"订阅墙：CSV 导出限额 {csv_limit} 行，当前请求被拦截"}, 403)
                return
            path = export_proposals_csv(format=fmt, limit=lim)
            import os
            self._json({"ok": True, "path": path, "size": os.path.getsize(path), "format": fmt, "limit": lim})
            return

        self._json({"error": "unknown endpoint"}, 404)

    def log_message(self, fmt, *args) -> None:
        """真实日志: 非致命错误输出到 stderr。"""
        import sys as _sys
        msg = "[dashboard] " + (fmt % args if args else fmt)
        _sys.stderr.write(msg.rstrip() + "\n")


def serve(port: int = None) -> None:
    port = port or CFG["app"]["dashboard_port"]
    server = RobustServer(("0.0.0.0", port), Handler)
    # 后台预刷新支付状态——避免首个请求卡 15s
    import threading
    def _warmup():
        try: _payments_status()
        except Exception: pass
        try:
            from . import market_data as _md
            for _s in _md.DEFAULT_SYMBOLS[:5]:
                _md.quote_synthetic(_s)
                _md.klines_synthetic(_s, limit=60)
            print("[dashboard] market_data pre-warmed ✅")
        except Exception as _e:
            print(f"[dashboard] market_data warmup skipped: {_e}")
    threading.Thread(target=_warmup, daemon=True).start()
    print(f"[dashboard] http://localhost:{port}  (Ctrl+C 退出)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] 已停止")
