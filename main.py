import os
import re
import asyncio
import logging
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import yt_dlp

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

YOUTUBE_REGEX = re.compile(
    r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/'
    r'(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})'
)

QUALITY_MAP = {
    '360p': 'bestvideo[height<=360]+bestaudio/best[height<=360]',
    '480p': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
    '720p': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
    '1080p': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'
}

MAX_VIDEO_SIZE = 50 * 1024 * 1024
MAX_DOCUMENT_SIZE = 2000 * 1024 * 1024

downloads_dir = Path('downloads')
downloads_dir.mkdir(exist_ok=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! Send me a YouTube URL and I'll help you download it.\n\n"
        "Just paste any YouTube link and I'll show you quality options."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if YOUTUBE_REGEX.search(text):
        url = text.strip()
        
        sent_message = await update.message.reply_text(
            "🎬 Choose video quality:"
        )
        message_id = sent_message.message_id
        
        if 'youtube_urls' not in context.user_data:
            context.user_data['youtube_urls'] = {}
        context.user_data['youtube_urls'][message_id] = url
        
        keyboard = [
            [
                InlineKeyboardButton("360p", callback_data=f"quality_360p_{message_id}"),
                InlineKeyboardButton("480p", callback_data=f"quality_480p_{message_id}")
            ],
            [
                InlineKeyboardButton("720p", callback_data=f"quality_720p_{message_id}"),
                InlineKeyboardButton("1080p", callback_data=f"quality_1080p_{message_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await sent_message.edit_text(
            "🎬 Choose video quality:",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            "⚠️ Please send a valid YouTube URL."
        )

async def download_video(url: str, quality: str, user_id: int):
    output_template = str(downloads_dir / f'{user_id}_%(title)s.%(ext)s')
    
    ydl_opts = {
        'format': QUALITY_MAP[quality],
        'outtmpl': output_template,
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate'
        }
    }
    
    loop = asyncio.get_event_loop()
    
    def download_sync():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return filename, info.get('title', 'video')
    
    filename, title = await loop.run_in_executor(None, download_sync)
    return filename, title

async def handle_quality_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    callback_parts = query.data.split('_')
    quality = callback_parts[1]
    message_id = int(callback_parts[2])
    
    youtube_urls = context.user_data.get('youtube_urls', {})
    url = youtube_urls.get(message_id)
    
    if not url:
        await query.edit_message_text("⚠️ Session expired. Please send the YouTube URL again.")
        return
    
    status_message = await query.edit_message_text(
        f"⏳ Downloading video in {quality}...\n"
        "This may take a few moments depending on the video length."
    )
    
    try:
        filename, title = await download_video(url, quality, query.from_user.id)
        
        file_path = Path(filename)
        if not file_path.exists():
            await status_message.edit_text(
                "⚠️ Unable to download this video. Please try another link."
            )
            return
        
        file_size = file_path.stat().st_size
        file_size_mb = file_size / (1024 * 1024)
        
        await status_message.edit_text(
            f"📤 Uploading video...\n"
            f"Size: {file_size_mb:.1f} MB"
        )
        
        if file_size > MAX_DOCUMENT_SIZE:
            await status_message.edit_text(
                f"⚠️ File is too large ({file_size_mb:.1f} MB).\n"
                f"Telegram's limit is 2000 MB. Please try a lower quality."
            )
            file_path.unlink()
            return
        
        with open(file_path, 'rb') as video_file:
            if file_size <= MAX_VIDEO_SIZE:
                await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=video_file,
                    caption=f"🎬 {title}\n📊 Quality: {quality}",
                    supports_streaming=True,
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=300
                )
            else:
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=video_file,
                    caption=f"🎬 {title}\n📊 Quality: {quality}\n\n"
                            f"ℹ️ Sent as document (file > 50 MB)",
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=300
                )
        
        await status_message.edit_text(
            f"✅ Video sent successfully!\n"
            f"🎬 {title}\n"
            f"📊 Quality: {quality}\n"
            f"📦 Size: {file_size_mb:.1f} MB"
        )
        
        file_path.unlink()
        
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e).lower()
        if 'age' in error_msg or 'restricted' in error_msg:
            await status_message.edit_text(
                "⚠️ This video is age-restricted and cannot be downloaded."
            )
        elif '403' in error_msg or 'forbidden' in error_msg:
            await status_message.edit_text(
                "⚠️ Unable to access this video. It might be blocked or region-restricted."
            )
        else:
            await status_message.edit_text(
                "⚠️ Unable to download this video. Please try another link."
            )
        logger.error(f"Download error: {e}")
        
    except Exception as e:
        await status_message.edit_text(
            "⚠️ An error occurred while processing your request. Please try again."
        )
        logger.error(f"Unexpected error: {e}")
    
    finally:
        if 'youtube_urls' in context.user_data and message_id in context.user_data['youtube_urls']:
            del context.user_data['youtube_urls'][message_id]

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ An error occurred. Please try again."
        )

async def post_init(application: Application):
    await application.bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot started successfully!")

def main():
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables!")
        print("\n⚠️ ERROR: TELEGRAM_BOT_TOKEN is not set!")
        print("\nPlease add your bot token to the Secrets:")
        print("1. Go to Tools > Secrets in Replit")
        print("2. Add a new secret:")
        print("   Key: TELEGRAM_BOT_TOKEN")
        print("   Value: Your bot token from @BotFather")
        print("\nGet your token from @BotFather on Telegram:")
        print("1. Open Telegram and search for @BotFather")
        print("2. Send /newbot or /mybots to get your token\n")
        return
    
    application = Application.builder().token(token).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_quality_selection))
    application.add_error_handler(error_handler)
    
    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
