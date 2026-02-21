"""
X Community Scout - Backend
============================
Finds brand new X Communities the moment they're created,
by scanning tweets for community links in real time.

Deploy on Railway / Render / any VPS.
Set environment variables:
  ACCOUNT1_AUTH_TOKEN  — X auth_token cookie
  ACCOUNT1_CT0         — X ct0 cookie
  ACCOUNT2_AUTH_TOKEN  — (optional) second account
  ACCOUNT2_CT0         — (optional) second account
"""

import asyncio
import json
import os
import re
import time
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS
from twikit import Client

# ═══════════════════════════════════════════════════════════════════════════════
# ACCOUNTS — loaded from environment variables (safe for deployment)
# Locally: set them in a .env file or just paste values below for testing
# ═══════════════════════════════════════════════════════════════════════════════
def _build_accounts():
    accounts = []
    for i in range(1, 6):  # support up to 5 accounts
        token = os.environ.get(f"ACCOUNT{i}_AUTH_TOKEN")
        ct0   = os.environ.get(f"ACCOUNT{i}_CT0")
        if token and ct0 and token != "your_token_here":
            accounts.append({
                "label":      f"Account {i}",
                "auth_token": token,
                "ct0":        ct0,
            })
    if not accounts:
        # Fallback for local dev — paste here only, never commit
        accounts = [{
            "label":      "Account 1",
            "auth_token": os.environ.get("AUTH_TOKEN", "PASTE_HERE"),
            "ct0":        os.environ.get("CT0", "PASTE_HERE"),
        }]
    return accounts

ACCOUNTS = _build_accounts()

# ═══════════════════════════════════════════════════════════════════════════════
# TUNING
# ═══════════════════════════════════════════════════════════════════════════════
SCAN_INTERVAL    = int(os.environ.get("SCAN_INTERVAL", 120))
QUERY_DELAY      = 3
RATE_LIMIT_PAUSE = 90
MAX_FEED_SIZE    = 500   # max communities to keep in memory

FRESH_QUERIES = [
    'just created community x.com/i/communities',
    'new community x.com/i/communities memecoin',
    'new community x.com/i/communities crypto',
    'community is live x.com/i/communities',
    'join my community x.com/i/communities',
    'created a community x.com/i/communities',
    'x.com/i/communities memecoin',
    'x.com/i/communities solana',
    'x.com/i/communities pump',
]

# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════
SEEN_FILE = "seen_communities.json"

def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default

def _save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

seen_ids = set(_load_json(SEEN_FILE, []))

# ═══════════════════════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════════════════════
discoveries  = []
scan_lock    = threading.Lock()
last_scan_at = 0
scans_run    = 0
total_found  = 0

# ═══════════════════════════════════════════════════════════════════════════════
# ACCOUNT ROTATOR
# ═══════════════════════════════════════════════════════════════════════════════
class Rotator:
    def __init__(self, accounts):
        self.accounts  = accounts
        self.cooldowns = {}
        self.idx       = 0
        self.lock      = threading.Lock()

    def next(self):
        with self.lock:
            now = time.time()
            for i in range(len(self.accounts)):
                idx = (self.idx + i) % len(self.accounts)
                if self.cooldowns.get(self.accounts[idx]["label"], 0) <= now:
                    self.idx = idx
                    return idx
            return min(range(len(self.accounts)),
                       key=lambda i: self.cooldowns.get(self.accounts[i]["label"], 0))

    def throttle(self, idx):
        label = self.accounts[idx]["label"]
        expiry = time.time() + RATE_LIMIT_PAUSE
        with self.lock:
            self.cooldowns[label] = expiry
            self.idx = (idx + 1) % len(self.accounts)
        print(f"[ROTATOR] {label} rate limited — {RATE_LIMIT_PAUSE}s cooldown")

    def status(self):
        now = time.time()
        return [{
            "label":    a["label"],
            "active":   i == self.idx,
            "limited":  self.cooldowns.get(a["label"], 0) > now,
            "cooldown": max(0, int(self.cooldowns.get(a["label"], 0) - now)),
        } for i, a in enumerate(self.accounts)]

rotator = Rotator(ACCOUNTS)

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
COMMUNITY_RE = re.compile(r'x\.com/i/communities/(\d{10,25})')

def make_client(idx=None):
    if idx is None:
        idx = rotator.next()
    acc = ACCOUNTS[idx]
    c = Client(language="en-US")
    c.set_cookies({"auth_token": acc["auth_token"], "ct0": acc["ct0"]})
    return c, idx

def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# ═══════════════════════════════════════════════════════════════════════════════
# CORE SCAN
# ═══════════════════════════════════════════════════════════════════════════════
async def scan_for_fresh():
    global total_found

    c, idx = make_client()
    found_ids = set()
    new_this_scan = []

    for query in FRESH_QUERIES:
        try:
            results = await c.search_tweet(query, product="Latest")
            print(f"[SCAN] '{query}' → {len(results)} tweets")

            for tweet in results:
                text = getattr(tweet, "text", "") or ""
                urls = []
                if hasattr(tweet, "urls") and tweet.urls:
                    urls = [u.get("expanded_url", "") or u.get("url", "")
                            for u in tweet.urls]
                full = text + " " + " ".join(urls)

                for cid in COMMUNITY_RE.findall(full):
                    if cid in found_ids or cid in seen_ids:
                        continue
                    found_ids.add(cid)

                    community = {
                        "id":          cid,
                        "url":         f"https://x.com/i/communities/{cid}",
                        "tweet":       text[:280],
                        "found_at":    int(time.time()),
                        "source":      query.split(' x.com')[0][:40],
                    }

                    seen_ids.add(cid)
                    _save_json(SEEN_FILE, list(seen_ids))

                    with scan_lock:
                        existing = {d["id"] for d in discoveries}
                        if cid not in existing:
                            discoveries.insert(0, community)
                            # Cap feed size
                            if len(discoveries) > MAX_FEED_SIZE:
                                discoveries.pop()
                            total_found += 1

                    new_this_scan.append(community)
                    print(f"[NEW] {cid} via '{community['source']}'")

        except Exception as e:
            err = str(e)
            if '429' in err or 'rate limit' in err.lower():
                rotator.throttle(idx)
                c, idx = make_client()
                await asyncio.sleep(5)
            elif '404' in err:
                pass  # query not supported, skip silently
            else:
                print(f"[SCAN] Error on '{query}': {e}")

        await asyncio.sleep(QUERY_DELAY)

    return new_this_scan

# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND LOOP
# ═══════════════════════════════════════════════════════════════════════════════
def scanner_loop():
    global last_scan_at, scans_run
    print(f"[SCANNER] Started — scanning every {SCAN_INTERVAL}s with {len(ACCOUNTS)} account(s)")
    while True:
        print(f"[SCANNER] Scan #{scans_run + 1} starting...")
        try:
            found = run_async(scan_for_fresh())
            scans_run   += 1
            last_scan_at = int(time.time())
            print(f"[SCANNER] Scan #{scans_run} done — {len(found)} new, {total_found} total")
        except Exception as e:
            print(f"[SCANNER] Error: {e}")
        time.sleep(SCAN_INTERVAL)

threading.Thread(target=scanner_loop, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════════════════
# FLASK
# ═══════════════════════════════════════════════════════════════════════════════
app = Flask(__name__)
CORS(app)

@app.route("/")
def index():
    return jsonify({"name": "X Community Scout API", "status": "ok", "version": "1.0"})

@app.route("/api/health")
def health():
    accs = rotator.status()
    return jsonify({
        "status":      "ok",
        "scans_run":   scans_run,
        "total_found": total_found,
        "discoveries": len(discoveries),
        "last_scan":   last_scan_at,
        "next_scan":   max(0, int(last_scan_at + SCAN_INTERVAL - time.time())),
        "accounts":    accs,
        "all_limited": all(a["limited"] for a in accs),
    })

@app.route("/api/discoveries")
def get_discoveries():
    limit      = min(int(request.args.get("limit", 100)), 500)
    since      = int(request.args.get("since", 0))   # unix timestamp — only return newer
    sort       = request.args.get("sort", "newest")

    with scan_lock:
        data = list(discoveries)

    if since:
        data = [c for c in data if c["found_at"] > since]

    if sort == "oldest":
        data.sort(key=lambda x: x["found_at"])

    return jsonify({"data": data[:limit], "total": len(data), "server_time": int(time.time())})

@app.route("/api/discoveries/clear", methods=["POST"])
def clear_discoveries():
    global discoveries
    with scan_lock:
        discoveries = []
    return jsonify({"status": "cleared"})

@app.route("/api/scan-now", methods=["POST"])
def scan_now():
    def _run():
        global last_scan_at, scans_run
        try:
            found = run_async(scan_for_fresh())
            scans_run   += 1
            last_scan_at = int(time.time())
        except Exception as e:
            print(f"[MANUAL] Error: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "scan started"})

@app.route("/api/communities")
def search_communities():
    keyword = request.args.get("q", "memecoin")
    async def _search():
        c, idx = make_client()
        try:
            results = await c.search_community(keyword)
        except Exception as e:
            err = str(e)
            if '429' in err or 'rate limit' in err.lower():
                rotator.throttle(idx)
            return []
        found = []
        for item in results:
            cid = str(getattr(item, "id", ""))
            found.append({
                "id":          cid,
                "name":        getattr(item, "name", "Unknown"),
                "description": getattr(item, "description", ""),
                "member_count":getattr(item, "member_count", 0),
                "url":         f"https://x.com/i/communities/{cid}",
                "found_at":    int(time.time()),
                "source":      keyword,
                "tweet":       "",
            })
        return sorted(found, key=lambda x: x.get("member_count") or 0)
    try:
        data = run_async(_search())
        return jsonify({"data": data, "total": len(data)})
    except Exception as e:
        return jsonify({"error": str(e), "data": []}), 500

@app.route("/api/trending-hashtags")
def trending_hashtags():
    keywords = request.args.get("keywords", "memecoin,solana").split(",")
    async def _fetch():
        c, idx = make_client()
        counts = {}
        for kw in keywords[:2]:
            try:
                results = await c.search_tweet(
                    f"#{kw.strip()} lang:en -is:retweet", product="Latest")
                for tweet in results:
                    for word in (getattr(tweet, "text", "") or "").split():
                        if word.startswith("#") and len(word) > 3:
                            tag = word.strip("#.,!?()[]").lower()
                            if tag:
                                counts[tag] = counts.get(tag, 0) + 1
            except Exception as e:
                err = str(e)
                if '429' in err or 'rate limit' in err.lower():
                    rotator.throttle(idx)
                    break
        return [{"tag": f"#{t}", "count": n}
                for t, n in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:20]]
    try:
        return jsonify({"data": run_async(_fetch())})
    except Exception as e:
        return jsonify({"error": str(e), "data": []}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🚀 X Community Scout running on port {port}")
    print(f"   Accounts: {len(ACCOUNTS)} | Scan interval: {SCAN_INTERVAL}s\n")
    app.run(host="0.0.0.0", debug=False, port=port)
