# admin_web.py
import os
from typing import List, Optional

from fastapi import FastAPI, Request, UploadFile, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import aiofiles

from db import (
    get_all_tracks, get_track_by_id, create_track, update_track, delete_track,
    create_admin_token, pop_admin_token,
)

TEMPLATES = Jinja2Templates(directory="templates")

# --- опциональные импорты для рассылок (могут отсутствовать в db.py) ---
HAS_BROADCASTS = True
try:
    from db import (
        broadcasts_all, broadcast_create, broadcast_add_media, broadcast_media,
        broadcast_delete, broadcast_mark_sent, get_all_user_ids,
    )
except Exception:
    HAS_BROADCASTS = False

    async def broadcasts_all():
        return []

    async def broadcast_create(title: str, text: str) -> int:
        raise RuntimeError("Broadcasts are not enabled in db.py")

    async def broadcast_add_media(*args, **kwargs):
        raise RuntimeError("Broadcasts are not enabled in db.py")

    async def broadcast_media(*args, **kwargs):
        return []

    async def broadcast_delete(*args, **kwargs):
        raise RuntimeError("Broadcasts are not enabled in db.py")

    async def broadcast_mark_sent(*args, **kwargs):
        return

    async def get_all_user_ids():
        return []


def _safe_url_prefix() -> str:
    """Базовый префикс для статики/относительных путей (если нужно)."""
    return ""


def create_app(bot):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "secret"))

    # ---------- auth ----------
    def _is_authed(request: Request) -> bool:
        return bool(request.session.get("adm_ok"))

    def _need_auth(request: Request):
        if not _is_authed(request):
            return RedirectResponse("/admin_web/login", status_code=302)

    @app.get("/", response_class=HTMLResponse)
    async def _root(_: Request):
        return RedirectResponse("/admin_web", status_code=302)

    @app.get("/admin_web/login", response_class=HTMLResponse)
    async def login_get(request: Request):
        return TEMPLATES.TemplateResponse("login.html", {"request": request})

    @app.post("/admin_web/login", response_class=HTMLResponse)
    async def login_post(request: Request, password: str = Form("")):
        # 1) вход по одноразовому токену ?key=
        token = request.query_params.get("key")
        ok = False
        if token:
            user_id = await pop_admin_token(token)
            ok = user_id is not None
        # 2) вход по паролю (SESSION_SECRET)
        if not ok and password and password == os.getenv("SESSION_SECRET", ""):
            ok = True

        if ok:
            request.session["adm_ok"] = True
            return RedirectResponse("/admin_web", status_code=302)

        return TEMPLATES.TemplateResponse(
            "login.html", {"request": request, "error": "Неверный токен или пароль"}
        )

    @app.get("/admin_web/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/admin_web/login", status_code=302)

    # ---------- tracks ----------
    @app.get("/admin_web", response_class=HTMLResponse)
    async def tracks_page(request: Request):
        guard = _need_auth(request)
        if guard:
            return guard
        items = await get_all_tracks()
        return TEMPLATES.TemplateResponse(
            "tracks.html",
            {
                "request": request,
                "items": items,
                "has_broadcasts": HAS_BROADCASTS,
            },
        )

    @app.post("/admin_web/upload")
    async def upload_track(
        request: Request,
        title: str = Form(""),
        audio: Optional[UploadFile] = None,
        hint: Optional[UploadFile] = None,
    ):
        guard = _need_auth(request)
        if guard:
            return guard

        # Автоимя файла вида «Музыкальное бинго — 01.mp3»
        items = await get_all_tracks()
        seq = len(items) + 1
        seq_name = f"Музыкальное бинго — {seq:02d}.mp3"

        audio_path = ""
        if audio and audio.filename:
            os.makedirs("uploads/audio", exist_ok=True)
            audio_path = os.path.join("uploads", "audio", seq_name)
            async with aiofiles.open(audio_path, "wb") as f:
                while chunk := await audio.read(1024 * 64):
                    await f.write(chunk)

        hint_path = ""
        if hint and hint.filename:
            os.makedirs("uploads/hints", exist_ok=True)
            ext = os.path.splitext(hint.filename)[1] or ".jpg"
            hint_path = os.path.join("uploads", "hints", f"hint_{seq:02d}{ext}")
            async with aiofiles.open(hint_path, "wb") as f:
                while chunk := await hint.read(1024 * 64):
                    await f.write(chunk)

        # В БД пишем строку формата (title, hint_image, file_field)
        await create_track(title or f"Хит #{seq:02d}", audio_path, hint_path)
        return RedirectResponse("/admin_web", status_code=302)

    @app.get("/admin_web/edit/{track_id}", response_class=HTMLResponse)
    async def edit_track_page(request: Request, track_id: int):
        guard = _need_auth(request)
        if guard:
            return guard
        row = await get_track_by_id(track_id)
        return TEMPLATES.TemplateResponse(
            "edit_track.html",
            {
                "request": request,
                "row": row,
                "has_broadcasts": HAS_BROADCASTS,
            },
        )

    @app.post("/admin_web/edit/{track_id}")
    async def edit_track_post(
        request: Request,
        track_id: int,
        title: str = Form(""),
        audio: Optional[UploadFile] = None,
        hint: Optional[UploadFile] = None,
    ):
        guard = _need_auth(request)
        if guard:
            return guard

        new_audio_path = None
        if audio and audio.filename:
            os.makedirs("uploads/audio", exist_ok=True)
            # сохраняем с расширением из загружаемого файла; имя — оставим «как есть» либо
            # можно перезаписать под «Музыкальное бинго — NN.mp3», но тут не меняем автоматически
            ext = os.path.splitext(audio.filename)[1] or ".mp3"
            # если имя не .mp3 — всё равно примем (Telegram воспроизводит), но расширение добавим
            safe_name = audio.filename if audio.filename.endswith(ext) else f"{os.path.splitext(audio.filename)[0]}{ext}"
            new_audio_path = os.path.join("uploads", "audio", safe_name)
            async with aiofiles.open(new_audio_path, "wb") as f:
                while chunk := await audio.read(1024 * 64):
                    await f.write(chunk)

        new_hint_path = None
        if hint and hint.filename:
            os.makedirs("uploads/hints", exist_ok=True)
            ext = os.path.splitext(hint.filename)[1] or ".jpg"
            new_hint_path = os.path.join("uploads", "hints", f"hint_{track_id:02d}{ext}")
            async with aiofiles.open(new_hint_path, "wb") as f:
                while chunk := await hint.read(1024 * 64):
                    await f.write(chunk)

        await update_track(track_id, title, new_audio_path, new_hint_path)
        return RedirectResponse("/admin_web", status_code=302)

    @app.post("/admin_web/delete/{track_id}")
    async def delete_track_post(request: Request, track_id: int):
        guard = _need_auth(request)
        if guard:
            return guard
        await delete_track(track_id)
        return RedirectResponse("/admin_web", status_code=302)

    # ---------- broadcasts (опционально) ----------
    @app.get("/admin_web/broadcasts", response_class=HTMLResponse)
    async def broadcasts_list(request: Request):
        guard = _need_auth(request)
        if guard:
            return guard
        if not HAS_BROADCASTS:
            return HTMLResponse(
                "<h2>Рассылки отключены: в db.py нет соответствующих функций.</h2>"
                "<p>Добавь их, либо игнорируй раздел.</p>",
                status_code=200,
            )
        items = await broadcasts_all()
        return TEMPLATES.TemplateResponse(
            "broadcasts_list.html",
            {"request": request, "items": items, "has_broadcasts": HAS_BROADCASTS},
        )

    @app.get("/admin_web/broadcasts/new", response_class=HTMLResponse)
    async def broadcasts_new(request: Request):
        guard = _need_auth(request)
        if guard:
            return guard
        if not HAS_BROADCASTS:
            return HTMLResponse("<h2>Рассылки отключены.</h2>", status_code=200)
        return TEMPLATES.TemplateResponse(
            "broadcasts_new.html", {"request": request, "has_broadcasts": HAS_BROADCASTS}
        )

    @app.post("/admin_web/broadcasts/preview", response_class=HTMLResponse)
    async def broadcasts_preview(
        request: Request,
        title: str = Form(""),
        text: str = Form(""),
        images: List[UploadFile] | None = None,
        videos: List[UploadFile] | None = None,
        files: List[UploadFile] | None = None,
    ):
        guard = _need_auth(request)
        if guard:
            return guard
        if not HAS_BROADCASTS:
            return HTMLResponse("<h2>Рассылки отключены.</h2>", status_code=200)

        bid = await broadcast_create(title, text)
        os.makedirs("uploads/broadcasts", exist_ok=True)

        async def _save_many(lst, kind):
            for up in lst or []:
                if not up.filename:
                    continue
                path = os.path.join("uploads", "broadcasts", up.filename)
                async with aiofiles.open(path, "wb") as f:
                    while chunk := await up.read(1024 * 64):
                        await f.write(chunk)
                await broadcast_add_media(bid, kind, path)

        await _save_many(images, "image")
        await _save_many(videos, "video")
        await _save_many(files, "file")

        return RedirectResponse("/admin_web/broadcasts", status_code=302)

    @app.post("/admin_web/broadcasts/delete/{bid}")
    async def broadcasts_del(request: Request, bid: int):
        guard = _need_auth(request)
        if guard:
            return guard
        if not HAS_BROADCASTS:
            return HTMLResponse("<h2>Рассылки отключены.</h2>", status_code=200)
        await broadcast_delete(bid)
        return RedirectResponse("/admin_web/broadcasts", status_code=302)

    @app.post("/admin_web/broadcasts/send/{bid}")
    async def broadcasts_send(request: Request, bid: int):
        guard = _need_auth(request)
        if guard:
            return guard
        if not HAS_BROADCASTS:
            return HTMLResponse("<h2>Рассылки отключены.</h2>", status_code=200)

        media = await broadcast_media(bid)
        title = None
        text = None
        for it in await broadcasts_all():
            if it[0] == bid:
                _, title, text, *_ = it

        uids = await get_all_user_ids()
        sent = 0
        failed = 0

        from aiogram.types import (
            FSInputFile,
            InputMediaPhoto,
            InputMediaVideo,
            InputMediaDocument,
        )

        album = []
        for kind, path in media[:10]:
            if kind == "image":
                album.append(InputMediaPhoto(media=FSInputFile(path)))
            elif kind == "video":
                album.append(InputMediaVideo(media=FSInputFile(path)))
            else:
                album.append(InputMediaDocument(media=FSInputFile(path)))

        for uid in uids:
            try:
                if album:
                    if text:
                        album[0].caption = text
                    await bot.send_media_group(uid, album)
                if not album and text:
                    await bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1

        await broadcast_mark_sent(bid)
        return RedirectResponse(
            f"/admin_web/broadcasts?sent={sent}&failed={failed}", status_code=302
        )

    # ---------- health ----------
    @app.api_route("/health", methods=["GET", "HEAD"])
    async def _health_edge():
        return PlainTextResponse("ok")

    @app.get("/healthz")
    async def _healthz():
        return PlainTextResponse("ok")

    return app
