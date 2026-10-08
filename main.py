import asyncio
import html as _html
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor as _TPE
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, List, Dict
from urllib.parse import unquote, urljoin, urlparse, parse_qs

import cloudscraper
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# Optional: real Chrome TLS fingerprint (pip install curl_cffi). Bot works without it too.
try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

# ==========================================================
# LOGGING SETUP
# ==========================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================================
# CONFIGURATION
# ==========================================================
# SECURITY: token ab sirf environment variable se aayega (code me mat likho).
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "1714266885"))
BOT_OWNER_NAME = os.getenv("BOT_OWNER_NAME", "@Mascotchlowa")
TELEGRAM_LINK = os.getenv("TELEGRAM_LINK", "https://t.me/Mascotchlowa")
SKY_PASSWORD = os.getenv("SKY_PASSWORD", "7989")
DB_FILE = os.getenv("DB_FILE", "bot_data.db")
PROXY_URL = os.getenv("PROXY_URL", "").strip()  # e.g. http://user:pass@host:port (optional)

STOP_PROCESS: Dict[int, bool] = {}
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Cloudscraper Instance
scraper = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
)

# ==========================================================
# DATABASE HANDLER
# ==========================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS allowed_users (
            user_id INTEGER PRIMARY KEY
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO allowed_users (user_id) VALUES (?)", (ADMIN_ID,))
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT,
            count INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS custom_sites (
            domain TEXT PRIMARY KEY,
            added_by INTEGER,
            added DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS site_rules (
            domain TEXT PRIMARY KEY,
            regex TEXT,
            shapes TEXT,
            strict INTEGER DEFAULT 1,
            note TEXT,
            updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS site_cookies (
            domain TEXT PRIMARY KEY,
            cookie TEXT,
            updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_logins (
            domain TEXT PRIMARY KEY,
            url TEXT,
            payload TEXT,
            updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS prefer_hosts (
            domain TEXT PRIMARY KEY,
            hosts TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            minutes INTEGER,
            last_run REAL,
            baselined INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watch_seen (
            watch_id INTEGER,
            page_url TEXT,
            PRIMARY KEY (watch_id, page_url)
        )
    """)
    conn.commit()
    conn.close()

def set_api_login(domain: str, url: str, payload: Optional[str]):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO api_logins (domain, url, payload) VALUES (?, ?, ?) "
        "ON CONFLICT(domain) DO UPDATE SET url=excluded.url, payload=excluded.payload, updated=CURRENT_TIMESTAMP",
        (domain, url, payload))
    conn.commit()
    conn.close()

def get_api_login(domain: str):
    """-> (api_url, payload_template_or_None) ya None. xhamster mirrors ke liye same path guess."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT url, payload FROM api_logins WHERE domain = ?", (domain,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0], row[1]
    except Exception:
        pass
    if "xhamster" in domain or "xhaccess" in domain:
        return f"https://{domain}/api/front/user/login", None
    return None

def is_user_allowed(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None or user_id == ADMIN_ID

def add_user_db(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO allowed_users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def remove_user_db(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM allowed_users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_all_users() -> List[int]:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM allowed_users")
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return users

# ==========================================================
# SITE LIST + PER-SITE LOGIN COOKIES (optional for every site)
# ==========================================================
# Full domains (exact domains used in the dedicated extractors)
SITES_FULL = [
    "xhamster46.desi",      # login / logout supported (cookie)
    "rusvideos.net", "pornhd8k.me", "evooli.com", "porn4days.pw", "porneec.com",
    "redheadpornx.com", "vxxx.com", "hdporn92.com", "inxxx.com", "pornk.top",
    "24videos.space", "sex-studentki.guru", "seksvideo.tv", "russkoeporno.mobi",
    "megatube.xxx", "freshporno.org", "darknessporn.com", "bdsmx.tube",
    "85po.com", "vtrahe.to", "fak.xxx",
]
# Matched by name, so every mirror / TLD of these works (e.g. xhamster46.desi)
SITES_KEYWORD = [
    "xvideos / xvideos2", "xhamster", "xhnews", "xhaccess", "viralxxxporn", "joysporn",
    "sxyprn", "pornhub", "spankbang", "redtube", "youporn", "4tube", "fapdu",
    "i-porntv / iporntv", "hqporn", "justporn", "sexvid", "eporner", "pornorus",
    "russkoe-porno", "gotporn", "anysex", "superporn",
]

def normalize_domain(value: str) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r'^https?://', '', v)
    v = v.split('/')[0].split('?')[0].split('#')[0].split(':')[0]
    if v.startswith('www.'):
        v = v[4:]
    return v

def clean_cookie(raw: str) -> str:
    c = re.sub(r'[\r\n]+', ' ', raw or '').strip()
    c = re.sub(r'^cookie:\s*', '', c, flags=re.I)
    return c.encode('ascii', errors='ignore').decode('ascii').strip()

def parse_cookie_str(c: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in (c or "").split(';'):
        if '=' in part:
            k, v = part.split('=', 1)
            k = k.strip()
            if k:
                out[k] = v.strip()
    return out

def set_cookie_db(domain: str, cookie: str):
    try:
        globals().get("_FETCH_CACHE", {}).clear()
    except Exception:
        pass
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO site_cookies (domain, cookie) VALUES (?, ?) "
        "ON CONFLICT(domain) DO UPDATE SET cookie=excluded.cookie, updated=CURRENT_TIMESTAMP",
        (domain, cookie))
    conn.commit()
    conn.close()

def delete_cookie_db(domain: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM site_cookies WHERE domain = ?", (domain,))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed

def list_cookie_domains() -> List[str]:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT domain FROM site_cookies ORDER BY domain")
    rows = [r[0] for r in cursor.fetchall()]
    conn.close()
    return rows

def get_cookie_for_url(url: str) -> Optional[str]:
    host = normalize_domain(url)
    if not host:
        return None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT domain, cookie FROM site_cookies")
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        return None
    for d, c in rows:
        if host == d or host.endswith('.' + d):
            return c
    return None

# ---- custom sites added from the bot (/addsite) ----
DOMAIN_RE = re.compile(r'^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,}|xn--[a-z0-9-]+)$')

def env_extra_sites() -> List[str]:
    """Optional permanent sites via env var EXTRA_SITES=domain1.com,domain2.net"""
    out = []
    for x in os.getenv("EXTRA_SITES", "").split(","):
        d = normalize_domain(x)
        if d and DOMAIN_RE.match(d) and d not in out:
            out.append(d)
    return out

def add_custom_site_db(domain: str, user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO custom_sites (domain, added_by) VALUES (?, ?)", (domain, user_id))
    conn.commit()
    conn.close()

def remove_custom_site_db(domain: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM custom_sites WHERE domain = ?", (domain,))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed

def list_custom_sites_db() -> List[str]:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT domain FROM custom_sites ORDER BY added")
    rows = [r[0] for r in cursor.fetchall()]
    conn.close()
    return rows

def get_all_sites() -> List[str]:
    """Built-in full domains + EXTRA_SITES env + sites added with /addsite (no duplicates)."""
    out = list(SITES_FULL)
    for d in env_extra_sites() + list_custom_sites_db():
        if d not in out:
            out.append(d)
    return out

# ---- site-specific extractor rules (/addscr) ----
_RULES_CACHE: Optional[Dict[str, dict]] = None

def _load_rules() -> Dict[str, dict]:
    global _RULES_CACHE
    if _RULES_CACHE is None:
        rules: Dict[str, dict] = {}
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT domain, regex, shapes, strict, note FROM site_rules")
            for d, rx, sh, st, note in cursor.fetchall():
                rules[d] = {
                    "regex": json.loads(rx or "[]"),
                    "shapes": json.loads(sh or "[]"),
                    "strict": bool(st),
                    "note": note or "",
                }
            conn.close()
        except Exception as e:
            logger.error(f"load rules error: {e}")
        _RULES_CACHE = rules
    return _RULES_CACHE

def save_site_rule(domain: str, regexes: List[str], shapes: List[str], strict: bool, note: str):
    global _RULES_CACHE
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO site_rules (domain, regex, shapes, strict, note) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(domain) DO UPDATE SET regex=excluded.regex, shapes=excluded.shapes, "
        "strict=excluded.strict, note=excluded.note, updated=CURRENT_TIMESTAMP",
        (domain, json.dumps(regexes), json.dumps(shapes), int(strict), note))
    conn.commit()
    conn.close()
    _RULES_CACHE = None

def delete_site_rule(domain: str) -> bool:
    global _RULES_CACHE
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM site_rules WHERE domain = ?", (domain,))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    _RULES_CACHE = None
    return changed

def list_rule_domains() -> List[str]:
    return sorted(_load_rules().keys())

def get_site_rule(url_or_domain: str) -> Optional[dict]:
    host = normalize_domain(url_or_domain)
    if not host:
        return None
    for d, r in _load_rules().items():
        if host == d or host.endswith('.' + d):
            return r
    return None

def get_custom_headers(url: str) -> dict:
    parsed = urlparse(url)
    domain = parsed.netloc or "xvideos2.com"
    referer = f"https://{domain}/"

    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Origin": referer.rstrip('/'),
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    }

# ==========================================================
# DUMMY HTTP SERVER & AUTO-PING KEEP ALIVE (24/7)
# ==========================================================
class DummyPortServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Status: Active and Running 24/7!")

    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.getenv("PORT", 8080))
    try:
        server = HTTPServer(('0.0.0.0', port), DummyPortServer)
        logger.info(f"Dummy Web Server running on port {port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"HTTP Server Exception: {e}")

def self_ping_loop():
    render_app_url = os.getenv("RENDER_EXTERNAL_URL")
    while True:
        time.sleep(600)
        if render_app_url:
            try:
                requests.get(render_app_url, timeout=10)
                logger.info("Self-ping sent successfully!")
            except Exception as e:
                logger.error(f"Self-ping failed: {e}")

# ==========================================================
# ROBUST FETCH / LINK DISCOVERY / GENERIC EXTRACTOR
# ==========================================================
UA = DEFAULT_USER_AGENT

SKIP_EXT = ('.css', '.js', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp',
            '.ico', '.woff', '.woff2', '.xml', '.json', '.txt', '.pdf')

# listing / navigation paths that are NOT video pages
BAD_PATH = re.compile(
    r'/(login|signin|register|signup|search|tags?|categories|category|cats?|'
    r'channels?|pornstars?|creators?|playlists?|collections?|models?|actors?|studios?|sites?|dmca|contact|terms|'
    r'privacy|2257|upload|premium|history|favorites|page|blog|about|faq)(/|$)',
    re.I)

LISTING_END = re.compile(r'/(?:newest|best|trending|popular|latest|weekly|monthly|daily|alltime|top-rated)(?:/\d+)?/?$', re.I)

JUNK = re.compile(
    r'(preview|trailer|thumb|poster|sprite|\.vtt|logo|banner|/ads?/|adserver|'
    r'blank\.mp4|teaser)', re.I)

LAST_STATUS: Dict[str, int] = {}
REDIRECTED: Dict[str, str] = {}          # url -> final url (login/landing par redirect hua)
LAST_ERR: Dict[str, str] = {}            # url -> last exception (asli karan dikhane ke liye)
_URL_FIX: Dict[str, tuple] = {}          # root host -> (scheme, www?)  (jo URL form asal me chalta hai)
_ALT_TRIED: Dict[str, int] = {}
_VID_QUERY_KEYS = {'v', 'id', 'video', 'vid', 'viewkey', 'video_id', 'watch', 'view', 'clip', 'vi'}
LAST_REPORT: dict = {"pages_ok": 0, "pages_fail": [], "links": 0, "extracted": 0}


def make_headers(url: str, referer: Optional[str] = None) -> dict:
    p = urlparse(url)
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer or f"{p.scheme or 'https'}://{p.netloc}/",
    }
    cookie = get_cookie_for_url(url)   # signed-in session for this site (if any)
    if cookie:
        headers["Cookie"] = cookie
    return headers


LAST_DETAIL: Dict[str, str] = {}

# ---- fast fetch: per-thread connection reuse + remembers best engine per site ----
_TL = threading.local()
_BEST_ENGINE: Dict[str, str] = {}

# ---- per-host success/fail counters: blocked site ko jaldi pakadne ke liye ----
_HOST_STATS: Dict[str, dict] = {}


def reset_host_stats(url: str):
    _HOST_STATS.pop(_root_host(urlparse(url).netloc), None)
    EXTRACT_NOTE["v"] = ""
    _XH_EMBED.update(tried=0, ok=0)
    globals().get("_YTDLP_STATS", {}).pop(_root_host(urlparse(url).netloc), None)


def host_is_blocked(root: str) -> bool:
    """Is run me ek bhi fetch success nahi hua aur 12+ fail -> site IP block kar rahi hai."""
    st = _HOST_STATS.get(root)
    return bool(st and st["ok"] == 0 and st["fail"] >= 12)


def block_hint(url: str) -> str:
    st = _HOST_STATS.get(_root_host(urlparse(url).netloc))
    if st and st["ok"] == 0 and st["fail"] >= 3:
        return (
            "\n🚫 Site is server ki IP ko BLOCK kar rahi hai (Cloudflare / bot protection).\n"
            "Fix: 1) bot ko ghar ke PC/Indian IP par chalao (Render jaise datacenter IP aksar block hote hain), "
            "ya 2) PROXY_URL env me residential proxy do, 3) pip install curl_cffi.\n"
        )
    return ""


async def guarded_extract(v: str, s: str) -> Optional[dict]:
    """extract_video_link + hard timeout + blocked-host par turant skip."""
    if host_is_blocked(_root_host(urlparse(v).netloc)):
        return None
    try:
        return await asyncio.wait_for(extract_video_link(v, source_page=s), timeout=100)
    except asyncio.TimeoutError:
        return None


def _cffi_sess():
    s = getattr(_TL, "cffi", None)
    if s is None:
        s = cffi_requests.Session(impersonate="chrome124")
        _TL.cffi = s
    return s


def _fetch_once(url: str, referer: Optional[str] = None, use_cookie: bool = True) -> Optional[str]:
    headers = make_headers(url, referer)
    if not use_cookie:                       # saved login cookie hata do (without-login mode)
        saved = get_cookie_for_url(url)
        if saved and headers.get("Cookie"):
            rest = headers["Cookie"].replace(saved, "").strip(" ;")
            if rest:
                headers["Cookie"] = rest
            else:
                headers.pop("Cookie", None)
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    root = _root_host(urlparse(url).netloc)
    st = _HOST_STATS.get(root)
    degraded = bool(st and st["ok"] == 0 and st["fail"] >= 6)   # lagataar fail -> fast-fail mode
    tmo = (5, 8) if degraded else (6, 15)

    engines = {}
    if cffi_requests:  # best against Cloudflare (real Chrome TLS fingerprint)
        engines["curl_cffi"] = lambda: _cffi_sess().get(url, headers=headers, timeout=tmo[1] + 3, proxies=proxies)
    engines["cloudscraper"] = lambda: scraper.get(url, headers=headers, timeout=tmo, proxies=proxies)
    engines["requests"] = lambda: requests.get(url, headers=headers, timeout=tmo, proxies=proxies)

    order = list(engines)
    pref = _BEST_ENGINE.get(root)
    if pref in engines:
        order.remove(pref)
        order.insert(0, pref)
    if degraded:
        order = order[:1]

    detail = []
    for name in order:
        try:
            r = engines[name]()
            LAST_STATUS[url] = r.status_code
            if r.status_code == 200 and len(r.text) > 500:
                if re.search(r'<title>\s*(Just a moment|Attention Required|Access denied|'
                             r'Verifying|Are you a robot|DDoS)', r.text, re.I):
                    LAST_STATUS[url] = "200 (bot-challenge page)"
                    detail.append(f"{name}:challenge")
                    logger.warning(f"fetch {url} -> bot challenge page ({name})")
                    continue
                final = str(getattr(r, "url", "") or "")
                if final and _is_wall_redirect(url, final):
                    LAST_STATUS[url] = "200 (login/landing redirect)"
                    REDIRECTED[url] = final
                    detail.append(f"{name}:login-redirect")
                    break
                REDIRECTED.pop(url, None)
                LAST_DETAIL[url] = ""
                _BEST_ENGINE[root] = name
                return r.text
            detail.append(f"{name}:{r.status_code}")
            logger.warning(f"fetch {url} -> HTTP {r.status_code} ({name})")
        except Exception as e:
            detail.append(f"{name}:error")
            LAST_ERR[url] = f"{name}: {type(e).__name__}: {str(e)[:110]}"
            logger.warning(f"fetch {url} error ({name}): {e}")
    LAST_DETAIL[url] = ", ".join(detail)
    return None


def fix_url(url: str) -> str:
    """Seekha hua working form lagao (http/https, www/non-www) - sirf apex/www host par."""
    try:
        pu = urlparse(url)
        if not pu.netloc or ':' in pu.netloc:
            return url
        root = _root_host(pu.netloc)
        fx = _URL_FIX.get(root)
        host = pu.netloc.lower()
        if not fx or host not in (root, "www." + root):
            return url
        scheme, www = fx
        return pu._replace(scheme=scheme, netloc=("www." + root) if www else root).geturl()
    except Exception:
        return url


def _alt_urls(url: str) -> List[str]:
    pu = urlparse(url)
    if not pu.netloc or ':' in pu.netloc:
        return []
    host, root = pu.netloc.lower(), _root_host(pu.netloc)
    if host not in (root, "www." + root):
        return []
    other_host = root if host.startswith("www.") else "www." + root
    other_scheme = "http" if pu.scheme == "https" else "https"
    combos = [(pu.scheme, other_host), (other_scheme, host), (other_scheme, other_host)]
    return [pu._replace(scheme=sc, netloc=h).geturl() for sc, h in combos]


def _conn_failed(url: str) -> bool:
    """Saare engines exception se fail hue (koi HTTP status nahi) -> DNS/SSL/connection problem."""
    d = LAST_DETAIL.get(url, "")
    return bool(d) and "error" in d and not re.search(r':(?:\d{3}|challenge|login-redirect)\b', d)


def _fetch_alias(url: str, referer: Optional[str] = None, use_cookie: bool = True) -> Optional[str]:
    real = fix_url(url)
    html = _fetch_once(real, referer, use_cookie)
    if real != url:
        for d in (LAST_STATUS, LAST_DETAIL, LAST_ERR, REDIRECTED):
            if real in d:
                d[url] = d[real]
    return html


def _try_alt_urls(url: str, referer: Optional[str], use_cookie: bool) -> Optional[str]:
    """https<->http aur www<->non-www try karo; jo chale use yaad rakho (_URL_FIX)."""
    root = _root_host(urlparse(url).netloc)
    if root in _URL_FIX or _ALT_TRIED.get(root, 0) >= 3:
        return None
    alts = _alt_urls(url)
    if not alts:
        return None
    _ALT_TRIED[root] = _ALT_TRIED.get(root, 0) + 1
    for alt in alts:
        html = _fetch_once(alt, referer, use_cookie)
        if html:
            ap = urlparse(alt)
            _URL_FIX[root] = (ap.scheme, ap.netloc.lower().startswith("www."))
            logger.info(f"URL fix learned: {root} -> {ap.scheme}://{ap.netloc}")
            LAST_DETAIL[url] = ""
            LAST_STATUS[url] = 200
            LAST_ERR.pop(url, None)
            return html
    return None


def err_hint(url: str) -> str:
    e = (LAST_ERR.get(url) or "").lower()
    if not e:
        return ""
    if re.search(r'resolve|name or service|getaddrinfo|dns|nodename|no address', e):
        return "💡 DNS: domain resolve nahi ho raha. Spelling ya www ke saath (http://www.site.com/) try karo.\n"
    if re.search(r'ssl|certificate|handshake|tls', e):
        return "💡 SSL/https problem: site shayad sirf http:// par hai, http:// se try karo.\n"
    if re.search(r'refused|reset', e):
        return "💡 Connection refused/reset: ye scheme band hai ya IP block hai (https<->http try karo).\n"
    if re.search(r'timed out|timeout', e):
        return "💡 Timeout: site slow hai ya IP block ho sakti hai (PROXY_URL / ghar ka IP).\n"
    return ""


def fetch_sync(url: str, referer: Optional[str] = None, use_cookie: bool = True) -> Optional[str]:
    """Login cookie saved ho to pehle usse, fail ho to bina login ke bhi try (dono mode kaam karein)."""
    root = _root_host(urlparse(url).netloc)
    has_cookie = bool(get_cookie_for_url(url))
    html = _fetch_alias(url, referer, use_cookie)
    if html is None and has_cookie and use_cookie:
        first = LAST_DETAIL.get(url, "")
        html = _fetch_alias(url, referer, False)
        if html is None:
            LAST_DETAIL[url] = f"with-login[{first}] | no-login[{LAST_DETAIL.get(url, '')}]"
    if html is None and _conn_failed(url):
        html = _try_alt_urls(url, referer, use_cookie)
    st = _HOST_STATS.setdefault(root, {"ok": 0, "fail": 0})
    st["ok" if html else "fail"] += 1
    return html


async def fetch(url: str, referer: Optional[str] = None, use_cookie: bool = True) -> Optional[str]:
    return await asyncio.to_thread(fetch_sync, url, referer, use_cookie)


def _shape(pu) -> str:
    segs = []
    for s in pu.path.split('/'):
        if not s:
            continue
        s = re.sub(r'\.(?:html?|php)$', '', s, flags=re.I)
        if s.isdigit():
            segs.append('{n}')
        elif '-' in s or '_' in s or re.search(r'\d', s) or len(s) > 20:
            segs.append('{slug}')
        else:
            segs.append(s.lower())
    q = ','.join(sorted(parse_qs(pu.query)))
    return '/'.join(segs) + (('?' + q) if q else '')


def _root_host(h: str) -> str:
    h = h.lower().split(':')[0]
    parts = h.split('.')
    return '.'.join(parts[-2:]) if len(parts) >= 2 else h


def find_video_links(html: str, page_url: str, use_rule: bool = True) -> List[str]:
    root = _root_host(urlparse(page_url).netloc)
    items: Dict[str, tuple] = {}

    def add(href: str, thumb: bool):
        href = href.strip().replace('&amp;', '&').replace('\\/', '/')
        if not href or href.startswith(('javascript:', '#', 'mailto:', 'tel:', 'data:')):
            return
        full = fix_url(urljoin(page_url, href).split('#')[0])
        pu = urlparse(full)
        if pu.scheme not in ('http', 'https'):
            return
        if _root_host(pu.netloc) != root:
            return
        if full.rstrip('/') == page_url.rstrip('/'):
            return
        if pu.path in ('', '/'):      # /?v=ID jaise links (ruporn24): sirf video-id wali query allow
            if not ({k.lower() for k in parse_qs(pu.query)} & _VID_QUERY_KEYS):
                return
        if pu.path.lower().endswith(SKIP_EXT) or BAD_PATH.search(pu.path) or LISTING_END.search(pu.path):
            return
        old = items.get(full)
        items[full] = (_shape(pu), thumb or (old[1] if old else False))

    for m in re.finditer(r'<a\b[^>]*?href=(?:["\']([^"\']+)["\']|([^\s>]+))[^>]*>(.*?)</a>',
                         html, re.I | re.S):
        inner = m.group(3)
        thumb = bool(re.search(r'<img\b|data-src|data-original|poster|background-image',
                               inner, re.I))
        add(m.group(1) or m.group(2), thumb)

    if len(items) < 3:  # links hidden inside JSON / script blocks
        for m in re.finditer(r'["\'](https?:\\?/\\?/[^"\'\s<>]+)["\']', html):
            add(m.group(1), False)
        for m in re.finditer(r'["\'](\\?/[^"\'\s<>]*[-_0-9][^"\'\s<>]*)["\']', html):
            add(m.group(1), False)

    if not items:
        return []

    if use_rule:  # site-specific link shapes saved by /addscr
        rule = get_site_rule(page_url)
        if rule and rule.get("shapes"):
            wanted = set(rule["shapes"])
            ruled = [u for u, (sh, _) in items.items() if sh in wanted]
            if ruled:
                return ruled

    score, thumbs = Counter(), Counter()
    for sh, th in items.values():
        score[sh] += 1
        thumbs[sh] += int(th)
    top = max(score.values())

    def pick(min_count: int, need_thumb: bool):
        return {sh for sh, c in score.items()
                if c >= min_count and c >= 0.3 * top
                and (not need_thumb or thumbs[sh] >= 0.5 * c)}

    good = pick(3, True) or pick(3, False) or pick(2, False)
    if good:
        return [u for u, (sh, _) in items.items() if sh in good]

    th_links = [u for u, (_, t) in items.items() if t]
    return (th_links or list(items))[:60]


def link_stats(html: str, page_url: str) -> str:
    root = _root_host(urlparse(page_url).netloc)
    hrefs = re.findall(r'<a\b[^>]*?href=["\']([^"\']+)["\']', html, re.I)
    same = [h for h in hrefs
            if _root_host(urlparse(urljoin(page_url, h)).netloc) == root]
    shapes = Counter(_shape(urlparse(urljoin(page_url, h))) for h in same)
    title = re.search(r'<title>(.*?)</title>', html, re.I | re.S)
    return (
        f"📄 Title: {(title.group(1).strip()[:80] if title else 'none')}\n"
        f"🔢 <a> tags: {len(hrefs)} | same-site: {len(same)} | <img>: {len(re.findall(r'<img\b', html, re.I))}\n"
        f"🧩 Top URL shapes: {shapes.most_common(4)}\n"
        f"⚙️ <script> blocks: {len(re.findall(r'<script', html, re.I))} "
        f"(zyada script + kam <a> = JS-rendered listing)"
    )


def _looks_like_single_video(url: str) -> bool:
    pu_ = urlparse(url)
    path = pu_.path
    if path in ('', '/'):
        return bool({k.lower() for k in parse_qs(pu_.query)} & _VID_QUERY_KEYS)
    return bool(re.search(
        r'/video\.|/video\d+|/videos?/[^/]+-\d+|/(?:videos?|shorts)/[^/]+-xh[A-Za-z0-9]+/?$|'
        r'/post/\d+|/watch/|/v/|/film/|/view_video|\.html?$', path))


async def build_page_urls(url: str, start: int, end: int) -> List[str]:
    host = urlparse(url).netloc.lower()
    base = url.rstrip('/')

    if "xvideos" in host:  # 0-indexed pages
        tmpl = (url + "&p={p}") if '?' in url else (base + "/{p}")
        return [url if p == 1 else tmpl.replace('{p}', str(p - 1))
                for p in range(start, end + 1)]

    if ("xhamster" in host or "xhaccess" in host) and '?' not in url:   # pages: /, /2, /3 ... (no probing)
        base2 = re.sub(r'/\d+$', '', base)
        first = url if base2 == base else base2
        return [first if p == 1 else f"{base2}/{p}" for p in range(start, end + 1)]

    html1 = await fast_fetch(url)
    templates = []
    if html1:
        m = re.search(r'rel=["\']next["\'][^>]*href=["\']([^"\']+)|'
                      r'href=["\']([^"\']+)["\'][^>]*rel=["\']next["\']', html1, re.I)
        if m:
            href = urljoin(url, (m.group(1) or m.group(2)).replace('&amp;', '&'))
            t = re.sub(r'(?<=[/=])2(?=/|&|$)', '{p}', href, count=1)
            if '{p}' in t:
                templates.append(t)

    if html1:
        pm = re.search(r'href=["\']([^"\']*[?&](?:p|page|pg|paged|pagenum|pageno)=2(?:&[^"\']*)?)["\']', html1, re.I)
        if pm:
            href2 = urljoin(url, pm.group(1).replace('&amp;', '&'))
            t2 = re.sub(r'([?&](?:p|page|pg|paged|pagenum|pageno)=)2(?=&|$)', r'\g<1>{p}', href2, count=1)
            if '{p}' in t2 and t2 not in templates:
                templates.insert(0, t2)

    if '?' in url:
        templates.append(url + "&page={p}")
        templates.append(url + "&p={p}")
    else:
        templates += [base + "/page/{p}/", base + "/{p}/", base + "?page={p}",
                      base + "/page/{p}", base + "/{p}", base + "/?page={p}"]

    if end < 2:
        return [url]

    p1_links = set(find_video_links(html1, url)) if html1 else set()

    async def probe(t):
        u2 = t.replace('{p}', '2')
        h2 = await fast_fetch(u2)
        if not h2:
            return None
        l2 = set(find_video_links(h2, u2))
        return t if (l2 and l2 != p1_links) else None

    probes = await asyncio.gather(*[probe(t) for t in templates])
    chosen = next((t for t in probes if t), None)

    if not chosen:
        logger.warning(f"No pagination pattern found for {url}")
        return [url] if start == 1 else []

    return [url if p == 1 else chosen.replace('{p}', str(p))
            for p in range(start, end + 1)]


VIDEO_PATTERNS = [
    r'<source[^>]+?src=["\']([^"\']+)["\']',
    r'<video[^>]+?src=["\']([^"\']+)["\']',
    r'property=["\']og:video(?::url|:secure_url)?["\'][^>]+content=["\']([^"\']+)',
    r'"contentUrl"\s*:\s*"([^"]+)"',
    # KVS engine: video_url: 'function/0/https://site/get_file/...mp4/'
    r'(?:video_url|video_alt_url\d*)\s*[:=]\s*["\']([^"\']+)["\']',
    r'(?:file|src|source|stream_url|videoUrl|hls|mp4|m3u8)["\']?\s*[:=]\s*'
    r'["\']([^"\']+\.(?:m3u8|mp4)[^"\']*)["\']',
    r'data-(?:src|video|file)=["\']([^"\']+\.(?:m3u8|mp4)[^"\']*)',
    r'((?:https?:)?(?:\\?/){2}[^\s"\'<>]+?\.(?:mp4\.m3u8|m3u8|mp4)(?:\?[^\s"\'<>]*)?)',
]


def _clean(raw: str, page_url: str) -> str:
    u = (raw.replace('\\/', '/').replace('\\u002F', '/')
            .replace('\\u0026', '&').replace('&amp;', '&').strip())
    u = re.sub(r'^function/\d+/', '', u)          # KVS prefix
    if u.startswith('//'):
        u = 'https:' + u
    return urljoin(page_url, u)


def _collect_candidates(html: str, page_url: str) -> List[str]:
    out = []
    for pat in VIDEO_PATTERNS:
        for m in re.finditer(pat, html, re.I):
            u = _clean(m.group(1), page_url)
            pu = urlparse(u)
            low = u.lower()
            if pu.scheme not in ('http', 'https'):
                continue
            if low.split('?')[0].endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif',
                                           '.svg', '.vtt', '.js', '.css')):
                continue
            if not ('.mp4' in low or '.m3u8' in low or 'get_file' in low):
                continue
            if JUNK.search(low):
                continue
            if u not in out:
                out.append(u)
    return out


_PREVIEW_PATH_RX = re.compile(
    r'(preview|prevu|prevju|trailer|trejler|teaser|tizer|thumb|poster|sprite|snippet|hover|sample|promo|'
    r'kartink|/gifs?/|/img/|/images?/|/screens?/|/fotos?/|/photos?/)', re.I)
_SIGNED_RX = re.compile(r'[?&](?:sig|signature|token|exp|expires?|hash|md5|key|st|e)=', re.I)


def _rank(u: str) -> float:
    s = 0.0
    low = u.lower()
    path = low.split('?')[0]
    if _PREVIEW_PATH_RX.search(path):        # preview/teaser clip (0.5s) ko neeche karo
        s -= 5
    if _SIGNED_RX.search(low):               # signed/expiring link aksar asli video hota hai
        s += 1.5
    if '.m3u8' in path:                      # .mp4.m3u8 ab sahi se HLS count hoga
        s += 3 if 'xhcdn' in low else 1      # xhamster CDN par HLS hi chalta hai
    elif '.mp4' in path or 'get_file' in low:
        s += 2
    q = re.search(r'(\d{3,4})p', low)
    if q:
        s += int(q.group(1)) / 10000
    return -s


def probe_size_sync(url: str, referer: Optional[str] = None) -> Optional[int]:
    """File ka total size (Range 0-0). 0 = HTML/dead, None = pata nahi."""
    h = {"User-Agent": UA, "Accept": "*/*", "Range": "bytes=0-0"}
    if referer:
        h["Referer"] = referer
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    try:
        r = requests.get(url, headers=h, timeout=(5, 8), stream=True, proxies=proxies)
        try:
            if (r.headers.get("Content-Type") or "").lower().startswith("text/"):
                return 0
            m = re.search(r'/(\d+)\s*$', r.headers.get("Content-Range") or "")
            if m:
                return int(m.group(1))
            if r.status_code == 200:
                cl = r.headers.get("Content-Length")
                return int(cl) if cl and cl.isdigit() else None
            return 0 if r.status_code in (404, 410) else None
        finally:
            r.close()
    except Exception:
        return None


def stream_score(u: str, page_url: str = "") -> float:
    low = u.lower()
    pu = urlparse(u)
    path = pu.path.lower()
    s_ = 0.0
    if '.m3u8' in path:
        s_ += 6
    elif re.search(r'\.(?:mp4|webm|mkv|mov|m4v|flv)(?:$|\?)', low) or 'get_file' in low:
        s_ += 4
    if _SIGNED_RX.search(low):
        s_ += 3
    try:
        if page_url and _root_host(pu.netloc) != _root_host(urlparse(page_url).netloc):
            s_ += 1                                     # alag CDN host (ebacdn.net jaisa)
    except Exception:
        pass
    if _PREVIEW_PATH_RX.search(path):
        s_ -= 6
    if is_preferred(u, page_url):
        s_ += 10
    q = stream_quality(u)
    if q:
        s_ += min(q, 2160) / 1000
    return s_


def stream_iplock(u: str) -> str:
    m = re.search(r'[?&]ip=(\d{1,3}(?:\.\d{1,3}){3})', u)
    return m.group(1) if m else ""


def iplock_hint(results: List[dict]) -> str:
    ips = sorted({r["iplock"] for r in results if r.get("iplock")})
    if not ips:
        return ""
    n = sum(1 for r in results if r.get("iplock"))
    return (
        f"\n⚠️ {n} link IP-locked hain ({', '.join(ips)}): ye sirf usi IP/network se chalengi jahan bot chal raha hai. "
        "Player usi network se kholo, ya bot ko apne ghar ke IP par chalao."
    )


def _page_meta(text: str, link: str, page_url: str):
    return make_variants(link, text, page_url), best_thumb(text, page_url), best_duration(text)


# ---- "prefer host": user batata hai asli video kis host par hoti hai (/prefer) ----
_PREFER: Dict[str, List[str]] = {}


def load_prefers():
    _PREFER.clear()
    try:
        conn = sqlite3.connect(DB_FILE)
        for dom, hosts in conn.execute("SELECT domain, hosts FROM prefer_hosts"):
            try:
                _PREFER[_root_host(dom)] = list(json.loads(hosts or "[]"))
            except Exception:
                pass
        conn.close()
    except Exception as e:
        logger.error(f"load_prefers error: {e}")


def save_prefer(domain: str, hosts: List[str]):
    conn = sqlite3.connect(DB_FILE)
    if hosts:
        conn.execute("INSERT INTO prefer_hosts (domain, hosts) VALUES (?, ?) "
                     "ON CONFLICT(domain) DO UPDATE SET hosts=excluded.hosts", (domain, json.dumps(hosts)))
    else:
        conn.execute("DELETE FROM prefer_hosts WHERE domain=?", (domain,))
    conn.commit()
    conn.close()
    load_prefers()


def is_preferred(u: str, page_url: str = "") -> bool:
    if not _PREFER or not page_url:
        return False
    hosts = _PREFER.get(_root_host(urlparse(page_url).netloc))
    if not hosts:
        return False
    h = urlparse(u).netloc.lower().split(':')[0]
    return any(h == x or h.endswith('.' + x) for x in hosts)


# ---- dig stages: jab sirf preview mile to asli link ke liye aur kahan dekhein ----
_IFRAME_RX = re.compile(r'<(?:iframe|embed)\b[^>]*?\b(?:src|data-src|data-lazy-src|data-url)=["\']([^"\']+)["\']', re.I)
_AD_RX = re.compile(r'(doubleclick|banner|facebook|twitter|/ads?/|googlesyndication|adserver)', re.I)
_ASSET_RX = re.compile(r'\.(?:js|css|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|map)(?:\?|$)', re.I)
_DIG_STATS: Dict[str, Dict[str, list]] = {}      # root -> stage -> [ok, fail]


async def _stage_decode(text: str, video_url: str):
    dec = await asyncio.to_thread(deobfuscate_extra, text)
    urls = _quick_streams(dec, video_url) if dec else []
    return urls, f"decoded {len(dec or '')} chars -> {len(urls)} stream(s)"


async def _stage_iframes(text: str, video_url: str):
    srcs: List[str] = []
    for m in _IFRAME_RX.finditer(text):
        src = _clean(m.group(1), video_url)
        if src.startswith('http') and not _AD_RX.search(src) and src not in srcs:
            srcs.append(src)
    urls: List[str] = []
    opened = 0
    for src in srcs[:4]:
        h = await fetch(src, referer=video_url)
        if not h:
            continue
        opened += 1
        urls += await asyncio.to_thread(_quick_streams, h, src)
        d2 = await asyncio.to_thread(deobfuscate_extra, h)
        if d2:
            urls += _quick_streams(d2, src)
        if not urls:                                   # iframe ke andar iframe (1 level)
            for m2 in list(_IFRAME_RX.finditer(h))[:2]:
                s2 = _clean(m2.group(1), src)
                if s2.startswith('http') and not _AD_RX.search(s2):
                    h2 = await fetch(s2, referer=src)
                    if h2:
                        urls += await asyncio.to_thread(_quick_streams, h2, s2)
    urls = list(dict.fromkeys(urls))
    return urls, f"{len(srcs)} iframe, {opened} khule -> {len(urls)} stream(s)"


async def _stage_api(text: str, video_url: str):
    urls: List[str] = []
    notes: List[str] = []
    try:
        d = await scr_spa_discover(video_url, text, light=True)
        urls += [x["download_link"] for x in d["streams"]]
        notes.append(f"json/api {len(d['streams'])}")
    except Exception as e:
        notes.append(f"json/api error {str(e)[:40]}")
    pu = urlparse(video_url)
    ids = set(re.findall(r'[0-9a-fA-F]{8,}|\d{3,}', pu.path + " " + pu.query))
    eps: List[str] = []
    if ids:                                            # page ke script me wo URLs jinme video-id hai
        for m in re.finditer(r'["\']((?:https?:)?//[^"\'\s<>]+|/[A-Za-z0-9_\-./?=&%:]+)["\']', text):
            raw = m.group(1)
            if _ASSET_RX.search(raw) or not any(i in raw for i in ids):
                continue
            ep = urljoin(video_url, ('https:' + raw) if raw.startswith('//') else raw)
            if ep.rstrip('/') != video_url.rstrip('/') and ep not in eps:
                eps.append(ep)
    got = 0
    for ep in eps[:8]:
        body = await asyncio.to_thread(_raw_fetch_sync, ep, video_url, "application/json, text/html, */*")
        if not body:
            continue
        st = _quick_streams(body, ep)
        d3 = await asyncio.to_thread(deobfuscate_extra, body)
        if d3:
            st += _quick_streams(d3, ep)
        try:
            js, _pg = scr_json_harvest([json.loads(body.strip())], ep)
            st += [x["download_link"] for x in js]
        except Exception:
            pass
        got += len(st)
        urls += st
    notes.append(f"id-endpoints {len(eps)} -> {got} stream(s)")
    return list(dict.fromkeys(urls)), ", ".join(notes)


async def _stage_ytdlp(text: str, video_url: str):
    if not ytdlp_allowed(video_url):
        return [], "yt-dlp off/band"
    try:
        y = await asyncio.wait_for(ytdlp_try(video_url, video_url), timeout=30)
    except asyncio.TimeoutError:
        return [], "yt-dlp timeout"
    return ([y[1]] if y else []), ("yt-dlp mila" if y else "yt-dlp: kuch nahi")


async def _stage_browser(text: str, video_url: str):
    if not async_playwright:
        return [], "browser: playwright install nahi (pip install playwright && playwright install chromium)"
    try:
        html, streams = await asyncio.wait_for(pw_render(video_url, wait=4, play=True), timeout=45)
    except asyncio.TimeoutError:
        return [], "browser timeout"
    urls = list(streams)
    if html:
        urls += _quick_streams(html, video_url)
    urls = list(dict.fromkeys(urls))
    return urls, f"browser network: {len(urls)} stream(s)"


_DIG_STAGES = [("decode", _stage_decode), ("iframe", _stage_iframes), ("api", _stage_api),
               ("ytdlp", _stage_ytdlp), ("browser", _stage_browser)]


async def refine_stream(stream_link: str, cands: List[str], text: str, video_url: str):
    """Chuni hui stream agar preview/0.5s clip lage to asli video dhundo.
    -> (link, status)  status: 'ok' (jaisa tha) | 'fixed' (asli mili) | 'preview_only' (asli nahi mili)."""
    def suspicious(u: str) -> bool:
        return bool(_PREVIEW_PATH_RX.search(urlparse(u).path.lower()))

    if '.m3u8' in stream_link.lower() and not suspicious(stream_link):
        return stream_link, "ok"
    pool = list(dict.fromkeys([stream_link] + list(cands)))
    sizes: Dict[str, Optional[int]] = {}
    root = _root_host(urlparse(video_url).netloc)

    async def probe(urls: List[str]):
        todo = [u for u in urls if u not in sizes]
        res = await asyncio.gather(*[asyncio.to_thread(probe_size_sync, u, video_url) for u in todo])
        sizes.update(dict(zip(todo, res)))

    def top_prog() -> List[str]:
        prog = [u for u in pool if '.m3u8' not in u.lower()]
        return sorted(prog, key=lambda u: -stream_score(u, video_url))[:4]

    def small(u: str) -> bool:
        return sizes.get(u) is not None and sizes[u] < 1_200_000

    def pick_real() -> Optional[str]:
        good = [u for u in pool if sizes.get(u) != 0 and not suspicious(u) and not small(u)]
        pref = [u for u in good if is_preferred(u, video_url)]
        if pref:
            return max(pref, key=lambda u: stream_score(u, video_url))
        bigs = [u for u in good if '.m3u8' not in u.lower() and (sizes.get(u) or 0) >= 1_500_000]
        if bigs:
            return max(bigs, key=lambda u: (stream_score(u, video_url), sizes[u] or 0))
        hls = [u for u in good if '.m3u8' in u.lower()]
        if hls:
            return max(hls, key=lambda u: stream_score(u, video_url))
        unknown = [u for u in good if sizes.get(u) is None and u != stream_link]
        if unknown:
            best = max(unknown, key=lambda u: stream_score(u, video_url))
            if stream_score(best, video_url) > stream_score(stream_link, video_url):
                return best
        return None

    pp = [u for u in pool if is_preferred(u, video_url)]
    if pp:                                              # /prefer host wali link page me hai -> wahi
        best = max(pp, key=lambda u: stream_score(u, video_url))
        return best, ("ok" if best == stream_link else "fixed")
    if not suspicious(stream_link):
        if len(pool) < 2:
            return stream_link, "ok"
        await probe([stream_link])
        if not small(stream_link):
            return stream_link, "ok"                    # theek lag raha hai (ya size pata nahi)
    await probe(top_prog())
    real = pick_real()
    if real and real != stream_link:
        return real, "fixed"
    # ---- sirf preview/clip mili: page ke andar gehra dekho (stage by stage, host-wise seekhte hue) ----
    deadline = time.time() + 55
    stats = _DIG_STATS.setdefault(root, {})
    order = sorted(_DIG_STAGES, key=lambda st: -stats.get(st[0], [0, 0])[0])
    for name, fn in order:
        st = stats.setdefault(name, [0, 0])
        if st[0] == 0 and st[1] >= 4:                  # is host par ye stage kabhi kaam nahi aaya -> skip
            continue
        left = deadline - time.time()
        if left < 6:
            break
        try:
            urls, _note = await asyncio.wait_for(fn(text, video_url), timeout=left)
        except Exception:
            urls = []
        new = [u for u in dict.fromkeys(urls) if u not in pool]
        if new:
            pool += new
            await probe(top_prog())
        real = pick_real()
        if real and real != stream_link:
            st[0] += 1
            return real, "fixed"
        st[1] += 1
    return stream_link, ("preview_only" if (suspicious(stream_link) or small(stream_link)) else "ok")


async def generic_extract(html: str, page_url: str, depth: int = 0) -> Optional[str]:
    cands = await asyncio.to_thread(_collect_candidates, html, page_url)
    if cands:
        return sorted(cands, key=_rank)[0]

    if depth < 2:  # follow iframe embeds
        for m in re.finditer(r'<iframe[^>]+?src=["\']([^"\']+)["\']', html, re.I):
            src = _clean(m.group(1), page_url)
            if not src.startswith('http'):
                continue
            if re.search(r'(doubleclick|banner|facebook|twitter|/ads?/)', src, re.I):
                continue
            h = await fetch(src, referer=page_url)
            if h:
                r = await generic_extract(h, src, depth + 1)
                if r:
                    return r
    return None

# ==========================================================
# DEDICATED DOMAIN EXTRACTION ENGINES (43 SITES)
# ==========================================================
def xh_best_stream(text: str) -> Optional[str]:
    """xhamster: page me jitne bhi .m3u8 hain unme se best (multi= master playlist > h264 > av1)."""
    t = (text.replace('\\/', '/').replace('\\u002F', '/')
             .replace('\\u0026', '&').replace('&amp;', '&'))
    urls = re.findall(r'https?://[^\s"\'<>\\]+?\.m3u8[^\s"\'<>\\]*', t)
    best, best_score = None, -1
    for u in dict.fromkeys(urls):
        low = u.lower()
        if JUNK.search(low):
            continue
        score = 0
        if 'multi=' in low: score += 4      # quality-selector wali master playlist
        if 'xhcdn' in low: score += 1
        if '.h264.' in low: score += 2
        elif '.av1.' in low: score += 1
        if score > best_score:
            best, best_score = u, score
    return best


EXTRACT_NOTE = {"v": ""}
_XH_EMBED = {"tried": 0, "ok": 0}


def page_diag(text: str) -> str:
    """Video page me kya mila / kya nahi -> failure ka asli karan dikhane ke liye."""
    t = text or ""
    low = t.lower()
    title = re.search(r'<title[^>]*>(.*?)</title>', t, re.I | re.S)
    keys = ["initials", "xplayersettings", "unavailable", "premium", "sign in", "captcha",
            "just a moment", ".m3u8", ".mp4", "<video", "<iframe"]
    cnt = ", ".join(f"{k}={low.count(k)}" for k in keys)
    ttl = re.sub(r'\s+', ' ', title.group(1)).strip()[:70] if title else "none"
    return f"📄 {len(t)} bytes | title: {ttl}\n🔎 {cnt}"


def note_hint() -> str:
    n = EXTRACT_NOTE.get("v")
    return f"\n🔬 Last failed video page:\n{n[:700]}\n" if n else ""


def xh_from_initials(text: str) -> Optional[str]:
    """xhamster: window.initials JSON ke andar se HLS/MP4 sources (m3u8 ko priority)."""
    t = text
    for m in re.finditer(r'(?:window\.)?initials\s*=\s*\{', t):
        blob = _balanced(t, m.end() - 1)
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue
        found: List[str] = []

        def walk(o, d=0):
            if d > 16:
                return
            if isinstance(o, dict):
                for v in o.values():
                    walk(v, d + 1)
            elif isinstance(o, list):
                for v in o[:300]:
                    walk(v, d + 1)
            elif isinstance(o, str):
                low = o.lower()
                if ('.m3u8' in low or '.mp4' in low) and o.startswith(('http', '//')) and not JUNK.search(low):
                    found.append(o)

        walk(data)
        urls = [_clean(u, "https://x.invalid/") for u in found]
        hls = [u for u in urls if '.m3u8' in u.lower().split('?')[0]]
        if hls:
            return sorted(hls, key=lambda u: (0 if 'multi=' in u.lower() else 1,
                                              0 if '.h264.' in u.lower() else 1))[0]
        if urls:
            return sorted(urls, key=_rank)[0]
    return None


async def xh_embed_try(video_url: str) -> Optional[str]:
    """Video page me stream na mile to embed page try karo (sirf failure par, limited)."""
    if _XH_EMBED["tried"] >= 5 and _XH_EMBED["ok"] == 0:
        return None
    pu = urlparse(video_url)
    m = re.search(r'-((?:xh)?[A-Za-z0-9]{5,})/?$', pu.path)
    if not m:
        return None
    _XH_EMBED["tried"] += 1
    base = f"{pu.scheme}://{pu.netloc}"
    for u in (f"{base}/xembed.php?video={m.group(1)}", f"{base}/embed/{m.group(1)}"):
        h = await fetch(u, referer=video_url)
        if h:
            s_ = xh_best_stream(h) or xh_from_initials(h)
            if s_:
                _XH_EMBED["ok"] += 1
                return s_
    return None


_JUNK_TITLE = re.compile(
    r'(free online dating|adult personals|just a moment|attention required|access denied|'
    r'^\s*(?:404|403|error|not found)\b|age verification|verify your age)', re.I)


def _is_wall_redirect(url: str, final: str) -> bool:
    """Requested page se login / dating / home page par redirect hua? (login wall)"""
    a, b = urlparse(url), urlparse(final)
    if a.path in ('', '/'):
        return False
    pa, pb = a.path.rstrip('/'), b.path.rstrip('/')
    if pa == pb:
        return False
    return pb == '' or bool(re.search(r'^/(login|signin|sign-in|signup|register|dating|age|verify|gate|auth)(/|$)', pb, re.I))


def best_title(text: str) -> str:
    """og:title > h1 > <title>; HTML entities decode, site-name suffix hata do."""
    t = ""
    m = (re.search(r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)', text, re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:title["\']', text, re.I))
    if m:
        t = m.group(1)
    if not t:
        m = re.search(r'<h1[^>]*>(.*?)</h1>', text, re.I | re.S)
        if m:
            t = m.group(1)
    if not t:
        m = re.search(r'<title[^>]*>(.*?)</title>', text, re.I | re.S)
        if m:
            t = m.group(1)
    t = _html.unescape(re.sub(r'<[^>]+>', '', t))
    t = re.sub(r'\s+', ' ', t).strip()
    t = re.sub(r'\s*[|\-–—]\s*(?:xhamster|[A-Za-z0-9.\- ]{3,30}\.(?:com|net|org|xxx|tv|desi|guru|me|to|pw|top|space|tube))\s*$',
               '', t, flags=re.I).strip()
    return t[:200] or "Video"


LAST_DROP: Dict[str, object] = {}


def clean_results(items: List[dict]):
    """Junk title / duplicate hatao. Ek hi stream 3+ pages par aaye (ad/promo) to har page ki apni
    unique stream (page ke baaki candidates me se) chun kar REPAIR karo, na mile tabhi hatao.
    -> (clean, dropped).  Reasons LAST_DROP me."""
    items = [i for i in items if i]
    freq: Counter = Counter()
    by_link: Dict[str, set] = {}
    for it in items:
        for c in set(it.get("_cands") or ()):
            freq[c] += 1
        by_link.setdefault(it["download_link"], set()).add(it["page_url"])
    reasons: Counter = Counter()
    sample: Dict[str, str] = {}
    out, seen_pages, seen_links = [], set(), set()
    for it in items:
        cands = it.get("_cands") or []
        it = {k: v for k, v in it.items() if k != "_cands"}
        if _JUNK_TITLE.search(it.get("title") or ""):
            reasons["junk_title"] += 1
            sample.setdefault("title", it.get("title") or "")
            continue
        if it.get("preview_only") and get_setting("keep_preview") != "1":
            reasons["preview_only"] += 1               # sirf 0.5s preview clip mili, asli video nahi
            sample.setdefault("preview", it.get("page_url") or "")
            continue
        link = it["download_link"]
        if len(by_link[link]) >= 3:                        # shared stream: ad/promo ya galat pick
            alt = [c for c in cands if freq[c] == 1 and c != link]
            if alt:
                best = sorted(alt, key=_rank)[0]
                link = process_tpl_link(best) if ".m3u8" in best else best
                it["download_link"] = link
                it["variants"] = make_variants(link, "", it.get("page_url") or "")
                it["expires"] = stream_expiry(link)
                reasons["repaired"] += 1
            else:
                reasons["shared_stream"] += 1
                sample.setdefault("shared", by_link and link)
                continue
        if it["page_url"] in seen_pages or link in seen_links:
            reasons["duplicate"] += 1
            continue
        seen_pages.add(it["page_url"])
        seen_links.add(link)
        out.append(it)
    before = len(out)
    out = apply_filters(out)
    if before != len(out):
        reasons["filters(/settings)"] += before - len(out)
    LAST_DROP.clear()
    LAST_DROP.update(dict(reasons))
    LAST_DROP["sample"] = sample
    return out, len(items) - len(out)


def drop_hint() -> str:
    d = LAST_DROP
    if not d:
        return ""
    parts = [f"{k}={v}" for k, v in d.items() if k != "sample" and v]
    if not parts:
        return ""
    s_ = "🧹 Karan: " + ", ".join(parts) + "\n"
    smp = d.get("sample") or {}
    if smp.get("shared"):
        s_ += f"   shared stream (sab pages par same): {str(smp['shared'])[:110]}\n"
    if smp.get("title"):
        s_ += f"   junk title: {str(smp['title'])[:60]}\n"
    if smp.get("preview"):
        s_ += (f"   preview_only: sirf 0.5s clip mili, asli link page me nahi dikhi\n"
               f"   -> /sniff {str(smp['preview'])[:90]}  (report mujhe bhejo)\n")
    return s_


def login_hint(url: str) -> str:
    out = ""
    fin = REDIRECTED.get(url)
    if fin:
        out += f"\n🔐 Page login/landing par redirect ho gaya ({fin[:80]}).\n"
    path = urlparse(url).path.lower()
    if re.search(r'/(my|account|favorites?|watch-?history|watch-?later|liked|subscriptions|profile)(/|$)', path):
        has = bool(get_cookie_for_url(url))
        out += (
            "\n🔐 Ye account page hai (login chahiye). Cookie: "
            + ("saved ✅ (expire/galat ho sakti hai, /login dobara karo)" if has
               else "SAVED NAHI ❌ -> /login <domain> <cookie>")
            + "\nℹ️ Render restart/redeploy par DB reset ho jata hai; permanent ke liye env me "
              "SITE_COOKIE_1 = domain|cookie rakho.\n"
        )
    return out


def load_env_cookies():
    """Env se login cookies: SITE_COOKIE_1="domain|cookie"  ya  SITE_COOKIES='{"domain":"cookie"}'"""
    n = 0
    for k, v in os.environ.items():
        if not k.upper().startswith("SITE_COOKIE") or not (v or "").strip():
            continue
        v = v.strip()
        pairs = []
        if v.startswith("{"):
            try:
                pairs = list(json.loads(v).items())
            except Exception as e:
                logger.error(f"{k}: bad JSON ({e})")
        elif "|" in v:
            d, c = v.split("|", 1)
            pairs = [(d, c)]
        for d, c in pairs:
            d, c = normalize_domain(d), clean_cookie(str(c))
            if d and '=' in c:
                set_cookie_db(d, c)
                n += 1
    if n:
        logger.info(f"Env se {n} site cookie(s) load hui")


def xh_player_hls(text: str) -> Optional[str]:
    """xhamster: IS video ki apni player settings (xplayerSettings.sources.hls) - promo/related streams nahi."""
    for m in re.finditer(r'"xplayerSettings"\s*:\s*(\{)', text):
        blob = _balanced(text, m.start(1))
        if not blob:
            continue
        urls: List[str] = []
        try:
            data = json.loads(blob)
            hls = (data.get("sources") or {}).get("hls") or {}
            if isinstance(hls, dict):
                for codec in ("h264", "av1"):
                    o = hls.get(codec)
                    u = o.get("url") if isinstance(o, dict) else (o if isinstance(o, str) else None)
                    if u:
                        urls.append(u)
                for o in hls.values():
                    u = o.get("url") if isinstance(o, dict) else None
                    if u and u not in urls:
                        urls.append(u)
        except Exception:
            pass
        if not urls:
            urls = re.findall(r'"url"\s*:\s*"([^"]+\.m3u8[^"]*)"', blob)
        for u in urls:
            u = _clean(u, "https://x.invalid/")
            if '.m3u8' in u.lower() and not JUNK.search(u.lower()):
                return u
    return None


_QS_RX = re.compile(r'((?:https?:)?(?:\\?/){2}[^\s"\'<>]+?\.(?:mp4\.m3u8|m3u8|mp4)(?:\?[^\s"\'<>]*)?)')


def _quick_streams(text: str, page_url: str, limit: int = 12) -> List[str]:
    """Page me jitni valid stream URLs hain (ek hi pass) - shared/promo stream pakadne ke liye."""
    out: List[str] = []
    for m in _QS_RX.finditer(text):
        u = _clean(m.group(1), page_url)
        if _valid_stream_url(u) and u not in out:
            out.append(u)
            if len(out) >= limit:
                break
    return out


def _xh_parse(text: str) -> Optional[str]:
    return (xh_player_hls(text) or xh_best_stream(text)
            or (xh_from_initials(text) if 'initials' in text else None))


def process_tpl_link(hls_link: str) -> str:
    try:
        if "_TPL_" not in hls_link:
            return hls_link
        decoded_link = unquote(hls_link)
        multi_match = re.search(r'multi=([^/]+)', decoded_link)
        if multi_match:
            res_labels = re.findall(r'(\d+p)', multi_match.group(1))
            if res_labels:
                best_res = sorted(set(res_labels), key=lambda x: int(x.replace('p', '')))[-1]
                return hls_link.replace('_TPL_', best_res)
        return hls_link.replace('_TPL_', '720p')
    except Exception:
        return hls_link


def fetch_url_sync(url: str) -> Optional[str]:
    try:
        headers = get_custom_headers(url)
        resp = scraper.get(url, headers=headers, timeout=20)
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        logger.error(f"Scraper fetch error on {url}: {e}")
    return None


async def fetch_url_with_cloudscraper(url: str) -> Optional[str]:
    return await asyncio.to_thread(fetch_url_sync, url)


# ==========================================================
# /addscr  -> AUTO-BUILD A DOMAIN-SPECIFIC EXTRACTOR
# ==========================================================
IMG_EXT = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.svg', '.vtt', '.js', '.css')


def _valid_stream_url(u: str) -> bool:
    pu = urlparse(u)
    low = u.lower()
    if pu.scheme not in ('http', 'https'):
        return False
    if low.split('?')[0].endswith(IMG_EXT):
        return False
    if not ('.mp4' in low or '.m3u8' in low or 'get_file' in low):
        return False
    if JUNK.search(low):
        return False
    return True


def apply_site_rule(rule: dict, html: str, page_url: str) -> Optional[str]:
    """Runs the saved regex list on a page and returns the best stream URL."""
    strict = rule.get("strict", True)
    cands: List[str] = []
    for rx in rule.get("regex", []):
        try:
            for m in re.finditer(rx, html, re.I):
                raw = m.group(1) if m.groups() else m.group(0)
                if not raw:
                    continue
                u = _clean(raw, page_url)
                if strict:
                    ok = _valid_stream_url(u)
                else:
                    pu = urlparse(u)
                    ok = pu.scheme in ('http', 'https') and not u.lower().split('?')[0].endswith(IMG_EXT)
                if ok and u not in cands:
                    cands.append(u)
        except re.error as e:
            logger.error(f"Bad site-rule regex {rx!r}: {e}")
    if not cands:
        return None
    return sorted(cands, key=_rank)[0]


def _candidates_with_raw(html: str, page_url: str) -> List[tuple]:
    """[(raw_text_in_html, cleaned_url, pattern_index)] for every valid stream URL on a page."""
    out, seen = [], set()
    for idx, pat in enumerate(VIDEO_PATTERNS):
        for m in re.finditer(pat, html, re.I):
            raw = m.group(1)
            if not raw:
                continue
            u = _clean(raw, page_url)
            if _valid_stream_url(u) and u not in seen:
                seen.add(u)
                out.append((raw, u, idx))
    return out


def derive_regex(html: str, raw: str) -> Optional[str]:
    """Builds a precise regex from the text that sits right before the stream URL."""
    idx = html.find(raw)
    if idx < 0:
        return None
    before = html[max(0, idx - 300):idx]
    # <tag ... attr="URL"
    m = re.search(r'<([A-Za-z][\w-]*)\b[^<>]*?\s([\w:-]+)\s*=\s*["\']$', before)
    if m:
        tag, attr = m.group(1), m.group(2)
        return rf'<{re.escape(tag)}\b[^>]*?\b{re.escape(attr)}\s*=\s*["\']([^"\']+)["\']'
    # key: "URL"   /   key = 'URL'   /   video_url: 'function/0/URL'
    m = re.search(r'([A-Za-z_][\w.-]*)["\']?\s*[:=]\s*["\'](?:function/\d+/)?$', before)
    if m:
        key = m.group(1)
        return rf'\b{re.escape(key)}["\']?\s*[:=]\s*["\'](?:function/\d+/)?([^"\']+)["\']'
    return None


async def build_auto_rule(url: str, domain: str) -> dict:
    html0 = await fetch(url)
    if not html0:
        return {"ok": False, "report":
                f"❌ Fetch failed: HTTP {LAST_STATUS.get(url, '?')} ({LAST_DETAIL.get(url, '')})\n"
                f"Pehle /debug {url} se block/IP check karo."}

    path = urlparse(url).path
    start_is_video = path not in ('', '/') and _looks_like_single_video(url)

    links = [l for l in find_video_links(html0, url, use_rule=False)
             if l.rstrip('/') != url.rstrip('/')]
    how = "listing"
    if len(links) < 4:                                   # age-gate / consent page?
        try:
            got = await scr_unlock_gate(url, html0)
        except Exception:
            got = None
        if got:
            html0, url = got
            links = [l for l in find_video_links(html0, url, use_rule=False)
                     if l.rstrip('/') != url.rstrip('/')]
            how = "age-gate bypass"
    if len(links) < 2:                                   # JS site: embedded JSON / API se video pages
        try:
            d = await scr_spa_discover(url, html0, light=True)
            alt = [l for l in d["links"] if l.rstrip('/') != url.rstrip('/')]
            if len(alt) > len(links):
                links, how = alt, "JSON/API"
        except Exception:
            pass
    if len(links) < 2:                                   # sitemap.xml
        try:
            sm = await scr_sitemap(url)
            alt = sm["links"][:12]
            if len(alt) > len(links):
                links, how = alt, "sitemap"
        except Exception:
            pass
    shapes: List[str] = []
    for l in links:
        sh = _shape(urlparse(l))
        if sh not in shapes:
            shapes.append(sh)

    samples: List[tuple] = []
    if start_is_video:
        samples.append((url, html0))
        own = _shape(urlparse(url))
        if own not in shapes:
            shapes.append(own)

    pick = links[:4]
    pages = await asyncio.gather(*[fetch(l, referer=url) for l in pick])
    for l, h in zip(pick, pages):
        if h and len(samples) < 3:
            samples.append((l, h))

    if not samples:
        fails = [f"{l[:70]} -> HTTP {LAST_STATUS.get(l, '?')}" for l, h in zip(pick, pages) if not h]
        return {"ok": False, "report":
                "❌ Koi video page sample nahi mila.\n"
                f"🔗 Listing se links mile: {len(links)} ({how})\n"
                + ("🚫 Video page fetch fail:\n" + "\n".join(fails[:3]) + "\n" if fails else "")
                + "Listing me links nahi mile ya video pages fetch nahi hue.\n"
                f"Tip: kisi ek video page ka URL do: /addscr <video page URL>\n"
                f"Detail: /debug {url}"}

    regexes: List[str] = []
    note = ""
    per_page = [_candidates_with_raw(h, v) for v, h in samples]
    seed_i = next((i for i, c in enumerate(per_page) if c), None)
    if seed_i is not None:
        best = sorted(per_page[seed_i], key=lambda c: _rank(c[1]))[0]
        options = []
        drv = derive_regex(samples[seed_i][1], best[0])
        if drv:
            options.append((drv, "auto-derived regex"))
        options.append((VIDEO_PATTERNS[best[2]], f"generic pattern #{best[2]}"))
        for rx, label in options:
            hits = sum(1 for v, h in samples
                       if apply_site_rule({"regex": [rx], "strict": True}, h, v))
            if hits * 2 >= len(samples):
                regexes = [rx]
                note = f"{label} ({hits}/{len(samples)} pages)"
                break

    if not regexes:
        found_embed = False
        for v, h in samples[:2]:
            if await generic_extract(h, v):
                found_embed = True
                break
        if not found_embed:
            return {"ok": False, "report":
                    f"❌ {len(samples)} video page(s) check kiye par stream URL (.mp4/.m3u8) nahi mila.\n"
                    "Possible: JS se bana link, packed/base64 script, ya login chahiye.\n"
                    f"Dekho: /dump {samples[0][0]} -> HTML file bhejo, main exact rule bana dunga.\n"
                    f"Ya manual: /addscr {samples[0][0]} <regex with group 1 = stream URL>"}
        note = "stream embed/iframe ke andar hai (generic follower use hoga)"

    return {"ok": True, "regex": regexes, "shapes": shapes, "strict": True,
            "note": note, "test_url": samples[0][0]}


async def build_manual_rule(url: str, domain: str, rx_text: str) -> dict:
    rx_text = rx_text.strip()
    if not rx_text or len(rx_text) > 400:
        return {"ok": False, "report": "❌ Regex khali hai ya 400 chars se lamba hai."}
    try:
        re.compile(rx_text)
    except re.error as e:
        return {"ok": False, "report": f"❌ Regex galat hai: {e}"}

    path = urlparse(url).path
    if path in ('', '/'):
        return {"ok": True, "regex": [rx_text], "shapes": [], "strict": False,
                "note": "manual regex (untested)",
                "report": "⚠️ Video page URL nahi diya, isliye regex test nahi hua."}

    html = await fetch(url)
    if not html:
        return {"ok": False, "report":
                f"❌ Test page fetch failed: HTTP {LAST_STATUS.get(url, '?')} ({LAST_DETAIL.get(url, '')})"}
    link = apply_site_rule({"regex": [rx_text], "strict": False}, html, url)
    if not link:
        return {"ok": False, "report": "❌ Is regex ne diye gaye page par koi stream URL nahi nikala. Regex check karo."}
    return {"ok": True, "regex": [rx_text], "shapes": [], "strict": False,
            "note": "manual regex (tested)", "test_url": url}


async def extract_video_link(video_url: str, source_page: str = "") -> Optional[dict]:
    """Login cookie ke saath try, na mile to bina login ke try (cookie expire/galat ho tab bhi chalega)."""
    res = await _extract_video_link_impl(video_url, source_page, True)
    if res is None and get_cookie_for_url(video_url):
        res = await _extract_video_link_impl(video_url, source_page, False)
    return res


async def _extract_video_link_impl(video_url: str, source_page: str = "",
                                   use_cookie: bool = True) -> Optional[dict]:
    try:
        text = await fetch(video_url, referer=source_page or None, use_cookie=use_cookie)
        if not text:
            return None

        title = best_title(text)

        stream_link = None
        file_type = "VIDEO"
        domain = urlparse(video_url).netloc.lower()

        # Site-specific extractor saved with /addscr (tried first; xhamster uses its own HLS picker)
        rule = get_site_rule(video_url)
        if rule and rule.get("regex") and "xhamster" not in domain:
            rule_link = apply_site_rule(rule, text, video_url)
            if rule_link:
                stream_link = rule_link
                domain = ""   # rule matched -> skip the built-in domain chain

        # --------------------------------------------------
        # Site Specific Extractors (dedicated sites)
        # --------------------------------------------------
        if "xvideos" in domain or "xvideos2" in domain:
            xv_high = re.search(r'html5player\.setVideoUrlHigh\s*\(\s*["\'](https?:[^\s"\']+?)["\']\s*\)', text)
            xv_low = re.search(r'html5player\.setVideoUrlLow\s*\(\s*["\'](https?:[^\s"\']+?)["\']\s*\)', text)
            xv_hls = re.search(r'html5player\.setVideoHLS\s*\(\s*["\'](https?:[^\s"\']+?)["\']\s*\)', text)
            if xv_high: stream_link = xv_high.group(1)
            elif xv_hls: stream_link = xv_hls.group(1)
            elif xv_low: stream_link = xv_low.group(1)

        elif "viralxxxporn" in domain:
            vxp_match = re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                        re.search(r'file\s*:\s*["\'](https?:[^\s"\']+?)["\']', text)
            if vxp_match: stream_link = vxp_match.group(1)

        elif "xhnews" in domain:
            xhn_match = re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                        re.search(r'(https?:[^\s"\']*?\.m3u8[^\s"\']*)', text)
            if xhn_match: stream_link = xhn_match.group(1)

        elif "xhamster" in domain or "xhaccess" in domain:   # xhaccess = xHamster mirror
            xh_id = re.search(r'-((?:xh)?[A-Za-z0-9]{5,})/?$', urlparse(video_url).path)
            if xh_id and xh_id.group(1) not in text:
                EXTRACT_NOTE["v"] = (f"{video_url}\n⚠️ Page me video-id nahi mila "
                                     f"(login wall / landing page / redirect)\n{page_diag(text)}")
                return None
            xh_link = await asyncio.to_thread(_xh_parse, text)
            if not xh_link:
                xh_match = re.search(r'"m3u8":\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                           re.search(r'"mp4":\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                           re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text)
                if xh_match:
                    cand = xh_match.group(1).replace('\\/', '/')
                    if not JUNK.search(cand.lower()):
                        xh_link = cand
            if not xh_link:
                xh_link = await xh_embed_try(video_url)
            if xh_link:
                stream_link = xh_link

        elif "joysporn" in domain:
            jp_match = re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'video_url\s*:\s*["\'](https?:[^\s"\']+?)["\']', text)
            if jp_match: stream_link = jp_match.group(1)

        elif "xhaccess" in domain:
            xha_match = re.search(r'file\s*:\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                        re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text)
            if xha_match: stream_link = xha_match.group(1)

        elif "sxyprn" in domain:
            sxy_match = re.search(r'data-src=["\'](https?:[^\s"\']+?)["\']', text) or \
                        re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                        re.search(r'(https?:[^\s"\']*?\.mp4[^\s"\']*)', text)
            if sxy_match: stream_link = sxy_match.group(1)

        elif "pornhub" in domain:
            ph_match = re.search(r'"mediaDefinitions":\s*(\[.*?\])', text)
            if ph_match:
                try:
                    media_json = json.loads(ph_match.group(1))
                    for item in media_json:
                        if item.get("videoUrl"):
                            stream_link = item["videoUrl"]
                            break
                except Exception: pass
            if not stream_link:
                ph_m = re.search(r'quality_\d+p\s*=\s*["\'](https?:[^\s"\']+?)["\']', text)
                if ph_m: stream_link = ph_m.group(1)

        elif "spankbang" in domain:
            sb_match = re.search(r'stream_url\s*:\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'(https?:[^\s"\']*?\.m3u8[^\s"\']*)', text)
            if sb_match: stream_link = sb_match.group(1)

        elif "redtube" in domain or "youporn" in domain:
            rt_match = re.search(r'mediaDefinitions\s*:\s*(\[.*?\])', text)
            if rt_match:
                try:
                    media_json = json.loads(rt_match.group(1))
                    for item in media_json:
                        if item.get("videoUrl"):
                            stream_link = item["videoUrl"]
                            break
                except Exception: pass

        elif "4tube" in domain or "fapdu" in domain:
            ft_match = re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'"file":\s*["\'](https?:[^\s"\']+?)["\']', text)
            if ft_match: stream_link = ft_match.group(1)

        elif "i-porntv" in domain or "iporntv" in domain:
            ip_match = re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'file\s*:\s*["\'](https?:[^\s"\']+?)["\']', text)
            if ip_match: stream_link = ip_match.group(1)

        elif "hqporn" in domain or "justporn" in domain or "sexvid" in domain:
            hq_match = re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'file\s*:\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'video_url\s*:\s*["\'](https?:[^\s"\']+?)["\']', text)
            if hq_match: stream_link = hq_match.group(1)

        elif "eporner" in domain:
            ep_match = re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'file\s*:\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'(https?:[^\s"\']*?\.mp4[^\s"\']*)', text)
            if ep_match: stream_link = ep_match.group(1)

        elif "pornorus" in domain or "russkoe-porno" in domain:
            pr_match = re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'file\s*:\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'(https?:[^\s"\']*?\.m3u8[^\s"\']*)', text)
            if pr_match: stream_link = pr_match.group(1)

        elif "gotporn" in domain:
            gp_match = re.search(r'video_url\s*:\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text)
            if gp_match: stream_link = gp_match.group(1)

        elif "fak.xxx" in domain:
            fk_match = re.search(r'file\s*:\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text)
            if fk_match: stream_link = fk_match.group(1)

        elif "anysex" in domain:
            as_match = re.search(r'video_url\s*:\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'(https?:[^\s"\']*?\.mp4[^\s"\']*)', text)
            if as_match: stream_link = as_match.group(1)

        elif "superporn" in domain:
            sp_match = re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'file\s*:\s*["\'](https?:[^\s"\']+?)["\']', text)
            if sp_match: stream_link = sp_match.group(1)

        # Smart Generic Extractor (all remaining / newly added sites + fallback)
        if not stream_link:
            stream_link = await generic_extract(text, video_url)

        if not stream_link:                      # packed JS / base64 ke andar chhupa link
            extra = await asyncio.to_thread(deobfuscate_extra, text)
            if extra:
                stream_link = await generic_extract(extra, video_url, depth=2)

        if not stream_link and ytdlp_allowed(video_url):     # yt-dlp fallback (hazaaron sites)
            y = await ytdlp_try(video_url, source_page or None)
            if y:
                stream_link = y[1]
                if y[0] and title in ("Video", ""):
                    title = y[0]

        if not stream_link:
            EXTRACT_NOTE["v"] = f"{video_url}\n{page_diag(text)}"

        if stream_link:
            cands = await asyncio.to_thread(_quick_streams, text, video_url)
            if stream_link not in cands:
                cands.insert(0, stream_link)
            stream_link, rstatus = await refine_stream(stream_link, cands, text, video_url)   # preview clip -> asli video
            if stream_link not in cands:
                cands.insert(0, stream_link)
            final_link = process_tpl_link(stream_link) if ".m3u8" in stream_link else stream_link

            if ".pdf" in final_link.lower(): file_type = "PDF"
            elif any(ext in final_link.lower() for ext in ['.mp3', '.wav', '.m4a', '.aac']): file_type = "AUDIO"
            elif any(ext in final_link.lower() for ext in ['.jpg', '.png', '.jpeg', '.webp']): file_type = "IMAGE"

            variants, thumb, duration = await asyncio.to_thread(_page_meta, text, final_link, video_url)
            return {
                "title": title,
                "type": file_type,
                "page_url": video_url,
                "source_page": source_page or video_url,
                "download_link": final_link,
                "expires": stream_expiry(final_link),
                "thumb": thumb,
                "duration": duration,
                "iplock": stream_iplock(final_link),
                "preview_only": rstatus == "preview_only",
                "variants": variants,
                "_cands": cands
            }
    except Exception as e:
        logger.error(f"Extraction Error for {video_url}: {e}")
    return None


async def download_video_ffmpeg(url: str, output_path: str) -> bool:
    try:
        headers = get_custom_headers(url)
        cmd = [
            "ffmpeg",
            "-y",
            "-headers", f"User-Agent: {headers['User-Agent']}\r\nReferer: {headers['Referer']}\r\n",
            "-i", url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            output_path
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await proc.wait()
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except Exception as e:
        logger.error(f"FFmpeg download error: {e}")
        return False


# ==========================================================
# MULTI-PAGE SCRAPING ENGINE (UPGRADED)
# ==========================================================
async def scrape_multi_pages_chunk(url: str, start_page: int = 1, end_page: int = 10,
                                   progress=None, user_id: int = 0) -> List[dict]:
    global LAST_REPORT
    _ensure_fast_executor()
    reset_host_stats(url)
    rep = {"pages_ok": 0, "pages_fail": [], "links": 0, "extracted": 0}
    LAST_REPORT = rep

    if _looks_like_single_video(url):
        res = await extract_video_link(url, source_page=url)
        if res:
            rep["extracted"] = 1
            return [res]
        # extraction failed -> maybe it was actually a listing page, continue below

    page_urls = [fix_url(x) for x in await build_page_urls(url, start_page, end_page)]
    url_to_source: Dict[str, str] = {}

    async def crawl(pu: str):
        html = await fast_fetch(pu)
        if not html:
            rep["pages_fail"].append(f"{pu} (HTTP {LAST_STATUS.get(pu, '?')})")
            return
        rep["pages_ok"] += 1
        for l in find_video_links(html, pu):
            url_to_source.setdefault(l, pu)

    await asyncio.gather(*[crawl(p) for p in page_urls])
    rep["links"] = len(url_to_source)
    if not url_to_source:
        return []

    limiter = AdaptiveLimiter(SCR_CONCURRENCY)
    total = len(url_to_source)
    state = {"done": 0}

    async def sem_extract(v_url, src_p):
        if user_id and STOP_PROCESS.get(user_id):
            return None
        async with limiter:
            r = await guarded_extract(v_url, src_p)
            limiter.feedback(v_url, bool(r))
        state["done"] += 1
        if progress:
            try:
                await progress(state["done"], total)
            except Exception:
                pass
        return r

    results = [r for r in await asyncio.gather(
        *[sem_extract(v, s) for v, s in url_to_source.items()]) if r]
    results, dropped = clean_results(results)
    results = await verify_results(results, rep)
    rep["dropped"] = dropped
    rep["extracted"] = len(results)
    return results

                # ==========================================================
# HTML WEB APP GENERATOR  (YouTube-style player: thumbnails, favorites, all formats)
# ==========================================================
import hashlib


def best_thumb(text: str, page_url: str) -> str:
    """Page se poster/thumbnail URL (og:image, twitter:image, <video poster>, JSON-LD)."""
    head = text[:150000]
    for rx in (r'<meta[^>]+(?:property|name)=["\']og:image(?::secure_url|:url)?["\'][^>]*content=["\']([^"\']+)',
               r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:image(?::secure_url|:url)?["\']',
               r'<meta[^>]+(?:property|name)=["\']twitter:image(?::src)?["\'][^>]*content=["\']([^"\']+)',
               r'<video[^>]+poster=["\']([^"\']+)',
               r'"thumbnailUrl"\s*:\s*\[?\s*"([^"]+)"'):
        m = re.search(rx, head, re.I)
        if m:
            u = _html.unescape(m.group(1)).replace('\\/', '/').strip()
            if u and not u.startswith('data:'):
                return urljoin(page_url, u)
    return ""


def _iso_duration(s: str) -> int:
    m = re.match(r'^P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$', (s or "").strip(), re.I)
    if not m or not any(m.groups()):
        return 0
    h, mi, se = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + int(float(se or 0))


def best_duration(text: str) -> int:
    """Seconds me duration (og:video:duration / JSON-LD / "duration": N), warna 0."""
    head = text[:200000]
    cands = []
    for m in re.finditer(r'<meta[^>]+(?:property|name|itemprop)=["\'](?:og:video:duration|video:duration|duration)["\']'
                         r'[^>]*content=["\']([^"\']+)', head, re.I):
        cands.append(m.group(1))
    cands += re.findall(r'"duration"\s*:\s*"([^"]+)"', head)
    cands += re.findall(r'"duration"\s*:\s*(\d{1,6})\b', head)
    for c in cands:
        c = c.strip()
        sec = int(float(c)) if re.match(r'^\d+(?:\.\d+)?$', c) else _iso_duration(c)
        if 1 <= sec <= 172800:
            return sec
    return 0


_Q_FILE_RX = re.compile(r'^(?P<pre>[^?#]*/)(?P<q>\d{3,4}p|_TPL_)(?P<post>\.[^/?#]*)(?P<qs>[?#].*)?$')


def make_variants(link: str, text: str = "", page_url: str = "") -> List[dict]:
    """Quality options [{'q':1080,'u':url},...] (desc). xhamster 'multi=' list, <source label/res>, URL ke token se."""
    found: Dict[int, str] = {}
    fm = _Q_FILE_RX.match(link)
    mm = re.search(r'multi=([^/]+)', unquote(link))
    if mm and fm:
        for lab in re.findall(r'(\d{3,4})p', mm.group(1)):
            found[int(lab)] = fm.group('pre') + lab + 'p' + fm.group('post') + (fm.group('qs') or '')
    if text and len(found) < 2:
        for m in re.finditer(r'<source\b([^>]*)>', text[:400000], re.I):
            attrs = m.group(1)
            sm = re.search(r'\bsrc=["\']([^"\']+)["\']', attrs, re.I)
            qm = re.search(r'\b(?:label|title|res|size|data-res|data-quality)=["\']?(\d{3,4})p?\b', attrs, re.I)
            if sm and qm:
                u = _clean(sm.group(1), page_url or link)
                if _valid_stream_url(u):
                    found.setdefault(int(qm.group(1)), u)
    if found:
        q0 = None
        if fm and fm.group('q') != '_TPL_':
            q0 = int(fm.group('q')[:-1])
        else:
            tm = re.search(r'(\d{3,4})p', link.split('?')[0].rsplit('/', 1)[-1])
            q0 = int(tm.group(1)) if tm else None
        if q0:
            found.setdefault(q0, link)
    out = [{"q": q, "u": u} for q, u in sorted(found.items(), reverse=True) if 100 <= q <= 4320]
    return out if len(out) >= 2 else []


# ==========================================================
# PLAYER TEMPLATE (ADVANCED: AMBIENT LIGHT + 3-DOT MENU)
# ==========================================================
PLAYERTEMPLATE = r'''<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#0f0f0f">
<title>{{TITLE}} - {{OWNER}}</title>
<meta name="author" content="{{OWNER}}">
<!-- Player by {{OWNER}} -->
<style>
:root{
  --red:#f00;
  --bg:#0f0f0f;
  --bg2:#272727;
  --tx:#f1f1f1;
  --tx2:#aaa;
  --line:#303030;
  --chip:#272727;
  --chipA:#f1f1f1;
  --chipAt:#0f0f0f;
  --hh:56px;
}
[data-theme="light"]{
  --bg:#fff;
  --bg2:#f2f2f2;
  --tx:#0f0f0f;
  --tx2:#606060;
  --line:#e5e5e5;
  --chip:#f2f2f2;
  --chipA:#0f0f0f;
  --chipAt:#fff;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--tx);font-family:Roboto,Segoe UI,Arial,sans-serif}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;padding:0}
.hidden{display:none!important}
a{color:inherit}

/* lock */
#lock{position:fixed;inset:0;z-index:9999;background:var(--bg);display:flex;align-items:center;justify-content:center}
.lbox{width:88%;max-width:320px;background:var(--bg2);border-radius:16px;padding:26px;text-align:center}
.lbox h3{margin:10px 0 16px}
.lbox input{width:100%;padding:12px 14px;border-radius:24px;border:1px solid var(--line);background:var(--bg);color:var(--tx);outline:0;margin-bottom:12px;font-size:15px}
.lbox button{width:100%;padding:12px;border-radius:24px;background:var(--red);color:#fff;font-weight:600}
#lerr{color:#ff5252;font-size:12px;min-height:16px;margin-top:8px}
.lg{display:inline-flex;width:32px;height:22px;border-radius:7px;background:var(--red);align-items:center;justify-content:center}
.lg svg{width:14px;height:14px}

/* header */
.top{position:sticky;top:0;z-index:60;height:var(--hh);display:flex;align-items:center;gap:12px;padding:0 14px;background:var(--bg);border-bottom:1px solid var(--line)}
.logo{display:flex;align-items:center;gap:8px;text-decoration:none;font-weight:700;font-size:17px;min-width:0}
.ownt{font-size:12px;color:var(--tx2);text-decoration:none;white-space:nowrap;margin-left:2px}
.ownt:hover{color:var(--tx)}
.wm{position:absolute;top:10px;right:14px;z-index:2;color:rgba(255,255,255,.5);font-weight:700;font-size:14px;text-shadow:0 1px 5px #000;pointer-events:none}
.own2{margin-top:14px;font-size:12px}
.own2 a{color:var(--tx2);text-decoration:none}
.foot a{color:var(--tx2)}
.logo b{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:34vw}
.search{flex:1;max-width:640px;margin:0 auto;display:flex;position:relative}
.search input{width:100%;height:38px;border-radius:20px;border:1px solid var(--line);background:var(--bg);color:var(--tx);padding:0 38px 0 16px;outline:0;font-size:15px}
.search input:focus{border-color:#3ea6ff}
#qclr{position:absolute;right:10px;top:8px;color:var(--tx2)}
.tools{display:flex;gap:6px;align-items:center}
.tools button,.tools a{width:38px;height:38px;border-radius:50%;display:flex;align-items:center;justify-content:center;text-decoration:none;font-size:18px}
.tools button:hover,.tools a:hover{background:var(--bg2)}

/* chips */
.chips{position:sticky;top:var(--hh);z-index:50;background:var(--bg);display:flex;gap:10px;padding:10px 14px;overflow-x:auto;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;padding:7px 13px;border-radius:9px;background:var(--chip);font-size:14px;font-weight:500;white-space:nowrap}
.chip.on{background:var(--chipA);color:var(--chipAt)}

/* grid */
.bar{display:flex;justify-content:space-between;align-items:center;padding:4px 16px 8px;color:var(--tx2);font-size:13px}
.bar select{background:var(--chip);color:var(--tx);border:0;border-radius:8px;padding:6px 8px;font-size:13px}
.clr{background:var(--chip);color:var(--tx);border-radius:8px;padding:6px 10px;font-size:13px;margin-right:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:22px 16px;padding:8px 16px 40px}
.card{cursor:pointer;min-width:0;outline:0}
.thumb{position:relative;aspect-ratio:16/9;border-radius:12px;overflow:hidden;background:linear-gradient(135deg,var(--g1,333),var(--g2,111))}
.thumb .tImg,.thumb .tVid{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;background:#000}
.thumb .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:44px;font-weight:700;color:rgba(255,255,255,.55)}
.thumb.has .ph{display:none}
.dur{position:absolute;right:6px;bottom:8px;background:rgba(0,0,0,.8);color:#fff;font-size:12px;font-weight:600;padding:2px 5px;border-radius:5px}
.fmt{position:absolute;left:6px;top:6px;background:rgba(0,0,0,.65);color:#fff;font-size:10px;font-weight:700;padding:2px 6px;border-radius:5px;letter-spacing:.4px}
.fv{position:absolute;right:6px;top:6px;width:32px;height:32px;border-radius:50%;background:rgba(0,0,0,.55);color:#fff;font-size:17px;display:flex;align-items:center;justify-content:center;opacity:0;transition:.15s}
.fv.on{opacity:1;color:#ff4d6d}
.card:hover .fv,.card:focus .fv{opacity:1}
@media(hover:none){.fv{opacity:1}}
.prog{position:absolute;left:0;right:0;bottom:0;height:3px;background:rgba(255,255,255,.3)}
.prog i{display:block;height:100%;background:var(--red)}
.meta{display:flex;gap:12px;padding:12px 2px 0}
.av{flex:none;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;color:#fff;font-size:15px}
.tx{flex:1;min-width:0}
.ttl{margin:0;font-size:15px;font-weight:600;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word}
.sub{color:var(--tx2);font-size:13px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.more{flex:none;width:32px;height:32px;border-radius:50%;font-size:18px;color:var(--tx2)}
.more:hover{background:var(--bg2)}
.empty{text-align:center;color:var(--tx2);padding:70px 20px}
.foot{text-align:center;color:var(--tx2);font-size:12px;padding:10px 0 40px}
@media(max-width:600px){
  .grid{grid-template-columns:1fr;gap:18px;padding:0 0 40px}
  .thumb{border-radius:0}
  .meta{padding:10px 12px 0}
  .bar{padding:4px 12px 8px}
  .logo b{display:none}
}

/* watch */
.wl{display:grid;grid-template-columns:minmax(0,1fr) 400px;gap:24px;padding:20px 24px 40px;max-width:1800px;margin:0 auto}
.theater .wl{grid-template-columns:1fr}
.theater .player{border-radius:0;max-height:80vh}
.theater .wmain{margin:0 -24px}
.theater .wmain:not(.player){margin-left:24px;margin-right:24px}
.player{position:relative;background:#000;aspect-ratio:16/9;width:100%;max-height:calc(100vh - 110px);border-radius:12px;overflow:hidden;user-select:none;isolation:isolate}
.player video,.player img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000}
.player:fullscreen{max-height:none;border-radius:0}
.aud{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:90px;color:#555}
.ov{position:absolute;inset:0 0 54px 0;z-index:3}
.big{position:absolute;left:50%;top:50%;width:68px;height:68px;margin:-34px;border-radius:50%;background:rgba(0,0,0,.6);color:#fff;font-size:30px;display:flex;align-items:center;justify-content:center;opacity:0;transition:.2s;pointer-events:none}
.paused .big{opacity:1}
.spin{position:absolute;left:50%;top:50%;width:46px;height:46px;margin:-23px;border:4px solid rgba(255,255,255,.25);border-top-color:#fff;border-radius:50%;animation:sp 1s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.rip{position:absolute;top:50%;margin-top:-34px;padding:14px 18px;border-radius:40px;background:rgba(0,0,0,.6);color:#fff;font-weight:600;opacity:0;transition:.25s;pointer-events:none}
.rip.l{left:10%}.rip.r{right:10%}.rip.on{opacity:1}
.err{position:absolute;inset:0;background:rgba(0,0,0,.88);color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:18px;text-align:center;font-size:14px;z-index:5}
.err .eb{display:flex;flex-wrap:wrap;gap:8px;justify-content:center}
.err a,.err button{background:#fff;color:#000;padding:8px 14px;border-radius:18px;font-weight:600;font-size:13px;text-decoration:none}
.ctl{position:absolute;left:0;right:0;bottom:0;z-index:4;padding:0 12px 6px;background:linear-gradient(transparent,rgba(0,0,0,.85));opacity:0;transition:opacity .2s;color:#fff}
.player.show .ctl,.player.paused .ctl{opacity:1}
.player:not(.show):not(.paused){cursor:none}
.seek{position:relative;height:18px;cursor:pointer;touch-action:none;display:flex;align-items:center}
.seek::before{content:"";position:absolute;left:0;right:0;height:4px;background:rgba(255,255,255,.3);border-radius:2px;transition:height .1s}
.seek:hover::before{height:6px}
.buf,.pro{position:absolute;left:0;height:4px;border-radius:2px;width:0;pointer-events:none}
.seek:hover .buf,.seek:hover .pro{height:6px}
.buf{background:rgba(255,255,255,.45)}.pro{background:var(--red)}
.knob{position:absolute;left:0;width:14px;height:14px;margin-left:-7px;border-radius:50%;background:var(--red);transform:scale(0);transition:transform .1s;pointer-events:none}
.seek:hover .knob,.seek.drag .knob{transform:scale(1)}
.tip{position:absolute;bottom:22px;transform:translateX(-50%);background:rgba(0,0,0,.85);padding:2px 6px;border-radius:4px;font-size:12px;display:none;pointer-events:none}
.seek:hover .tip{display:block}
.crow{display:flex;align-items:center;gap:2px;font-size:13px}
.crow button{width:38px;height:38px;border-radius:50%;font-size:17px;color:#fff;display:flex;align-items:center;justify-content:center}
.crow button:hover{background:rgba(255,255,255,.15)}
.crow .t{padding:0 8px;white-space:nowrap}
.sp{flex:1}
.crow .tx2{width:auto;padding:0 10px;border-radius:18px;font-size:13px;font-weight:600}
.vol{width:70px;accent-color:#fff}
@media(max-width:600px){.vol{display:none}}
.menu{position:absolute;right:10px;bottom:62px;background:rgba(28,28,28,.96);border-radius:12px;padding:6px 0;min-width:130px;max-height:60vh;overflow:auto}
.menu button{display:block;width:100%;text-align:left;padding:9px 18px;color:#fff;font-size:14px}
.menu button:hover{background:rgba(255,255,255,.12)}
.menu button.on{font-weight:700;color:#3ea6ff}

/* Advanced menu styles */
.menu .sep{height:1px;background:rgba(255,255,255,.12);margin:6px 0}
.spdchips{display:flex;gap:6px;flex-wrap:wrap;padding:6px 12px 10px}
.spdchip{padding:5px 9px;border-radius:999px;background:rgba(255,255,255,.08);color:#fff;font-size:12px;font-weight:600;cursor:pointer;user-select:none}
.spdchip.on{background:#3ea6ff;color:#000}
.sliderrow{display:grid;grid-template-columns:100px 1fr 48px;align-items:center;gap:8px;padding:6px 12px;font-size:13px;color:#fff}
.sliderrow input[type=range]{width:100%;accent-color:#3ea6ff}
.sliderrow .val{text-align:right;font-weight:700;font-size:12px;color:#cfcfcf}
.togrow{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;font-size:13px;color:#fff}
.togrow label{display:flex;align-items:center;gap:8px;cursor:pointer}
.jumprow{display:flex;gap:6px;padding:6px 12px 10px}
.jumpchip{flex:1;padding:6px 8px;border-radius:8px;background:rgba(255,255,255,.06);color:#fff;font-size:12px;font-weight:600;text-align:center;cursor:pointer;user-select:none}
.jumpchip:hover{background:rgba(255,255,255,.14)}
.dimoverlay{position:absolute;inset:0;background:rgba(0,0,0,.35);pointer-events:none;opacity:0;transition:opacity .25s ease;z-index:2}
.player.dimmed .dimoverlay{opacity:1}
.player.zoom-fill video,.player.zoom-fill img{object-fit:cover}
.player.night video,.player.night img{filter:brightness(.92) contrast(1.05)}

.wt{font-size:20px;line-height:1.35;margin:14px 0 8px;font-weight:700;word-break:break-word}
.acts{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.act{display:inline-flex;align-items:center;gap:6px;padding:9px 15px;border-radius:20px;background:var(--bg2);font-size:14px;font-weight:600;text-decoration:none}
.act:hover{filter:brightness(1.2)}
.act.on{background:var(--chipA);color:var(--chipAt)}
.desc{background:var(--bg2);border-radius:12px;padding:12px 14px;font-size:14px;line-height:1.6;color:var(--tx2);word-break:break-all}
.desc b{color:var(--tx)}
.ah{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-size:15px}
.ah label{font-size:13px;color:var(--tx2);display:flex;gap:6px;align-items:center}
.up{display:flex;gap:10px;margin-bottom:12px;cursor:pointer}
.up .thumb{width:168px;flex:none;border-radius:8px}
.up .tx .ttl{font-size:14px}
.up.cur .ttl{color:#3ea6ff}
@media(max-width:1000px){
  .wl{grid-template-columns:1fr;padding:0 0 40px;gap:14px}
  .player{position:sticky;top:var(--hh);z-index:40;border-radius:0;max-height:none}
  .wmain:not(.player){margin-left:14px;margin-right:14px}
  .wside{padding:0 14px}
  .theater .wmain{margin:0}
  .theater .wmain:not(.player){margin-left:14px;margin-right:14px}
}

/* popup toast */
.pop{position:fixed;z-index:200;background:var(--bg2);border-radius:12px;padding:6px 0;min-width:190px;box-shadow:0 6px 30px rgba(0,0,0,.5)}
.pop button,.pop a{display:block;width:100%;text-align:left;padding:10px 16px;font-size:14px;text-decoration:none}
.pop button:hover,.pop a:hover{background:rgba(128,128,128,.25)}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#323232;color:#fff;padding:10px 18px;border-radius:8px;font-size:14px;z-index:300;opacity:0;pointer-events:none;transition:.25s}
#toast.on{opacity:1}
</style>
</head>
<body>
<div id="lock" hidden>
  <div class="lbox">
    <span class="lg"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="#fff"/></svg></span>
    <h3>Protected</h3>
    <input id="pw" type="password" placeholder="Password" autocomplete="off">
    <button id="pwb">Unlock</button>
    <div id="lerr"></div>
  </div>
  <div class="own2" id="lockOwn"></div>
</div>

<div id="app" hidden>
  <header class="top">
    <a class="logo" href="#">
      <span class="lg"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="#fff"/></svg></span>
      <b id="siteT"></b>
    </a>
    <a class="ownt" id="ownT" target="_blank" rel="noopener" hidden>by {{OWNER}}</a>
    <div class="search">
      <input id="q" type="search" placeholder="Search" autocomplete="off">
      <button id="qclr" hidden>✕</button>
    </div>
    <div class="tools">
      <button id="setB" title="Menu / clear history / export">⚙</button>
      <button id="themeB" title="Theme">🌓</button>
      <a id="tgB" target="_blank" rel="noopener" title="Telegram">✈</a>
    </div>
  </header>
  <nav class="chips" id="chips"></nav>
  <main id="home">
    <div class="bar">
      <span id="counts"></span>
      <span class="rt">
        <button class="clr" id="clr" hidden>✕</button>
        <select id="sort">
          <option value="def">Default</option>
          <option value="az">A → Z</option>
          <option value="za">Z → A</option>
          <option value="long">Longest</option>
          <option value="short">Shortest</option>
        </select>
      </span>
    </div>
    <div class="grid" id="grid"></div>
    <div class="empty" id="empty" hidden></div>
    <div class="foot" id="foot"></div>
  </main>
  <section id="watch" hidden>
    <div class="wl">
      <div class="wmain">
        <div class="player paused" id="pl">
          <video id="v" playsinline preload="auto"></video>
          <img id="imgv" hidden alt="">
          <div class="aud" id="aud" hidden>▶</div>
          <div class="wm" id="wm"></div>
          <div class="ov" id="ov">
            <div class="spin" id="spin" hidden></div>
            <div class="rip l" id="ripL">-10s</div>
            <div class="rip r" id="ripR">+10s</div>
            <div class="big" id="big">▶</div>
          </div>
          <div class="err" id="err" hidden></div>
          <div class="ctl" id="ctl">
            <div class="seek" id="seek">
              <div class="buf" id="buf"></div>
              <div class="pro" id="pro"></div>
              <div class="knob" id="knob"></div>
              <div class="tip" id="tip">00:00</div>
            </div>
            <div class="crow">
              <button id="bPlay" title="Play (k)">▶</button>
              <button id="bNext" title="Next (n)">⏭</button>
              <button id="bVol" title="Mute (m)">🔊</button>
              <input id="vol" type="range" min="0" max="1" step="0.05" value="1">
              <span class="t" id="tm">00:00 / 00:00</span>
              <span class="sp"></span>
              <button class="tx2" id="bSpd" title="Speed">1x</button>
              <button class="tx2" id="bQ" title="Quality" hidden>Auto</button>
              <button id="bPip" title="Picture in picture">⧉</button>
              <button id="bTh" title="Theater (t)">⛶</button>
              <button id="bFs" title="Fullscreen (f)">⛶</button>
              <button class="tx2" id="bAdv" title="Advanced">⚙</button>
            </div>
            <div class="crow" id="spdRow" hidden>
              <span class="t" style="font-size:12px;color:#ddd;padding:0 6px;">Speed</span>
              <div class="spdchips" id="spdChips"></div>
            </div>
          </div>
          <div class="dimoverlay" id="dimOv"></div>
          <div class="menu" id="advMenu" hidden>
            <div class="sliderrow">
              <span>Brightness</span>
              <input type="range" id="rngBright" min="0" max="1.6" step="0.01" value="1">
              <span class="val" id="valBright">1.00</span>
            </div>
            <div class="sliderrow">
              <span>Contrast</span>
              <input type="range" id="rngContr" min="0.6" max="1.6" step="0.01" value="1">
              <span class="val" id="valContr">1.00</span>
            </div>
            <div class="sliderrow">
              <span>Saturation</span>
              <input type="range" id="rngSat" min="0" max="2" step="0.01" value="1">
              <span class="val" id="valSat">1.00</span>
            </div>
            <div class="sliderrow">
              <span>Hue</span>
              <input type="range" id="rngHue" min="-0.5" max="0.5" step="0.01" value="0">
              <span class="val" id="valHue">0</span>
            </div>
            <div class="sep"></div>
            <div class="sliderrow">
              <span>Volume</span>
              <input type="range" id="rngVol" min="0" max="3" step="0.01" value="1">
              <span class="val" id="valVol">1.00</span>
            </div>
            <div class="sep"></div>
            <div class="togrow">
              <label><input type="checkbox" id="chkZoom"> Zoom to fill</label>
              <label><input type="checkbox" id="chkNight"> Night mode</label>
            </div>
            <div class="togrow">
              <label><input type="checkbox" id="chkDim"> Dim when paused</label>
              <label><input type="checkbox" id="chkTitle"> Title when paused</label>
            </div>
            <div class="sep"></div>
            <div class="jumprow">
              <div class="jumpchip" id="jmpIntro">Intro</div>
              <div class="jumpchip" id="jmpRecap">Recap</div>
              <div class="jumpchip" id="jmpLast">Last</div>
            </div>
            <div class="sep"></div>
            <button id="btnResetAdv">Reset all</button>
          </div>
        </div>
        <div class="wt" id="wt"></div>
        <div class="acts" id="acts"></div>
        <div class="desc" id="desc"></div>
        <div class="ah"><b>Up next</b><label><input type="checkbox" id="auto" checked> Autoplay</label></div>
        <div id="upn"></div>
      </div>
      <aside class="wside">
        <div class="ah"><b>Up next</b><label><input type="checkbox" id="auto2" checked> Autoplay</label></div>
        <div id="upn2"></div>
      </aside>
    </div>
  </section>
  <div id="toast"></div>
</div>

<script>
(function(){
"use strict";
var DATA={{DATA}}, CFG={{CFG}};
var HLSURL="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js",
    DASHURL="https://cdn.jsdelivr.net/npm/dashjs@4/dist/dash.all.min.js",
    TSURL="https://cdn.jsdelivr.net/npm/mpegts.js@1/dist/mpegts.js";

function s(r){return document.querySelector(r)}
function elt(c,x){var e=document.createElement(c);if(x!==null)e.textContent=x;return e}
var LS={get:function(k,d){try{var v=localStorage.getItem(k);return v===null?d:JSON.parse(v)}catch(e){return d}},
        set:function(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
var SS={get:function(k){try{return sessionStorage.getItem(k)}catch(e){return null}},
        set:function(k,v){try{sessionStorage.setItem(k,v)}catch(e){}}};

/* sha256 password gate */
function sha256(ascii){function r(v,a){return(v>>>a)|(v<<(32-a))}
  var mp=Math.pow,mw=mp(2,32),i,j,result,words=[],a=ascii.length*8;
  var hash=[0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19],
      k=[0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
         0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
         0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
         0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
         0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
         0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
         0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
         0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2],
      p=ascii.length,i,c;
  for(i=0;i<313;i+=2)hash[i%8]=hash[i%8]*mp(2,32)|0;
  ascii+="\x80";while(ascii.length%64!==56)ascii+="\x00";
  for(i=0;i<ascii.length;i++)j=ascii.charCodeAt(i);if(j>255)return;words[i>>2]|=j<<(24-(i%4)*8);
  words[words.length]=a/mw|0;words[words.length]=a;
  for(j=0;j<words.length;j+=16){var w=words.slice(j,j+16),oldhash=hash.slice(0);
    for(i=0;i<64;i++){var w15=w[i-15],w2=w[i-2],a=hash[0],e=hash[4];
      var t1=hash[7]+r(e,6)+r(e,11)+r(e,25)+(e&hash[5]^~e&hash[6])+k[i]+(i<16?w[i]:w[i-16]+r(w[i-7],7)+r(w[i-7],18)+(w[i-7]>>>3));
      var t2=r(a,2)+r(a,13)+r(a,22)+(a&hash[1]^a&hash[2]^hash[3]);
      hash[7]=hash[6];hash[6]=hash[5];hash[5]=hash[4];hash[4]=hash[3]+t1|0;
      hash[3]=hash[2];hash[2]=hash[1];hash[1]=hash[0];hash[0]=t1+t2|0;}
    for(i=0;i<8;i++)hash[i]=hash[i]+oldhash[i]|0;}
  for(i=0;i<8;i++)for(j=3;j>=0;j--){var b=hash[i]>>(j*8)&255;result+=(b<16?"0":"")+b.toString(16);}
  return result;
}
function hashPw(pw){return sha256(unescape(encodeURIComponent(pw)))}

/* helpers */
function fmtTime(s){s=Math.max(0,Math.floor(s));var h=Math.floor(s/3600),m=Math.floor((s%3600)/60),x=s%60;return h?h+":"+(m<10?"0":"")+m+":"+(x<10?"0":"")+x:(m<10?"0":"")+m+":"+(x<10?"0":"")+x}
function domainOf(u){try{return new URL(u).hostname.replace(/^www\./,"")}catch(e){return""}}
function hnum(s){var h=0;for(var i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))|0;return Math.abs(h)}
function grad(t){var h=hnum(t)%360;return `hsl(${h},55%,38%)`}
function pathOf(u){return u.split("#")[0].split("?")[0].toLowerCase()}
function extOf(u){var m=pathOf(u).match(/[a-z0-9]{2,5}$/);return m?m[0]:""}
var AUDIOEXT=["mp3","m4a","aac","wav","ogg","oga","opus","flac","wma"];
function fmtOf(it){var u=it.u.toLowerCase();if(u.indexOf(".m3u8")>-1)return"HLS";var e=extOf(u);if(e==="mpd")return"DASH";if(e==="m4v")return"MP4";if(e)return e.toUpperCase();return it.k||"VIDEO"}
function isAudio(it){return it.k==="AUDIO"||AUDIOEXT.indexOf(extOf(it.u))>-1}
function engineOf(it){var u=it.u.toLowerCase(),e=extOf(u);if(u.indexOf(".m3u8")>-1)return"hls";if(e==="mpd")return"dash";if(e==="ts"||e==="flv"||e==="m2ts")return"mpegts";return"native"}
var sc;
function loadScript(u){if(sc)return sc;sc=new Promise(function(ok,no){var s=document.createElement("script");s.src=u;s.async=true;s.onload=ok;s.onerror=function(){delete sc;sc=null;no(new Error("script load fail"))};document.head.appendChild(s)});return sc}
var toastT;
function toast(m){var t=s("#toast");t.textContent=m;t.className="on";clearTimeout(toastT);toastT=setTimeout(function(){t.className=""},2200)}
function copy(t){if(navigator.clipboard){navigator.clipboard.writeText(t).then(function(){toast("Link copied")},function(){fb()})}else{fb()}function fb(){var a=document.createElement("textarea");a.value=t;document.body.appendChild(a);a.select();try{document.execCommand("copy");toast("Link copied")}catch(e){toast("Copy fail")}a.remove()}}
function extLinks(u){var enc=encodeURIComponent(u),ua=navigator.userAgent,ios=/iPhone|iPad|iPod/.test(ua),and=/Android/.test(ua),a=[];a.push({n:"VLC",h:ios?`vlc-x-callback://x-callback-url/stream?url=${enc}`:`intent:${u}#Intent;package=org.videolan.vlc;type=video;end`});if(and)a.push({n:"MX Player",h:`intent:${u}#Intent;package=com.mxtech.videoplayer.ad;type=video;end`});a.push({n:"Open link",h:u});return a}

/* state */
var FAV=LS.get("ytb_fav",{}), LATER=LS.get("ytb_later",{}), HIST=LS.get("ytb_hist",{});
var ALL=DATA.slice(), BYURL={}, DUR={}, THUMBS={};
function slimit(it){return {t:it.t,u:it.u,k:it.k,p:it.p,th:it.th,d:it.d,v:it.v}}
ALL.forEach(function(it){BYURL[it.u]=it});
Object.keys(FAV).forEach(function(u){var o=FAV[u];if(!BYURL[u]){o.x=1;BYURL[u]=o;ALL.push(o)}});
Object.keys(LATER).forEach(function(u){var o=LATER[u];if(!BYURL[u]){o.x=1;BYURL[u]=o;ALL.push(o)}});
Object.keys(HIST).forEach(function(u){var h=HIST[u];if(!BYURL[u]){h.it.x=1;BYURL[u]=h.it;ALL.push(h.it)}});
var VIEW={chip:"all",q:"",sort:"def"}, LIST=[], CUR=null;
function saveLists(){LS.set("ytb_fav",FAV);LS.set("ytb_later",LATER)}
function toggleFav(it){if(FAV[it.u]){delete FAV[it.u];toast("Removed from Favorites")}else{FAV[it.u]=slimit(it);toast("Added to Favorites ⭐")}saveLists();updChips()}
function toggleLater(it){if(LATER[it.u]){delete LATER[it.u];toast("Removed from Watch later")}else{LATER[it.u]=slimit(it);toast("Saved to Watch later ⏱")}saveLists();updChips()}
function saveHist(it,pos,dur){HIST[it.u]={ts:Date.now(),pos:pos||0,dur:dur||0,it:slimit(it)};var ks=Object.keys(HIST);if(ks.length>300){ks.sort(function(a,b){return HIST[b].ts-HIST[a].ts});for(var i=0;i<ks.length-300;i++)delete HIST[ks[i]]}LS.set("ytb_hist",HIST)}

/* thumbnails */
var thumbQ=[], thumbRun=0, liveVid=0;
function queueThumb(fn){thumbQ.push(fn);pump()}
function pump(){while(thumbRun<3&&thumbQ.length){var f=thumbQ.shift();thumbRun++;f().then(function(){thumbRun--},function(){thumbRun--});pump()}}
function setThumb(box,node){box.insertBefore(node,box.firstChild);box.classList.add("has")}
function setDur(it,sec,box){if(!isFinite(sec)||sec<=0)return;DUR[it.u]=sec;if(!it.d)it.d=sec;var d=box.querySelector(".dur");if(d){d.textContent=fmtTime(sec);d.hidden=false}}
function fillThumb(it,box){if(THUMBS[it.u]){var im=new Image();im.className="tImg";im.src=THUMBS[it.u];setThumb(box,im);return}if(it.k==="IMAGE"){var i2=new Image();i2.className="tImg";i2.referrerPolicy="no-referrer";i2.onload=function(){setThumb(box,i2)};i2.src=it.u;return}if(isAudio(it)||it.k==="PDF")return;if(it.th){var img=new Image();img.className="tImg";img.referrerPolicy="no-referrer";img.decoding="async";img.onload=function(){setThumb(box,img)};img.onerror=function(){frameThumb(it,box)};img.src=it.th}else{frameThumb(it,box)}}
function frameThumb(it,box){var eng=engineOf(it);if(eng!=="native"&&eng!=="hls")return;queueThumb(function(){return new Promise(function(res){var v=document.createElement("video"),done=false,h=null,cors=true,timer,tries=0;v.muted=true;v.setAttribute("playsinline","");v.preload="metadata";v.className="tVid";function cleanup(){try{if(h)h.destroy();h=null}catch(e){}try{v.removeAttribute("src");v.load()}catch(e){}}function finish(ok,keep){if(done)return;done=true;clearTimeout(timer);if(ok&&keep)setThumb(box,v);else cleanup();res()}function capture(){try{var w=v.videoWidth,hh=v.videoHeight;if(!w||!hh)return false;var c=document.createElement("canvas");c.width=320;c.height=Math.round(320*hh/w);c.getContext("2d").drawImage(v,0,0,c.width,c.height);var url=c.toDataURL("image/jpeg",.7);THUMBS[it.u]=url;var im=new Image();im.className="tImg";im.src=url;setThumb(box,im);return true}catch(e){return false}}function ready(){if(done)return;if(capture()){finish(true,false);return}if(eng==="native"||liveVid>40)finish(true,true);else finish(false,false)}v.addEventListener("loadedmetadata",function(){if(isFinite(v.duration)&&v.duration>0)setDur(it,v.duration,box);try{v.currentTime=Math.min(Math.max(v.duration*.12,1),20)}catch(e){}setTimeout(function(){if(!done&&v.readyState>=2)ready()},2500)},true);v.addEventListener("seeked",ready);v.addEventListener("loadeddata",function(){if(eng==="hls")setTimeout(ready,300)});v.addEventListener("error",function(){if(eng==="native"&&cors&&tries===0){tries++;cors=false;v.removeAttribute("crossorigin");v.src=it.u;return}finish(false,false)});timer=setTimeout(function(){finish(false,false)},15000);if(eng==="hls"){loadScript(HLSURL).then(function(){if(!window.Hls||!Hls.isSupported()){finish(false,false);return}h=new Hls({maxBufferLength:2,maxMaxBufferLength:4,startPosition:3,enableWorker:false});h.on(Hls.Events.ERROR,function(e,d){if(d.fatal)finish(false,false)});h.loadSource(it.u);h.attachMedia(v)},function(){finish(false,false)})}else{v.crossOrigin=cors?"anonymous":"";v.src=it.u}})});var obs=null;if(window.IntersectionObserver){obs=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting){obs.unobserve(en.target);fillThumb(en.target.it,en.target)}})},{rootMargin:"400px"})}function mkThumb(it){var th=elt("div","thumb"),g=grad(it.t);th.style.setProperty("--g1",g);th.style.setProperty("--g2",g);th.appendChild(elt("span","ph",it.t?.trim().charAt(0).toUpperCase()));th.appendChild(elt("span","fmt",fmtOf(it)));var d=elt("span","dur",it.d?fmtTime(it.d):"");if(!d.textContent)d.hidden=true;th.appendChild(d);th.it=it;if(obs)obs.observe(th);else fillThumb(it,th);return th}

/* home grid */
var grid=s("#grid"), chipsEl=s("#chips");
function curDur(it){return it.d||DUR[it.u]||0}
function computeList(){var base,c=VIEW.chip;if(c==="fav")base=Object.keys(FAV).map(function(u){return BYURL[u]}).filter(Boolean);else if(c==="later")base=Object.keys(LATER).map(function(u){return BYURL[u]}).filter(Boolean);else if(c==="hist")base=Object.keys(HIST).sort(function(a,b){return HIST[b].ts-HIST[a].ts}).map(function(u){return BYURL[u]}).filter(Boolean);else{base=DATA.slice();if(c.indexOf("file")===0)base=base.filter(function(it){return fmtOf(it)===c.slice(4)})}var q=VIEW.q.trim().toLowerCase();if(q)base=base.filter(function(it){return it.t.toLowerCase().indexOf(q)>-1||domainOf(it.u).indexOf(q)>-1});if(c!=="hist"){if(VIEW.sort==="az")base.sort(function(a,b){return a.t.localeCompare(b.t)});else if(VIEW.sort==="za")base.sort(function(a,b){return b.t.localeCompare(a.t)});else if(VIEW.sort==="long")base.sort(function(a,b){return curDur(b)-curDur(a)});else if(VIEW.sort==="short")base.sort(function(a,b){return (curDur(a)>1e9?0:curDur(a))-(curDur(b)>1e9?0:curDur(b))})}return base}
function updChips(){chipsEl.textContent="";var counts={};DATA.forEach(function(it){var f=fmtOf(it);counts[f]=(counts[f]||0)+1});var defs=[["all","All"],["fav","⭐ Favorites",Object.keys(FAV).length],["later","⏱ Watch later",Object.keys(LATER).length],["hist","🕒 History",Object.keys(HIST).length]];Object.keys(counts).sort(function(a,b){return counts[b]-counts[a]}).forEach(function(f){defs.push([f,f,counts[f]])});defs.forEach(function(d){var b=elt("button","chip",d[0]);if(VIEW.chip===d[0])b.classList.add("on");b.onclick=function(){VIEW.chip=d[0];updChips();renderGrid()};chipsEl.appendChild(b)})}
function showPop(anchor,entries){closePop();var p=elt("div","pop");p.id="pop";entries.forEach(function(en){if(en.href){var a=elt("a","",en.n);a.href=en.href;a.target="_blank";a.rel="noopener noreferrer";p.appendChild(a)}else{var b=elt("button","",en.n);b.onclick=function(e){e.stopPropagation();closePop();en.f()};p.appendChild(b)}});document.body.appendChild(p);var r=anchor.getBoundingClientRect(),w=p.offsetWidth+220,h=p.offsetHeight+40;p.style.left=Math.max(8,Math.min(window.innerWidth-w-8,r.right-w))+"px";p.style.top=Math.max(8,Math.min(window.innerHeight-h-8,r.bottom+4))+"px"}
function popMenu(anchor,it){var e=[{t:"▶ Play",f:function(){go(it)}},{t:FAV[it.u]?"⭐ Remove favorite":"⭐ Add to favorites",f:function(){toggleFav(it);renderGrid()}},{t:LATER[it.u]?"⏱ Remove from Watch later":"⏱ Watch later",f:function(){toggleLater(it);if(VIEW.chip==="later")renderGrid()}},{t:"📋 Copy link",f:function(){copy(it.u)}}];if(HIST[it.u])e.push({t:"🗑 Remove from history",f:function(){delete HIST[it.u];LS.set("ytb_hist",HIST);toast("Removed from history");updChips();renderGrid()}});extLinks(it.u).forEach(function(x){e.push({t:"📤 "+x.n,href:x.h})});showPop(anchor,e)}
function clearList(kind){var nm={hist:"history",fav:"favorites",later:"watch later"};if(kind==="all"){if(!confirm("Saara saved data (favorites, watch later, history) delete karna hai?"))return;FAV={};LATER={};HIST={};LS.set("ytb_fav",FAV);LS.set("ytb_later",LATER);LS.set("ytb_hist",HIST);toast("Sab saaf ho gaya")}else{if(!confirm("Poori "+nm[kind]+" delete karni hai?"))return;if(kind==="hist"){HIST={};LS.set("ytb_hist",HIST)}else if(kind==="fav"){FAV={};LS.set("ytb_fav",FAV)}else{LATER={};LS.set("ytb_later",LATER)}}toast(nm[kind]+" cleared");updChips();renderGrid()}
function exportFav(){var l=Object.keys(FAV).map(function(u){return FAV[u]});if(!l.length){toast("Favorites khali hain");return}var txt="#EXTM3U\n"+l.map(function(i){return "#EXTINF:-1,"+i.t.replace(/,/g,"")+"\n"+i.u}).join("\n");try{var b=new Blob([txt],{type:"audio/x-mpegurl"}),a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="favorites.m3u";document.body.appendChild(a);a.click();a.remove();toast("favorites.m3u download ho gayi")}catch(e){copy(txt)}}
function gearMenu(anchor){showPop(anchor,[{t:"🗑 Clear history ("+Object.keys(HIST).length+")",f:function(){clearList("hist")}},{t:"⭐ Clear favorites ("+Object.keys(FAV).length+")",f:function(){clearList("fav")}},{t:"⏱ Clear watch later ("+Object.keys(LATER).length+")",f:function(){clearList("later")}},{t:"♻ Reset all saved data",f:function(){clearList("all")}},{t:"📥 Export favorites (M3U)",f:exportFav}].concat(CFG.owner&&CFG.tg?[{t:"✈ "+CFG.owner,href:CFG.tg}]:[]))}
function closePop(){var p=s("#pop");if(p)p.remove()}
document.addEventListener("click",closePop);
function mkCard(it){var c=elt("article","card");c.tabIndex=0;var th=mkThumb(it);var fv=elt("button","fv",FAV[it.u]?"❤":"♡");fv.title="Favorite";fv.onclick=function(e){e.stopPropagation();toggleFav(it);fv.className="fv"+(FAV[it.u]?" on":"");fv.textContent=FAV[it.u]?"❤":"♡";if(VIEW.chip==="fav")renderGrid()};th.appendChild(fv);var h=HIST[it.u];if(h&&h.dur>0){var pr=elt("div","prog"),i=elt("i","");i.style.width=Math.min(100,h.pos/h.dur*100)+"%";pr.appendChild(i);th.appendChild(pr)}c.appendChild(th);var m=elt("div","meta"),a=elt("div","av",domainOf(it.u)?.charAt(0).toUpperCase()),g=grad(domainOf(it.u));a.style.background=g;var tx=elt("div","tx"),t=elt("h3","ttl",it.t||"Video");t.title=it.t;tx.appendChild(t);tx.appendChild(elt("div","sub",domainOf(it.u)+" • "+fmtOf(it)));var mb=elt("button","more","⋮");mb.onclick=function(e){e.stopPropagation();popMenu(mb,it)};m.appendChild(a);m.appendChild(tx);m.appendChild(mb);c.appendChild(m);c.onclick=function(){go(it)};c.onkeydown=function(e){if(e.key==="Enter")go(it)};return c}
function renderGrid(){LIST=computeList();grid.textContent="";var frag=document.createDocumentFragment();LIST.forEach(function(it){frag.appendChild(mkCard(it))});grid.appendChild(frag);s("#counts").textContent=LIST.length+" item"+(LIST.length===1?"":"s");var clr=s("#clr"),cc=VIEW.chip;if(cc==="hist"||cc==="fav"||cc==="later")clr.hidden=false;else clr.hidden=true;if(!clr.hidden){clr.textContent="✕ Clear "+(cc==="hist"?"history":cc==="fav"?"favorites":"watch later");clr.onclick=function(){clearList(cc)}}var em=s("#empty");em.hidden=LIST.length>0;if(!LIST.length)em.textContent=VIEW.chip==="fav"?"Koi favorite nahi. Kisi video par ⭐ dabao.":VIEW.chip==="later"?"Watch later khali hai.":VIEW.chip==="hist"?"Abhi koi video nahi dekha.":"Kuch nahi mila."}
function go(it){location.hash="#w"+ALL.indexOf(it)}

/* player */
var V=s("#v"), PL=s("#pl"), hls=null, mp=null, dash=null, retries=0, hideT, lastSave=0, dragging=false, IMGV=s("#imgv");
var CURQ="auto", SEEKAT=0, CURURL;
var isTouch=false;try{isTouch=window.matchMedia("(pointer:coarse)").matches}catch(e){}
function destroyEngines(){try{if(hls)hls.destroy();hls=null}catch(e){}try{if(mp)mp.destroy();mp=null}catch(e){}try{if(dash)dash.reset();dash=null}catch(e){}try{V.pause();V.removeAttribute("src");V.load()}catch(e){}}
function setLoading(b){s("#spin").hidden=!b}
function showErr(msg,it){var e=s("#err");if(!msg){e.hidden=true;e.textContent="";return}e.textContent="";e.hidden=false;setLoading(false);e.appendChild(elt("div","",msg));var box=elt("div","eb");var rt=elt("button","","Retry");rt.onclick=function(){if(CUR)loadItem(CUR,CURURL,true,V.currentTime)};box.appendChild(rt);var lv=lowerVariant();if(lv){var lb=elt("button","","Try "+lv[0]);lb.onclick=function(){CURQ=lv[0];loadItem(CUR,lv[1],true,V.currentTime)};box.appendChild(lb)}if(it)extLinks(it.u).forEach(function(x){var a=elt("a","",x.n+" Open link");a.href=x.h;a.target="_blank";a.rel="noopener noreferrer";box.appendChild(a)});var cp=elt("button","","Copy link");cp.onclick=function(){copy(it.u)};box.appendChild(cp);e.appendChild(box)}
function fail(it,why){showErr(why+" Ye stream browser me play nahi ho raha ("+fmtOf(it)+"). Kisi external player me kholo.",it)}
function hasVariants(it){return !!(it&&it.v&&it.v.length>1)}
function qLabel(q){return q&&q>=720?"HD":""}
function curQ(){if(CURQ!=="auto")return CURQ;var v=CUR&&CUR.v;if(v)for(var i=0;i<v.length;i++)if(v[i][1]===CUR.u)return v[i][0];return null}
function lowerVariant(){if(!hasVariants(CUR))return null;var cq=curQ(),v=CUR.v;for(var i=0;i<v.length;i++)if(cq===null||v[i][0]===cq)return v[i];return null}
function updQBtn(){var b=s("#bQ"),hl=hls&&hls.levels&&hls.levels.length>1;if(!hl&&!hasVariants(CUR)){b.hidden=true;return}b.hidden=false;if(hl){var lv=hls.levels[hls.currentLevel];b.textContent=hls.currentLevel>=0&&lv?lv.height+"p":"Auto"}else{var q=curQ();b.textContent=q?q+"p":"Auto"}}
function buildQuality(){updQBtn()}
function setQuality(v){var u=CUR.u;if(v!=="auto")for(var i=0;i<CUR.v.length;i++)if(CUR.v[i][0]===v){CUR.u=CUR.v[i][1];break}CURQ=v;loadItem(CUR,u,true,V.currentTime)}
function loadItem(it,opt){opt=opt||{};destroyEngines();showErr("");retries=0;V.hidden=true;IMGV.hidden=true;s("#aud").hidden=true;s("#menu").hidden=true;if(!opt.keepQ)CURQ="auto";var u=opt.url||it.u;CURURL=u;SEEKAT=opt.at||0;if(it.k==="IMAGE"){IMGV.hidden=false;IMGV.referrerPolicy="no-referrer";IMGV.src=it.u;setLoading(false);updQBtn();return}if(it.k==="PDF"){showErr("PDF file hai.",it);return}V.hidden=false;s("#aud").hidden=!isAudio(it);setLoading(true);updQBtn();var eng=engineOf(it,u),k=it.k;var go2=function(){var p=V.play();if(p)p.catch(function(){})};if(eng==="hls"){loadScript(HLSURL).then(function(){if(window.Hls&&Hls.isSupported()){hls=new Hls({enableWorker:true,maxBufferLength:40});hls.on(Hls.Events.MANIFEST_PARSED,function(){buildQuality();go2()});hls.on(Hls.Events.LEVEL_SWITCHED,function(){updQBtn()});hls.on(Hls.Events.ERROR,function(e,d){if(!d.fatal)return;if(d.type===Hls.ErrorTypes.NETWORK_ERROR&&retries<2){retries++;hls.startLoad()}else if(d.type===Hls.ErrorTypes.MEDIA_ERROR&&retries<3){retries++;hls.recoverMediaError()}else{fail(it,"HLS stream load nahi hui (CORS/expired link/block ho sakta hai)")}});hls.loadSource(u);hls.attachMedia(V)}else if(V.canPlayType("application/vnd.apple.mpegurl")){V.src=u;go2()}else{fail(it,"HLS support nahi")}},function(){if(V.canPlayType("application/vnd.apple.mpegurl")){V.src=u;go2()}else{fail(it,"hls.js load nahi hui (internet?)")}})}else if(eng==="dash"){loadScript(DASHURL).then(function(){dash=dashjs.MediaPlayer().create();dash.initialize(V,u,true);dash.on(dashjs.MediaPlayer.events.ERROR,function(){fail(it,"dash.js load nahi hui")})},function(){fail(it,"dash.js load nahi hui")})}else if(eng==="mpegts"){loadScript(TSURL).then(function(){if(window.mpegts&&mpegts.isSupported()){mp=mpegts.createPlayer({type:extOf(u)==="flv"?"flv":"mpegts",url:u,isLive:false});mp.attachMediaElement(V);mp.load();go2()}else{fail(it,"TS/FLV is browser me supported nahi")}},function(){fail(it,"mpegts.js load nahi hui")})}else{V.src=u;go2()}}
function togglePlay(){if(V.paused){var p=V.play();if(p)p.catch(function(){})}else{V.pause()}}
function skip(s,rip){if(isFinite(V.duration)&&V.duration>0)V.currentTime=Math.max(0,Math.min(V.duration,V.currentTime+s));else V.currentTime=Math.max(0,V.currentTime+s);if(rip){rip.classList.add("on");setTimeout(function(){rip.classList.remove("on")},450)}}
function showCtl(){PL.classList.add("show");clearTimeout(hideT);if(!V.paused){hideT=setTimeout(function(){PL.classList.remove("show");s("#menu").hidden=true},2800)}}
function updPlayIcon(){var b=s("#bPlay");b.innerHTML=V.paused?"▶":"⏸";PL.classList.toggle("paused",V.paused)}
function updTime(){var d=V.duration,c=V.currentTime||0;s("#tm").textContent=fmtTime(c)+(isFinite(d)?"/ "+fmtTime(d):" / LIVE");if(isFinite(d)&&d>0&&!dragging){var p=c/d*100;s("#pro").style.width=p+"%";s("#knob").style.left=p+"%"}}
function updBuf(){try{var d=V.duration,b=V.buffered;if(b.length&&isFinite(d)&&d>0){var c=V.currentTime,e=0;for(var i=0;i<b.length;i++)if(b.start(i)<=c&&b.end(i)>=c)e=b.end(i);s("#buf").style.width=e/d*100+"%"}}catch(e){}}
V.addEventListener("play",function(){updPlayIcon();showCtl()});
V.addEventListener("pause",function(){updPlayIcon();showCtl()});
V.addEventListener("waiting",function(){setLoading(true)});
V.addEventListener("playing",function(){setLoading(false);showErr("")});
V.addEventListener("canplay",function(){setLoading(false)});
V.addEventListener("progress",updBuf);
V.addEventListener("timeupdate",function(){updTime();updBuf();if(CUR&&Date.now()-lastSave>4000&&V.currentTime>1){lastSave=Date.now();saveHist(CUR,V.currentTime,isFinite(V.duration)?V.duration:0)}});
V.addEventListener("loadedmetadata",function(){updTime();if(CUR&&isFinite(V.duration))DUR[CUR.u]=V.duration;if(SEEKAT>1){try{V.currentTime=SEEKAT}catch(e){}SEEKAT=0;return}var h=CUR&&HIST[CUR.u];if(h&&h.pos>5&&isFinite(V.duration)&&h.pos<V.duration-8){V.currentTime=h.pos;toast("Resumed from "+fmtTime(h.pos))}});
V.addEventListener("ended",function(){if(CUR)saveHist(CUR,0,isFinite(V.duration)?V.duration:0);if(s("#auto").checked){var n=nextItem(1);if(n)go(n)}});
V.addEventListener("error",function(){if(CUR&&!hls&&!mp&&!dash)V.getAttribute("src");fail(CUR,"Ye format/codec browser support nahi karta (MKV/AVI/HEVC/WMV etc.)")});
V.addEventListener("volumechange",function(){s("#bVol").innerHTML=V.muted||V.volume===0?"🔇":"🔊";s("#vol").value=V.muted?0:V.volume});
s("#bVol").onclick=function(){V.muted=!V.muted};
s("#bPlay").onclick=togglePlay;
s("#bNext").onclick=function(){var n=nextItem(1);if(n)go(n);else toast("Aur video nahi hai")};
function upList(){return LIST.indexOf(CUR)>-1?LIST:DATA}
function nextItem(d){var l=upList(),i=l.indexOf(CUR);return l[i+d]||null}
/* seek bar */
var seek=s("#seek");
function frac(e){var r=seek.getBoundingClientRect();return Math.min(1,Math.max(0,(e.clientX-r.left)/r.width))}
seek.addEventListener("pointerdown",function(e){dragging=true;seek.classList.add("drag");try{seek.setPointerCapture(e.pointerId)}catch(x){}move(e)});
seek.addEventListener("pointermove",function(e){var f=frac(e),d=V.duration;if(isFinite(d)){s("#tip").textContent=fmtTime(f*d);s("#tip").style.left=f*100+"%"}if(dragging)move(e)});
seek.addEventListener("pointerup",function(e){if(dragging){move(e);dragging=false;seek.classList.remove("drag")}});
function move(e){var f=frac(e),d=V.duration;if(isFinite(d)&&d>0){s("#pro").style.width=f*100+"%";s("#knob").style.left=f*100+"%";V.currentTime=f*d}}
/* gestures on overlay */
var ov=s("#ov"), lastTap=0, tapT=null;
ov.addEventListener("click",function(e){var now=Date.now(),r=ov.getBoundingClientRect(),x=e.clientX-r.left;if(now-lastTap<300){clearTimeout(tapT);lastTap=0;if(x<.33*r.width)skip(-10,s("#ripL"));else if(x>.67*r.width)skip(10,s("#ripR"));else toggleFs()}else{lastTap=now;tapT=setTimeout(function(){if(isTouch&&!PL.classList.contains("show")&&!V.paused)showCtl();else togglePlay()},260)}});
PL.addEventListener("mousemove",showCtl);
PL.addEventListener("touchstart",showCtl,{passive:true});
/* menus */
var menu=s("#menu");
function openMenu(items,cur,fn){if(!menu.hidden){menu.k=items.join("|");menu.hidden=true;return}menu.textContent="";menu.k=items.join("|");items.forEach(function(it){var b=elt("button","",it.v===cur?"● "+it.l:"  "+it.l);b.onclick=function(){fn(it.v);menu.hidden=true};menu.appendChild(b)});menu.hidden=false}
s("#bSpd").onclick=function(){var sp=[.25,.5,.75,1,1.25,1.5,2,3,4].map(function(s){return {v:s,l:s+"x"}});openMenu(sp,V.playbackRate,function(v){V.playbackRate=v;s("#bSpd").textContent=v+"x"})};
s("#bQ").onclick=function(){if(hls&&hls.levels&&hls.levels.length>1){var it=[{v:-1,l:"Auto"}].concat(hls.levels.map(function(l,i){return {v:i,l:(l.height?qLabel(l.height):Math.round(l.bitrate/1000)+"k")+" ("+l.height+"p)"}}).sort(function(a,b){return b.h-a.h}));openMenu(it,hls.currentLevel,function(v){hls.currentLevel=v;updQBtn()})}else if(hasVariants(CUR)){var items=[{v:"auto",l:"Auto"}].concat(CUR.v.map(function(p){return {v:p[0],l:qLabel(p[0])}}));openMenu(items,CURQ,setQuality)}};
s("#bPip").onclick=function(){try{if(document.pictureInPictureElement)document.exitPictureInPicture();else if(V.requestPictureInPicture)V.requestPictureInPicture();else toast("PiP supported nahi")}catch(e){toast("PiP supported nahi")}};
function toggleTheater(){document.body.classList.toggle("theater")}
s("#bTh").onclick=toggleTheater;
function toggleFs(){var d=document;if(d.fullscreenElement||d.webkitFullscreenElement){d.exitFullscreen();d.webkitExitFullscreen()}else{var f=PL.requestFullscreen||PL.webkitRequestFullscreen;if(f)f.call(PL);else if(V.webkitEnterFullscreen)V.webkitEnterFullscreen()}}
s("#bFs").onclick=toggleFs;
document.addEventListener("keydown",function(e){if(s("#watch").hidden)return;var t=e.target,tn=t.tagName;if(tn==="SELECT"||tn==="TEXTAREA"||tn==="INPUT"&&(t.type!=="range"&&t.type!=="checkbox"))return;var k=e.key;if(k===" "||k==="k"){e.preventDefault();togglePlay()}else if(k==="ArrowRight")skip(5,s("#ripR"));else if(k==="ArrowLeft")skip(-5,s("#ripL"));else if(k==="l")skip(10,s("#ripR"));else if(k==="j")skip(-10,s("#ripL"));else if(k==="ArrowUp"){e.preventDefault();V.volume=Math.min(1,V.volume+.1)}else if(k==="ArrowDown"){e.preventDefault();V.volume=Math.max(0,V.volume-.1)}else if(k==="m")V.muted=!V.muted;else if(k==="f")toggleFs();else if(k==="t")toggleTheater();else if(k==="n"){var n=nextItem(1);if(n)go(n)}else if(k==="p"){var p=nextItem(-1);if(p)go(p)}else if(/[0-9]/.test(k)&&isFinite(V.duration))V.currentTime=V.duration*(+k)/10;if(k!==" ")showCtl()});
/* media session */
function setSession(it){if(!("mediaSession" in navigator))return;try{navigator.mediaSession.metadata=new MediaMetadata({title:it.t||"Video",artist:domainOf(it.u),artwork:THUMBS[it.u]?[{src:THUMBS[it.u],sizes:"320x180",type:"image/jpeg"}]:it.th?[{src:it.th}]:[]});navigator.mediaSession.setActionHandler("play",function(){V.play()});navigator.mediaSession.setActionHandler("pause",function(){V.pause()});navigator.mediaSession.setActionHandler("seekbackward",function(){skip(-10)});navigator.mediaSession.setActionHandler("seekforward",function(){skip(10)});navigator.mediaSession.setActionHandler("nexttrack",function(){var n=nextItem(1);if(n)go(n)});navigator.mediaSession.setActionHandler("previoustrack",function(){var p=nextItem(-1);if(p)go(p)})}catch(e){}}

/* watch page */
function actBtn(txt,fn,on){var b=elt("button","act"+(on?" on":""),txt);b.onclick=fn;return b}
function renderMeta(it){s("#wt").textContent=it.t||"Video";document.title=(it.t||"Video")+" - "+(CFG.title||CFG.owner||"Player");var a=s("#acts");a.textContent="";var fb=actBtn(FAV[it.u]?"❤ Favorited":"♡ Favorite",function(){toggleFav(it);fb.className="act"+(FAV[it.u]?" on":"");fb.textContent=FAV[it.u]?"❤ Favorited":"♡ Favorite"},!!FAV[it.u]);a.appendChild(fb);var lb=actBtn(LATER[it.u]?"⏱ Saved":"⏱ Watch later",function(){toggleLater(it);lb.className="act"+(LATER[it.u]?" on":"");lb.textContent=LATER[it.u]?"⏱ Saved":"⏱ Watch later"},!!LATER[it.u]);a.appendChild(lb);a.appendChild(actBtn("📋 Copy link",function(){copy(it.u)}));var dl=elt("a","act",engineOf(it,u)==="native"?"📥 Download":"📥 Open stream");dl.href=it.u;dl.target="_blank";dl.rel="noopener noreferrer";if(engineOf(it,u)!=="native")dl.setAttribute("download","");a.appendChild(dl);extLinks(it.u).slice(0,2).forEach(function(x){if(x.n==="Open link")return;var e=elt("a","act","📤 "+x.n);e.href=x.h;e.target="_blank";e.rel="noopener noreferrer";a.appendChild(e)});if(CFG.owner&&CFG.tg){var ob=elt("a","act","✈ "+CFG.owner);ob.href=CFG.tg;ob.target="_blank";ob.rel="noopener noreferrer";a.appendChild(ob)}if(it.p){var sp=elt("a","act","🔗 Source page");sp.href=it.p;sp.target="_blank";sp.rel="noopener noreferrer";a.appendChild(sp)}var d=s("#desc");d.textContent="";function row(k,v){var p=elt("div","");p.appendChild(elt("b","",k+": "));p.appendChild(document.createTextNode(v));d.appendChild(p)}row("Format",fmtOf(it));row("Source",domainOf(it.u)||"-");if(CFG.owner){var cr=elt("div","");cr.appendChild(elt("b","Credits: "));var ca=elt("a","",CFG.owner);ca.href=CFG.tg;ca.target="_blank";ca.rel="noopener noreferrer";ca.style.color="inherit";cr.appendChild(ca);d.appendChild(cr)}if(it.d&&DUR[it.u])row("Duration",fmtTime(DUR[it.u]));row("Stream",it.u)}
function renderUpNext(it){var box=s("#upn");box.textContent="";var l=upList(),i=l.indexOf(it),n=0;for(var j=i+1;j<l.length&&n<40;j++,n++){var x=l[j];var r=elt("div","up"),th=mkThumb(x),tx=elt("div","tx");tx.appendChild(elt("h3","ttl",x.t||"Video"));tx.appendChild(elt("div","sub",domainOf(x.u)+" • "+fmtOf(x)));r.appendChild(th);r.appendChild(tx);r.onclick=function(){go(x)};box.appendChild(r)}if(!n)box.appendChild(elt("div","sub","Aur video nahi hai"))}
function showWatch(i){var it=ALL[i];if(!it){location.hash="#";return}CUR=it;LIST=LIST.length?LIST:computeList();s("#home").hidden=true;chipsEl.hidden=true;s("#watch").hidden=false;renderMeta(it);renderUpNext(it);loadItem(it);setSession(it);window.scrollTo(0,0);showCtl()}
function showHome(){if(!s("#watch").hidden){destroyEngines();try{if(document.fullscreenElement)document.exitFullscreen()}catch(e){}}s("#watch").hidden=true;CUR=null;document.title=(CFG.title||"Player")+(CFG.owner?" - "+CFG.owner:"");s("#home").hidden=false;chipsEl.hidden=false;renderGrid();updChips()}
function route(){var m=location.hash.match(/^#w(\d+)$/);if(m)showWatch(+m[1]);else showHome()}

/* init */
function start(){s("#lock").hidden=false;s("#app").hidden=true;s("#siteT").textContent=CFG.title||"Player";if(CFG.owner){var ow=s("#ownT");ow.textContent="by "+CFG.owner;ow.hidden=false;if(CFG.tg)ow.href=CFG.tg}s("#wm").textContent=CFG.owner||"";var ft=s("#foot");ft.textContent="Credits: ";var fa=elt("a","",CFG.owner);fa.href=CFG.tg||"#";fa.target="_blank";fa.rel="noopener noreferrer";ft.appendChild(fa);var tg=s("#tgB");if(CFG.tg){tg.href=CFG.tg}else{tg.hidden=true}var th=LS.get("ytb_theme","dark");document.documentElement.setAttribute("data-theme",th);s("#setB").onclick=function(e){e.stopPropagation();gearMenu(this)};s("#themeB").onclick=function(){var n=document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark";document.documentElement.setAttribute("data-theme",n);LS.set("ytb_theme",n)};var q=s("#q"),qc=s("#qclr"),qt;q.oninput=function(){qc.hidden=!q.value;clearTimeout(qt);qt=setTimeout(function(){VIEW.q=q.value;renderGrid()},180)};qc.onclick=function(){q.value="";qc.hidden=true;VIEW.q="";renderGrid()};s("#sort").onchange=function(){VIEW.sort=this.value;renderGrid()};window.addEventListener("hashchange",route);route()}
function unlock(){var v=s("#pw").value;if(hashPw(v)===CFG.hash){SS.set("ytb_ok",CFG.hash);start()}else{s("#lerr").textContent="Incorrect password"}}
if(!CFG.hash||SS.get("ytb_ok")===CFG.hash){start()}else{s("#lock").hidden=false;if(CFG.owner){var lo=s("#lockOwn"),la=elt("a","","by "+CFG.owner);la.href=CFG.tg;la.target="_blank";la.rel="noopener noreferrer";lo.appendChild(la)}}s("#pwb").onclick=unlock;s("#pw").onkeydown=function(e){if(e.key==="Enter")unlock()};
window.ytbcur=function(){return {q:CURQ,u:CURURL,sha256:sha256,hashPw:hashPw,fmtOf:fmtOf,engineOf:engineOf,computeList:computeList,state:function(){return {ALL:ALL,VIEW:VIEW,FAV:FAV}}}};
})();
</script>
</body>
</html>'''


def generate_webapp_html(results: List[dict], title: str = "Scraped Video Web Player") -> str:
    """
    Self-contained YouTube-style web player (password gate, auto thumbnails, favorites, history,
    HLS/MP4/WebM/MKV/DASH/TS/FLV/audio, external-player fallback).
    """
    items = []
    for it in results:
        variants = []
        for vq, vu in it.get("variants") or []:
            variants.append([vq, vu])
        items.append({
            "t": it.get("title") or "Video",
            "u": it["download_link"],
            "k": it.get("type") or "VIDEO",
            "p": it.get("page_url") or "",
            "th": it.get("thumb") or "",
            "d": it.get("duration") or 0,
            "v": variants,
            "ip": it.get("iplock") or "",
        })

    def js(o) -> str:
        return json.dumps(o, ensure_ascii=False).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")

    cfg = {
        "title": title,
        "owner": BOT_OWNER_NAME,
        "tg": TELEGRAM_LINK,
        "hash": hashlib.sha256(SKY_PASSWORD.encode("utf-8")).hexdigest() if SKY_PASSWORD else "",
    }

    return (
        PLAYERTEMPLATE
        .replace("{{OWNER}}", _html.escape(BOT_OWNER_NAME))
        .replace("{{TITLE}}", _html.escape(title))
        .replace("{{CFG}}", js(cfg))
        .replace("{{DATA}}", js(items))
    )


# ==========================================================
# TELEGRAM BOT COMMANDS (MINIMAL SET)
# ==========================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text("Access Denied! Aap is bot ko use nahi kar sakte.")
        return
    await update.message.reply_text(
        "43-Site Dedicated Bulk Link Scraper Bot Active!\n\n"
        "Features:\n"
        "1. Full Web Player UI (Custom Video Media Player interface in HTML).\n"
        "2. 4 Files Export (2 TXT + 2 HTML Files: Full Web App + Simple List).\n"
        "3. FFmpeg Downloader (Upload .txt file to auto-download & send video).\n\n"
        "Commands: /site, /scr, /addsite, /addscr, /delscr, /removesite, /login, /logout, /cookie, /updatecookie, /stop, /stats, /userlist, /debug, /dump, /sniff, /prefer, /sky, /settings, /jobs, /cancel, /watch, /watchlist, /unwatch, /backup"
    )


async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        add_user_db(uid)
        await update.message.reply_text(f"User {uid} added.", parse_mode="Markdown")


async def remove_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        remove_user_db(uid)
        await update.message.reply_text(f"User {uid} removed.", parse_mode="Markdown")


async def user_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_allowed(update.effective_user.id):
        return
    users = get_all_users()
    msg = "Authorized Users:\n"
    for uid in users:
        role = "Admin" if uid == ADMIN_ID else "User"
        msg += f"- `{uid}` ({role})\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_allowed(update.effective_user.id):
        return
    users_count = len(get_all_users())
    await update.message.reply_text(
        f"Bot Status\n"
        f"- Authorized Users: {users_count}\n"
        f"- Dedicated Site Extractors: 43 Sites Active\n"
        f"- Engine Status: 24/7 Active"
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STOP_PROCESS[update.effective_user.id] = True
    await update.message.reply_text("Process Stop Request Sent!")


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/debug <url> - shows exactly why a site fails (fetch, link discovery, extraction)."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /debug <listing_or_video_url>")
        return
    url = context.args[0]
    reset_host_stats(url)
    t0 = time.time()
    html = await fetch(url)
    dt = time.time() - t0
    if not html:
        await update.message.reply_text(
            f"Fetch failed after {dt:.1f}s.\n"
            f"HTTP: {LAST_STATUS.get(url, '?')}\n"
            f"Engines: {LAST_DETAIL.get(url, '?')}\n"
            f"Error: {LAST_ERR.get(url, '?')}\n"
            f"{err_hint(url)}\n"
            f"curl_cffi installed: {'YES' if cffi_requests else 'NO'}\n"
            f"Proxy set: {'YES' if PROXY_URL else 'NO'}\n"
            "403/503 = Cloudflare/IP block, 404 = wrong URL ?, timeout = DNS?\n"
        )
        return
    wk = fix_url(url)
    lines = [
        f"Working URL: {wk}" + (" (tumhara URL fail hua, ye chalta hai - bot ab isi ko use karega)" if wk != url else ""),
        f"Fetched {len(html)} bytes in {dt:.1f}s",
        f"Engine: {_BEST_ENGINE.get(_root_host(urlparse(url).netloc), '?')}",
        f"curl_cffi: {'YES' if cffi_requests else 'NO'}",
        link_stats(html, url),
    ]
    if _looks_like_single_video(url):
        if get_cookie_for_url(url):
            a = await _extract_video_link_impl(url, url, True)
            b = await _extract_video_link_impl(url, url, False)
            lines.append(f"With login: {a['download_link'] if a else 'FAILED'}")
            lines.append(f"Without login: {b['download_link'] if b else 'FAILED'}")
            item = a or b
        else:
            item = await extract_video_link(url, source_page=url)
            lines.append(f"Extract this video page (no login saved): {item['download_link'] if item else 'FAILED'}")
        if not item:
            lines.append(page_diag(html))
            qs = _quick_streams(html, url)
            lines.append(f"Page me streams: {len(qs)}")
            lines += [f"  - {x[:105]}" for x in qs[:4]]
        await update.message.reply_text("\n".join(lines)[:4000], disable_web_page_preview=True)
        return
    links = find_video_links(html, url)
    lines.append(f"Video-like links on page: {len(links)}")
    if links:
        item = await extract_video_link(links[0], source_page=url)
        s = item["download_link"] if item else None
        lines.append(f"Extract test on 1st link: {s or 'FAILED'}")
        if not item:
            lines.append(note_hint())
        else:
            lines.append("Single video page? test with the real extractor (xhamster HLS picker included).")
    await update.message.reply_text("\n".join(lines)[:4000], disable_web_page_preview=True)


async def dump_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dump <url> - sends the raw HTML the bot receives, so it can be inspected."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /dump <url>")
        return
    url = context.args[0]
    html = await fetch(url)
    if not html:
        await update.message.reply_text(f"Fetch failed. HTTP {LAST_STATUS.get(url, '?')}")
        return
    buf = io.BytesIO(html.encode("utf-8", errors="ignore"))
    buf.name = "page_dump.html"
    await update.message.reply_document(document=buf, caption=f"Raw HTML ({len(html)} bytes) of {url}")


# ==========================================================
# MAIN ENTRY
# ==========================================================
def main():
    init_db()
    load_env_cookies()
    load_prefers()

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set. Exiting.")
        return

    # Start dummy HTTP server + self-ping (24/7 keep-alive)
    threading.Thread(target=run_dummy_server, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("adduser", add_user_command))
    app.add_handler(CommandHandler("removeuser", remove_user_command))
    app.add_handler(CommandHandler("userlist", user_list_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CommandHandler("dump", dump_command))

    logger.info("Bot polling started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
