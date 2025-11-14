#!/usr/bin/env python3
"""
Telegram Media Butler Bot

Features:
- Download videos from YouTube, TikTok, Instagram, Twitter, and other platforms
- Quality selection (1080p, 720p, 480p, 360p, or MP3 audio)
- Video-to-audio conversion (user uploads video → MP3)
- Automatic file splitting for files larger than 50 MB (49MB parts)
- Image OCR (English + Sinhala)  -> uses ocr_image.py
- PDF OCR (English + Sinhala)    -> uses ocr_pdf.py
- Music search + download from YouTube as MP3 (song name)

User-friendly button-based interface with main menu.
"""

import os
import logging
import re
import subprocess
import zipfile
from typing import Dict, Any, Optional
from pathlib import Path
import asyncio  # for running blocking work in threads

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import yt_dlp

# OCR handlers in separate files
from ocr_image import handle_image_ocr
from ocr_pdf import handle_pdf_ocr

# ------------- Logging -------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------- Constants -------------

MAX_FILE_SIZE = 50 * 1024 * 1024      # 50 MB
PART_SIZE = 49 * 1024 * 1024          # 49 MB parts
TELEGRAM_MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2 GB

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

SUPPORTED_PLATFORMS = [
    r"(youtube\.com|youtu\.be)",
    r"tiktok\.com",
    r"instagram\.com",
    r"twitter\.com",
    r"x\.com",
    r"facebook\.com",
    r"vimeo\.com",
    r"dailymotion\.com",
    r"twitch\.tv",
]

# user_id -> session data
user_sessions: Dict[int, Dict[str, Any]] = {}

# Admin (optional) – only admin can update cookies
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# ------------- Load cookies from env (optional) -------------

COOKIES_ENV = os.getenv("YTDLP_COOKIES")
if COOKIES_ENV:
    try:
        with open("cookies.txt", "w", encoding="utf-8") as f:
            f.write(COOKIES_ENV)
        logger.info("cookies.txt created from YTDLP_COOKIES env var.")
    except Exception as e:
        logger.error(f"Failed to write cookies.txt from env: {e}")


# ------------- Helpers -------------

def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Create the main menu keyboard."""
    keyboard = [
        [KeyboardButton("📹 Download Video"), KeyboardButton("🎵 Convert Video to MP3")],
        [KeyboardButton("📄 PDF to Text (OCR)"), KeyboardButton("🖼️ Image Text (OCR)")],
        [KeyboardButton("🎧 Download Music (Search)"), KeyboardButton("❓ Help")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def is_video_url(text: str) -> bool:
    """Check if the message contains a supported video URL."""
    if not text:
        return False
    for pattern in SUPPORTED_PLATFORMS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def get_video_info(url: str) -> Optional[Dict[str, Any]]:
    """Get video information using yt-dlp (with optional cookies)."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "noplaylist": True,
    }

    cookies_path = "cookies.txt"
    if os.path.exists(cookies_path):
        ydl_opts["cookiefile"] = cookies_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except Exception as e:
        logger.error(f"Error getting video info: {e}")
        return None


def format_file_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


# ------------- /start & menu -------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message."""
    if not update.message:
        return

    text = (
        "👋 Welcome to Media Butler!\n\n"
        "Choose what you want to do:\n\n"
        "📹 *Download Video* – from YouTube, TikTok, etc.\n"
        "🎵 *Convert Video to MP3* – upload a video file.\n"
        "📄 *PDF to Text (OCR)* – send a PDF file.\n"
        "🖼️ *Image Text (OCR)* – send an image.\n"
        "🎧 *Download Music (Search)* – type song name.\n"
    )
    await update.message.reply_text(text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")


async def handle_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle main menu button selections."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    text = update.message.text

    if text == "📹 Download Video":
        user_sessions[user_id] = {"mode": "download"}
        await update.message.reply_text(
            "📹 *Video Download Mode*\n\n"
            "Send me a video link from:\n"
            "• YouTube, TikTok, Instagram, Twitter/X\n"
            "• Facebook, Vimeo, etc.\n\n"
            "Just paste the link 👇",
            parse_mode="Markdown",
        )

    elif text == "🎵 Convert Video to MP3":
        user_sessions[user_id] = {"mode": "convert"}
        await update.message.reply_text(
            "🎵 *Video → MP3 Converter*\n\n"
            "Now send me a *video file* and I will convert it to MP3.",
            parse_mode="Markdown",
        )

    elif text == "📄 PDF to Text (OCR)":
        user_sessions[user_id] = {"mode": "pdf_ocr"}
        await update.message.reply_text(
            "📄 *PDF OCR Mode*\n\n"
            "Send me a PDF file and I’ll extract the text (English + Sinhala).",
            parse_mode="Markdown",
        )

    elif text == "🖼️ Image Text (OCR)":
        user_sessions[user_id] = {"mode": "img_ocr"}
        await update.message.reply_text(
            "🖼️ *Image OCR Mode*\n\n"
            "Send me an image that contains English and/or Sinhala text.",
            parse_mode="Markdown",
        )

    elif text == "🎧 Download Music (Search)":
        user_sessions[user_id] = {"mode": "music"}
        await update.message.reply_text(
            "🎧 *Music Download Mode*\n\n"
            "Send me a song name, artist name, or both.\n"
            "Example:\n"
            "_Shape of You Ed Sheeran_\n"
            "_Dinakage Sithin - Kasun Kalhara_",
            parse_mode="Markdown",
        )

    elif text == "❓ Help":
        await help_command(update, context)


# ------------- Text messages (URLs / music queries) -------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route plain text based on current mode."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    message_text = update.message.text

    session = user_sessions.get(user_id, {})
    mode = session.get("mode")

    # Music search mode
    if mode == "music":
        waiting_msg = await update.message.reply_text("🎧 Searching YouTube for your song...")
        await download_music_by_search(update, context, user_id, message_text, waiting_msg)
        return

    # Video download mode
    if mode == "download":
        await handle_video_url(update, context)
        return

    # If no mode or other mode
    await update.message.reply_text(
        "Please pick what you want to do from the menu 👇",
        reply_markup=get_main_menu_keyboard(),
    )


# ------------- Video URL handling -------------

async def handle_video_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle video URL and show quality options."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    message_text = update.message.text

    if not is_video_url(message_text):
        await update.message.reply_text(
            "❌ This doesn't look like a supported video URL.\n"
            "Send a link from YouTube, TikTok, Instagram, Twitter, etc."
        )
        return

    processing_msg = await update.message.reply_text("🔍 Analyzing video link...")

    video_info = get_video_info(message_text)
    if not video_info:
        await processing_msg.edit_text(
            "❌ I couldn't process this link.\n"
            "Make sure the video is public and the URL is correct.\n\n"
            "Some videos may also require login / cookies (age-restricted or protected)."
        )
        return

    user_sessions[user_id] = {
        "mode": "download",
        "url": message_text,
        "video_info": video_info,
        "title": video_info.get("title", "Unknown"),
    }

    keyboard = [
        [
            InlineKeyboardButton("📺 1080p", callback_data="quality_1080"),
            InlineKeyboardButton("📺 720p", callback_data="quality_720"),
        ],
        [
            InlineKeyboardButton("📺 480p", callback_data="quality_480"),
            InlineKeyboardButton("📺 360p", callback_data="quality_360"),
        ],
        [InlineKeyboardButton("🎵 MP3 Audio", callback_data="quality_audio")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    title = video_info.get("title", "Unknown")[:100]
    dur = video_info.get("duration", 0)
    dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "Unknown"

    await processing_msg.edit_text(
        f"✅ Video found!\n\n"
        f"📝 Title: {title}\n"
        f"⏱️ Duration: {dur_str}\n\n"
        f"Choose quality:",
        reply_markup=reply_markup,
    )


async def handle_quality_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return

    await query.answer()
    user_id = update.effective_user.id

    if user_id not in user_sessions:
        await query.edit_message_text("❌ Session expired. Send the link again.")
        return

    session = user_sessions[user_id]
    quality_choice = query.data.replace("quality_", "")
    session["quality"] = quality_choice

    text_map = {
        "1080": "1080p Full HD",
        "720": "720p HD",
        "480": "480p",
        "360": "360p",
        "audio": "MP3 Audio",
    }
    quality_text = text_map.get(quality_choice, quality_choice)

    await query.edit_message_text(
        f"⬇️ Downloading in {quality_text}...\n\nThis may take a few moments."
    )

    await download_video(update, context, user_id, session)


async def download_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session: Dict[str, Any],
) -> None:
    url = session["url"]
    quality = session["quality"]
    video_title = session["title"]

    safe_filename = re.sub(r"[^\w\s-]", "", video_title).strip()
    safe_filename = re.sub(r"[-\s]+", "_", safe_filename)[:50] or f"user_{user_id}_video"

    if quality == "audio":
        output_path = DOWNLOAD_DIR / f"{safe_filename}.mp3"
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(output_path),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
    else:
        output_path = DOWNLOAD_DIR / f"{safe_filename}.mp4"
        height_map = {"1080": 1080, "720": 720, "480": 480, "360": 360}
        max_h = height_map.get(quality, 720)
        ydl_opts = {
            "format": f"bestvideo[height<={max_h}]+bestaudio/best[height<={max_h}]/best",
            "outtmpl": str(output_path),
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }

    cookies_path = "cookies.txt"
    if os.path.exists(cookies_path):
        ydl_opts["cookiefile"] = cookies_path

    try:
        # run yt-dlp in background thread
        def _do_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        await asyncio.to_thread(_do_download)

        if quality == "audio":
            files = list(DOWNLOAD_DIR.glob(f"{safe_filename}*.mp3"))
        else:
            files = list(DOWNLOAD_DIR.glob(f"{safe_filename}*.mp4"))

        if not files:
            await update.callback_query.edit_message_text("❌ Download failed. Try again.")
            return

        downloaded_file = files[0]
        size = downloaded_file.stat().st_size

        if size > MAX_FILE_SIZE:
            await handle_large_file(update, context, user_id, downloaded_file, size)
        else:
            await send_file(update, context, downloaded_file, size)
            cleanup_file(downloaded_file)
            user_sessions.pop(user_id, None)

    except Exception as e:
        logger.error(f"Download error: {e}")
        msg = str(e)
        if "Sign in to confirm you’re not a bot" in msg or "Sign in to confirm you're not a bot" in msg:
            text = (
                "❌ I can't download this video.\n\n"
                "YouTube is asking to *sign in to confirm you're not a bot*.\n"
                "This usually happens for protected / limited videos or when the server IP looks like a bot.\n\n"
                "👉 Try a different video, or update cookies."
            )
        else:
            text = f"❌ Download failed:\n{msg}\n\nTry another link."
        await update.callback_query.edit_message_text(text)


# ------------- Large file splitting -------------

async def handle_large_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    file_path: Path,
    file_size: int,
) -> None:
    session = user_sessions.get(user_id, {})
    session["large_file_path"] = str(file_path)
    session["large_file_size"] = file_size
    user_sessions[user_id] = session

    keyboard = [
        [
            InlineKeyboardButton(
                "📹 Split into Video Parts (49MB)", callback_data="split_video"
            )
        ],
        [
            InlineKeyboardButton(
                "🗜️ Split into ZIP Parts (49MB)", callback_data="split_zip"
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        f"⚠️ Large file detected!\n\n"
        f"Size: {format_file_size(file_size)}\n\n"
        f"How do you want to receive it?",
        reply_markup=reply_markup,
    )


async def handle_split_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return

    await query.answer()
    user_id = update.effective_user.id

    if user_id not in user_sessions or "large_file_path" not in user_sessions[user_id]:
        await query.edit_message_text("❌ Session expired. Send the link again.")
        return

    session = user_sessions[user_id]
    file_path = Path(session["large_file_path"])
    file_size = session["large_file_size"]
    split_type = query.data

    await query.edit_message_text("⚙️ Preparing file, please wait...")

    if split_type == "split_video":
        await split_video_parts(update, context, file_path, file_size)
    elif split_type == "split_zip":
        await split_zip_parts(update, context, file_path, file_size)


async def split_video_parts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_path: Path,
    file_size: int,
) -> None:
    """Split video into multiple 49MB parts and send each as playable Telegram video."""
    parts = []
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ]
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        total_duration = float(result.stdout.strip() or "0")

        current_time = 0.0
        part_num = 1

        while current_time < total_duration and part_num <= 100:
            out_file = file_path.parent / f"{file_path.stem}_part{part_num}{file_path.suffix}"

            cmd = [
                "ffmpeg",
                "-i",
                str(file_path),
                "-ss",
                str(current_time),
                "-fs",
                "49M",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "1",
                str(out_file),
            ]
            await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)

            if out_file.exists() and out_file.stat().st_size > 0:
                parts.append(out_file)

                cmd_dur = [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(out_file),
                ]
                res_dur = await asyncio.to_thread(subprocess.run, cmd_dur, capture_output=True, text=True)
                try:
                    part_dur = float(res_dur.stdout.strip() or "0")
                    current_time += part_dur
                except Exception:
                    break

                part_num += 1
            else:
                break

        for idx, p in enumerate(parts, 1):
            size = p.stat().st_size
            caption = f"📹 Part {idx}/{len(parts)} - {format_file_size(size)}"

            width = height = duration = None
            try:
                cmd_meta = [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height,duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(p),
                ]
                meta_res = await asyncio.to_thread(subprocess.run, cmd_meta, capture_output=True, text=True)
                if meta_res.returncode == 0:
                    lines = [ln.strip() for ln in meta_res.stdout.splitlines() if ln.strip()]
                    if len(lines) >= 2:
                        width = int(float(lines[0]))
                        height = int(float(lines[1]))
                    if len(lines) >= 3:
                        try:
                            duration = int(float(lines[2]))
                        except Exception:
                            duration = None
            except Exception as e:
                logger.warning(f"ffprobe meta failed for {p}: {e}")

            with open(p, "rb") as f:
                send_kwargs = dict(
                    chat_id=update.effective_chat.id,
                    video=f,
                    caption=caption,
                    supports_streaming=True,
                    write_timeout=180,
                    read_timeout=180,
                )
                if width and height:
                    send_kwargs["width"] = width
                    send_kwargs["height"] = height
                if duration:
                    send_kwargs["duration"] = duration

                await context.bot.send_video(**send_kwargs)

            cleanup_file(p)

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ All parts sent successfully!",
        )

    except Exception as e:
        logger.error(f"Error splitting video: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error splitting video: {e}",
        )
        for p in parts:
            cleanup_file(p)
    finally:
        cleanup_file(file_path)
        if update.effective_user:
            user_sessions.pop(update.effective_user.id, None)


async def split_zip_parts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_path: Path,
    file_size: int,
) -> None:
    zip_path = file_path.parent / f"{file_path.stem}.zip"
    parts = []
    try:
        await update.callback_query.edit_message_text("🗜️ Creating ZIP archive...")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(file_path, file_path.name)

        zip_size = zip_path.stat().st_size

        if zip_size > MAX_FILE_SIZE:
            await update.callback_query.edit_message_text("✂️ Splitting ZIP into 49MB parts...")
            with open(zip_path, "rb") as f:
                part_num = 1
                while True:
                    chunk = f.read(PART_SIZE)
                    if not chunk:
                        break
                    part_path = file_path.parent / f"{file_path.stem}.z{part_num:02d}"
                    with open(part_path, "wb") as pf:
                        pf.write(chunk)
                    parts.append(part_path)
                    part_num += 1

            instructions = (
                "📦 ZIP archive split into 49MB parts.\n\n"
                f"Total parts: {len(parts)}\n\n"
                "To combine on PC:\n"
                "1. Download all parts\n"
                "2. Run:  cat *.z* > combined.zip\n"
                "3. Then: unzip combined.zip"
            )
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=instructions,
            )

            for idx, p in enumerate(parts, 1):
                size = p.stat().st_size
                with open(p, "rb") as f:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=f,
                        caption=f"Part {idx}/{len(parts)} - {format_file_size(size)}",
                    )
                cleanup_file(p)
        else:
            with open(zip_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=f,
                    caption=f"🗜️ ZIP Archive - {format_file_size(zip_size)}",
                )

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ All parts sent successfully!",
        )

    except Exception as e:
        logger.error(f"Error creating ZIP: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error creating ZIP: {e}",
        )
        for p in parts:
            cleanup_file(p)
    finally:
        cleanup_file(zip_path)
        cleanup_file(file_path)
        if update.effective_user:
            user_sessions.pop(update.effective_user.id, None)


# ------------- Send file helper (video/audio) -------------

async def send_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_path: Path,
    file_size: int,
    display_name: Optional[str] = None,
) -> None:
    """Send file to user."""
    if not update.effective_chat:
        return

    try:
        caption = f"✅ Download complete!\n📦 Size: {format_file_size(file_size)}"
        with open(file_path, "rb") as f:
            if file_path.suffix.lower() == ".mp3":
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=f,
                    caption=caption,
                    title=display_name,
                )
            else:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=f,
                    caption=caption,
                    supports_streaming=True,
                )
    except Exception as e:
        logger.error(f"Error sending file: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error sending file: {e}",
        )


def cleanup_file(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception as e:
        logger.error(f"Error deleting {path}: {e}")


# ------------- Video file → MP3 -------------

async def handle_video_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle video files sent by user for conversion."""
    if not update.message or not update.message.video or not update.effective_user:
        return

    user_id = update.effective_user.id
    session = user_sessions.get(user_id, {})
    if session.get("mode") != "convert":
        await update.message.reply_text(
            "Please choose 🎵 *Convert Video to MP3* from the menu first.",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(),
        )
        return

    processing_msg = await update.message.reply_text("⏳ Downloading your video...")

    try:
        telegram_file = await update.message.video.get_file()
        in_path = DOWNLOAD_DIR / f"user_{user_id}_video.mp4"
        await telegram_file.download_to_drive(str(in_path))

        await processing_msg.edit_text("🎵 Converting to MP3...")

        out_path = DOWNLOAD_DIR / f"user_{user_id}_audio.mp3"
        cmd = [
            "ffmpeg",
            "-i",
            str(in_path),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            "192k",
            str(out_path),
        ]
        await asyncio.to_thread(subprocess.run, cmd, capture_output=True, check=True)

        if out_path.exists():
            size = out_path.stat().st_size
            await send_file(update, context, out_path, size)
            await processing_msg.delete()
            cleanup_file(in_path)
            cleanup_file(out_path)
        else:
            await processing_msg.edit_text("❌ Conversion failed. Please try again.")
    except Exception as e:
        logger.error(f"Video → audio error: {e}")
        await processing_msg.edit_text(f"❌ Error: {e}")


# ------------- Music search & download -------------

async def download_music_by_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    query_text: str,
    waiting_msg,
) -> None:
    """Search YouTube for a song and download best audio as MP3."""
    safe_query = re.sub(r"[^\w\s-]", "", query_text).strip()
    safe_query = re.sub(r"[-\s]+", "_", safe_query)[:50] or f"user_{user_id}_song"

    out_tmpl = str(DOWNLOAD_DIR / f"{safe_query}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_tmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    cookies_path = "cookies.txt"
    if os.path.exists(cookies_path):
        ydl_opts["cookiefile"] = cookies_path

    try:
        def _do_music():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(query_text, download=True)

        info = await asyncio.to_thread(_do_music)

        yt_title = None
        if isinstance(info, dict):
            if "requested_downloads" in info and info["requested_downloads"]:
                rd = info["requested_downloads"][0]
                yt_title = rd.get("title")
                if not yt_title and "info_dict" in rd:
                    yt_title = rd["info_dict"].get("title")

            if not yt_title:
                yt_title = info.get("title")

        if not yt_title:
            yt_title = "Unknown Title"

        base_from_title = re.sub(r"[^\w\s-]", "", yt_title).strip()
        base_from_title = re.sub(r"[-\s]+", "_", base_from_title)[:60] or safe_query

        candidate: Optional[Path] = None
        if isinstance(info, dict):
            if "requested_downloads" in info and info["requested_downloads"]:
                fp = info["requested_downloads"][0].get("filepath")
                if fp:
                    candidate = Path(fp)

        if candidate is None:
            candidate = DOWNLOAD_DIR / f"{safe_query}.mp3"
        if not candidate.exists():
            matches = list(DOWNLOAD_DIR.glob(f"{safe_query}*.mp3"))
            if matches:
                candidate = matches[0]

        if not candidate.exists():
            logger.error(
                "Music download: file not found for query '%s'. Files: %s",
                query_text,
                [p.name for p in DOWNLOAD_DIR.glob('*')],
            )
            await waiting_msg.edit_text("❌ Failed to download song. Try another name?")
            return

        nice_path = candidate.with_name(base_from_title + candidate.suffix)
        try:
            candidate.rename(nice_path)
            candidate = nice_path
        except Exception as e:
            logger.warning(f"Rename failed: {e}")

        size = candidate.stat().st_size
        await waiting_msg.edit_text("✅ Found and downloaded! Uploading to Telegram...")

        await send_file(update, context, candidate, size, display_name=yt_title)
        cleanup_file(candidate)

    except Exception as e:
        logger.error(f"Music download error: {e}")
        await waiting_msg.edit_text(f"❌ Error downloading music:\n{e}")


# ------------- Cookies upload handler -------------

async def handle_cookies_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Allow the bot admin to upload a new cookies.txt file directly via Telegram.
    Only runs for a document named exactly 'cookies.txt'.
    """
    if not update.message or not update.message.document or not update.effective_user:
        return

    doc = update.message.document
    user_id = update.effective_user.id

    if doc.file_name != "cookies.txt":
        return

    if ADMIN_ID != 0 and user_id != ADMIN_ID:
        await update.message.reply_text("❌ Only the bot admin can update cookies.")
        return

    try:
        file = await doc.get_file()
        await file.download_to_drive("cookies.txt")
        await update.message.reply_text(
            "✅ cookies.txt updated.\n"
            "New YouTube downloads and music searches will use these cookies."
        )
        logger.info("cookies.txt updated via Telegram by user %s", user_id)
    except Exception as e:
        logger.error(f"Failed to save cookies.txt from Telegram: {e}")
        await update.message.reply_text(f"❌ Failed to save cookies.txt: {e}")


# ------------- Help -------------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = (
        "📚 *How to use this bot*\n\n"
        "📹 *Download Video*\n"
        "1. Tap 'Download Video'\n"
        "2. Send a video link\n"
        "3. Choose quality or MP3\n\n"
        "🎵 *Convert Video to MP3*\n"
        "1. Tap 'Convert Video to MP3'\n"
        "2. Upload a video file\n\n"
        "📄 *PDF to Text (OCR)* – send a PDF\n"
        "🖼️ *Image Text (OCR)* – send an image\n"
        "🎧 *Download Music (Search)* – send song name\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard())


# ------------- main() -------------

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set!")
        print("Set TELEGRAM_BOT_TOKEN env var and restart.")
        return

    # 🔥 Enable concurrent update handling
    app = (
        Application
        .builder()
        .token(token)
        .concurrent_updates(True)   # <-- key line for parallel handling
        .build()
    )

    # /start + /help
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # Menu buttons
    app.add_handler(
        MessageHandler(
            filters.Regex(
                "^(📹 Download Video|🎵 Convert Video to MP3|📄 PDF to Text \\(OCR\\)|🖼️ Image Text \\(OCR\\)|🎧 Download Music \\(Search\\)|❓ Help)$"
            ),
            handle_menu_choice,
        )
    )

    # OCR handlers
    app.add_handler(MessageHandler(filters.PHOTO, handle_image_ocr))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf_ocr))

    # Cookies upload
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookies_document))

    # Video files for conversion
    app.add_handler(MessageHandler(filters.VIDEO, handle_video_file))

    # Callback buttons (quality / splitting)
    app.add_handler(CallbackQueryHandler(handle_quality_selection, pattern="^quality_"))
    app.add_handler(CallbackQueryHandler(handle_split_choice, pattern="^split_"))

    # All other text (URLs / music queries)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    logger.info("🤖 Bot starting...")
    print("✅ Media Butler bot running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
