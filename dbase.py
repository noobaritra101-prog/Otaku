import asyncio
import logging
import sqlite3
import time

import aiosqlite
from configaration import DB_PATH

log = logging.getLogger("otaku.db")
_db: aiosqlite.Connection | None = None
nex_lock = asyncio.Lock()    # serialises Nex transfers / bets across modules

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    nex         INTEGER NOT NULL DEFAULT 0,
    favorite_id INTEGER,
    last_daily  INTEGER NOT NULL DEFAULT 0,
    joined_at   INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT,
    msg_count   INTEGER NOT NULL DEFAULT 0,
    drop_every  INTEGER NOT NULL DEFAULT 100,
    drop_enabled INTEGER NOT NULL DEFAULT 1,
    added_at    INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS characters (
    char_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    anime     TEXT NOT NULL,
    gender    TEXT NOT NULL CHECK (gender IN ('waifu','husbando')),
    rarity    TEXT NOT NULL,
    file_id   TEXT NOT NULL,
    added_by  INTEGER,
    added_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    sub_rarity  TEXT,
    artist_name TEXT,
    db_msg_id   INTEGER,
    media_type  TEXT NOT NULL DEFAULT 'photo'
);

CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS collection (
    user_id   INTEGER NOT NULL,
    char_id   INTEGER NOT NULL,
    qty       INTEGER NOT NULL DEFAULT 1,
    first_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (user_id, char_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS shop_offers (
    day     TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    char_id INTEGER NOT NULL,
    rarity  TEXT NOT NULL,
    price   INTEGER NOT NULL,
    bought  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id, char_id)
);

CREATE TABLE IF NOT EXISTS drops (
    chat_id    INTEGER PRIMARY KEY,
    char_id    INTEGER NOT NULL,
    message_id INTEGER,
    dropped_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS whoami_stats (
    user_id INTEGER PRIMARY KEY,
    games   INTEGER NOT NULL DEFAULT 0,
    best    REAL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS whoami_wins (
    user_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    wins    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
) WITHOUT ROWID;
"""

INDEXES = "CREATE INDEX IF NOT EXISTS idx_char_rarity ON characters(rarity);"


async def _add_missing_columns() -> None:
    """Old databases: add every column the current layout has and the old table lacks."""
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(SCHEMA)
        tables = [r[0] for r in ref.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            want = ref.execute(f"PRAGMA table_info({t})").fetchall()      # cid, name, type, notnull, dflt, pk
            async with _db.execute(f"PRAGMA table_info({t})") as cur:
                have = {r["name"] for r in await cur.fetchall()}
            for _cid, name, typ, notnull, dflt, pk in want:
                if name in have or pk:
                    continue
                if dflt is not None and "(" not in str(dflt):
                    default = f" DEFAULT {dflt}"
                elif notnull or dflt is not None:                          # NOT NULL / expression default
                    default = " DEFAULT ''" if "CHAR" in typ.upper() or "TEXT" in typ.upper() else " DEFAULT 0"
                else:
                    default = ""
                null = " NOT NULL" if notnull else ""
                try:
                    await _db.execute(f"ALTER TABLE {t} ADD COLUMN {name} {typ}{null}{default}")
                    log.info("migrated: added %s.%s", t, name)
                except Exception:
                    log.exception("could not add column %s.%s", t, name)
    finally:
        ref.close()


async def init_db() -> None:
    global _db
    if _db is not None:                      # never leave an old connection open behind a new one
        try:
            await _db.close()
        except Exception:
            pass
        _db = None
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA synchronous=NORMAL")
    await _db.execute("PRAGMA temp_store=MEMORY")
    await _db.execute("PRAGMA cache_size=-65536")     # 64 MB
    await _db.execute("PRAGMA mmap_size=268435456")   # 256 MB
    await _db.execute("PRAGMA foreign_keys=ON")
    await _db.executescript(SCHEMA)
    # migrate older databases: users.coins -> users.nex
    async with _db.execute("PRAGMA table_info(users)") as cur:
        cols = {r["name"] for r in await cur.fetchall()}
    if "coins" in cols and "nex" not in cols:
        await _db.execute("ALTER TABLE users RENAME COLUMN coins TO nex")
    if "favorite_id" not in cols:
        await _db.execute("ALTER TABLE users ADD COLUMN favorite_id INTEGER")
    if "last_daily" not in cols:
        await _db.execute("ALTER TABLE users ADD COLUMN last_daily INTEGER NOT NULL DEFAULT 0")
    # migrate older databases: characters.sub_rarity / artist_name / db_msg_id
    async with _db.execute("PRAGMA table_info(characters)") as cur:
        ccols = {r["name"] for r in await cur.fetchall()}
    for col, typ in (("sub_rarity", "TEXT"), ("artist_name", "TEXT"), ("db_msg_id", "INTEGER"),
                     ("media_type", "TEXT NOT NULL DEFAULT 'photo'")):
        if col not in ccols:
            await _db.execute(f"ALTER TABLE characters ADD COLUMN {col} {typ}")
    # migrate older databases: chats.drop_enabled
    async with _db.execute("PRAGMA table_info(chats)") as cur:
        chcols = {r["name"] for r in await cur.fetchall()}
    if "drop_enabled" not in chcols:
        await _db.execute("ALTER TABLE chats ADD COLUMN drop_enabled INTEGER NOT NULL DEFAULT 1")
    await _add_missing_columns()
    try:
        await _db.executescript(INDEXES)
    except Exception:
        log.exception("could not create indexes")
    await _db.commit()


async def close_db() -> None:
    global _db
    if _db:
        db, _db = _db, None
        await db.close()


# ---------- settings ----------
async def get_setting(key: str) -> str | None:
    async with _db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
        r = await cur.fetchone()
    return r[0] if r else None


async def set_setting(key: str, value: str) -> None:
    await _db.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    await _db.commit()


# ---------- users / chats ----------
async def upsert_user(user_id: int, username: str | None, first_name: str) -> None:
    await _db.execute(
        """INSERT INTO users (user_id, username, first_name) VALUES (?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                                              first_name=excluded.first_name""",
        (user_id, username, first_name),
    )
    await _db.commit()


async def upsert_chat(chat_id: int, title: str | None) -> None:
    await _db.execute(
        """INSERT INTO chats (chat_id, title) VALUES (?,?)
           ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title""",
        (chat_id, title),
    )
    await _db.commit()


async def stats() -> tuple[int, int, int]:
    async with _db.execute(
        "SELECT (SELECT COUNT(*) FROM users), (SELECT COUNT(*) FROM chats), (SELECT COUNT(*) FROM characters)"
    ) as cur:
        r = await cur.fetchone()
    return r[0], r[1], r[2]


# ---------- currency (Nex) ----------
async def get_nex(user_id: int) -> int:
    async with _db.execute("SELECT nex FROM users WHERE user_id=?", (user_id,)) as cur:
        r = await cur.fetchone()
    return r[0] if r else 0


async def add_nex(user_id: int, amount: int) -> int:
    """Add (or subtract with a negative amount) Nex. Balance never drops below 0. Returns new balance."""
    await _db.execute(
        "UPDATE users SET nex = MAX(0, nex + ?) WHERE user_id=?", (amount, user_id))
    await _db.commit()
    return await get_nex(user_id)


async def claim_daily(user_id: int, amount: int, cooldown: int) -> tuple[bool, int, int]:
    """Atomic daily claim. Returns (claimed, balance, seconds_left). The user row must exist."""
    now = int(time.time())
    cur = await _db.execute(
        "UPDATE users SET nex = nex + ?, last_daily = ? WHERE user_id=? AND last_daily <= ?",
        (amount, now, user_id, now - cooldown))
    await _db.commit()
    if cur.rowcount:
        return True, await get_nex(user_id), 0
    async with _db.execute("SELECT last_daily FROM users WHERE user_id=?", (user_id,)) as c:
        r = await c.fetchone()
    left = max(1, (r[0] if r else 0) + cooldown - now)
    return False, await get_nex(user_id), left


# ---------- admins ----------
async def is_admin(user_id: int) -> bool:
    async with _db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)) as cur:
        return await cur.fetchone() is not None


async def list_admins() -> list[int]:
    async with _db.execute("SELECT user_id FROM admins ORDER BY user_id") as cur:
        return [r[0] for r in await cur.fetchall()]


async def get_user_name(user_id: int) -> str | None:
    async with _db.execute("SELECT first_name FROM users WHERE user_id=?", (user_id,)) as cur:
        r = await cur.fetchone()
    return r[0] if r else None


async def add_admin(user_id: int) -> None:
    await _db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
    await _db.commit()


async def del_admin(user_id: int) -> None:
    await _db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    await _db.commit()


# ---------- characters ----------
CHAR_FIELDS = {"name", "anime", "gender", "rarity", "sub_rarity", "file_id", "db_msg_id", "media_type"}


async def add_character(name: str, anime: str, gender: str, rarity: str, sub_rarity: str | None,
                        file_id: str, added_by: int, artist_name: str,
                        media_type: str = "photo", char_id: int | None = None) -> int:
    """Insert a card and return its ID. AUTOINCREMENT keeps counting and never reuses IDs, unless char_id is given:
    then the card takes that free ID (fills a gap, e.g. after a delete). A taken ID falls back to the next new one."""
    vals = (name, anime, gender, rarity, sub_rarity, file_id, added_by, artist_name, media_type)
    if char_id is not None:
        try:
            await _db.execute(
                "INSERT INTO characters (char_id, name, anime, gender, rarity, sub_rarity, file_id, added_by, artist_name, media_type)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)", (char_id, *vals))
            await _db.commit()
            return char_id
        except aiosqlite.IntegrityError:
            await _db.rollback()
    cur = await _db.execute(
        "INSERT INTO characters (name, anime, gender, rarity, sub_rarity, file_id, added_by, artist_name, media_type)"
        " VALUES (?,?,?,?,?,?,?,?,?)", vals)
    await _db.commit()
    return cur.lastrowid


async def get_character(char_id: int):
    async with _db.execute("SELECT * FROM characters WHERE char_id=?", (char_id,)) as cur:
        return await cur.fetchone()


async def update_character(char_id: int, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in CHAR_FIELDS}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    await _db.execute(f"UPDATE characters SET {sets} WHERE char_id=?", (*fields.values(), char_id))
    await _db.commit()


async def count_characters() -> int:
    async with _db.execute("SELECT COUNT(*) FROM characters") as cur:
        return (await cur.fetchone())[0]


async def list_characters(offset: int = 0, limit: int = 15):
    """Newest first (highest ID first), for the /uploaded list."""
    async with _db.execute(
        "SELECT char_id, name, anime, rarity, sub_rarity FROM characters "
        "ORDER BY char_id DESC LIMIT ? OFFSET ?", (limit, offset)) as cur:
        return await cur.fetchall()


async def delete_character(char_id: int) -> None:
    await _db.execute("DELETE FROM collection WHERE char_id=?", (char_id,))
    await _db.execute("DELETE FROM drops WHERE char_id=?", (char_id,))
    await _db.execute("DELETE FROM characters WHERE char_id=?", (char_id,))
    await _db.commit()


# ---------- drops ----------
async def get_chat(chat_id: int):
    async with _db.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)) as cur:
        return await cur.fetchone()


async def set_chat_drop(chat_id: int, every: int | None = None, enabled: bool | None = None) -> None:
    """Change a group's drop interval and / or on-off switch (creates the chat row if missing)."""
    await _db.execute("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
    if every is not None:
        await _db.execute("UPDATE chats SET drop_every=? WHERE chat_id=?", (every, chat_id))
    if enabled is not None:
        await _db.execute("UPDATE chats SET drop_enabled=? WHERE chat_id=?", (int(enabled), chat_id))
    await _db.commit()


async def set_chat_count(chat_id: int, count: int) -> None:
    await _db.execute("UPDATE chats SET msg_count=? WHERE chat_id=?", (count, chat_id))
    await _db.commit()


async def pick_character(rarity: str, exclude_id: int | None = None, blocked_subs=()):
    """One random card of a main rarity, skipping blocked sub rarities (event cards) and one char_id."""
    sql = "SELECT * FROM characters WHERE rarity=?"
    args: list = [rarity]
    if blocked_subs:
        sql += f" AND (sub_rarity IS NULL OR sub_rarity NOT IN ({','.join('?' * len(blocked_subs))}))"
        args += list(blocked_subs)
    if exclude_id:
        sql += " AND char_id != ?"
        args.append(exclude_id)
    sql += " ORDER BY RANDOM() LIMIT 1"
    async with _db.execute(sql, args) as cur:
        return await cur.fetchone()


async def get_drop(chat_id: int):
    async with _db.execute("SELECT * FROM drops WHERE chat_id=?", (chat_id,)) as cur:
        return await cur.fetchone()


async def swap_drop(chat_id: int, old_char_id: int | None, char_id: int, message_id: int) -> bool:
    """Make a new card the live drop. Returns True if the old drop was still unclaimed (so it ran away)."""
    replaced = False
    if old_char_id is not None:
        cur = await _db.execute("DELETE FROM drops WHERE chat_id=? AND char_id=?", (chat_id, old_char_id))
        replaced = cur.rowcount == 1
    await _db.execute(
        "INSERT OR REPLACE INTO drops (chat_id, char_id, message_id, dropped_at) VALUES (?,?,?,?)",
        (chat_id, char_id, message_id, int(time.time())))
    await _db.commit()
    return replaced


async def claim_drop(chat_id: int, char_id: int) -> bool:
    """Atomic: only the first caller gets True, the drop row is gone for everyone else."""
    cur = await _db.execute("DELETE FROM drops WHERE chat_id=? AND char_id=?", (chat_id, char_id))
    await _db.commit()
    return cur.rowcount == 1


async def add_to_collection(user_id: int, char_id: int) -> int:
    """Add one copy to a user's collection. Returns the new quantity (1 = new entry)."""
    await _db.execute(
        "INSERT INTO collection (user_id, char_id, qty) VALUES (?,?,1) "
        "ON CONFLICT(user_id, char_id) DO UPDATE SET qty = qty + 1", (user_id, char_id))
    await _db.commit()
    async with _db.execute("SELECT qty FROM collection WHERE user_id=? AND char_id=?", (user_id, char_id)) as cur:
        return (await cur.fetchone())[0]


async def search_characters(query: str = "", offset: int = 0, limit: int = 50):
    """Inline mode: all cards (newest first), or filtered by name / series / rarity / sub rarity / ID."""
    sql, args = "SELECT * FROM characters", []
    q = query.strip()
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cond = " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in ("name", "anime", "rarity", "sub_rarity"))
        args = [like] * 4
        if q.isdigit():
            cond += " OR char_id = ?"
            args.append(int(q))
        sql += f" WHERE {cond}"
    sql += " ORDER BY char_id DESC LIMIT ? OFFSET ?"
    async with _db.execute(sql, (*args, limit, offset)) as cur:
        return await cur.fetchall()


async def top_holders(char_id: int, limit: int = 10):
    """Users who own a card: most copies first, then whoever bound it earliest."""
    async with _db.execute(
        "SELECT u.user_id, u.username, u.first_name, c.qty FROM collection c "
        "JOIN users u ON u.user_id = c.user_id WHERE c.char_id=? "
        "ORDER BY c.qty DESC, c.first_at ASC LIMIT ?", (char_id, limit)) as cur:
        return await cur.fetchall()


# ---------- harem (collection view) ----------
_OWNED = "FROM collection c JOIN characters ch ON ch.char_id = c.char_id WHERE c.user_id=?"


async def collection_count(user_id: int, rarity: str | None = None) -> int:
    sql, args = f"SELECT COUNT(*) {_OWNED}", [user_id]
    if rarity:
        sql += " AND ch.rarity=?"
        args.append(rarity)
    async with _db.execute(sql, args) as cur:
        return (await cur.fetchone())[0]


async def collection_stats(user_id: int) -> tuple[int, int]:
    """(unique characters, total copies)."""
    async with _db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(c.qty), 0) {_OWNED}", (user_id,)) as cur:
        r = await cur.fetchone()
    return r[0], r[1]


async def collection_rarities(user_id: int) -> set[str]:
    async with _db.execute(f"SELECT DISTINCT ch.rarity {_OWNED}", (user_id,)) as cur:
        return {r[0] for r in await cur.fetchall()}


async def collection_page(user_id: int, rarity: str | None = None, offset: int = 0, limit: int = 10):
    """A user's characters, most recently bound first."""
    sql = f"SELECT ch.char_id, ch.name, ch.anime, ch.rarity, ch.sub_rarity, ch.file_id, ch.media_type, c.qty {_OWNED}"
    args: list = [user_id]
    if rarity:
        sql += " AND ch.rarity=?"
        args.append(rarity)
    sql += " ORDER BY c.first_at DESC, ch.char_id DESC LIMIT ? OFFSET ?"
    async with _db.execute(sql, (*args, limit, offset)) as cur:
        return await cur.fetchall()


async def search_collection(user_id: int, query: str = "", offset: int = 0, limit: int = 50):
    """Inline mode: one user's own characters (most recently bound first), optional text filter, with qty."""
    sql = ("SELECT ch.*, c.qty FROM collection c JOIN characters ch ON ch.char_id = c.char_id "
           "WHERE c.user_id=?")
    args: list = [user_id]
    q = query.strip()
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cond = " OR ".join(f"ch.{col} LIKE ? ESCAPE '\\'" for col in ("name", "anime", "rarity", "sub_rarity"))
        args += [like] * 4
        if q.isdigit():
            cond += " OR ch.char_id = ?"
            args.append(int(q))
        sql += f" AND ({cond})"
    sql += " ORDER BY c.first_at DESC, ch.char_id DESC LIMIT ? OFFSET ?"
    async with _db.execute(sql, (*args, limit, offset)) as cur:
        return await cur.fetchall()


# ---------- favorite / gift ----------
_xfer_lock = asyncio.Lock()


async def owned_qty(user_id: int, char_id: int) -> int:
    async with _db.execute(
            "SELECT qty FROM collection WHERE user_id=? AND char_id=?", (user_id, char_id)) as cur:
        r = await cur.fetchone()
    return r[0] if r else 0


async def get_favorite(user_id: int) -> int | None:
    async with _db.execute("SELECT favorite_id FROM users WHERE user_id=?", (user_id,)) as cur:
        r = await cur.fetchone()
    return r[0] if r else None


async def set_favorite(user_id: int, char_id: int | None) -> None:
    await _db.execute("UPDATE users SET favorite_id=? WHERE user_id=?", (char_id, user_id))
    await _db.commit()


async def transfer_character(from_id: int, to_id: int, char_id: int) -> bool:
    """Move one copy from one user to another. False if the sender no longer owns it.
    If the sender gave away their last copy of their favorite, the favorite is cleared."""
    async with _xfer_lock:
        try:
            cur = await _db.execute(
                "UPDATE collection SET qty = qty - 1 WHERE user_id=? AND char_id=? AND qty >= 1",
                (from_id, char_id))
            if cur.rowcount != 1:
                await _db.rollback()
                return False
            await _db.execute("DELETE FROM collection WHERE user_id=? AND char_id=? AND qty <= 0",
                              (from_id, char_id))
            await _db.execute(
                "INSERT INTO collection (user_id, char_id, qty) VALUES (?,?,1) "
                "ON CONFLICT(user_id, char_id) DO UPDATE SET qty = qty + 1", (to_id, char_id))
            await _db.execute(
                "UPDATE users SET favorite_id=NULL WHERE user_id=? AND favorite_id=? "
                "AND NOT EXISTS (SELECT 1 FROM collection WHERE user_id=? AND char_id=?)",
                (from_id, char_id, from_id, char_id))
            await _db.commit()
            return True
        except Exception:
            await _db.rollback()
            raise


# ---------- harem: anime-wise view ----------
_AKEY = "LOWER(TRIM(ch.anime))"


async def collection_anime_count(user_id: int) -> int:
    """Number of different series the user owns at least one character of."""
    async with _db.execute(f"SELECT COUNT(DISTINCT {_AKEY}) {_OWNED}", (user_id,)) as cur:
        return (await cur.fetchone())[0]


async def collection_anime_page(user_id: int, offset: int = 0, limit: int = 5):
    """Series of a user's collection: most collected first. Rows: akey, anime, collected, total."""
    sql = (f"SELECT {_AKEY} AS akey, MIN(ch.anime) AS anime, COUNT(*) AS collected, "
           f"(SELECT COUNT(*) FROM characters t WHERE LOWER(TRIM(t.anime)) = {_AKEY}) AS total "
           f"{_OWNED} GROUP BY akey ORDER BY collected DESC, akey LIMIT ? OFFSET ?")
    async with _db.execute(sql, (user_id, limit, offset)) as cur:
        return await cur.fetchall()


async def collection_by_animes(user_id: int, keys: list[str]):
    """A user's characters inside the given series keys (akey), ordered by series then ID."""
    if not keys:
        return []
    sql = (f"SELECT ch.char_id, ch.name, ch.anime, ch.rarity, ch.sub_rarity, ch.file_id, ch.media_type, "
           f"c.qty, {_AKEY} AS akey {_OWNED} AND {_AKEY} IN ({','.join('?' * len(keys))}) "
           f"ORDER BY akey, ch.char_id")
    async with _db.execute(sql, (user_id, *keys)) as cur:
        return await cur.fetchall()


# ---------- shop ----------
async def pick_characters(rarity: str, count: int, blocked_subs=()):
    """`count` different random cards of a main rarity, skipping blocked (event) sub rarities."""
    sql, args = "SELECT * FROM characters WHERE rarity=?", [rarity]
    if blocked_subs:
        sql += f" AND (sub_rarity IS NULL OR sub_rarity NOT IN ({','.join('?' * len(blocked_subs))}))"
        args += list(blocked_subs)
    sql += " ORDER BY RANDOM() LIMIT ?"
    args.append(count)
    async with _db.execute(sql, args) as cur:
        return await cur.fetchall()


async def shop_has_offers(day: str, user_id: int) -> bool:
    async with _db.execute("SELECT 1 FROM shop_offers WHERE day=? AND user_id=? LIMIT 1", (day, user_id)) as cur:
        return await cur.fetchone() is not None


async def shop_add(day: str, user_id: int, items: list[tuple[int, str, int]]) -> None:
    """items = (char_id, rarity, price): this user's cards for the day."""
    await _db.executemany(
        "INSERT OR IGNORE INTO shop_offers (day, user_id, char_id, rarity, price) VALUES (?,?,?,?,?)",
        [(day, user_id, *i) for i in items])
    await _db.commit()


_OFFER = ("SELECT ch.*, s.price, s.bought FROM shop_offers s JOIN characters ch ON ch.char_id = s.char_id "
          "WHERE s.day=? AND s.user_id=?")


async def shop_items(day: str, user_id: int):
    """The user's cards for the day with price and bought flag (deleted characters are skipped)."""
    async with _db.execute(_OFFER + " ORDER BY s.rowid", (day, user_id)) as cur:
        return await cur.fetchall()


async def shop_item(day: str, user_id: int, char_id: int):
    async with _db.execute(_OFFER + " AND s.char_id=?", (day, user_id, char_id)) as cur:
        return await cur.fetchone()


async def shop_buy(user_id: int, day: str, char_id: int) -> tuple[str, int]:
    """Buy the user's own offer. Returns (status, balance). status: ok, gone, limit, poor.
    Takes the shared Nex lock; every step is a guarded UPDATE and is undone if a later step fails."""
    async with nex_lock:
        item = await shop_item(day, user_id, char_id)
        if not item:
            return "gone", await get_nex(user_id)
        price, bal = item["price"], await get_nex(user_id)
        if item["bought"]:
            return "limit", bal
        if bal < price:
            return "poor", bal
        flag = await _db.execute(
            "UPDATE shop_offers SET bought=1 WHERE day=? AND user_id=? AND char_id=? AND bought=0",
            (day, user_id, char_id))
        if flag.rowcount != 1:
            await _db.commit()
            return "limit", bal
        pay = await _db.execute(
            "UPDATE users SET nex = nex - ? WHERE user_id=? AND nex >= ?", (price, user_id, price))
        if pay.rowcount != 1:
            await _db.execute("UPDATE shop_offers SET bought=0 WHERE day=? AND user_id=? AND char_id=?",
                              (day, user_id, char_id))
            await _db.commit()
            return "poor", await get_nex(user_id)
        try:
            await _db.execute(
                "INSERT INTO collection (user_id, char_id, qty) VALUES (?,?,1) "
                "ON CONFLICT(user_id, char_id) DO UPDATE SET qty = qty + 1", (user_id, char_id))
            await _db.commit()
        except Exception:
            await _db.rollback()
            await _db.execute("UPDATE shop_offers SET bought=0 WHERE day=? AND user_id=? AND char_id=?",
                              (day, user_id, char_id))
            await _db.execute("UPDATE users SET nex = nex + ? WHERE user_id=?", (price, user_id))
            await _db.commit()
            raise
        return "ok", await get_nex(user_id)


async def shop_cleanup(keep_day: str) -> None:
    """Old days are not needed any more."""
    await _db.execute("DELETE FROM shop_offers WHERE day < ?", (keep_day,))
    await _db.commit()


# ---------- leaderboard ----------
async def top_nex(limit: int = 10):
    """Richest users first (users with 0 Nex are left out)."""
    async with _db.execute(
        "SELECT user_id, username, first_name, nex FROM users WHERE nex > 0 "
        "ORDER BY nex DESC, user_id LIMIT ?", (limit,)) as cur:
        return await cur.fetchall()


async def top_collectors(limit: int = 10):
    """Most unique characters first, then most copies. Rows: user_id, username, first_name, uniq, copies."""
    async with _db.execute(
        "SELECT u.user_id, u.username, u.first_name, COUNT(*) AS uniq, SUM(c.qty) AS copies "
        "FROM collection c JOIN users u ON u.user_id = c.user_id JOIN characters ch ON ch.char_id = c.char_id "
        "GROUP BY u.user_id ORDER BY uniq DESC, copies DESC, u.user_id LIMIT ?", (limit,)) as cur:
        return await cur.fetchall()


# ---------- /whoami wins and ranks ----------
async def whoami_win_add(user_id: int, day: str, cap: int) -> tuple[bool, int]:
    """Count one /whoami win for the day unless the daily cap is reached. Returns (counted, wins today)."""
    await _db.execute("INSERT OR IGNORE INTO whoami_wins (user_id, day, wins) VALUES (?,?,0)", (user_id, day))
    cur = await _db.execute(
        "UPDATE whoami_wins SET wins = wins + 1 WHERE user_id=? AND day=? AND wins < ?", (user_id, day, cap))
    await _db.commit()
    async with _db.execute("SELECT wins FROM whoami_wins WHERE user_id=? AND day=?", (user_id, day)) as c:
        r = await c.fetchone()
    return cur.rowcount == 1, (r[0] if r else 0)


async def top_whoami(limit: int = 10):
    """Most /whoami wins first. Rows: user_id, username, first_name, wins."""
    async with _db.execute(
        "SELECT u.user_id, u.username, u.first_name, SUM(w.wins) AS wins "
        "FROM whoami_wins w JOIN users u ON u.user_id = w.user_id "
        "GROUP BY u.user_id HAVING wins > 0 ORDER BY wins DESC, u.user_id LIMIT ?", (limit,)) as cur:
        return await cur.fetchall()


async def rank_nex(user_id: int) -> int | None:
    async with _db.execute("SELECT nex FROM users WHERE user_id=?", (user_id,)) as cur:
        r = await cur.fetchone()
    if not r or r[0] <= 0:
        return None
    async with _db.execute("SELECT COUNT(*) FROM users WHERE nex > ?", (r[0],)) as cur:
        return (await cur.fetchone())[0] + 1


async def rank_collectors(user_id: int) -> int | None:
    async with _db.execute(
        "SELECT COUNT(*) FROM collection c JOIN characters ch ON ch.char_id = c.char_id WHERE c.user_id=?",
        (user_id,)) as cur:
        mine = (await cur.fetchone())[0]
    if not mine:
        return None
    async with _db.execute(
        "SELECT COUNT(*) FROM (SELECT COUNT(*) AS n FROM collection c "
        "JOIN characters ch ON ch.char_id = c.char_id GROUP BY c.user_id) WHERE n > ?", (mine,)) as cur:
        return (await cur.fetchone())[0] + 1


async def rank_whoami(user_id: int) -> int | None:
    async with _db.execute("SELECT COALESCE(SUM(wins), 0) FROM whoami_wins WHERE user_id=?", (user_id,)) as cur:
        mine = (await cur.fetchone())[0]
    if not mine:
        return None
    async with _db.execute(
        "SELECT COUNT(*) FROM (SELECT SUM(wins) AS n FROM whoami_wins GROUP BY user_id) WHERE n > ?",
        (mine,)) as cur:
        return (await cur.fetchone())[0] + 1


async def whoami_game_add(user_id: int) -> None:
    """One finished /whoami game for a player (the one who started it and the one who won it)."""
    await _db.execute(
        "INSERT INTO whoami_stats (user_id, games) VALUES (?,1) "
        "ON CONFLICT(user_id) DO UPDATE SET games = games + 1", (user_id,))
    await _db.commit()


async def whoami_best(user_id: int, secs: float) -> None:
    """Keep the player's fastest correct guess."""
    await _db.execute(
        "INSERT INTO whoami_stats (user_id, games, best) VALUES (?,0,?) "
        "ON CONFLICT(user_id) DO UPDATE SET best = CASE WHEN best IS NULL OR ? < best THEN ? ELSE best END",
        (user_id, secs, secs, secs))
    await _db.commit()


async def top_games(limit: int = 10):
    async with _db.execute(
        "SELECT u.user_id, u.username, u.first_name, s.games FROM whoami_stats s "
        "JOIN users u ON u.user_id = s.user_id WHERE s.games > 0 "
        "ORDER BY s.games DESC, u.user_id LIMIT ?", (limit,)) as cur:
        return await cur.fetchall()


async def top_fastest(limit: int = 10):
    async with _db.execute(
        "SELECT u.user_id, u.username, u.first_name, s.best FROM whoami_stats s "
        "JOIN users u ON u.user_id = s.user_id WHERE s.best IS NOT NULL "
        "ORDER BY s.best ASC, u.user_id LIMIT ?", (limit,)) as cur:
        return await cur.fetchall()


async def rank_games(user_id: int) -> int | None:
    async with _db.execute("SELECT games FROM whoami_stats WHERE user_id=?", (user_id,)) as cur:
        r = await cur.fetchone()
    if not r or r[0] <= 0:
        return None
    async with _db.execute("SELECT COUNT(*) FROM whoami_stats WHERE games > ?", (r[0],)) as cur:
        return (await cur.fetchone())[0] + 1


async def rank_fastest(user_id: int) -> int | None:
    async with _db.execute("SELECT best FROM whoami_stats WHERE user_id=?", (user_id,)) as cur:
        r = await cur.fetchone()
    if not r or r[0] is None:
        return None
    async with _db.execute("SELECT COUNT(*) FROM whoami_stats WHERE best < ?", (r[0],)) as cur:
        return (await cur.fetchone())[0] + 1


async def swap_characters(a_id: int, b_id: int, a_char: int, b_char: int) -> bool:
    """Exchange one copy: user a gives a_char to user b, user b gives b_char to user a. One transaction.
    False (nothing changed) if either side no longer owns their character. Favorites are cleared when a last
    copy is given away."""
    async with _xfer_lock:
        try:
            for uid, ch in ((a_id, a_char), (b_id, b_char)):
                cur = await _db.execute(
                    "UPDATE collection SET qty = qty - 1 WHERE user_id=? AND char_id=? AND qty >= 1", (uid, ch))
                if cur.rowcount != 1:
                    await _db.rollback()
                    return False
            for uid, ch in ((a_id, a_char), (b_id, b_char)):
                await _db.execute("DELETE FROM collection WHERE user_id=? AND char_id=? AND qty <= 0", (uid, ch))
            for uid, ch in ((b_id, a_char), (a_id, b_char)):
                await _db.execute(
                    "INSERT INTO collection (user_id, char_id, qty) VALUES (?,?,1) "
                    "ON CONFLICT(user_id, char_id) DO UPDATE SET qty = qty + 1", (uid, ch))
            for uid, ch in ((a_id, a_char), (b_id, b_char)):
                await _db.execute(
                    "UPDATE users SET favorite_id=NULL WHERE user_id=? AND favorite_id=? "
                    "AND NOT EXISTS (SELECT 1 FROM collection WHERE user_id=? AND char_id=?)", (uid, ch, uid, ch))
            await _db.commit()
            return True
        except Exception:
            await _db.rollback()
            raise


# ---------- /waifuverse and /search ----------
_VAKEY = "LOWER(TRIM(anime))"


async def verse_anime_count() -> int:
    async with _db.execute(f"SELECT COUNT(DISTINCT {_VAKEY}) FROM characters") as cur:
        return (await cur.fetchone())[0]


async def verse_anime_page(offset: int = 0, limit: int = 10, uid: int = 0):
    """Series A to Z. Rows: akey, anime, n (characters in it), owned (how many of them uid owns)."""
    sql = (f"SELECT {_VAKEY} AS akey, MIN(anime) AS anime, COUNT(*) AS n, "
           f"(SELECT COUNT(*) FROM collection c JOIN characters t ON t.char_id = c.char_id "
           f"WHERE c.user_id = ? AND LOWER(TRIM(t.anime)) = {_VAKEY}) AS owned "
           f"FROM characters GROUP BY akey ORDER BY akey LIMIT ? OFFSET ?")
    async with _db.execute(sql, (uid, limit, offset)) as cur:
        return await cur.fetchall()


async def verse_chars_count(akey: str | None = None) -> int:
    sql, args = "SELECT COUNT(*) FROM characters", ()
    if akey is not None:
        sql, args = f"{sql} WHERE {_VAKEY}=?", (akey,)
    async with _db.execute(sql, args) as cur:
        return (await cur.fetchone())[0]


async def verse_chars_page(akey: str | None = None, offset: int = 0, limit: int = 15):
    """Characters by ID (all, or one series)."""
    sql, args = "SELECT char_id, name, anime, rarity, sub_rarity FROM characters", []
    if akey is not None:
        sql += f" WHERE {_VAKEY}=?"
        args.append(akey)
    sql += " ORDER BY char_id LIMIT ? OFFSET ?"
    async with _db.execute(sql, (*args, limit, offset)) as cur:
        return await cur.fetchall()


def _search_where(query: str) -> tuple[str, list]:
    """Every word must appear in the name or the series; a number (or #number) also matches the ID."""
    q = query.strip().lstrip("#").strip()
    words = q.lower().split()[:6]
    conds, args = [], []
    for w in words:
        like = "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds.append("(LOWER(name) LIKE ? ESCAPE '\\' OR LOWER(anime) LIKE ? ESCAPE '\\')")
        args += [like, like]
    where = " AND ".join(conds) or "0"
    if q.isdigit():
        where = f"({where}) OR char_id = ?"
        args.append(int(q))
    return where, args


async def search_count(query: str) -> int:
    where, args = _search_where(query)
    async with _db.execute(f"SELECT COUNT(*) FROM characters WHERE {where}", args) as cur:
        return (await cur.fetchone())[0]


async def search_page(query: str, offset: int = 0, limit: int = 10):
    """Exact ID first, then exact name, then names starting with the text, then the rest A to Z."""
    where, args = _search_where(query)
    q = query.strip().lstrip("#").strip().lower()
    esc_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    sql = (f"SELECT * FROM characters WHERE {where} ORDER BY (char_id = ?) DESC, (LOWER(name) = ?) DESC, "
           f"(LOWER(name) LIKE ? ESCAPE '\\') DESC, LOWER(name), char_id LIMIT ? OFFSET ?")
    async with _db.execute(sql, (*args, int(q) if q.isdigit() else -1, q, esc_q + "%", limit, offset)) as cur:
        return await cur.fetchall()


async def card_totals(char_id: int) -> tuple[int, int]:
    """(people who own it, copies in total)."""
    async with _db.execute(
        "SELECT COUNT(*), COALESCE(SUM(qty), 0) FROM collection WHERE char_id=?", (char_id,)) as cur:
        r = await cur.fetchone()
    return r[0], r[1]


async def owned_among(user_id: int, ids: list[int]) -> set[int]:
    """Which of these character IDs the user owns (for the checklist ticks)."""
    if not ids:
        return set()
    async with _db.execute(
        f"SELECT char_id FROM collection WHERE user_id=? AND char_id IN ({','.join('?' * len(ids))})",
        (user_id, *ids)) as cur:
        return {r[0] for r in await cur.fetchall()}


# ---------- series / name checks for /upload ----------
async def series_counts():
    """Every spelling of a series with its character count."""
    async with _db.execute("SELECT anime, COUNT(*) AS n FROM characters GROUP BY anime") as cur:
        return await cur.fetchall()


async def all_char_names() -> list[str]:
    async with _db.execute("SELECT DISTINCT name FROM characters") as cur:
        return [r[0] for r in await cur.fetchall()]


async def rename_series(old_spellings: list[str], new: str) -> list[int]:
    """Give every character of the listed spellings the series name `new`. Returns the changed character IDs."""
    if not old_spellings:
        return []
    marks = ",".join("?" * len(old_spellings))
    async with _db.execute(f"SELECT char_id FROM characters WHERE anime IN ({marks}) AND anime != ?",
                           (*old_spellings, new)) as cur:
        ids = [r[0] for r in await cur.fetchall()]
    if ids:
        await _db.execute(f"UPDATE characters SET anime=? WHERE anime IN ({marks})", (new, *old_spellings))
        await _db.commit()
    return ids


# ---------- website API (webapi.py) ----------
async def owner_cards(user_id: int):
    """Every character a user owns: char_id, name, anime, rarity, sub_rarity, media_type, qty."""
    async with _db.execute(
        "SELECT c.char_id, c.name, c.anime, c.rarity, c.sub_rarity, c.media_type, col.qty "
        "FROM collection col JOIN characters c ON c.char_id = col.char_id WHERE col.user_id = ?", (user_id,)) as cur:
        return await cur.fetchall()


async def owner_card(user_id: int, char_id: int):
    """The character row, only when the user owns it (used for the website picture proxy)."""
    async with _db.execute(
        "SELECT c.* FROM collection col JOIN characters c ON c.char_id = col.char_id "
        "WHERE col.user_id = ? AND col.char_id = ?", (user_id, char_id)) as cur:
        return await cur.fetchone()
