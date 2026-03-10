# X Community Scout 🔍

Discover new X (Twitter) Communities and trending crypto/memecoin hashtags — no paid API required.

---

## What You Get

| File       | Purpose |
|------------|---------|
| `app.py`   | Python Flask backend — fetches X data using **twikit** |
| `index.html` | Beautiful web dashboard to view results |

---

## Setup (5 minutes)

### 1. Install dependencies
```bash
pip install flask flask-cors twikit
```

### 2. Set your X credentials in `app.py`
Open `app.py` and edit these three lines near the top:
```python
X_USERNAME = "your_x_username"
X_EMAIL    = "your_email@example.com"
X_PASSWORD = "your_password"
```
> **Note:** On first run, twikit will log in and save a `cookies.json` file.  
> Subsequent runs reuse the saved session — no repeated logins.

### 3. Start the backend
```bash
python app.py
```
You should see:
```
🚀 X Community Scout API running at http://localhost:5000
```

### 4. Open the dashboard
Just open `index.html` in your browser. No server needed for the frontend.

---

## Usage

- **Search box** — type any keyword to find matching X Communities
- **Quick chips** — one-click presets for memecoin, solana, crypto, defi, nft, pump
- **Scan button** — hits the backend and returns results
- **Trending Hashtags** — auto-loaded from recent crypto tweets
- **Settings (⚙)** — change the backend URL if needed

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Check if backend is running |
| GET | `/api/communities?q=memecoin` | Search X communities by keyword |
| GET | `/api/trending-hashtags?keywords=memecoin,solana` | Get trending hashtags |
| GET | `/api/community/<id>/tweets` | Recent tweets from a community |

---

## Notes & Limits

- twikit uses X's **internal GraphQL API** — no paid API key needed
- Requires a valid X account login
- Respect X's rate limits — don't hammer the scan button repeatedly
- `cookies.json` is created after first login — keep it private
- X can change their internal API at any time, which may break twikit temporarily

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Backend offline` in dashboard | Make sure `python app.py` is running |
| `twikit not installed` warning | Run `pip install twikit` |
| Login fails | Double-check username/email/password in `app.py` |
| No communities found | Try different keywords; some niches have fewer communities |
| Rate limit errors | Wait a few minutes before scanning again |
