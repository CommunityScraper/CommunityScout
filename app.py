"""
X Community Scout - v2 (AI Edition)
=====================================
Upgrades over v1:
  - X API v2 (Bearer Token) for reliable tweet search — no more cookie scraping
  - Falls back to twikit cookies if X API not configured
  - Claude AI scores every community for relevance + signal quality
  - Each community gets: score (0-10), signal label, one-line summary
  - High-score communities (7+) flagged as HOT ALPHA

Environment variables:
  X_BEARER_TOKEN       — X API v2 Bearer Token (get from developer.twitter.com)
  ANTHROPIC_API_KEY    — Claude API key
  ACCOUNT1_AUTH_TOKEN  — X cookie fallback (if no X API)
  ACCOUNT1_CT0         — X cookie fallback (if no X API)
  SCAN_INTERVAL        — seconds between scans (default 120)
"""

import asyncio
import json
import os
import re
import time
import threading
import requests as req
from flask import Flask, jsonify, request
from flask_cors import CORS

# ── Optional imports ──────────────────────────────────────────────────
try:
    from twikit import Client as TwikitClient
    HAS_TWIKIT = True
except ImportError:
    HAS_TWIKIT = False

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
X_BEARER_TOKEN   = os.environ.get("X_BEARER_TOKEN", "").strip()
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# Mutable config so we can disable X API at runtime without global keyword
_config = {"x_bearer": X_BEARER_TOKEN}
SCAN_INTERVAL    = int(os.environ.get("SCAN_INTERVAL", 120))
QUERY_DELAY      = 3
RATE_LIMIT_PAUSE = 90
MAX_FEED_SIZE    = 500
HOT_SCORE        = 7    # communities scoring >= this get "HOT ALPHA" flag

FRESH_QUERIES = [
    'x.com/i/communities memecoin',
    'x.com/i/communities solana',
    'x.com/i/communities pumpfun',
    'x.com/i/communities crypto token',
    'x.com/i/communities coin',
    'created community x.com/i/communities crypto',
    'new community x.com/i/communities memecoin',
    'x.com/i/communities trading',
    'x.com/i/communities defi',
]

# Cookie accounts (fallback if no X API)
def _build_accounts():
    accounts = []
    for i in range(1, 6):
        token    = os.environ.get(f"ACCOUNT{i}_AUTH_TOKEN", "").strip()
        ct0      = os.environ.get(f"ACCOUNT{i}_CT0", "").strip()
        username = os.environ.get(f"ACCOUNT{i}_USERNAME", "").strip()
        password = os.environ.get(f"ACCOUNT{i}_PASSWORD", "").strip()
        email    = os.environ.get(f"ACCOUNT{i}_EMAIL", "").strip()
        if (token and ct0) or (username and password):
            accounts.append({
                "label":      f"Account {i}",
                "auth_token": token,
                "ct0":        ct0,
                "username":   username,
                "password":   password,
                "email":      email,
            })
    if not accounts:
        accounts = [{
            "label":      "Account 1",
            "auth_token": os.environ.get("AUTH_TOKEN", "").strip(),
            "ct0":        os.environ.get("CT0", "").strip(),
            "username":   os.environ.get("X_USERNAME", "").strip(),
            "password":   os.environ.get("X_PASSWORD", "").strip(),
            "email":      os.environ.get("X_EMAIL", "").strip(),
        }]
    return accounts

ACCOUNTS = _build_accounts()

# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════
SEEN_FILE = "seen_communities.json"

def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except: pass
    return default

def _save_json(path, data):
    try:
        with open(path, "w") as f: json.dump(data, f)
    except: pass

seen_ids = set(_load_json(SEEN_FILE, []))

# ═══════════════════════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════════════════════
discoveries  = []
scan_lock    = threading.Lock()
last_scan_at = 0
scans_run    = 0
total_found  = 0
ai_scored    = 0

# ═══════════════════════════════════════════════════════════════════════════════
# ACCOUNT ROTATOR (cookie fallback)
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
        self.cooldowns[label] = time.time() + RATE_LIMIT_PAUSE
        self.idx = (idx + 1) % len(self.accounts)
        print(f"[ROTATOR] {label} cooling down {RATE_LIMIT_PAUSE}s")

    def status(self):
        now = time.time()
        return [{
            "label":    a["label"],
            "active":   i == self.idx,
            "limited":  self.cooldowns.get(a["label"], 0) > now,
            "cooldown": max(0, int(self.cooldowns.get(a["label"], 0) - now)),
        } for i, a in enumerate(self.accounts)]

rotator = Rotator(ACCOUNTS) if ACCOUNTS else None

COMMUNITY_RE = re.compile(r'x\.com/i/communities/(\d{10,25})')

def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(None)

# ═══════════════════════════════════════════════════════════════════════════════
# X API v2 SEARCH (primary method)
# ═══════════════════════════════════════════════════════════════════════════════
def xapi_search(query, max_results=20):
    """Search recent tweets via X API v2 Bearer Token."""
    if not _config["x_bearer"]:
        return []
    try:
        url = "https://api.twitter.com/2/tweets/search/recent"
        params = {
            "query":        query,
            "max_results":  max_results,
            "tweet.fields": "text,author_id,created_at,entities",
            "expansions":   "author_id",
            "user.fields":  "username,public_metrics",
        }
        headers = {"Authorization": f"Bearer {_config['x_bearer']}"}
        r = req.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 401:
            print(f"[XAPI] 401 Unauthorized — disabling X API, falling back to twikit.")
            _config["x_bearer"] = ""
            return []
        if r.status_code == 429:
            print(f"[XAPI] Rate limited")
            return []
        if r.status_code != 200:
            print(f"[XAPI] Error {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        tweets = data.get("data", [])
        users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
        results = []
        for t in tweets:
            author = users.get(t.get("author_id", ""), {})
            results.append({
                "text":      t.get("text", ""),
                "author":    author.get("username", "unknown"),
                "followers": author.get("public_metrics", {}).get("followers_count", 0),
                "created":   t.get("created_at", ""),
            })
        return results
    except Exception as e:
        print(f"[XAPI] Exception: {e}")
        return []

# ═══════════════════════════════════════════════════════════════════════════════
# TWIKIT LOGIN-BASED CLIENT (more reliable on cloud servers than cookies)
# ═══════════════════════════════════════════════════════════════════════════════
_twikit_client  = None
_twikit_ready   = False
COOKIES_FILE    = "twikit_cookies.json"

async def _init_twikit():
    """Login with username/password or load saved cookies."""
    global _twikit_client, _twikit_ready
    if not HAS_TWIKIT or not ACCOUNTS:
        return False

    acc = ACCOUNTS[0]
    username = acc.get("username", "")
    password = acc.get("password", "")
    email    = acc.get("email", "")

    c = TwikitClient(language="en-US")

    # Try saved cookies first (avoids repeated logins)
    if os.path.exists(COOKIES_FILE):
        try:
            c.load_cookies(COOKIES_FILE)
            # Quick test
            await c.search_tweet("test", product="Latest")
            _twikit_client = c
            _twikit_ready  = True
            print("[TWIKIT] Loaded saved cookies ✓")
            return True
        except Exception:
            print("[TWIKIT] Saved cookies expired, re-logging in...")

    # Fall back to raw cookies from env
    if acc.get("auth_token") and acc.get("ct0"):
        try:
            c2 = TwikitClient(language="en-US")
            c2.set_cookies({"auth_token": acc["auth_token"], "ct0": acc["ct0"]})
            _twikit_client = c2
            _twikit_ready  = True
            print("[TWIKIT] Using env cookies ✓")
            return True
        except Exception as e:
            print(f"[TWIKIT] Cookie setup failed: {e}")

    # Try username/password login if credentials provided
    if username and password:
        try:
            await c.login(
                auth_info_1=username,
                auth_info_2=email or username,
                password=password
            )
            c.save_cookies(COOKIES_FILE)
            _twikit_client = c
            _twikit_ready  = True
            print("[TWIKIT] Logged in with credentials ✓")
            return True
        except Exception as e:
            print(f"[TWIKIT] Login failed: {e}")

    return False

def ensure_twikit():
    """Ensure twikit client is ready, blocking until done."""
    global _twikit_ready
    if not _twikit_ready:
        try:
            result = run_async(_init_twikit())
            if not result:
                print("[TWIKIT] Could not initialize — check credentials")
        except Exception as e:
            print(f"[TWIKIT] Init error: {e}")

async def twikit_search(query):
    """Search via twikit — fresh client per call to avoid stale event loop."""
    if not HAS_TWIKIT or not ACCOUNTS:
        return []
    acc = ACCOUNTS[0]
    try:
        c = TwikitClient(language="en-US")
        c.set_cookies({"auth_token": acc["auth_token"], "ct0": acc["ct0"]})
        results = await c.search_tweet(query, product="Latest")
        tweets = []
        for t in results:
            text = getattr(t, "text", "") or ""
            expanded = ""
            if hasattr(t, "urls") and t.urls:
                expanded = " ".join(
                    u.get("expanded_url", "") or u.get("url", "")
                    for u in (t.urls if isinstance(t.urls, list) else [])
                )
            elif hasattr(t, "entities") and t.entities:
                ents = t.entities if isinstance(t.entities, dict) else {}
                urls = ents.get("urls", [])
                expanded = " ".join(u.get("expanded_url", "") or u.get("url", "") for u in urls)
            tweets.append({"text": text + " " + expanded, "author": "", "followers": 0, "created": ""})
        return tweets
    except Exception as e:
        err = str(e)
        if '429' in err or 'rate limit' in err.lower():
            if rotator: rotator.throttle(0)
        elif '404' in err:
            pass
        else:
            print(f"[TWIKIT] Search error: {e}")
        return []

def search_tweets(query):
    """Use X API if available, else twikit."""
    if _config["x_bearer"]:
        results = xapi_search(query)
        print(f"[XAPI] '{query}' → {len(results)} tweets")
        return results
    else:
        ensure_twikit()
        results = run_async(twikit_search(query))
        print(f"[TWIKIT] '{query}' → {len(results)} tweets")
        return results

# ═══════════════════════════════════════════════════════════════════════════════
# CLAUDE AI SCORING
# ═══════════════════════════════════════════════════════════════════════════════
_anthropic_client = None

def get_anthropic():
    global _anthropic_client
    if not HAS_ANTHROPIC or not ANTHROPIC_KEY:
        return None
    if not _anthropic_client:
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    return _anthropic_client

def ai_score_community(tweet_text, source_query, author="", followers=0):
    """
    Ask Claude to score a community from 0-10 for memecoin trader relevance.
    Returns dict with score, label, summary.
    """
    global ai_scored
    client = get_anthropic()
    if not client:
        return None

    prompt = f"""You are analyzing an X (Twitter) community link found in a tweet for a memecoin trader.

Tweet text: {tweet_text[:500]}
Found via query: {source_query}
Tweet author followers: {followers}

Score this community from 0 to 10 for a memecoin trader based on:
- How new/fresh it likely is (newer = higher score)
- How relevant to crypto/memecoin trading
- Signal quality (is this a real community launch or just noise?)
- Author credibility (follower count, language used)

Respond in JSON only, no other text:
{{
  "score": <0-10 integer>,
  "label": "<one of: HOT ALPHA | PROMISING | NEUTRAL | LOW SIGNAL>",
  "summary": "<one sentence: what this community appears to be about>"
}}"""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        # Strip markdown if present
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        ai_scored += 1
        return result
    except Exception as e:
        print(f"[AI] Scoring error: {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# CORE SCAN
# ═══════════════════════════════════════════════════════════════════════════════
def scan_for_fresh():
    global total_found

    found_ids    = set()
    new_this_scan = []

    for query in FRESH_QUERIES:
        try:
            tweets = search_tweets(query)

            for tweet_data in tweets:
                text      = tweet_data.get("text", "")
                author    = tweet_data.get("author", "")
                followers = tweet_data.get("followers", 0)

                for cid in COMMUNITY_RE.findall(text):
                    if cid in found_ids or cid in seen_ids:
                        continue
                    found_ids.add(cid)

                    # AI scoring (async in background thread to not block scan)
                    ai = None
                    if ANTHROPIC_KEY:
                        ai = ai_score_community(text, query, author, followers)

                    score   = ai.get("score", 5) if ai else None
                    label   = ai.get("label", "") if ai else ""
                    summary = ai.get("summary", "") if ai else ""
                    is_hot  = score is not None and score >= HOT_SCORE

                    community = {
                        "id":       cid,
                        "url":      f"https://x.com/i/communities/{cid}",
                        "tweet":    text[:280],
                        "author":   author,
                        "followers":followers,
                        "found_at": int(time.time()),
                        "source":   query.split(' x.com')[0][:40],
                        # AI fields
                        "score":    score,
                        "label":    label,
                        "summary":  summary,
                        "is_hot":   is_hot,
                    }

                    seen_ids.add(cid)
                    _save_json(SEEN_FILE, list(seen_ids))

                    with scan_lock:
                        existing = {d["id"] for d in discoveries}
                        if cid not in existing:
                            discoveries.insert(0, community)
                            if len(discoveries) > MAX_FEED_SIZE:
                                discoveries.pop()
                            total_found += 1

                    new_this_scan.append(community)
                    score_str = f" [AI:{score}/10 {label}]" if score is not None else ""
                    print(f"[NEW] {cid}{score_str}")

        except Exception as e:
            print(f"[SCAN] Error on '{query}': {e}")

        time.sleep(QUERY_DELAY)

    return new_this_scan

# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND LOOP
# ═══════════════════════════════════════════════════════════════════════════════
def scanner_loop():
    global last_scan_at, scans_run
    mode = "X API v2" if _config["x_bearer"] else "twikit"
    ai   = "Claude AI scoring ON" if ANTHROPIC_KEY else "no AI scoring"
    print(f"[SCANNER] Started — {mode} | {ai} | every {SCAN_INTERVAL}s")

    # Pre-initialize twikit before first scan
    if not _config["x_bearer"]:
        ensure_twikit()

    while True:
        print(f"[SCANNER] Scan #{scans_run + 1}...")
        try:
            found        = scan_for_fresh()
            scans_run   += 1
            last_scan_at = int(time.time())
            hot          = sum(1 for c in found if c.get("is_hot"))
            print(f"[SCANNER] Done — {len(found)} new ({hot} hot), {total_found} total, {ai_scored} AI scored")
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
    return jsonify({"name": "X Community Scout API", "status": "ok", "version": "2.0"})

@app.route("/api/health")
def health():
    accs = rotator.status() if rotator else []
    return jsonify({
        "status":      "ok",
        "version":     "2.0",
        "mode":        "xapi" if _config["x_bearer"] else "twikit",
        "ai_scoring":  bool(ANTHROPIC_KEY),
        "scans_run":   scans_run,
        "total_found": total_found,
        "ai_scored":   ai_scored,
        "discoveries": len(discoveries),
        "last_scan":   last_scan_at,
        "next_scan":   max(0, int(last_scan_at + SCAN_INTERVAL - time.time())),
        "accounts":    accs,
        "all_limited": all(a["limited"] for a in accs) if accs else False,
    })

@app.route("/api/discoveries")
def get_discoveries():
    limit    = min(int(request.args.get("limit", 100)), 500)
    since    = int(request.args.get("since", 0))
    sort     = request.args.get("sort", "newest")
    hot_only = request.args.get("hot_only", "false").lower() == "true"

    with scan_lock:
        data = list(discoveries)

    if since:
        data = [c for c in data if c["found_at"] > since]
    if hot_only:
        data = [c for c in data if c.get("is_hot")]
    if sort == "oldest":
        data.sort(key=lambda x: x["found_at"])
    elif sort == "score":
        data.sort(key=lambda x: x.get("score") or 0, reverse=True)

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
            found        = scan_for_fresh()
            scans_run   += 1
            last_scan_at = int(time.time())
        except Exception as e:
            print(f"[MANUAL] Error: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "scan started"})

@app.route("/api/communities")
def search_communities():
    keyword = request.args.get("q", "memecoin")
    if _config["x_bearer"]:
        # Use X API
        tweets = xapi_search(f"x.com/i/communities {keyword}", max_results=20)
        found = []
        seen = set()
        for t in tweets:
            for cid in COMMUNITY_RE.findall(t["text"]):
                if cid in seen: continue
                seen.add(cid)
                ai = ai_score_community(t["text"], keyword, t["author"], t["followers"]) if ANTHROPIC_KEY else None
                found.append({
                    "id":      cid,
                    "url":     f"https://x.com/i/communities/{cid}",
                    "tweet":   t["text"][:280],
                    "author":  t["author"],
                    "found_at":int(time.time()),
                    "source":  keyword,
                    "score":   ai.get("score") if ai else None,
                    "label":   ai.get("label", "") if ai else "",
                    "summary": ai.get("summary", "") if ai else "",
                    "is_hot":  (ai.get("score", 0) >= HOT_SCORE) if ai else False,
                })
        return jsonify({"data": found, "total": len(found)})
    else:
        # Twikit fallback
        async def _search():
            if not HAS_TWIKIT or not ACCOUNTS: return []
            idx = rotator.next()
            acc = ACCOUNTS[idx]
            c = TwikitClient(language="en-US")
            c.set_cookies({"auth_token": acc["auth_token"], "ct0": acc["ct0"]})
            try:
                results = await c.search_community(keyword)
            except Exception as e:
                if '429' in str(e): rotator.throttle(idx)
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
                    "score":       None,
                    "label":       "",
                    "summary":     "",
                    "is_hot":      False,
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
    counts = {}
    for kw in keywords[:3]:
        tweets = search_tweets(f"#{kw.strip()} lang:en -is:retweet")
        for t in tweets:
            for word in t["text"].split():
                if word.startswith("#") and len(word) > 3:
                    tag = word.strip("#.,!?()[]").lower()
                    if tag: counts[tag] = counts.get(tag, 0) + 1
    data = [{"tag": f"#{t}", "count": n}
            for t, n in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:20]]
    return jsonify({"data": data})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🚀 X Community Scout v2")
    print(f"   Mode:       {'X API v2' if _config["x_bearer"] else 'twikit cookies'}")
    print(f"   AI Scoring: {'ON (Claude)' if ANTHROPIC_KEY else 'OFF'}")
    print(f"   Accounts:   {len(ACCOUNTS)}")
    print(f"   Port:       {port}\n")
    app.run(host="0.0.0.0", debug=False, port=port)
