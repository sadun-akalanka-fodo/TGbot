# bot.py
import asyncio
import os
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode

# --- Metadata Libraries ---
from PIL import Image
from PIL.ExifTags import TAGS
from hachoir.parser import createParser
from hachoir.metadata import extractMetadata
from mutagen import File as MutagenFile

# --- TinyDB ---
from tinydb import TinyDB, Query
from tinydb.storages import JSONStorage
from tinydb.middlewares import CachingMiddleware

# --- yt-dlp ---
import yt_dlp

# --- .env ---
from dotenv import load_dotenv
load_dotenv()

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIG ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is required!")
if ADMIN_ID == 0:
    raise ValueError("ADMIN_ID is required!")

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# --- Database ---
UserQuery = Query()
users_db = TinyDB(DATA_DIR / "users.json", storage=CachingMiddleware(JSONStorage))

# --- Constants ---
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
MEDIA_THRESHOLD = 50 * 1024 * 1024      # 50 MB
SUPPORTED_EXTS = {
    'video': ('.mp4', '.mkv', '.avi', '.mov', '.webm'),
    'audio': ('.mp3', '.wav', '.ogg', '.m4a', '.flac'),
    'image': ('.jpg', '.jpeg', '.png', '.webp', '.bmp'),
    'document': ('.pdf', '.zip', '.txt', '.docx', '.xlsx', '.pptx')
}
PAGE_SIZE = 5

# --- Helpers ---
def get_user_data(user_id: int) -> Dict:
    user = users_db.get(UserQuery.id == user_id)
    if not user:
        user = {"id": user_id, "files": [], "rules": [], "state": None}
        users_db.insert(user)
    return user

def save_user_data(user_id: int, data: Dict):
    users_db.upsert(data, UserQuery.id == user_id)

def is_direct_file_url(url: str) -> tuple[bool, str, str]:
    if not url.startswith(('http://', 'https://')):
        return False, "", ""
    match = re.search(r'\.([a-zA-Z0-9]+)(\?.*)?(#.*)?$', url)
    if not match:
        return False, "", ""
    ext = '.' + match.group(1).lower()
    for cat, exts in SUPPORTED_EXTS.items():
        if ext in exts:
            return True, ext, cat
    return False, "", ""

def format_size(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024: return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"

# --- UPLOAD FILE AS DOCUMENT (2 GB SAFE) ---
async def upload_as_document(update: Update, file_path: Path, caption: str = "", filename: str = None):
    if not file_path.exists():
        await update.message.reply_text("File missing.")
        return

    file_size = file_path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"Too big: {format_size(file_size)}")
        os.remove(file_path)
        return

    msg = await update.message.reply_text("Uploading as document...")

    try:
        with open(file_path, "rb") as f:
            await update.message.reply_document(
                document=InputFile(f, filename=filename or file_path.name),
                caption=caption
            )
        os.remove(file_path)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"Upload failed: {e}")
        if file_path.exists():
            os.remove(file_path)

# --- YouTube Handler (ALWAYS AS DOCUMENT) ---
async def handle_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text("Fetching info...")

    ydl_opts = {
        'format': 'best[height<=1080]',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'video')
            duration = info.get('duration', 0)
            filesize = info.get('filesize_approx', 0) or 0

            if filesize > MAX_FILE_SIZE:
                await msg.edit_text(f"Too big: {format_size(filesize)}")
                return

            await msg.edit_text(f"Downloading: {title}")

            temp_path = DATA_DIR / f"yt_{update.effective_user.id}_{int(datetime.now().timestamp())}.mp4"
            ydl_opts['outtmpl'] = str(temp_path)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            caption = f"{title}\nFrom: {url}"
            await upload_as_document(update, temp_path, caption, f"{title}.mp4")

    except Exception as e:
        await msg.edit_text(f"Error: {str(e)}")

# --- Direct URL Handler (SMART UPLOAD) ---
async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "awaiting_url": return
    url = update.message.text.strip()

    if "youtube.com" in url or "youtu.be" in url:
        await handle_youtube(update, context, url)
        context.user_data["state"] = None
        return

    is_valid, ext, category = is_direct_file_url(url)
    if not is_valid:
        await update.message.reply_text("Invalid URL.")
        return

    msg = await update.message.reply_text("Checking...")
    try:
        async with context.application.bot_data["session"].head(url) as resp:
            size = int(resp.headers.get("content-length", 0))
            if size > MAX_FILE_SIZE:
                await msg.edit_text(f"Too large: {format_size(size)}")
                return
    except:
        pass

    await msg.edit_text("Downloading...")
    file_path = DATA_DIR / f"dl_{update.effective_user.id}_{int(datetime.now().timestamp())}{ext}"
    try:
        async with context.application.bot_data["session"].get(url) as resp:
            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024*1024):
                    f.write(chunk)

        file_size = file_path.stat().st_size
        caption = f"From: {url}"

        if file_size > MEDIA_THRESHOLD or category in ['document']:
            await upload_as_document(update, file_path, caption, file_path.name)
        else:
            with open(file_path, "rb") as f:
                if category == "video":
                    await update.message.reply_video(f, caption=caption, filename=file_path.name)
                elif category == "audio":
                    await update.message.reply_audio(f, caption=caption, filename=file_path.name)
                elif category == "image":
                    await update.message.reply_photo(f, caption=caption)
                else:
                    await update.message.reply_document(f, caption=caption, filename=file_path.name)
            os.remove(file_path)
    except Exception as e:
        await msg.edit_text(f"Error: {e}")
    finally:
        context.user_data["state"] = None

# --- [REST OF CODE: cloud, inspector, rules, etc. — SAME AS BEFORE] ---
# ... (copy from previous full version)

# --- Start Command ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await main_menu(update, context)

# --- Main ---
async def main():
    import aiohttp
    session = aiohttp.ClientSession()
    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["session"] = session

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Bot is running...")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
