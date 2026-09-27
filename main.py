import asyncio
import io
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, List, Set, Dict
from urllib.parse import unquote, urljoin, urlparse

import cloudscraper
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
# CONFIGURATION & GLOBAL VARIABLES
# ==========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7673015455:AAFrMbFSEpPXV33WMUud-bRFPUxvzN7znBk")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1714266885"))
DB_FILE = "bot_data.db"

STOP_PROCESS: Dict[int, bool] = {}
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

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
    domain = parsed.netloc or "beeg.onl"
    referer = f"https://{domain}/"
    
    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Origin": referer.rstrip('/'),
        "Sec-Ch-Ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    }

# ==========================================================
# DUMMY HTTP SERVER & KEEP ALIVE
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
# EXTRACTION & SCRAPING ENGINE
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
        resp = scraper.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        logger.error(f"Scraper fetch error on {url}: {e}")
    return None

async def fetch_url_with_cloudscraper(url: str) -> Optional[str]:
    return await asyncio.to_thread(fetch_url_sync, url)

async def extract_video_link(video_url: str) -> Optional[dict]:
    try:
        text = await fetch_url_with_cloudscraper(video_url)
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
        file_type = None

        m_hls = re.search(r'(https?:[^\s"\']*?\.m3u8[^\s"\']*)', text)
        if m_hls:
            stream_link = m_hls.group(1).replace('\\/', '/')
            file_type = "M3U8"
        else:
            m_mp4 = re.search(r'(https?:[^\s"\']*?\.mp4[^\s"\']*)', text)
            if m_mp4:
                stream_link = m_mp4.group(1).replace('\\/', '/')
                file_type = "MP4"

        if not stream_link and "sxyprn" in video_url:
            sxy_match = re.search(r'data-s=["\'](https?:[^\s"\']+?)["\']', text) or re.search(r'file\s*:\s*["\'](https?:[^\s"\']+?)["\']', text)
            if sxy_match:
                stream_link = sxy_match.group(1)
                file_type = "MP4" if ".mp4" in stream_link else "M3U8"

        if not stream_link and "beeg" in video_url:
            beeg_match = re.search(r'(https?:[^\s"\']*?\.(?:m3u8|mp4)[^\s"\']*)', text) or re.search(r'src=["\'](https?:[^\s"\']+?\.(?:m3u8|mp4)[^\s"\']*)["\']', text)
            if beeg_match:
                stream_link = beeg_match.group(1).replace('\\/', '/')
                file_type = "MP4" if ".mp4" in stream_link else "M3U8"

        if stream_link:
            final_link = process_tpl_link(stream_link) if file_type == "M3U8" else stream_link
            return {
                "title": title,
                "type": file_type,
                "page_url": video_url,
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
# FIXED MULTI-PAGE SCRAPING ENGINE (FOR BEEG.ONL & ALL)
# ==========================================================
async def scrape_multi_pages_chunk(url: str, start_page: int = 1, end_page: int = 10) -> List[dict]:
    found_urls: Set[str] = set()
    parsed = urlparse(url)
    domain_name = parsed.netloc or "beeg.onl"
    base_domain = f"https://{domain_name}"

    is_single_video = (
        url.endswith('.html') or 
        re.search(r'/video/[^/]+', url) or
        re.search(r'/post/\d+', url)
    )
    
    if is_single_video and not any(url.endswith(x) for x in ['index.html', 'ilisting.html', '/']):
        res = await extract_video_link(url)
        return [res] if res else []

    page_urls = []
    base_u = url.rstrip('/')

    for p in range(start_page, end_page + 1):
        if p == 1:
            page_urls.append(url)
            page_urls.append(f"{base_domain}/")
            continue
        
        if "beeg" in domain_name:
            page_urls.append(f"{base_u}/page/{p}/")
            page_urls.append(f"{base_u}/page/{p}")
            page_urls.append(f"{base_domain}/page/{p}/")
            page_urls.append(f"{base_u}/?page={p}")

        elif "sxyprn" in domain_name:
            if "?" in url:
                page_urls.append(f"{url}&page={p}")
            else:
                page_urls.append(f"{base_u}?page={p}")
                page_urls.append(f"{base_u}/{p}")

        elif "joysporn" in domain_name:
            if base_u == base_domain:
                page_urls.append(f"{base_domain}/apapu/{p}/")
            elif base_u.endswith('.html'):
                page_urls.append(url)
            else:
                page_urls.append(f"{base_u}/{p}/")
                page_urls.append(f"{base_u}?page={p}")

        elif any(x in domain_name for x in ["xhamster", "xhaccess", "pornhub", "spankbang", "redtube", "youporn"]):
            page_urls.append(f"{base_u}/{p}")
            page_urls.append(f"{base_u}?page={p}")
            if "?" in url:
                page_urls.append(f"{url}&page={p}")

    page_urls = list(set(page_urls))

    async def fetch_page_links(p_url):
        try:
            html_text = await fetch_url_with_cloudscraper(p_url)
            if not html_text:
                return

            raw_links = re.findall(r'href=["\']([^"\']+)["\']', html_text)

            for href in raw_links:
                clean_href = href.split('?')[0].split('#')[0]
                
                if any(clean_href.endswith(ext) for ext in ['.css', '.js', '.jpg', '.png', '.gif', '.svg', '.jpeg', '.webp']):
                    continue

                full_u = href if href.startswith("http") else urljoin(base_domain, href)
                
                if "beeg" in domain_name:
                    if "/video/" in clean_href or clean_href.endswith('.html') or re.search(r'/[^/]+-\d+/?$', clean_href):
                        if not any(x in clean_href for x in ['/page/', '/category/', '/tag/', '/index.html']):
                            found_urls.add(full_u)

                elif "sxyprn" in domain_name:
                    if re.search(r'/post/\w+', clean_href) or re.search(r'/video/\w+', clean_href) or clean_href.endswith('.html'):
                        found_urls.add(full_u)

                elif "joysporn" in domain_name:
                    if clean_href.endswith('.html') or "/video/" in clean_href or "/videos/" in clean_href:
                        if not any(clean_href.endswith(x) for x in ['index.html', 'main.html', 'ilisting.html']):
                            found_urls.add(full_u)

                elif any(x in domain_name for x in ["xhamster", "xhaccess", "pornhub", "spankbang", "redtube", "youporn"]):
                    if any(key in clean_href for key in ["/videos/", "/video/", "/view_video.php", "/watch/"]):
                        if not re.search(r'/videos?/?$', clean_href):
                            found_urls.add(full_u)

        except Exception as e:
            logger.error(f"Error crawling page {p_url}: {e}")

    await asyncio.gather(*[fetch_page_links(pu) for pu in page_urls])

    if not found_urls:
        return []

    semaphore = asyncio.Semaphore(35)
    async def sem_extract(v_url):
        async with semaphore:
            return await extract_video_link(v_url)

    tasks = [sem_extract(v_url) for v_url in found_urls]
    results = await asyncio.gather(*tasks)
    
    return [res for res in results if res is not None]

# ==========================================================
# TELEGRAM BOT HANDLERS & COMMAND REGISTER
# ==========================================================
async def setup_bot_commands(application):
    commands = [
        BotCommand("start", "Start the bot and show help"),
        BotCommand("stats", "Show bot & user statistics"),
        BotCommand("stop", "Stop active task/scraping"),
        BotCommand("userlist", "List allowed users (Admin only)"),
        BotCommand("adduser", "Add allowed user ID (Admin only)"),
        BotCommand("removeuser", "Remove user ID (Admin only)")
    ]
    await application.bot.set_my_commands(commands)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text("⛔ **Access Denied!**")
        return

    await update.message.reply_text(
        "⚡ **9-Site Bulk Scraper & Downloader Bot Active!**\n\n"
        "🌐 **Supported Sites (9 Total):**\n"
        "• xHamster | Joysporn | Xhaccess | Sxyprn | Beeg.onl\n"
        "• Pornhub | Spankbang | Redtube | Youporn\n\n"
        "📌 **Features & Usage:**\n"
        "1. **300+ Link Extraction:** Target URL bhejein, bot Pages 1-10 tak links extract karega.\n"
        "2. **Clean TXT & HTML Output:** Video Links & Direct Stream URLs deliver karega.\n"
        "3. **FFmpeg Downloader:** `.txt` file upload karke auto download karein.\n\n"
        "🛠️ **Commands:** `/start`, `/stats`, `/stop`, `/userlist`, `/adduser`, `/removeuser`"
    )

async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin command only.")
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        add_user_db(uid)
        await update.message.reply_text(f"✅ User `{uid}` database me add ho gaya.", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ **Usage:** `/adduser <user_id>`")

async def removeuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin command only.")
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        remove_user_db(uid)
        await update.message.reply_text(f"🗑️ User `{uid}` database se hata diya gaya.", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ **Usage:** `/removeuser <user_id>`")

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
        f"• **Supported Sites:** 9 Platforms\n"
        f"• **Extract Capacity:** ~300+ Links / Batch\n"
        f"• **Engine:** Cloudflare Bypass Active 🟢"
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STOP_PROCESS[update.effective_user.id] = True
    await update.message.reply_text("🛑 **Process Stop Request Bhej Diya Gaya Hai!**")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        return

    doc = update.message.document
    if not doc or not doc.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Kripya valid `.txt` file upload karein.")
        return

    STOP_PROCESS[user_id] = False
    status_msg = await update.message.reply_text("📥 **TXT file read ho rahi hai...**")

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
        await status_msg.edit_text(f"🚀 Total **{total}** links processing me hain! Rokne ke liye `/stop` bhejein.")

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

            await progress_msg.edit_text(f"📥 **[{idx}/{total}] FFmpeg Downloading...**\n`{video_title[:30]}...`")
            success = await download_video_ffmpeg(stream_url, output_file)

            if success:
                await progress_msg.edit_text(f"📤 **[{idx}/{total}] Telegram Par Upload Ho Raha Hai...**")
                try:
                    with open(output_file, 'rb') as vf:
                        await update.message.reply_video(
                            video=vf, 
                            caption=f"🎥 **{video_title}**\n\n🔗 **Link {idx}/{total}**",
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

        await status_msg.edit_text("✅ **All videos processing completed!**")

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

        await status_msg.edit_text(f"✅ Total **{len(results)}** Videos Extracted! TXT aur HTML files tayar ho rahi hain...")

        txt_content = f"--- Scraped Video Links (Pages {start_page}-{end_page} | {len(results)} Items) ---\n\n"
        for idx, item in enumerate(results, 1):
            txt_content += f"{idx}. Title: {item['title']}\n"
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

        await update_or_query.message.reply_document(
            document=txt_bytes, 
            caption=f"📁 **Pages {start_page}-{end_page} TXT File** ({len(results)} Links)"
        )
        
        await update_or_query.message.reply_document(
            document=html_bytes, 
            caption=f"🌐 **Pages {start_page}-{end_page} HTML File**\n\nAage ke pages (**{next_start} to {next_end}**) scrape karne ke liye niche button par click karein:",
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
        "joysporn", "xhaccess", "xhamster", "sxyprn", 
        "pornhub", "spankbang", "redtube", "youporn", "beeg.onl", "beeg"
    ]

    if not any(domain in target_url.lower() for domain in supported_domains):
        await update.message.reply_text("❌ Yeh domain supported nahi hai. Supported sites: xHamster, Joysporn, Sxyprn, Beeg.onl, Pornhub, Spankbang, Redtube, Youporn.")
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
            await query.message.reply_text("❌ Target URL lost. Kripya URL firse bhej kar start karein.")
            return

        await run_scrape_chunk(query, context, target_url, start_page=start_page, end_page=end_page)

# ==========================================================
# MAIN EXECUTION ENTRYPOINT
# ==========================================================
def main():
    init_db()
    threading.Thread(target=run_dummy_server, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()
    
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(setup_bot_commands).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("stats", stats_command))
    
    app.add_handler(CommandHandler("adduser", adduser_command))
    app.add_handler(CommandHandler("removeuser", removeuser_command))
    app.add_handler(CommandHandler("userlist", userlist_command))
    
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.Document.TXT, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Advanced 9-Site 300+ Link Scraper Active!")
    app.run_polling()

if __name__ == "__main__":
    main()
