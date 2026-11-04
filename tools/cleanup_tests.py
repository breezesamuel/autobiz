"""清理联调测试产物：删除本轮创建的探针发票与测试账目行。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402
from autobiz.payment.paypal import PayPal  # noqa: E402

pp = PayPal()
inv = pp.list_invoices(10)
for i in inv:
    num = (i.get("detail") or {}).get("invoice_number", "")
    if num in ("AB-RAW-TEST", "autobiz-api-test"):
        r = requests.delete(f"{pp.base}/v2/invoicing/invoices/{i['id']}",
                            headers=pp._hdr(), timeout=30)
        print("delete probe invoice", i["id"], "->", r.status_code)

conn = sqlite3.connect(str(Path(__file__).resolve().parents[1] / "data" / "autobiz.db"))
cur = conn.execute("DELETE FROM revenue WHERE channel='api_test'")
print("revenue test rows deleted:", cur.rowcount)
conn.commit()
print("remaining revenue rows:",
      conn.execute("SELECT COUNT(*) FROM revenue").fetchone()[0])
conn.close()
