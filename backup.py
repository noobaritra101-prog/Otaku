"""Otaku Nexus - database backup to the backup group (BACKUP_GROUP).

- Every BACKUP_EVERY seconds: snapshot the database, zip it, send the zip to the group, make it the only pinned
  message (unpin all, then pin), then delete the previous backups (the pinned one and every earlier one this process
  sent that is still there; anything that cannot be deleted is retried on the next cycle). The new file is sent and pinned FIRST, so there is never a moment without
  a backup.
- On startup (before the database is opened): read the pinned message of the group, download the zip and
  restore it, but only when the backup is newer than the local database (or there is no local database).
- Startup restore compares the last_backup_at marker stored inside the local database with the backup's time
  (not file times); an empty local database always restores. Message ids of sent backups are saved in the
  database so old files are still deleted after a restart. An empty database never overwrites a pinned backup.
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
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from configaration import BACKUP_EVERY, BACKUP_GROUP, CO_OWNERS, DB_PATH, OWNER_ID
import dbase as db

router = Router()
log = logging.getLogger("otaku.backup")

CHAT = f"@{BACKUP_GROUP}"
PREFIX = "otaku_backup_"
_lock = asyncio.Lock()
_restore_failed = False          # set when a restore was attempted and failed: backups are paused then
_tasks: set[asyncio.Task] = set()   # keeps background tasks alive until they finish


def _fmt(title: str, text: str) -> str:
    return f"<b>{escape(title)}</b>\n<blockquote>{escape(text)}</blockquote>"


async def notice(m: Message, title: str, text: str) -> Message:
    """Plain HTML reply to the command."""
    return await m.reply(_fmt(title, text))


async def _edit(msg: Message, title: str, text: str) -> None:
    """Edit a status message in place; send a new reply if it cannot be edited."""
    try:
        await msg.edit_text(_fmt(title, text))
    except TelegramAPIError as e:
        if "not modified" in str(e).lower():
            return
        log.debug("status edit failed, replying instead: %s", e)
        try:
            await msg.reply(_fmt(title, text))
        except TelegramAPIError:
            log.exception("could not report the result")


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


# ───────────── local database helpers (blocking, used before the async db is open too) ─────────────
def _local_get(key: str) -> str | None:
    """Read one value from the settings table of the local database file, None when unavailable."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        finally:
            con.close()
        return r[0] if r else None
    except sqlite3.Error:
        return None


def _local_is_empty() -> bool:
    """True when the local database has no users and no characters (a fresh, empty file)."""
    if not os.path.exists(DB_PATH):
        return True
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            u = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            c = con.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
        finally:
            con.close()
        return (u + c) == 0
    except sqlite3.Error:
        return True


def _ids_of(raw: str | None) -> list[int]:
    return [int(x) for x in (raw or "").split(",") if x.strip().isdigit()]


# ───────────── send + pin + clean up ─────────────
_sent_ids: list[int] = []        # backup messages sent and not deleted yet (also saved in the database)


async def _save_ids() -> None:
    try:
        await db.set_setting("backup_msg_ids", ",".join(str(i) for i in _sent_ids[-30:]))
    except Exception:
        log.exception("could not save the backup message ids")


async def _drop_old(bot, ids: set[int]) -> list[str]:
    """Unpin and delete old backup messages. Returns problems; whatever cannot be deleted stays in _sent_ids."""
    problems: list[str] = []
    for mid in sorted(ids):
        try:
            await bot.unpin_chat_message(chat_id=CHAT, message_id=mid)
        except TelegramAPIError as e:
            log.debug("unpin old backup %s: %s", mid, e)      # already unpinned / gone is fine
        try:
            await bot.delete_message(chat_id=CHAT, message_id=mid)
        except TelegramAPIError as e:
            if "not found" in str(e).lower():                 # already deleted
                if mid in _sent_ids:
                    _sent_ids.remove(mid)
            else:
                log.warning("delete old backup %s failed, will retry next cycle: %s", mid, e)
                problems.append(f"could not delete {mid}: {str(e)[:80]}")
            continue
        if mid in _sent_ids:
            _sent_ids.remove(mid)
    return problems


async def run_backup(bot, force: bool = False) -> tuple[bool, str]:
    """One backup cycle. Returns (ok, text).

    Send the new zip, make it the ONLY pinned message (unpin_all, then pin), then unpin + delete every older backup:
    the pinned one, every id saved in the database (survives restarts) and every one sent by this process.
    A backup of an EMPTY database is refused while a backup is pinned (force=True skips that check)."""
    if _restore_failed:
        return False, "Backup paused: the startup restore failed, fix it and restart."
    async with _lock:
        sent = None
        try:
            try:
                prev = await _pinned(bot)
            except TelegramAPIError as e:
                log.warning("could not read the pinned message: %s", e)
                prev = None
            if not force and _is_backup(prev) and await asyncio.to_thread(_local_is_empty):
                return False, "The database is empty but a backup is pinned. Not overwriting it. Restart the bot to restore, or /import."
            for i in _ids_of(await db.get_setting("backup_msg_ids")):
                if i not in _sent_ids:
                    _sent_ids.append(i)
            ts = datetime.now(timezone.utc)
            await db.set_setting("last_backup_at", str(int(ts.timestamp())))   # goes inside the snapshot
            data, name = await asyncio.to_thread(_snapshot_zip)
            sent = await bot.send_document(
                CHAT, BufferedInputFile(data, filename=name),
                caption=f"<b>Database backup</b>\n{ts.strftime('%Y-%m-%d %H:%M:%S UTC')} · {len(data) / 1024:.0f} KB",
                disable_notification=True)
            try:
                await bot.unpin_all_chat_messages(chat_id=CHAT)     # nothing old stays pinned, even a stale pin
            except TelegramAPIError as e:
                log.warning("unpin all failed: %s", e)
            try:
                await bot.pin_chat_message(chat_id=CHAT, message_id=sent.message_id, disable_notification=True)
            except Exception:
                # unpinned new backups would never be found at startup: remove it, keep the old pin
                try:
                    await bot.delete_message(chat_id=CHAT, message_id=sent.message_id)
                except TelegramAPIError:
                    pass
                raise
            _sent_ids.append(sent.message_id)
            old = {i for i in _sent_ids if i != sent.message_id}
            if _is_backup(prev) and prev.message_id != sent.message_id:
                old.add(prev.message_id)
            problems = await _drop_old(bot, old)
            await _save_ids()
            log.info("backup sent (%s, %d bytes), removed %d old", name, len(data), len(old) - len(problems))
            text = f"{name} · {len(data) / 1024:.0f} KB"
            if problems:
                text += ". Old backups left: " + "; ".join(problems[:2]) + ". Give the bot the Delete messages right."
            return True, text
        except Exception as e:
            log.exception("backup failed")
            hint = ""
            low = str(e).lower()
            if "not enough rights" in low or "administrator" in low or "pin" in low:
                hint = " Make the bot admin in the backup group with Pin and Delete messages rights."
            elif "chat not found" in low:
                hint = " The bot is not in the backup group."
            return False, (str(e)[:160] + hint)


async def backup_loop(bot) -> None:
    """Back up right away, then every BACKUP_EVERY seconds."""
    while True:
        await run_backup(bot)
        await asyncio.sleep(BACKUP_EVERY)


# ───────────── restore on startup ─────────────
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
    if os.path.exists(DB_PATH):                  # keep whatever was there, just in case
        try:
            shutil.copy2(DB_PATH, DB_PATH + ".bak")
        except OSError:
            pass
    for ext in ("-wal", "-shm"):                 # stale WAL files would corrupt the restored copy
        try:
            os.remove(DB_PATH + ext)
        except OSError:
            pass
    os.replace(tmp, DB_PATH)


async def _tell_owner(bot, text: str) -> None:
    try:
        await bot.send_message(OWNER_ID, text)
    except TelegramAPIError as e:
        log.warning("could not message the owner: %s", e)


async def _find_backup(bot) -> Message | None:
    """The newest backup message: the pinned one, else the last ids saved in the local database (forwarded to
    the owner to read their file id, the forward is deleted again)."""
    try:
        pin = await _pinned(bot)
        if _is_backup(pin):
            return pin
    except TelegramAPIError as e:
        log.warning("restore: could not read the pinned message: %s", e)
    for mid in reversed(_ids_of(await asyncio.to_thread(_local_get, "backup_msg_ids"))):
        try:
            fwd = await bot.forward_message(chat_id=OWNER_ID, from_chat_id=CHAT, message_id=mid)
        except TelegramAPIError:
            continue
        try:
            await bot.delete_message(chat_id=OWNER_ID, message_id=fwd.message_id)
        except TelegramAPIError:
            pass
        if _is_backup(fwd):
            return fwd
    return None


async def restore(bot) -> None:
    """Call BEFORE db.init_db(). Loads the newest backup from the group unless the local database is already
    at least as new (decided by the last_backup_at marker stored INSIDE the database, not by file times)."""
    global _restore_failed
    try:
        msg = await _find_backup(bot)
        if not msg:
            log.info("restore: no backup found in %s, starting with the local database", CHAT)
            if _local_is_empty():
                await _tell_owner(
                    bot, "<b>No backup found</b>\n<blockquote>No pinned backup in "
                    f"{CHAT} and the database is empty. Send your old .db file with /import as the caption "
                    "to load it.</blockquote>")
            return
        remote_ts = int(msg.forward_date.timestamp()) if msg.forward_date else int(msg.date.timestamp())
        local_ts = int(_local_get("last_backup_at") or 0)
        if not _local_is_empty() and local_ts >= remote_ts - 120:
            log.info("restore: local database (marker %s) is as new as the backup (%s), keeping it", local_ts, remote_ts)
            return
        buf = io.BytesIO()
        await bot.download(msg.document.file_id, destination=buf)
        await asyncio.to_thread(_write_db, buf.getvalue())
        log.info("restore: loaded %s (%d bytes)", msg.document.file_name, len(buf.getvalue()))
        await _tell_owner(bot, f"<b>Backup restored</b>\n<blockquote>{escape(msg.document.file_name or '')} "
                               f"({len(buf.getvalue()) / 1024:.0f} KB) was loaded at startup.</blockquote>")
    except Exception as e:
        _restore_failed = True
        log.exception("restore failed, backups paused so the good backup is not overwritten")
        await _tell_owner(bot, "<b>Restore failed</b>\n<blockquote>" + escape(str(e)[:200]) +
                          "\nBackups are paused so the good backup is not overwritten. Fix it and restart, "
                          "or use /import.</blockquote>")


# ───────────── /backup (owner and co-owners): back up now ─────────────
@router.message(Command("backup"), F.from_user.id.in_({OWNER_ID, *CO_OWNERS}))
async def backup_cmd(m: Message):
    status = await notice(m, "Backup", "Sending the backup...")
    ok, text = await run_backup(m.bot)
    await _edit(status, "Backup sent" if ok else "Backup failed", text)


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
    """Make sure the file opens, is not corrupt and looks like a bot database.
    Opened immutable + read only through a proper file URI: works for WAL files without their -wal, and for
    paths with spaces or special characters."""
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("the database file is corrupt")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    if not tables & {"users", "characters", "collection"}:
        raise ValueError("no bot tables (users / characters / collection) in this database")


def _normalise(path: str) -> None:
    """Rewrite the file as a clean standalone database (journal mode DELETE, no WAL dependency). Blocking."""
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.commit()
    finally:
        con.close()


def _swap_in(raw: bytes) -> None:
    """Put the new database in place; the current one is kept as <db>.bak. Blocking."""
    tmp = DB_PATH + ".import"
    with open(tmp, "wb") as f:
        f.write(raw)
    try:
        _check_db(tmp)
        _normalise(tmp)
    except Exception:
        for ext in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(tmp + ext)
            except OSError:
                pass
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


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:150]}"


async def import_db(bot, doc, progress=None) -> tuple[bool, str]:
    """Replace the live database with an uploaded one and convert it. Returns (ok, text).
    `progress(text)` is awaited between the steps so the user sees what is happening."""
    global _restore_failed

    async def say(t: str) -> None:
        if progress:
            try:
                await progress(t)
            except Exception:
                pass

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        return False, "The file is over 20 MB, Telegram does not let bots download it."
    await say("Downloading the file...")
    buf = io.BytesIO()
    try:
        await asyncio.wait_for(bot.download(doc.file_id, destination=buf), timeout=120)
        raw = await asyncio.to_thread(_extract_db, buf.getvalue())
    except Exception as e:
        log.exception("import: could not read the file")
        return False, f"Could not read the file. {_err(e)}"

    await say("Checking the database...")
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
            return False, f"Nothing was changed. {_err(e)}"
        await say("Converting to the current layout...")
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
            return False, f"Conversion failed, the previous database was put back. {_err(e)}"
        _restore_failed = False
    return True, f"{users} users, {chats} groups, {chars} characters loaded."


async def _backup_after_import(bot, msg: Message, text: str) -> None:
    ok, info = await run_backup(bot, force=True)      # new backup + pin
    await _edit(msg, "Database imported", text + (" Backup sent and pinned." if ok else f" Backup failed: {info}"))


async def _do_import(bot, status: Message, doc) -> None:
    """The slow part, runs in the background so the command is answered instantly."""
    async def progress(t: str) -> None:
        await _edit(status, "Import", t)

    try:
        ok, text = await import_db(bot, doc, progress)
    except Exception as e:
        log.exception("import crashed")
        return await _edit(status, "Import failed", _err(e))
    if not ok:
        return await _edit(status, "Import failed", text)
    await _edit(status, "Database imported", text + " Sending a backup...")
    await _backup_after_import(bot, status, text)


@router.message(Command("import"), F.from_user.id.in_({OWNER_ID, *CO_OWNERS}))
async def import_cmd(m: Message):
    status = await notice(m, "Import", "Got it, working on it...")      # instant reply, nothing before it
    doc = m.document or (m.reply_to_message.document if m.reply_to_message else None)
    if not doc:
        return await _edit(status, "Import", "Send the old .db (or backup .zip) file with /import as the caption, "
                                             "or reply to the file with /import.")
    t = asyncio.create_task(_do_import(m.bot, status, doc))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
