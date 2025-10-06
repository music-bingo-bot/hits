# admin_web.py
import os
from typing import List, Optional

from fastapi import FastAPI, Request, UploadFile, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import aiofiles

# для рассылок и треков
from db import (
    get_all_tracks, get_track_by_id,
    create_track, update_track, delete_track,          # алиасы совместимости
    update_track_file,
    create_admin_token, consume_admin_token,
    broadcasts_all, broadcast_create, broadcast_add_media,
    broadcast_media, broadcast_delete, broadcast_mark_sent,
    get_all_user_ids,
)

TEMPLATES = Jinja2Templates(directory="templates")


# ---- утилита: зачистка ID3 у mp3 ----
def _strip_id3_safe(path: str) -> None:
    """
    Удаляем ID3-теги, чтобы Telegram не подставлял 'Исполнитель – Название' из метаданных.
    Работает «мягко»: если файла/библиотеки нет — просто пропускаем.
    """
    try:
        from mutagen import File
        from mutagen.id3 import ID3, ID3NoHeaderError
        if not os.path.exists(path):
            return
        try:
            tags = ID3(path)
            # полностью сносим теги
            tags.delete(path)
        except ID3NoHeaderError:
            pass  # тегов и так нет
        # На всякий случай обрабатываем другие контейнеры
        mf = File(path)
        if mf is not None and mf.tags is not None:
            mf.delete()  # некоторые контейнеры поддерживают delete()
            mf.save()
    except Exception:
        # не валим загрузку, если что-то пошло не так
        pass


def create_app(bot):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "secret"))

    # раздача /uploads (аудио/картинки) как статики
    os.makedirs("uploads", exist_ok=True)
    app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

    # ---------- auth ----------
    def _is_authed(request: Request) -> bool:
        return bool(request.session.get("adm_ok"))

    def _need_auth(request: Request):
        if not _is_authed(request):
            return RedirectResponse("/admin_web/login", status_code=302)

    @app.get("/", response_class=HTMLResponse)
    def _root(_: Request):
        return RedirectResponse("/admin_web", status_code=302)

    @app.get("/admin_web/login", response_class=HTMLResponse)
    async def login_get(request: Request):
        return TEMPLATES.TemplateResponse("login.html", {"request": request})

    @app.post("/admin_web/login", response_class=HTMLResponse)
    async def login_post(request: Request, password: str = Form("")):
        ok = False
        # 1) одноразовый токен ?key=...
        token = request.query_params.get("key")
        if token:
            user_id = await consume_admin_token(token)
            ok = user_id is not None
        # 2) обычный пароль (SESSION_SECRET)
        if not ok and password and password == os.getenv("SESSION_SECRET", ""):
            ok = True

        if ok:
            request.session["adm_ok"] = True
            return RedirectResponse("/admin_web", status_code=302)
        return TEMPLATES.TemplateResponse("login.html", {"request": request, "error": "Неверный токен или пароль"})

    @app.get("/admin_web/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/admin_web/login", status_code=302)

    # ---------- tracks ----------
    @app.get("/admin_web", response_class=HTMLResponse)
    async def tracks_page(request: Request):
        guard = _need_auth(request)
        if guard: return guard
        items = await get_all_tracks()
        return TEMPLATES.TemplateResponse("tracks.html", {"request": request, "items": items})

    @app.post("/admin_web/upload")
    async def upload_track(
        request: Request,
        title: str = Form(""),
        audio: Optional[UploadFile] = None,
        hint: Optional[UploadFile] = None,
    ):
        guard = _need_auth(request)
        if guard: return guard

        # следующий номер
        items = await get_all_tracks()
        seq = len(items) + 1

        # имя MP3 — с длинным тире
        seq_name = f"Музыкальное бинго — {seq:02d}.mp3"

        audio_path = None
        if audio and audio.filename:
            os.makedirs("uploads/audio", exist_ok=True)
            audio_path = os.path.join("uploads", "audio", seq_name)
            async with aiofiles.open(audio_path, "wb") as f:
                while chunk := await audio.read(64 * 1024):
                    await f.write(chunk)
            # удаляем ID3
            _strip_id3_safe(audio_path)

        hint_path = None
        if hint and hint.filename:
            os.makedirs("uploads/hints", exist_ok=True)
            ext = os.path.splitext(hint.filename)[1] or ".jpg"
            hint_path = os.path.join("uploads", "hints", f"hint_{seq:02d}{ext}")
            async with aiofiles.open(hint_path, "wb") as f:
                while chunk := await hint.read(64 * 1024):
                    await f.write(chunk)

        # сохраняем (title, HINT, AUDIO)
        await create_track(title or f"Хит #{seq:02d}", hint_path or "", audio_path or "")
        return RedirectResponse("/admin_web", status_code=302)

    @app.get("/admin_web/edit/{track_id}", response_class=HTMLResponse)
    async def edit_track_page(request: Request, track_id: int):
        guard = _need_auth(request)
        if guard: return guard
        row = await get_track_by_id(track_id)
        return TEMPLATES.TemplateResponse("edit_track.html", {"request": request, "row": row})

    @app.post("/admin_web/edit/{track_id}")
    async def edit_track_post(
        request: Request, track_id: int,
        title: str = Form(""),
        audio: Optional[UploadFile] = None,
        hint: Optional[UploadFile] = None,
    ):
        guard = _need_auth(request)
        if guard: return guard

        # заменить картинку-подсказку
        hint_path = None
        if hint and hint.filename:
            os.makedirs("uploads/hints", exist_ok=True)
            ext = os.path.splitext(hint.filename)[1] or ".jpg"
            hint_path = os.path.join("uploads", "hints", f"hint_{track_id:02d}{ext}")
            async with aiofiles.open(hint_path, "wb") as f:
                while chunk := await hint.read(64 * 1024):
                    await f.write(chunk)

        # заменить аудио
        audio_path = None
        if audio and audio.filename:
            os.makedirs("uploads/audio", exist_ok=True)
            audio_path = os.path.join("uploads", "audio", f"Музыкальное бинго — {track_id:02d}.mp3")
            async with aiofiles.open(audio_path, "wb") as f:
                while chunk := await audio.read(64 * 1024):
                    await f.write(chunk)
            _strip_id3_safe(audio_path)

        # обновляем БД
        if title or hint_path is not None:
            # если подсказку не меняли — оставляем прежний путь
            old = await get_track_by_id(track_id)
            new_hint = hint_path if hint_path is not None else (old[2] if old else "")
            await update_track(track_id, title or (old[1] if old else ""), new_hint)
        if audio_path is not None:
            await update_track_file(track_id, audio_path)

        return RedirectResponse("/admin_web", status_code=302)

    @app.post("/admin_web/delete/{track_id}")
    async def delete_track_post(request: Request, track_id: int):
        guard = _need_auth(request)
        if guard: return guard
        await delete_track(track_id)
        return RedirectResponse("/admin_web", status_code=302)

    # ---------- broadcasts ----------
    @app.get("/admin_web/broadcasts", response_class=HTMLResponse)
    async def broadcasts_list(request: Request):
        guard = _need_auth(request)
        if guard: return guard
        items = await broadcasts_all()
        return TEMPLATES.TemplateResponse("broadcasts_list.html", {"request": request, "items": items})

    @app.get("/admin_web/broadcasts/new", response_class=HTMLResponse)
    async def broadcasts_new(request: Request):
        guard = _need_auth(request)
        if guard: return guard
        return TEMPLATES.TemplateResponse("broadcasts_new.html", {"request": request})

    @app.post("/admin_web/broadcasts/preview", response_class=HTMLResponse)
    async def broadcasts_preview(
        request: Request,
        title: str = Form(""),
        text: str = Form(""),
        images: List[UploadFile] = Form([]),
        videos: List[UploadFile] = Form([]),
        files:  List[UploadFile] = Form([]),
    ):
        guard = _need_auth(request)
        if guard: return guard

        bid = await broadcast_create(title, text)
        os.makedirs("uploads/broadcasts", exist_ok=True)

        async def _save_many(lst, kind):
            for up in lst or []:
                if not up.filename:
                    continue
                path = os.path.join("uploads", "broadcasts", up.filename)
                async with aiofiles.open(path, "wb") as f:
                    while chunk := await up.read(64 * 1024):
                        await f.write(chunk)
                await broadcast_add_media(bid, kind, path)

        await _save_many(images, "image")
        await _save_many(videos, "video")
        await _save_many(files,  "file")

        return RedirectResponse("/admin_web/broadcasts", status_code=302)

    @app.post("/admin_web/broadcasts/delete/{bid}")
    async def broadcasts_del(request: Request, bid: int):
        guard = _need_auth(request)
        if guard: return guard
        await broadcast_delete(bid)
        return RedirectResponse("/admin_web/broadcasts", status_code=302)

    @app.post("/admin_web/broadcasts/send/{bid}")
    async def broadcasts_send(request: Request, bid: int):
        guard = _need_auth(request)
        if guard: return guard

        media = await broadcast_media(bid)
        title = None
        text = None
        for it in await broadcasts_all():
            if it[0] == bid:
                _, title, text, *_ = it

        uids = await get_all_user_ids()
        sent = 0
        failed = 0

        from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, InputMediaDocument

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
        return RedirectResponse(f"/admin_web/broadcasts?sent={sent}&failed={failed}", status_code=302)

    # ---------- health ----------
    @app.api_route("/health", methods=["GET", "HEAD"])
    async def _health_edge():
        return PlainTextResponse("ok")

    @app.get("/healthz")
    async def _healthz():
        return PlainTextResponse("ok")

    return app
