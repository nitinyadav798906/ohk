import asyncio
import io
import json
import logging
import os
import re
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, List, Set
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup
import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Logging Setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# CONFIGURATION
BOT_TOKEN = os.getenv("BOT_TOKEN", "7673015455:AAFrMbFSEpPXV33WMUud-bRFPUxvzN7znBk")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1714266885"))

ALLOWED_USERS: Set[int] = {ADMIN_ID}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://xhaccess.com/",
}

# Dummy HTTP Server (Render Port Binding Bypass)
class DummyPortServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"200+ Link Auto-Extract Scraper & Downloader Active!")
    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.getenv("PORT", 8080))
    try:
        server = HTTPServer(('0.0.0.0', port), DummyPortServer)
        server.serve_forever()
    except Exception as e:
        logger.error(f"HTTP Server Exception: {e}")

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
        response = await client.get(video_url, timeout=12.0)
        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, 'html.parser')

        title = "Unknown Title"
        if soup.select_one('h1'):
            title = soup.select_one('h1').get_text(strip=True)
        elif soup.title and soup.title.string:
            title = soup.title.string.replace(" - xHamster.com", "").replace(" - xhaccess.com", "").strip()

        stream_link = None
        file_type = None

        preload = soup.find('link', rel='preload', attrs={'as': 'fetch'})
        if preload and preload.get('href') and '.m3u8' in preload.get('href'):
            stream_link = preload.get('href')
            file_type = "M3U8"

        if not stream_link:
            script = soup.find('script', id='initials-script')
            if script and script.string:
                try:
                    clean_json = script.string.replace('window.initials=', '').rstrip(';')
                    data = json.loads(clean_json)
                    hls = data.get('xplayerSettings', {}).get('hls', {})
                    if 'h264' in hls:
                        stream_link = hls['h264'].get('url')
                        file_type = "M3U8"
                    elif 'av1' in hls:
                        stream_link = hls['av1'].get('url')
                        file_type = "M3U8"
                except Exception:
                    pass

        if not stream_link:
            video_tag = soup.find('video')
            if video_tag:
                if video_tag.get('src') and '.mp4' in video_tag.get('src'):
                    stream_link = video_tag.get('src')
                    file_type = "MP4"
                else:
                    source = video_tag.find('source', attrs={'type': 'video/mp4'})
                    if source and source.get('src'):
                        stream_link = source.get('src')
                        file_type = "MP4"

        if not stream_link:
            regex_hls = r'(https.*?\.m3u8[^"\s]*)'
            regex_mp4 = r'(https.*?\.mp4[^"\s]*)'
            for s in soup.find_all('script'):
                if s.string:
                    m_hls = re.search(regex_hls, s.string)
                    if m_hls:
                        stream_link = m_hls.group(1).replace('\\/', '/')
                        file_type = "M3U8"
                        break
                    m_mp4 = re.search(regex_mp4, s.string)
                    if m_mp4:
                        stream_link = m_mp4.group(1).replace('\\/', '/')
                        file_type = "MP4"
                        break

        if stream_link:
            final_link = process_tpl_link(stream_link) if file_type == "M3U8" else stream_link
            return {
                "title": title,
                "type": file_type,
                "page_url": video_url,
                "download_link": final_link
            }
    except Exception as e:
        logger.error(f"Error extracting {video_url}: {e}")
    return None

async def scrape_multi_pages(url: str, total_pages: int = 6) -> List[dict]:
    base_domain = "https://xhaccess.com"
    all_video_urls = set()

    async with httpx.AsyncClient(headers=HEADERS, verify=False, follow_redirects=True, timeout=15.0) as client:
        if "/videos/" in url and not url.rstrip('/').endswith('/videos'):
            res = await extract_video_link(client, url)
            return [res] if res else []

        page_urls = []
        for p in range(1, total_pages + 1):
            p_url = f"{url}&page={p}" if "?" in url else (f"{url}?page={p}" if p > 1 else url)
            page_urls.append(p_url)

        async def fetch_page_links(p_url):
            try:
                resp = await client.get(p_url)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, 'html.parser')
                    links = soup.select('a.video-thumb__image-container, a[href*="/videos/"]')
                    for a in links:
                        href = a.get('href', '')
                        if "/videos/" in href and not href.endswith('/videos/'):
                            all_video_urls.add(urljoin(base_domain, href))
            except Exception as e:
                logger.error(f"Failed crawling page {p_url}: {e}")

        await asyncio.gather(*[fetch_page_links(pu) for pu in page_urls])

        semaphore = asyncio.Semaphore(20)
        async def sem_extract(v_url):
            async with semaphore:
                return await extract_video_link(client, v_url)

        tasks = [sem_extract(v_url) for v_url in all_video_urls]
        results = await asyncio.gather(*tasks)
        return [res for res in results if res is not None]

# ------------------------------------------------------------------
# TELEGRAM HANDLERS
# ------------------------------------------------------------------

def is_authorized(user_id: int) -> bool:
    return user_id in ALLOWED_USERS or user_id == ADMIN_ID

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ **Access Denied!**")
        return

    await update.message.reply_text(
        "⚡ **200+ Bulk Scraper & Permanent Downloader Bot**\n\n"
        "1. **Scrape:** Category URL bhejein (6 pages scan karke 200+ links extract hongi).\n"
        "2. **Upload Videos:** `.txt` file upload karein -> Bot live link refresh karke saari videos Telegram par upload kar dega."
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ **Access Denied!**")
        return

    doc = update.message.document
    if not doc or not doc.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Kripya valid `.txt` file upload karein.")
        return

    status_msg = await update.message.reply_text("📥 **TXT file process ho rahi hai...**")

    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = io.BytesIO()
        await file.download_to_memory(file_bytes)
        file_content = file_bytes.getvalue().decode('utf-8', errors='ignore')

        urls = re.findall(r'(https?://[^\s]+)', file_content)
        if not urls:
            await status_msg.edit_text("❌ TXT file me koi valid URL nahi mila.")
            return

        total = len(urls)
        await status_msg.edit_text(f"🚀 Total **{total}** links processing me hain...")

        async with httpx.AsyncClient(headers=HEADERS, verify=False, follow_redirects=True, timeout=15.0) as client:
            for idx, raw_url in enumerate(urls, 1):
                progress_msg = await update.message.reply_text(f"⏳ **[{idx}/{total}] Fetching Live Link...**")
                
                stream_url = raw_url
                if "/videos/" in raw_url:
                    extracted = await extract_video_link(client, raw_url)
                    if extracted:
                        stream_url = extracted['download_link']

                output_file = f"video_{idx}.mp4"

                try:
                    cmd = [
                        "yt-dlp",
                        "-o", output_file,
                        "--no-check-certificate",
                        stream_url
                    ]
                    proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    await proc.wait()

                    if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
                        await progress_msg.edit_text(f"📤 **[{idx}/{total}] Uploading to Telegram...**")
                        with open(output_file, 'rb') as vf:
                            await update.message.reply_video(video=vf, caption=f"🎥 **Video {idx}/{total}**")
                        os.remove(output_file)
                        await progress_msg.delete()
                    else:
                        await progress_msg.edit_text(f"❌ **[{idx}/{total}] Download Failed!**")

                except Exception as e:
                    logger.error(f"Error downloading: {e}")
                    await progress_msg.edit_text(f"❌ **[{idx}/{total}] Processing Error!**")

        await status_msg.edit_text("✅ **All videos uploaded successfully!**")

    except Exception as e:
        logger.error(f"Error processing document: {e}")
        await status_msg.edit_text(f"❌ File Process Error: {str(e)}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ **Access Denied!**")
        return

    text_parts = update.message.text.strip().split()
    target_url = text_parts[0]
    pages_to_scrape = 6

    if len(text_parts) > 1 and text_parts[1].isdigit():
        pages_to_scrape = int(text_parts[1])

    if not ("xhaccess.com" in target_url or "xhamster" in target_url):
        await update.message.reply_text("❌ Valid xhaccess URL bhejein ya `.txt` file upload karein.")
        return

    status_msg = await update.message.reply_text(f"🔎 **Scraping Min 200+ Links ({pages_to_scrape} Pages)...**")

    results = await scrape_multi_pages(target_url, total_pages=pages_to_scrape)

    if not results:
        await status_msg.edit_text("❌ Links extract nahi ho sake.")
        return

    await status_msg.edit_text(f"✅ Total **{len(results)}** Videos Extracted! Files ready ho rahi hain...")

    # 1. TXT FILE
    txt_content = f"--- Scraped Video Links ({len(results)} Items) ---\n\n"
    for idx, item in enumerate(results, 1):
        txt_content += f"{idx}. Title: {item['title']}\n"
        txt_content += f"   Type: [{item['type']}]\n"
        txt_content += f"   Permanent Page Link: {item['page_url']}\n"
        txt_content += f"   Live Stream Link: {item['download_link']}\n\n"

    txt_bytes = io.BytesIO(txt_content.encode('utf-8'))
    txt_bytes.name = f"scraped_{len(results)}_links.txt"

    # 2. HTML FILE
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Scraped Links ({len(results)})</title>
    <style>
        body {{ font-family: sans-serif; background: #121212; color: #e0e0e0; margin: 20px; }}
        .card {{ background: #1e1e1e; padding: 15px; margin-bottom: 12px; border-radius: 8px; border-left: 5px solid #0088cc; }}
        a {{ color: #4da6ff; word-break: break-all; }}
    </style>
</head>
<body>
    <h2>Scraped Videos ({len(results)} Total)</h2>
"""
    for idx, item in enumerate(results, 1):
        html_content += f"""
    <div class="card">
        <h3>{idx}. {item['title']} [{item['type']}]</h3>
        <p><strong>Permanent Page Link:</strong> <a href="{item['page_url']}" target="_blank">{item['page_url']}</a></p>
        <p><strong>Live Stream Link:</strong> <a href="{item['download_link']}" target="_blank">{item['download_link']}</a></p>
    </div>"""

    html_content += "\n</body>\n</html>"

    html_bytes = io.BytesIO(html_content.encode('utf-8'))
    html_bytes.name = f"scraped_{len(results)}_links.html"

    await update.message.reply_document(document=txt_bytes, caption=f"📁 **TXT File** ({len(results)} Links)\n\n💡 *Is TXT file ko kisi bhi time bot me bhej kar video download karwa sakte hain.*")
    await update.message.reply_document(document=html_bytes, caption="🌐 **HTML View File**")

    await status_msg.delete()

def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.Document.TXT, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Bot Active!")
    app.run_polling()

if __name__ == "__main__":
    main()
