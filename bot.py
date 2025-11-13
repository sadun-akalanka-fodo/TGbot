# bot.py
import asyncio
import os
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
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

def format_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

# --- Main Menu ---
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Downloader", callback_data="downloader")],
        [InlineKeyboardButton("Cloud Storage", callback_data="cloud")],
        [InlineKeyboardButton("Metadata Inspector", callback_data="inspector")],
        [InlineKeyboardButton("Auto-Forward Rules", callback_data="rules")],
    ]
    if update.effective_user.id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("Admin Broadcast", callback_data="admin_broadcast")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "Welcome to Multi-Tool Bot!\nChoose a feature:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

# --- Downloader Menu ---
async def downloader_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Back", callback_data="main")]]
    text = (
        "*Downloader (2 GB Support)*\n\n"
        "Send:\n"
        "• Direct file link\n"
        "• YouTube link\n\n"
        "Files > 50 MB → sent as document"
    )
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN
    )
    context.user_data["state"] = "awaiting_url"

# --- UPLOAD FILE (SMART) ---
async def upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: Path, caption: str = ""):
    file_size = file_path.stat().st_size
    file_name = file_path.name

    if file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"Too big: {format_size(file_size)} > 2 GB")
        os.remove(file_path)
        return

    msg = await update.message.reply_text("Uploading...")

    try:
        with open(file_path, "rb") as f:
            if file_size <= MEDIA_THRESHOLD:
                # Try as media (preview)
                if file_path.suffix.lower() in {'.mp4', '.mov', '.webm', '.mkv', '.avi'}:
                    await update.message.reply_video(f, caption=caption, filename=file_name)
                elif file_path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'}:
                    await update.message.reply_photo(f, caption=caption)
                elif file_path.suffix.lower() in {'.mp3', '.wav', '.ogg', '.m4a'}:
                    await update.message.reply_audio(f, caption=caption, filename=file_name)
                else:
                    await update.message.reply_document(f, caption=caption, filename=file_name)
            else:
                # > 50 MB → always document
                await update.message.reply_document(f, caption=caption, filename=file_name)
        os.remove(file_path)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"Upload failed: {e}")
        if file_path.exists():
            os.remove(file_path)

# --- YouTube Handler ---
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
            title = info.get('title', 'Unknown')
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
            await upload_file(update, context, temp_path, caption)

    except Exception as e:
        await msg.edit_text(f"Error: {str(e)}")

# --- Direct URL Handler ---
async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "awaiting_url": return
    url = update.message.text.strip()

    # --- YOUTUBE ---
    if "youtube.com" in url or "youtu.be" in url:
        await handle_youtube(update, context, url)
        context.user_data["state"] = None
        return

    # --- DIRECT FILE ---
    is_valid, ext, category = is_direct_file_url(url)
    if not is_valid:
        await update.message.reply_text("Invalid URL. Must be direct file or YouTube link.")
        return

    msg = await update.message.reply_text("Checking size...")
    try:
        async with context.application.bot_data["session"].head(url) as resp:
            if resp.status != 200:
                await msg.edit_text("URL not accessible.")
                return
            size = int(resp.headers.get("content-length", 0))
            if size > MAX_FILE_SIZE:
                await msg.edit_text(f"Too large: {format_size(size)}")
                return
    except:
        await msg.edit_text("Size check failed. Downloading anyway...")

    await msg.edit_text("Downloading...")
    file_path = DATA_DIR / f"dl_{update.effective_user.id}_{int(datetime.now().timestamp())}{ext}"
    try:
        async with context.application.bot_data["session"].get(url) as resp:
            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024*1024):
                    f.write(chunk)

        caption = f"Downloaded from: {url}"
        await upload_file(update, context, file_path, caption)
    except Exception as e:
        await msg.edit_text(f"Error: {e}")
    finally:
        context.user_data["state"] = None

# --- Cloud Storage (unchanged) ---
async def cloud_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Save File", callback_data="cloud_save")],
        [InlineKeyboardButton("My Files", callback_data="cloud_list_1")],
        [InlineKeyboardButton("Wipe All", callback_data="cloud_wipe_confirm")],
        [InlineKeyboardButton("Back", callback_data="main")]
    ]
    await update.callback_query.edit_message_text(
        "Personal Cloud Storage\nSave files using Telegram file_id.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cloud_save_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text(
        "Send a file to save.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cloud")]])
    )
    context.user_data["state"] = "awaiting_file_save"

async def handle_file_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "awaiting_file_save": return
    file = None; file_id = None; file_name = "file"
    if update.message.document:
        file = update.message.document
        file_id = file.file_id
        file_name = file.file_name or "document"
    elif update.message.photo:
        file = update.message.photo[-1]
        file_id = file.file_id
        file_name = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    elif update.message.video:
        file = update.message.video
        file_id = file.file_id
        file_name = getattr(file, "file_name", f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4")
    elif update.message.audio:
        file = update.message.audio
        file_id = file.file_id
        file_name = file.file_name or "audio"
    else:
        await update.message.reply_text("Unsupported file.")
        return

    user = get_user_data(update.effective_user.id)
    user["files"].append({
        "file_id": file_id,
        "file_name": file_name,
        "file_type": file.mime_type or "unknown",
        "size": file.file_size or 0,
        "saved_at": datetime.now().isoformat()
    })
    save_user_data(update.effective_user.id, user)
    await update.message.reply_text(f"Saved: `{file_name}`", parse_mode=ParseMode.MARKDOWN)
    context.user_data["state"] = None
    await cloud_menu(update, context)

# --- (Other functions: cloud_list, resend, inspector, rules, etc. — SAME AS BEFORE) ---
# ... [PASTE FROM PREVIOUS CODE] ...

# --- Callback & Message Handler (unchanged) ---
# ... [PASTE FROM PREVIOUS CODE] ...

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
