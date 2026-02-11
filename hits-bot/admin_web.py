# admin_web.py
import os
import tempfile
import zipfile
from typing import List, Optional

from fastapi import FastAPI, Request, UploadFile, Form, File
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    PlainTextResponse,
    FileResponse,
)
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import aiofiles

# для рассылок и треков
from db import (
    get_all_tracks,
    get_track_by_id,
    create_track,
    update_track,
    delete_track,  # алиас
    update_track_file,
    create_admin_token,
    consume_admin_token,
    broadcasts_all,
    broadcast_create,
    broadcast_add_media,
    broadcast_media,
    broadcast_delete,
    broadcast_mark_sent,
    get_all_user_ids,
)

from r2_storage import (
    r2_enabled,
    build_r2_key,
    put_bytes_to_r2,
    overwrite_bytes_in_r2,
    presign_get_url,
    normalize_r2_audio_ref,
    normalize_r2_hint_ref,
)

TEMPLATES = Jinja2Templates(directory="templates")


# ---- утилита: зачистка ID3 у mp3 ----
def _strip_id3_safe(path: str) -> None:
    """
    Удаляем ID3-теги, чтобы Telegram не подставлял
    «Исполнитель – Название» из метаданных.
    Если библиотека/файл недоступны — тихо пропускаем.
    """
    try:
        from mutagen import File
        from mutagen.id3 import ID3, ID3NoHeaderError

        if not os.path.exists(path):
            return

        try:
            tags = ID3(path)
            tags.delete(path)
        except ID3NoHeaderError:
            pass

        mf = File(path)
        if mf is not None and mf.tags is not None:
            mf.delete()
            mf.save()
    except Exception:
        pass


def create_app(bot):
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.getenv("SESSION_SECRET", "secret"),
    )

    # раздача /uploads (аудио/картинки/БД) как статики (legacy + DB)
    os.makedirs("uploads", exist_ok=True)
    app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

    # ---------- R2 proxy (для админки/браузера: <img src="/r2/...">) ----------
    @app.get("/r2/{key:path}")
    async def r2_proxy(key: str):
        # безопасность: не даём "вылезти" из пути
        if not key or ".." in key or key.startswith("/"):
            return PlainTextResponse("bad key", status_code=400)
        if not r2_enabled():
            return PlainTextResponse("r2 disabled", status_code=404)

        try:
            url = presign_get_url(key, expires_seconds=3600)
            return RedirectResponse(url, status_code=302)
        except Exception:
            return PlainTextResponse("not found", status_code=404)

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
        return TEMPLATES.TemplateResponse(
            "tracks.html",
            {"request": request, "items": items},
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

        # следующий номер
        items = await get_all_tracks()
        seq = len(items) + 1

        # имя MP3 — с длинным тире (как было)
        seq_name = f"Музыкальное бинго — {seq:02d}.mp3"

        audio_field = ""
        if audio and audio.filename:
            if r2_enabled():
                # читаем в память через temp-file (чтобы mutagen зачистил)
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
                tmp_path = tmp.name
                tmp.close()

                try:
                    async with aiofiles.open(tmp_path, "wb") as f:
                        while chunk := await audio.read(64 * 1024):
                            await f.write(chunk)
                    _strip_id3_safe(tmp_path)

                    async with aiofiles.open(tmp_path, "rb") as f:
                        content = await f.read()

                    key = build_r2_key("audio", f"track_{seq:02d}.mp3")
                    put_bytes_to_r2(content, key, content_type="audio/mpeg")
                    audio_field = f"r2:{key}"
                finally:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
            else:
                # legacy local
                os.makedirs("uploads/audio", exist_ok=True)
                audio_path = os.path.join("uploads", "audio", seq_name)
                async with aiofiles.open(audio_path, "wb") as f:
                    while chunk := await audio.read(64 * 1024):
                        await f.write(chunk)
                _strip_id3_safe(audio_path)
                audio_field = audio_path

        hint_field = ""
        if hint and hint.filename:
            if r2_enabled():
                ext = os.path.splitext(hint.filename)[1] or ".jpg"
                data = await hint.read()
                key = build_r2_key("hints", f"hint_{seq:02d}{ext}")
                put_bytes_to_r2(data, key, content_type=getattr(hint, "content_type", None) or None)

                # ВАЖНО: именно так, чтобы шаблон <img src="/{{ hint_field }}"> работал
                hint_field = f"r2/{key}"
            else:
                os.makedirs("uploads/hints", exist_ok=True)
                ext = os.path.splitext(hint.filename)[1] or ".jpg"
                hint_path = os.path.join("uploads", "hints", f"hint_{seq:02d}{ext}")
                async with aiofiles.open(hint_path, "wb") as f:
                    fchunk = await hint.read()
                    await f.write(fchunk)
                hint_field = hint_path

        await create_track(title or f"Хит #{seq:02d}", hint_field or "", audio_field or "")
        return RedirectResponse("/admin_web", status_code=302)

    @app.get("/admin_web/edit/{track_id}", response_class=HTMLResponse)
    async def edit_track_page(request: Request, track_id: int):
        guard = _need_auth(request)
        if guard:
            return guard
        row = await get_track_by_id(track_id)
        return TEMPLATES.TemplateResponse(
            "edit_track.html",
            {"request": request, "row": row},
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

        old = await get_track_by_id(track_id)
        old_title = old[1] if old else ""
        old_hint = old[2] if old else ""
        old_audio = old[3] if old else ""

        # заменить картинку-подсказку
        hint_field = None
        if hint and hint.filename:
            if r2_enabled():
                ext = os.path.splitext(hint.filename)[1] or ".jpg"
                data = await hint.read()

                existing_key = normalize_r2_hint_ref(old_hint or "")
                key = existing_key or build_r2_key("hints", f"hint_{track_id:02d}{ext}")
                overwrite_bytes_in_r2(data, key, content_type=getattr(hint, "content_type", None) or None)

                hint_field = f"r2/{key}"
            else:
                os.makedirs("uploads/hints", exist_ok=True)
                ext = os.path.splitext(hint.filename)[1] or ".jpg"
                hint_path = os.path.join("uploads", "hints", f"hint_{track_id:02d}{ext}")
                async with aiofiles.open(hint_path, "wb") as f:
                    while chunk := await hint.read(64 * 1024):
                        await f.write(chunk)
                hint_field = hint_path

        # заменить аудио
        audio_field = None
        if audio and audio.filename:
            if r2_enabled():
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
                tmp_path = tmp.name
                tmp.close()

                try:
                    async with aiofiles.open(tmp_path, "wb") as f:
                        while chunk := await audio.read(64 * 1024):
                            await f.write(chunk)
                    _strip_id3_safe(tmp_path)

                    async with aiofiles.open(tmp_path, "rb") as f:
                        content = await f.read()

                    existing_key = normalize_r2_audio_ref(old_audio or "")
                    key = existing_key or build_r2_key("audio", f"track_{track_id:02d}.mp3")
                    overwrite_bytes_in_r2(content, key, content_type="audio/mpeg")

                    audio_field = f"r2:{key}"
                finally:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
            else:
                os.makedirs("uploads/audio", exist_ok=True)
                audio_path = os.path.join(
                    "uploads",
                    "audio",
                    f"Музыкальное бинго — {track_id:02d}.mp3",
                )
                async with aiofiles.open(audio_path, "wb") as f:
                    while chunk := await audio.read(64 * 1024):
                        await f.write(chunk)
                _strip_id3_safe(audio_path)
                audio_field = audio_path

        # обновляем БД
        if title or hint_field is not None:
            new_hint = hint_field if hint_field is not None else (old_hint or "")
            await update_track(track_id, title or (old_title or ""), new_hint)
        if audio_field is not None:
            await update_track_file(track_id, audio_field)

        return RedirectResponse("/admin_web", status_code=302)

    @app.post("/admin_web/delete/{track_id}")
    async def delete_track_post(request: Request, track_id: int):
        guard = _need_auth(request)
        if guard:
            return guard
        await delete_track(track_id)
        return RedirectResponse("/admin_web", status_code=302)

    # ---------- BACKUP / RESTORE ----------
    @app.get("/admin_web/backup")
    async def backup_download(request: Request):
        guard = _need_auth(request)
        if guard:
            return guard

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        tmp.close()
        zip_path = tmp.name

        base = "uploads"
        os.makedirs(base, exist_ok=True)

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            if os.path.isdir(base):
                for root, _dirs, files in os.walk(base):
                    for name in files:
                        full = os.path.join(root, name)
                        arc = os.path.relpath(full, start=os.path.dirname(base))
                        zf.write(full, arcname=arc)

        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename="backup_uploads.zip",
        )

    @app.post("/admin_web/restore")
    async def backup_restore(request: Request, archive: UploadFile):
        guard = _need_auth(request)
        if guard:
            return guard

        if not archive or not archive.filename:
            return RedirectResponse("/admin_web?restore=missing", status_code=302)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        async with aiofiles.open(tmp.name, "wb") as f:
            while chunk := await archive.read(64 * 1024):
                await f.write(chunk)

        base = "uploads"
        os.makedirs(base, exist_ok=True)

        with zipfile.ZipFile(tmp.name, "r") as zf:
            for member in zf.infolist():
                target = os.path.normpath(os.path.join(".", member.filename))
                if not target.startswith(("uploads", "./uploads")):
                    continue
                zf.extract(member, ".")

        try:
            os.remove(tmp.name)
        except Exception:
            pass

        return RedirectResponse("/admin_web?restore=ok", status_code=302)

    # ---------- broadcasts ----------
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
        return TEMPLATES.TemplateResponse(
            "broadcasts_new.html",
            {"request": request},
        )

    @app.post("/admin_web/broadcasts/preview", response_class=HTMLResponse)
    async def broadcasts_preview(
        request: Request,
        title: str = Form(""),
        text: str = Form(""),
        images: List[UploadFile] = File(default=[]),
        videos: List[UploadFile] = File(default=[]),
        files: List[UploadFile] = File(default=[]),
    ):
        guard = _need_auth(request)
        if guard:
            return guard

        bid = await broadcast_create(title, text)
        os.makedirs("uploads/broadcasts", exist_ok=True)

        async def _save_many(lst, kind: str):
            for up in lst or []:
                if not up or not up.filename:
                    continue
                path = os.path.join("uploads", "broadcasts", up.filename)
                async with aiofiles.open(path, "wb") as f:
                    while chunk := await up.read(64 * 1024):
                        await f.write(chunk)
                await broadcast_add_media(bid, kind, path)

        await _save_many(images, "image")
        await _save_many(videos, "video")
        await _save_many(files, "file")

        media = await broadcast_media(bid)
        imgs = [p for (k, p) in media if k == "image"]
        vids = [p for (k, p) in media if k == "video"]
        fils = [p for (k, p) in media if k == "file"]

        return TEMPLATES.TemplateResponse(
            "broadcasts_preview.html",
            {
                "request": request,
                "id": bid,
                "title": title,
                "text": text,
                "images": imgs,
                "videos": vids,
                "files": fils,
            },
        )

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

        from aiogram.types import (
            FSInputFile as TgFSInputFile,
            InputMediaPhoto,
            InputMediaVideo,
            InputMediaDocument,
        )

        album = []
        for kind, path in media[:10]:
            if kind == "image":
                album.append(InputMediaPhoto(media=TgFSInputFile(path)))
            elif kind == "video":
                album.append(InputMediaVideo(media=TgFSInputFile(path)))
            else:
                album.append(InputMediaDocument(media=TgFSInputFile(path)))

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

    # ---------- health ----------
    @app.api_route("/health", methods=["GET", "HEAD"])
    async def _health_edge():
        return PlainTextResponse("ok")

    @app.get("/healthz")
    async def _healthz():
        return PlainTextResponse("ok")

    return app
