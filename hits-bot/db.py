import os, time, secrets, string
import aiosqlite

def _now_ts() -> int:
    return int(time.time())

async def init_db():
    os.makedirs("uploads", exist_ok=True)
    os.makedirs("uploads/audio", exist_ok=True)
    os.makedirs("uploads/hints", exist_ok=True)
    async with aiosqlite.connect("uploads/db.sqlite3") as db:
        await db.execute("""            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                film_title TEXT NOT NULL,
                hint TEXT,
                file_field TEXT NOT NULL,
                hint_image TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""            CREATE TABLE IF NOT EXISTS admin_tokens (
                token TEXT PRIMARY KEY,
                user_id INTEGER,
                expires_at INTEGER
            )
        """)
        await db.execute("""            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def save_user(user_id: int, username: str|None, first_name: str|None, last_name: str|None):
    async with aiosqlite.connect("uploads/db.sqlite3") as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
            (user_id, username, first_name, last_name)
        )
        await db.commit()

async def get_all_tracks():
    async with aiosqlite.connect("uploads/db.sqlite3") as db:
        cur = await db.execute("SELECT id, film_title, hint, file_field, hint_image FROM tracks ORDER BY id")
        return await cur.fetchall()

async def get_track_by_id(tid: int):
    async with aiosqlite.connect("uploads/db.sqlite3") as db:
        cur = await db.execute("SELECT id, film_title, hint, file_field, hint_image FROM tracks WHERE id = ?", (tid,))
        return await cur.fetchone()

async def insert_track(title: str, hint: str|None, file_field: str, hint_image: str|None):
    async with aiosqlite.connect("uploads/db.sqlite3") as db:
        await db.execute(
            "INSERT INTO tracks (film_title, hint, file_field, hint_image) VALUES (?, ?, ?, ?)",
            (title, hint, file_field, hint_image)
        )
        await db.commit()

async def update_track(tid: int, title: str|None=None, hint: str|None=None,
                       file_field: str|None=None, hint_image: str|None=None):
    fields, vals = [], []
    if title is not None:      fields.append("film_title = ?"); vals.append(title)
    if hint is not None:       fields.append("hint = ?"); vals.append(hint)
    if file_field is not None: fields.append("file_field = ?"); vals.append(file_field)
    if hint_image is not None: fields.append("hint_image = ?"); vals.append(hint_image)
    if not fields:
        return
    vals.append(tid)
    async with aiosqlite.connect("uploads/db.sqlite3") as db:
        await db.execute(f"UPDATE tracks SET {', '.join(fields)} WHERE id = ?", vals)
        await db.commit()

async def delete_track(tid: int):
    async with aiosqlite.connect("uploads/db.sqlite3") as db:
        await db.execute("DELETE FROM tracks WHERE id = ?", (tid,))
        await db.commit()

async def set_setting(key: str, value: str|None):
    async with aiosqlite.connect("uploads/db.sqlite3") as db:
        if value is None:
            await db.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def get_setting(key: str) -> str|None:
    async with aiosqlite.connect("uploads/db.sqlite3") as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

def _rand_token(n=40):
    alphabet = string.ascii_letters + string.digits
    import random
    return ''.join(random.choice(alphabet) for _ in range(n))

async def create_admin_token(user_id: int, ttl_minutes: int = 10) -> str:
    token = _rand_token(48)
    expires = _now_ts() + ttl_minutes * 60
    async with aiosqlite.connect("uploads/db.sqlite3") as db:
        await db.execute("INSERT INTO admin_tokens (token, user_id, expires_at) VALUES (?, ?, ?)", (token, user_id, expires))
        await db.commit()
    return token

async def validate_admin_token(token: str) -> int|None:
    now = _now_ts()
    async with aiosqlite.connect("uploads/db.sqlite3") as db:
        cur = await db.execute("SELECT user_id FROM admin_tokens WHERE token = ? AND expires_at >= ?", (token, now))
        row = await cur.fetchone()
        return row[0] if row else None
