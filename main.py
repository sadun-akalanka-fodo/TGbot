# bot.py
import asyncio
import os
import re
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
    MessageEntity
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# --- Metadata Extraction Libraries (Pure Python) ---
from PIL import Image
from PIL.ExifTags import TAGS
from hachoir.parser import createParser
from hachoir.metadata import extractMetadata
from mutagen import File as MutagenFile

# --- TinyDB ---
from tinydb import TinyDB, Query
from tinydb.storages import JSONStorage
from tinydb.middlewares import CachingMiddleware

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuration ---
BOT_TOKEN = "8596675371:AAGu8Su9mRNKGP0p8ZwpAL_kRemrpySJkoQ"
ADMIN_ID = 6356573938
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# --- Database Setup ---
UserQuery = Query()
db = TinyDB(DATA_DIR / "storage.tiny", storage=CachingMiddleware(JSONStorage))
users_db = TinyDB(DATA_DIR / "users.json")

# --- Constants ---
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB (Telegram limit)
SUPPORTED_EXTS = {
    'video': ('.mp4', '.mkv', '.avi', '.mov', '.webm'),
    'audio': ('.mp3', '.wav', '.ogg', '.m4a', '.flac'),
    'image': ('.jpg', '.jpeg', '.png', '.webp', '.bmp'),
    'document': ('.pdf', '.zip', '.txt', '.docx', '.xlsx', '.pptx')
}
PAGE_SIZE = 5

# --- Helper Functions ---
def get_user_data(user_id: int) -> Dict:
    user = users_db.get(UserQuery.id == user_id)
    if not user:
        user = {"id": user_id, "files": [], "rules": [], "state": None, "temp": {}}
        users_db.insert(user)
    return user

def save_user_data(user_id: int, data: Dict):
    users_db.upsert(data, UserQuery.id == user_id)

def is_direct_file_url(url: str) -> tuple[bool, str, str]:
    if not url.startswith(('http://', 'https://')):
        return False, "", ""
    parsed = re.search(r'\.([a-zA-Z0-9]+)(\?.*)?(#.*)?$', url)
    if not parsed:
        return False, "", ""
    ext = '.' + parsed.group(1).lower()
    for category, exts in SUPPORTED_EXTS.items():
        if ext in exts:
            return True, ext, category
    return False, "", ""

def format_size(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"

def format_duration(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"

# --- Menus ---
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

# --- Downloader ---
async def downloader_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Back", callback_data="main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        "Send me a direct file URL (e.g., ends with .mp4, .pdf, .jpg)\n\n"
        "Only true file links are allowed. No YouTube, TikTok, etc.",
        reply_markup=reply_markup
    )
    context.user_data["state"] = "awaiting_url"

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "awaiting_url":
        return
    url = update.message.text.strip()
    is_valid, ext, category = is_direct_file_url(url)
    if not is_valid:
        await update.message.reply_text("Invalid URL. Must end with a file extension like .mp4, .pdf, etc.")
        return

    msg = await update.message.reply_text("Checking file size...")
    try:
        async with context.application.bot_data["session"].head(url) as resp:
            if resp.status != 200:
                await msg.edit_text("Failed to access URL.")
                return
            size = int(resp.headers.get("content-length", 0))
            if size > MAX_FILE_SIZE:
                await msg.edit_text(f"File too large: {format_size(size)} > 50 MB")
                return
            if size == 0:
                await msg.edit_text("Could not determine file size.")
                return
    except:
        await msg.edit_text("Failed to check file size.")
        return

    await msg.edit_text(f"Downloading {format_size(size)} file...")
    try:
        file_path = DATA_DIR / f"temp_{update.effective_user.id}_{datetime.now().timestamp()}{ext}"
        async with context.application.bot_data["session"].get(url) as resp:
            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024*1024):
                    f.write(chunk)
        await msg.edit_text("Uploading to Telegram...")
        with open(file_path, "rb") as f:
            if category == "video":
                await update.message.reply_video(f, caption="Downloaded from URL")
            elif category == "audio":
                await update.message.reply_audio(f, caption="Downloaded from URL")
            elif category == "image":
                await update.message.reply_photo(f, caption="Downloaded from URL")
            else:
                await update.message.reply_document(f, caption="Downloaded from URL")
        os.remove(file_path)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"Error: {str(e)}")
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
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        "Personal Cloud Storage\nUpload files to save them permanently.",
        reply_markup=reply_markup
    )

async def cloud_save_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text(
        "Send me a file to save (document, photo, video, audio).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cloud")]])
    )
    context.user_data["state"] = "awaiting_file_save"

async def handle_file_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "awaiting_file_save":
        return
    file = None
    file_id = None
    file_name = "Unknown"
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
        await update.message.reply_text("Please send a supported file.")
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
    await update.message.reply_text(f"File saved: `{file_name}`", parse_mode=ParseMode.MARKDOWN)
    context.user_data["state"] = None
    await cloud_menu(update, context)

async def cloud_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    user = get_user_data(update.effective_user.id)
    files = user["files"]
    total = len(files)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = min(max(page, 1), max(pages, 1))
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    page_files = files[start:end]

    text = f"My Files ({total} total) - Page {page}/{max(pages, 1)}\n\n"
    keyboard = []
    for i, f in enumerate(page_files):
        text += f"{start + i + 1}. `{f['file_name']}` ({format_size(f['size'])})\n"
        keyboard.append([InlineKeyboardButton(f"Resend #{start + i + 1}", callback_data=f"cloud_resend_{start + i}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"cloud_list_{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("Next", callback_data=f"cloud_list_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("Back to Cloud", callback_data="cloud")])
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
        await update.callback_query.answer("Could not resend file.", show_alert=True)

async def cloud_wipe_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("YES, DELETE ALL", callback_data="cloud_wipe_yes")],
        [InlineKeyboardButton("Cancel", callback_data="cloud")]
    ]
    await update.callback_query.edit_message_text(
        "Are you sure you want to delete ALL your saved files?",
        reply_markup=InlineKeyboardMarkup(keyboard)
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
        "Send me a file to inspect (photo, video, audio, document).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="main")]])
    )
    context.user_data["state"] = "awaiting_inspector_file"

async def handle_inspector_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "awaiting_inspector_file":
        return
    file = None
    if update.message.document:
        file = update.message.document
    elif update.message.photo:
        file = update.message.photo[-1]
    elif update.message.video:
        file = update.message.video
    elif update.message.audio:
        file = update.message.audio
    else:
        await update.message.reply_text("Unsupported file.")
        return

    msg = await update.message.reply_text("Analyzing file...")
    try:
        tg_file = await context.bot.get_file(file.file_id)
        file_path = DATA_DIR / f"inspect_{update.effective_user.id}_{file.file_id}"
        await tg_file.download_to_drive(file_path)

        metadata = {}
        if file_path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'}:
            with Image.open(file_path) as img:
                metadata = {
                    "Format": img.format,
                    "Resolution": f"{img.width}x{img.height}",
                    "Size": format_size(file.file_size or 0),
                }
                exif = img.getexif()
                if exif:
                    for tag_id, value in exif.items():
                        tag = TAGS.get(tag_id, tag_id)
                        if tag in ["DateTime", "Model", "Software"]:
                            metadata[f"EXIF {tag}"] = str(value)
        else:
            parser = createParser(str(file_path))
            if parser:
                meta = extractMetadata(parser)
                if meta:
                    for line in meta.exportPlaintext():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            metadata[k.strip()] = v.strip()
            if not metadata and file_path.suffix.lower() in {'.mp3', '.m4a', '.flac'}:
                audio = MutagenFile(file_path)
                if audio:
                    metadata["Duration"] = format_duration(audio.info.length)
                    metadata["Bitrate"] = f"{audio.info.bitrate // 1000} kbps"

        text = "*File Metadata*\n\n"
        for k, v in metadata.items():
            text += f"• *{k}*: `{v}`\n"
        if not metadata:
            text += "No metadata found."
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        os.remove(file_path)
    except Exception as e:
        await msg.edit_text(f"Error: {str(e)}")
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
        "Send rule in format:\n`keyword | chat_id`\nExample: `report | -1001234567890`",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="rules")]]),
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data["state"] = "awaiting_rule"

async def handle_rule_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "awaiting_rule":
        return
    text = update.message.text.strip()
    if " | " not in text:
        await update.message.reply_text("Invalid format. Use: `keyword | chat_id`")
        return
    keyword, chat_id = text.split(" | ", 1)
    try:
        chat_id = int(chat_id.strip())
    except:
        await update.message.reply_text("Invalid chat ID.")
        return
    user = get_user_data(update.effective_user.id)
    user["rules"].append({"keyword": keyword.strip().lower(), "chat_id": chat_id, "enabled": True})
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
        text += f"{i+1}. If *{r['keyword']}* → `{r['chat_id']}` [{status}]\n"
        keyboard.append([
            InlineKeyboardButton(f"Toggle", callback_data=f"rule_toggle_{i}"),
            InlineKeyboardButton(f"Delete", callback_data=f"rule_delete_{i}")
        ])
    keyboard.append([InlineKeyboardButton("Back", callback_data="rules")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

# --- Admin Broadcast ---
async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.callback_query.edit_message_text(
        "Send message to broadcast to all users:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="main")]])
    )
    context.user_data["state"] = "admin_broadcast"

async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != "admin_broadcast" or update.effective_user.id != ADMIN_ID:
        return
    message = update.message
    users = users_db.all()
    sent = 0
    for user in users:
        try:
            await message.copy(user["id"])
            sent += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await update.message.reply_text(f"Broadcast sent to {sent} users.")
    context.user_data["state"] = None

# --- Callback Handler ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "main":
        await main_menu(update, context)
    elif data == "downloader":
        await downloader_menu(update, context)
    elif data == "cloud":
        await cloud_menu(update, context)
    elif data == "cloud_save":
        await cloud_save_prompt(update, context)
    elif data.startswith("cloud_list_"):
        page = int(data.split("_")[-1])
        await cloud_list(update, context, page)
    elif data.startswith("cloud_resend_"):
        idx = int(data.split("_")[-1])
        await cloud_resend(update, context, idx)
    elif data == "cloud_wipe_confirm":
        await cloud_wipe_confirm(update, context)
    elif data == "cloud_wipe_yes":
        await cloud_wipe_yes(update, context)
    elif data == "inspector":
        await inspector_menu(update, context)
    elif data == "rules":
        await rules_menu(update, context)
    elif data == "rule_add":
        await rule_add(update, context)
    elif data == "rule_list":
        await rule_list(update, context)
    elif data.startswith("rule_toggle_"):
        idx = int(data.split("_")[-1])
        user = get_user_data(update.effective_user.id)
        if idx < len(user["rules"]):
            user["rules"][idx]["enabled"] = not user["rules"][idx]["enabled"]
            save_user_data(update.effective_user.id, user)
        await rule_list(update, context)
    elif data.startswith("rule_delete_"):
        idx = int(data.split("_")[-1])
        user = get_user_data(update.effective_user.id)
        if idx < len(user["rules"]):
            user["rules"].pop(idx)
            save_user_data(update.effective_user.id, user)
        await rule_list(update, context)
    elif data == "admin_broadcast":
        await admin_broadcast(update, context)

# --- Message Handler with Auto-Forward ---
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user_data(user_id)
    rules = [r for r in user["rules"] if r["enabled"]]

    if update.message.document:
        filename = (update.message.document.file_name or "").lower()
        for rule in rules:
            if rule["keyword"] in filename:
                try:
                    await update.message.forward(rule["chat_id"])
                except:
                    pass

    # Handle states
    if context.user_data.get("state") == "awaiting_url":
        await handle_url(update, context)
    elif context.user_data.get("state") == "awaiting_file_save":
        await handle_file_save(update, context)
    elif context.user_data.get("state") == "awaiting_inspector_file":
        await handle_inspector_file(update, context)
    elif context.user_data.get("state") == "awaiting_rule":
        await handle_rule_add(update, context)
    elif context.user_data.get("state") == "admin_broadcast":
        await handle_broadcast(update, context)

# --- Start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await main_menu(update, context)

# --- Main ---
async def main():
    import aiohttp
    session = aiohttp.ClientSession()
    application = Application.builder().token(BOT_TOKEN).build()
    application.bot_data["session"] = session

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    logger.info("Bot started")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
