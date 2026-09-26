import asyncio
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, List, Set, Dict
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
# CONFIGURATION & GLOBAL VARIABLES
# ==========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7673015455:AAFrMbFSEpPXV33WMUud-bRFPUxvzN7znBk")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1714266885"))

ALLOWED_USERS: Set[int] = {ADMIN_ID}
STOP_PROCESS: Dict[int, bool] = {}
USER_LIBRARY: Dict[int, List[Dict[str, str]]] = {}

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

# Cloudscraper Instance
scraper = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
)

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
    """ Self ping to prevent Render/Koyeb sleeping """
    render_app_url = os.getenv("RENDER_EXTERNAL_URL")
    while True:
        time.sleep(600)  # Ping every 10 minutes
        if render_app_url:
            try:
                requests.get(render_app_url, timeout=10)
                logger.info("Self-ping sent successfully!")
            except Exception as e:
                logger.error(f"Self-ping failed: {e}")

# ==========================================================
# EXTRACTION & SCRAPING CORE ENGINE
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

async def scrape_multi_pages_chunk(url: str, start_page: int = 1, end_page: int = 6) -> List[dict]:
    all_video_urls = set()
    parsed = urlparse(url)
    domain_name = parsed.netloc or "xhamster.com"
    base_domain = f"https://{domain_name}"

    is_single_video = (
        url.endswith('.html') or 
        re.search(r'/videos?/[^/]+-\d+', url) or 
        re.search(r'/video/\d+', url)
    )
    
    if is_single_video and not any(url.endswith(x) for x in ['index.html', 'ilisting.html', '/']):
        res = await extract_video_link(url)
        return [res] if res else []

    page_urls = []
    base_u = url.rstrip('/')

    for p in range(start_page, end_page + 1):
        if p == 1:
            page_urls.append(url)
            continue
        
        if "joysporn" in domain_name:
            if base_u == base_domain:
                page_urls.append(f"{base_domain}/apapu/{p}/")
            elif base_u.endswith('.html'):
                page_urls.append(url)
            else:
                page_urls.append(f"{base_u}/{p}/")
                page_urls.append(f"{base_u}?page={p}")

        elif "xhamster" in domain_name or "xhaccess" in domain_name:
            page_urls.append(f"{base_u}/{p}")
            page_urls.append(f"{base_u}?page={p}")
            if "?" in url:
                page_urls.append(f"{url}&page={p}")

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
                
                if "joysporn" in domain_name:
                    if clean_href.endswith('.html') or "/video/" in clean_href or "/videos/" in clean_href:
                        if not any(clean_href.endswith(x) for x in ['index.html', 'main.html', 'ilisting.html']):
                            all_video_urls.add(full_u)

                elif "xhamster" in domain_name or "xhaccess" in domain_name:
                    if "/videos/" in clean_href or "/video/" in clean_href:
                        if not re.search(r'/videos?/?$', clean_href):
                            if (
                                re.search(r'-\d+$', clean_href) or 
                                re.search(r'/\d+$', clean_href) or 
                                clean_href.endswith('.html') or
                                len(clean_href.strip('/').split('/')) >= 3
                            ):
                                all_video_urls.add(full_u)

        except Exception as e:
            logger.error(f"Error crawling page {p_url}: {e}")

    await asyncio.gather(*[fetch_page_links(pu) for pu in page_urls])

    if not all_video_urls:
        return []

    semaphore = asyncio.Semaphore(25)
    async def sem_extract(v_url):
        async with semaphore:
            return await extract_video_link(v_url)

    tasks = [sem_extract(v_url) for v_url in all_video_urls]
    results = await asyncio.gather(*tasks)
    
    return [res for res in results if res is not None]

# ==========================================================
# TELEGRAM BOT HANDLERS
# ==========================================================
def is_authorized(user_id: int) -> bool:
    return user_id in ALLOWED_USERS or user_id == ADMIN_ID

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ **Access Denied! Aap is bot ko use nahi kar sakte.**")
        return

    await update.message.reply_text(
        "⚡ **Multi-Site Complete Scraper & Downloader Bot Active!**\n\n"
        "🌐 **Supported Sites:** xHamster, Joysporn, Xhaccess\n\n"
        "📌 **Features & Usage:**\n"
        "1. **Direct Link Scraping:** Koi bhi URL bhejein, bot Pages 1 to 6 tak ka total ~200-300 video links nikal kar `.txt` aur `.html` file dega.\n"
        "2. **Auto Continue Button:** Next pages (7 to 12) scrape karne ke liye inline button milega.\n"
        "3. **Video Downloader:** Kisi bhi `.txt` file ko upload karein, bot FFmpeg se videos download karke direct Telegram par upload karega.\n\n"
        "🛠️ **Commands:**\n"
        "• `/stop` - Running task ko rokne ke liye.\n"
        "• `/mylibrary` - Apni saved files dekhein.\n"
        "• `/stats` - Bot status dekhein.\n\n"
        "👑 **Admin Commands:**\n"
        "• `/adduser <user_id>`\n"
        "• `/removeuser <user_id>`\n"
        "• `/userlist`"
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
    await update.message.reply_text(f"✅ User ID `{new_user}` ko whitelist kar diya gaya hai!", parse_mode="Markdown")

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
    await update.message.reply_text(f"🗑️ User ID `{rem_user}` ko access se hata diya gaya hai!", parse_mode="Markdown")

async def userlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        return
    msg = "👥 **Authorized Users:**\n\n"
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
        f"📊 **Bot Operational Status:**\n\n"
        f"• **Authorized Users:** {len(ALLOWED_USERS)}\n"
        f"• **Total Files Processed:** {total_files}\n"
        f"• **Bypass Engine:** Cloudflare Bypass Active\n"
        f"• **Status:** 🟢 Active & Ready"
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    STOP_PROCESS[user_id] = True
    await update.message.reply_text("🛑 **Process Stop Request Bhej Diya Gaya Hai!**")

async def mylibrary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        return
    user_files = USER_LIBRARY.get(user_id, [])
    if not user_files:
        await update.message.reply_text("📚 **Aapki Library Khaali Hai!**")
        return
    msg = "📚 **Aapki Saved Files:**\n\n"
    for idx, item in enumerate(user_files, 1):
        msg += f"{idx}. 📁 `{item['filename']}` ({item['count']} Links)\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
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

        await status_msg.edit_text(f"🚀 Total **{total}** links processing me hain! Rokne ke liye `/stop` bhejein.")

        for idx, raw_url in enumerate(urls, 1):
            if STOP_PROCESS.get(user_id, False):
                await update.message.reply_text("🛑 **Task Stopped By User!**")
                break

            progress_msg = await update.message.reply_text(f"⏳ **[{idx}/{total}] Link Extract Ho Raha Hai...**")
            
            stream_url = raw_url
            video_title = f"Video #{idx}"

            if ("video" in raw_url or raw_url.endswith('.html')) and not (raw_url.endswith('.m3u8') or raw_url.endswith('.mp4')):
                extracted = await extract_video_link(raw_url)
                if extracted and extracted.get('download_link'):
                    stream_url = extracted['download_link']
                    video_title = extracted.get('title', video_title)

            output_file = f"temp_video_{user_id}_{idx}.mp4"

            # Pre-cleanup
            if os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except Exception:
                    pass

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

            # Post-cleanup to release RAM/Disk
            if os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except Exception:
                    pass

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

        await status_msg.edit_text(f"✅ Total **{len(results)}** Videos Extracted! Files tayar ho rahi hain...")

        txt_content = f"--- Scraped Video Links (Pages {start_page}-{end_page} | {len(results)} Items) ---\n\n"
        for idx, item in enumerate(results, 1):
            txt_content += f"{idx}. Title: {item['title']}\n"
            txt_content += f"   Permanent Page Link: {item['page_url']}\n"
            txt_content += f"   Live Stream Link: {item['download_link']}\n\n"

        txt_bytes = io.BytesIO(txt_content.encode('utf-8'))
        txt_bytes.name = f"scraped_p{start_page}_to_p{end_page}.txt"

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

    if not ("joysporn" in target_url or "xhaccess" in target_url or "xhamster" in target_url):
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
# MAIN EXECUTION ENTRYPOINT
# ==========================================================
def main():
    # Start Dummy Web Server & Keep-Alive Ping
    threading.Thread(target=run_dummy_server, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()
    
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
    
    print("🤖 Ultra-Fast Cloudflare-Bypassed Scraper & Downloader Active!")
    app.run_polling()

if __name__ == "__main__":
    main()
