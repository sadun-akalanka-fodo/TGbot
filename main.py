#!/usr/bin/env python3
"""
Telegram Video Downloader Bot

Features:
- Download videos from YouTube, TikTok, Instagram, Twitter, and other platforms
- Quality selection (1080p, 720p, 480p, 360p, or MP3 audio)
- Video-to-audio conversion
- Automatic file splitting for files larger than 50 MB (49MB parts)
- PDF OCR (English + Sinhala) – send a PDF to extract text
- Image OCR (English + Sinhala) – send an image to extract text
- Music downloader: search song name on YouTube and download as MP3
- User-friendly button-based interface with main menu
"""

import os
import logging
import re
import subprocess
import zipfile
from typing import Optional, Dict, Any
from pathlib import Path

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

# 🔹 OCR handlers from separate files
from ocr_pdf import handle_pdf_ocr
from ocr_image import handle_image_ocr

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
PART_SIZE = 49 * 1024 * 1024      # 49 MB parts for splitting
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Supported platforms (for URL detection)
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

# Store user session data
user_sessions: Dict[int, Dict[str, Any]] = {}


def get_main_menu_keyboard():
    """Create the main menu keyboard."""
    keyboard = [
        [KeyboardButton("📹 Download Video")],
        [KeyboardButton("🎵 Convert Video to MP3")],
        [KeyboardButton("🎧 Download Music (Search)")],
        [KeyboardButton("📄 PDF to Text (OCR)")],
        [KeyboardButton("🖼️ Image to Text (OCR)")],
        [KeyboardButton("❓ Help")],
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
    """Get video information using yt-dlp."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except Exception as e:
        logger.error(f"Error getting video info: {e}")
        return None


def format_file_size(size_bytes: int) -> str:
    """Format file size in human-readable format."""
    size = float(size_bytes)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


# ============================
#      /start COMMAND
# ============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message when /start command is issued."""
    welcome_message = (
        "👋 Welcome to the Multi-Tool Bot!\n\n"
        "Choose what you want to do:\n\n"
        "📹 Download Video\n"
        "🎵 Convert Video to MP3\n"
        "🎧 Download Music (Search by name)\n"
        "📄 PDF to Text (OCR)\n"
        "🖼️ Image to Text (OCR)\n\n"
        "Select an option from the menu below!"
    )
    await update.message.reply_text(
        welcome_message,
        reply_markup=get_main_menu_keyboard()
    )


# ============================
#       MENU HANDLER
# ============================
async def handle_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle main menu button selections."""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    text = update.message.text
    
    if text == "📹 Download Video":
        user_sessions[user_id] = {'mode': 'download'}
        await update.message.reply_text(
            "📹 *Video Download Mode*\n\n"
            "Send me a video link from:\n"
            "• YouTube\n"
            "• TikTok\n"
            "• Instagram\n"
            "• Twitter/X\n"
            "• Facebook\n"
            "• Vimeo\n"
            "• And many more!\n\n"
            "Just paste the link and I'll help you download it!",
            reply_markup=get_main_menu_keyboard()
        )
    
    elif text == "🎵 Convert Video to MP3":
        user_sessions[user_id] = {'mode': 'convert'}
        await update.message.reply_text(
            "🎵 *Video → MP3 Converter*\n\n"
            "Send me any video file and I'll convert it to MP3 audio for you!",
            reply_markup=get_main_menu_keyboard()
        )
    
    elif text == "🎧 Download Music (Search)":
        user_sessions[user_id] = {'mode': 'music'}
        await update.message.reply_text(
            "🎧 *Music Download Mode*\n\n"
            "Send me a song name, artist name, or both.\n"
            "Example:\n"
            "• _Shape of You Ed Sheeran_\n"
            "• _Dinakage Sithin - Kasun Kalhara_",
            reply_markup=get_main_menu_keyboard()
        )
    
    elif text == "📄 PDF to Text (OCR)":
        await update.message.reply_text(
            "📄 *PDF OCR Mode*\n\n"
            "Send me a PDF with **printed** English or Sinhala text.\n"
            "I'll extract the text and send it back.\n\n"
            "⚠ Handwriting is *not* supported.",
            reply_markup=get_main_menu_keyboard()
        )
    
    elif text == "🖼️ Image to Text (OCR)":
        await update.message.reply_text(
            "🖼️ *Image OCR Mode*\n\n"
            "Send me a clear image with printed English or Sinhala text.\n"
            "I'll read the text and send it back.\n\n"
            "⚠ Handwriting is *not* supported.",
            reply_markup=get_main_menu_keyboard()
        )
    
    elif text == "❓ Help":
        await help_command(update, context)


# ============================
#   TEXT MESSAGE HANDLER
# ============================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages: video URLs or music search queries."""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    message_text = (update.message.text or "").strip()
    session = user_sessions.get(user_id)
    mode = session.get('mode') if session else None

    # If no mode selected
    if mode not in ('download', 'music'):
        await update.message.reply_text(
            "Please select an option from the menu first!",
            reply_markup=get_main_menu_keyboard()
        )
        return
    
    # =====================
    #  VIDEO DOWNLOAD MODE
    # =====================
    if mode == 'download':
        if not is_video_url(message_text):
            await update.message.reply_text(
                "❌ This doesn't look like a supported video URL.\n"
                "Please send a link from YouTube, TikTok, Instagram, Twitter, or other supported platforms."
            )
            return
        
        # Send processing message
        processing_msg = await update.message.reply_text("🔍 Analyzing video link...")
        
        # Get video information
        video_info = get_video_info(message_text)
        
        if not video_info:
            await processing_msg.edit_text(
                "❌ Sorry, I couldn't process this video link.\n"
                "Please make sure the link is correct and the video is publicly accessible."
            )
            return
        
        # Store video URL and info in user session
        user_sessions[user_id] = {
            'mode': 'download',
            'url': message_text,
            'video_info': video_info,
            'title': video_info.get('title', 'Unknown'),
        }
        
        # Create quality selection keyboard with 1080p option
        keyboard = [
            [
                InlineKeyboardButton("📺 1080p (Full HD)", callback_data="quality_1080"),
                InlineKeyboardButton("📺 720p (HD)", callback_data="quality_720"),
            ],
            [
                InlineKeyboardButton("📺 480p", callback_data="quality_480"),
                InlineKeyboardButton("📺 360p", callback_data="quality_360"),
            ],
            [
                InlineKeyboardButton("🎵 MP3 Audio", callback_data="quality_audio"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        title = video_info.get('title', 'Unknown')[:100]  # Limit title length
        duration = video_info.get('duration', 0)
        duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "Unknown"
        
        await processing_msg.edit_text(
            f"✅ Video found!\n\n"
            f"📝 Title: {title}\n"
            f"⏱️ Duration: {duration_str}\n\n"
            f"Please select the quality you want to download:",
            reply_markup=reply_markup
        )
        return
    
    # =====================
    #    MUSIC SEARCH MODE
    # =====================
    if mode == 'music':
        if not message_text:
            await update.message.reply_text("❗ Please send a song name or artist.")
            return
        
        waiting_msg = await update.message.reply_text(
            f"🎧 Searching YouTube for:\n`{message_text}`",
            parse_mode="Markdown"
        )
        
        await download_music_by_search(update, context, user_id, message_text, waiting_msg)


# ============================
#  QUALITY SELECTION HANDLER
# ============================
async def handle_quality_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle quality selection callback."""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    
    await query.answer()
    
    user_id = update.effective_user.id
    
    if user_id not in user_sessions:
        await query.edit_message_text("❌ Session expired. Please send the video link again.")
        return
    
    session = user_sessions[user_id]
    quality_choice = query.data.replace("quality_", "")
    session['quality'] = quality_choice
    
    # Show downloading message
    quality_text = {
        '1080': '1080p Full HD',
        '720': '720p HD',
        '480': '480p',
        '360': '360p',
        'audio': 'MP3 Audio'
    }.get(quality_choice, quality_choice)
    
    await query.edit_message_text(
        f"⬇️ Downloading in {quality_text}...\n\n"
        f"Please wait, this may take a few moments."
    )
    
    # Start download
    await download_video(update, context, user_id, session)


# ============================
#    VIDEO DOWNLOAD LOGIC
# ============================
async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, session: Dict[str, Any]) -> None:
    """Download video with selected quality."""
    url = session['url']
    quality = session['quality']
    video_title = session['title']
    
    # Clean filename
    safe_filename = re.sub(r'[^\w\s-]', '', video_title).strip()
    safe_filename = re.sub(r'[-\s]+', '_', safe_filename)[:50]
    
    # Prepare download options
    if quality == 'audio':
        output_path = DOWNLOAD_DIR / f"{safe_filename}.mp3"
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': str(output_path),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
            'no_warnings': True,
        }
    else:
        output_path = DOWNLOAD_DIR / f"{safe_filename}.mp4"
        # Map quality to height
        height_map = {'1080': 1080, '720': 720, '480': 480, '360': 360}
        max_height = height_map.get(quality, 720)
        
        ydl_opts = {
            'format': f'bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best',
            'outtmpl': str(output_path),
            'merge_output_format': 'mp4',
            'quiet': True,
            'no_warnings': True,
        }
    
    try:
        # Download the video
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # Find the actual downloaded file (yt-dlp might modify extension)
        if quality == 'audio':
            downloaded_files = list(DOWNLOAD_DIR.glob(f"{safe_filename}*.mp3"))
        else:
            downloaded_files = list(DOWNLOAD_DIR.glob(f"{safe_filename}*.mp4"))
        
        if not downloaded_files:
            await update.callback_query.edit_message_text("❌ Download failed. Please try again.")
            return
        
        downloaded_file = downloaded_files[0]
        file_size = downloaded_file.stat().st_size
        
        # Check file size and handle accordingly
        if file_size > MAX_FILE_SIZE:
            await handle_large_file(update, context, user_id, downloaded_file, file_size)
        else:
            await send_file(update, context, downloaded_file, file_size)
            cleanup_file(downloaded_file)
            if user_id in user_sessions:
                del user_sessions[user_id]
            
    except Exception as e:
        logger.error(f"Download error: {e}")
        await update.callback_query.edit_message_text(
            f"❌ Download failed: {str(e)}\n\n"
            "Please try again or use a different link."
        )


# ============================
#     MUSIC SEARCH DOWNLOAD
# ============================
async def download_music_by_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    query_text: str,
    waiting_msg
) -> None:
    """Search YouTube for a song and download best audio as MP3."""
    # Safe base name
    safe_query = re.sub(r'[^\w\s-]', '', query_text).strip()
    safe_query = re.sub(r'[-\s]+', '_', safe_query)[:50] or f"user_{user_id}_song"

    # Let yt-dlp decide the extension, we will locate the final file after post-processing
    out_tmpl = str(DOWNLOAD_DIR / f"{safe_query}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_tmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",  # search and pick first result
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query_text, download=True)

        # --- Figure out the actual output file path ---
        candidate: Path | None = None

        # 1) Try to read filepath from yt-dlp info (newer versions)
        if isinstance(info, dict):
            # If it's a search, info might be a playlist-like structure
            if "requested_downloads" in info and info["requested_downloads"]:
                fp = info["requested_downloads"][0].get("filepath")
                if fp:
                    candidate = Path(fp)

        # 2) Fallback: assume mp3 with our base name
        if candidate is None:
            candidate = DOWNLOAD_DIR / f"{safe_query}.mp3"

        # 3) If still not there, glob any mp3 that starts with our base name
        if not candidate.exists():
            matches = list(DOWNLOAD_DIR.glob(f"{safe_query}*.mp3"))
            if matches:
                candidate = matches[0]

        # 4) If STILL nothing, log and tell user
        if not candidate.exists():
            logger.error(
                "Music download: file not found for query '%s'. Files in downloads: %s",
                query_text,
                [p.name for p in DOWNLOAD_DIR.glob('*')]
            )
            await waiting_msg.edit_text("❌ Failed to download song. Try another name?")
            return

        file_size = candidate.stat().st_size
        await waiting_msg.edit_text("✅ Found and downloaded! Uploading to Telegram...")

        # Reuse your existing helper
        await send_file(update, context, candidate, file_size)

        # Cleanup
        cleanup_file(candidate)

    except Exception as e:
        logger.error(f"Music download error: {e}")
        await waiting_msg.edit_text(f"❌ Error downloading music:\n{e}")



# ============================
#   LARGE FILE HANDLING
# ============================
async def handle_large_file(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, file_path: Path, file_size: int) -> None:
    """Handle files larger than 50 MB - ask user for splitting preference."""
    session = user_sessions.get(user_id, {})
    session['large_file_path'] = str(file_path)
    session['large_file_size'] = file_size
    user_sessions[user_id] = session
    
    keyboard = [
        [InlineKeyboardButton("📹 Split into Video Parts (49MB each)", callback_data="split_video")],
        [InlineKeyboardButton("🗜️ Split into ZIP Parts (49MB each)", callback_data="split_zip")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(
        f"⚠️ Large file detected!\n\n"
        f"📦 File size: {format_file_size(file_size)}\n\n"
        f"This file is larger than 50 MB. How would you like to receive it?",
        reply_markup=reply_markup
    )


async def handle_split_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file splitting choice."""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    
    await query.answer()
    
    user_id = update.effective_user.id
    
    if user_id not in user_sessions or 'large_file_path' not in user_sessions[user_id]:
        await query.edit_message_text("❌ Session expired. Please send the video link again.")
        return
    
    session = user_sessions[user_id]
    file_path = Path(session['large_file_path'])
    file_size = session['large_file_size']
    split_type = query.data
    
    await query.edit_message_text(f"⚙️ Processing file for splitting...\n\nPlease wait...")
    
    if split_type == "split_video":
        await split_video_parts(update, context, file_path, file_size)
    elif split_type == "split_zip":
        await split_zip_parts(update, context, file_path, file_size)


async def split_video_parts(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: Path, file_size: int) -> None:
    """Split video into multiple 49MB parts using ffmpeg with file size limit."""
    parts = []  # Track all created parts for cleanup
    
    try:
        # Get video duration
        cmd = [
            'ffprobe', '-v', 'error', '-show_entries', 
            'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', 
            str(file_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        total_duration = float(result.stdout.strip())
        
        await update.callback_query.edit_message_text(
            f"✂️ Splitting video into parts (49MB each)...\n\n"
            "This may take a while..."
        )
        
        current_time = 0
        part_num = 1
        
        while current_time < total_duration:
            output_file = file_path.parent / f"{file_path.stem}_part{part_num}{file_path.suffix}"
            
            cmd = [
                'ffmpeg', '-i', str(file_path),
                '-ss', str(current_time),
                '-fs', '49M',
                '-c', 'copy',
                '-avoid_negative_ts', '1',
                str(output_file)
            ]
            
            subprocess.run(cmd, capture_output=True, text=True)
            
            if output_file.exists() and output_file.stat().st_size > 0:
                parts.append(output_file)
                
                # Get duration of this part
                cmd_duration = [
                    'ffprobe', '-v', 'error', '-show_entries',
                    'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
                    str(output_file)
                ]
                part_duration_result = subprocess.run(cmd_duration, capture_output=True, text=True)
                try:
                    part_duration = float(part_duration_result.stdout.strip())
                    current_time += part_duration
                except:
                    break
                
                part_num += 1
            else:
                break
            
            if part_num > 100:
                break
        
        # Send all parts
        for idx, part_file in enumerate(parts, 1):
            part_size_bytes = part_file.stat().st_size
            with open(part_file, 'rb') as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=f,
                    caption=f"Part {idx}/{len(parts)} - {format_file_size(part_size_bytes)}",
                    write_timeout=180,
                    read_timeout=180
                )
            cleanup_file(part_file)

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ All parts sent successfully!"
        )
        
    except Exception as e:
        logger.error(f"Error splitting video: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error splitting video: {str(e)}"
        )
        for part_file in parts:
            cleanup_file(part_file)
    
    finally:
        cleanup_file(file_path)
        user_id = update.effective_user.id if update.effective_user else None
        if user_id and user_id in user_sessions:
            del user_sessions[user_id]


async def split_zip_parts(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: Path, file_size: int) -> None:
    """Split file into 49MB ZIP parts."""
    zip_path = file_path.parent / f"{file_path.stem}.zip"
    parts = []
    
    try:
        await update.callback_query.edit_message_text("🗜️ Creating ZIP archive...")
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(file_path, file_path.name)
        
        zip_size = zip_path.stat().st_size
        
        if zip_size > MAX_FILE_SIZE:
            await update.callback_query.edit_message_text("✂️ Splitting ZIP into 49MB parts...")
            
            with open(zip_path, 'rb') as f:
                part_num = 1
                while True:
                    chunk = f.read(PART_SIZE)
                    if not chunk:
                        break
                    
                    part_path = file_path.parent / f"{file_path.stem}.z{part_num:02d}"
                    with open(part_path, 'wb') as part_file:
                        part_file.write(chunk)
                    parts.append(part_path)
                    part_num += 1
            
            instructions = (
                "📦 ZIP archive split into 49MB parts\n\n"
                f"Total parts: {len(parts)}\n\n"
                "To combine:\n"
                "1. Download all parts\n"
                "2. Use: cat *.z* > combined.zip\n"
                "3. Extract: unzip combined.zip"
            )
            
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=instructions
            )
            
            for idx, part_file in enumerate(parts, 1):
                part_size_bytes = part_file.stat().st_size
                with open(part_file, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=f,
                        caption=f"Part {idx}/{len(parts)} - {format_file_size(part_size_bytes)}",
                        write_timeout=180,
                        read_timeout=180
                    )
                cleanup_file(part_file)
        else:
            with open(zip_path, 'rb') as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=f,
                    caption=f"🗜️ ZIP Archive - {format_file_size(zip_size)}"
                )
        
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ All parts sent successfully!"
        )
        
    except Exception as e:
        logger.error(f"Error creating ZIP parts: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error creating ZIP: {str(e)}"
        )
        for part_file in parts:
            cleanup_file(part_file)
    
    finally:
        cleanup_file(zip_path)
        cleanup_file(file_path)
        user_id = update.effective_user.id if update.effective_user else None
        if user_id and user_id in user_sessions:
            del user_sessions[user_id]


# ============================
#      SEND FILE HELPER
# ============================
async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: Path, file_size: int) -> None:
    """Send file to user."""
    if not update.effective_chat:
        return
    
    try:
        caption = f"✅ Download complete!\n📦 Size: {format_file_size(file_size)}"
        
        with open(file_path, 'rb') as f:
            if file_path.suffix.lower() == '.mp3':
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=f,
                    caption=caption
                )
            else:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=f,
                    caption=caption,
                    supports_streaming=True
                )
    except Exception as e:
        logger.error(f"Error sending file: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Error sending file: {str(e)}"
        )


# ============================
#  VIDEO → MP3 CONVERSION
# ============================
async def handle_video_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle video files sent by user for conversion."""
    if not update.effective_user or not update.message or not update.message.video:
        return
    
    user_id = update.effective_user.id
    
    if user_id not in user_sessions or user_sessions[user_id].get('mode') != 'convert':
        await update.message.reply_text(
            "Please select 🎵 Convert Video to MP3 from the menu first!",
            reply_markup=get_main_menu_keyboard()
        )
        return
    
    processing_msg = await update.message.reply_text("⏳ Downloading your video...")
    
    try:
        video_file = await update.message.video.get_file()
        file_path = DOWNLOAD_DIR / f"user_{user_id}_video.mp4"
        await video_file.download_to_drive(str(file_path))
        
        await processing_msg.edit_text("🎵 Converting to MP3...")
        
        audio_path = DOWNLOAD_DIR / f"user_{user_id}_audio.mp3"
        
        cmd = [
            'ffmpeg', '-i', str(file_path),
            '-vn',
            '-acodec', 'libmp3lame',
            '-b:a', '192k',
            str(audio_path)
        ]
        
        subprocess.run(cmd, capture_output=True, check=True)
        
        if audio_path.exists():
            audio_size = audio_path.stat().st_size
            if update.effective_chat:
                with open(audio_path, 'rb') as f:
                    await context.bot.send_audio(
                        chat_id=update.effective_chat.id,
                        audio=f,
                        caption=f"🎵 Converted to MP3\n📦 Size: {format_file_size(audio_size)}"
                    )
            
            await processing_msg.delete()
            cleanup_file(file_path)
            cleanup_file(audio_path)
        else:
            await processing_msg.edit_text("❌ Conversion failed. Please try again.")
            
    except Exception as e:
        logger.error(f"Video to audio conversion error: {e}")
        await processing_msg.edit_text(f"❌ Error: {str(e)}")


def cleanup_file(file_path: Path) -> None:
    """Delete file if it exists."""
    try:
        if file_path.exists():
            file_path.unlink()
    except Exception as e:
        logger.error(f"Error deleting file {file_path}: {e}")


# ============================
#         HELP COMMAND
# ============================
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send help message."""
    help_text = (
        "📚 *How to use this bot:*\n\n"
        "📹 *Download Video*\n"
        "1. Tap '📹 Download Video'\n"
        "2. Send a video link (YouTube, TikTok, Instagram, etc.)\n"
        "3. Choose quality (1080p, 720p, 480p, 360p, or MP3)\n\n"
        "🎵 *Convert Video to MP3*\n"
        "1. Tap '🎵 Convert Video to MP3'\n"
        "2. Upload any video file\n"
        "3. Receive MP3 audio\n\n"
        "🎧 *Download Music (Search)*\n"
        "1. Tap '🎧 Download Music (Search)'\n"
        "2. Send a song name + artist\n"
        "3. I will search YouTube and send you the MP3\n\n"
        "📄 *PDF to Text (OCR)*\n"
        "• Send a PDF with printed English or Sinhala text\n"
        "• I will extract the text\n\n"
        "🖼️ *Image to Text (OCR)*\n"
        "• Send a clear image with printed English or Sinhala text\n"
        "• I will extract the text\n\n"
        "Use the menu buttons below to switch features."
    )
    await update.message.reply_text(help_text, reply_markup=get_main_menu_keyboard())


# ============================
#            MAIN
# ============================
def main() -> None:
    """Start the bot."""
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set!")
        print("\n❌ ERROR: TELEGRAM_BOT_TOKEN not found!")
        return
    
    application = Application.builder().token(token).build()
    
    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    
    # Menu buttons
    application.add_handler(MessageHandler(
        filters.Regex(r"^(📹 Download Video|🎵 Convert Video to MP3|🎧 Download Music \(Search\)|📄 PDF to Text \(OCR\)|🖼️ Image to Text \(OCR\)|❓ Help)$"),
        handle_menu_choice
    ))
    
    # Text messages (URLs or music search)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.Regex(r"^(📹 Download Video|🎵 Convert Video to MP3|🎧 Download Music \(Search\)|📄 PDF to Text \(OCR\)|🖼️ Image to Text \(OCR\)|❓ Help)$"),
        handle_message
    ))
    
    # Video files for MP3 conversion
    application.add_handler(MessageHandler(
        filters.VIDEO,
        handle_video_file
    ))
    
    # PDF files → OCR
    application.add_handler(MessageHandler(
        filters.Document.PDF,
        handle_pdf_ocr
    ))
    
    # Photos → OCR
    application.add_handler(MessageHandler(
        filters.PHOTO,
        handle_image_ocr
    ))
    
    # Inline buttons (quality selection / split choice)
    application.add_handler(CallbackQueryHandler(
        handle_quality_selection,
        pattern="^quality_"
    ))
    application.add_handler(CallbackQueryHandler(
        handle_split_choice,
        pattern="^split_"
    ))
    
    logger.info("🤖 Bot starting...")
    print("\n✅ Telegram Bot is running!")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
