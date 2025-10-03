import os, uuid, shutil
from typing import Optional

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from db import get_all_tracks, insert_track, update_track, delete_track, validate_admin_token

env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html","xml","jinja2"]),
)

def _save_upload(upload: Optional[UploadFile], subdir: str) -> Optional[str]:
    if not upload or not getattr(upload, "filename", None):
        return None
    os.makedirs(f"uploads/{subdir}", exist_ok=True)
    ext = os.path.splitext(upload.filename)[1] or ""
    name = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join("uploads", subdir, name)
    with open(path, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return path

def create_app(bot) -> FastAPI:
    app = FastAPI()
    app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

    @app.middleware("http")
    async def _login_gate(req, call_next):
        if str(req.url.path).startswith("/uploads") or req.url.path in ("/admin_web/login","/admin_web/logout","/health","/healthz","/"):
            return await call_next(req)
        if req.url.path.startswith("/admin_web"):
            secret = os.getenv("SESSION_SECRET","")
            if req.cookies.get("auth") == secret:
                return await call_next(req)
            key = req.query_params.get("key")
            if key:
                uid = await validate_admin_token(key)
                if uid:
                    resp = RedirectResponse(url="/admin_web")
                    resp.set_cookie("auth", secret, httponly=True, max_age=3600)
                    return resp
            if req.method == "GET":
                tpl = env.get_template("login.html")
                return HTMLResponse(tpl.render(error=None))
        return await call_next(req)

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return RedirectResponse(url="/admin_web")

    @app.get("/admin_web", response_class=HTMLResponse)
    async def admin_index():
        tracks = await get_all_tracks()
        tpl = env.get_template("index.html")
        return HTMLResponse(tpl.render(tracks=tracks))

    @app.get("/admin_web/login", response_class=HTMLResponse)
    async def login_page():
        tpl = env.get_template("login.html")
        return HTMLResponse(tpl.render(error=None))

    @app.post("/admin_web/login", response_class=HTMLResponse)
    async def do_login(password: str = Form(...)):
        secret = os.getenv("SESSION_SECRET","")
        if secret and password == secret:
            resp = RedirectResponse(url="/admin_web", status_code=302)
            resp.set_cookie("auth", secret, httponly=True, max_age=3600)
            return resp
        tpl = env.get_template("login.html")
        return HTMLResponse(tpl.render(error="Неверный пароль"))

    @app.post("/admin_web/logout")
    async def do_logout():
        resp = RedirectResponse(url="/admin_web/login", status_code=302)
        resp.delete_cookie("auth")
        return resp

    @app.post("/admin_web/tracks/create")
    async def create_track(
        title: str = Form(...),
        hint_text: str = Form(None),
        audio: UploadFile = File(...),
        hint_image: UploadFile = File(None),
    ):
        audio_path = _save_upload(audio, "audio")
        hint_img_path = _save_upload(hint_image, "hints") if hint_image else None
        if not audio_path:
            return PlainTextResponse("Не загружен файл аудио", status_code=400)
        from db import insert_track
        await insert_track(title.strip(), hint_text, audio_path, hint_img_path)
        return RedirectResponse(url="/admin_web", status_code=302)

    @app.post("/admin_web/tracks/{tid}/edit")
    async def edit_track(
        tid: int,
        title: str = Form(None),
        hint_text: str = Form(None),
        audio: UploadFile = File(None),
        hint_image: UploadFile = File(None),
    ):
        new_audio = _save_upload(audio, "audio") if audio and audio.filename else None
        new_hint_img = _save_upload(hint_image, "hints") if hint_image and hint_image.filename else None
        await update_track(
            tid,
            title=title.strip() if title is not None else None,
            hint=hint_text,
            file_field=new_audio,
            hint_image=new_hint_img
        )
        return RedirectResponse(url="/admin_web", status_code=302)

    @app.post("/admin_web/tracks/{tid}/delete")
    async def remove_track(tid: int):
        await delete_track(tid)
        return RedirectResponse(url="/admin_web", status_code=302)

    return app
