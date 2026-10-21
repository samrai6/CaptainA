import html
import logging
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, ContextTypes, filters

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
DEFAULT_FOOTER = "🔰𝐉𝐨𝐢𝐧 :- 🌟🌟\n@SKR_updates\n@SKR_updates"
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
                "CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, post_mode TEXT NOT NULL DEFAULT 'normal', download_text TEXT NOT NULL DEFAULT '⬇️ Download', watch_text TEXT NOT NULL DEFAULT '▶️ Watch Online', updates_text TEXT NOT NULL DEFAULT '📢 Join Updates Channel', updates_url TEXT NOT NULL DEFAULT 'https://t.me/SKR_updates', dump_channel_id TEXT NOT NULL DEFAULT '')"
            )
            existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(user_settings)").fetchall()}
            for column, definition in [
                ("download_text", "TEXT NOT NULL DEFAULT '⬇️ Download'"),
                ("watch_text", "TEXT NOT NULL DEFAULT '▶️ Watch Online'"),
                ("updates_text", "TEXT NOT NULL DEFAULT '📢 Join Updates Channel'"),
                ("updates_url", "TEXT NOT NULL DEFAULT 'https://t.me/SKR_updates'"),
                ("dump_channel_id", "TEXT NOT NULL DEFAULT ''"),
            ]:
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE user_settings ADD COLUMN {column} {definition}")
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


DEFAULT_BUTTON_SETTINGS = {
    "download_text": "⬇️ Download",
    "watch_text": "▶️ Watch Online",
    "updates_text": "📢 Join Updates Channel",
    "updates_url": "https://t.me/SKR_updates",
    "dump_channel_id": "",
}


def get_user_settings(user_id: int) -> dict:
    settings = dict(DEFAULT_BUTTON_SETTINGS)
    settings["post_mode"] = "normal"
    if _mongo_settings is not None:
        row = _mongo_settings.find_one({"user_id": user_id})
        if row:
            settings.update({key: row[key] for key in settings if key in row})
        return settings
    with sqlite3.connect(FOOTER_DB_PATH) as conn:
        row = conn.execute(
            "SELECT post_mode, download_text, watch_text, updates_text, updates_url, dump_channel_id FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row:
        keys = ["post_mode", "download_text", "watch_text", "updates_text", "updates_url", "dump_channel_id"]
        settings.update(dict(zip(keys, row)))
    return settings


def save_setting(user_id: int, key: str, value: str):
    allowed = set(DEFAULT_BUTTON_SETTINGS) | {"post_mode"}
    if key not in allowed:
        raise ValueError("Unsupported setting")
    if _mongo_settings is not None:
        _mongo_settings.update_one(
            {"user_id": user_id}, {"$set": {key: value}}, upsert=True
        )
        return
    with sqlite3.connect(FOOTER_DB_PATH) as conn:
        settings = get_user_settings(user_id)
        settings[key] = value
        conn.execute(
            "INSERT INTO user_settings (user_id, post_mode, download_text, watch_text, updates_text, updates_url, dump_channel_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "post_mode=excluded.post_mode, download_text=excluded.download_text, "
            "watch_text=excluded.watch_text, updates_text=excluded.updates_text, updates_url=excluded.updates_url, "
            "dump_channel_id=excluded.dump_channel_id",
            (user_id, settings["post_mode"], settings["download_text"], settings["watch_text"],
             settings["updates_text"], settings["updates_url"], settings["dump_channel_id"]),
        )


def save_post_mode(user_id: int, mode: str):
    if mode not in {"normal", "buttons"}:
        raise ValueError("Unsupported post mode")
    save_setting(user_id, "post_mode", mode)


def get_post_mode(user_id: int) -> str:
    return get_user_settings(user_id)["post_mode"]


def render_footer(footer: str) -> str:
    rendered = []
    for line in footer.splitlines():
        safe = html.escape(line)
        if URL_RE.fullmatch(line.strip()):
            safe = f'<a href="{html.escape(line.strip(), quote=True)}">{safe}</a>'
        rendered.append(f"<b>{safe}</b>")
    return "\n".join(rendered)
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


def make_buttons(download_link: str, watch_link: str, settings: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(settings["download_text"], url=download_link),
            InlineKeyboardButton(settings["watch_text"], url=watch_link),
        ],
        [
            InlineKeyboardButton(settings["updates_text"], url=settings["updates_url"]),
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
            "Nimma current footer:\n\n" + get_footer(user.id),
            parse_mode=None,
        )


async def resetfooter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if message and user:
        reset_footer(user.id)
        await message.reply_text("✅ Default footer restore aagide.", parse_mode=None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    settings = get_user_settings(update.effective_user.id)
    mode_label = "Normal Mode" if settings["post_mode"] == "normal" else "Button Mode"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Normal Mode", callback_data="mode:normal"), InlineKeyboardButton("🔘 Button Mode", callback_data="mode:buttons")],
        [InlineKeyboardButton("🗃 Set Dump Channel ID", callback_data="settings:dump")],
        [InlineKeyboardButton("🔧 Button Settings", callback_data="settings:buttons"), InlineKeyboardButton("✍️ Footer Settings", callback_data="settings:footer")],
    ])
    dump_id = settings.get("dump_channel_id") or "Not set"
    await update.message.reply_text(
        f"⚜️ SKR Photo Caption Bot\n\nCurrent Mode: {mode_label}\nDump Channel ID: {dump_id}\n\nChoose an option below.",
        reply_markup=keyboard,
        parse_mode=None,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(HELP_TEXT, parse_mode=None)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    if not message or not message.photo or not user:
        return
    movie_name, download_link, watch_link = parse_caption(message.caption or "")
    if not movie_name or not download_link:
        await message.reply_text("❌ First line movie name, next line shortlink. Optional second link is used for Watch.", parse_mode=None)
        return
    settings = get_user_settings(user.id)
    if settings["post_mode"] == "buttons":
        caption = make_buttons_caption(movie_name, download_link, watch_link, get_footer(user.id))
        keyboard = make_buttons(download_link, watch_link, settings)
    else:
        caption = make_caption(movie_name, download_link, get_footer(user.id))
        keyboard = None
    context.user_data["pending_dump_post"] = {"type": "photo", "file_id": message.photo[-1].file_id, "caption": caption, "keyboard": keyboard}
    confirm_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ YES — Dump", callback_data="dump:yes"), InlineKeyboardButton("❌ NO — Cancel", callback_data="dump:no")]])
    await message.reply_text("📤 ಈ ಪೋಸ್ಟ್ ಅನ್ನು Dump Channel‌ಗೆ ಕಳುಹಿಸಬೇಕೇ?\n\nYES = ಕಳುಹಿಸು\nNO = Cancel", reply_markup=confirm_keyboard, parse_mode=None)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    if not message or not message.text or not user:
        return
    if context.user_data.get("awaiting_dump_channel"):
        channel_id = message.text.strip()
        if not re.fullmatch(r"-?\d+", channel_id):
            await message.reply_text("❌ Channel ID numeric aagirabeku. Matte try maadi.", parse_mode=None)
            return
        save_setting(user.id, "dump_channel_id", channel_id)
        context.user_data.pop("awaiting_dump_channel", None)
        await message.reply_text("✅ Dump Channel ID save aagide. Bot channel admin aagirabeku.", parse_mode=None)
        return
    movie_name, download_link, watch_link = parse_caption(message.text)
    if not movie_name or not download_link:
        return
    settings = get_user_settings(user.id)
    if settings["post_mode"] == "buttons":
        output = make_buttons_caption(movie_name, download_link, watch_link, get_footer(user.id))
        keyboard = make_buttons(download_link, watch_link, settings)
    else:
        output = make_caption(movie_name, download_link, get_footer(user.id))
        keyboard = None
    context.user_data["pending_dump_post"] = {"type": "text", "caption": output, "keyboard": keyboard}
    confirm_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ YES — Dump", callback_data="dump:yes"), InlineKeyboardButton("❌ NO — Cancel", callback_data="dump:no")]])
    await message.reply_text("📤 ಈ ಪೋಸ್ಟ್ ಅನ್ನು Dump Channel‌ಗೆ ಕಳುಹಿಸಬೇಕೇ?\n\nYES = ಕಳುಹಿಸು\nNO = Cancel", reply_markup=confirm_keyboard, parse_mode=None)


async def button_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, user = update.callback_query, update.effective_user
    if not query or not user:
        return
    await query.answer()
    data = query.data or ""
    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        if mode in {"normal", "buttons"}:
            save_post_mode(user.id, mode)
            await query.edit_message_text("✅ Mode changed to " + ("Normal Mode" if mode == "normal" else "Button Mode") + ".\n/start ಮೂಲಕ settings ಮತ್ತೆ ತೆರೆಯಬಹುದು.")
        return
    if data == "settings:dump":
        context.user_data["awaiting_dump_channel"] = True
        await query.message.reply_text("Dump Channel ID ಕಳುಹಿಸು. Channel‌ನಲ್ಲಿ bot admin ಆಗಿರಬೇಕು.", parse_mode=None)
        return
    if data == "settings:buttons":
        await query.message.reply_text("Button settings commands:\n/buttonsettings\n/setdownloadtext <name>\n/setwatchtext <name>\n/setupdatesbutton <name> | <link>", parse_mode=None)
        return
    if data == "settings:footer":
        await query.message.reply_text("Footer commands:\n/footer\n/setfooter <footer text>\n/resetfooter", parse_mode=None)
        return
    if data == "dump:no":
        context.user_data.pop("pending_dump_post", None)
        await query.edit_message_text("❌ Dump Cancelled. Channel‌ಗೆ ಕಳುಹಿಸಿಲ್ಲ.")
        # Telegram bots cannot force-open the user's PM screen, but can send a
        # private message so the user can continue from the bot chat.
        mode_label = "Normal Mode" if get_user_settings(user.id)["post_mode"] == "normal" else "Button Mode"
        settings = get_user_settings(user.id)
        dump_id = settings.get("dump_channel_id") or "Not set"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 Normal Mode", callback_data="mode:normal"), InlineKeyboardButton("🔘 Button Mode", callback_data="mode:buttons")],
            [InlineKeyboardButton("🗃 Set Dump Channel ID", callback_data="settings:dump")],
            [InlineKeyboardButton("🔧 Button Settings", callback_data="settings:buttons"), InlineKeyboardButton("✍️ Footer Settings", callback_data="settings:footer")],
        ])
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=f"❌ Dump Cancelled. Post ಕಳುಹಿಸಿಲ್ಲ.\n\n⚜️ SKR Photo Caption Bot\nCurrent Mode: {mode_label}\nDump Channel ID: {dump_id}\n\nಇಲ್ಲಿಂದ ಮುಂದುವರಿಸಬಹುದು.",
                reply_markup=keyboard,
                parse_mode=None,
            )
        except Exception:
            logging.warning("Could not send cancellation message to PM for user %s; user may not have started the bot privately.", user.id)
        return
    if data == "dump:yes":
        pending = context.user_data.get("pending_dump_post")
        channel_id = get_user_settings(user.id).get("dump_channel_id", "").strip()
        if not pending:
            await query.edit_message_text("⚠️ Pending post ಇಲ್ಲ. ಮತ್ತೆ post ಕಳುಹಿಸು.")
            return
        if not channel_id:
            await query.edit_message_text("⚠️ Dump Channel ID set ಆಗಿಲ್ಲ. /start → Set Dump Channel ID ಆಯ್ಕೆಮಾಡು.")
            return
        try:
            if pending["type"] == "photo":
                await context.bot.send_photo(chat_id=channel_id, photo=pending["file_id"], caption=pending["caption"], parse_mode=ParseMode.HTML, reply_markup=pending.get("keyboard"))
            else:
                await context.bot.send_message(chat_id=channel_id, text=pending["caption"], parse_mode=ParseMode.HTML, reply_markup=pending.get("keyboard"))
        except Exception:
            logging.exception("Failed to dump post to configured channel")
            await query.edit_message_text("❌ Dump ಆಗಲಿಲ್ಲ. Channel ID ಸರಿಯಿದೆಯೇ ಮತ್ತು bot channel admin ಆಗಿದೆಯೇ ಪರಿಶೀಲಿಸು.")
            context.user_data.pop("pending_dump_post", None)
            return
        context.user_data.pop("pending_dump_post", None)
        await query.edit_message_text("✅ Post Dump Channel‌ಗೆ ಕಳುಹಿಸಲಾಗಿದೆ.")


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


async def buttonsettings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    s = get_user_settings(user.id)
    await message.reply_text(
        "🔧 Your button settings\\n\\n"
        f"Download button: {s['download_text']}\\n"
        f"Watch button: {s['watch_text']}\\n"
        f"Updates button: {s['updates_text']}\\n"
        f"Updates URL: {s['updates_url']}\\n\\n"
        "Update them separately with these commands:\\n"
        "/setdownloadtext New Download Label\\n"
        "/setwatchtext New Watch Label\\n"
        "/setupdatesbutton Button Label | https://t.me/yourchannel\\n\\n"
        "Download and Watch URLs come from each movie message (short link first, stream link second).",
        parse_mode=None,
    )


async def setdownloadtext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    value = " ".join(context.args).strip()
    if not message or not user:
        return
    if not value or len(value) > 60:
        await message.reply_text("Format: /setdownloadtext ⬇️ Download", parse_mode=None)
        return
    save_setting(user.id, "download_text", value)
    await message.reply_text("✅ Download button label saved.", parse_mode=None)


async def setwatchtext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    value = " ".join(context.args).strip()
    if not message or not user:
        return
    if not value or len(value) > 60:
        await message.reply_text("Format: /setwatchtext ▶️ Watch Online", parse_mode=None)
        return
    save_setting(user.id, "watch_text", value)
    await message.reply_text("✅ Watch button label saved.", parse_mode=None)


async def setupdatesbutton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    raw = " ".join(context.args).strip()
    if not message or not user:
        return
    if "|" not in raw:
        await message.reply_text("Format: /setupdatesbutton 📢 Join Updates Channel | https://t.me/SKR_updates", parse_mode=None)
        return
    label, url = (part.strip() for part in raw.split("|", 1))
    if not label or len(label) > 60 or not URL_RE.fullmatch(url):
        await message.reply_text("Button label and valid http(s) URL beku.", parse_mode=None)
        return
    save_setting(user.id, "updates_text", label)
    save_setting(user.id, "updates_url", url)
    await message.reply_text("✅ Updates button label and link saved separately from movie links.", parse_mode=None)


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
    app.add_handler(CallbackQueryHandler(button_callbacks))
    app.add_handler(CommandHandler("setfooter", setfooter))
    app.add_handler(CommandHandler("footer", showfooter))
    app.add_handler(CommandHandler("resetfooter", resetfooter))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("normal", normal_mode))
    app.add_handler(CommandHandler("buttons", buttons_mode))
    app.add_handler(CommandHandler("buttonsettings", buttonsettings))
    app.add_handler(CommandHandler("setdownloadtext", setdownloadtext))
    app.add_handler(CommandHandler("setwatchtext", setwatchtext))
    app.add_handler(CommandHandler("setupdatesbutton", setupdatesbutton))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
