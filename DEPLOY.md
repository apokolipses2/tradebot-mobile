# Deploy TradeBot Mobile (free, PC-off, phone-friendly)

Everything is pre-built. You do the account clicks; Claude runs the git commands.

## Stage 1 — GitHub (stores the code online)
1. Go to https://github.com  →  sign up (free) if you don't have an account.
2. Click the **+** (top-right) → **New repository**.
3. Repository name: `tradebot-mobile`
4. Leave it **Public**. Do **NOT** check "Add a README" / .gitignore / license (keep it empty).
5. Click **Create repository**.
6. Copy the URL shown (looks like `https://github.com/YOURNAME/tradebot-mobile.git`).
7. **Paste that URL to Claude** — Claude pushes the code up (a browser login popup will appear; log in to GitHub in it).

## Stage 2 — Render (runs it 24/7 for free)
1. Go to https://render.com → **Get Started** → **Sign in with GitHub** (one click).
2. Click **New +** → **Web Service**.
3. Connect / pick the **tradebot-mobile** repo → **Connect**.
4. Render auto-detects the settings from `render.yaml`. Make sure plan = **Free**.
5. Click **Create Web Service** → wait ~2-3 minutes for it to build.
6. When it's live, Render shows a URL like `https://tradebot-mobile.onrender.com`.

## Done
Open that Render URL on your phone — from anywhere, PC off. Bookmark it / add to home screen.

Note: on the FREE plan it "sleeps" after ~15 min unused, so the first open after a
while takes ~30-60 seconds to wake up, then it's fast. That's the trade-off for $0.
