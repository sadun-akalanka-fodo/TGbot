import os
import json
import logging
import asyncio
import time
from pathlib import Path
from collections import defaultdict, deque
from typing import Dict, Any, List, Optional

import httpx
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Chat,
    Message,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ================== BASIC CONFIG & LOGGING ==================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

STATE_FILE = Path("bot_state.json")

# In-memory tracking for anti-spam (not persisted)
user_activity: Dict[int, Dict[int, deque]] = defaultdict(lambda: defaultdict(deque))
RECENT_MESSAGES: Dict[int, deque] = defaultdict(lambda: deque(maxlen=300))  # chat_id -> recent messages


# ================== STATE MANAGEMENT ==================

def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading state: {e}")
            data = {}
    else:
        data = {}

    data.setdefault("cloud_files", {})        # user_id -> list of {file_id, file_type, file_name, size, ts}
    data.setdefault("proxy_data", {})         # user_id -> {proxies: [...], last_results: [...]}
    data.setdefault("auto_responder", {})     # scope_key -> list of {trigger, reply}
    data.setdefault("anti_spam", {})          # chat_id -> {enabled, msg_limit, time_window, block_links}
    data.setdefault("cleaner", {})            # chat_id -> cleaner settings (optional)
    data.setdefault("reposter_rules", [])     # list of {source_chat_id, target_chat_id, filter_type}
    return data


def save_state():
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving state: {e}")


state = load_state()


# ================== HELPER FUNCTIONS ==================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_menu_keyboard(is_admin_user: bool) -> ReplyKeyboardMarkup:
    rows = [
        ["📁 Cloud Storage", "🤖 Auto-Responder"],
        ["🌐 Proxy Tools", "🧹 Cleaner Tools"],
        ["🔁 Reposter", "🛡 Group Protection"],
    ]
    if is_admin_user:
        rows.append(["⚙️ Admin Panel"])
    rows.append(["❌ Close Menu"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def cloud_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        ["⬆ Upload file", "📄 My files"],
        ["🗑 Delete file", "🔙 Back to main"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def proxy_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        ["📥 Import proxy list", "✅ Test proxies"],
        ["📊 Last test summary", "🔙 Back to main"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def autoresponder_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        ["➕ Add rule", "📋 List rules"],
        ["🗑 Delete rule", "🔙 Back to main"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def cleaner_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        ["🧽 Clean media (last 100)", "🧹 Clean links (last 100)"],
        ["🧻 Clean forwarded (last 100)", "🔙 Back to main"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def reposter_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        ["➕ Add reposter rule"],
        ["📋 List reposter rules"],
        ["🗑 Remove reposter rule"],
        ["🔙 Back to main"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def group_protection_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        ["🟢 Enable anti-spam", "🔴 Disable anti-spam"],
        ["⚙️ Set spam limits", "🔗 Toggle link blocking"],
        ["🔙 Back to main"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def admin_panel_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        ["👥 User stats", "📈 Usage stats"],
        ["🛡 Anti-spam overview"],
        ["🔁 Reposter overview"],
        ["🔙 Back to main"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def get_scope_key_for_private(user_id: int) -> str:
    return f"user:{user_id}"


def get_scope_key_for_chat(chat_id: int) -> str:
    return f"chat:{chat_id}"


def human_size(num_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


# ================== START & MAIN MENU ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    is_admin_user = is_admin(user.id)
    kb = main_menu_keyboard(is_admin_user)

    await update.message.reply_text(
        "✨ Welcome to your multi-tool Telegram assistant.\n\n"
        "Use the buttons below to access tools.\n"
        "No commands needed – just tap.",
        reply_markup=kb,
    )


# ================== CLOUD STORAGE ==================

async def handle_cloud_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    if not user:
        return

    user_id = str(user.id)

    if text == "⬆ Upload file":
        context.user_data["mode"] = "cloud_upload_wait_file"
        await update.message.reply_text(
            "📁 Send me any file now, and I will store it in your cloud vault.",
            reply_markup=cloud_menu_keyboard(),
        )

    elif text == "📄 My files":
        files = state["cloud_files"].get(user_id, [])
        if not files:
            await update.message.reply_text(
                "📭 You have no stored files yet.",
                reply_markup=cloud_menu_keyboard(),
            )
            return

        for idx, f in enumerate(files, start=1):
            msg = f"{idx}. {f.get('file_name', 'Unnamed')} ({human_size(f.get('size', 0))})"
            await update.message.reply_text(msg)

        await update.message.reply_text(
            "To retrieve a file, send its number (e.g., `1`).",
            reply_markup=cloud_menu_keyboard(),
            parse_mode="Markdown",
        )
        context.user_data["mode"] = "cloud_get_file"

    elif text == "🗑 Delete file":
        files = state["cloud_files"].get(user_id, [])
        if not files:
            await update.message.reply_text(
                "📭 You have no stored files.",
                reply_markup=cloud_menu_keyboard(),
            )
            return

        for idx, f in enumerate(files, start=1):
            msg = f"{idx}. {f.get('file_name', 'Unnamed')} ({human_size(f.get('size', 0))})"
            await update.message.reply_text(msg)
        await update.message.reply_text(
            "Send the number of the file you want to delete.",
            reply_markup=cloud_menu_keyboard(),
        )
        context.user_data["mode"] = "cloud_delete_file"


async def handle_incoming_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store file in cloud if user is in upload mode."""
    user = update.effective_user
    if not user or not update.message:
        return

    mode = context.user_data.get("mode")
    if mode != "cloud_upload_wait_file":
        return  # ignore if not in cloud upload mode

    file_obj = (
        update.message.document
        or update.message.video
        or update.message.audio
        or update.message.voice
        or update.message.photo[-1]
        if update.message.photo
        else None
    )

    if not file_obj:
        await update.message.reply_text(
            "❌ Please send a file (document, video, audio, voice, or photo).",
            reply_markup=cloud_menu_keyboard(),
        )
        return

    file_id = file_obj.file_id
    file_name = getattr(file_obj, "file_name", "file")
    file_size = getattr(file_obj, "file_size", 0)

    user_id = str(user.id)
    files = state["cloud_files"].setdefault(user_id, [])
    files.append(
        {
            "file_id": file_id,
            "file_type": file_obj.__class__.__name__,
            "file_name": file_name,
            "size": file_size,
            "ts": int(time.time()),
        }
    )
    save_state()

    await update.message.reply_text(
        f"✅ Stored `{file_name}` in your cloud.",
        reply_markup=cloud_menu_keyboard(),
        parse_mode="Markdown",
    )


async def handle_cloud_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    mode = context.user_data.get("mode")
    if mode not in ("cloud_get_file", "cloud_delete_file"):
        return

    user_id = str(user.id)
    files = state["cloud_files"].get(user_id, [])
    if not files:
        await update.message.reply_text(
            "📭 No files stored.",
            reply_markup=cloud_menu_keyboard(),
        )
        context.user_data["mode"] = None
        return

    try:
        idx = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(
            "❌ Please send a valid number.",
            reply_markup=cloud_menu_keyboard(),
        )
        return

    if not (1 <= idx <= len(files)):
        await update.message.reply_text(
            "❌ Number out of range.",
            reply_markup=cloud_menu_keyboard(),
        )
        return

    file_info = files[idx - 1]

    if mode == "cloud_get_file":
        # send file back by file_id
        await update.message.reply_text("📤 Sending your file...")
        if file_info["file_type"] == "Document":
            await update.message.reply_document(file_info["file_id"])
        elif file_info["file_type"] == "Video":
            await update.message.reply_video(file_info["file_id"])
        elif file_info["file_type"] == "Audio":
            await update.message.reply_audio(file_info["file_id"])
        elif file_info["file_type"] == "Voice":
            await update.message.reply_voice(file_info["file_id"])
        else:
            # default as document
            await update.message.reply_document(file_info["file_id"])

    elif mode == "cloud_delete_file":
        del files[idx - 1]
        state["cloud_files"][user_id] = files
        save_state()
        await update.message.reply_text(
            "🗑 File deleted from your cloud.",
            reply_markup=cloud_menu_keyboard(),
        )

    context.user_data["mode"] = None


# ================== PROXY TOOLS ==================

async def handle_proxy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    if not user:
        return
    user_id = str(user.id)

    if text == "📥 Import proxy list":
        context.user_data["mode"] = "proxy_import"
        await update.message.reply_text(
            "Paste your proxy list as text.\nOne proxy per line (e.g. `ip:port`).",
            reply_markup=proxy_menu_keyboard(),
        )

    elif text == "✅ Test proxies":
        pdata = state["proxy_data"].get(user_id)
        if not pdata or not pdata.get("proxies"):
            await update.message.reply_text(
                "❌ No proxy list found. Use '📥 Import proxy list' first.",
                reply_markup=proxy_menu_keyboard(),
            )
            return

        await update.message.reply_text(
            "⏳ Starting proxy tests... This may take some time.",
            reply_markup=proxy_menu_keyboard(),
        )

        asyncio.create_task(run_proxy_tests(update, context, user_id))

    elif text == "📊 Last test summary":
        pdata = state["proxy_data"].get(user_id)
        if not pdata or not pdata.get("last_results"):
            await update.message.reply_text(
                "ℹ️ No test results found yet.",
                reply_markup=proxy_menu_keyboard(),
            )
            return

        results = pdata["last_results"]
        total = len(results)
        ok = sum(1 for r in results if r["status"] == "OK")
        failed = total - ok
        avg_ms = (
            sum(r["latency_ms"] for r in results if r["status"] == "OK") / ok
            if ok
            else 0
        )
        msg = (
            f"📊 Proxy Test Summary\n\n"
            f"Total: {total}\n"
            f"✅ Working: {ok}\n"
            f"❌ Failed: {failed}\n"
            f"⏱ Avg. latency: {avg_ms:.1f} ms"
        )
        await update.message.reply_text(msg, reply_markup=proxy_menu_keyboard())


async def run_proxy_tests(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str):
    chat_id = update.effective_chat.id
    pdata = state["proxy_data"].get(user_id)
    if not pdata:
        return
    proxies = pdata.get("proxies", [])
    results = []
    test_url = "https://example.org"

    async with httpx.AsyncClient(timeout=5.0) as client:
        for i, proxy in enumerate(proxies, start=1):
            proxy_str = proxy.strip()
            if not proxy_str:
                continue
            start = time.time()
            status = "FAILED"
            latency_ms = 0.0
            try:
                r = await client.get(test_url, proxies={"https://": f"http://{proxy_str}"})
                latency_ms = (time.time() - start) * 1000
                if r.status_code == 200:
                    status = "OK"
            except Exception:
                status = "FAILED"

            results.append(
                {"proxy": proxy_str, "status": status, "latency_ms": latency_ms}
            )

            if i % 10 == 0:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Tested {i}/{len(proxies)} proxies...",
                )

    state["proxy_data"][user_id]["last_results"] = results
    save_state()
    await context.bot.send_message(
        chat_id=chat_id,
        text="✅ Proxy testing finished. Use '📊 Last test summary' to view results.",
    )


async def handle_proxy_import_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if context.user_data.get("mode") != "proxy_import":
        return

    lines = [line.strip() for line in update.message.text.splitlines() if line.strip()]
    if not lines:
        await update.message.reply_text(
            "❌ No proxies detected. Please send one per line.",
            reply_markup=proxy_menu_keyboard(),
        )
        return

    user_id = str(user.id)
    state["proxy_data"][user_id] = {"proxies": lines, "last_results": []}
    save_state()
    context.user_data["mode"] = None

    await update.message.reply_text(
        f"✅ Stored {len(lines)} proxies.\nUse '✅ Test proxies' to start testing.",
        reply_markup=proxy_menu_keyboard(),
    )


# ================== AUTO-RESPONDER ==================

async def handle_autoresponder_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # Scope: private chat -> per user; group -> per chat
    if chat.type == Chat.PRIVATE:
        scope_key = get_scope_key_for_private(user.id)
    else:
        scope_key = get_scope_key_for_chat(chat.id)

    rules = state["auto_responder"].setdefault(scope_key, [])

    if text == "➕ Add rule":
        context.user_data["mode"] = "autoresponder_add_trigger"
        context.user_data["autoresponder_scope"] = scope_key
        await update.message.reply_text(
            "✏️ Send the trigger text (word or phrase to detect).",
            reply_markup=autoresponder_menu_keyboard(),
        )

    elif text == "📋 List rules":
        if not rules:
            await update.message.reply_text(
                "📭 No rules defined yet.",
                reply_markup=autoresponder_menu_keyboard(),
            )
            return
        msg = "📋 Auto-responder rules:\n\n"
        for idx, r in enumerate(rules, start=1):
            msg += f"{idx}. If message contains '{r['trigger']}' → reply '{r['reply']}'\n"
        await update.message.reply_text(msg, reply_markup=autoresponder_menu_keyboard())

    elif text == "🗑 Delete rule":
        if not rules:
            await update.message.reply_text(
                "📭 No rules to delete.",
                reply_markup=autoresponder_menu_keyboard(),
            )
            return
        msg = "Send the rule number to delete:\n\n"
        for idx, r in enumerate(rules, start=1):
            msg += f"{idx}. '{r['trigger']}' → '{r['reply']}'\n"
        await update.message.reply_text(msg, reply_markup=autoresponder_menu_keyboard())
        context.user_data["mode"] = "autoresponder_delete"
        context.user_data["autoresponder_scope"] = scope_key


async def handle_autoresponder_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    mode = context.user_data.get("mode")
    scope_key = context.user_data.get("autoresponder_scope")
    if not scope_key:
        return

    rules = state["auto_responder"].setdefault(scope_key, [])

    if mode == "autoresponder_add_trigger":
        context.user_data["new_trigger"] = update.message.text.strip()
        context.user_data["mode"] = "autoresponder_add_reply"
        await update.message.reply_text(
            "💬 Now send the reply text for this trigger.",
            reply_markup=autoresponder_menu_keyboard(),
        )

    elif mode == "autoresponder_add_reply":
        trigger = context.user_data.get("new_trigger")
        reply = update.message.text.strip()
        if not trigger:
            await update.message.reply_text(
                "❌ Trigger missing. Start again with '➕ Add rule'.",
                reply_markup=autoresponder_menu_keyboard(),
            )
        else:
            rules.append({"trigger": trigger, "reply": reply})
            save_state()
            await update.message.reply_text(
                f"✅ Rule added:\nIf message contains '{trigger}' → reply '{reply}'",
                reply_markup=autoresponder_menu_keyboard(),
            )
        context.user_data["mode"] = None
        context.user_data["new_trigger"] = None
        context.user_data["autoresponder_scope"] = None

    elif mode == "autoresponder_delete":
        try:
            idx = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text(
                "❌ Please send a valid number.",
                reply_markup=autoresponder_menu_keyboard(),
            )
            return

        if not (1 <= idx <= len(rules)):
            await update.message.reply_text(
                "❌ Number out of range.",
                reply_markup=autoresponder_menu_keyboard(),
            )
            return

        removed = rules.pop(idx - 1)
        save_state()
        await update.message.reply_text(
            f"🗑 Deleted rule: '{removed['trigger']}' → '{removed['reply']}'",
            reply_markup=autoresponder_menu_keyboard(),
        )
        context.user_data["mode"] = None
        context.user_data["autoresponder_scope"] = None


async def maybe_run_autoresponder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check if any auto-responder rule matches and reply."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    text = msg.text or msg.caption
    if not text:
        return

    # Avoid reacting to our own messages
    if msg.from_user and msg.from_user.is_bot:
        return

    # Private rules
    if chat.type == Chat.PRIVATE:
        scope_keys = [get_scope_key_for_private(user.id)]
    else:
        scope_keys = [get_scope_key_for_chat(chat.id)]

    for scope_key in scope_keys:
        rules = state["auto_responder"].get(scope_key) or []
        for r in rules:
            if r["trigger"].lower() in text.lower():
                try:
                    await msg.reply_text(r["reply"])
                except Exception as e:
                    logger.error(f"Error sending autoresponder reply: {e}")
                break


# ================== ANTI-SPAM & GROUP PROTECTION ==================

async def handle_group_protection_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    if chat.type == Chat.PRIVATE:
        await update.message.reply_text(
            "🛡 Group Protection works only in groups or supergroups.",
            reply_markup=group_protection_menu_keyboard(),
        )
        return

    if not is_admin(user.id):
        await update.message.reply_text(
            "⛔ Only bot admins can configure group protection.",
            reply_markup=group_protection_menu_keyboard(),
        )
        return

    text = update.message.text
    chat_id = str(chat.id)
    cfg = state["anti_spam"].setdefault(
        chat_id,
        {"enabled": False, "msg_limit": 5, "time_window": 10, "block_links": False},
    )

    if text == "🟢 Enable anti-spam":
        cfg["enabled"] = True
        save_state()
        await update.message.reply_text(
            "🛡 Anti-spam enabled for this group.",
            reply_markup=group_protection_menu_keyboard(),
        )
    elif text == "🔴 Disable anti-spam":
        cfg["enabled"] = False
        save_state()
        await update.message.reply_text(
            "🛡 Anti-spam disabled for this group.",
            reply_markup=group_protection_menu_keyboard(),
        )
    elif text == "⚙️ Set spam limits":
        context.user_data["mode"] = "set_spam_limits"
        await update.message.reply_text(
            f"Current: {cfg['msg_limit']} messages per {cfg['time_window']} seconds.\n"
            "Send `messages time_window` (e.g. `5 10`).",
            reply_markup=group_protection_menu_keyboard(),
        )
    elif text == "🔗 Toggle link blocking":
        cfg["block_links"] = not cfg["block_links"]
        save_state()
        status = "ON" if cfg["block_links"] else "OFF"
        await update.message.reply_text(
            f"🔗 Link blocking is now {status}.",
            reply_markup=group_protection_menu_keyboard(),
        )


async def handle_spam_limit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or not update.message:
        return
    if context.user_data.get("mode") != "set_spam_limits":
        return

    if not is_admin(user.id):
        context.user_data["mode"] = None
        return

    parts = update.message.text.strip().split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await update.message.reply_text(
            "❌ Invalid format. Use `messages time_window`, e.g. `5 10`.",
            reply_markup=group_protection_menu_keyboard(),
        )
        context.user_data["mode"] = None
        return

    msg_limit = int(parts[0])
    time_window = int(parts[1])
    cfg = state["anti_spam"].setdefault(
        str(chat.id),
        {"enabled": False, "msg_limit": 5, "time_window": 10, "block_links": False},
    )
    cfg["msg_limit"] = msg_limit
    cfg["time_window"] = time_window
    save_state()
    context.user_data["mode"] = None

    await update.message.reply_text(
        f"✅ Updated spam limits: {msg_limit} messages / {time_window} seconds.",
        reply_markup=group_protection_menu_keyboard(),
    )


def count_links(text: str) -> int:
    if not text:
        return 0
    # very simple heuristic
    markers = ["http://", "https://", "t.me/", "www."]
    return sum(text.count(m) for m in markers)


async def group_message_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    # Track recent messages for cleaner
    RECENT_MESSAGES[chat.id].append(msg)

    # Anti-spam
    chat_cfg = state["anti_spam"].get(str(chat.id))
    if not chat_cfg or not chat_cfg.get("enabled"):
        # Still run auto-responder if exists
        await maybe_run_autoresponder(update, context)
        return

    # Skip bot admins from spam rules
    if is_admin(user.id):
        await maybe_run_autoresponder(update, context)
        return

    text = msg.text or msg.caption or ""
    now = time.time()
    q = user_activity[chat.id][user.id]
    q.append(now)
    # keep only last N seconds
    while q and now - q[0] > chat_cfg["time_window"]:
        q.popleft()

    if len(q) > chat_cfg["msg_limit"]:
        try:
            await msg.delete()
        except Exception as e:
            logger.error(f"Failed to delete spam message: {e}")
        return

    # Link blocking
    if chat_cfg.get("block_links") and count_links(text) > 0:
        try:
            await msg.delete()
        except Exception as e:
            logger.error(f"Failed to delete link message: {e}")
        return

    # Auto-responder (for groups)
    await maybe_run_autoresponder(update, context)


# ================== CLEANER TOOLS ==================

async def handle_cleaner_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    text = update.message.text
    if not chat or not user:
        return

    if chat.type == Chat.PRIVATE:
        await update.message.reply_text(
            "🧹 Cleaner tools work only in groups/channels where the bot is present.",
            reply_markup=cleaner_menu_keyboard(),
        )
        return

    if not is_admin(user.id):
        await update.message.reply_text(
            "⛔ Only bot admins can run cleaners.",
            reply_markup=cleaner_menu_keyboard(),
        )
        return

    if text == "🧽 Clean media (last 100)":
        asyncio.create_task(run_cleaner(update, context, mode="media"))
    elif text == "🧹 Clean links (last 100)":
        asyncio.create_task(run_cleaner(update, context, mode="links"))
    elif text == "🧻 Clean forwarded (last 100)":
        asyncio.create_task(run_cleaner(update, context, mode="forwards"))


async def run_cleaner(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    chat = update.effective_chat
    if not chat:
        return

    await update.message.reply_text("⏳ Running cleaner on last 100 messages...")
    msgs = list(RECENT_MESSAGES[chat.id])[-100:]
    deleted = 0

    for m in msgs:
        try:
            if mode == "media":
                if m.photo or m.video or m.document or m.audio or m.voice:
                    await m.delete()
                    deleted += 1
            elif mode == "links":
                text = m.text or m.caption or ""
                if count_links(text) > 0:
                    await m.delete()
                    deleted += 1
            elif mode == "forwards":
                if m.forward_from or m.forward_from_chat:
                    await m.delete()
                    deleted += 1
        except Exception as e:
            logger.error(f"Cleaner delete error: {e}")

    await context.bot.send_message(
        chat_id=chat.id,
        text=f"🧹 Cleaner finished. Deleted {deleted} messages.",
        reply_markup=cleaner_menu_keyboard(),
    )


# ================== REPOSTER ==================

async def handle_reposter_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    text = update.message.text
    if not user or not chat:
        return

    if not is_admin(user.id):
        await update.message.reply_text(
            "⛔ Only admins can configure repost rules.",
            reply_markup=reposter_menu_keyboard(),
        )
        return

    if text == "➕ Add reposter rule":
        context.user_data["mode"] = "reposter_add_source"
        await update.message.reply_text(
            "Send the source chat ID or @username (where messages come from).",
            reply_markup=reposter_menu_keyboard(),
        )

    elif text == "📋 List reposter rules":
        rules = state["reposter_rules"]
        if not rules:
            await update.message.reply_text(
                "📭 No reposter rules defined.",
                reply_markup=reposter_menu_keyboard(),
            )
            return
        msg = "🔁 Reposter rules:\n\n"
        for idx, r in enumerate(rules, start=1):
            msg += (
                f"{idx}. {r['source_chat_id']} → {r['target_chat_id']} "
                f"({r['filter_type']})\n"
            )
        await update.message.reply_text(msg, reply_markup=reposter_menu_keyboard())

    elif text == "🗑 Remove reposter rule":
        rules = state["reposter_rules"]
        if not rules:
            await update.message.reply_text(
                "📭 No rules to remove.",
                reply_markup=reposter_menu_keyboard(),
            )
            return
        msg = "Send the rule number to remove:\n\n"
        for idx, r in enumerate(rules, start=1):
            msg += (
                f"{idx}. {r['source_chat_id']} → {r['target_chat_id']} "
                f"({r['filter_type']})\n"
            )
        await update.message.reply_text(msg, reply_markup=reposter_menu_keyboard())
        context.user_data["mode"] = "reposter_delete"


async def handle_reposter_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    mode = context.user_data.get("mode")
    if mode not in ("reposter_add_source", "reposter_add_target", "reposter_add_filter", "reposter_delete"):
        return

    if not is_admin(user.id):
        context.user_data["mode"] = None
        return

    text = update.message.text.strip()

    if mode == "reposter_add_source":
        context.user_data["reposter_source"] = text
        context.user_data["mode"] = "reposter_add_target"
        await update.message.reply_text(
            "Now send the target chat ID or @username (where messages should be reposted).",
            reply_markup=reposter_menu_keyboard(),
        )

    elif mode == "reposter_add_target":
        context.user_data["reposter_target"] = text
        context.user_data["mode"] = "reposter_add_filter"
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("All messages", callback_data="repf_all"),
                    InlineKeyboardButton("Only media", callback_data="repf_media"),
                ],
                [
                    InlineKeyboardButton("Only text", callback_data="repf_text"),
                ]
            ]
        )
        await update.message.reply_text(
            "Choose filter type for this rule:",
            reply_markup=kb,
        )

    elif mode == "reposter_delete":
        try:
            idx = int(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid number.",
                reply_markup=reposter_menu_keyboard(),
            )
            return
        rules = state["reposter_rules"]
        if not (1 <= idx <= len(rules)):
            await update.message.reply_text(
                "❌ Out of range.",
                reply_markup=reposter_menu_keyboard(),
            )
            return
        removed = rules.pop(idx - 1)
        save_state()
        await update.message.reply_text(
            f"🗑 Removed rule: {removed['source_chat_id']} → {removed['target_chat_id']} ({removed['filter_type']})",
            reply_markup=reposter_menu_keyboard(),
        )
        context.user_data["mode"] = None


async def handle_reposter_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    user = update.effective_user
    if not user:
        return

    if context.user_data.get("mode") != "reposter_add_filter":
        return

    if not is_admin(user.id):
        return

    filter_type = "all"
    if data == "repf_media":
        filter_type = "media"
    elif data == "repf_text":
        filter_type = "text"

    source = context.user_data.get("reposter_source")
    target = context.user_data.get("reposter_target")
    if not source or not target:
        await query.edit_message_text("❌ Missing source/target. Start again.")
        context.user_data["mode"] = None
        return

    state["reposter_rules"].append(
        {
            "source_chat_id": source,
            "target_chat_id": target,
            "filter_type": filter_type,
        }
    )
    save_state()
    context.user_data["mode"] = None
    context.user_data["reposter_source"] = None
    context.user_data["reposter_target"] = None

    await query.edit_message_text(
        f"✅ Reposter rule added:\n{source} → {target} ({filter_type})"
    )


async def maybe_repost_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    # avoid reposting our own messages
    if msg.from_user and msg.from_user.is_bot:
        return

    # Check rules
    for rule in state["reposter_rules"]:
        src = rule["source_chat_id"]
        # source may be numeric id or @username
        match = False
        if src.startswith("@") and chat.username:
            match = src.lstrip("@").lower() == chat.username.lower()
        else:
            try:
                match = int(src) == chat.id
            except ValueError:
                pass

        if not match:
            continue

        filter_type = rule["filter_type"]
        has_media = bool(msg.photo or msg.video or msg.document or msg.audio or msg.voice)
        has_text = bool(msg.text or msg.caption)

        if filter_type == "media" and not has_media:
            continue
        if filter_type == "text" and not has_text:
            continue

        target = rule["target_chat_id"]
        try:
            if target.startswith("@"):
                await msg.forward(chat_id=target)
            else:
                await msg.forward(chat_id=int(target))
        except Exception as e:
            logger.error(f"Error forwarding message for repost rule: {e}")


# ================== ADMIN PANEL ==================

async def handle_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_admin(user.id):
        return

    text = update.message.text

    if text == "👥 User stats":
        cloud_users = len(state["cloud_files"])
        proxy_users = len(state["proxy_data"])
        auto_scopes = len(state["auto_responder"])
        await update.message.reply_text(
            f"👥 User Stats:\n\n"
            f"Users with cloud files: {cloud_users}\n"
            f"Users with proxy data: {proxy_users}\n"
            f"Auto-responder scopes: {auto_scopes}",
            reply_markup=admin_panel_keyboard(),
        )

    elif text == "📈 Usage stats":
        rules = len(state["reposter_rules"])
        anti_chats = len(state["anti_spam"])
        await update.message.reply_text(
            f"📈 Usage Stats:\n\n"
            f"Reposter rules: {rules}\n"
            f"Chats with anti-spam config: {anti_chats}",
            reply_markup=admin_panel_keyboard(),
        )

    elif text == "🛡 Anti-spam overview":
        lines = []
        for chat_id, cfg in state["anti_spam"].items():
            lines.append(
                f"{chat_id}: enabled={cfg['enabled']}, "
                f"limit={cfg['msg_limit']}/{cfg['time_window']}s, "
                f"block_links={cfg['block_links']}"
            )
        if not lines:
            msg = "No anti-spam configs yet."
        else:
            msg = "🛡 Anti-spam configs:\n" + "\n".join(lines)
        await update.message.reply_text(msg, reply_markup=admin_panel_keyboard())

    elif text == "🔁 Reposter overview":
        rules = state["reposter_rules"]
        if not rules:
            await update.message.reply_text(
                "📭 No reposter rules defined.",
                reply_markup=admin_panel_keyboard(),
            )
            return
        msg = "🔁 Reposter rules:\n\n"
        for idx, r in enumerate(rules, start=1):
            msg += (
                f"{idx}. {r['source_chat_id']} → {r['target_chat_id']} "
                f"({r['filter_type']})\n"
            )
        await update.message.reply_text(msg, reply_markup=admin_panel_keyboard())


# ================== MAIN TEXT HANDLER (BUTTON NAV) ==================

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    text = update.message.text

    # main navigation
    if text == "❌ Close Menu":
        context.user_data["mode"] = None
        await update.message.reply_text("Menu closed.", reply_markup=ReplyKeyboardRemove())
        return

    if text == "🔙 Back to main":
        context.user_data["mode"] = None
        kb = main_menu_keyboard(is_admin(user.id))
        await update.message.reply_text("Back to main menu.", reply_markup=kb)
        return

    # branch by main buttons
    if text == "📁 Cloud Storage":
        context.user_data["mode"] = None
        await update.message.reply_text(
            "📁 Cloud Storage menu:",
            reply_markup=cloud_menu_keyboard(),
        )
        return

    if text == "🌐 Proxy Tools":
        context.user_data["mode"] = None
        await update.message.reply_text(
            "🌐 Proxy tools menu:",
            reply_markup=proxy_menu_keyboard(),
        )
        return

    if text == "🤖 Auto-Responder":
        await update.message.reply_text(
            "🤖 Auto-responder menu:",
            reply_markup=autoresponder_menu_keyboard(),
        )
        return

    if text == "🧹 Cleaner Tools":
        await update.message.reply_text(
            "🧹 Cleaner tools menu:",
            reply_markup=cleaner_menu_keyboard(),
        )
        return

    if text == "🔁 Reposter":
        await update.message.reply_text(
            "🔁 Reposter menu:",
            reply_markup=reposter_menu_keyboard(),
        )
        return

    if text == "🛡 Group Protection":
        await update.message.reply_text(
            "🛡 Group protection menu:",
            reply_markup=group_protection_menu_keyboard(),
        )
        return

    if text == "⚙️ Admin Panel":
        if is_admin(user.id):
            await update.message.reply_text(
                "⚙️ Admin panel:",
                reply_markup=admin_panel_keyboard(),
            )
        else:
            await update.message.reply_text("⛔ Admins only.")
        return

    # submenus
    if text in {"⬆ Upload file", "📄 My files", "🗑 Delete file"}:
        await handle_cloud_menu(update, context)
        return

    if text in {"📥 Import proxy list", "✅ Test proxies", "📊 Last test summary"}:
        await handle_proxy_menu(update, context)
        return

    if text in {"➕ Add rule", "📋 List rules", "🗑 Delete rule"}:
        await handle_autoresponder_menu(update, context)
        return

    if text in {
        "🧽 Clean media (last 100)",
        "🧹 Clean links (last 100)",
        "🧻 Clean forwarded (last 100)",
    }:
        await handle_cleaner_menu(update, context)
        return

    if text in {"➕ Add reposter rule", "📋 List reposter rules", "🗑 Remove reposter rule"}:
        await handle_reposter_menu(update, context)
        return

    if text in {
        "🟢 Enable anti-spam",
        "🔴 Disable anti-spam",
        "⚙️ Set spam limits",
        "🔗 Toggle link blocking",
    }:
        await handle_group_protection_menu(update, context)
        return

    if text in {
        "👥 User stats",
        "📈 Usage stats",
        "🛡 Anti-spam overview",
        "🔁 Reposter overview",
    }:
        await handle_admin_panel(update, context)
        return

    # Mode-based behavior
    mode = context.user_data.get("mode")

    if mode in ("cloud_get_file", "cloud_delete_file"):
        await handle_cloud_number_input(update, context)
        return

    if mode in (
        "proxy_import",
        "autoresponder_add_trigger",
        "autoresponder_add_reply",
        "autoresponder_delete",
        "reposter_add_source",
        "reposter_add_target",
        "reposter_delete",
        "set_spam_limits",
    ):
        # delegate to relevant handlers
        if mode == "proxy_import":
            await handle_proxy_import_text(update, context)
        elif mode.startswith("autoresponder_"):
            await handle_autoresponder_text(update, context)
        elif mode.startswith("reposter_"):
            await handle_reposter_text(update, context)
        elif mode == "set_spam_limits":
            await handle_spam_limit_text(update, context)
        return

    # If nothing else, still maybe trigger auto-responder
    await maybe_run_autoresponder(update, context)


# ================== ERROR HANDLER ==================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update %s caused error %s", update, context.error)


# ================== MAIN ==================

def main():
    if not TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN is not set.")
        return

    logger.info("Starting multi-tool bot...")
    logger.info(f"Admins: {ADMIN_IDS}")

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))

    # Callbacks for inline buttons
    app.add_handler(CallbackQueryHandler(handle_reposter_filter_callback, pattern=r"^repf_"))

    # Files (cloud upload)
    app.add_handler(
        MessageHandler(
            filters.Document | filters.Video | filters.Audio | filters.Voice | filters.PHOTO,
            handle_incoming_file,
        )
    )

    # Group monitor (anti-spam + autoresponder + cleaner tracking + repost)
    app.add_handler(
        MessageHandler(
            filters.ALL & (filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP),
            group_message_monitor,
        ),
        group=0,
    )
    # Reposter for all chats (after spam checks)
    app.add_handler(
        MessageHandler(
            filters.ALL & (filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP | filters.ChatType.CHANNEL),
            maybe_repost_message,
        ),
        group=1,
    )

    # Text router for buttons & modes (private + groups)
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            text_router,
        ),
        group=2,
    )

    # Global error handler
    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
