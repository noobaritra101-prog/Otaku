"""Otaku Nexus - database backup to the backup group (BACKUP_GROUP).

- Every BACKUP_EVERY seconds: snapshot the database, zip it, send the zip to the group, pin it, then unpin
  and delete the previous backup. The new file is sent and pinned FIRST, so there is never a moment without
  a backup.
- On startup (before the database is opened): read the pinned message of the group, download the zip and
  restore it, but only when the backup is newer than the local database (or there is no local database).
- Nothing is kept in memory between runs: the "previous backup" is always read from the group's pin.
- The bot must be admin in the backup group with the Pin messages and Delete messages rights.
- Telegram limits: bots can download files up to 20 MB and upload up to 50 MB.
- /import (owner and co-owners): send an old .db (or a backup .zip) as a file with /import as the caption, or
  reply to the file with /import. The file is checked, the current database is kept as <db>.bak, the old file
  replaces it and is converted to the current layout by db.init_db (old columns and tables are migrated), then a
  fresh backup is sent and pinned. Use it when no pinned backup is found at startup.
"""
import asyncio
import io
import shutil
import logging
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from configaration import BACKUP_EVERY, BACKUP_GROUP, CO_OWNERS, DB_PATH, OWNER_ID
import dbase as db
from minigame import notice

router = Router()
log = logging.getLogger("otaku.backup")

CHAT = f"@{BACKUP_GROUP}"
PREFIX = "otaku_backup_"
_lock = asyncio.Lock()
_restore_failed = False          # set when a restore was attempted and failed: backups are paused then


# ───────────── snapshot / zip ─────────────
def _snapshot_zip() -> tuple[bytes, str]:
    """Consistent copy of the live database (sqlite backup API, safe with WAL), zipped. Blocking."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            z.write(tmp, arcname=os.path.basename(DB_PATH))
        name = f"{PREFIX}{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.zip"
        return buf.getvalue(), name
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _is_backup(msg: Message | None) -> bool:
    d = msg.document if msg else None
    return bool(d and (d.file_name or "").startswith(PREFIX))


async def _pinned(bot) -> Message | None:
    return (await bot.get_chat(CHAT)).pinned_message


# ───────────── send + pin + clean up ─────────────
async def run_backup(bot) -> tuple[bool, str]:
    """One backup cycle. Returns (ok, text)."""
    if _restore_failed:
        return False, "Backup paused: the startup restore failed, fix it and restart."
    async with _lock:
        try:
            data, name = await asyncio.to_thread(_snapshot_zip)
            prev = await _pinned(bot)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            sent = await bot.send_document(
                CHAT, BufferedInputFile(data, filename=name),
                caption=f"<b>Database backup</b>\n{now} · {len(data) / 1024:.0f} KB",
                disable_notification=True)
            await bot.pin_chat_message(CHAT, sent.message_id, disable_notification=True)
            if _is_backup(prev) and prev.message_id != sent.message_id:
                try:
                    await bot.unpin_chat_message(CHAT, prev.message_id)
                except TelegramAPIError as e:
                    log.warning("unpin old backup failed: %s", e)
                try:
                    await bot.delete_message(CHAT, prev.message_id)
                except TelegramAPIError as e:
                    log.warning("delete old backup failed: %s", e)
            log.info("backup sent (%s, %d bytes)", name, len(data))
            return True, f"{name} · {len(data) / 1024:.0f} KB"
        except Exception as e:
            log.exception("backup failed")
            return False, str(e)[:200]


async def backup_loop(bot) -> None:
    """Back up right away, then every BACKUP_EVERY seconds."""
    while True:
        await run_backup(bot)
        await asyncio.sleep(BACKUP_EVERY)


# ───────────── restore on startup ─────────────
def _local_mtime() -> float:
    times = [os.path.getmtime(p) for p in (DB_PATH, DB_PATH + "-wal") if os.path.exists(p)]
    return max(times) if times else 0.0


def _write_db(zip_bytes: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = [n for n in z.namelist() if n.endswith(".db")]
        if not names:
            raise ValueError("no .db file inside the backup zip")
        raw = z.read(names[0])
    if not raw.startswith(b"SQLite format 3"):
        raise ValueError("backup is not a SQLite database")
    tmp = DB_PATH + ".restore"
    with open(tmp, "wb") as f:
        f.write(raw)
    for ext in ("-wal", "-shm"):                 # stale WAL files would corrupt the restored copy
        try:
            os.remove(DB_PATH + ext)
        except OSError:
            pass
    os.replace(tmp, DB_PATH)


async def restore(bot) -> None:
    """Call BEFORE db.init_db(). Restores the pinned backup when it is newer than the local database."""
    global _restore_failed
    try:
        pin = await _pinned(bot)
        if not _is_backup(pin):
            log.info("restore: no backup pinned in %s, starting with the local database", CHAT)
            if not os.path.exists(DB_PATH):
                try:
                    await bot.send_message(
                        OWNER_ID, "<b>No backup found</b>\n<blockquote>No pinned backup in "
                        f"{CHAT} and no local database. Send your old .db file with /import as the caption "
                        "to load it.</blockquote>")
                except TelegramAPIError as e:
                    log.warning("could not tell the owner that no backup was found: %s", e)
            return
        remote = pin.date.timestamp()
        if os.path.exists(DB_PATH) and _local_mtime() >= remote:
            log.info("restore: local database is newer than the pinned backup, keeping it")
            return
        buf = io.BytesIO()
        await bot.download(pin.document.file_id, destination=buf)
        await asyncio.to_thread(_write_db, buf.getvalue())
        log.info("restore: loaded %s (%d bytes)", pin.document.file_name, len(buf.getvalue()))
    except Exception:
        _restore_failed = True
        log.exception("restore failed, backups paused so the good backup is not overwritten")


# ───────────── /backup (owner and co-owners): back up now ─────────────
@router.message(Command("backup"), F.from_user.id.in_({OWNER_ID, *CO_OWNERS}))
async def backup_cmd(m: Message):
    ok, text = await run_backup(m.bot)
    await notice(m, "Backup sent" if ok else "Backup failed", text)


# ───────────── /import (owner and co-owners): load an old .db file ─────────────
def _extract_db(raw: bytes) -> bytes:
    """The SQLite bytes of an uploaded file: a plain .db, or the first .db inside a zip."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if n.lower().endswith((".db", ".sqlite", ".sqlite3"))]
            if not names:
                raise ValueError("no .db file inside the zip")
            raw = z.read(names[0])
    if not raw.startswith(b"SQLite format 3"):
        raise ValueError("this is not a SQLite database")
    return raw


def _check_db(path: str) -> None:
    """Make sure the file opens, is not corrupt and looks like a bot database."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("the database file is corrupt")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    if not tables & {"users", "characters", "collection"}:
        raise ValueError("no bot tables (users / characters / collection) in this database")


def _swap_in(raw: bytes) -> None:
    """Put the new database in place; the current one is kept as <db>.bak. Blocking."""
    tmp = DB_PATH + ".import"
    with open(tmp, "wb") as f:
        f.write(raw)
    try:
        _check_db(tmp)
    except Exception:
        os.remove(tmp)
        raise
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, DB_PATH + ".bak")
    for ext in ("-wal", "-shm"):
        try:
            os.remove(DB_PATH + ext)
        except OSError:
            pass
    os.replace(tmp, DB_PATH)


def _put_back() -> None:
    """Undo _swap_in: bring <db>.bak back."""
    bak = DB_PATH + ".bak"
    if os.path.exists(bak):
        for ext in ("-wal", "-shm"):
            try:
                os.remove(DB_PATH + ext)
            except OSError:
                pass
        shutil.copy2(bak, DB_PATH)


async def import_db(bot, doc) -> tuple[bool, str]:
    """Replace the live database with an uploaded one and convert it. Returns (ok, text)."""
    global _restore_failed
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        return False, "The file is over 20 MB, Telegram does not let bots download it."
    buf = io.BytesIO()
    try:
        await bot.download(doc.file_id, destination=buf)
        raw = await asyncio.to_thread(_extract_db, buf.getvalue())
    except Exception as e:
        return False, f"Could not read the file: {str(e)[:150]}"

    async with _lock:                                 # no backup runs while the database is swapped
        try:
            await db.close_db()
            await asyncio.to_thread(_swap_in, raw)
        except Exception as e:
            log.exception("import failed before the swap")
            try:
                await db.init_db()                    # reopen the untouched current database
            except Exception:
                log.exception("could not reopen the database")
            return False, f"Nothing was changed. {str(e)[:150]}"
        try:
            await db.init_db()                        # opens it and converts the old layout
            users, chats, chars = await db.stats()
        except Exception as e:
            log.exception("import conversion failed, putting the previous database back")
            await asyncio.to_thread(_put_back)
            try:
                await db.init_db()
            except Exception:
                log.exception("could not reopen the previous database")
            return False, f"Conversion failed, the previous database was put back. {str(e)[:150]}"
        _restore_failed = False
    await run_backup(bot)                             # new backup + pin right away
    return True, f"{users} users, {chats} groups, {chars} characters. Backup sent and pinned."


@router.message(Command("import"), F.from_user.id.in_({OWNER_ID, *CO_OWNERS}))
async def import_cmd(m: Message):
    doc = (m.document or (m.reply_to_message.document if m.reply_to_message else None))
    if not doc:
        return await notice(m, "Import", "Send the old .db (or backup .zip) file with /import as the caption, "
                                         "or reply to the file with /import.")
    ok, text = await import_db(m.bot, doc)
    await notice(m, "Database imported" if ok else "Import failed", text)
