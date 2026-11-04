"""SQLite 存储层：线索、提案、运行记录、收入事件。"""
import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DB_PATH

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT UNIQUE NOT NULL,
  source TEXT NOT NULL,
  title TEXT,
  url TEXT,
  description TEXT,
  budget_min REAL,
  budget_max REAL,
  currency TEXT DEFAULT 'USD',
  posted_at TEXT,
  fetched_at TEXT NOT NULL,
  score INTEGER DEFAULT 0,
  score_reasons TEXT DEFAULT '[]',
  status TEXT DEFAULT 'new',
  raw_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_source ON leads(source);

CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL,
  content TEXT NOT NULL,
  method TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  sent_at TEXT,
  FOREIGN KEY(lead_id) REFERENCES leads(id)
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  phase TEXT NOT NULL,
  found INTEGER DEFAULT 0,
  new INTEGER DEFAULT 0,
  errors TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS revenue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  amount REAL NOT NULL,
  currency TEXT DEFAULT 'USD',
  channel TEXT,
  note TEXT,
  status TEXT DEFAULT 'pending',
  created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _local.conn = conn
    return conn


def upsert_lead(d: dict) -> bool:
    """插入线索，已存在则不覆盖。返回是否为新线索。"""
    conn = get_conn()
    cur = conn.execute("SELECT id FROM leads WHERE uid = ?", (d["uid"],))
    if cur.fetchone():
        return False
    conn.execute(
        """INSERT INTO leads
        (uid, source, title, url, description, budget_min, budget_max,
         currency, posted_at, fetched_at, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            d["uid"], d["source"], d.get("title", ""), d.get("url", ""),
            (d.get("description") or "")[:20000],
            d.get("budget_min"), d.get("budget_max"), d.get("currency") or "USD",
            d.get("posted_at"), now_iso(),
            json.dumps(d.get("raw") or {}, ensure_ascii=False)[:50000],
        ),
    )
    conn.commit()
    return True


def save_scores(lead_id: int, score: int, reasons: list) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE leads SET score=?, score_reasons=? WHERE id=?",
        (score, json.dumps(reasons, ensure_ascii=False), lead_id),
    )
    conn.commit()


def set_status(lead_id: int, status: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE leads SET status=? WHERE id=?", (status, lead_id))
    conn.commit()


def all_leads() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM leads ORDER BY score DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def leads_by_status(status: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM leads WHERE status=? ORDER BY score DESC", (status,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_lead(lead_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    return dict(row) if row else None


def add_proposal(lead_id: int, content: str, method: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO proposals (lead_id, content, method, generated_at) VALUES (?,?,?,?)",
        (lead_id, content, method, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def proposals_for(lead_id: int) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM proposals WHERE lead_id=? ORDER BY id DESC", (lead_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_run(phase: str, found: int, new: int, errors: list) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO runs (started_at, finished_at, phase, found, new, errors) VALUES (?,?,?,?,?,?)",
        (now_iso(), now_iso(), phase, found, new,
         json.dumps(errors, ensure_ascii=False)),
    )
    conn.commit()
    return cur.lastrowid


def recent_runs(limit: int = 20) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_revenue(amount: float, channel: str, note: str = "",
                currency: str = "USD", status: str = "pending") -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO revenue (amount, currency, channel, note, status, created_at)
        VALUES (?,?,?,?,?,?)""",
        (amount, currency, channel, note, status, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def summary() -> dict:
    conn = get_conn()
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    by_source = {
        r["source"]: r["n"]
        for r in conn.execute(
            "SELECT source, COUNT(*) n FROM leads GROUP BY source ORDER BY n DESC"
        )
    }
    by_status = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, COUNT(*) n FROM leads GROUP BY status"
        )
    }
    return {
        "total_leads": q("SELECT COUNT(*) FROM leads"),
        "shortlisted": q("SELECT COUNT(*) FROM leads WHERE status='shortlisted'"),
        "proposed": q("SELECT COUNT(*) FROM leads WHERE status='proposed'"),
        "proposals_generated": q("SELECT COUNT(*) FROM proposals"),
        "runs": q("SELECT COUNT(*) FROM runs"),
        "revenue_confirmed": q(
            "SELECT COALESCE(SUM(amount),0) FROM revenue WHERE status='confirmed'"
        ),
        "revenue_pipeline": q("SELECT COALESCE(SUM(amount),0) FROM revenue"),
        "avg_score": round(q("SELECT COALESCE(AVG(score),0) FROM leads"), 1),
        "budget_sum_usd": round(q(
            "SELECT COALESCE(SUM(v2 * rate), 0) FROM ("
            "SELECT CASE WHEN COALESCE(budget_max, budget_min, 0) > 20000 "
            "THEN COALESCE(budget_max, budget_min, 0) / 12.0 "
            "ELSE COALESCE(budget_max, budget_min, 0) END AS v2, "
            "CASE currency WHEN 'EUR' THEN 1.08 WHEN 'GBP' THEN 1.27 "
            "WHEN 'CNY' THEN 0.14 WHEN 'AUD' THEN 0.66 ELSE 1.0 END AS rate "
            "FROM leads)"), 2),
        "by_source": by_source,
        "by_status": by_status,
    }
