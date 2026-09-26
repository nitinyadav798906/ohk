import asyncio
import io
import json
import logging
import os
import re
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, List, Set, Dict
from urllib.parse import unquote, urljoin

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# Logging Setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================================
# CONFIGURATION
# ==========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7673015455:AAFrMbFSEpPXV33WMUud-bRFPUxvzN7znBk")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1714266885"))

ALLOWED_USERS: Set[int] = {ADMIN_ID}
STOP_PROCESS: Dict[int, bool] = {}
USER_LIBRARY: Dict[int, List[Dict[str, str]]] = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://xhaccess.com/",
}

# ==========================================================
# DUMMY HTTP SERVER
# ==========================================================
class DummyPortServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Active!")
    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.getenv("PORT", 8080))
    try:
        server = HTTPServer(('0.0.0.0', port), DummyPortServer)
        server.serve_forever()
    except Exception as e:
        logger.error(f"HTTP Server Exception: {e}")

# ==========================================================
# HELPER FUNCTIONS
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

async def extract_video_link(client: httpx.AsyncClient, video_url: str) -> Optional[dict]:
    try:
        response = await client.get(video_url, timeout=10.0)
        if response.status_code != 200:
            return None

        text = response.text
        title = "Video"

        title_match = re.search(r'<h1[^>]*>(.*?)</h1>', text, re.IGNORECASE | re.DOTALL)
        if title_match:
            title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()

        stream_link = None
        file_type = None

        m_hls = re.search(r'(https[^\s"\']*?\.m3u8[^\s"\']*)', text)
        if m_hls:
            stream_link = m_hls.group(1).replace('\\/', '/')
            file_type = "M3U8"
        else:
            m_mp4 = re.search(r'(https[^\s"\']*?\.mp4[^\s"\']*)', text)
            if m_mp4:
                stream_link = m_mp4.group(1).replace('\\/', '/')
                file_type = "MP4"

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
        cmd = [
            "ffmpeg",
            "-y",
            "-headers", f"User-Agent: {HEADERS['User-Agent']}\r\nReferer: {HEADERS['Referer']}\r\n",
            "-i", url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            output_path
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await proc.wait()
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except Exception as e:
        logger.error(f"FFmpeg error: {e}")
        return False

async def scrape_multi_pages_chunk(url: str, start_page: int = 1, end_page: int = 6) -> List[dict]:
    base_domain = "https://xhaccess.com"
    all_video_urls = set()

    limits = httpx.Limits(max_keepalive_connections=200, max_connections=300)
    async with httpx.AsyncClient(headers=HEADERS, verify=False, follow_redirects=True, limits=limits, timeout=10.0) as client:
        # Single Video URL check
        if "/videos/" in url and not url.rstrip('/').endswith('/videos'):
            res = await extract_video_link(client, url)
            return [res] if res else []

        page_urls = []
        for p in range(start_page, end_page + 1):
            if "?" in url:
                p_url = f"{url}&page={p}"
            else:
                p_url = f"{url}?page={p}" if p > 1 else url
            page_urls.append(p_url)

        async def fetch_page_links(p_url):
            try:
                resp = await client.get(p_url)
                if resp.status_code == 200:
                    # Extended Flexible Regex for Video Links
                    found_links = re.findall(r'href=["\'](/videos/[^"\']+)["\']', resp.text)
                    if not found_links:
                        found_links = re.findall(r'href=["\'](https?://[^"\']*/videos/[^"\']+)["\']', resp.text)
                    
                    for href in found_links:
                        if not href.endswith('/videos/') and not href.endswith('/videos'):
                            full_u = href if href.startswith("http") else urljoin(base_domain, href)
                            all_video_urls.add(full_u)
            except Exception as e:
                logger.error(f"Failed crawling page {p_url}: {e}")

        await asyncio.gather(*[fetch_page_links(pu) for pu in page_urls])

        if not all_video_urls:
            return []

        semaphore = asyncio.Semaphore(100)
        async def sem_extract(v_url):
            async with semaphore:
                return await extract_video_link(client, v_url)

        tasks = [sem_extract(v_url) for v_url in all_video_urls]
        results = await asyncio.gather(*tasks)
        return [res for res in results if res is not None]

# ==========================================================
# TELEGRAM HANDLERS
# ==========================================================
def is_authorized(user_id: int) -> bool:
    return user_id in ALLOWED_USERS or user_id == ADMIN_ID

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ **Access Denied!**")
        return

    await update.message.reply_text(
        "⚡ **Ultra-Fast Continuous Scraper & Downloader Bot**\n\n"
        "📌 **General Commands:**\n"
        "• URL bhejein: Ultra-Fast speed se 6 pages scan karke continuous button option dega.\n"
        "• `.txt` file upload karein: Live stream auto-refresh karke FFmpeg se video upload karega.\n"
        "• `/stop` - Running download process ko rokne ke liye.\n"
        "• `/mylibrary` - Apni saved `.txt` files dekhne ke liye.\n"
        "• `/stats` - Total active users aur bot status dekhne ke liye.\n\n"
        "👑 **Admin Commands:**\n"
        "• `/adduser <user_id>` - Access dene ke liye.\n"
        "• `/removeuser <user_id>` - Access hatane ke liye.\n"
        "• `/userlist` - Authorized users ki list dekhne ke liye."
    )

async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Keval Admin hi yeh command use kar sakta hai!")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Usage: `/adduser <user_id>`", parse_mode="Markdown")
        return
    new_user = int(context.args[0])
    ALLOWED_USERS.add(new_user)
    await update.message.reply_text(f"✅ User ID `{new_user}` ko add kar diya gaya hai!", parse_mode="Markdown")

async def removeuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Keval Admin hi yeh command use kar sakta hai!")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Usage: `/removeuser <user_id>`", parse_mode="Markdown")
        return
    rem_user = int(context.args[0])
    if rem_user == ADMIN_ID:
        await update.message.reply_text("❌ Admin ko remove nahi kiya ja sakta!")
        return
    ALLOWED_USERS.discard(rem_user)
    await update.message.reply_text(f"🗑️ User ID `{rem_user}` ko remove kar diya gaya hai!", parse_mode="Markdown")

async def userlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        return
    msg = "👥 **Authorized Users List:**\n\n"
    for uid in ALLOWED_USERS:
        role = "👑 Admin" if uid == ADMIN_ID else "👤 User"
        msg += f"• `{uid}` ({role})\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        return
    total_files = sum(len(files) for files in USER_LIBRARY.values())
    await update.message.reply_text(
        f"📊 **Bot Status & Stats:**\n\n"
        f"• **Authorized Users:** {len(ALLOWED_USERS)}\n"
        f"• **Total Processed Files:** {total_files}\n"
        f"• **Engine:** Concurrency 100 HTTP/2 + FFmpeg Engine\n"
        f"• **Status:** 🟢 Active & Ready"
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    STOP_PROCESS[user_id] = True
    await update.message.reply_text("🛑 **Process Stop Request Bhej Diya Gaya!**")

async def mylibrary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        return
    user_files = USER_LIBRARY.get(user_id, [])
    if not user_files:
        await update.message.reply_text("📚 **Aapki Library Khaali Hai!**")
        return
    msg = "📚 **Aapki Library (Saved Files):**\n\n"
    for idx, item in enumerate(user_files, 1):
        msg += f"{idx}. 📁 `{item['filename']}` ({item['count']} Links)\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ **Access Denied!**")
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

        if user_id not in USER_LIBRARY:
            USER_LIBRARY[user_id] = []
        USER_LIBRARY[user_id].append({"filename": doc.file_name, "count": str(total)})

        await status_msg.edit_text(f"🚀 Total **{total}** links processing me hain! Rokne ke liye `/stop` likhein.")

        limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
        async with httpx.AsyncClient(headers=HEADERS, verify=False, follow_redirects=True, limits=limits, timeout=10.0) as client:
            for idx, raw_url in enumerate(urls, 1):
                if STOP_PROCESS.get(user_id, False):
                    await update.message.reply_text("🛑 **Task Stopped By User!**")
                    break

                progress_msg = await update.message.reply_text(f"⏳ **[{idx}/{total}] Link Extract Ho Raha Hai...**")
                
                stream_url = raw_url
                video_title = f"Video #{idx}"

                if "/videos/" in raw_url and not (raw_url.endswith('.m3u8') or raw_url.endswith('.mp4')):
                    extracted = await extract_video_link(client, raw_url)
                    if extracted and extracted.get('download_link'):
                        stream_url = extracted['download_link']
                        video_title = extracted.get('title', video_title)

                output_file = f"temp_video_{user_id}.mp4"

                if os.path.exists(output_file):
                    os.remove(output_file)

                await progress_msg.edit_text(f"📥 **[{idx}/{total}] FFmpeg Downloader Active...**\n`{video_title[:30]}...`")
                
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
                    os.remove(output_file)

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

        await status_msg.edit_text(f"✅ Total **{len(results)}** Videos Extracted! Preparing files...")

        # TXT File Generation
        txt_content = f"--- Scraped Video Links (Pages {start_page}-{end_page} | {len(results)} Items) ---\n\n"
        for idx, item in enumerate(results, 1):
            txt_content += f"{idx}. Title: {item['title']}\n"
            txt_content += f"   Permanent Page Link: {item['page_url']}\n"
            txt_content += f"   Live Stream Link: {item['download_link']}\n\n"

        txt_bytes = io.BytesIO(txt_content.encode('utf-8'))
        txt_bytes.name = f"scraped_p{start_page}_to_p{end_page}.txt"

        # HTML File Generation
        html_content = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Scraped Links ({start_page}-{end_page})</title>
<style>
body {{ font-family: sans-serif; background: #121212; color: #e0e0e0; margin: 20px; }}
.card {{ background: #1e1e1e; padding: 15px; margin-bottom: 12px; border-radius: 8px; border-left: 5px solid #0088cc; }}
a {{ color: #4da6ff; word-break: break-all; }}
</style></head><body><h2>Scraped Videos Pages {start_page} to {end_page} ({len(results)} Total)</h2>"""
        for idx, item in enumerate(results, 1):
            html_content += f"""<div class="card"><h3>{idx}. {item['title']} [{item['type']}]</h3>
<p><strong>Permanent Page Link:</strong> <a href="{item['page_url']}" target="_blank">{item['page_url']}</a></p>
<p><strong>Live Stream Link:</strong> <a href="{item['download_link']}" target="_blank">{item['download_link']}</a></p></div>"""
        html_content += "</body></html>"

        html_bytes = io.BytesIO(html_content.encode('utf-8'))
        html_bytes.name = f"scraped_p{start_page}_to_p{end_page}.html"

        context.user_data['last_url'] = target_url
        context.user_data['next_start'] = end_page + 1

        next_start = end_page + 1
        next_end = next_start + 5

        keyboard = [
            [InlineKeyboardButton(f"▶️ Continue (Pages {next_start}-{next_end})", callback_data="continue_scrape")],
            [InlineKeyboardButton("🛑 Stop Scraping", callback_data="stop_scrape")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update_or_query.message.reply_document(document=txt_bytes, caption=f"📁 **Pages {start_page}-{end_page} TXT File** ({len(results)} Links)")
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
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ **Access Denied!**")
        return

    text = update.message.text.strip()
    url_match = re.search(r'(https?://[^\s]+)', text)

    if not url_match:
        await update.message.reply_text("❌ Valid URL bhejein!")
        return

    target_url = url_match.group(1)

    if not ("xhaccess" in target_url or "xhamster" in target_url):
        await update.message.reply_text("❌ Yeh domain supported nahi hai. Kripya valid URL bhejein.")
        return

    await run_scrape_chunk(update, context, target_url, start_page=1, end_page=6)

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_authorized(user_id):
        return

    if query.data == "stop_scrape":
        await query.edit_message_caption(caption=query.message.caption + "\n\n🛑 **Scraping Stopped By User.**")
        return

    if query.data == "continue_scrape":
        target_url = context.user_data.get('last_url')
        start_page = context.user_data.get('next_start', 7)
        end_page = start_page + 5

        if not target_url:
            await query.message.reply_text("❌ Target URL lost. Kripya URL firse bhej kar start karein.")
            return

        await run_scrape_chunk(query, context, target_url, start_page=start_page, end_page=end_page)

# ==========================================================
# MAIN EXECUTION
# ==========================================================
def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("mylibrary", mylibrary_command))
    app.add_handler(CommandHandler("stats", stats_command))
    
    app.add_handler(CommandHandler("adduser", adduser_command))
    app.add_handler(CommandHandler("removeuser", removeuser_command))
    app.add_handler(CommandHandler("userlist", userlist_command))
    
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.Document.TXT, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Ultra-Fast Infinite Scraper & Downloader Bot Active!")
    app.run_polling()

if __name__ == "__main__":
    main()
