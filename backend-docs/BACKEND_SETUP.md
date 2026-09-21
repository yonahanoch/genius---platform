# Genius — מדריך הקמה מלא

## מה המערכת כוללת
✓ Onboarding חנות + איסוף ספקים (מה שפיספסנו!)
✓ ניתוח CSV אוטומטי עם Claude
✓ WhatsApp דו-כיווני (Twilio)
✓ הזמנות אוטומטיות לספקים בתגובה "2"
✓ תשלומים ומנויים (Stripe)
✓ תזכורות סוף ניסיון אוטומטיות
✓ Scheduler לילי
✓ דאשבורד ניהול ב-/admin

---

## שלב 1 — Replit (5 דקות)
1. replit.com → Create Repl → **Python**
2. העתק את `main.py` ואת `requirements.txt`
3. Replit יתקין הכל אוטומטית בהרצה ראשונה

## שלב 2 — מפתחות API (Secrets 🔒)

| Secret | מאיפה | חובה? |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com | ✓ חובה |
| `TWILIO_ACCOUNT_SID` | twilio.com/console | לא — בלעדיו מצב דמו |
| `TWILIO_AUTH_TOKEN` | twilio.com/console | לא |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886` | לא |
| `STRIPE_SECRET_KEY` | dashboard.stripe.com | לא — רק לתשלומים |
| `STRIPE_PRICE_BASIC` | Stripe → Products → ₪299 | לא |
| `STRIPE_PRICE_PRO` | Stripe → Products → ₪599 | לא |

| `ADMIN_TOKEN` | סיסמה ארוכה שתבחר | מומלץ — בלעדיו נוצרת אוטומטית בקובץ `.admin_token` |
| `STRIPE_WEBHOOK_SECRET` | Stripe → Webhooks → Signing secret | חובה כדי שתשלום יפעיל מנוי |
| `PUBLIC_URL` | הכתובת הציבורית של השרת | חובה לאימות חתימות Twilio |
| `CORS_ORIGINS` | ברירת מחדל: האתר ב-GitHub Pages + localhost | לא |
| `GENIUS_DATA_DIR` | תיקיית מסד הנתונים (ברירת מחדל: תיקיית ההרצה) | לא |
| `PROXY_HOPS` | כמה פרוקסים לפני השרת (Replit: 1, שרת ישיר: 0) — לזיהוי IP אמיתי במגבלות קצב | לא (ברירת מחדל 1) |
| `LENDING_SHOW_AMOUNT` | `1` רק כשיש גוף מממן שותף — אחרת סכום מסגרת לא מוצג | לא |

**טיפ:** התחל רק עם ANTHROPIC_API_KEY. השאר ירוץ במצב דמו — מדפיס לקונסול במקום לשלוח.

## אבטחה — מה חשוב לדעת
- **כל חנות מקבלת קוד גישה** (store_token) ברגע שהיא נוצרת — בהרשמה (`/onboard`) או בהעלאת הקובץ הראשון. בלי הקוד אי אפשר לקרוא או לשנות את נתוני החנות. השרת שומר רק hash של הקוד.
- **חנויות דמו** פתוחות לקריאה לכולם, ואי אפשר לשנות אותן. הן נבנות מחדש בכל הפעלה של השרת.
- **מסכי ניהול** (`/admin`, `/admin/seed-demo`) דורשים את הכותרת `X-Admin-Token`.
- **מנוי בתשלום** מופעל רק מ-webhook חתום של Stripe (`checkout.session.completed`). דף ה-payment-success לא משנה כלום.
- **WhatsApp נכנס** מתקבל רק עם חתימת Twilio תקינה; זיהוי החנות לפי התאמה מדויקת של מספר הטלפון.
- **רשת**: חנות אמיתית משתתפת בהעברות ובקנייה משותפת רק אחרי שהצטרפה בעצמה. מספרי טלפון לא מוצגים באתר.
- **קוד גישה שאבד**: `POST /admin/store/<id>/reset-token` (עם X-Admin-Token) מנפיק קוד חדש — לוודא זהות בעל החנות לפני שמוסרים. `DELETE /admin/store/<id>` מוחק חנות (למשל מישהו נרשם עם טלפון שלא שלו).
- **רשת**: טלפון של חנות נחשף לחנות אחרת רק אחרי ששתיהן לחצו "בקש יצירת קשר". הודעות WhatsApp ברשת הן טקסט קבוע, בלי שמות או טקסט שהחנויות כתבו.
- **מנוי**: אחרי שתקופת הניסיון נגמרת בלי תשלום, או שמנוי בוטל — אפשר עדיין לצפות בנתונים, אבל לא להעלות חדשים או להשתמש בצ'אט.
- **מסד הנתונים** (JSON) נכתב עם נעילה וכתיבה אטומית, ועם גיבוי `.bak`. זה מספיק לשרת אחד; ללקוחות רבים — לעבור ל-Postgres.

## בדיקות
```bash
cd backend && python3 tests/test_audit.py
```

## שלב 3 — Twilio WhatsApp (כשמוכנים)
1. twilio.com → הרשמה חינם
2. Console → Messaging → **Try WhatsApp**
3. שלח מהנייד שלך: `join <שם-הסנדבוקס>` למספר שיוצג
4. בהגדרות Sandbox → Webhook URL:
   `https://YOUR-REPL.repl.co/webhook/whatsapp`

**לייצור אמיתי:** WhatsApp Business API דורש אישור Meta (2-3 ימים) + Templates מאושרים. עשה את זה מוקדם!

## שלב 4 — Stripe (כשיש לקוח משלם)
1. dashboard.stripe.com → הרשמה
2. Products → צור "Genius Basic" ₪299/חודש → העתק את ה-price_xxx
3. צור "Genius Pro" ₪599/חודש
4. Webhooks → הוסף: `https://YOUR-REPL.repl.co/webhook/stripe` עם האירועים `checkout.session.completed`, `invoice.payment_failed`, `customer.subscription.deleted`
5. העתק את ה-Signing secret ל-`STRIPE_WEBHOOK_SECRET`

---

## איך משתמשים — תרחיש מלא

### חיבור חנות (אתה עושה בביקור):
```bash
curl -X POST https://YOUR-REPL.repl.co/onboard \
  -H "Content-Type: application/json" \
  -d '{
    "name": "מכולת השכונה",
    "phone": "0501234567",
    "system": "hashavshevet",
    "suppliers": [
      {"name": "תנובה", "phone": "0521111111", "products": "מוצרי חלב"},
      {"name": "אסם", "phone": "0532222222", "products": "יבשים וחטיפים"}
    ]
  }'
```
→ בעל החנות מקבל הודעת ברוכים הבאים ב-WhatsApp

### ניתוח (בשלב ראשון אתה מריץ, אח"כ אוטומטי):
```bash
curl -X POST https://YOUR-REPL.repl.co/analyze \
  -F "store_id=0501234567" \
  -F "file=@sales.csv"
```
→ Claude מנתח → בעל החנות מקבל דוח ב-WhatsApp

### בעל החנות עונה ב-WhatsApp:
- **1** = אישור המלצות
- **2** = שליחת הזמנות אוטומטיות לספקים שלו! 📦
- **3** = דחייה

### תשלום (אחרי 30 יום):
שלח לו את הלינק: `https://YOUR-REPL.repl.co/subscribe/0501234567/basic`
→ דף תשלום Stripe → מנוי פעיל

---

## פורמט CSV מומלץ

| שם מוצר | כמות נמכרת | תאריך | מחיר | מלאי נוכחי | ספק |
|---|---|---|---|---|---|
| שמן זית 750 | 0 | 01/05/2026 | 24.50 | 23 | אסם |
| חלב 3% | 45 | 22/05/2026 | 6.90 | 12 | תנובה |

ככל שיש יותר עמודות — הניתוח מדויק יותר. מינימום: שם מוצר, כמות, תאריך.

---

## מה הלאה (לא דחוף)
- [ ] החלפת JSON ב-Supabase (כשיש 10+ חנויות)
- [ ] שליפה אוטומטית מאימייל חשבשבת
- [ ] דאשבורד ויזואלי (יש לך כבר את ה-HTML!)
- [ ] WhatsApp Templates מאושרים ב-Meta
