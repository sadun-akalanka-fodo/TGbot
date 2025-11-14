import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes
from PIL import Image, ImageOps
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

        # Open and lightly preprocess image
        img = Image.open(str(img_path)).convert("L")  # grayscale
        img = ImageOps.autocontrast(img)

        # Optional: upscale small images (helps OCR sometimes)
        w, h = img.size
        if max(w, h) < 800:
            img = img.resize((w * 2, h * 2), Image.LANCZOS)

        # Run Tesseract OCR (English + Sinhala)
        # Make sure Tesseract has both 'eng' and 'sin' traineddata installed
        text = pytesseract.image_to_string(
            img,
            lang="eng+sin",
            config="--psm 6"
        )

        if not text or not text.strip():
            await processing_msg.edit_text("❌ I couldn't detect any readable text in this image.")
            return

        text = text.strip()

        # Telegram limit is ~4096 chars, keep some margin
        max_len = 3500
        chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]

        # Remove the "processing" message
        await processing_msg.delete()

        if len(chunks) == 1:
            # No Markdown → avoids issues with * _ [ ] etc.
            await update.message.reply_text(
                "🖼️ Image OCR Result:\n\n" + chunks[0]
            )
        else:
            for idx, chunk in enumerate(chunks, 1):
                header = f"🖼️ Image OCR (Part {idx}/{len(chunks)}):\n\n"
                await update.message.reply_text(header + chunk)

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
