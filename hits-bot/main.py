# main.py
import asyncio
import os
import random
import traceback
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass
from typing import Dict, List

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
import uvicorn

from db import (
    init_db, get_all_tracks, get_track_by_id,
    create_admin_token, get_setting, save_user
)
import messages as MSG
from admin_web import create_app

from r2_storage import (
    r2_enabled,
    normalize_r2_audio_ref,
    normalize_r2_hint_ref,
    presign_get_url,
    get_bytes_from_r2,
    overwrite_bytes_in_r2,
)

# ---------- ENV ----------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
MIN_TRACKS = int(os.getenv("MIN_TRACKS", "1"))

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")


# ---------- LOGGING ----------
os.makedirs("logs", exist_ok=True)
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
fh = RotatingFileHandler("logs/app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
root_logger.addHandler(fh)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
root_logger.addHandler(ch)
logger = logging.getLogger("hits-bot")


# ---------- BOT / WEB ----------
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
app: FastAPI = create_app(bot)

@app.api_route("/health", methods=["GET", "HEAD"])
async def _health_edge():
    return PlainTextResponse("ok")

@app.get("/healthz")
async def _healthz():
    return PlainTextResponse("ok")


# ---------- utils ----------
async def safe_send_text(method, *args, **kwargs):
    try:
        return await method(*args, **kwargs)
    except TelegramBadRequest:
        kwargs.pop("parse_mode", None)
        return await method(*args, parse_mode=None, **kwargs)


@dataclass
class GameState:
    order_ids: List[int]
    idx: int

games: Dict[int, GameState] = {}


# ---------- keyboards ----------
def kb_main():
    kb = InlineKeyboardBuilder()
    kb.button(text=MSG.get("BUTTON_START"), callback_data="game:start")
    kb.button(text=MSG.get("BUTTON_HELP"),  callback_data="game:help")
    kb.adjust(2)
    return kb.as_markup()

def kb_track_full():
    kb = InlineKeyboardBuilder()
    kb.button(text=MSG.get("BUTTON_HINT"),   callback_data="game:hint")
    kb.button(text=MSG.get("BUTTON_ANSWER"), callback_data="game:answer")
    kb.button(text=MSG.get("BUTTON_NEXT"),   callback_data="game:next")
    kb.adjust(2, 1)
    return kb.as_markup()

def kb_after_hint():
    kb = InlineKeyboardBuilder()
    kb.button(text=MSG.get("BUTTON_ANSWER"), callback_data="game:answer")
    kb.button(text=MSG.get("BUTTON_NEXT"),   callback_data="game:next")
    kb.adjust(2)
    return kb.as_markup()

def kb_after_answer():
    kb = InlineKeyboardBuilder()
    kb.button(text=MSG.get("BUTTON_NEXT"),   callback_data="game:next")
    return kb.as_markup()

def kb_restart():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Сыграть ещё раз", callback_data="game:restart")
    return kb.as_markup()


# ---------- helpers ----------
def _public_host() -> str:
    for key in ("PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN", "REPLIT_WEB_URL"):
        val = os.getenv(key, "").strip()
        if val:
            return val if val.startswith("http") else f"https://{val}"
    return "https://example.com"


# --- ID3 strip (аудио) ---
def _strip_id3_bytes_mp3(data: bytes) -> bytes:
    """
    Снимаем ID3v2 (в начале) и ID3v1 (в конце).
    Это важно, чтобы Telegram не подставлял названия из метаданных.
    """
    b = data or b""
    if not b:
        return b

    # ID3v2 header: "ID3" + ver(2) + flags(1) + size(4 synchsafe)
    if len(b) >= 10 and b[:3] == b"ID3":
        size_bytes = b[6:10]
        tag_size = (
            (size_bytes[0] & 0x7F) << 21 |
            (size_bytes[1] & 0x7F) << 14 |
            (size_bytes[2] & 0x7F) << 7  |
            (size_bytes[3] & 0x7F)
        )
        cut = 10 + tag_size
        if cut <= len(b):
            b = b[cut:]

    # ID3v1 footer: last 128 bytes start with "TAG"
    if len(b) >= 128 and b[-128:-125] == b"TAG":
        b = b[:-128]

    return b


SANITIZED_R2_KEYS: set[str] = set()


async def _r2_audio_url_ensure_sanitized(r2_key: str, download_filename: str) -> str:
    """
    1) Если ещё не чистили этот ключ — скачиваем из R2, снимаем ID3, overwrite (если изменилось)
    2) Возвращаем presigned URL
    """
    if r2_key and r2_key not in SANITIZED_R2_KEYS and r2_enabled():
        try:
            raw, _meta = get_bytes_from_r2(r2_key)
            clean = _strip_id3_bytes_mp3(raw)
            if clean != raw:
                overwrite_bytes_in_r2(clean, r2_key, content_type="audio/mpeg")
            SANITIZED_R2_KEYS.add(r2_key)
        except Exception as e:
            logger.warning(f"sanitize R2 mp3 failed key={r2_key}: {e}")

    return presign_get_url(
        r2_key,
        expires_seconds=3600,
        download_filename=download_filename,
        content_type="audio/mpeg",
    )


def _r2_image_url(r2_key: str) -> str:
    # content_type можно не форсить, пусть CF отдаёт как есть
    return presign_get_url(r2_key, expires_seconds=3600)


# ---------- handlers ----------
@dp.message(CommandStart())
async def start_cmd(m: Message):
    try:
        await save_user(m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name)
    except Exception as e:
        logger.warning(f"save_user failed: {e}")

    welcome_file_id = await get_setting("WELCOME_IMAGE_FILE_ID")
    if welcome_file_id:
        await safe_send_text(m.answer_photo, welcome_file_id, caption=MSG.get("WELCOME"), reply_markup=kb_main())
    else:
        await safe_send_text(m.answer, MSG.get("WELCOME"), reply_markup=kb_main())
    logger.info(f"/start by {m.from_user.id}")

@dp.message(Command("admin"))
async def admin_cmd(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        await safe_send_text(m.answer, MSG.get("NEED_ADMIN")); return
    await safe_send_text(m.answer, MSG.get("ADMIN_MENU"))

@dp.message(Command("admin_web"))
async def admin_web_cmd(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        await safe_send_text(m.answer, MSG.get("NEED_ADMIN")); return
    await safe_send_text(m.answer, f"🌐 Веб-админка: {_public_host().rstrip('/')}/admin_web\nЛучше входить через /admin_link")

@dp.message(Command("admin_link"))
async def admin_link_cmd(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        await safe_send_text(m.answer, MSG.get("NEED_ADMIN")); return
    token = await create_admin_token(m.from_user.id, ttl_minutes=10)
    await safe_send_text(m.answer, f"🔐 Одноразовая ссылка (10 мин):\n{_public_host().rstrip('/')}/admin_web?key={token}")

@dp.callback_query(F.data == "game:help")
async def cb_help(c: CallbackQuery):
    await safe_send_text(c.message.answer, MSG.get("HELP"))
    await c.answer()

@dp.callback_query(F.data == "game:start")
async def cb_start(c: CallbackQuery):
    tracks = await get_all_tracks()
    if len(tracks) < max(MIN_TRACKS, 1):
        await safe_send_text(
            c.message.answer,
            f"⚠️ В плейлисте {len(tracks)} трек(ов). Нужно ≥ {max(MIN_TRACKS,1)}. Загрузите через /admin_web."
        )
        await c.answer()
        return

    order_ids = [t[0] for t in tracks]
    random.shuffle(order_ids)
    games[c.message.chat.id] = GameState(order_ids=order_ids, idx=0)

    await safe_send_text(c.message.answer, MSG.get("START_GAME"))
    await send_current_track(c.message.chat.id)
    await c.answer()

async def send_current_track(chat_id: int):
    state = games.get(chat_id)
    if not state:
        return

    row = await get_track_by_id(state.order_ids[state.idx])
    if not row:
        await bot.send_message(chat_id, "❌ Трек не найден.")
        return

    _id, _title, _hint_img, file_field = row
    total = len(state.order_ids)
    caption = MSG.get("TRACK_X_OF_Y", i=state.idx + 1, total=total)

    width = len(str(total))
    seq_title = f"Музыкальное бинго — {state.idx + 1:0{width}d}.mp3"

    try:
        # --- R2 audio (preferred) ---
        r2_key = normalize_r2_audio_ref(file_field or "")
        if r2_key and r2_enabled():
            audio_url = await _r2_audio_url_ensure_sanitized(r2_key, download_filename=seq_title)
            await bot.send_audio(
                chat_id,
                audio=audio_url,
                caption=caption,
                title=seq_title,
                performer="",  # как было: пусто
                reply_markup=kb_track_full()
            )
            return

        # --- legacy local ---
        if file_field and file_field.startswith("uploads/") and os.path.exists(file_field):
            await bot.send_audio(
                chat_id,
                audio=FSInputFile(file_field),
                caption=caption,
                title=seq_title,
                performer="",
                reply_markup=kb_track_full()
            )
        else:
            # telegram file_id / url
            await bot.send_audio(
                chat_id,
                audio=file_field,
                caption=caption,
                title=seq_title,
                performer="",
                reply_markup=kb_track_full()
            )
    except TelegramBadRequest:
        await bot.send_message(chat_id, "Не удалось отправить аудио. Проверь файл/file_id.")
    except Exception as e:
        logger.exception(f"send_audio failed: {e}")
        await bot.send_message(chat_id, "Не удалось отправить аудио.")

@dp.callback_query(F.data == "game:hint")
async def cb_hint(c: CallbackQuery):
    state = games.get(c.message.chat.id)
    if not state: return await c.answer()
    row = await get_track_by_id(state.order_ids[state.idx])
    if not row: return await c.answer()

    _id, _title, hint_image, _file = row
    if hint_image:
        # --- R2 hint (stored as "r2/<key>") ---
        r2_key = normalize_r2_hint_ref(hint_image or "")
        if r2_key and r2_enabled():
            try:
                url = _r2_image_url(r2_key)
                await c.message.answer_photo(url, reply_markup=kb_after_hint())
                await c.answer()
                return
            except Exception as e:
                logger.warning(f"send hint from R2 failed key={r2_key}: {e}")

        if hint_image.startswith("uploads/") and os.path.exists(hint_image):
            await c.message.answer_photo(FSInputFile(hint_image), reply_markup=kb_after_hint())
        else:
            await c.message.answer_photo(hint_image, reply_markup=kb_after_hint())
    else:
        await c.message.answer("Подсказка недоступна.")
    await c.answer()

@dp.callback_query(F.data == "game:answer")
async def cb_answer(c: CallbackQuery):
    state = games.get(c.message.chat.id)
    if not state: return await c.answer()
    row = await get_track_by_id(state.order_ids[state.idx])
    if not row: return await c.answer()

    _id, title, _hint, _file = row
    await safe_send_text(c.message.answer, f"{MSG.get('ANSWER_PREFIX')} {title}", reply_markup=kb_after_answer())
    await c.answer()

@dp.callback_query(F.data == "game:next")
async def cb_next(c: CallbackQuery):
    state = games.get(c.message.chat.id)
    if not state: return await c.answer()

    if state.idx < len(state.order_ids) - 1:
        state.idx += 1
        await send_current_track(c.message.chat.id)
    else:
        await safe_send_text(c.message.answer, MSG.get("END_GAME"), reply_markup=kb_restart())
    await c.answer()

@dp.callback_query(F.data == "game:restart")
async def cb_restart(c: CallbackQuery):
    tracks = await get_all_tracks()
    if len(tracks) < max(MIN_TRACKS, 1):
        await safe_send_text(c.message.answer, f"⚠️ В плейлисте {len(tracks)} трек(ов). Нужно ≥ {max(MIN_TRACKS,1)}.")
        return await c.answer()

    order_ids = [t[0] for t in tracks]
    random.shuffle(order_ids)
    games[c.message.chat.id] = GameState(order_ids=order_ids, idx=0)

    await safe_send_text(c.message.answer, "Погнали ещё! 🎵")
    await send_current_track(c.message.chat.id)
    await c.answer()


# ---------- keepalive ----------
async def keepalive():
    import aiohttp
    host = next((os.getenv(k, "").strip() for k in ("PUBLIC_URL","RAILWAY_PUBLIC_DOMAIN","REPLIT_WEB_URL") if os.getenv(k)), "")
    if not host:
        return
    url = (host if host.startswith("http") else f"https://{host}").rstrip("/") + "/healthz"
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=10):
                    pass
        except Exception:
            pass
        await asyncio.sleep(240)

# ---------- run ----------
async def run_bot():
    await init_db()
    delay = 1
    while True:
        try:
            logger.info("[polling] start")
            await dp.start_polling(bot)
            logger.info("[polling] finished")
            break
        except Exception as e:
            logger.error(f"[polling] crashed: {repr(e)}")
            traceback.print_exc()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

async def run_web():
    port = int(os.getenv("PORT", "8080"))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info"))
    await server.serve()

async def main():
    web_task = asyncio.create_task(run_web())
    bot_task = asyncio.create_task(run_bot())
    ka_task = asyncio.create_task(keepalive())
    await asyncio.gather(web_task, bot_task, ka_task)

if __name__ == "__main__":
    asyncio.run(main())
