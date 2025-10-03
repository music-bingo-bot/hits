import aiosqlite
import os
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

DB_PATH = os.path.join("uploads", "db.sqlite3")

async def init_db():
    os.makedirs("uploads", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS tracks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            hint_image TEXT,           -- путь к картинке-подсказке (uploads/hints/...)
            file_field TEXT NOT NULL    -- file_id TG или локальный путь uploads/audio/....
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            val TEXT
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,        -- telegram user id
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS admin_tokens(
            token TEXT PRIMARY KEY,
            user_id INTEGER,
            expires_at TEXT
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            text TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            sent_at TEXT
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broadcast_id INTEGER NOT NULL,
            kind TEXT NOT NULL,              -- image | video | file
            path TEXT NOT NULL,
            FOREIGN KEY(broadcast_id) REFERENCES broadcasts(id) ON DELETE CASCADE
        );
        """)
        await db.commit()

# ----- settings -----
async def get_setting(key:str)->Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT val FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

async def set_setting(key:str, val:str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO settings(key,val) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET val=excluded.val", (key,val))
        await db.commit()

# ----- users -----
async def save_user(uid:int, username:str|None, first:str|None, last:str|None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users(id, username, first_name, last_name)
            VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name
        """, (uid, username, first, last))
        await db.commit()

async def get_all_user_ids()->List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM users")
        return [r[0] for r in await cur.fetchall()]

# ----- admin tokens -----
async def create_admin_token(user_id:int, ttl_minutes:int=10)->str:
    import secrets
    token = secrets.token_urlsafe(24)
    exp = (datetime.utcnow() + timedelta(minutes=ttl_minutes)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO admin_tokens(token,user_id,expires_at) VALUES(?,?,?)", (token,user_id,exp))
        await db.commit()
    return token

async def pop_admin_token(token:str)->Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, expires_at FROM admin_tokens WHERE token=?", (token,))
        row = await cur.fetchone()
        if not row: return None
        user_id, exp = row
        await db.execute("DELETE FROM admin_tokens WHERE token=?", (token,))
        await db.commit()
        if exp and datetime.utcnow() > datetime.fromisoformat(exp):
            return None
        return user_id

# ----- tracks -----
async def get_all_tracks()->List[Tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, title, hint_image, file_field FROM tracks ORDER BY id ASC")
        return await cur.fetchall()

async def get_track_by_id(track_id:int)->Optional[Tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, title, hint_image, file_field FROM tracks WHERE id=?", (track_id,))
        return await cur.fetchone()

async def create_track(title:str, file_field:str, hint_image:str|None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO tracks(title, file_field, hint_image) VALUES(?,?,?)", (title, file_field, hint_image))
        await db.commit()

async def update_track(track_id:int, title:str, file_field:str|None, hint_image:str|None):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await get_track_by_id(track_id)
        if not row: return
        _, _, old_hint, old_file = row
        file_value = file_field if file_field else old_file
        hint_value = hint_image if hint_image else old_hint
        await db.execute("UPDATE tracks SET title=?, file_field=?, hint_image=? WHERE id=?",
                         (title, file_value, hint_value, track_id))
        await db.commit()

async def delete_track(track_id:int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tracks WHERE id=?", (track_id,))
        await db.commit()

# ----- broadcasts -----
async def broadcasts_all():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id,title,text,created_at,sent_at FROM broadcasts ORDER BY id DESC")
        return await cur.fetchall()

async def broadcast_create(title:str, text:str)->int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO broadcasts(title,text) VALUES(?,?)", (title,text))
        await db.commit()
        return cur.lastrowid

async def broadcast_add_media(bid:int, kind:str, path:str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO broadcast_media(broadcast_id,kind,path) VALUES(?,?,?)", (bid,kind,path))
        await db.commit()

async def broadcast_media(bid:int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT kind,path FROM broadcast_media WHERE broadcast_id=? ORDER BY id", (bid,))
        return await cur.fetchall()

async def broadcast_delete(bid:int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM broadcast_media WHERE broadcast_id=?", (bid,))
        await db.execute("DELETE FROM broadcasts WHERE id=?", (bid,))
        await db.commit()

async def broadcast_mark_sent(bid:int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE broadcasts SET sent_at=? WHERE id=?", (datetime.utcnow().isoformat(), bid))
        await db.commit()
