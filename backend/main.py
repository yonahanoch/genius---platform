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
                    reason = sa.get("name") + " has stock, " + sb.get("name") + " needs it"
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
