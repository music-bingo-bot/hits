# admin_web.py
import os
from typing import List, Optional

from fastapi import FastAPI, Request, UploadFile, Form, File
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import aiofiles

from db import (
    # треки
    get_all_tracks, get_track_by_id,
    create_track, update_track, delete_track,
    update_track_file,
    # авторизация
    create_admin_token, consume_admin_token,
    # рассылки
    broadcasts_all, broadcast_create, broadcast_add_media,
    broadcast_media, broadcast_delete, broadcast_mark_sent,
    get_all_user_ids,
)

TEMPLATES = Jinja2Templates(directory="templates")


def create_app(bot):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "secret"))

    # отдаём /uploads как статику (аудио/картинки)
    os.makedirs("uploads", exist_ok=True)
    app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

    # ---------- auth ----------
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
            user_id = await consume_admin_token(token)
            ok = user_id is not None
        # 2) пароль = SESSION_SECRET
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

    # ---------- tracks ----------
    @app.get("/admin_web", response_class=HTMLResponse)
    async def tracks_page(request: Request):
        guard = _need_auth(request)
        if guard:
            return guard
        items = await get_all_tracks()
        return TEMPLATES.TemplateResponse("tracks.html", {"request": request, "items": items})

    @app.post("/admin_web/upload")
    async def upload_track(
        request: Request,
        title: str = Form(""),
        audio: Optional[UploadFile] = File(None),
        hint: Optional[UploadFile] = File(None),
    ):
        guard = _need_auth(request)
        if guard:
            return guard

        # следующий порядковый номер по текущему списку
        items = await get_all_tracks()
        seq = len(items) + 1

        # ВНИМАНИЕ: длинное тире в имени файла
        seq_name = f"Музыкальное бинго — {seq:02d}.mp3"

        audio_path = None
        if audio and audio.filename:
            os.makedirs("uploads/audio", exist_ok=True)
            audio_path = os.path.join("uploads", "audio", seq_name)
            async with aiofiles.open(audio_path, "wb") as f:
                while chunk := await audio.read(64 * 1024):
                    await f.write(chunk)

        hint_path = None
        if hint and hint.filename:
            os.makedirs("uploads/hints", exist_ok=True)
            ext = os.path.splitext(hint.filename)[1] or ".jpg"
            hint_path = os.path.join("uploads", "hints", f"hint_{seq:02d}{ext}")
            async with aiofiles.open(hint_path, "wb") as f:
                while chunk := await hint.read(64 * 1024):
                    await f.write(chunk)

        # сохраняем в БД в порядке (title, HINT, AUDIO)
        await create_track(title or f"Хит #{seq:02d}", hint_path or "", audio_path or "")
        return RedirectResponse("/admin_web", status_code=302)

    @app.get("/admin_web/edit/{track_id}", response_class=HTMLResponse)
    async def edit_track_page(request: Request, track_id: int):
        guard = _need_auth(request)
        if guard:
            return guard
        row = await get_track_by_id(track_id)
        return TEMPLATES.TemplateResponse("edit_track.html", {"request": request, "row": row})

    @app.post("/admin_web/edit/{track_id}")
    async def edit_track_post(
        request: Request,
        track_id: int,
        title: str = Form(""),
        audio: Optional[UploadFile] = File(None),
        hint: Optional[UploadFile] = File(None),
    ):
        guard = _need_auth(request)
        if guard:
            return guard

        # текущее состояние строки, чтобы не затирать значение, если поле не пришло
        current = await get_track_by_id(track_id)  # (id, film_title, hint, file_id)
        current_hint = current[2] if current else ""
        # обновляем название сразу (даже если пустое — можно поправить ещё раз)
        await update_track(track_id, title or (current[1] if current else ""), current_hint)

        # заменить картинку-подсказку
        if hint and hint.filename:
            os.makedirs("uploads/hints", exist_ok=True)
            ext = os.path.splitext(hint.filename)[1] or ".jpg"
            hint_path = os.path.join("uploads", "hints", f"hint_{track_id:02d}{ext}")
            async with aiofiles.open(hint_path, "wb") as f:
                while chunk := await hint.read(64 * 1024):
                    await f.write(chunk)
            await update_track(track_id, title or (current[1] if current else ""), hint_path)

        # заменить аудио (имя строго по плейлисту)
        if audio and audio.filename:
            os.makedirs("uploads/audio", exist_ok=True)
            audio_path = os.path.join("uploads", "audio", f"Музыкальное бинго — {track_id:02d}.mp3")
            async with aiofiles.open(audio_path, "wb") as f:
                while chunk := await audio.read(64 * 1024):
                    await f.write(chunk)
            await update_track_file(track_id, audio_path)

        return RedirectResponse("/admin_web", status_code=302)

    @app.post("/admin_web/delete/{track_id}")
    async def delete_track_post(request: Request, track_id: int):
        guard = _need_auth(request)
        if guard:
            return guard
        await delete_track(track_id)
        return RedirectResponse("/admin_web", status_code=302)

    # ---------- broadcasts ----------
    @app.get("/admin_web/broadcasts", response_class=HTMLResponse)
    async def broadcasts_list(request: Request):
        guard = _need_auth(request)
        if guard:
            return guard
        items = await broadcasts_all()
        return TEMPLATES.TemplateResponse("broadcasts_list.html", {"request": request, "items": items})

    @app.get("/admin_web/broadcasts/new", response_class=HTMLResponse)
    async def broadcasts_new(request: Request):
        guard = _need_auth(request)
        if guard:
            return guard
        return TEMPLATES.TemplateResponse("broadcasts_new.html", {"request": request})

    @app.post("/admin_web/broadcasts/preview")
    async def broadcasts_preview(
        request: Request,
        title: str = Form(""),
        text: str = Form(""),
        images: List[UploadFile] = File(default_factory=list),
        videos: List[UploadFile] = File(default_factory=list),
        files: List[UploadFile] = File(default_factory=list),
    ):
        guard = _need_auth(request)
        if guard:
            return guard

        bid = await broadcast_create(title, text)
        os.makedirs("uploads/broadcasts", exist_ok=True)

        async def _save_many(lst: List[UploadFile], kind: str):
            for up in lst or []:
                if not up or not up.filename:
                    continue
                safe_name = up.filename
                path = os.path.join("uploads", "broadcasts", safe_name)
                async with aiofiles.open(path, "wb") as f:
                    while chunk := await up.read(64 * 1024):
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

        media_rows = await broadcast_media(bid)  # [(kind, path), ...]
        title_txt = None
        body_txt = None
        for it in await broadcasts_all():
            if it[0] == bid:
                _, title_txt, body_txt, *_ = it
                break

        uids = await get_all_user_ids()
        sent = 0
        failed = 0

        from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, InputMediaDocument

        async def send_to(uid: int):
            nonlocal sent, failed
            try:
                n = len(media_rows)

                if n >= 2:
                    album = []
                    for kind, path in media_rows[:10]:
                        if kind == "image":
                            album.append(InputMediaPhoto(media=FSInputFile(path)))
                        elif kind == "video":
                            album.append(InputMediaVideo(media=FSInputFile(path)))
                        else:
                            album.append(InputMediaDocument(media=FSInputFile(path)))
                    if body_txt:
                        album[0].caption = body_txt
                    await bot.send_media_group(uid, album)

                elif n == 1:
                    kind, path = media_rows[0]
                    file = FSInputFile(path)
                    if kind == "image":
                        await bot.send_photo(uid, file, caption=body_txt or None)
                    elif kind == "video":
                        await bot.send_video(uid, file, caption=body_txt or None)
                    else:
                        await bot.send_document(uid, file, caption=body_txt or None)

                elif body_txt:
                    await bot.send_message(uid, body_txt)

                sent += 1
            except Exception as e:
                failed += 1
                print(f"[broadcast] send failed to {uid}: {e}")

        for uid in uids:
            await send_to(uid)

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
