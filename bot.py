import html
import logging
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
HELP_TEXT = (
    "📸 Photo + caption format:\n\n"
    "First line: Movie name\n"
    "Next lines: Download link, then optional Watch link\n\n"
    "Example:\n"
    "ಕೂಲಿ (2025) ಕನ್ನಡ ಡಬ್ (HD)\n"
    "Download: https://example.com/download\n"
    "Watch: https://example.com/watch\n\n"
    "Use /normal for the regular caption or /buttons for inline buttons.\n"
    "Send this along with a photo or as text."
)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
DEFAULT_FOOTER = "🔰𝐉𝐨𝐢𝐧 :- 🌟🌟\\n@SKR_updates\\n@SKR_updates"
FOOTER_DB_PATH = os.getenv("FOOTER_DB_PATH", "footer.db")
MONGO_URI = (
    os.getenv("MONGODB_URI")
    or os.getenv("MONGO_URI")
    or os.getenv("MONGO_URL")
    or os.getenv("MONGODB_URL")
    or ""
).strip()
_mongo_client = None
_mongo_footers = None
_mongo_settings = None


def init_footer_db():
    global _mongo_client, _mongo_footers, _mongo_settings
    if MONGO_URI:
        from pymongo import MongoClient
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
        _mongo_client.admin.command("ping")
        db_name = os.getenv("MONGODB_DATABASE", "skr_photo_caption_bot")
        collection_name = os.getenv("MONGODB_FOOTER_COLLECTION", "user_footers")
        _mongo_footers = _mongo_client[db_name][collection_name]
        _mongo_settings = _mongo_client[db_name][os.getenv("MONGODB_SETTINGS_COLLECTION", "user_settings")]
        _mongo_footers.create_index("user_id", unique=True)
        _mongo_settings.create_index("user_id", unique=True)
        logging.info("Footer storage connected to MongoDB database %s", db_name)
    else:
        with sqlite3.connect(FOOTER_DB_PATH) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_footers (user_id INTEGER PRIMARY KEY, footer TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, post_mode TEXT NOT NULL DEFAULT 'normal')"
            )
        logging.warning("MongoDB URI not found; using local SQLite footer storage.")


def get_footer(user_id: int) -> str:
    if _mongo_footers is not None:
        row = _mongo_footers.find_one({"user_id": user_id}, {"footer": 1})
        return row["footer"] if row and "footer" in row else DEFAULT_FOOTER
    with sqlite3.connect(FOOTER_DB_PATH) as conn:
        row = conn.execute(
            "SELECT footer FROM user_footers WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row else DEFAULT_FOOTER


def save_footer(user_id: int, footer: str):
    if _mongo_footers is not None:
        _mongo_footers.update_one(
            {"user_id": user_id},
            {"$set": {"footer": footer}},
            upsert=True,
        )
        return
    with sqlite3.connect(FOOTER_DB_PATH) as conn:
        conn.execute(
            "INSERT INTO user_footers (user_id, footer) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET footer = excluded.footer",
            (user_id, footer),
        )


def reset_footer(user_id: int):
    if _mongo_footers is not None:
        _mongo_footers.delete_one({"user_id": user_id})
        return
    with sqlite3.connect(FOOTER_DB_PATH) as conn:
        conn.execute("DELETE FROM user_footers WHERE user_id = ?", (user_id,))


def get_post_mode(user_id: int) -> str:
    if _mongo_settings is not None:
        row = _mongo_settings.find_one({"user_id": user_id}, {"post_mode": 1})
        return row.get("post_mode", "normal") if row else "normal"
    with sqlite3.connect(FOOTER_DB_PATH) as conn:
        row = conn.execute(
            "SELECT post_mode FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row else "normal"


def save_post_mode(user_id: int, mode: str):
    if _mongo_settings is not None:
        _mongo_settings.update_one(
            {"user_id": user_id}, {"$set": {"post_mode": mode}}, upsert=True
        )
        return
    with sqlite3.connect(FOOTER_DB_PATH) as conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, post_mode) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET post_mode = excluded.post_mode",
            (user_id, mode),
        )


def render_footer(footer: str) -> str:
    rendered = []
    for line in footer.splitlines():
        safe = html.escape(line)
        if URL_RE.fullmatch(line.strip()):
            safe = f'<a href="{html.escape(line.strip(), quote=True)}">{safe}</a>'
        rendered.append(f"<b>{safe}</b>")
    return "\\n".join(rendered)
def literal_bold(text: str) -> str:
    """Render **wrapped content** as bold HTML; keep URLs clickable."""
    safe_text = html.escape(text)
    if URL_RE.fullmatch(text):
        return f'<b><a href="{safe_text}">{safe_text}</a></b>'
    return f"<b>{safe_text}</b>"


def make_caption(movie_name: str, link: str, footer: str = DEFAULT_FOOTER) -> str:
    title = " ".join(literal_bold(word) for word in movie_name.split())
    safe_link = html.escape(link)
    return (
        f"{title}\n"
        f"{literal_bold('━━━━━━━━━━━━━━━━━━━━')}\n"
        f"{literal_bold('𝗙𝗜𝗟𝗘𝗦')} {literal_bold('𝗟𝗶𝗻𝗸')} {literal_bold('👇🏻👇🏻👇🏻👇🏻')}\n"
        f"{literal_bold(link)}\n"
        f"{literal_bold(link)}\n"
        f"{literal_bold(link)}\n\n"
        f"{literal_bold('𝗛𝗢𝗪')} {literal_bold('𝗧𝗢')} {literal_bold('𝗢𝗣𝗘𝗡')} {literal_bold('𝗟𝗶𝗡𝗞🔗👇👇')}\n"
        f"{literal_bold('https://t.me/Hwotoopenlink')}\n"
        f"{literal_bold('══════════════════════')}\n"
        f"{render_footer(footer)}"
    )


def parse_caption(text: str):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None, None, None
    movie_name = lines[0]
    links = []
    for line in lines[1:]:
        match = URL_RE.search(line)
        if match:
            link = match.group(0).rstrip(".,);]")
            if link not in links:
                links.append(link)
    if not movie_name or not links:
        return None, None, None
    download_link = links[0]
    watch_link = links[1] if len(links) > 1 else links[0]
    return movie_name, download_link, watch_link


def make_buttons_caption(movie_name: str, download_link: str, watch_link: str, footer: str = DEFAULT_FOOTER) -> str:
    title = " ".join(literal_bold(word) for word in movie_name.split())
    return (
        f"{title}\n"
        f"{literal_bold('━━━━━━━━━━━━━━━━━━━━')}\n"
        f"{literal_bold('𝗙𝗜𝗟𝗘𝗦')} {literal_bold('𝗟𝗶𝗻𝗸')} {literal_bold('👇🏻👇🏻👇🏻👇🏻')}\n"
        f"{literal_bold('Download / Watch using the buttons below')}\n"
        f"{literal_bold('══════════════════════')}\n"
        f"{render_footer(footer)}"
    )


def make_buttons(download_link: str, watch_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬇️ Download", url=download_link),
            InlineKeyboardButton("▶️ Watch Online", url=watch_link),
        ],
        [
            InlineKeyboardButton("📢 Join Updates Channel", url="https://t.me/SKR_updates"),
        ],
    ])


async def setfooter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    footer = " ".join(context.args).strip()
    if not footer:
        await message.reply_text(
            "Footer set maadalu format: /setfooter 🔰 Join :- @YourChannel",
            parse_mode=None,
        )
        return
    if len(footer) > 1000:
        await message.reply_text("Footer 1000 characters olage irabeku.", parse_mode=None)
        return
    save_footer(user.id, footer.replace("\\n", "\n"))
    await message.reply_text(
        "✅ Nimma footer save aagide. Idu nimma captions-ge maatra apply aagutte.",
        parse_mode=None,
    )


async def showfooter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if message and user:
        await message.reply_text(
            "Nimma current footer:\\n\\n" + get_footer(user.id),
            parse_mode=None,
        )


async def resetfooter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if message and user:
        reset_footer(user.id)
        await message.reply_text("✅ Default footer restore aagide.", parse_mode=None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "Welcome to SKR Photo Caption Bot!\n\n" + HELP_TEXT,
            parse_mode=None,
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(HELP_TEXT, parse_mode=None)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.photo:
        return

    movie_name, download_link, watch_link = parse_caption(message.caption or "")
    reply_parameters = ReplyParameters(message_id=message.message_id)
    if not movie_name or not download_link or not watch_link:
        await message.reply_text(
            "❌ Caption format sari illa. First line movie name, next line download link; optional second link Watch link.",
            parse_mode=None,
            reply_parameters=reply_parameters,
        )
        return

    user_id = update.effective_user.id if update.effective_user else 0
    if get_post_mode(user_id) == "buttons":
        caption = make_buttons_caption(movie_name, download_link, watch_link, get_footer(user_id))
        keyboard = make_buttons(download_link, watch_link)
    else:
        caption = make_caption(movie_name, download_link, get_footer(user_id))
        keyboard = None

    await message.reply_photo(
        photo=message.photo[-1].file_id,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        reply_parameters=reply_parameters,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    movie_name, download_link, watch_link = parse_caption(message.text)
    if not movie_name or not download_link or not watch_link:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    if context.user_data.get("post_mode", "normal") == "buttons":
        output = make_buttons_caption(movie_name, download_link, watch_link, get_footer(user_id))
        keyboard = make_buttons(download_link, watch_link)
    else:
        output = make_caption(movie_name, download_link, get_footer(user_id))
        keyboard = None

    await message.reply_text(
        output,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        reply_parameters=ReplyParameters(message_id=message.message_id),
    )


async def normal_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        user = update.effective_user
        if not user:
            return
        save_post_mode(user.id, "normal")
        await update.effective_message.reply_text(
            "✅ Normal post mode saved. Movie name + short link + stream link ಕಳುಹಿಸಿದರೆ ಈ format automatic ಆಗಿ ಬರುತ್ತದೆ.",
            parse_mode=None,
        )


async def buttons_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        user = update.effective_user
        if not user:
            return
        save_post_mode(user.id, "buttons")
        await update.effective_message.reply_text(
            "✅ Buttons post mode saved. Movie name + short link + stream link ಕಳುಹಿಸಿದರೆ Download/Watch buttons automatic ಆಗಿ ಬರುತ್ತವೆ. ಈ setting restart ಆದರೂ ಉಳಿಯುತ್ತದೆ. Telegram button colours ಅನ್ನು ಪ್ರತ್ಯೇಕವಾಗಿ ಸೆಟ್ ಮಾಡಲು ಬಿಡುವುದಿಲ್ಲ.",
            parse_mode=None,
        )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"SKR Photo Caption Bot is running")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Health check server listening on port %s", port)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    init_footer_db()
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setfooter", setfooter))
    app.add_handler(CommandHandler("footer", showfooter))
    app.add_handler(CommandHandler("resetfooter", resetfooter))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("normal", normal_mode))
    app.add_handler(CommandHandler("buttons", buttons_mode))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
