import asyncio
import html
import io
import json
import logging
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, ContextTypes, filters
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
HELP_TEXT = (
    "📸 Photo + caption format:\n\n"
    "Text-only format (poster auto-search):\n"
    "First line: Movie name\n"
    "Second line: Shortlink\n"
    "Third line: Stream link\n\n"
    "Example:\n"
    "Devi 2 (2019)\n"
    "https://example.com/shortlink\n"
    "https://example.com/stream\n\n"
    "Use /normal for the regular caption or /buttons for inline buttons.\n"
    "Send this along with a photo or as text."
)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
DEFAULT_FOOTER = "✦ **JOIN : @SKR_updates**"
FOOTER_DB_PATH = os.getenv("FOOTER_DB_PATH", "footer.db")
MONGO_URI = (
    os.getenv("MONGODB_URI")
    or os.getenv("MONGO_URI")
    or os.getenv("MONGO_URL")
    or os.getenv("MONGODB_URL")
    or os.getenv("DATABASE_URL")
    or ""
).strip()
_mongo_client = None
_mongo_footers = None
_mongo_settings = None
_mongo_dumps = None
_mongo_poster_settings = None


def init_footer_db():
    global _mongo_client, _mongo_footers, _mongo_settings, _mongo_dumps, _mongo_poster_settings
    if MONGO_URI:
        from pymongo import MongoClient
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
        _mongo_client.admin.command("ping")
        db_name = os.getenv("MONGODB_DATABASE", "skr_photo_caption_bot")
        collection_name = os.getenv("MONGODB_FOOTER_COLLECTION", "user_footers")
        _mongo_footers = _mongo_client[db_name][collection_name]
        _mongo_settings = _mongo_client[db_name][os.getenv("MONGODB_SETTINGS_COLLECTION", "user_settings")]
        _mongo_dumps = _mongo_client[db_name][os.getenv("MONGODB_DUMPS_COLLECTION", "user_dumps")]
        _mongo_poster_settings = _mongo_client[db_name][os.getenv("MONGODB_POSTER_SETTINGS_COLLECTION", "poster_settings")]
        _mongo_footers.create_index("user_id", unique=True)
        _mongo_settings.create_index("user_id", unique=True)
        _mongo_dumps.create_index([("user_id", 1), ("channel_id", 1)], unique=True)
        _mongo_poster_settings.create_index("user_id", unique=True)
        logging.info("Footer storage connected to MongoDB database %s", db_name)
    else:
        with sqlite3.connect(FOOTER_DB_PATH) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_footers (user_id INTEGER PRIMARY KEY, footer TEXT NOT NULL, button_footer TEXT NOT NULL DEFAULT '🔰𝐉𝐨𝐢𝐧 :- 🌟🌟\\n@SKR_updates')"
            )
            existing_footer_columns = {row[1] for row in conn.execute("PRAGMA table_info(user_footers)").fetchall()}
            if "button_footer" not in existing_footer_columns:
                conn.execute("ALTER TABLE user_footers ADD COLUMN button_footer TEXT NOT NULL DEFAULT '🔰𝐉𝐨𝐢𝐧 :- 🌟🌟\\n@SKR_updates'")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, post_mode TEXT NOT NULL DEFAULT 'normal', download_text TEXT NOT NULL DEFAULT '📥 DOWNLOAD', watch_text TEXT NOT NULL DEFAULT '▶️ WATCH ONLINE', updates_text TEXT NOT NULL DEFAULT '📢 UPDATES CHANNEL', updates_url TEXT NOT NULL DEFAULT 'https://t.me/SKR_updates', dump_channel_id TEXT NOT NULL DEFAULT '')"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS user_dumps (user_id INTEGER NOT NULL, channel_id TEXT NOT NULL, UNIQUE(user_id, channel_id))")
            conn.execute("CREATE TABLE IF NOT EXISTS shortxlinks_tokens (user_id INTEGER PRIMARY KEY, api_token TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS poster_settings (user_id INTEGER PRIMARY KEY, watermark_text TEXT NOT NULL DEFAULT '@SKR_updates', watermark_size INTEGER NOT NULL DEFAULT 28, watermark_position TEXT NOT NULL DEFAULT 'bottom-right', watermark_enabled INTEGER NOT NULL DEFAULT 1, ai_edit_enabled INTEGER NOT NULL DEFAULT 0)")
            existing_poster_columns = {row[1] for row in conn.execute("PRAGMA table_info(poster_settings)").fetchall()}
            if "watermark_enabled" not in existing_poster_columns:
                conn.execute("ALTER TABLE poster_settings ADD COLUMN watermark_enabled INTEGER NOT NULL DEFAULT 1")
            if "ai_edit_enabled" not in existing_poster_columns:
                conn.execute("ALTER TABLE poster_settings ADD COLUMN ai_edit_enabled INTEGER NOT NULL DEFAULT 0")
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


DEFAULT_POSTER_SETTINGS = {"watermark_text": "@SKR_updates", "watermark_size": 28, "watermark_position": "bottom-right", "watermark_enabled": 1, "ai_edit_enabled": 0}


def get_poster_settings(user_id: int) -> dict:
    settings = dict(DEFAULT_POSTER_SETTINGS)
    if _mongo_poster_settings is not None:
        row = _mongo_poster_settings.find_one({"user_id": user_id})
        if row:
            settings.update({key: row[key] for key in settings if key in row})
        return settings
    with sqlite3.connect(FOOTER_DB_PATH) as conn:
        row = conn.execute("SELECT watermark_text, watermark_size, watermark_position, watermark_enabled, ai_edit_enabled FROM poster_settings WHERE user_id = ?", (user_id,)).fetchone()
    if row:
        settings.update(dict(zip(["watermark_text", "watermark_size", "watermark_position", "watermark_enabled", "ai_edit_enabled"], row)))
        # Migrate the old built-in watermark to the selected cinematic style.
        if settings["watermark_text"] == "@SKR_updates":
            settings["watermark_text"] = "★ 𝑺𝑲𝑹 𝑼𝑷𝑫𝑨𝑻𝑬𝑺 ★"
            with sqlite3.connect(FOOTER_DB_PATH) as conn:
                conn.execute("UPDATE poster_settings SET watermark_text = ? WHERE user_id = ?", (settings["watermark_text"], user_id))
    return settings


def save_poster_setting(user_id: int, key: str, value):
    if key not in DEFAULT_POSTER_SETTINGS:
        raise ValueError("Unsupported poster setting")
    if _mongo_poster_settings is not None:
        _mongo_poster_settings.update_one({"user_id": user_id}, {"$set": {key: value}}, upsert=True)
        return
    settings = get_poster_settings(user_id)
    settings[key] = value
    with sqlite3.connect(FOOTER_DB_PATH) as conn:
        conn.execute("INSERT INTO poster_settings (user_id, watermark_text, watermark_size, watermark_position, watermark_enabled, ai_edit_enabled) VALUES (?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT(user_id) DO UPDATE SET watermark_text=excluded.watermark_text, watermark_size=excluded.watermark_size, watermark_position=excluded.watermark_position, watermark_enabled=excluded.watermark_enabled, ai_edit_enabled=excluded.ai_edit_enabled",
                     (user_id, settings["watermark_text"], settings["watermark_size"], settings["watermark_position"], settings["watermark_enabled"], settings["ai_edit_enabled"]))


def search_google_image_posters(movie_name: str) -> list:
    """Extract direct image URLs from Google Images HTML without an API key."""
    query = clean_imdb_movie_title(transliterate_kannada_to_english(movie_name)).strip()
    if not query:
        return []
    try:
        params = urllib.parse.urlencode({"tbm": "isch", "q": query + " movie poster", "safe": "active"})
        request = urllib.request.Request(
            "https://www.google.com/search?" + params,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            page = response.read().decode("utf-8", errors="ignore")

        # Google embeds source image URLs in JSON fields and imgurl query parameters.
        page = html.unescape(page)
        page = page.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\u003a", ":").replace("\\u002f", "/")
        raw_candidates = []
        for pattern in (
            r'"ou"\s*:\s*"(https?://[^"]+)"',
            r'"image_url"\s*:\s*"(https?://[^"]+)"',
            r'imgurl=(https?%3A%2F%2F[^&"]+)',
            r'https?://[^"\\<>\s]+',
        ):
            raw_candidates.extend(re.findall(pattern, page, flags=re.IGNORECASE))

        posters, seen = [], set()
        for candidate in raw_candidates:
            candidate = candidate.replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=")
            candidate = urllib.parse.unquote(candidate).strip().rstrip("),;")
            candidate = candidate.replace("\\u0026", "&")
            if not candidate.startswith(("http://", "https://")):
                continue
            parsed = urllib.parse.urlsplit(candidate)
            host = (parsed.hostname or "").lower()
            if not host or host == "google.com" or host.endswith(".google.com") or host.endswith(".gstatic.com") or host.endswith(".googleusercontent.com"):
                continue
            # Reject page URLs and keep likely direct image resources; extensionless image-CDN URLs are valid too.
            path = parsed.path.lower()
            image_ext = re.search(r"\.(?:jpe?g|png|webp|avif|gif)$", path) is not None
            known_image_host = any(h in host for h in ("image.tmdb.org", "media-amazon.com", "m.media-amazon.com", "imdb.com", "images.unsplash.com"))
            if not image_ext and not known_image_host and not any(key in parsed.query.lower() for key in ("format=jpg", "format=jpeg", "format=png", "fm=jpg", "fm=webp")):
                continue
            candidate = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
            if candidate in seen:
                continue
            seen.add(candidate)
            posters.append({"url": candidate, "label": "Google Images poster", "year": ""})
            if len(posters) >= 12:
                break
        logging.info("Google Images returned %s poster candidates for %r", len(posters), query)
        return posters
    except Exception:
        logging.warning("Google Images poster search failed for %r", movie_name, exc_info=True)
        return []

def ai_edit_poster(raw: bytes, movie_name: str) -> bytes | None:
    """Try free public Hugging Face Spaces image editing without an API key."""
    try:
        import tempfile
        from pathlib import Path
        from gradio_client import Client, handle_file

        # This public Space provides FLUX.1 Kontext editing through Gradio.
        # It uses shared community GPU capacity, so it may be busy or rate-limited.
        with tempfile.TemporaryDirectory(prefix="skr_poster_") as temp_dir:
            input_path = Path(temp_dir) / "poster_input.png"
            with Image.open(io.BytesIO(raw)) as source:
                source.convert("RGB").save(input_path, format="PNG")

            prompt = (
                "Perform a high-quality, precise edit of this supplied movie poster, not a redesign. "
                "SOURCE IMAGE FIDELITY IS THE TOP PRIORITY. Keep the same original actors and every face, "
                "eyes, hair, body, hands, clothing, pose, lighting, colors, background, and framing unchanged. "
                "Do not regenerate, beautify, smooth, sharpen artificially, blur, crop, resize, or replace any "
                "part of the photograph. Preserve natural facial details and the original image texture. "
                "TEXT RULE: the only readable text allowed is this exact movie title, character-for-character: "
                f"'{movie_name}'. Keep that title legible and correctly spelled. Remove taglines, credits, "
                "release dates, logos containing text, and all other lettering only where the background can "
                "be reconstructed naturally. Never paint over a face, body, or important artwork to remove text. "
                "If safe removal is not possible, preserve the original image region instead of damaging it. "
                "Do not add any new text, symbols, logos, or objects. Return one clean, sharp poster with the "
                "same aspect ratio and dimensions as the input."
            )
            client = Client("black-forest-labs/FLUX.1-Kontext-Dev", verbose=False)
            result = client.predict(
                input_image=handle_file(str(input_path)),
                prompt=prompt,
                seed=42,
                randomize_seed=True,
                guidance_scale=3.0,
                steps=20,
                api_name="/infer",
            )
            image_result = result[0] if isinstance(result, (tuple, list)) else result
            result_path = None
            if isinstance(image_result, str):
                result_path = image_result
            elif isinstance(image_result, dict):
                result_path = image_result.get("path") or image_result.get("name")
                if not result_path and image_result.get("url"):
                    image_url = image_result["url"]
                    request = urllib.request.Request(
                        image_url, headers={"User-Agent": "Mozilla/5.0 SKRPhotoCaptionBot/1.0"}
                    )
                    with urllib.request.urlopen(request, timeout=45) as response:
                        edited = response.read(15 * 1024 * 1024 + 1)
                    if edited and len(edited) <= 15 * 1024 * 1024:
                        logging.info("Free public-space AI poster edit succeeded for %r", movie_name)
                        return edited
            if result_path and os.path.isfile(result_path):
                with open(result_path, "rb") as image_file:
                    edited = image_file.read(15 * 1024 * 1024 + 1)
                if edited and len(edited) <= 15 * 1024 * 1024:
                    logging.info("Free public-space AI poster edit succeeded for %r", movie_name)
                    return edited
        logging.warning("Free public-space AI returned no usable image for %r", movie_name)
    except Exception:
        # Keep poster creation working if the public Space is down, busy, or out of quota.
        logging.warning("Free public-space AI poster edit failed; using OCR cleanup", exc_info=True)
    return None



def watermark_uploaded_photo(raw: bytes, settings: dict) -> bytes:
    """Add the configured watermark to a user-uploaded photo without AI editing."""
    with Image.open(io.BytesIO(raw)) as source:
        image = source.convert("RGBA")
    width, height = image.size
    label = str(settings.get("watermark_text", "@SKR_updates"))[:80]
    if settings.get("watermark_enabled", 1) and label:
        draw = ImageDraw.Draw(image, "RGBA")
        font_size = max(12, min(int(settings.get("watermark_size", 28)), max(12, width // 8)))
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", font_size)
        except OSError:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), label, font=font, stroke_width=1)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        margin = max(10, width // 40)
        pos = settings.get("watermark_position", "bottom-right")
        x = margin if "left" in pos else (width - text_w) // 2 if "center" in pos else width - text_w - margin
        y = margin if pos.startswith("top") else (height - text_h) // 2 if pos == "center" else height - text_h - margin
        draw.text((x, y), label, font=font, fill=(255, 255, 255, 245),
                  stroke_width=max(1, font_size // 18), stroke_fill=(0, 0, 0, 170))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def make_watermarked_poster(image_url: str, settings: dict, movie_name: str = "") -> bytes:
    request = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0 SKRPhotoCaptionBot/1.0"})
    with urllib.request.urlopen(request, timeout=7) as response:
        raw = response.read(15 * 1024 * 1024 + 1)
    if len(raw) > 15 * 1024 * 1024:
        raise ValueError("Poster image is too large")
    ai_enabled = bool(movie_name and settings.get("ai_edit_enabled", 0))
    if not ai_enabled:
        # AI OFF means preserve the original poster and add only the watermark.
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
    else:
        edited_raw = ai_edit_poster(raw, movie_name)
        if edited_raw:
            try:
                image = Image.open(io.BytesIO(edited_raw)).convert("RGBA")
            except Exception:
                logging.warning("Could not decode AI output; using original poster", exc_info=True)
                image = Image.open(io.BytesIO(raw)).convert("RGBA")
        else:
            # If AI service fails, keep the original poster untouched.
            image = Image.open(io.BytesIO(raw)).convert("RGBA")
    # Keep the original/generated poster dimensions; do not force a fixed output size.
    width, height = image.size
    draw = ImageDraw.Draw(image, "RGBA")
    font_size = max(12, min(int(settings.get("watermark_size", 28)), max(12, width // 8)))
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()
    label = str(settings.get("watermark_text", "@SKR_updates"))[:80]
    if not settings.get("watermark_enabled", 1) or not label:
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()
    bbox = draw.textbbox((0, 0), label, font=font, stroke_width=1)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    margin = max(10, width // 40)
    pos = settings.get("watermark_position", "bottom-right")
    x = margin if "left" in pos else (width - text_w) // 2 if "center" in pos else width - text_w - margin
    y = margin if pos.startswith("top") else (height - text_h) // 2 if pos == "center" else height - text_h - margin
    draw.text((x, y), label, font=font, fill=(255, 255, 255, 245), stroke_width=max(1, font_size // 18), stroke_fill=(0, 0, 0, 170))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def poster_input(pending: dict):
    data = pending.get("watermarked_poster")
    if data:
        stream = io.BytesIO(data)
        stream.name = "skr_poster.png"
        return stream
    return pending.get("file_id") or pending.get("poster_url")


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


def get_button_footer(user_id: int) -> str:
    # Backward-compatible alias; both modes share the same footer.
    return get_footer(user_id)


def save_button_footer(user_id: int, footer: str):
    # Backward-compatible alias; both modes share the same footer.
    save_footer(user_id, footer)


def reset_button_footer(user_id: int):
    # Backward-compatible alias; both modes share the same footer.
    reset_footer(user_id)
DEFAULT_BUTTON_SETTINGS = {
    "download_text": "📥 DOWNLOAD",
    "watch_text": "▶️ WATCH ONLINE",
    "updates_text": "📢 UPDATES CHANNEL",
    "updates_url": "https://t.me/SKR_updates",
    "dump_channel_id": "",
}


def get_dump_channels(user_id: int) -> list:
    """Return this user's dump channels, including a legacy single dump if present."""
    if _mongo_dumps is not None:
        rows = list(_mongo_dumps.find({"user_id": user_id}, {"channel_id": 1, "_id": 0}))
        channels = [str(row["channel_id"]) for row in rows if row.get("channel_id")]
    else:
        with sqlite3.connect(FOOTER_DB_PATH) as conn:
            rows = conn.execute("SELECT channel_id FROM user_dumps WHERE user_id = ? ORDER BY rowid", (user_id,)).fetchall()
        channels = [str(row[0]) for row in rows]
    legacy = get_user_settings(user_id).get("dump_channel_id", "").strip()
    if legacy and legacy not in channels:
        channels.insert(0, legacy)
    return list(dict.fromkeys(channels))


def add_dump_channel(user_id: int, channel_id: str):
    channel_id = str(channel_id).strip()
    if _mongo_dumps is not None:
        _mongo_dumps.update_one({"user_id": user_id, "channel_id": channel_id}, {"$setOnInsert": {"user_id": user_id, "channel_id": channel_id}}, upsert=True)
        return
    with sqlite3.connect(FOOTER_DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO user_dumps (user_id, channel_id) VALUES (?, ?)", (user_id, channel_id))


def remove_dump_channel(user_id: int, channel_id: str):
    channel_id = str(channel_id).strip()
    if _mongo_dumps is not None:
        _mongo_dumps.delete_one({"user_id": user_id, "channel_id": channel_id})
    else:
        with sqlite3.connect(FOOTER_DB_PATH) as conn:
            conn.execute("DELETE FROM user_dumps WHERE user_id = ? AND channel_id = ?", (user_id, channel_id))
    settings = get_user_settings(user_id)
    if settings.get("dump_channel_id", "").strip() == channel_id:
        save_setting(user_id, "dump_channel_id", "")


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
    if mode not in {"normal", "buttons", "multi", "postlink", "postlink_normal", "postlink_button"}:
        raise ValueError("Unsupported post mode")
    save_setting(user_id, "post_mode", mode)


def get_post_mode(user_id: int) -> str:
    return get_user_settings(user_id)["post_mode"]


def render_markdown_bold(text: str) -> str:
    """Render **bold** markers as Telegram HTML while keeping all other text normal."""
    rendered = []
    for part in re.split(r"(\*\*.*?\*\*)", str(text), flags=re.DOTALL):
        if not part:
            continue
        is_bold = len(part) >= 4 and part.startswith("**") and part.endswith("**")
        value = part[2:-2] if is_bold else part
        safe = html.escape(value)
        if URL_RE.fullmatch(value.strip()):
            safe = f'<a href="{html.escape(value.strip(), quote=True)}">{safe}</a>'
        rendered.append(f"<b>{safe}</b>" if is_bold else safe)
    return "".join(rendered)


def render_footer(footer: str) -> str:
    """Render configured footer lines; only text wrapped in ** is bold."""
    footer = (footer or "").replace("\\n", "\n")
    # Migrate known legacy built-in footers to the current normal-font branding.
    compact = footer.strip()
    legacy_lines = [line.strip() for line in compact.splitlines() if line.strip()]
    is_legacy_footer = (
        compact == "✦ 𝐉𝐎𝐈𝐍 : @SKR_updates"
        or (
            legacy_lines
            and legacy_lines[0] == "🔰𝐉𝐨𝐢𝐧 :- 🌟🌟"
            and all(line == "@SKR_updates" for line in legacy_lines[1:])
            and len(legacy_lines) <= 3
        )
    )
    if is_legacy_footer:
        footer = "✦ **JOIN : @SKR_updates**"
    rendered_lines = []
    for line in footer.splitlines():
        rendered = render_markdown_bold(line)
        # Make the SKR channel mention clickable while preserving bold formatting.
        rendered = rendered.replace(
            "@SKR_updates",
            '<a href="https://t.me/SKR_updates">@SKR_updates</a>',
        )
        rendered_lines.append(rendered)
    return "\n".join(rendered_lines)


def literal_bold(text: str) -> str:
    """Render content as bold HTML; hide Markdown ** markers and keep URLs clickable."""
    text = text.replace("**", "")
    safe_text = html.escape(text)
    if URL_RE.fullmatch(text):
        return f'<b><a href="{html.escape(text, quote=True)}">{safe_text}</a></b>'
    return f"<b>{safe_text}</b>"


def make_caption(movie_name: str, link: str, footer: str = DEFAULT_FOOTER, watch_link: str = None) -> str:
    """Build Normal Mode caption in the user's requested format; bold all link text."""
    safe_movie_name = html.escape(movie_name)
    invite_url = "https://t.me/+2FzlW3vBPmI4Y2Nl"
    title = f'<b><a href="{invite_url}">{safe_movie_name}</a></b>'
    divider = "<b>━━━━━━━━━━━━━━━━━━━━</b>"
    footer_divider = "<b>══════════════════════</b>"

    raw_link = (link or "").strip()
    safe_link = html.escape(raw_link, quote=True)
    link_text = html.escape(raw_link)
    files_link = f'<b><a href="{safe_link}">{link_text}</a></b>'
    files_label = "<b>𝗙𝗜𝗟𝗘𝗦 𝗟𝗶𝗻𝗸 👇🏻👇🏻👇🏻👇🏻</b>"
    help_label = '<b><a href="https://t.me/Hwotoopenlink">𝗛𝗢𝗪 𝗧𝗢 𝗢𝗣𝗘𝗡 𝗟𝗶𝗡𝗞🔗👇👇</a></b>'
    help_url = '<b><a href="https://t.me/Hwotoopenlink">https://t.me/Hwotoopenlink</a></b>'
    skr_link = '<b><a href="https://t.me/SKR_updates">@SKR_updates</a></b>'

    return (
        f"{title}\n"
        f"{divider}\n\n"
        f"{files_label}\n"
        f"{files_link}\n"
        f"{files_link}\n"
        f"{files_link}\n\n"
        f"{help_label}\n"
        f"{help_url}\n"
        f"{footer_divider}\n"
        f"<b>🔰𝐉𝐨𝐢𝐧 :- 🌟🌟</b>\n"
        f"{skr_link}\n"
        f"{skr_link}"
    )

def clean_imdb_movie_title(movie_name: str) -> str:
    """Keep the movie title and release year, removing language/quality tags."""
    title = movie_name.strip()
    noise_patterns = [
        r"(?i)\b(?:kannada|kan|hindi|tamil|telugu|malayalam|english)\b",
        r"(?i)\b(?:dub(?:bed)?|original|multi(?:ple)?[ -]?audio)\b",
        r"(?i)\b(?:hdrip|web[ -]?dl|webrip|bluray|brrip|dvdrip|camrip|hdtv|prehd)\b",
        r"(?i)\b\d{3,4}\s*[pP]\b",
        r"(?i)\b(?:x264|x265|h264|h265|hevc|10bit|8bit)\b",
        r"(?i)\b(?:4k|uhd|hd|full[ -]?hd)\b",
        r"#\w+",
    ]
    for pattern in noise_patterns:
        title = re.sub(pattern, " ", title)
    title = re.sub(r"[|_]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -–—#")
    return title or movie_name.strip()


def transliterate_kannada_to_english(title: str) -> str:
    """Ask Google Input Tools for a Latin-script transliteration; fall back safely."""
    if not re.search(r"[\u0C80-\u0CFF]", title):
        return title
    try:
        params = urllib.parse.urlencode({
            "text": title,
            "itc": "kn-t-i0-und",
            "num": "1",
            "cp": "0",
            "cs": "1",
            "ie": "utf-8",
            "oe": "utf-8",
        })
        request = urllib.request.Request(
            "https://inputtools.google.com/request?" + params,
            headers={"User-Agent": "Mozilla/5.0 SKRPhotoCaptionBot/1.0"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result and result[0] == "SUCCESS":
            suggestions = result[1]
            if suggestions and suggestions[0] and suggestions[0][0]:
                return suggestions[0][0]
    except Exception:
        logging.warning("Kannada title transliteration failed for %r", title, exc_info=True)
    return title


def find_imdb_poster(movie_name: str):
    """Search IMDb using a cleaned, Latin-script movie title."""
    try:
        search_title = clean_imdb_movie_title(movie_name)
        english_title = transliterate_kannada_to_english(search_title)
        query = urllib.parse.quote(english_title)
        request = urllib.request.Request(
            f"https://v2.sg.media-imdb.com/suggestion/x/{query}.json",
            headers={"User-Agent": "Mozilla/5.0 SKRPhotoCaptionBot/1.0"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        candidates = data.get("d", [])
        normalized = re.sub(r"[^a-z0-9]", "", english_title.lower())
        candidates.sort(key=lambda item: 0 if re.sub(r"[^a-z0-9]", "", item.get("l", "").lower()) == normalized else 1)
        for item in candidates:
            image = item.get("i") or {}
            poster = image.get("imageUrl")
            if poster and item.get("id", "").startswith("tt"):
                # IMDb thumbnails often include small UX/ UY resize parameters.
                # Remove them to request the highest available source image.
                poster = re.sub(r"\._V1_.*?(\.(?:jpe?g|png|webp))$", r"._V1_\1", poster, flags=re.IGNORECASE)
                return poster
    except Exception:
        logging.warning("IMDb poster lookup failed for %r", movie_name, exc_info=True)
    return None


def parse_postlink_caption(text: str):
    text = (text or "").replace("\\n", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first_url_line = next((i for i, line in enumerate(lines) if URL_RE.search(line)), None)
    if first_url_line is None or first_url_line == 0:
        return None, [], "Uploading Join Fast 👇👇"
    # Preserve separate title lines so each can be made independently clickable.
    title = "\n".join(line.replace("**", "").strip() for line in lines[:first_url_line])
    links = []
    for line in lines[first_url_line:]:
        for match in URL_RE.finditer(line):
            link = match.group(0).strip("*").rstrip(".,);]*_")
            if link and link not in links:
                links.append(link)
    if not title or not links:
        return None, [], "Uploading Join Fast 👇👇"
    links = (links + [links[-1]] * 3)[:3]
    return title, links, "Uploading Join Fast 👇👇"


def make_postlink_caption(title: str, links: list, final_line: str, footer: str) -> str:
    # Every separate title line is clickable; JOIN NOW is a separate inline button.
    invite_url = links[0]
    title_lines = [line.strip().replace("**", "") for line in title.splitlines() if line.strip()]
    linked_title = "\n".join(
        f'<b><a href="{html.escape(invite_url, quote=True)}">{html.escape(line)}</a></b>'
        for line in title_lines
    )
    footer_lines = list(dict.fromkeys(
        line.strip() for line in footer.replace("\\n", "\n").splitlines() if line.strip()
    ))
    footer_text = render_footer("\n".join(footer_lines))
    return linked_title + "\n\n" + footer_text


def parse_caption(text: str):
    # Accept a movie title on one or multiple lines; the first URL marks the end of the title.
    text = (text or "").replace("\\n", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first_url_line = next((i for i, line in enumerate(lines) if URL_RE.search(line)), None)
    if first_url_line is None or first_url_line == 0:
        return None, None, None
    movie_name = " ".join(lines[:first_url_line]).strip()
    links = []
    for line in lines[first_url_line:]:
        match = URL_RE.search(line)
        if match:
            link = match.group(0).strip("*").rstrip(".,);]*_")
            if link not in links:
                links.append(link)
    if not movie_name or not links:
        return None, None, None
    download_link = links[0]
    watch_link = links[1] if len(links) > 1 else links[0]
    return movie_name, download_link, watch_link

def parse_multi_caption(text: str):
    """Accept one- or multi-line movie titles, then links ordered 360p, 480p, 720p, 1080p."""
    text = (text or "").replace("\\n", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first_url_line = next((i for i, line in enumerate(lines) if URL_RE.search(line)), None)
    if first_url_line is None or first_url_line == 0:
        return None, []
    movie_name = " ".join(lines[:first_url_line]).strip()
    links = []
    for line in lines[first_url_line:]:
        match = URL_RE.search(line)
        if match:
            link = match.group(0).strip("*").rstrip(".,);]*_")
            if link not in links:
                links.append(link)
    return (movie_name, links[:4]) if movie_name and links else (None, [])

def parse_text_submission(text: str):
    """First line is the IMDb search title; one or more following lines form the caption title before URLs."""
    text = (text or "").replace("\\n", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first_url_line = next((i for i, line in enumerate(lines) if URL_RE.search(line)), None)
    # Keep the existing format: IMDb search title on line one, caption title on line two (optionally continued).
    if first_url_line is None or first_url_line < 2:
        return None, None, []
    imdb_title = lines[0]
    caption_title = " ".join(lines[1:first_url_line]).strip()
    links = []
    for line in lines[first_url_line:]:
        match = URL_RE.search(line)
        if match:
            link = match.group(0).strip("*").rstrip(".,);]*_")
            if link not in links:
                links.append(link)
    return (imdb_title, caption_title, links) if caption_title and links else (None, None, [])

def make_multi_buttons_caption(movie_name: str, footer: str = DEFAULT_FOOTER) -> str:
    title = f'<b><a href="https://t.me/+2FzlW3vBPmI4Y2Nl">🎬 {html.escape(movie_name)}</a></b>'
    help_text = render_markdown_bold("🔗 **HOW TO OPEN LINK 👇**")
    help_url = render_markdown_bold("**https://t.me/Hwotoopenlink**")
    footer = footer.replace("\\n", "\n")
    footer_lines = list(dict.fromkeys(line for line in footer.splitlines() if line.strip()))
    # Keep every footer item plain text; the movie title is the only clickable caption text.
    footer_text = "\n".join(
        re.sub(r"\*\*(.*?)\*\*", lambda match: "<b>" + match.group(1) + "</b>", html.escape(line))
        for line in footer_lines
    )
    return f"{title}\n\n{help_text}\n{help_url}\n\n{footer_text}"


def make_multi_buttons(links: list, settings: dict) -> InlineKeyboardMarkup:
    labels = ["360p", "480p", "720p", "1080p"]
    quality_buttons = [InlineKeyboardButton(label, url=link) for label, link in zip(labels, links)]
    # Single-button rows let Telegram stretch the inline keyboard neatly to the photo/message width.
    rows = [[button] for button in quality_buttons]
    rows.append([InlineKeyboardButton(settings["updates_text"], url=settings["updates_url"])])
    return InlineKeyboardMarkup(rows)


def make_buttons_caption(movie_name: str, download_link: str, watch_link: str, footer: str = DEFAULT_FOOTER) -> str:
    title = f'<b><a href="https://t.me/+2FzlW3vBPmI4Y2Nl">🎬 {html.escape(movie_name)}</a></b>'
    help_text = render_markdown_bold("🔗 **HOW TO OPEN LINK 👇**")
    help_url = render_markdown_bold("**https://t.me/Hwotoopenlink**")
    footer = footer.replace("\\n", "\n")
    footer_lines = list(dict.fromkeys(line for line in footer.splitlines() if line.strip()))
    # Keep every footer item plain text; the movie title is the only clickable caption text.
    footer_text = "\n".join(
        re.sub(r"\*\*(.*?)\*\*", lambda match: "<b>" + match.group(1) + "</b>", html.escape(line))
        for line in footer_lines
    )
    return f"{title}\n\n{help_text}\n{help_url}\n\n{footer_text}"


def make_buttons(download_link: str, watch_link: str, settings: dict) -> InlineKeyboardMarkup:
    # One button per row makes the keyboard visually fit the photo width.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(settings["download_text"], url=download_link)],
        [InlineKeyboardButton(settings["watch_text"], url=watch_link)],
        [InlineKeyboardButton(settings["updates_text"], url=settings["updates_url"])],
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


async def setbuttonfooter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    footer = " ".join(context.args).strip()
    if not footer:
        await message.reply_text("Format: /setbuttonfooter 🔰 Join :- @YourChannel\\n@YourChannel", parse_mode=None)
        return
    if len(footer) > 1000:
        await message.reply_text("Footer 1000 characters olage irabeku.", parse_mode=None)
        return
    save_footer(user.id, footer.replace("\\n", "\n"))
    await message.reply_text("✅ Shared footer saved for Normal Mode and Button Mode.", parse_mode=None)


async def showbuttonfooter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if message and user:
        await message.reply_text("Current shared footer (Normal + Button Mode):\\n\\n" + get_button_footer(user.id), parse_mode=None)


async def resetbuttonfooter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if message and user:
        reset_button_footer(user.id)
        await message.reply_text("✅ Default shared footer restored for both modes.", parse_mode=None)


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
    mode_label = {"normal": "Normal Mode", "buttons": "Button Mode", "multi": "Multi Button Mode", "postlink": "Postlink Mode", "postlink_normal": "Postlink Mode — Normal Post", "postlink_button": "Postlink Mode — Button Post"}.get(settings["post_mode"], "Normal Mode")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Normal Mode", callback_data="mode:normal"), InlineKeyboardButton("🔘 Button Mode", callback_data="mode:buttons")],
        [InlineKeyboardButton("🎬 Multi Button Mode", callback_data="mode:multi")],
        [InlineKeyboardButton("🔗 Postlink Mode", callback_data="mode:postlink")],
        [InlineKeyboardButton("🗃 Set Dump Channel ID", callback_data="settings:dump")],
        [InlineKeyboardButton("🔧 Button Settings", callback_data="settings:buttons")],
        [InlineKeyboardButton("✍️ Edit Shared Footer", callback_data="settings:footer")],
        [InlineKeyboardButton("🖼️ Watermark Settings", callback_data="settings:watermark")],
        [InlineKeyboardButton("🔗 ShortXLinks API Settings", callback_data="settings:shortxlinks")],
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


def clean_media_filename(filename: str):
    """Remove uploader usernames joined to filenames by dash/tilde separators."""
    if not filename:
        return filename
    # Remove channel/uploader handles and popcorn emoji from either end.
    filename = re.sub(r"^\s*@[\w]+(?:\s*🍿)?\s+", "", filename, count=1)
    filename = re.sub(r"^\s*@[\w]+(?:\s*[~\-–—]\s*)+", "", filename, count=1)
    filename = re.sub(r"(?:\s*[~\-–—]\s*)+@[\w]+(?=\.[^.]+$|$)", "", filename, count=1)
    filename = re.sub(r"\s+@[\w]+\s*$", "", filename).strip()
    # Also handle suffixes such as "Movie.mkv @Channel 🍿" or "Movie.mkv 🍿 @Channel".
    for _ in range(3):
        filename = re.sub(r"\s*(?:@[\w]+|🍿)\s*$", "", filename).strip()
    filename = re.sub(r"^\s*[~–—]\s*", "", filename).strip()
    return filename


def extract_mediainfo_filename(raw_text: str):
    """Extract the filename from the first line of a MediaInfo message."""
    if not raw_text:
        return None
    first_line = raw_text.strip().splitlines()[0].strip()
    match = re.match(r"^MediaInfo\s+of\s+(.+?)\s*$", first_line, flags=re.IGNORECASE)
    filename = match.group(1).strip().strip("'\"") if match else first_line.strip().strip("'\"")
    filename = clean_media_filename(filename)
    return filename or None

def build_mediainfo_filename(raw_text: str, base_filename: str = None):
    """Create a consistently ordered release filename using the filename and MediaInfo caption."""
    raw_text = raw_text or ""
    caption_filename = (
        extract_mediainfo_filename(raw_text)
        if re.match(r"^\s*MediaInfo\s+of\s+", raw_text, re.IGNORECASE)
        else None
    )
    filename = clean_media_filename(caption_filename or base_filename or extract_mediainfo_filename(raw_text))
    if not filename:
        return None

    stem, ext = os.path.splitext(filename)
    if not ext:
        ext = ".mkv"
    # Normalize common release-name separators without damaging the movie title.
    work = re.sub(r"[._]+", " ", stem)
    work = re.sub(r"\s*-\s*", " ", work)
    work = re.sub(r"\s+", " ", work).strip()
    combined = f"{stem}\n{raw_text}"
    lower_work = work.lower()
    lower_all = combined.lower()

    languages = [
        ("Kannada", "Kan"), ("Malayalam", "Mal"), ("Tamil", "Tam"),
        ("Telugu", "Tel"), ("Hindi", "Hin"), ("English", "Eng"),
        ("Bengali", "Ben"), ("Marathi", "Mar"), ("Punjabi", "Pun"),
        ("Urdu", "Urd"), ("Japanese", "Jpn"), ("Korean", "Kor"),
        ("Chinese", "Chi"), ("Spanish", "Spa"), ("French", "Fre"),
        ("German", "Ger"), ("Russian", "Rus"), ("Arabic", "Ara"),
    ]
    language_tags = []
    display_languages = []
    audio_section = re.search(r"Audio Tracks([\s\S]*?)(?:Subtitle Tracks|Technical|$)", raw_text, re.IGNORECASE)
    audio_text = audio_section.group(1) if audio_section else raw_text
    for language, tag in languages:
        # Include languages explicitly mentioned in the filename, audio tracks, or prose summary.
        full_name_found = bool(re.search(r"\b" + re.escape(language) + r"\b", work, re.IGNORECASE) or re.search(r"\b" + re.escape(language) + r"\b", audio_text, re.IGNORECASE))
        short_tag_found = bool(re.search(r"\b" + re.escape(tag) + r"\b", work, re.IGNORECASE))
        if full_name_found or short_tag_found:
            language_tags.append(tag)
            display_languages.append(language if full_name_found else tag)

    # Dub marker is kept only when it is explicitly present or inferable from a language-labelled filename.
    dub = "Dub" if re.search(r"\b(?:dub|dubbed)\b", work, re.IGNORECASE) else None

    # Keep HQ as a separate release tag after the title/year.
    hq_tag = "HQ" if re.search(r"\bHQ\b", work, re.IGNORECASE) else None
    quality_match = re.search(r"\b(360|480|540|720|1080|1440|2160)p\b", work, re.IGNORECASE)
    if not quality_match:
        quality_match = re.search(r"\b(360|480|540|720|1080|1440|2160)p\b", raw_text, re.IGNORECASE)
    quality = quality_match.group(1) + "p" if quality_match else None
    if not quality:
        resolution = re.search(r"Resolution:\s*(\d{3,5})\s*[x×]\s*(\d{3,5})", raw_text, re.IGNORECASE)
        if resolution:
            h = int(resolution.group(2))
            quality = "360p" if h <= 400 else "480p" if h <= 600 else "720p" if h <= 800 else "1080p" if h <= 1100 else "1440p" if h <= 1600 else "2160p"

    # Preserve HEVC as its own release label; x265/x264/AV1 describe the video codec.
    hevc = "HEVC" if re.search(r"\bHEVC\b|\bH\.?265\b|\bH265\b", lower_work) else None
    video_codec = None
    video_section = re.search(r"Video Stream([\s\S]*?)(?:Audio Tracks|Subtitle Tracks|Technical|$)", raw_text, re.IGNORECASE)
    video_text = video_section.group(1) if video_section else raw_text
    if re.search(r"\bAV1\b", lower_work) or re.search(r"\bAV1\b", video_text, re.IGNORECASE):
        video_codec = "AV1"
    elif re.search(r"\b(?:x265|H\.?265|H265|HEVC)\b", lower_work) or re.search(r"\b(?:HEVC|H\.?265|H265|x265)\b", video_text, re.IGNORECASE):
        video_codec = "x265"
    elif re.search(r"\b(?:x264|H\.?264|H264|AVC)\b", lower_work) or re.search(r"\b(?:H\.?264|H264|AVC|x264)\b", video_text, re.IGNORECASE):
        video_codec = "x264"

    source_patterns = [
        # Actual release source first, then streaming provider tags.
        (r"\bWEB[- ]?DL\b|\bweb download\b", "WEB-DL"),
        (r"\bWEBRip\b|\bweb rip\b", "WEBRip"),
        (r"\bHDRip\b", "HDRip"),
        (r"\bBluRay\b|\bBlu-ray\b", "BluRay"),
        (r"\bBRRip\b", "BRRip"),
        (r"\bHDTC\b", "HDTC"),
        (r"\bHDCAM\b", "HDCAM"),
        (r"\bYT[- ]?DLP\b", "YT-DLP"),
        (r"\bNF\b|\bNetflix\b", "NF"),
        (r"\bAMZN\b|\bAmazon\b|\bPrime Video\b", "AMZN"),
        (r"\bDSNP\b", "DSNP"),
        (r"\bATVP\b", "ATVP"),
        (r"\bZEE5\b", "ZEE5"),
        (r"\bJIO\b|\bJioCinema\b", "JIO"),
    ]
    source_tags = []
    for pattern, tag in source_patterns:
        if re.search(pattern, combined, re.IGNORECASE) and tag.lower() not in [x.lower() for x in source_tags]:
            source_tags.append(tag)

    # Use only the primary audio codec; don't list every codec mentioned in a
    # MediaInfo summary. Prefer the Audio Tracks section over title/prose text.
    audio_tags = []
    audio_source = audio_text if audio_section else combined
    audio_codec_patterns = [
        (r"\bDTS[- ]HD\b", "DTS-HD"),
        (r"\b(?:E[- ]?AC[- ]?3|EAC3)\b", "EAC3"),
        (r"\b(?:DD\s*\+|DDP)\b", "DDP"),
        (r"\b(?:AC[- ]?3|AC3)\b", "AC3"),
        (r"\bAAC\b", "AAC"),
        (r"\bDTS\b", "DTS"),
        (r"\bFLAC\b", "FLAC"),
        (r"\bOpus\b", "Opus"),
        (r"\bMP3\b", "MP3"),
    ]
    found_audio = []
    for pattern, tag in audio_codec_patterns:
        match = re.search(pattern, audio_source, re.IGNORECASE)
        if match:
            found_audio.append((match.start(), match, tag))
    if found_audio:
        _, codec_match, codec_tag = min(found_audio, key=lambda item: item[0])
        # Look for a channel layout near the primary codec in the same audio section.
        nearby = audio_source[codec_match.start():codec_match.start() + 180]
        channel_match = re.search(r"\b(7\.1|5\.1|2\.0)\b", nearby)
        if not channel_match:
            channel_match = re.search(r"\b(7\.1|5\.1|2\.0)\b", audio_source)
        channel = channel_match.group(1) if channel_match else None
        if channel:
            audio_tags.append(f"{codec_tag}{channel}" if codec_tag == "DDP" else f"{codec_tag} {channel}")
        else:
            audio_tags.append(codec_tag)

    # Normalize audio bitrate and place it after audio codec/channels.
    bitrate_source = audio_source if audio_section else combined
    bitrate_match = re.search(r"\b(\d{2,5})\s*(?:kbps|kb/s|kbit/s|kbits/s)\b", bitrate_source, re.IGNORECASE)
    bitrate_tag = f"{bitrate_match.group(1)}kbps" if bitrate_match else None

    # Add subtitle labels only when explicitly present in the filename or MediaInfo caption.
    subtitle_tags = []
    if re.search(r"\b(?:ESub|Eng\s*Sub|EngSub|English subtitles?)\b", combined, re.IGNORECASE):
        subtitle_tags.append("ESub")
    # Do not infer KanSub or other subtitle tags from language track details.
    subtitle_tags = list(dict.fromkeys(subtitle_tags))

    # Remove technical metadata from the title area, then put year/title first.
    title = work
    remove_patterns = [
        r"\b(?:360|480|540|720|1080|1440|2160)p\b",
        r"\bHQ\b",
        r"\b\d+(?:\.\d+)?\s*(?:GB|GiB|MB|MiB)\b",
        r"\b\d{2,5}\s*(?:kbps|kb/s|kbit/s|kbits/s)\b",
        r"\b(?:HEVC|AV1|x264|x265|H\.?264|H\.?265|H264|H265|AVC)\b",
        r"\b(?:AAC|AC3|AC-3|EAC3|E-AC-3|DDP|DD\s*\+|DTS-HD|DTS|FLAC|Opus|MP3)\b",

        r"\b(?:2\.0|5\.1|7\.1)\b",
        r"\b(?:ESub|KanSub|MalSub|TamSub|TelSub|HinSub|EngSub|BenSub|MarSub|PunSub|UrdSub|JpnSub|KorSub|ChiSub|SpaSub|FreSub|GerSub|RusSub|AraSub)\b",
        r"\bEng\s*Sub\b|\bEnglish subtitles?\b",
        r"\b(?:YT[- ]?DLP|NF|Netflix|AMZN|Amazon|Prime Video|WEB[- ]?DL|WEBRip|HDRip|BluRay|BRRip|HDTC|HDCAM|DSNP|ATVP|ZEE5|JIO|JioCinema)\b",
    ]
    for pattern in remove_patterns:
        title = re.sub(pattern, " ", title, flags=re.IGNORECASE)
    for language, tag in languages:
        title = re.sub(r"\b" + re.escape(language) + r"\b", " ", title, flags=re.IGNORECASE)
        title = re.sub(r"\b" + re.escape(tag) + r"\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(?:dubbed|dub)\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" ._-()")

    # Prefer the readable title/year form when the filename uses dots or underscores.
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", work)
    if year_match and not re.search(r"\(" + year_match.group(1) + r"\)", title):
        title = re.sub(r"\b" + year_match.group(1) + r"\b", " ", title)
        title = re.sub(r"\s+", " ", title).strip(" ._-()")
        title = f"{title} ({year_match.group(1)})".strip()

    # Canonical order: Movie Name (Year), Language, Resolution, Source,
    # Video Codec, Audio Codec/Channels, Subtitles. Do not include HQ.
    ordered = []
    if title:
        ordered.append(title)
    ordered.extend(display_languages)
    if dub:
        ordered.append(dub)
    if quality:
        ordered.append(quality)
    ordered.extend(source_tags)
    if video_codec:
        ordered.append(video_codec)
    ordered.extend(audio_tags)
    if bitrate_tag:
        ordered.append(bitrate_tag)
    ordered.extend(subtitle_tags)

    # Deduplicate while preserving canonical order.
    final_tags = []
    for tag in ordered:
        if tag and tag.lower() not in [x.lower() for x in final_tags]:
            final_tags.append(tag)
    return f"{' '.join(final_tags)}{ext}"

async def send_mediainfo_filename(message, raw_text: str):
    filename = build_mediainfo_filename(raw_text)
    if not filename:
        await message.reply_text(
            "❌ MediaInfo filename ಸಿಗಲಿಲ್ಲ. MediaInfo bot‌ನ message ಅನ್ನು reply ಮಾಡಿ /mediainfo ಕಳುಹಿಸು.",
            parse_mode=None,
        )
        return

    # Send the same four bold filename formats as separate Telegram messages.
    outputs = [
        f"/l1 -n {filename}",
        f"/l2 -n {filename}",
        f"/l3 -n {filename}",
        f"/l -n {filename}",
    ]
    for output in outputs:
        try:
            await message.reply_text(
                f"<b>{html.escape(output)}</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logging.exception("Could not reply with MediaInfo filename format: %s", output)
            await message.reply_text(output, parse_mode=None)


async def mediainfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Format a replied-to MediaInfo message or text supplied with /mediainfo."""
    message = update.effective_message
    if not message:
        return

    source_text = " ".join(context.args).strip() if context.args else ""
    if not source_text and message.reply_to_message:
        replied = message.reply_to_message
        source_text = replied.text or replied.caption or ""

    if not source_text:
        await message.reply_text(
            "MediaInfo messageಗೆ Reply ಮಾಡಿ /mediainfo ಕಳುಹಿಸು. ಅಥವಾ /mediainfo MediaInfo of Movie.Name.mkv ಎಂದು ಕಳುಹಿಸು.",
            parse_mode=None,
        )
        return

    await send_mediainfo_filename(message, source_text)


async def handle_mediainfo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically format MediaInfo headers pasted into chat."""
    message = update.effective_message
    if not message or not message.text:
        return
    if not re.match(r"^\s*MediaInfo\s+of\s+", message.text, flags=re.IGNORECASE):
        return
    await send_mediainfo_filename(message, message.text)


async def handle_media_filename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply with a normalized filename, using the media caption/MediaInfo when available."""
    message = update.effective_message
    if not message:
        return

    media = message.document or message.video or message.audio or message.animation
    if not media:
        return

    filename = getattr(media, "file_name", None) or "media_file"
    filename = clean_media_filename(filename)

    # Telegram normally stores media text in message.caption. Also support
    # a caption/text on a replied-to media message, for users who reply to a file.
    caption = (getattr(message, "caption", None) or "").strip()
    if not caption and message.reply_to_message:
        replied = message.reply_to_message
        caption = (
            getattr(replied, "caption", None)
            or getattr(replied, "text", None)
            or ""
        ).strip()

    if caption:
        caption_header = extract_mediainfo_filename(caption)
        is_mediainfo_header = bool(
            re.match(r"^\s*MediaInfo\s+of\s+", caption, re.IGNORECASE)
        )
        # Use the complete filename from the caption's MediaInfo header when present.
        # Otherwise preserve the uploaded filename and enrich it from caption details.
        base_for_caption = caption_header if is_mediainfo_header and caption_header else filename
        formatted = build_mediainfo_filename(caption, base_filename=base_for_caption)
        if formatted:
            filename = formatted

    # Send all four filename formats as separate bold Telegram messages.
    outputs = [
        f"/l1 -n {filename}",
        f"/l2 -n {filename}",
        f"/l3 -n {filename}",
        f"/l -n {filename}",
    ]
    for output in outputs:
        try:
            await message.reply_text(
                f"<b>{html.escape(output)}</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logging.exception("Could not reply with filename format: %s", output)
            await message.reply_text(output, parse_mode=None)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    if not message or not message.photo or not user:
        return
    settings = get_user_settings(user.id)
    if settings["post_mode"] in {"postlink", "postlink_normal", "postlink_button"}:
        movie_name, postlink_links, final_line = parse_postlink_caption(message.caption or "")
        if not movie_name or not postlink_links:
            await message.reply_text("❌ Postlink Mode: photo ಜೊತೆಗೆ captionನಲ್ಲಿ Movie title ಮತ್ತು Telegram invite link ಕಳುಹಿಸು.", parse_mode=None)
            return
        if settings["post_mode"] == "postlink_button":
            caption = make_postlink_caption(movie_name, postlink_links, final_line, get_footer(user.id))
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 JOIN NOW", url=postlink_links[0])]])
        else:
            footer_text = render_footer(get_footer(user.id))
            caption = literal_bold(movie_name) + "\n\n" + "\n".join(literal_bold(link) for link in postlink_links) + "\n\n" + footer_text
            keyboard = None
    else:
        # If this photo was requested after a text post's poster search failed,
        # reuse that post's saved title and links; the user only needs to send a photo.
        manual = context.user_data.pop("pending_manual_photo", None)
        if manual:
            movie_name, download_link, watch_link = manual
        else:
            movie_name, download_link, watch_link = parse_caption(message.caption or "")
            if not movie_name or not download_link:
                await message.reply_text("❌ Photo ಜೊತೆಗೆ caption ಹಾಕು: ಮೊದಲ ಸಾಲಿನಲ್ಲಿ Movie Name, ಮುಂದಿನ ಸಾಲಿನಲ್ಲಿ Shortlink, ನಂತರ Stream Link.", parse_mode=None)
                return
    if settings["post_mode"] in {"postlink", "postlink_normal", "postlink_button"}:
        pass
    elif settings["post_mode"] == "multi":
        # Prefer the saved text-post data when this is the requested fallback photo.
        manual_multi = context.user_data.pop("pending_manual_multi_photo", None)
        if manual_multi:
            movie_name, quality_links = manual_multi
        else:
            movie_name, quality_links = parse_multi_caption(message.caption or "")
            if not movie_name or not quality_links:
                await message.reply_text("❌ First line Movie Name; next lines links in order 360p, 480p, 720p, 1080p.", parse_mode=None)
                return
        caption = make_multi_buttons_caption(movie_name, get_button_footer(user.id))
        keyboard = make_multi_buttons(quality_links, settings)
    elif settings["post_mode"] == "buttons":
        caption = make_buttons_caption(movie_name, download_link, watch_link, get_button_footer(user.id))
        keyboard = make_buttons(download_link, watch_link, settings)
    else:
        caption = make_caption(movie_name, download_link, get_footer(user.id), watch_link=watch_link)
        keyboard = None
    # Normalize literal newline markers and remove accidental empty parentheses before posting.
    caption = caption.replace("\\n", "\n").replace("()", "")
    # Apply the user's watermark to photos they upload directly, too.
    pending_photo = {"type": "photo", "file_id": message.photo[-1].file_id, "caption": caption, "keyboard": keyboard}
    try:
        telegram_file = await context.bot.get_file(message.photo[-1].file_id)
        uploaded_bytes = bytes(await telegram_file.download_as_bytearray())
        watermarked_bytes = await asyncio.to_thread(
            watermark_uploaded_photo, uploaded_bytes, get_poster_settings(user.id)
        )
        pending_photo.pop("file_id", None)
        pending_photo["watermarked_poster"] = watermarked_bytes
    except Exception:
        logging.warning("Could not watermark user-uploaded photo; keeping original photo", exc_info=True)
    context.user_data["pending_dump_post"] = pending_photo
    confirm_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ YES — Dump", callback_data="dump:yes"), InlineKeyboardButton("❌ NO — Cancel", callback_data="dump:no")]])
    await message.reply_text("📤 ಈ ಪೋಸ್ಟ್ ಅನ್ನು Dump Channel‌ಗೆ ಕಳುಹಿಸಬೇಕೇ?\n\nYES = ಕಳುಹಿಸು\nNO = Cancel", reply_markup=confirm_keyboard, parse_mode=None)



SHORTXLINKS_API_URL = "https://shortxlinks.com/api"


def get_shortxlinks_token(user_id: int) -> str:
    """Prefer the user's token configured in the bot; fall back to Render env."""
    if _mongo_settings is not None:
        row = _mongo_settings.find_one({"user_id": user_id}, {"shortxlinks_api_token": 1})
        if row and row.get("shortxlinks_api_token"):
            return str(row["shortxlinks_api_token"]).strip()
    else:
        with sqlite3.connect(FOOTER_DB_PATH) as conn:
            row = conn.execute(
                "SELECT api_token FROM shortxlinks_tokens WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row and row[0]:
            return str(row[0]).strip()
    return os.getenv("SHORTXLINKS_API_KEY", "").strip()


def save_shortxlinks_token(user_id: int, token: str):
    if _mongo_settings is not None:
        _mongo_settings.update_one(
            {"user_id": user_id},
            {"$set": {"shortxlinks_api_token": token}},
            upsert=True,
        )
    else:
        with sqlite3.connect(FOOTER_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO shortxlinks_tokens (user_id, api_token) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET api_token=excluded.api_token",
                (user_id, token),
            )


def delete_shortxlinks_token(user_id: int):
    if _mongo_settings is not None:
        _mongo_settings.update_one(
            {"user_id": user_id},
            {"$unset": {"shortxlinks_api_token": ""}},
        )
    else:
        with sqlite3.connect(FOOTER_DB_PATH) as conn:
            conn.execute("DELETE FROM shortxlinks_tokens WHERE user_id = ?", (user_id,))




def create_shortxlinks_url(long_url: str, api_token: str) -> str:
    """Create a short URL through the ShortXLinks plain-text API response."""
    query = urllib.parse.urlencode({
        "api": api_token,
        "url": long_url,
        "format": "text",
    })
    request_url = f"{SHORTXLINKS_API_URL}?{query}"
    request = urllib.request.Request(
        request_url,
        headers={"User-Agent": "CaptainA-TelegramBot/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = response.read().decode("utf-8").strip()

    if not result:
        raise ValueError("ShortXLinks API returned an empty response.")
    if result.startswith("{"):
        try:
            payload = json.loads(result)
            if payload.get("status") == "error":
                raise ValueError(str(payload.get("message") or "ShortXLinks API returned an error."))
            result = str(payload.get("shortenedUrl") or "").strip()
        except json.JSONDecodeError:
            pass
    if not result.startswith(("http://", "https://")):
        raise ValueError("ShortXLinks API did not return a valid short URL.")
    return result


async def removeshortlinktoken_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    delete_shortxlinks_token(user.id)
    await message.reply_text(
        "✅ Botನಲ್ಲಿ save ಮಾಡಿದ ShortXLinks token ತೆಗೆದುಹಾಕಲಾಗಿದೆ. "
        "Renderನಲ್ಲಿ SHORTXLINKS_API_KEY ಇದ್ದರೆ ಅದನ್ನು fallback ಆಗಿ ಬಳಸುತ್ತದೆ.",
        parse_mode=None,
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    context.user_data.pop("awaiting_shortxlinks_token", None)
    context.user_data.pop("awaiting_shortlink_url", None)
    await message.reply_text("✅ Cancel ಮಾಡಲಾಗಿದೆ.", parse_mode=None)


async def shortlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    api_token = get_shortxlinks_token(update.effective_user.id) if update.effective_user else ""
    if not api_token:
        await message.reply_text(
            "❌ API token set ಆಗಿಲ್ಲ. /start ಒತ್ತು → 🔗 ShortXLinks API Settings → 🔑 Set / Change API Token ಆಯ್ಕೆ ಮಾಡು.",
            parse_mode=None,
        )
        return
    if context.args:
        long_url = context.args[0].strip()
        if not long_url.startswith(("http://", "https://")):
            await message.reply_text("❌ ಸರಿಯಾದ http:// ಅಥವಾ https:// URL ಕಳುಹಿಸು.", parse_mode=None)
            return
        await _send_shortlink(message, long_url, api_token)
        return
    context.user_data["awaiting_shortlink_url"] = True
    await message.reply_text("🔗 Shorten ಮಾಡಬೇಕಾದ URL ಅನ್ನು ಕಳುಹಿಸಿ", parse_mode=None)


async def _send_shortlink(message, long_url: str, api_token: str):
    try:
        short_url = await asyncio.to_thread(create_shortxlinks_url, long_url, api_token)
    except Exception as exc:
        logging.warning("ShortXLinks request failed: %s", exc)
        await message.reply_text("❌ Shortlink ಮಾಡಲು ಆಗಲಿಲ್ಲ. URL/API key ಪರಿಶೀಲಿಸಿ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸು.", parse_mode=None)
        return
    await message.reply_text(f"🔗 Shortlink:\n{short_url}", parse_mode=None)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    if not message or not message.text or not user:
        return
    if context.user_data.get("awaiting_shortxlinks_token"):
        context.user_data.pop("awaiting_shortxlinks_token", None)
        token = message.text.strip()
        if not token or len(token) > 500 or any(ch.isspace() for ch in token):
            await message.reply_text("❌ Token format ಸರಿಯಿಲ್ಲ. /start → 🔗 ShortXLinks API Settings ಮೂಲಕ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸು.", parse_mode=None)
            return
        save_shortxlinks_token(user.id, token)
        await message.reply_text("✅ ShortXLinks API token bot settingsನಲ್ಲಿ save ಆಯಿತು. ಈಗ /shortlink ಬಳಸಿ.", parse_mode=None)
        return
    if context.user_data.get("awaiting_shortlink_url"):
        context.user_data.pop("awaiting_shortlink_url", None)
        long_url = message.text.strip()
        if not long_url.startswith(("http://", "https://")):
            await message.reply_text("❌ ಸರಿಯಾದ http:// ಅಥವಾ https:// URL ಕಳುಹಿಸು. ಮತ್ತೆ /shortlink ಬಳಸು.", parse_mode=None)
            return
        api_token = get_shortxlinks_token(user.id)
        if not api_token:
            await message.reply_text("❌ API token set ಆಗಿಲ್ಲ. /start ಒತ್ತು → 🔗 ShortXLinks API Settings → 🔑 Set / Change API Token ಆಯ್ಕೆ ಮಾಡು.", parse_mode=None)
            return
        await _send_shortlink(message, long_url, api_token)
        return
    if context.user_data.get("awaiting_watermark_text"):
        value = message.text.strip()
        context.user_data.pop("awaiting_watermark_text", None)
        if not value or len(value) > 80:
            await message.reply_text("❌ Watermark text 1–80 characters ಇರಲಿ. ಮತ್ತೆ Watermark Settings ತೆರೆಯು.", parse_mode=None)
            return
        save_poster_setting(user.id, "watermark_text", value)
        await message.reply_text("✅ Watermark text save ಆಯಿತು: " + value, reply_markup=watermark_settings_keyboard(get_poster_settings(user.id)), parse_mode=None)
        return
    if context.user_data.get("awaiting_watermark_size"):
        context.user_data.pop("awaiting_watermark_size", None)
        try:
            size = int(message.text.strip())
        except ValueError:
            size = 0
        if size < 12 or size > 100:
            await message.reply_text("❌ Font size 12–100 ನಡುವೆ ಇರಬೇಕು.", reply_markup=watermark_settings_keyboard(get_poster_settings(user.id)), parse_mode=None)
            return
        save_poster_setting(user.id, "watermark_size", size)
        await message.reply_text(f"✅ Font size {size} save ಆಯಿತು.", reply_markup=watermark_settings_keyboard(get_poster_settings(user.id)), parse_mode=None)
        return
    if get_post_mode(user.id) == "postlink" and not context.user_data.get("awaiting_dump_channel"):
        await message.reply_text("🔗 Postlink Modeನಲ್ಲಿ IMDb search ಇಲ್ಲ. Photo ಜೊತೆಗೆ movie title ಮತ್ತು Telegram invite link ಅನ್ನು captionನಲ್ಲಿ ಹಾಕು.", parse_mode=None)
        return
    if context.user_data.get("awaiting_dump_channel"):
        channel_id = message.text.strip()
        if not re.fullmatch(r"-?\d+", channel_id):
            await message.reply_text("❌ Channel ID numeric aagirabeku. Matte try maadi.", parse_mode=None)
            return
        add_dump_channel(user.id, channel_id)
        context.user_data.pop("awaiting_dump_channel", None)
        await message.reply_text("✅ Dump Channel add aagide. /dumps ಮೂಲಕ list ನೋಡಬಹುದು. Channel‌ನಲ್ಲಿ bot admin aagirabeku.", parse_mode=None)
        return
    imdb_title, movie_name, links = parse_text_submission(message.text)
    if not imdb_title or not movie_name or not links:
        return
    download_link = links[0]
    watch_link = links[1] if len(links) > 1 else links[0]
    settings = get_user_settings(user.id)
    if settings["post_mode"] in {"postlink", "postlink_normal", "postlink_button"}:
        postlink_links = (links + [links[-1]] * 3)[:3]
        if settings["post_mode"] == "postlink_button":
            output = make_postlink_caption(movie_name, postlink_links, "Uploading Join Fast 👇👇", get_footer(user.id))
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 JOIN NOW", url=postlink_links[0])]])
        else:
            output = literal_bold(movie_name) + "\n\n" + "\n".join(literal_bold(link) for link in postlink_links) + "\n\n" + render_footer(get_footer(user.id))
            keyboard = None
    elif settings["post_mode"] == "multi":
        quality_links = links[:4]
        output = make_multi_buttons_caption(movie_name, get_button_footer(user.id))
        keyboard = make_multi_buttons(quality_links, settings)
    elif settings["post_mode"] == "buttons":
        output = make_buttons_caption(movie_name, download_link, watch_link, get_button_footer(user.id))
        keyboard = make_buttons(download_link, watch_link, settings)
    else:
        output = make_caption(movie_name, download_link, get_footer(user.id), watch_link=watch_link)
        keyboard = None
    # Normalize literal newline markers and remove accidental empty parentheses before posting.
    output = output.replace("\\n", "\n").replace("()", "")
    # Show visible progress so the user knows what the bot is doing.
    watermark_settings = get_poster_settings(user.id)
    ai_mode_enabled = bool(watermark_settings.get("ai_edit_enabled", 0))
    progress_message = await message.reply_text(
        "🔎 Movie poster search aguttide...\nPlease wait.",
        parse_mode=None,
    )
    # AI OFF: search IMDb only and keep its poster original (no AI editing).
    # AI ON: search Google Images and IMDb concurrently for poster options.
    if ai_mode_enabled:
        google_task = asyncio.to_thread(search_google_image_posters, imdb_title)
        imdb_task = asyncio.to_thread(find_imdb_poster, imdb_title)
        google_result, imdb_result = await asyncio.gather(google_task, imdb_task, return_exceptions=True)
        posters = google_result if isinstance(google_result, list) else []
        imdb_poster = imdb_result if isinstance(imdb_result, str) and imdb_result else None
        if isinstance(google_result, Exception):
            logging.warning("Google poster search failed: %s", google_result)
        if isinstance(imdb_result, Exception):
            logging.warning("IMDb poster search failed: %s", imdb_result)
        if imdb_poster and all(item["url"] != imdb_poster for item in posters):
            posters.append({"url": imdb_poster, "label": "IMDb poster", "year": ""})
    else:
        imdb_result = await asyncio.to_thread(find_imdb_poster, imdb_title)
        posters = [{"url": imdb_result, "label": "IMDb poster", "year": ""}] if isinstance(imdb_result, str) and imdb_result else []
    if ai_mode_enabled:
        await progress_message.edit_text(
            "🤖 Free AI poster editing start agide...\n"
            "⚙️ Working... Please wait.",
            parse_mode=None,
        )
    else:
        await progress_message.edit_text(
            "🖼️ Original poster ready maduttide...\nWatermark setting prakara apply agutte.",
            parse_mode=None,
        )
    if posters:
        context.user_data.pop("pending_manual_photo", None)
        context.user_data.pop("pending_manual_multi_photo", None)
        # Automatically use the first poster that can be downloaded and processed.
        # Do not ask the user to choose from Google Images / IMDb options.
        watermark_settings = get_poster_settings(user.id)
        watermarked = None
        for poster in posters[:3]:
            try:
                watermarked = await asyncio.to_thread(
                    make_watermarked_poster, poster["url"], watermark_settings, movie_name
                )
                break
            except Exception:
                logging.warning("Poster candidate failed; trying the next result", exc_info=True)
        if watermarked is not None:
            context.user_data.pop("pending_poster_selection", None)
            context.user_data["pending_dump_post"] = {
                "type": "photo",
                "watermarked_poster": watermarked,
                "caption": output,
                "keyboard": keyboard,
            }
            confirm_keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ YES — Dump", callback_data="dump:yes"),
                InlineKeyboardButton("❌ NO — Cancel", callback_data="dump:no"),
            ]])
            await progress_message.edit_text(
                "✅ Poster processing complete!\n"
                + ("🤖 AI edit try madalagide." if ai_mode_enabled else "🖼️ Original poster use madalagide.")
                + "\n📤 Dump Channel‌ಗೆ ಕಳುಹಿಸಬೇಕೇ?",
                reply_markup=confirm_keyboard,
                parse_mode=None,
            )
            return
        logging.warning("All poster candidates failed to download or process for %s", imdb_title)
    context.user_data["pending_manual_photo"] = (movie_name, download_link, watch_link)
    if settings["post_mode"] == "multi":
        context.user_data["pending_manual_multi_photo"] = (movie_name, links[:4])
    await message.reply_text(
        "🖼️ Google Images / IMDbನಲ್ಲಿ poster ಸಿಗಲಿಲ್ಲ.\n"
        "📸 Photo ಮಾತ್ರ ಕಳುಹಿಸು — Movie Name ಮತ್ತು links ಅನ್ನು ನಿನ್ನ ಹಿಂದಿನ postನಿಂದಲೇ ತೆಗೆದುಕೊಳ್ಳುತ್ತೇನೆ. ಮತ್ತೆ name/link ಬರೆಯಬೇಕಾಗಿಲ್ಲ.",
        parse_mode=None,
    )

async def deliver_pending_post(context: ContextTypes.DEFAULT_TYPE, user_id: int, channel_id: str, pending: dict):
    if pending["type"] == "photo":
        # Always send posters as Telegram photos so they display inline with the caption,
        # rather than as downloadable documents. Keep the processed image at high quality.
        await context.bot.send_photo(
            chat_id=channel_id,
            photo=poster_input(pending),
            caption=pending["caption"],
            parse_mode=ParseMode.HTML,
            reply_markup=pending.get("keyboard"),
        )
    else:
        await context.bot.send_message(chat_id=channel_id, text=pending["caption"], parse_mode=ParseMode.HTML, reply_markup=pending.get("keyboard"))


async def adddump_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    channel_id = " ".join(context.args).strip()
    if not re.fullmatch(r"-?\d+", channel_id):
        await message.reply_text("Format: /adddump -1001234567890\nBot channel admin ಆಗಿರಬೇಕು.", parse_mode=None)
        return
    add_dump_channel(user.id, channel_id)
    await message.reply_text(f"✅ Dump channel add ಆಗಿದೆ: {channel_id}\nಒಟ್ಟು dumps: {len(get_dump_channels(user.id))}", parse_mode=None)


async def dumps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    channels = get_dump_channels(user.id)
    if not channels:
        await message.reply_text("Dump channels ಇನ್ನೂ add ಮಾಡಿಲ್ಲ.\n/adddump -1001234567890", parse_mode=None)
        return
    lines = [f"{i}. {channel_id}" for i, channel_id in enumerate(channels, 1)]
    await message.reply_text("🗃 Your Dump Channels:\n\n" + "\n".join(lines) + "\n\nRemove: /removedump 1", parse_mode=None)


async def removedump_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    if not context.args or not context.args[0].isdigit():
        await message.reply_text("Format: /removedump 1\n/dumps ಮೂಲಕ number ನೋಡಿ.", parse_mode=None)
        return
    channels = get_dump_channels(user.id)
    index = int(context.args[0]) - 1
    if index < 0 or index >= len(channels):
        await message.reply_text("❌ ಆ number‌ನ dump channel ಸಿಗಲಿಲ್ಲ. /dumps ನೋಡಿ.", parse_mode=None)
        return
    channel_id = channels[index]
    remove_dump_channel(user.id, channel_id)
    await message.reply_text(f"✅ Dump channel remove ಆಗಿದೆ: {channel_id}", parse_mode=None)


def watermark_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    enabled = bool(settings.get("watermark_enabled", 1))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Watermark Text", callback_data="wm:edit_text")],
        [InlineKeyboardButton("🔠 Change Font Size", callback_data="wm:edit_size")],
        [InlineKeyboardButton("📍 Choose Position", callback_data="wm:positions")],
        [InlineKeyboardButton("🔕 Disable Watermark" if enabled else "🔛 Enable Watermark", callback_data="wm:toggle")],
        [InlineKeyboardButton("🤖 AI Edit: ON" if settings.get("ai_edit_enabled", 0) else "🤖 AI Edit: OFF", callback_data="wm:ai_toggle")],
        [InlineKeyboardButton("⬅️ Back to Settings", callback_data="settings:back")],
    ])


def watermark_positions_keyboard() -> InlineKeyboardMarkup:
    positions = [("↖️ Top Left", "top-left"), ("↗️ Top Right", "top-right"), ("↙️ Bottom Left", "bottom-left"), ("↘️ Bottom Right", "bottom-right"), ("🎯 Center", "center")]
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data="wm:pos:" + pos)] for label, pos in positions] + [[InlineKeyboardButton("⬅️ Back", callback_data="settings:watermark")]])


async def build_dump_selection_rows(bot, channels: list, selected: list) -> list:
    """Show each dump channel's Telegram title and ID in its selection button."""
    rows = []
    for i, channel_id in enumerate(channels):
        try:
            chat = await bot.get_chat(channel_id)
            channel_name = getattr(chat, "title", None) or "Unknown Channel"
        except Exception:
            logging.warning("Could not resolve dump channel name for %s", channel_id, exc_info=True)
            channel_name = "Name unavailable"
        prefix = "☑️ " if i in selected else "☐ "
        suffix = f" | {channel_id}"
        max_name_length = max(1, 64 - len(prefix) - len(suffix))
        label = f"{prefix}{channel_name[:max_name_length]}{suffix}"
        rows.append([InlineKeyboardButton(label, callback_data=f"pickdump:{i}")])
    return rows


async def button_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, user = update.callback_query, update.effective_user
    if not query or not user:
        return
    await query.answer()
    data = query.data or ""
    if data.startswith("posterpick:"):
        selection = context.user_data.get("pending_poster_selection")
        if not selection:
            await query.edit_message_text("⚠️ Poster selection expired. Movie message ಮತ್ತೆ ಕಳುಹಿಸು.")
            return
        try:
            index = int(data.split(":", 1)[1])
            poster = selection["posters"][index]
        except (ValueError, IndexError, KeyError):
            await query.edit_message_text("⚠️ ಈ poster ಆಯ್ಕೆ ಸಿಗಲಿಲ್ಲ. ಮತ್ತೆ search ಮಾಡು.")
            return
        try:
            watermark_settings = get_poster_settings(user.id)
            watermarked = await asyncio.to_thread(make_watermarked_poster, poster["url"], watermark_settings, selection.get("movie_name", ""))
        except Exception:
            logging.exception("Could not download/watermark selected poster")
            await query.edit_message_text("❌ Poster download/watermark ಆಗಲಿಲ್ಲ. ಬೇರೆ poster ಆಯ್ಕೆ ಮಾಡಿ.")
            return
        context.user_data["pending_dump_post"] = {
            "type": "photo", "watermarked_poster": watermarked,
            "caption": selection["caption"], "keyboard": selection["keyboard"]
        }
        context.user_data.pop("pending_poster_selection", None)
        confirm_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ YES — Dump", callback_data="dump:yes"), InlineKeyboardButton("❌ NO — Cancel", callback_data="dump:no")]])
        watermark_note = ("White watermark: " + watermark_settings["watermark_text"]) if watermark_settings.get("watermark_enabled", 1) else "Watermark OFF — poster ಮೇಲೆ watermark ಹಾಕಿಲ್ಲ."
        await query.edit_message_text("✅ Poster ಆಯ್ಕೆ ಆಯಿತು. " + watermark_note)
        await context.bot.send_message(chat_id=user.id, text="📤 ಈ ಪೋಸ್ಟ್ ಅನ್ನು Dump Channel‌ಗೆ ಕಳುಹಿಸಬೇಕೇ?\n\nYES = ಕಳುಹಿಸು\nNO = Cancel", reply_markup=confirm_keyboard, parse_mode=None)
        return
    if data == "settings:shortxlinks":
        token = get_shortxlinks_token(user.id)
        status = "✅ Token saved" if token else "❌ Token not set"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Set / Change API Token", callback_data="sx:set")],
            [InlineKeyboardButton("🗑 Remove Saved Token", callback_data="sx:remove")],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="settings:back")],
        ])
        await query.edit_message_text(
            "🔗 ShortXLinks API Settings\\n\\n"
            f"Status: {status}\\n\\n"
            "Token bot databaseನಲ್ಲಿ save ಆಗುತ್ತದೆ. Render Environment Variable ಅಗತ್ಯವಿಲ್ಲ.",
            reply_markup=keyboard,
            parse_mode=None,
        )
        return
    if data == "sx:set":
        context.user_data["awaiting_shortxlinks_token"] = True
        await query.edit_message_text(
            "🔐 ShortXLinks API token ಅನ್ನು ಈಗ ಮುಂದಿನ messageನಲ್ಲಿ ಕಳುಹಿಸು.\\n"
            "Token private chatನಲ್ಲಿ ಮಾತ್ರ ಕಳುಹಿಸು. ರದ್ದು ಮಾಡಲು /cancel ಬಳಸು.",
            parse_mode=None,
        )
        return
    if data == "sx:remove":
        delete_shortxlinks_token(user.id)
        await query.edit_message_text(
            "✅ Saved API token ತೆಗೆದುಹಾಕಲಾಗಿದೆ.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="settings:shortxlinks")]]),
            parse_mode=None,
        )
        return
    if data == "settings:watermark":
        s = get_poster_settings(user.id)
        status = "ON 🔛" if s.get("watermark_enabled", 1) else "OFF 🔕"
        await query.edit_message_text(f"🖼️ Poster Settings\n\nWatermark: {status}\nText: {s['watermark_text']}\nFont size: {s['watermark_size']}\nPosition: {s['watermark_position']}\nAI Edit: {'ON' if s.get('ai_edit_enabled', 1) else 'OFF'}\n\nAI Edit OFF ಮಾಡಿದರೆ AI enhancement skip ಆಗುತ್ತದೆ; watermark ಇನ್ನೂ ಅನ್ವಯವಾಗುತ್ತದೆ.", reply_markup=watermark_settings_keyboard(s), parse_mode=None)
        return
    if data == "settings:back":
        await query.edit_message_text("⚙️ Settings menu ಮತ್ತೆ ತೆರೆಯಲು /start ಒತ್ತು.")
        return
    if data == "wm:edit_text":
        context.user_data["awaiting_watermark_text"] = True
        await query.edit_message_text("✏️ ಹೊಸ watermark text ಕಳುಹಿಸು (ಉದಾ: @SKR_updates). 1–80 characters.")
        return
    if data == "wm:edit_size":
        context.user_data["awaiting_watermark_size"] = True
        await query.edit_message_text("🔠 Font size ಕಳುಹಿಸು (12–100). Default: 28")
        return
    if data == "wm:positions":
        await query.edit_message_text("📍 Watermark position ಆಯ್ಕೆ ಮಾಡು:", reply_markup=watermark_positions_keyboard())
        return
    if data == "wm:toggle":
        s = get_poster_settings(user.id)
        save_poster_setting(user.id, "watermark_enabled", 0 if s.get("watermark_enabled", 1) else 1)
        s = get_poster_settings(user.id)
        status = "enabled 🔛" if s.get("watermark_enabled", 1) else "disabled 🔕"
        await query.edit_message_text("✅ Watermark " + status + ".", reply_markup=watermark_settings_keyboard(s), parse_mode=None)
        return
    if data == "wm:ai_toggle":
        s = get_poster_settings(user.id)
        save_poster_setting(user.id, "ai_edit_enabled", 0 if s.get("ai_edit_enabled", 1) else 1)
        s = get_poster_settings(user.id)
        status = "ON 🤖" if s.get("ai_edit_enabled", 1) else "OFF ⏸️"
        await query.edit_message_text(
            f"✅ AI Poster Edit {status}.\n\n" +
            ("AI enhancement ಹೊಸ posters ಮೇಲೆ ನಡೆಯುತ್ತದೆ." if s.get("ai_edit_enabled", 1) else "AI enhancement skip ಆಗುತ್ತದೆ. Posterಗೆ watermark/OCR cleanup ಮುಂದುವರಿಯಬಹುದು."),
            reply_markup=watermark_settings_keyboard(s), parse_mode=None)
        return
    if data.startswith("wm:pos:"):
        pos = data.split(":", 2)[2]
        if pos in {"bottom-right", "bottom-left", "top-right", "top-left", "center"}:
            save_poster_setting(user.id, "watermark_position", pos)
        s = get_poster_settings(user.id)
        await query.edit_message_text("✅ Position save ಆಯಿತು: " + s["watermark_position"], reply_markup=watermark_settings_keyboard(s), parse_mode=None)
        return
    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        if mode in {"normal", "buttons", "multi", "postlink", "postlink_normal", "postlink_button"}:
            save_post_mode(user.id, mode)
            if mode == "postlink":
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 Normal Post", callback_data="mode:postlink_normal")],
                    [InlineKeyboardButton("🔘 Button Post", callback_data="mode:postlink_button")],
                ])
                await query.edit_message_text("🔗 Postlink Mode — choose post type:", reply_markup=keyboard)
                return
            mode_name = {"normal": "Normal Mode", "buttons": "Button Mode", "multi": "Multi Button Mode", "postlink_normal": "Postlink Mode — Normal Post", "postlink_button": "Postlink Mode — Button Post"}[mode]
            await query.edit_message_text("✅ Mode changed to " + mode_name + ".\n/start ಮೂಲಕ settings ಮತ್ತೆ ತೆರೆಯಬಹುದು.")
        return
    if data == "settings:dump":
        context.user_data["awaiting_dump_channel"] = True
        await query.message.reply_text("Nimma Dump Channel ID ಕಳುಹಿಸು. Idu nimma account-ge maatra save aagutte; bere users tamma ID set maadabahudu. Channel‌ನಲ್ಲಿ bot admin ಆಗಿರಬೇಕು.", parse_mode=None)
        return
    if data == "settings:buttons":
        await query.message.reply_text("Button settings commands:\n/buttonsettings\n/setdownloadtext <name>\n/setwatchtext <name>\n/setupdatesbutton <name> | <link>", parse_mode=None)
        return
    if data == "settings:footer":
        await query.message.reply_text("Footer commands:\n/footer\n/setfooter <footer text>\n/resetfooter", parse_mode=None)
        return
    if data == "dump:yes":
        pending = context.user_data.get("pending_dump_post")
        channels = get_dump_channels(user.id)
        if not pending:
            await query.edit_message_text("⚠️ Pending post ಇಲ್ಲ. ಮತ್ತೆ post ಕಳುಹಿಸು.")
            return
        if not channels:
            await query.edit_message_text("⚠️ Dump Channel add ಆಗಿಲ್ಲ. /adddump ಮೂಲಕ channel ID add ಮಾಡು.")
            return
        context.user_data["selected_dump_indices"] = []
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📣 Send All", callback_data="dest:all")],
            [InlineKeyboardButton("☑️ Select Dumps", callback_data="pickdump:select")],
        ])
        await query.edit_message_text("📤 Post ಯಾವ channels‌ಗೆ ಕಳುಹಿಸಬೇಕು?", reply_markup=keyboard)
        return
    if data.startswith("pickdump:"):
        action = data.split(":", 1)[1]
        pending = context.user_data.get("pending_dump_post")
        channels = get_dump_channels(user.id)
        if not pending:
            await query.edit_message_text("⚠️ Pending post ಇಲ್ಲ. ಮತ್ತೆ post ಕಳುಹಿಸು.")
            return
        if not channels:
            await query.edit_message_text("⚠️ Dump Channel add ಆಗಿಲ್ಲ. /adddump ಮೂಲಕ channel ID add ಮಾಡು.")
            return
        if action == "back":
            context.user_data["selected_dump_indices"] = []
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📣 Send All", callback_data="dest:all")],
                [InlineKeyboardButton("☑️ Select Dumps", callback_data="pickdump:select")],
            ])
            await query.edit_message_text("📤 Post ಯಾವ channels‌ಗೆ ಕಳುಹಿಸಬೇಕು?", reply_markup=keyboard)
            return
        if action == "select":
            context.user_data["selected_dump_indices"] = []
        elif action in {"send", "all"}:
            selected = context.user_data.get("selected_dump_indices", [])
            destinations = channels if action == "all" else [
                channel for i, channel in enumerate(channels) if i in selected
            ]
            if not destinations:
                rows = await build_dump_selection_rows(context.bot, channels, selected)
                keyboard = InlineKeyboardMarkup(rows)
                keyboard.inline_keyboard.append([
                    InlineKeyboardButton("✅ Send Selected (0)", callback_data="pickdump:send")
                ])
                keyboard.inline_keyboard.append([
                    InlineKeyboardButton("📣 Send All", callback_data="pickdump:all"),
                    InlineKeyboardButton("↩️ Back", callback_data="pickdump:back"),
                ])
                await query.edit_message_text("ಕನಿಷ್ಠ ಒಂದು Dump ಆಯ್ಕೆ ಮಾಡು; ನಂತರ Send Selected ಒತ್ತು.", reply_markup=keyboard)
                return
            successes, failures = [], []
            for channel_id in destinations:
                try:
                    await deliver_pending_post(context, user.id, channel_id, pending)
                    successes.append(channel_id)
                except Exception:
                    logging.exception("Failed to send to dump channel %s", channel_id)
                    failures.append(channel_id)
            context.user_data.pop("pending_dump_post", None)
            context.user_data.pop("selected_dump_indices", None)
            result = f"✅ ಯಶಸ್ವಿಯಾಗಿ ಕಳುಹಿಸಲಾಗಿದೆ: {len(successes)} channel(s)."
            if failures:
                result += f"\n❌ Failed: {', '.join(failures)}\nBot admin ಆಗಿದೆಯೇ ಪರಿಶೀಲಿಸು."
            await query.edit_message_text(result, parse_mode=None)
            return
        elif action.isdigit():
            index = int(action)
            if index < 0 or index >= len(channels):
                await query.edit_message_text("⚠️ Dump Channel ಸಿಗಲಿಲ್ಲ. /dumps ಮೂಲಕ ಪರಿಶೀಲಿಸು.")
                return
            selected = list(context.user_data.get("selected_dump_indices", []))
            if index in selected:
                selected.remove(index)
            else:
                selected.append(index)
            context.user_data["selected_dump_indices"] = selected
        else:
            return
        selected = context.user_data.get("selected_dump_indices", [])
        rows = await build_dump_selection_rows(context.bot, channels, selected)
        rows.append([InlineKeyboardButton(f"✅ Send Selected ({len(selected)})", callback_data="pickdump:send")])
        rows.append([
            InlineKeyboardButton("📣 Send All", callback_data="pickdump:all"),
            InlineKeyboardButton("↩️ Back", callback_data="pickdump:back"),
        ])
        await query.edit_message_text(
            f"☑️ Dump channels ಆಯ್ಕೆ ಮಾಡು. Selected: {len(selected)}\nSend Selected ಒತ್ತಿದ ನಂತರ ಮಾತ್ರ post ಹೋಗುತ್ತದೆ.",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return
    if data.startswith("dest:"):
        pending = context.user_data.get("pending_dump_post")
        channels = get_dump_channels(user.id)
        if not pending:
            await query.edit_message_text("⚠️ Pending post ಇಲ್ಲ. ಮತ್ತೆ post ಕಳುಹಿಸು.")
            return
        target = data.split(":", 1)[1]
        destinations = channels if target == "all" else ([target] if target in channels else [])
        if not destinations:
            await query.edit_message_text("⚠️ Dump Channel ಸಿಗಲಿಲ್ಲ. /dumps ಮೂಲಕ ಪರಿಶೀಲಿಸು.")
            return
        successes, failures = [], []
        for channel_id in destinations:
            try:
                await deliver_pending_post(context, user.id, channel_id, pending)
                successes.append(channel_id)
            except Exception:
                logging.exception("Failed to send to dump channel %s", channel_id)
                failures.append(channel_id)
        context.user_data.pop("pending_dump_post", None)
        result = f"✅ ಯಶಸ್ವಿಯಾಗಿ ಕಳುಹಿಸಲಾಗಿದೆ: {len(successes)} channel(s)."
        if failures:
            result += f"\n❌ Failed: {', '.join(failures)}\nBot admin ಆಗಿದೆಯೇ ಪರಿಶೀಲಿಸು."
        await query.edit_message_text(result, parse_mode=None)
        return
    if data == "dump:no":
        pending = context.user_data.pop("pending_dump_post", None)
        await query.edit_message_text("❌ Dump Cancelled. Post ಅನ್ನು PMಗೆ ಕಳುಹಿಸುತ್ತಿದ್ದೇನೆ.")
        if not pending:
            await context.bot.send_message(
                chat_id=user.id,
                text="⚠️ Pending post ಸಿಗಲಿಲ್ಲ. ಫೋಟೋ ಮತ್ತು caption ಮತ್ತೆ ಕಳುಹಿಸು.",
                parse_mode=None,
            )
            return
        try:
            if pending["type"] == "photo":
                await context.bot.send_photo(
                    chat_id=user.id,
                    photo=poster_input(pending),
                    caption=pending["caption"],
                    parse_mode=ParseMode.HTML,
                    reply_markup=pending.get("keyboard"),
                )
            else:
                await context.bot.send_message(
                    chat_id=user.id,
                    text=pending["caption"],
                    parse_mode=ParseMode.HTML,
                    reply_markup=pending.get("keyboard"),
                )
        except Exception:
            logging.exception("Failed to send cancelled post to user's PM")
            await context.bot.send_message(
                chat_id=user.id,
                text="❌ Post PMಗೆ ಕಳುಹಿಸಲು ಆಗಲಿಲ್ಲ. Botಗೆ PMನಲ್ಲಿ /start ಮಾಡಿರುವುದನ್ನು ಪರಿಶೀಲಿಸು.",
                parse_mode=None,
            )
        return
    if data == "dump:yes":
        pending = context.user_data.get("pending_dump_post")
        channel_id = get_user_settings(user.id).get("dump_channel_id", "").strip()
        if not pending:
            await query.edit_message_text("⚠️ Pending post ಇಲ್ಲ. ಮತ್ತೆ post ಕಳುಹಿಸು.")
            return
        if not channel_id:
            await query.edit_message_text("⚠️ Nimma Dump Channel ID set ಆಗಿಲ್ಲ. /start → Set Dump Channel ID ಮೂಲಕ nimma ID set maadi.")
            return
        try:
            if pending["type"] == "photo":
                await context.bot.send_photo(chat_id=channel_id, photo=poster_input(pending), caption=pending["caption"], parse_mode=ParseMode.HTML, reply_markup=pending.get("keyboard"))
            else:
                await context.bot.send_message(chat_id=channel_id, text=pending["caption"], parse_mode=ParseMode.HTML, reply_markup=pending.get("keyboard"))
        except Exception:
            logging.exception("Failed to dump post to configured channel")
            await query.edit_message_text("❌ Dump ಆಗಲಿಲ್ಲ. Channel ID ಸರಿಯಿದೆಯೇ ಮತ್ತು bot channel admin ಆಗಿದೆಯೇ ಪರಿಶೀಲಿಸು.")
            context.user_data.pop("pending_dump_post", None)
            return
        context.user_data.pop("pending_dump_post", None)
        await query.edit_message_text("✅ Post Dump Channel‌ಗೆ ಕಳುಹಿಸಲಾಗಿದೆ.")


async def watermark_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    s = get_poster_settings(user.id)
    await message.reply_text(
        f"Current watermark: {s['watermark_text']}\nSize: {s['watermark_size']}\nPosition: {s['watermark_position']}\n\n"
        "/setwatermark @SKR_updates\n/setwatermarksize 28\n/setwatermarkposition bottom-right\n"
        "Positions: bottom-right, bottom-left, top-right, top-left, center", parse_mode=None)


async def setwatermark_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    value = " ".join(context.args).strip()
    if not message or not user:
        return
    if not value or len(value) > 80:
        await message.reply_text("Format: /setwatermark @SKR_updates", parse_mode=None)
        return
    save_poster_setting(user.id, "watermark_text", value)
    await message.reply_text("✅ Watermark text save ಆಯಿತು. ಹೊಸ posters ಮೇಲೆ white fontನಲ್ಲಿ ಬರುತ್ತದೆ.", parse_mode=None)


async def setwatermarksize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    try:
        size = int(context.args[0]) if context.args else 0
    except ValueError:
        size = 0
    if size < 12 or size > 100:
        await message.reply_text("Format: /setwatermarksize 28 (12-100)", parse_mode=None)
        return
    save_poster_setting(user.id, "watermark_size", size)
    await message.reply_text(f"✅ Watermark font size {size} save ಆಯಿತು.", parse_mode=None)


async def setwatermarkposition_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.effective_message, update.effective_user
    allowed = {"bottom-right", "bottom-left", "top-right", "top-left", "center"}
    value = "-".join(context.args).strip().lower()
    if not message or not user:
        return
    if value not in allowed:
        await message.reply_text("Format: /setwatermarkposition bottom-right\nOptions: bottom-right, bottom-left, top-right, top-left, center", parse_mode=None)
        return
    save_poster_setting(user.id, "watermark_position", value)
    await message.reply_text("✅ Watermark position save ಆಯಿತು.", parse_mode=None)


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
    app.add_handler(CommandHandler("adddump", adddump_command))
    app.add_handler(CommandHandler("dumps", dumps_command))
    app.add_handler(CommandHandler("removedump", removedump_command))
    app.add_handler(CallbackQueryHandler(button_callbacks))
    app.add_handler(CommandHandler("setfooter", setfooter))
    app.add_handler(CommandHandler("footer", showfooter))
    app.add_handler(CommandHandler("resetfooter", resetfooter))
    app.add_handler(CommandHandler("setbuttonfooter", setbuttonfooter))
    app.add_handler(CommandHandler("buttonfooter", showbuttonfooter))
    app.add_handler(CommandHandler("resetbuttonfooter", resetbuttonfooter))

    app.add_handler(CommandHandler("removeshortlinktoken", removeshortlinktoken_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("shortlink", shortlink_command))
    app.add_handler(CommandHandler("s", shortlink_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("mediainfo", mediainfo_command))
    app.add_handler(CommandHandler("m", mediainfo_command))
    app.add_handler(CommandHandler("normal", normal_mode))
    app.add_handler(CommandHandler("buttons", buttons_mode))
    app.add_handler(CommandHandler("buttonsettings", buttonsettings))
    app.add_handler(CommandHandler("setdownloadtext", setdownloadtext))
    app.add_handler(CommandHandler("setwatchtext", setwatchtext))
    app.add_handler(CommandHandler("setupdatesbutton", setupdatesbutton))
    app.add_handler(CommandHandler("watermark", watermark_command))
    app.add_handler(CommandHandler("setwatermark", setwatermark_command))
    app.add_handler(CommandHandler("setwatermarksize", setwatermarksize_command))
    app.add_handler(CommandHandler("setwatermarkposition", setwatermarkposition_command))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.ANIMATION, handle_media_filename))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mediainfo_text), group=-1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
