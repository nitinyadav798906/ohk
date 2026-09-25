import asyncio
import io
import json
import logging
import os
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, List, Dict, Set
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

# ------------------------------------------------------------------
# CONFIGURATION & ACCESS CONTROL
# ------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "7673015455:AAFrMbFSEpPXV33WMUud-bRFPUxvzN7znBk")

# Yahan apna Telegram Numeric ID dalein (e.g., 123456789)
# ID janne ke liye Telegram par @userinfobot se message karein.
ADMIN_ID = int(os.getenv("ADMIN_ID", "1714266885")) 

# Memory Storage for Allowed Users & HTTP Sessions
ALLOWED_USERS: Set[int] = {ADMIN_ID}
USER_SESSIONS: Dict[int, httpx.AsyncClient] = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://xhaccess.com/",
}
# ------------------------------------------------------------------


# Dummy HTTP Server (Render Port Binding Bypass)
class DummyPortServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Admin-Protected Scraper Bot Active!")
    
    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.getenv("PORT", 8080))
    try:
        server = HTTPServer(('0.0.0.0', port), DummyPortServer)
        print(f"🌐 Fake HTTP Server running on port {port}")
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


async def get_user_client(user_id: int) -> httpx.AsyncClient:
    if user_id not in USER_SESSIONS:
        USER_SESSIONS[user_id] = httpx.AsyncClient(
            headers=HEADERS, 
            verify=False, 
            follow_redirects=True, 
            timeout=20.0
        )
    return USER_SESSIONS[user_id]


async def login_to_xhaccess(user_id: int, username: str, password: str) -> bool:
    client = await get_user_client(user_id)
    login_url = "https://xhaccess.com/login"
    
    try:
        resp = await client.get(login_url)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        csrf_token = None
        csrf_input = soup.find('input', {'name': '_token'}) or soup.find('input', {'name': 'csrf_token'})
        if csrf_input:
            csrf_token = csrf_input.get('value')

        payload = {
            "login[username]": username,
            "login[password]": password,
        }
        if csrf_token:
            payload["_token"] = csrf_token

        post_resp = await client.post(login_url, data=payload)
        
        if post_resp.status_code == 200 and ("logout" in post_resp.text.lower() or "my/" in post_resp.text.lower()):
            return True
        return False
    except Exception as e:
        logger.error(f"Login failed for user {user_id}: {e}")
        return False


async def extract_video_link(client: httpx.AsyncClient, video_url: str) -> Optional[dict]:
    try:
        response = await client.get(video_url)
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

        # 1. HLS (.m3u8) Search
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

        # 2. MP4 Direct Link Search
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

        # 3. Fallback Regex
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
                "url": video_url,
                "download_link": final_link
            }

    except Exception as e:
        logger.error(f"Error scraping {video_url}: {e}")
    return None


async def scrape_xhaccess(client: httpx.AsyncClient, url: str, pages: int = 1) -> List[dict]:
    base_domain = "https://xhaccess.com"

    try:
        if "/videos/" in url and not url.rstrip('/').endswith('/videos'):
            result = await extract_video_link(client, url)
            return [result] if result else []

        current_url = url
        visited = set()
        all_video_urls = set()

        for _ in range(pages):
            if not current_url or current_url in visited:
                break
            visited.add(current_url)

            try:
                response = await client.get(current_url)
                if response.status_code != 200:
                    break

                soup = BeautifulSoup(response.text, 'html.parser')
                video_links = soup.select('a.video-thumb__image-container, a[href*="/videos/"]')
                
                for a in video_links:
                    href = a.get('href', '')
                    if "/videos/" in href and not href.endswith('/videos/'):
                        all_video_urls.add(urljoin(base_domain, href))

                next_btn = soup.select_one('a[rel="next"], a.pagination__next')
                current_url = urljoin(base_domain, next_btn.get('href')) if next_btn else None
            except Exception as e:
                logger.error(f"Error pagination on {current_url}: {e}")
                break

        tasks = [extract_video_link(client, v_url) for v_url in all_video_urls]
        results = await asyncio.gather(*tasks)
        return [res for res in results if res is not None]

    except Exception as e:
        logger.error(f"Global Scraper Error: {e}")
        return []


# ------------------------------------------------------------------
# TELEGRAM HANDLERS (ADMIN & PERMISSION LOGIC)
# ------------------------------------------------------------------

def is_authorized(user_id: int) -> bool:
    """Check constraint for authorized users."""
    return user_id in ALLOWED_USERS or user_id == ADMIN_ID


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ **Access Denied!** Aapko is bot ko use karne ki permission nahi hai.")
        return

    admin_info = "\n\n👑 **Admin Commands:**\n`/add <user_id>` - Add user\n`/remove <user_id>` - Remove user\n`/users` - Allowed users list" if user_id == ADMIN_ID else ""

    await update.message.reply_text(
        "👋 **Namaste! Private Scraper Bot Ready.**\n\n"
        "1. **Normal Scrape:** Direct Video/Category URL bhejein.\n"
        "2. **Login Account:** `/login <username> <password>` bhej kar login karein.\n"
        "3. **Folders Scrape:** Favorites/Watch Later URL se poora folder extract karein.\n"
        "4. **Logout:** `/logout` se saved session clear karein."
        f"{admin_info}"
    )


async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to add new users."""
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Sirf **Admin** new users add kar sakta hai.")
        return

    if not context.args:
        await update.message.reply_text("❌ Usage: `/add <user_id>`")
        return

    try:
        new_user = int(context.args[0])
        ALLOWED_USERS.add(new_user)
        await update.message.reply_text(f"✅ User `{new_user}` successfully add ho gaya!")
    except ValueError:
        await update.message.reply_text("❌ Valid User ID enter karein (Numerical ID).")


async def remove_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to remove users."""
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Sirf **Admin** users ko remove kar sakta hai.")
        return

    if not context.args:
        await update.message.reply_text("❌ Usage: `/remove <user_id>`")
        return

    try:
        target_user = int(context.args[0])
        if target_user == ADMIN_ID:
            await update.message.reply_text("❌ Admin ko remove nahi kiya ja sakta.")
            return

        if target_user in ALLOWED_USERS:
            ALLOWED_USERS.remove(target_user)
            await update.message.reply_text(f"🚫 User `{target_user}` remove ho gaya!")
        else:
            await update.message.reply_text("❌ User list me nahi mila.")
    except ValueError:
        await update.message.reply_text("❌ Valid User ID enter karein.")


async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to list authorized users."""
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        return

    users_str = "\n".join([f"- `{uid}`" for uid in ALLOWED_USERS])
    await update.message.reply_text(f"📋 **Allowed Users List:**\n{users_str}")


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ **Access Denied!**")
        return

    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: `/login <username> <password>`")
        return

    username = context.args[0]
    password = context.args[1]

    status = await update.message.reply_text("🔑 **Logging in...**")
    success = await login_to_xhaccess(user_id, username, password)

    if success:
        await status.edit_text("✅ **Login Successful!** Ab aap private Watch Later/Favorites URL scrape kar sakte hain.")
    else:
        await status.edit_text("❌ **Login Failed!** Username/Password check karein.")


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id in USER_SESSIONS:
        del USER_SESSIONS[user_id]
        await update.message.reply_text("🔒 Account **Logged Out** aur session cookies delete ho gayi hain.")
    else:
        await update.message.reply_text("❌ Aap logged in nahi hain.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ **Access Denied!** You are not allowed to use this bot.")
        return

    text = update.message.text.strip()

    if not ("xhaccess.com" in text or "xhamster" in text):
        await update.message.reply_text("❌ Kripya ek valid **xhaccess.com** URL bhejein.")
        return

    status_msg = await update.message.reply_text("🔎 **Scraping in progress...**")
    
    client = await get_user_client(user_id)
    results = await scrape_xhaccess(client, text, pages=1)

    if not results:
        await status_msg.edit_text("❌ Koi bhi `.m3u8` ya `.mp4` video link nahi mil saka.")
        return

    await status_msg.edit_text(f"✅ Total **{len(results)}** videos milli! File generate ho rahi hain...")

    # TXT FILE
    txt_content = f"--- Scraped Video Links ({len(results)} items) ---\n\n"
    for idx, item in enumerate(results, 1):
        txt_content += f"{idx}. Title: {item['title']}\n"
        txt_content += f"   Format: [{item['type']}]\n"
        txt_content += f"   Source URL: {item['url']}\n"
        txt_content += f"   Direct Stream Link: {item['download_link']}\n\n"

    txt_bytes = io.BytesIO(txt_content.encode('utf-8'))
    txt_bytes.name = "scraped_links.txt"

    # HTML FILE
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Scraped Video Links</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #121212; color: #e0e0e0; margin: 20px; }}
        h2 {{ color: #0088cc; text-align: center; }}
        .card {{ background: #1e1e1e; padding: 15px; margin-bottom: 15px; border-radius: 8px; border-left: 5px solid #0088cc; }}
        .badge {{ background: #0088cc; color: white; padding: 3px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }}
        .badge-mp4 {{ background: #28a745; }}
        h3 {{ margin-top: 0; font-size: 18px; color: #ffffff; }}
        a {{ color: #4da6ff; word-break: break-all; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <h2>Scraped Links Result ({len(results)} Videos)</h2>
"""
    for idx, item in enumerate(results, 1):
        badge_class = "badge-mp4" if item['type'] == "MP4" else ""
        html_content += f"""
    <div class="card">
        <h3>{idx}. {item['title']} <span class="badge {badge_class}">{item['type']}</span></h3>
        <p><strong>Page:</strong> <a href="{item['url']}" target="_blank">{item['url']}</a></p>
        <p><strong>Media Link:</strong> <a href="{item['download_link']}" target="_blank">{item['download_link']}</a></p>
    </div>"""

    html_content += "\n</body>\n</html>"

    html_bytes = io.BytesIO(html_content.encode('utf-8'))
    html_bytes.name = "scraped_links.html"

    # Send Documents
    await update.message.reply_document(document=txt_bytes, caption="📁 **TXT Format Result**")
    await update.message.reply_document(document=html_bytes, caption="🌐 **HTML Format Result**")

    await status_msg.delete()


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling update:", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_TOKEN missing!")
        return

    # Start Fake Server for Render Port Check
    threading.Thread(target=run_dummy_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Register Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("logout", logout_command))
    app.add_handler(CommandHandler("add", add_user_command))
    app.add_handler(CommandHandler("remove", remove_user_command))
    app.add_handler(CommandHandler("users", list_users_command))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(global_error_handler)

    print("🤖 Admin-Protected Bot start ho chuka hai!")
    app.run_polling()


if __name__ == "__main__":
    main()
