import os
import uuid
import json
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# Supabase (or any Postgres) connection string — set as DATABASE_URL on Render.
# Use the Session pooler URI, not the direct connection (Render's free tier can't reach IPv6).
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ---------- Config ----------
# Set these as environment variables on Render — never hardcode real keys here.
AT_USERNAME = os.environ.get("AT_USERNAME", "sandbox")   # 'sandbox' for testing, your app username once live
AT_API_KEY = os.environ.get("AT_API_KEY", "")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")  # set to real Render URL once deployed
FEE_PER_ORDER = 50  # KSh you charge the seller per confirmed order

# One entry per seller. Add more as other shops go live.
SELLERS = {
    "prime-wear": {
        "name": "Prime Wear Collections 254",
        "phone": os.environ.get("PRIME_WEAR_PHONE", "+2547XXXXXXXX"),  # placeholder until he confirms his number
    },
}

sms = None
def get_sms_client():
    """Lazily create the Africa's Talking SMS client so the app can still start without a key set."""
    global sms
    if sms is None and AT_API_KEY:
        import africastalking
        africastalking.initialize(AT_USERNAME, AT_API_KEY)
        sms = africastalking.SMS
    return sms


# ---------- Database ----------
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

def init_db():
    if not DATABASE_URL:
        print("[DB skipped - no DATABASE_URL set]")
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            seller TEXT NOT NULL,
            buyer_name TEXT,
            phone TEXT,
            area TEXT,
            items_json TEXT,
            amount INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            confirmed_at TEXT,
            reported BOOLEAN DEFAULT FALSE
        )
    """)
    # Safe to run every startup — adds the column only if an older table doesn't have it yet.
    cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS reported BOOLEAN DEFAULT FALSE")
    conn.commit()
    cur.close()
    conn.close()


# ---------- SMS ----------
def send_confirm_sms(order_id, seller_id, buyer_name, amount):
    seller = SELLERS.get(seller_id)
    if not seller:
        return False, "unknown seller"
    link = f"{BASE_URL}/confirm/{order_id}"
    message = f"New order: KSh {amount} from {buyer_name}. Tap to confirm you received payment: {link}"
    client = get_sms_client()
    if not client:
        print(f"[SMS skipped - no API key set] Would send to {seller['phone']}: {message}")
        return False, "no API key configured"
    try:
        response = client.send(message, [seller["phone"]])
        print(f"[SMS attempt - confirm] to {seller['phone']}: {response}")
        return True, response
    except Exception as e:
        print(f"[SMS failed - confirm] to {seller['phone']}: {e}")
        return False, str(e)

def send_summary_sms(seller_id, order_count, total_amount):
    seller = SELLERS.get(seller_id)
    if not seller:
        return False, "unknown seller"
    fee_owed = order_count * FEE_PER_ORDER
    message = (
        f"Today's sales: {order_count} confirmed order(s), KSh {total_amount} total. "
        f"Collective 254 fee owed: KSh {fee_owed}."
    )
    client = get_sms_client()
    if not client:
        print(f"[SMS skipped - no API key set] Would send to {seller['phone']}: {message}")
        return False, "no API key configured"
    try:
        response = client.send(message, [seller["phone"]])
        print(f"[SMS attempt - summary] to {seller['phone']}: {response}")
        return True, response
    except Exception as e:
        print(f"[SMS failed - summary] to {seller['phone']}: {e}")
        return False, str(e)


# ---------- Routes ----------
@app.after_request
def add_cors_headers(resp):
    # The shop lives on GitHub Pages, a different origin, so the browser needs these to allow the request.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

@app.route("/")
def index():
    return jsonify({"status": "ok", "service": "Collective 254 order backend"})

@app.route("/api/order", methods=["POST", "OPTIONS"])
def create_order():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(force=True, silent=True) or {}
    seller_id = data.get("seller")
    buyer_name = data.get("buyer_name", "").strip()
    phone = data.get("phone", "").strip()
    area = data.get("area", "")
    items = data.get("items", [])
    amount = data.get("amount", 0)

    if seller_id not in SELLERS:
        return jsonify({"error": "unknown seller"}), 400
    if not buyer_name or not phone or not amount:
        return jsonify({"error": "missing buyer_name, phone, or amount"}), 400

    order_id = uuid.uuid4().hex[:10]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO orders (id, seller, buyer_name, phone, area, items_json, amount, status, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s)",
        (order_id, seller_id, buyer_name, phone, area, json.dumps(items), amount,
         datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    cur.close()
    conn.close()

    sent, info = send_confirm_sms(order_id, seller_id, buyer_name, amount)
    return jsonify({"order_id": order_id, "status": "pending", "sms_sent": sent})


CONFIRM_PAGE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Confirm Payment</title>
<style>
  body{font-family:sans-serif; background:#F6F0E4; color:#211B16; padding:30px 20px; text-align:center;}
  .box{max-width:380px; margin:40px auto; background:#fff; border-radius:12px; padding:28px 22px; box-shadow:0 8px 20px rgba(0,0,0,0.1); text-align:left;}
  h2{margin-bottom:14px; text-align:center;}
  p{margin-bottom:12px; color:#5c4f43;}
  .amount{font-size:1.3rem; font-weight:800; text-align:center; margin-bottom:18px;}
  .detail-row{display:flex; justify-content:space-between; font-size:0.9rem; padding:6px 0; border-bottom:1px solid #eee;}
  .detail-row span:first-child{color:#8a7d6f;}
  .items-box{background:#F6F0E4; border-radius:8px; padding:10px 12px; margin:14px 0;}
  .item-line{font-size:0.88rem; padding:4px 0;}
  button{background:#3f7a4d; color:#fff; border:none; padding:14px 26px; border-radius:8px; font-weight:700; font-size:1rem; width:100%; margin-top:16px;}
  .done{color:#3f7a4d; font-weight:700; font-size:1.1rem; text-align:center;}
  .already{color:#8a7d6f; text-align:center;}
</style></head><body>
<div class="box">
{% if already %}
  <p class="already">This order was already confirmed on {{ confirmed_at }}.</p>
{% elif not_found %}
  <p class="already">Order not found.</p>
{% else %}
  <h2>Confirm Payment</h2>
  <div class="amount">KSh {{ amount }}</div>
  <div class="detail-row"><span>Buyer</span><span>{{ buyer_name }}</span></div>
  <div class="detail-row"><span>Phone</span><span>{{ phone }}</span></div>
  <div class="detail-row"><span>Area</span><span>{{ area }}</span></div>
  <div class="items-box">
    {% for item in items %}
    <div class="item-line">{{ item.qty }}x {{ item.name }}{% if item.size %} — size {{ item.size }}{% endif %}{% if item.color %} — {{ item.color }}{% endif %}</div>
    {% endfor %}
  </div>
  <form method="POST">
    <button type="submit">Yes, I received this payment</button>
  </form>
{% endif %}
</div>
</body></html>
"""

CONFIRMED_PAGE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Confirmed</title>
<style>body{font-family:sans-serif; background:#F6F0E4; color:#211B16; padding:60px 20px; text-align:center;}
.done{color:#3f7a4d; font-weight:700; font-size:1.2rem;}</style></head><body>
<p class="done">Confirmed. Thank you.</p>
</body></html>
"""

@app.route("/confirm/<order_id>", methods=["GET", "POST"])
def confirm_order(order_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
    row = cur.fetchone()

    if not row:
        cur.close(); conn.close()
        return render_template_string(CONFIRM_PAGE, not_found=True, already=False)

    if request.method == "POST":
        if row["status"] != "confirmed":
            cur.execute(
                "UPDATE orders SET status='confirmed', confirmed_at=%s WHERE id=%s",
                (datetime.now(timezone.utc).isoformat(), order_id)
            )
            conn.commit()
        cur.close(); conn.close()
        return render_template_string(CONFIRMED_PAGE)

    cur.close(); conn.close()
    if row["status"] == "confirmed":
        return render_template_string(CONFIRM_PAGE, already=True, not_found=False, confirmed_at=row["confirmed_at"])
    return render_template_string(CONFIRM_PAGE, already=False, not_found=False,
                                   amount=row["amount"], buyer_name=row["buyer_name"],
                                   phone=row["phone"], area=row["area"],
                                   items=json.loads(row["items_json"] or "[]"))


@app.route("/api/order-status/<order_id>")
def order_status(order_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, confirmed_at FROM orders WHERE id = %s", (order_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": row["status"], "confirmed_at": row["confirmed_at"]})


ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "changeme")

@app.route("/admin/<secret>")
def admin_dashboard(secret):
    if secret != ADMIN_SECRET:
        return "Not found", 404
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE status='confirmed' ORDER BY confirmed_at DESC")
    rows = cur.fetchall()
    cur.close(); conn.close()

    by_seller = {}
    for r in rows:
        by_seller.setdefault(r["seller"], {"count": 0, "total": 0})
        by_seller[r["seller"]]["count"] += 1
        by_seller[r["seller"]]["total"] += r["amount"]

    lines = ["<h2>Confirmed orders</h2>"]
    for seller_id, stats in by_seller.items():
        seller_name = SELLERS.get(seller_id, {}).get("name", seller_id)
        fee = stats["count"] * FEE_PER_ORDER
        lines.append(f"<p><b>{seller_name}</b>: {stats['count']} orders, KSh {stats['total']} sales, KSh {fee} fee owed</p>")

    lines.append("<hr><table border='1' cellpadding='6'><tr><th>Order</th><th>Seller</th><th>Buyer</th><th>Amount</th><th>Confirmed</th></tr>")
    for r in rows:
        lines.append(f"<tr><td>{r['id']}</td><td>{r['seller']}</td><td>{r['buyer_name']}</td><td>{r['amount']}</td><td>{r['confirmed_at']}</td></tr>")
    lines.append("</table>")

    return "<html><body style='font-family:sans-serif;padding:20px;'>" + "".join(lines) + "</body></html>"


@app.route("/internal/daily-summary/<secret>", methods=["POST", "GET"])
def daily_summary(secret):
    if secret != ADMIN_SECRET:
        return "Not found", 404

    conn = get_db()
    cur = conn.cursor()
    results = {}
    for seller_id in SELLERS:
        cur.execute(
            "SELECT id, amount FROM orders WHERE seller=%s AND status='confirmed' AND reported=FALSE",
            (seller_id,)
        )
        rows = cur.fetchall()
        if not rows:
            results[seller_id] = {"orders": 0, "sent": False}
            continue
        count = len(rows)
        total = sum(r["amount"] for r in rows)
        sent, info = send_summary_sms(seller_id, count, total)
        if sent:
            ids = tuple(r["id"] for r in rows)
            cur.execute("UPDATE orders SET reported=TRUE WHERE id IN %s", (ids,))
            conn.commit()
        results[seller_id] = {"orders": count, "total": total, "fee": count * FEE_PER_ORDER, "sent": sent}
    cur.close(); conn.close()
    return jsonify(results)


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
else:
    init_db()
