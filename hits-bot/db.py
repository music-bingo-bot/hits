# db.py
import time
import secrets
import aiosqlite
from datetime import datetime, timedelta

DB_PATH = "uploads/db.sqlite3"

CREATE_SQL = '''
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    film_title TEXT NOT NULL,
    hint TEXT NOT NULL,            -- путь к картинке-подсказке
    file_id TEXT NOT NULL          -- file_id TG или путь к mp3 (uploads/audio/..)
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS broadcast_media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broadcast_id INTEGER REFERENCES broadcasts(id) ON DELETE CASCADE,
    kind TEXT CHECK(kind IN ('image','video','file')),
    path TEXT
);

CREATE TABLE IF NOT EXISTS broadcast_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broadcast_id INTEGER REFERENCES broadcasts(id) ON DELETE CASCADE,
    user_id INTEGER,
    ok INTEGER,
    error TEXT,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

-- Persisted cache of already-sanitized R2 audio keys (survives restarts/scaling)
CREATE TABLE IF NOT EXISTS r2_sanitized (
    key TEXT PRIMARY KEY,
    sanitized_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_broadcast_media_broadcast_id ON broadcast_media(broadcast_id);
CREATE INDEX IF NOT EXISTS idx_broadcasts_created_at ON broadcasts(COALESCE(sent_at, created_at));
CREATE INDEX IF NOT EXISTS idx_users_joined_at ON users(joined_at);
'''

# ---------- Init ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys = ON;")
        for stmt in CREATE_SQL.split(";"):
            if stmt.strip():
                await db.execute(stmt)
        await db.commit()

# ---------- Tracks ----------
async def add_track(film_title: str, hint: str, file_id: str) -> int:
    """Добавление трека (title, hint_image_path, audio_file_id_or_path)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            "INSERT INTO tracks (film_title, hint, file_id) VALUES (?, ?, ?)",
            (film_title, hint, file_id)
        )
        await db.commit()
        return cur.lastrowid

async def list_tracks(limit: int = 1000, offset: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            "SELECT id, film_title, hint FROM tracks ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset)
        )
        return await cur.fetchall()

async def get_all_tracks():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute("SELECT id, film_title, hint, file_id FROM tracks ORDER BY id")
        return await cur.fetchall()

async def get_track_by_id(track_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            "SELECT id, film_title, hint, file_id FROM tracks WHERE id=?",
            (track_id,)
        )
        return await cur.fetchone()

async def remove_track(track_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute("DELETE FROM tracks WHERE id=?", (track_id,))
        await db.commit()
        return cur.rowcount > 0

async def update_track(track_id: int, film_title: str, hint: str):
    """Обновить только название и путь к подсказке (картинке)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            "UPDATE tracks SET film_title=?, hint=? WHERE id=?",
            (film_title, hint, track_id)
        )
        await db.commit()

async def update_track_file(track_id: int, file_id: str):
    """Обновить audio (file_id или путь к mp3)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            "UPDATE tracks SET file_id=? WHERE id=?",
            (file_id, track_id)
        )
        await db.commit()

# ---------- Settings ----------
async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        await db.commit()

async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

# ---------- Admin tokens ----------
async def create_admin_token(user_id: int, ttl_minutes: int = 10) -> str:
    token = secrets.token_urlsafe(24)
    expires = int(time.time()) + ttl_minutes * 60
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            "INSERT INTO admin_tokens(token,user_id,expires_at) VALUES(?,?,?)",
            (token, user_id, expires)
        )
        await db.commit()
    return token

async def consume_admin_token(token: str) -> int | None:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            "SELECT user_id, expires_at FROM admin_tokens WHERE token=?",
            (token,)
        )
        row = await cur.fetchone()
        # одноразовое использование — удаляем в любом случае
        await db.execute("DELETE FROM admin_tokens WHERE token=?", (token,))
        await db.commit()

    if not row:
        return None
    user_id, exp = row
    if exp < now:
        return None
    return user_id

# Совместимость с названием, которое использует admin_web.py
pop_admin_token = consume_admin_token

# ---------- Users ----------
async def save_user(user_id: int, username: str | None, first_name: str | None, last_name: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            """INSERT INTO users (user_id, username, first_name, last_name)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 first_name=excluded.first_name,
                 last_name=excluded.last_name""",
            (user_id, username, first_name, last_name)
        )
        await db.commit()

async def get_all_user_ids() -> list[int]:
    """Для рассылок: список всех user_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
        return [r[0] for r in rows]

# ---------- Broadcasts ----------
async def create_broadcast(title: str, text: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            "INSERT INTO broadcasts (title, text) VALUES (?,?)",
            (title, text)
        )
        await db.commit()
        return cur.lastrowid

async def add_broadcast_media(bid: int, kind: str, path: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute(
            "INSERT INTO broadcast_media (broadcast_id, kind, path) VALUES (?,?,?)",
            (bid, kind, path)
        )
        await db.commit()

async def list_broadcasts():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            "SELECT id, title, text, created_at, sent_at "
            "FROM broadcasts "
            "ORDER BY COALESCE(sent_at, created_at) DESC"
        )
        return await cur.fetchall()

async def get_broadcast_media(bid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            "SELECT kind, path FROM broadcast_media WHERE broadcast_id=? ORDER BY id",
            (bid,)
        )
        return await cur.fetchall()

async def mark_broadcast_sent(bid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("UPDATE broadcasts SET sent_at=CURRENT_TIMESTAMP WHERE id=?", (bid,))
        await db.commit()

async def delete_broadcast(bid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("DELETE FROM broadcasts WHERE id=?", (bid,))
        await db.commit()

# ---------- R2 sanitized keys ----------
async def is_r2_key_sanitized(key: str) -> bool:
    if not key:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM r2_sanitized WHERE key=? LIMIT 1", (key,))
        row = await cur.fetchone()
        return bool(row)

async def mark_r2_key_sanitized(key: str):
    if not key:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO r2_sanitized(key) VALUES(?) ON CONFLICT(key) DO NOTHING",
            (key,)
        )
        await db.commit()

# ---------- Совместимость со «старыми» именами ----------
# Треки
create_track   = add_track
delete_track   = remove_track

# Рассылки
broadcasts_all      = list_broadcasts
broadcast_create    = create_broadcast
broadcast_add_media = add_broadcast_media
broadcast_media     = get_broadcast_media
broadcast_mark_sent = mark_broadcast_sent
broadcast_delete    = delete_broadcast
