"""
Genius — מערכת מלאה
====================
כולל את כל מה שגילינו ב-reality check:
✓ Onboarding עם איסוף ספקים
✓ ניתוח אוטומטי עם Claude
✓ WhatsApp (Twilio) עם טיפול ב-templates
✓ הזמנות אוטומטיות לספקים
✓ תשלומים עם Stripe
✓ Scheduler לילי
✓ דאשבורד ניהול

הוראות ב-SETUP.md
"""

import os
import json
import csv
import io
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, redirect
from flask_cors import CORS
import anthropic

# ── אתחול ──
app = Flask(__name__)
CORS(app)  # allows the website (hosted on GitHub Pages, a different origin) to call this API

ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TWILIO_SID     = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM    = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
STRIPE_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_BASIC = os.environ.get("STRIPE_PRICE_BASIC", "")  # price_xxx של ₪299
STRIPE_PRICE_PRO   = os.environ.get("STRIPE_PRICE_PRO", "")    # price_xxx של ₪599

DB_FILE = "genius_db.json"

# ═══════════════════════════════════════
# מסד נתונים פשוט (JSON) — להחליף ב-Supabase בהמשך
# ═══════════════════════════════════════

def load_db():
    if not os.path.exists(DB_FILE):
        return {"stores": {}, "suppliers": {}, "orders": [], "recommendations": []}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

# ═══════════════════════════════════════
# WhatsApp — עם טיפול נכון ב-templates
# ═══════════════════════════════════════

def send_whatsapp(phone: str, message: str) -> bool:
    """
    שולח WhatsApp דרך Twilio.
    חשוב: ללקוח חדש שלא שלח הודעה ב-24 שעות האחרונות,
    Twilio ישלח רק template מאושר. אחרי שהלקוח עונה — חלון חופשי.
    """
    if not TWILIO_SID:
        print(f"[DEMO MODE] WhatsApp ל-{phone}:\n{message}\n")
        return True
    
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        
        if not phone.startswith("whatsapp:"):
            clean = phone.replace("-", "").replace(" ", "").lstrip("0")
            phone = f"whatsapp:+972{clean}"
        
        client.messages.create(from_=TWILIO_FROM, body=message, to=phone)
        print(f"✓ WhatsApp נשלח ל-{phone}")
        return True
    except Exception as e:
        print(f"✗ שגיאת WhatsApp: {e}")
        return False

# ═══════════════════════════════════════
# Claude — ניתוח מכירות
# ═══════════════════════════════════════

def analyze_with_claude(csv_text: str, store: dict, suppliers: dict) -> dict:
    """
    מנתח נתוני מכירות ומחזיר JSON מובנה עם המלצות.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    
    suppliers_info = "\n".join([
        f"- {s['name']}: {s.get('products', 'כללי')} (טלפון: {s['phone']})"
        for s in suppliers.values()
    ]) or "אין ספקים רשומים"
    
    prompt = f"""אתה סוכן AI של חנות "{store['name']}".

נתוני מכירות (CSV):
{csv_text}

ספקים של החנות:
{suppliers_info}

נתח את הנתונים והחזר אך ורק JSON תקין בפורמט הבא (בלי markdown, בלי הסברים):
{{
  "dead_products": [
    {{"name": "שם", "stock": 0, "days_no_sale": 0, "current_price": 0, "recommended_price": 0, "reason": "הסבר קצר"}}
  ],
  "hot_products": [
    {{"name": "שם", "weekly_sales": 0, "stock": 0, "days_until_empty": 0, "order_quantity": 0, "supplier": "שם ספק אם ידוע"}}
  ],
  "total_potential_savings": 0,
  "summary_he": "סיכום קצר בעברית של 2 משפטים"
}}

כללים:
- מוצר מת: לא נמכר 30+ יום
- מוצר חם: מכירות גבוהות + מלאי שיגמר בתוך שבוע
- מחיר מומלץ: הורדה של 20-30% למוצר מת
- אם אין נתון, השתמש ב-0 או null"""

    message = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    text = next(block.text for block in message.content if block.type == "text").strip()
    # נקה markdown אם יש
    text = text.replace("```json", "").replace("```", "").strip()
    
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": "ניתוח נכשל", "raw": text}

def format_whatsapp_report(analysis: dict, store_name: str) -> str:
    """ממיר ניתוח JSON להודעת WhatsApp יפה"""
    
    lines = [
        f"🏪 דוח Genius — {store_name}",
        f"📅 {datetime.now().strftime('%d/%m/%Y')}",
        ""
    ]
    
    dead = analysis.get("dead_products", [])
    if dead:
        lines.append("⚠️ מוצרים תקועים:")
        for i, p in enumerate(dead[:3], 1):
            lines.append(f"{i}. {p['name']} — {p['days_no_sale']} ימים ללא מכירה")
            lines.append(f"   המלצה: הורד מ-₪{p['current_price']} ל-₪{p['recommended_price']}")
        lines.append("")
    
    hot = analysis.get("hot_products", [])
    if hot:
        lines.append("📈 מוצרים חמים — הזמן מלאי:")
        for i, p in enumerate(hot[:3], 1):
            lines.append(f"{i}. {p['name']} — נגמר בעוד {p['days_until_empty']} ימים")
            lines.append(f"   הזמן: {p['order_quantity']} יח'" + (f" מ{p['supplier']}" if p.get('supplier') else ""))
        lines.append("")
    
    savings = analysis.get("total_potential_savings", 0)
    if savings:
        lines.append(f"💰 חיסכון פוטנציאלי: ₪{savings:,}")
        lines.append("")
    
    lines.append("✅ לאישור כל ההמלצות: ענה 1")
    lines.append("📞 להזמנה אוטומטית מהספקים: ענה 2")
    lines.append("❌ לדחייה: ענה 3")
    
    return "\n".join(lines)

# ═══════════════════════════════════════
# הזמנות אוטומטיות לספקים
# ═══════════════════════════════════════

def send_order_to_supplier(supplier: dict, store: dict, products: list) -> bool:
    """שולח הצעת רכש לספק ב-WhatsApp"""
    
    products_text = "\n".join([
        f"• {p['name']} — {p['order_quantity']} יחידות"
        for p in products
    ])
    
    message = f"""📦 הזמנה חדשה — Genius

שלום {supplier['name']},

חנות "{store['name']}" מעוניינת להזמין:

{products_text}

לאישור ההזמנה ומשלוח הצעת מחיר:
ענה למספר זה או התקשר ל-{store['phone']}

— נשלח אוטומטית ע"י Genius"""
    
    return send_whatsapp(supplier["phone"], message)

# ═══════════════════════════════════════
# API Routes
# ═══════════════════════════════════════

@app.route("/")
def home():
    db = load_db()
    return jsonify({
        "status": "Genius פועל ✓",
        "stores": len(db["stores"]),
        "suppliers": len(db["suppliers"]),
        "pending_recommendations": len([r for r in db["recommendations"] if r.get("status") == "pending"]),
        "time": datetime.now().strftime("%d/%m/%Y %H:%M")
    })

# ── Onboarding מלא — כולל ספקים ──

@app.route("/onboard", methods=["POST"])
def onboard():
    """
    חיבור חנות חדשה — כולל ספקים!
    {
      "name": "מכולת השכונה",
      "phone": "0501234567",
      "system": "hashavshevet",
      "plan": "basic",
      "suppliers": [
        {"name": "תנובה", "phone": "0521111111", "products": "מוצרי חלב"},
        {"name": "אסם", "phone": "0532222222", "products": "יבשים"}
      ]
    }
    """
    data = request.json
    if not data or not data.get("name") or not data.get("phone"):
        return jsonify({"error": "חסרים שם וטלפון"}), 400
    
    db = load_db()
    store_id = data["phone"].replace("-", "").replace("+", "")
    
    # שמור חנות
    db["stores"][store_id] = {
        "name": data["name"],
        "phone": data["phone"],
        "system": data.get("system", "other"),
        "plan": data.get("plan", "trial"),  # trial = 30 יום חינם
        "trial_ends": (datetime.now() + timedelta(days=30)).isoformat(),
        "joined": datetime.now().isoformat(),
        "active": True,
        "stripe_customer_id": None
    }
    
    # שמור ספקים — הדבר שפיספסנו!
    for sup in data.get("suppliers", []):
        sup_id = sup["phone"].replace("-", "")
        db["suppliers"][sup_id] = {
            "name": sup["name"],
            "phone": sup["phone"],
            "products": sup.get("products", ""),
            "store_ids": db["suppliers"].get(sup_id, {}).get("store_ids", []) + [store_id]
        }
    
    save_db(db)
    
    # הודעת ברוכים הבאים
    welcome = f"""🎉 ברוכים הבאים ל-Genius!

שלום {data['name']},

החנות שלך חוברה בהצלחה.
✓ 30 ימי ניסיון חינם
✓ ניתוח ראשון — מחר ב-7:00 בבוקר
✓ {len(data.get('suppliers', []))} ספקים חוברו

חשוב: שמור את המספר הזה באנשי קשר
כדי לקבל את העדכונים שלנו.

לשאלות — פשוט ענה להודעה זו."""
    
    send_whatsapp(data["phone"], welcome)
    
    return jsonify({"success": True, "store_id": store_id})

# ── ניתוח CSV ──

@app.route("/analyze", methods=["POST"])
def analyze():
    """ניתוח קובץ מכירות + שליחת WhatsApp + שמירת המלצות"""
    store_id = request.form.get("store_id")
    db = load_db()
    store = db["stores"].get(store_id)
    
    if not store:
        return jsonify({"error": "חנות לא נמצאה"}), 404
    if "file" not in request.files:
        return jsonify({"error": "חסר קובץ"}), 400
    
    csv_text = request.files["file"].read().decode("utf-8-sig")
    
    # ספקים של החנות הזו
    store_suppliers = {
        k: v for k, v in db["suppliers"].items()
        if store_id in v.get("store_ids", [])
    }
    
    # נתח עם Claude
    analysis = analyze_with_claude(csv_text, store, store_suppliers)
    
    if "error" in analysis:
        return jsonify(analysis), 500
    
    # שמור המלצות
    rec_id = f"rec_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    db["recommendations"].append({
        "id": rec_id,
        "store_id": store_id,
        "analysis": analysis,
        "status": "pending",
        "created": datetime.now().isoformat()
    })
    save_db(db)
    
    # שלח WhatsApp
    report = format_whatsapp_report(analysis, store["name"])
    send_whatsapp(store["phone"], report)
    
    return jsonify({"success": True, "analysis": analysis})

# ── Webhook — תגובות WhatsApp ──

@app.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_webhook():
    """מטפל בתגובות של בעלי חנויות"""
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()
    
    db = load_db()
    
    # מצא את החנות
    store_id, store = None, None
    for sid, s in db["stores"].items():
        clean = s["phone"].replace("-", "").lstrip("0")
        if clean in from_number:
            store_id, store = sid, s
            break
    
    if not store:
        return "OK", 200
    
    # מצא המלצה אחרונה ממתינה
    pending = [r for r in db["recommendations"] 
               if r["store_id"] == store_id and r["status"] == "pending"]
    last_rec = pending[-1] if pending else None
    
    if body == "1" and last_rec:
        # אישור כל ההמלצות
        last_rec["status"] = "approved"
        save_db(db)
        reply = "✅ כל ההמלצות אושרו!\nהמחירים יעודכנו במערכת.\nאל תשכח להחליף את המדבקות 😊"
        
    elif body == "2" and last_rec:
        # הזמנה אוטומטית מספקים — הפיצ'ר שפיספסנו!
        hot = last_rec["analysis"].get("hot_products", [])
        sent_count = 0
        
        for sup_id, sup in db["suppliers"].items():
            if store_id not in sup.get("store_ids", []):
                continue
            # מצא מוצרים של הספק הזה
            sup_products = [p for p in hot if p.get("supplier") == sup["name"]]
            if not sup_products:
                continue
            if send_order_to_supplier(sup, store, sup_products):
                sent_count += 1
        
        last_rec["status"] = "ordered"
        save_db(db)
        
        if sent_count:
            reply = f"📦 נשלחו הזמנות ל-{sent_count} ספקים!\nהם יחזרו אליך ישירות עם הצעות מחיר."
        else:
            reply = "לא נמצאו ספקים מתאימים למוצרים האלה.\nהוסף ספקים באתר או ענה עם שם וטלפון של ספק."
            
    elif body == "3" and last_rec:
        last_rec["status"] = "rejected"
        save_db(db)
        reply = "הבנתי, ההמלצות נדחו.\nאני לומד מזה — ההמלצות הבאות יהיו מדויקות יותר."
        
    else:
        reply = """קיבלתי ✓
1 — אישור המלצות
2 — הזמנה אוטומטית מספקים  
3 — דחייה
או שאל אותי כל שאלה על החנות"""
    
    send_whatsapp(store["phone"], reply)
    return "OK", 200

CHAT_SYSTEM_PROMPTS = {
    "store": "You are the Genius AI agent for a small retail store. Answer briefly and practically in Hebrew, based on general retail best practices (dead stock, hot products, pricing). Keep replies under 4 sentences.",
    "admin": "You are the Genius AI agent for the network administrator, overseeing multiple stores. Answer briefly in Hebrew with network-level insights. Keep replies under 4 sentences.",
    "supplier": "You are the Genius AI agent helping a supplier manage orders and pricing on the Genius network. Answer briefly in Hebrew. Keep replies under 4 sentences.",
}

@app.route("/chat", methods=["POST"])
def chat():
    data = request.json or {}
    message = (data.get("message") or "").strip()
    role = data.get("role", "store")
    history = data.get("history", [])

    if not message:
        return jsonify({"error": "Missing message"}), 400

    if not ANTHROPIC_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        messages = (history or []) + [{"role": "user", "content": message}]
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=500,
            system=CHAT_SYSTEM_PROMPTS.get(role, CHAT_SYSTEM_PROMPTS["store"]),
            messages=messages,
        )
        reply = next(block.text for block in response.content if block.type == "text")
        return jsonify({"reply": reply})
    except Exception as e:
        print(f"Chat error: {e}")
        return jsonify({"error": str(e)}), 500

# ── Stripe — תשלומים (הדבר שפיספסנו לגמרי) ──

@app.route("/subscribe/<store_id>/<plan>")
def subscribe(store_id, plan):
    """יוצר לינק תשלום Stripe לחנות"""
    if not STRIPE_KEY:
        return jsonify({"error": "Stripe לא מוגדר — הוסף STRIPE_SECRET_KEY"}), 500
    
    import stripe
    stripe.api_key = STRIPE_KEY
    
    db = load_db()
    store = db["stores"].get(store_id)
    if not store:
        return jsonify({"error": "חנות לא נמצאה"}), 404
    
    price_id = STRIPE_PRICE_BASIC if plan == "basic" else STRIPE_PRICE_PRO
    
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=request.host_url + f"payment-success/{store_id}",
        cancel_url=request.host_url,
        metadata={"store_id": store_id}
    )
    
    return redirect(session.url)

@app.route("/payment-success/<store_id>")
def payment_success(store_id):
    db = load_db()
    if store_id in db["stores"]:
        db["stores"][store_id]["plan"] = "paid"
        save_db(db)
        send_whatsapp(db["stores"][store_id]["phone"], 
                     "✅ התשלום התקבל! המנוי שלך פעיל.\nתודה שבחרת ב-Genius 💚")
    return "התשלום הצליח! אפשר לסגור את החלון."

@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    """מטפל באירועי Stripe — תשלום נכשל, מנוי בוטל וכו'"""
    event = request.json
    
    if event.get("type") == "invoice.payment_failed":
        store_id = event["data"]["object"].get("metadata", {}).get("store_id")
        if store_id:
            db = load_db()
            if store_id in db["stores"]:
                send_whatsapp(db["stores"][store_id]["phone"],
                             "⚠️ התשלום החודשי נכשל.\nעדכן את אמצעי התשלום כדי להמשיך לקבל המלצות.")
    
    return "OK", 200

# ── דאשבורד ניהול ──

@app.route("/admin")
def admin_dashboard():
    """דאשבורד פשוט למנהל — JSON לעכשיו"""
    db = load_db()
    return jsonify({
        "stores": db["stores"],
        "suppliers": db["suppliers"],
        "recommendations": db["recommendations"][-20:],  # 20 אחרונות
        "stats": {
            "total_stores": len(db["stores"]),
            "active_stores": len([s for s in db["stores"].values() if s.get("active")]),
            "trial_stores": len([s for s in db["stores"].values() if s.get("plan") == "trial"]),
            "paid_stores": len([s for s in db["stores"].values() if s.get("plan") == "paid"]),
        }
    })

# ===========================================
# Transfer opportunities between stores
# ===========================================
def find_transfer_opportunities(db):
    latest = {}
    for rec in db["recommendations"]:
        sid = rec["store_id"]
        if sid not in latest or rec["created"] > latest[sid]["created"]:
            latest[sid] = rec
    opps = []
    ids = list(latest.keys())
    for a in ids:
        sa = db["stores"].get(a)
        if not sa:
            continue
        dead = latest[a]["analysis"].get("dead_products", [])
        for b in ids:
            if a == b:
                continue
            sb = db["stores"].get(b)
            if not sb:
                continue
            hot = latest[b]["analysis"].get("hot_products", [])
            for dp in dead:
                if dp.get("stock", 0) <= 0:
                    continue
                for hp in hot:
                    n1 = (dp.get("name") or "").strip().lower()
                    n2 = (hp.get("name") or "").strip().lower()
                    if n1 != n2:
                        continue
                    qty = min(dp.get("stock", 0), hp.get("order_quantity", dp.get("stock", 0)))
                    if qty <= 0:
                        continue
                    reason = ("עודף אצל " + str(sa.get("name")) + " · מחסור אצל "
                              + str(sb.get("name")))
                    opps.append({"product_name": dp.get("name"), "from_store_id": a, "from_store_name": sa.get("name"), "from_store_phone": sa.get("phone"), "to_store_id": b, "to_store_name": sb.get("name"), "to_store_phone": sb.get("phone"), "suggested_quantity": qty, "suggested_price": dp.get("recommended_price"), "reason": reason})
    return opps

@app.route("/network/transfer-opportunities", methods=["GET"])
def transfer_opportunities():
    db = load_db()
    opps = find_transfer_opportunities(db)
    return jsonify({"opportunities": opps, "count": len(opps)})

@app.route("/network/transfer-opportunities/notify", methods=["POST"])
def notify_transfer_opportunities():
    data = request.json or {}
    db = load_db()
    opps = find_transfer_opportunities(db)
    match = None
    for o in opps:
            same_from = o["from_store_id"] == data.get("from_store_id")
            same_to = o["to_store_id"] == data.get("to_store_id")
            same_name = o["product_name"] == data.get("product_name")
            if same_from and same_to and same_name:
                match = o
                break
    if not match:
        return jsonify({"error": "not found"}), 404
    qty2 = str(match["suggested_quantity"])
    m1 = "Genius opportunity: " + qty2 + " units of " + match["product_name"] + " available. Contact: " + str(match["to_store_phone"])
    m2 = "Genius opportunity: " + match["from_store_name"] + " has " + qty2 + " units of " + match["product_name"] + " you need. Contact: " + str(match["from_store_phone"])
    s1 = send_whatsapp(match["from_store_phone"], m1)
    s2 = send_whatsapp(match["to_store_phone"], m2)
    notified_list = [match["from_store_id"], match["to_store_id"]]
    return jsonify({"success": s1 and s2, "notified": notified_list})

def nightly_job():
    """
    Nightly analysis job
    """
    print(f"\n=== ניתוח לילי {datetime.now()} ===")
    db = load_db()
    
    for store_id, store in db["stores"].items():
        if not store.get("active"):
            continue
        
        # בדוק אם תקופת הניסיון הסתיימה
        if store.get("plan") == "trial":
            trial_end = datetime.fromisoformat(store["trial_ends"])
            days_left = (trial_end - datetime.now()).days
            
            if days_left == 3:
                send_whatsapp(store["phone"], 
                    f"שלום {store['name']}! 👋\nנשארו 3 ימים לתקופת הניסיון.\nלהמשך השירות: {os.environ.get('REPLIT_URL', '')}/subscribe/{store_id}/basic")
            elif days_left <= 0:
                store["active"] = False
                send_whatsapp(store["phone"],
                    "תקופת הניסיון הסתיימה 😢\nנשמח שתחזור! להפעלה מחדש פנה אלינו.")
        
        print(f"  {store['name']}: {'פעיל' if store.get('active') else 'לא פעיל'}")
    
    save_db(db)

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(nightly_job, "cron", hour=3, minute=0, timezone="Asia/Jerusalem")
    scheduler.start()
    print("✓ Scheduler הופעל — ניתוח לילי ב-3:00")
except ImportError:
    print("! APScheduler לא מותקן — אין ניתוח אוטומטי")

# ── הפעלה ──


# ===========================================
# Demand forecasting - function #13
# ===========================================
def parse_sales_csv(csv_text):
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    sales = {}
    def pick(row, options):
        for key in row:
            if key and key.replace("\ufeff", "").strip().strip('"').lower() in options:
                return row[key]
        return None
    date_keys = {"date", "day", "sale_date", "תאריך", "תאריך מכירה", "יום", "תאריך מסמך"}
    name_keys = {"product", "name", "product_name", "item", "description",
                 "מוצר", "שם", "שם מוצר", "שם פריט", "פריט", "תיאור", "תיאור פריט"}
    qty_keys = {"qty", "quantity", "units", "sold",
                "כמות", "כמות שנמכרה", "נמכר", "יחידות", "יח'"}
    for row in reader:
        raw_date = pick(row, date_keys)
        raw_name = pick(row, name_keys)
        raw_qty = pick(row, qty_keys)
        if not raw_name or not raw_qty:
            continue
        name = raw_name.strip()
        if not name:
            continue
        try:
            qty = float(str(raw_qty).strip())
        except (ValueError, TypeError):
            continue
        d = None
        if raw_date:
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
                try:
                    d = datetime.strptime(str(raw_date).strip(), fmt)
                    break
                except ValueError:
                    continue
        sales.setdefault(name, []).append((d, qty))
    return sales

def forecast_product(history, current_stock, lead_time_days=3):
    dated = [(d, q) for d, q in history if d is not None]
    total_qty = sum(q for _, q in history)
    if len(dated) >= 2:
        dates = [d for d, _ in dated]
        span_days = (max(dates) - min(dates)).days + 1
        dated_qty = sum(q for _, q in dated)
        daily_rate = dated_qty / span_days if span_days > 0 else dated_qty
    elif total_qty > 0:
        daily_rate = total_qty / 30
    else:
        daily_rate = 0.0
    trend = "stable"
    if len(dated) >= 4:
        ordered = sorted(dated, key=lambda x: x[0])
        mid = len(ordered) // 2
        first = sum(q for _, q in ordered[:mid])
        second = sum(q for _, q in ordered[mid:])
        if first > 0:
            change = (second - first) / first
            if change > 0.20:
                trend = "rising"
            elif change < -0.20:
                trend = "falling"
    adjusted_rate = daily_rate
    if trend == "rising":
        adjusted_rate = daily_rate * 1.25
    elif trend == "falling":
        adjusted_rate = daily_rate * 0.80
    if adjusted_rate > 0:
        days_until_empty = int(current_stock / adjusted_rate)
    else:
        days_until_empty = 999
    reorder_now = days_until_empty <= lead_time_days + 2
    needed = adjusted_rate * (14 + lead_time_days)
    order_quantity = max(0, int(round(needed - current_stock)))
    if len(dated) >= 10:
        confidence = "high"
    elif len(dated) >= 4 or total_qty > 0:
        confidence = "medium"
    else:
        confidence = "low"
    return {"daily_rate": round(daily_rate, 2), "adjusted_daily_rate": round(adjusted_rate, 2), "days_until_empty": days_until_empty, "reorder_now": reorder_now, "order_quantity": order_quantity, "trend": trend, "confidence": confidence, "data_points": len(history)}

def forecast_all(csv_text, stock_map, lead_time_days=3):
    sales = parse_sales_csv(csv_text)
    results = []
    every = [d for h in sales.values() for d, q in h if d is not None]
    data_end = max(every) if every else None
    for name, history in sales.items():
        stock = stock_map.get(name, 0)
        f = forecast_product(history, stock, lead_time_days)
        sold = [d for d, q in history if d is not None and q > 0]
        # the average over the whole history would keep "selling" an item
        # that stopped weeks ago — once it's been silent 14+ days, it's stopped
        if data_end is not None and sold and (data_end - max(sold)).days >= 14:
            f.update({"adjusted_daily_rate": 0.0, "days_until_empty": 999,
                      "reorder_now": False, "order_quantity": 0, "trend": "stopped",
                      "days_since_last_sale": (data_end - max(sold)).days})
        f["product_name"] = name
        f["current_stock"] = stock
        results.append(f)
    results.sort(key=lambda r: r["days_until_empty"])
    return results

@app.route("/forecast/<store_id>", methods=["POST"])
def forecast_endpoint(store_id):
    data = request.json or {}
    csv_text = data.get("csv", "")
    stock_map = data.get("stock", {})
    lead_time = data.get("lead_time_days", 3)
    if not csv_text:
        return jsonify({"error": "missing csv"}), 400
    try:
        results = forecast_all(csv_text, stock_map, lead_time)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    urgent = [r for r in results if r["reorder_now"]]
    return jsonify({"store_id": store_id, "forecasts": results, "urgent_count": len(urgent), "total_products": len(results)})

# ===========================================
# Group buying - function #10
# ===========================================

def volume_tier(total_qty):
    """Volume discount tier based on aggregated order size."""
    if total_qty >= 500:
        return "platinum", 15
    if total_qty >= 200:
        return "gold", 10
    if total_qty >= 100:
        return "silver", 7
    if total_qty >= 50:
        return "bronze", 4
    return "none", 0


def find_group_buying_opportunities(db, min_stores=2):
    """
    Finds products several stores need at the same time, so their
    orders can be combined into one volume order at a better price.
    """
    latest = {}
    for rec in db.get("recommendations", []):
        sid = rec.get("store_id")
        if sid is None:
            continue
        if sid not in latest or rec.get("created", "") > latest[sid].get("created", ""):
            latest[sid] = rec

    demand = {}
    for sid, rec in latest.items():
        store = db.get("stores", {}).get(sid)
        if not store:
            continue
        analysis = rec.get("analysis") or {}
        for p in analysis.get("hot_products", []) or []:
            raw = p.get("name")
            name = (raw or "").strip()
            if not name:
                continue
            try:
                qty = int(p.get("order_quantity", 0) or 0)
            except (ValueError, TypeError):
                continue
            if qty <= 0:
                continue
            key = name.lower()
            if key not in demand:
                demand[key] = {"display_name": name, "participants": []}
            demand[key]["participants"].append({
                "store_id": sid,
                "store_name": store.get("name"),
                "store_phone": store.get("phone"),
                "quantity": qty,
            })

    opps = []
    for info in demand.values():
        parts = info["participants"]
        if len(parts) < min_stores:
            continue
        total = sum(p["quantity"] for p in parts)
        tier, pct = volume_tier(total)
        opps.append({
            "product_name": info["display_name"],
            "participating_stores": len(parts),
            "total_quantity": total,
            "participants": parts,
            "discount_tier": tier,
            "estimated_discount_pct": pct,
        })

    opps.sort(key=lambda o: o["total_quantity"], reverse=True)
    return opps


# ===========================================
# Data-driven lending - function #12
# ===========================================

def monthly_revenue_from_sales(sales, price_map=None):
    """
    sales: {product: [(date, qty), ...]} from parse_sales_csv
    Returns {"YYYY-MM": revenue} using price_map when available,
    otherwise unit counts as a proxy.
    """
    price_map = price_map or {}
    buckets = {}
    for name, history in sales.items():
        price = price_map.get(name)
        try:
            price = float(price) if price is not None else 1.0
        except (ValueError, TypeError):
            price = 1.0
        for d, qty in history:
            if d is None:
                continue
            key = "%04d-%02d" % (d.year, d.month)
            buckets[key] = buckets.get(key, 0.0) + (qty * price)
    return buckets


def calculate_credit_profile(csv_text, price_map=None):
    """
    Scores a store's creditworthiness from its own sales history.
    No external credit bureau - the data the store already gave us.
    """
    sales = parse_sales_csv(csv_text)
    monthly = monthly_revenue_from_sales(sales, price_map)

    months = sorted(monthly.keys())
    values = [monthly[m] for m in months]
    n = len(values)

    if n == 0:
        return {
            "months_of_data": 0,
            "avg_monthly_revenue": 0,
            "trend": "unknown",
            "volatility_pct": 0,
            "score": 0,
            "risk": "insufficient_data",
            "max_loan": 0,
            "reason": "no dated sales data",
        }

    avg = sum(values) / n

    # trend: first half vs second half
    trend = "stable"
    if n >= 2:
        mid = n // 2
        first = sum(values[:mid]) / max(1, mid)
        second = sum(values[mid:]) / max(1, n - mid)
        if first > 0:
            change = (second - first) / first
            if change > 0.15:
                trend = "growing"
            elif change < -0.15:
                trend = "declining"

    # volatility: coefficient of variation
    if n >= 2 and avg > 0:
        var = sum((v - avg) ** 2 for v in values) / n
        volatility = (var ** 0.5) / avg * 100
    else:
        volatility = 0.0

    # score 0-100
    score = 50
    if n >= 6:
        score += 20
    elif n >= 3:
        score += 10
    else:
        score -= 10

    if trend == "growing":
        score += 20
    elif trend == "declining":
        score -= 20

    if volatility < 20:
        score += 15
    elif volatility < 40:
        score += 5
    else:
        score -= 15

    score = max(0, min(100, score))

    if score >= 75:
        risk, mult = "low", 2.0
    elif score >= 55:
        risk, mult = "medium", 1.2
    elif score >= 35:
        risk, mult = "high", 0.5
    else:
        risk, mult = "very_high", 0.0

    max_loan = int(round(avg * mult))

    return {
        "months_of_data": n,
        "avg_monthly_revenue": round(avg, 2),
        "trend": trend,
        "volatility_pct": round(volatility, 1),
        "score": score,
        "risk": risk,
        "max_loan": max_loan,
        "monthly_breakdown": {m: round(monthly[m], 2) for m in months},
    }


@app.route("/network/group-buying", methods=["GET"])
def group_buying():
    db = load_db()
    opps = find_group_buying_opportunities(db)
    return jsonify({"opportunities": opps, "count": len(opps)})


@app.route("/network/group-buying/notify", methods=["POST"])
def notify_group_buying():
    data = request.json or {}
    product = data.get("product_name")
    if not product:
        return jsonify({"error": "missing product_name"}), 400
    db = load_db()
    opps = find_group_buying_opportunities(db)
    match = None
    for o in opps:
        if o["product_name"].strip().lower() == str(product).strip().lower():
            match = o
            break
    if not match:
        return jsonify({"error": "no matching group"}), 404
    sent = []
    for part in match["participants"]:
        msg = (
            "Genius group buy: " + str(match["total_quantity"]) + " units of "
            + match["product_name"] + " across " + str(match["participating_stores"])
            + " stores. Est. discount " + str(match["estimated_discount_pct"])
            + "%. Your share: " + str(part["quantity"]) + " units."
        )
        ok = send_whatsapp(part["store_phone"], msg)
        sent.append({"store_id": part["store_id"], "sent": bool(ok)})
    return jsonify({"product_name": match["product_name"], "notified": sent})


@app.route("/lending/<store_id>", methods=["POST"])
def lending_endpoint(store_id):
    data = request.json or {}
    csv_text = data.get("csv", "")
    price_map = data.get("prices", {})
    if not csv_text:
        return jsonify({"error": "missing csv"}), 400
    try:
        profile = calculate_credit_profile(csv_text, price_map)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    profile["store_id"] = store_id
    return jsonify(profile)

# ===========================================
# Demo data seeding
# ===========================================

def _demo_sales_csv(base, months=6, growth=0):
    """Builds a realistic sales CSV: 3 sale days a month, optional growth."""
    rows = ["date,product,qty"]
    for i in range(months):
        month = 3 + i
        for day in (2, 4, 7, 9, 11, 14, 16, 18, 21, 23, 25, 28):
            for name, qty in base:
                q = int(qty * (1 + growth * i))
                rows.append("2026-%02d-%02d,%s,%d" % (month, day, name, max(1, q)))
    return "\n".join(rows)


DEMO_STORES = {
    "demo_pharm": {
        "name": "כהן פארם",
        "phone": "0501234567",
        "active": True,
        "demo": True,
        "stock": {"חלב תנובה 3%": 40, "שמפו הד אנד שולדרס": 25, "קרם הגנה SPF 50": 45, "לחם אחיד": 60, "ביצים L (12)": 90, "קוטג' 5%": 75, "קפה עלית 200g": 40, "נייר טואלט 32": 25, "שמן קנולה 1ל": 55, "גבינה צהובה 28%": 48, "סוכר 1ק\"ג": 70},
        "prices": {"חלב תנובה 3%": 7, "שמפו הד אנד שולדרס": 24, "קרם הגנה SPF 50": 38, "לחם אחיד": 7, "ביצים L (12)": 14, "קוטג' 5%": 6, "קפה עלית 200g": 23, "נייר טואלט 32": 39, "שמן קנולה 1ל": 12, "גבינה צהובה 28%": 32, "סוכר 1ק\"ג": 6},
        "sales_base": [("חלב תנובה 3%", 20), ("שמפו הד אנד שולדרס", 5),
                        ("לחם אחיד", 45), ("ביצים L (12)", 30), ("קוטג' 5%", 38),
                        ("קפה עלית 200g", 12), ("נייר טואלט 32", 9), ("שמן קנולה 1ל", 16)],
        "growth": 0.06,
        "analysis": {
            "dead_products": [
                {"name": "קרם הגנה SPF 50", "stock": 45, "days_no_sale": 62,
                 "current_price": 38, "recommended_price": 27,
                 "reason": "סוף עונת הקיץ — המלאי תקוע"},
            ],
            "hot_products": [
                {"name": "חלב תנובה 3%", "weekly_sales": 140, "stock": 40,
                 "days_until_empty": 2, "order_quantity": 150, "supplier": "תנובה"},
                {"name": "שמפו הד אנד שולדרס", "weekly_sales": 32, "stock": 25,
                 "days_until_empty": 5, "order_quantity": 80, "supplier": "P&G"},
            ],
            "total_potential_savings": 1870,
            "summary_he": "שני מוצרים עומדים להיגמר השבוע. קרם ההגנה תקוע 62 יום — כדאי להוריד מחיר.",
        },
    },
    "demo_super": {
        "name": "סופר דיזנגוף",
        "phone": "0502345678",
        "active": True,
        "demo": True,
        "stock": {"חלב תנובה 3%": 55, "קרם הגנה SPF 50": 8, "שמפו הד אנד שולדרס": 18, "מטרייה מתקפלת": 30, "לחם אחיד": 60, "ביצים L (12)": 90, "קוטג' 5%": 75, "קפה עלית 200g": 40, "נייר טואלט 32": 25, "שמן קנולה 1ל": 55, "גבינה צהובה 28%": 48, "סוכר 1ק\"ג": 70},
        "prices": {"חלב תנובה 3%": 7, "קרם הגנה SPF 50": 38, "שמפו הד אנד שולדרס": 24, "מטרייה מתקפלת": 45, "לחם אחיד": 7, "ביצים L (12)": 14, "קוטג' 5%": 6, "קפה עלית 200g": 23, "נייר טואלט 32": 39, "שמן קנולה 1ל": 12, "גבינה צהובה 28%": 32, "סוכר 1ק\"ג": 6},
        "sales_base": [("חלב תנובה 3%", 28), ("קרם הגנה SPF 50", 3), ("שמפו הד אנד שולדרס", 4),
                        ("לחם אחיד", 70), ("ביצים L (12)", 52), ("קוטג' 5%", 61),
                        ("קפה עלית 200g", 22), ("נייר טואלט 32", 17), ("שמן קנולה 1ל", 26),
                        ("גבינה צהובה 28%", 33), ("סוכר 1ק\"ג", 19)],
        "growth": 0.10,
        "analysis": {
            "dead_products": [
                {"name": "מטרייה מתקפלת", "stock": 30, "days_no_sale": 95,
                 "current_price": 45, "recommended_price": 32,
                 "reason": "מחוץ לעונה — 95 יום ללא מכירה"},
            ],
            "hot_products": [
                {"name": "חלב תנובה 3%", "weekly_sales": 195, "stock": 55,
                 "days_until_empty": 2, "order_quantity": 200, "supplier": "תנובה"},
                {"name": "קרם הגנה SPF 50", "weekly_sales": 21, "stock": 8,
                 "days_until_empty": 3, "order_quantity": 40, "supplier": "ניאוטרוגינה"},
                {"name": "שמפו הד אנד שולדרס", "weekly_sales": 25, "stock": 18,
                 "days_until_empty": 5, "order_quantity": 60, "supplier": "P&G"},
            ],
            "total_potential_savings": 2340,
            "summary_he": "החלב נגמר בעוד יומיים. קרם הגנה חסר — יש עודף בכהן פארם ברשת.",
        },
    },
    "demo_makolet": {
        "name": "מכולת הרצל",
        "phone": "0503456789",
        "active": True,
        "demo": True,
        "stock": {"חלב תנובה 3%": 30, "ממתקי פורים": 80, "לחם אחיד": 60, "ביצים L (12)": 90, "קוטג' 5%": 75, "קפה עלית 200g": 40, "נייר טואלט 32": 25, "שמן קנולה 1ל": 55, "גבינה צהובה 28%": 48, "סוכר 1ק\"ג": 70},
        "prices": {"חלב תנובה 3%": 7, "ממתקי פורים": 15, "לחם אחיד": 7, "ביצים L (12)": 14, "קוטג' 5%": 6, "קפה עלית 200g": 23, "נייר טואלט 32": 39, "שמן קנולה 1ל": 12, "גבינה צהובה 28%": 32, "סוכר 1ק\"ג": 6},
        "sales_base": [("חלב תנובה 3%", 13),
                        ("לחם אחיד", 24), ("ביצים L (12)", 15), ("קוטג' 5%", 18),
                        ("קפה עלית 200g", 6), ("שמן קנולה 1ל", 8)],
        "growth": -0.10,
        "analysis": {
            "dead_products": [
                {"name": "ממתקי פורים", "stock": 80, "days_no_sale": 140,
                 "current_price": 15, "recommended_price": 9,
                 "reason": "החג עבר לפני חודשים — נזילות תקועה"},
            ],
            "hot_products": [
                {"name": "חלב תנובה 3%", "weekly_sales": 88, "stock": 30,
                 "days_until_empty": 3, "order_quantity": 90, "supplier": "תנובה"},
            ],
            "total_potential_savings": 1200,
            "summary_he": "מכירות בירידה קלה. ממתקי פורים תקועים 140 יום.",
        },
    },
}


def build_demo_db(db):
    """Adds (or refreshes) the demo stores. Never touches real stores."""
    from datetime import datetime
    now = datetime.now().isoformat()

    db.setdefault("stores", {})
    db.setdefault("recommendations", [])
    db.setdefault("suppliers", {})

    # drop previous demo rows so re-seeding stays idempotent
    db["recommendations"] = [
        r for r in db["recommendations"] if r.get("store_id") not in DEMO_STORES
    ]

    created = []
    for sid, spec in DEMO_STORES.items():
        db["stores"][sid] = {
            "name": spec["name"],
            "phone": spec["phone"],
            "active": True,
            "demo": True,
            "stock": spec["stock"],
            "prices": spec["prices"],
            "sales_csv": _demo_sales_csv(spec["sales_base"], 6, spec["growth"]),
        }
        db["recommendations"].append({
            "store_id": sid,
            "created": now,
            "analysis": spec["analysis"],
        })
        created.append({"store_id": sid, "name": spec["name"]})

    created.append(build_bakery_demo(db))
    return created


@app.route("/admin/seed-demo", methods=["POST"])
def seed_demo():
    """
    Populates three demo stores so the network screens have something
    real to render. Idempotent, and leaves real stores untouched.
    """
    db = load_db()
    created = build_demo_db(db)
    save_db(db)
    return jsonify({
        "seeded": created,
        "total_stores": len(db.get("stores", {})),
    })


@app.route("/admin/seed-demo", methods=["DELETE"])
def remove_demo():
    """Removes every demo store and its recommendations."""
    db = load_db()
    removed = [sid for sid in list(db.get("stores", {})) if sid in DEMO_STORES]
    for sid in removed:
        db["stores"].pop(sid, None)
    db["recommendations"] = [
        r for r in db.get("recommendations", []) if r.get("store_id") not in DEMO_STORES
    ]
    save_db(db)
    return jsonify({"removed": removed, "total_stores": len(db.get("stores", {}))})


@app.route("/forecast/<store_id>", methods=["GET"])
def forecast_saved(store_id):
    """Forecast from the sales data already saved on the store."""
    db = load_db()
    store = db.get("stores", {}).get(store_id)
    if not store:
        return jsonify({"error": "store not found"}), 404
    csv_text = store.get("sales_csv")
    if not csv_text:
        return jsonify({"error": "no saved sales data for this store"}), 404
    results = forecast_all(csv_text, store.get("stock", {}), 3)
    urgent = [r for r in results if r["reorder_now"]]
    return jsonify({
        "store_id": store_id,
        "store_name": store.get("name"),
        "forecasts": results,
        "urgent_count": len(urgent),
        "total_products": len(results),
    })


@app.route("/lending/<store_id>", methods=["GET"])
def lending_saved(store_id):
    """Credit profile from the sales data already saved on the store."""
    db = load_db()
    store = db.get("stores", {}).get(store_id)
    if not store:
        return jsonify({"error": "store not found"}), 404
    csv_text = store.get("sales_csv")
    if not csv_text:
        return jsonify({"error": "no saved sales data for this store"}), 404
    profile = calculate_credit_profile(csv_text, store.get("prices", {}))
    profile["store_id"] = store_id
    profile["store_name"] = store.get("name")
    return jsonify(profile)

@app.route("/store/<store_id>", methods=["GET"])
def store_state(store_id):
    """
    Everything the store's own screens need: latest analysis, weekly
    revenue and a unified product list with status and recommendation.
    """
    db = load_db()
    store = db.get("stores", {}).get(store_id)
    if not store:
        return jsonify({"error": "store not found"}), 404

    # latest analysis for this store
    latest = None
    for rec in db.get("recommendations", []):
        if rec.get("store_id") != store_id:
            continue
        if latest is None or rec.get("created", "") > latest.get("created", ""):
            latest = rec
    analysis = (latest or {}).get("analysis") or {}

    dead = analysis.get("dead_products", []) or []
    hot = analysis.get("hot_products", []) or []
    prices = store.get("prices", {}) or {}
    stock = store.get("stock", {}) or {}

    # weekly revenue, and per-product weekly units, from the saved history
    weekly_sales = 0
    per_product_weekly = {}
    csv_text = store.get("sales_csv")
    if csv_text:
        try:
            sales = parse_sales_csv(csv_text)
            monthly = monthly_revenue_from_sales(sales, prices)
            if monthly:
                months = sorted(monthly.keys())
                weekly_sales = int(round(monthly[months[-1]] / 4.33))
                last = months[-1]
                for name, history in sales.items():
                    units = sum(
                        q for d, q in history
                        if d is not None and ("%04d-%02d" % (d.year, d.month)) == last
                    )
                    per_product_weekly[name] = int(round(units / 4.33))
        except Exception:
            weekly_sales = 0
            per_product_weekly = {}

    # one product list the table can render directly
    products = []
    for p in hot:
        name = p.get("name")
        products.append({
            "name": name,
            "stock": p.get("stock", stock.get(name, 0)),
            "weekly_sales": p.get("weekly_sales", 0),
            "price": prices.get(name),
            "status": "hot",
            "action": "הגדל מלאי" if p.get("order_quantity", 0) > 0 else None,
            "order_quantity": p.get("order_quantity", 0),
            "days_until_empty": p.get("days_until_empty"),
        })
    for p in dead:
        name = p.get("name")
        rec_price = p.get("recommended_price")
        products.append({
            "name": name,
            "stock": p.get("stock", stock.get(name, 0)),
            "weekly_sales": 0,
            "price": p.get("current_price", prices.get(name)),
            "status": "dead",
            "action": ("הורד ל-₪" + str(rec_price)) if rec_price else "שקול הנחה",
            "recommended_price": rec_price,
            "days_no_sale": p.get("days_no_sale"),
        })

    # anything the store stocks that the analysis did not flag
    flagged = {p["name"] for p in products}
    for name, qty in stock.items():
        if name in flagged:
            continue
        wk = per_product_weekly.get(name, 0)
        # stock sitting with no sales is not "stable" — surface it
        slow = wk == 0 and qty > 0
        products.append({
            "name": name,
            "stock": qty,
            "weekly_sales": wk,
            "price": prices.get(name),
            "status": "slow" if slow else "stable",
            "action": "בדוק מלאי" if slow else None,
        })

    alerts = len(dead) + len([p for p in hot if p.get("order_quantity", 0) > 0])

    return jsonify({
        "store_id": store_id,
        "store_name": store.get("name"),
        "weekly_sales": weekly_sales,
        "product_count": len(products),
        "alerts": alerts,
        "potential_savings": analysis.get("total_potential_savings", 0),
        "summary_he": analysis.get("summary_he", ""),
        "dead_products": dead,
        "hot_products": hot,
        "products": products,
        "has_analysis": latest is not None,
    })

# ===========================================
# מבנה אחיד (BKMVDATA) — התקן שכל מערכת בישראל חייבת לייצא
# מפרט: רשות המסים, הוראה 131
# ===========================================

# (field, start_1indexed, length)
BKMV_C100 = [
    ("doc_type", 23, 3), ("doc_number", 26, 20), ("issue_date", 46, 8),
    ("customer_name", 58, 50), ("total_before_discount", 288, 15),
    ("vat", 333, 15), ("total_with_vat", 348, 15),
    ("cancelled", 400, 1), ("doc_date", 401, 8),
]
BKMV_D110 = [
    ("doc_type", 23, 3), ("doc_number", 26, 20), ("line_number", 46, 4),
    ("sku", 74, 20), ("description", 94, 30), ("unit", 204, 20),
    ("quantity", 224, 17), ("unit_price", 241, 15),
    ("line_discount", 256, 15), ("line_total", 271, 15),
    ("doc_date", 297, 8),
]
BKMV_M100 = [
    ("universal_id", 23, 20), ("supplier_sku", 43, 20), ("sku", 63, 20),
    ("name", 83, 50), ("unit", 173, 20),
    ("opening_balance", 193, 12), ("receipts", 205, 12),
    ("disbursements", 217, 12), ("cost", 229, 10),
]
BKMV_Z900 = [("total_records", 1155 - 1000, 15)]  # position resolved below

# document types that represent a sale to a customer.
# Israeli spec groups these in the 300/400 ranges; tune per real file.
BKMV_SALES_DOCS = {"305", "320", "330", "300", "400"}
BKMV_CREDIT_DOCS = {"330"}  # credit notes reverse a sale


def _bkmv_decode(raw):
    """BKMVDATA is single-byte Hebrew: ISO-8859-8 on Windows, CP862 on DOS."""
    if isinstance(raw, str):
        return raw.encode("iso8859-8", errors="replace")
    return raw


def _bkmv_field(line_bytes, start, length):
    """start is 1-indexed per the spec."""
    chunk = line_bytes[start - 1:start - 1 + length]
    for enc in ("iso8859-8", "cp862", "utf-8"):
        try:
            return chunk.decode(enc).strip().strip("!")
        except (UnicodeDecodeError, LookupError):
            continue
    return chunk.decode("latin-1", errors="replace").strip()


def _bkmv_num(s, decimals=2):
    """
    Spec writes amounts as X9(12)V99 — implicit decimal point, optional sign.
    '000000000012550' with 2 decimals -> 125.50
    """
    if not s:
        return 0.0
    s = s.strip().replace("!", "")
    if not s:
        return 0.0
    sign = 1.0
    if s[0] in "+-":
        sign = -1.0 if s[0] == "-" else 1.0
        s = s[1:]
    s = s.strip() or "0"
    if not s.lstrip("0").isdigit() and s.lstrip("0") != "":
        digits = "".join(c for c in s if c.isdigit())
        if not digits:
            return 0.0
        s = digits
    try:
        return sign * (int(s or "0") / (10 ** decimals))
    except ValueError:
        return 0.0


def _bkmv_date(s):
    from datetime import datetime
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return None


def parse_bkmv(content):
    """
    Reads a BKMVDATA file and returns its documents, line items and
    inventory records, plus a self-check against the Z900 record count.
    """
    data = _bkmv_decode(content)
    lines = [l for l in data.replace(b"\r\n", b"\n").split(b"\n") if l.strip()]

    docs, items, stock_rows = [], [], []
    doc_type_counts = {}
    declared_total = None
    seen = 0

    for lb in lines:
        code = _bkmv_field(lb, 1, 4)
        # vendor docs spell record codes C100/D110/M100/Z900; the spec as
        # extracted from its Hebrew PDF reads 100C/110D. RTL extraction often
        # reverses Latin+digit runs, so accept both spellings.
        if len(code) == 4 and code[:3].isdigit() and code[3].isalpha():
            code = code[3] + code[:3]
        seen += 1
        if code == "C100":
            rec = {k: _bkmv_field(lb, s, n) for k, s, n in BKMV_C100}
            rec["total_with_vat"] = _bkmv_num(rec["total_with_vat"])
            rec["vat"] = _bkmv_num(rec["vat"])
            rec["date"] = _bkmv_date(rec["doc_date"]) or _bkmv_date(rec["issue_date"])
            docs.append(rec)
            dt = rec["doc_type"]
            doc_type_counts[dt] = doc_type_counts.get(dt, 0) + 1
        elif code == "D110":
            rec = {k: _bkmv_field(lb, s, n) for k, s, n in BKMV_D110}
            rec["quantity"] = _bkmv_num(rec["quantity"], 4)
            rec["unit_price"] = _bkmv_num(rec["unit_price"])
            rec["line_discount"] = _bkmv_num(rec["line_discount"])
            rec["line_total"] = _bkmv_num(rec["line_total"])
            rec["date"] = _bkmv_date(rec["doc_date"])
            items.append(rec)
        elif code == "M100":
            rec = {k: _bkmv_field(lb, s, n) for k, s, n in BKMV_M100}
            rec["opening_balance"] = _bkmv_num(rec["opening_balance"])
            rec["receipts"] = _bkmv_num(rec["receipts"])
            rec["disbursements"] = _bkmv_num(rec["disbursements"])
            rec["cost"] = _bkmv_num(rec["cost"])
            rec["on_hand"] = rec["opening_balance"] + rec["receipts"] - rec["disbursements"]
            stock_rows.append(rec)
        elif code == "Z900":
            declared_total = _bkmv_num(_bkmv_field(lb, 20, 15), 0)

    return {
        "documents": docs,
        "lines": items,
        "inventory": stock_rows,
        "doc_type_counts": doc_type_counts,
        "records_seen": seen,
        "records_declared": int(declared_total) if declared_total else None,
        "count_matches": (declared_total is None) or int(declared_total) == seen,
    }


def bkmv_to_store_data(parsed, sales_doc_types=None):
    """
    Turns a parsed BKMVDATA file into the three things the rest of the
    system already speaks: a sales CSV, a stock map and a price map.
    """
    allowed = sales_doc_types if sales_doc_types is not None else BKMV_SALES_DOCS

    rows = ["date,product,qty"]
    prices, seen_dates = {}, 0
    for ln in parsed.get("lines", []):
        if allowed and ln.get("doc_type") not in allowed:
            continue
        name = (ln.get("description") or ln.get("sku") or "").strip()
        if not name:
            continue
        d = ln.get("date")
        if d is None:
            continue
        qty = ln.get("quantity", 0)
        if ln.get("doc_type") in BKMV_CREDIT_DOCS:
            qty = -abs(qty)
        if qty == 0:
            continue
        rows.append("%04d-%02d-%02d,%s,%s" % (d.year, d.month, d.day, name, qty))
        seen_dates += 1
        up = ln.get("unit_price", 0)
        if up > 0 and name not in prices:
            prices[name] = round(up, 2)

    stock = {}
    for it in parsed.get("inventory", []):
        name = (it.get("name") or it.get("sku") or "").strip()
        if not name:
            continue
        stock[name] = int(round(it.get("on_hand", 0)))
        if it.get("cost", 0) > 0 and name not in prices:
            prices[name] = round(it["cost"], 2)

    return {
        "sales_csv": "\n".join(rows),
        "stock": stock,
        "prices": prices,
        "sale_lines_used": seen_dates,
    }

@app.route("/import/bkmv/<store_id>", methods=["POST"])
def import_bkmv(store_id):
    """
    Loads a Tax Authority uniform-format file (BKMVDATA) into a store.

    Accepts either a multipart upload (field name "file") or the raw file
    as the request body. Pass ?preview=1 to inspect a file without saving.
    """
    # Touching request.files consumes the stream, so decide by content type
    # first — otherwise a raw-body upload arrives empty.
    raw = None
    ctype = (request.content_type or "").lower()
    if ctype.startswith("multipart/form-data"):
        f = request.files.get("file") or next(iter(request.files.values()), None)
        if f:
            raw = f.read()
    else:
        raw = request.get_data(cache=False) or b""
        if not raw and request.form:
            # some clients send the body form-encoded; take the first value
            raw = next(iter(request.form.values()), "").encode("utf-8", "replace")
    if not raw:
        return jsonify({"error": "no file received"}), 400

    try:
        parsed = parse_bkmv(raw)
    except Exception as e:
        return jsonify({"error": "could not read file: " + str(e)}), 400

    if not parsed["documents"] and not parsed["lines"] and not parsed["inventory"]:
        return jsonify({
            "error": "no BKMVDATA records found — is this the right file?",
            "records_seen": parsed["records_seen"],
        }), 400

    types_param = request.args.get("doc_types")
    allowed = set(t.strip() for t in types_param.split(",")) if types_param else None
    converted = bkmv_to_store_data(parsed, allowed)

    summary = {
        "store_id": store_id,
        "records_seen": parsed["records_seen"],
        "records_declared": parsed["records_declared"],
        "count_matches": parsed["count_matches"],
        "documents": len(parsed["documents"]),
        "line_items": len(parsed["lines"]),
        "inventory_items": len(parsed["inventory"]),
        "doc_type_counts": parsed["doc_type_counts"],
        "sale_lines_used": converted["sale_lines_used"],
        "products_with_stock": len(converted["stock"]),
        "products_with_price": len(converted["prices"]),
    }

    if request.args.get("preview"):
        summary["preview"] = True
        summary["sample_csv"] = "\n".join(converted["sales_csv"].split("\n")[:11])
        return jsonify(summary)

    if converted["sale_lines_used"] == 0 and not converted["stock"]:
        summary["error"] = ("file parsed but held no usable sales or stock — "
                            "check doc_type_counts and pass ?doc_types=")
        return jsonify(summary), 422

    db = load_db()
    store = db.setdefault("stores", {}).setdefault(store_id, {
        "name": request.args.get("name") or store_id, "phone": "", "active": True,
    })
    store["sales_csv"] = converted["sales_csv"]
    if converted["stock"]:
        store["stock"] = converted["stock"]
    if converted["prices"]:
        merged = dict(store.get("prices") or {})
        merged.update(converted["prices"])
        store["prices"] = merged
    store["data_source"] = "bkmv"
    store["imported_at"] = datetime.now().isoformat()
    analysis = _save_rule_analysis(db, store_id)
    save_db(db)
    summary["analysis"] = {
        "dead": len((analysis or {}).get("dead_products", [])),
        "hot": len((analysis or {}).get("hot_products", [])),
        "summary_he": (analysis or {}).get("summary_he"),
    }

    summary["saved"] = True
    summary["store_name"] = store.get("name")
    return jsonify(summary)

# ===========================================
# Trends — computed from the store's own history
# ===========================================

HE_DAYS = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"]


def _he_dow(d):
    """Python: Mon=0..Sun=6. Israeli week starts Sunday."""
    return (d.weekday() + 1) % 7


@app.route("/trends/<store_id>", methods=["GET"])
def trends(store_id):
    """
    Weekly revenue, day-of-week demand, and which products are moving.
    All of it from the store's own sales — no external service.
    """
    db = load_db()
    store = db.get("stores", {}).get(store_id)
    if not store:
        return jsonify({"error": "store not found"}), 404
    csv_text = store.get("sales_csv")
    if not csv_text:
        return jsonify({"error": "no sales data"}), 404

    prices = store.get("prices", {}) or {}
    sales = parse_sales_csv(csv_text)

    # without prices, revenue is all zeros and every chart goes flat —
    # fall back to units so a plain CSV still shows its shape
    priced = any((prices.get(n) or 0) for n in sales)
    metric = "revenue" if priced else "units"

    def rev(name, qty):
        if not priced:
            return float(qty)
        try:
            return qty * float(prices.get(name, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    # ---- weekly revenue, last 8 weeks ----
    from datetime import timedelta
    weekly = {}
    all_dates = []
    for name, hist in sales.items():
        for d, q in hist:
            if d is None:
                continue
            all_dates.append(d)
            monday = d - timedelta(days=d.weekday())
            key = monday.strftime("%Y-%m-%d")
            weekly[key] = weekly.get(key, 0.0) + rev(name, q)
    weeks = sorted(weekly.keys())[-8:]
    weekly_series = [{"week": w, "revenue": int(round(weekly[w]))} for w in weeks]

    # ---- demand by day of week ----
    dow_rev = [0.0] * 7
    dow_days = [set() for _ in range(7)]
    for name, hist in sales.items():
        for d, q in hist:
            if d is None:
                continue
            i = _he_dow(d)
            dow_rev[i] += rev(name, q)
            dow_days[i].add(d.date())
    dow = []
    for i in range(7):
        n = len(dow_days[i]) or 1
        dow.append({"day": HE_DAYS[i], "avg_revenue": int(round(dow_rev[i] / n)),
                    "observed_days": len(dow_days[i])}) 
    busiest = max(dow, key=lambda x: x["avg_revenue"]) if any(d["avg_revenue"] for d in dow) else None
    quietest = min((d for d in dow if d["observed_days"]),
                   key=lambda x: x["avg_revenue"], default=None)

    # ---- per-product: which day carries it, and is it rising ----
    movers = []
    for name, hist in sales.items():
        dated = sorted([(d, q) for d, q in hist if d is not None], key=lambda x: x[0])
        if len(dated) < 4:
            continue
        mid = len(dated) // 2
        first = sum(q for _, q in dated[:mid])
        second = sum(q for _, q in dated[mid:])
        change = ((second - first) / first * 100) if first else 0.0
        by_day = [0.0] * 7
        for d, q in dated:
            by_day[_he_dow(d)] += q
        peak = by_day.index(max(by_day))
        total = sum(by_day) or 1
        movers.append({
            "product": name,
            "change_pct": round(change, 1),
            "direction": "rising" if change > 15 else ("falling" if change < -15 else "stable"),
            "peak_day": HE_DAYS[peak],
            "peak_share_pct": int(round(by_day[peak] / total * 100)),
            "units": int(round(total)),
        })
    movers.sort(key=lambda m: abs(m["change_pct"]), reverse=True)

    # ---- one honest headline ----
    insight = None
    if busiest and quietest and busiest["avg_revenue"] > 0:
        ratio = busiest["avg_revenue"] / max(1, quietest["avg_revenue"])
        if ratio >= 1.5:
            insight = ("יום %s חזק פי %.1f מיום %s. כדאי להיערך במלאי ובכוח אדם."
                       % (busiest["day"], ratio, quietest["day"]))
    concentrated = [m for m in movers if m["peak_share_pct"] >= 35]
    if concentrated:
        top = concentrated[0]
        extra = ("%d%% מהמכירות של %s מרוכזות ביום %s."
                 % (top["peak_share_pct"], top["product"], top["peak_day"]))
        insight = (insight + " " + extra) if insight else extra

    return jsonify({
        "store_id": store_id,
        "store_name": store.get("name"),
        "weekly": weekly_series,
        "by_day_of_week": dow,
        "busiest_day": busiest,
        "quietest_day": quietest,
        "movers": movers[:10],
        "insight": insight,
        "based_on_days": len(set(d.date() for d in all_dates)),
        "source": "own_sales",
        "metric": metric,
    })

# ===========================================
# Bakery demo — day-of-week demand is the whole story
# ===========================================

# per-weekday multiplier, Sunday..Saturday (Israeli week)
BAKERY_ITEMS = [
    # name,                base, price, [Sun, Mon, Tue, Wed, Thu, Fri, Sat], drift
    ("חלה מתוקה",            18,  14, [0.2, 0.2, 0.2, 0.3, 1.6, 4.8, 0.1],  0.03),
    ("לחם כפרי מחמצת",       26,  18, [1.0, 1.0, 0.9, 1.0, 1.3, 1.6, 0.2],  0.02),
    ("קרואסון חמאה",         22,   9, [0.7, 0.6, 0.6, 0.7, 1.1, 1.9, 2.2],  0.05),
    ("בורקס גבינה",          34,   7, [1.2, 1.1, 1.0, 1.1, 1.2, 1.4, 0.3],  0.00),
    ("עוגת שמרים שוקולד",     9,  42, [0.4, 0.3, 0.3, 0.4, 1.2, 2.8, 0.4],  0.04),
    ("רוגלך (100 גר')",      28,  11, [1.0, 1.0, 1.0, 1.0, 1.2, 1.5, 0.6],  0.01),
    ("בגט צרפתי",            20,  10, [1.0, 1.0, 1.0, 1.0, 1.2, 1.7, 0.2], -0.01),
    ("עוגיות חמאה 250 גר'",   7,  24, [0.8, 0.8, 0.7, 0.7, 0.8, 0.9, 0.3], -0.09),
]


def build_bakery_csv(weeks=8):
    """Daily sales with real weekday shape, so trends have something to find."""
    from datetime import datetime, timedelta
    rows = ["date,product,qty"]
    start = datetime(2026, 7, 6)  # a Monday
    for w in range(weeks):
        for offset in range(7):
            day = start + timedelta(days=w * 7 + offset)
            dow = (day.weekday() + 1) % 7  # Sunday = 0
            for name, base, _price, shape, drift in BAKERY_ITEMS:
                qty = base * shape[dow] * (1 + drift * w)
                # a little texture so the data isn't suspiciously smooth
                qty *= 1 + (((w * 7 + offset + len(name)) % 5) - 2) * 0.04
                qty = int(round(qty))
                if qty > 0:
                    rows.append("%s,%s,%d" % (day.strftime("%Y-%m-%d"), name, qty))
    return "\n".join(rows)


def build_bakery_demo(db):
    from datetime import datetime
    sid = "demo_bakery"
    stock = {
        "חלה מתוקה": 12, "לחם כפרי מחמצת": 30, "קרואסון חמאה": 18,
        "בורקס גבינה": 45, "עוגת שמרים שוקולד": 6, "רוגלך (100 גר')": 35,
        "בגט צרפתי": 22, "עוגיות חמאה 250 גר'": 40,
    }
    prices = {n: p for n, _b, p, _s, _d in BAKERY_ITEMS}

    db.setdefault("stores", {})[sid] = {
        "name": "מאפיית לחם הארץ",
        "phone": "0504567890",
        "active": True,
        "demo": True,
        "stock": stock,
        "prices": prices,
        "sales_csv": build_bakery_csv(8),
    }
    db.setdefault("recommendations", [])
    db["recommendations"] = [r for r in db["recommendations"] if r.get("store_id") != sid]
    db["recommendations"].append({
        "store_id": sid,
        "created": datetime.now().isoformat(),
        "analysis": {
            "dead_products": [
                {"name": "עוגיות חמאה 250 גר'", "stock": 40, "days_no_sale": 0,
                 "current_price": 24, "recommended_price": 17,
                 "reason": "מכירות ירדו 40% בחודשיים — המלאי מצטבר"},
            ],
            "hot_products": [
                {"name": "חלה מתוקה", "weekly_sales": 152, "stock": 12,
                 "days_until_empty": 1, "order_quantity": 180, "supplier": "קמח שלמה"},
                {"name": "עוגת שמרים שוקולד", "weekly_sales": 48, "stock": 6,
                 "days_until_empty": 1, "order_quantity": 60, "supplier": "קמח שלמה"},
            ],
            "total_potential_savings": 960,
            "summary_he": "חלה ועוגת שמרים נגמרות לפני שישי. עוגיות החמאה בירידה מתמשכת.",
        },
    })
    return {"store_id": sid, "name": "מאפיית לחם הארץ"}

# ===========================================
# Real-store onboarding: CSV import + rule-based analysis
# ===========================================

def _decode_upload(raw):
    """Israeli Excel saves CSV as Windows-1255 more often than UTF-8."""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8-sig", "cp1255", "iso8859-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_upload():
    """Same stream rule as the BKMV route: check content type before files."""
    ctype = (request.content_type or "").lower()
    if ctype.startswith("multipart/form-data"):
        f = request.files.get("file") or next(iter(request.files.values()), None)
        return f.read() if f else b""
    return request.get_data(cache=False) or b""


def build_rule_analysis(store):
    """
    A recommendation built from plain rules, so a newly connected store gets
    a working overview without an AI key. The Claude analysis can replace it.
      dead: stock on hand and no sale for 30+ days of the data's own span
      hot:  forecast says it runs out within lead time + 2 days
    """
    csv_text = store.get("sales_csv") or ""
    stock = store.get("stock") or {}
    prices = store.get("prices") or {}
    sales = parse_sales_csv(csv_text)

    dates = [d for h in sales.values() for d, _ in h if d is not None]
    if not dates:
        return None
    last_day = max(dates)

    dead = []
    names = set(sales) | set(stock)
    for name in names:
        on_hand = stock.get(name, 0) or 0
        if on_hand <= 0:
            continue
        sold = [d for d, q in sales.get(name, []) if d is not None and q > 0]
        days = (last_day - max(sold)).days if sold else 999
        if days < 30:
            continue
        price = prices.get(name)
        rec = int(round(price * 0.75)) if price else None
        dead.append({
            "name": name, "stock": on_hand, "days_no_sale": days if days < 999 else None,
            "current_price": price, "recommended_price": rec,
            "reason": ("לא נמכר %d ימים" % days) if days < 999 else "אין מכירות בנתונים",
        })

    hot = []
    if stock:
        for f in forecast_all(csv_text, stock, 3):
            if f["product_name"] not in stock:
                continue            # no stock figure — can't judge urgency
            if f["reorder_now"] and f["order_quantity"] > 0:
                hot.append({
                    "name": f["product_name"],
                    "weekly_sales": int(round(f["adjusted_daily_rate"] * 7)),
                    "stock": f["current_stock"],
                    "days_until_empty": f["days_until_empty"],
                    "order_quantity": f["order_quantity"],
                })

    freed = sum((d["stock"] or 0) * (d["recommended_price"] or 0) for d in dead)
    parts = []
    if hot:
        parts.append("%d מוצרים עומדים להיגמר" % len(hot))
    if dead:
        parts.append("%d מוצרים לא זזים 30+ יום" % len(dead))
    if not stock:
        parts.append("חסרים נתוני מלאי — העלה קובץ אחיד כדי לקבל התראות מחסור")
    summary = ". ".join(parts) + "." if parts else "לא נמצאו בעיות דחופות."

    return {
        "dead_products": sorted(dead, key=lambda x: -(x["days_no_sale"] or 999)),
        "hot_products": sorted(hot, key=lambda x: x["days_until_empty"]),
        "total_potential_savings": int(freed),
        "summary_he": summary,
        "source": "rules",
    }


def _save_rule_analysis(db, store_id):
    from datetime import datetime
    analysis = build_rule_analysis(db["stores"][store_id])
    if analysis is None:
        return None
    db.setdefault("recommendations", [])
    db["recommendations"] = [
        r for r in db["recommendations"]
        if not (r.get("store_id") == store_id and (r.get("analysis") or {}).get("source") == "rules")
    ]
    db["recommendations"].append({
        "store_id": store_id, "created": datetime.now().isoformat(), "analysis": analysis,
    })
    return analysis


@app.route("/import/csv/<store_id>", methods=["POST"])
def import_csv(store_id):
    """
    Loads a sales CSV (date, product, qty — Hebrew or English headers).
    ?preview=1 inspects without saving.
    """
    raw = _read_upload()
    if not raw:
        return jsonify({"error": "no file received"}), 400
    text = _decode_upload(raw)
    sales = parse_sales_csv(text)
    dated = sum(1 for h in sales.values() for d, _ in h if d is not None)
    if not sales or dated == 0:
        return jsonify({
            "error": "לא נמצאו שורות מכירה עם תאריך. נדרשות עמודות: תאריך, מוצר, כמות",
        }), 400

    dates = sorted(d for h in sales.values() for d, _ in h if d is not None)
    summary = {
        "store_id": store_id,
        "products": len(sales),
        "sale_rows": dated,
        "first_date": dates[0].strftime("%Y-%m-%d"),
        "last_date": dates[-1].strftime("%Y-%m-%d"),
    }
    if request.args.get("preview"):
        summary["preview"] = True
        return jsonify(summary)

    db = load_db()
    store = db.setdefault("stores", {}).setdefault(store_id, {
        "name": request.args.get("name") or store_id, "phone": "", "active": True,
    })
    store["sales_csv"] = text
    store["data_source"] = "csv"
    analysis = _save_rule_analysis(db, store_id)
    save_db(db)
    summary["saved"] = True
    summary["analysis"] = {
        "dead": len((analysis or {}).get("dead_products", [])),
        "hot": len((analysis or {}).get("hot_products", [])),
        "summary_he": (analysis or {}).get("summary_he"),
    }
    return jsonify(summary)


@app.route("/stores", methods=["GET"])
def list_stores():
    db = load_db()
    out = []
    for sid, s in db.get("stores", {}).items():
        out.append({
            "store_id": sid,
            "name": s.get("name") or sid,
            "demo": bool(s.get("demo")),
            "has_data": bool(s.get("sales_csv")),
            "data_source": s.get("data_source") or ("demo" if s.get("demo") else None),
        })
    out.sort(key=lambda x: (x["demo"], x["name"]))
    return jsonify({"stores": out, "count": len(out)})


if __name__ == "__main__":
    print("=" * 50)
    print("🚀 Genius — מערכת מלאה")
    print("=" * 50)
    db = load_db()
    print(f"חנויות: {len(db['stores'])} | ספקים: {len(db['suppliers'])}")
    print(f"Claude API: {'✓' if ANTHROPIC_KEY else '✗ חסר'}")
    print(f"Twilio: {'✓' if TWILIO_SID else '✗ חסר (מצב דמו)'}")
    print(f"Stripe: {'✓' if STRIPE_KEY else '✗ חסר'}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=8080)
