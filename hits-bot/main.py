# main.py
import asyncio, os, random, traceback, logging
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
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
app: FastAPI = create_app(bot)

@app.api_route("/health", methods=["GET","HEAD"])
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

def _public_host()->str:
    for key in ("PUBLIC_URL","RAILWAY_PUBLIC_DOMAIN","REPLIT_WEB_URL"):
        val = os.getenv(key,"").strip()
        if val:
            return val if val.startswith("http") else f"https://{val}"
    return "https://example.com"

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
    if len(tracks) < max(MIN_TRACKS,1):
        await safe_send_text(c.message.answer, f"⚠️ В плейлисте {len(tracks)} трек(ов). Нужно ≥ {max(MIN_TRACKS,1)}. Загрузите через /admin_web.")
        await c.answer(); return
    order_ids = [t[0] for t in tracks]
    random.shuffle(order_ids)
    games[c.message.chat.id] = GameState(order_ids=order_ids, idx=0)
    await safe_send_text(c.message.answer, MSG.get("START_GAME"))
    await send_current_track(c.message.chat.id)
    await c.answer()

async def send_current_track(chat_id:int):
    state = games.get(chat_id)
    if not state:
        return

    row = await get_track_by_id(state.order_ids[state.idx])
    if not row:
        await bot.send_message(chat_id, "❌ Трек не найден."); return

    _id, title, hint_image, file_field = row
    caption = MSG.get("TRACK_X_OF_Y", i=state.idx+1, total=len(state.order_ids))

    width = len(str(len(state.order_ids)))
    seq_title = f"Музыкальное бинго — {state.idx+1:0{width}d}.mp3"  # только заголовок — без performer

    try:
        if file_field and file_field.startswith("uploads/") and os.path.exists(file_field):
            await bot.send_audio(
                chat_id,
                audio=FSInputFile(file_field),
                caption=caption,
                title=seq_title,
                reply_markup=kb_track_full()
            )
        else:
            await bot.send_audio(
                chat_id,
                audio=file_field,
                caption=caption,
                title=seq_title,
                reply_markup=kb_track_full()
            )
    except TelegramBadRequest:
        await bot.send_message(chat_id, "❌ Не удалось отправить аудио. Проверь файл/file_id.")

@dp.callback_query(F.data == "game:hint")
async def cb_hint(c: CallbackQuery):
    state = games.get(c.message.chat.id)
    if not state: return await c.answer()
    row = await get_track_by_id(state.order_ids[state.idx])
    if not row: return await c.answer()
    _id, _title, hint_image, _file = row

    # подсказка — ТОЛЬКО ФОТО, без подписи; ниже — кнопки Ответ/Следующий
    if hint_image and hint_image.startswith("uploads/") and os.path.exists(hint_image):
        await c.message.answer_photo(FSInputFile(hint_image), reply_markup=kb_after_hint())
    else:
        await c.message.answer("Подсказка недоступна.", reply_markup=kb_after_hint())
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
    if state.idx < len(state.order_ids)-1:
        state.idx += 1
        await send_current_track(c.message.chat.id)
    else:
        await safe_send_text(c.message.answer, MSG.get("END_GAME"), reply_markup=kb_restart())
    await c.answer()

@dp.callback_query(F.data == "game:restart")
async def cb_restart(c: CallbackQuery):
    tracks = await get_all_tracks()
    if len(tracks) < max(MIN_TRACKS,1):
        await safe_send_text(c.message.answer, f"⚠️ В плейлисте {len(tracks)} трек(ов). Нужно ≥ {max(MIN_TRACKS,1)}.")
        return await c.answer()
    order_ids = [t[0] for t in tracks]; random.shuffle(order_ids)
    games[c.message.chat.id] = GameState(order_ids=order_ids, idx=0)
    await safe_send_text(c.message.answer, "Погнали ещё! 🎵")
    await send_current_track(c.message.chat.id)
    await c.answer()


# ---------- keepalive ----------
async def keepalive():
    import aiohttp, asyncio as aio
    host = next((os.getenv(k,"").strip() for k in ("PUBLIC_URL","RAILWAY_PUBLIC_DOMAIN","REPLIT_WEB_URL") if os.getenv(k)), "")
    if not host: return
    url = (host if host.startswith("http") else f"https://{host}").rstrip("/") + "/healthz"
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=10): pass
        except Exception: pass
        await aio.sleep(240)

# ---------- run ----------
async def run_bot():
    await init_db()
    delay = 1
    while True:
        try:
            await dp.start_polling(bot); break
        except Exception as e:
            logging.exception("[polling] crashed: %r", e)
            await asyncio.sleep(delay); delay = min(delay*2, 60)

async def run_web():
    port = int(os.getenv("PORT","8080"))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info"))
    await server.serve()

async def main():
    web_task = asyncio.create_task(run_web())
    bot_task = asyncio.create_task(run_bot())
    ka_task = asyncio.create_task(keepalive())
    await asyncio.gather(web_task, bot_task, ka_task)

if __name__ == "__main__":
    asyncio.run(main())
