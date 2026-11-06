"""procurement —— AI 采购增强模块。

三件事：
  1. 6 家供应商数据库（跌倒雷达/外骨骼/护理耗材各 2 家）
  2. 智能比价：加权 score = price×0.4 + delivery×0.2 + quality×0.2 + supplier_rating×0.2
  3. 订单物流追踪模拟（状态机）+ 批量成本预测

依赖：SQLite（复用 autobiz.db.get_conn()）
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import db


# ======================================================================
# 供应商表 schema + 种子数据
# ======================================================================

SUPPLIERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  category TEXT NOT NULL,              -- radar / exoskeleton / supplies
  price_index REAL NOT NULL,            -- 相对价格指数 0.8~1.4 (越低越便宜)
  delivery_days INTEGER NOT NULL,        -- 标准交期
  quality_score REAL NOT NULL,          -- 质量分 0~1
  supplier_rating REAL NOT NULL,        -- 供应商评级 0~1 (含资质/历史/售后)
  reliability REAL DEFAULT 1.0,         -- 交付可靠性 0~1
  on_time_rate REAL DEFAULT 0.95,       -- 准时交付率
  notes TEXT DEFAULT '{}',              -- 扩展 JSON
  registered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suppliers_cat ON suppliers(category);


CREATE TABLE IF NOT EXISTS purchase_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  supplier_id INTEGER NOT NULL,
  item_type TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  unit_price REAL NOT NULL,
  total_amount REAL NOT NULL,
  status TEXT DEFAULT 'pending',         -- pending/processing/shipped/delivered/cancelled
  tracking_json TEXT DEFAULT '{}',       -- 物流状态机历史
  created_at TEXT NOT NULL,
  estimated_delivery TEXT,
  actual_delivery TEXT,
  FOREIGN KEY(supplier_id) REFERENCES suppliers(id)
);
CREATE INDEX IF NOT EXISTS idx_po_status ON purchase_orders(status);
"""

# 种子数据：6 家供应商 × 3 品类
SEED_SUPPLIERS = [
    # ---- 跌倒雷达 radar ----
    {"name": "RadarCare Pro",     "category": "radar",       "price_index": 1.15,
     "delivery_days": 14, "quality_score": 0.92, "supplier_rating": 0.91,
     "reliability": 0.98, "on_time_rate": 0.96},
    {"name": "FallGuard Tech",    "category": "radar",       "price_index": 0.92,
     "delivery_days": 21, "quality_score": 0.82, "supplier_rating": 0.84,
     "reliability": 0.88, "on_time_rate": 0.88},

    # ---- 外骨骼 exoskeleton ----
    {"name": "ExoMotion Labs",    "category": "exoskeleton", "price_index": 1.25,
     "delivery_days": 45, "quality_score": 0.95, "supplier_rating": 0.96,
     "reliability": 0.99, "on_time_rate": 0.94},
    {"name": "RehabAssist Co.",   "category": "exoskeleton", "price_index": 1.00,
     "delivery_days": 30, "quality_score": 0.80, "supplier_rating": 0.82,
     "reliability": 0.90, "on_time_rate": 0.85},

    # ---- 护理耗材 supplies ----
    {"name": "MedSupplies 365",   "category": "supplies",   "price_index": 0.85,
     "delivery_days": 7,  "quality_score": 0.78, "supplier_rating": 0.80,
     "reliability": 0.95, "on_time_rate": 0.97},
    {"name": "CareKit Direct",    "category": "supplies",   "price_index": 0.98,
     "delivery_days": 10, "quality_score": 0.90, "supplier_rating": 0.88,
     "reliability": 0.92, "on_time_rate": 0.93},
]


PRICING_WEIGHTS = {
    "price": 0.40,
    "delivery": 0.20,
    "quality": 0.20,
    "supplier": 0.20,
}


def _init_schema() -> None:
    conn = db.get_conn()
    conn.executescript(SUPPLIERS_SCHEMA)
    # 种子数据（幂等）
    existing = conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
    if existing == 0:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for s in SEED_SUPPLIERS:
            conn.execute("""
                INSERT INTO suppliers
                (name, category, price_index, delivery_days, quality_score,
                 supplier_rating, reliability, on_time_rate, registered_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (s["name"], s["category"], s["price_index"], s["delivery_days"],
                  s["quality_score"], s["supplier_rating"],
                  s["reliability"], s["on_time_rate"], now))
        conn.commit()
        print(f"[procurement] ✅ seeded {len(SEED_SUPPLIERS)} suppliers")


_init_schema()


# ======================================================================
# 智能比价
# ======================================================================

def _normalize(values: list[float], higher_is_better: bool = True) -> list[float]:
    """把一组值归一化到 [0,1]。

    Args:
        higher_is_better: True → 越大越好（quality, rating）；False → 越小越好（price, delivery）
    """
    if not values:
        return []
    vmin, vmax = min(values), max(values)
    if vmax == vmin:
        return [0.5] * len(values)
    if higher_is_better:
        return [(v - vmin) / (vmax - vmin) for v in values]
    else:
        return [(vmax - v) / (vmax - vmin) for v in values]


def compare_suppliers(category: str,
                      weights: Optional[dict] = None,
                      min_quality: float = 0.0,
                      max_delivery_days: int = 999) -> list[dict]:
    """对一个品类下的供应商做加权比价排名。

    score = price_norm × 0.4 + delivery_norm × 0.2 + quality_norm × 0.2 + supplier_norm × 0.2

    Args:
        category: "radar" / "exoskeleton" / "supplies"
        weights:  可覆盖默认权重 {price, delivery, quality, supplier}
        min_quality: 质量下限过滤（低于此值直接排除）
        max_delivery_days: 交期上限过滤

    Returns:
        排名后的供应商列表（含 price_norm 等中间值 + total_score）
    """
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM suppliers WHERE category = ? "
        "AND quality_score >= ? AND delivery_days <= ?",
        (category, min_quality, max_delivery_days)
    ).fetchall()

    if not rows:
        return []

    data = [dict(r) for r in rows]
    w = weights or PRICING_WEIGHTS

    # 归一化
    prices = [d["price_index"] for d in data]
    deliveries = [d["delivery_days"] for d in data]
    qualities = [d["quality_score"] for d in data]
    ratings = [d["supplier_rating"] for d in data]

    price_norms = _normalize(prices, higher_is_better=False)
    deliv_norms = _normalize(deliveries, higher_is_better=False)
    qual_norms = _normalize(qualities, higher_is_better=True)
    rating_norms = _normalize(ratings, higher_is_better=True)

    for i, d in enumerate(data):
        d["price_norm"] = round(price_norms[i], 3)
        d["delivery_norm"] = round(deliv_norms[i], 3)
        d["quality_norm"] = round(qual_norms[i], 3)
        d["supplier_norm"] = round(rating_norms[i], 3)
        d["total_score"] = round(
            price_norms[i]    * w["price"]
          + deliv_norms[i]    * w["delivery"]
          + qual_norms[i]     * w["quality"]
          + rating_norms[i]   * w["supplier"], 4)

    # 加权维度贡献（调试/可视化用）
    for d in data:
        d["score_breakdown"] = {
            f"price×{w['price']}":     round(d["price_norm"]   * w["price"],    4),
            f"delivery×{w['delivery']}": round(d["delivery_norm"] * w["delivery"], 4),
            f"quality×{w['quality']}":  round(d["quality_norm"]  * w["quality"],   4),
            f"supplier×{w['supplier']}": round(d["supplier_norm"] * w["supplier"], 4),
        }

    data.sort(key=lambda x: x["total_score"], reverse=True)
    for rank, d in enumerate(data, 1):
        d["rank"] = rank
    return data


def best_supplier(category: str, **kwargs) -> Optional[dict]:
    rank = compare_suppliers(category, **kwargs)
    return rank[0] if rank else None


# ======================================================================
# 订单物流追踪（模拟状态机）
# ======================================================================

ORDER_STATES = ["pending", "processing", "shipped", "in_transit",
                "customs", "delivered", "cancelled"]

TRANSITIONS = {
    "pending":    ["processing", "cancelled"],
    "processing": ["shipped", "cancelled"],
    "shipped":    ["in_transit"],
    "in_transit": ["customs", "delivered"],
    "customs":    ["delivered", "cancelled"],
}


def create_order(category: str, quantity: int = 1,
                 supplier_id: Optional[int] = None,
                 unit_price_override: Optional[float] = None) -> dict:
    """创建一张采购订单。自动选最优供应商（没指定的话）。"""
    conn = db.get_conn()

    if supplier_id is None:
        best = best_supplier(category)
        if best is None:
            return {"ok": False, "error": f"no suppliers for {category}"}
        supplier_id = best["id"]
        supplier_name = best["name"]
        base_price = 1000.0 * best["price_index"]  # 模拟基准价
    else:
        row = conn.execute("SELECT * FROM suppliers WHERE id=?",
                           (supplier_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": f"supplier {supplier_id} not found"}
        supplier_name = row["name"]
        base_price = 1000.0 * row["price_index"]

    unit = unit_price_override or base_price
    total = unit * quantity
    now = datetime.now(timezone.utc)
    estimated = now + timedelta(days=14)

    cur = conn.execute("""
        INSERT INTO purchase_orders
        (supplier_id, item_type, quantity, unit_price, total_amount,
         status, created_at, estimated_delivery)
        VALUES (?,?,?,?,?,?,?,?)
    """, (supplier_id, category, quantity, unit, total,
          "pending",
          now.strftime("%Y-%m-%dT%H:%M:%SZ"),
          estimated.strftime("%Y-%m-%dT%H:%M:%SZ")))
    conn.commit()

    return {"ok": True, "order_id": cur.lastrowid,
            "supplier": supplier_name,
            "category": category, "quantity": quantity,
            "unit_price": round(unit, 2), "total": round(total, 2),
            "status": "pending",
            "estimated_delivery": estimated.strftime("%Y-%m-%d")}


def advance_order(order_id: int, new_status: Optional[str] = None) -> dict:
    """推进订单状态机（模拟）。不传 new_status 自动走下一步。"""
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM purchase_orders WHERE id=?",
                       (order_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "order not found"}
    data = dict(row)
    current = data["status"]

    # 决定下一状态
    if new_status is None:
        possible = TRANSITIONS.get(current, [])
        if not possible:
            return {"ok": False, "error": f"no transition from {current}"}
        new_status = possible[0]

    # 记录追踪历史
    tracking = json.loads(data.get("tracking_json") or "{}")
    history = tracking.setdefault("history", [])
    history.append({
        "from": current, "to": new_status,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    updates = {"status": new_status,
               "tracking_json": json.dumps(tracking, ensure_ascii=False)}
    if new_status == "delivered":
        updates["actual_delivery"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    fields = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE purchase_orders SET {fields} WHERE id=?",
                 list(updates.values()) + [order_id])
    conn.commit()

    return {"ok": True, "order_id": order_id,
            "previous": current, "status": new_status}


def simulate_full_logistics(order_id: int, seed: int | None = None) -> list[dict]:
    """模拟一张订单从 pending 到 delivered 的完整流程（快速跑完）。"""
    if seed is not None:
        random.seed(seed)

    events = []
    current = "pending"
    while current not in ("delivered", "cancelled"):
        possible = TRANSITIONS.get(current, [])
        if not possible:
            break
        # 随机选一个合法下一状态
        # pending 大概率 processing（90%），小概率 cancelled
        if current == "pending":
            next_s = "processing" if random.random() < 0.9 else "cancelled"
        elif current == "processing":
            next_s = "shipped" if random.random() < 0.95 else "cancelled"
        elif current == "in_transit":
            next_s = random.choice(["customs", "delivered"])
        elif current == "customs":
            next_s = "delivered" if random.random() < 0.9 else "cancelled"
        else:
            next_s = possible[0]

        r = advance_order(order_id, next_s)
        events.append(r)
        if not r.get("ok"):
            break
        current = r["status"]

    return events


# ======================================================================
# 批量成本预测
# ======================================================================

def bulk_cost_predict(category: str, qty_list: list[int],
                      bulk_discount_curve: Optional[dict] = None,
                      supplier_id: Optional[int] = None) -> list[dict]:
    """批量成本预测：输入数量列表 → 每件折扣 + 总成本。

    Args:
        category: 品类
        qty_list: 要预测的数量列表，如 [1, 5, 10, 50, 100]
        bulk_discount_curve: 覆盖默认折扣曲线
            默认: {5: 0.05, 10: 0.10, 20: 0.15, 50: 0.22, 100: 0.30}
            含义: 5件起打 95 折，10件打 9 折 ...
        supplier_id: 固定供应商（None 自动选最优）

    Returns:
        每个 qty 的预测结果 [{"qty": int, "unit_price": float, "total": float,
                               "discount_pct": float, "supplier": str}]
    """
    if bulk_discount_curve is None:
        bulk_discount_curve = {5: 0.05, 10: 0.10, 20: 0.15, 50: 0.22, 100: 0.30}

    # 选供应商
    conn = db.get_conn()
    if supplier_id:
        row = conn.execute("SELECT * FROM suppliers WHERE id=?",
                           (supplier_id,)).fetchone()
        if row is None:
            return []
        supplier = dict(row)
    else:
        supplier = best_supplier(category)
        if supplier is None:
            return []

    base_unit = 1000.0 * supplier["price_index"]

    # 折扣曲线（数量越多折扣越大）
    def discount_for(qty: int) -> float:
        applicable = [d for thresh, d in bulk_discount_curve.items()
                      if qty >= thresh]
        return max(applicable) if applicable else 0.0

    results = []
    for qty in sorted(qty_list):
        disc = discount_for(qty)
        unit = base_unit * (1.0 - disc)
        total = unit * qty
        results.append({
            "qty": qty,
            "unit_price": round(unit, 2),
            "total": round(total, 2),
            "discount_pct": round(disc * 100, 1),
            "supplier": supplier["name"],
            "supplier_id": supplier["id"],
            "base_unit": round(base_unit, 2),
        })

    return results


def summary() -> dict:
    """采购系统摘要 —— dashboard 用。"""
    conn = db.get_conn()
    cats = [dict(r) for r in conn.execute("""
        SELECT category, COUNT(*) n,
               AVG(price_index) avg_price,
               AVG(delivery_days) avg_days,
               AVG(quality_score) avg_quality
        FROM suppliers GROUP BY category
    """).fetchall()]

    po_summary = dict(conn.execute("""
        SELECT
          COUNT(*) total_orders,
          SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END) delivered,
          SUM(CASE WHEN status='shipped' OR status='in_transit'
                   OR status='customs' THEN 1 ELSE 0 END) in_transit,
          SUM(CASE WHEN status='pending' OR status='processing' THEN 1 ELSE 0 END) pending,
          COALESCE(SUM(total_amount), 0.0) total_spent
        FROM purchase_orders
    """).fetchone())

    return {"suppliers_by_category": cats, "orders": po_summary}


# ======================================================================
# 快速验证
# ======================================================================

def _self_test() -> dict:
    report = {}

    # 1. 比价
    for cat in ("radar", "exoskeleton", "supplies"):
        report[f"rank_{cat}"] = [
            {"rank": s["rank"], "name": s["name"],
             "score": s["total_score"], "breakdown": s["score_breakdown"]}
            for s in compare_suppliers(cat)]

    # 2. 下单 + 模拟物流
    order = create_order("radar", quantity=3)
    report["order_created"] = order
    if order.get("ok"):
        events = simulate_full_logistics(order["order_id"], seed=42)
        report["logistics_events"] = [
            {"to": e["status"], "previous": e["previous"]}
            for e in events if e.get("ok")
        ]

    # 3. 批量成本预测
    report["bulk_radar"] = bulk_cost_predict("radar", [1, 2, 5, 10, 20, 50])

    # 4. 摘要
    report["summary"] = summary()

    return report


if __name__ == "__main__":
    print("=" * 60)
    print("  PROCUREMENT AI — SELF TEST")
    print("=" * 60)
    r = _self_test()
    print(json.dumps(r, indent=2, default=str, ensure_ascii=False))
