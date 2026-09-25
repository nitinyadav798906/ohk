import asyncio
import io
import json
import os
import re
from typing import Optional
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

# ------------------------------------------------------------------
# Render Environment Variables se Token Automatically Read hoga
BOT_TOKEN = "7673015455:AAFrMbFSEpPXV33WMUud-bRFPUxvzN7znBk"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.google.com/",
}
# ------------------------------------------------------------------

def process_tpl_link(hls_link: str) -> str:
    """_TPL_ wale template links ko best resolution se replace karta hai."""
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
    """Single Video Page se .m3u8 ya .mp4 link extract karta hai."""
    try:
        response = await client.get(video_url, timeout=15.0)
        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, 'html.parser')

        # Title Extractions
        title = "Unknown Title"
        if soup.select_one('h1'):
            title = soup.select_one('h1').get_text(strip=True)
        elif soup.title:
            title = soup.title.string.replace(" - xHamster.com", "").replace(" - xhaccess.com", "").strip()

        stream_link = None
        file_type = None

        # ------------------ 1. SEARCH FOR .M3U8 (HLS) ------------------
        # Method A: Preload tag
        preload = soup.find('link', rel='preload', attrs={'as': 'fetch'})
        if preload and preload.get('href') and '.m3u8' in preload.get('href'):
            stream_link = preload.get('href')
            file_type = "M3U8"

        # Method B: window.initials JSON Script
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

        # ------------------ 2. SEARCH FOR .MP4 (DIRECT LINK) ------------------
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

        # ------------------ 3. REGEX FALLBACK SEARCH ------------------
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

        # Result Payload
        if stream_link:
            final_link = process_tpl_link(stream_link) if file_type == "M3U8" else stream_link
            return {
                "title": title,
                "type": file_type,
                "url": video_url,
                "download_link": final_link
            }

    except Exception:
        pass
    return None


async def scrape_xhaccess(url: str, pages: int = 1):
    """Category/Search ya Direct Video Link ko process karta hai."""
    base_domain = "https://xhaccess.com"

    async with httpx.AsyncClient(headers=HEADERS, verify=False, follow_redirects=True) as client:
        # Direct Video URL
        if "/videos/" in url:
            result = await extract_video_link(client, url)
            return [result] if result else []

        # Multi-page Crawling
        current_url = url
        visited = set()
        all_video_urls = set()

        for _ in range(pages):
            if not current_url or current_url in visited:
                break
            visited.add(current_url)

            try:
                response = await client.get(current_url, timeout=15.0)
                if response.status_code != 200:
                    break

                soup = BeautifulSoup(response.text, 'html.parser')
                video_links = soup.select('a.video-thumb__image-container')
                for a in video_links:
                    href = a.get('href', '')
                    if "/videos/" in href:
                        all_video_urls.add(urljoin(base_domain, href))

                next_btn = soup.select_one('a[rel="next"]')
                current_url = urljoin(base_domain, next_btn.get('href')) if next_btn else None
            except Exception:
                break

        # Parallel Scraping
        tasks = [extract_video_link(client, v_url) for v_url in all_video_urls]
        results = await asyncio.gather(*tasks)
        return [res for res in results if res is not None]


# ------------------------------------------------------------------
# Telegram Handlers
# ------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Namaste!**\n\n"
        "Mujhe kisi bhi **xhaccess.com** page ka URL bhejein.\n"
        "Main video ke **.m3u8** (HLS) aur **.mp4** links scrap karke **.txt** aur **.html** file me bhej dunga."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not ("xhaccess.com" in text or "xhamster" in text):
        await update.message.reply_text("❌ Kripya ek valid **xhaccess.com** link bhejein.")
        return

    status_msg = await update.message.reply_text("🔎 **Scraping shuru ho gayi hai, kripya thoda intezar karein...**")

    results = await scrape_xhaccess(text, pages=1)

    if not results:
        await status_msg.edit_text("❌ Koi bhi `.m3u8` ya `.mp4` video link nahi mil saka.")
        return

    await status_msg.edit_text(f"✅ Total **{len(results)}** videos milli! File taiyar ki ja rahi hai...")

    # 1. TXT FILE
    txt_content = f"--- Scraped Links ({len(results)} videos) ---\n\n"
    for idx, item in enumerate(results, 1):
        txt_content += f"{idx}. Title: {item['title']}\n"
        txt_content += f"   Format: [{item['type']}]\n"
        txt_content += f"   Page URL: {item['url']}\n"
        txt_content += f"   Stream/Download Link: {item['download_link']}\n\n"

    txt_bytes = io.BytesIO(txt_content.encode('utf-8'))
    txt_bytes.name = "scraped_links.txt"

    # 2. HTML FILE
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Scraped Video Links</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #121212; color: #e0e0e0; margin: 20px; }}
        h2 {{ color: #0088cc; text-align: center; }}
        .card {{ background: #1e1e1e; padding: 15px; margin-bottom: 15px; border-radius: 8px; border-left: 5px solid #0088cc; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }}
        .badge {{ background: #0088cc; color: white; padding: 3px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }}
        .badge-mp4 {{ background: #28a745; }}
        h3 {{ margin-top: 0; font-size: 18px; color: #ffffff; }}
        a {{ color: #4da6ff; word-break: break-all; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .label {{ font-weight: bold; color: #aaa; display: inline-block; width: 110px; }}
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
        <p><span class="label">Page URL:</span> <a href="{item['url']}" target="_blank">{item['url']}</a></p>
        <p><span class="label">Media Link:</span> <a href="{item['download_link']}" target="_blank">{item['download_link']}</a></p>
    </div>"""

    html_content += "\n</body>\n</html>"

    html_bytes = io.BytesIO(html_content.encode('utf-8'))
    html_bytes.name = "scraped_links.html"

    # Send Documents to User
    await update.message.reply_document(document=txt_bytes, caption="📁 **TXT File Format**")
    await update.message.reply_document(document=html_bytes, caption="🌐 **HTML File Format**")

    await status_msg.delete()


def main():
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_TOKEN Environment Variable nahi mila!")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot start ho chuka hai!")
    app.run_polling()


if __name__ == "__main__":
    main()
