# Genius — AI Retail Network

One codebase, two faces: a **website** (installable as an app via PWA) and a
**backend** that both it and a future native mobile app will share.

```
genius-platform/
├── web/                 ← the website / PWA
│   ├── index.html       ← the whole app UI (store owner + admin + supplier)
│   ├── manifest.json    ← makes it installable ("Add to Home Screen")
│   ├── service-worker.js← offline support
│   └── js/
│       └── api.js       ← the ONE file that talks to the backend
│                           (a future React Native app reuses this same logic)
├── backend/             ← the brain
│   ├── main.py          ← Flask API: onboarding, analysis, WhatsApp, Stripe
│   └── requirements.txt
└── docs/                ← setup guides
```

## Why this structure?

**Website and app share one backend.** Today it's a website that behaves like
an app (PWA — installable, works offline, no App Store). Tomorrow, if you
build a real iOS/Android app in React Native, it calls the *exact same*
`backend/main.py` API. You never rebuild the brain — only the face.

---

## Part 1 — Run the backend (Replit)

1. Go to [replit.com](https://replit.com) → **Create Repl** → **Python**
2. Upload `backend/main.py` and `backend/requirements.txt`
3. In **Secrets** 🔒 add:
   - `ANTHROPIC_API_KEY` (required)
   - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` (optional — demo mode without them)
   - `STRIPE_SECRET_KEY` (optional — only needed for payments)
4. Click **Run**. Copy the URL Replit gives you (e.g. `https://genius.yourname.repl.co`)

## Part 2 — Connect the website to it

Open `web/index.html`, find this line near the top:

```js
window.GENIUS_API_URL = "http://localhost:8080"; // TODO: replace with your live Replit backend URL
```

Replace it with your real Replit URL. That's the only line that connects the
website to the brain.

## Part 3 — Put it on GitHub

You'll need a free GitHub account first: **[github.com/signup](https://github.com/signup)**

Once you have one:

1. Go to **github.com/new** → name it `genius-platform` → **Create repository**
2. GitHub will show you commands like these — run them from this project folder:
   ```bash
   git remote add origin https://github.com/YOUR-USERNAME/genius-platform.git
   git branch -M main
   git push -u origin main
   ```
3. To make the website live for free: repo **Settings → Pages → Deploy from
   branch → main → /web** → Save. GitHub gives you a live URL in ~1 minute.

## Part 4 — Future: a real mobile app

When you're ready for an actual App Store / Play Store app, the path is:
1. Create a React Native project
2. Copy the *logic* from `web/js/api.js` into it (fetch calls stay identical)
3. Build native screens that call the same backend

No backend changes needed — this is why the API client is a separate file.
