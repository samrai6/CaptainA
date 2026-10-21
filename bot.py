import logging
import os
import re

from telegram import Update
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


def make_caption(movie_name: str, link: str) -> str:
    # Literal ** characters are intentional; Telegram parse mode is disabled.
    title = " ".join(f"**{word}**" for word in movie_name.split())
    return (
        f"{title}\n"
        "**━━━━━━━━━━━━━━━━━━━━**\n"
        "**𝗙𝗜𝗟𝗘𝗦** **𝗟𝗶𝗻𝗸** **👇🏻👇🏻👇🏻👇🏻**\n"
        f"**{link}**\n"
        f"**{link}**\n"
        f"**{link}**\n\n"
        "**𝗛𝗢𝗪** **𝗧𝗢** **𝗢𝗣𝗘𝗡** **𝗟𝗶𝗡𝗞🔗👇👇**\n"
        "**https://t.me/Hwotoopenlink**\n"
        "**══════════════════════**\n"
        "**🔰𝐉𝐨𝐢𝐧** **:-** **🌟🌟**\n"
        "**@SKR_updates**\n"
        "**@SKR_updates**"
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
    if not movie_name or not link:
        await message.reply_text(
            "❌ Caption format sari illa. First line movie name, next line file link haaki.",
            parse_mode=None,
        )
        return

    output_caption = make_caption(movie_name, link)
    await message.reply_photo(
        photo=message.photo[-1].file_id,
        caption=output_caption,
        parse_mode=None,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    movie_name, link = parse_caption(message.text)
    if not movie_name or not link:
        return

    await message.reply_text(make_caption(movie_name, link), parse_mode=None)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
