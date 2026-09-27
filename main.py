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
from typing import List, Dict
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
from playwright.async_api import async_playwright

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
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "1714266885"))
except ValueError:
    ADMIN_ID = 1234567890

DB_FILE = "bot_data.db"
STOP_PROCESS: Dict[int, bool] = {}
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
PARALLEL_WORKERS = 5

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
    conn.commit()
    conn.close()

def is_user_allowed(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

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
# DUMMY HTTP SERVER & KEEP-ALIVE LOOP
# ==========================================================
class DummyPortServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Ultra-Fast Scraping Engine Online!")

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
                logger.info("Self-ping successful!")
            except Exception as e:
                logger.error(f"Self-ping failed: {e}")

# ==========================================================
# HIGH-SPEED PARSING ENGINE
# ==========================================================
def fast_http_scrape_single(url: str) -> dict:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    video_links = set()
    found_streams = set()
    title = "Extracted Video"

    try:
        resp = requests.get(url, headers=headers, timeout=3.5)
        if resp.status_code == 200:
            html = resp.text
            
            t_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
            if t_match:
                title = t_match.group(1).strip().replace("\n", " ")

            streams = re.findall(r'https?://[^\s"\'<>]+\.(?:m3u8|mp4)[^\s"\'<>]*', html)
            for st in streams:
                if not any(x in st.lower() for x in [".jpg", ".png", ".gif", ".jpeg", ".ts", "thumb", "preview"]):
                    clean_st = st.replace('\\/', '/')
                    found_streams.add(clean_st)

            hrefs = re.findall(r'href=["\'](https?://[^\s"\']+)["\']', html)
            for href in hrefs:
                clean = href.split('?')[0].split('#')[0]
                if any(k in clean.lower() for k in ["/videos/", "/video/", "/movies/", "/watch/", "/post/", "/v/"]):
                    if not any(x in clean.lower() for x in ["/categories/", "/tags/", "/search/", "/page/", "/users/"]):
                        video_links.add(href)
    except Exception:
        pass

    stream_link = list(found_streams)[0] if found_streams else None
    file_type = "M3U8" if stream_link and ".m3u8" in stream_link else "MP4"

    return {
        "title": title[:50],
        "type": file_type,
        "page_url": url,
        "download_link": stream_link,
        "video_links": list(video_links)
    }

async def scrape_playwright_fallback(browser, url: str) -> dict:
    video_links = set()
    found_streams = set()
    title = "Video"

    context = await browser.new_context(
        user_agent=DEFAULT_USER_AGENT,
        viewport={'width': 480, 'height': 320},
        java_script_enabled=True
    )
    
    await context.route(
        "**/*.{png,jpg,jpeg,gif,svg,css,woff,woff2,ttf,otf,ico,mp3,wav,ogg,webp,avif,pdf}", 
        lambda route: route.abort()
    )

    page = await context.new_page()

    def handle_response(response):
        res_url = response.url
        if (".m3u8" in res_url or ".mp4" in res_url):
            if not any(x in res_url.lower() for x in [".jpg", ".png", ".gif", ".jpeg", ".ts", "thumb"]):
                found_streams.add(res_url)

    page.on("response", handle_response)

    try:
        await page.goto(url, wait_until="commit", timeout=2800)
        await asyncio.sleep(0.3)

        try: title = await page.title()
        except Exception: title = "Extracted Video"

        html_content = await page.content()
        regex_matches = re.findall(r'https?://[^\s"\'<>]+\.(?:m3u8|mp4)[^\s"\'<>]*', html_content)
        for match in regex_matches:
            if not any(x in match.lower() for x in [".jpg", ".png", ".gif", ".jpeg", ".ts", "thumb"]):
                found_streams.add(match.replace('\\/', '/'))

        hrefs = await page.eval_on_selector_all("a[href]", "elements => elements.map(e => e.href)")
        for href in hrefs:
            clean = href.split('?')[0].split('#')[0]
            if any(k in clean.lower() for k in ["/video/", "/videos/", "/post/", "/watch/", "/v/", "/embed/"]):
                if not any(x in clean.lower() for x in ["/page/", "/category/", "/tag/", "/search/"]):
                    video_links.add(href)
    except Exception:
        pass
    finally:
        await context.close()

    stream_link = list(found_streams)[0] if found_streams else None
    file_type = "M3U8" if stream_link and ".m3u8" in stream_link else "MP4"

    return {
        "title": title[:50],
        "type": file_type,
        "page_url": url,
        "download_link": stream_link,
        "video_links": list(video_links)
    }

# ==========================================================
# PARALLEL BATCH SCRAPING ENGINE WITH STOP CHECK
# ==========================================================
async def run_scrape_chunk(update_or_query, context, target_url: str, start_page: int, end_page: int):
    message_target = update_or_query.message if isinstance(update_or_query, Update) else update_or_query.message
    user_id = update_or_query.effective_user.id
    STOP_PROCESS[user_id] = False

    status_msg = await message_target.reply_text(f"⚡ **Ultra-Fast Scraping Started (Pages {start_page}-{end_page})...**")

    base_u = target_url.rstrip('/')
    page_urls = []
    for p in range(start_page, end_page + 1):
        if p == 1:
            page_urls.append(target_url)
        else:
            page_urls.append(f"{base_u}/page/{p}/")
            page_urls.append(f"{base_u}/{p}")
            page_urls.append(f"{base_u}/?page={p}")

    page_urls = list(set(page_urls))

    all_video_pages = set()
    for pu in page_urls:
        if STOP_PROCESS.get(user_id, False):
            await status_msg.edit_text("🛑 **Scraping stopped.**")
            return
        res = fast_http_scrape_single(pu)
        if res.get("video_links"):
            all_video_pages.update(res["video_links"])

    if not all_video_pages:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            tasks = [scrape_playwright_fallback(browser, pu) for pu in page_urls]
            results = await asyncio.gather(*tasks)
            await browser.close()

            for r in results:
                if r.get("video_links"):
                    all_video_pages.update(r["video_links"])

    if not all_video_pages:
        await status_msg.edit_text(f"❌ Pages {start_page} to {end_page} par koi video links nahi mile.")
        return

    targets = list(all_video_pages)[:30]
    total_targets = len(targets)
    extracted_results = []
    last_update_time = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )

        semaphore = asyncio.Semaphore(PARALLEL_WORKERS)

        async def worker(v_url, idx):
            if STOP_PROCESS.get(user_id, False):
                return
            async with semaphore:
                if STOP_PROCESS.get(user_id, False):
                    return
                res = fast_http_scrape_single(v_url)
                if not res.get("download_link"):
                    res = await scrape_playwright_fallback(browser, v_url)

                if res.get("download_link"):
                    extracted_results.append(res)

                nonlocal last_update_time
                if time.time() - last_update_time > 1.0 or idx == total_targets:
                    last_update_time = time.time()
                    count = len(extracted_results)
                    progress_pct = int((idx / total_targets) * 100)
                    
                    live_text = (
                        f"⚡ **ULTRA-FAST PARALLEL SCRAPING**\n"
                        f"📑 **Pages:** `{start_page}` to `{end_page}`\n"
                        f"⏳ **Scanned:** `{idx}/{total_targets}` (`{progress_pct}%`)\n"
                        f"🎯 **Extracted Streams:** `{count}` Found! 🔥\n\n"
                    )
                    for item in extracted_results[-3:]:
                        live_text += f"• `{item['title'][:20]}` → [Link]({item['download_link']})\n"

                    try:
                        await status_msg.edit_text(live_text, parse_mode="Markdown", disable_web_page_preview=True)
                    except Exception:
                        pass

        tasks = [worker(url, idx) for idx, url in enumerate(targets, 1)]
        await asyncio.gather(*tasks)
        await browser.close()

    if STOP_PROCESS.get(user_id, False):
        await status_msg.edit_text("🛑 **Process stopped by user.**")
        return

    if not extracted_results:
        await status_msg.edit_text("❌ Direct Stream URLs extract nahi ho paaye.")
        return

    txt_content = f"--- Scraped Links (Pages {start_page}-{end_page} | Total: {len(extracted_results)}) ---\n\n"
    for idx, item in enumerate(extracted_results, 1):
        txt_content += f"{idx}. {item['title']}\nPage: {item['page_url']}\nStream: {item['download_link']}\n\n"

    txt_bytes = io.BytesIO(txt_content.encode('utf-8'))
    txt_bytes.name = f"scraped_p{start_page}_to_p{end_page}.txt"

    next_start = end_page + 1
    next_end = next_start + 9
    keyboard = [
        [InlineKeyboardButton(f"▶️ Next Batch (Pages {next_start}-{next_end})", callback_data="continue_scrape")],
        [InlineKeyboardButton("🛑 Stop", callback_data="stop_scrape")]
    ]

    context.user_data['last_url'] = target_url
    context.user_data['next_start'] = next_start

    await message_target.reply_document(
        document=txt_bytes, 
        caption=f"📁 **Pages {start_page}-{end_page} Completed!**\nFound `{len(extracted_results)}` direct stream links.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    await status_msg.delete()

# ==========================================================
# COMMAND HANDLERS (/stop, /states, /start, etc.)
# ==========================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_allowed(update.effective_user.id):
        await update.message.reply_text("❌ Aap Authorized nahi hain.")
        return
    await update.message.reply_text("🤖 **Ultra-Fast Video Scraper Active!**\n\nLink bhejo, scraping start ho jaayegi.")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stop Command implementation"""
    user_id = update.effective_user.id
    STOP_PROCESS[user_id] = True
    await update.message.reply_text("🛑 **Scraping process stopping request sent!**")

async def states_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/states & /stats Command implementation"""
    if not is_user_allowed(update.effective_user.id):
        return
    users = get_all_users()
    stats_text = (
        f"📊 **BOT SYSTEM STATUS & STATES**\n\n"
        f"👑 **Admin ID:** `{ADMIN_ID}`\n"
        f"👥 **Total Authorized Users:** `{len(users)}`\n"
        f"⚡ **Parallel Workers Engine:** `{PARALLEL_WORKERS} Threads`\n"
        f"🟢 **Status:** 24/7 Active & Running\n"
    )
    await update.message.reply_text(stats_text, parse_mode="Markdown")

async def add_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if context.args:
        try:
            uid = int(context.args[0])
            add_user_db(uid)
            await update.message.reply_text(f"✅ User `{uid}` added!")
        except ValueError:
            await update.message.reply_text("❌ Invalid User ID.")

async def remove_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if context.args:
        try:
            uid = int(context.args[0])
            remove_user_db(uid)
            await update.message.reply_text(f"🗑️ User `{uid}` removed!")
        except ValueError:
            await update.message.reply_text("❌ Invalid User ID.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_allowed(update.effective_user.id): return
    text = update.message.text.strip()
    url_match = re.search(r'(https?://[^\s]+)', text)
    if url_match:
        await run_scrape_chunk(update, context, url_match.group(1), start_page=1, end_page=10)

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "continue_scrape":
        target_url = context.user_data.get('last_url')
        start_page = context.user_data.get('next_start', 11)
        await run_scrape_chunk(query, context, target_url, start_page=start_page, end_page=start_page+9)
    elif query.data == "stop_scrape":
        STOP_PROCESS[user_id] = True
        await query.message.edit_text("🛑 **Scraping Process Stopped.**")

def main():
    init_db()
    threading.Thread(target=run_dummy_server, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands Registered
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("states", states_cmd))
    app.add_handler(CommandHandler("stats", states_cmd))
    app.add_handler(CommandHandler("adduser", add_user_cmd))
    app.add_handler(CommandHandler("removeuser", remove_user_cmd))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback_handler))

    print("🤖 Ultra-Fast Scraper Ready with /stop and /states!")
    app.run_polling()

if __name__ == "__main__":
    main()
