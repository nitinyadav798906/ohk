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
DB_FILE = "bot_data.db"
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
    conn.commit()
    conn.close()

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

def set_cookie_db(domain: str, cookie: str):
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
    r'channels?|pornstars?|models?|actors?|studios?|sites?|dmca|contact|terms|'
    r'privacy|2257|upload|premium|history|favorites|page|blog|about|faq)(/|$)',
    re.I)

JUNK = re.compile(
    r'(preview|trailer|thumb|poster|sprite|\.vtt|logo|banner|/ads?/|adserver|'
    r'blank\.mp4|teaser)', re.I)

LAST_STATUS: Dict[str, int] = {}
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


def _cffi_sess():
    s = getattr(_TL, "cffi", None)
    if s is None:
        s = cffi_requests.Session(impersonate="chrome124")
        _TL.cffi = s
    return s


def fetch_sync(url: str, referer: Optional[str] = None) -> Optional[str]:
    headers = make_headers(url, referer)
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    engines = {}
    if cffi_requests:  # best against Cloudflare (real Chrome TLS fingerprint)
        engines["curl_cffi"] = lambda: _cffi_sess().get(url, headers=headers, timeout=15, proxies=proxies)
    engines["cloudscraper"] = lambda: scraper.get(url, headers=headers, timeout=15, proxies=proxies)
    engines["requests"] = lambda: requests.get(url, headers=headers, timeout=15, proxies=proxies)

    root = _root_host(urlparse(url).netloc)
    order = list(engines)
    pref = _BEST_ENGINE.get(root)
    if pref in engines:
        order.remove(pref)
        order.insert(0, pref)

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
                LAST_DETAIL[url] = ""
                _BEST_ENGINE[root] = name
                return r.text
            detail.append(f"{name}:{r.status_code}")
            logger.warning(f"fetch {url} -> HTTP {r.status_code} ({name})")
        except Exception as e:
            detail.append(f"{name}:error")
            logger.warning(f"fetch {url} error ({name}): {e}")
    LAST_DETAIL[url] = ", ".join(detail)
    return None


async def fetch(url: str, referer: Optional[str] = None) -> Optional[str]:
    return await asyncio.to_thread(fetch_sync, url, referer)


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
        full = urljoin(page_url, href).split('#')[0]
        pu = urlparse(full)
        if pu.scheme not in ('http', 'https'):
            return
        if _root_host(pu.netloc) != root:
            return
        if pu.path in ('', '/') or full.rstrip('/') == page_url.rstrip('/'):
            return
        if pu.path.lower().endswith(SKIP_EXT) or BAD_PATH.search(pu.path):
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
    path = urlparse(url).path
    if path in ('', '/'):
        return False
    return bool(re.search(
        r'/video\.|/video\d+|/videos?/[^/]+-\d+|/post/\d+|/watch/|/v/|/film/|'
        r'/view_video|\.html?$', path))


async def build_page_urls(url: str, start: int, end: int) -> List[str]:
    host = urlparse(url).netloc.lower()
    base = url.rstrip('/')

    if "xvideos" in host:  # 0-indexed pages
        tmpl = (url + "&p={p}") if '?' in url else (base + "/{p}")
        return [url if p == 1 else tmpl.replace('{p}', str(p - 1))
                for p in range(start, end + 1)]

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

    if '?' in url:
        templates.append(url + "&page={p}")
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
    r'((?:https?:)?(?:\\?/){2}[^\s"\'<>]+?\.(?:m3u8|mp4)(?:\?[^\s"\'<>]*)?)',
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


def _rank(u: str) -> float:
    s = 0.0
    low = u.lower()
    path = low.split('?')[0]
    if '.m3u8' in path:                      # .mp4.m3u8 ab sahi se HLS count hoga
        s += 3 if 'xhcdn' in low else 1      # xhamster CDN par HLS hi chalta hai
    elif '.mp4' in path or 'get_file' in low:
        s += 2
    q = re.search(r'(\d{3,4})p', low)
    if q:
        s += int(q.group(1)) / 10000
    return -s


async def generic_extract(html: str, page_url: str, depth: int = 0) -> Optional[str]:
    cands = _collect_candidates(html, page_url)
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
        return {"ok": False, "report":
                "❌ Koi video page sample nahi mila.\n"
                "Listing me links nahi mile ya video pages fetch nahi hue.\n"
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
    try:
        text = await fast_fetch(video_url, referer=source_page or None)
        if not text:
            return None

        title = "Video"
        title_match = re.search(r'<h1[^>]*>(.*?)</h1>', text, re.IGNORECASE | re.DOTALL)
        if not title_match:
            title_match = re.search(r'<title>(.*?)</title>', text, re.IGNORECASE | re.DOTALL)

        if title_match:
            title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
            title = re.sub(r'\s+', ' ', title)

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

        elif "xhamster" in domain:
            xh_link = xh_best_stream(text)
            if xh_link:
                stream_link = xh_link
            else:
                xh_match = re.search(r'"m3u8":\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                           re.search(r'"mp4":\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                           re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text)
                if xh_match: stream_link = xh_match.group(1).replace('\\/', '/')

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

        if stream_link:
            final_link = process_tpl_link(stream_link) if ".m3u8" in stream_link else stream_link

            if ".pdf" in final_link.lower(): file_type = "PDF"
            elif any(ext in final_link.lower() for ext in ['.mp3', '.wav', '.m4a', '.aac']): file_type = "AUDIO"
            elif any(ext in final_link.lower() for ext in ['.jpg', '.png', '.jpeg', '.webp']): file_type = "IMAGE"

            return {
                "title": title,
                "type": file_type,
                "page_url": video_url,
                "source_page": source_page or video_url,
                "download_link": final_link
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
async def scrape_multi_pages_chunk(url: str, start_page: int = 1, end_page: int = 10) -> List[dict]:
    global LAST_REPORT
    _ensure_fast_executor()
    rep = {"pages_ok": 0, "pages_fail": [], "links": 0, "extracted": 0}
    LAST_REPORT = rep

    if _looks_like_single_video(url):
        res = await extract_video_link(url, source_page=url)
        if res:
            rep["extracted"] = 1
            return [res]
        # extraction failed -> maybe it was actually a listing page, continue below

    page_urls = await build_page_urls(url, start_page, end_page)
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

    semaphore = asyncio.Semaphore(SCR_CONCURRENCY)

    async def sem_extract(v_url, src_p):
        async with semaphore:
            return await extract_video_link(v_url, source_page=src_p)

    results = [r for r in await asyncio.gather(
        *[sem_extract(v, s) for v, s in url_to_source.items()]) if r]
    rep["extracted"] = len(results)
    return results

# ==========================================================
# HTML WEB APP GENERATOR
# ==========================================================
def generate_web_app_html(results: List[dict], title: str = "Scraped Video Web Player") -> str:
    js_playlist = []
    items_html = ""

    v_c = sum(1 for x in results if x.get('type') == 'VIDEO')
    a_c = sum(1 for x in results if x.get('type') == 'AUDIO')
    p_c = sum(1 for x in results if x.get('type') == 'PDF')
    i_c = sum(1 for x in results if x.get('type') == 'IMAGE')
    raw_lines = results

    for idx, item in enumerate(results):
        js_playlist.append({
            "name": item['title'],
            "url": item['download_link'],
            "type": item['type'],
            "poster": "https://images.unsplash.com/photo-1574375927938-d5a98e8ffe85?w=500&q=80"
        })

        icon = "🎬"
        if item['type'] == 'PDF': icon = "📄"
        elif item['type'] == 'AUDIO': icon = "🎵"
        elif item['type'] == 'IMAGE': icon = "🖼"

        safe_title = _html.escape(item['title'])
        items_html += f"""
        <div class="list-item" id="item-{idx}" data-type="{item['type']}" onclick="openCinema({idx})">
            <div class="item-icon-box">{icon}</div>
            <div class="item-info">
                <div class="item-title">{safe_title}</div>
                <div class="item-meta">
                    <span class="meta-tag tag-{item['type']}">{item['type']}</span>
                    <span id="list-fav-{idx}" style="display:none; color:var(--red);">❤️ Fav</span>
                </div>
            </div>
        </div>"""

    # "</script>" ya "</" title me aaye to page na toote
    playlist_json = json.dumps(js_playlist).replace("</", "<\\/")
    safe_page_title = _html.escape(title)

    login_html = ""
    security_script = ""
    if SKY_PASSWORD:
        login_html = f"""
        <div id="login-screen" style="display:flex;">
            <div class="login-box">
                <h3 style="margin-top:0;">Protected Access</h3>
                <input type="password" id="passInput" placeholder="Enter Password">
                <button onclick="checkPass()">Unlock Player</button>
                <p id="errMsg" style="color:red; font-size:12px; margin-top:10px;"></p>
            </div>
        </div>"""
    else:
        security_script = "document.getElementById('app-wrapper').style.display = 'block';"

    html_template = f"""<!DOCTYPE html>
<html lang="en" data-theme="dark" data-color="blue">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
    <title>{safe_page_title}</title>
    <link rel="stylesheet" href="https://cdn.plyr.io/3.7.8/plyr.css" />
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{ --red: #ef4444; --green: #10b981; --orange: #f59e0b; }}
        [data-theme="dark"] {{ --bg: #0f172a; --card-bg: #1e293b; --text: #f8fafc; --text-sec: #94a3b8; --border: #334155; --modal-bg: #000; }}
        [data-theme="light"] {{ --bg: #f8fafc; --card-bg: #ffffff; --text: #1e293b; --text-sec: #64748b; --border: #e2e8f0; --modal-bg: #fff; }}

        [data-color="blue"] {{ --primary: #3b82f6; }}
        [data-color="red"] {{ --primary: #ef4444; }}
        [data-color="green"] {{ --primary: #22c55e; }}
        [data-color="purple"] {{ --primary: #a855f7; }}
        [data-color="orange"] {{ --primary: #f97316; }}
        [data-color="pink"] {{ --primary: #ec4899; }}
        [data-color="cyan"] {{ --primary: #06b6d4; }}

        body {{ font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); margin: 0; padding-bottom: 80px; transition: 0.3s; }}
        * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
        #app-wrapper {{ display: none; }}

        #login-screen {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: var(--bg); z-index: 9999; display: none; justify-content: center; align-items: center; }}
        .login-box {{ background: var(--card-bg); padding: 25px; border-radius: 12px; width: 85%; max-width: 300px; border: 1px solid var(--border); text-align: center; }}
        .login-box input {{ width: 100%; padding: 12px; margin-bottom: 15px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg); color: var(--text); outline: none; }}
        .login-box button {{ width: 100%; padding: 12px; background: var(--primary); color: white; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; }}

        .header {{ background: var(--card-bg); padding: 15px; position: sticky; top: 0; z-index: 50; border-bottom: 1px solid var(--border); }}
        .h-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
        .h-title {{ margin: 0; font-size: 16px; font-weight: 700; color: var(--primary); }}
        .right-actions {{ display: flex; align-items: center; gap: 12px; }}
        .tg-link {{ color: white; background: var(--primary); text-decoration: none; font-size: 11px; font-weight: bold; padding: 5px 12px; border-radius: 20px; }}
        .mode-btn {{ cursor: pointer; font-size: 18px; }}

        .theme-row {{ display: flex; gap: 8px; overflow-x: auto; padding-bottom: 5px; }}
        .t-dot {{ width: 22px; height: 22px; border-radius: 50%; cursor: pointer; border: 2px solid transparent; transition: 0.2s; flex-shrink: 0; }}
        .t-dot:hover {{ transform: scale(1.2); }}

        .stats-container {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; padding: 15px; }}
        .stat-card {{ background: var(--card-bg); padding: 10px 5px; border-radius: 8px; text-align: center; cursor: pointer; border: 1px solid var(--border); transition: 0.2s; }}
        .stat-num {{ font-size: 14px; font-weight: 800; display: block; }}
        .stat-label {{ font-size: 9px; font-weight: 600; text-transform: uppercase; margin-top: 2px; color: var(--text-sec); }}
        .sc-fav {{ color: var(--red); border-color: var(--red); }}
        .sc-vid {{ color: var(--primary); }}
        .sc-aud {{ color: var(--orange); }}
        .sc-pdf {{ color: var(--green); }}

        .list-container {{ padding: 0 15px; }}
        .search-box {{ display: flex; align-items: center; background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 10px; }}
        .search-bar {{ width: 100%; padding: 12px; border: none; background: transparent; color: var(--text); outline: none; }}
        .clear-search {{ padding: 0 12px; cursor: pointer; display: none; color: var(--text-sec); }}
        .list-item {{ background: var(--card-bg); margin-bottom: 8px; border-radius: 8px; padding: 12px; display: flex; align-items: center; border: 1px solid var(--border); cursor: pointer; }}
        .item-icon-box {{ width: 40px; height: 40px; background: rgba(100,100,100,0.1); border-radius: 8px; display: flex; justify-content: center; align-items: center; margin-right: 12px; font-size: 18px; }}

        .item-info {{ flex-grow: 1; min-width: 0; }}
        .item-title {{ font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 4px; }}
        .item-meta {{ display: flex; align-items: center; gap: 8px; }}
        .meta-tag {{ font-size: 9px; padding: 2px 6px; border-radius: 4px; font-weight: bold; background: rgba(100,100,100,0.1); }}
        .tag-VIDEO {{ color: var(--primary); }} .tag-PDF {{ color: var(--green); }} .tag-AUDIO {{ color: var(--orange); }}

        .cinema-modal, .player-overlay {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: #000; z-index: 3000; }}
        .player-overlay {{ z-index: 4000; background: black; flex-direction: column; }}

        .bg-layer {{ position: absolute; top: 0; left: 0; width: 100%; height: 60%; background-size: cover; background-position: center; mask-image: linear-gradient(to bottom, black 20%, transparent 100%); -webkit-mask-image: linear-gradient(to bottom, black 20%, transparent 100%); opacity: 0.6; }}
        .cinema-content {{ position: absolute; bottom: 0; width: 100%; height: 60%; padding: 20px; background: linear-gradient(to top, #000 20%, transparent); display: flex; flex-direction: column; justify-content: flex-end; align-items: center; gap: 15px; }}
        .c-poster {{ width: 120px; height: 180px; border-radius: 8px; object-fit: cover; box-shadow: 0 5px 20px black; border: 1px solid rgba(255,255,255,0.2); }}
        .c-title {{ font-size: 20px; font-weight: 800; color: white; text-align: center; margin: 0; }}
        .action-btn {{ width: 100%; padding: 14px; border-radius: 8px; font-size: 15px; font-weight: 700; border: none; cursor: pointer; }}
        .btn-main {{ background: var(--primary); color: white; }}
        .btn-sub {{ background: rgba(255,255,255,0.15); color: white; border: 1px solid rgba(255,255,255,0.2); backdrop-filter: blur(5px); }}

        .watermark {{ position: absolute; top: 15px; right: 60px; color: rgba(255,255,255,0.4); font-weight: 900; font-size: 16px; pointer-events: none; z-index: 55; text-shadow: 0 2px 5px black; }}
        .red-bar-box {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; z-index: 50; display: flex; justify-content: center; align-items: center; }}
        .red-bar {{ width: 50px; height: 0%; background: linear-gradient(to top, rgba(255,0,0,0.8), transparent); box-shadow: 0 0 40px #ff0000; opacity: 0; transition: height 0.1s; border-radius: 20px; }}
        .gesture-val {{ position: absolute; color: white; font-weight: bold; font-size: 30px; opacity: 0; z-index: 60; top: 40%; left: 50%; transform: translateX(-50%); text-shadow: 0 0 10px black; }}

        .player-header {{ position: absolute; top: 0; width: 100%; padding: 15px; display: flex; justify-content: space-between; z-index: 50; background: linear-gradient(to bottom, rgba(0,0,0,0.8), transparent); align-items: center; }}
        .player-mid {{ flex-grow: 1; position: relative; display: flex; align-items: center; justify-content: center; width: 100%; }}
        .bottom-controls {{ background: #000; padding: 15px; display: flex; justify-content: center; gap: 8px; border-top: 1px solid #222; flex-wrap: wrap; z-index: 60; }}
        .ctrl-btn {{ background: #222; color: white; border: none; padding: 8px 14px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; }}
        .ctrl-next {{ background: var(--primary); color: white; }}

        .settings-menu {{ position: absolute; top: 60px; right: 20px; background: rgba(20,20,20,0.95); border: 1px solid #333; border-radius: 8px; padding: 15px; z-index: 100; display: none; flex-direction: column; gap: 10px; width: 220px; backdrop-filter: blur(10px); }}
        .sm-item {{ display: flex; flex-direction: column; gap: 5px; }}

        .sm-label {{ font-size: 12px; color: #aaa; text-transform: uppercase; }}
        .sm-select {{ background: #333; color: white; border: none; padding: 8px; border-radius: 4px; font-size: 14px; }}
        .clean-btn {{ background: #ef4444; color: white; border: none; padding: 8px; width: 100%; border-radius: 4px; font-weight: bold; cursor: pointer; margin-top: 5px; }}
        .lock-icon {{ position: absolute; bottom: 30px; right: 20px; color: white; background: rgba(255,255,255,0.2); padding: 12px; border-radius: 50%; cursor: pointer; z-index: 65; font-size: 18px; }}

        body.minimized .player-overlay {{ width: 320px !important; height: 180px !important; top: auto !important; left: auto !important; bottom: 20px !important; right: 20px !important; border-radius: 12px; border: 2px solid var(--primary); box-shadow: 0 10px 40px rgba(0,0,0,0.5); }}
        body.minimized .bottom-controls, body.minimized .settings-menu, body.minimized .lock-icon, body.minimized .watermark, body.minimized .red-bar-box, body.minimized .gesture-val {{ display: none !important; }}
        body.minimized .player-header {{ padding: 5px; }}
        body.minimized #pTitle {{ font-size: 10px; white-space: nowrap; }}
        body.minimized .player-mid {{ pointer-events: none; }}

        .pdf-frame {{ width: 100%; height: 100%; border: none; background: white; }}
        .img-view {{ width: 100%; height: 100%; object-fit: contain; }}
        .footer {{ text-align: center; padding: 20px; color: var(--text-sec); font-size: 11px; }}
        #toast {{ position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,0.8); color: white; padding: 8px 16px; border-radius: 20px; font-size: 12px; z-index: 5000; display: none; }}
    </style>
</head>
<body>
    {login_html}

    <div id="app-wrapper">
        <div class="header">
            <div class="h-top">
                <div class="h-title">{safe_page_title}</div>
                <div class="right-actions">
                    <a href="{TELEGRAM_LINK}" target="_blank" class="tg-link">✈ Join TG</a>
                    <span class="mode-btn" onclick="toggleMode()">🌓</span>
                </div>
            </div>
            <div class="theme-row">
                <div class="t-dot" style="background:#3b82f6" onclick="setTheme('blue')"></div>
                <div class="t-dot" style="background:#ef4444" onclick="setTheme('red')"></div>
                <div class="t-dot" style="background:#22c55e" onclick="setTheme('green')"></div>
                <div class="t-dot" style="background:#a855f7" onclick="setTheme('purple')"></div>
                <div class="t-dot" style="background:#f97316" onclick="setTheme('orange')"></div>
                <div class="t-dot" style="background:#ec4899" onclick="setTheme('pink')"></div>
                <div class="t-dot" style="background:#06b6d4" onclick="setTheme('cyan')"></div>
            </div>
        </div>

        <div class="stats-container">
            <div class="stat-card" onclick="filterList('all')"><span class="stat-num">{len(raw_lines)}</span><span class="stat-label">All</span></div>
            <div class="stat-card sc-fav" onclick="filterList('FAV')"><span class="stat-num" id="favCount">-</span><span class="stat-label">❤️ Favs</span></div>
            <div class="stat-card sc-vid" onclick="filterList('VIDEO')"><span class="stat-num">{v_c}</span><span class="stat-label">Video</span></div>
            <div class="stat-card sc-aud" onclick="filterList('AUDIO')"><span class="stat-num">{a_c}</span><span class="stat-label">Audio</span></div>
            <div class="stat-card" onclick="filterList('PDF')"><span class="stat-num" style="color:var(--green)">{p_c}</span><span class="stat-label">PDF</span></div>
            <div class="stat-card" onclick="filterList('IMAGE')"><span class="stat-num" style="color:var(--orange)">{i_c}</span><span class="stat-label">Img</span></div>
        </div>

        <div class="list-container">
            <div class="search-box">
                <input type="text" class="search-bar" id="searchInput" placeholder="Search..." onkeyup="searchList()">
                <span class="clear-search" onclick="clearSearch()">✕</span>
            </div>
            <div id="playlistContainer">{items_html}</div>
            <div class="footer">Credits: {BOT_OWNER_NAME}</div>
        </div>
    </div>

    <div id="cinemaModal" class="cinema-modal">
        <div onclick="closeCinema()" style="position:absolute; top:20px; left:20px; color:white; font-size:24px; z-index:60; cursor:pointer;">✕</div>
        <div class="bg-layer" id="bgLayer"></div>
        <div class="cinema-content">
            <img src="" class="c-poster" id="cPoster">
            <h1 class="c-title" id="cTitle">Title</h1>
            <div style="display:flex; gap:10px; font-size:12px; opacity:0.8;">
                <span style="background:rgba(255,255,255,0.2); padding:2px 6px; border-radius:4px;">HD</span>
                <span id="cType">VIDEO</span>
            </div>
            <div style="width:100%; display:flex; flex-direction:column; gap:10px;">
                <button class="action-btn btn-main" onclick="startPlayer()">▶ Watch Now</button>
                <button class="action-btn btn-sub" onclick="toggleFav('favBtn')" id="favBtn">❤️ Add to Favorites</button>
            </div>
        </div>
    </div>

    <div id="playerOverlay" class="player-overlay">
        <div class="red-bar-box"><div class="red-bar" id="redBar"></div></div>
        <div class="gesture-val" id="gVal">50%</div>
        <div class="watermark">{BOT_OWNER_NAME}</div>

        <div class="player-header">
            <div style="display:flex; align-items:center; gap:15px; width:70%;">
                <span style="color:white; font-weight:600; font-size:14px; overflow:hidden; white-space:nowrap; text-overflow:ellipsis;" id="pTitle">Player</span>
                <span onclick="toggleMinimize()" style="color:white; cursor:pointer; font-size:18px;">📉</span>
            </div>
            <div style="display:flex; gap:20px;">
                <span onclick="toggleSettings()" style="color:white; font-size:20px; cursor:pointer;">⚙️</span>
                <span onclick="closePlayer()" style="color:white; font-size:24px; cursor:pointer;">✕</span>
            </div>
        </div>

        <div id="settingsMenu" class="settings-menu">
            <div class="sm-item"><div class="sm-label">Speed</div>
                <select class="sm-select" onchange="changeSpeed(this.value)">
                    <option value="0.5">0.5x</option><option value="1" selected>1x</option><option value="1.5">1.5x</option><option value="2">2x</option><option value="3">3x</option><option value="4">4x</option>
                </select>
            </div>
            <div class="sm-item"><div class="sm-label">Quality</div>
                <select class="sm-select" id="qualitySelect" onchange="changeQuality(this.value)"><option value="-1">Auto</option></select>
            </div>
            <div class="sm-item" style="border-top:1px solid #444; padding-top:10px; margin-top:5px;">
                <button class="clean-btn" onclick="cleanAllData()">🗑️ Clean All Data</button>
            </div>
        </div>

        <div class="player-mid" id="gestureArea" onclick="if(document.body.classList.contains('minimized')) toggleMinimize()">
            <div class="lock-icon" onclick="toggleLock(); event.stopPropagation();">🔓</div>
            <video id="player" playsinline controls style="width:100%; max-height:100%;"></video>
            <iframe id="pdfFrame" class="pdf-frame" style="display:none;"></iframe>
            <img id="imgView" class="img-view" style="display:none;">
        </div>

        <div class="bottom-controls" id="extControls">
            <button class="ctrl-btn" onclick="seek(-10)">⏪ 10s</button>
            <button class="ctrl-btn" onclick="seek(10)">10s ⏩</button>
            <button class="ctrl-btn" onclick="showToast('GIF Mode: ON')">GIF</button>
            <button class="ctrl-btn" onclick="showToast('CC: Enabled')">CC</button>
            <button class="ctrl-btn ctrl-next" onclick="playNext()">Next ⏭</button>
            <button class="ctrl-btn" onclick="downloadCurrent()">⬇ DL</button>
            <button class="ctrl-btn" onclick="toggleFav('pFavBtn')" id="pFavBtn">🤍 Fav</button>
        </div>
    </div>

    <div id="toast">Alert</div>

    <script src="https://cdn.plyr.io/3.7.8/plyr.polyfilled.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <script>
        function toggleMode() {{
            const current = document.documentElement.getAttribute('data-theme');
            const next = current === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem('uTheme', next);
        }}
        function setTheme(color) {{
            document.documentElement.setAttribute('data-color', color);
            localStorage.setItem('uColor', color);
        }}
        document.documentElement.setAttribute('data-theme', localStorage.getItem('uTheme') || 'dark');
        document.documentElement.setAttribute('data-color', localStorage.getItem('uColor') || 'blue');

        function checkPass() {{
            if(document.getElementById('passInput').value === "{SKY_PASSWORD}") {{
                document.getElementById('login-screen').style.display = 'none';
                document.getElementById('app-wrapper').style.display = 'block';
            }} else document.getElementById('errMsg').innerText = "Incorrect Password!";
        }}
        {security_script}

        const playlist = {playlist_json};
        let currentIndex = -1;
        let hls = new Hls();
        let isLocked = false;

        const player = new Plyr('#player', {{
            controls: ['play-large', 'play', 'progress', 'current-time', 'mute', 'settings', 'fullscreen'],
            hideControls: true, speed: {{ selected: 1, options: [0.5, 1, 1.5, 2, 3, 4] }}
        }});

        player.on('ended', () => playNext());

        window.onload = function() {{
            updateFavCount();
            playlist.forEach((item, idx) => {{
                if(localStorage.getItem('fav_' + item.url)) document.getElementById('list-fav-' + idx).style.display = 'inline';
            }});
        }};

        function cleanAllData() {{
            if(confirm("Clear all Watch History & Favorites?")) {{
                localStorage.clear();
                location.reload();
            }}
        }}

        function openCinema(idx) {{
            currentIndex = idx;
            const item = playlist[idx];
            document.getElementById('bgLayer').style.backgroundImage = `url('${{item.poster}}')`;
            document.getElementById('cPoster').src = item.poster;
            document.getElementById('cTitle').innerText = item.name;
            document.getElementById('cType').innerText = item.type;
            updateFavBtn('favBtn');
            document.getElementById('cinemaModal').style.display = 'block';
        }}

        function closeCinema() {{ document.getElementById('cinemaModal').style.display = 'none'; }}

        function startPlayer() {{
            document.getElementById('cinemaModal').style.display = 'none';
            document.getElementById('playerOverlay').style.display = 'flex';
            document.getElementById('pTitle').innerText = playlist[currentIndex].name;
            updateFavBtn('pFavBtn');

            const item = playlist[currentIndex];
            const v = document.getElementById('player');
            const p = document.getElementById('pdfFrame');
            const i = document.getElementById('imgView');
            v.style.display='none'; p.style.display='none'; i.style.display='none';
            document.getElementById('settingsMenu').style.display = 'none';

            if(item.type === 'VIDEO' || item.type === 'AUDIO') {{
                v.style.display='block';
                if(Hls.isSupported() && item.url.includes('.m3u8')) {{
                    hls.loadSource(item.url); hls.attachMedia(v);
                    hls.on(Hls.Events.MANIFEST_PARSED, () => {{
                        const qSel = document.getElementById('qualitySelect');
                        qSel.innerHTML = '<option value="-1">Auto</option>';
                        hls.levels.forEach((l, idx) => {{ qSel.innerHTML += `<option value="${{idx}}">${{l.height}}p</option>`; }});
                    }});
                }} else {{ v.src = item.url; }}
                player.play();
            }} else if(item.type === 'PDF') {{
                p.style.display='block';
                p.src = "https://docs.google.com/gview?embedded=true&url=" + encodeURIComponent(item.url);
            }} else if(item.type === 'IMAGE') {{
                i.style.display='block'; i.src = item.url;
            }} else {{ window.open(item.url, '_blank'); closePlayer(); }}
        }}

        function closePlayer() {{
            player.pause();
            document.getElementById('playerOverlay').style.display = 'none';
            document.body.classList.remove('minimized');
        }}

        let startY = 0;
        const area = document.getElementById('gestureArea');
        const redBar = document.getElementById('redBar');
        const gVal = document.getElementById('gVal');

        area.addEventListener('touchstart', (e) => {{ if(!isLocked) startY = e.touches[0].clientY; }});
        area.addEventListener('touchmove', (e) => {{
            if(isLocked) return;
            e.preventDefault();
            const delta = startY - e.touches[0].clientY;
            redBar.style.opacity = '1';
            let h = Math.abs(delta) * 0.5; if(h>100) h=100;
            redBar.style.height = h + "%";
            gVal.style.opacity = '1';
            if(e.touches[0].clientX > window.innerWidth / 2) {{
                let change = delta / 500;
                let newVol = Math.min(Math.max(player.volume + change, 0), 1);
                player.volume = newVol;
                gVal.innerText = "Vol: " + Math.round(newVol * 100) + "%";
            }}
        }});
        area.addEventListener('touchend', () => {{ redBar.style.opacity = '0'; gVal.style.opacity = '0'; }});

        function toggleSettings() {{
            const menu = document.getElementById('settingsMenu');
            menu.style.display = (menu.style.display === 'flex') ? 'none' : 'flex';
        }}
        function changeSpeed(val) {{ player.speed = parseFloat(val); }}
        function changeQuality(val) {{ hls.currentLevel = parseInt(val); }}
        function seek(s) {{ player.currentTime += s; }}
        function playNext() {{ if(currentIndex+1 < playlist.length) {{ currentIndex++; startPlayer(); }} }}
        function downloadCurrent() {{ window.open(playlist[currentIndex].url, '_blank'); }}
        function toggleLock() {{
            isLocked = !isLocked;
            document.querySelector('.lock-icon').innerText = isLocked ? '🔒' : '🔓';
            document.getElementById('extControls').style.display = isLocked ? 'none' : 'flex';
        }}
        function toggleMinimize() {{ document.body.classList.toggle('minimized'); }}

        function toggleFav(btnId) {{
            const url = playlist[currentIndex].url;
            if(localStorage.getItem('fav_'+url)) {{
                localStorage.removeItem('fav_'+url);
                document.getElementById('list-fav-' + currentIndex).style.display = 'none';
            }} else {{
                localStorage.setItem('fav_'+url, 'true');
                document.getElementById('list-fav-' + currentIndex).style.display = 'inline';
            }}
            updateFavBtn(btnId);
            updateFavCount();
        }}
        function updateFavBtn(btnId) {{
            const url = playlist[currentIndex].url;
            const btn = document.getElementById(btnId);
            const isFav = localStorage.getItem('fav_'+url);
            if(btnId === 'favBtn') btn.innerText = isFav ? "✓ Added" : "❤️ Add to Favorites";
            else btn.innerText = isFav ? "❤️ Saved" : "🤍 Fav";
        }}
        function updateFavCount() {{
            let c = 0;
            playlist.forEach(i => {{ if(localStorage.getItem('fav_'+i.url)) c++; }});
            document.getElementById('favCount').innerText = c;
        }}
        function filterList(t) {{
            document.querySelectorAll('.list-item').forEach(e => {{
                let show = false;
                if(t === 'all') show = true;
                else if(t === 'FAV') {{
                    const idx = e.id.split('-')[1];
                    if(localStorage.getItem('fav_' + playlist[idx].url)) show = true;
                }}
                else if(e.getAttribute('data-type') === t) show = true;
                e.style.display = show ? 'flex' : 'none';
            }});
        }}
        function searchList() {{
            const v = document.getElementById('searchInput').value.toLowerCase();
            document.querySelector('.clear-search').style.display = v ? 'block' : 'none';
            document.querySelectorAll('.list-item').forEach(e => e.style.display = e.innerText.toLowerCase().includes(v) ? 'flex' : 'none');
        }}
        function clearSearch() {{
            document.getElementById('searchInput').value = '';
            searchList();
        }}
        function showToast(msg) {{
            const t = document.getElementById('toast');
            t.innerText = msg; t.style.display = 'block';
            setTimeout(() => t.style.display = 'none', 2000);
        }}
    </script>
</body>
</html>
"""
    return html_template

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
        "3. FFmpeg Downloader: Upload .txt file to auto-download & send video.\n\n"
        "🛠️ Commands: /site, /scr, /addsite, /addscr, /delscr, /removesite, /login, /logout, /stop, /stats, /userlist, /debug, /dump"
    )

async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        add_user_db(uid)
        await update.message.reply_text(f"✅ User `{uid}` added.", parse_mode="Markdown")

async def removeuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        remove_user_db(uid)
        await update.message.reply_text(f"🗑 User `{uid}` removed.", parse_mode="Markdown")

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
    await update.message.reply_text(
        f"📊 Bot Status:\n\n"
        f"• Authorized Users: {users_count}\n"
        f"• Dedicated Site Extractors: 43 Sites Active\n"
        f"• Engine Status: 24/7 Active 🟢"
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
    html = await fetch(url)
    if not html:
        await update.message.reply_text(
            f"❌ Fetch failed. HTTP: {LAST_STATUS.get(url, '?')}\n"
            f"🔧 Engines: {LAST_DETAIL.get(url, '?')}\n"
            f"curl_cffi installed: {'YES' if cffi_requests else 'NO'} | "
            f"Proxy set: {'YES' if PROXY_URL else 'NO'}\n\n"
            "403/503 = Cloudflare/IP block | 404 = wrong URL | ? = timeout/DNS")
        return

    lines = [f"✅ Fetched {len(html)} bytes", link_stats(html, url)]
    links = find_video_links(html, url)
    lines.append(f"🔗 Video-like links on page: {len(links)}")
    lines += links[:3]
    if links:
        h = await fetch(links[0], referer=url)
        s = await generic_extract(h, links[0]) if h else None
        lines.append(f"▶ Extract test on 1st link: {s or 'FAILED'}")
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

async def save_cookie_flow(update: Update, domain: str, raw_cookie: str):
    """Saves cookie for a domain, deletes the user's message (it contains secrets) and tests the site."""
    cookie = clean_cookie(raw_cookie)
    try:
        await update.message.delete()
    except Exception:
        pass
    if not domain or '=' not in cookie:
        await update.effective_chat.send_message("❌ Invalid cookie. Format: name=value; name2=value2; ...")
        return
    set_cookie_db(domain, cookie)
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
        f"🔑 Signed in: {domain}\n🍪 Cookies saved: {n}\n{test}\n"
        f"🧹 Cookie wala message delete kar diya gaya.")

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
            keyboard.append([InlineKeyboardButton(f"🚪 Sign Out {d}", callback_data=f"signout:{d}"[:64])])
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

async def run_scrape_chunk(update_or_query, context, target_url: str, start_page: int, end_page: int):
    status_msg = await update_or_query.message.reply_text(f"⚡ Scraping Pages {start_page} to {end_page}...")

    try:
        results = await scrape_multi_pages_chunk(target_url, start_page=start_page, end_page=end_page)

        if not results:
            r = LAST_REPORT
            await status_msg.edit_text(
                f"❌ Pages {start_page} to {end_page} par koi video links nahi mile.\n\n"
                f"📄 Pages OK: {r['pages_ok']}\n"
                f"🚫 Failed: {r['pages_fail'][:3]}\n"
                f"🔗 Links found: {r['links']}\n"
                f"✅ Extracted: {r['extracted']}\n\n"
                f"Detail ke liye: /debug {target_url}\nRaw HTML ke liye: /dump {target_url}"
            )
            return

        await status_msg.edit_text(f"✅ Total {len(results)} Videos Extracted! 2 TXT aur 2 HTML files generate ho rahi hain...")

        # FILE 1: FULL DETAILS TXT
        txt_full_content = f"--- Scraped Video Links Full (Pages {start_page}-{end_page} | {len(results)} Items) ---\n\n"
        for idx, item in enumerate(results, 1):
            txt_full_content += f"{idx}. Title: {item['title']}\n"
            txt_full_content += f"   Source Listing Page: {item['source_page']}\n"
            txt_full_content += f"   Permanent Video Page: {item['page_url']}\n"
            txt_full_content += f"   Direct Stream Link: {item['download_link']}\n\n"

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
        if text.lower() == "cancel":
            await update.message.reply_text("❎ Sign in cancel ho gaya.")
            return
        await save_cookie_flow(update, pending_domain, text)
        return

    url_match = re.search(r'(https?://[^\s]+)', text)

    if not url_match:
        await update.message.reply_text("❌ Valid URL bhejein!")
        return

    target_url = url_match.group(1)
    await run_scrape_chunk(update, context, target_url, start_page=1, end_page=10)

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_user_allowed(query.from_user.id): return

    if query.data.startswith("signin:") or query.data.startswith("signout:"):
        if query.from_user.id != ADMIN_ID:
            return
        action, dom = query.data.split(":", 1)
        if action == "signin":
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
SCR_CONCURRENCY = 24        # parallel video-page extractions (fast)
SCR_PAGE_CONCURRENCY = 10   # parallel listing-page fetches
SCR_MAX_PAGES = 30          # max pages per single run
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
        asyncio.get_running_loop().set_default_executor(_TPE(max_workers=64))
        _EXECUTOR_READY = True


async def fast_fetch(url: str, referer: Optional[str] = None) -> Optional[str]:
    """fetch() + short in-memory cache (same page is never downloaded twice)."""
    hit = _FETCH_CACHE.get(url)
    if hit and time.time() - hit[0] < _SCR_CACHE_TTL:
        return hit[1]
    page = await fetch(url, referer)
    if page:
        if len(_FETCH_CACHE) > 300:
            _FETCH_CACHE.clear()
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


async def scr_scrape(url: str, start: int, end: int, user_id: int, progress=None):
    """Fast scraper: parallel pages + parallel extraction + cache + retry + self-heal."""
    _ensure_fast_executor()
    domain = normalize_domain(url)
    rep = {"pages_ok": 0, "pages_fail": [], "links": 0, "extracted": 0,
           "cached": 0, "healed": False, "diag": ""}
    first_html: Dict[str, str] = {}

    if _looks_like_single_video(url):
        r = await extract_video_link(url, source_page=url)
        if r:
            rep.update(links=1, extracted=1)
            return [r], rep

    page_urls = await build_page_urls(url, start, end)
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
        links = scr_find_links(page, pu)
        if pu == page_urls[0]:
            first_html["h"] = page
            if len(links) < 4:
                rep["diag"] = _scr_diag(page, pu, links)
        for l in links:
            url_to_source.setdefault(l, pu)

    await asyncio.gather(*[crawl(p) for p in page_urls])
    rep["links"] = len(url_to_source)

    results: Dict[str, dict] = {}
    if url_to_source:
        esem = asyncio.Semaphore(SCR_CONCURRENCY)
        total = len(url_to_source)
        state = {"done": 0, "last": 0.0}

        async def work(v: str, s: str):
            if STOP_PROCESS.get(user_id):
                return
            hit = _STREAM_CACHE.get(v)
            if hit:
                results[v] = hit
                rep["cached"] += 1
            else:
                async with esem:
                    r = await extract_video_link(v, source_page=s)
                    if not r and not STOP_PROCESS.get(user_id):
                        await asyncio.sleep(1)               # one quick retry
                        r = await extract_video_link(v, source_page=s)
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

    rep["extracted"] = len(ordered)
    return ordered, rep


async def scr_send_files(chat, results: List[dict], start: int, end: int, domain: str, url: str):
    tag = f"{domain}_p{start}_to_p{end}"
    full = f"--- {domain} | Pages {start}-{end} | {len(results)} Items ---\n\n"
    simple = f"--- {domain} Simple Links (Pages {start}-{end} | {len(results)} Items) ---\n\n"
    for i, it in enumerate(results, 1):
        full += (f"{i}. Title: {it['title']}\n   Source Listing Page: {it['source_page']}\n"
                 f"   Permanent Video Page: {it['page_url']}\n   Direct Stream Link: {it['download_link']}\n\n")
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

    await chat.send_document(document=mk(full, f"{tag}_full.txt"), caption=f"📁 Full TXT ({len(results)} links)")
    await chat.send_document(document=mk(simple, f"{tag}_simple.txt"), caption="📁 Simple TXT (Title: Stream Link)")
    await chat.send_document(
        document=mk(generate_web_app_html(results, title=f"{domain} ({start}-{end})"), f"{tag}_full.html"),
        caption="🌐 Full Web App HTML (Player UI)")
    await chat.send_document(
        document=mk(simple_html, f"{tag}_simple.html"),
        caption=f"🌐 Simple HTML\n\nAage ke pages ({nxt}-{nxt + 9}) ke liye button dabao:", reply_markup=kb)


async def scr_run(chat, context, user_id: int, url: str, start: int, end: int):
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
        results, rep = await scr_scrape(url, start, end, user_id, progress)
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
        msg += ("\n💡 Ye site JS se load hoti lagti hai. Browser DevTools -> Network -> XHR/Fetch me jo "
                "videos-list API URL dikhe wo bhejo, ya /dump " + url + " ki HTML file bhejo.")
        await status.edit_text(msg[:4000], disable_web_page_preview=True)
        return

    heal = " | 🩹 extractor auto-healed" if rep["healed"] else ""
    await status.edit_text(
        f"{learn}✅ {len(results)}/{rep['links']} extracted (cache: {rep['cached']}){heal}\nFiles bhej raha hoon...")
    context.user_data['scr_url'] = url
    context.user_data['scr_next'] = end + 1
    await scr_send_files(chat, results, start, end, domain, url)
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
            "/scr <url> 3-8      -> pages 3-8\n\n"
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
async def pw_render(url: str, wait: float = 4.0, scroll: bool = False):
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
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(int(wait * 1000))
            if scroll:
                for _ in range(3):
                    await page.mouse.wheel(0, 4000)
                    await page.wait_for_timeout(700)
            for sel in ("video", ".play", "[class*=play]", "button"):     # nudge lazy players
                if streams:
                    break
                try:
                    await page.click(sel, timeout=1200)
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass
            html = await page.content()
            return html, streams
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
# MAIN ENTRYPOINT
# ==========================================================
async def _post_init(app):
    _ensure_fast_executor()


def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN environment variable set nahi hai. "
                         "Naya token BotFather se lo aur env me BOT_TOKEN=... rakho.")
    init_db()
    threading.Thread(target=run_dummy_server, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()

    app = (ApplicationBuilder().token(BOT_TOKEN)
           .concurrent_updates(True).post_init(_post_init).build())

    app.add_handler(CommandHandler("start", start_command))
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

    app.add_handler(CommandHandler("adduser", adduser_command))
    app.add_handler(CommandHandler("removeuser", removeuser_command))
    app.add_handler(CommandHandler("userlist", userlist_command))

    # /scr buttons MUST be registered before the generic callback handler
    app.add_handler(CallbackQueryHandler(scr_callback, pattern=r"^scr_"))
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.Document.TXT, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"curl_cffi: {'ON' if cffi_requests else 'OFF (pip install curl_cffi)'} | "
                f"Proxy: {'ON' if PROXY_URL else 'OFF'}")
    print("🤖 43-Site Dedicated Extractor & Web App Bot Running!")
    app.run_polling()

if __name__ == "__main__":
    main()
