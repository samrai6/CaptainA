import html
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from telegram import ReplyParameters, Update
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
    "Second line: File link\n\n"
    "Example:\n"
    "ಕೂಲಿ (2025) ಕನ್ನಡ ಡಬ್ (HD)\n"
    "https://example.com/movie\n\n"
    "Send this along with a photo."
)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
def literal_bold(text: str) -> str:
    """Render **wrapped content** as bold HTML; keep URLs clickable."""
    safe_text = html.escape(text)
    if URL_RE.fullmatch(text):
        return f'<b><a href="{safe_text}">{safe_text}</a></b>'
    return f"<b>{safe_text}</b>"


def make_caption(movie_name: str, link: str) -> str:
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
        f"{literal_bold('🔰𝐉𝐨𝐢𝐧')} {literal_bold(':-')} {literal_bold('🌟🌟')}\n"
        f"{literal_bold('@SKR_updates')}\n"
        f"{literal_bold('@SKR_updates')}"
    )


def parse_caption(text: str):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None, None
    movie_name = lines[0]
    link = None
    for line in lines[1:]:
        match = URL_RE.search(line)
        if match:
            link = match.group(0).rstrip(".,);]")
            break
    if not movie_name or not link:
        return None, None
    return movie_name, link


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

    movie_name, link = parse_caption(message.caption or "")
    reply_parameters = ReplyParameters(message_id=message.message_id)
    if not movie_name or not link:
        await message.reply_text(
            "❌ Caption format sari illa. First line movie name, next line file link haaki.",
            parse_mode=None,
            reply_parameters=reply_parameters,
        )
        return

    await message.reply_photo(
        photo=message.photo[-1].file_id,
        caption=make_caption(movie_name, link),
        parse_mode=ParseMode.HTML,
        reply_parameters=reply_parameters,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    movie_name, link = parse_caption(message.text)
    if not movie_name or not link:
        return

    await message.reply_text(
        make_caption(movie_name, link),
        parse_mode=ParseMode.HTML,
        reply_parameters=ReplyParameters(message_id=message.message_id),
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
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
