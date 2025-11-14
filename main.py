#!/usr/bin/env python3
"""
Telegram Video Downloader Bot + PDF OCR

Features:
- Download videos from YouTube, TikTok, Instagram, Twitter, and other platforms
- Quality selection (1080p, 720p, 480p, 360p, or MP3 audio)
- Video-to-audio conversion
- Automatic file splitting for files larger than 50 MB (49MB parts)
- PDF OCR (English + Sinhala) — send any PDF
- User-friendly button-based interface with main menu
"""

import os
import asyncio
import logging
import re
import subprocess
import zipfile
from typing import Optional, Dict, Any
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import yt_dlp

# 🆕 Import OCR module
from ocr_pdf import handle_pdf_ocr


# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
MAX_FILE_SIZE = 50 * 1024 * 1024
PART_SIZE = 49 * 1024 * 1024
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Platforms
SUPPORTED_PLATFORMS = [
    r'(youtube\.com|youtu\.be)',
    r'tiktok\.com',
    r'instagram\.com',
    r'twitter\.com',
    r'x\.com',
    r'facebook\.com',
    r'vimeo\.com',
    r'dailymotion\.com',
    r'twitch\.tv',
]

# User sessions
user_sessions: Dict[int, Dict[str, Any]] = {}


# ============================
#     MAIN MENU BUTTONS
# ============================
def get_main_menu_keyboard():
    keyboard = [
        [KeyboardButton("📹 Download Video")],
        [KeyboardButton("🎵 Convert Video to MP3")],
        [KeyboardButton("📄 PDF to Text (OCR)")],
        [KeyboardButton("❓ Help")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ============================
#  HELPERS
# ============================
def is_video_url(text: str) -> bool:
    if not text:
        return False
    for pattern in SUPPORTED_PLATFORMS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def get_video_info(url: str) -> Optional[Dict[str, Any]]:
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            return ydl.extract_info(url, download=False)
    except:
        return None


def format_file_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


# ============================
#      /start COMMAND
# ============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to the Ultimate Telegram Utility Bot!\n\n"
        "Choose an option below:\n"
        "📹 Download videos\n"
        "🎵 Convert video to MP3\n"
        "📄 Extract text from PDFs (OCR)\n\n"
        "Send /help if you need guidance.",
        reply_markup=get_main_menu_keyboard()
    )


# ============================
#       MENU HANDLER
# ============================
async def handle_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "📹 Download Video":
        user_sessions[user_id] = {"mode": "download"}
        await update.message.reply_text(
            "📹 Video Download Mode Enabled\n\n"
            "Now send me any video link!"
        )

    elif text == "🎵 Convert Video to MP3":
        user_sessions[user_id] = {"mode": "convert"}
        await update.message.reply_text(
            "🎵 Send me any video file and I'll convert it to MP3!"
        )

    elif text == "📄 PDF to Text (OCR)":
        await update.message.reply_text(
            "📄 PDF OCR Mode\n\n"
            "Send me any PDF with **printed** English or Sinhala text.\n"
            "I'll extract the text for you.\n\n"
            "⚠ Handwriting is NOT supported."
        )

    elif text == "❓ Help":
        await help_command(update, context)


# ============================
#       URL MESSAGE
# ============================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = update.message.text

    # User MUST choose "Download Video" first
    if user_id not in user_sessions or user_sessions[user_id].get("mode") != "download":
        await update.message.reply_text(
            "Please select 📹 *Download Video* from the menu first!",
            reply_markup=get_main_menu_keyboard()
        )
        return

    if not is_video_url(msg):
        await update.message.reply_text("❌ Invalid video link!")
        return

    processing = await update.message.reply_text("🔍 Fetching video info...")

    video_info = get_video_info(msg)
    if not video_info:
        await processing.edit_text("❌ Unable to fetch video info!")
        return

    user_sessions[user_id].update({
        "url": msg,
        "title": video_info.get("title", "unknown"),
    })

    keyboard = [
        [InlineKeyboardButton("📺 1080p", callback_data="quality_1080"),
         InlineKeyboardButton("📺 720p", callback_data="quality_720")],
        [InlineKeyboardButton("📺 480p", callback_data="quality_480"),
         InlineKeyboardButton("📺 360p", callback_data="quality_360")],
        [InlineKeyboardButton("🎵 MP3 Audio", callback_data="quality_audio")]
    ]

    await processing.edit_text(
        f"🎬 *{video_info.get('title','Unknown')}*\n\n"
        "Choose your download quality:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ============================
#       QUALITY SELECTION
# ============================
async def handle_quality_selection(update, context):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    choice = query.data.replace("quality_", "")

    session = user_sessions.get(user_id)
    if not session:
        await query.edit_message_text("❌ Session expired.")
        return

    session["quality"] = choice
    await query.edit_message_text("⬇️ Downloading video...\nPlease wait...")

    await download_video(update, context, user_id, session)


# ============================
#   DOWNLOAD / SPLIT VIDEO
# ============================
async def download_video(update, context, user_id, session):
    url = session["url"]
    title = session["title"]
    quality = session["quality"]

    safe = re.sub(r"[^\w\s-]", "", title).strip()
    safe = re.sub(r"[-\s]+", "_", safe)[:50]

    if quality == "audio":
        output = DOWNLOAD_DIR / f"{safe}.mp3"
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(output),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
            }],
        }
    else:
        res_map = {"1080": 1080, "720": 720, "480": 480, "360": 360}
        max_h = res_map.get(quality, 720)

        output = DOWNLOAD_DIR / f"{safe}.mp4"
        ydl_opts = {
            "format": f"bestvideo[height<={max_h}]+bestaudio/best",
            "outtmpl": str(output),
            "merge_output_format": "mp4",
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Find output file
        if quality == "audio":
            file = list(DOWNLOAD_DIR.glob(f"{safe}*.mp3"))[0]
        else:
            file = list(DOWNLOAD_DIR.glob(f"{safe}*.mp4"))[0]

        size = file.stat().st_size

        if size > MAX_FILE_SIZE:
            await handle_large_file(update, context, user_id, file, size)
        else:
            await send_file(update, context, file, size)
            file.unlink()

    except Exception as e:
        await update.callback_query.edit_message_text(f"❌ Download error:\n{e}")


# ============================
#     SEND FILE
# ============================
async def send_file(update, context, file, size):
    caption = f"✅ Done!\n📦 Size: {format_file_size(size)}"

    with open(file, "rb") as f:
        if file.suffix == ".mp3":
            await context.bot.send_audio(update.effective_chat.id, f, caption=caption)
        else:
            await context.bot.send_video(update.effective_chat.id, f, caption=caption)


# ============================
#     HELP COMMAND
# ============================
async def help_command(update, context):
    await update.message.reply_text(
        "📚 *Bot Features*\n\n"
        "📹 **Download videos** (YouTube, TikTok, IG, FB…)\n"
        "🎵 **Convert video → MP3**\n"
        "📄 **PDF OCR** (English + Sinhala)\n\n"
        "Just use the menu buttons below!",
        reply_markup=get_main_menu_keyboard()
    )


# ============================
#          MAIN
# ============================
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN missing!")
        return

    app = Application.builder().token(token).build()

    # --- COMMANDS ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # --- MENU BUTTONS ---
    app.add_handler(MessageHandler(
        filters.Regex(r"^(📹 Download Video|🎵 Convert Video to MP3|📄 PDF to Text \(OCR\)|❓ Help)$"),
        handle_menu_choice
    ))

    # --- URL TEXT ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # --- VIDEO FILES ---
    app.add_handler(MessageHandler(filters.VIDEO, handle_video_file))

    # --- PDF FILES (OCR) ---
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf_ocr))

    # --- INLINE BUTTONS ---
    app.add_handler(CallbackQueryHandler(handle_quality_selection, pattern="^quality_"))

    print("🤖 Bot is running...")
    app.run_polling()


# ENTRY POINT
if __name__ == "__main__":
    main()
