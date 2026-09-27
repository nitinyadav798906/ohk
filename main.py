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
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

# ==========================================================
# DATABASE HANDLER (SQLite Access Control)
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
# PLAYWRIGHT SCRAPER & FFMEPG ENGINE
# ==========================================================
async def scrape_page_with_playwright(url: str) -> dict:
    video_links = set()
    stream_link = None
    title = "Video"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=DEFAULT_USER_AGENT)
        page = await context.new_page()

        def handle_response(response):
            nonlocal stream_link
            res_url = response.url
            if ".m3u8" in res_url or ".mp4" in res_url:
                if not stream_link and not any(x in res_url for x in [".jpg", ".png", ".gif", ".jpeg"]):
                    stream_link = res_url

        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight/2)")
            await asyncio.sleep(2)

            title = await page.title()
            hrefs = await page.eval_on_selector_all("a[href]", "elements => elements.map(e => e.href)")
            for href in hrefs:
                clean = href.split('?')[0].split('#')[0]
                if any(k in clean for k in ["/video/", "/videos/", "/post/", ".html"]) and not any(x in clean for x in ["/page/", "/category/", "/tag/", "/index.html"]):
                    video_links.add(href)

        except Exception as e:
            logger.error(f"Playwright Scraping Error on {url}: {e}")
        finally:
            await browser.close()

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
            "-user_agent", DEFAULT_USER_AGENT,
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
# MULTI-PAGE CHUNK SCRAPING LOGIC
# ==========================================================
async def run_scrape_chunk(update_or_query, context, target_url: str, start_page: int, end_page: int):
    # Fixed Update / Callback Query message targeting
    message_target = update_or_query.message if isinstance(update_or_query, Update) else update_or_query.message

    status_msg = await message_target.reply_text(f"⚡ **Scraping Pages {start_page} to {end_page} with Headless Browser Engine...**")

    parsed = urlparse(target_url)
    domain_name = parsed.netloc or "beeg.onl"
    base_domain = f"https://{domain_name}"
    base_u = target_url.rstrip('/')

    page_urls = []
    for p in range(start_page, end_page + 1):
        if p == 1:
            page_urls.append(target_url)
            page_urls.append(f"{base_domain}/")
        else:
            page_urls.append(f"{base_u}/page/{p}/")
            page_urls.append(f"{base_u}/page/{p}")
            page_urls.append(f"{base_domain}/page/{p}/")
            page_urls.append(f"{base_u}/?page={p}")

    all_video_pages = set()

    for pu in set(page_urls):
        res = await scrape_page_with_playwright(pu)
        if res.get("video_links"):
            all_video_pages.update(res["video_links"])

    if not all_video_pages:
        await status_msg.edit_text(f"❌ Pages {start_page} to {end_page} par koi video links nahi mile.")
        return

    await status_msg.edit_text(f"✅ Total **{len(all_video_pages)}** Video Pages Found! Extracting Stream URLs...")

    extracted_results = []
    for v_url in list(all_video_pages)[:30]:
        res = await scrape_page_with_playwright(v_url)
        if res.get("download_link"):
            extracted_results.append(res)

    if not extracted_results:
        await status_msg.edit_text("❌ Pages mile par stream URLs extract nahi ho sake.")
        return

    # Generate TXT File
    txt_content = f"--- Scraped Video Links (Pages {start_page}-{end_page} | {len(extracted_results)} Items) ---\n\n"
    for idx, item in enumerate(extracted_results, 1):
        txt_content += f"{idx}. Title: {item['title']}\n"
        txt_content += f"   Permanent Video Page: {item['page_url']}\n"
        txt_content += f"   Direct Stream Link: {item['download_link']}\n\n"

    txt_bytes = io.BytesIO(txt_content.encode('utf-8'))
    txt_bytes.name = f"scraped_p{start_page}_to_p{end_page}.txt"

    # Generate HTML File
    html_content = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Scraped Links ({start_page}-{end_page})</title>
<style>
body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #121212; color: #e0e0e0; margin: 20px; }}
.card {{ background: #1e1e1e; padding: 18px; margin-bottom: 15px; border-radius: 8px; border-left: 5px solid #0088cc; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }}
a {{ color: #4da6ff; word-break: break-all; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.tag {{ display: inline-block; background: #0088cc; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; margin-left: 8px; }}
</style></head><body><h2>Scraped Videos Pages {start_page} to {end_page} ({len(extracted_results)} Items)</h2>"""

    for idx, item in enumerate(extracted_results, 1):
        html_content += f"""<div class="card">
<h3>{idx}. {item['title']} <span class="tag">{item['type']}</span></h3>
<p><strong>🔗 Permanent Video Link:</strong> <a href="{item['page_url']}" target="_blank">{item['page_url']}</a></p>
<p><strong>⚡ Direct Stream URL:</strong> <a href="{item['download_link']}" target="_blank">{item['download_link']}</a></p>
</div>"""
    html_content += "</body></html>"

    html_bytes = io.BytesIO(html_content.encode('utf-8'))
    html_bytes.name = f"scraped_p{start_page}_to_p{end_page}.html"

    next_start = end_page + 1
    next_end = next_start + 9
    keyboard = [
        [InlineKeyboardButton(f"▶️ Continue (Pages {next_start}-{next_end})", callback_data="continue_scrape")],
        [InlineKeyboardButton("🛑 Stop Scraping", callback_data="stop_scrape")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    context.user_data['last_url'] = target_url
    context.user_data['next_start'] = next_start

    await message_target.reply_document(
        document=txt_bytes, 
        caption=f"📁 **Pages {start_page}-{end_page} TXT File** ({len(extracted_results)} Links)"
    )
    await message_target.reply_document(
        document=html_bytes, 
        caption=f"🌐 **Pages {start_page}-{end_page} HTML File**\n\nAage ke pages (**{next_start} to {next_end}**) scrape karne ke liye niche button par click karein:",
        reply_markup=reply_markup
    )
    await status_msg.delete()

# ==========================================================
# TELEGRAM BOT HANDLERS
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
        "⚡ **Advanced Playwright Bulk Scraper & Downloader Bot Active!**\n\n"
        "🌐 **Supported Sites (9 Total):**\n"
        "• Beeg.onl | xHamster | Joysporn | Xhaccess | Sxyprn\n"
        "• Pornhub | Spankbang | Redtube | Youporn\n\n"
        "📌 **Features & Usage:**\n"
        "1. **Headless Browser Crawling:** Modern Cloudflare & JS-rendered sites support.\n"
        "2. **Dual Output:** Get TXT and Interactive Dark HTML File.\n"
        "3. **FFmpeg Auto Downloader:** Upload `.txt` file to auto download and upload videos to Telegram.\n\n"
        "🛠️ **Commands:** `/start`, `/stats`, `/stop`, `/userlist`, `/adduser`, `/removeuser`"
    )

async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin command only.")
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        add_user_db(uid)
        await update.message.reply_text(f"✅ User `{uid}` database me add kar diya gaya.", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ **Usage:** `/adduser <user_id>`")

async def removeuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin command only.")
        return
    if context.args and context.args[0].isdigit():
        uid = int(context.args[0])
        remove_user_db(uid)
        await update.message.reply_text(f"🗑️ User `{uid}` database se removal completed.", parse_mode="Markdown")
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
        f"• **Engine:** Playwright Chromium Active 🟢"
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

        for idx, raw_url in enumerate(urls, 1):
            if STOP_PROCESS.get(user_id, False):
                await update.message.reply_text("🛑 **Task Stopped By User!**")
                break

            progress_msg = await update.message.reply_text(f"⏳ **[{idx}/{total}] Processing...**")
            stream_url = raw_url
            video_title = f"Video #{idx}"

            if not (raw_url.endswith('.m3u8') or raw_url.endswith('.mp4')):
                extracted = await scrape_page_with_playwright(raw_url)
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

    print("🤖 Playwright Headless Bot Active!")
    app.run_polling()

if __name__ == "__main__":
    main()
