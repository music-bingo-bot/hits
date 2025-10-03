# admin_web.py
import os
import re
from typing import List, Optional

import aiofiles
from fastapi import FastAPI, Request, UploadFile, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from db import (
    # треки
    get_all_tracks, get_track_by_id, create_track, update_track, delete_track,
    # админ-токены
    create_admin_token, pop_admin_token,
    # рассылки
    broadcasts_all, broadcast_create, broadcast_add_media, broadcast_media,
    broadcast_delete, broadcast_mark_sent, get_all_user_ids
)

TEMPLATES = Jinja2Templates(directory="templates")


def _safe_filename(name: str, default_ext: str) -> str:
    """
    Аккуратно чистим имя файла от мусора. Если нет расширения — подставим default_ext.
    """
    name = name.strip().replace("\\", "_").replace("/", "_")
    name = re.sub(r'[^0-9A-Za-zА-Яа-я.\- _()+]+', "", name)
    if "." not in os.path.basename(name):
        name += default_ext
    return name


def _seq_audio_filename(seq: int) -> str:
    # Имя файла как в ТЗ. В Windows допустим «-», «—» тоже ok на ext4.
    # Если хочешь строго «—», поменяй дефис на эм-даш.
    return f"Музыкальное бинго - {seq:02d}.mp3"


def create_app(bot):
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.getenv("SESSION_SECRET", "secret"),  # itsdangerous обязателен в requirements
    )

    # -------------------- AUTH --------------------
    def _is_authed(request: Request) -> bool:
        return bool(request.session.get("adm_ok"))

    def _need_auth(request: Request):
        if not _is_authed(request):
            return RedirectResponse("/admin_web/login", status_code=302)

    @app.get("/", response_class=HTMLResponse)
    def _root(request: Request):
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
            user_id = await pop_admin_token(token)
            ok = user_id is not None

        # 2) пароль из переменной окружения SESSION_SECRET
        if not ok and password and password == os.getenv("SESSION_SECRET", ""):
            ok = True

        if ok:
            request.session["adm_ok"] = True
            return RedirectResponse("/admin_web", status_code=302)

        return TEMPLATES.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверный токен или пароль"},
        )

    @app.get("/admin_web/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/admin_web/login", status_code=302)

    # -------------------- TRACKS --------------------
    @app.get("/admin_web", response_class=HTMLResponse)
    async def tracks_page(request: Request):
        guard = _need_auth(request)
        if guard:
            return guard
        items = await get_all_tracks()
        return TEMPLATES.TemplateResponse(
            "tracks.html",
            {"request": request, "items": items},
        )

    # Совместимость: примем и старый путь /admin_web/upload, и новый /admin_web/tracks/new
    async def _handle_upload(request: Request, title: str, audio: Optional[UploadFile], hint: Optional[UploadFile]):
        guard = _need_auth(request)
        if guard:
            return guard

        # следующий порядковый номер
        existing = await get_all_tracks()
        seq = len(existing) + 1

        # Пути
        os.makedirs("uploads/audio", exist_ok=True)
        os.makedirs("uploads/hints", exist_ok=True)

        audio_path = None
        if audio and audio.filename:
            # сохраняем под автоименем «Музыкальное бинго - 01.mp3»
            target = os.path.join("uploads", "audio", _seq_audio_filename(seq))
            async with aiofiles.open(target, "wb") as f:
                while chunk := await audio.read(1024 * 64):
                    await f.write(chunk)
            audio_path = target

        hint_path = None
        if hint and hint.filename:
            # сохраняем картинку-подсказку как hint_01.jpg|png …
            ext = os.path.splitext(hint.filename)[1].lower() or ".jpg"
            safe = f"hint_{seq:02d}{ext}"
            target = os.path.join("uploads", "hints", safe)
            async with aiofiles.open(target, "wb") as f:
                while chunk := await hint.read(1024 * 64):
                    await f.write(chunk)
            hint_path = target

        # заголовок по умолчанию
        track_title = title.strip() or f"Хит #{seq:02d}"

        # записываем в БД: (title, file_path, hint_image_path)
        # db.create_track(title, audio_path, hint_path) → будет соответствовать main.py
        await create_track(track_title, audio_path or "", hint_path or "")

        return RedirectResponse("/admin_web", status_code=302)

    @app.post("/admin_web/upload")
    async def upload_track_legacy(
        request: Request,
        title: str = Form(""),
        audio: UploadFile | None = None,
        hint: UploadFile | None = None,
    ):
        return await _handle_upload(request, title, audio, hint)

    @app.post("/admin_web/tracks/new")
    async def upload_track_new(
        request: Request,
        title: str = Form(""),
        audio: UploadFile | None = None,
        hint_image: UploadFile | None = None,
        # некоторые формы могли назвать поле hint иначе
        hint: UploadFile | None = None,
    ):
        return await _handle_upload(request, title, audio, hint_image or hint)

    @app.get("/admin_web/edit/{track_id}", response_class=HTMLResponse)
    async def edit_track_page(request: Request, track_id: int):
        guard = _need_auth(request)
        if guard:
            return guard
        row = await get_track_by_id(track_id)
        return TEMPLATES.TemplateResponse(
            "edit_track.html",
            {"request": request, "row": row, "track_id": track_id},
        )

    @app.post("/admin_web/edit/{track_id}")
    async def edit_track_post(
        request: Request,
        track_id: int,
        title: str = Form(""),
        audio: UploadFile | None = None,
        hint_image: UploadFile | None = None,
        hint: UploadFile | None = None,
    ):
        guard = _need_auth(request)
        if guard:
            return guard

        os.makedirs("uploads/audio", exist_ok=True)
        os.makedirs("uploads/hints", exist_ok=True)

        audio_path = None
        if audio and audio.filename:
            # если загружают новый mp3 — сохраняем рядом
            # оставим «читабельное» имя от пользователя, но чистим
            ext = os.path.splitext(audio.filename)[1].lower() or ".mp3"
            pretty = _safe_filename((title or f"track_{track_id}"), ext)
            target = os.path.join("uploads", "audio", pretty)
            async with aiofiles.open(target, "wb") as f:
                while chunk := await audio.read(1024 * 64):
                    await f.write(chunk)
            audio_path = target

        hint_up = hint_image or hint
        hint_path = None
        if hint_up and hint_up.filename:
            ext = os.path.splitext(hint_up.filename)[1].lower() or ".jpg"
            safe = f"hint_{track_id:02d}{ext}"
            target = os.path.join("uploads", "hints", safe)
            async with aiofiles.open(target, "wb") as f:
                while chunk := await hint_up.read(1024 * 64):
                    await f.write(chunk)
            hint_path = target

        await update_track(track_id, title.strip(), audio_path, hint_path)
        return RedirectResponse("/admin_web", status_code=302)

    @app.post("/admin_web/delete/{track_id}")
    async def delete_track_post(request: Request, track_id: int):
        guard = _need_auth(request)
        if guard:
            return guard
        await delete_track(track_id)
        return RedirectResponse("/admin_web", status_code=302)

    # -------------------- BROADCASTS --------------------
    @app.get("/admin_web/broadcasts", response_class=HTMLResponse)
    async def broadcasts_list(request: Request):
        guard = _need_auth(request)
        if guard:
            return guard
        items = await broadcasts_all()
        return TEMPLATES.TemplateResponse(
            "broadcasts_list.html",
            {"request": request, "items": items},
        )

    @app.get("/admin_web/broadcasts/new", response_class=HTMLResponse)
    async def broadcasts_new(request: Request):
        guard = _need_auth(request)
        if guard:
            return guard
        return TEMPLATES.TemplateResponse("broadcasts_new.html", {"request": request})

    @app.post("/admin_web/broadcasts/preview", response_class=HTMLResponse)
    async def broadcasts_preview(
        request: Request,
        title: str = Form(""),
        text: str = Form(""),
        images: List[UploadFile] = [],
        videos: List[UploadFile] = [],
        files: List[UploadFile] = [],
    ):
        guard = _need_auth(request)
        if guard:
            return guard

        bid = await broadcast_create(title.strip(), text.strip())
        os.makedirs("uploads/broadcasts", exist_ok=True)

        async def _save_many(lst: List[UploadFile], kind: str):
            for up in lst or []:
                if not up or not up.filename:
                    continue
                fname = _safe_filename(up.filename, ".bin")
                path = os.path.join("uploads", "broadcasts", fname)
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
        await broadcast_delete(bid)
        return RedirectResponse("/admin_web/broadcasts", status_code=302)

    @app.post("/admin_web/broadcasts/send/{bid}")
    async def broadcasts_send(request: Request, bid: int):
        guard = _need_auth(request)
        if guard:
            return guard

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
        return RedirectResponse(
            f"/admin_web/broadcasts?sent={sent}&failed={failed}",
            status_code=302,
        )

    # -------------------- HEALTH --------------------
    @app.api_route("/health", methods=["GET", "HEAD"])
    async def _health_edge():
        return PlainTextResponse("ok")

    @app.get("/healthz")
    async def _healthz():
        return PlainTextResponse("ok")

    return app
