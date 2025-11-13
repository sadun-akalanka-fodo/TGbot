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

# --- UPLOAD AS DOCUMENT (2 GB SAFE) ---
async def upload_as_document(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: Path, caption: str = "", filename: str = None):
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
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=InputFile(f, filename=filename or file_path.name),
                caption=caption,
                message_thread_id=update.effective_message.message_thread_id if update.effective_message.is_topic_message else None
            )
        os.remove(file_path)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"Upload failed: {e}")
        if file_path.exists():
            os.remove(file_path)

# --- YouTube Handler (WITH COOKIES) ---
async def handle_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text("Fetching info...")

    cookies_path = "cookies.txt"
    ydl_opts = {
        'format': 'best[height<=1080]',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
    }

    if os.path.exists(cookies_path):
        ydl_opts['cookiefile'] = cookies_path
        logger.info("Using cookies.txt")
    else:
        await msg.edit_text("Warning: cookies.txt missing. Limited access.")
        ydl_opts['format'] = 'best[height<=720]'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'video')
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
            await upload_as_document(update, context, temp_path, caption, f"{title}.mp4")

    except Exception as e:
        error_msg = str(e)
        if "Sign in" in error_msg or "bot" in error_msg.lower():
            await msg.edit_text("Warning: YouTube blocked. Update cookies.txt (log in first).")
        else:
            await msg.edit_text(f"Error: {error_msg}")

# --- Direct URL Handler ---
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
            await upload_as_document(update, context, file_path, caption, file_path.name)
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

# --- Cloud Storage ---
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

async def cloud_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    user = get_user_data(update.effective_user.id)
    files = user["files"]
    total = len(files)
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(max(page, 1), pages)
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_files = files[start:end]

    text = f"My Files ({total}) - Page {page}/{pages}\n\n"
    keyboard = []
    for i, f in enumerate(page_files):
        text += f"{start + i + 1}. `{f['file_name']}` ({format_size(f['size'])})\n"
        keyboard.append([InlineKeyboardButton("Resend", callback_data=f"cloud_resend_{start + i}")])
    nav = []
    if page > 1: nav.append(InlineKeyboardButton("Prev", callback_data=f"cloud_list_{page-1}"))
    if page < pages: nav.append(InlineKeyboardButton("Next", callback_data=f"cloud_list_{page+1}"))
    if nav: keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("Back", callback_data="cloud")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def cloud_resend(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    user = get_user_data(update.effective_user.id)
    if idx >= len(user["files"]):
        await update.callback_query.answer("File not found.", show_alert=True)
        return
    file = user["files"][idx]
    try:
        if "photo" in file["file_name"]:
            await context.bot.send_photo(update.effective_chat.id, file["file_id"], caption=file["file_name"])
        elif "video" in file["file_name"]:
            await context.bot.send_video(update.effective_chat.id, file["file_id"], caption=file["file_name"])
        elif "audio" in file["file_name"]:
            await context.bot.send_audio(update.effective_chat.id, file["file_id"], caption=file["file_name"])
        else:
            await context.bot.send_document(update.effective_chat.id, file["file_id"], caption=file["file_name"])
    except:
        await update.callback_query.answer("Failed to resend.", show_alert=True)

async def cloud_wipe_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text(
        "Delete ALL saved files?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("YES", callback_data="cloud_wipe_yes")],
            [InlineKeyboardButton("Cancel", callback_data="cloud")]
        ])
    )

async def cloud_wipe_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_data(update.effective_user.id)
    user["files"] = []
    save_user_data(update.effective_user.id, user)
    await update.callback_query.edit_message_text("All files deleted.")
    await asyncio.sleep(1)
    await cloud_menu(update, context)

# --- Metadata Inspector ---
async def inspector_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text(
        "Send a file to analyze.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="main")]])
    )
    context.user_data["state"] = "awaiting_inspector_file"

async def handle_inspector_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "awaiting_inspector_file": return
    file = None
    if update.message.document: file = update.message.document
    elif update.message.photo: file = update.message.photo[-1]
    elif update.message.video: file = update.message.video
    elif update.message.audio: file = update.message.audio
    else:
        await update.message.reply_text("Unsupported.")
        return

    msg = await update.message.reply_text("Analyzing...")
    try:
        tg_file = await context.bot.get_file(file.file_id)
        path = DATA_DIR / f"inspect_{update.effective_user.id}_{file.file_id}"
        await tg_file.download_to_drive(path)

        metadata = {}
        if path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'}:
            with Image.open(path) as img:
                metadata = {"Format": img.format, "Resolution": f"{img.width}x{img.height}", "Size": format_size(file.file_size or 0)}
                exif = img.getexif()
                if exif:
                    for tag_id in exif:
                        tag = TAGS.get(tag_id, tag_id)
                        if tag in ["DateTime", "Model", "Software"]:
                            metadata[f"EXIF {tag}"] = str(exif[tag_id])
        else:
            parser = createParser(str(path))
            if parser:
                meta = extractMetadata(parser)
                if meta:
                    for line in meta.exportPlaintext():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            metadata[k.strip()] = v.strip()
            if not metadata and path.suffix.lower() in {'.mp3', '.m4a'}:
                audio = MutagenFile(path)
                if audio:
                    metadata["Duration"] = f"{int(audio.info.length // 60)}:{int(audio.info.length % 60):02d}"
                    metadata["Bitrate"] = f"{audio.info.bitrate // 1000} kbps"

        text = "*Metadata*\n\n"
        for k, v in metadata.items():
            text += f"• *{k}*: `{v}`\n"
        if not metadata: text += "No metadata."
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        os.remove(path)
    except Exception as e:
        await msg.edit_text(f"Error: {e}")
    context.user_data["state"] = None

# --- Auto-Forward Rules ---
async def rules_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Add Rule", callback_data="rule_add")],
        [InlineKeyboardButton("List Rules", callback_data="rule_list")],
        [InlineKeyboardButton("Back", callback_data="main")]
    ]
    await update.callback_query.edit_message_text("Auto-Forward Rules", reply_markup=InlineKeyboardMarkup(keyboard))

async def rule_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text(
        "Format: `keyword | chat_id`\nExample: `report | -1001234567890`",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="rules")]]),
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data["state"] = "awaiting_rule"

async def handle_rule_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "awaiting_rule": return
    text = update.message.text.strip()
    if " | " not in text:
        await update.message.reply_text("Use: `keyword | chat_id`")
        return
    kw, cid = text.split(" | ", 1)
    try: cid = int(cid.strip())
    except: await update.message.reply_text("Invalid chat ID."); return
    user = get_user_data(update.effective_user.id)
    user["rules"].append({"keyword": kw.strip().lower(), "chat_id": cid, "enabled": True})
    save_user_data(update.effective_user.id, user)
    await update.message.reply_text("Rule added!")
    context.user_data["state"] = None
    await rules_menu(update, context)

async def rule_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_data(update.effective_user.id)
    rules = user["rules"]
    text = f"Rules ({len(rules)}):\n\n"
    keyboard = []
    for i, r in enumerate(rules):
        status = "ON" if r["enabled"] else "OFF"
        text += f"{i+1}. *{r['keyword']}* to `{r['chat_id']}` [{status}]\n"
        keyboard.append([
            InlineKeyboardButton("Toggle", callback_data=f"rule_toggle_{i}"),
            InlineKeyboardButton("Delete", callback_data=f"rule_delete_{i}")
        ])
    keyboard.append([InlineKeyboardButton("Back", callback_data="rules")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

# --- Admin Broadcast ---
async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.callback_query.edit_message_text(
        "Send message to broadcast:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="main")]])
    )
    context.user_data["state"] = "admin_broadcast"

async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "admin_broadcast" or update.effective_user.id != ADMIN_ID: return
    users = users_db.all()
    sent = 0
    for user in users:
        try:
            await update.message.copy(user["id"])
            sent += 1
            await asyncio.sleep(0.05)
        except: pass
    await update.message.reply_text(f"Sent to {sent} users.")
    context.user_data["state"] = None

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
        "Files > 50 MB sent as document"
    )
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN
    )
    context.user_data["state"] = "awaiting_url"

# --- Callback Handler ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    if d == "main": await main_menu(update, context)
    elif d == "downloader": await downloader_menu(update, context)
    elif d == "cloud": await cloud_menu(update, context)
    elif d == "cloud_save": await cloud_save_prompt(update, context)
    elif d.startswith("cloud_list_"): await cloud_list(update, context, int(d.split("_")[-1]))
    elif d.startswith("cloud_resend_"): await cloud_resend(update, context, int(d.split("_")[-1]))
    elif d == "cloud_wipe_confirm": await cloud_wipe_confirm(update, context)
    elif d == "cloud_wipe_yes": await cloud_wipe_yes(update, context)
    elif d == "inspector": await inspector_menu(update, context)
    elif d == "rules": await rules_menu(update, context)
    elif d == "rule_add": await rule_add(update, context)
    elif d == "rule_list": await rule_list(update, context)
    elif d.startswith("rule_toggle_"):
        idx = int(d.split("_")[-1])
        user = get_user_data(update.effective_user.id)
        if idx < len(user["rules"]):
            user["rules"][idx]["enabled"] = not user["rules"][idx]["enabled"]
            save_user_data(update.effective_user.id, user)
        await rule_list(update, context)
    elif d.startswith("rule_delete_"):
        idx = int(d.split("_")[-1])
        user = get_user_data(update.effective_user.id)
        if idx < len(user["rules"]): user["rules"].pop(idx); save_user_data(update.effective_user.id, user)
        await rule_list(update, context)
    elif d == "admin_broadcast": await admin_broadcast(update, context)

# --- Message Handler ---
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user_data(user_id)
    rules = [r for r in user["rules"] if r["enabled"]]

    if update.message.document:
        fn = (update.message.document.file_name or "").lower()
        for r in rules:
            if r["keyword"] in fn:
                try: await update.message.forward(r["chat_id"])
                except: pass

    state = context.user_data.get("state")
    if state == "awaiting_url": await handle_url(update, context)
    elif state == "awaiting_file_save": await handle_file_save(update, context)
    elif state == "awaiting_inspector_file": await handle_inspector_file(update, context)
    elif state == "awaiting_rule": await handle_rule_add(update, context)
    elif state == "admin_broadcast": await handle_broadcast(update, context)

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
