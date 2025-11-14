import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes
from PIL import Image
import pytesseract

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

async def handle_image_ocr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle images sent by user and extract text using Tesseract OCR."""
    if not update.message or not update.message.photo:
        return

    user = update.effective_user
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not user or not chat_id:
        return

    user_id = user.id

    # Get highest-resolution photo
    photo = update.message.photo[-1]

    processing_msg = await update.message.reply_text("🖼️ Processing image for text...")

    img_path = DOWNLOAD_DIR / f"ocr_img_{user_id}.jpg"

    try:
        # Download image from Telegram
        file = await photo.get_file()
        await file.download_to_drive(str(img_path))

        # Run Tesseract OCR (English + Sinhala)
        # Make sure Tesseract has 'eng' and 'sin' traineddata installed on your system
        text = pytesseract.image_to_string(Image.open(str(img_path)), lang="eng+sin")

        if not text or not text.strip():
            await processing_msg.edit_text("❌ I couldn't detect any readable text in this image.")
            return

        # Telegram message limit ~4096 chars → split if needed
        text = text.strip()
        max_len = 3500
        chunks = [text[i:i+max_len] for i in range(0, len(text), max_len)]

        await processing_msg.delete()

        if len(chunks) == 1:
            await update.message.reply_text("🖼️ *Image OCR Result:*\n\n" + chunks[0], parse_mode="Markdown")
        else:
            for idx, chunk in enumerate(chunks, 1):
                header = f"🖼️ *Image OCR (Part {idx}/{len(chunks)})*\n\n"
                await update.message.reply_text(header + chunk, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Image OCR error: {e}")
        try:
            await processing_msg.edit_text(f"❌ Error while reading text: {e}")
        except Exception:
            pass
    finally:
        try:
            if img_path.exists():
                img_path.unlink()
        except Exception as e:
            logger.error(f"Error deleting image file {img_path}: {e}")
