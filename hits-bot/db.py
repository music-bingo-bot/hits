# db.py
import os
import aiosqlite

DB_PATH = os.path.join("uploads", "db.sqlite3")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    -- раньше был hint TEXT, теперь храним путь к картинке
    hint_image TEXT,
    file_field TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS admin_tokens (
    token TEXT PRIMARY KEY,
    admin_id INTEGER NOT NULL,
    expires_at TIMESTAMP NOT NULL
);
"""

MIGRATIONS = [
    # добавим колонку hint_image, если её нет (на случай старой БД)
    ("ALTER TABLE tracks ADD COLUMN hint_image TEXT", "PRAGMA table_info(tracks)", "hint_image"),
]

async def init_db():
    os.makedirs("uploads", exist_ok=True)
    os.makedirs(os.path.join("uploads", "audio"), exist_ok=True)
    os.makedirs(os.path.join("uploads", "hints"), exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA_SQL)
        # простая миграция: добавить колонку hint_image при отсутствии
        cur = await db.execute("PRAGMA table_info(tracks)")
        cols = [row[1] for row in await cur.fetchall()]
        if "hint_image" not in cols:
            await db.execute("ALTER TABLE tracks ADD COLUMN hint_image TEXT")
        await db.commit()

async def get_all_tracks():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, title, hint_image, file_field FROM tracks ORDER BY id ASC")
        return await cur.fetchall()

async def get_track_by_id(track_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, title, hint_image, file_field FROM tracks WHERE id = ?", (track_id,))
        return await cur.fetchone()

async def create_track(title: str, file_field: str, hint_image: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO tracks (title, file_field, hint_image) VALUES (?, ?, ?)",
            (title, file_field, hint_image)
        )
        await db.commit()

async def update_track(track_id: int, title: str | None = None,
                       file_field: str | None = None, hint_image: str | None = None):
    sets, vals = [], []
    if title is not None:
        sets.append("title = ?"); vals.append(title)
    if file_field is not None:
        sets.append("file_field = ?"); vals.append(file_field)
    if hint_image is not None:
        sets.append("hint_image = ?"); vals.append(hint_image)
    if not sets:
        return
    vals.append(track_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE tracks SET {', '.join(sets)} WHERE id = ?", vals)
        await db.commit()

async def delete_track(track_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        await db.commit()

# settings
async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        await db.commit()

# admin tokens (как у тебя было)
import secrets, datetime
async def create_admin_token(admin_id: int, ttl_minutes: int = 10) -> str:
    token = secrets.token_urlsafe(24)
    expires = datetime.datetime.utcnow() + datetime.timedelta(minutes=ttl_minutes)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO admin_tokens(token, admin_id, expires_at) VALUES (?, ?, ?)",
                         (token, admin_id, expires.isoformat()))
        await db.commit()
    return token

async def pop_admin_token(token: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT admin_id, expires_at FROM admin_tokens WHERE token = ?", (token,))
        row = await cur.fetchone()
        if not row:
            return None
        admin_id, expires_at = row
        try:
            exp = datetime.datetime.fromisoformat(expires_at)
        except Exception:
            exp = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        await db.execute("DELETE FROM admin_tokens WHERE token = ?", (token,))
        await db.commit()
        if exp < datetime.datetime.utcnow():
            return None
        return admin_id

# users
async def save_user(uid: int, username: str | None, first: str | None, last: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users(user_id, username, first_name, last_name)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name,
              last_name=excluded.last_name
        """, (uid, username, first, last))
        await db.commit()
