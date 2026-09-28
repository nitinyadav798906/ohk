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
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, List, Dict
from urllib.parse import unquote, urljoin, urlparse

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
    domain = parsed.netloc or "xhamster.com"
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
# 13 DEDICATED DOMAIN EXTRACTION ENGINES
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

async def extract_video_link(video_url: str, source_page: str = "") -> Optional[dict]:
    try:
        text = await fetch_url_with_cloudscraper(video_url)
        if not text:
            return None

        # Title Extraction
        title = "Video"
        title_match = re.search(r'<h1[^>]*>(.*?)</h1>', text, re.IGNORECASE | re.DOTALL)
        if not title_match:
            title_match = re.search(r'<title>(.*?)</title>', text, re.IGNORECASE | re.DOTALL)
            
        if title_match:
            title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
            title = re.sub(r'\s+', ' ', title)

        stream_link = None
        file_type = None
        domain = urlparse(video_url).netloc.lower()

        # ----------------------------------------------------
        # 1. SITE DEDICATED EXTRACTION LOGIC
        # ----------------------------------------------------
        if "pornhub" in domain:
            match = re.search(r'var\s+flashvars_\d+\s*=\s*(\{.*?\});', text)
            if match:
                try:
                    data = json.loads(match.group(1))
                    for media in data.get("mediaDefinitions", []):
                        if media.get("videoUrl"):
                            stream_link = media["videoUrl"]
                            file_type = "M3U8" if ".m3u8" in stream_link else "MP4"
                            break
                except Exception:
                    pass

        elif "spankbang" in domain:
            sb_match = re.search(r'var\040stream_url\040=\040["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text)
            if sb_match:
                stream_link = sb_match.group(1)
                file_type = "MP4" if ".mp4" in stream_link else "M3U8"

        elif "redtube" in domain or "youporn" in domain:
            yt_match = re.search(r'page_params\.mediaDefinitions\s*=\s*(\[.*?\]);', text) or \
                       re.search(r'definition\s*:\s*["\'](https?:[^\s"\']+?)["\']', text)
            if yt_match:
                stream_link = yt_match.group(1) if yt_match.group(1).startswith("http") else None
                file_type = "MP4"

        elif "sxyprn" in domain:
            sxy_match = re.search(r'data-s=["\'](https?:[^\s"\']+?)["\']', text) or \
                        re.search(r'file\s*:\s*["\'](https?:[^\s"\']+?)["\']', text)
            if sxy_match:
                stream_link = sxy_match.group(1)
                file_type = "MP4" if ".mp4" in stream_link else "M3U8"

        elif "joysporn" in domain or "xhaccess" in domain:
            joy_match = re.search(r'source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                        re.search(r'file\s*:\s*["\'](https?:[^\s"\']+?)["\']', text)
            if joy_match:
                stream_link = joy_match.group(1)
                file_type = "MP4" if ".mp4" in stream_link else "M3U8"

        elif "4tube" in domain or "iporntv" in domain:
            ft_match = re.search(r'"src"\s*:\s*["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'https?:[^\s"\']+\.m3u8[^\s"\']*', text)
            if ft_match:
                stream_link = ft_match.group(0) if isinstance(ft_match.group(0), str) else ft_match.group(1)
                file_type = "M3U8"

        elif "hqporn" in domain or "justporn" in domain or "sexvid" in domain:
            hq_match = re.search(r'<source\s+src=["\'](https?:[^\s"\']+?)["\']', text) or \
                       re.search(r'video_url\s*:\s*["\'](https?:[^\s"\']+?)["\']', text)
            if hq_match:
                stream_link = hq_match.group(1)
                file_type = "MP4" if ".mp4" in stream_link else "M3U8"

        # ----------------------------------------------------
        # 2. FALLBACK GENERIC EXTRACTOR (HLS -> MP4 -> JS)
        # ----------------------------------------------------
        if not stream_link:
            m_hls = re.findall(r'(https?:[^\s"\']*?\.m3u8[^\s"\']*)', text)
            for hls_candidate in m_hls:
                clean_hls = hls_candidate.replace('\\/', '/')
                if not any(clean_hls.lower().endswith(ext) for ext in ['.jpg', '.png', '.jpeg', '.webp']):
                    stream_link = clean_hls
                    file_type = "M3U8"
                    break

        if not stream_link:
            m_mp4 = re.findall(r'(https?:[^\s"\']*?\.mp4(?:\?[^\s"\']*)?)', text)
            for mp4_candidate in m_mp4:
                clean_mp4 = mp4_candidate.replace('\\/', '/')
                if any(clean_mp4.lower().endswith(ext) for ext in ['.jpg', '.png', '.jpeg', '.webp']):
                    continue
                stream_link = clean_mp4
                file_type = "MP4"
                break

        if stream_link:
            final_link = process_tpl_link(stream_link) if file_type == "M3U8" else stream_link
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
# MULTI-PAGE SCRAPING ENGINE (13 SITES INTEGRATED)
# ==========================================================
async def scrape_multi_pages_chunk(url: str, start_page: int = 1, end_page: int = 10) -> List[dict]:
    url_to_source = {}
    parsed = urlparse(url)
    domain_name = parsed.netloc or "xhamster.com"
    base_domain = f"https://{domain_name}"

    # Check if single video link
    is_single_video = (
        url.endswith('.html') or 
        re.search(r'/videos?/[^/]+-\d+', url) or 
        re.search(r'/video/\d+', url) or
        re.search(r'/post/\d+', url) or
        re.search(r'/watch/', url) or
        re.search(r'/v/', url) or
        re.search(r'/film/', url)
    )
    
    if is_single_video and not any(url.endswith(x) for x in ['index.html', 'ilisting.html', '/']):
        res = await extract_video_link(url, source_page=url)
        return [res] if res else []

    page_urls = []
    base_u = url.rstrip('/')

    # Domain specific pagination rules
    for p in range(start_page, end_page + 1):
        if p == 1:
            page_urls.append(url)
            continue
        
        if "joysporn" in domain_name or "xhaccess" in domain_name:
            if base_u == base_domain:
                page_urls.append(f"{base_domain}/apapu/{p}/")
            else:
                page_urls.append(f"{base_u}/{p}/")
                page_urls.append(f"{base_u}?page={p}")

        elif "sxyprn" in domain_name:
            page_urls.append(f"{base_u}?page={p}")
            page_urls.append(f"{base_u}/{p}")

        elif "pornhub" in domain_name or "redtube" in domain_name or "youporn" in domain_name:
            page_urls.append(f"{base_u}?page={p}")

        elif "spankbang" in domain_name:
            page_urls.append(f"{base_u}/{p}/item/")
            page_urls.append(f"{base_u}?page={p}")

        else:
            page_urls.append(f"{base_u}/{p}")
            page_urls.append(f"{base_u}?page={p}")
            page_urls.append(f"{base_u}/page/{p}/")

    async def fetch_page_links(p_url):
        try:
            html_text = await fetch_url_with_cloudscraper(p_url)
            if not html_text:
                return

            raw_links = re.findall(r'href=["\']([^"\']+)["\']', html_text)

            for href in raw_links:
                if not href or href.startswith("javascript:") or href.startswith("#"):
                    continue

                clean_href = href.split('?')[0].split('#')[0]
                
                # Exclude static assets
                if any(clean_href.lower().endswith(ext) for ext in ['.css', '.js', '.jpg', '.png', '.gif', '.svg', '.jpeg', '.webp', '.ico']):
                    continue

                full_u = href if href.startswith("http") else urljoin(base_domain, href)
                
                # Site Specific Video Patterns
                video_patterns = [
                    r'/videos?/', r'/view_video', r'/watch/', r'/post/', 
                    r'/contents/', r'/v/', r'/film/', r'/play/', r'/item/', 
                    r'/e/', r'\.html$'
                ]

                if any(re.search(pat, clean_href.lower()) for pat in video_patterns):
                    if not re.search(r'/videos?/?$', clean_href) and not re.search(r'/category/?$', clean_href):
                        url_to_source[full_u] = p_url

        except Exception as e:
            logger.error(f"Error crawling page {p_url}: {e}")

    await asyncio.gather(*[fetch_page_links(pu) for pu in page_urls])

    if not url_to_source:
        return []

    # Parallel Extract Requests with Semaphore limit
    semaphore = asyncio.Semaphore(10)
    async def sem_extract(v_url, src_p):
        async with semaphore:
            return await extract_video_link(v_url, source_page=src_p)

    tasks = [sem_extract(v_url, src_p) for v_url, src_p in url_to_source.items()]
    results = await asyncio.gather(*tasks)
    
    return [res for res in results if res is not None]

# ==========================================================
# TELEGRAM BOT HANDLERS
# ==========================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text("⛔ **Access Denied! Aap is bot ko use nahi kar sakte.**")
        return

    await update.message.reply_text(
        "⚡ **13-Site Dedicated Bulk Link Scraper Bot Active!**\n\n"
        "🌐 **Supported Platforms:**\n"
        "1. xHamster  2. Joysporn  3. Xhaccess  4. Sxyprn\n"
        "5. Pornhub   6. Spankbang 7. Redtube   8. Youporn\n"
        "9. 4tube     10. IPornTV 11. HQPorn   12. JustPorn  13. SexVid\n\n"
        "📌 **Features:**\n"
        "1. **Domain Extraction Engine:** Native extractors for all 13 sites.\n"
        "2. **Export Files:** TXT & Interactive HTML Files.\n"
        "3. **FFmpeg Downloader:** Upload `.txt` file to auto-download & send video.\n\n"
        "🛠️ **Commands:** `/stop`, `/stats`, `/userlist`"
    )

async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        add_user_db(uid)
        await update.message.reply_text(f"✅ User `{uid}` added.", parse_mode="Markdown")

async def removeuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        remove_user_db(uid)
        await update.message.reply_text(f"🗑️ User `{uid}` removed.", parse_mode="Markdown")

async def userlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_allowed(update.effective_user.id):
        return
    users = get_all_users()
    msg = "👥 **Authorized Users:**\n\n"
    for uid in users:
        role = "👑 Admin" if uid == ADMIN_ID else "👤 User"
        msg += f"• `{uid}` ({role})\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_allowed(update.effective_user.id):
        return
    users_count = len(get_all_users())
    await update.message.reply_text(
        f"📊 **Bot Status:**\n\n"
        f"• **Authorized Users:** {users_count}\n"
        f"• **Dedicated Site Extractors:** 13\n"
        f"• **Engine Status:** 24/7 Active 🟢"
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STOP_PROCESS[update.effective_user.id] = True
    await update.message.reply_text("🛑 **Process Stop Request Sent!**")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        return

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
            await status_msg.edit_text(f"❌ Pages {start_page} to {end_page} par koi video links nahi mile.")
            return

        await status_msg.edit_text(f"✅ Total **{len(results)}** Videos Extracted! Files ready ho rahi hain...")

        txt_content = f"--- Scraped Video Links (Pages {start_page}-{end_page} | {len(results)} Items) ---\n\n"
        for idx, item in enumerate(results, 1):
            txt_content += f"{idx}. Title: {item['title']}\n"
            txt_content += f"   Source Listing Page: {item['source_page']}\n"
            txt_content += f"   Permanent Video Page: {item['page_url']}\n"
            txt_content += f"   Direct Stream Link: {item['download_link']}\n\n"

        txt_bytes = io.BytesIO(txt_content.encode('utf-8'))
        txt_bytes.name = f"scraped_p{start_page}_to_p{end_page}.txt"

        html_content = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Scraped Links ({start_page}-{end_page})</title>
<style>
body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #121212; color: #e0e0e0; margin: 20px; }}
.card {{ background: #1e1e1e; padding: 18px; margin-bottom: 15px; border-radius: 8px; border-left: 5px solid #0088cc; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }}
a {{ color: #4da6ff; word-break: break-all; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.tag {{ display: inline-block; background: #0088cc; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; margin-left: 8px; }}
</style></head><body><h2>Scraped Videos Pages {start_page} to {end_page} ({len(results)} Total Items)</h2>"""

        for idx, item in enumerate(results, 1):
            html_content += f"""<div class="card">
<h3>{idx}. {item['title']} <span class="tag">{item['type']}</span></h3>
<p><strong>🌐 Source Listing Page:</strong> <a href="{item['source_page']}" target="_blank">{item['source_page']}</a></p>
<p><strong>🔗 Permanent Video Link:</strong> <a href="{item['page_url']}" target="_blank">{item['page_url']}</a></p>
<p><strong>⚡ Direct Stream URL:</strong> <a href="{item['download_link']}" target="_blank">{item['download_link']}</a></p>
</div>"""
        html_content += "</body></html>"

        html_bytes = io.BytesIO(html_content.encode('utf-8'))
        html_bytes.name = f"scraped_p{start_page}_to_p{end_page}.html"

        context.user_data['last_url'] = target_url
        context.user_data['next_start'] = end_page + 1

        next_start = end_page + 1
        next_end = next_start + 9

        keyboard = [
            [InlineKeyboardButton(f"▶️ Continue (Pages {next_start}-{next_end})", callback_data="continue_scrape")],
            [InlineKeyboardButton("🛑 Stop Scraping", callback_data="stop_scrape")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update_or_query.message.reply_document(document=txt_bytes, caption=f"📁 **Pages {start_page}-{end_page} TXT File** ({len(results)} Links)")
        await update_or_query.message.reply_document(
            document=html_bytes, 
            caption=f"🌐 **Pages {start_page}-{end_page} HTML File**\n\nAage ke pages (**{next_start} to {next_end}**) scrape karne ke liye button click karein:",
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
    supported_domains = [
        "joysporn", "xhaccess", "xhamster", "sxyprn", "pornhub", 
        "spankbang", "redtube", "youporn", "4tube", "iporntv", 
        "hqporn", "justporn", "sexvid"
    ]

    if not any(domain in target_url for domain in supported_domains):
        await update.message.reply_text("❌ Domain supported nahi hai. Supported list ke liye `/start` dabayein.")
        return

    await run_scrape_chunk(update, context, target_url, start_page=1, end_page=10)

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_user_allowed(query.from_user.id):
        return

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
    
    app.add_handler(CommandHandler("adduser", adduser_command))
    app.add_handler(CommandHandler("removeuser", removeuser_command))
    app.add_handler(CommandHandler("userlist", userlist_command))
    
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.Document.TXT, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 All-in-One 13-Site Dedicated Extractor & Downloader Bot Running!")
    app.run_polling()

if __name__ == "__main__":
    main()
