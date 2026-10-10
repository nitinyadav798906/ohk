import asyncio
import hashlib
import hmac
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
import ipaddress
import secrets
import socket
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Optional, List, Dict
from urllib.parse import unquote, urljoin, urlparse, urlunparse, parse_qs, parse_qsl, quote, urlsplit

import cloudscraper
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
try:                                    # Mini App (python-telegram-bot v20+)
    from telegram import WebAppInfo, MenuButtonWebApp
except ImportError:
    WebAppInfo = MenuButtonWebApp = None
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mini_state (
            user_id INTEGER PRIMARY KEY,
            data TEXT,
            updated REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mini_shares (
            token TEXT PRIMARY KEY,
            owner INTEGER,
            by_name TEXT,
            name TEXT,
            data TEXT,
            created REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS site_health (
            domain TEXT, ts REAL, links INTEGER, ok INTEGER
        )
    """)
    try:
        cursor.execute("ALTER TABLE mini_shares ADD COLUMN expires REAL")
    except sqlite3.OperationalError:
        pass
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
    globals()["_COOKIE_CACHE"] = None
    try:                                   # nayi cookie ke baad purane (logged-out) cached pages hata do
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
    globals()["_COOKIE_CACHE"] = None
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

_COOKIE_CACHE: Optional[list] = None




def cookie_age_warnings(max_days: float = 14.0) -> List[str]:
    """Return human lines for cookies older than max_days (admin alert)."""
    out = []
    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute("SELECT domain, updated FROM site_cookies").fetchall()
        conn.close()
        now = time.time()
        for dom, upd in rows:
            try:
                # updated may be string CURRENT_TIMESTAMP style
                if isinstance(upd, (int, float)):
                    ts = float(upd)
                else:
                    ts = time.mktime(time.strptime(str(upd)[:19], "%Y-%m-%d %H:%M:%S"))
                age_d = (now - ts) / 86400
                if age_d >= max_days:
                    out.append(f"• {dom}: {age_d:.0f} days old")
            except Exception:
                continue
    except Exception:
        pass
    return out


def get_cookie_for_url(url: str) -> Optional[str]:
    global _COOKIE_CACHE
    host = normalize_domain(url)
    if not host:
        return None
    rows = _COOKIE_CACHE
    if rows is None:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT domain, cookie FROM site_cookies")
            rows = cursor.fetchall()
            conn.close()
            _COOKIE_CACHE = rows
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
# ---- stream proxy: acctoken / IP-lock / Referer-lock links player me bot ke through chalane ke liye ----
PROXY_BASE = (os.getenv("PROXY_BASE") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
_PX_SECRET = hashlib.sha256(((BOT_TOKEN or "dev") + "|px").encode()).digest()


def px_sig(u: str, r: str) -> str:
    return hmac.new(_PX_SECRET, (u + "\n" + r).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def px_path(u: str, r: str = "") -> str:
    return f"/p?u={quote(u, safe='')}&r={quote(r, safe='')}&s={px_sig(u, r)}"


def px_url(u: str, r: str = "") -> str:
    return (PROXY_BASE + px_path(u, r)) if PROXY_BASE else ""


def px_needs(u: str, iplock: str = "") -> bool:
    return bool(iplock) or bool(re.search(r'acctoken|[?&]ip=\d', u, re.I))


def _px_host_ok(host: str) -> bool:
    """SSRF guard: private/loopback/link-local IPs par proxy nahi (sirf test ke liye PROXY_ALLOW_PRIVATE=1)."""
    if os.getenv("PROXY_ALLOW_PRIVATE") == "1":
        return True
    try:
        for fam, _t, _p, _c, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                return False
        return True
    except Exception:
        return False


def px_rewrite_m3u8(text: str, base: str, ref: str) -> str:
    def P(u):
        return px_path(u, ref)
    out = []
    for ln in text.splitlines():
        t = ln.strip()
        if not t:
            out.append(ln)
        elif t.startswith("#"):
            out.append(re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + P(urljoin(base, m.group(1))) + '"', ln))
        else:
            out.append(P(urljoin(base, t)))
    return "\n".join(out) + "\n"


def px_rewrite_mpd(text: str, base: str, ref: str) -> str:
    """DASH MPD: BaseURL / SegmentTemplate media / initialization / SegmentURL media relative URLs ko proxy path me rewrite."""
    def P(u: str) -> str:
        u = (u or "").strip()
        if not u or u.startswith("data:") or u.startswith("#"):
            return u
        full = urljoin(base, u)
        if not full.startswith(("http://", "https://")):
            return u
        return px_path(full, ref)

    def repl_attr(m):
        return m.group(1) + P(m.group(2)) + m.group(3)

    out = text
    # BaseURL text content
    out = re.sub(
        r'(<BaseURL[^>]*>)([^<]+)(</BaseURL>)',
        lambda m: m.group(1) + P(m.group(2).strip()) + m.group(3),
        out, flags=re.I)
    # media= / initialization= / sourceURL= attributes (SegmentTemplate, SegmentURL, ...)
    out = re.sub(
        r'\b((?:media|initialization|sourceURL|bitstreamSwitchingURL)=["\'])([^"\']+)(["\'])',
        repl_attr, out, flags=re.I)
    return out


_PX_USAGE: Dict[str, object] = {"day": "", "total": 0, "ip": {}}


def px_account(ip: str, nbytes: int = 0, check: bool = False) -> bool:
    """Proxy bandwidth: har IP ka daily cap (env PROXY_DAILY_MB_PER_IP, default 2048) + optional total cap (PROXY_DAILY_MB_TOTAL)."""
    day = time.strftime("%Y-%m-%d")
    if _PX_USAGE["day"] != day:
        _PX_USAGE.update(day=day, total=0, ip={})
    if check:
        try:
            cap_ip = float(os.getenv("PROXY_DAILY_MB_PER_IP", "2048")) * 1048576
            cap_all = float(os.getenv("PROXY_DAILY_MB_TOTAL", "0")) * 1048576
        except ValueError:
            return True
        return not ((cap_ip and _PX_USAGE["ip"].get(ip, 0) >= cap_ip) or (cap_all and _PX_USAGE["total"] >= cap_all))
    _PX_USAGE["ip"][ip] = _PX_USAGE["ip"].get(ip, 0) + nbytes
    _PX_USAGE["total"] += nbytes
    return True


def px_usage_info() -> dict:
    ips = sorted(_PX_USAGE["ip"].items(), key=lambda kv: -kv[1])[:5]
    mask = lambda ip: re.sub(r'(\d+)$', 'x', ip) if '.' in ip else ip[:8] + '..'
    return {"day": _PX_USAGE["day"], "total_mb": round(_PX_USAGE["total"] / 1048576, 1),
            "ips": [[mask(i), round(b / 1048576, 1)] for i, b in ips]}


_RF_CACHE: Dict[str, tuple] = {}


def rf_url(page_url: str) -> str:
    return f"{PROXY_BASE}/r?p={quote(page_url, safe='')}&s={px_sig(page_url, 'refresh')}"


async def _refresh_one(p: str) -> Optional[dict]:
    _STREAM_CACHE.pop(p, None)
    r = await guarded_extract(p, p)
    if not r or r.get("preview_only"):
        return None
    return _player_item(r)


def rf_handle(h):
    """/r?p=<page_url>&s=<sig> : expire hui link ke liye page se NAYI stream nikalo (HTML player + Mini App, dono)."""
    q = parse_qs(urlsplit(h.path).query)
    p = (q.get("p") or [""])[0]
    sg = (q.get("s") or [""])[0]

    def out(code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        h.send_response(code)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Access-Control-Allow-Origin", "*")
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(body)

    if not p.startswith(("http://", "https://")) or not hmac.compare_digest(sg, px_sig(p, "refresh")):
        return out(403, {"error": "bad signature"})
    hit = _RF_CACHE.get(p)
    if hit and time.time() - hit[0] < 90:
        return out(200, hit[1])
    try:
        res = asyncio.run_coroutine_threadsafe(_refresh_one(p), MAIN_LOOP).result(100)
    except Exception:
        return out(502, {"error": "refresh fail"})
    if not res:
        return out(404, {"error": "naya link nahi mila"})
    if len(_RF_CACHE) > 500:
        _RF_CACHE.clear()
    _RF_CACHE[p] = (time.time(), res)
    return out(200, res)


def record_health(url: str, links: int, ok_n: int):
    """Har scrape ka success rate yaad rakho; achanak girne par admin ko alert."""
    if links < 3:
        return
    dom = _root_host(urlparse(url).netloc)
    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute("SELECT links, ok FROM site_health WHERE domain=? ORDER BY ts DESC LIMIT 5", (dom,)).fetchall()
        conn.execute("INSERT INTO site_health (domain, ts, links, ok) VALUES (?,?,?,?)", (dom, time.time(), links, ok_n))
        conn.execute("DELETE FROM site_health WHERE ts < ?", (time.time() - 30 * 86400,))
        conn.commit()
        conn.close()
    except Exception:
        return
    prev = (sum(r[1] for r in rows) / max(1, sum(r[0] for r in rows))) if len(rows) >= 2 else None
    rate = ok_n / links
    if prev is not None and prev >= 0.7 and rate < 0.3 and MINI_BOT is not None and MAIN_LOOP is not None:
        try:
            asyncio.run_coroutine_threadsafe(MINI_BOT.send_message(
                ADMIN_ID, f"⚠️ Site health: {dom} ka success rate {prev:.0%} se gir ke {rate:.0%} ho gaya.\n"
                          f"Check: /debug https://{dom}/"), MAIN_LOOP)
        except Exception:
            pass


def health_overview() -> list:
    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute("SELECT domain, links, ok, ts FROM site_health ORDER BY ts DESC LIMIT 400").fetchall()
        conn.close()
    except Exception:
        return []
    by: Dict[str, list] = {}
    for d, l, o, ts in rows:
        by.setdefault(d, []).append((l, o, ts))
    out = []
    for d, lst in by.items():
        last5 = lst[:5]
        out.append({"domain": d, "runs": len(lst), "last": round(lst[0][1] / max(1, lst[0][0]), 2),
                    "avg": round(sum(x[1] for x in last5) / max(1, sum(x[0] for x in last5)), 2), "ts": lst[0][2]})
    return sorted(out, key=lambda x: x["last"])


def px_handle(h, head: bool = False):
    q = parse_qs(urlsplit(h.path).query)
    u = (q.get("u") or [""])[0]
    r = (q.get("r") or [""])[0]
    sg = (q.get("s") or [""])[0]

    def cors():
        h.send_header("Access-Control-Allow-Origin", "*")
        h.send_header("Access-Control-Allow-Headers", "Range")
        h.send_header("Access-Control-Expose-Headers", "Content-Length,Content-Range,Accept-Ranges")

    def deny(code=403):
        h.send_response(code)
        cors()
        h.send_header("Content-Length", "0")
        h.end_headers()

    if not u.startswith(("http://", "https://")) or not hmac.compare_digest(sg, px_sig(u, r)):
        return deny(403)
    ip = (h.headers.get("X-Forwarded-For") or h.client_address[0]).split(",")[0].strip()
    if not px_account(ip, check=True):
        return deny(429)
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    if r:
        hdrs["Referer"] = r
    if h.headers.get("Range"):
        hdrs["Range"] = h.headers["Range"]
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    root0, cur, resp = _root_host(urlparse(u).netloc), u, None
    try:
        for _ in range(6):
            if not _px_host_ok(urlparse(cur).hostname or ""):
                return deny(403)
            hh = dict(hdrs)
            if _root_host(urlparse(cur).netloc) == root0:       # cookie sirf usi site ko, CDN ko nahi
                ck = get_cookie_for_url(cur)
                ex = globals().get("_SCR_EXTRA_COOKIES", {}).get(root0)
                ck = "; ".join(x for x in (ck, ex) if x)
                if ck:
                    hh["Cookie"] = ck
            resp = requests.get(cur, headers=hh, stream=True, timeout=(8, 30), allow_redirects=False, proxies=proxies)
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                cur = urljoin(cur, resp.headers["Location"])
                resp.close()
                continue
            break
        ct = resp.headers.get("Content-Type", "")
        path_l = urlparse(cur).path.lower()
        is_m3u8 = ".m3u8" in path_l or "mpegurl" in ct.lower()
        is_mpd = ".mpd" in path_l or "dash+xml" in ct.lower() or "mpeg-dash" in ct.lower()
        if resp.status_code == 200 and (is_m3u8 or is_mpd):
            data = b""
            for ch in resp.iter_content(65536):
                data += ch
                if len(data) > 4_000_000:
                    break
            txt = data.decode("utf-8", "ignore")
            if is_mpd:
                body = px_rewrite_mpd(txt, cur, r).encode("utf-8")
                ctype = "application/dash+xml"
            else:
                body = px_rewrite_m3u8(txt, cur, r).encode("utf-8")
                ctype = "application/vnd.apple.mpegurl"
            h.send_response(200)
            h.send_header("Content-Type", ctype)
            h.send_header("Content-Length", str(len(body)))
            cors()
            h.end_headers()
            if not head:
                h.wfile.write(body)
                px_account(ip, len(body))
            return
        h.send_response(resp.status_code)
        for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
            if resp.headers.get(k):
                h.send_header(k, resp.headers[k])
        cors()
        h.end_headers()
        if not head:
            for ch in resp.iter_content(65536):
                h.wfile.write(ch)
                px_account(ip, len(ch))
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as e:
        logger.warning(f"proxy error {u[:80]}: {e}")
        try:
            deny(502)
        except Exception:
            pass
    finally:
        try:
            if resp is not None:
                resp.close()
        except Exception:
            pass


# ==========================================================
# TELEGRAM MINI APP: same server (PORT) se /app page + /api/* (Telegram login se secured)
# ==========================================================
MAIN_LOOP = None            # bot ka asyncio loop (_post_init me set)
MINI_BOT = None
MINI_JOBS: Dict[str, dict] = {}
MINI_BOT_USERNAME = ""


def tg_verify_init(init_data: str, max_age: int = 86400) -> Optional[dict]:
    """Telegram WebApp initData ka HMAC check (official algorithm). -> user dict ya None."""
    try:
        d = dict(parse_qsl(init_data or "", keep_blank_values=True))
        h = d.pop("hash", None)
        if not h or not BOT_TOKEN:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if time.time() - int(d.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(d.get("user", "{}"))
        return user if isinstance(user, dict) and user.get("id") else None
    except Exception:
        return None


def mini_state_get(uid: int) -> dict:
    try:
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute("SELECT data, updated FROM mini_state WHERE user_id=?", (uid,)).fetchone()
        conn.close()
        if row:
            d = json.loads(row[0] or "{}")
            d["ts"] = row[1]
            return d
    except Exception:
        pass
    return {}


def mini_state_set(uid: int, data: dict):
    clean = {k: data.get(k) for k in ("fav", "later", "hist", "lib") if k in data}
    raw = json.dumps(clean, ensure_ascii=False)
    if len(raw) > 1_800_000:
        raise ValueError("state bahut bada hai")
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT INTO mini_state (user_id, data, updated) VALUES (?, ?, ?) "
                 "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                 (uid, raw, time.time()))
    conn.commit()
    conn.close()


async def _mini_job_run(jid: str, uid: int, url, start: int, end: int):
    j = MINI_JOBS[jid]
    urls = [url] if isinstance(url, str) else list(url)
    STOP_PROCESS[uid] = False
    jj = job_start(uid, "mini", urls[0], start, end)
    merged: List[dict] = []
    seen = set()
    rep_all = {"pages_ok": 0, "links": 0, "extracted": 0, "cached": 0}
    try:
        for idx, u in enumerate(urls, 1):
            if STOP_PROCESS.get(uid):
                break
            j["part"] = f"{idx}/{len(urls)}" if len(urls) > 1 else ""

            async def progress(done, total):
                j["done"], j["total"] = done, total

            try:
                u2 = await scr_preflight(u)                # age-gate bypass, jaise /scr me
            except Exception:
                u2 = u
            try:
                results, rep = await scr_scrape(u2, start, end, uid, progress)
            except Exception as e:
                logger.error(f"mini job url error {u}: {e}")
                continue
            for r in results:
                it = _player_item(r)
                if it["u"] not in seen:
                    seen.add(it["u"])
                    merged.append(it)
            for k in rep_all:
                rep_all[k] += int(rep.get(k) or 0)
        j["items"] = merged
        j["rep"] = rep_all
        j["status"] = "done"
    except Exception as e:
        logger.error(f"mini job error: {e}")
        j.update(status="error", error=str(e)[:200])
    finally:
        job_end(jj)


def mini_job_start(uid: int, url: str, start: int, end: int) -> str:
    now = time.time()
    for k in [k for k, v in MINI_JOBS.items() if now - v["t0"] > 3600]:
        MINI_JOBS.pop(k, None)
    if any(v["uid"] == uid and v["status"] == "running" for v in MINI_JOBS.values()):
        raise ValueError("Pehla scrape abhi chal raha hai (Stop dabao ya ruko)")
    jid = secrets.token_hex(6)
    MINI_JOBS[jid] = {"uid": uid, "status": "running", "done": 0, "total": 0, "items": [], "t0": now, "url": url}
    asyncio.run_coroutine_threadsafe(_mini_job_run(jid, uid, url, start, end), MAIN_LOOP)
    return jid


def normalize_setting(key: str, val: str) -> str:
    key, val = (key or "").lower(), (val or "").strip()
    if key not in _SETTING_DEFAULTS:
        raise ValueError(f"Unknown setting: {key}")
    if key in ("verify", "ytdlp", "keep_preview"):
        return "1" if val.lower() in ("on", "1", "yes", "true") else "0"
    if key == "proxy":
        return val.lower() if val.lower() in ("auto", "on", "off") else "auto"
    if key == "min_quality":
        if val.lower() in ("", "off", "0", "none"):
            return "0"
        if not val.isdigit():
            raise ValueError("min_quality number do (jaise 720)")
        return val
    if key == "export":
        return ",".join(x for x in re.split(r'[,\s]+', val.lower()) if x in ("m3u", "json", "csv"))
    return "" if val.lower() in ("off", "none", "clear", "-") else val.lower()


def admin_overview() -> dict:
    saved, rules, custom = set(list_cookie_domains()), set(list_rule_domains()), set(list_custom_sites_db())
    return {
        "sites": [{"domain": d, "signed": d in saved, "rule": d in rules, "custom": d in custom} for d in get_all_sites()],
        "cookies": sorted(saved),
        "jobs": [{"id": j, "kind": v["kind"], "pages": v["pages"], "secs": int(time.time() - v["t0"]),
                  "url": normalize_domain(v["url"]), "user": v["user"]} for j, v in JOBS.items()],
        "settings": {k: (get_setting(k) or "") for k in _SETTING_DEFAULTS},
        "users": get_all_users(), "prefer": dict(_PREFER), "proxy": PROXY_BASE, "admin": ADMIN_ID,
        "health": health_overview(), "px_usage": px_usage_info()}


def _admin_api(path: str, body: dict) -> dict:
    act = body.get("action")
    if path == "/api/admin/overview":
        return admin_overview()
    if path == "/api/admin/setting":
        key = str(body.get("key") or "")
        set_setting(key.lower(), normalize_setting(key, str(body.get("value") or "")))
        return {"ok": True, "value": get_setting(key.lower())}
    if path == "/api/admin/job":
        if body.get("all"):
            for v in JOBS.values():
                STOP_PROCESS[v["user"]] = True
        else:
            STOP_PROCESS[int(body.get("user") or 0)] = True
        return {"ok": True}
    if path == "/api/admin/user":
        uid2 = int(body.get("id") or 0)
        if not uid2:
            raise ValueError("user id do")
        if act == "remove":
            if uid2 == ADMIN_ID:
                raise ValueError("admin ko hata nahi sakte")
            remove_user_db(uid2)
        else:
            add_user_db(uid2)
        return {"ok": True}
    if path == "/api/admin/prefer":
        dom = _root_host(normalize_domain(str(body.get("site") or "")))
        if not DOMAIN_RE.match(dom):
            raise ValueError("Invalid site")
        raw = str(body.get("host") or "").strip()
        if not raw or raw.lower() in ("off", "clear", "none"):
            save_prefer(dom, [])
        else:
            host = (urlparse(raw).netloc if "://" in raw else raw).lower().split(":")[0]
            host = host[4:] if host.startswith("www.") else host
            if not DOMAIN_RE.match(host):
                raise ValueError("Invalid host")
            cur = list(_PREFER.get(dom, []))
            save_prefer(dom, cur if host in cur else cur + [host])
        return {"ok": True}
    dom = normalize_domain(str(body.get("domain") or ""))
    if not DOMAIN_RE.match(dom):
        raise ValueError("Invalid domain")
    if path == "/api/admin/cookie":
        if act == "del":
            return {"ok": delete_cookie_db(dom)}
        ck = clean_cookie(str(body.get("cookie") or ""))
        if "=" not in ck:
            raise ValueError("cookie format: name=value; name2=value2")
        if body.get("merge"):
            old = get_cookie_for_url(f"https://{dom}/")
            if old:
                od = parse_cookie_str(old)
                od.update(parse_cookie_str(ck))
                ck = "; ".join(f"{k}={v}" for k, v in od.items())
        set_cookie_db(dom, ck)
        REDIRECTED.clear()
        return {"ok": True, "count": len(parse_cookie_str(ck))}
    if path == "/api/admin/site":
        if act == "remove":
            if dom in SITES_FULL:
                raise ValueError("built-in site hata nahi sakte")
            return {"ok": remove_custom_site_db(dom)}
        if dom not in get_all_sites():
            add_custom_site_db(dom, ADMIN_ID)
        return {"ok": True}
    if path == "/api/admin/test":
        url = f"https://{dom}/"
        html = asyncio.run_coroutine_threadsafe(fetch(url), MAIN_LOOP).result(70)
        return {"ok": bool(html), "status": str(LAST_STATUS.get(url, "")), "detail": LAST_DETAIL.get(url, "")}
    raise ValueError("unknown admin endpoint")


def mini_page(h):
    body = generate_web_app_html([], title="Mini Player", mini=True).encode("utf-8")
    h.send_response(200)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)


def api_handle(h, method: str):
    path = urlsplit(h.path).path
    q = parse_qs(urlsplit(h.path).query)

    def send(code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        h.send_response(code)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(body)

    try:
        user = tg_verify_init(h.headers.get("X-Init-Data", ""))
        if not user:
            return send(401, {"error": "Telegram Mini App se kholo (login check fail)"})
        uid = int(user["id"])
        if not is_user_allowed(uid):
            return send(403, {"error": "Access denied"})
        body = {}
        if method == "POST":
            n = int(h.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                return send(413, {"error": "too large"})
            body = json.loads(h.rfile.read(n) or b"{}")
        if path == "/api/me":
            return send(200, {"id": uid, "name": user.get("first_name", ""), "admin": uid == ADMIN_ID,
                              "proxy": bool(PROXY_BASE)})
        if path == "/api/state":
            if method == "POST":
                mini_state_set(uid, body)
                return send(200, {"ok": True})
            return send(200, mini_state_get(uid))
        if path == "/api/scrape" and method == "POST":
            raw_urls = body.get("urls") or [body.get("url")]
            urls = [str(x).strip() for x in raw_urls if x][:10]
            if not urls or any(not re.match(r'^https?://', x, re.I) or not DOMAIN_RE.match(normalize_domain(x)) for x in urls):
                return send(400, {"error": "Valid http(s) URL do"})
            st = max(1, int(body.get("start") or 1))
            en = max(st, min(int(body.get("end") or 5), st + SCR_MAX_PAGES - 1))
            return send(200, {"id": mini_job_start(uid, urls[0] if len(urls) == 1 else urls, st, en)})
        if path == "/api/job":
            j = MINI_JOBS.get((q.get("id") or [""])[0])
            if not j or j["uid"] != uid:
                return send(404, {"error": "job nahi mila"})
            return send(200, {k: j.get(k) for k in ("status", "done", "total", "items", "error", "rep", "part")})
        if path == "/api/stop" and method == "POST":
            STOP_PROCESS[uid] = True
            return send(200, {"ok": True})
        if path == "/api/send" and method == "POST":
            items = [x for x in (body.get("items") or []) if isinstance(x, dict) and str(x.get("u", "")).startswith("http")][:3000]
            if not items:
                return send(400, {"error": "koi item nahi"})
            m3u = "#EXTM3U\n" + "".join(f"#EXTINF:-1,{str(x.get('t') or 'Video').replace(chr(10), ' ')}\n{x['u']}\n" for x in items)
            bio = io.BytesIO(m3u.encode("utf-8"))
            bio.name = "playlist.m3u"
            asyncio.run_coroutine_threadsafe(
                MINI_BOT.send_document(chat_id=uid, document=bio, caption=f"📤 {len(items)} links (Mini App se)"),
                MAIN_LOOP).result(40)
            return send(200, {"ok": True, "n": len(items)})
        if path == "/api/watch":
            if method == "POST":
                act = body.get("action")
                if act == "del":
                    return send(200, {"ok": watch_del(int(body.get("id") or 0), None if uid == ADMIN_ID else uid)})
                url = str(body.get("url") or "").strip()
                if not re.match(r'^https?://', url, re.I):
                    return send(400, {"error": "Valid URL do"})
                if len(watch_rows(uid)) >= (20 if uid == ADMIN_ID else 5):
                    return send(400, {"error": "watch limit poori"})
                mins = max(10, min(int(body.get("minutes") or 60), 1440))
                return send(200, {"ok": True, "id": watch_add(uid, url, mins)})
            return send(200, {"watches": [{"id": w[0], "url": w[2], "minutes": w[3]} for w in watch_rows(uid)]})
        if path.startswith("/api/admin/"):
            if uid != ADMIN_ID:
                return send(403, {"error": "sirf admin"})
            return send(200, _admin_api(path, body))
        if path == "/api/share" and method == "POST":
            items = [x for x in (body.get("items") or []) if isinstance(x, dict) and str(x.get("u", "")).startswith("http")][:500]
            if not items:
                return send(400, {"error": "koi item nahi"})
            raw = json.dumps(items, ensure_ascii=False)
            if len(raw) > 1_500_000:
                return send(413, {"error": "playlist bahut badi"})
            token = secrets.token_urlsafe(6)
            conn = sqlite3.connect(DB_FILE)
            days = int(body.get("days", 7) or 0)
            exp = (time.time() + days * 86400) if days > 0 else None
            conn.execute("INSERT INTO mini_shares (token, owner, by_name, name, data, created, expires) VALUES (?,?,?,?,?,?,?)",
                         (token, uid, str(user.get("first_name") or "")[:40], str(body.get("name") or "Playlist")[:60], raw, time.time(), exp))
            conn.commit()
            conn.close()
            return send(200, {"token": token, "web": f"{PROXY_BASE}/app?share={token}",
                              "link": f"https://t.me/{MINI_BOT_USERNAME}?startapp={token}" if MINI_BOT_USERNAME else ""})
        if path == "/api/shares":
            conn = sqlite3.connect(DB_FILE)
            rows = conn.execute("SELECT token, name, expires FROM mini_shares WHERE owner=? AND (expires IS NULL OR expires>?) "
                                "ORDER BY created DESC LIMIT 30", (uid, time.time())).fetchall()
            conn.close()
            return send(200, {"shares": [{"token": r[0], "name": r[1], "expires": r[2]} for r in rows]})
        if path == "/api/share/revoke" and method == "POST":
            conn = sqlite3.connect(DB_FILE)
            cur = conn.execute("DELETE FROM mini_shares WHERE token=? AND (owner=? OR ?)",
                               (str(body.get("token") or ""), uid, 1 if uid == ADMIN_ID else 0))
            conn.commit()
            n = cur.rowcount
            conn.close()
            return send(200, {"ok": n > 0})
        if path == "/api/shared":
            conn = sqlite3.connect(DB_FILE)
            row = conn.execute("SELECT name, by_name, data, expires FROM mini_shares WHERE token=?", ((q.get("t") or [""])[0],)).fetchone()
            conn.close()
            if row and row[3] and row[3] < time.time():
                row = None
            if not row:
                return send(404, {"error": "share link nahi mila / expire"})
            return send(200, {"name": row[0], "by": row[1], "items": json.loads(row[2])})
        return send(404, {"error": "unknown endpoint"})
    except ValueError as e:
        return send(400, {"error": str(e)})
    except Exception as e:
        logger.error(f"api error {path}: {e}")
        return send(500, {"error": "server error"})


class DummyPortServer(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.startswith("/api/"):
            return api_handle(self, "POST")
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/p?"):
            return px_handle(self)
        if self.path.startswith("/r?"):
            return rf_handle(self)
        if self.path.split("?")[0] in ("/app", "/app/"):
            return mini_page(self)
        if self.path.startswith("/api/"):
            return api_handle(self, "GET")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Status: Active and Running 24/7!")

    def do_HEAD(self):
        if self.path.startswith("/p?"):
            return px_handle(self, head=True)
        self.send_response(200)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.end_headers()

    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.getenv("PORT", 8080))
    try:
        server = ThreadingHTTPServer(('0.0.0.0', port), DummyPortServer)
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
    m = re.search(r'-(xh[A-Za-z0-9]+)/?$', pu.path)
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
        out += ("\n🔐 Ye account page hai (login chahiye). Cookie: "
                + ("saved ✅ (expire/galat ho sakti hai, /login dobara karo)" if has
                   else "SAVED NAHI ❌ -> /login <domain> <cookie>")
                + "\nℹ️ Render restart/redeploy par DB reset ho jata hai; permanent ke liye env me "
                  "SITE_COOKIE_1 = domain|cookie rakho.\n")
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
    _rt = _root_host(urlparse(url).netloc)
    for _d in (globals().get("_DIG_DEAD", {}), globals().get("_DIG_FAILS", {})):
        _d.pop(_rt, None)
        _d.pop("n:" + _rt, None)


def host_is_blocked(root: str) -> bool:
    """Is run me ek bhi fetch success nahi hua aur 12+ fail -> site IP block kar rahi hai."""
    st = _HOST_STATS.get(root)
    return bool(st and st["ok"] == 0 and st["fail"] >= 12)


def block_hint(url: str) -> str:
    st = _HOST_STATS.get(_root_host(urlparse(url).netloc))
    if st and st["ok"] == 0 and st["fail"] >= 3:
        return ("\n🚫 Site is server ki IP ko BLOCK kar rahi hai (Cloudflare / bot protection).\n"
                "Fix: 1) bot ko ghar ke PC/Indian IP par chalao (Render jaise datacenter IP aksar block hote hain), "
                "ya 2) PROXY_URL env me residential proxy do, 3) pip install curl_cffi.\n")
    return ""


async def guarded_extract(v: str, s: str) -> Optional[dict]:
    """extract_video_link + hard timeout + blocked-host par turant skip."""
    if host_is_blocked(_root_host(urlparse(v).netloc)):
        return None
    try:
        return await asyncio.wait_for(extract_video_link(v, source_page=s), timeout=100)
    except asyncio.TimeoutError:
        return None


def _cs_sess():
    s_ = getattr(_TL, "cs", None)
    if s_ is None:
        s_ = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
        _TL.cs = s_
    return s_


def _rq_sess():
    s_ = getattr(_TL, "rq", None)
    if s_ is None:
        s_ = requests.Session()
        ad = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16)
        s_.mount("http://", ad)
        s_.mount("https://", ad)
        _TL.rq = s_
    s_.cookies.clear()                       # sites ke beech cookie-pollution na ho
    return s_


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
    engines["cloudscraper"] = lambda: _cs_sess().get(url, headers=headers, timeout=tmo, proxies=proxies)
    engines["requests"] = lambda: _rq_sess().get(url, headers=headers, timeout=tmo, proxies=proxies)

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


_BF_STATS: Dict[str, list] = {}      # root -> [ok, fail, last_fail_ts]  (browser se page fetch)


def _browser_fetch_allowed(url: str) -> bool:
    if not async_playwright or os.getenv("BROWSER_FETCH", "1") == "0":
        return False
    if not re.search(r':(?:403|503|429|challenge)\b', LAST_DETAIL.get(url, "")):
        return False
    st = _BF_STATS.get(_root_host(urlparse(url).netloc))
    return not (st and st[0] == 0 and st[1] >= 3 and time.time() - st[2] < 600)


async def _browser_fetch(url: str) -> Optional[str]:
    root = _root_host(urlparse(url).netloc)
    st = _BF_STATS.setdefault(root, [0, 0, 0.0])
    ck: Dict[str, str] = {}
    try:
        html, _s = await asyncio.wait_for(pw_render(url, wait=3, cookies_out=ck), timeout=50)
    except Exception:
        html = None
    ok = bool(html) and len(html) > 500 and not re.search(
        r'<title>\s*(Just a moment|Attention Required|Access denied|Verifying|Are you a robot|DDoS)', html, re.I)
    if not ok:
        st[1] += 1
        st[2] = time.time()
        return None
    st[0] += 1
    if ck.get("cf_clearance"):                         # ab normal fast engines bhi isi cookie se chalenge
        _SCR_EXTRA_COOKIES[root] = "; ".join(f"{k}={v}" for k, v in ck.items())
    LAST_STATUS[url] = 200
    hs = _HOST_STATS.setdefault(root, {"ok": 0, "fail": 0})
    hs["ok"] += 1
    hs["fail"] = max(0, hs["fail"] - 1)
    return html


async def fetch(url: str, referer: Optional[str] = None, use_cookie: bool = True) -> Optional[str]:
    html = await asyncio.to_thread(fetch_sync, url, referer, use_cookie)
    if html is None and _browser_fetch_allowed(url):
        html = await _browser_fetch(url)
    return html


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


def _path_page_template(html: str, url: str) -> Optional[str]:
    """Page me '/same-path/2/', '/same-path/page/2/', '/same-path-2.html' jaisa link mile to {p} template banao."""
    pu = urlparse(url)
    base_path = pu.path.rstrip('/')
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        fp = urlparse(urljoin(url, m.group(1).replace('&amp;', '&')).split('#')[0])
        if fp.netloc.lower() != pu.netloc.lower() or not fp.path.startswith(base_path) or fp.query:
            continue
        rest = fp.path[len(base_path):]
        if re.fullmatch(r'[/_-]?(?:(?:page|p|pg)[/_-]?)?2(?:/|\.html?)?', rest, re.I):
            return urlunparse((pu.scheme, pu.netloc, base_path + rest.replace('2', '{p}', 1), '', '', ''))
    return None


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

    if html1:
        t3 = _path_page_template(html1, url)
        if t3 and t3 not in templates:
            templates.insert(0, t3)

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
    tok = sum(1 for r in results if "acctoken" in r["download_link"].lower())
    out = ""
    if ips:
        n = sum(1 for r in results if r.get("iplock"))
        out += (f"\n⚠️ {n} link IP-locked hain ({', '.join(ips)}): ye sirf usi IP/network se chalengi jahan bot chal raha hai.")
    if tok:
        out += f"\n⚠️ {tok} link me acctoken hai (bot ki IP/Referer se bandhi)."
    if ips or tok:
        out += ("\n✅ HTML player inhe bot-proxy se chalayega (PROXY_BASE set hai)." if PROXY_BASE else
                "\n🔧 Fix: env PROXY_BASE=https://<tumhari-app-url> set karo, phir player ye links bot ke through chalayega.")
    return out


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
_PROBE_OK: Dict[str, int] = {}
_DIG_FAILS: Dict[str, int] = {}
_DIG_DEAD: Dict[str, float] = {}
_DIG_SEM_: list = [None]


def _dig_sem():
    if _DIG_SEM_[0] is None:
        _DIG_SEM_[0] = asyncio.Semaphore(6)
    return _DIG_SEM_[0]


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


async def dig_stream(text: str, video_url: str) -> Optional[str]:
    """Static HTML me kuch nahi mila: iframe (lazy bhi) / API-JSON / browser network se dhundo (host-wise seekhta hai)."""
    root = _root_host(urlparse(video_url).netloc)
    if _DIG_DEAD.get("n:" + root, 0) > time.time():
        return None
    stats = _DIG_STATS.setdefault(root, {})
    deadline = time.time() + 40
    async with _dig_sem():
        for name, fn in _DIG_STAGES:
            if name in ("decode", "ytdlp"):             # ye pehle hi try ho chuke
                continue
            st = stats.setdefault("n:" + name, [0, 0])
            if st[0] == 0 and st[1] >= 4:
                continue
            left = deadline - time.time()
            if left < 6:
                break
            try:
                urls, _n = await asyncio.wait_for(fn(text, video_url), timeout=left)
            except Exception:
                urls = []
            urls = [u for u in dict.fromkeys(urls) if _valid_stream_url(u)]
            if urls:
                st[0] += 1
                _DIG_FAILS["n:" + root] = 0
                return max(urls, key=lambda u: stream_score(u, video_url))
            st[1] += 1
    _DIG_FAILS["n:" + root] = _DIG_FAILS.get("n:" + root, 0) + 1
    if _DIG_FAILS["n:" + root] >= 5:
        _DIG_DEAD["n:" + root] = time.time() + 600
    return None


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
        others = [u for u in pool if u != stream_link and not suspicious(u)]
        if not others or _PROBE_OK.get(root, 0) >= 4:
            return stream_link, "ok"                    # behtar vikalp nahi / is host par hamesha theek niklaa
        await probe([stream_link])
        if not small(stream_link):
            _PROBE_OK[root] = _PROBE_OK.get(root, 0) + 1
            return stream_link, "ok"                    # theek lag raha hai (ya size pata nahi)
    await probe(top_prog())
    real = pick_real()
    if real and real != stream_link:
        return real, "fixed"
    # ---- sirf preview/clip mili: page ke andar gehra dekho (stage by stage, host-wise seekhte hue) ----
    def _bad():
        return stream_link, ("preview_only" if (suspicious(stream_link) or small(stream_link)) else "ok")

    if _DIG_DEAD.get(root, 0) > time.time():           # is host par dig lagataar fail -> 10 min skip (tez)
        return _bad()
    async with _dig_sem():
        deadline = time.time() + 30
        stats = _DIG_STATS.setdefault(root, {})
        order = sorted(_DIG_STAGES, key=lambda st: -stats.get(st[0], [0, 0])[0])
        for name, fn in order:
            st = stats.setdefault(name, [0, 0])
            if st[0] == 0 and st[1] >= 4:              # is host par ye stage kabhi kaam nahi aaya -> skip
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
                _DIG_FAILS[root] = 0
                return real, "fixed"
            st[1] += 1
    _DIG_FAILS[root] = _DIG_FAILS.get(root, 0) + 1
    if _DIG_FAILS[root] >= 3:
        _DIG_DEAD[root] = time.time() + 600
    return _bad()


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

        if not stream_link:                      # kuch nahi mila -> iframe/API/browser se gehra dhundo
            stream_link = await dig_stream(text, video_url)

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


_PLAYER_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#0f0f0f">
<title>__TITLE__ | __OWNER__</title>
<meta name="author" content="__OWNER__">
<!-- Player by __OWNER__ -->
<style>
:root{--red:#f00;--bg:#0f0f0f;--bg2:#272727;--tx:#f1f1f1;--tx2:#aaa;--line:#303030;--chip:#272727;--chipA:#f1f1f1;--chipAt:#0f0f0f;--hh:56px}
[data-theme=light]{--bg:#fff;--bg2:#f2f2f2;--tx:#0f0f0f;--tx2:#606060;--line:#e5e5e5;--chip:#f2f2f2;--chipA:#0f0f0f;--chipAt:#fff}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--tx);font-family:Roboto,"Segoe UI",Arial,sans-serif}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;padding:0}
[hidden]{display:none!important}
a{color:inherit}
/* ---------- lock ---------- */
#lock{position:fixed;inset:0;z-index:9999;background:var(--bg);display:flex;align-items:center;justify-content:center}
.lbox{width:88%;max-width:320px;background:var(--bg2);border-radius:16px;padding:26px;text-align:center}
.lbox h3{margin:10px 0 16px}
.lbox input{width:100%;padding:12px 14px;border-radius:24px;border:1px solid var(--line);background:var(--bg);color:var(--tx);outline:0;margin-bottom:12px;font-size:15px}
.lbox button{width:100%;padding:12px;border-radius:24px;background:var(--red);color:#fff;font-weight:600}
#lerr{color:#ff5252;font-size:12px;min-height:16px;margin-top:8px}
.lg{display:inline-flex;width:32px;height:22px;border-radius:7px;background:var(--red);align-items:center;justify-content:center}
.lg svg{width:14px;height:14px}
/* ---------- header ---------- */
.top{position:sticky;top:0;z-index:60;height:var(--hh);display:flex;align-items:center;gap:12px;padding:0 14px;background:var(--bg);border-bottom:1px solid var(--line)}
.logo{display:flex;align-items:center;gap:8px;text-decoration:none;font-weight:700;font-size:17px;min-width:0}
.ownt{font-size:12px;color:var(--tx2);text-decoration:none;white-space:nowrap;margin-left:2px}
.ownt:hover{color:var(--tx)}
.wm{position:absolute;top:10px;right:14px;z-index:2;color:rgba(255,255,255,.5);font-weight:700;font-size:14px;text-shadow:0 1px 5px #000;pointer-events:none}
.own2{margin-top:14px;font-size:12px}.own2 a{color:var(--tx2);text-decoration:none}
.foot a{color:var(--tx2)}
.logo b{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:34vw}
.search{flex:1;max-width:640px;margin:0 auto;display:flex;position:relative}
.search input{width:100%;height:38px;border-radius:20px;border:1px solid var(--line);background:var(--bg);color:var(--tx);padding:0 38px 0 16px;outline:0;font-size:15px}
.search input:focus{border-color:#3ea6ff}
#qclr{position:absolute;right:10px;top:8px;color:var(--tx2)}
.tools{display:flex;gap:6px;align-items:center}
.tools button,.tools a{width:38px;height:38px;border-radius:50%;display:flex;align-items:center;justify-content:center;text-decoration:none;font-size:18px}
.tools button:hover,.tools a:hover{background:var(--bg2)}
/* ---------- chips ---------- */
.chips{position:sticky;top:var(--hh);z-index:50;background:var(--bg);display:flex;gap:10px;padding:10px 14px;overflow-x:auto;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;padding:7px 13px;border-radius:9px;background:var(--chip);font-size:14px;font-weight:500;white-space:nowrap}
.chip.on{background:var(--chipA);color:var(--chipAt)}
/* ---------- grid ---------- */
.bar{display:flex;justify-content:space-between;align-items:center;padding:4px 16px 8px;color:var(--tx2);font-size:13px}
.bar select{background:var(--chip);color:var(--tx);border:0;border-radius:8px;padding:6px 8px;font-size:13px}
.clr{background:var(--chip);color:var(--tx);border-radius:8px;padding:6px 10px;font-size:13px;margin-right:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:22px 16px;padding:8px 16px 40px}
.card{cursor:pointer;min-width:0;outline:0}
.thumb{position:relative;aspect-ratio:16/9;border-radius:12px;overflow:hidden;background:linear-gradient(135deg,var(--g1,#333),var(--g2,#111))}
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
@media(max-width:600px){.grid{grid-template-columns:1fr;gap:18px;padding:0 0 40px}.thumb{border-radius:0}.meta{padding:10px 12px 0}.bar{padding:4px 12px 8px}.logo b{display:none}}
/* ---------- watch ---------- */
.wl{display:grid;grid-template-columns:minmax(0,1fr) 400px;gap:24px;padding:20px 24px 40px;max-width:1800px;margin:0 auto}
.theater .wl{grid-template-columns:1fr}
.theater .player{max-height:80vh}.theater .pin{border-radius:0}
.theater .wmain{margin:0 -24px}
.theater .wmain>*:not(.player){margin-left:24px;margin-right:24px}
.wmain{position:relative;z-index:0}
.player{position:relative;aspect-ratio:16/9;width:100%;max-height:calc(100vh - 110px);user-select:none}
.pin{position:absolute;inset:0;overflow:hidden;background:#000;border-radius:12px}
.ambc{position:absolute;z-index:-1;pointer-events:none;transition:opacity .4s}
body.amb-on{overflow-x:hidden}
html:has(body.amb-on){scrollbar-width:none}
html:has(body.amb-on)::-webkit-scrollbar{display:none}
.amb-on .top{box-shadow:var(--hsh,none)}
.amb-on .desc,.amb-on .act{background:color-mix(in srgb,var(--bg2) 75%,transparent)}
.player video,.player #imgv{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000}
.player:fullscreen{max-height:none;background:#000}.player:fullscreen .pin{border-radius:0}.player:fullscreen .ambc{display:none}
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
.seek:before{content:"";position:absolute;left:0;right:0;height:4px;background:rgba(255,255,255,.3);border-radius:2px;transition:height .1s}
.seek:hover:before{height:6px}
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
#vol{width:70px;accent-color:#fff}
@media(max-width:600px){#vol{display:none}}
.menu{position:absolute;right:10px;bottom:62px;background:rgba(28,28,28,.96);border-radius:12px;padding:6px 0;min-width:130px;max-height:60%;overflow:auto}
.menu button{display:block;width:100%;text-align:left;padding:9px 18px;color:#fff;font-size:14px}
.menu button:hover{background:rgba(255,255,255,.12)}
.menu button.on{font-weight:700;color:#3ea6ff}
#wt{font-size:20px;line-height:1.35;margin:14px 0 8px;font-weight:700;word-break:break-word}
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
@media(max-width:1000px){.wl{grid-template-columns:1fr;padding:0 0 40px;gap:14px}.player{position:sticky;top:var(--hh);z-index:40;max-height:none}.pin{border-radius:0}.ambc{max-width:100vw}.wmain>*:not(.player){margin-left:14px;margin-right:14px}.wside{padding:0 14px}.theater .wmain{margin:0}.theater .wmain>*:not(.player){margin-left:14px;margin-right:14px}}
.dimov{position:absolute;inset:0;z-index:1;background:rgba(0,0,0,.55);display:flex;align-items:flex-start;padding:18px 70px 0 18px;opacity:0;transition:opacity .25s;pointer-events:none}
.dimov b{font-size:20px;line-height:1.3;color:#fff;text-shadow:0 1px 6px #000;max-width:80%}
.player.dim .dimov{opacity:1}
#vstats{position:absolute;left:10px;top:10px;z-index:9;background:rgba(0,0,0,.72);color:#0f0;font:11px/1.45 monospace;padding:8px 10px;border-radius:8px;max-width:min(340px,90%);pointer-events:none;white-space:pre;display:none}#vstats.on{display:block}.pbm{position:absolute;right:10px;bottom:64px;width:min(340px,92%);max-height:72%;overflow:auto;z-index:8;background:rgba(18,18,18,.97);color:#fff;border-radius:14px;padding:12px 14px;box-shadow:0 8px 30px rgba(0,0,0,.6);font-size:14px}
.pbm .hd{display:flex;align-items:center;gap:8px;font-weight:700;font-size:16px;margin-bottom:10px;cursor:pointer}
.pbm .chips2{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.pbm .chip2{flex:1 1 auto;padding:8px 10px;border-radius:20px;background:#2a2a2a;text-align:center;font-weight:600;min-width:52px}
.pbm .chip2.on{background:#fff;color:#000}
.pbm .row2{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:11px 2px;border-top:1px solid #2c2c2c;cursor:pointer;color:#fff;text-decoration:none}
.pbm .row2 small{color:#9a9a9a;display:block;font-size:12px;margin-top:2px}
.pbm .sw{flex:none;width:46px;height:26px;border-radius:13px;background:#3a3a3a;position:relative;transition:.2s}
.pbm .sw:after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:.2s}
.pbm .sw.on{background:#2f7fff}.pbm .sw.on:after{left:23px}
.pbm .sl{padding:10px 2px}
.pbm .sl .lb{display:flex;justify-content:space-between;margin-bottom:6px;font-weight:600}
.pbm .sl .lb span{color:#bbb}
.pbm .sl .lb button{color:#bbb;font-size:16px;margin-left:8px}
.pbm input[type=range]{width:100%;accent-color:#ff3d3d}
.pbm .arrow{color:#aaa}
.endsc{position:absolute;inset:0;z-index:6;background:rgba(0,0,0,.92);overflow:auto;padding:16px 18px;color:#fff;display:flex;flex-direction:column;gap:12px}
.endsc .eh{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.endsc .en{display:flex;gap:12px;align-items:center;background:#1c1c1c;border-radius:12px;padding:10px;flex:1 1 320px;max-width:520px;cursor:pointer}
.endsc .en .thumb{width:150px;flex:none;border-radius:8px}
.endsc .cd{width:54px;height:54px;border-radius:50%;flex:none;display:flex;align-items:center;justify-content:center;background:conic-gradient(#fff var(--p,0%),#444 0)}
.endsc .cd i{width:44px;height:44px;border-radius:50%;background:#1c1c1c;display:flex;align-items:center;justify-content:center;font-style:normal;font-size:20px;font-weight:700}
.endsc .eb2{display:flex;gap:8px;flex-wrap:wrap}
.endsc .eb2 button{background:#fff;color:#000;padding:9px 16px;border-radius:20px;font-weight:700}
.endsc .eb2 button.g{background:#333;color:#fff}
.endsc .eg{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.endsc .eg .c{cursor:pointer}
.endsc .eg .ttl{font-size:13px;margin-top:6px}
.ubox{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center}
.ucard{background:var(--bg2);color:var(--tx);width:min(520px,92%);max-height:92vh;overflow:auto;border-radius:16px;padding:18px;box-shadow:0 10px 40px rgba(0,0,0,.6)}
.ucard h3{margin:0 0 10px}
.ufl{display:block;font-size:13px;color:var(--tx2);margin-bottom:10px}
.ufl input,.ufl textarea,.ufl select,.ucard select{display:block;width:100%;margin-top:4px;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--tx);font:inherit;outline:0}
.unote{font-size:12px;color:var(--tx2);margin:6px 0 12px;line-height:1.5}
.ubtn{display:flex;gap:8px;flex-wrap:wrap}
.ubtn button{background:var(--chipA);color:var(--chipAt);padding:9px 16px;border-radius:20px;font-weight:700}
.ubtn button.g{background:var(--chip);color:var(--tx)}
.mbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:8px 16px}
.mbar input{flex:1 1 240px;min-width:0;height:40px;border-radius:20px;border:1px solid var(--line);background:var(--bg);color:var(--tx);padding:0 16px;outline:0;font-size:15px}
.mbar textarea{flex:1 1 240px;min-width:0;min-height:40px;max-height:120px;border-radius:20px;border:1px solid var(--line);background:var(--bg);color:var(--tx);padding:9px 16px;outline:0;font:inherit;resize:vertical}
.mbar select{height:40px;border-radius:20px;border:1px solid var(--line);background:var(--chip);color:var(--tx);padding:0 10px}
.mbar .act{border:0;cursor:pointer;color:var(--tx)}.mbar .act:disabled{opacity:.5}
.mpost{display:contents}.mmsg{flex-basis:100%;font-size:13px;color:var(--tx2);min-height:16px}
.adm{position:fixed;inset:0;z-index:600;background:var(--bg);color:var(--tx);overflow:auto;padding:12px 16px 40px}
.ahd{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}.atabs{display:flex;gap:8px;overflow-x:auto;margin-bottom:12px}
.arow{display:flex;align-items:center;gap:8px;padding:9px 0;border-bottom:1px solid var(--line);font-size:14px;flex-wrap:wrap}.arow .l{flex:1;min-width:0;word-break:break-all}
.arow button,.aform button{background:var(--chip);color:var(--tx);padding:6px 12px;border-radius:14px;font-size:13px}
.aform{display:flex;flex-direction:column;gap:8px;margin:12px 0}.aform input,.aform textarea,.arow input,.arow select{background:var(--bg2);color:var(--tx);border:1px solid var(--line);border-radius:10px;padding:9px 12px;font:inherit}
/* ---------- popup / toast ---------- */
.pop{position:fixed;z-index:200;background:var(--bg2);border-radius:12px;padding:6px 0;min-width:190px;box-shadow:0 6px 30px rgba(0,0,0,.5)}
.pop button,.pop a{display:block;width:100%;text-align:left;padding:10px 16px;font-size:14px;text-decoration:none}
.pop button:hover,.pop a:hover{background:rgba(128,128,128,.25)}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#323232;color:#fff;padding:10px 18px;border-radius:8px;font-size:14px;z-index:300;opacity:0;pointer-events:none;transition:.25s}
#toast.on{opacity:1}
</style>
</head>
<body>
<div id="lock" hidden><div class="lbox"><span class="lg"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="#fff"/></svg></span><h3>Protected</h3><input id="pw" type="password" placeholder="Password" autocomplete="off"><button id="pwb">Unlock</button><div id="lerr"></div><div class="own2" id="lockOwn"></div></div></div>
<div id="app" hidden>
<header class="top">
  <a class="logo" href="#/"><span class="lg"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="#fff"/></svg></span><b id="siteT"></b></a><a class="ownt" id="ownT" target="_blank" rel="noopener" hidden></a>
  <div class="search"><input id="q" type="search" placeholder="Search" autocomplete="off"><button id="qclr" hidden>&#10005;</button></div>
  <div class="tools"><button id="setB" title="Menu (clear history / export)">&#9881;</button><button id="themeB" title="Theme">&#127763;</button><a id="tgB" target="_blank" rel="noopener" title="Telegram">&#9992;</a></div>
</header>
<nav class="chips" id="chips"></nav>
<main id="home">
  <div class="mbar" id="mbar" hidden>
    <textarea id="murl" rows="1" placeholder="Listing / video URL paste karo (ek ya kai, har line me ek)"></textarea>
    <select id="mpages"><option value="1">1 page</option><option value="3">3 pages</option><option value="5" selected>5 pages</option><option value="10">10 pages</option></select>
    <button class="act" id="mgo">\u26A1 Scrape</button><button class="act" id="mstop" hidden>\u23F9 Stop</button><button class="act" id="mpl">\uD83D\uDCDA Playlists</button><button class="act" id="madmin" hidden>\uD83D\uDEE0 Admin</button>
    <span class="mpost" id="mpost" hidden><button class="act" id="msave">\uD83D\uDCBE Save playlist</button><button class="act" id="msend">\uD83D\uDCE4 Send M3U to chat</button><button class="act" id="mwatch">\uD83D\uDC41 Watch URL</button><button class="act" id="mshare">\uD83D\uDD17 Share</button><button class="act" id="mrf">\uD83D\uDD04 Refresh links</button></span>
    <div class="mmsg" id="mmsg"></div>
  </div>
  <div class="bar"><span id="count"></span><span class="rt"><button class="clr" id="clr" hidden></button><select id="sort"><option value="def">Default</option><option value="az">A &rarr; Z</option><option value="za">Z &rarr; A</option><option value="long">Longest</option><option value="short">Shortest</option></select></span></div>
  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" hidden></div>
  <div class="foot" id="foot"></div>
</main>
<section id="watch" hidden>
 <div class="wl">
  <div class="wmain">
   <div class="player paused" id="pl"><canvas id="amb" class="ambc"></canvas><div class="pin">
     <video id="v" playsinline preload="auto"></video>
     <img id="imgv" hidden alt="">
     <div class="aud" id="aud" hidden>&#9835;</div>
     <div class="dimov" id="dimov"><b id="dimT"></b></div>
     <div class="wm" id="wm"></div>
     <div class="ov" id="ov">
       <div class="spin" id="spin" hidden></div>
       <div class="rip l" id="ripL">&#9194; 10s</div><div class="rip r" id="ripR">10s &#9193;</div>
       <div class="big" id="big">&#9654;</div>
     </div>
     <div class="err" id="err" hidden></div>\n     <div id="vstats"></div>
     <div class="endsc" id="endsc" hidden></div>
     <div class="pbm" id="pbm" hidden></div>
     <div class="ctl" id="ctl">
       <div class="seek" id="seek"><div class="buf" id="buf"></div><div class="pro" id="pro"></div><div class="knob" id="knob"></div><div class="tip" id="tip">0:00</div></div>
       <div class="crow">
         <button id="bPlay" title="Play (k)">&#9654;</button><button id="bNext" title="Next (n)">&#9197;</button>
         <button id="bVol" title="Mute (m)">&#128266;</button><input id="vol" type="range" min="0" max="1" step="0.05" value="1">
         <span class="t" id="tm">0:00 / 0:00</span><span class="sp"></span>
         <button class="tx2" id="bSpd" title="Speed">1x</button><button class="tx2" id="bQ" title="Quality" hidden>Auto</button>
         <button id="bPip" title="Picture in picture">&#10064;</button><button id="bTh" title="Theater (t)">&#9645;</button><button id="bFs" title="Fullscreen (f)">&#9974;</button><button id="bMore" title="Settings (Playback, Zoom, Brightness...)">&#8942;</button>
       </div>
       <div class="menu" id="menu" hidden></div>
     </div>
   </div></div>
   <h1 id="wt"></h1>
   <div class="acts" id="acts"></div>
   <div class="desc" id="desc"></div>
  </div>
  <aside class="wside"><div class="ah"><b>Up next</b><label><input type="checkbox" id="auto" checked> Autoplay</label></div><div id="upn"></div></aside>
 </div>
</section>
</div>
<div id="toast"></div>
<script>
(function(){
"use strict";
var DATA=__DATA__, CFG=__CFG__;
var HLS_URL="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js",
    DASH_URL="https://cdn.jsdelivr.net/npm/dashjs@4/dist/dash.all.min.js",
    SHAKA_URL="https://cdn.jsdelivr.net/npm/shaka-player@4.7.11/dist/shaka-player.compiled.min.js",
    TS_URL="https://cdn.jsdelivr.net/npm/mpegts.js@1/dist/mpegts.js";
function $(s,r){return (r||document).querySelector(s)}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
var LS={get:function(k,d){try{var v=localStorage.getItem(k);return v===null?d:JSON.parse(v)}catch(e){return d}},
        set:function(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
var SS={get:function(k){try{return sessionStorage.getItem(k)}catch(e){return null}},set:function(k,v){try{sessionStorage.setItem(k,v)}catch(e){}}};

/* ---------- sha256 (password gate, secure-context nahi chahiye) ---------- */
function sha256(ascii){
  function rr(v,a){return (v>>>a)|(v<<(32-a))}
  var mp=Math.pow,mw=mp(2,32),i,j,result="",words=[],abl=ascii.length*8;
  var hash=sha256.h=sha256.h||[],k=sha256.k=sha256.k||[],pc=k.length,ic={};
  for(var c=2;pc<64;c++){if(!ic[c]){for(i=0;i<313;i+=c)ic[i]=c;hash[pc]=(mp(c,.5)*mw)|0;k[pc++]=(mp(c,1/3)*mw)|0}}
  ascii+="\x80";while(ascii.length%64-56)ascii+="\x00";
  for(i=0;i<ascii.length;i++){j=ascii.charCodeAt(i);if(j>>8)return"";words[i>>2]|=j<<((3-i)%4)*8}
  words[words.length]=((abl/mw)|0);words[words.length]=abl;
  for(j=0;j<words.length;){
    var w=words.slice(j,j+=16),old=hash;hash=hash.slice(0,8);
    for(i=0;i<64;i++){
      var w15=w[i-15],w2=w[i-2],a=hash[0],e=hash[4];
      var t1=hash[7]+(rr(e,6)^rr(e,11)^rr(e,25))+((e&hash[5])^((~e)&hash[6]))+k[i]+(w[i]=(i<16)?w[i]:(w[i-16]+(rr(w15,7)^rr(w15,18)^(w15>>>3))+w[i-7]+(rr(w2,17)^rr(w2,19)^(w2>>>10)))|0);
      var t2=(rr(a,2)^rr(a,13)^rr(a,22))+((a&hash[1])^(a&hash[2])^(hash[1]&hash[2]));
      hash=[(t1+t2)|0].concat(hash);hash[4]=(hash[4]+t1)|0;
    }
    for(i=0;i<8;i++)hash[i]=(hash[i]+old[i])|0;
  }
  for(i=0;i<8;i++)for(j=3;j+1;j--){var b=(hash[i]>>(j*8))&255;result+=((b<16)?0:"")+b.toString(16)}
  return result;
}
function hashPw(p){return sha256(unescape(encodeURIComponent(p)))}

/* ---------- helpers ---------- */
function fmtTime(s){s=Math.max(0,Math.floor(s||0));var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
  return (h?h+":"+(m<10?"0":""):"")+m+":"+(x<10?"0":"")+x}
function domainOf(u){try{return new URL(u).hostname.replace(/^www\./,"")}catch(e){return ""}}
function hnum(s){var h=0;for(var i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))|0;return Math.abs(h)}
function grad(t){var h=hnum(t||"x")%360;return ["hsl("+h+",55%,38%)","hsl("+((h+45)%360)+",60%,20%)"]}
function pathOf(u){return u.split("#")[0].split("?")[0].toLowerCase()}
function extOf(u){var m=pathOf(u).match(/\.([a-z0-9]{2,5})$/);return m?m[1]:""}
var AUDIO_EXT=["mp3","m4a","aac","wav","ogg","oga","opus","flac","wma"];
function fmtOf(it){
  var u=it.u.toLowerCase();
  if(it.eng==="hls")return "HLS";if(it.eng==="dash")return "DASH";
  if(u.indexOf(".m3u8")>-1)return "HLS";
  var e=extOf(u);
  if(e==="mpd")return "DASH";
  if(e==="m4v")return "MP4";
  if(e)return e.toUpperCase();
  return it.k||"VIDEO";
}
function isAudio(it){return it.k==="AUDIO"||AUDIO_EXT.indexOf(extOf(it.u))>-1}
function engineOf(it){
  var u=it.u.toLowerCase(),e=extOf(u);
  if(it.eng)return it.eng;
  if(u.indexOf(".m3u8")>-1)return "hls";
  if(e==="mpd")return "dash";
  if(e==="ts"||e==="flv"||e==="m2ts")return "mpegts";
  return "native";
}
var _sc={};
function loadScript(u){
  if(_sc[u])return _sc[u];
  _sc[u]=new Promise(function(ok,no){var s=document.createElement("script");s.src=u;s.async=true;s.onload=ok;s.onerror=function(){delete _sc[u];no(new Error("script load fail"))};document.head.appendChild(s)});
  return _sc[u];
}
var toastT;
function toast(m){var t=$("#toast");t.textContent=m;t.className="on";clearTimeout(toastT);toastT=setTimeout(function(){t.className=""},2200)}
function copy(t){
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(function(){toast("Link copied")},function(){fb()})}else fb();
  function fb(){var a=document.createElement("textarea");a.value=t;document.body.appendChild(a);a.select();try{document.execCommand("copy");toast("Link copied")}catch(e){toast("Copy fail")}a.remove()}
}
function extLinks(u){
  var enc=encodeURIComponent(u),ua=navigator.userAgent||"",ios=/iPhone|iPad|iPod/.test(ua),and=/Android/.test(ua),a=[];
  a.push({n:"VLC",h:ios?"vlc-x-callback://x-callback-url/stream?url="+enc:(and?"intent:"+u+"#Intent;package=org.videolan.vlc;type=video/*;end":"vlc://"+u)});
  if(and)a.push({n:"MX Player",h:"intent:"+u+"#Intent;package=com.mxtech.videoplayer.ad;type=video/*;end"});
  a.push({n:"Open link",h:u});
  return a;
}

/* ---------- state ---------- */
var FAV=LS.get("ytb_fav",{}),LATER=LS.get("ytb_later",{}),HIST=LS.get("ytb_hist",{});
var ALL=DATA.slice(),BYURL={},DUR={},THUMBS={};
function slim(it){return {t:it.t,u:it.u,k:it.k,p:it.p,th:it.th,d:it.d,v:it.v,x:it.x,xa:it.xa,rf:it.rf,drm:it.drm,eng:it.eng}}
ALL.forEach(function(it){BYURL[it.u]=it});
[FAV,LATER].forEach(function(m){Object.keys(m).forEach(function(u){if(!BYURL[u]&&m[u]&&m[u].u){var o=m[u];o._x=1;BYURL[u]=o;ALL.push(o)}})});
Object.keys(HIST).forEach(function(u){var h=HIST[u];if(!BYURL[u]&&h&&h.it&&h.it.u){h.it._x=1;BYURL[u]=h.it;ALL.push(h.it)}});
var VIEW={chip:"all",q:"",sort:"def"},LIST=[],CUR=null;
function saveLists(){LS.set("ytb_fav",FAV);LS.set("ytb_later",LATER);syncPush()}
function toggleFav(it){if(FAV[it.u]){delete FAV[it.u];toast("Removed from Favorites")}else{FAV[it.u]=slim(it);toast("Added to Favorites \u2665")}saveLists();updChips()}
function toggleLater(it){if(LATER[it.u]){delete LATER[it.u];toast("Removed from Watch later")}else{LATER[it.u]=slim(it);toast("Saved to Watch later")}saveLists();updChips()}
function saveHist(it,pos,dur){
  HIST[it.u]={ts:Date.now(),pos:pos,dur:dur,it:slim(it)};
  var ks=Object.keys(HIST);if(ks.length>300){ks.sort(function(a,b){return HIST[a].ts-HIST[b].ts});for(var i=0;i<ks.length-300;i++)delete HIST[ks[i]]}
  LS.set("ytb_hist",HIST);syncPush();
}

/* ---------- thumbnails ---------- */
var thumbQ=[],thumbRun=0,liveVid=0;
function queueThumb(fn){thumbQ.push(fn);pump()}
function pump(){while(thumbRun<3&&thumbQ.length){var f=thumbQ.shift();thumbRun++;f().then(function(){thumbRun--;pump()},function(){thumbRun--;pump()})}}
function setThumb(box,node){box.insertBefore(node,box.firstChild);box.classList.add("has")}
function setDur(it,sec,box){
  if(!isFinite(sec)||sec<=0)return;DUR[it.u]=sec;if(!it.d)it.d=sec;
  var d=box&&box.querySelector(".dur");if(d){d.textContent=fmtTime(sec);d.hidden=false}
}
function fillThumb(it,box){
  if(THUMBS[it.u]){var im=new Image();im.className="tImg";im.src=THUMBS[it.u];setThumb(box,im);return}
  if(it.k==="IMAGE"){var i2=new Image();i2.className="tImg";i2.referrerPolicy="no-referrer";i2.onload=function(){setThumb(box,i2)};i2.src=it.u;return}
  if(isAudio(it)||it.k==="PDF"){return}
  if(it.th){
    var img=new Image();img.className="tImg";img.referrerPolicy="no-referrer";img.decoding="async";
    img.onload=function(){setThumb(box,img)};img.onerror=function(){frameThumb(it,box)};img.src=it.th;
  }else frameThumb(it,box);
}
function frameThumb(it,box){
  var eng=engineOf(it);
  if(eng!=="native"&&eng!=="hls")return;
  queueThumb(function(){return new Promise(function(res){
    var v=document.createElement("video"),done=false,h=null,cors=true,timer,tries=0;
    v.muted=true;v.setAttribute("playsinline","");v.preload="metadata";v.className="tVid";
    function cleanup(){try{if(h){h.destroy();h=null}}catch(e){}try{v.removeAttribute("src");v.load()}catch(e){}}
    function finish(ok,keep){
      if(done)return;done=true;clearTimeout(timer);
      if(ok&&keep){setThumb(box,v);liveVid++}else cleanup();
      res();
    }
    function capture(){
      try{
        var w=v.videoWidth,hh=v.videoHeight;if(!w||!hh)return false;
        var c=document.createElement("canvas");c.width=320;c.height=Math.round(320*hh/w);
        c.getContext("2d").drawImage(v,0,0,c.width,c.height);
        var url=c.toDataURL("image/jpeg",.7);THUMBS[it.u]=url;
        var im=new Image();im.className="tImg";im.src=url;setThumb(box,im);return true;
      }catch(e){return false}
    }
    function ready(){
      if(done)return;
      if(capture()){finish(true,false);return}
      if(eng==="native"&&liveVid<40){finish(true,true)}else finish(false,false);
    }
    v.addEventListener("loadedmetadata",function(){
      if(isFinite(v.duration)&&v.duration>0){setDur(it,v.duration,box);
        try{v.currentTime=Math.min(Math.max(v.duration*.12,1),20)}catch(e){}}
      setTimeout(function(){if(!done&&v.readyState>=2)ready()},2500);
    });
    v.addEventListener("seeked",ready);
    v.addEventListener("loadeddata",function(){if(eng==="hls")setTimeout(ready,300)});
    v.addEventListener("error",function(){
      if(eng==="native"&&cors&&tries===0){tries++;cors=false;v.removeAttribute("crossorigin");v.src=it.u;return}
      finish(false,false);
    });
    timer=setTimeout(function(){finish(false,false)},15000);
    if(eng==="hls"){
      loadScript(HLS_URL).then(function(){
        if(!window.Hls||!Hls.isSupported()){finish(false,false);return}
        h=new Hls({maxBufferLength:2,maxMaxBufferLength:4,startPosition:3,enableWorker:false});
        h.on(Hls.Events.ERROR,function(e,d){if(d.fatal)finish(false,false)});
        h.loadSource(it.u);h.attachMedia(v);
      },function(){finish(false,false)});
    }else{v.crossOrigin="anonymous";v.src=it.u}
  })});
}
var obs=null;
if(window.IntersectionObserver){obs=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting){obs.unobserve(en.target);fillThumb(en.target._it,en.target)}})},{rootMargin:"400px"})}
function mkThumb(it){
  var th=el("div","thumb"),g=grad(it.t);th.style.setProperty("--g1",g[0]);th.style.setProperty("--g2",g[1]);
  th.appendChild(el("span","ph",(it.t||"?").trim().charAt(0).toUpperCase()||"?"));
  th.appendChild(el("span","fmt",fmtOf(it)));
  var d=el("span","dur",(it.d||DUR[it.u])?fmtTime(it.d||DUR[it.u]):"");if(!d.textContent)d.hidden=true;th.appendChild(d);
  th._it=it;if(obs)obs.observe(th);else fillThumb(it,th);
  return th;
}

/* ---------- home (grid) ---------- */
var grid=$("#grid"),chipsEl=$("#chips");
function curDur(it){return it.d||DUR[it.u]||0}
function computeList(){
  var base,c=VIEW.chip;
  if(c==="fav")base=Object.keys(FAV).map(function(u){return BYURL[u]}).filter(Boolean);
  else if(c==="later")base=Object.keys(LATER).map(function(u){return BYURL[u]}).filter(Boolean);
  else if(c==="hist")base=Object.keys(HIST).sort(function(a,b){return HIST[b].ts-HIST[a].ts}).map(function(u){return BYURL[u]}).filter(Boolean);
  else{base=DATA.slice();if(c.indexOf("f:")===0)base=base.filter(function(it){return fmtOf(it)===c.slice(2)})}
  var q=VIEW.q.trim().toLowerCase();
  if(q)base=base.filter(function(it){return (it.t||"").toLowerCase().indexOf(q)>-1||domainOf(it.u).indexOf(q)>-1});
  if(c!=="hist"){
    if(VIEW.sort==="az")base.sort(function(a,b){return (a.t||"").localeCompare(b.t||"")});
    else if(VIEW.sort==="za")base.sort(function(a,b){return (b.t||"").localeCompare(a.t||"")});
    else if(VIEW.sort==="long")base.sort(function(a,b){return curDur(b)-curDur(a)});
    else if(VIEW.sort==="short")base.sort(function(a,b){return (curDur(a)||1e9)-(curDur(b)||1e9)});
  }
  return base;
}
function updChips(){
  chipsEl.textContent="";
  var counts={};DATA.forEach(function(it){var f=fmtOf(it);counts[f]=(counts[f]||0)+1});
  var defs=[["all","All"],["fav","\u2665 Favorites ("+Object.keys(FAV).length+")"],["later","\u23F1 Watch later ("+Object.keys(LATER).length+")"],["hist","\u21BB History ("+Object.keys(HIST).length+")"]];
  Object.keys(counts).sort(function(a,b){return counts[b]-counts[a]}).forEach(function(f){defs.push(["f:"+f,f+" ("+counts[f]+")"])});
  defs.forEach(function(d){
    var b=el("button","chip"+(VIEW.chip===d[0]?" on":""),d[1]);
    b.onclick=function(){VIEW.chip=d[0];updChips();renderGrid()};chipsEl.appendChild(b);
  });
}
function showPop(anchor,entries){
  closePop();
  var p=el("div","pop");p.id="pop";
  entries.forEach(function(en){
    if(en.href){var a=el("a","",en.t);a.href=en.href;a.target="_blank";a.rel="noopener noreferrer";p.appendChild(a)}
    else{var b=el("button","",en.t);b.onclick=function(e){e.stopPropagation();closePop();en.f()};p.appendChild(b)}
  });
  document.body.appendChild(p);
  var r=anchor.getBoundingClientRect(),w=p.offsetWidth||220,hh=p.offsetHeight||240;
  p.style.left=Math.max(8,Math.min(window.innerWidth-w-8,r.right-w))+"px";
  p.style.top=Math.max(8,Math.min(window.innerHeight-hh-8,r.bottom+4))+"px";
}
function popMenu(anchor,it){
  var e=[
    {t:"\u25B6  Play",f:function(){go(it)}},
    {t:(FAV[it.u]?"\u2665  Remove favorite":"\u2661  Add to favorites"),f:function(){toggleFav(it);renderGrid()}},
    {t:(LATER[it.u]?"\u23F1  Remove from Watch later":"\u23F1  Watch later"),f:function(){toggleLater(it);if(VIEW.chip==="later")renderGrid()}},
    {t:"\uD83D\uDCCB  Copy link",f:function(){copy(it.u)}}
  ];
  if(HIST[it.u])e.push({t:"\uD83D\uDDD1  Remove from history",f:function(){delete HIST[it.u];LS.set("ytb_hist",HIST);toast("Removed from history");updChips();renderGrid()}});
  extLinks(it.u).forEach(function(x){e.push({t:"\uD83D\uDCFA  "+(x.n==="Open link"?"Open / Download":"Open in "+x.n),href:x.h})});
  showPop(anchor,e);
}
function clearList(kind){
  var nm={hist:"history",fav:"favorites",later:"watch later"};
  if(kind==="all"){
    if(!confirm("Saara saved data (favorites, watch later, history) delete karna hai?"))return;
    FAV={};LATER={};HIST={};LS.set("ytb_fav",FAV);LS.set("ytb_later",LATER);LS.set("ytb_hist",HIST);toast("Sab saaf ho gaya");
  }else{
    if(!confirm("Poori "+nm[kind]+" delete karni hai?"))return;
    if(kind==="hist"){HIST={};LS.set("ytb_hist",HIST)}else if(kind==="fav"){FAV={};LS.set("ytb_fav",FAV)}else{LATER={};LS.set("ytb_later",LATER)}
    toast(nm[kind]+" cleared");
  }
  updChips();renderGrid();syncPush();
}
function exportFav(){
  var l=Object.keys(FAV).map(function(u){return FAV[u]});
  if(!l.length){toast("Favorites khali hain");return}
  var txt="#EXTM3U\n"+l.map(function(i){return "#EXTINF:-1,"+(i.t||"Video").replace(/[\r\n]+/g," ")+"\n"+i.u}).join("\n")+"\n";
  try{var b=new Blob([txt],{type:"audio/x-mpegurl"}),a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="favorites.m3u";document.body.appendChild(a);a.click();a.remove();toast("favorites.m3u download ho gayi")}catch(e){copy(txt)}
}
function gearMenu(anchor){
  showPop(anchor,[
    {t:"\uD83D\uDD17  Play URL / MPD...",f:openUrlBox},
    {t:"\uD83D\uDDD1  Clear history ("+Object.keys(HIST).length+")",f:function(){clearList("hist")}},
    {t:"\uD83D\uDDD1  Clear favorites ("+Object.keys(FAV).length+")",f:function(){clearList("fav")}},
    {t:"\uD83D\uDDD1  Clear watch later ("+Object.keys(LATER).length+")",f:function(){clearList("later")}},
    {t:"\u26A0  Reset all saved data",f:function(){clearList("all")}},
    {t:"\u2B07  Export favorites (M3U)",f:exportFav}
  ].concat(CFG.owner&&CFG.tg?[{t:"\u2708  "+CFG.owner,href:CFG.tg}]:[]));
}
function closePop(){var p=$("#pop");if(p)p.remove()}
document.addEventListener("click",closePop);
function mkCard(it){
  var c=el("article","card");c.tabIndex=0;
  var th=mkThumb(it);
  var fv=el("button","fv"+(FAV[it.u]?" on":""),FAV[it.u]?"\u2665":"\u2661");fv.title="Favorite";
  fv.onclick=function(e){e.stopPropagation();toggleFav(it);fv.className="fv"+(FAV[it.u]?" on":"");fv.textContent=FAV[it.u]?"\u2665":"\u2661";if(VIEW.chip==="fav")renderGrid()};
  th.appendChild(fv);
  var h=HIST[it.u];if(h&&h.dur>0){var pr=el("div","prog"),i=el("i");i.style.width=Math.min(100,h.pos/h.dur*100)+"%";pr.appendChild(i);th.appendChild(pr)}
  c.appendChild(th);
  var m=el("div","meta"),av=el("div","av",(domainOf(it.u)||"?").charAt(0).toUpperCase()),g=grad(domainOf(it.u));av.style.background=g[0];
  var tx=el("div","tx"),t=el("h3","ttl",it.t||"Video");t.title=it.t||"";
  tx.appendChild(t);tx.appendChild(el("div","sub",(domainOf(it.u)||"stream")+" \u2022 "+fmtOf(it)));
  var mb=el("button","more","\u22EE");mb.onclick=function(e){e.stopPropagation();popMenu(mb,it)};
  m.appendChild(av);m.appendChild(tx);m.appendChild(mb);c.appendChild(m);
  c.onclick=function(){go(it)};
  c.onkeydown=function(e){if(e.key==="Enter")go(it)};
  return c;
}
function renderGrid(){
  LIST=computeList();grid.textContent="";
  var frag=document.createDocumentFragment();LIST.forEach(function(it){frag.appendChild(mkCard(it))});grid.appendChild(frag);
  $("#count").textContent=LIST.length+" item"+(LIST.length===1?"":"s");
  var clr=$("#clr"),cc=VIEW.chip;
  if((cc==="hist"||cc==="fav"||cc==="later")&&LIST.length){clr.hidden=false;clr.textContent="\uD83D\uDDD1 Clear "+(cc==="hist"?"history":cc==="fav"?"favorites":"watch later");clr.onclick=function(){clearList(cc)}}
  else clr.hidden=true;
  var em=$("#empty");em.hidden=LIST.length>0;
  if(!LIST.length)em.textContent=VIEW.chip==="fav"?"Koi favorite nahi. Kisi video par \u2661 dabao.":VIEW.chip==="later"?"Watch later khali hai.":VIEW.chip==="hist"?"Abhi koi video nahi dekha.":"Kuch nahi mila.";
}
function go(it){location.hash="#/w/"+ALL.indexOf(it)}

/* ---------- player ---------- */
var V=$("#v"),PL=$("#pl"),hls=null,mp=null,dash=null,retries=0,hideT,lastSave=0,dragging=false,IMGV=$("#imgv");
var CURQ="auto",SEEK_AT=0,CURURL="",CURENG="native",PXS={};
var shakaPlayer=null,HOLD2X=false,HOLD_SPD=1,PRELOAD_V=null,PRELOAD_U="",BUF_TOAST_T=0;
function PXON(it){if(PXS[it.u]!==undefined)return PXS[it.u];return !!(it.x&&it.xa)&&LS.get("ytb_px",true)}
function reloadCur(at,cors){var o={keepQ:true,at:at};if(cors)o.cors=true;if(CUR&&CURURL!==CUR.x)o.url=CURURL;loadItem(CUR,o)}
function b64u(x){x=(x||"").trim();if(/^[0-9a-fA-F]+$/.test(x)&&x.length%2===0){var s="";for(var i=0;i<x.length;i+=2)s+=String.fromCharCode(parseInt(x.substr(i,2),16));return btoa(s).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"")}return x}
/* ---------- playback settings (3-dot menu) ---------- */
var PBDEF={zoom:false,bright:100,boost:100,night:false,dim:true,intro:0,outro:0,contrast:100,sat:100,warm:0,loop:false};
var PB=LS.get("ytb_pb",{}),PBLEVEL="main";
function pbv(k){return PB[k]!==undefined?PB[k]:PBDEF[k]}
function pbset(k,v){PB[k]=v;LS.set("ytb_pb",PB)}
var AC=null,SRCN=null,GAINN=null,COMPN=null,GRAPH=false,PENDING_FX=false,FX_AT=0,CORS_BLOCK={},CORS_LOAD=false,OUTRO_DONE=false,ENDSHOWN=false,endT=null;
function applyVideoFx(){
  var f=[],b=pbv("bright"),c=pbv("contrast"),s=pbv("sat"),w=pbv("warm");
  if(b!==100)f.push("brightness("+b+"%)");
  if(c!==100)f.push("contrast("+c+"%)");
  if(s!==100)f.push("saturate("+s+"%)");
  if(w>0)f.push("sepia("+w+"%)");
  V.style.filter=f.join(" ");
  V.style.objectFit=pbv("zoom")?"cover":"";
  V.loop=!!pbv("loop");
}
function needFx(){return pbv("boost")>100||pbv("night")}
function setFx(){
  if(!GRAPH)return;
  var n=pbv("night");
  try{
    COMPN.threshold.value=n?-38:0;COMPN.ratio.value=n?8:1;COMPN.knee.value=n?24:0;COMPN.attack.value=0.003;COMPN.release.value=0.25;
    GAINN.gain.value=(pbv("boost")/100)*(n?1.5:1);
  }catch(e){}
}
function ensureGraph(){
  if(GRAPH)return true;
  try{
    var A=window.AudioContext||window.webkitAudioContext;if(!A)return false;
    AC=AC||new A();
    SRCN=AC.createMediaElementSource(V);COMPN=AC.createDynamicsCompressor();GAINN=AC.createGain();
    SRCN.connect(COMPN);COMPN.connect(GAINN);GAINN.connect(AC.destination);
    GRAPH=true;if(AC.resume)AC.resume();return true;
  }catch(e){return false}
}
function resetFx(msg){pbset("boost",100);pbset("night",false);if(msg)toast(msg);setFx();if(!$("#pbm").hidden)renderPb(PBLEVEL)}
function applyAudioFx(){
  if(!needFx()){setFx();return}
  var eng=CURENG;
  if(eng==="native"&&!GRAPH&&V.crossOrigin!=="anonymous"){
    if(CORS_BLOCK[CURURL]){resetFx("Is stream par Boost/Night mode nahi chalega (server CORS allow nahi karta)");return}
    PENDING_FX=true;FX_AT=V.currentTime||0;                 /* CORS ke saath dobara load, phir graph banega */
    reloadCur(FX_AT,true);return;
  }
  if(!ensureGraph()){resetFx("Audio boost is browser me nahi chal raha");return}
  setFx();
}
function replaceVideo(){
  var old=V,nv=document.createElement("video");
  nv.id="v";nv.setAttribute("playsinline","");nv.preload="auto";nv.volume=old.volume;nv.muted=old.muted;
  try{old.pause();old.removeAttribute("src");old.load()}catch(e){}
  old.parentNode.replaceChild(nv,old);V=nv;GRAPH=false;SRCN=GAINN=COMPN=null;bindVideo();applyVideoFx();
}
var isTouch=false;try{isTouch=window.matchMedia&&matchMedia("(pointer:coarse)").matches}catch(e){}
function destroyEngines(){
  try{if(hls){hls.destroy()}}catch(e){}hls=null;
  try{if(mp){mp.destroy()}}catch(e){}mp=null;
  try{if(dash){dash.reset()}}catch(e){}dash=null;
  try{if(shakaPlayer){shakaPlayer.destroy();shakaPlayer=null}}catch(e){shakaPlayer=null}
  try{V.pause();V.removeAttribute("src");V.load()}catch(e){}
  try{if(STATS_ON){var box=$("#vstats");if(box){box.className="";box.textContent=""}}}catch(e){}
  try{if(PRELOAD_V){PRELOAD_V.removeAttribute("src");PRELOAD_V.load()}}catch(e){}
}

/* ---- advanced: live stats overlay + DASH/HLS audio tracks ---- */
var STATS_ON=false,STATS_T=null;
function toggleStats(){
  STATS_ON=!STATS_ON;
  var box=$("#vstats");
  if(!STATS_ON){box.className="";box.textContent="";clearInterval(STATS_T);STATS_T=null;toast("Stats off");return}
  box.className="on";toast("Stats on (i key)");
  function tick(){
    if(!STATS_ON)return;
    var lines=[],res=(V.videoWidth||0)+"x"+(V.videoHeight||0);
    lines.push("Engine: "+(CURENG||"?")+" | Res: "+res);
    lines.push("Time: "+fmtTime(V.currentTime||0)+" / "+(isFinite(V.duration)?fmtTime(V.duration):"LIVE"));
    try{
      var b=V.buffered,buf=0;
      if(b&&b.length){for(var i=0;i<b.length;i++)if(b.start(i)<=(V.currentTime||0)&&b.end(i)>=(V.currentTime||0))buf=b.end(i)-(V.currentTime||0)}
      lines.push("Buffer: "+buf.toFixed(1)+"s | Rate: "+(V.playbackRate||1)+"x");
    }catch(e){}
    if(hls&&hls.levels&&hls.levels.length){
      var lv=hls.levels[hls.currentLevel]||{};
      lines.push("HLS lvl: "+(hls.currentLevel)+"/"+(hls.levels.length-1)+" "+(lv.height?lv.height+"p":"")+" "+(lv.bitrate?Math.round(lv.bitrate/1000)+"k":""));
    }
    if(dash){
      try{
        var db=dashBitrates(),qi=dash.getQualityFor?dash.getQualityFor("video"):-1;
        var bi=db[qi]||{};
        lines.push("DASH Q: "+(qi)+"/"+(db.length?db.length-1:0)+" "+(bi.height?bi.height+"p":"")+" "+(bi.bitrate?Math.round(bi.bitrate/1000)+"k":""));
        if(dash.getAverageThroughput){var th=dash.getAverageThroughput("video");if(th)lines.push("Throughput: "+Math.round(th)+" kbps")}
      }catch(e){}
    }
    if(CUR&&CUR.u)lines.push((CUR.u||"").slice(0,90));
    box.textContent=lines.join("\\n");
  }
  tick();STATS_T=setInterval(tick,800);
}
function listAudioTracks(){
  var out=[];
  try{
    if(dash&&dash.getTracksFor){
      var tr=dash.getTracksFor("audio")||[];
      tr.forEach(function(t,i){
        var lab=(t.lang||t.language||"")+" "+(t.roles?t.roles.join(","):"")+" "+(t.labels&&t.labels[0]?t.labels[0]:"");
        out.push({v:i,l:(lab.trim()||("Audio "+(i+1))),track:t});
      });
    }
  }catch(e){}
  try{
    if(hls&&hls.audioTracks&&hls.audioTracks.length){
      hls.audioTracks.forEach(function(t,i){out.push({v:i,l:(t.name||t.lang||("Audio "+(i+1))),hls:true})});
    }
  }catch(e){}
  return out;
}
function setAudioTrack(i){
  try{
    if(dash&&dash.getTracksFor){
      var tr=dash.getTracksFor("audio")||[];
      if(tr[i])dash.setCurrentTrack(tr[i]);
    }
    if(hls&&hls.audioTracks&&hls.audioTracks[i]!=null)hls.audioTrack=i;
  }catch(e){}
  toast("Audio track set");
}

function setLoading(b){$("#spin").hidden=!b}
function showErr(msg,it){
  var e=$("#err");
  if(!msg){e.hidden=true;e.textContent="";return}
  e.textContent="";e.hidden=false;setLoading(false);
  e.appendChild(el("div","",msg));
  var box=el("div","eb");
  var rt=el("button","","Retry");rt.onclick=function(){if(CUR)reloadCur(V.currentTime)};box.appendChild(rt);
  var lv=lowerVariant();
  if(lv){var lb=el("button","","Try "+lv[0]+"p");lb.onclick=function(){CURQ=lv[0];loadItem(CUR,{url:lv[1],keepQ:true,at:V.currentTime})};box.appendChild(lb)}
  if(it){
    extLinks(it.u).forEach(function(x){var a=el("a","",x.n==="Open link"?"Open / Download":x.n);a.href=x.h;a.target="_blank";a.rel="noopener noreferrer";box.appendChild(a)});
    if(it.rf){var rb=el("button","","\uD83D\uDD04 Refresh link");rb.onclick=function(){setLoading(true);refreshItem(it,function(ok){if(ok){toast("Naya link mil gaya");loadItem(it,{keepQ:true})}else{setLoading(false);toast("Naya link nahi mila")}})};box.appendChild(rb)}
    if(it.x){var usingPx=(CURURL===it.x),pb=el("button","",usingPx?"Try direct (no proxy)":"Try via bot proxy");pb.onclick=function(){PXS[it.u]=!usingPx;loadItem(CUR,{keepQ:true,at:V.currentTime})};box.appendChild(pb)}
    var cp=el("button","","Copy link");cp.onclick=function(){copy(it.u)};box.appendChild(cp);
  }
  e.appendChild(box);
}
function fail(it,why){
  var key=it.p||it.u,now=Date.now();
  if(it.rf&&!(RFTRIED[key]&&now-RFTRIED[key]<600000)){          /* link expire ho gayi ho sakti hai -> khud refresh */
    RFTRIED[key]=now;toast("Link refresh ho rahi...");setLoading(true);
    refreshItem(it,function(ok){if(ok&&CUR===it){toast("Naya link mil gaya");loadItem(it,{keepQ:true})}else showErr((why||"Ye stream browser me play nahi ho raha")+" ("+fmtOf(it)+"). Refresh se bhi naya link nahi mila:",it)});
    return;
  }
  showErr((why||"Ye stream browser me play nahi ho raha")+" ("+fmtOf(it)+"). Kisi external player me kholo:",it)
}
function hasVariants(it){return !!(it&&it.v&&it.v.length>1)}
function qLabel(q){return q+"p"+(q>=720?" HD":"")}
function curQ(){
  if(CURQ!=="auto")return CURQ;
  var v=CUR&&CUR.v;if(v)for(var i=0;i<v.length;i++)if(v[i][1]===CUR.u)return v[i][0];
  return null;
}
function lowerVariant(){
  if(!hasVariants(CUR))return null;
  var cq=curQ(),v=CUR.v;
  for(var i=0;i<v.length;i++){if(cq==null||v[i][0]<cq)return v[i]}
  return null;
}
function dashBitrates(){
  try{
    if(!dash||!dash.getBitrateInfoListFor)return [];
    return dash.getBitrateInfoListFor("video")||[];
  }catch(e){return []}
}
function updQBtn(){
  var b=$("#bQ"),hl=hls&&hls.levels&&hls.levels.length>1,db=dashBitrates();
  if(!hl&&!db.length&&!hasVariants(CUR)){b.hidden=true;return}
  b.hidden=false;
  if(hl){var lv=hls.levels[hls.currentLevel];b.textContent=(hls.currentLevel>=0&&lv&&lv.height)?lv.height+"p":"Auto"}
  else if(db.length){
    try{
      var qi=dash.getQualityFor?dash.getQualityFor("video"):-1;
      var auto=dash.getAutoSwitchQualityFor?dash.getAutoSwitchQualityFor("video"):true;
      if(auto||qi<0){b.textContent="Auto"}
      else{var bi=db[qi];b.textContent=bi&&bi.height?(bi.height+"p"):(bi?(Math.round(bi.bitrate/1000)+"k"):"Auto")}
    }catch(e){b.textContent="Auto"}
  }
  else{var q=curQ();b.textContent=q?q+"p":"Auto"}
}
function buildQuality(){updQBtn()}
function setQuality(v){
  var u=CUR.u;
  if(v!=="auto"){for(var i=0;i<CUR.v.length;i++)if(CUR.v[i][0]===v)u=CUR.v[i][1]}
  CURQ=v;loadItem(CUR,{url:u,at:V.currentTime,keepQ:true});
}

/* ---- power extras: Shaka fallback, hold-to-2x, preload next, share timestamp, buffer toast ---- */
function tryShaka(it,u,opt){
  loadScript(SHAKA_URL).then(function(){
    if(!window.shaka){fail(it,"DASH engines fail (dash.js + shaka)");return}
    try{shaka.polyfill.installAll()}catch(e){}
    if(!shaka.Player.isBrowserSupported()){fail(it,"Browser DASH support nahi");return}
    shakaPlayer=new shaka.Player(V);
    shakaPlayer.addEventListener("error",function(ev){
      var code=(ev&&ev.detail&&ev.detail.code)||"?";
      if(retries++<2){try{shakaPlayer.retry()}catch(e){fail(it,"Shaka error "+code)}}
      else fail(it,"Shaka/DASH fail ("+code+")");
    });
    shakaPlayer.configure({abr:{enabled:true},streaming:{bufferingGoal:20,rebufferingGoal:4}});
    shakaPlayer.load(u).then(function(){
      setLoading(false);buildQuality();
      var p=V.play();if(p&&p.catch)p.catch(function(){});
      if(SEEK_AT>1){try{V.currentTime=SEEK_AT}catch(e){}SEEK_AT=0}
      toast("Shaka engine");
    },function(e){fail(it,"Shaka load fail")});
  },function(){fail(it,"Shaka load nahi hui")});
}
function shareTimestamp(){
  if(!CUR)return;
  var t=Math.floor(V.currentTime||0),base=CUR.p||CUR.u||"";
  var link=base+(base.indexOf("?")>=0?"&":"?")+"t="+t+"s";
  var msg=(CUR.t||"Video")+" @ "+fmtTime(t)+"\\n"+link;
  copy(msg);
  toast("Timestamp link copied");
}
function preloadNext(){
  try{
    var n=nextItem(1);if(!n||!n.u)return;
    if(PRELOAD_U===n.u)return;
    PRELOAD_U=n.u;
    if(!PRELOAD_V){PRELOAD_V=document.createElement("video");PRELOAD_V.muted=true;PRELOAD_V.preload="auto";PRELOAD_V.style.display="none";document.body.appendChild(PRELOAD_V)}
    var eng=engineOf(n);
    if(eng==="native"||eng==="hls"){PRELOAD_V.src=n.x||n.u}
  }catch(e){}
}
function onBufferToast(){
  var now=Date.now();
  if(now-BUF_TOAST_T<8000)return;
  BUF_TOAST_T=now;
  if(hls&&hls.levels&&hls.levels.length>1&&hls.currentLevel>0){
    try{hls.nextLevel=Math.max(0,hls.currentLevel-1);toast("Slow connection – quality kam")}catch(e){}
  }else if(dash&&dashBitrates().length>1){
    try{
      var qi=dash.getQualityFor("video");
      if(qi>0){dash.setAutoSwitchQualityFor("video",false);dash.setQualityFor("video",qi-1);toast("Slow connection – quality kam")}
    }catch(e){}
  }else toast("Buffering…");
}
function loadItem(it,opt){
  opt=opt||{};
  destroyEngines();showErr("");hideEnd();OUTRO_DONE=false;CORS_LOAD=false;amBars={t:0,b:0,on:true};amN=0;AB.a=AB.b=null;var tk0=V.querySelector("track");if(tk0)tk0.remove();retries=0;V.hidden=true;IMGV.hidden=true;$("#aud").hidden=true;$("#menu").hidden=true;
  applyVideoFx();
  if(!opt.keepQ)CURQ="auto";
  var u0=opt.url||it.u,eng0=engineOf({u:u0,k:it.k,eng:it.eng}),u=(!opt.url&&it.x&&eng0!=="dash"&&PXON(it))?it.x:u0;CURURL=u;CURENG=eng0;SEEK_AT=opt.at||0;
  if(it.k==="IMAGE"){IMGV.hidden=false;IMGV.referrerPolicy="no-referrer";IMGV.src=it.u;setLoading(false);updQBtn();return}
  if(it.k==="PDF"){showErr("PDF file hai.",it);return}
  V.hidden=false;$("#aud").hidden=!(isAudio(it)||AUDONLY);V.style.visibility=AUDONLY?"hidden":"";setLoading(true);updQBtn();
  var eng=eng0;
  var go2=function(){var p=V.play();if(p&&p.catch)p.catch(function(){})};
  if(eng==="hls"){
    loadScript(HLS_URL).then(function(){
      if(window.Hls&&Hls.isSupported()){
        hls=new Hls({enableWorker:true,maxBufferLength:40});
        hls.on(Hls.Events.MANIFEST_PARSED,function(){buildQuality();go2()});
        hls.on(Hls.Events.LEVEL_SWITCHED,function(){updQBtn()});
        hls.on(Hls.Events.ERROR,function(e,d){
          if(!d.fatal)return;
          if(d.type===Hls.ErrorTypes.NETWORK_ERROR&&retries++<2){hls.startLoad()}
          else if(d.type===Hls.ErrorTypes.MEDIA_ERROR&&retries++<3){hls.recoverMediaError()}
          else fail(it,"HLS stream load nahi hui (CORS/expired link/block ho sakta hai)");
        });
        hls.loadSource(u);hls.attachMedia(V);
      }else if(V.canPlayType("application/vnd.apple.mpegurl")){V.src=u;go2()}
      else fail(it,"HLS support nahi");
    },function(){if(V.canPlayType("application/vnd.apple.mpegurl")){V.src=u;go2()}else fail(it,"hls.js load nahi hui (internet?)")});
  }else if(eng==="dash"){
    loadScript(DASH_URL).then(function(){
      if(!window.dashjs){fail(it,"dash.js load nahi hui");return}
      dash=dashjs.MediaPlayer().create();
      try{
        dash.updateSettings({
          streaming:{
            abr:{autoSwitchBitrate:{video:true,audio:true}},
            buffer:{stableBufferTime:12,bufferTimeAtTopQuality:30,bufferTimeAtTopQualityLongForm:60},
            retryAttempts:{MPD:3,MediaSegment:3,InitializationSegment:3},
            retryIntervals:{MPD:500,MediaSegment:500,InitializationSegment:500}
          },
          debug:{logLevel:dashjs.Debug?dashjs.Debug.LOG_LEVEL_WARNING:0}
        });
      }catch(e){}
      if(it.drm&&it.drm.length){
        var ckk={};it.drm.forEach(function(p){ckk[b64u(p[0])]=b64u(p[1])});
        try{dash.setProtectionData({"org.w3.clearkey":{"clearkeys":ckk}})}catch(e){}
      }
      dash.on(dashjs.MediaPlayer.events.STREAM_INITIALIZED,function(){
        setLoading(false);buildQuality();go2();
        if(SEEK_AT>1){try{V.currentTime=SEEK_AT}catch(e){}SEEK_AT=0}
      });
      dash.on(dashjs.MediaPlayer.events.QUALITY_CHANGE_RENDERED,function(){updQBtn()});
      dash.on(dashjs.MediaPlayer.events.ERROR,function(e){
        var msg=(e&&e.error&&(e.error.message||e.error.code))||"DASH error";
        if(retries++<2){try{dash.reset();dash.initialize(V,u,true)}catch(ex){fail(it,"DASH: "+msg)}}
        else{try{if(dash){dash.reset()}dash=null}catch(ex){}toast("dash.js fail → Shaka try");tryShaka(it,u,opt)}
      });
      dash.on(dashjs.MediaPlayer.events.PLAYBACK_ERROR,function(){
        if(retries++<2){try{dash.seek(V.currentTime||0)}catch(e){}}
      });
      try{dash.initialize(V,u,true)}catch(e){fail(it,"DASH init fail: "+(e.message||e))}
    },function(){fail(it,"dash.js load nahi hui (internet?)")});
  }else if(eng==="mpegts"){
    loadScript(TS_URL).then(function(){
      if(window.mpegts&&mpegts.isSupported()){mp=mpegts.createPlayer({type:extOf(u0)==="flv"?"flv":"mpegts",url:u,isLive:false});mp.attachMediaElement(V);mp.load();go2()}
      else fail(it,"TS/FLV is browser me supported nahi");
    },function(){fail(it,"mpegts.js load nahi hui")});
  }else{
    CORS_LOAD=!!(opt.cors||GRAPH||(needFx()&&!CORS_BLOCK[u]));
    if(CORS_LOAD)V.crossOrigin="anonymous";else V.removeAttribute("crossorigin");
    V.src=u;go2();
  }
}
function togglePlay(){if(V.paused){var p=V.play();if(p&&p.catch)p.catch(function(){})}else V.pause()}
function skip(s,rip){
  if(isFinite(V.duration))V.currentTime=Math.max(0,Math.min(V.duration,V.currentTime+s));else V.currentTime=Math.max(0,V.currentTime+s);
  if(rip){rip.classList.add("on");setTimeout(function(){rip.classList.remove("on")},450)}
}
function showCtl(){PL.classList.add("show");clearTimeout(hideT);if(!V.paused)hideT=setTimeout(function(){if(!$("#pbm").hidden)return;PL.classList.remove("show");$("#menu").hidden=true},2800)}
function updPlayIcon(){$("#bPlay").innerHTML=V.paused?"&#9654;":"&#10074;&#10074;";PL.classList.toggle("paused",V.paused);PL.classList.toggle("dim",!!(V.paused&&pbv("dim")&&(V.currentTime||0)>0.5&&!ENDSHOWN))}
function updTime(){
  var d=V.duration,c=V.currentTime||0;
  $("#tm").textContent=fmtTime(c)+" / "+(isFinite(d)?fmtTime(d):"LIVE");
  if(isFinite(d)&&d>0&&!dragging){var p=c/d*100;$("#pro").style.width=p+"%";$("#knob").style.left=p+"%"}
}
function updBuf(){
  try{var d=V.duration,b=V.buffered;if(b.length&&isFinite(d)&&d>0){var c=V.currentTime,e=0;for(var i=0;i<b.length;i++){if(b.start(i)<=c&&b.end(i)>=c)e=b.end(i)}$("#buf").style.width=(e/d*100)+"%"}}catch(e){}
}
function bindVideo(){
V.addEventListener("play",function(){hideEnd();updPlayIcon();showCtl();amN=0;ambStart()});
V.addEventListener("seeked",function(){amN=0;ambStart()});V.addEventListener("loadeddata",function(){amN=0;ambStart()});
V.addEventListener("pause",function(){updPlayIcon();showCtl()});
V.addEventListener("waiting",function(){setLoading(true);onBufferToast()});
V.addEventListener("playing",function(){setLoading(false);showErr("");preloadNext()});
V.addEventListener("canplay",function(){setLoading(false)});
V.addEventListener("progress",updBuf);
V.addEventListener("timeupdate",function(){
  updTime();updBuf();
  if(CUR&&Date.now()-lastSave>4000&&V.currentTime>1){lastSave=Date.now();saveHist(CUR,V.currentTime,isFinite(V.duration)?V.duration:0)}
  if(AB.a!==null&&AB.b!==null&&V.currentTime>=AB.b){V.currentTime=AB.a}
  var o=pbv("outro"),d=V.duration;
  if(o>0&&!OUTRO_DONE&&isFinite(d)&&d>o*4&&d-V.currentTime<=o&&V.currentTime>1){OUTRO_DONE=true;V.pause();toast("Outro skipped");onEnded()}
});
V.addEventListener("loadedmetadata",function(){
  updTime();if(CUR&&isFinite(V.duration))DUR[CUR.u]=V.duration;
  if(CUR&&SPD[CUR.u]){V.playbackRate=SPD[CUR.u]}$("#bSpd").textContent=V.playbackRate+"x";
  if(PENDING_FX){PENDING_FX=false;applyAudioFx()}else if(GRAPH||needFx()){applyAudioFx()}
  if(SEEK_AT>1){try{V.currentTime=SEEK_AT}catch(e){}SEEK_AT=0;return}
  var h=CUR&&HIST[CUR.u];
  if(h&&h.pos>5&&isFinite(V.duration)&&h.pos<V.duration-8){V.currentTime=h.pos;toast("Resumed from "+fmtTime(h.pos))}
  else if(pbv("intro")>0&&isFinite(V.duration)&&V.duration>pbv("intro")*4){V.currentTime=pbv("intro");toast("Intro skipped ("+pbv("intro")+"s)")}
});
V.addEventListener("ended",function(){onEnded()});
V.addEventListener("error",function(){
  if(!(CUR&&!hls&&!mp&&!dash&&V.getAttribute("src")))return;
  if(CORS_LOAD){                                          /* CORS wali load fail: boost nahi, normal play */
    CORS_BLOCK[CURURL]=true;CORS_LOAD=false;PENDING_FX=false;
    var at=V.currentTime||FX_AT||0,wasG=GRAPH;
    pbset("boost",100);pbset("night",false);
    if(wasG)replaceVideo();
    toast("Server CORS allow nahi karta: Boost/Night mode band, normal play");
    reloadCur(at);return;
  }
  fail(CUR,"Ye format/codec browser support nahi karta (MKV/AVI/HEVC/WMV etc.)")
});
V.addEventListener("volumechange",function(){$("#bVol").innerHTML=(V.muted||V.volume===0)?"&#128263;":"&#128266;";$("#vol").value=V.muted?0:V.volume});
}
bindVideo();
$("#vol").addEventListener("input",function(){V.muted=false;V.volume=parseFloat(this.value)});
$("#bVol").onclick=function(){V.muted=!V.muted};
$("#bPlay").onclick=togglePlay;
$("#bNext").onclick=function(){var n=nextItem(1);if(n)go(n);else toast("Aur video nahi hai")};
function upList(){return LIST.indexOf(CUR)>-1?LIST:DATA}
function nextItem(d){var l=upList(),i=l.indexOf(CUR);return l[i+d]||null}
/* seek bar */
var seek=$("#seek");
function frac(e){var r=seek.getBoundingClientRect();return Math.min(1,Math.max(0,(e.clientX-r.left)/r.width))}
seek.addEventListener("pointerdown",function(e){dragging=true;seek.classList.add("drag");try{seek.setPointerCapture(e.pointerId)}catch(x){}move(e)});
seek.addEventListener("pointermove",function(e){var f=frac(e),d=V.duration;if(isFinite(d)){$("#tip").textContent=fmtTime(f*d);$("#tip").style.left=(f*100)+"%"}if(dragging)move(e)});
seek.addEventListener("pointerup",function(e){if(dragging){move(e);dragging=false;seek.classList.remove("drag")}});
function move(e){var f=frac(e),d=V.duration;if(isFinite(d)&&d>0){$("#pro").style.width=(f*100)+"%";$("#knob").style.left=(f*100)+"%";V.currentTime=f*d}}
/* gestures on overlay */
var ov=$("#ov"),lastTap=0,tapT=null;
ov.addEventListener("click",function(e){
  var now=Date.now(),r=ov.getBoundingClientRect(),x=(e.clientX-r.left)/r.width;
  if(now-lastTap<300){clearTimeout(tapT);lastTap=0;if(x<.33)skip(-10,$("#ripL"));else if(x>.67)skip(10,$("#ripR"));else toggleFs()}
  else{lastTap=now;tapT=setTimeout(function(){if(isTouch&&!PL.classList.contains("show")&&!V.paused)showCtl();else togglePlay()},260)}
});
PL.addEventListener("mousemove",showCtl);PL.addEventListener("touchstart",showCtl,{passive:true});
var TG={};
ov.addEventListener("touchstart",function(e){if(e.touches.length!==1){TG={};return}var t_=e.touches[0];TG={x:t_.clientX,y:t_.clientY,m:null,v0:V.volume,b0:pbv("bright"),t0:Date.now()};
  clearTimeout(TG.holdT);TG.holdT=setTimeout(function(){if(!TG.t0||TG.m)return;HOLD2X=true;HOLD_SPD=V.playbackRate||1;V.playbackRate=Math.max(HOLD_SPD,2);toast("2x (hold)")},450);
},{passive:true});
ov.addEventListener("touchend",function(){clearTimeout(TG.holdT);if(HOLD2X){V.playbackRate=HOLD_SPD;HOLD2X=false;toast("1x");$("#bSpd").textContent=V.playbackRate+"x"}TG={};},{passive:true});
ov.addEventListener("touchcancel",function(){clearTimeout(TG.holdT);if(HOLD2X){V.playbackRate=HOLD_SPD;HOLD2X=false}$("#bSpd").textContent=(V.playbackRate||1)+"x";TG={};},{passive:true});
ov.addEventListener("touchmove",function(e){
  if(!e.touches||e.touches.length!==1||TG.x===undefined)return;
  var t_=e.touches[0],dx=t_.clientX-TG.x,dy=t_.clientY-TG.y,r_=ov.getBoundingClientRect();
  if(!TG.m){if(Math.abs(dy)>14&&Math.abs(dy)>Math.abs(dx)*1.5){clearTimeout(TG.holdT);if(HOLD2X){V.playbackRate=HOLD_SPD;HOLD2X=false}TG.m=(TG.x-r_.left)<r_.width/2?"b":"v"}else return}
  var d_=-dy/(r_.height||300);
  if(TG.m==="v"){V.muted=false;V.volume=Math.min(1,Math.max(0,TG.v0+d_));toast("Volume "+Math.round(V.volume*100)+"%")}
  else{var b_=Math.round(Math.min(200,Math.max(50,TG.b0+d_*150)));pbset("bright",b_);applyVideoFx();toast("Brightness "+b_+"%")}
},{passive:true});
/* menus */
var menu=$("#menu");
function openMenu(items,cur,fn){
  if(!menu.hidden&&menu._k===items.join("|")){menu.hidden=true;return}
  menu.textContent="";menu._k=items.join("|");
  items.forEach(function(it){var b=el("button",it.v===cur?"on":"",it.l);b.onclick=function(){fn(it.v);menu.hidden=true};menu.appendChild(b)});
  menu.hidden=false;
}
$("#bSpd").onclick=function(){
  var sp=[0.25,0.5,0.75,1,1.25,1.5,2,3,4].map(function(s){return {v:s,l:s+"x"}});
  openMenu(sp,V.playbackRate,setSpeed);
};
$("#bQ").onclick=function(){
  if(hls&&hls.levels&&hls.levels.length>1){
    var it=[{v:-1,l:"Auto"}];
    hls.levels.map(function(l,i){return {v:i,l:(l.height?qLabel(l.height):(Math.round(l.bitrate/1000)+"k")),h:l.height||0}}).sort(function(a,b){return b.h-a.h}).forEach(function(x){it.push(x)});
    openMenu(it,hls.currentLevel,function(v){hls.currentLevel=v;updQBtn()});
  }else if(dash&&dashBitrates().length){
    var db=dashBitrates(),cur=-1;
    try{cur=dash.getAutoSwitchQualityFor&&dash.getAutoSwitchQualityFor("video")?-1:(dash.getQualityFor("video")||-1)}catch(e){}
    var it=[{v:-1,l:"Auto"}];
    db.map(function(b,i){return {v:i,l:(b.height?qLabel(b.height):(Math.round(b.bitrate/1000)+"k"))+(b.bitrate?" · "+Math.round(b.bitrate/1000)+"k":""),h:b.height||0}}).sort(function(a,b){return b.h-a.h}).forEach(function(x){it.push(x)});
    openMenu(it,cur,function(v){
      try{
        if(v<0){dash.setAutoSwitchQualityFor("video",true)}
        else{dash.setAutoSwitchQualityFor("video",false);dash.setQualityFor("video",v)}
      }catch(e){}
      updQBtn();
    });
  }else if(hasVariants(CUR)){
    var items=[{v:"auto",l:"Auto"}].concat(CUR.v.map(function(p){return {v:p[0],l:qLabel(p[0])}}));
    openMenu(items,CURQ,setQuality);
  }
};
$("#bPip").onclick=function(){try{if(document.pictureInPictureElement)document.exitPictureInPicture();else if(V.requestPictureInPicture)V.requestPictureInPicture();else toast("PiP supported nahi")}catch(e){toast("PiP supported nahi")}};
function toggleTheater(){document.body.classList.toggle("theater")}
$("#bTh").onclick=toggleTheater;
function toggleFs(){
  var d=document;
  if(d.fullscreenElement||d.webkitFullscreenElement){(d.exitFullscreen||d.webkitExitFullscreen).call(d);return}
  var f=PL.requestFullscreen||PL.webkitRequestFullscreen;
  if(f)f.call(PL);else if(V.webkitEnterFullscreen)V.webkitEnterFullscreen();
}
$("#bFs").onclick=toggleFs;
document.addEventListener("keydown",function(e){
  if($("#watch").hidden)return;
  var t=e.target,tn=t&&t.tagName;if(tn==="SELECT"||tn==="TEXTAREA"||(tn==="INPUT"&&t.type!=="range"&&t.type!=="checkbox"))return;
if(e.key==="i"||e.key==="I"){e.preventDefault();toggleStats();return}
  if(e.key==="c"||e.key==="C"){e.preventDefault();var tk=V.querySelector("track");if(tk){tk.track.mode=tk.track.mode==="showing"?"hidden":"showing";toast("Captions "+(tk.track.mode==="showing"?"ON":"OFF"))}else toast("Subtitle nahi lagi");return}
  if(e.key==="s"&&!e.ctrlKey&&!e.metaKey){e.preventDefault();shareTimestamp();return}
  var k=e.key;
  if(k==="Escape"&&!$("#pbm").hidden){pbClose();return}
  if(k===" "||k==="k"){e.preventDefault();togglePlay()}
  else if(k==="ArrowRight"){skip(5,$("#ripR"))}else if(k==="ArrowLeft"){skip(-5,$("#ripL"))}
  else if(k==="l"){skip(10,$("#ripR"))}else if(k==="j"){skip(-10,$("#ripL"))}
  else if(k==="ArrowUp"){e.preventDefault();V.volume=Math.min(1,V.volume+.1)}else if(k==="ArrowDown"){e.preventDefault();V.volume=Math.max(0,V.volume-.1)}
  else if(k==="m"){V.muted=!V.muted}else if(k==="f"){toggleFs()}else if(k==="t"){toggleTheater()}
  else if(k==="."){V.pause();V.currentTime=(V.currentTime||0)+1/30}else if(k===","){V.pause();V.currentTime=Math.max(0,(V.currentTime||0)-1/30)}
  else if(k==="n"){var n=nextItem(1);if(n)go(n)}else if(k==="p"){var p=nextItem(-1);if(p)go(p)}
  else if(/^[0-9]$/.test(k)&&isFinite(V.duration)){V.currentTime=V.duration*(+k)/10}
  if(k!==" ")showCtl();
});
/* media session */
function setSession(it){
  if(!("mediaSession" in navigator))return;
  try{
    navigator.mediaSession.metadata=new MediaMetadata({title:it.t||"Video",artist:domainOf(it.u),artwork:THUMBS[it.u]?[{src:THUMBS[it.u],sizes:"320x180",type:"image/jpeg"}]:(it.th?[{src:it.th}]:[])});
    navigator.mediaSession.setActionHandler("play",function(){V.play()});
    navigator.mediaSession.setActionHandler("pause",function(){V.pause()});
    navigator.mediaSession.setActionHandler("seekbackward",function(){skip(-10)});
    navigator.mediaSession.setActionHandler("seekforward",function(){skip(10)});
    navigator.mediaSession.setActionHandler("nexttrack",function(){var n=nextItem(1);if(n)go(n)});
    navigator.mediaSession.setActionHandler("previoustrack",function(){var p=nextItem(-1);if(p)go(p)});
  }catch(e){}
}

/* ---------- Ambient light (YouTube jaisa glow; settings = ambient.json) ---------- */
var AM=CFG.amb||{},AMS=LS.get("ytb_amb",{}),amCv=$("#amb"),amCx=null,amRaf=0,amLast=0,amN=0,amTry=0,amBars={t:0,b:0,on:true};
function amv(k,d){return AMS[k]!==undefined?AMS[k]:(AM[k]!==undefined?AM[k]:d)}
function amset(k,v){AMS[k]=v;LS.set("ytb_amb",AMS);applyAmb()}
function applyAmb(){
  var on=!!amv("enabled",true),sp=amv("spread",130)/100,bl=amv("blur",25),fs=amv("spreadFadeStart",20),fc=amv("spreadFadeCurve",20);
  document.body.classList.toggle("amb-on",on);amCv.hidden=!on;
  amCv.style.width=(sp*100)+"%";amCv.style.height=(sp*100)+"%";amCv.style.left=((1-sp)*50)+"%";amCv.style.top=((1-sp)*50)+"%";
  amCv.style.filter="blur("+Math.round(bl*1.6)+"px) saturate(1.35)";
  var m="radial-gradient(ellipse closest-side,#000 "+Math.max(0,100-fs-fc)+"%,rgba(0,0,0,0) 100%)";
  amCv.style.webkitMaskImage=m;amCv.style.maskImage=m;amCv.style.opacity=amv("intensity",90)/100;
  document.documentElement.style.setProperty("--hsh","0 0 "+amv("headerShadowSize",25)+"px rgba(0,0,0,"+(amv("headerShadowOpacity",30)/100)+")");
  if(on)ambStart();
}
function detectBars(vw,vh){
  try{
    var c=document.createElement("canvas");c.width=64;c.height=36;var x=c.getContext("2d");x.drawImage(V,0,0,64,36);
    var d=x.getImageData(0,0,64,36).data;
    function lum(r){var s=0;for(var i=0;i<64;i++){var p=(r*64+i)*4;s+=d[p]*.3+d[p+1]*.59+d[p+2]*.11}return s/64}
    var t=0,b=0;while(t<12&&lum(t)<14)t++;while(b<12&&lum(35-b)<14)b++;
    amBars={t:t/36,b:b/36,on:true};
  }catch(e){amBars={t:0,b:0,on:false}}
}
function ambDraw(){
  if(!amCx){amCv.width=128;amCv.height=72;try{amCx=amCv.getContext("2d")}catch(e){}if(!amCx)return}
  var vw=V.videoWidth,vh=V.videoHeight;if(!vw||!vh||V.readyState<2)return;
  if(amv("detectHorizontalBarSizeEnabled",true)&&amBars.on!==false&&amN%45===0)detectBars(vw,vh);
  var sy=Math.round(vh*amBars.t),sh=Math.max(1,Math.round(vh*(1-amBars.t-amBars.b)));
  try{amCx.drawImage(V,0,sy,vw,sh,0,0,128,72);amN++}catch(e){}
}
function ambFrame(ts){
  amRaf=0;
  if(!amv("enabled",true)||$("#watch").hidden||document.hidden)return;
  var fps=Math.min(60,Math.max(5,amv("framerateLimit",60)));
  if(ts-amLast>=1000/fps){amLast=ts;amTry++;ambDraw()}
  if(amTry<400&&(!V.paused||amN<3||!amv("energySaver",true)))amRaf=requestAnimationFrame(ambFrame);   /* data na ho to bhi bounded */   /* paused: 2-3 frame ke baad ruk jao */
}
function ambStart(){if(!amRaf&&amv("enabled",true)&&!$("#watch").hidden){amTry=0;amRaf=requestAnimationFrame(ambFrame)}}
document.addEventListener("visibilitychange",function(){if(!document.hidden){amN=0;ambStart()}});

/* ---------- end screen (YouTube jaisa: replay + up next countdown + suggestions) ---------- */
function hideEnd(){clearInterval(endT);endT=null;ENDSHOWN=false;var e=$("#endsc");if(e){e.hidden=true;e.textContent=""}}
function onEnded(){
  if(!CUR||ENDSHOWN)return;
  saveHist(CUR,0,isFinite(V.duration)?V.duration:0);
  showEnd();
}
function showEnd(){
  ENDSHOWN=true;PL.classList.remove("dim");
  var box=$("#endsc");box.textContent="";box.hidden=false;
  var l=upList(),i=l.indexOf(CUR),next=l[i+1]||null,auto=$("#auto").checked,head=el("div","eh");
  if(next){
    var en=el("div","en"),th=mkThumb(next),tx=el("div","tx");
    tx.appendChild(el("div","sub","Up next"));tx.appendChild(el("h3","ttl",next.t||"Video"));
    en.appendChild(th);en.appendChild(tx);en.onclick=function(){hideEnd();go(next)};head.appendChild(en);
    if(auto){
      var cd=el("div","cd"),inn=el("i","","6"),left=6;cd.appendChild(inn);cd.style.setProperty("--p","0%");head.appendChild(cd);
      endT=setInterval(function(){left--;inn.textContent=left;cd.style.setProperty("--p",((6-left)/6*100)+"%");if(left<=0){hideEnd();go(next)}},1000);
    }
  }
  var bt=el("div","eb2"),rp=el("button","","\u21BA Replay");
  rp.onclick=function(){hideEnd();V.currentTime=0;var p=V.play();if(p&&p.catch)p.catch(function(){})};bt.appendChild(rp);
  if(next&&auto){var cn=el("button","g","Cancel autoplay");cn.onclick=function(){clearInterval(endT);endT=null;var c=head.querySelector(".cd");if(c)c.remove();cn.remove()};bt.appendChild(cn)}
  if(next){var pn=el("button","","Play now");pn.onclick=function(){hideEnd();go(next)};bt.appendChild(pn)}
  head.appendChild(bt);box.appendChild(head);
  var sug=l.slice(i+2,i+8);
  if(!sug.length)sug=l.filter(function(x){return x!==CUR&&x!==next}).slice(0,6);
  if(sug.length){
    box.appendChild(el("div","sub","More videos"));var g=el("div","eg");
    sug.forEach(function(x){var c=el("div","c");c.appendChild(mkThumb(x));c.appendChild(el("div","ttl",x.t||"Video"));c.onclick=function(){hideEnd();go(x)};g.appendChild(c)});
    box.appendChild(g);
  }
}

/* ---------- extras: refresh link, speed memory, subtitles, sleep, A-B, shot, audio-only, cast, import ---------- */
var RFTRIED={},SPD=LS.get("ytb_spd",{}),AB={a:null,b:null},AUDONLY=false,SLEEPT=null,SLEEPEND=0,SUBURL=null;
function setSpeed(v){V.playbackRate=v;$("#bSpd").textContent=v+"x";if(CUR){if(v===1)delete SPD[CUR.u];else SPD[CUR.u]=v;LS.set("ytb_spd",SPD)}}
function applyFresh(it,nw){
  var old=it.u;
  ["u","th","d","ip","v","rf"].forEach(function(k){if(nw[k]!==undefined)it[k]=nw[k]});
  if(nw.x!==undefined){it.x=nw.x;if(nw.xa)it.xa=nw.xa;else delete it.xa}else{delete it.x;delete it.xa}
  if(old!==it.u){
    delete BYURL[old];BYURL[it.u]=it;
    [FAV,LATER].forEach(function(m){if(m[old]){delete m[old];m[it.u]=slim(it)}});
    if(HIST[old]){HIST[it.u]=HIST[old];delete HIST[old];HIST[it.u].it=slim(it)}
    if(SPD[old]){SPD[it.u]=SPD[old];delete SPD[old];LS.set("ytb_spd",SPD)}
    LS.set("ytb_hist",HIST);saveLists();
  }
}
function refreshItem(it,cb){
  if(!it.rf){cb(false);return}
  fetch(it.rf).then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.error||"fail");return j})})
    .then(function(nw){applyFresh(it,nw);cb(true)}).catch(function(){cb(false)});
}
function refreshAll(list,done){
  var todo=list.filter(function(x){return x.rf}),i=0,okn=0,run=0,fin=0;
  if(!todo.length){toast("Is list me refresh-able link nahi");return}
  function next(){
    if(fin>=todo.length){toast("\uD83D\uDD04 "+okn+"/"+todo.length+" links refresh hui");if(done)done();return}
    while(run<3&&i<todo.length){(function(it){run++;i++;refreshItem(it,function(ok){if(ok)okn++;run--;fin++;toast("Refresh "+fin+"/"+todo.length);next()})})(todo[i])}
  }
  next();
}
function srt2vtt(t){return "WEBVTT\n\n"+String(t).replace(/\r/g,"").replace(/(\d+:\d+:\d+),(\d+)/g,"$1.$2")}
function setSub(vtt,label){
  var old=V.querySelector("track");if(old)old.remove();
  if(SUBURL){try{URL.revokeObjectURL(SUBURL)}catch(e){}SUBURL=null}
  if(!vtt){toast("Subtitles off");return}
  SUBURL=URL.createObjectURL(new Blob([vtt],{type:"text/vtt"}));
  var tr=document.createElement("track");tr.kind="subtitles";tr.label=label||"Sub";tr.srclang="en";tr.src=SUBURL;tr.default=true;V.appendChild(tr);
  try{tr.track.mode="showing"}catch(e){}
  toast("Subtitles on");
}
function pickFile(accept,cb){
  var i=document.createElement("input");i.type="file";i.accept=accept;
  i.onchange=function(){var f=i.files&&i.files[0];if(!f)return;var rd=new FileReader();rd.onload=function(){cb(String(rd.result||""))};rd.readAsText(f)};
  i.click();
}
function parseLinks(txt){
  var out=[],pend=null;
  String(txt).split(/\r?\n/).forEach(function(ln){
    ln=ln.trim();if(!ln)return;
    if(/^#EXTINF/i.test(ln)){pend=(ln.split(",").slice(1).join(",")||"").trim();return}
    if(ln[0]==="#")return;
    var m=ln.match(/(https?:\/\/[^\s"<>]+)/);
    if(!m){var mt=ln.match(/^(?:\d+[.)]\s*)?Title\s*:\s*(.+)$/i);if(mt)pend=mt[1].trim();return}
    var t_=ln.slice(0,m.index).replace(/^\d+[.)]\s*/,"").replace(/[\s:\-|>]+$/,"").trim()||pend||domainOf(m[1])||"Video";
    out.push({t:t_.slice(0,200),u:m[1],k:"VIDEO",p:"",th:"",d:0,v:[]});pend=null;
  });
  return out;
}
function setSleep(min){
  clearTimeout(SLEEPT);SLEEPT=null;SLEEPEND=0;
  if(!min){toast("Sleep timer off");return}
  SLEEPEND=Date.now()+min*60000;
  SLEEPT=setTimeout(function(){V.pause();toast("Sleep timer: pause kar diya");SLEEPT=null;SLEEPEND=0},min*60000);
  toast("Sleep timer: "+min+" min");
}
function shot(){
  var msg="CORS ki wajah se screenshot nahi ho sakta (Bot proxy on karke try karo)";
  try{
    var c=document.createElement("canvas");c.width=V.videoWidth;c.height=V.videoHeight;if(!c.width){toast("Video abhi load nahi hui");return}
    c.getContext("2d").drawImage(V,0,0);
    c.toBlob(function(b){if(!b){toast(msg);return}var a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="screenshot.png";document.body.appendChild(a);a.click();a.remove();toast("Screenshot saved")},"image/png");
  }catch(e){toast(msg)}
}
function toggleAudOnly(){AUDONLY=!AUDONLY;V.style.visibility=AUDONLY?"hidden":"";$("#aud").hidden=!(AUDONLY||(CUR&&isAudio(CUR)));toast(AUDONLY?"Audio-only ON":"Audio-only OFF")}
function castTV(){
  if(V.remote&&V.remote.prompt){V.remote.prompt().catch(function(){toast("Koi device nahi mila")})}
  else toast("Is browser me Cast/AirPlay supported nahi (Chrome ya Safari use karo)");
}

/* ---------- Play URL box (MPD + ClearKey, cookie, proxy) ---------- */
function openUrlBox(){
  var old=$("#ubox");if(old)old.remove();
  var bx=el("div","ubox");bx.id="ubox";var c=el("div","ucard");
  c.appendChild(el("h3","","\uD83D\uDD17 Play URL / MPD"));
  function fld(lbl,tag,ph){var w=el("label","ufl",lbl),i=el(tag);if(ph)i.placeholder=ph;if(tag==="textarea")i.rows=2;w.appendChild(i);c.appendChild(w);return i}
  var iu=fld("Stream URL (mp4 / m3u8 / mpd ...)","input","https://...");
  var it_=fld("Title (optional)","input","");
  var ty=el("select");[["","Auto detect"],["hls","HLS (.m3u8)"],["dash","DASH (.mpd)"],["native","MP4 / WebM / other"]].forEach(function(o){var op=el("option","",o[1]);op.value=o[0];ty.appendChild(op)});
  var tw=el("label","ufl","Type");tw.appendChild(ty);c.appendChild(tw);
  var ik=fld("ClearKey (MPD) - key_id:key, ek line me ek","textarea","kid_hex:key_hex");
  var ic=fld("Cookie (optional)","input","name=value; name2=value2");
  c.appendChild(el("div","unote","Cookie/Referer header browser se bhejna mumkin nahi, isliye cookie sirf 'Copy command' me use hoti hai. Cookie ya acctoken wali stream agar direct na chale to bot-proxy (PROXY_BASE) ya VLC/ffmpeg use karo."));
  var row=el("div","ubtn");
  function mk(){
    var u=iu.value.trim();if(!/^https?:\/\//i.test(u)){toast("Valid http(s) URL daalo");return null}
    var drm=ik.value.split(/\n/).map(function(l){var p=l.trim().split(":");return p.length>=2&&p[0]&&p[1]?[p[0].trim(),p.slice(1).join(":").trim()]:null}).filter(Boolean);
    var item=BYURL[u]||{t:"",u:u,k:"VIDEO",p:"",th:"",d:0,v:[],_x:1};
    item.t=it_.value.trim()||item.t||domainOf(u)||"Pasted URL";item.drm=drm.length?drm:undefined;item.eng=ty.value||(/\.mpd(\?|$)/i.test(u)?"dash":undefined);
    if(!BYURL[u]){BYURL[u]=item;ALL.push(item)}
    return item;
  }
  var pl=el("button","","\u25B6 Play");pl.onclick=function(){var item=mk();if(!item)return;bx.remove();PXS[u0of(item)]=undefined;go(item)};
  function u0of(i){return i.u}
  var cm=el("button","g","Copy ffmpeg command");cm.onclick=function(){
    var item=mk();if(!item)return;var k=(item.drm&&item.drm[0])?" -decryption_key "+item.drm[0][1]:"",ck=ic.value.trim()?' -headers "Cookie: '+ic.value.trim().replace(/"/g,'\\"')+'\\r\\n"':"";
    copy("ffmpeg"+ck+k+' -i "'+item.u+'" -c copy out.mp4')};
  var cn=el("button","g","Cancel");cn.onclick=function(){bx.remove()};
  row.appendChild(pl);row.appendChild(cm);row.appendChild(cn);c.appendChild(row);
  bx.appendChild(c);bx.onclick=function(e){if(e.target===bx)bx.remove()};document.body.appendChild(bx);iu.focus();
}

/* ---------- 3-dot settings panel (Playback / Advanced color) ---------- */
function pbClose(){$("#pbm").hidden=true}
function renderPb(level){
  PBLEVEL=level||"main";var box=$("#pbm");box.textContent="";box.hidden=false;
  function hd(txt,back){
    var h=el("div","hd");
    if(back){h.appendChild(el("span","","\u2039"));h.appendChild(el("span","",txt));h.onclick=function(){renderPb(back)}}
    else{h.appendChild(el("span","",txt));var x=el("span","","\u2715");x.style.marginLeft="auto";x.onclick=pbClose;h.appendChild(x)}
    return h;
  }
  function nav(label,sub,to){var r=el("div","row2"),t=el("div");t.appendChild(el("div","",label));if(sub)t.appendChild(el("small","",sub));r.appendChild(t);r.appendChild(el("span","arrow","\u203A"));r.onclick=function(){renderPb(to)};return r}
  function sw(label,sub,key,after){
    var r=el("div","row2"),t=el("div");t.appendChild(el("div","",label));if(sub)t.appendChild(el("small","",sub));
    var s=el("div","sw"+(pbv(key)?" on":""));r.appendChild(t);r.appendChild(s);
    r.onclick=function(){pbset(key,!pbv(key));s.className="sw"+(pbv(key)?" on":"");applyVideoFx();updPlayIcon();if(after)after()};return r;
  }
  function slider(label,key,min,max,step,fmt,after){
    var w=el("div","sl"),lb=el("div","lb"),vv=el("span","",fmt(pbv(key))),rs=el("button","","\u00D7"),right=el("div");
    right.appendChild(vv);right.appendChild(rs);lb.appendChild(el("div","",label));lb.appendChild(right);
    var inp=el("input");inp.type="range";inp.min=min;inp.max=max;inp.step=step;inp.value=pbv(key);
    inp.oninput=function(){pbset(key,+inp.value);vv.textContent=fmt(+inp.value);applyVideoFx();if(after)after()};
    rs.onclick=function(){inp.value=PBDEF[key];inp.oninput()};
    w.appendChild(lb);w.appendChild(inp);return w;
  }
  var pct=function(v){return v+"%"},secs=function(v){return v?v+"s":"Off"};
  if(PBLEVEL==="main"){
    box.appendChild(hd("Settings"));
    box.appendChild(nav("\u25B6  Playback","Speed, zoom, brightness, volume boost, skip...","play"));
    box.appendChild(sw("\u21BB  Loop video",null,"loop"));
    var ar=el("div","row2"),at=el("div");at.appendChild(el("div","","\u2728  Ambient light"));at.appendChild(el("small","","Video ke peeche glow (YouTube jaisa)"));
    var asw=el("div","sw"+(amv("enabled",true)?" on":""));ar.appendChild(at);ar.appendChild(asw);
    ar.onclick=function(){amset("enabled",!amv("enabled",true));asw.className="sw"+(amv("enabled",true)?" on":"")};box.appendChild(ar);
    box.appendChild(nav("Ambient settings","Blur, spread, intensity, fps","amb"));
    var pu=el("div","row2"),put=el("div");put.appendChild(el("div","","\uD83D\uDD17  Play URL (MPD / key / cookie)"));put.appendChild(el("small","","Koi bhi link paste karke chalao"));pu.appendChild(put);pu.onclick=function(){pbClose();openUrlBox()};box.appendChild(pu);
    box.appendChild(nav("More tools","Subtitles, sleep timer, A-B repeat, refresh, screenshot, cast...","tools"));
    if(CFG.px){var xr=el("div","row2"),xt=el("div");xt.appendChild(el("div","","\uD83D\uDEE1  Bot proxy (acctoken / IP-lock links)"));xt.appendChild(el("small","","Stream bot ke through chalegi (same IP + Referer)"));
      var xs=el("div","sw"+(LS.get("ytb_px",true)?" on":""));xr.appendChild(xt);xr.appendChild(xs);xr.onclick=function(){LS.set("ytb_px",!LS.get("ytb_px",true));xs.className="sw"+(LS.get("ytb_px",true)?" on":"");if(CUR&&CUR.x)loadItem(CUR,{keepQ:true,at:V.currentTime})};box.appendChild(xr)}
    if(CUR){
      var cp=el("div","row2");cp.appendChild(el("div","","\uD83D\uDCCB  Copy link"));cp.onclick=function(){copy(CURURL||CUR.u)};box.appendChild(cp);
      extLinks(CURURL||CUR.u).forEach(function(x){var a=el("a","row2");a.appendChild(el("div","","\uD83D\uDCFA  "+(x.n==="Open link"?"Open / Download":"Open in "+x.n)));a.href=x.h;a.target="_blank";a.rel="noopener noreferrer";box.appendChild(a)});
    }
  }else if(PBLEVEL==="play"){
    box.appendChild(hd("Playback","main"));
    var chips=el("div","chips2");
    [0.25,0.5,1,1.5,2].forEach(function(s){
      var c=el("div","chip2"+(V.playbackRate===s?" on":""),s+"x");
      c.onclick=function(){setSpeed(s);renderPb("play")};chips.appendChild(c);
    });
    box.appendChild(chips);
    box.appendChild(sw("\u26F6  Zoom to fill",null,"zoom"));
    box.appendChild(slider("Brightness","bright",50,200,5,pct));
    box.appendChild(slider("Volume Boost","boost",100,300,10,pct,applyAudioFx));
    box.appendChild(sw("Night mode - quieter loud scenes, clearer dialogue",null,"night",applyAudioFx));
    box.appendChild(sw("Dim and show the title when paused",null,"dim"));
    box.appendChild(slider("Skip intros automatically (pehle N sec)","intro",0,120,5,secs));
    box.appendChild(slider("Skip end credits automatically (aakhri N sec)","outro",0,120,5,secs));
    box.appendChild(nav("Advanced color","Contrast, saturation, warmth","color"));
  }else if(PBLEVEL==="tools"){
    box.appendChild(hd("More tools","main"));
    function rowBtn(label,sub,fn){var r=el("div","row2"),t_=el("div");t_.appendChild(el("div","",label));if(sub)t_.appendChild(el("small","",sub));r.appendChild(t_);r.onclick=fn;box.appendChild(r);return r}
    if(CUR&&CUR.rf)rowBtn("\uD83D\uDD04  Refresh this link","Expire link ka naya stream page se",function(){pbClose();setLoading(true);refreshItem(CUR,function(ok){if(ok){toast("Naya link mil gaya");loadItem(CUR,{keepQ:true})}else{setLoading(false);toast("Naya link nahi mila")}})});
    var rl=upList().filter(function(x){return x.rf});
    if(rl.length)rowBtn("\uD83D\uDD04  Refresh all links ("+rl.length+")","Poori list ki links dobara nikalo",function(){pbClose();refreshAll(rl,function(){LIST=[];renderGrid()})});
    rowBtn("\uD83D\uDCAC  Subtitles","File ya URL (.srt / .vtt)",function(){renderPb("subs")});
    rowBtn("\uD83D\uDCCA  Live stats","Resolution / bitrate / buffer (i key)",function(){toggleStats();renderPb("tools")});
    rowBtn("\uD83D\uDD0A  Audio track",(function(){var a=listAudioTracks();return a.length?(a.length+" tracks"):"Single / N/A"})(),function(){
      var a=listAudioTracks();if(!a.length){toast("Extra audio track nahi");return;}
      openMenu(a.map(function(x){return {v:x.v,l:x.l}}),-1,function(v){setAudioTrack(v)});
    });
    rowBtn("\u23F2  Sleep timer",SLEEPEND?("Band hoga: "+Math.max(1,Math.round((SLEEPEND-Date.now())/60000))+" min me"):"15 / 30 / 60 / 90 min",function(){renderPb("sleep")});
    var ab=rowBtn("\uD83D\uDD01  A-B repeat",AB.a===null?"A point set karo":(AB.b===null?"A="+fmtTime(AB.a)+" | ab B set karo":"A="+fmtTime(AB.a)+" B="+fmtTime(AB.b)+" (clear karne ko tap)"),function(){
      if(AB.a===null){AB.a=V.currentTime||0;toast("A = "+fmtTime(AB.a))}else if(AB.b===null){if((V.currentTime||0)<=AB.a){toast("B, A ke baad hona chahiye");return}AB.b=V.currentTime;toast("Repeat on")}else{AB.a=AB.b=null;toast("A-B repeat off")}renderPb("tools")});
    rowBtn("\uD83D\uDD17  Share timestamp","Current time wali link copy (s key)",function(){shareTimestamp();pbClose()});
    rowBtn("\uD83D\uDCF8  Screenshot","Current frame PNG",function(){shot()});
    rowBtn("\uD83C\uDFA7  Audio only: "+(AUDONLY?"ON":"OFF"),"Screen band, awaaz chalti rahe",function(){toggleAudOnly();renderPb("tools")});
    rowBtn("\uD83D\uDCFA  Cast to TV","Chromecast / AirPlay (browser support par)",function(){castTV()});
    rowBtn("\uD83D\uDCE5  Import list (.m3u / .txt)","Links ki file se playlist banao",function(){pickFile(".m3u,.m3u8,.txt",function(txt){var its=parseLinks(txt);if(!its.length){toast("File me link nahi mila");return}its.forEach(function(x){if(!BYURL[x.u]){BYURL[x.u]=x;ALL.push(x);DATA.push(x)}});LIST=[];updChips();renderGrid();toast(its.length+" links import hui")})});
    rowBtn("\uD83D\uDCE4  Export library (JSON)","Favorites, watch later, history backup",function(){var b=new Blob([JSON.stringify({fav:FAV,later:LATER,hist:HIST})],{type:"application/json"}),a_=document.createElement("a");a_.href=URL.createObjectURL(b);a_.download="library.json";document.body.appendChild(a_);a_.click();a_.remove();toast("library.json download")});
  }else if(PBLEVEL==="subs"){
    box.appendChild(hd("Subtitles","tools"));
    var su=el("div","sl"),sinp=el("input");sinp.placeholder="https://.../sub.vtt (CORS allow ho)";sinp.style.width="100%";su.appendChild(sinp);box.appendChild(su);
    var go_=el("div","row2");go_.appendChild(el("div","","\u25B6  URL se load karo"));go_.onclick=function(){var u_=sinp.value.trim();if(!/^https?:/i.test(u_)){toast("Valid URL daalo");return}fetch(u_).then(function(r){return r.text()}).then(function(tx){setSub(/^\uFEFF?WEBVTT/.test(tx)?tx:srt2vtt(tx),"URL")}).catch(function(){toast("Subtitle load nahi hui (CORS?) - file se try karo")})};box.appendChild(go_);
    var fl=el("div","row2");fl.appendChild(el("div","","\uD83D\uDCC2  File se load karo (.srt / .vtt)"));fl.onclick=function(){pickFile(".srt,.vtt",function(tx){setSub(/^\uFEFF?WEBVTT/.test(tx)?tx:srt2vtt(tx),"File")})};box.appendChild(fl);
    var off=el("div","row2");off.appendChild(el("div","","\u2715  Subtitles off"));off.onclick=function(){setSub(null)};box.appendChild(off);
  }else if(PBLEVEL==="sleep"){
    box.appendChild(hd("Sleep timer","tools"));
    var chs=el("div","chips2");[["Off",0],["15 min",15],["30 min",30],["60 min",60],["90 min",90]].forEach(function(p_){var c_=el("div","chip2",p_[0]);c_.onclick=function(){setSleep(p_[1]);renderPb("sleep")};chs.appendChild(c_)});box.appendChild(chs);
  }else if(PBLEVEL==="amb"){
    box.appendChild(hd("Ambient light","main"));
    function asl(label,key,min,max,step,fmt){
      var w=el("div","sl"),lb=el("div","lb"),vv=el("span","",fmt(amv(key,0))),rs=el("button","","\u00D7"),right=el("div");
      right.appendChild(vv);right.appendChild(rs);lb.appendChild(el("div","",label));lb.appendChild(right);
      var inp=el("input");inp.type="range";inp.min=min;inp.max=max;inp.step=step;inp.value=amv(key,0);
      inp.oninput=function(){amset(key,+inp.value);vv.textContent=fmt(+inp.value)};
      rs.onclick=function(){delete AMS[key];LS.set("ytb_amb",AMS);inp.value=amv(key,0);vv.textContent=fmt(+inp.value);applyAmb()};
      w.appendChild(lb);w.appendChild(inp);return w;
    }
    box.appendChild(asl("Blur","blur",0,100,1,pct));
    box.appendChild(asl("Spread","spread",100,200,5,pct));
    box.appendChild(asl("Glow intensity","intensity",0,100,5,pct));
    box.appendChild(asl("Fade start","spreadFadeStart",0,60,5,pct));
    box.appendChild(asl("Fade curve","spreadFadeCurve",0,60,5,pct));
    box.appendChild(asl("Frame rate limit","framerateLimit",5,60,5,function(v){return v+" fps"}));
    var rr2=el("div","row2");rr2.appendChild(el("div","","Reset to ambient.json defaults"));rr2.onclick=function(){AMS={};LS.set("ytb_amb",AMS);applyAmb();renderPb("amb")};box.appendChild(rr2);
  }else if(PBLEVEL==="color"){
    box.appendChild(hd("Advanced color","play"));
    box.appendChild(slider("Contrast","contrast",50,200,5,pct));
    box.appendChild(slider("Saturation","sat",0,300,5,pct));
    box.appendChild(slider("Warmth (sepia)","warm",0,100,5,pct));
    var rr=el("div","row2");rr.appendChild(el("div","","Reset color"));
    rr.onclick=function(){["bright","contrast","sat","warm"].forEach(function(k){pbset(k,PBDEF[k])});applyVideoFx();renderPb("color")};box.appendChild(rr);
  }
}
$("#bMore").onclick=function(e){e.stopPropagation();if(!$("#pbm").hidden){pbClose();return}renderPb("main");showCtl()};
document.addEventListener("pointerdown",function(e){var b=$("#pbm");if(!b.hidden&&!e.target.closest("#pbm")&&!e.target.closest("#bMore"))pbClose()});

/* ---------- watch page ---------- */
function actBtn(txt,fn,on){var b=el("button","act"+(on?" on":""),txt);b.onclick=fn;return b}
function renderMeta(it){
  $("#wt").textContent=it.t||"Video";document.title=(it.t||"Video")+" - "+CFG.title+(CFG.owner?" | "+CFG.owner:"");
  var a=$("#acts");a.textContent="";
  var fb=actBtn(FAV[it.u]?"\u2665 Favorited":"\u2661 Favorite",function(){toggleFav(it);fb.className="act"+(FAV[it.u]?" on":"");fb.textContent=FAV[it.u]?"\u2665 Favorited":"\u2661 Favorite"},!!FAV[it.u]);a.appendChild(fb);
  var lb=actBtn(LATER[it.u]?"\u23F1 Saved":"\u23F1 Watch later",function(){toggleLater(it);lb.className="act"+(LATER[it.u]?" on":"");lb.textContent=LATER[it.u]?"\u23F1 Saved":"\u23F1 Watch later"},!!LATER[it.u]);a.appendChild(lb);
  a.appendChild(actBtn("\uD83D\uDCCB Copy link",function(){copy(it.u)}));
  var dl=el("a","act",engineOf(it)==="native"?"\u2B07 Download":"\u2B07 Open stream");dl.href=it.u;dl.target="_blank";dl.rel="noopener noreferrer";if(engineOf(it)==="native")dl.setAttribute("download","");a.appendChild(dl);
  extLinks(it.u).slice(0,2).forEach(function(x){if(x.n==="Open link")return;var e=el("a","act","\uD83D\uDCFA "+x.n);e.href=x.h;e.target="_blank";e.rel="noopener noreferrer";a.appendChild(e)});
  if(CFG.owner&&CFG.tg){var ob=el("a","act","\u2708 "+CFG.owner);ob.href=CFG.tg;ob.target="_blank";ob.rel="noopener noreferrer";a.appendChild(ob)}
  if(it.p){var sp=el("a","act","\u2197 Source page");sp.href=it.p;sp.target="_blank";sp.rel="noopener noreferrer";a.appendChild(sp)}
  var d=$("#desc");d.textContent="";
  function row(k,v){var p=el("div");p.appendChild(el("b","",k+": "));p.appendChild(document.createTextNode(v));d.appendChild(p)}
  row("Format",fmtOf(it)+" ("+engineOf(it)+")");row("Source",domainOf(it.u)||"-");
  if(CFG.owner){var cr=el("div");cr.appendChild(el("b","","Credits: "));var ca=el("a","",CFG.owner);ca.href=CFG.tg||"#";ca.target="_blank";ca.rel="noopener noreferrer";ca.style.color="inherit";cr.appendChild(ca);d.appendChild(cr)}
  if(it.d||DUR[it.u])row("Duration",fmtTime(it.d||DUR[it.u]));
  if(it.ip)row("\u26A0 IP-locked",it.ip+" (link sirf usi network se chalegi)");
  row("Stream",it.u);
}
function renderUpnext(it){
  var box=$("#upn");box.textContent="";var l=upList(),i=l.indexOf(it),n=0;
  for(var j=i+1;j<l.length&&n<40;j++,n++){
    (function(x){
      var r=el("div","up"),th=mkThumb(x),tx=el("div","tx");tx.appendChild(el("h3","ttl",x.t||"Video"));tx.appendChild(el("div","sub",domainOf(x.u)+" \u2022 "+fmtOf(x)));
      r.appendChild(th);r.appendChild(tx);r.onclick=function(){go(x)};box.appendChild(r);
    })(l[j]);
  }
  if(!n)box.appendChild(el("div","sub","Aur video nahi hai"));
}
function showWatch(i){
  var it=ALL[i];if(!it){location.hash="#/";return}
  CUR=it;LIST=LIST.length?LIST:computeList();
  $("#home").hidden=true;chipsEl.hidden=true;$("#watch").hidden=false;tgBack(true);
  $("#dimT").textContent=it.t||"";pbClose();renderMeta(it);renderUpnext(it);loadItem(it);setSession(it);applyAmb();window.scrollTo(0,0);showCtl();
}
function showHome(){
  if(!$("#watch").hidden){hideEnd();pbClose();cancelAnimationFrame(amRaf);amRaf=0;destroyEngines();try{if(document.fullscreenElement)document.exitFullscreen()}catch(e){}$("#watch").hidden=true;CUR=null;document.title=CFG.title+(CFG.owner?" | "+CFG.owner:"")}
  $("#home").hidden=false;chipsEl.hidden=false;tgBack(false);renderGrid();updChips();
}
function route(){var m=location.hash.match(/^#\/w\/(\d+)/);if(m)showWatch(+m[1]);else showHome()}

/* ---------- Telegram Mini App mode (CFG.mini) ---------- */
var MINI=!!CFG.mini,TGW=null,LIBR=[],MJOB=null,syncT=0,lastItems=[];
function api(path,body){
  var o={method:body?"POST":"GET",headers:{"X-Init-Data":(TGW&&TGW.initData)||""}};
  if(body){o.headers["Content-Type"]="application/json";o.body=JSON.stringify(body)}
  return fetch(path,o).then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.error||("HTTP "+r.status));return j})});
}
function pushNow(){if(!MINI||!TGW||!TGW.initData)return Promise.resolve();return api("/api/state",{fav:FAV,later:LATER,hist:HIST,lib:LIBR}).catch(function(){})}
function syncPush(){if(!MINI)return;clearTimeout(syncT);syncT=setTimeout(pushNow,1500)}
function rehydrate(){
  [FAV,LATER].forEach(function(m){Object.keys(m).forEach(function(u){if(!BYURL[u]&&m[u]&&m[u].u){var o=m[u];o._x=1;BYURL[u]=o;ALL.push(o)}})});
  Object.keys(HIST).forEach(function(u){var h=HIST[u];if(!BYURL[u]&&h&&h.it&&h.it.u){h.it._x=1;BYURL[u]=h.it;ALL.push(h.it)}});
}
function mergeState(s){
  if(s&&s.ts){FAV=s.fav||{};LATER=s.later||{};HIST=s.hist||{};LIBR=s.lib||[];LS.set("ytb_fav",FAV);LS.set("ytb_later",LATER);LS.set("ytb_hist",HIST)}
  else pushNow();
  rehydrate();updChips();renderGrid();
}
function setMsg(x){$("#mmsg").textContent=x}
function loadItems(items){
  DATA.splice(0,DATA.length);items.forEach(function(x){DATA.push(x)});
  ALL.length=0;DATA.forEach(function(x){ALL.push(x)});BYURL={};ALL.forEach(function(x){BYURL[x.u]=x});rehydrate();
  VIEW.chip="all";LIST=[];updChips();renderGrid();
}
function startScrape(){
  var urls=$("#murl").value.split(/\s+/).filter(function(x){return /^https?:\/\//i.test(x)});
  if(!urls.length){toast("Valid URL daalo");return}
  setMsg("Shuru ho raha hai...");$("#mgo").disabled=true;$("#mstop").hidden=false;$("#mpost").hidden=true;
  var body=urls.length>1?{urls:urls.slice(0,10),start:1,end:+$("#mpages").value}:{url:urls[0],start:1,end:+$("#mpages").value};
  api("/api/scrape",body).then(function(r){MJOB=r.id;pollJob()})
   .catch(function(e){setMsg("\u274C "+e.message);$("#mgo").disabled=false;$("#mstop").hidden=true});
}
function pollJob(){
  api("/api/job?id="+MJOB).then(function(j){
    if(j.status==="running"){setMsg((j.part?("URL "+j.part+" - "):"")+(j.total?("Extracting "+j.done+"/"+j.total):"Pages scan ho rahe hain..."));setTimeout(pollJob,1200);return}
    $("#mgo").disabled=false;$("#mstop").hidden=true;
    if(j.status==="done"){tgHaptic();lastItems=j.items;loadItems(j.items);setMsg("\u2705 "+j.items.length+" videos mile"+(j.items.length?"":" (bot me /debug <url> se check karo)"));$("#mpost").hidden=!j.items.length}
    else setMsg("\u274C "+(j.error||"fail"));
  }).catch(function(e){setMsg("\u274C "+e.message);$("#mgo").disabled=false;$("#mstop").hidden=true});
}
function playlistsMenu(anchor){
  var es=[];
  LIBR.forEach(function(p,i){
    es.push({t:"\uD83D\uDCDA "+p.name+" ("+p.items.length+")",f:function(){loadItems(p.items);lastItems=p.items;$("#mpost").hidden=false;setMsg("Playlist: "+p.name)}});
    es.push({t:"\uD83D\uDD17  Share: "+p.name,f:function(){shareItems(p.items,p.name)}});
    es.push({t:"\uD83D\uDDD1  Delete: "+p.name,f:function(){LIBR.splice(i,1);pushNow();toast("Playlist deleted")}});
  });
  if(!es.length)es.push({t:"Koi playlist nahi (scrape ke baad Save dabao)",f:function(){}});
  es.push({t:"\uD83D\uDD17  Manage share links",f:function(){api("/api/shares").then(function(r){
    var xs=r.shares.map(function(s_){return {t:"\uD83D\uDDD1 Revoke: "+s_.name+" ("+(s_.expires?new Date(s_.expires*1000).toLocaleDateString():"never")+")",f:function(){api("/api/share/revoke",{token:s_.token}).then(function(){toast("Link band kar di")})}}});
    if(!xs.length)xs.push({t:"Koi active share link nahi",f:function(){}});showPop(anchor,xs)}).catch(function(e){toast("\u274C "+e.message)})}});
  showPop(anchor,es);
}
/* ---- share + admin ---- */
function shareItems(items,name){
  if(!items||!items.length){toast("Pehle scrape karo");return}
  api("/api/share",{name:name,items:items,days:7}).then(function(r){var l=r.link||r.web;copy(l);setMsg("\uD83D\uDD17 Share link copy ho gaya: "+l)}).catch(function(e){toast("\u274C "+e.message)});
}
var ADM=null,ADMTAB="sites";
function admLoad(tab){api("/api/admin/overview").then(function(o){ADM=o;renderAdmin(tab||ADMTAB)}).catch(function(e){toast("\u274C "+e.message)})}
function admDo(path,body,msg){api(path,body).then(function(r){toast(msg||"Done");admLoad()}).catch(function(e){toast("\u274C "+e.message)})}
function openAdmin(){var b=$("#adm");if(!b){b=el("div","adm");b.id="adm";document.body.appendChild(b)}b.hidden=false;b.textContent="Loading...";admLoad("sites")}
function renderAdmin(tab){
  ADMTAB=tab;var box=$("#adm");box.textContent="";
  var hd=el("div","ahd"),x=el("button","act","\u2715 Close");x.onclick=function(){box.hidden=true};hd.appendChild(el("b","","\uD83D\uDEE0 Admin"));hd.appendChild(x);box.appendChild(hd);
  var tabs=el("div","atabs");[["sites","Sites"],["health","Health"],["cookies","Cookies"],["jobs","Jobs"],["settings","Settings"],["users","Users"]].forEach(function(t_){var b=el("button","chip"+(tab===t_[0]?" on":""),t_[1]);b.onclick=function(){renderAdmin(t_[0])};tabs.appendChild(b)});box.appendChild(tabs);
  var body=el("div");box.appendChild(body);
  function row(txt,btns){var r=el("div","arow"),l=el("div","l",txt);r.appendChild(l);(btns||[]).forEach(function(b){r.appendChild(b)});body.appendChild(r);return r}
  function btn(t_,f){var b=el("button","",t_);b.onclick=f;return b}
  function form(fields,label,fn){var f=el("div","aform"),ins=fields.map(function(p){var i=el(p[0]);i.placeholder=p[1];if(p[0]==="textarea")i.rows=3;f.appendChild(i);return i});f.appendChild(btn(label,function(){fn(ins)}));body.appendChild(f)}
  if(tab==="sites"){
    ADM.sites.forEach(function(si){row((si.signed?"\uD83D\uDFE2 ":"\u26AA ")+si.domain+(si.rule?" \uD83E\uDDE9":""),[btn("Test",function(){toast("Test chal raha...");api("/api/admin/test",{domain:si.domain}).then(function(r){toast(r.ok?"\u2705 reachable":"\u274C "+(r.status||"fail")+" "+r.detail)}).catch(function(e){toast("\u274C "+e.message)})})].concat(si.custom?[btn("\uD83D\uDDD1",function(){admDo("/api/admin/site",{action:"remove",domain:si.domain},"Site hata di")})]:[]))});
    form([["input","naya domain, jaise example.com"]],"\u2795 Add site",function(i){admDo("/api/admin/site",{action:"add",domain:i[0].value},"Site add")});
  }else if(tab==="cookies"){
    if(!ADM.cookies.length)row("Koi saved cookie nahi");
    ADM.cookies.forEach(function(d){row("\uD83C\uDF6A "+d,[btn("Delete",function(){admDo("/api/admin/cookie",{action:"del",domain:d},"Cookie delete")})])});
    var mr=el("label","arow","Purani me merge (updatecookie)");var mc=el("input");mc.type="checkbox";mc.checked=true;mr.appendChild(mc);body.appendChild(mr);
    form([["input","domain"],["textarea","cookie: name=value; name2=value2"]],"\uD83D\uDCBE Save cookie",function(i){admDo("/api/admin/cookie",{domain:i[0].value,cookie:i[1].value,merge:mc.checked},"Cookie saved");i[1].value=""});
  }else if(tab==="jobs"){
    if(!ADM.jobs.length)row("Abhi koi job nahi chal raha");
    ADM.jobs.forEach(function(j){row("#"+j.id+" "+j.kind+" | "+j.url+" | pages "+j.pages+" | "+j.secs+"s | user "+j.user,[btn("Cancel",function(){admDo("/api/admin/job",{user:j.user},"Stop request bheji")})])});
    if(ADM.jobs.length)body.appendChild(btn("\u23F9 Cancel all",function(){admDo("/api/admin/job",{all:true},"Sab ko stop request")}));
  }else if(tab==="settings"){
    row("Bot proxy: "+(ADM.proxy||"PROXY_BASE set nahi"));
    if(ADM.px_usage)row("\uD83D\uDEE1 Proxy aaj: "+ADM.px_usage.total_mb+" MB"+(ADM.px_usage.ips.length?" | top: "+ADM.px_usage.ips.map(function(p_){return p_[0]+" "+p_[1]+"MB"}).join(", "):""));
    Object.keys(ADM.settings).forEach(function(k){
      var v=ADM.settings[k],r=row(k+": ",[]);
      if(["verify","ytdlp","keep_preview"].indexOf(k)>-1){r.appendChild(btn(v==="1"||(k==="ytdlp"&&v!=="0")?"ON":"OFF",function(){var on=(v==="1")||(k==="ytdlp"&&v!=="0");admDo("/api/admin/setting",{key:k,value:on?"off":"on"},k+" updated")}))}
      else if(k==="proxy"){var sl=el("select");["auto","on","off"].forEach(function(o){var op=el("option","",o);op.value=o;if(o===(v||"auto"))op.selected=true;sl.appendChild(op)});sl.onchange=function(){admDo("/api/admin/setting",{key:k,value:sl.value},"proxy = "+sl.value)};r.appendChild(sl)}
      else{var inp=el("input");inp.value=v;r.appendChild(inp);r.appendChild(btn("Save",function(){admDo("/api/admin/setting",{key:k,value:inp.value},k+" saved")}))}
    });
    Object.keys(ADM.prefer).forEach(function(d){row("\u2B50 prefer "+d+": "+ADM.prefer[d].join(", "),[btn("Clear",function(){admDo("/api/admin/prefer",{site:d,host:"off"},"prefer clear")})])});
    form([["input","prefer: site (rusvideos.love)"],["input","asli video host (ebacdn.net)"]],"\u2B50 Set prefer host",function(i){admDo("/api/admin/prefer",{site:i[0].value,host:i[1].value},"prefer set")});
  }else if(tab==="health"){
    if(!ADM.health.length)row("Abhi data nahi (kuch scrape chalao)");
    ADM.health.forEach(function(h_){row((h_.last>=0.7?"\uD83D\uDFE2 ":(h_.last>=0.3?"\uD83D\uDFE1 ":"\uD83D\uDD34 "))+h_.domain+": "+Math.round(h_.last*100)+"% (avg "+Math.round(h_.avg*100)+"%, "+h_.runs+" runs)")});
  }else if(tab==="users"){
    ADM.users.forEach(function(u){row((u===ADM.admin?"\uD83D\uDC51 ":"\uD83D\uDC64 ")+u,u===ADM.admin?[]:[btn("Remove",function(){admDo("/api/admin/user",{action:"remove",id:u},"User hata diya")})])});
    form([["input","Telegram user id"]],"\u2795 Add user",function(i){admDo("/api/admin/user",{action:"add",id:+i[0].value},"User add")});
  }
}
function tgBack(on){if(!TGW||!TGW.BackButton)return;try{if(on){TGW.BackButton.show();if(!tgBack.b){tgBack.b=1;TGW.BackButton.onClick(function(){location.hash="#/"})}}else TGW.BackButton.hide()}catch(e){}}
function tgHaptic(){try{if(TGW&&TGW.HapticFeedback)TGW.HapticFeedback.notificationOccurred("success")}catch(e){}}
function initMini(){
  $("#mbar").hidden=false;
  $("#mgo").onclick=startScrape;$("#mstop").onclick=function(){api("/api/stop",{}).then(function(){setMsg("Stop request bheji...")})};
  $("#mpl").onclick=function(e){e.stopPropagation();playlistsMenu(this)};
  $("#mshare").onclick=function(){shareItems(lastItems,(domainOf((lastItems[0]||{}).p||(lastItems[0]||{}).u||"")||"Playlist"))};
  $("#madmin").onclick=openAdmin;
  $("#mrf").onclick=function(){setMsg("Links refresh ho rahi hain...");refreshAll(lastItems,function(){LIST=[];renderGrid();setMsg("\u2705 Refresh poora");pushNow()})};
  $("#msave").onclick=function(){if(!lastItems.length)return;LIBR.unshift({name:(domainOf(lastItems[0].p||lastItems[0].u)||"scrape")+" "+new Date().toLocaleDateString(),ts:Date.now(),items:lastItems});LIBR=LIBR.slice(0,8);pushNow().then(function(){toast("Playlist saved (sab devices me)")})};
  $("#msend").onclick=function(){api("/api/send",{items:lastItems.map(function(x){return {t:x.t,u:x.u}})}).then(function(r){toast("Chat me bhej diya ("+r.n+")")}).catch(function(e){toast("\u274C "+e.message)})};
  $("#mwatch").onclick=function(){api("/api/watch",{action:"add",url:($("#murl").value.trim().split(/\s+/)[0]||""),minutes:60}).then(function(r){toast("Watch #"+r.id+" add (har 60 min)")}).catch(function(e){toast("\u274C "+e.message)})};
  loadScript("https://telegram.org/js/telegram-web-app.js").then(null,function(){}).then(function(){
    TGW=window.Telegram&&Telegram.WebApp;
    if(!TGW||!TGW.initData){setMsg("\u26A0 Ye page Telegram Mini App ke andar kholo (bot me /app)");$("#mgo").disabled=true;return}
    try{TGW.ready();TGW.expand()}catch(e){}
    if(LS.get("ytb_theme",null)===null&&TGW.colorScheme)document.documentElement.setAttribute("data-theme",TGW.colorScheme);
    api("/api/state").then(mergeState).catch(function(e){setMsg("\u274C "+e.message)});
    api("/api/me").then(function(m_){if(m_.admin)$("#madmin").hidden=false}).catch(function(){});
    var sp=(TGW.initDataUnsafe&&TGW.initDataUnsafe.start_param)||((location.search.match(/[?&]share=([\w-]+)/)||[])[1]);
    if(sp)api("/api/shared?t="+encodeURIComponent(sp)).then(function(r){lastItems=r.items;loadItems(r.items);$("#mpost").hidden=false;setMsg("\uD83D\uDD17 Shared playlist: "+r.name+(r.by?" ("+r.by+")":"")+" - "+r.items.length+" videos. 'Save playlist' se apni library me rakho")}).catch(function(e){setMsg("\u274C "+e.message)});
  });
  document.addEventListener("visibilitychange",function(){if(document.hidden)pushNow()});
}

/* ---------- init ---------- */
function start(){
  $("#lock").hidden=true;$("#app").hidden=false;
  if(MINI)initMini();
  $("#siteT").textContent=CFG.title;
  if(CFG.owner){
    var ow=$("#ownT");ow.textContent="by "+CFG.owner;ow.hidden=false;if(CFG.tg)ow.href=CFG.tg;
    $("#wm").textContent=CFG.owner;
    var ft=$("#foot");ft.textContent="Credits: ";var fa=el("a","",CFG.owner);fa.href=CFG.tg||"#";fa.target="_blank";fa.rel="noopener noreferrer";ft.appendChild(fa);
  }
  var tg=$("#tgB");if(CFG.tg)tg.href=CFG.tg;else tg.hidden=true;
  var th=LS.get("ytb_theme","dark");document.documentElement.setAttribute("data-theme",th);
  $("#setB").onclick=function(e){e.stopPropagation();gearMenu(this)};
  $("#themeB").onclick=function(){var n=document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark";document.documentElement.setAttribute("data-theme",n);LS.set("ytb_theme",n)};
  var q=$("#q"),qc=$("#qclr"),qt;
  q.oninput=function(){qc.hidden=!q.value;clearTimeout(qt);qt=setTimeout(function(){VIEW.q=q.value;renderGrid()},180)};
  qc.onclick=function(){q.value="";qc.hidden=true;VIEW.q="";renderGrid()};
  $("#sort").onchange=function(){VIEW.sort=this.value;renderGrid()};
  window.addEventListener("hashchange",route);
  route();
}
function unlock(){
  var v=$("#pw").value;
  if(hashPw(v)===CFG.hash){SS.set("ytb_ok",CFG.hash);start()}
  else $("#lerr").textContent="Incorrect password";
}
if(!CFG.hash||SS.get("ytb_ok")===CFG.hash){start()}
else{
  $("#lock").hidden=false;
  if(CFG.owner){var lo=$("#lockOwn"),la=el("a","","by "+CFG.owner);la.href=CFG.tg||"#";la.target="_blank";la.rel="noopener noreferrer";lo.appendChild(la)}
  $("#pwb").onclick=unlock;$("#pw").onkeydown=function(e){if(e.key==="Enter")unlock()};
}
window.__ytb={refresh:refreshItem,applyFresh:applyFresh,parseLinks:parseLinks,srt2vtt:srt2vtt,AB:function(){return AB},spd:function(){return SPD},sleep:function(){return SLEEPEND},pushNow:function(){return pushNow()},amb:function(){return {AM:AM,AMS:AMS,n:amN,bars:amBars}},pb:function(){return PB},fx:function(){return {graph:GRAPH,cors:CORS_LOAD,blocked:CORS_BLOCK}},cur:function(){return {q:CURQ,u:CURURL}},sha256:hashPw,fmtOf:fmtOf,engineOf:engineOf,computeList:computeList,state:function(){return {ALL:ALL,VIEW:VIEW,FAV:FAV}}};
})();
</script>
</body>
</html>
"""


AMBIENT_DEFAULT = {
    "enabled": True, "blur": 25, "edge": 20, "spread": 130, "spreadFadeStart": 20, "spreadFadeCurve": 20,
    "framerateLimit": 60, "energySaver": True, "enableInPictureInPicture": True,
    "detectHorizontalBarSizeEnabled": True, "headerShadowOpacity": 30, "headerShadowSize": 25,
    "surroundingContentFillOpacity": -25, "hideScrollbar": True, "intensity": 90}


def load_ambient_cfg() -> dict:
    """Ambient light settings: ambient.json (ya env AMBIENT_JSON path) se, warna Logic Looper wali default."""
    cfg = dict(AMBIENT_DEFAULT)
    p = os.getenv("AMBIENT_JSON", "ambient.json")
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception as e:
        logger.error(f"ambient json error: {e}")
    return cfg


def _px_fields(it: dict) -> dict:
    d: dict = {}
    if PROXY_BASE and str(it.get("page_url") or "").startswith("http"):
        d["rf"] = rf_url(it["page_url"])                 # expire link ko page se dobara nikalne ke liye
    mode = (get_setting("proxy") or "auto").lower()
    u = it["download_link"]
    if not PROXY_BASE or mode == "off" or (it.get("type") or "VIDEO") in ("IMAGE", "PDF") or not u.startswith("http"):
        return d
    d["x"] = px_url(u, it.get("page_url") or "")
    if mode == "on" or px_needs(u, it.get("iplock") or ""):
        d["xa"] = 1
    return d


def _player_item(it: dict) -> dict:
    return dict({"t": it.get("title") or "Video", "u": it["download_link"], "k": it.get("type") or "VIDEO",
                 "p": it.get("page_url") or "", "th": it.get("thumb") or "", "d": it.get("duration") or 0,
                 "v": [[v["q"], v["u"]] for v in (it.get("variants") or [])],
                 "ip": it.get("iplock") or ""}, **_px_fields(it))


def generate_web_app_html(results: List[dict], title: str = "Scraped Video Web Player", mini: bool = False) -> str:
    """Self-contained YouTube-style web player (password gate, auto thumbnails, favorites, history,
    HLS/MP4/WebM/MKV/DASH/TS/FLV/audio, external-player fallback)."""
    items = [_player_item(it) for it in results]

    def js(o) -> str:
        return (json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
                .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))

    cfg = {"title": title, "owner": BOT_OWNER_NAME, "tg": TELEGRAM_LINK, "amb": load_ambient_cfg(), "px": bool(PROXY_BASE),
           "hash": hashlib.sha256(SKY_PASSWORD.encode("utf-8")).hexdigest() if SKY_PASSWORD else ""}
    if mini:
        cfg.update(mini=True, hash="")      # Mini App: login Telegram se hota hai, password nahi
    return (_PLAYER_TEMPLATE.replace("__OWNER__", _html.escape(BOT_OWNER_NAME))
            .replace("__TITLE__", _html.escape(title))
            .replace("__CFG__", js(cfg)).replace("__DATA__", js(items)))

# ==========================================================
# TELEGRAM BOT HANDLERS
# ==========================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text("⛔ Access Denied! Aap is bot ko use nahi kar sakte.")
        return

    await update.message.reply_text(
        "⚡ 43-Site Dedicated Bulk Link Scraper Bot Active!\n\n"
        "🌐 43 Supported Dedicated Platforms Included!\n\n"
        "📌 Features:\n"
        "1. Full Web Player UI: Custom Video & Media Player interface in HTML.\n"
        "2. 4 Files Export: 2 TXT & 2 HTML Files (Full Web App + Simple List).\n"
        "3. FFmpeg Downloader: Upload .txt file to auto-download & send video.\n4. ZIP pack of all result files + M3U/JSON/CSV exports.\n5. DASH quality + Shaka fallback, hold-to-2x, live stats, share timestamp.\n6. Scrape cache (8 min), cookie age alerts, watch monitor.\n\n"
        "📖 /help — saari commands + usage\n\n🛠️ Commands: /help, /site, /scr, /addsite, /addscr, /delscr, /removesite, /login, /logout, /cookie, /updatecookie, /stop, /stats, /userlist, /debug, /dump, /sniff, /prefer, /sky, /settings, /jobs, /cancel, /watch, /watchlist, /unwatch, /backup, /app, /mini, /restart, /updatewithoutrestart, /cleanup_backups, /adscr, /health"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help -> saari commands + usage (user vs admin)."""
    uid = update.effective_user.id
    if not is_user_allowed(uid):
        await update.message.reply_text("⛔ Access Denied!")
        return
    user_help = (
        "📖 <b>HELP — Commands &amp; Usage</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🚀 SCRAPE / EXTRACT</b>\n"
        "• <code>/scr &lt;url&gt;</code> — listing scrape (default pages 1–10)\n"
        "• <code>/scr &lt;url&gt; 5</code> — sirf page 5\n"
        "• <code>/scr &lt;url&gt; 1-15</code> — pages 1 se 15\n"
        "• <code>/scr &lt;url&gt; all</code> — max pages auto (jab tak naye links aayein)\n"
        "• Seedhe <b>URL paste</b> (command ke bina) — wahi scrape flow\n"
        "• <code>/stop</code> ya <code>/cancel</code> — apna chal raha scrape rok do\n"
        "• <code>/jobs</code> — abhi kaunse jobs chal rahe hain\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🌐 SITES</b>\n"
        "• <code>/site</code> — supported / custom sites list\n"
        "• <code>/addsite example.com</code> — naya domain add\n"
        "• <code>/addsite example.com cookie=...</code> — domain + cookie\n"
        "• <code>/removesite example.com</code> — custom site hatao\n"
        "• <code>/addscr</code> — site-specific link shapes / regex rules\n"
        "• <code>/delscr example.com</code> — rule delete\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🔑 LOGIN / COOKIES</b>\n"
        "• <code>/login domain cookie_string</code> — session cookie save\n"
        "• <code>/logout domain</code> — cookie hatao\n"
        "• <code>/updatecookie domain</code> — cookie update flow\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🎬 PLAYER / FILES</b>\n"
        "• Scrape ke baad: <b>2 TXT + 2 HTML + ZIP</b> (full player + simple)\n"
        "• <code>/sky</code> — agli TXT/M3U se YouTube-style HTML player\n"
        "• <code>/sky My Playlist</code> — title ke sath\n"
        "• <code>/sky off</code> — sky mode band\n"
        "• File caption me <code>/sky</code> likh ke TXT bhejo\n"
        "• <code>/app</code> / <code>/mini</code> — Telegram Mini App player\n• <code>/commands</code> — /help ka alias\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>👁 WATCH (auto new videos)</b>\n"
        "• <code>/watch &lt;listing_url&gt; [minutes]</code> — default 60 min\n"
        "  Example: <code>/watch https://site.com/new 30</code>\n"
        "• <code>/watchlist</code> — apni watches\n"
        "• <code>/unwatch &lt;id&gt;</code> — watch hatao\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>ℹ️ UTILS</b>\n"
        "• <code>/start</code> — bot intro\n"
        "• <code>/help</code> — yeh message\n"
        "• <code>/stats</code> — usage / status\n"
        "• <code>/stop</code> — scrape stop\n"
        "• <code>/cancel</code> — jobs stop (apne)\n\n"
        "📦 Exports: Full TXT, Simple TXT, Full HTML player, Simple HTML, ZIP, "
        "optional M3U/JSON/CSV (<code>/settings export</code> — admin).\n\n"
        f"👑 Owner: {BOT_OWNER_NAME}"
    )
    await update.message.reply_text(user_help, parse_mode="HTML", disable_web_page_preview=True)

    if uid == ADMIN_ID:
        admin_help = (
            "🔐 <b>ADMIN ONLY</b>\n\n"
            "• <code>/adduser &lt;id&gt;</code> / <code>/removeuser &lt;id&gt;</code> / <code>/userlist</code>\n"
            "• <code>/cookie &lt;domain&gt;</code> — id+pass se login, cookies nikaalo\n"
            "• <code>/cookie domain https://.../api/login</code> — API login save\n"
            "• <code>/debug &lt;url&gt;</code> — fetch/status diagnose\n"
            "• <code>/dump &lt;url&gt;</code> — page dump\n"
            "• <code>/sniff &lt;video_page_url&gt;</code> — stream kahan milti hai\n"
            "• <code>/prefer &lt;site&gt; &lt;host_or_url&gt;</code> — asli CDN host priority\n"
            "• <code>/prefer &lt;site&gt; off</code> — prefer clear\n"
            "• <code>/settings</code> — verify, ytdlp, min_quality, include/exclude, export, proxy\n"
            "• <code>/settings verify on</code> | <code>min_quality 720</code> | <code>export m3u,json</code>\n"
            "• <code>/settings reset</code>\n"
            "• <code>/backup</code> — DB backup file\n"
            "• Restore: <code>bot_data.db</code> bhejo, caption me <code>restore</code>\n"
            "• <code>/sbsync</code> / <code>/sbrestore</code> — Supabase storage sync\n"
            "• <code>/health</code> — site success rates + purani cookies\n"
            "• <code>/cancel all</code> — saari jobs stop\n"
            "• <code>/turbo</code> — ultra-fast scrape limits (is session)\n"
            "• <code>/turbo off</code> — normal limits wapas\n"
            "• <code>/restart</code> — bot process restart (host auto-start)\n"
            "• <code>/updatewithoutrestart</code> — cookies/rules/settings live reload\n"
            "• <code>/cleanup_backups</code> — temp files + purani DB rows + caches saaf\n"
            "• <code>/adscr</code> — alias of /addscr\n"
            "• <code>/mini</code> — alias of /app (Mini App)\n"
            "• <code>/commands</code> — alias of /help\n\n"
            "⚡ Speed env (optional): <code>SCR_CONCURRENCY</code>, <code>SCR_PAGE_CONCURRENCY</code>, "
            "<code>SCR_ALL_MAX</code>, <code>PROXY_URL</code>, <code>PROXY_BASE</code>"
        )
        await update.message.reply_text(admin_help, parse_mode="HTML", disable_web_page_preview=True)



async def turbo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/turbo [off] — admin: is process me scrape limits ultra-high."""
    global SCR_CONCURRENCY, SCR_PAGE_CONCURRENCY
    if update.effective_user.id != ADMIN_ID:
        return
    args = [a.lower() for a in (context.args or [])]
    if args and args[0] in ("off", "normal", "0"):
        SCR_CONCURRENCY = int(os.getenv("SCR_CONCURRENCY", "48"))
        SCR_PAGE_CONCURRENCY = int(os.getenv("SCR_PAGE_CONCURRENCY", "20"))
        await update.message.reply_text(
            f"♻️ Turbo OFF — concurrency={SCR_CONCURRENCY}, page={SCR_PAGE_CONCURRENCY}")
        return
    SCR_CONCURRENCY = max(SCR_CONCURRENCY, int(os.getenv("TURBO_CONCURRENCY", "72")))
    SCR_PAGE_CONCURRENCY = max(SCR_PAGE_CONCURRENCY, int(os.getenv("TURBO_PAGE_CONCURRENCY", "28")))
    await update.message.reply_text(
        f"🚀 TURBO ON (is process)\n"
        f"• Video extract parallel: {SCR_CONCURRENCY}\n"
        f"• Listing page parallel: {SCR_PAGE_CONCURRENCY}\n"
        f"• Thread workers: {os.getenv('SCR_WORKERS', '96')}\n\n"
        f"Band: /turbo off\n"
        f"Permanent ke liye env: SCR_CONCURRENCY / SCR_PAGE_CONCURRENCY / SCR_WORKERS")


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/health -> har site ka recent success rate (admin)."""
    if update.effective_user.id != ADMIN_ID:
        return
    rows = health_overview()
    if not rows:
        await update.message.reply_text("📭 Abhi koi scrape data nahi (kuch scrape chalao).")
        return
    ic = lambda r: "🟢" if r >= 0.7 else ("🟡" if r >= 0.3 else "🔴")
    u = px_usage_info()
    await update.message.reply_text("🩺 Site health (last run | avg of 5)\n" + "\n".join(
        f"{ic(x['last'])} {x['domain']}: {x['last']:.0%} | {x['avg']:.0%} ({x['runs']} runs)" for x in rows[:30])
        + f"\n\n🛡 Proxy aaj: {u['total_mb']} MB")


    # cookie freshness
    try:
        oldc = cookie_age_warnings(14)
        if oldc:
            await update.message.reply_text("🍪 Cookies 14+ din purani (re-login socho):\n" + "\n".join(oldc[:15]))
    except Exception:
        pass

async def app_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/app -> Mini App kholne ka button."""
    if not is_user_allowed(update.effective_user.id):
        return
    if not PROXY_BASE or WebAppInfo is None:
        await update.message.reply_text(
            "ℹ️ Mini App ke liye env PROXY_BASE=https://<tumhari-app-url> chahiye (HTTPS), "
            "aur python-telegram-bot v20+.")
        return
    await update.message.reply_text(
        "🎬 Mini Player: scrape, play, synced favorites/history, playlists.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Open Mini App", web_app=WebAppInfo(url=PROXY_BASE + "/app"))]]))


async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        add_user_db(uid)
        await update.message.reply_text(f"✅ User `{uid}` added.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Usage: /adduser <telegram_user_id>
Example: /adduser 123456789")

async def removeuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        if uid == ADMIN_ID:
            await update.message.reply_text("❌ Admin khud ko remove nahi kar sakta.")
            return
        remove_user_db(uid)
        await update.message.reply_text(f"🗑 User `{uid}` removed.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Usage: /removeuser <telegram_user_id>
Example: /removeuser 123456789")

async def userlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_allowed(update.effective_user.id): return
    users = get_all_users()
    msg = "👥 **Authorized Users:**\n\n"
    for uid in users:
        role = "👑 Admin" if uid == ADMIN_ID else "👤 User"
        msg += f"• `{uid}` ({role})\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_allowed(update.effective_user.id): return
    users_count = len(get_all_users())
    jobs_n = len(JOBS)
    sites_n = len(get_all_sites())
    cookies_n = len(list_cookie_domains())
    watches_n = len(watch_rows(None if update.effective_user.id == ADMIN_ID else update.effective_user.id))
    px = "ON" if PROXY_BASE else "OFF"
    conc = f"{SCR_CONCURRENCY}/{SCR_PAGE_CONCURRENCY}"
    await update.message.reply_text(
        f"📊 Bot Status

"
        f"• Users: {users_count}
"
        f"• Sites: {sites_n} | Cookies: {cookies_n}
"
        f"• Active jobs: {jobs_n}
"
        f"• Your watches: {watches_n}
"
        f"• Scrape parallel: {conc} (video/pages)
"
        f"• Proxy/MiniApp: {px}
"
        f"• curl_cffi: {'ON' if cffi_requests else 'OFF'}
"
        f"• Engine: 24/7 Active 🟢

"
        f"📖 /help — saari commands"
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STOP_PROCESS[update.effective_user.id] = True
    await update.message.reply_text("🛑 Process Stop Request Sent!")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/debug <url> -> shows exactly why a site fails (fetch / link discovery / extraction)."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /debug <listing_or_video_url>")
        return
    url = context.args[0]
    reset_host_stats(url)
    _t0 = time.time()
    html = await fetch(url)
    _dt = time.time() - _t0
    if not html:
        await update.message.reply_text(
            f"❌ Fetch failed after {_dt:.1f}s. HTTP: {LAST_STATUS.get(url, '?')}\n"
            f"🔧 Engines: {LAST_DETAIL.get(url, '?')}\n"
            f"🧯 Error: {LAST_ERR.get(url, '?')}\n{err_hint(url)}"
            f"curl_cffi installed: {'YES' if cffi_requests else 'NO'} | "
            f"Proxy set: {'YES' if PROXY_URL else 'NO'}\n\n"
            "403/503 = Cloudflare/IP block | 404 = wrong URL | ? = timeout/DNS")
        return

    _wk = fix_url(url)
    lines = ([f"🔁 Working URL: {_wk}  (tumhara URL fail hua, ye chalta hai - bot ab isi ko use karega)"]
             if _wk != url else []) + [f"✅ Fetched {len(html)} bytes in {_dt:.1f}s "
             f"(engine: {_BEST_ENGINE.get(_root_host(urlparse(url).netloc), '?')} | "
             f"curl_cffi: {'YES' if cffi_requests else 'NO'})",
             link_stats(html, url)]
    if _looks_like_single_video(url):
        if get_cookie_for_url(url):
            a = await _extract_video_link_impl(url, url, True)
            b = await _extract_video_link_impl(url, url, False)
            lines.append(f"🔑 With login   : {a['download_link'] if a else 'FAILED'}")
            lines.append(f"🔓 Without login: {b['download_link'] if b else 'FAILED'}")
            item = a or b
        else:
            item = await extract_video_link(url, source_page=url)
            lines.append(f"▶ Extract (this video page, no login saved): {item['download_link'] if item else 'FAILED'}")
        if not item:
            lines.append(page_diag(html))
        qs = _quick_streams(html, url)
        lines.append(f"🎞 Page me streams ({len(qs)}):")
        lines += [f"   {x[:105]}" for x in qs[:4]]
        await update.message.reply_text("\n".join(lines)[:4000], disable_web_page_preview=True)
        return
    links = find_video_links(html, url)
    lines.append(f"🔗 Video-like links on page: {len(links)}")
    lines += links[:3]
    if links:
        item = await extract_video_link(links[0], source_page=url)
        s = item["download_link"] if item else None
        lines.append(f"▶ Extract test on 1st link: {s or 'FAILED'}")
        if not item:
            lines.append(note_hint())
    else:
        # single video page? test with the real extractor (xhamster HLS picker included)
        item = await extract_video_link(url, source_page=url)
        s = item["download_link"] if item else None
        lines.append(f"▶ Treated as single video page: {s or 'no stream found'}")
    await update.message.reply_text("\n".join(lines)[:4000], disable_web_page_preview=True)

async def dump_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dump <url> -> sends the raw HTML the bot receives, so it can be inspected."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /dump <url>")
        return
    url = context.args[0]
    html = await fetch(url)
    if not html:
        await update.message.reply_text(f"❌ Fetch failed. HTTP: {LAST_STATUS.get(url, '?')}")
        return
    buf = io.BytesIO(html.encode('utf-8', errors='ignore'))
    buf.name = "page_dump.html"
    await update.message.reply_document(document=buf, caption=f"Raw HTML ({len(html)} bytes) of {url}")

async def save_cookie_flow(update: Update, domain: str, raw_cookie: str, merge: bool = False):
    """Saves cookie for a domain, deletes the user's message (it contains secrets) and tests the site."""
    cookie = clean_cookie(raw_cookie)
    try:
        await update.message.delete()
    except Exception:
        pass
    if not domain or '=' not in cookie:
        await update.effective_chat.send_message("❌ Invalid cookie. Format: name=value; name2=value2; ...")
        return
    detail = ""
    old = get_cookie_for_url(f"https://{domain}/") if merge else None
    if merge and old:                      # purani cookie me merge: same naam replace, naye add
        od, nd = parse_cookie_str(old), parse_cookie_str(cookie)
        added = [k for k in nd if k not in od]
        changed = [k for k in nd if k in od and od[k] != nd[k]]
        same = len(nd) - len(added) - len(changed)
        od.update(nd)
        cookie = "; ".join(f"{k}={v}" for k, v in od.items())
        detail = (f"➕ Naye: {', '.join(added[:8]) or '-'}\n"
                  f"✏️ Badle: {', '.join(changed[:8]) or '-'}\n"
                  f"＝ Same: {same}\n")
    elif merge:
        detail = "ℹ️ Pehle is site ki cookie saved nahi thi, nayi save ho gayi.\n"
    set_cookie_db(domain, cookie)
    REDIRECTED.clear()
    n = len([c for c in cookie.split(';') if '=' in c])
    test_url = f"https://{domain}/"
    html = await fetch(test_url)
    if html:
        test = "✅ Site reachable (HTTP 200)"
    else:
        test = (f"⚠️ Site fetch failed: HTTP {LAST_STATUS.get(test_url, '?')} "
                f"({LAST_DETAIL.get(test_url, '')})\n"
                "Cookie save ho gayi, par block/IP ki problem alag hai. /debug se check karo.")
    await update.effective_chat.send_message(
        f"{'🔄 Cookie updated' if merge and old else '🔑 Signed in'}: {domain}\n{detail}🍪 Cookies saved: {n}\n{test}\n"
        f"🧹 Cookie wala message delete kar diya gaya.\n"
        f"💾 Render restart par cookie hat sakti hai: permanent ke liye env me "
        f"SITE_COOKIE_1 = domain|cookie rakho.")

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/login <domain> <cookie string>"""
    if update.effective_user.id != ADMIN_ID:
        return
    parts = (update.message.text or "").split(None, 2)
    if len(parts) < 3:
        await update.message.reply_text(
            "Usage:\n/login <domain> <cookie string>\n\n"
            "Example:\n/login xhamster46.desi cookie_accept_v2=...; UID=...; ...\n\n"
            "Ya /site -> Sign In button dabao.")
        return
    await save_cookie_flow(update, normalize_domain(parts[1]), parts[2])

async def updatecookie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/updatecookie <domain> [cookie]  -> saved cookie me nayi cookie merge (same naam replace, naye add)."""
    if update.effective_user.id != ADMIN_ID:
        return
    parts = (update.message.text or "").split(None, 2)
    if len(parts) < 2:
        saved = list_cookie_domains()
        await update.message.reply_text(
            "🔄 /updatecookie <domain> <cookie>\n"
            "ya sirf /updatecookie <domain>  (phir cookie alag message me bhejo)\n\n"
            "• Purani cookie me merge hoti hai: same naam wali replace, naye add, baaki purani rahengi.\n"
            "• Poori badalni ho to: /login <domain> <cookie>\n"
            "• /site me har signed-in site ke saath 🔄 Update button bhi hai.\n\n"
            f"Signed-in sites: {', '.join(saved) if saved else 'none'}")
        return
    domain = normalize_domain(parts[1])
    if not DOMAIN_RE.match(domain):
        await update.message.reply_text(f"❌ Invalid domain: {parts[1]}")
        return
    if len(parts) >= 3:
        await save_cookie_flow(update, domain, parts[2], merge=True)
        return
    context.user_data['await_cookie'] = domain
    context.user_data['cookie_merge'] = True
    await update.message.reply_text(
        f"🔄 {domain} ki NAYI cookie ab bhejo (name=value; name2=value2; ...).\n"
        "Purani me merge hogi. Cancel karne ke liye: cancel\n"
        "Message save hote hi auto-delete ho jayega.")


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/logout <domain>  or  /logout all"""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        saved = list_cookie_domains()
        await update.message.reply_text(
            "Usage: /logout <domain>  |  /logout all\n"
            f"Signed-in sites: {', '.join(saved) if saved else 'none'}")
        return
    if context.args[0].lower() == "all":
        for d in list_cookie_domains():
            delete_cookie_db(d)
        await update.message.reply_text("🚪 Sab sites se sign out ho gaya.")
        return
    dom = normalize_domain(context.args[0])
    ok = delete_cookie_db(dom)
    await update.message.reply_text(f"🚪 Signed out: {dom}" if ok else f"ℹ️ {dom} par koi saved login nahi tha.")

async def site_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/site -> all supported sites + login status.  /site <domain> -> status + live test."""
    if not is_user_allowed(update.effective_user.id):
        return

    if context.args:
        dom = normalize_domain(context.args[0])
        has = get_cookie_for_url(f"https://{dom}/") is not None
        rule = get_site_rule(dom)
        ext = (f"🧩 ON ({rule.get('note') or 'saved'})" if rule else "none (generic extractor)")
        test_url = f"https://{dom}/"
        html = await fetch(test_url)
        status = "✅ reachable (HTTP 200)" if html else (
            f"❌ HTTP {LAST_STATUS.get(test_url, '?')} ({LAST_DETAIL.get(test_url, '')})")
        await update.message.reply_text(
            f"🌐 {dom}\n🔑 Login: {'SIGNED IN 🟢' if has else 'not signed in ⚪'}\n📡 Test: {status}\n🧩 Extractor: {ext}")
        return

    saved = set(list_cookie_domains())
    rules = set(list_rule_domains())
    lines = ["🌐 Supported Sites (full domains)",
             "🟢 = signed in | ⚪ = no login | 🧩 = site-specific extractor (/addscr)", ""]
    for d in SITES_FULL:
        lines.append(f"{'🟢' if d in saved else '⚪'} {d}{' 🧩' if d in rules else ''}")
    custom_sites = [d for d in get_all_sites() if d not in SITES_FULL]
    if custom_sites:
        lines += ["", "➕ Added by you (/addsite):"]
        for d in custom_sites:
            lines.append(f"{'🟢' if d in saved else '⚪'} {d}{' 🧩' if d in rules else ''}")
    lines += [
        "",
        "🔎 Name-match (kisi bhi mirror/TLD par chalega):",
        ", ".join(SITES_KEYWORD),
        "",
        "ℹ️ Inke alawa koi bhi doosri site bhi try hoti hai (generic extractor).",
        "",
        "🔑 Login optional hai, har site ke liye:",
        "/login <domain> <cookie>   |   /logout <domain>",
        "/site <domain> -> status + live test",
        "",
        "➕ Nayi site jodne ke liye: /addsite <full domain>",
        "🧩 Site ka apna extractor banane ke liye: /addscr <video/listing URL>",
        "⚡ Nayi site ki fast scraping (auto extractor + /site me add): /scr <url> [pages]",
        "➖ Hatane ke liye: /removesite <domain>  |  /delscr <domain>",
    ]
    keyboard = []
    if update.effective_user.id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("🔑 Sign In xhamster46.desi", callback_data="signin:xhamster46.desi")])
        for d in custom_sites:
            if d != "xhamster46.desi":
                keyboard.append([InlineKeyboardButton(f"🔑 Sign In {d}", callback_data=f"signin:{d}"[:64])])
        for d in sorted(saved):
            keyboard.append([InlineKeyboardButton(f"🔄 Update {d}", callback_data=f"cookieupd:{d}"[:64]),
                             InlineKeyboardButton("🚪 Sign Out", callback_data=f"signout:{d}"[:64])])
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        disable_web_page_preview=True)

async def addsite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addsite <full domain or URL> [optional cookie]  -> adds a site to the bot's list and tests it."""
    if update.effective_user.id != ADMIN_ID:
        return
    chat = update.effective_chat
    parts = (update.message.text or "").split(None, 2)

    if len(parts) < 2:
        custom = [d for d in get_all_sites() if d not in SITES_FULL]
        await chat.send_message(
            "Usage:\n"
            "/addsite <full domain>\n"
            "/addsite <full domain> <cookie>   (cookie optional)\n\n"
            "Examples:\n/addsite xhamster46.desi\n/addsite https://example.com/videos\n\n"
            f"➕ Abhi tak jodi gayi sites: {', '.join(custom) if custom else 'none'}")
        return

    domain = normalize_domain(parts[1])
    cookie = clean_cookie(parts[2]) if len(parts) > 2 else ""
    if cookie:  # message contains secrets -> remove it
        try:
            await update.message.delete()
        except Exception:
            pass

    if not DOMAIN_RE.match(domain):
        await chat.send_message(f"❌ Invalid domain: {domain or '(empty)'}\nFull domain do, jaise: example.com")
        return

    already = domain in get_all_sites()
    if not already:
        add_custom_site_db(domain, update.effective_user.id)
    cookie_note = ""
    if cookie and '=' in cookie:
        set_cookie_db(domain, cookie)
        cookie_note = "\n🔑 Cookie saved (signed in)"

    status = await chat.send_message(f"⏳ {domain} test ho raha hai...")
    home = f"https://{domain}/"
    html = await fetch(home)
    if not html:
        report = (f"📡 Fetch: ❌ HTTP {LAST_STATUS.get(home, '?')} ({LAST_DETAIL.get(home, '')})\n"
                  "⚠️ Site list me add ho gayi, par abhi bot ise khol nahi pa raha "
                  "(Cloudflare/IP block ho sakta hai). /debug se check karo.")
    else:
        links = find_video_links(html, home)
        report = f"📡 Fetch: ✅ HTTP 200\n🔗 Video-like links (homepage): {len(links)}"
        if links:
            h = await fetch(links[0], referer=home)
            stream = await generic_extract(h, links[0]) if h else None
            report += f"\n▶ Extract test: {'✅ stream mila' if stream else '⚠️ FAILED (is site ka sample bhejo)'}"
        else:
            report += "\nℹ️ Homepage par links nahi mile; kisi listing/video URL se try karo."

    head = "ℹ️ Pehle se list me thi" if already else "✅ Site added"
    await status.edit_text(
        f"{head}: {domain}{cookie_note}\n{report}\n\n"
        "Ab is site ka listing ya video URL bot ko bhejo. /site se list dekho.")

async def removesite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/removesite <domain> -> removes a site added with /addsite."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        custom = list_custom_sites_db()
        await update.message.reply_text(
            "Usage: /removesite <domain>\n"
            f"Removable sites: {', '.join(custom) if custom else 'none'}")
        return
    domain = normalize_domain(context.args[0])
    if domain in SITES_FULL:
        await update.message.reply_text("⛔ Ye built-in site hai, hata nahi sakte.")
    elif domain in env_extra_sites() and domain not in list_custom_sites_db():
        await update.message.reply_text("ℹ️ Ye EXTRA_SITES env variable se aayi hai; wahan se hatao.")
    elif remove_custom_site_db(domain):
        await update.message.reply_text(
            f"➖ Removed: {domain}\n(Login cookie bhi hatani ho to: /logout {domain})")
    else:
        await update.message.reply_text(f"ℹ️ {domain} list me nahi mili.")

async def addscr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addscr <video or listing URL> [regex]  -> builds a domain-specific extractor and adds the site."""
    if update.effective_user.id != ADMIN_ID:
        return
    chat = update.effective_chat
    parts = (update.message.text or "").split(None, 2)

    if len(parts) < 2:
        rules = list_rule_domains()
        await chat.send_message(
            "🧩 /addscr - site ke hisaab se extractor banata hai\n\n"
            "Auto (recommended):\n"
            "/addscr https://example.com/videos/        (listing URL)\n"
            "/addscr https://example.com/video/abc-123  (video page URL)\n\n"
            "Manual regex (group 1 = stream URL):\n"
            '/addscr https://example.com/video/abc-123 src="(https[^"]+)"\n\n'
            "Hatane ke liye: /delscr <domain>\n\n"
            f"🧩 Abhi extractors: {', '.join(rules) if rules else 'none'}")
        return

    target = parts[1].strip()
    manual = parts[2].strip() if len(parts) > 2 else ""
    domain = normalize_domain(target)
    if not DOMAIN_RE.match(domain):
        await chat.send_message(f"❌ Invalid domain/URL: {target}\nFull domain ya URL do, jaise: https://example.com/videos/")
        return
    url = target if re.match(r'^https?://', target, re.I) else f"https://{domain}/"

    status = await chat.send_message(f"🧩 {domain} analyze ho raha hai (2-3 pages fetch honge)...")

    if domain not in get_all_sites():           # also appears in /site
        add_custom_site_db(domain, update.effective_user.id)

    try:
        res = await (build_manual_rule(url, domain, manual) if manual else build_auto_rule(url, domain))
    except Exception as e:
        logger.error(f"/addscr error for {domain}: {e}")
        await status.edit_text(f"❌ Analyze error: {e}")
        return

    if not res.get("ok"):
        await status.edit_text(
            f"⚠️ {domain} site list me hai, par extractor nahi ban paya.\n\n{res.get('report', '')}")
        return

    save_site_rule(domain, res["regex"], res["shapes"], res.get("strict", True), res.get("note", ""))

    verify = ""
    if res.get("test_url"):
        item = await extract_video_link(res["test_url"], source_page=url)
        verify = ("\n▶ Test: ✅ " + item["download_link"][:110]) if item else "\n▶ Test: ⚠️ extract fail"
    extra = ("\n" + res["report"]) if res.get("report") else ""

    await status.edit_text(
        f"✅ Extractor saved: {domain}\n"
        f"🧩 Mode: {res.get('note') or 'saved'}\n"
        f"🔗 Video URL shapes: {', '.join(res['shapes']) if res['shapes'] else 'auto'}"
        f"{verify}{extra}\n\n"
        "Ab is site ka listing URL bot ko bhejo, scraping isi extractor se hogi.\n"
        f"/site me 🧩 dikhega. Hatane ke liye: /delscr {domain}")

async def delscr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/delscr <domain> -> removes a site-specific extractor."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        rules = list_rule_domains()
        await update.message.reply_text(
            f"Usage: /delscr <domain>\nExtractors: {', '.join(rules) if rules else 'none'}")
        return
    domain = normalize_domain(context.args[0])
    if delete_site_rule(domain):
        await update.message.reply_text(f"🗑 Extractor removed: {domain}\n(Ab built-in/generic extractor use hoga)")
    else:
        await update.message.reply_text(f"ℹ️ {domain} ka koi extractor nahi tha.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id): return

    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith('.txt'):
        await update.message.reply_text("❌ Valid .txt file upload karein.")
        return

    if sky_armed(context) or (update.message.caption or "").strip().lower().startswith("/sky"):
        await sky_from_file(update, context, doc)        # /sky mode: HTML player banao, download nahi
        return

    STOP_PROCESS[user_id] = False
    status_msg = await update.message.reply_text("📥 TXT file reading started...")

    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = io.BytesIO()
        await file.download_to_memory(file_bytes)
        file_content = file_bytes.getvalue().decode('utf-8', errors='ignore')

        raw_lines = file_content.splitlines()
        urls = [m.group(1) for line in raw_lines if (m := re.search(r'(https?://[^\s]+)', line))]

        if not urls:
            await status_msg.edit_text("❌ TXT file me koi valid URL nahi mila.")
            return

        total = len(urls)
        await status_msg.edit_text(f"🚀 Total {total} links queued! Stop karne ke liye /stop bhejein.")

        for idx, raw_url in enumerate(urls, 1):
            if STOP_PROCESS.get(user_id, False):
                await update.message.reply_text("🛑 Task Stopped By User!")
                break

            progress_msg = await update.message.reply_text(f"⏳ [{idx}/{total}] Processing...")
            stream_url = raw_url
            video_title = f"Video #{idx}"

            if not (raw_url.endswith('.m3u8') or raw_url.endswith('.mp4')):
                extracted = await extract_video_link(raw_url)
                if extracted and extracted.get('download_link'):
                    stream_url = extracted['download_link']
                    video_title = extracted.get('title', video_title)

            output_file = f"temp_video_{user_id}_{idx}.mp4"
            if os.path.exists(output_file):
                try: os.remove(output_file)
                except Exception: pass

            await progress_msg.edit_text(f"📥 [{idx}/{total}] Downloading Video...")
            success = await download_video_ffmpeg(stream_url, output_file)

            if success:
                await progress_msg.edit_text(f"📤 [{idx}/{total}] Telegram Uploading...")
                try:
                    with open(output_file, 'rb') as vf:
                        await update.message.reply_video(
                            video=vf,
                            caption=f"🎥 {video_title}\n\n🔗 Item {idx}/{total}",
                            supports_streaming=True
                        )
                    await progress_msg.delete()
                except Exception as upload_err:
                    await progress_msg.edit_text(f"❌ Upload Error: {str(upload_err)}")
            else:
                await progress_msg.edit_text(f"❌ [{idx}/{total}] Download Failed!")

            if os.path.exists(output_file):
                try: os.remove(output_file)
                except Exception: pass

        await status_msg.edit_text("✅ Processing completed!")

    except Exception as e:
        logger.error(f"Error processing document: {e}")
        await status_msg.edit_text(f"❌ File Process Error: {str(e)}")

async def _run_scrape_chunk_impl(update_or_query, context, target_url: str, start_page: int, end_page: int):
    status_msg = await update_or_query.message.reply_text(f"⚡ Scraping Pages {start_page} to {end_page}...")

    try:
        last = {"t": 0.0}

        async def progress(done, total):
            if time.time() - last["t"] < 3:
                return
            last["t"] = time.time()
            await status_msg.edit_text(f"⚡ Extracting {done}/{total} (Pages {start_page}-{end_page})...")

        results = await scrape_multi_pages_chunk(target_url, start_page=start_page,
                                                 end_page=end_page, progress=progress,
                                                 user_id=_uid_of(update_or_query))

        if not results:
            r = LAST_REPORT
            await status_msg.edit_text(
                f"❌ Pages {start_page} to {end_page} par koi video links nahi mile.\n\n"
                f"📄 Pages OK: {r['pages_ok']}\n"
                f"🚫 Failed: {r['pages_fail'][:3]}\n"
                f"🔗 Links found: {r['links']}\n"
                f"✅ Extracted: {r['extracted']}\n"
                f"🧹 Junk/duplicate hataye: {r.get('dropped', 0)}\n{drop_hint()}"
                f"{block_hint(target_url)}{note_hint()}{login_hint(target_url)}\n"
                f"Detail ke liye: /debug {target_url}\nRaw HTML ke liye: /dump {target_url}"
            )
            return

        await status_msg.edit_text(f"✅ Total {len(results)} Videos Extracted!{iplock_hint(results)}\n2 TXT aur 2 HTML files generate ho rahi hain...")

        # FILE 1: FULL DETAILS TXT
        txt_full_content = f"--- Scraped Video Links Full (Pages {start_page}-{end_page} | {len(results)} Items) ---\n\n"
        for idx, item in enumerate(results, 1):
            txt_full_content += f"{idx}. Title: {item['title']}\n"
            txt_full_content += f"   Source Listing Page: {item['source_page']}\n"
            txt_full_content += f"   Permanent Video Page: {item['page_url']}\n"
            txt_full_content += f"   Direct Stream Link: {item['download_link']}\n" + exp_line(item) + "\n"

        txt_full_bytes = io.BytesIO(txt_full_content.encode('utf-8'))
        txt_full_bytes.name = f"scraped_p{start_page}_to_p{end_page}_full.txt"

        # FILE 2: SIMPLE TXT
        txt_simple_content = f"--- Simple Video Stream Links (Pages {start_page}-{end_page} | {len(results)} Items) ---\n\n"
        for idx, item in enumerate(results, 1):
            txt_simple_content += f"{item['title']}: {item['download_link']}\n"

        txt_simple_bytes = io.BytesIO(txt_simple_content.encode('utf-8'))
        txt_simple_bytes.name = f"scraped_p{start_page}_to_p{end_page}_simple.txt"

        # FILE 3: FULL WEB APP HTML
        html_web_app = generate_web_app_html(results, title=f"Media Player ({start_page}-{end_page})")
        html_full_bytes = io.BytesIO(html_web_app.encode('utf-8'))
        html_full_bytes.name = f"scraped_p{start_page}_to_p{end_page}_full.html"

        # FILE 4: SIMPLE LIST HTML
        html_simple_content = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Scraped Stream Links Simple ({start_page}-{end_page})</title>
<style>
body {{ font-family: 'Segoe UI', sans-serif; background: #121212; color: #e0e0e0; margin: 20px; }}
.card {{ background: #1e1e1e; padding: 15px; margin-bottom: 12px; border-radius: 8px; border-left: 5px solid #00cc66; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }}
a {{ color: #4da6ff; word-break: break-all; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.tag {{ display: inline-block; background: #00cc66; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; margin-left: 8px; }}
</style></head><body><h2>Scraped Direct Stream Links ({start_page}-{end_page}) - Simple Version</h2>"""

        for idx, item in enumerate(results, 1):
            html_simple_content += f"""<div class="card">
<h3>{idx}. {_html.escape(item['title'])} <span class="tag">{item['type']}</span></h3>
<p><strong>⚡ Stream URL:</strong> <a href="{_html.escape(item['download_link'])}" target="_blank">{_html.escape(item['download_link'])}</a></p>
</div>"""
        html_simple_content += "</body></html>"

        html_simple_bytes = io.BytesIO(html_simple_content.encode('utf-8'))
        html_simple_bytes.name = f"scraped_p{start_page}_to_p{end_page}_simple.html"

        context.user_data['last_url'] = target_url
        context.user_data['next_start'] = end_page + 1

        next_start = end_page + 1
        next_end = next_start + 9

        keyboard = [
            [InlineKeyboardButton(f"▶️ Continue (Pages {next_start}-{next_end})", callback_data="continue_scrape")],
            [InlineKeyboardButton("🛑 Stop Scraping", callback_data="stop_scrape")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update_or_query.message.reply_document(document=txt_full_bytes, caption=f"📁 Pages {start_page}-{end_page} Full TXT File ({len(results)} Links)")
        await update_or_query.message.reply_document(document=txt_simple_bytes, caption=f"📁 Pages {start_page}-{end_page} Simple TXT File (Title: Direct Stream Link)")
        await update_or_query.message.reply_document(document=html_full_bytes, caption=f"🌐 Pages {start_page}-{end_page} Full Web App HTML File (Interactive Player UI)")
        await _send_exports(update_or_query.message.reply_document, results, f"p{start_page}_to_p{end_page}")
        await update_or_query.message.reply_document(
            document=html_simple_bytes,
            caption=f"🌐 Pages {start_page}-{end_page} Simple HTML File\n\nAage ke pages ({next_start} to {next_end}) scrape karne ke liye button click karein:",
            reply_markup=reply_markup
        )
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Error in run_scrape_chunk: {e}")
        await status_msg.edit_text(f"❌ Scraping error: {str(e)}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text("⛔ Access Denied!")
        return

    text = update.message.text.strip()

    pending_domain = context.user_data.get('await_cookie')
    if pending_domain and user_id == ADMIN_ID:
        context.user_data.pop('await_cookie', None)
        merge = context.user_data.pop('cookie_merge', False)
        if text.lower() == "cancel":
            await update.message.reply_text("❎ Cancel ho gaya.")
            return
        await save_cookie_flow(update, pending_domain, text, merge=merge)
        return

    cf = context.user_data.get('cookie_flow')
    if cf and user_id == ADMIN_ID:
        await cookie_flow_step(update, context, cf, text)
        return

    url_match = re.search(r'(https?://[^\s]+)', text)

    if not url_match:
        await update.message.reply_text("❌ Valid URL bhejein!")
        return

    target_url = url_match.group(1)
    if re.search(r'\ball\b', text.replace(target_url, " "), re.I):       # "<url> all" -> saare pages
        context.user_data['scr_all'] = True
        await scr_run(update.effective_chat, context, user_id, target_url, 1, SCR_ALL_MAX)
        return
    await scr_run(update.effective_chat, context, user_id, target_url, 1, 10)

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_user_allowed(query.from_user.id): return

    if query.data.startswith(("signin:", "signout:", "cookieupd:")):
        if query.from_user.id != ADMIN_ID:
            return
        action, dom = query.data.split(":", 1)
        if action == "cookieupd":
            context.user_data['await_cookie'] = dom
            context.user_data['cookie_merge'] = True
            await query.message.reply_text(
                f"🔄 {dom} ki NAYI cookie bhejo (name=value; name2=value2; ...).\n"
                "Purani cookie me merge hogi: same naam wali replace, naye add.\n"
                "Cancel karne ke liye: cancel\n"
                "Message save hote hi auto-delete ho jayega.")
        elif action == "signin":
            context.user_data.pop('cookie_merge', None)
            context.user_data['await_cookie'] = dom
            await query.message.reply_text(
                f"🔑 {dom} ke liye cookie string ab bhejo (name=value; name2=value2; ...).\n"
                "Cancel karne ke liye: cancel\n"
                "Cookie wala message save hote hi auto-delete ho jayega.")
        else:
            ok = delete_cookie_db(dom)
            await query.message.reply_text(f"🚪 Signed out: {dom}" if ok else f"ℹ️ {dom} par login nahi tha.")
        return

    if query.data == "stop_scrape":
        STOP_PROCESS[query.from_user.id] = True
        try:
            await query.edit_message_caption(caption=(query.message.caption or "") + "\n\n🛑 Scraping Stopped By User.")
        except Exception:
            pass
        return

    if query.data == "continue_scrape":
        target_url = context.user_data.get('last_url')
        start_page = context.user_data.get('next_start', 11)
        end_page = start_page + 9

        if not target_url:
            await query.message.reply_text("❌ Target URL lost. Target URL dobara bhejein.")
            return

        await run_scrape_chunk(query, context, target_url, start_page=start_page, end_page=end_page)

# ==========================================================
# /scr  ->  NEW-SITE FAST SCRAPER (auto domain-specific extractor)
# ==========================================================
SCR_CONCURRENCY = int(os.getenv("SCR_CONCURRENCY", "48"))        # parallel video-page extractions (ultra-fast)
SCR_PAGE_CONCURRENCY = int(os.getenv("SCR_PAGE_CONCURRENCY", "20"))   # parallel listing-page fetches (ultra-fast)
SCR_MAX_PAGES = 30          # max pages per single run
SCR_ALL_MAX = int(os.getenv("SCR_ALL_MAX", "300"))   # /scr <url> all : itne pages tak (jaise hi naye links band, ruk jata hai)
_SCR_CACHE_TTL = 300        # seconds, html cache
_FETCH_CACHE: Dict[str, tuple] = {}
_STREAM_CACHE: Dict[str, dict] = {}   # video page url -> extracted result (instant re-runs)
_EXECUTOR_READY = False

# ---- age-gate / consent-page bypass (many sites show "I am 18+" first) ----
_SCR_EXTRA_COOKIES: Dict[str, str] = {}   # root host -> extra cookie string
_AGE_COOKIES = ("age_verified=1; ageverified=1; age_gate=1; age_confirmed=1; over18=1; is_adult=1; "
                "adult=1; agree=1; agreed=1; kt_agecheck=1; disclaimer=1; accepted=1; "
                "age_check=1; av=1; verified=1")
_GATE_RX = re.compile(
    r'(18\s*\+|over\s*18|18\s*years|age\s*(?:verif|check|confirm|gate)|adults?\s*only|'
    r'are\s+you\s+(?:over\s+)?18|i\s*am\s*(?:over\s*)?18|i\s*agree|enter\s*site|disclaimer)', re.I)
_ENTER_RX = re.compile(
    r'(?:\b(?:enter|agree|accept|continue|yes|confirm|proceed)\b|i\s*am|over\s*18|18\s*\+)', re.I)

_orig_make_headers = make_headers      # original is kept; this wraps it (adds age cookies if needed)


def make_headers(url: str, referer: Optional[str] = None) -> dict:
    h = _orig_make_headers(url, referer)
    extra = _SCR_EXTRA_COOKIES.get(_root_host(urlparse(url).netloc))
    if extra:
        h["Cookie"] = (h["Cookie"] + "; " + extra) if h.get("Cookie") else extra
    return h


def _ensure_fast_executor():
    """Bigger thread pool so asyncio.to_thread(fetch_sync) really runs in parallel."""
    global _EXECUTOR_READY
    if not _EXECUTOR_READY:
        asyncio.get_running_loop().set_default_executor(_TPE(max_workers=int(os.getenv("SCR_WORKERS", "96"))))
        _EXECUTOR_READY = True


async def fast_fetch(url: str, referer: Optional[str] = None) -> Optional[str]:
    """fetch() + short in-memory cache (same page is never downloaded twice)."""
    hit = _FETCH_CACHE.get(url)
    if hit and time.time() - hit[0] < _SCR_CACHE_TTL:
        return hit[1]
    page = await fetch(url, referer)
    if page:
        if len(_FETCH_CACHE) > 400:
            for k_ in sorted(_FETCH_CACHE, key=lambda x: _FETCH_CACHE[x][0])[:120]:
                _FETCH_CACHE.pop(k_, None)
        _FETCH_CACHE[url] = (time.time(), page)
    return page


def _is_builtin_domain(domain: str) -> bool:
    """True if main.py already has a dedicated extractor for this domain."""
    if domain in SITES_FULL:
        return True
    for k in SITES_KEYWORD:
        for name in re.split(r'\s*/\s*', k):
            if name and name.lower() in domain:
                return True
    return False


def scr_find_links(html: str, page_url: str) -> List[str]:
    """find_video_links() + extra discovery (data-href, onclick, JSON urls) when the page has few <a> links."""
    links = find_video_links(html, page_url)
    if len(links) >= 4:
        return links
    extra = re.findall(r'data-(?:href|url|link|video-url|video-link|permalink)\s*=\s*["\']([^"\']+)["\']', html, re.I)
    extra += re.findall(r'(?:location(?:\.href)?\s*=|window\.open\()\s*["\']([^"\']+)["\']', html, re.I)
    extra += re.findall(r'"(?:url|link|permalink|video_url|href)"\s*:\s*"([^"]+)"', html, re.I)
    if not extra:
        return links
    synth = "".join(f'<a href="{u}"><img src="x"></a>' for u in dict.fromkeys(extra))
    more = find_video_links(html + synth, page_url)
    return more if len(more) > len(links) else links


async def scr_unlock_gate(url: str, html: str):
    """Tries to pass an age-gate / consent page. Returns (html, base_url) of the real listing or None."""
    root = _root_host(urlparse(url).netloc)
    if len(html) > 30000 and not _GATE_RX.search(html):
        return None                                   # big normal page, not a gate
    _SCR_EXTRA_COOKIES[root] = _AGE_COOKIES
    _FETCH_CACHE.pop(url, None)
    h = await fetch(url)                              # same URL again, now with age cookies
    if h and len(scr_find_links(h, url)) >= 4:
        return h, url

    cands: List[str] = []
    for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href, inner = m.group(1), re.sub(r'<[^>]+>', ' ', m.group(2))
        if not (_ENTER_RX.search(inner) or _ENTER_RX.search(href)):
            continue
        full = urljoin(url, href.replace('&amp;', '&')).split('#')[0]
        if (full.startswith(('http://', 'https://'))
                and _root_host(urlparse(full).netloc) == root
                and full.rstrip('/') != url.rstrip('/') and full not in cands):
            cands.append(full)
    for c in cands[:3]:                               # follow "Enter / I am 18+" links
        h2 = await fetch(c, referer=url)
        if h2 and len(scr_find_links(h2, c)) >= 4:
            return h2, c
    _SCR_EXTRA_COOKIES.pop(root, None)
    return None


async def scr_preflight(url: str) -> str:
    """Checks the listing page once; if it looks like a gate, unlocks it and returns the real URL."""
    if _looks_like_single_video(url):
        return url
    first = await fast_fetch(url)
    if not first or len(scr_find_links(first, url)) >= 4:
        return url
    got = await scr_unlock_gate(url, first)
    if got:
        html2, base = got
        _FETCH_CACHE[base] = (time.time(), html2)
        return base
    return url


def _scr_title(html: str) -> str:
    m = (re.search(r'<h1[^>]*>(.*?)</h1>', html, re.I | re.S)
         or re.search(r'<title>(.*?)</title>', html, re.I | re.S))
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', m.group(1))).strip() if m else "Video"


async def _scr_single_fallback(html: Optional[str], page_url: str) -> Optional[dict]:
    """The given URL may itself be a page with a player (no listing)."""
    if not html:
        return None
    s = await generic_extract(html, page_url)
    if not s:
        return None
    final = process_tpl_link(s) if ".m3u8" in s else s
    return {"title": _scr_title(html), "type": "VIDEO", "page_url": page_url,
            "source_page": page_url, "download_link": final}


def _scr_diag(html: str, page_url: str, links: List[str]) -> str:
    gate = "HAAN" if _GATE_RX.search(html) else "nahi"
    out = [link_stats(html, page_url), f"🚧 Age-gate/consent shak: {gate}",
           f"🔗 Is page par mile links ({len(links)}):"]
    out += [l[:100] for l in links[:3]]
    return "\n".join(out)


async def scr_scrape(url: str, start: int, end: int, user_id: int, progress=None, auto_end: bool = False):
    """Fast scraper: parallel pages + parallel extraction + cache + retry + self-heal."""
    _ensure_fast_executor()
    reset_host_stats(url)
    domain = normalize_domain(url)
    rep = {"pages_ok": 0, "pages_fail": [], "links": 0, "extracted": 0,
           "cached": 0, "healed": False, "diag": ""}
    first_html: Dict[str, str] = {}

    if _looks_like_single_video(url):
        r = await extract_video_link(url, source_page=url)
        if r:
            rep.update(links=1, extracted=1)
            return [r], rep

    page_urls = [fix_url(x) for x in await build_page_urls(url, start, end)]
    url_to_source: Dict[str, str] = {}
    psem = asyncio.Semaphore(SCR_PAGE_CONCURRENCY)

    async def crawl(pu: str):
        if STOP_PROCESS.get(user_id):
            return
        async with psem:
            page = await fast_fetch(pu)
        if not page:
            rep["pages_fail"].append(f"{pu} (HTTP {LAST_STATUS.get(pu, '?')})")
            return
        rep["pages_ok"] += 1
        links = await asyncio.to_thread(scr_find_links, page, pu)
        if pu == page_urls[0]:
            first_html["h"] = page
            if len(links) < 4:
                rep["diag"] = _scr_diag(page, pu, links)
        for l in links:
            url_to_source.setdefault(l, pu)

    if auto_end:        # "all" mode: pages ki waves, jis wave me naye links na aayein wahin ruk jao
        wave = max(4, SCR_PAGE_CONCURRENCY)
        for i in range(0, len(page_urls), wave):
            if STOP_PROCESS.get(user_id):
                break
            before, fails0 = len(url_to_source), len(rep["pages_fail"])
            await asyncio.gather(*[crawl(p) for p in page_urls[i:i + wave]])
            if len(url_to_source) == before:                      # koi naya link nahi
                break
            if len(rep["pages_fail"]) - fails0 >= max(2, wave // 2):   # aadhe se zyada pages 404/fail = last page paar
                break
    else:
        await asyncio.gather(*[crawl(p) for p in page_urls])
    rep["links"] = len(url_to_source)

    results: Dict[str, dict] = {}
    if url_to_source:
        esem = AdaptiveLimiter(SCR_CONCURRENCY)
        total = len(url_to_source)
        state = {"done": 0, "last": 0.0}

        async def work(v: str, s: str):
            if STOP_PROCESS.get(user_id):
                return
            hit = _STREAM_CACHE.get(v)
            if hit and hit.get("expires") and hit["expires"] < time.time() + 300:
                hit = None                                   # link expire hone wala hai -> dobara nikalo
            if hit:
                results[v] = hit
                rep["cached"] += 1
            else:
                async with esem:
                    r = await guarded_extract(v, s)
                    if (not r and not STOP_PROCESS.get(user_id)
                            and not host_is_blocked(_root_host(urlparse(v).netloc))):
                        await asyncio.sleep(1)               # one quick retry
                        r = await guarded_extract(v, s)
                    esem.feedback(v, bool(r))
                if r:
                    if len(_STREAM_CACHE) > 5000:
                        _STREAM_CACHE.clear()
                    results[v] = r
                    _STREAM_CACHE[v] = r
            state["done"] += 1
            if progress and time.time() - state["last"] > 2.5:
                state["last"] = time.time()
                try:
                    await progress(state["done"], total)
                except Exception:
                    pass

        await asyncio.gather(*[work(v, s) for v, s in url_to_source.items()])

        # ---- SELF-HEAL: saved rule stopped working (site changed) -> relearn once ----
        failed = [v for v in url_to_source if v not in results]
        if (get_site_rule(domain) and total >= 6 and len(results) < 0.3 * total
                and not STOP_PROCESS.get(user_id)):
            try:
                res = await build_auto_rule(url, domain)
                if res.get("ok"):
                    save_site_rule(domain, res["regex"], res["shapes"],
                                   res.get("strict", True), (res.get("note") or "") + " [auto-healed]")
                    rep["healed"] = True
                    await asyncio.gather(*[work(v, url_to_source[v]) for v in failed])
            except Exception as e:
                logger.error(f"self-heal error {domain}: {e}")

    ordered = [results[v] for v in url_to_source if v in results]

    # ---- JS-rendered (SPA) site: embedded JSON / hidden API / sitemap / optional browser ----
    if not ordered and first_html.get("h") and not STOP_PROCESS.get(user_id):
        try:
            ordered, spa_diag = await scr_spa_fallback(url, first_html["h"], page_urls[0], start, end, user_id)
        except Exception as e:
            logger.error(f"spa fallback error {domain}: {e}")
            ordered, spa_diag = [], f"SPA error: {e}"
        rep["diag"] = (rep["diag"] + "\n" + spa_diag).strip()
        rep["links"] = max(rep["links"], len(ordered))

    # ---- FALLBACK: the page itself holds a player (no listing) ----
    if not ordered and first_html.get("h") and not STOP_PROCESS.get(user_id):
        single = await _scr_single_fallback(first_html["h"], page_urls[0])
        if single:
            ordered = [single]
            rep["links"] = max(rep["links"], 1)

    ordered, dropped = clean_results(ordered)
    ordered = await verify_results(ordered, rep)
    rep["dropped"] = dropped
    rep["extracted"] = len(ordered)
    try:
        record_health(url, rep["links"], len(ordered))
    except Exception:
        pass
    return ordered, rep


async def scr_send_files(chat, results: List[dict], start: int, end: int, domain: str, url: str, show_next: bool = True):
    tag = f"{domain}_p{start}_to_p{end}"
    full = f"--- {domain} | Pages {start}-{end} | {len(results)} Items ---\n\n"
    simple = f"--- {domain} Simple Links (Pages {start}-{end} | {len(results)} Items) ---\n\n"
    for i, it in enumerate(results, 1):
        full += (f"{i}. Title: {it['title']}\n   Source Listing Page: {it['source_page']}\n"
                 f"   Permanent Video Page: {it['page_url']}\n   Direct Stream Link: {it['download_link']}\n" + exp_line(it) + "\n")
        simple += f"{it['title']}: {it['download_link']}\n"

    def mk(text: str, name: str):
        b = io.BytesIO(text.encode('utf-8'))
        b.name = name
        return b

    cards = ""
    for i, it in enumerate(results, 1):
        cards += (f'<div class="card"><h3>{i}. {_html.escape(it["title"])} '
                  f'<span class="tag">{it["type"]}</span></h3>'
                  f'<p><a href="{_html.escape(it["download_link"])}" target="_blank">'
                  f'{_html.escape(it["download_link"])}</a></p></div>')
    simple_html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>' + _html.escape(domain) + '</title><style>'
        'body{font-family:Segoe UI,sans-serif;background:#121212;color:#e0e0e0;margin:20px}'
        '.card{background:#1e1e1e;padding:15px;margin-bottom:12px;border-radius:8px;border-left:5px solid #00cc66}'
        'a{color:#4da6ff;word-break:break-all;text-decoration:none}'
        '.tag{background:#00cc66;color:#fff;padding:2px 8px;border-radius:4px;font-size:12px}'
        '</style></head><body><h2>' + _html.escape(domain) + f' ({start}-{end})</h2>' + cards + '</body></html>')

    nxt = end + 1
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"▶️ Continue (Pages {nxt}-{nxt + 9})", callback_data="scr_continue")],
        [InlineKeyboardButton("🛑 Stop", callback_data="scr_stop")]])

        _html_full = generate_web_app_html(results, title=f"{domain} ({start}-{end})")
    _zip_files = [
        (f"{tag}_full.txt", full),
        (f"{tag}_simple.txt", simple),
        (f"{tag}_full.html", _html_full),
        (f"{tag}_simple.html", simple_html),
    ]
    await chat.send_document(document=mk(full, f"{tag}_full.txt"), caption=f"📁 Full TXT ({len(results)} links)")
    await chat.send_document(document=mk(simple, f"{tag}_simple.txt"), caption="📁 Simple TXT (Title: Stream Link)")
    await chat.send_document(
        document=mk(_html_full, f"{tag}_full.html"),
        caption="🌐 Full Web App HTML (Player UI)")
    await _send_exports(chat.send_document, results, tag)
    await chat.send_document(
        document=mk(simple_html, f"{tag}_simple.html"),
        caption=(f"🌐 Simple HTML\n\nAage ke pages ({nxt}-{nxt + 9}) ke liye button dabao:" if show_next
                 else "🌐 Simple HTML (saare pages ho gaye)"),
        reply_markup=kb if show_next else None)
    try:
        zbuf = pack_result_zip(tag, _zip_files)
        if zbuf:
            await chat.send_document(document=zbuf, caption=f"📦 ZIP: saari files ek me ({len(results)} links)")
    except Exception as _ze:
        logger.warning(f"zip pack fail: {_ze}")



async def _scr_run_impl(chat, context, user_id: int, url: str, start: int, end: int):
    domain = normalize_domain(url)
    STOP_PROCESS[user_id] = False
    status = await chat.send_message(f"⚡ {domain} | Pages {start}-{end} ...")

    # ---- 0) age-gate / consent page? unlock it first (so rule-learning sees the real listing) ----
    gate_note = ""
    try:
        new_url = await scr_preflight(url)
        if new_url != url or _root_host(urlparse(url).netloc) in _SCR_EXTRA_COOKIES:
            gate_note = "🔓 Age-gate/consent page bypass hua\n"
        url = new_url
    except Exception as e:
        logger.error(f"/scr preflight error {domain}: {e}")

    # ---- 1) NEW SITE? -> add to /site list + auto-build domain-specific extractor ----
    learn = gate_note
    if not get_site_rule(domain) and not _is_builtin_domain(domain) and domain not in _RULE_FAILED:
        if domain not in get_all_sites():
            add_custom_site_db(domain, user_id)          # now visible in /site
        await status.edit_text(f"{gate_note}🧩 Nayi site: {domain}\nAuto extractor ban raha hai (2-3 pages analyze)...")
        try:
            res = await build_auto_rule(url, domain)
        except Exception as e:
            res = {"ok": False, "report": str(e)}
        if res.get("ok"):
            save_site_rule(domain, res["regex"], res["shapes"], res.get("strict", True), res.get("note", ""))
            learn += f"🧩 Extractor saved ({res.get('note') or 'auto'}) | /site me add ho gayi\n"
        else:
            _RULE_FAILED.add(domain)
            why = (res.get("report") or "").splitlines()
            learn += ("⚠️ Auto extractor nahi bana, generic extractor use ho raha hai "
                      "(site /site me add hai)\n" + (f"ℹ️ {why[0][:150]}\n" if why else ""))
        await status.edit_text(f"{learn}⚡ Scraping Pages {start}-{end} ...")

    async def progress(done, total):
        await status.edit_text(f"{learn}⚡ Extracting {done}/{total} ...")

    try:
        all_mode = context.user_data.pop('scr_all', False)
        _cached = scrape_cache_get(url, start, end)
        if _cached and not all_mode:
            results, rep = _cached
            try:
                await status.edit_text(f"⚡ Cache hit (8 min): {len(results)} links — files bhej raha hoon...")
            except Exception:
                pass
        else:
            results, rep = await scr_scrape(url, start, end, user_id, progress, auto_end=all_mode)
            scrape_cache_set(url, start, end, results, rep)
    except Exception as e:
        logger.error(f"/scr error: {e}")
        await status.edit_text(f"❌ Scraping error: {e}")
        return

    if not results:
        msg = (f"{learn}❌ Koi video link nahi mila.\n"
               f"📄 Pages OK: {rep['pages_ok']} | 🚫 Failed: {rep['pages_fail'][:3]}\n"
               f"🔗 Links: {rep['links']} | ✅ Extracted: {rep['extracted']}\n")
        if rep.get("diag"):
            msg += f"\n🔍 Diagnosis:\n{rep['diag']}\n"
        msg += (f"🧹 Junk/duplicate hataye: {rep.get('dropped', 0)}\n{drop_hint()}"
                + block_hint(url) + note_hint() + login_hint(url))
        msg += ("\n💡 Ye site JS se load hoti lagti hai. Browser DevTools -> Network -> XHR/Fetch me jo "
                "videos-list API URL dikhe wo bhejo, ya /dump " + url + " ki HTML file bhejo.")
        await status.edit_text(msg[:4000], disable_web_page_preview=True)
        return

    heal = " | 🩹 extractor auto-healed" if rep["healed"] else ""
    await status.edit_text(
        f"{learn}✅ {len(results)}/{rep['links']} extracted (cache: {rep['cached']}){heal}{iplock_hint(results)}\nFiles bhej raha hoon...")
    context.user_data['scr_url'] = url
    context.user_data['scr_next'] = end + 1
    shown_end = max(start, start + rep['pages_ok'] - 1) if all_mode else end
    await scr_send_files(chat, results, start, shown_end, domain, url, show_next=not all_mode)
    try:
        await status.delete()
    except Exception:
        pass


async def scr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/scr <url> [pages]   e.g.  /scr https://newsite.com/videos/   |   /scr <url> 5   |   /scr <url> 3-8"""
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        return
    chat = update.effective_chat
    args = context.args or []
    if not args:
        await chat.send_message(
            "⚡ /scr - nayi site ki fast scraping\n\n"
            "/scr <url>          -> pages 1-10\n"
            "/scr <url> 5        -> pages 1-5\n"
            "/scr <url> 3-8      -> pages 3-8\n"
            "/scr <url> all      -> SAARE pages (jab tak naye videos aate hain), login/without login dono\n\n"
            "• Site main.py me nahi hai to auto domain-specific extractor banta hai\n"
            "• Site apne aap /site list me add ho jaati hai\n"
            "• Age-gate (18+) page auto bypass, parallel fast scraping, cache, auto-retry, auto-heal\n"
            f"• Max {SCR_MAX_PAGES} pages ek baar me\n\n"
            f"🧩 Saved extractors: {', '.join(list_rule_domains()) or 'none'}")
        return

    m = re.search(r'https?://[^\s]+', " ".join(args))
    target = m.group(0) if m else args[0]
    domain = normalize_domain(target)
    if not DOMAIN_RE.match(domain):
        await chat.send_message(f"❌ Invalid URL/domain: {target}")
        return
    url = target if re.match(r'^https?://', target, re.I) else f"https://{domain}/"

    start, end = 1, 10
    for a in args:
        rm = re.fullmatch(r'(\d+)(?:-(\d+))?', a)
        if rm:
            if rm.group(2):
                start, end = int(rm.group(1)), int(rm.group(2))
            else:
                start, end = 1, int(rm.group(1))
            break
    start = max(1, start)
    end = max(start, min(end, start + SCR_MAX_PAGES - 1))
    if any(a.lower() == "all" for a in args):
        start, end = 1, SCR_ALL_MAX
        context.user_data['scr_all'] = True

    await scr_run(chat, context, user_id, url, start, end)


async def scr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_user_allowed(q.from_user.id):
        return
    if q.data == "scr_stop":
        STOP_PROCESS[q.from_user.id] = True
        try:
            await q.edit_message_caption(caption=(q.message.caption or "") + "\n\n🛑 Stopped.")
        except Exception:
            pass
        return
    if q.data == "scr_continue":
        url = context.user_data.get('scr_url')
        nxt = context.user_data.get('scr_next', 11)
        if not url:
            await q.message.reply_text("❌ URL lost. /scr <url> dobara bhejo.")
            return
        await scr_run(q.message.chat, context, q.from_user.id, url, nxt, nxt + 9)


# ==========================================================
# JS-RENDERED (SPA) SITES: embedded JSON / hidden API / sitemap / optional browser
# ==========================================================
try:   # optional: real headless browser (pip install playwright && playwright install chromium)
    from playwright.async_api import async_playwright
except Exception:
    async_playwright = None

_RULE_FAILED: set = set()           # domains where auto-rule already failed (don't retry on every /scr)
_JSON_CACHE: Dict[str, object] = {}
_PW = {"pw": None, "browser": None}
_PW_SEM = None

_SKIP_SCRIPT = re.compile(
    r'(google|gtag|analytics|facebook|jquery|plyr|hls\.|swiper|cloudflare|recaptcha|adsbygoogle|'
    r'doubleclick|yandex|metrika|histats|unpkg|polyfill|bootstrap|fontawesome|sentry|hotjar|'
    r'exoclick|juicyads|trafficjunky|popads|propeller)', re.I)
_API_SKIP = re.compile(
    r'(\.(?:js|css|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|map|html?)(?:\?|$)|analytics|tracking|metrics|'
    r'(?:^|[/_.-])logs?(?:[/_.?-]|$)|locale|i18n|translation|(?:^|[/_.-])ads?/|adserver|'
    r'auth|login|logout|register|token|csrf|captcha|consent|cookie|manifest|package\.json|browserconfig|'
    r'service-worker|favicon)', re.I)
_API_WORDS = re.compile(
    r'(video|clip|movie|film|list|feed|latest|newest|new|popular|trending|home|index|content|post|'
    r'search|browse|catalog|recommend|top|best|hot|all)', re.I)
_API_RX = [
    r'["\'`]((?:https?:)?//[^"\'`\s]+?/(?:api|ajax|rest|v\d)/[^"\'`\s]*)["\'`]',
    r'["\'`](/(?:api|ajax|rest|graphql|_next/data|v\d)/[^"\'`\s]*)["\'`]',
    r'["\'`]([^"\'`\s]+\.json(?:\?[^"\'`\s]*)?)["\'`]',
]
_PAGE_KEYS = ('url', 'link', 'permalink', 'href', 'page_url', 'pageUrl', 'path', 'uri',
              'canonical', 'watch_url', 'video_url_page')
_TITLE_KEYS = ('title', 'name', 'headline', 'caption')


def _raw_fetch_sync(url: str, referer: Optional[str] = None, accept: str = "application/json, text/plain, */*") -> Optional[str]:
    """Like fetch_sync but accepts short bodies (JSON / robots / sitemap)."""
    headers = make_headers(url, referer)
    headers["Accept"] = accept
    headers["X-Requested-With"] = "XMLHttpRequest"
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    attempts = []
    if cffi_requests:
        attempts.append(lambda: cffi_requests.get(url, headers=headers, impersonate="chrome124",
                                                   timeout=25, proxies=proxies))
    attempts.append(lambda: scraper.get(url, headers=headers, timeout=25, proxies=proxies))
    attempts.append(lambda: requests.get(url, headers=headers, timeout=25, proxies=proxies))
    for fn in attempts:
        try:
            r = fn()
            if r.status_code == 200 and r.text and len(r.text.strip()) > 2:
                return r.text
        except Exception:
            continue
    return None


async def scr_get_json(url: str, referer: Optional[str] = None):
    if url in _JSON_CACHE:
        return _JSON_CACHE[url]
    txt = await asyncio.to_thread(_raw_fetch_sync, url, referer)
    data = None
    if txt:
        try:
            data = json.loads(txt.strip().lstrip('\ufeff'))
        except Exception:
            data = None
    if len(_JSON_CACHE) > 300:
        _JSON_CACHE.clear()
    _JSON_CACHE[url] = data
    return data


def _balanced(text: str, i: int) -> Optional[str]:
    """text[i] is '{' or '[' -> returns the balanced JSON-ish substring (string-aware)."""
    open_c = text[i]
    close_c = '}' if open_c == '{' else ']'
    depth, in_s, esc = 0, None, False
    for j in range(i, min(len(text), i + 1_500_000)):
        c = text[j]
        if in_s:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == in_s:
                in_s = None
        elif c in '"\'':
            in_s = c
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return None


def embedded_json_blobs(html: str) -> list:
    """JSON stored inside the page: <script type=application/json>, __NEXT_DATA__, window.__STATE__ = {...}"""
    blobs = []
    for m in re.finditer(r'<script\b[^>]*\btype=["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>',
                         html, re.I | re.S):
        try:
            blobs.append(json.loads(m.group(1)))
        except Exception:
            pass
    for m in re.finditer(r'(?:window\.)?(?:__[A-Za-z_]+__|initialState|INITIAL_STATE|pageData|videoData)'
                         r'\s*=\s*([{\[])', html):
        s = _balanced(html, m.start(1))
        if s:
            try:
                blobs.append(json.loads(s))
            except Exception:
                pass
    return blobs[:12]


def _walk_json(obj, depth: int = 0):
    if depth > 12:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from _walk_json(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:500]:
            if isinstance(v, (dict, list)):
                yield from _walk_json(v, depth + 1)


def _strings(obj, depth: int = 0):
    if depth > 3:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:50]:
            yield from _strings(v, depth + 1)


def scr_json_harvest(blobs: list, page_url: str):
    """-> (stream_items, page_url_strings) found inside JSON objects."""
    streams, pages, seen = [], [], set()
    for blob in blobs:
        for d in _walk_json(blob):
            title = next((str(d[k]) for k in _TITLE_KEYS
                          if isinstance(d.get(k), str) and d[k].strip()), None)
            best = None
            for s in _strings(d):
                low = s.lower()
                if len(s) > 2000 or not ('.mp4' in low or '.m3u8' in low or 'get_file' in low):
                    continue
                u = _clean(s, page_url)
                if _valid_stream_url(u) and (best is None or _rank(u) < _rank(best)):
                    best = u
            if best and best not in seen:
                seen.add(best)
                streams.append({
                    "title": re.sub(r'\s+', ' ', title or "Video")[:150], "type": "VIDEO",
                    "page_url": page_url, "source_page": page_url,
                    "download_link": process_tpl_link(best) if ".m3u8" in best else best})
                continue
            for k in _PAGE_KEYS:
                v = d.get(k)
                if isinstance(v, str) and 1 < len(v) < 300 and not v.startswith(('javascript:', '#', 'data:')):
                    pages.append(v)
                    break
    return streams, pages


def _links_from_raw(raw: List[str], page_url: str) -> List[str]:
    """Raw url strings -> filtered video-page links (same shape-majority logic as normal pages)."""
    if not raw:
        return []
    synth = "".join(f'<a href="{_html.escape(u)}"><img src="x"></a>' for u in dict.fromkeys(raw))
    return find_video_links(synth, page_url, use_rule=False)


def _script_srcs(html: str, page_url: str) -> List[str]:
    out = []
    for m in re.finditer(r'<script\b[^>]*?\bsrc=["\']([^"\']+)["\']', html, re.I):
        u = urljoin(page_url, m.group(1).replace('&amp;', '&'))
        if u.startswith(('http://', 'https://')) and not _SKIP_SCRIPT.search(u) and u not in out:
            out.append(u)
    return out


def _api_candidates(texts: List[str], page_url: str) -> List[str]:
    found: Dict[str, int] = {}
    for t in texts:
        for rx in _API_RX:
            for m in re.finditer(rx, t):
                raw = m.group(1).replace('\\/', '/')
                if '${' in raw or '{' in raw or '}' in raw or len(raw) > 300:
                    continue
                u = urljoin(page_url, ('https:' + raw) if raw.startswith('//') else raw)
                if not u.startswith(('http://', 'https://')) or _API_SKIP.search(u) or _SKIP_SCRIPT.search(u):
                    continue
                if u.endswith('='):
                    u += '1'
                score = len(_API_WORDS.findall(u)) + (2 if '/api/' in u else 0) + (1 if '.json' in u else 0)
                if score and u not in found:
                    found[u] = score
    return sorted(found, key=lambda x: -found[x])[:12]


async def scr_spa_discover(page_url: str, html: str, start: int = 1, end: int = 1, light: bool = False) -> dict:
    """Finds data of a JS-rendered page: embedded JSON state + API endpoints read from its JS bundles."""
    info: List[str] = []
    blobs = embedded_json_blobs(html)
    streams, raw_pages = scr_json_harvest(blobs, page_url)
    info.append(f"🧬 Embedded JSON blobs: {len(blobs)} (streams {len(streams)}, links {len(raw_pages)})")

    texts = re.findall(r'<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.I | re.S)
    srcs = _script_srcs(html, page_url)[:8]
    bundles = await asyncio.gather(*[fast_fetch(s, referer=page_url) for s in srcs]) if srcs else []
    texts += [b for b in bundles if b]
    info.append(f"📦 JS bundles read: {sum(1 for b in bundles if b)}/{len(srcs)}")

    eps = _api_candidates(texts, page_url)
    info.append(f"🔌 API candidates: {len(eps)}")
    probes = await asyncio.gather(*[scr_get_json(e, page_url) for e in eps]) if eps else []

    best_ep, best_n = None, 0
    for ep, data in zip(eps, probes):
        if data is None:
            info.append(f"   ✗ {ep[:90]}")
            continue
        s, p = scr_json_harvest([data], page_url)
        n = len(s) + len(p)
        info.append(f"   ✓ {ep[:90]} ({len(s)} streams, {len(p)} links)")
        streams += s
        raw_pages += p
        if n > best_n:
            best_ep, best_n = ep, n

    # ---- pagination of the best API endpoint (?page=N) ----
    if not light and best_ep and end > start:
        pm = re.search(r'([?&](?:page|p|pg|pageNumber|page_number)=)(\d+)', best_ep)
        if pm:
            todo = [re.sub(r'([?&](?:page|p|pg|pageNumber|page_number)=)\d+',
                           lambda m_: m_.group(1) + str(n), best_ep)
                    for n in range(start, min(end, start + 29) + 1) if str(n) != pm.group(2)]
            for data in await asyncio.gather(*[scr_get_json(u, page_url) for u in todo]):
                if data is not None:
                    s, p = scr_json_harvest([data], page_url)
                    streams += s
                    raw_pages += p

    uniq = {}
    for s in streams:
        uniq.setdefault(s["download_link"], s)
    return {"streams": list(uniq.values()), "links": _links_from_raw(raw_pages, page_url), "info": info}


async def scr_sitemap(page_url: str) -> dict:
    """sitemap.xml fallback (SPAs usually still publish one, sometimes with <video:content_loc> streams)."""
    pu = urlparse(page_url)
    base = f"{pu.scheme}://{pu.netloc}"
    maps: List[str] = []
    robots = await asyncio.to_thread(_raw_fetch_sync, base + "/robots.txt", None, "text/plain,*/*")
    if robots:
        maps += re.findall(r'(?im)^\s*sitemap:\s*(\S+)', robots)
    for p in ("/sitemap.xml", "/sitemap_index.xml", "/video-sitemap.xml", "/sitemap-videos.xml"):
        if base + p not in maps:
            maps.append(base + p)

    streams, links, seen_maps, queue = [], [], set(), maps[:6]
    while queue and len(seen_maps) < 6 and len(links) < 600:
        m = queue.pop(0)
        if m in seen_maps:
            continue
        seen_maps.add(m)
        xml = await asyncio.to_thread(_raw_fetch_sync, m, None, "application/xml,text/xml,*/*")
        if not xml:
            continue
        blocks = re.findall(r'<url>(.*?)</url>', xml, re.I | re.S)
        if not blocks:                                   # sitemap index -> child sitemaps
            kids = re.findall(r'<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)', xml, re.I)
            kids.sort(key=lambda k: 0 if 'video' in k.lower() else 1)
            queue += [k for k in kids if k not in seen_maps][:3]
            continue
        for b in blocks:
            loc = re.search(r'<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)', b, re.I)
            cl = re.search(r'<video:content_loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)', b, re.I)
            tt = re.search(r'<video:title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</video:title>', b, re.I | re.S)
            if cl and _valid_stream_url(_clean(cl.group(1), page_url)):
                u = _clean(cl.group(1), page_url)
                streams.append({"title": (tt.group(1).strip() if tt else "Video")[:150], "type": "VIDEO",
                                "page_url": loc.group(1) if loc else page_url, "source_page": m,
                                "download_link": process_tpl_link(u) if ".m3u8" in u else u})
            elif loc:
                links.append(loc.group(1).replace('&amp;', '&'))
    root = _root_host(pu.netloc)
    links = [l for l in dict.fromkeys(links) if _root_host(urlparse(l).netloc) == root
             and not BAD_PATH.search(urlparse(l).path) and not urlparse(l).path.lower().endswith(SKIP_EXT)
             and urlparse(l).path not in ('', '/')]
    return {"streams": streams, "links": links}


# ---------------- optional real browser (Playwright) ----------------
async def pw_render(url: str, wait: float = 4.0, scroll: bool = False, play: bool = False,
                    netlog: Optional[list] = None, cookies_out: Optional[dict] = None):
    """-> (rendered_html, [stream urls seen in network traffic]). Needs: pip install playwright && playwright install chromium"""
    global _PW_SEM
    if not async_playwright:
        return None, []
    if _PW_SEM is None:
        _PW_SEM = asyncio.Semaphore(2)
    streams: List[str] = []
    async with _PW_SEM:
        ctx = None
        try:
            if _PW["browser"] is None:
                _PW["pw"] = await async_playwright().start()
                _PW["browser"] = await _PW["pw"].chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = await _PW["browser"].new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
            page = await ctx.new_page()
            page.on("request", lambda r: streams.append(r.url) if _valid_stream_url(r.url) else None)

            def _on_resp(resp):
                try:
                    ct = (resp.headers.get("content-type") or "").lower()
                    rt = resp.request.resource_type
                    if netlog is not None and (rt in ("xhr", "fetch", "media", "document") or "video" in ct or "mpegurl" in ct):
                        netlog.append((rt, resp.status, ct.split(";")[0], resp.url))
                    if ("video/" in ct or "mpegurl" in ct) and resp.url not in streams and not JUNK.search(resp.url.lower()):
                        streams.append(resp.url)          # URL me extension na ho tab bhi content-type se pakdo
                except Exception:
                    pass

            page.on("response", _on_resp)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(int(wait * 1000))
            if scroll:
                for _ in range(3):
                    await page.mouse.wheel(0, 4000)
                    await page.wait_for_timeout(700)
            for sel in ("video", ".play", "[class*=play]", "button"):     # nudge lazy players
                if streams and not play:
                    break
                try:
                    await page.click(sel, timeout=1200)
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass
            if play:                                            # player aksar iframe ke andar hota hai
                for fr in list(page.frames)[1:4]:
                    for sel in ("video", ".play", "[class*=play]", "button"):
                        try:
                            await fr.click(sel, timeout=800)
                            await page.wait_for_timeout(1200)
                            break
                        except Exception:
                            pass
            html = await page.content()
            if cookies_out is not None:
                try:
                    for c_ in await ctx.cookies():
                        cookies_out[c_["name"]] = c_["value"]
                except Exception:
                    pass
            return html, list(dict.fromkeys(streams))
        except Exception as e:
            logger.error(f"pw_render error {url}: {e}")
            return None, streams
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass


async def scr_deep_extract(v: str, s: str) -> Optional[dict]:
    """Video page is a JS shell: try embedded JSON / API streams, then the real browser."""
    page = await fast_fetch(v, referer=s)
    if page:
        d = await scr_spa_discover(v, page, light=True)
        if d["streams"]:
            it = dict(sorted(d["streams"], key=lambda x: _rank(x["download_link"]))[0])
            it.update(page_url=v, source_page=s)
            if it["title"] == "Video":
                it["title"] = _scr_title(page)
            return it
    if async_playwright:
        html, streams = await pw_render(v, wait=4)
        if streams:
            best = sorted(streams, key=_rank)[0]
            return {"title": _scr_title(html or ""), "type": "VIDEO", "page_url": v, "source_page": s,
                    "download_link": process_tpl_link(best) if ".m3u8" in best else best}
    return None


async def scr_extract_many(items: Dict[str, str], user_id: int) -> List[dict]:
    sem = asyncio.Semaphore(SCR_CONCURRENCY)
    out: Dict[str, dict] = {}

    async def one(v: str, s: str):
        if STOP_PROCESS.get(user_id):
            return
        async with sem:
            r = await extract_video_link(v, source_page=s)
            if not r:
                r = await scr_deep_extract(v, s)
        if r:
            out[v] = r

    await asyncio.gather(*[one(v, s) for v, s in items.items()])
    return [out[v] for v in items if v in out]


async def scr_spa_fallback(url: str, html: str, page_url: str, start: int, end: int, user_id: int):
    """Everything we try when a listing page is an empty JS shell. -> (items, diag_text)"""
    d = await scr_spa_discover(page_url, html, start, end)
    info = list(d["info"])
    direct, links, sliced = list(d["streams"]), list(d["links"]), False

    if not direct and not links and async_playwright:
        rendered, _ = await pw_render(page_url, wait=5, scroll=True)
        if rendered:
            links = scr_find_links(rendered, page_url)
        info.append(f"🌐 Browser render: {len(links)} links")
    elif not async_playwright:
        info.append("🌐 Browser mode: OFF (optional: pip install playwright && playwright install chromium)")

    if not direct and not links:
        sm = await scr_sitemap(page_url)
        direct, links, sliced = sm["streams"], sm["links"], True
        info.append(f"🗺 Sitemap: {len(sm['streams'])} streams / {len(sm['links'])} links")

    if sliced:                                           # sitemap is one long list -> emulate pages (24 per page)
        per = 24
        direct = direct[(start - 1) * per: end * per]
        links = links[(start - 1) * per: end * per]

    got = await scr_extract_many({l: page_url for l in links[:300]}, user_id) if links else []
    merged, seen = [], set()
    for it in direct[:300] + got:
        if it["download_link"] not in seen:
            seen.add(it["download_link"])
            merged.append(it)
    info.append(f"✅ SPA mode result: {len(merged)} items")
    return merged, "\n".join(info)


# ==========================================================
# /sky : TXT / M3U file -> YouTube-style HTML player
# ==========================================================
_URL_LINE_RX = re.compile(r'(https?://[^\s<>"\']+)', re.I)
_SKY_LABELS = re.compile(r'^(source listing page|permanent video page|direct stream link)\b', re.I)


def _kind_of(url: str) -> str:
    if '.m3u8' in url.lower():
        return "VIDEO"
    last = urlparse(url).path.lower().rsplit('/', 1)[-1]
    ext = last.rsplit('.', 1)[-1] if '.' in last else ''
    if ext in ("mp3", "m4a", "aac", "wav", "ogg", "oga", "opus", "flac", "wma"):
        return "AUDIO"
    if ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp"):
        return "IMAGE"
    if ext == "pdf":
        return "PDF"
    return "VIDEO"


def _title_from_url(url: str, n: int) -> str:
    pu = urlparse(url)
    name = unquote(pu.path.rstrip('/').rsplit('/', 1)[-1])
    name = re.sub(r'(?:\.(?:mp4|mkv|webm|mov|avi|flv|ts|mpd|mp3|m4a|m3u8|ogg|wav|flac|aac|jpe?g|png|gif|webp|bmp|pdf))+$', '', name, flags=re.I)
    name = re.sub(r'[-_.]+', ' ', name).strip()
    if len(name) < 4 or re.match(r'^\d{3,4}p\b', name):
        return f"{pu.netloc.replace('www.', '')} #{n}"
    return name[:120]


def parse_links_txt(text: str, limit: int = 3000) -> List[dict]:
    """TXT/M3U se items: 'Title: URL', sirf URL, bot ka full format (Title/Direct Stream Link), #EXTINF."""
    items: List[dict] = []
    seen = set()
    pend_title, pend_page = None, ""

    def add(url: str, title: Optional[str], page: str = ""):
        url = url.rstrip('.,;)]}>')
        if url in seen or len(items) >= limit:
            return
        seen.add(url)
        t_ = re.sub(r'\s+', ' ', _html.unescape(title or "")).strip()
        items.append({"title": t_[:200] or _title_from_url(url, len(items) + 1), "type": _kind_of(url),
                      "page_url": page, "source_page": page, "download_link": url,
                      "variants": make_variants(url)})

    for raw in text.splitlines():
        ln = raw.strip().lstrip('\ufeff')
        if not ln:
            continue
        if ln.upper().startswith('#EXTINF'):
            pend_title = ln.split(',', 1)[1].strip() if ',' in ln else None
            continue
        if ln.startswith('#'):
            continue
        m = _URL_LINE_RX.search(ln)
        if not m:
            mt = (re.match(r'^\d+\s*[\.\)]\s*Title\s*:\s*(.+)$', ln, re.I)
                  or re.match(r'^Title\s*:\s*(.+)$', ln, re.I))
            if mt:
                pend_title = mt.group(1).strip()
            continue
        url = m.group(1)
        label = re.sub(r'^\d+\s*[\.\)]\s*', '', ln[:m.start()].strip())
        label = re.sub(r'[\s:\-|\u2013\u2014>]+$', '', label).strip()
        lm = _SKY_LABELS.match(label)
        if lm:
            k = lm.group(1).lower()
            if k.startswith('permanent'):
                pend_page = url
            elif k.startswith('direct'):
                add(url, pend_title, pend_page)
                pend_title, pend_page = None, ""
            continue
        label = re.sub(r'^Title\s*:\s*', '', label, flags=re.I)
        add(url, label or pend_title)
        pend_title, pend_page = None, ""
    return items


def sky_armed(context) -> bool:
    return context.user_data.get('sky_until', 0) > time.time()


async def sky_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sky [title]  -> agli TXT/M3U file se YouTube-style HTML player banake bhejo.  /sky off -> band."""
    uid = update.effective_user.id
    if not is_user_allowed(uid):
        return
    args = context.args or []
    if args and args[0].lower() == "off":
        context.user_data.pop('sky_until', None)
        context.user_data.pop('sky_title', None)
        await update.message.reply_text("❎ /sky band. Ab TXT file bhejne par normal download chalega.")
        return
    context.user_data['sky_until'] = time.time() + 600
    context.user_data['sky_title'] = " ".join(args).strip()
    await update.message.reply_text(
        "🎬 Sky mode ON (10 minute)\n\n"
        "Ab apni .txt (ya .m3u) file bhejo, usme jo links hongi unse YouTube-style HTML player bana ke dunga:\n"
        "• thumbnail auto, favorites, history, quality menu\n"
        "• formats: mp4, m3u8, mkv, webm, mp3 aur baaki\n\n"
        "Accept: `Title: URL`, sirf URL, bot ki full/simple TXT, M3U playlist.\n"
        "Title dene ke liye: /sky My Playlist\n"
        "Ya file ke caption me /sky likh ke bhejo. Band: /sky off".replace("`", ""))


async def sky_from_file(update: Update, context: ContextTypes.DEFAULT_TYPE, doc):
    chat = update.effective_chat
    status = await update.message.reply_text("🎬 Player ban raha hai...")
    try:
        f = await context.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await f.download_to_memory(buf)
        raw = buf.getvalue()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('latin-1', errors='ignore')
        items = parse_links_txt(text)
        if not items:
            await status.edit_text("❌ File me koi valid http/https link nahi mila.\n"
                                   "Format: `Title: URL` ya har line me ek URL.".replace("`", ""))
            return
        cap = (update.message.caption or "").strip()
        cap_title = re.sub(r'^/sky\b', '', cap, flags=re.I).strip()
        stem = re.sub(r'\.[A-Za-z0-9]+$', '', doc.file_name or "playlist")
        title = (cap_title or context.user_data.get('sky_title') or stem.replace('_', ' ')).strip()[:60] or "Sky Player"
        html = await asyncio.to_thread(generate_web_app_html, items, title)
        out = io.BytesIO(html.encode('utf-8'))
        safe = re.sub(r'[^A-Za-z0-9._-]+', '_', stem)[:40].strip('_') or "playlist"
        out.name = f"{safe}_player.html"
        kinds = Counter(i["type"] for i in items)
        await chat.send_document(
            document=out,
            caption=(f"🎬 {title}\n📦 {len(items)} items "
                     f"({', '.join(f'{k} {v}' for k, v in kinds.items())})\n"
                     f"🔒 Password: SKY_PASSWORD wala\n"
                     f"👑 {BOT_OWNER_NAME}\n\nChrome me kholo (Telegram ke in-app browser se behtar)."))
        context.user_data['sky_until'] = time.time() + 600       # agli file ke liye bhi chalu
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"sky error: {e}")
        await status.edit_text(f"❌ Player nahi ban paya: {str(e)[:150]}")


async def sky_only_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """.m3u files: sirf /sky mode ya caption '/sky' par."""
    if not is_user_allowed(update.effective_user.id):
        return
    doc = update.message.document
    if sky_armed(context) or (update.message.caption or "").strip().lower().startswith("/sky"):
        await sky_from_file(update, context, doc)
    else:
        await update.message.reply_text("ℹ️ Is file se HTML player banana hai? Pehle /sky bhejo, phir file.")


# ==========================================================
# MAIN ENTRYPOINT
# ==========================================================
# ==========================================================
# /cookie <website>  ->  id + password maango, login karke cookies bhejo
# ==========================================================
_LOGIN_PATHS = ("/login", "/signin", "/user/login", "/account/login", "/login/", "/sign-in",
                "/auth/login", "/users/login", "/accounts/login", "/member/login")


def _attr(a: str, k: str) -> str:
    mm = re.search(rf'\b{k}\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', a, re.I)
    return _html.unescape((mm.group(1) or mm.group(2) or mm.group(3) or "")) if mm else ""


def parse_forms(html: str) -> List[dict]:
    forms = []
    for fm in re.finditer(r'<form\b([^>]*)>(.*?)</form>', html, re.I | re.S):
        attrs, body = fm.group(1), fm.group(2)
        inputs = []
        for im in re.finditer(r'<input\b([^>]*)>', body, re.I | re.S):
            a = im.group(1)
            inputs.append({"name": _attr(a, "name"),
                           "type": (_attr(a, "type") or "text").lower(),
                           "value": _attr(a, "value")})
        forms.append({"action": _attr(attrs, "action"),
                      "method": (_attr(attrs, "method") or "post").lower(),
                      "inputs": inputs})
    return forms


def _login_form(html: str) -> Optional[dict]:
    for f in parse_forms(html):
        if any(i["type"] == "password" and i["name"] for i in f["inputs"]):
            return f
    return None


def _cookie_dict(sess) -> dict:
    try:
        return dict(sess.cookies.get_dict())
    except Exception:
        try:
            return {k: v for k, v in sess.cookies.items()}
        except Exception:
            return {}


def _fill_template(o, u: str, p: str):
    if isinstance(o, str):
        return o.replace("{user}", u).replace("{pass}", p)
    if isinstance(o, dict):
        return {k: _fill_template(v, u, p) for k, v in o.items()}
    if isinstance(o, list):
        return [_fill_template(v, u, p) for v in o]
    return o


def _redact(t: str, *secrets) -> str:
    for sx in secrets:
        if sx:
            t = t.replace(sx, "***")
    return t


def _api_login(sess, base: str, user: str, pwd: str, api, proxies) -> dict:
    """JSON API login (xhamster jaisi JS sites): common payloads try karta hai, har try ka result dikhata hai."""
    url, template = api
    log: List[str] = []
    statuses: List[str] = []
    csrf = None
    try:
        h0 = make_headers(base + "/")
        h0.pop("Cookie", None)
        r0 = sess.get(base + "/", headers=h0, timeout=20, proxies=proxies)
        statuses.append(f"/:{r0.status_code}")
        if r0.status_code == 200:
            mm = re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)', r0.text, re.I)
            if mm:
                csrf = mm.group(1)
    except Exception:
        statuses.append("/:error")
    before = set(_cookie_dict(sess))

    if template:
        try:
            variants = [("template", _fill_template(json.loads(template), user, pwd))]
        except Exception:
            return {"ok": False, "error": "payload template JSON galat hai", "statuses": statuses,
                    "cookies": _cookie_dict(sess), "api_log": log}
    else:
        variants = [(k, {k: user, "password": pwd, "remember": True}) for k in ("username", "login", "email")]

    for name, payload in variants:
        h = make_headers(base + "/", base + "/")
        h.pop("Cookie", None)
        h.update({"Accept": "application/json, text/plain, */*", "Content-Type": "application/json",
                  "X-Requested-With": "XMLHttpRequest", "Origin": base})
        if csrf:
            h["X-CSRF-Token"] = csrf
        xs = _cookie_dict(sess).get("XSRF-TOKEN")
        if xs:
            h["X-XSRF-TOKEN"] = unquote(xs)
        try:
            r = sess.post(url, json=payload, headers=h, timeout=25, proxies=proxies)
        except Exception as e:
            log.append(f"{name}: error {_redact(str(e), user, pwd)[:60]}")
            continue
        body = (r.text or "")[:600]
        statuses.append(f"API[{name}]:{r.status_code}")
        log.append(f"{name}: HTTP {r.status_code} {_redact(re.sub(chr(92) + 's+', ' ', body), user, pwd)[:150]}")
        try:
            js = r.json()
        except Exception:
            js = None
        errorlike = isinstance(js, dict) and bool(js.get("error") or js.get("errors") or js.get("success") is False)
        cookies = _cookie_dict(sess)
        new = sorted(set(cookies) - before)
        ok = r.status_code < 400 and not errorlike and (isinstance(js, dict) or bool(new))
        if ok:
            return {"ok": True, "cookies": cookies, "new": new, "statuses": statuses, "api_log": log}
        low = body.lower()
        if re.search(r'captcha|recaptcha|turnstile', low):
            log.append("⛔ Captcha maang raha hai: API se login nahi hoga, browser se cookie lo (/login).")
            break
        if r.status_code in (403, 503) and js is None:
            log.append("⛔ 403/503: Cloudflare/IP block.")
            break
        if re.search(r'invalid (login|password|cred|user)|incorrect|wrong (pass|login|cred)|credentials|неверн', low):
            break            # fields sahi the, password/ID galat -> aur try karke account lock mat karo
    return {"ok": False, "error": "API login confirm nahi hua", "statuses": statuses,
            "cookies": _cookie_dict(sess), "api_log": log}


def cookie_login_sync(base: str, user: str, pwd: str, api=None) -> dict:
    """Generic form login: login page dhundo -> form bharo (hidden/csrf fields ke saath) -> POST -> cookies."""
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    if cffi_requests:
        sess = cffi_requests.Session(impersonate="chrome124")
    else:
        sess = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
    if api:
        return _api_login(sess, base, user, pwd, api, proxies)
    statuses: List[str] = []

    def hdrs(ref=None):
        h = make_headers(base + "/", ref)
        h.pop("Cookie", None)
        return h

    def get(u, ref=None):
        return sess.get(u, headers=hdrs(ref), timeout=20, proxies=proxies)

    # 1) login page dhundo
    cands: List[str] = []
    try:
        r0 = get(base + "/")
        statuses.append(f"/:{r0.status_code}")
        if r0.status_code == 200:
            for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r0.text, re.I | re.S):
                txt = re.sub(r'<[^>]+>', ' ', m.group(2))
                if re.search(r'log\s*-?in|sign\s*-?in|login|signin', m.group(1) + " " + txt, re.I):
                    u = urljoin(base + "/", m.group(1).replace('&amp;', '&')).split('#')[0]
                    if u.startswith('http') and u not in cands:
                        cands.append(u)
    except Exception as e:
        statuses.append(f"/:error({str(e)[:40]})")
    for pth in _LOGIN_PATHS:
        u = base + pth
        if u not in cands:
            cands.append(u)

    page_url, page_html, form = None, None, None
    for u in cands[:10]:
        try:
            r = get(u, base + "/")
        except Exception as e:
            statuses.append(f"{urlparse(u).path}:error")
            continue
        statuses.append(f"{urlparse(u).path or '/'}:{r.status_code}")
        if r.status_code == 200:
            f = _login_form(r.text)
            if f:
                page_url, page_html, form = u, r.text, f
                break
    if not form:
        return {"ok": False, "error": "login form nahi mila", "statuses": statuses,
                "cookies": _cookie_dict(sess)}

    # 2) form bharo
    pw_name = next(i["name"] for i in form["inputs"] if i["type"] == "password" and i["name"])
    text_inputs = [i for i in form["inputs"] if i["type"] in ("text", "email", "tel") and i["name"]]
    user_field = next((i for i in text_inputs
                       if re.search(r'user|login|email|mail|name|id', i["name"], re.I)),
                      text_inputs[0] if text_inputs else None)
    if not user_field:
        return {"ok": False, "error": "form me id/email field nahi mila", "statuses": statuses,
                "cookies": _cookie_dict(sess)}
    data = {}
    for i in form["inputs"]:
        n, t = i["name"], i["type"]
        if not n or t in ("submit", "button", "image", "file", "reset"):
            continue
        if t == "checkbox":
            if re.search(r'remember|keep|stay', n, re.I):
                data[n] = i["value"] or "1"
            continue
        if t == "radio":
            data.setdefault(n, i["value"])
            continue
        data[n] = i["value"]
    data[user_field["name"]] = user
    data[pw_name] = pwd

    action = urljoin(page_url, form["action"]) if form["action"] else page_url
    h = hdrs(page_url)
    h["Origin"] = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    before = set(_cookie_dict(sess))
    try:
        if form["method"] == "get":
            r = sess.get(action, params=data, headers=h, timeout=25, proxies=proxies)
        else:
            r = sess.post(action, data=data, headers=h, timeout=25, proxies=proxies)
    except Exception as e:
        return {"ok": False, "error": f"submit error: {str(e)[:80]}", "statuses": statuses,
                "cookies": _cookie_dict(sess)}
    statuses.append(f"POST:{r.status_code}")
    cookies = _cookie_dict(sess)
    still_form = bool(_login_form(r.text or ""))
    new_names = sorted(set(cookies) - before)
    ok = r.status_code < 400 and not still_form
    return {"ok": ok, "cookies": cookies, "new": new_names, "statuses": statuses,
            "still_form": still_form, "final_url": str(getattr(r, "url", action))[:120],
            "http": r.status_code}


async def cookie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cookie <website>  -> id + password poochta hai, login karke cookie string bhejta hai."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text(
            "🍪 /cookie <website>\n\nExample:\n/cookie example.com\n\n"
            "JS/API login wali site (jaise xhamster):\n"
            "/cookie xhamster46.desi https://xhamster46.desi/api/front/user/login\n"
            '(optional payload: ... {"username":"{user}","password":"{pass}","remember":true})\n\n'
            "Phir bot ID/email aur password maangega, login karke cookies bhej dega.\n"
            "Cancel karne ke liye: cancel")
        return
    domain = normalize_domain(context.args[0])
    if not DOMAIN_RE.match(domain):
        await update.message.reply_text(f"❌ Invalid domain: {context.args[0]}")
        return
    parts = (update.message.text or "").split(None, 3)
    if len(parts) >= 3 and parts[2].lower().startswith("http"):      # /cookie <domain> <api_url> [payload json]
        tmpl = parts[3].strip() if len(parts) > 3 else None
        if tmpl:
            try:
                json.loads(tmpl)
            except Exception:
                await update.message.reply_text('❌ Payload JSON galat hai. Example: {"username":"{user}","password":"{pass}"}')
                return
        set_api_login(domain, parts[2].strip(), tmpl)
        await update.message.reply_text(f"✅ API login saved: {parts[2].strip()}" + (" (custom payload)" if tmpl else " (auto payload)"))
    context.user_data['cookie_flow'] = {"domain": domain, "step": "id"}
    await update.message.reply_text(
        f"🍪 {domain}\n👤 Apna ID / email bhejo (cancel likhne par band):")


async def cookie_flow_step(update: Update, context: ContextTypes.DEFAULT_TYPE, cf: dict, text: str):
    chat = update.effective_chat
    if text.strip().lower() == "cancel":
        context.user_data.pop('cookie_flow', None)
        await chat.send_message("❎ Cookie login cancel ho gaya.")
        return
    try:
        await update.message.delete()           # id/password wala message turant delete
    except Exception:
        pass
    if cf["step"] == "id":
        cf["user"] = text.strip()
        cf["step"] = "pass"
        await chat.send_message("🔒 Ab password bhejo (message turant delete ho jayega):")
        return

    context.user_data.pop('cookie_flow', None)
    domain, user, pwd = cf["domain"], cf["user"], text.strip()
    status = await chat.send_message(f"⏳ {domain} par login ho raha hai...")
    try:
        res = await asyncio.wait_for(
            asyncio.to_thread(cookie_login_sync, f"https://{domain}", user, pwd, get_api_login(domain)),
            timeout=120)
    except Exception as e:
        await status.edit_text(f"❌ Login error: {str(e)[:200]}")
        return

    cookies = res.get("cookies") or {}
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    stat = ", ".join(res.get("statuses", []))
    if res.get("api_log"):
        stat += "\n🧪 API tries:\n" + "\n".join(res["api_log"])[:1200]

    if res.get("error"):
        hint = ""
        if any(x.endswith(":403") or x.endswith(":503") for x in res.get("statuses", [])):
            hint = ("\n🚫 403/503 = site tumhare server ki IP ko block kar rahi hai, isliye login page hi nahi khul raha.\n"
                    "Fix: bot ko ghar ke PC/Indian IP par chalao ya PROXY_URL (residential) lagao.")
        elif cookies:
            hint = "\nℹ️ Site JS/AJAX login use karti ho sakti hai (form nahi hai)."
        await status.edit_text(f"❌ {domain}: {res['error']}\n📡 {stat}{hint}")
        return

    head = ("✅ Login ho gaya lagta hai" if res.get("ok")
            else "⚠️ Login confirm nahi hua (password galat ya JS/captcha login ho sakta hai)")
    info = (f"{head}\n🌐 {domain}\n📡 {stat}\n🍪 Cookies: {len(cookies)}"
            f"{' | naye: ' + ', '.join(res['new'][:6]) if res.get('new') else ''}")
    if not cookie_str:
        await status.edit_text(info + "\n\n❌ Koi cookie nahi mili.")
        return
    use = f"/login {domain} {cookie_str}"
    if len(use) < 3500:
        await status.edit_text(info + "\n\nBot me lagane ke liye ye bhejo:")
        await chat.send_message(use, disable_web_page_preview=True)
    else:
        await status.edit_text(info + "\n\nCookie lambi hai, file me bhej raha hoon.")
        buf = io.BytesIO(use.encode('utf-8'))
        buf.name = f"cookie_{domain}.txt"
        await chat.send_document(document=buf, caption=f"🍪 {domain} cookies (/login ke saath use karo)")


# ==========================================================
# POWER FEATURES
#   yt-dlp fallback | packed-JS / base64 unpacker | filters | dead-link check
#   settings | jobs | exports (m3u/json/csv) | backup/restore | watch (auto-monitor)
#   adaptive speed limiter
# ==========================================================
import shutil
import tempfile
import zipfile

try:   # optional: pip install yt-dlp  (hazaaron sites ke liye fallback extractor)
    import yt_dlp
except Exception:
    yt_dlp = None

# ---------------- settings (DB me save, /settings se badlo) ----------------
_SETTINGS: Dict[str, str] = {}
_SETTING_DEFAULTS = {"verify": "0", "min_quality": "0", "include": "", "exclude": "",
                     "export": "", "ytdlp": "1", "keep_preview": "0", "proxy": "auto"}


def load_settings():
    _SETTINGS.clear()
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM settings")
        for k, v in cur.fetchall():
            _SETTINGS[k] = v or ""
        conn.close()
    except Exception as e:
        logger.error(f"load_settings error: {e}")
    load_prefers()


def get_setting(key: str) -> str:
    if key in _SETTINGS:
        return _SETTINGS[key]
    return os.getenv("SETTING_" + key.upper(), _SETTING_DEFAULTS.get(key, ""))


def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()
    _SETTINGS[key] = value


def reset_settings():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM settings")
    conn.commit()
    conn.close()
    _SETTINGS.clear()


# ---------------- adaptive speed limiter ----------------
class AdaptiveLimiter:
    """Concurrency khud kam/zyada: 429/503 aaye to aadhi, sab theek ho to dheere badhti hai.
    (Waiter-queue: 100-100 pending task ab har 50ms jag-jag ke event loop ko slow nahi karte.)"""

    def __init__(self, maxn: int):
        import collections
        self.max = max(1, maxn)
        self.limit = self.max
        self.active = 0
        self.ok = 0
        self.throttled = 0
        self._w = collections.deque()

    def _wake(self):
        while self._w and self.active < self.limit:
            fut = self._w.popleft()
            if not fut.done():
                self.active += 1
                fut.set_result(None)

    async def __aenter__(self):
        if self.active < self.limit and not self._w:
            self.active += 1
            return
        fut = asyncio.get_running_loop().create_future()
        self._w.append(fut)
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.active -= 1
                self._wake()
            else:
                try:
                    self._w.remove(fut)
                except ValueError:
                    pass
            raise

    async def __aexit__(self, *a):
        self.active -= 1
        self._wake()

    def feedback(self, url: str, ok: bool):
        if LAST_STATUS.get(url) in (429, 503):
            self.throttled += 1
            self.limit = max(4, self.limit // 2)
        elif ok:
            self.ok += 1
            if self.ok % 12 == 0 and self.limit < self.max:
                self.limit = min(self.max, self.limit + 3)
                self._wake()


# ---------------- packed JS / base64 se chhupe links ----------------
_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_PACKED_RX = re.compile(
    r"\}\(\s*'((?:[^'\\]|\\.)*)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'((?:[^'\\]|\\.)*)'\s*\.split\(\s*'\|'\s*\)", re.S)


def _unbase(s: str, base: int) -> Optional[int]:
    n = 0
    for ch in s:
        v = _B62.find(ch)
        if v < 0 or v >= base:
            return None
        n = n * base + v
    return n


def unpack_packed(text: str) -> List[str]:
    """Dean Edwards p,a,c,k,e,d packer ko kholta hai."""
    out = []
    for m in _PACKED_RX.finditer(text):
        payload, a, kw = m.group(1), int(m.group(2)), m.group(4)
        words = kw.split('|')

        def repl(mm):
            i = _unbase(mm.group(0), a)
            return words[i] if i is not None and i < len(words) and words[i] else mm.group(0)

        res = re.sub(r'\b\w+\b', repl, payload)
        out.append(res.replace("\\'", "'").replace("\\\\", "\\"))
    return out


def deobfuscate_extra(text: str) -> str:
    """Packed JS + atob()/base64 URLs ko decode karke extra text (generic extractor ke liye)."""
    import base64
    parts: List[str] = []
    try:
        parts += unpack_packed(text)
    except Exception:
        pass
    cands = re.findall(r'atob\(\s*["\']([A-Za-z0-9+/=_-]{16,})["\']', text)
    cands += re.findall(r'["\'](aHR0c[A-Za-z0-9+/=]{12,})["\']', text)        # "aHR0c" = "http"
    for c in dict.fromkeys(cands):
        try:
            raw = base64.b64decode(c + "=" * (-len(c) % 4), altchars=b"-_" if ('-' in c or '_' in c) else None)
            dec = raw.decode('utf-8', errors='ignore')
            if '.m3u8' in dec or '.mp4' in dec or dec.startswith('http'):
                parts.append(dec)
        except Exception:
            continue
    return "\n".join(parts)


# ---------------- yt-dlp fallback ----------------
_YTDLP_STATS: Dict[str, dict] = {}


def ytdlp_allowed(url: str) -> bool:
    if yt_dlp is None or get_setting("ytdlp") == "0":
        return False
    st = _YTDLP_STATS.get(_root_host(urlparse(url).netloc))
    return not (st and st["ok"] == 0 and st["fail"] >= 6)


def _ytdlp_pick(info: dict):
    if info.get("entries"):
        ents = [e for e in info["entries"] if e]
        if ents:
            info = ents[0]
    title = info.get("title") or ""
    best, best_score = None, -1
    pool = list(info.get("formats") or [])
    if info.get("url"):
        pool.append(info)
    for f in pool:
        u = f.get("url")
        if not u or f.get("vcodec") == "none" or f.get("ext") in ("mhtml", "jpg", "png", "webp"):
            continue
        proto = f.get("protocol") or ""
        progressive = proto.startswith("http") and "m3u8" not in proto and "dash" not in proto
        score = (f.get("height") or 0) + (100000 if progressive else 0)
        if score > best_score:
            best, best_score = u, score
    if not best and info.get("manifest_url"):
        best = info["manifest_url"]
    return (title, best) if best else None


def ytdlp_extract_sync(url: str, referer: Optional[str] = None):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    cookie = get_cookie_for_url(url)
    if cookie:
        headers["Cookie"] = cookie
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "socket_timeout": 15, "http_headers": headers}
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return _ytdlp_pick(info) if info else None


async def ytdlp_try(url: str, referer: Optional[str] = None):
    st = _YTDLP_STATS.setdefault(_root_host(urlparse(url).netloc), {"ok": 0, "fail": 0})
    try:
        res = await asyncio.wait_for(asyncio.to_thread(ytdlp_extract_sync, url, referer), timeout=45)
    except Exception as e:
        logger.info(f"yt-dlp fail {url[:80]}: {str(e)[:80]}")
        res = None
    st["ok" if res else "fail"] += 1
    return res


# ---------------- filters / expiry / dead-link check ----------------
def stream_quality(u: str) -> Optional[int]:
    q = [int(x) for x in re.findall(r'(\d{3,4})p', u.lower())]
    return max(q) if q else None


def apply_filters(items: List[dict]) -> List[dict]:
    try:
        minq = int(get_setting("min_quality") or 0)
    except ValueError:
        minq = 0
    inc = [w.strip().lower() for w in get_setting("include").split(",") if w.strip()]
    exc = [w.strip().lower() for w in get_setting("exclude").split(",") if w.strip()]
    if not (minq or inc or exc):
        return items
    out = []
    for it in items:
        t = (it.get("title") or "").lower()
        q = stream_quality(it["download_link"])
        if minq and q is not None and q < minq:
            continue
        if inc and not any(w in t for w in inc):
            continue
        if exc and any(w in t for w in exc):
            continue
        out.append(it)
    return out


def stream_expiry(u: str) -> Optional[int]:
    m = (re.search(r'[,/=](1[5-9]\d{8})(?=[,/&?]|$)', u)
         or re.search(r'[?&](?:expires?|exp|e|validto|valid_until)=(\d{10})', u, re.I))
    if m:
        ts = int(m.group(1))
        if 1_500_000_000 < ts < 2_500_000_000:
            return ts
    return None


def exp_line(it: dict) -> str:
    out = ""
    ts = it.get("expires")
    if ts:
        out += "   Expires: " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) + "\n"
    if it.get("iplock"):
        out += f"   IP-locked: {it['iplock']} (sirf usi IP/network se chalegi)\n"
    return out


def verify_stream_sync(url: str, referer: Optional[str] = None) -> Optional[bool]:
    """True = chal raha | False = pakka dead (404/410/HTML/bad playlist) | None = pata nahi (rakho)."""
    h = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    try:
        if '.m3u8' in url.lower():
            r = requests.get(url, headers=h, timeout=(5, 10), proxies=proxies, stream=True)
            try:
                if r.status_code >= 400:
                    return False if r.status_code in (404, 410) else None
                chunk = next(r.iter_content(2048), b"")
                return chunk.lstrip().startswith(b"#EXTM3U")
            finally:
                r.close()
        h["Range"] = "bytes=0-1023"
        r = requests.get(url, headers=h, timeout=(5, 10), proxies=proxies, stream=True)
        try:
            if r.status_code in (200, 206):
                return not (r.headers.get("Content-Type") or "").lower().startswith("text/")
            return False if r.status_code in (404, 410) else None
        finally:
            r.close()
    except Exception:
        return None


async def verify_results(items: List[dict], rep: Optional[dict] = None) -> List[dict]:
    if get_setting("verify") != "1" or not items:
        return items
    sem = asyncio.Semaphore(16)

    async def one(it):
        async with sem:
            it["alive"] = await asyncio.to_thread(verify_stream_sync, it["download_link"], it.get("page_url"))

    await asyncio.gather(*[one(i) for i in items])
    alive = [i for i in items if i.get("alive") is not False]
    if rep is not None:
        rep["dead"] = len(items) - len(alive)
    return alive


# ---------------- exports ----------------

def pack_result_zip(tag: str, files: list) -> Optional[io.BytesIO]:
    """files: list of (filename, text_or_bytes). Returns zip BytesIO or None."""
    if not files:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
            zf.writestr(name, raw)
    buf.seek(0)
    buf.name = f"{tag}_all.zip"
    return buf


def build_exports(results: List[dict], tag: str, fmts: set) -> List[tuple]:
    import csv
    out = []
    if "m3u" in fmts:
        lines = ["#EXTM3U"]
        for it in results:
            lines += [f"#EXTINF:-1,{(it.get('title') or 'Video').replace(chr(10), ' ')}", it["download_link"]]
        out.append((f"{tag}.m3u", "\n".join(lines) + "\n", "🎵 M3U playlist (VLC/MX Player)"))
    if "json" in fmts:
        out.append((f"{tag}.json", json.dumps(results, ensure_ascii=False, indent=1), "🧾 JSON"))
    if "csv" in fmts:
        sio = io.StringIO()
        w = csv.writer(sio)
        w.writerow(["title", "type", "stream", "page", "expires"])
        for it in results:
            w.writerow([it.get("title"), it.get("type"), it["download_link"], it.get("page_url"), it.get("expires") or ""])
        out.append((f"{tag}.csv", sio.getvalue(), "📊 CSV"))
    return out


async def _send_exports(send, results: List[dict], tag: str):
    fmts = {x.strip().lower() for x in get_setting("export").split(",") if x.strip()}
    for name, text, cap in build_exports(results, tag, fmts):
        b = io.BytesIO(text.encode('utf-8'))
        b.name = name
        await send(document=b, caption=cap)


# ---------------- jobs ----------------

# ---- short-lived scrape result cache (same URL+pages 8 min me dobara full scrape na ho) ----
_SCRAPE_CACHE: Dict[str, tuple] = {}  # key -> (ts, results_list, rep_dict)


def scrape_cache_key(url: str, start: int, end: int) -> str:
    return f"{normalize_domain(url)}|{urlparse(url).path}|{start}-{end}"


def scrape_cache_get(url: str, start: int, end: int, max_age: int = 480):
    k = scrape_cache_key(url, start, end)
    hit = _SCRAPE_CACHE.get(k)
    if not hit:
        return None
    ts, results, rep = hit
    if time.time() - ts > max_age:
        _SCRAPE_CACHE.pop(k, None)
        return None
    return results, rep


def scrape_cache_set(url: str, start: int, end: int, results, rep):
    if not results:
        return
    if len(_SCRAPE_CACHE) > 80:
        # drop oldest
        oldest = sorted(_SCRAPE_CACHE.items(), key=lambda kv: kv[1][0])[:20]
        for k, _ in oldest:
            _SCRAPE_CACHE.pop(k, None)
    _SCRAPE_CACHE[scrape_cache_key(url, start, end)] = (time.time(), results, rep)


JOBS: Dict[int, dict] = {}
_JOB_SEQ = [0]


def _uid_of(x) -> int:
    u = getattr(x, "effective_user", None) or getattr(x, "from_user", None)
    return u.id if u else 0


def job_start(uid: int, kind: str, url: str, a: int, b: int) -> int:
    _JOB_SEQ[0] += 1
    JOBS[_JOB_SEQ[0]] = {"user": uid, "kind": kind, "url": url, "pages": f"{a}-{b}", "t0": time.time()}
    return _JOB_SEQ[0]


def job_end(jid: int):
    JOBS.pop(jid, None)


async def run_scrape_chunk(update_or_query, context, target_url: str, start_page: int, end_page: int):
    uid = _uid_of(update_or_query)
    STOP_PROCESS[uid] = False
    jid = job_start(uid, "scrape", target_url, start_page, end_page)
    try:
        await _run_scrape_chunk_impl(update_or_query, context, target_url, start_page, end_page)
    finally:
        job_end(jid)


async def scr_run(chat, context, user_id: int, url: str, start: int, end: int):
    jid = job_start(user_id, "scr", url, start, end)
    try:
        await _scr_run_impl(chat, context, user_id, url, start, end)
    finally:
        job_end(jid)


async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_allowed(uid):
        return
    mine = {j: v for j, v in JOBS.items() if uid == ADMIN_ID or v["user"] == uid}
    if not mine:
        await update.message.reply_text("📭 Abhi koi job nahi chal raha.")
        return
    lines = ["🧵 Chalte hue jobs:"]
    for j, v in mine.items():
        lines.append(f"#{j} | {v['kind']} | pages {v['pages']} | {int(time.time() - v['t0'])}s | {normalize_domain(v['url'])}"
                     + (f" | user {v['user']}" if uid == ADMIN_ID else ""))
    lines.append("\nRokne ke liye: /cancel  (admin: /cancel all)")
    await update.message.reply_text("\n".join(lines))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_allowed(uid):
        return
    if uid == ADMIN_ID and context.args and context.args[0].lower() == "all":
        for v in JOBS.values():
            STOP_PROCESS[v["user"]] = True
        await update.message.reply_text("🛑 Sabhi jobs ko stop request bhej di.")
        return
    STOP_PROCESS[uid] = True
    await update.message.reply_text("🛑 Tumhare jobs ko stop request bhej di.")


# ---------------- /settings ----------------
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if args and args[0].lower() == "reset":
        reset_settings()
        await update.message.reply_text("♻️ Settings default par aa gayi.")
        return
    if args:
        key = args[0].lower()
        val = " ".join(args[1:]).strip()
        if key not in _SETTING_DEFAULTS:
            await update.message.reply_text(f"❌ Unknown setting: {key}")
            return
        if val.lower() in ("off", "none", "clear", "-", "0") and key in ("include", "exclude", "export", "min_quality"):
            val = "0" if key == "min_quality" else ""
        elif key in ("verify", "ytdlp", "keep_preview"):
            val = "1" if val.lower() in ("on", "1", "yes", "true") else "0"
        elif key == "min_quality":
            if not val.isdigit():
                await update.message.reply_text("❌ min_quality number do (jaise 720) ya off")
                return
        elif key == "proxy":
            val = val.lower() if val.lower() in ("auto", "on", "off") else "auto"
        elif key == "export":
            val = ",".join(x for x in re.split(r'[,\s]+', val.lower()) if x in ("m3u", "json", "csv"))
        else:
            val = val.lower()
        set_setting(key, val)
    show = lambda k: get_setting(k) or "-"
    await update.message.reply_text(
        "⚙️ Settings\n\n"
        f"• verify (dead link check): {'ON' if get_setting('verify') == '1' else 'OFF'}\n"
        f"• ytdlp (fallback extractor): {'ON' if get_setting('ytdlp') != '0' else 'OFF'}"
        f"{'' if yt_dlp else '  (yt-dlp install nahi hai)'}\n"
        f"• keep_preview (sirf 0.5s clip wale items bhi rakho): {'ON' if get_setting('keep_preview') == '1' else 'OFF'}\n"
        f"• proxy (player ko bot ke through stream: auto/on/off): {get_setting('proxy') or 'auto'} | PROXY_BASE: {PROXY_BASE or 'SET NAHI'}\n"
        f"• min_quality: {get_setting('min_quality')}\n"
        f"• include (title me ye words): {show('include')}\n"
        f"• exclude (title me ye words nahi): {show('exclude')}\n"
        f"• export (extra files): {show('export')}\n\n"
        "Badalne ke liye:\n"
        "/settings verify on\n/settings ytdlp off\n/settings min_quality 720\n"
        "/settings include mom,step   (comma se alag)\n/settings exclude gay\n"
        "/settings export m3u,json,csv\n/settings include off   (clear)\n/settings reset")


# ---------------- backup / restore ----------------
def db_snapshot(path: str):
    src = sqlite3.connect(DB_FILE)
    dst = sqlite3.connect(path)
    src.backup(dst)
    dst.close()
    src.close()


async def _send_backup(bot, chat_id: int, caption: str):
    tmp = os.path.join(tempfile.gettempdir(), f"bot_data_{int(time.time())}.db")
    try:
        db_snapshot(tmp)
        with open(tmp, "rb") as f:
            await bot.send_document(chat_id=chat_id, document=f, filename="bot_data.db", caption=caption)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await _send_backup(context.bot, update.effective_chat.id,
                       "💾 DB backup (cookies, rules, sites, watches).\nRestore: ye file bhejo, caption me: restore")


async def restore_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if "restore" not in (update.message.caption or "").lower():
        await update.message.reply_text("ℹ️ DB restore ke liye file ke saath caption me likho: restore")
        return
    doc = update.message.document
    tmp = os.path.join(tempfile.gettempdir(), f"restore_{int(time.time())}.db")
    try:
        f = await context.bot.get_file(doc.file_id)
        await f.download_to_drive(tmp)
        c = sqlite3.connect(tmp)
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.close()
        if not {"allowed_users", "site_cookies"} <= names:
            raise ValueError("ye is bot ka DB nahi lagta")
        src = sqlite3.connect(tmp)
        dst = sqlite3.connect(DB_FILE)
        src.backup(dst)
        dst.close()
        src.close()
        init_db()
        globals()["_RULES_CACHE"] = None
        globals()["_COOKIE_CACHE"] = None
        load_settings()
        _FETCH_CACHE.clear()
        await update.message.reply_text(
            f"✅ Restore ho gaya.\n🍪 Cookies: {len(list_cookie_domains())} | 🧩 Rules: {len(list_rule_domains())} | "
            f"👁 Watches: {len(watch_rows())}")
    except Exception as e:
        await update.message.reply_text(f"❌ Restore fail: {str(e)[:150]}")
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


async def _backup_loop(app):
    try:
        hrs = float(os.getenv("BACKUP_HOURS", "6"))
    except ValueError:
        hrs = 6
    if hrs <= 0:
        return
    while True:
        await asyncio.sleep(hrs * 3600)
        try:
            await _send_backup(app.bot, ADMIN_ID, "💾 Auto backup (restore: ye file bhejo, caption me: restore)")
        except Exception as e:
            logger.error(f"auto backup error: {e}")


# ---------------- watch: auto-monitor ----------------
def watch_add(uid: int, url: str, minutes: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO watches (user_id, url, minutes, last_run, baselined) VALUES (?, ?, ?, 0, 0)",
                (uid, url, minutes))
    wid = cur.lastrowid
    conn.commit()
    conn.close()
    return wid


def watch_rows(uid: Optional[int] = None) -> List[tuple]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    if uid is None:
        cur.execute("SELECT id, user_id, url, minutes, last_run, baselined FROM watches ORDER BY id")
    else:
        cur.execute("SELECT id, user_id, url, minutes, last_run, baselined FROM watches WHERE user_id=? ORDER BY id", (uid,))
    rows = cur.fetchall()
    conn.close()
    return rows


def watch_del(wid: int, uid: Optional[int] = None) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    if uid is None:
        cur.execute("DELETE FROM watches WHERE id=?", (wid,))
    else:
        cur.execute("DELETE FROM watches WHERE id=? AND user_id=?", (wid, uid))
    ok = cur.rowcount > 0
    if ok:
        cur.execute("DELETE FROM watch_seen WHERE watch_id=?", (wid,))
    conn.commit()
    conn.close()
    return ok


async def _watch_run(app, wid: int, uid: int, url: str, baselined: int):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE watches SET last_run=? WHERE id=?", (time.time(), wid))
    conn.commit()
    conn.close()
    results, rep = await scr_scrape(url, 1, 1, -abs(uid) - 1)      # pseudo user id: /stop se na ruke
    if not results:
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT page_url FROM watch_seen WHERE watch_id=?", (wid,))
    seen = {r[0] for r in cur.fetchall()}
    new = [r for r in results if r["page_url"] not in seen]
    cur.executemany("INSERT OR IGNORE INTO watch_seen (watch_id, page_url) VALUES (?, ?)",
                    [(wid, r["page_url"]) for r in results])
    if not baselined:
        cur.execute("UPDATE watches SET baselined=1 WHERE id=?", (wid,))
    conn.commit()
    conn.close()
    if not baselined:
        await app.bot.send_message(uid, f"👁 Watch #{wid} shuru: {len(results)} purane videos ignore kiye, "
                                        f"naye aate hi bhejunga.\n{url}", disable_web_page_preview=True)
        return
    if not new:
        return
    head = f"🆕 Watch #{wid}: {len(new)} naye video\n{url}\n\n"
    body = "".join(f"{it['title'][:80]}\n{it['download_link']}\n\n" for it in new[:8])
    await app.bot.send_message(uid, (head + body)[:4000], disable_web_page_preview=True)
    if len(new) > 8:
        b = io.BytesIO("".join(f"{it['title']}: {it['download_link']}\n" for it in new).encode('utf-8'))
        b.name = f"watch_{wid}_new.txt"
        await app.bot.send_document(uid, document=b, caption=f"📁 Watch #{wid}: saare {len(new)} naye links")


async def _watch_loop(app):
    await asyncio.sleep(45)
    while True:
        try:
            for wid, uid, url, minutes, last, baselined in watch_rows():
                if time.time() - (last or 0) >= minutes * 60:
                    try:
                        await _watch_run(app, wid, uid, url, baselined)
                    except Exception as e:
                        logger.error(f"watch {wid} error: {e}")
        except Exception as e:
            logger.error(f"watch loop error: {e}")
        await asyncio.sleep(60)


async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/watch <listing url> [minutes]  -> naye videos aate hi bot khud bhej dega."""
    uid = update.effective_user.id
    if not is_user_allowed(uid):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "👁 /watch <listing URL> [minutes]\nExample: /watch https://site.com/new 60\n"
            "Page 1 har N minute me check hota hai (min 10), naye videos aap ko mil jaate hain.\n"
            "/watchlist | /unwatch <id>")
        return
    m = re.search(r'https?://\S+', " ".join(args))
    if not m:
        await update.message.reply_text("❌ Valid URL do.")
        return
    url = m.group(0)
    mins = 60
    for a in args:
        if a.isdigit():
            mins = max(10, min(int(a), 1440))
    limit = 20 if uid == ADMIN_ID else 5
    if len(watch_rows(uid)) >= limit:
        await update.message.reply_text(f"❌ Max {limit} watches.")
        return
    wid = watch_add(uid, url, mins)
    await update.message.reply_text(f"✅ Watch #{wid} add: har {mins} min\n{url}\n(Pehli baar sirf baseline banega.)",
                                    disable_web_page_preview=True)


async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_allowed(uid):
        return
    rows = watch_rows(None if uid == ADMIN_ID else uid)
    if not rows:
        await update.message.reply_text("📭 Koi watch nahi hai. /watch <url>")
        return
    await update.message.reply_text("👁 Watches:\n" + "\n".join(
        f"#{w} | har {mi} min | {normalize_domain(u)}" + (f" | user {us}" if uid == ADMIN_ID else "")
        for w, us, u, mi, la, bl in rows), disable_web_page_preview=True)


async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_allowed(uid):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /unwatch <id>  (/watchlist se id dekho)")
        return
    ok = watch_del(int(context.args[0]), None if uid == ADMIN_ID else uid)
    await update.message.reply_text("🗑 Watch hata di." if ok else "ℹ️ Aisi watch nahi mili.")


# ---------------- Supabase Storage sync: Render restart par bhi cookies/rules/settings bache rahein ----------------
def _sb_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip('"\'').strip()


def _sb_base(u: str) -> str:
    """https://xxxx.supabase.co/rest/v1/ jaisa kuch bhi paste ho to sirf https://xxxx.supabase.co rakho."""
    pu = urlparse(u)
    return f"{pu.scheme}://{pu.netloc}" if pu.scheme and pu.netloc else u.rstrip("/")


SB_URL = _sb_base(_sb_env("SUPABASE_URL"))
SB_KEY = _sb_env("SUPABASE_KEY")                              # service_role key (secret!)
SB_BUCKET = _sb_env("SUPABASE_BUCKET", "bot-backup")
SB_OBJECT = _sb_env("SUPABASE_OBJECT", "bot_data.db")
_SB_LAST = {"hash": None, "ok": None}


def sb_enabled() -> bool:
    return bool(SB_URL and SB_KEY)


def _sb_headers(extra: Optional[dict] = None) -> dict:
    h = {"Authorization": f"Bearer {SB_KEY}", "apikey": SB_KEY}
    h.update(extra or {})
    return h


def sb_create_bucket() -> str:
    """Bucket na ho to private bucket bana do (service_role key chahiye). -> 'ok' ya error text."""
    try:
        r = requests.post(f"{SB_URL}/storage/v1/bucket", timeout=30, headers=_sb_headers(),
                          json={"id": SB_BUCKET, "name": SB_BUCKET, "public": False})
        if r.status_code in (200, 201) or r.status_code == 409 or "already exists" in r.text.lower():
            return "ok"
        return f"HTTP {r.status_code}: {r.text[:110]}"
    except Exception as e:
        return f"error: {str(e)[:100]}"


def _sb_bucket_hint(why: str) -> str:
    return (f"Bucket '{SB_BUCKET}' nahi mila aur bot ban bhi nahi paya ({why}).\n"
            f"Fix: Supabase -> Storage -> New bucket -> naam bilkul '{SB_BUCKET}' (Private) banao, "
            f"ya SUPABASE_BUCKET me apne bucket ka sahi naam do.\n"
            f"SUPABASE_KEY 'service_role' key honi chahiye (anon key se bucket nahi banta).")


def sb_upload_db() -> str:
    """DB ka consistent snapshot Supabase Storage me upload (upsert). -> 'ok' ya error text."""
    tmp = os.path.join(tempfile.gettempdir(), f"sb_up_{int(time.time())}.db")
    try:
        db_snapshot(tmp)
        data = open(tmp, "rb").read()
        h = hashlib.md5(data).hexdigest()
        if h == _SB_LAST["hash"]:
            return "unchanged"
        up = f"{SB_URL}/storage/v1/object/{SB_BUCKET}/{SB_OBJECT}"
        hd = _sb_headers({"Content-Type": "application/octet-stream", "x-upsert": "true"})
        r = requests.post(up, data=data, timeout=60, headers=hd)
        if "bucket not found" in r.text.lower():            # bucket nahi hai -> khud banao, phir ek baar retry
            cr = sb_create_bucket()
            if cr != "ok":
                return _sb_bucket_hint(cr)
            r = requests.post(up, data=data, timeout=60, headers=hd)
        if r.status_code in (200, 201):
            _SB_LAST["hash"] = h
            _SB_LAST["ok"] = time.time()
            return "ok"
        return f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return f"error: {str(e)[:120]}"
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def sb_download_db() -> str:
    """Supabase se DB laakar local DB_FILE me daalo (valid SQLite ho tabhi). -> 'ok' ya error text."""
    tmp = os.path.join(tempfile.gettempdir(), f"sb_down_{int(time.time())}.db")
    try:
        r = requests.get(f"{SB_URL}/storage/v1/object/authenticated/{SB_BUCKET}/{SB_OBJECT}",
                         headers=_sb_headers(), timeout=60)
        if r.status_code != 200:
            low = r.text.lower()
            if "bucket not found" in low:
                return f"bucket '{SB_BUCKET}' nahi mila (pehle /sbsync chalao, wo bana dega)"
            if "not found" in low or r.status_code == 404:
                return "abhi tak koi backup upload nahi hua"
            return f"HTTP {r.status_code}: {r.text[:100]}"
        open(tmp, "wb").write(r.content)
        c = sqlite3.connect(tmp)
        names = {x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.close()
        if not {"allowed_users", "site_cookies"} <= names:
            return "downloaded file is not this bot's DB"
        src = sqlite3.connect(tmp)
        dst = sqlite3.connect(DB_FILE)
        src.backup(dst)
        dst.close()
        src.close()
        _SB_LAST["hash"] = hashlib.md5(r.content).hexdigest()
        globals()["_COOKIE_CACHE"] = None
        globals()["_RULES_CACHE"] = None
        return "ok"
    except Exception as e:
        return f"error: {str(e)[:120]}"
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


async def _sb_loop(app):
    if not sb_enabled():
        return
    try:
        mins = max(1.0, float(os.getenv("SB_SYNC_MINUTES", "3")))
    except ValueError:
        mins = 3.0
    while True:
        await asyncio.sleep(mins * 60)
        res = await asyncio.to_thread(sb_upload_db)
        if res not in ("ok", "unchanged"):
            logger.error(f"Supabase sync fail: {res}")


async def sbsync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sbsync -> abhi upload.   /sbrestore -> Supabase se DB wapas lo."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not sb_enabled():
        await update.message.reply_text("ℹ️ Supabase set nahi hai. Env: SUPABASE_URL, SUPABASE_KEY, SUPABASE_BUCKET")
        return
    _SB_LAST["hash"] = None
    res = await asyncio.to_thread(sb_upload_db)
    await update.message.reply_text(f"☁️ Supabase upload: {res}\n"
                                    f"📦 {SB_URL} | bucket: {SB_BUCKET} | file: {SB_OBJECT}")


async def sbrestore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not sb_enabled():
        await update.message.reply_text("ℹ️ Supabase set nahi hai.")
        return
    res = await asyncio.to_thread(sb_download_db)
    if res == "ok":
        init_db()
        globals()["_RULES_CACHE"] = None
        globals()["_COOKIE_CACHE"] = None
        load_settings()
        _FETCH_CACHE.clear()
        await update.message.reply_text(f"☁️ Restore ho gaya. 🍪 {len(list_cookie_domains())} cookies | "
                                        f"🧩 {len(list_rule_domains())} rules | 👁 {len(watch_rows())} watches")
    else:
        await update.message.reply_text(f"❌ Supabase restore fail: {res}")


# ---------------- /sniff (diagnosis) aur /prefer (asli video host batao) ----------------
def _fmt_stream(u: str, size: Optional[int], page_url: str) -> str:
    tag = []
    if _PREVIEW_PATH_RX.search(urlparse(u).path.lower()):
        tag.append("PREVIEW?")
    if is_preferred(u, page_url):
        tag.append("PREFER")
    if stream_iplock(u):
        tag.append("IP-lock")
    sz = "?" if size is None else ("DEAD/HTML" if size == 0 else f"{size / 1_048_576:.1f}MB")
    return f"[{sz}{' ' + ','.join(tag) if tag else ''}] {u[:105]}"


def page_census(html: str, page_url: str) -> List[str]:
    """Page ke andar kya-kya hai: hosts, video tags, JS variables, endpoints, prefer-host ka pata."""
    t = html.replace('\\/', '/')
    low = t.lower()
    out: List[str] = []
    hosts = Counter(urlparse(u).netloc.lower() for u in re.findall(r'https?://[^\s"\'<>\\)]+', t))
    if hosts:
        out.append("🌍 Hosts: " + ", ".join(f"{h}×{c}" for h, c in hosts.most_common(8)))
    tags = {k: low.count("<" + k) for k in ("video", "source", "iframe", "embed", "object", "audio")}
    out.append("🏷 Tags: " + (", ".join(f"{k}={v}" for k, v in tags.items() if v) or "koi video/iframe tag nahi"))
    vals = []
    for m in re.finditer(r'<(video|source|iframe|embed)\b([^>]*)>', t, re.I):
        for am in re.finditer(r'\b(src|data-src|data-lazy-src|data-video|data-url|poster)=["\']([^"\']+)["\']', m.group(2), re.I):
            vals.append(f"  <{m.group(1).lower()} {am.group(1)}> {am.group(2)[:85]}")
    out += vals[:6]
    seen, n = set(), 0
    for m in re.finditer(r'["\']?([\w\-]*(?:video|player|source|stream|file|src|url|mp4|hls|embed)[\w\-]*)["\']?\s*[:=]\s*["\']([^"\']{12,300})["\']', t, re.I):
        name, val = m.group(1), m.group(2)
        if _ASSET_RX.search(val) or val in seen or val.startswith("data:"):
            continue
        if not (val.startswith(("http", "/")) or len(val) > 24):
            continue
        seen.add(val)
        out.append(f"  var {name[:22]} = {val[:80]}")
        n += 1
        if n >= 6:
            break
    eps: List[str] = []
    for m in re.finditer(r'["\']((?:https?:)?//[^"\'\s<>]+|/[A-Za-z0-9_\-./]+(?:\?[^"\'\s<>]{0,60})?)["\']', t):
        v = m.group(1)
        if _ASSET_RX.search(v) or v in eps:
            continue
        if re.search(r'(ajax|/api/|player|embed|get_?video|source|stream|/play|\.php|\.json|token|sign)', v, re.I):
            eps.append(v)
    if eps:
        out.append("🔌 Endpoints (page me):")
        out += [f"  {e[:100]}" for e in eps[:8]]
    das = []
    for m in re.finditer(r'\b(data-[\w-]*(?:video|src|url|file|embed|player|stream|mp4|hls|sign|token)[\w-]*)=["\']([^"\']{8,200})["\']', t, re.I):
        das.append(f"  {m.group(1)}={m.group(2)[:70]}")
    out += list(dict.fromkeys(das))[:5]
    for h in (_PREFER.get(_root_host(urlparse(page_url).netloc)) or []):
        c = low.count(h)
        if c:
            i = low.find(h)
            out.append(f"⭐ '{h}' page me {c} baar, e.g.: ...{t[max(0, i - 50):i + 90]}...")
        else:
            out.append(f"⭐ '{h}' page ke HTML me KAHIN NAHI -> JS/XHR/click ke baad aata hoga (browser log neeche dekho)")
    return out


async def _send_report(update: Update, st, lines: List[str]):
    text = "\n".join(lines)
    if len(text) <= 3900:
        await st.edit_text(text, disable_web_page_preview=True)
        return
    await st.edit_text(text[:3500] + "\n... (poori report file me bhej raha hoon)", disable_web_page_preview=True)
    chat = getattr(update, "effective_chat", None)
    if chat:
        b = io.BytesIO(text.encode("utf-8"))
        b.name = "sniff_report.txt"
        await chat.send_document(document=b, caption="🔬 Poori /sniff report")


async def sniff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sniff <page URL> -> page me stream kahan-kahan hai (static/packed/iframe/api/yt-dlp/browser) + final pick.
    Listing page di to pehla video page khud kholta hai."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("🔬 /sniff <video page URL>\nBatata hai asli video link kahan milti hai (ya kyun nahi milti).")
        return
    url = context.args[0]
    st = await update.message.reply_text("🔬 Sniff chal raha hai (kuch sec)...")
    html = await fetch(url)
    if not html:
        await st.edit_text(f"❌ Page fetch fail: HTTP {LAST_STATUS.get(url, '?')} | {LAST_ERR.get(url, '?')}\n{err_hint(url)}")
        return
    lines: List[str] = []
    static = await asyncio.to_thread(_quick_streams, html, url, 12)
    sus = [u for u in static if _PREVIEW_PATH_RX.search(urlparse(u).path.lower())]
    links = find_video_links(html, url)
    if (not _looks_like_single_video(url) and len(links) >= 6 and len(static) >= 4 and len(sus) >= 0.8 * len(static)):
        lines.append(f"📋 Ye LISTING page hai ({len(static)} preview clips, {len(links)} video links). "
                     f"Pehla video page sniff kar raha hoon:\n   {links[0]}")
        listing = url
        url = links[0]
        html = await fetch(url, referer=listing)
        if not html:
            lines.append(f"❌ Video page fetch fail: HTTP {LAST_STATUS.get(url, '?')} | {LAST_ERR.get(url, '?')}")
            await _send_report(update, st, lines)
            return
        static = await asyncio.to_thread(_quick_streams, html, url, 12)
    lines.append(f"📄 {len(html)} bytes | {url[:100]}")
    sizes = dict(zip(static[:6], await asyncio.gather(*[asyncio.to_thread(probe_size_sync, u, url) for u in static[:6]])))
    lines.append(f"🎞 Static streams ({len(static)}):")
    lines += [f"  {_fmt_stream(u, sizes.get(u), url)}" for u in static[:6]]
    lines += page_census(html, url)
    for name, fn in _DIG_STAGES:
        urls, note, log = [], "", []
        try:
            if name == "browser" and async_playwright:
                h2, bs = await asyncio.wait_for(pw_render(url, wait=4, play=True, netlog=log), timeout=55)
                urls = list(bs) + (_quick_streams(h2, url) if h2 else [])
                urls = list(dict.fromkeys(urls))
                note = f"browser: {len(urls)} stream(s), {len(log)} network entries"
            else:
                urls, note = await asyncio.wait_for(fn(html, url), timeout=50)
        except Exception as e:
            note = f"error {str(e)[:50]}"
        lines.append(f"▶ {name}: {note}")
        top = [u for u in urls if u not in static][:3]
        szs = await asyncio.gather(*[asyncio.to_thread(probe_size_sync, u, url) for u in top])
        lines += [f"  {_fmt_stream(u, z, url)}" for u, z in zip(top, szs)]
        if log:
            lines.append("🌐 Browser network (xhr/media/iframe):")
            lines += [f"  [{rt} {stt} {ct or '-'}] {u[:92]}" for rt, stt, ct, u in log[:14]]
    pref = _PREFER.get(_root_host(urlparse(url).netloc))
    lines.append(f"⭐ Prefer hosts: {', '.join(pref) if pref else '-'}")
    _STREAM_CACHE.pop(url, None)
    reset_host_stats(url)
    item = await extract_video_link(url, source_page=url)
    if item:
        lines.append(f"✅ FINAL: {_fmt_stream(item['download_link'], None, url)}")
        lines.append("⚠️ Status: sirf preview mili" if item.get("preview_only") else "👍 Status: asli video lagti hai")
    else:
        lines.append("❌ FINAL: kuch nahi mila")
    await _send_report(update, st, lines)


def domainOf_safe(u: str) -> str:
    try:
        return urlparse(u).netloc
    except Exception:
        return ""


async def prefer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/prefer <site> <asli video link ya host>  |  /prefer <site> off  |  /prefer (list)"""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        rows = sorted(_PREFER.items())
        await update.message.reply_text(
            "⭐ /prefer <site> <asli video ki link ya host>\n"
            "Example: /prefer rusvideos.love https://ebacdn.net/videos_ssd3/pornhub/x/y.mp4?...\n"
            "Us site ke pages me us host ki link ko hamesha upar rakha jata hai.\n"
            "Hatane ke liye: /prefer <site> off\n\nAbhi: " + ("\n".join(f"• {d}: {', '.join(h)}" for d, h in rows) if rows else "koi nahi"))
        return
    dom = _root_host(normalize_domain(args[0]))
    cur = list(_PREFER.get(dom, []))
    if len(args) == 1:
        await update.message.reply_text(f"⭐ {dom}: {', '.join(cur) if cur else 'koi prefer host nahi'}")
        return
    if args[1].lower() in ("off", "clear", "none", "-"):
        save_prefer(dom, [])
        await update.message.reply_text(f"🗑 {dom} ka prefer host hata diya.")
        return
    raw = args[1]
    host = (urlparse(raw).netloc if "://" in raw else raw).lower().split(':')[0].strip()
    if host.startswith("www."):
        host = host[4:]
    if not DOMAIN_RE.match(host):
        await update.message.reply_text(f"❌ Valid host/link nahi: {raw[:60]}")
        return
    if host not in cur:
        cur.append(host)
    save_prefer(dom, cur)
    await update.message.reply_text(f"⭐ {dom}: ab {', '.join(cur)} wali links ko priority milegi.\nTest: /sniff <video page URL>")


async def _post_init(app):
    global MAIN_LOOP, MINI_BOT
    MAIN_LOOP = asyncio.get_running_loop()
    MINI_BOT = app.bot
    try:
        globals()["MINI_BOT_USERNAME"] = app.bot.username or ""
    except Exception:
        pass
    if PROXY_BASE and MenuButtonWebApp is not None:
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Player", web_app=WebAppInfo(url=PROXY_BASE + "/app")))
        except Exception as e:
            logger.warning(f"menu button set fail: {e}")
    _ensure_fast_executor()
    app.bot_data["tasks"] = [asyncio.create_task(_watch_loop(app)), asyncio.create_task(_backup_loop(app)),
                             asyncio.create_task(_sb_loop(app))]



# ==========================================================
# EXTRA ADMIN / UTILITY COMMANDS (add-only)
#   /restart  /cleanup_backups  /updatewithoutrestart
#   aliases: /adscr=/addscr  /mini=/app  /commands=/help
# ==========================================================
async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/restart — bot process soft-restart (admin). Polling dubara start."""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "♻️ Restart request...\n"
        "• DB / cookies / rules disk par safe hain\n"
        "• Process exit → Render/host auto-restart karega\n"
        "• Local: process manager se dobara chalao")
    logger.warning(f"Admin {ADMIN_ID} ne /restart trigger kiya")
    # graceful: stop jobs, then exit so supervisor restarts
    for v in list(JOBS.values()):
        STOP_PROCESS[v.get("user", 0)] = True
    async def _die():
        await asyncio.sleep(1.2)
        os._exit(0)
    asyncio.create_task(_die())


async def cleanup_backups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cleanup_backups — temp DB snapshots, purani health rows, expire shares saaf."""
    if update.effective_user.id != ADMIN_ID:
        return
    removed_files = 0
    notes = []
    # temp bot_data_*.db / sb_*.db / restore_*.db
    try:
        import glob
        patterns = [
            os.path.join(tempfile.gettempdir(), "bot_data_*.db"),
            os.path.join(tempfile.gettempdir(), "sb_up_*.db"),
            os.path.join(tempfile.gettempdir(), "sb_down_*.db"),
            os.path.join(tempfile.gettempdir(), "restore_*.db"),
        ]
        for pat in patterns:
            for f in glob.glob(pat):
                try:
                    os.remove(f)
                    removed_files += 1
                except Exception:
                    pass
        notes.append(f"🗑 Temp DB files: {removed_files}")
    except Exception as e:
        notes.append(f"Temp clean error: {e}")

    # old site_health (>30d already in record_health; force 14d trim)
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.execute("DELETE FROM site_health WHERE ts < ?", (time.time() - 14 * 86400,))
        hdel = cur.rowcount
        # expired mini_shares
        cur = conn.execute("DELETE FROM mini_shares WHERE expires IS NOT NULL AND expires < ?", (time.time(),))
        sdel = cur.rowcount
        # orphan watch_seen (watch deleted)
        cur = conn.execute(
            "DELETE FROM watch_seen WHERE watch_id NOT IN (SELECT id FROM watches)")
        wdel = cur.rowcount
        conn.commit()
        conn.close()
        notes.append(f"🩺 site_health rows: {hdel}")
        notes.append(f"🔗 expired shares: {sdel}")
        notes.append(f"👁 orphan watch_seen: {wdel}")
    except Exception as e:
        notes.append(f"DB clean error: {e}")

    # in-memory caches
    try:
        n1 = len(globals().get("_FETCH_CACHE", {}))
        n2 = len(globals().get("_STREAM_CACHE", {}))
        n3 = len(globals().get("_SCRAPE_CACHE", {}))
        globals().get("_FETCH_CACHE", {}).clear()
        globals().get("_STREAM_CACHE", {}).clear()
        globals().get("_SCRAPE_CACHE", {}).clear()
        globals()["_COOKIE_CACHE"] = None
        globals()["_RULES_CACHE"] = None
        notes.append(f"⚡ Caches cleared (fetch={n1}, stream={n2}, scrape={n3})")
    except Exception as e:
        notes.append(f"Cache clear: {e}")

    await update.message.reply_text("✅ Cleanup done:\n" + "\n".join(notes))


async def updatewithoutrestart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/updatewithoutrestart — DB se settings/cookies/rules/prefers reload, process mat maro."""
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        init_db()
        load_settings()
        load_prefers()
        load_env_cookies()
        globals()["_COOKIE_CACHE"] = None
        globals()["_RULES_CACHE"] = None
        globals().get("_FETCH_CACHE", {}).clear()
        globals().get("_STREAM_CACHE", {}).clear()
        if "_SCRAPE_CACHE" in globals():
            globals()["_SCRAPE_CACHE"].clear()
        msg = (
            "✅ Live reload OK (bina restart):\n"
            f"• Cookies: {len(list_cookie_domains())}\n"
            f"• Rules: {len(list_rule_domains())}\n"
            f"• Users: {len(get_all_users())}\n"
            f"• Sites: {len(get_all_sites())}\n"
            f"• Watches: {len(watch_rows())}\n"
            f"• Settings: {', '.join(k+'='+(get_setting(k) or '-') for k in list(_SETTING_DEFAULTS)[:6])}...\n\n"
            "Naya Python code deploy ke liye ab bhi host restart chahiye.\n"
            "Sirf DB/env data is command se turant apply."
        )
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Reload fail: {str(e)[:200]}")


async def adscr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias: /adscr -> /addscr"""
    return await addscr_command(update, context)


async def mini_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias: /mini -> /app (Mini App)"""
    return await app_command(update, context)


async def commands_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias: /commands -> /help"""
    return await help_command(update, context)



def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN environment variable set nahi hai. "
                         "Naya token BotFather se lo aur env me BOT_TOKEN=... rakho.")
    if sb_enabled() and (not os.path.exists(DB_FILE) or os.getenv("SB_FORCE_RESTORE") == "1"):
        _r = sb_download_db()                       # Render restart ke baad DB wapas
        logger.info(f"Supabase restore at startup: {_r}")
    init_db()
    load_settings()
    load_env_cookies()
    threading.Thread(target=run_dummy_server, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()

    app = (ApplicationBuilder().token(BOT_TOKEN)
           .concurrent_updates(True).post_init(_post_init).build())

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("turbo", turbo_command))
    app.add_handler(CommandHandler("app", app_command))
    app.add_handler(CommandHandler("health", health_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CommandHandler("dump", dump_command))
    app.add_handler(CommandHandler("site", site_command))
    app.add_handler(CommandHandler("scr", scr_command))
    app.add_handler(CommandHandler("addsite", addsite_command))
    app.add_handler(CommandHandler("addscr", addscr_command))
    app.add_handler(CommandHandler("delscr", delscr_command))
    app.add_handler(CommandHandler("removesite", removesite_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("logout", logout_command))
    app.add_handler(CommandHandler("cookie", cookie_command))
    app.add_handler(CommandHandler("updatecookie", updatecookie_command))
    app.add_handler(CommandHandler("sky", sky_command))
    app.add_handler(CommandHandler("sniff", sniff_command))
    app.add_handler(CommandHandler("prefer", prefer_command))
    app.add_handler(CommandHandler("sbsync", sbsync_command))
    app.add_handler(CommandHandler("sbrestore", sbrestore_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("jobs", jobs_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("watch", watch_command))
    app.add_handler(CommandHandler("watchlist", watchlist_command))
    app.add_handler(CommandHandler("unwatch", unwatch_command))

    app.add_handler(CommandHandler("adduser", adduser_command))
    app.add_handler(CommandHandler("removeuser", removeuser_command))
    app.add_handler(CommandHandler("userlist", userlist_command))

    app.add_handler(CommandHandler("restart", restart_command))
    app.add_handler(CommandHandler("cleanup_backups", cleanup_backups_command))
    app.add_handler(CommandHandler("updatewithoutrestart", updatewithoutrestart_command))
    app.add_handler(CommandHandler("adscr", adscr_command))
    app.add_handler(CommandHandler("mini", mini_command))
    app.add_handler(CommandHandler("commands", commands_command))


    # /scr buttons MUST be registered before the generic callback handler
    app.add_handler(CallbackQueryHandler(scr_callback, pattern=r"^scr_"))
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.Document.FileExtension("db"), restore_document))
    app.add_handler(MessageHandler(filters.Document.FileExtension("m3u"), sky_only_document))
    app.add_handler(MessageHandler(filters.Document.TXT, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"curl_cffi: {'ON' if cffi_requests else 'OFF (pip install curl_cffi)'} | "
                f"Proxy: {'ON' if PROXY_URL else 'OFF'}")
    print("🤖 43-Site Dedicated Extractor & Web App Bot Running!")
    app.run_polling()

if __name__ == "__main__":
    main()
