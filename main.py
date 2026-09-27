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
from typing import Optional, List, Dict
from urllib.parse import urlparse

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
# DUMMY HTTP SERVER & KEEP ALIVE
# ==========================================================
class DummyPortServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Status: Fully Active & Operational!")

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
# TURBO ULTRA-FAST PLAYWRIGHT SCRAPING ENGINE
# ==========================================================
async def scrape_single_url_fast(browser, url: str) -> dict:
    video_links = set()
    stream_link = None
    title = "Video"

    context = await browser.new_context(
        user_agent=DEFAULT_USER_AGENT,
        viewport={'width': 640, 'height': 360},
        java_script_enabled=True,
        has_touch=False
    )
    
    # Resource blocking for 90% bandwidth saving and maximum speed
    await context.route(
        "**/*.{png,jpg,jpeg,gif,svg,css,woff,woff2,ttf,otf,ico,mp3,wav,ogg,webp,avif,pdf}", 
        lambda route: route.abort()
    )

    page = await context.new_page()

    def handle_response(response):
        nonlocal stream_link
        res_url = response.url
        if (".m3u8" in res_url or ".mp4" in res_url) and not stream_link:
            if not any(x in res_url.lower() for x in [".jpg", ".png", ".gif", ".jpeg", ".ts", "thumb"]):
                stream_link = res_url

    page.on("response", handle_response)

    try:
        # Strict hard cutoff at 3.5 seconds
        await page.goto(url, wait_until="domcontentloaded", timeout=3500)
        await asyncio.sleep(0.3)

        try:
            title = await page.title()
        except Exception:
            title = "Extracted Video"

        hrefs = await page.eval_on_selector_all("a[href]", "elements => elements.map(e => e.href)")
        for href in hrefs:
            clean = href.split('?')[0].split('#')[0]
            if any(k in clean.lower() for k in [
                "/video/", "/videos/", "/post/", "/watch/", "/v/", "/embed/", "/view/", "/play/", ".html"
            ]) and not any(x in clean.lower() for x in [
                "/page/", "/category/", "/tag/", "/index.html", "/search/", "/channels/", "/actors/", "/login"
            ]):
                video_links.add(href)

    except Exception as e:
        logger.debug(f"Fast Scrape Timeout/Error on {url}: {e}")
    finally:
        await context.close()

    file_type = "M3U8" if stream_link and ".m3u8" in stream_link else "MP4"

    return {
        "title": title,
        "type": file_type,
        "page_url": url,
        "download_link": stream_link,
        "video_links": list(video_links)
    }

async def download_video_ffmpeg(url: str, output_path: str) -> bool:
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-headers", f"User-Agent: {DEFAULT_USER_AGENT}\r\n",
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
# BATCH SCRAPING LOGIC WITH LIVE COUNTER
# ==========================================================
async def run_scrape_chunk(update_or_query, context, target_url: str, start_page: int, end_page: int):
    message_target = update_or_query.message if isinstance(update_or_query, Update) else update_or_query.message
    status_msg = await message_target.reply_text(f"🚀 **Turbo Scraping Started (Pages {start_page} to {end_page})...**")

    base_u = target_url.rstrip('/')
    page_urls = []
    for p in range(start_page, end_page + 1):
        if p == 1:
            page_urls.append(target_url)
        else:
            page_urls.append(f"{base_u}/page/{p}/")
            page_urls.append(f"{base_u}/page/{p}")
            page_urls.append(f"{base_u}/?page={p}")

    page_urls = list(set(page_urls))

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled"
            ]
        )

        # STEP 1: Scan Pages Concurrently
        await status_msg.edit_text(f"🔍 **Scanning Pages {start_page}-{end_page} concurrently...**")
        index_tasks = [scrape_single_url_fast(browser, pu) for pu in page_urls]
        index_results = await asyncio.gather(*index_tasks)

        all_video_pages = set()
        for res in index_results:
            if res.get("video_links"):
                all_video_pages.update(res["video_links"])

        if not all_video_pages:
            await browser.close()
            await status_msg.edit_text(f"❌ Pages {start_page} to {end_page} par koi valid video links nahi mile.")
            return

        targets_to_scrape = list(all_video_pages)[:25]
        total_targets = len(targets_to_scrape)
        
        extracted_results = []
        last_update_time = time.time()

        # STEP 2: Live Extraction Loop
        for idx, v_url in enumerate(targets_to_scrape, 1):
            res = await scrape_single_url_fast(browser, v_url)
            if res.get("download_link"):
                extracted_results.append(res)

            # Throttle status updates to every 1.5 seconds to respect Telegram Limits
            if time.time() - last_update_time > 1.5 or idx == total_targets:
                last_update_time = time.time()
                
                count = len(extracted_results)
                progress_pct = int((idx / total_targets) * 100)
                
                live_text = (
                    f"⚡ **LIVE SCRAPING IN PROGRESS**\n"
                    f"📑 **Pages:** `{start_page}` to `{end_page}`\n"
                    f"⏳ **Scanned:** `{idx}/{total_targets}` Links (`{progress_pct}%`)\n"
                    f"🎯 **Extracted Direct Streams:** `{count}` Found! 🔥\n\n"
                    f"👇 **Recent Streams Found:**\n"
                )
                
                for item in extracted_results[-3:]:
                    title_clean = item['title'][:22]
                    live_text += f"• `{title_clean}` → [Stream Link]({item['download_link']})\n"

                try:
                    await status_msg.edit_text(live_text, parse_mode="Markdown", disable_web_page_preview=True)
                except Exception:
                    pass

        await browser.close()

    if not extracted_results:
        await status_msg.edit_text("❌ Video pages mile par direct stream URLs extract nahi ho sake.")
        return

    # Generate TXT Output File
    txt_content = f"--- Scraped Video Links (Pages {start_page}-{end_page} | Total: {len(extracted_results)} Items) ---\n\n"
    for idx, item in enumerate(extracted_results, 1):
        txt_content += f"{idx}. Title: {item['title']}\n"
        txt_content += f"   Page URL: {item['page_url']}\n"
        txt_content += f"   Direct Stream URL: {item['download_link']}\n\n"

    txt_bytes = io.BytesIO(txt_content.encode('utf-8'))
    txt_bytes.name = f"scraped_p{start_page}_to_p{end_page}.txt"

    # Generate HTML Output File
    html_content = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Scraped Links ({start_page}-{end_page})</title>
<style>
body {{ font-family: sans-serif; background: #121212; color: #e0e0e0; margin: 20px; }}
.card {{ background: #1e1e1e; padding: 15px; margin-bottom: 12px; border-radius: 8px; border-left: 5px solid #0088cc; }}
a {{ color: #4da6ff; word-break: break-all; text-decoration: none; }}
.tag {{ background: #0088cc; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 11px; margin-left: 8px; }}
</style></head><body><h2>Scraped Videos Pages {start_page} to {end_page} ({len(extracted_results)} Items Extracted)</h2>"""

    for idx, item in enumerate(extracted_results, 1):
        html_content += f"""<div class="card">
<h3>{idx}. {item['title']} <span class="tag">{item['type']}</span></h3>
<p><strong>🔗 Page URL:</strong> <a href="{item['page_url']}" target="_blank">{item['page_url']}</a></p>
<p><strong>⚡ Direct Stream URL:</strong> <a href="{item['download_link']}" target="_blank">{item['download_link']}</a></p>
</div>"""
    html_content += "</body></html>"

    html_bytes = io.BytesIO(html_content.encode('utf-8'))
    html_bytes.name = f"scraped_p{start_page}_to_p{end_page}.html"

    next_start = end_page + 1
    next_end = next_start + 9
    keyboard = [
        [InlineKeyboardButton(f"▶️ Continue Next Batch (Pages {next_start}-{next_end})", callback_data="continue_scrape")],
        [InlineKeyboardButton("🛑 Stop Scraping", callback_data="stop_scrape")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    context.user_data['last_url'] = target_url
    context.user_data['next_start'] = next_start

    await message_target.reply_document(
        document=txt_bytes, 
        caption=f"📁 **Pages {start_page}-{end_page} TXT File** ({len(extracted_results)} Total Direct Links)"
    )
    await message_target.reply_document(
        document=html_bytes, 
        caption=f"🌐 **Pages {start_page}-{end_page} HTML File**\n\nAage ke pages (**{next_start} to {next_end}**) scrape karne ke liye button dabaein:",
        reply_markup=reply_markup
    )
    await status_msg.delete()

# ==========================================================
# TELEGRAM BOT COMMAND & MESSAGE HANDLERS
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
        "⚡ **Universal Turbo Live Scraper Bot Active!**\n\n"
        "• Direct URL bhejein parallel scraping run karne ke liye.\n"
        "• Extracted TXT file upload karke automatic FFmpeg video downloads run karein."
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
        await update.message.reply_text(f"🗑️ User `{uid}` database se remove ho gaya.", parse_mode="Markdown")
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
        f"📊 **Bot Statistics:**\n\n"
        f"• **Authorized Users:** {users_count}\n"
        f"• **Engine Speed:** 3.5s Hard Cutoff Turbo Engine ⚡"
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

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )

            for idx, raw_url in enumerate(urls, 1):
                if STOP_PROCESS.get(user_id, False):
                    await update.message.reply_text("🛑 **Task Stopped By User!**")
                    break

                progress_msg = await update.message.reply_text(f"⏳ **[{idx}/{total}] Processing...**")
                stream_url = raw_url
                video_title = f"Video #{idx}"

                if not (raw_url.endswith('.m3u8') or raw_url.endswith('.mp4')):
                    extracted = await scrape_single_url_fast(browser, raw_url)
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

            await browser.close()

        await status_msg.edit_text("✅ **All videos processing completed!**")

    except Exception as e:
        logger.error(f"Error processing document: {e}")
        await status_msg.edit_text(f"❌ File Process Error: {str(e)}")

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
            await query.message.reply_text("❌ Target URL lost. Please re-send the URL.")
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

    print("🤖 Fully Final Turbo Live Scraper Bot Active!")
    app.run_polling()

if __name__ == "__main__":
    main()
