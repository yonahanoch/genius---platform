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

**טיפ:** התחל רק עם ANTHROPIC_API_KEY. השאר ירוץ במצב דמו — מדפיס לקונסול במקום לשלוח.

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
4. Webhooks → הוסף: `https://YOUR-REPL.repl.co/webhook/stripe`

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
