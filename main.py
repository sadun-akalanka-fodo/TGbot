#!/usr/bin/env python3
"""
Telegram Video Downloader Bot

Features:
- Download videos from YouTube, TikTok, Instagram, Twitter, and other platforms
- Quality selection (1080p, 720p, 480p, 360p, or MP3 audio)
- Video-to-audio conversion
- Automatic file splitting for files larger than 50 MB (49MB parts)
- PDF OCR (English + Sinhala) – send a PDF to extract text
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

# 🔹 import OCR handler from separate file
from ocr_pdf import handle_pdf_ocr

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB in bytes
PART_SIZE = 49 * 1024 * 1024  # 49 MB per part
TELEGRAM_MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2 GB (Telegram limit)
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
        [KeyboardButton("📄 PDF to Text (OCR)")],
        [KeyboardButton("❓ Help")]
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message when /start command is issued."""
    welcome_message = (
        "👋 Welcome to the Video Downloader Bot!\n\n"
        "Choose what you want to do:\n\n"
        "📹 Download Video - Download from YouTube, TikTok, Instagram, etc.\n"
        "🎵 Convert Video to MP3 - Convert any video file to MP3 audio\n"
        "📄 PDF to Text (OCR) - Send a PDF and I'll extract English + Sinhala text\n\n"
        "Select an option from the menu below!"
    )
    await update.message.reply_text(
        welcome_message,
        reply_markup=get_main_menu_keyboard()
    )


async def handle_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle main menu button selections."""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    text = update.message.text
    
    if text == "📹 Download Video":
        # Set user mode to download
        user_sessions[user_id] = {'mode': 'download'}
        await update.message.reply_text(
            "📹 Video Download Mode\n\n"
            "Send me a video link from:\n"
            "• YouTube\n"
            "• TikTok\n"
            "• Instagram\n"
            "• Twitter/X\n"
            "• Facebook\n"
            "• Vimeo\n"
            "• And many more!\n\n"
            "Just paste the link and I'll help you download it!"
        )
    
    elif text == "🎵 Convert Video to MP3":
        # Set user mode to convert
        user_sessions[user_id] = {'mode': 'convert'}
        await update.message.reply_text(
            "🎵 Video to MP3 Converter\n\n"
            "Send me any video file and I'll convert it to MP3 audio for you!\n\n"
            "Just upload the video file."
        )
    
    elif text == "📄 PDF to Text (OCR)":
        await update.message.reply_text(
            "📄 PDF OCR Mode\n\n"
            "Send me a PDF with **printed** English or Sinhala text.\n"
            "I'll extract the text and send it back.\n\n"
            "⚠ Handwriting is NOT supported."
        )
    
    elif text == "❓ Help":
        await help_command(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages (URLs)."""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    message_text = update.message.text
    
    # Check if user has selected download mode
    if user_id not in user_sessions or user_sessions[user_id].get('mode') != 'download':
        await update.message.reply_text(
            "Please select an option from the menu first!",
            reply_markup=get_main_menu_keyboard()
        )
        return
    
    # Check if it's a video URL
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
            # For audio, look for .mp3 file
            downloaded_files = list(DOWNLOAD_DIR.glob(f"{safe_filename}*.mp3"))
        else:
            # For video, look for .mp4 file
            downloaded_files = list(DOWNLOAD_DIR.glob(f"{safe_filename}*.mp4"))
        
        if not downloaded_files:
            await update.callback_query.edit_message_text("❌ Download failed. Please try again.")
            return
        
        downloaded_file = downloaded_files[0]
        file_size = downloaded_file.stat().st_size
        
        # Check file size and handle accordingly
        if file_size > MAX_FILE_SIZE:
            # Don't cleanup yet - file needed for splitting after user chooses option
            await handle_large_file(update, context, user_id, downloaded_file, file_size)
        else:
            # Send the file directly
            await send_file(update, context, downloaded_file, file_size)
            # Cleanup after sending
            cleanup_file(downloaded_file)
            if user_id in user_sessions:
                del user_sessions[user_id]
            
    except Exception as e:
        logger.error(f"Download error: {e}")
        await update.callback_query.edit_message_text(
            f"❌ Download failed: {str(e)}\n\n"
            "Please try again or use a different link."
        )


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
        # Get video duration to calculate approximate number of parts
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
        
        # Split video using ffmpeg with file size limit
        current_time = 0
        part_num = 1
        
        while current_time < total_duration:
            output_file = file_path.parent / f"{file_path.stem}_part{part_num}{file_path.suffix}"
            
            # Use -fs flag to limit file size to 49MB
            cmd = [
                'ffmpeg', '-i', str(file_path),
                '-ss', str(current_time),
                '-fs', '49M',  # Limit file size to 49MB
                '-c', 'copy',
                '-avoid_negative_ts', '1',
                str(output_file)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if output_file.exists() and output_file.stat().st_size > 0:
                parts.append(output_file)
                
                # Get duration of the created part to calculate next start time
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
                    # If we can't get duration, break to avoid infinite loop
                    break
                
                part_num += 1
            else:
                break
            
            # Safety check to prevent infinite loop
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
                    write_timeout=180,   # 3 minutes upload time
                    read_timeout=180     # 3 minutes response wait
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
        # Cleanup any partial parts that were created
        logger.info(f"Cleaning up {len(parts)} partial video parts after error")
        for part_file in parts:
            cleanup_file(part_file)
    
    finally:
        # Always cleanup original file and session
        logger.info(f"Cleaning up original file: {file_path}")
        cleanup_file(file_path)
        user_id = update.effective_user.id if update.effective_user else None
        if user_id and user_id in user_sessions:
            del user_sessions[user_id]


async def split_zip_parts(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: Path, file_size: int) -> None:
    """Split file into 49MB ZIP parts."""
    zip_path = file_path.parent / f"{file_path.stem}.zip"
    parts = []  # Track all created parts for cleanup
    
    try:
        # Create ZIP of the original file
        await update.callback_query.edit_message_text("🗜️ Creating ZIP archive...")
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(file_path, file_path.name)
        
        zip_size = zip_path.stat().st_size
        
        # If ZIP is still too large, split it into 49MB parts
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
            
            # Send all parts with instructions
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
            # Send single ZIP file
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
        # Cleanup any partial ZIP parts that were created
        logger.info(f"Cleaning up {len(parts)} partial ZIP parts after error")
        for part_file in parts:
            cleanup_file(part_file)
    
    finally:
        # Always cleanup ZIP and original file and session
        logger.info(f"Cleaning up ZIP archive: {zip_path}")
        cleanup_file(zip_path)
        logger.info(f"Cleaning up original file: {file_path}")
        cleanup_file(file_path)
        user_id = update.effective_user.id if update.effective_user else None
        if user_id and user_id in user_sessions:
            del user_sessions[user_id]


async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: Path, file_size: int) -> None:
    """Send file to user."""
    if not update.effective_chat:
        return
    
    try:
        caption = f"✅ Download complete!\n📦 Size: {format_file_size(file_size)}"
        
        with open(file_path, 'rb') as f:
            if file_path.suffix == '.mp3':
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


async def handle_video_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle video files sent by user for conversion."""
    if not update.effective_user or not update.message or not update.message.video:
        return
    
    user_id = update.effective_user.id
    
    # Check if user is in convert mode
    if user_id not in user_sessions or user_sessions[user_id].get('mode') != 'convert':
        await update.message.reply_text(
            "Please select 🎵 Convert Video to MP3 from the menu first!",
            reply_markup=get_main_menu_keyboard()
        )
        return
    
    # Show processing message
    processing_msg = await update.message.reply_text("⏳ Downloading your video...")
    
    try:
        # Download the video file
        video_file = await update.message.video.get_file()
        file_path = DOWNLOAD_DIR / f"user_{user_id}_video.mp4"
        await video_file.download_to_drive(str(file_path))
        
        await processing_msg.edit_text("🎵 Converting to MP3...")
        
        # Convert to audio
        audio_path = DOWNLOAD_DIR / f"user_{user_id}_audio.mp3"
        
        cmd = [
            'ffmpeg', '-i', str(file_path),
            '-vn',  # No video
            '-acodec', 'libmp3lame',
            '-b:a', '192k',
            str(audio_path)
        ]
        
        subprocess.run(cmd, capture_output=True, check=True)
        
        if audio_path.exists():
            audio_size = audio_path.stat().st_size
            
            # Send audio file
            if update.effective_chat:
                with open(audio_path, 'rb') as f:
                    await context.bot.send_audio(
                        chat_id=update.effective_chat.id,
                        audio=f,
                        caption=f"🎵 Converted to MP3\n📦 Size: {format_file_size(audio_size)}"
                    )
            
            await processing_msg.delete()
            
            # Cleanup
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send help message."""
    help_text = (
        "📚 How to use this bot:\n\n"
        "📹 Download Video:\n"
        "1️⃣ Select 'Download Video' from the menu\n"
        "2️⃣ Send a video link from YouTube, TikTok, Instagram, etc.\n"
        "3️⃣ Choose quality (1080p, 720p, 480p, 360p, or MP3)\n"
        "4️⃣ Receive your file!\n\n"
        "🎵 Convert Video to MP3:\n"
        "1️⃣ Select 'Convert Video to MP3' from the menu\n"
        "2️⃣ Upload any video file\n"
        "3️⃣ Receive MP3 audio!\n\n"
        "📄 PDF to Text (OCR):\n"
        "• Send me any PDF with printed English or Sinhala\n"
        "• I will extract the text and send it back.\n\n"
        "Supported platforms:\n"
        "• YouTube\n"
        "• TikTok\n"
        "• Instagram\n"
        "• Twitter/X\n"
        "• Facebook\n"
        "• Vimeo\n"
        "• And many more!"
    )
    await update.message.reply_text(help_text, reply_markup=get_main_menu_keyboard())


def main() -> None:
    """Start the bot."""
    # Get bot token from environment variable
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set!")
        print("\n❌ ERROR: TELEGRAM_BOT_TOKEN not found!")
        print("Please set your Telegram bot token as an environment variable.")
        print("\nTo get a token:")
        print("1. Message @BotFather on Telegram")
        print("2. Send /newbot and follow instructions")
        print("3. Copy the token and add it to Replit Secrets")
        return
    
    # Create the Application
    application = Application.builder().token(token).build()
    
    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    
    # Handle menu button selections
    application.add_handler(MessageHandler(
        filters.Regex(r"^(📹 Download Video|🎵 Convert Video to MP3|📄 PDF to Text \(OCR\)|❓ Help)$"),
        handle_menu_choice
    ))
    
    # Handle video URLs (only in download mode)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.Regex(r"^(📹 Download Video|🎵 Convert Video to MP3|📄 PDF to Text \(OCR\)|❓ Help)$"),
        handle_message
    ))
    
    # Handle video files for conversion
    application.add_handler(MessageHandler(
        filters.VIDEO,
        handle_video_file
    ))
    
    # Handle PDF files for OCR (English + Sinhala)
    application.add_handler(MessageHandler(
        filters.Document.PDF,
        handle_pdf_ocr
    ))
    
    # Handle callback queries (button presses)
    application.add_handler(CallbackQueryHandler(
        handle_quality_selection,
        pattern="^quality_"
    ))
    application.add_handler(CallbackQueryHandler(
        handle_split_choice,
        pattern="^split_"
    ))
    
    # Start the bot
    logger.info("🤖 Bot starting...")
    print("\n✅ Telegram Video Downloader Bot is running!")
    print("Send video links or PDFs to your bot to get started.\n")
    
    # Run the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
