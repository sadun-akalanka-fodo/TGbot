# ocr_pdf.py
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

import pytesseract
from pdf2image import convert_from_path

logger = logging.getLogger(__name__)

# Use same downloads folder as main bot
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Default OCR languages: English + Sinhala
# You can change this to "eng", "sin", "eng+sin", etc.
OCR_LANG = "eng+sin"


def cleanup_file(path: Path) -> None:
    """Delete file if exists."""
    try:
        if path.exists():
            path.unlink()
    except Exception as e:
        logger.error(f"Error deleting file {path}: {e}")


async def handle_pdf_ocr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle PDF files: extract text using OCR and send as messages."""
    if not update.message or not update.message.document:
        return

    doc = update.message.document

    # Only handle PDFs
    if not doc.file_name.lower().endswith(".pdf"):
        return

    processing = await update.message.reply_text(
        "📄 Reading your PDF...\n"
        "⏳ Converting pages and extracting text (English + Sinhala)..."
    )

    pdf_path = DOWNLOAD_DIR / f"ocr_{update.effective_user.id}.pdf"

    try:
        # 1) Download the PDF
        file = await doc.get_file()
        await file.download_to_drive(str(pdf_path))

        # 2) Convert PDF pages to images
        images = convert_from_path(str(pdf_path))

        # 3) Run OCR on each page
        full_text = ""
        for idx, img in enumerate(images, start=1):
            page_text = pytesseract.image_to_string(img, lang=OCR_LANG)
            if page_text.strip():
                full_text += f"\n\n--- Page {idx} ---\n{page_text}"

        if not full_text.strip():
            await processing.edit_text(
                "❌ I couldn't extract any text.\n"
                "Make sure the PDF has clear printed text (not handwriting)."
            )
            return

        # 4) Send text in chunks (Telegram max ~4096 chars)
        await processing.edit_text(
            "✅ Text extracted! Sending it in parts..."
        )

        MAX_LEN = 3500
        for i in range(0, len(full_text), MAX_LEN):
            chunk = full_text[i:i+MAX_LEN]
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=chunk
            )

    except Exception as e:
        logger.error(f"OCR error: {e}")
        await processing.edit_text(f"❌ Error while reading PDF: {e}")
    finally:
        cleanup_file(pdf_path)
