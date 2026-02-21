# Community Scout 🔭

**Find brand new X Communities the moment they're created.**

Scans live tweets for community links every 2 minutes. Built for memecoin traders who need to be first.

---

## How It Works

1. Searches X for tweets containing community links using announcement patterns
2. Extracts community IDs from tweets in real time
3. Displays them in a live feed — newest first
4. No X API key needed — uses cookie-based auth

---

## Local Development

**1. Install dependencies**
```bash
pip install -r requirements.txt
```

**2. Set your X cookies**

Get these from x.com → F12 → Application → Cookies → copy `auth_token` and `ct0`.

Set as environment variables:
```bash
# Windows
set ACCOUNT1_AUTH_TOKEN=your_token_here
set ACCOUNT1_CT0=your_ct0_here

# Mac/Linux
export ACCOUNT1_AUTH_TOKEN=your_token_here
export ACCOUNT1_CT0=your_ct0_here
```

**3. Run**
```bash
python app.py
```

Open `index.html` in your browser. Backend runs on `http://localhost:5000`.

---

## Deploy to Railway (Free, ~5 minutes)

### Backend

1. Push this folder to a GitHub repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
3. Add environment variables in the Railway dashboard:
   ```
   ACCOUNT1_AUTH_TOKEN = your_auth_token
   ACCOUNT1_CT0        = your_ct0
   ```
4. Railway reads the `Procfile` automatically and deploys
5. You get a URL like `https://community-scout-production.up.railway.app`

### Frontend

1. Open `index.html` and find this line near the bottom:
   ```js
   const API_BASE = window.API_BASE || 'http://localhost:5000';
   ```
   Replace with your Railway URL:
   ```js
   const API_BASE = window.API_BASE || 'https://your-app.up.railway.app';
   ```
2. Go to [netlify.com](https://netlify.com) → drag and drop `index.html` onto the deploy area
3. Netlify gives you a live URL instantly

**Total cost: $0**

---

## Adding More Accounts (Faster Scanning)

Each account = more scans per minute. Add them as env vars:

```
ACCOUNT1_AUTH_TOKEN=...
ACCOUNT1_CT0=...
ACCOUNT2_AUTH_TOKEN=...
ACCOUNT2_CT0=...
ACCOUNT3_AUTH_TOKEN=...
ACCOUNT3_CT0=...
```

The rotator switches automatically when one hits a rate limit.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ACCOUNT1_AUTH_TOKEN` | ✅ | X auth_token cookie |
| `ACCOUNT1_CT0` | ✅ | X ct0 cookie |
| `ACCOUNT2_AUTH_TOKEN` | ➕ | Second account (optional) |
| `ACCOUNT2_CT0` | ➕ | Second account (optional) |
| `SCAN_INTERVAL` | ➕ | Seconds between scans (default: 120) |
| `PORT` | auto | Set by Railway automatically |

---

## Stack

- **Backend:** Python, Flask, twikit
- **Frontend:** Single HTML file, no build step, no dependencies
- **Deploy:** Railway + Netlify
