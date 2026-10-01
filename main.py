import asyncio
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
BOT_TOKEN = os.getenv("BOT_TOKEN", "7673015455:AAFW01HGes-gzQUg_1Fb6gKD2HlSTOZcG0Y")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1714266885"))
BOT_OWNER_NAME = os.getenv("BOT_OWNER_NAME", "@Mascotchlowa")
TELEGRAM_LINK = os.getenv("TELEGRAM_LINK", "https://t.me/Mascotchlowa")
SKY_PASSWORD = os.getenv("SKY_PASSWORD", "7989")
DB_FILE = "bot_data.db"

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
# NEW: ROBUST FETCH / LINK DISCOVERY / GENERIC EXTRACTOR
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
    return {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer or f"{p.scheme or 'https'}://{p.netloc}/",
    }


def fetch_sync(url: str, referer: Optional[str] = None) -> Optional[str]:
    headers = make_headers(url, referer)
    attempts = []
    if cffi_requests:  # best against Cloudflare (real Chrome TLS fingerprint)
        attempts.append(lambda: cffi_requests.get(
            url, headers=headers, impersonate="chrome124", timeout=25))
    attempts.append(lambda: scraper.get(url, headers=headers, timeout=25))
    attempts.append(lambda: requests.get(url, headers=headers, timeout=25))

    for fn in attempts:
        try:
            r = fn()
            LAST_STATUS[url] = r.status_code
            if r.status_code == 200 and len(r.text) > 500:
                return r.text
            logger.warning(f"fetch {url} -> HTTP {r.status_code}")
        except Exception as e:
            logger.warning(f"fetch {url} error: {e}")
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


def find_video_links(html: str, page_url: str) -> List[str]:
    host = urlparse(page_url).netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    items: Dict[str, tuple] = {}

    for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         html, re.I | re.S):
        href = m.group(1).strip().replace('&amp;', '&')
        inner = m.group(2)
        if href.startswith(('javascript:', '#', 'mailto:', 'tel:')):
            continue
        full = urljoin(page_url, href).split('#')[0]
        pu = urlparse(full)
        if pu.scheme not in ('http', 'https'):
            continue
        link_host = pu.netloc.lower()
        if link_host.startswith('www.'):
            link_host = link_host[4:]
        if link_host != host:
            continue
        if pu.path in ('', '/') or full.rstrip('/') == page_url.rstrip('/'):
            continue
        if pu.path.lower().endswith(SKIP_EXT) or BAD_PATH.search(pu.path):
            continue
        thumb = bool(re.search(r'<img\b|data-src|data-original|poster|background-image',
                               inner, re.I))
        old = items.get(full)
        items[full] = (_shape(pu), thumb or (old[1] if old else False))

    if not items:
        return []

    score, thumbs = Counter(), Counter()
    for sh, th in items.values():
        score[sh] += 1
        thumbs[sh] += int(th)
    top = max(score.values())

    good = {sh for sh, c in score.items()
            if c >= 3 and c >= 0.3 * top and thumbs[sh] >= 0.5 * c}
    if not good:
        good = {sh for sh, c in score.items() if c >= 3 and c >= 0.3 * top}
    if not good:  # tiny page: just use anything with a thumbnail
        return [u for u, (_, th) in items.items() if th]

    return [u for u, (sh, _) in items.items() if sh in good]


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

    html1 = await fetch(url)
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
    chosen = None
    for t in templates:
        u2 = t.replace('{p}', '2')
        h2 = await fetch(u2)
        if not h2:
            continue
        l2 = set(find_video_links(h2, u2))
        if l2 and l2 != p1_links:
            chosen = t
            break

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
    if '.mp4' in low or 'get_file' in low:
        s += 2
    elif '.m3u8' in low:
        s += 1
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

async def extract_video_link(video_url: str, source_page: str = "") -> Optional[dict]:
    try:
        text = await fetch(video_url, referer=source_page or None)
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

        # --------------------------------------------------
        # Site Specific Extractors (dedicated sites)
        # The newly added 20 sites now use the smart generic extractor below
        # (generic_extract) which handles <source>, file:, video_url (KVS),
        # //cdn links, escaped links and iframe embeds.
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
        html = await fetch(pu)
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

    semaphore = asyncio.Semaphore(5)

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

        items_html += f"""
        <div class="list-item" id="item-{idx}" data-type="{item['type']}" onclick="openCinema({idx})">
            <div class="item-icon-box">{icon}</div>
            <div class="item-info">
                <div class="item-title">{item['title']}</div>
                <div class="item-meta">
                    <span class="meta-tag tag-{item['type']}">{item['type']}</span>
                    <span id="list-fav-{idx}" style="display:none; color:var(--red);">❤️ Fav</span>
                </div>
            </div>
        </div>"""

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
    <title>{title}</title>
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
                <div class="h-title">{title}</div>
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

        const playlist = {json.dumps(js_playlist)};
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
        await update.message.reply_text("⛔ **Access Denied! Aap is bot ko use nahi kar sakte.**")
        return

    await update.message.reply_text(
        "⚡ **43-Site Dedicated Bulk Link Scraper Bot Active!**\n\n"
        "🌐 **43 Supported Dedicated Platforms Included!**\n\n"
        "📌 **Features:**\n"
        "1. **Full Web Player UI:** Custom Video & Media Player interface in HTML.\n"
        "2. **4 Files Export:** 2 TXT & 2 HTML Files (Full Web App + Simple List).\n"
        "3. **FFmpeg Downloader:** Upload `.txt` file to auto-download & send video.\n\n"
        "🛠️ **Commands:** `/stop`, `/stats`, `/userlist`"
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
        f"📊 **Bot Status:**\n\n"
        f"• **Authorized Users:** {users_count}\n"
        f"• **Dedicated Site Extractors:** 43 Sites Active\n"
        f"• **Engine Status:** 24/7 Active 🟢"
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STOP_PROCESS[update.effective_user.id] = True
    await update.message.reply_text("🛑 **Process Stop Request Sent!**")

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
            "403/503 = Cloudflare/IP block | 404 = wrong URL | ? = timeout/DNS")
        return

    lines = [f"✅ Fetched {len(html)} bytes"]
    links = find_video_links(html, url)
    lines.append(f"🔗 Video-like links on page: {len(links)}")
    lines += links[:3]
    if links:
        h = await fetch(links[0], referer=url)
        s = await generic_extract(h, links[0]) if h else None
        lines.append(f"▶ Extract test on 1st link: {s or 'FAILED'}")
    else:
        s = await generic_extract(html, url)
        lines.append(f"▶ Treated as single video page: {s or 'no stream found'}")
    await update.message.reply_text("\n".join(lines)[:4000], disable_web_page_preview=True)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id): return

    doc = update.message.document
    if not doc or not doc.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Valid `.txt` file upload karein.")
        return

    STOP_PROCESS[user_id] = False
    status_msg = await update.message.reply_text("📥 **TXT file reading started...**")

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
        await status_msg.edit_text(f"🚀 Total **{total}** links queued! Stop karne ke liye `/stop` bhejein.")

        for idx, raw_url in enumerate(urls, 1):
            if STOP_PROCESS.get(user_id, False):
                await update.message.reply_text("🛑 **Task Stopped By User!**")
                break

            progress_msg = await update.message.reply_text(f"⏳ **[{idx}/{total}] Processing...**")
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

            await progress_msg.edit_text(f"📥 **[{idx}/{total}] Downloading Video...**")
            success = await download_video_ffmpeg(stream_url, output_file)

            if success:
                await progress_msg.edit_text(f"📤 **[{idx}/{total}] Telegram Uploading...**")
                try:
                    with open(output_file, 'rb') as vf:
                        await update.message.reply_video(
                            video=vf, 
                            caption=f"🎥 **{video_title}**\n\n🔗 **Item {idx}/{total}**",
                            supports_streaming=True
                        )
                    await progress_msg.delete()
                except Exception as upload_err:
                    await progress_msg.edit_text(f"❌ Upload Error: {str(upload_err)}")
            else:
                await progress_msg.edit_text(f"❌ **[{idx}/{total}] Download Failed!**")

            if os.path.exists(output_file):
                try: os.remove(output_file)
                except Exception: pass

        await status_msg.edit_text("✅ **Processing completed!**")

    except Exception as e:
        logger.error(f"Error processing document: {e}")
        await status_msg.edit_text(f"❌ File Process Error: {str(e)}")

async def run_scrape_chunk(update_or_query, context, target_url: str, start_page: int, end_page: int):
    status_msg = await update_or_query.message.reply_text(f"⚡ **Scraping Pages {start_page} to {end_page}...**")

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
                f"Detail ke liye: /debug {target_url}"
            )
            return

        await status_msg.edit_text(f"✅ Total **{len(results)}** Videos Extracted! 2 TXT aur 2 HTML files generate ho rahi hain...")

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
<h3>{idx}. {item['title']} <span class="tag">{item['type']}</span></h3>
<p><strong>⚡ Stream URL:</strong> <a href="{item['download_link']}" target="_blank">{item['download_link']}</a></p>
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

        await update_or_query.message.reply_document(document=txt_full_bytes, caption=f"📁 **Pages {start_page}-{end_page} Full TXT File** ({len(results)} Links)")
        await update_or_query.message.reply_document(document=txt_simple_bytes, caption=f"📁 **Pages {start_page}-{end_page} Simple TXT File** (Title: Direct Stream Link)")
        await update_or_query.message.reply_document(document=html_full_bytes, caption=f"🌐 **Pages {start_page}-{end_page} Full Web App HTML File** (Interactive Player UI)")
        await update_or_query.message.reply_document(
            document=html_simple_bytes, 
            caption=f"🌐 **Pages {start_page}-{end_page} Simple HTML File**\n\nAage ke pages (**{next_start} to {next_end}**) scrape karne ke liye button click karein:",
            reply_markup=reply_markup
        )
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Error in run_scrape_chunk: {e}")
        await status_msg.edit_text(f"❌ Scraping error: {str(e)}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text("⛔ **Access Denied!**")
        return

    text = update.message.text.strip()
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

    if query.data == "stop_scrape":
        await query.edit_message_caption(caption=query.message.caption + "\n\n🛑 **Scraping Stopped By User.**")
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
# MAIN ENTRYPOINT
# ==========================================================
def main():
    init_db()
    threading.Thread(target=run_dummy_server, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("debug", debug_command))
    
    app.add_handler(CommandHandler("adduser", adduser_command))
    app.add_handler(CommandHandler("removeuser", removeuser_command))
    app.add_handler(CommandHandler("userlist", userlist_command))
    
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.Document.TXT, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 43-Site Dedicated Extractor & Web App Bot Running!")
    app.run_polling()

if __name__ == "__main__":
    main()
