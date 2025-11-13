# bot.py
import asyncio
import os
import re
import logging
import zipfile
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode
from telegram.error import Conflict

# --- Libraries ---
from PIL import Image
from PIL.ExifTags import TAGS
from hachoir.parser import createParser
from hachoir.metadata import extractMetadata
from mutagen import File as MutagenFile
from tinydb import TinyDB, Query
from tinydb.storages import JSONStorage
from tinydb.middlewares import CachingMiddleware
import yt_dlp
from dotenv import load_dotenv
load_dotenv()

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIG ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
if not BOT_TOKEN or ADMIN_ID == 0:
    raise ValueError("BOT_TOKEN and ADMIN_ID required!")

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# --- Database ---
UserQuery = Query()
users_db = TinyDB(DATA_DIR / "users.json", storage=CachingMiddleware(JSONStorage))

# --- Constants ---
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
MEDIA_THRESHOLD = 50 * 1024 * 1024      # 50 MB

# --- Helpers ---
def get_user_data(user_id: int) -> Dict:
    user = users_db.get(UserQuery.id == user_id)
    if not user:
        user = {"id": user_id, "files": [], "rules": [], "state": None, "temp": {}}
        users_db.insert(user)
    if "temp" not in user:
        user["temp"] = {}
    return user

def save_user_data(user_id: int, data: Dict):
    users_db.upsert(data, UserQuery.id == user_id)

def format_size(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024: return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"

def is_direct_file_url(url: str) -> tuple[bool, str, str]:
    if not url.startswith(('http://', 'https://')):
        return False, "", ""
    match = re.search(r'\.([a-zA-Z0-9]+)(\?.*)?(#.*)?$', url)
    if not match:
        return False, "", ""
    ext = '.' + match.group(1).lower()
    for cat, exts in {
        'video': ('.mp4', '.mkv', '.avi', '.mov', '.webm'),
        'audio': ('.mp3', '.wav', '.ogg', '.m4a', '.flac'),
        'image': ('.jpg', '.jpeg', '.png', '.webp', '.bmp'),
        'document': ('.pdf', '.zip', '.txt', '.docx', '.xlsx', '.pptx')
    }.items():
        if ext in exts:
            return True, ext, cat
    return False, "", ""

# --- UPLOAD AS DOCUMENT ---
async def upload_as_document(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: Path, caption: str = "", filename: str = None):
    if not file_path.exists():
        await update.effective_message.reply_text("File missing.")
        return

    file_size = file_path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        await update.effective_message.reply_text(f"Too big: {format_size(file_size)}")
        os.remove(file_path)
        return

    msg = await update.effective_message.reply_text("Uploading...")

    try:
        with open(file_path, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=InputFile(f, filename=filename or file_path.name),
                caption=caption[:1024],
                filename=filename or file_path.name,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=60,
                pool_timeout=60,
                message_thread_id=update.effective_message.message_thread_id if update.effective_message.is_topic_message else None
            )
        os.remove(file_path)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"Upload failed: {e}")
        if file_path.exists():
            try: os.remove(file_path)
            except: pass

# --- SPLIT VIDEO (FFmpeg) ---
async def split_and_send_video(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: Path, title: str):
    msg = await update.effective_message.reply_text("Splitting video into parts...")

    if not shutil.which("ffmpeg"):
        await msg.edit_text("FFmpeg not installed. Contact admin.")
        return

    part_dir = DATA_DIR / f"parts_{update.effective_user.id}_{int(datetime.now().timestamp())}"
    part_dir.mkdir(exist_ok=True)

    try:
        import subprocess
        cmd = [
            'ffmpeg', '-i', str(file_path),
            '-f', 'segment', '-segment_time', '180',
            '-c', 'copy', '-reset_timestamps', '1',
            '-map', '0', '-loglevel', 'error',
            str(part_dir / "part_%03d.mp4")
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            await msg.edit_text(f"FFmpeg error: {result.stderr[:300]}")
            return

        parts = sorted(part_dir.glob("part_*.mp4"))
        if not parts:
            await msg.edit_text("No parts created.")
            return

        for i, p in enumerate(parts):
            await upload_as_document(update, context, p, f"{title} [Part {i+1}/{len(parts)}]", p.name)
        await msg.edit_text(f"Sent {len(parts)} video parts.")
    except Exception as e:
        await msg.edit_text(f"Split failed: {e}")
    finally:
        if part_dir.exists():
            shutil.rmtree(part_dir)

# --- ZIP INTO PARTS ---
async def zip_and_send_parts(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: Path, title: str):
    msg = await update.effective_message.reply_text("Zipping into parts...")

    zip_dir = DATA_DIR / f"zip_{update.effective_user.id}_{int(datetime.now().timestamp())}"
    zip_dir.mkdir(exist_ok=True)

    try:
        max_part_size = 45 * 1024 * 1024
        part_num = 1
        current_zip_path = zip_dir / f"{title}_part{part_num}.zip"
        current_zip = zipfile.ZipFile(current_zip_path, 'w', zipfile.ZIP_DEFLATED)
        current_size = 0

        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                if current_size + len(chunk) > max_part_size:
                    current_zip.close()
                    await upload_as_document(update, context, current_zip_path, f"{title} [Part {part_num}]", current_zip_path.name)
                    part_num += 1
                    current_zip_path = zip_dir / f"{title}_part{part_num}.zip"
                    current_zip = zipfile.ZipFile(current_zip_path, 'w', zipfile.ZIP_DEFLATED)
                    current_size = 0
                current_zip.writestr(file_path.name, chunk)
                current_size += len(chunk)
        if current_size > 0:
            current_zip.close()
            await upload_as_document(update, context, current_zip_path, f"{title} [Part {part_num}]", current_zip_path.name)
        await msg.edit_text(f"Sent {part_num} zip parts.")
    except Exception as e:
        await msg.edit_text(f"Zip failed: {e}")
    finally:
        if zip_dir.exists():
            shutil.rmtree(zip_dir)

# --- YouTube Quality ---
async def ask_youtube_quality(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    user = get_user_data(update.effective_user.id)
    user["temp"]["yt_url"] = url
    save_user_data(update.effective_user.id, user)

    keyboard = [
        [InlineKeyboardButton("1080p Video", callback_data="yt_1080")],
        [InlineKeyboardButton("720p Video", callback_data="yt_720")],
        [InlineKeyboardButton("480p Video", callback_data="yt_480")],
        [InlineKeyboardButton("Audio Only (MP3)", callback_data="yt_audio")],
        [InlineKeyboardButton("Cancel", callback_data="cancel")]
    ]
    await update.effective_message.reply_text(
        "Choose quality:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# --- YouTube Download ---
async def handle_youtube_download(update: Update, context: ContextTypes.DEFAULT_TYPE, quality: str):
    user = get_user_data(update.effective_user.id)
    url = user["temp"].get("yt_url")
    if not url:
        await update.callback_query.edit_message_text("URL expired.")
        return

    msg = await update.callback_query.edit_message_text("Fetching info...")
    cookies_path = "cookies.txt"
    ydl_opts = {'noplaylist': True, 'quiet': True, 'no_warnings': True}
    if os.path.exists(cookies_path):
        ydl_opts['cookiefile'] = cookies_path

    if quality == "audio":
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
        ext = ".mp3"
    else:
        height = int(quality)
        ydl_opts['format'] = f'best[height<={height}]/best'
        ext = ".mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'video')
            filesize = info.get('filesize_approx', 0) or 0

            if filesize > MAX_FILE_SIZE:
                await msg.edit_text(f"Too big: {format_size(filesize)}")
                return

            await msg.edit_text(f"Downloading: {title}")

            temp_path = DATA_DIR / f"yt_{update.effective_user.id}_{int(datetime.now().timestamp())}{ext}"
            ydl_opts['outtmpl'] = str(temp_path)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            file_size = temp_path.stat().st_size
            caption = f"{title}\nFrom: {url}"

            if file_size > MEDIA_THRESHOLD:
                keyboard = [
                    [InlineKeyboardButton("Split Video Parts", callback_data=f"split_video|{temp_path.name}")],
                    [InlineKeyboardButton("Zip Parts", callback_data=f"zip_parts|{temp_path.name}")],
                    [InlineKeyboardButton("Cancel", callback_data="cancel")]
                ]
                await msg.edit_text(
                    f"File is {format_size(file_size)} (>50MB)\nChoose:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                user["temp"]["pending_file"] = str(temp_path)
                save_user_data(update.effective_user.id, user)
            else:
                await upload_as_document(update, context, temp_path, caption, f"{title}{ext}")

    except Exception as e:
        await msg.edit_text(f"Error: {e}")

# --- HANDLE SPLIT/ZIP CHOICE ---
async def handle_split_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, filename: str):
    user_id = update.effective_user.id
    user = get_user_data(user_id)
    pending_file = user["temp"].get("pending_file")

    if pending_file != f"{DATA_DIR}/{filename}":
        await update.callback_query.edit_message_text("File expired. Download again.")
        return

    file_path = Path(pending_file)
    if not file_path.exists():
        await update.callback_query.edit_message_text("File not found.")
        return

    title = file_path.stem

    if action == "split_video":
        await split_and_send_video(update, context, file_path, title)
    elif action == "zip_parts":
        await zip_and_send_parts(update, context, file_path, title)

    # Clear temp
    user["temp"].pop("pending_file", None)
    user["temp"].pop("yt_url", None)
    save_user_data(user_id, user)

# --- Direct URL ---
async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    if "youtube.com" in url or "youtu.be" in url:
        await ask_youtube_quality(update, context, url)
        return

    is_valid, ext, _ = is_direct_file_url(url)
    if not is_valid:
        await update.effective_message.reply_text("Unsupported URL.")
        return

    msg = await update.effective_message.reply_text("Checking...")
    try:
        async with context.application.bot_data["session"].head(url) as resp:
            size = int(resp.headers.get("content-length", 0))
            if size > MAX_FILE_SIZE:
                await msg.edit_text(f"Too large: {format_size(size)}")
                return
    except: pass

    await msg.edit_text("Downloading...")
    file_path = DATA_DIR / f"dl_{update.effective_user.id}_{int(datetime.now().timestamp())}{ext}"
    try:
        async with context.application.bot_data["session"].get(url) as resp:
            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024*1024):
                    f.write(chunk)

        file_size = file_path.stat().st_size
        caption = f"From: {url}"

        if file_size > MEDIA_THRESHOLD:
            keyboard = [
                [InlineKeyboardButton("Split into Parts", callback_data=f"split_video|{file_path.name}")],
                [InlineKeyboardButton("Zip Parts", callback_data=f"zip_parts|{file_path.name}")],
                [InlineKeyboardButton("Cancel", callback_data="cancel")]
            ]
            await msg.edit_text(
                f"File is {format_size(file_size)} (>50MB)\nChoose:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            user = get_user_data(update.effective_user.id)
            user["temp"]["pending_file"] = str(file_path)
            save_user_data(update.effective_user.id, user)
        else:
            await upload_as_document(update, context, file_path, caption, file_path.name)
    except Exception as e:
        await msg.edit_text(f"Error: {e}")

# --- Main Menu ---
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Downloader", callback_data="downloader")],
        [InlineKeyboardButton("Cloud Storage", callback_data="cloud")],
        [InlineKeyboardButton("Inspector", callback_data="inspector")],
        [InlineKeyboardButton("Auto-Forward", callback_data="rules")],
    ]
    if update.effective_user.id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("Broadcast", callback_data="admin_broadcast")])
    await update.effective_message.reply_text(
        "Welcome! Choose:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# --- Downloader Menu ---
async def downloader_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Back", callback_data="main")]]
    await update.callback_query.edit_message_text(
        "Send:\n• YouTube link\n• Direct file link\n\n>50MB → Split or Zip",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# --- Callback Handler ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "main":
        await main_menu(update, context)
    elif data == "downloader":
        await downloader_menu(update, context)
    elif data.startswith("yt_"):
        await handle_youtube_download(update, context, data.split("_")[1])
    elif data.startswith("split_video|") or data.startswith("zip_parts|"):
        action, filename = data.split("|", 1)
        await handle_split_choice(update, context, action.split("_")[0], filename)
    elif data == "cancel":
        await q.edit_message_text("Cancelled.")
        user = get_user_data(update.effective_user.id)
        user["temp"].clear()
        save_user_data(update.effective_user.id, user)

# --- Message Handler ---
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if text.startswith(("http://", "https://")):
        await handle_url(update, context, text)

# --- Start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await main_menu(update, context)

# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")

# --- Main ---
async def main():
    import aiohttp
    session = aiohttp.ClientSession()
    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["session"] = session

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.COMMAND, start))
    app.add_error_handler(error_handler)

    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Conflict:
        logger.warning("Another instance running. Exiting.")
        return

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot is running...")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
