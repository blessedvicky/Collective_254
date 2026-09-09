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
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "changeme")  # protects admin dashboard, daily-summary trigger, and product edits

# One entry per seller. Add more as other shops go live.
SELLERS = {
    "prime-wear": {
        "name": "Prime Wear Collections 254",
        "phone": os.environ.get("PRIME_WEAR_PHONE", "+2547XXXXXXXX"),  # placeholder until he confirms his number
        "report_token": os.environ.get("PRIME_WEAR_REPORT_TOKEN", "changeme-prime-report"),  # secret link token for his own sales report
    },
}

# Fixed category options per seller — the admin panel can only pick from this list,
# so a product can never land somewhere that doesn't exist on the shop.
CATEGORY_OPTIONS = {
    "prime-wear": [
        {"id": "sneakers", "label": "Sneakers"},
        {"id": "official", "label": "Official Wear"},
        {"id": "boots", "label": "Boots"},
        {"id": "sandals", "label": "Sandals & Slides"},
        {"id": "kids", "label": "Kids"},
    ],
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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            seller TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price INTEGER NOT NULL,
            sizes TEXT,
            colors TEXT,
            description TEXT,
            photo_url TEXT,
            in_stock BOOLEAN DEFAULT TRUE,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# ---------- SMS ----------
def send_confirm_sms(order_id, seller_id, buyer_name, amount):
    link = f"{BASE_URL}/confirm/{order_id}"
    message = f"New order: KSh {amount} from {buyer_name}. Tap to confirm you received payment: {link}"
    return send_sms(seller_id, message)

def send_sms(seller_id, message):
    """Generic sender — used for the confirm-link SMS and the daily report-link SMS."""
    seller = SELLERS.get(seller_id)
    if not seller:
        return False, "unknown seller"
    client = get_sms_client()
    if not client:
        print(f"[SMS skipped - no API key set] Would send to {seller['phone']}: {message}")
        return False, "no API key configured"
    try:
        response = client.send(message, [seller["phone"]])
        print(f"[SMS attempt] to {seller['phone']}: {response}")
        return True, response
    except Exception as e:
        print(f"[SMS failed] to {seller['phone']}: {e}")
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


# ---------- Products (Stage 1 — no admin UI yet, just the API it will talk to) ----------
def check_admin(req):
    """Accepts the secret either as ?secret= or an X-Admin-Secret header."""
    supplied = req.args.get("secret") or req.headers.get("X-Admin-Secret")
    return supplied == ADMIN_SECRET

@app.route("/api/categories/<seller_id>")
def get_categories(seller_id):
    if seller_id not in SELLERS:
        return jsonify({"error": "unknown seller"}), 404
    return jsonify(CATEGORY_OPTIONS.get(seller_id, []))

@app.route("/api/products/<seller_id>", methods=["GET", "POST", "OPTIONS"])
def products_collection(seller_id):
    if request.method == "OPTIONS":
        return "", 204
    if seller_id not in SELLERS:
        return jsonify({"error": "unknown seller"}), 404

    conn = get_db()
    cur = conn.cursor()

    if request.method == "GET":
        cur.execute("SELECT * FROM products WHERE seller=%s ORDER BY category, name", (seller_id,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        for r in rows:
            r["sizes"] = [s for s in (r["sizes"] or "").split(",") if s]
            r["colors"] = [c for c in (r["colors"] or "").split(",") if c]
        return jsonify(rows)

    # POST — create a new product. Admin-only.
    if not check_admin(request):
        cur.close(); conn.close()
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    category = data.get("category")
    price = data.get("price")
    sizes = data.get("sizes", [])
    colors = data.get("colors", [])
    description = data.get("description", "")
    photo_url = data.get("photo_url", "")
    in_stock = bool(data.get("in_stock", True))

    valid_categories = [c["id"] for c in CATEGORY_OPTIONS.get(seller_id, [])]
    if not name or not price:
        cur.close(); conn.close()
        return jsonify({"error": "name and price are required"}), 400
    if category not in valid_categories:
        cur.close(); conn.close()
        return jsonify({"error": f"category must be one of {valid_categories}"}), 400

    product_id = uuid.uuid4().hex[:10]
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "INSERT INTO products (id, seller, name, category, price, sizes, colors, description, photo_url, in_stock, created_at, updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (product_id, seller_id, name, category, price, ",".join(sizes), ",".join(colors),
         description, photo_url, in_stock, now, now)
    )
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"id": product_id, "status": "created"})

@app.route("/api/products/<seller_id>/<product_id>", methods=["PUT", "DELETE", "OPTIONS"])
def products_item(seller_id, product_id):
    if request.method == "OPTIONS":
        return "", 204
    if seller_id not in SELLERS:
        return jsonify({"error": "unknown seller"}), 404
    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 401

    conn = get_db()
    cur = conn.cursor()

    if request.method == "DELETE":
        cur.execute("DELETE FROM products WHERE id=%s AND seller=%s", (product_id, seller_id))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"status": "deleted"})

    # PUT — update an existing product
    data = request.get_json(force=True, silent=True) or {}
    valid_categories = [c["id"] for c in CATEGORY_OPTIONS.get(seller_id, [])]
    category = data.get("category")
    if category is not None and category not in valid_categories:
        cur.close(); conn.close()
        return jsonify({"error": f"category must be one of {valid_categories}"}), 400

    fields, values = [], []
    for key in ["name", "category", "price", "description", "photo_url", "in_stock"]:
        if key in data:
            fields.append(f"{key}=%s")
            values.append(data[key])
    if "sizes" in data:
        fields.append("sizes=%s"); values.append(",".join(data["sizes"]))
    if "colors" in data:
        fields.append("colors=%s"); values.append(",".join(data["colors"]))
    fields.append("updated_at=%s"); values.append(datetime.now(timezone.utc).isoformat())

    if not fields:
        cur.close(); conn.close()
        return jsonify({"error": "nothing to update"}), 400

    values.extend([product_id, seller_id])
    cur.execute(f"UPDATE products SET {', '.join(fields)} WHERE id=%s AND seller=%s", values)
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"status": "updated"})


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
    for seller_id, seller in SELLERS.items():
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
        fee = count * FEE_PER_ORDER
        ids = [r["id"] for r in rows]

        report_link = f"{BASE_URL}/report/{seller_id}/{seller['report_token']}?ids={','.join(ids)}"
        message = (
            f"Today's sales are ready: {count} order(s), KSh {total} total. "
            f"View full details and what you owe Blessed Victor: {report_link}"
        )
        sent, info = send_sms(seller_id, message)
        if sent:
            cur.execute("UPDATE orders SET reported=TRUE WHERE id = ANY(%s)", (ids,))
            conn.commit()
        results[seller_id] = {"orders": count, "total": total, "fee": fee, "sent": sent, "report_link": report_link}
    cur.close(); conn.close()
    return jsonify(results)


REPORT_PAGE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sales Report — {{ seller_name }}</title>
<style>
  :root{ --espresso:#2A1B12; --rust:#B5451F; --rust-dark:#8F3517; --gold:#C89B4A; --cream:#EDE3D0; --cream-2:#F6F0E4; --green:#3f7a4d; }
  *{box-sizing:border-box;}
  body{font-family:sans-serif; background:var(--cream-2); color:var(--espresso); margin:0; padding:0 0 40px;}
  .hero{background:linear-gradient(180deg,var(--espresso),#241609); color:var(--cream); padding:36px 20px 30px; text-align:center;}
  .hero .eyebrow{color:var(--gold); font-weight:700; font-size:0.75rem; letter-spacing:0.08em; text-transform:uppercase; margin-bottom:8px;}
  .hero h1{font-size:1.4rem; margin-bottom:4px;}
  .hero .date{color:rgba(237,227,208,0.7); font-size:0.85rem;}
  .wrap{max-width:480px; margin:0 auto; padding:0 18px;}
  .stat-row{display:flex; gap:12px; margin:-22px 0 24px;}
  .stat-card{flex:1; background:#fff; border-radius:12px; padding:16px 14px; text-align:center; box-shadow:0 8px 20px rgba(42,27,18,0.12);}
  .stat-card .num{font-size:1.4rem; font-weight:800; color:var(--rust-dark);}
  .stat-card .label{font-size:0.72rem; color:#8a7d6f; margin-top:2px;}
  .owe-box{background:var(--rust); color:#fff; border-radius:12px; padding:18px 20px; margin-bottom:26px; box-shadow:0 10px 22px rgba(181,69,31,0.3);}
  .owe-box .owe-label{font-size:0.8rem; opacity:0.9; margin-bottom:4px;}
  .owe-box .owe-amount{font-size:1.6rem; font-weight:800;}
  .owe-box .owe-note{font-size:0.75rem; opacity:0.85; margin-top:6px;}
  h2{font-size:1.05rem; margin:0 0 12px;}
  .section{margin-bottom:28px;}
  .sold-item{background:#fff; border-radius:8px; padding:11px 14px; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center; box-shadow:0 4px 10px rgba(42,27,18,0.06);}
  .sold-item .name{font-size:0.9rem; font-weight:600;}
  .sold-item .meta{font-size:0.76rem; color:#8a7d6f;}
  .sold-item .qty{background:var(--gold); color:var(--espresso); font-weight:800; font-size:0.85rem; padding:4px 10px; border-radius:999px;}
  .order-row{background:#fff; border-radius:8px; padding:11px 14px; margin-bottom:8px; box-shadow:0 4px 10px rgba(42,27,18,0.06);}
  .order-row .top{display:flex; justify-content:space-between; font-size:0.88rem; font-weight:700;}
  .order-row .sub{font-size:0.76rem; color:#8a7d6f; margin-top:3px;}
  .empty{color:#8a7d6f; font-size:0.85rem; text-align:center; padding:20px;}
</style></head><body>
<div class="hero">
  <div class="eyebrow">Collective 254</div>
  <h1>{{ seller_name }}'s Sales Report</h1>
  <div class="date">Generated {{ generated_at }}</div>
</div>
<div class="wrap">
  <div class="stat-row">
    <div class="stat-card"><div class="num">{{ order_count }}</div><div class="label">Orders</div></div>
    <div class="stat-card"><div class="num">KSh {{ total_sales }}</div><div class="label">Total sales</div></div>
  </div>

  <div class="owe-box">
    <div class="owe-label">You owe Blessed Victor (Collective 254 fee)</div>
    <div class="owe-amount">KSh {{ fee_owed }}</div>
    <div class="owe-note">KSh {{ fee_per_order }} × {{ order_count }} confirmed order(s)</div>
  </div>

  <div class="section">
    <h2>What sold — for restocking</h2>
    {% if sold_items|length == 0 %}<div class="empty">Nothing in this batch.</div>{% endif %}
    {% for item in sold_items %}
    <div class="sold-item">
      <div><div class="name">{{ item.name }}</div><div class="meta">{% if item.size %}Size {{ item.size }}{% endif %}{% if item.color %} · {{ item.color }}{% endif %}</div></div>
      <div class="qty">{{ item.qty }}</div>
    </div>
    {% endfor %}
  </div>

  <div class="section">
    <h2>Orders in this batch</h2>
    {% for o in orders %}
    <div class="order-row">
      <div class="top"><span>{{ o.buyer_name }}</span><span>KSh {{ o.amount }}</span></div>
      <div class="sub">{{ o.phone }} · {{ o.area }} · confirmed {{ o.confirmed_at }}</div>
    </div>
    {% endfor %}
  </div>
</div>
</body></html>
"""

@app.route("/report/<seller_id>/<token>")
def daily_report(seller_id, token):
    seller = SELLERS.get(seller_id)
    if not seller or token != seller.get("report_token"):
        return "Not found", 404

    ids_param = request.args.get("ids", "")
    ids = [i for i in ids_param.split(",") if i]
    if not ids:
        return "No orders specified in this report link.", 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ANY(%s) AND seller=%s", (ids, seller_id))
    rows = cur.fetchall()
    cur.close(); conn.close()

    order_count = len(rows)
    total_sales = sum(r["amount"] for r in rows)
    fee_owed = order_count * FEE_PER_ORDER

    sold_agg = {}
    for r in rows:
        for item in json.loads(r["items_json"] or "[]"):
            key = (item.get("name"), item.get("size"), item.get("color"))
            if key not in sold_agg:
                sold_agg[key] = {"name": item.get("name"), "size": item.get("size"), "color": item.get("color"), "qty": 0}
            sold_agg[key]["qty"] += item.get("qty", 1)
    sold_items = sorted(sold_agg.values(), key=lambda x: -x["qty"])

    return render_template_string(
        REPORT_PAGE,
        seller_name=seller["name"],
        generated_at=datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        order_count=order_count,
        total_sales=total_sales,
        fee_owed=fee_owed,
        fee_per_order=FEE_PER_ORDER,
        sold_items=sold_items,
        orders=rows,
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
else:
    init_db()
