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
import re
import json
import csv
import io
import time
import hmac
import shutil
import secrets
import hashlib
import tempfile
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, redirect, g
from flask_cors import CORS
import anthropic

try:
    import fcntl  # POSIX file lock, so several worker processes can't interleave writes
except ImportError:  # pragma: no cover - Windows dev machines
    fcntl = None

# ── אתחול ──
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # uploads above 5MB are refused

# Only the real website (and local dev) may call the API from a browser.
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS",
    "https://yonahanoch.github.io,http://localhost:8000,http://127.0.0.1:8000",
).split(",") if o.strip()]
CORS(app, origins=CORS_ORIGINS,
     allow_headers=["Content-Type", "X-Store-Token", "X-Admin-Token"])

ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TWILIO_SID     = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM    = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
STRIPE_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_BASIC = os.environ.get("STRIPE_PRICE_BASIC", "")  # price_xxx של ₪299
STRIPE_PRICE_PRO   = os.environ.get("STRIPE_PRICE_PRO", "")    # price_xxx של ₪599
PUBLIC_URL     = os.environ.get("PUBLIC_URL", "").rstrip("/")  # used to verify Twilio signatures

DATA_DIR = os.path.abspath(os.environ.get("GENIUS_DATA_DIR", os.getcwd()))
DB_FILE = os.path.join(DATA_DIR, "genius_db.json")
DB_BACKUP = DB_FILE + ".bak"
DB_LOCKFILE = DB_FILE + ".lock"
ADMIN_TOKEN_FILE = os.path.join(DATA_DIR, ".admin_token")


def _load_admin_token():
    """
    ADMIN_TOKEN from the environment, else one random token kept on disk and
    shared by every worker process. If it can't be stored, admin is disabled
    rather than each process inventing its own.
    """
    env = os.environ.get("ADMIN_TOKEN", "").strip()
    if env:
        return env
    for _ in range(3):
        try:
            with open(ADMIN_TOKEN_FILE, "r", encoding="utf-8") as f:
                tok = f.read().strip()
            if tok:
                return tok
        except OSError:
            pass
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            fd = os.open(ADMIN_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            tok = secrets.token_urlsafe(24)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(tok)
            return tok
        except FileExistsError:
            time.sleep(0.05)     # another worker is writing it — read again
        except OSError as e:
            print(f"! admin token file not writable ({e}) — set ADMIN_TOKEN; admin disabled")
            return ""
    return ""


ADMIN_TOKEN = _load_admin_token()

# ═══════════════════════════════════════
# מסד נתונים (JSON) — כתיבה בטוחה: נעילה + כתיבה אטומית + גיבוי
# ═══════════════════════════════════════
# Every request that may write holds DB_LOCK from start to finish, so two
# requests can never load the same copy and overwrite each other. Writes go to
# a temp file that replaces the real one in a single step, so a crash mid-write
# can't leave a half-written database. The previous version is kept as .bak.

DB_LOCK = threading.RLock()
_lock_state = threading.local()


class _DbLock:
    """Thread lock + (where available) an OS file lock, re-entrant per thread."""

    def __enter__(self):
        DB_LOCK.acquire()
        depth = getattr(_lock_state, "depth", 0)
        if depth == 0 and fcntl is not None:
            os.makedirs(DATA_DIR, exist_ok=True)
            _lock_state.fh = open(DB_LOCKFILE, "a")
            fcntl.flock(_lock_state.fh, fcntl.LOCK_EX)
        _lock_state.depth = depth + 1
        return self

    def __exit__(self, *exc):
        _lock_state.depth -= 1
        if _lock_state.depth == 0 and fcntl is not None and getattr(_lock_state, "fh", None):
            fcntl.flock(_lock_state.fh, fcntl.LOCK_UN)
            _lock_state.fh.close()
            _lock_state.fh = None
        DB_LOCK.release()
        return False


db_lock = _DbLock


def _empty_db():
    return {"stores": {}, "suppliers": {}, "orders": [], "recommendations": []}


def load_db():
    for path in (DB_FILE, DB_BACKUP):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                db = json.load(f)
            for k, v in _empty_db().items():
                db.setdefault(k, v)
            if path == DB_BACKUP:
                print("! main database unreadable — loaded the backup copy")
            return db
        except (ValueError, OSError) as e:
            print(f"! could not read {path}: {e}")
    return _empty_db()


def save_db(db):
    with db_lock():
        os.makedirs(DATA_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".genius_db.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(db, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(DB_FILE):
                shutil.copy2(DB_FILE, DB_BACKUP)
            os.replace(tmp, DB_FILE)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise


# The lock is taken only around load -> modify -> save, never while a request
# body is still arriving or while an outside service (Twilio, Stripe, Claude)
# is being called — so one slow client can't stall everyone else. Readers
# don't need it: os.replace swaps the file in one step.


# ═══════════════════════════════════════
# הרשאות — כל חנות מקבלת מפתח גישה סודי
# ═══════════════════════════════════════
# A store's data can be read or changed only with its own token (sent in the
# X-Store-Token header) or the admin token. Demo stores are readable by
# anyone but can't be changed. Only a hash of each token is stored.

STORE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,40}$")


def _hash_token(tok):
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


def new_store_token(store):
    tok = secrets.token_urlsafe(24)
    store["token_hash"] = _hash_token(tok)
    return tok


def is_admin():
    tok = request.headers.get("X-Admin-Token", "")
    return bool(ADMIN_TOKEN) and bool(tok) and hmac.compare_digest(tok, ADMIN_TOKEN)


def has_store_access(store):
    if is_admin():
        return True
    want = (store or {}).get("token_hash")
    tok = request.headers.get("X-Store-Token", "")
    return bool(want) and bool(tok) and hmac.compare_digest(_hash_token(tok), want)


def can_read(store):
    return bool(store) and (bool(store.get("demo")) or has_store_access(store))


def can_write(store):
    """Demo stores are read-only for everyone except the admin."""
    if not store:
        return False
    if store.get("demo"):
        return is_admin()
    return has_store_access(store)


def is_entitled(store):
    """Trial still running, or a paid plan confirmed by Stripe."""
    if not store:
        return False
    if store.get("demo") or store.get("plan") == "paid":
        return store.get("active", True) is not False or store.get("demo")
    if store.get("plan") in (None, "trial"):
        ends = store.get("trial_ends")
        if not ends:
            return True
        try:
            return datetime.fromisoformat(ends) > datetime.now()
        except ValueError:
            return False
    return False


def deny(msg="אין הרשאה לחנות הזו", code=403):
    return jsonify({"error": msg}), code


def require_admin():
    if not is_admin():
        return deny("נדרשת הרשאת מנהל", 401)
    return None


def get_readable_store(db, store_id):
    """(store, None) when the caller may read it, else (None, error response)."""
    store = db.get("stores", {}).get(store_id)
    # "no such store" and "not yours" look the same from outside, so store ids
    # (phone numbers) can't be probed to learn who is a customer
    if not store:
        return None, (deny() if not is_admin() else (jsonify({"error": "store not found"}), 404))
    if not can_read(store):
        return None, deny()
    return store, None


# ── simple per-IP rate limit (in memory) ──
NEW_STORE_LIMIT = int(os.environ.get("NEW_STORE_LIMIT_PER_HOUR", "20"))
_RATE = {}
_RATE_LOCK = threading.Lock()


# Behind a hosting proxy (Replit) every request arrives from the proxy, and the
# real client address is the entry the proxy APPENDS to X-Forwarded-For. A
# client can put anything at the front of that header, so only the entry added
# by our own proxy (PROXY_HOPS from the right) is trusted.
PROXY_HOPS = int(os.environ.get("PROXY_HOPS", "1"))


def client_ip():
    if PROXY_HOPS > 0:
        parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        if len(parts) >= PROXY_HOPS:
            return parts[-PROXY_HOPS]
    return request.remote_addr or "?"


def rate_limited(bucket, limit, per_seconds, per_ip=True, key=None):
    now = time.time()
    key = (bucket, key if key is not None else (client_ip() if per_ip else "*"))
    with _RATE_LOCK:
        hits = [t for t in _RATE.get(key, []) if now - t < per_seconds]
        if len(hits) >= limit:
            _RATE[key] = hits
            return True
        hits.append(now)
        _RATE[key] = hits
    return False


@app.errorhandler(413)
def _too_big(e):
    return jsonify({"error": "הקובץ גדול מדי (עד 5MB)"}), 413


def normalize_phone(phone):
    """Digits of an Israeli number without country code or leading 0."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if digits.startswith("972"):
        digits = digits[3:]
    return digits.lstrip("0")


def store_with_phone(db, phone, exclude=None):
    n = normalize_phone(phone)
    if not n:
        return None
    for sid, s in db.get("stores", {}).items():
        if sid != exclude and normalize_phone(s.get("phone")) == n:
            return sid
    return None


def is_demo_phone(phone):
    n = normalize_phone(phone)
    return any(normalize_phone(s.get("phone")) == n for s in _demo_specs())


def _demo_specs():
    specs = list(DEMO_STORES.values()) if "DEMO_STORES" in globals() else []
    specs.append({"phone": BAKERY_PHONE} if "BAKERY_PHONE" in globals() else {})
    return specs

# ═══════════════════════════════════════
# WhatsApp — עם טיפול נכון ב-templates
# ═══════════════════════════════════════

def send_whatsapp(phone: str, message: str) -> bool:
    """
    שולח WhatsApp דרך Twilio.
    חשוב: ללקוח חדש שלא שלח הודעה ב-24 שעות האחרונות,
    Twilio ישלח רק template מאושר. אחרי שהלקוח עונה — חלון חופשי.
    """
    if not phone:
        print("[SKIP] WhatsApp: no phone number on file")
        return False
    if not TWILIO_SID or is_demo_phone(phone):
        # demo stores carry made-up numbers — never text a real person by accident
        print(f"[DEMO MODE] WhatsApp ל-{phone}:\n{message}\n")
        return True
    
    try:
        from twilio.rest import Client
        from twilio.http.http_client import TwilioHttpClient
        client = Client(TWILIO_SID, TWILIO_TOKEN, http_client=TwilioHttpClient(timeout=10))
        
        if not phone.startswith("whatsapp:"):
            phone = f"whatsapp:+972{normalize_phone(phone)}"
        
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
  "stuck_value": 0,
  "summary_he": "סיכום קצר בעברית של 2 משפטים"
}}

כללים:
- מוצר מת: לא נמכר 30+ יום
- מוצר חם: מכירות גבוהות + מלאי שיגמר בתוך שבוע
- מחיר מומלץ: הורדה של 20-30% למוצר מת
- stuck_value: סכום (מלאי × מחיר נוכחי) של המוצרים המתים — כסף תקוע, לא "חיסכון"
- אל תמציא נתונים: אם אין נתון מלאי או מחיר, השתמש ב-null"""

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

def _num_or_none(v):
    try:
        if v is None or v == "":
            return None
        f = float(str(v).replace(",", "").replace("₪", "").strip())
        return int(f) if f.is_integer() else round(f, 2)
    except (TypeError, ValueError):
        return None


def clean_analysis(a):
    """Model output is untrusted: keep known fields, force numbers to numbers."""
    if not isinstance(a, dict):
        return {"dead_products": [], "hot_products": [], "summary_he": ""}
    num_dead = ("stock", "days_no_sale", "current_price", "recommended_price")
    num_hot = ("weekly_sales", "stock", "days_until_empty", "order_quantity")

    def rows(items, nums, texts):
        out = []
        for p in (items or [])[:50] if isinstance(items, list) else []:
            if not isinstance(p, dict) or not str(p.get("name") or "").strip():
                continue
            r = {"name": str(p["name"]).strip()[:120]}
            for k in nums:
                r[k] = _num_or_none(p.get(k))
            for k in texts:
                if p.get(k) is not None:
                    r[k] = str(p[k])[:200]
            out.append(r)
        return out
    return {
        "dead_products": rows(a.get("dead_products"), num_dead, ("reason",)),
        "hot_products": rows(a.get("hot_products"), num_hot, ("supplier",)),
        "stuck_value": _num_or_none(a.get("stuck_value")) or 0,
        "summary_he": str(a.get("summary_he") or "")[:600],
        "source": "ai",
    }


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
            days = p.get("days_no_sale")
            lines.append(f"{i}. {p.get('name')} — " +
                         (f"{days} ימים ללא מכירה" if days else "אין מכירות בנתונים"))
            if p.get("current_price") and p.get("recommended_price"):
                lines.append(f"   המלצה: הורד מ-₪{p['current_price']} ל-₪{p['recommended_price']}")
        lines.append("")
    
    hot = analysis.get("hot_products", [])
    if hot:
        lines.append("📈 מוצרים חמים — הזמן מלאי:")
        for i, p in enumerate(hot[:3], 1):
            lines.append(f"{i}. {p.get('name')} — נגמר בעוד {p.get('days_until_empty')} ימים")
            lines.append(f"   הזמן: {p.get('order_quantity')} יח'" + (f" מ{p['supplier']}" if p.get('supplier') else ""))
        lines.append("")
    
    stuck = analysis.get("stuck_value") or 0
    if stuck:
        lines.append(f"💰 כסף תקוע במלאי שלא זז: ₪{int(stuck):,}")
        lines.append("")
    
    lines.append("✅ לאישור כל ההמלצות: ענה 1")
    lines.append("📞 להזמנה אוטומטית מהספקים: ענה 2")
    lines.append("❌ לדחייה: ענה 3")
    
    return "\n".join(lines)

# ═══════════════════════════════════════
# הזמנות אוטומטיות לספקים
# ═══════════════════════════════════════

def supplier_order_text(supplier: dict, store: dict, products: list) -> str:
    """Purchase request text for a supplier (sent by the caller)."""
    products_text = "\n".join([
        f"• {p.get('name')} — {p.get('order_quantity')} יחידות"
        for p in products
    ])
    return f"""📦 הזמנה חדשה — Genius

שלום {supplier['name']},

חנות "{store.get('name')}" מעוניינת להזמין:

{products_text}

לאישור ההזמנה ומשלוח הצעת מחיר:
ענה למספר זה או התקשר ל-{store.get('phone')}

— נשלח אוטומטית ע"י Genius"""


def send_order_to_supplier(supplier: dict, store: dict, products: list) -> bool:
    return send_whatsapp(supplier.get("phone"), supplier_order_text(supplier, store, products))

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
ONBOARD_DAILY_CAP = int(os.environ.get("ONBOARD_DAILY_CAP", "100"))

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
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:80]
    phone = str(data.get("phone") or "").strip()
    if not name or not phone:
        return jsonify({"error": "חסרים שם וטלפון"}), 400
    national = normalize_phone(phone)
    if not (8 <= len(national) <= 10):
        return jsonify({"error": "מספר טלפון לא תקין"}), 400
    if rate_limited("onboard", 10, 3600):
        return jsonify({"error": "יותר מדי ניסיונות — נסה שוב מאוחר יותר"}), 429

    store_id = "0" + national
    attach_to = str(data.get("store_id") or "")
    with db_lock():
        db = load_db()
        # a phone belongs to one store, and an existing store is never overwritten
        if store_id in db["stores"] or store_with_phone(db, phone):
            return jsonify({"error": "כבר קיימת חנות עם מספר הטלפון הזה"}), 409
        existing = db["stores"].get(attach_to) if attach_to else None
        if existing is not None:
            # registering from a store the caller already owns (e.g. created by
            # an upload): add the phone to it instead of making a second store
            if not can_write(existing) or existing.get("demo"):
                return deny()
            if normalize_phone(existing.get("phone")):
                return jsonify({"error": "לחנות הזו כבר רשום טלפון"}), 409
            existing["phone"] = phone
            existing["name"] = existing.get("name") or name
            store_id, token, skipped = attach_to, None, []
        else:
            token, skipped = _create_onboarded_store(db, store_id, name, phone, data)
        save_db(db)

    welcome = f"""🎉 ברוכים הבאים ל-Genius!

שלום {name},

החנות שלך נרשמה.
✓ 30 ימי ניסיון חינם
✓ השלב הבא: העלה קובץ מכירות באתר (מסך "חיבור נתונים") — הניתוח מוכן מיד

לשאלות — פשוט ענה להודעה זו."""
    # a flood of sign-ups from many addresses must not turn into a flood of
    # messages to strangers: past the daily cap, sign-up works but stays silent
    if not rate_limited("welcome_all", ONBOARD_DAILY_CAP, 86400, per_ip=False):
        send_whatsapp(phone, welcome)

    out = {"success": True, "store_id": store_id, "skipped_suppliers": skipped,
           "attached": token is None}
    if token:
        out["store_token"] = token
        out["note"] = "שמור את store_token — הוא המפתח לנתוני החנות"
    return jsonify(out)


def _create_onboarded_store(db, store_id, name, phone, data):
    store = {
        "name": name,
        "phone": phone,
        "system": str(data.get("system") or "other")[:40],
        "plan": "trial",  # paid only after Stripe confirms; the client can't choose
        "requested_plan": "pro" if data.get("plan") == "pro" else "basic",
        "trial_ends": (datetime.now() + timedelta(days=30)).isoformat(),
        "joined": datetime.now().isoformat(),
        "active": True,
        "stripe_customer_id": None
    }
    token = new_store_token(store)
    db["stores"][store_id] = store

    skipped = []
    for sup in data.get("suppliers") or []:
        if not isinstance(sup, dict):
            continue
        sup_phone = normalize_phone(sup.get("phone"))
        sup_name = str(sup.get("name") or "").strip()
        if not sup_name or len(sup_phone) < 8:
            skipped.append(sup_name or "?")
            continue
        sup_id = "0" + sup_phone
        existing = db["suppliers"].get(sup_id, {})
        db["suppliers"][sup_id] = {
            "name": sup_name[:80],
            "phone": sup.get("phone"),
            "products": str(sup.get("products") or "")[:200],
            "store_ids": sorted(set(existing.get("store_ids", []) + [store_id])),
        }

    return token, skipped

# ── ניתוח CSV ──

@app.route("/analyze", methods=["POST"])
def analyze():
    """ניתוח קובץ מכירות + שליחת WhatsApp + שמירת המלצות"""
    store_id = request.form.get("store_id")
    with db_lock():
        db = load_db()
        store = db["stores"].get(store_id)
        if not store:
            return deny()
        if not can_write(store):
            return deny()
        if not is_entitled(store):
            return jsonify({"error": "המנוי לא פעיל"}), 402
        store_suppliers = {
            k: v for k, v in db["suppliers"].items()
            if store_id in v.get("store_ids", [])
        }
    if "file" not in request.files:
        return jsonify({"error": "חסר קובץ"}), 400
    if not ANTHROPIC_KEY:
        return jsonify({"error": "ניתוח AI לא מוגדר — השתמש בחיבור נתונים לניתוח לפי כללים"}), 503
    if rate_limited("analyze", 5, 3600):
        return jsonify({"error": "יותר מדי בקשות — נסה שוב מאוחר יותר"}), 429

    csv_text = _decode_upload(request.files["file"].read())

    # the AI call runs without holding the database lock
    analysis = analyze_with_claude(csv_text, store, store_suppliers)
    if "error" in analysis:
        return jsonify(analysis), 502
    analysis = clean_analysis(analysis)

    rec_id = f"rec_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    with db_lock():
        db = load_db()
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
    # only Twilio may call this: check its signature on every request
    if not is_admin():
        if not TWILIO_TOKEN:
            return "WhatsApp not configured", 503
        try:
            from twilio.request_validator import RequestValidator
        except ImportError:
            return "twilio package missing", 503
        path = request.full_path.rstrip("?")
        urls = [PUBLIC_URL + path] if PUBLIC_URL else [request.url, request.url.replace("http://", "https://", 1)]
        sig = request.headers.get("X-Twilio-Signature", "")
        v = RequestValidator(TWILIO_TOKEN)
        if not any(v.validate(u, request.form.to_dict(), sig) for u in urls):
            return "invalid signature", 403

    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()
    sender = normalize_phone(from_number)
    if len(sender) < 8:
        return "OK", 200

    outbox = []          # (phone, text) — sent after the database is released
    with db_lock():
        db = load_db()
        # exact number match — a store without a phone never matches anyone
        store_id, store = None, None
        for sid, s in db["stores"].items():
            if s.get("demo"):
                continue
            own = normalize_phone(s.get("phone"))
            if own and own == sender:
                store_id, store = sid, s
                break
        if not store:
            return "OK", 200

        pending = [r for r in db["recommendations"]
                   if r.get("store_id") == store_id and r.get("status") == "pending"]
        last_rec = pending[-1] if pending else None

        if body == "1" and last_rec:
            last_rec["status"] = "approved"
            reply = "✅ ההמלצות סומנו כמאושרות.\nאת המחירים מעדכנים בקופה שלך — ואל תשכח להחליף את המדבקות 😊"
        elif body == "2" and last_rec:
            hot = (last_rec.get("analysis") or {}).get("hot_products", []) or []
            sent_count = 0
            for sup_id, sup in db["suppliers"].items():
                if store_id not in sup.get("store_ids", []):
                    continue
                sup_products = [p for p in hot if p.get("supplier") == sup["name"]]
                if sup_products:
                    outbox.append((sup.get("phone"), supplier_order_text(sup, store, sup_products)))
                    sent_count += 1
            last_rec["status"] = "ordered"
            if sent_count:
                reply = f"📦 נשלחות הזמנות ל-{sent_count} ספקים.\nהם יחזרו אליך ישירות עם הצעות מחיר."
            else:
                reply = "לא נמצאו ספקים מתאימים למוצרים האלה.\nהוסף ספקים באתר או ענה עם שם וטלפון של ספק."
        elif body == "3" and last_rec:
            last_rec["status"] = "rejected"
            reply = "הבנתי, ההמלצות נדחו.\nאם תכתוב למה — זה יעזור לנו לשפר את הכללים."
        else:
            reply = """קיבלתי ✓
1 — אישור המלצות
2 — הזמנה אוטומטית מספקים
3 — דחייה
או שאל אותי כל שאלה על החנות"""
        if last_rec and body in ("1", "2", "3"):
            save_db(db)
        store_phone = store.get("phone")

    for ph, text in outbox:
        send_whatsapp(ph, text)
    send_whatsapp(store_phone, reply)
    return "OK", 200

CHAT_SYSTEM_PROMPTS = {
    "store": "You are the Genius AI agent for a small retail store. Answer briefly and practically in Hebrew. Use ONLY the store data given below for any number or product claim; if the data doesn't cover the question, say so plainly instead of guessing. Keep replies under 4 sentences.",
    "admin": "You are the Genius AI agent for the network administrator. Answer briefly in Hebrew. You have no live network data in this conversation — give general guidance and say so if asked for figures. Keep replies under 4 sentences.",
    "supplier": "You are the Genius AI agent helping a supplier on the Genius network. Answer briefly in Hebrew. You have no live order data in this conversation — give general guidance and say so if asked for figures. Keep replies under 4 sentences.",
}

CHAT_DAILY_CAP = int(os.environ.get("CHAT_DAILY_CAP", "300"))       # all stores together
CHAT_STORE_DAILY_CAP = int(os.environ.get("CHAT_STORE_DAILY_CAP", "60"))   # one real store
CHAT_DEMO_DAILY_CAP = int(os.environ.get("CHAT_DEMO_DAILY_CAP", "60"))     # all demo visitors together
CHAT_TRIAL_DAILY_CAP = int(os.environ.get("CHAT_TRIAL_DAILY_CAP", "200"))  # all trial stores together
# Paid stores draw only on CHAT_DAILY_CAP and their own per-store cap, so trial
# or demo traffic (which can't be verified without an SMS code) never starves them.


def store_context_for_chat(store, analysis):
    """A compact, factual summary of the store's own data for the model."""
    lines = ["Store name: " + str(store.get("name"))]
    sales = parse_sales_csv(store.get("sales_csv") or "")
    dates = [d for h in sales.values() for d, _ in h if d is not None]
    if dates:
        lines.append("Sales data covers %s to %s, %d products."
                     % (min(dates).date(), max(dates).date(), len(sales)))
        last = max(dates)
        recent = {}
        for n, h in sales.items():
            u = sum(q for d, q in h if d is not None and (last - d).days < 28)
            if u:
                recent[n] = u
        top = sorted(recent.items(), key=lambda x: -x[1])[:10]
        if top:
            lines.append("Units sold in the last 4 weeks of data: " +
                         "; ".join("%s: %d" % (n, u) for n, u in top))
    else:
        lines.append("No sales data uploaded yet.")
    lines.append("Stock figures known: " + ("yes" if store.get("stock") else "no"))
    lines.append("Prices known: " + ("yes" if store.get("prices") else "no"))
    if analysis:
        if analysis.get("summary_he"):
            lines.append("Latest analysis: " + analysis["summary_he"])
        for p in (analysis.get("hot_products") or [])[:5]:
            lines.append("Running out: %s (stock %s, ~%s days left)"
                         % (p.get("name"), p.get("stock"), p.get("days_until_empty")))
        for p in (analysis.get("dead_products") or [])[:5]:
            lines.append("Not selling: %s (stock %s, %s days without a sale)"
                         % (p.get("name"), p.get("stock"), p.get("days_no_sale") or "no sales in data"))
    return "\n".join(lines)


def latest_analysis(db, store_id):
    latest = None
    for rec in db.get("recommendations", []):
        if rec.get("store_id") != store_id:
            continue
        if latest is None or rec.get("created", "") > latest.get("created", ""):
            latest = rec
    return (latest or {}).get("analysis") or {}, latest


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()[:2000]
    role = data.get("role", "store")
    if role not in CHAT_SYSTEM_PROMPTS:
        role = "store"
    store_id = str(data.get("store_id") or "")

    if not message:
        return jsonify({"error": "Missing message"}), 400

    # the caller must be allowed to see the store it asks about
    context, is_demo, is_paid = "", False, False
    db = load_db()
    if role == "store":
        store = db["stores"].get(store_id)
        if not store or not can_read(store):
            return deny()
        if not store.get("demo") and not is_entitled(store):
            return jsonify({"error": "המנוי לא פעיל — הצ'אט זמין בתקופת הניסיון ובמנוי"}), 402
        is_paid = store.get("plan") == "paid"
        is_demo = bool(store.get("demo"))
        context = store_context_for_chat(store, latest_analysis(db, store_id)[0])
    elif not is_admin():
        return deny("הצ'אט הזה זמין רק למנהל", 401)

    if not ANTHROPIC_KEY:
        return jsonify({"error": "הצ'אט לא מוגדר כרגע (חסר מפתח AI)"}), 503
    if rate_limited("chat", 20, 3600):
        return jsonify({"error": "הגעת למגבלת ההודעות לשעה — נסה שוב מאוחר יותר"}), 429
    if is_demo and rate_limited("chat_demo", CHAT_DEMO_DAILY_CAP, 86400, per_ip=False):
        return jsonify({"error": "צ'אט ההדגמה הגיע למגבלה היומית"}), 429
    if role == "store" and not is_demo and rate_limited("chat_store", CHAT_STORE_DAILY_CAP, 86400, key=store_id):
        return jsonify({"error": "הגעת למגבלת ההודעות היומית של החנות"}), 429
    if role == "store" and not is_demo and not is_paid and \
            rate_limited("chat_trial", CHAT_TRIAL_DAILY_CAP, 86400, per_ip=False):
        return jsonify({"error": "הצ'אט לתקופת הניסיון הגיע למגבלה היומית"}), 429
    if is_paid and rate_limited("chat_all", CHAT_DAILY_CAP, 86400, per_ip=False):
        return jsonify({"error": "הצ'אט הגיע למגבלה היומית"}), 429

    # only plain user/assistant turns from the client, trimmed
    history = []
    for h in (data.get("history") or [])[-10:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") \
                and isinstance(h.get("content"), str) and h["content"].strip():
            history.append({"role": h["role"], "content": h["content"][:2000]})
    while history and history[0]["role"] != "user":
        history.pop(0)

    system = CHAT_SYSTEM_PROMPTS[role]
    if context:
        system += "\n\n--- STORE DATA ---\n" + context

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=500,
            system=system,
            messages=history + [{"role": "user", "content": message}],
        )
        reply = next(block.text for block in response.content if block.type == "text")
        return jsonify({"reply": reply})
    except Exception as e:
        print(f"Chat error: {e}")
        return jsonify({"error": "הצ'אט לא זמין כרגע"}), 502

# ── Stripe — תשלומים (הדבר שפיספסנו לגמרי) ──

@app.route("/subscribe/<store_id>/<plan>")
def subscribe(store_id, plan):
    """יוצר לינק תשלום Stripe לחנות"""
    if plan not in ("basic", "pro"):
        return jsonify({"error": "תוכנית לא מוכרת"}), 400
    if not STRIPE_KEY:
        return jsonify({"error": "תשלומים עדיין לא מופעלים"}), 503
    price_id = STRIPE_PRICE_BASIC if plan == "basic" else STRIPE_PRICE_PRO
    if not price_id:
        return jsonify({"error": "מחיר התוכנית לא הוגדר ב-Stripe"}), 503

    import stripe
    stripe.api_key = STRIPE_KEY

    db = load_db()
    store = db["stores"].get(store_id)
    if not store or store.get("demo"):
        return jsonify({"error": "חנות לא נמצאה"}), 404

    # creating a checkout page changes nothing — the store becomes "paid" only
    # when Stripe's signed webhook confirms the payment
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=request.host_url + f"payment-success/{store_id}",
        cancel_url=request.host_url,
        client_reference_id=store_id,
        metadata={"store_id": store_id},
        subscription_data={"metadata": {"store_id": store_id}},
    )
    return redirect(session.url)


@app.route("/payment-success/<store_id>")
def payment_success(store_id):
    # Visiting this page proves nothing; the webhook below activates the plan.
    return "תודה! התשלום בבדיקה — המנוי יופעל ברגע ש-Stripe יאשר אותו (בדרך כלל תוך דקה)."


def _store_for_stripe_object(db, obj):
    meta = obj.get("metadata") or {}
    sid = meta.get("store_id") or obj.get("client_reference_id")
    if not sid:
        sub_meta = ((obj.get("subscription_details") or {}).get("metadata") or {})
        sid = sub_meta.get("store_id")
    if sid and sid in db["stores"]:
        return sid
    cust = obj.get("customer")
    if cust:
        for k, s in db["stores"].items():
            if s.get("stripe_customer_id") == cust:
                return k
    return None


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    """Stripe events, accepted only with a valid Stripe signature."""
    if not STRIPE_WEBHOOK_SECRET:
        return "Stripe webhook not configured", 503
    try:
        import stripe
        event = stripe.Webhook.construct_event(
            request.get_data(), request.headers.get("Stripe-Signature", ""),
            STRIPE_WEBHOOK_SECRET)
    except Exception as e:  # bad signature or malformed payload
        print(f"Stripe webhook rejected: {e}")
        return "invalid signature", 400

    etype = event["type"]
    obj = event["data"]["object"]
    obj = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
    message = None
    with db_lock():
        db = load_db()
        sid = _store_for_stripe_object(db, obj)
        if not sid:
            return "OK", 200
        store = db["stores"][sid]
        if etype == "checkout.session.completed" and obj.get("payment_status") in ("paid", "no_payment_required"):
            store["plan"] = "paid"
            store["active"] = True
            store["stripe_customer_id"] = obj.get("customer") or store.get("stripe_customer_id")
            save_db(db)
            message = "✅ התשלום התקבל! המנוי שלך פעיל.\nתודה שבחרת ב-Genius 💚"
        elif etype == "invoice.payment_failed":
            message = "⚠️ התשלום החודשי נכשל.\nעדכן את אמצעי התשלום כדי להמשיך לקבל המלצות."
        elif etype == "customer.subscription.deleted":
            store["plan"] = "canceled"
            store["active"] = False
            save_db(db)
        phone = store.get("phone")
    if message:
        send_whatsapp(phone, message)
    return "OK", 200

# ── דאשבורד ניהול ──

def _public_store(s):
    """Store record without secrets or bulky raw data."""
    return {k: v for k, v in s.items() if k not in ("token_hash", "sales_csv")}


@app.route("/admin")
def admin_dashboard():
    """דאשבורד פשוט למנהל — JSON לעכשיו"""
    err = require_admin()
    if err:
        return err
    db = load_db()
    return jsonify({
        "stores": {k: _public_store(s) for k, s in db["stores"].items()},
        "suppliers": db["suppliers"],
        "recommendations": db["recommendations"][-20:],  # 20 אחרונות
        "stats": {
            "total_stores": len(db["stores"]),
            "active_stores": len([s for s in db["stores"].values() if s.get("active")]),
            "trial_stores": len([s for s in db["stores"].values() if s.get("plan") == "trial"]),
            "paid_stores": len([s for s in db["stores"].values() if s.get("plan") == "paid"]),
        }
    })

@app.route("/admin/store/<store_id>/reset-token", methods=["POST"])
def admin_reset_token(store_id):
    """Issue a new access code (e.g. an owner lost theirs, or an older store
    has none). The admin checks the owner's identity before handing it over."""
    err = require_admin()
    if err:
        return err
    with db_lock():
        db = load_db()
        store = db["stores"].get(store_id)
        if not store or store.get("demo"):
            return jsonify({"error": "store not found"}), 404
        token = new_store_token(store)
        save_db(db)
    return jsonify({"store_id": store_id, "store_token": token})


@app.route("/admin/store/<store_id>", methods=["DELETE"])
def admin_delete_store(store_id):
    """Remove a store (e.g. someone registered a phone that isn't theirs)."""
    err = require_admin()
    if err:
        return err
    with db_lock():
        db = load_db()
        if store_id not in db["stores"] or db["stores"][store_id].get("demo"):
            return jsonify({"error": "store not found"}), 404
        db["stores"].pop(store_id)
        db["recommendations"] = [r for r in db["recommendations"] if r.get("store_id") != store_id]
        for sup in db["suppliers"].values():
            sup["store_ids"] = [i for i in sup.get("store_ids", []) if i != store_id]
        db["contact_consents"] = [c for c in db.get("contact_consents", [])
                                  if store_id not in (c.get("a"), c.get("b"))]
        save_db(db)
    return jsonify({"deleted": store_id})


# ===========================================
# Transfer opportunities between stores
# ===========================================
def _norm_name(name):
    """Loose product-name key: case, spaces, quotes and punctuation don't matter."""
    s = str(name or "").lower().strip()
    s = re.sub(r"[\"'׳״`.,()\-_/]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def network_members(db):
    """Stores that take part in network matching: demo stores, plus real
    stores that chose to join (network_opt_in)."""
    return {sid: s for sid, s in db.get("stores", {}).items()
            if s.get("demo") or (s.get("network_opt_in") and normalize_phone(s.get("phone")))}


def caller_store_ids(db):
    """Which real stores the caller has proven access to (via ?store_id=)."""
    if is_admin():
        return set(db.get("stores", {}))
    sid = request.args.get("store_id") or (request.get_json(silent=True) or {}).get("store_id")
    store = db.get("stores", {}).get(sid or "")
    return {sid} if store and has_store_access(store) else set()


def _visible(db, store_ids, mine):
    """An opportunity is shown when every store in it is a demo store, or when
    the caller owns one of the stores in it."""
    stores = db.get("stores", {})
    if all(stores.get(s, {}).get("demo") for s in store_ids):
        return True
    return bool(set(store_ids) & mine)


def find_transfer_opportunities(db):
    members = network_members(db)
    latest = {sid: latest_analysis(db, sid)[0] for sid in members}
    opps = []
    for a, sa in members.items():
        dead = latest[a].get("dead_products", []) or []
        for b, sb in members.items():
            if a == b:
                continue
            hot = {_norm_name(p.get("name")): p for p in (latest[b].get("hot_products") or [])}
            for dp in dead:
                if (dp.get("stock") or 0) <= 0:
                    continue
                hp = hot.get(_norm_name(dp.get("name")))
                if not hp:
                    continue
                qty = min(dp.get("stock") or 0, hp.get("order_quantity") or dp.get("stock") or 0)
                if qty <= 0:
                    continue
                opps.append({
                    "product_name": dp.get("name"),
                    "from_store_id": a, "from_store_name": sa.get("name"),
                    "to_store_id": b, "to_store_name": sb.get("name"),
                    "suggested_quantity": qty,
                    "suggested_price": dp.get("recommended_price"),
                    "reason": "עודף אצל " + str(sa.get("name")) + " · מחסור אצל " + str(sb.get("name")),
                    "demo": bool(sa.get("demo") and sb.get("demo")),
                })
    return opps


def _consent_key(o):
    return (o["from_store_id"], o["to_store_id"], _norm_name(o["product_name"]))


def _consents_for(db, o):
    k = _consent_key(o)
    return {c["by"] for c in db.get("contact_consents", [])
            if (c.get("a"), c.get("b"), c.get("product")) == k}


@app.route("/network/transfer-opportunities", methods=["GET"])
def transfer_opportunities():
    db = load_db()
    mine = caller_store_ids(db)
    out = []
    for o in find_transfer_opportunities(db):
        parties = [o["from_store_id"], o["to_store_id"]]
        if not _visible(db, parties, mine):
            continue
        me = next((p for p in parties if p in mine), None)
        if me and not o["demo"]:
            other = parties[1] if me == parties[0] else parties[0]
            agreed = _consents_for(db, o)
            o["you_asked"] = me in agreed
            o["other_asked"] = other in agreed
            # contact details only once BOTH stores asked to be put in touch
            if me in agreed and other in agreed:
                o["contact_phone"] = db["stores"][other].get("phone")
        out.append(o)
    return jsonify({"opportunities": out, "count": len(out)})


MATCH_MSG = ("יש התאמה חדשה להעברת מלאי ברשת Genius. "
             "היכנסו למסך \"העברת מלאי\" באתר כדי לראות אותה ולאשר יצירת קשר.")
BOTH_MSG = ("שני הצדדים אישרו יצירת קשר להעברת מלאי ב-Genius. "
            "פרטי הקשר מופיעים עכשיו במסך \"העברת מלאי\" באתר.")


@app.route("/network/transfer-opportunities/notify", methods=["POST"])
def notify_transfer_opportunities():
    """
    The caller's store asks to be put in touch. The other store gets a fixed
    notice (no names, phones or product text). Phones are revealed in the site
    only after both stores have asked.
    """
    data = request.get_json(silent=True) or {}
    outbox = []
    with db_lock():
        db = load_db()
        match = None
        for o in find_transfer_opportunities(db):
            if (o["from_store_id"] == data.get("from_store_id")
                    and o["to_store_id"] == data.get("to_store_id")
                    and o["product_name"] == data.get("product_name")):
                match = o
                break
        if not match:
            return jsonify({"error": "not found"}), 404
        if match["demo"]:
            return jsonify({"success": True, "demo": True, "notified": []})
        mine = caller_store_ids(db)
        parties = [match["from_store_id"], match["to_store_id"]]
        me = next((p for p in parties if p in mine), None)
        if not me:
            return deny()
        other = parties[1] if me == parties[0] else parties[0]
        if me in _consents_for(db, match):
            # asking twice changes nothing and sends nothing
            both = _consents_for(db, match) >= {me, other}
            return jsonify({"success": True, "demo": False, "both_agreed": both,
                            "already_requested": True, "notified": []})
        a, b_, prod = _consent_key(match)
        db.setdefault("contact_consents", []).append(
            {"a": a, "b": b_, "product": prod, "by": me, "at": datetime.now().isoformat()})
        save_db(db)
        both = _consents_for(db, match) >= {me, other}
        if both:
            outbox = [(db["stores"][me].get("phone"), BOTH_MSG), (db["stores"][other].get("phone"), BOTH_MSG)]
        else:
            outbox = [(db["stores"][other].get("phone"), MATCH_MSG)]
    sent = [bool(send_whatsapp(ph, msg)) for ph, msg in outbox]
    return jsonify({"success": all(sent), "demo": False, "both_agreed": both,
                    "notified": [other] if not both else parties})


@app.route("/store/<store_id>/settings", methods=["POST"])
def store_settings(store_id):
    """Store-owner settings. For now: joining the network (opt-in)."""
    data = request.get_json(silent=True) or {}
    with db_lock():
        db = load_db()
        store = db.get("stores", {}).get(store_id)
        if not store or not can_write(store):
            return deny()
        if "network_opt_in" in data:
            if data["network_opt_in"] and not normalize_phone(store.get("phone")):
                return jsonify({"error": "כדי להצטרף לרשת צריך להירשם עם טלפון (מסך ההרשמה)"}), 400
            store["network_opt_in"] = bool(data["network_opt_in"])
        if data.get("name"):
            store["name"] = str(data["name"]).strip()[:80]
        save_db(db)
    return jsonify({"store_id": store_id, "network_opt_in": bool(store.get("network_opt_in")),
                    "name": store.get("name"), "has_phone": bool(normalize_phone(store.get("phone")))})


def nightly_job():
    """
    Nightly: trial reminders/expiry and a fresh rule analysis per store.
    Runs once per day even with several worker processes.
    """
    print(f"\n=== ניתוח לילי {datetime.now()} ===")
    today = datetime.now().date().isoformat()
    outbox = []
    with db_lock():
        db = load_db()
        meta = db.setdefault("meta", {})
        if meta.get("nightly_last") == today:
            print("  already ran today")
            return
        meta["nightly_last"] = today
        for store_id, store in db["stores"].items():
            if store.get("demo") or not store.get("active"):
                continue
            if store.get("plan") == "trial" and store.get("trial_ends"):
                try:
                    trial_end = datetime.fromisoformat(store["trial_ends"])
                except ValueError:
                    trial_end = None
                days_left = (trial_end - datetime.now()).days if trial_end else None
                if days_left == 3:
                    base = PUBLIC_URL or os.environ.get("REPLIT_URL", "")
                    outbox.append((store.get("phone"),
                        f"שלום {store.get('name')}! 👋\nנשארו 3 ימים לתקופת הניסיון.\nלהמשך השירות: {base}/subscribe/{store_id}/basic"))
                elif days_left is not None and days_left <= 0:
                    store["active"] = False
                    outbox.append((store.get("phone"),
                        "תקופת הניסיון הסתיימה 😢\nנשמח שתחזור! להפעלה מחדש פנה אלינו."))
            if store.get("active") and store.get("sales_csv"):
                try:
                    _save_rule_analysis(db, store_id)
                except Exception as e:
                    print(f"  ! analysis failed for {store_id}: {e}")
        save_db(db)
    for ph, msg in outbox:
        send_whatsapp(ph, msg)


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
# ===========================================
# Sales file reader — Hebrew/English headers, any common Israeli export shape
# ===========================================
_EXACT = {
    "date": {"date", "day", "sale_date", "תאריך", "תאריך מכירה", "יום", "תאריך מסמך"},
    "name": {"product", "name", "product_name", "item", "description",
             "מוצר", "שם", "שם מוצר", "שם פריט", "פריט", "תיאור", "תיאור פריט"},
    "qty": {"qty", "quantity", "units", "sold",
            "כמות", "כמות שנמכרה", "נמכר", "יחידות", "יח'"},
    "price": {"price", "unit_price", "מחיר", "מחיר ליחידה"},
    "total": {"total", "amount", "revenue", "סה\"כ", "סהכ", "סכום", "פדיון"},
}


def _header_role(h, exact_only):
    k = str(h or "").replace("﻿", "").strip().strip('"').strip().lower()
    if not k:
        return None
    for role, keys in _EXACT.items():
        if k in keys:
            return role
    if exact_only:
        return None
    if any(x in k for x in ("קוד", "code", "sku", "ברקוד", "barcode", "מק\"ט", "מקט")):
        return "sku"
    if "תאריך" in k or "date" in k:
        return "date"
    if any(x in k for x in ("כמות", "qty", "quantity", "יחידות", "units")):
        return "qty"
    if "מחיר" in k or "price" in k:
        return "price"
    if any(x in k for x in ("סה\"כ", "סהכ", "total", "סכום", "amount", "revenue", "פדיון")):
        return "total"
    if any(x in k for x in ("מוצר", "פריט", "תיאור", "product", "item", "description", "name", "שם")):
        return "name"
    return None


def _map_columns(header):
    cols = {}
    for exact_only in (True, False):
        for i, h in enumerate(header):
            role = _header_role(h, exact_only)
            if role and role not in cols and i not in cols.values():
                cols[role] = i
    return cols


def _parse_num(raw):
    s = str(raw if raw is not None else "").strip().strip('"')
    for ch in ("₪", "‏", "‎", " ", "\xa0"):
        s = s.replace(ch, "")
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.endswith("-"):          # some Israeli exports write negatives as "5-"
        neg, s = True, s[:-1]
    if s.startswith("-"):
        neg, s = (not neg), s[1:]
    if s.startswith("+"):
        s = s[1:]
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")          # 1,200 -> 1200
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+,\d{1,2}", s):
        s = s.replace(".", "").replace(",", ".")  # 1.200,50 -> 1200.50
    elif re.fullmatch(r"\d+,\d{1,2}", s):
        s = s.replace(",", ".")         # 1,5 -> 1.5
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _date_parts(raw):
    s = str(raw if raw is not None else "").strip().strip('"')
    if not s:
        return None
    s = re.split(r"[ T]", s)[0]
    m = re.fullmatch(r"(\d{5})(\.0+)?", s)
    if m and 20000 < int(m.group(1)) < 80000:       # Excel serial day number
        return ("serial", int(m.group(1)))
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return ("ymd", int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        return ("ymd", int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", s)
    if m:
        y = int(m.group(3))
        if y < 100:
            y += 2000 if y < 70 else 1900
        return ("xy", int(m.group(1)), int(m.group(2)), y)
    return "bad"


def _build_date(p, day_first):
    try:
        if p[0] == "serial":
            return datetime(1899, 12, 30) + timedelta(days=p[1])
        if p[0] == "ymd":
            return datetime(p[1], p[2], p[3])
        a, b, y = p[1], p[2], p[3]
        return datetime(y, b, a) if day_first else datetime(y, a, b)
    except ValueError:
        return None


def _sniff_delimiter(text):
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        first = sample.split("\n", 1)[0]
        return max(",;\t|", key=first.count)


def parse_sales_table(text):
    """
    Reads a sales export and reports exactly what it kept and what it dropped.
    Returns {"sales": {product: [(date|None, qty)]}, "prices": {...}, "info": {...}}
    """
    info = {"rows_total": 0, "rows_used": 0, "dropped": {}, "delimiter": None,
            "date_order": None, "columns": {}}
    out = {"sales": {}, "prices": {}, "info": info}
    text = (text or "").lstrip("﻿").strip()
    if not text:
        return out
    # the header may sit under a few title lines, which can fool the sniffer —
    # so try the sniffed separator first, then the other common ones
    sniffed = _sniff_delimiter(text)
    header_at, cols, rows, delim = None, {}, [], sniffed
    for cand in [sniffed] + [d for d in ",;\t|" if d != sniffed]:
        cand_rows = list(csv.reader(io.StringIO(text), delimiter=cand))
        for i, row in enumerate(cand_rows[:10]):
            c = _map_columns(row)
            if "name" in c and "qty" in c:
                header_at, cols, rows, delim = i, c, cand_rows, cand
                break
        if header_at is not None:
            break
    info["delimiter"] = delim
    if header_at is None:
        info["error"] = "missing_columns"
        return out
    info["columns"] = {role: rows[header_at][i].strip() for role, i in cols.items()}
    body = rows[header_at + 1:]

    def cell(row, role):
        i = cols.get(role)
        return row[i] if i is not None and i < len(row) else None

    # decide day/month order once for the whole file
    parts = [_date_parts(cell(r, "date")) if "date" in cols else None for r in body]
    xy = [p for p in parts if isinstance(p, tuple) and p[0] == "xy"]
    first_big = any(p[1] > 12 for p in xy)
    second_big = any(p[2] > 12 for p in xy)
    if second_big and not first_big:
        day_first, info["date_order"] = False, "month_first"
    else:
        day_first = True
        if first_big and second_big:
            info["date_order"] = "mixed"      # rows that don't fit are dropped
        elif first_big:
            info["date_order"] = "day_first"
        elif xy and any(p[1] != p[2] for p in xy):
            info["date_order"] = "assumed_day_first"
        elif xy:
            info["date_order"] = "day_first"

    def drop(reason):
        info["dropped"][reason] = info["dropped"].get(reason, 0) + 1

    last_price = {}
    for row, p in zip(body, parts):
        if not any(str(c).strip() for c in row):
            continue
        info["rows_total"] += 1
        name = str(cell(row, "name") or "").strip()
        if not name:
            drop("missing_name")
            continue
        qty = _parse_num(cell(row, "qty"))
        if qty is None:
            drop("bad_quantity")
            continue
        d = None
        if "date" in cols:
            if p is None:
                drop("missing_date")
                continue
            if p == "bad" or (p[0] == "xy" and info["date_order"] == "mixed" and p[2] > 12):
                drop("bad_date")
                continue
            d = _build_date(p, day_first)
            if d is None:
                drop("bad_date")
                continue
        out["sales"].setdefault(name, []).append((d, qty))
        info["rows_used"] += 1
        price = _parse_num(cell(row, "price")) if "price" in cols else None
        if (price is None or price <= 0) and "total" in cols and qty:
            tot = _parse_num(cell(row, "total"))
            price = (tot / qty) if tot and qty > 0 else None
        if price and price > 0:
            prev = last_price.get(name)
            if prev is None or (d and (prev[0] is None or d >= prev[0])):
                last_price[name] = (d, round(price, 2))
    out["prices"] = {n: v[1] for n, v in last_price.items()}
    return out


def parse_sales_csv(csv_text):
    return parse_sales_table(csv_text)["sales"]


def sales_to_csv(sales):
    """Canonical storage form: ISO dates, quoted names."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "product", "qty"])
    rows = sorted(((d, n, q) for n, h in sales.items() for d, q in h if d is not None),
                  key=lambda r: (r[0], r[1]))
    for d, n, q in rows:
        w.writerow([d.strftime("%Y-%m-%d"), n, ("%g" % q)])
    return buf.getvalue().strip()


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
    if current_stock is None:
        # without a stock figure there is nothing to count down from —
        # say so instead of pretending the shelf is empty
        days_until_empty, reorder_now, order_quantity = None, False, None
    else:
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
    return {"daily_rate": round(daily_rate, 2), "adjusted_daily_rate": round(adjusted_rate, 2), "days_until_empty": days_until_empty, "reorder_now": reorder_now, "order_quantity": order_quantity, "trend": trend, "confidence": confidence, "data_points": len(history), "stock_known": current_stock is not None}


def _stock_lookup(stock_map):
    """Exact name first, then a loose match (spacing/quotes) against the stock list."""
    stock_map = stock_map or {}
    loose = {_norm_name(k): v for k, v in stock_map.items()}

    def get(name):
        if name in stock_map:
            v = stock_map[name]
        else:
            v = loose.get(_norm_name(name))
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None
    return get


def forecast_all(csv_text, stock_map, lead_time_days=3):
    sales = parse_sales_csv(csv_text)
    stock_of = _stock_lookup(stock_map)
    results = []
    every = [d for h in sales.values() for d, q in h if d is not None]
    data_end = max(every) if every else None
    for name, history in sales.items():
        stock = stock_of(name)
        f = forecast_product(history, stock, lead_time_days)
        sold = [d for d, q in history if d is not None and q > 0]
        # the average over the whole history would keep "selling" an item
        # that stopped weeks ago — once it's been silent 14+ days, it's stopped
        if data_end is not None and sold and (data_end - max(sold)).days >= 14:
            f.update({"adjusted_daily_rate": 0.0,
                      "days_until_empty": None if stock is None else 999,
                      "reorder_now": False, "order_quantity": None if stock is None else 0,
                      "trend": "stopped",
                      "days_since_last_sale": (data_end - max(sold)).days})
        f["product_name"] = name
        f["current_stock"] = None if stock is None else int(round(stock))
        results.append(f)
    results.sort(key=lambda r: (r["days_until_empty"] is None,
                                r["days_until_empty"] if r["days_until_empty"] is not None else 0,
                                -r["adjusted_daily_rate"]))
    return results


def _lead_time(raw, default=3):
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 60 else None


@app.route("/forecast/<store_id>", methods=["POST"])
def forecast_endpoint(store_id):
    """Stateless: forecasts the CSV sent in the request. Stores nothing."""
    data = request.get_json(silent=True) or {}
    csv_text = data.get("csv", "")
    stock_map = data.get("stock") or {}
    lead_time = _lead_time(data.get("lead_time_days", 3))
    if not csv_text or not isinstance(csv_text, str):
        return jsonify({"error": "missing csv"}), 400
    if not isinstance(stock_map, dict):
        return jsonify({"error": "stock must be an object {product: quantity}"}), 400
    if lead_time is None:
        return jsonify({"error": "lead_time_days must be a whole number 0-60"}), 400
    results = forecast_all(csv_text, stock_map, lead_time)
    urgent = [r for r in results if r["reorder_now"]]
    return jsonify({"store_id": store_id, "forecasts": results, "urgent_count": len(urgent),
                    "total_products": len(results),
                    "stock_known": any(r["stock_known"] for r in results)})

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
    Products several network stores need at the same time, so their orders
    can be combined into one volume order. The discount is a rough estimate
    by volume — there is no supplier price list behind it yet.
    """
    members = network_members(db)
    demand = {}
    for sid, store in members.items():
        analysis = latest_analysis(db, sid)[0]
        for p in analysis.get("hot_products", []) or []:
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            try:
                qty = int(p.get("order_quantity", 0) or 0)
            except (ValueError, TypeError):
                continue
            if qty <= 0:
                continue
            key = _norm_name(name)
            demand.setdefault(key, {"display_name": name, "participants": []})
            demand[key]["participants"].append({
                "store_id": sid,
                "store_name": store.get("name"),
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
            "discount_is_estimate": True,
            "discount_note": "הערכה לפי נפח הזמנה — יש לאמת מול הספק",
            "demo": all(members[p["store_id"]].get("demo") for p in parts),
        })

    opps.sort(key=lambda o: o["total_quantity"], reverse=True)
    return opps


# ===========================================
# Data-driven lending - function #12
# ===========================================

def monthly_revenue_from_sales(sales, price_map=None):
    """
    sales: {product: [(date, qty), ...]} from parse_sales_csv
    Returns {"YYYY-MM": revenue} from products that have a price.
    Products without a price are left out — never counted as ₪1.
    """
    price_of = _stock_lookup(price_map or {})
    buckets = {}
    for name, history in sales.items():
        price = price_of(name)
        if not price or price <= 0:
            continue
        for d, qty in history:
            if d is None:
                continue
            key = "%04d-%02d" % (d.year, d.month)
            buckets[key] = buckets.get(key, 0.0) + (qty * price)
    return buckets


def price_coverage(sales, price_map):
    """Share of units sold that belong to products with a known price."""
    price_of = _stock_lookup(price_map or {})
    total = priced = 0.0
    for name, history in sales.items():
        u = sum(q for d, q in history if d is not None and q > 0)
        total += u
        if (price_of(name) or 0) > 0:
            priced += u
    return (priced / total) if total else 0.0


def _full_months(sales):
    """Months fully covered by the data (partial first/last months dropped)."""
    import calendar
    dates = [d for h in sales.values() for d, _ in h if d is not None]
    if not dates:
        return set()
    first, last = min(dates), max(dates)
    months = set()
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        days_in = calendar.monthrange(y, m)[1]
        starts_ok = (y, m) != (first.year, first.month) or first.day <= 5
        ends_ok = (y, m) != (last.year, last.month) or last.day >= days_in - 5
        if starts_ok and ends_ok:
            months.add("%04d-%02d" % (y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return months


LENDING_MIN_MONTHS = 6
# A shekel credit line is shown only once there is a real lending partner
# behind it; until then the profile shows the score and history, no amount.
LENDING_SHOW_AMOUNT = os.environ.get("LENDING_SHOW_AMOUNT", "") == "1"
LENDING_DISCLAIMER = ("הערכה פנימית בלבד, מחושבת מנתוני המכירות שהעלית. "
                      "זו לא הצעת אשראי ולא התחייבות של גוף מממן.")


def calculate_credit_profile(csv_text, price_map=None):
    """
    A simple health score from the store's own sales. It is a heuristic,
    not a credit decision, and it refuses to guess when data is thin.
    """
    sales = parse_sales_csv(csv_text)
    coverage = price_coverage(sales, price_map)
    base = {"disclaimer": LENDING_DISCLAIMER, "price_coverage_pct": int(round(coverage * 100)),
            "min_months": LENDING_MIN_MONTHS}

    if coverage < 0.5:
        return dict(base, months_of_data=0, avg_monthly_revenue=None, trend="unknown",
                    volatility_pct=None, score=None, risk="insufficient_data", max_loan=0,
                    reason="חסרים מחירים לרוב המוצרים — אי אפשר לחשב הכנסה בשקלים. "
                           "העלה קובץ עם עמודת מחיר או קובץ אחיד מהקופה.")

    full = _full_months(sales)
    monthly = {m: v for m, v in monthly_revenue_from_sales(sales, price_map).items() if m in full}
    months = sorted(monthly.keys())
    values = [monthly[m] for m in months]
    n = len(values)

    if n < 3:
        return dict(base, months_of_data=n, avg_monthly_revenue=None, trend="unknown",
                    volatility_pct=None, score=None, risk="insufficient_data", max_loan=0,
                    reason="נדרשים לפחות 3 חודשים מלאים של נתונים לפרופיל, ו-%d להערכת מסגרת."
                           % LENDING_MIN_MONTHS)

    avg = sum(values) / n
    mid = n // 2
    first = sum(values[:mid]) / max(1, mid)
    second = sum(values[mid:]) / max(1, n - mid)
    trend = "stable"
    if first > 0:
        change = (second - first) / first
        if change > 0.15:
            trend = "growing"
        elif change < -0.15:
            trend = "declining"

    volatility = ((sum((v - avg) ** 2 for v in values) / n) ** 0.5 / avg * 100) if avg > 0 else 0.0

    score = 50 + (20 if n >= 6 else 10)
    score += {"growing": 20, "declining": -20}.get(trend, 0)
    score += 15 if volatility < 20 else (5 if volatility < 40 else -15)
    score = max(0, min(100, score))

    if score >= 75:
        risk, mult = "low", 2.0
    elif score >= 55:
        risk, mult = "medium", 1.2
    elif score >= 35:
        risk, mult = "high", 0.5
    else:
        risk, mult = "very_high", 0.0

    enough = n >= LENDING_MIN_MONTHS
    if not enough:
        reason = "יש %d חודשים מלאים — הערכת מסגרת דורשת %d חודשים." % (n, LENDING_MIN_MONTHS)
    elif not LENDING_SHOW_AMOUNT:
        reason = "סכום מסגרת יוצג כשיהיה גוף מממן שותף."
    else:
        reason = None
    return dict(
        base,
        months_of_data=n,
        avg_monthly_revenue=round(avg, 2),
        trend=trend,
        volatility_pct=round(volatility, 1),
        score=score,
        risk=risk,
        max_loan=int(round(avg * mult)) if (enough and LENDING_SHOW_AMOUNT) else 0,
        amount_hidden=bool(enough and not LENDING_SHOW_AMOUNT),
        reason=reason,
        monthly_breakdown={m: round(monthly[m], 2) for m in months},
        revenue_basis="partial_prices" if coverage < 0.95 else "prices",
    )


@app.route("/network/group-buying", methods=["GET"])
def group_buying():
    db = load_db()
    mine = caller_store_ids(db)
    opps = [o for o in find_group_buying_opportunities(db)
            if _visible(db, [p["store_id"] for p in o["participants"]], mine)]
    return jsonify({"opportunities": opps, "count": len(opps)})


GROUP_MSG = ("יש קנייה משותפת פתוחה ברשת Genius למוצר שאתם מזמינים. "
             "הפרטים והכמויות במסך \"קנייה משותפת\" באתר.")


@app.route("/network/group-buying/notify", methods=["POST"])
def notify_group_buying():
    data = request.get_json(silent=True) or {}
    product = data.get("product_name")
    if not product:
        return jsonify({"error": "missing product_name"}), 400
    db = load_db()
    match = None
    for o in find_group_buying_opportunities(db):
        if _norm_name(o["product_name"]) == _norm_name(product):
            match = o
            break
    if not match:
        return jsonify({"error": "no matching group"}), 404
    if match["demo"]:
        return jsonify({"product_name": match["product_name"], "notified": [], "demo": True})
    mine = caller_store_ids(db)
    ids = [p["store_id"] for p in match["participants"]]
    me = next((i for i in ids if i in mine), None)
    if not me:
        return deny()
    with db_lock():
        db2 = load_db()
        notices = db2.setdefault("meta", {}).setdefault("group_notices", {})
        key = me + "|" + _norm_name(product)
        today = datetime.now().date().isoformat()
        if notices.get(key) == today:
            return jsonify({"error": "כבר נשלחה הודעה על הקבוצה הזו היום"}), 429
        notices[key] = today
        save_db(db2)
    sent = []
    for part in match["participants"]:
        if part["store_id"] == me:
            continue
        ok = send_whatsapp(db["stores"][part["store_id"]].get("phone"), GROUP_MSG)
        sent.append({"store_id": part["store_id"], "sent": bool(ok)})
    return jsonify({"product_name": match["product_name"], "notified": sent, "demo": False})


@app.route("/lending/<store_id>", methods=["POST"])
def lending_endpoint(store_id):
    """Stateless: scores the CSV sent in the request. Stores nothing."""
    data = request.get_json(silent=True) or {}
    csv_text = data.get("csv", "")
    price_map = data.get("prices") or {}
    if not csv_text or not isinstance(csv_text, str):
        return jsonify({"error": "missing csv"}), 400
    if not isinstance(price_map, dict):
        return jsonify({"error": "prices must be an object {product: price}"}), 400
    profile = calculate_credit_profile(csv_text, price_map)
    profile["store_id"] = store_id
    return jsonify(profile)

# ===========================================
# Demo data seeding
# ===========================================
# Demo stores hold made-up sales, stock and prices. Everything shown for them
# (dead stock, reorder alerts, transfers, group buys) is computed by the same
# rules a real store gets — nothing in the results is typed in by hand.

DEMO_MONTHS = 6          # March..August 2026
# grocery week in Israel, Sunday..Saturday: busy Thursday/Friday, quiet Shabbat
DEMO_WEEK = [1.0, 0.9, 0.9, 1.0, 1.3, 1.6, 0.3]


def _demo_sales_csv(base, months=DEMO_MONTHS, growth=0):
    """
    Daily demo sales. base: [(name, units_per_2.5_days)] or
    [(name, qty, last_month_index)] for an item that stopped selling after
    that month (seasonal / over-ordered). Fractions carry over day to day,
    so small items still sell a unit now and then.
    """
    rows = ["date,product,qty"]
    carry = {}
    day = datetime(2026, 3, 1)
    end = datetime(2026, 3 + months, 1)
    while day < end:
        i = day.month - 3
        dow = (day.weekday() + 1) % 7
        for item in base:
            name, qty = item[0], item[1]
            if len(item) > 2 and i > item[2]:
                continue
            want = qty * 12 / 30.0 * DEMO_WEEK[dow] * (1 + growth * i) + carry.get(name, 0.0)
            q = int(want)
            carry[name] = want - q
            if q > 0:
                rows.append("%s,%s,%d" % (day.strftime("%Y-%m-%d"), name, q))
        day += timedelta(days=1)
    return "\n".join(rows)


DEMO_STORES = {
    "demo_pharm": {
        "name": "כהן פארם",
        "phone": "0501234567",
        "stock": {"חלב תנובה 3%": 40, "שמפו הד אנד שולדרס": 60, "קרם הגנה SPF 50": 45, "לחם אחיד": 260, "ביצים L (12)": 170, "קוטג' 5%": 200, "קפה עלית 200g": 120, "נייר טואלט 32": 80, "שמן קנולה 1ל": 140},
        "prices": {"חלב תנובה 3%": 7, "שמפו הד אנד שולדרס": 24, "קרם הגנה SPF 50": 38, "לחם אחיד": 7, "ביצים L (12)": 14, "קוטג' 5%": 6, "קפה עלית 200g": 23, "נייר טואלט 32": 39, "שמן קנולה 1ל": 12},
        "sales_base": [("חלב תנובה 3%", 20), ("שמפו הד אנד שולדרס", 5), ("קרם הגנה SPF 50", 4, 3),
                       ("לחם אחיד", 45), ("ביצים L (12)", 30), ("קוטג' 5%", 38),
                       ("קפה עלית 200g", 12), ("נייר טואלט 32", 9), ("שמן קנולה 1ל", 16)],
        "growth": 0.06,
    },
    "demo_super": {
        "name": "סופר דיזנגוף",
        "phone": "0502345678",
        "stock": {"חלב תנובה 3%": 55, "קרם הגנה SPF 50": 8, "שמפו הד אנד שולדרס": 70, "מטרייה מתקפלת": 30, "לחם אחיד": 400, "ביצים L (12)": 300, "קוטג' 5%": 360, "קפה עלית 200g": 130, "נייר טואלט 32": 100, "שמן קנולה 1ל": 150, "גבינה צהובה 28%": 190, "סוכר 1ק\"ג": 110},
        "prices": {"חלב תנובה 3%": 7, "קרם הגנה SPF 50": 38, "שמפו הד אנד שולדרס": 24, "מטרייה מתקפלת": 45, "לחם אחיד": 7, "ביצים L (12)": 14, "קוטג' 5%": 6, "קפה עלית 200g": 23, "נייר טואלט 32": 39, "שמן קנולה 1ל": 12, "גבינה צהובה 28%": 32, "סוכר 1ק\"ג": 6},
        "sales_base": [("חלב תנובה 3%", 28), ("קרם הגנה SPF 50", 3), ("שמפו הד אנד שולדרס", 4),
                       ("מטרייה מתקפלת", 2, 0),
                       ("לחם אחיד", 70), ("ביצים L (12)", 52), ("קוטג' 5%", 61),
                       ("קפה עלית 200g", 22), ("נייר טואלט 32", 17), ("שמן קנולה 1ל", 26),
                       ("גבינה צהובה 28%", 33), ("סוכר 1ק\"ג", 19)],
        "growth": 0.10,
    },
    "demo_makolet": {
        "name": "מכולת הרצל",
        "phone": "0503456789",
        "stock": {"חלב תנובה 3%": 20, "ממתקי פורים": 80, "לחם אחיד": 90, "ביצים L (12)": 60, "קוטג' 5%": 70, "קפה עלית 200g": 25, "שמן קנולה 1ל": 30},
        "prices": {"חלב תנובה 3%": 7, "ממתקי פורים": 15, "לחם אחיד": 7, "ביצים L (12)": 14, "קוטג' 5%": 6, "קפה עלית 200g": 23, "שמן קנולה 1ל": 12},
        "sales_base": [("חלב תנובה 3%", 13), ("ממתקי פורים", 6, 0),
                       ("לחם אחיד", 24), ("ביצים L (12)", 15), ("קוטג' 5%", 18),
                       ("קפה עלית 200g", 6), ("שמן קנולה 1ל", 8)],
        "growth": -0.10,
    },
}

DEMO_IDS = set(DEMO_STORES) | {"demo_bakery"}


def build_demo_db(db):
    """Adds (or refreshes) the demo stores. Never touches real stores."""
    db.setdefault("stores", {})
    db.setdefault("recommendations", [])
    db.setdefault("suppliers", {})
    db["recommendations"] = [r for r in db["recommendations"] if r.get("store_id") not in DEMO_IDS]

    created = []
    for sid, spec in DEMO_STORES.items():
        db["stores"][sid] = {
            "name": spec["name"],
            "phone": spec["phone"],
            "active": True,
            "demo": True,
            "stock": spec["stock"],
            "prices": spec["prices"],
            "sales_csv": _demo_sales_csv(spec["sales_base"], DEMO_MONTHS, spec["growth"]),
            "data_source": "demo",
        }
        _save_rule_analysis(db, sid)
        created.append({"store_id": sid, "name": spec["name"]})

    created.append(build_bakery_demo(db))
    return created


def ensure_demo(db):
    """Demo stores are refreshed on startup, so no admin call is needed."""
    build_demo_db(db)


@app.route("/admin/seed-demo", methods=["POST"])
def seed_demo():
    """Re-creates the demo stores. Idempotent, leaves real stores untouched."""
    err = require_admin()
    if err:
        return err
    with db_lock():
        db = load_db()
        created = build_demo_db(db)
        save_db(db)
    return jsonify({"seeded": created, "total_stores": len(db.get("stores", {}))})


@app.route("/admin/seed-demo", methods=["DELETE"])
def remove_demo():
    """Removes every demo store and its recommendations."""
    err = require_admin()
    if err:
        return err
    with db_lock():
        db = load_db()
        removed = [sid for sid in list(db.get("stores", {})) if sid in DEMO_IDS]
        for sid in removed:
            db["stores"].pop(sid, None)
        db["recommendations"] = [
            r for r in db.get("recommendations", []) if r.get("store_id") not in DEMO_IDS
        ]
        save_db(db)
    return jsonify({"removed": removed, "total_stores": len(db.get("stores", {}))})


@app.route("/forecast/<store_id>", methods=["GET"])
def forecast_saved(store_id):
    """Forecast from the sales data already saved on the store."""
    db = load_db()
    store, err = get_readable_store(db, store_id)
    if err:
        return err
    csv_text = store.get("sales_csv")
    if not csv_text:
        return jsonify({"error": "no saved sales data for this store"}), 404
    results = forecast_all(csv_text, store.get("stock") or {}, 3)
    urgent = [r for r in results if r["reorder_now"]]
    return jsonify({
        "store_id": store_id,
        "store_name": store.get("name"),
        "forecasts": results,
        "urgent_count": len(urgent),
        "total_products": len(results),
        "stock_known": any(r["stock_known"] for r in results),
    })


@app.route("/lending/<store_id>", methods=["GET"])
def lending_saved(store_id):
    """Credit profile from the sales data already saved on the store."""
    db = load_db()
    store, err = get_readable_store(db, store_id)
    if err:
        return err
    csv_text = store.get("sales_csv")
    if not csv_text:
        return jsonify({"error": "no saved sales data for this store"}), 404
    profile = calculate_credit_profile(csv_text, store.get("prices") or {})
    profile["store_id"] = store_id
    profile["store_name"] = store.get("name")
    return jsonify(profile)


def recent_sales(sales, days=28):
    """Units per product over the last `days` of the data, and the window used."""
    dates = [d for h in sales.values() for d, _ in h if d is not None]
    if not dates:
        return {}, 0
    last = max(dates)
    span = min(days, (last - min(dates)).days + 1)
    units = {}
    for n, h in sales.items():
        u = sum(q for d, q in h if d is not None and (last - d).days < span)
        units[n] = u
    return units, span


@app.route("/store/<store_id>", methods=["GET"])
def store_state(store_id):
    """
    Everything the store's own screens need: latest analysis, weekly sales
    and one product list with status and recommendation.
    """
    db = load_db()
    store, err = get_readable_store(db, store_id)
    if err:
        return err

    analysis, latest = latest_analysis(db, store_id)
    dead = analysis.get("dead_products", []) or []
    hot = analysis.get("hot_products", []) or []
    prices = store.get("prices", {}) or {}
    stock = store.get("stock", {}) or {}
    price_of = _stock_lookup(prices)
    stock_of = _stock_lookup(stock)

    sales = parse_sales_csv(store.get("sales_csv") or "")
    units, span = recent_sales(sales, 28)
    weeks = (span / 7.0) if span else 0
    weekly_units = {n: (u / weeks if weeks else 0) for n, u in units.items()}
    coverage = price_coverage(sales, prices) if sales else 0.0
    metric = "revenue" if coverage >= 0.5 else "units"
    weekly_revenue = sum(weekly_units[n] * (price_of(n) or 0) for n in weekly_units) if metric == "revenue" else None
    weekly_total_units = sum(weekly_units.values())

    hot_by = {_norm_name(p.get("name")): p for p in hot}
    dead_by = {_norm_name(p.get("name")): p for p in dead}
    names = list(dict.fromkeys(list(sales) + list(stock)))
    products = []
    for name in names:
        k = _norm_name(name)
        on_hand = stock_of(name)
        wk = int(round(weekly_units.get(name, 0)))
        row = {"name": name, "stock": None if on_hand is None else int(round(on_hand)),
               "weekly_sales": wk, "price": price_of(name), "action": None}
        if k in hot_by:
            p = hot_by[k]
            row.update(status="hot", action="הזמן %s יח'" % p.get("order_quantity"),
                       order_quantity=p.get("order_quantity"),
                       days_until_empty=p.get("days_until_empty"))
        elif k in dead_by:
            p = dead_by[k]
            rec = p.get("recommended_price")
            row.update(status="dead", action=("הורד ל-₪%s" % rec) if rec else "שקול הנחה",
                       recommended_price=rec, days_no_sale=p.get("days_no_sale"))
        elif on_hand is None:
            row.update(status="no_stock_data")
        elif wk == 0 and on_hand > 0:
            row.update(status="slow", action="בדוק מלאי")
        else:
            row.update(status="stable")
        products.append(row)
    order = {"hot": 0, "dead": 1, "slow": 2, "stable": 3, "no_stock_data": 4}
    products.sort(key=lambda r: (order.get(r["status"], 9), -(r["weekly_sales"] or 0)))

    stuck_value = sum((d.get("stock") or 0) * (price_of(d.get("name")) or 0) for d in dead)
    alerts = len(dead) + len([p for p in hot if (p.get("order_quantity") or 0) > 0])

    return jsonify({
        "store_id": store_id,
        "store_name": store.get("name"),
        "demo": bool(store.get("demo")),
        "metric": metric,
        "weekly_sales": int(round(weekly_revenue if metric == "revenue" else weekly_total_units)),
        "weekly_revenue": None if weekly_revenue is None else int(round(weekly_revenue)),
        "weekly_units": int(round(weekly_total_units)),
        "window_days": span,
        "price_coverage_pct": int(round(coverage * 100)),
        "stock_known": bool(stock),
        "product_count": len(products),
        "alerts": alerts,
        "stuck_value": int(round(stuck_value)) if metric == "revenue" or stuck_value else None,
        "summary_he": analysis.get("summary_he", ""),
        "analysis_source": analysis.get("source") or ("ai" if latest else None),
        "dead_products": dead,
        "hot_products": hot,
        "products": products,
        "has_analysis": latest is not None,
        "plan": store.get("plan"),
        "active": store.get("active", True),
        "trial_ends": store.get("trial_ends"),
        "data_source": store.get("data_source"),
        "network_opt_in": bool(store.get("network_opt_in")),
        "has_phone": bool(normalize_phone(store.get("phone"))),
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
# Z900 (closing record) shares the A100 prefix: code 4, record no. 9, VAT no. 9,
# primary id 15, system constant 8 — then the total record count, 15 digits,
# at positions 46-60 (field 1155). Verified against the official layout table
# (instruction 131, gov.il); not yet against a real vendor export.
BKMV_Z900 = [("total_records", 46, 15)]

# document types (spec appendix, verified against instruction 131 on gov.il):
# 300 invoice/transaction invoice, 305 tax invoice, 320 tax invoice/receipt,
# 330 credit tax invoice. 400 (receipt) carries payments, not item lines.
# Open question for a real file: a business that issues 300 and then 305 for
# the same sale would be counted twice — check doc_type_counts on import.
BKMV_SALES_DOCS = {"300", "305", "320"}
BKMV_CREDIT_DOCS = {"330"}  # credit notes reverse a sale


def _bkmv_decode(raw):
    """BKMVDATA is single-byte Hebrew: ISO-8859-8 on Windows, CP862 on DOS."""
    if isinstance(raw, str):
        return raw.encode("iso8859-8", errors="replace")
    return raw


def _bkmv_encoding(data):
    """
    Pick the file's Hebrew code page once. ISO-8859-8 puts letters at
    0xE0-0xFA, CP862 (DOS) at 0x80-0x9A; whichever range dominates wins.
    """
    iso = sum(1 for b in data if 0xE0 <= b <= 0xFA)
    dos = sum(1 for b in data if 0x80 <= b <= 0x9A)
    return "cp862" if dos > iso else "iso8859-8"


_BKMV_ENC = threading.local()


def _bkmv_field(line_bytes, start, length):
    """start is 1-indexed per the spec."""
    chunk = line_bytes[start - 1:start - 1 + length]
    enc = getattr(_BKMV_ENC, "value", "iso8859-8")
    try:
        return chunk.decode(enc).strip().strip("!")
    except (UnicodeDecodeError, LookupError):
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
    _BKMV_ENC.value = _bkmv_encoding(data)
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
            rec["cancelled"] = rec.get("cancelled", "").strip() not in ("", "0")
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
            s0, n0 = BKMV_Z900[0][1], BKMV_Z900[0][2]
            declared_total = _bkmv_num(_bkmv_field(lb, s0, n0), 0)

    # lines of a cancelled document are not sales
    cancelled = {(d["doc_type"], d["doc_number"]) for d in docs if d.get("cancelled")}
    for it in items:
        it["cancelled"] = (it.get("doc_type"), it.get("doc_number")) in cancelled

    return {
        "cancelled_documents": len(cancelled),
        "encoding": _BKMV_ENC.value,
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
    allowed = sales_doc_types if sales_doc_types is not None else (BKMV_SALES_DOCS | BKMV_CREDIT_DOCS)

    sales = {}
    prices, seen_dates, skipped_cancelled = {}, 0, 0
    for ln in parsed.get("lines", []):
        if allowed and ln.get("doc_type") not in allowed:
            continue
        if ln.get("cancelled"):
            skipped_cancelled += 1
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
        sales.setdefault(name, []).append((d, qty))
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
        # M100 "cost" is the purchase cost, not the shelf price — it is not
        # used as a selling price, or revenue would be understated

    return {
        "sales_csv": sales_to_csv(sales),
        "stock": stock,
        "prices": prices,
        "sale_lines_used": seen_dates,
        "cancelled_lines_skipped": skipped_cancelled,
    }

def store_for_import(db, store_id):
    """
    Returns (store, new_token, error). A new store id creates a store and a
    fresh access token (returned once); an existing store needs its token.
    """
    if not STORE_ID_RE.match(store_id or ""):
        return None, None, (jsonify({"error": "מזהה חנות לא תקין"}), 400)
    stores = db.setdefault("stores", {})
    store = stores.get(store_id)
    if store is not None:
        if not can_write(store):
            return None, None, deny("אין הרשאה לעדכן את החנות הזו")  # same text as below
        if not store.get("demo") and not is_entitled(store):
            return None, None, (jsonify({"error": "המנוי לא פעיל — אי אפשר לעדכן נתונים"}), 402)
        return store, None, None
    # new stores made by an upload get a random "store_..." id; phone-number
    # ids belong to /onboard, so an upload can't squat someone's number
    if not store_id.startswith("store_") or len(store_id) < 14:
        return None, None, deny("אין הרשאה לעדכן את החנות הזו")
    if rate_limited("new_store", NEW_STORE_LIMIT, 3600):
        return None, None, (jsonify({"error": "יותר מדי חנויות חדשות — נסה שוב מאוחר יותר"}), 429)
    store = {"name": (request.args.get("name") or "").strip()[:80] or "החנות שלי",
             "phone": "", "active": True, "plan": "trial",
             "trial_ends": (datetime.now() + timedelta(days=30)).isoformat(),
             "joined": datetime.now().isoformat()}
    token = new_store_token(store)
    stores[store_id] = store
    return store, token, None


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
        "encoding": parsed.get("encoding"),
        "cancelled_documents": parsed.get("cancelled_documents", 0),
        "cancelled_lines_skipped": converted.get("cancelled_lines_skipped", 0),
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

    lk = db_lock()
    lk.__enter__()
    try:
        return _save_bkmv_import(store_id, converted, summary)
    finally:
        lk.__exit__(None, None, None)


def _save_bkmv_import(store_id, converted, summary):
    db = load_db()
    store, token, err = store_for_import(db, store_id)
    if err:
        return err
    if converted["sale_lines_used"]:
        store["sales_csv"] = converted["sales_csv"]
    if converted["stock"]:
        store["stock"] = converted["stock"]
        dates = [d for h in parse_sales_csv(converted["sales_csv"]).values() for d, _ in h if d]
        store["stock_as_of"] = max(dates).strftime("%Y-%m-%d") if dates else datetime.now().strftime("%Y-%m-%d")
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
    if token:
        summary["store_token"] = token
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
    store, err = get_readable_store(db, store_id)
    if err:
        return err
    csv_text = store.get("sales_csv")
    if not csv_text:
        return jsonify({"error": "no sales data"}), 404

    prices = store.get("prices", {}) or {}
    price_of = _stock_lookup(prices)
    sales = parse_sales_csv(csv_text)

    # revenue needs prices for most of what's sold; otherwise show units
    priced = price_coverage(sales, prices) >= 0.5
    metric = "revenue" if priced else "units"

    def rev(name, qty):
        if not priced:
            return float(qty)
        return qty * (price_of(name) or 0)

    # ---- weekly totals (Israeli week: Sunday-Saturday), last 8 full weeks ----
    weekly = {}
    all_dates = []
    for name, hist in sales.items():
        for d, q in hist:
            if d is None:
                continue
            all_dates.append(d)
            sunday = d - timedelta(days=_he_dow(d))
            key = sunday.strftime("%Y-%m-%d")
            weekly[key] = weekly.get(key, 0.0) + rev(name, q)
    partial_dropped = 0
    if all_dates:
        first, last = min(all_dates), max(all_dates)
        full = [w for w in weekly
                if datetime.strptime(w, "%Y-%m-%d") >= first - timedelta(hours=1)
                and datetime.strptime(w, "%Y-%m-%d") + timedelta(days=6) <= last + timedelta(hours=1)]
        partial_dropped = len(weekly) - len(full)
        weekly = {w: weekly[w] for w in full}
    if len(weekly) < 2:
        weekly = {}      # one week, or pieces of weeks, is not a trend
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
        "week_starts": "sunday",
        "not_enough_weeks": len(weekly_series) < 2,
        "partial_weeks_dropped": partial_dropped,
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


BAKERY_PHONE = "0504567890"


def build_bakery_demo(db):
    """
    Bread and pastries are baked every morning, so there is no shelf stock to
    count down — only the packaged cookies carry a stock figure. The bakery's
    story is its week: the trends screen shows it.
    """
    sid = "demo_bakery"
    prices = {n: p for n, _b, p, _s, _d in BAKERY_ITEMS}
    db.setdefault("stores", {})[sid] = {
        "name": "מאפיית לחם הארץ",
        "phone": BAKERY_PHONE,
        "active": True,
        "demo": True,
        "stock": {"עוגיות חמאה 250 גר'": 40},
        "prices": prices,
        "sales_csv": build_bakery_csv(8),
        "data_source": "demo",
    }
    _save_rule_analysis(db, sid)
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
    a working overview without an AI key.
      dead: stock on hand and no sale for 30+ days (needs 30+ days of data)
      hot:  stock known, and the forecast says it runs out within lead time + 2 days
    """
    csv_text = store.get("sales_csv") or ""
    stock = store.get("stock") or {}
    prices = store.get("prices") or {}
    stock_of = _stock_lookup(stock)
    price_of = _stock_lookup(prices)
    sales = parse_sales_csv(csv_text)

    dates = [d for h in sales.values() for d, _ in h if d is not None]
    if not dates:
        return None
    first_day, last_day = min(dates), max(dates)
    span = (last_day - first_day).days + 1

    dead = []
    sales_by_key = {_norm_name(n): h for n, h in sales.items()}
    for name in dict.fromkeys(list(stock) + list(sales)):
        on_hand = stock_of(name) or 0
        if on_hand <= 0:
            continue
        hist = sales.get(name) or sales_by_key.get(_norm_name(name), [])
        sold = [d for d, q in hist if d is not None and q > 0]
        days = (last_day - max(sold)).days if sold else span
        if days < 30:
            continue
        price = price_of(name)
        rec = round(price * 0.75, 1) if price else None
        dead.append({
            "name": name, "stock": int(round(on_hand)),
            "days_no_sale": days if sold else None,
            "current_price": price, "recommended_price": rec,
            "reason": ("לא נמכר %d ימים" % days) if sold
                      else ("אין אף מכירה ב-%d ימי הנתונים" % span),
        })

    hot = []
    for f in forecast_all(csv_text, stock, 3):
        if not f["stock_known"]:
            continue            # no stock figure — can't judge urgency
        if f["reorder_now"] and (f["order_quantity"] or 0) > 0:
            hot.append({
                "name": f["product_name"],
                "weekly_sales": int(round(f["adjusted_daily_rate"] * 7)),
                "stock": f["current_stock"],
                "days_until_empty": f["days_until_empty"],
                "order_quantity": f["order_quantity"],
            })

    stuck = sum((d["stock"] or 0) * (d["current_price"] or 0) for d in dead)
    parts = []
    if hot:
        parts.append("מוצר אחד עומד להיגמר" if len(hot) == 1 else "%d מוצרים עומדים להיגמר" % len(hot))
    if dead:
        parts.append("מוצר אחד לא זז 30+ יום" if len(dead) == 1 else "%d מוצרים לא זזים 30+ יום" % len(dead))
    if not stock:
        parts.append("חסרים נתוני מלאי — בלי זה אין התראות מחסור. קובץ אחיד מהקופה כולל מלאי")
    summary = ". ".join(parts) + "." if parts else "לא נמצאו בעיות דחופות."

    return {
        "dead_products": sorted(dead, key=lambda x: -(x["days_no_sale"] or 10 ** 6)),
        "hot_products": sorted(hot, key=lambda x: x["days_until_empty"]),
        "stuck_value": int(round(stuck)),
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


DROP_REASONS_HE = {
    "missing_name": "חסר שם מוצר",
    "bad_quantity": "כמות לא מספרית",
    "missing_date": "חסר תאריך",
    "bad_date": "תאריך לא תקין",
}


@app.route("/import/csv/<store_id>", methods=["POST"])
def import_csv(store_id):
    """
    Loads a sales file (date, product, quantity — Hebrew or English headers,
    comma/semicolon/tab separated). ?preview=1 inspects without saving.
    Optional columns: unit price, or line total (price = total / qty).
    """
    raw = _read_upload()
    if not raw:
        return jsonify({"error": "no file received"}), 400
    text = _decode_upload(raw)
    table = parse_sales_table(text)
    sales, info = table["sales"], table["info"]
    dated = {n: [(d, q) for d, q in h if d is not None] for n, h in sales.items()}
    dated = {n: h for n, h in dated.items() if h}
    n_dated = sum(len(h) for h in dated.values())
    if info.get("error") == "missing_columns" or not dated:
        return jsonify({
            "error": "לא נמצאו שורות מכירה עם תאריך. נדרשות עמודות: תאריך, מוצר, כמות",
            "columns_found": info.get("columns"),
            "dropped": info.get("dropped"),
        }), 400

    dates = sorted(d for h in dated.values() for d, _ in h)
    dropped = dict(info["dropped"])
    undated = sum(1 for h in sales.values() for d, _ in h if d is None)
    if undated:
        dropped["missing_date"] = dropped.get("missing_date", 0) + undated
    summary = {
        "store_id": store_id,
        "products": len(dated),
        "sale_rows": n_dated,
        "rows_in_file": info["rows_total"],
        "rows_dropped": sum(dropped.values()),
        "dropped": dropped,
        "dropped_he": {DROP_REASONS_HE.get(k, k): v for k, v in dropped.items()},
        "date_order": info.get("date_order"),
        "columns": info.get("columns"),
        "products_with_price": len(table["prices"]),
        "first_date": dates[0].strftime("%Y-%m-%d"),
        "last_date": dates[-1].strftime("%Y-%m-%d"),
    }
    if info.get("date_order") == "assumed_day_first":
        summary["warning"] = "התאריכים בקובץ יכולים להיקרא בשתי דרכים — קראנו אותם כיום/חודש."
    elif info.get("date_order") == "mixed":
        summary["warning"] = "בקובץ יש תאריכים בשני פורמטים שונים — שורות שלא תאמו לא נקלטו."
    if request.args.get("preview"):
        summary["preview"] = True
        return jsonify(summary)

    with db_lock():
        return _save_csv_import(store_id, dated, table, summary)


def _save_csv_import(store_id, dated, table, summary):
    db = load_db()
    store, token, err = store_for_import(db, store_id)
    if err:
        return err
    store["sales_csv"] = sales_to_csv(dated)
    store["data_source"] = "csv"
    # stock counted before these sales would make every forecast wrong
    if store.get("stock") and (store.get("stock_as_of") or "") < summary["last_date"]:
        store["stock"] = {}
        store.pop("stock_as_of", None)
        summary["stock_note"] = ("נתוני המלאי הקודמים ישנים מהמכירות בקובץ הזה ולכן הוסרו — "
                                 "העלה קובץ אחיד עדכני כדי לקבל שוב התראות מחסור.")
    store["imported_at"] = datetime.now().isoformat()
    if table["prices"]:
        merged = dict(store.get("prices") or {})
        merged.update(table["prices"])
        store["prices"] = merged
    analysis = _save_rule_analysis(db, store_id)
    save_db(db)
    summary["saved"] = True
    summary["store_name"] = store.get("name")
    if token:
        summary["store_token"] = token
    summary["analysis"] = {
        "dead": len((analysis or {}).get("dead_products", [])),
        "hot": len((analysis or {}).get("hot_products", [])),
        "summary_he": (analysis or {}).get("summary_he"),
    }
    return jsonify(summary)


@app.route("/stores", methods=["GET"])
def list_stores():
    """Demo stores for everyone; every store for the admin. A store owner's
    own stores are remembered by the website together with their tokens."""
    db = load_db()
    admin = is_admin()
    out = []
    for sid, s in db.get("stores", {}).items():
        if not (s.get("demo") or admin):
            continue
        out.append({
            "store_id": sid,
            "name": s.get("name") or sid,
            "demo": bool(s.get("demo")),
            "has_data": bool(s.get("sales_csv")),
            "data_source": s.get("data_source") or ("demo" if s.get("demo") else None),
        })
    out.sort(key=lambda x: (x["demo"], x["name"]))
    return jsonify({"stores": out, "count": len(out)})


# refresh demo stores once at startup (idempotent; real stores untouched)
try:
    with db_lock():
        _db = load_db()
        ensure_demo(_db)
        save_db(_db)
        del _db
except Exception as _e:  # never block startup on demo data
    print(f"! demo seeding failed: {_e}")


if __name__ == "__main__":
    print("=" * 50)
    print("🚀 Genius — מערכת מלאה")
    print("=" * 50)
    db = load_db()
    print(f"חנויות: {len(db['stores'])} | ספקים: {len(db['suppliers'])}")
    print(f"Claude API: {'✓' if ANTHROPIC_KEY else '✗ חסר'}")
    print(f"Twilio: {'✓' if TWILIO_SID else '✗ חסר (מצב דמו)'}")
    print(f"Stripe: {'✓' if STRIPE_KEY else '✗ חסר'}")
    print(f"Admin token: {'from ADMIN_TOKEN' if os.environ.get('ADMIN_TOKEN') else ADMIN_TOKEN_FILE}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=8080)
