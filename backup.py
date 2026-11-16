"""Otaku Nexus - database backup to the backup group (BACKUP_GROUP).

v0.47 - timeout fix + smart backup
- Every Telegram call has its own generous timeout (aiogram defaults are 30 s download / 60 s request, which is what
  caused "HTTP Client says - Request timeout error") and is retried with exponential backoff + jitter. Upload and
  download timeouts grow with the file size. FloodWait (retry_after) is respected.
- Downloads go to a temp file, the size is verified, the zip CRC is tested, the SQLite file is integrity-checked
  BEFORE it replaces the live database. The old database is always kept as <db>.bak.
- A network failure during the startup restore no longer pauses backups forever: a watchdog keeps retrying in the
  background (1, 2, 4 ... 15 min) and does a live restore as soon as Telegram answers. Real problems (corrupt zip,
  no bot tables, backup over 20 MB) still pause backups and tell the owner. /restore retries by hand, no restart.
- Smart skip: when the database has not changed since the last backup and the pinned backup is still there, the
  cycle is skipped (no useless uploads). /backup always uploads.
- Failed cycles are retried early (1, 2, 4, 8, 15 min) instead of waiting BACKUP_EVERY; the owner is told after 3
  failures in a row and again when it recovers.
- The snapshot is integrity-checked before it is uploaded. Warns when the zip nears Telegram's 20 MB bot download
  limit (a bigger backup could be sent but never restored by a bot).
- /backupstatus shows the state of everything.

Original behaviour (kept)
- Every BACKUP_EVERY seconds: snapshot the database, zip it, send the zip to the group, make it the only pinned
  message (unpin all, then pin), then delete the previous backups. The new file is sent and pinned FIRST, so there
  is never a moment without a backup.
- On startup (before the database is opened): read the pinned message, download the zip and restore it, but only
  when the backup is newer than the local database (last_backup_at marker stored inside the database) or the local
  database is empty. An empty database never overwrites a pinned backup.
- Message ids of sent backups are saved in settings.backup_msg_ids so old files are deleted after a restart.
- The bot must be admin in the backup group with the Pin messages and Delete messages rights.
- Telegram limits: bots can download files up to 20 MB and upload up to 50 MB.
- /import (owner and co-owners): send an old .db (or a backup .zip) as a file with /import as the caption, or reply
  to the file with /import. The file is checked, the current database is kept as <db>.bak, the old file replaces
  it and is converted by db.init_db, then a fresh backup is sent and pinned.
"""
import asyncio
import io
import logging
import os
import random
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from html import escape
from typing import Awaitable, Callable, TypeVar

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import (TelegramAPIError, TelegramNetworkError, TelegramRetryAfter,
                                TelegramServerError)
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from configaration import BACKUP_EVERY, BACKUP_GROUP, CO_OWNERS, DB_PATH, OWNER_ID
import dbase as db
from minigame import notice

router = Router()
log = logging.getLogger("otaku.backup")

CHAT = f"@{BACKUP_GROUP}"
PREFIX = "otaku_backup_"
MB = 1024 * 1024
BOT_DOWNLOAD_LIMIT = 20 * MB
BOT_UPLOAD_LIMIT = 50 * MB
API_TIMEOUT = 25                 # small calls (get_chat, get_file, pin, delete ...): a hung connection is dropped
                                 # fast and retried on a fresh one instead of waiting a full minute
RETRIES = 6                      # tries for small calls
TRANSFER_RETRIES = 3             # tries for upload / download
ALERT_AFTER = 3                  # tell the owner after this many failed cycles in a row

_lock = asyncio.Lock()
_restore_failed = False          # restore failed: backups are paused (other modules may read this name)
_tasks: set[asyncio.Task] = set()
_watch_conn: sqlite3.Connection | None = None

S = {                            # status shown by /backupstatus
    "last_ok": 0, "last_size": 0, "last_name": "", "fails": 0, "last_error": "",
    "skipped": 0, "restore_state": "ok", "restore_error": "", "next_run": 0,
    "last_version": None, "size_warned": False,
}

T = TypeVar("T")
_TRANSIENT = (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError, aiohttp.ClientError,
              ConnectionError, TimeoutError)


# ───────────── retry / timeout helpers ─────────────
def _is_transient(e: BaseException) -> bool:
    return isinstance(e, _TRANSIENT) or isinstance(e, TelegramRetryAfter)


def _transfer_timeout(size: int) -> int:
    """Seconds for one upload / download: at least 3 minutes, +45 s per MB (slow hosts included)."""
    return int(max(180, 60 + (size / MB) * 45))


async def _retry(label: str, factory: Callable[[], Awaitable[T]], tries: int = RETRIES, base: float = 3.0) -> T:
    """Run factory() until it works. Only network style errors are retried; Telegram refusals raise at once."""
    for attempt in range(1, tries + 1):
        try:
            return await factory()
        except TelegramRetryAfter as e:
            if attempt == tries:
                raise
            wait = float(e.retry_after) + 1
        except _TRANSIENT as e:
            if attempt == tries:
                raise
            wait = min(30.0, base * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
            log.warning("%s: %s (try %d/%d), retry in %.0fs", label, str(e)[:100] or type(e).__name__,
                        attempt, tries, wait)
        await asyncio.sleep(wait)
    raise RuntimeError("unreachable")


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _ago(ts: float) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    return f"{s // 3600} h {s % 3600 // 60} min ago"


# ───────────── snapshot / zip ─────────────
def _snapshot_zip() -> tuple[bytes, str]:
    """Consistent copy of the live database (sqlite backup API, safe with WAL), checked, zipped. Blocking."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(DB_PATH, timeout=30)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("the live database failed its integrity check, not uploading it")
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


def _msg_ts(msg: Message) -> int:
    """Time of the backup: original date of a forwarded copy, else the message date."""
    fo = getattr(msg, "forward_origin", None)
    d = getattr(fo, "date", None) or getattr(msg, "forward_date", None) or msg.date
    return int(d.timestamp())


async def _pinned(bot) -> Message | None:
    chat = await _retry("get_chat", lambda: bot.get_chat(CHAT, request_timeout=API_TIMEOUT), tries=5)
    return chat.pinned_message


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


def _data_version() -> int | None:
    """PRAGMA data_version of a long-lived connection: changes whenever ANOTHER connection commits.
    Used to skip a backup when nothing changed. None = unknown (never skip)."""
    global _watch_conn
    try:
        if _watch_conn is None:
            _watch_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        return int(_watch_conn.execute("PRAGMA data_version").fetchone()[0])
    except sqlite3.Error:
        _reset_watch()
        return None


def _reset_watch() -> None:
    global _watch_conn
    if _watch_conn is not None:
        try:
            _watch_conn.close()
        except sqlite3.Error:
            pass
        _watch_conn = None
    S["last_version"] = None


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
            await _retry("unpin old", lambda: bot.unpin_chat_message(chat_id=CHAT, message_id=mid,
                                                                      request_timeout=API_TIMEOUT), tries=2)
        except TelegramAPIError as e:
            log.debug("unpin old backup %s: %s", mid, e)      # already unpinned / gone is fine
        try:
            await _retry("delete old", lambda: bot.delete_message(chat_id=CHAT, message_id=mid,
                                                                   request_timeout=API_TIMEOUT), tries=2)
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


async def run_backup(bot, force: bool = False, manual: bool = False) -> tuple[bool, str]:
    """One backup cycle. Returns (ok, text).

    Send the new zip, make it the ONLY pinned message (unpin_all, then pin), then unpin + delete every older backup.
    A backup of an EMPTY database is refused while a backup is pinned (force=True skips that check).
    manual=True (the /backup command) never skips an unchanged database."""
    if _restore_failed:
        return False, "Backup paused: the startup restore failed. Use /restore to retry, or /import."
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

            # smart skip: nothing changed since the last backup and it is still pinned
            if not force and not manual and _is_backup(prev) and S["last_version"] is not None:
                cur = await asyncio.to_thread(_data_version)
                if cur is not None and cur == S["last_version"]:
                    S["skipped"] += 1
                    log.info("backup skipped: database unchanged since the last backup")
                    return True, "No changes since the last backup, skipped."

            for i in _ids_of(await db.get_setting("backup_msg_ids")):
                if i not in _sent_ids:
                    _sent_ids.append(i)
            ts = datetime.now(timezone.utc)
            await db.set_setting("last_backup_at", str(int(ts.timestamp())))   # goes inside the snapshot
            version = await asyncio.to_thread(_data_version)                   # sampled before the snapshot
            data, name = await asyncio.to_thread(_snapshot_zip)
            if len(data) > BOT_UPLOAD_LIMIT:
                raise ValueError(f"backup is {len(data) / MB:.1f} MB, over Telegram's 50 MB upload limit")
            caption = f"<b>Database backup</b>\n{ts.strftime('%Y-%m-%d %H:%M:%S UTC')} · {len(data) / 1024:.0f} KB"
            up_timeout = _transfer_timeout(len(data))
            sent = await _retry(
                "send_document",
                lambda: bot.send_document(CHAT, BufferedInputFile(data, filename=name), caption=caption,
                                          disable_notification=True, request_timeout=up_timeout),
                tries=TRANSFER_RETRIES, base=5.0)
            try:
                await _retry("unpin_all", lambda: bot.unpin_all_chat_messages(chat_id=CHAT, request_timeout=API_TIMEOUT),
                             tries=2)
            except TelegramAPIError as e:
                log.warning("unpin all failed: %s", e)
            try:
                await _retry("pin", lambda: bot.pin_chat_message(chat_id=CHAT, message_id=sent.message_id,
                                                                  disable_notification=True,
                                                                  request_timeout=API_TIMEOUT), tries=3)
            except Exception:
                # unpinned new backups would never be found at startup: remove it, keep the old pin
                try:
                    await bot.delete_message(chat_id=CHAT, message_id=sent.message_id, request_timeout=API_TIMEOUT)
                except TelegramAPIError:
                    pass
                raise
            _sent_ids.append(sent.message_id)
            old = {i for i in _sent_ids if i != sent.message_id}
            if _is_backup(prev) and prev.message_id != sent.message_id:
                old.add(prev.message_id)
            problems = await _drop_old(bot, old)
            await _save_ids()

            S.update(last_ok=time.time(), last_size=len(data), last_name=name, last_error="", fails=0,
                     last_version=version)
            log.info("backup sent (%s, %d bytes), removed %d old", name, len(data), len(old) - len(problems))
            text = f"{name} · {len(data) / 1024:.0f} KB"
            if problems:
                text += ". Old backups left: " + "; ".join(problems[:2]) + ". Give the bot the Delete messages right."
            if len(data) > BOT_DOWNLOAD_LIMIT * 0.9:
                text += (f". Warning: {len(data) / MB:.1f} MB, bots can only download 20 MB, so this backup could "
                         "not be restored automatically. Trim the database.")
                if not S["size_warned"]:
                    S["size_warned"] = True
                    _spawn(_tell_owner(bot, "<b>Backup is getting too big</b>\n<blockquote>"
                                            f"{len(data) / MB:.1f} MB. Bots can download only 20 MB, so the "
                                            "automatic restore will fail above that.</blockquote>"))
            return True, text
        except Exception as e:
            log.exception("backup failed")
            low = str(e).lower()
            hint = ""
            if _is_transient(e):
                hint = " Telegram was too slow or unreachable, it will be retried automatically."
            elif "not enough rights" in low or "administrator" in low or "pin" in low:
                hint = " Make the bot admin in the backup group with Pin and Delete messages rights."
            elif "chat not found" in low:
                hint = " The bot is not in the backup group."
            msg = (str(e)[:160] or type(e).__name__) + hint
            S["last_error"] = msg
            return False, msg


async def backup_loop(bot) -> None:
    """Back up right away, then every BACKUP_EVERY seconds. Failed cycles retry early with backoff."""
    fails = 0
    while True:
        try:
            ok, text = await run_backup(bot)
        except Exception as e:                                  # never let the loop die
            log.exception("backup loop crashed once")
            ok, text = False, str(e)[:160]
        delay = BACKUP_EVERY
        if ok:
            if fails >= ALERT_AFTER:
                await _tell_owner(bot, "<b>Backups are working again</b>")
            fails = 0
        elif _restore_failed:
            delay = 60                                          # paused, check again soon (restore may succeed)
        else:
            fails += 1
            S["fails"] = fails
            delay = min(BACKUP_EVERY, 60 * 2 ** (fails - 1), 900)
            if fails == ALERT_AFTER:
                await _tell_owner(bot, f"<b>Backup failing</b>\n<blockquote>{fails} cycles in a row.\n"
                                       f"{escape(text[:250])}</blockquote>")
        S["next_run"] = time.time() + delay
        await asyncio.sleep(delay)


# ───────────── download ─────────────
async def _download(bot, doc) -> bytes:
    """Download a Telegram file to a temp file with long timeouts and retries, verify its size, return the bytes."""
    size = doc.file_size or 0
    if size > BOT_DOWNLOAD_LIMIT:
        raise ValueError(f"the file is {size / MB:.1f} MB, Telegram does not let bots download more than 20 MB")
    tg = await _retry("get_file", lambda: bot.get_file(doc.file_id, request_timeout=API_TIMEOUT))
    fd, tmp = tempfile.mkstemp(suffix=".dl")
    os.close(fd)
    try:
        async def one() -> None:
            await bot.download_file(tg.file_path, destination=tmp, timeout=_transfer_timeout(size),
                                    chunk_size=256 * 1024)
            got = os.path.getsize(tmp)
            if size and got != size:
                raise ConnectionError(f"incomplete download {got}/{size} bytes")
        await _retry("download", one, tries=TRANSFER_RETRIES, base=5.0)
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ───────────── validate + swap ─────────────
def _extract_db(raw: bytes) -> bytes:
    """The SQLite bytes of an uploaded file: a plain .db, or the best .db inside a zip (CRC tested)."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            bad = z.testzip()
            if bad:
                raise ValueError(f"the zip is damaged ({bad})")
            names = [n for n in z.namelist() if n.lower().endswith((".db", ".sqlite", ".sqlite3"))]
            if not names:
                raise ValueError("no .db file inside the zip")
            want = os.path.basename(DB_PATH)
            names.sort(key=lambda n: os.path.basename(n) != want)     # the file named like our db first
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


async def _apply_raw(raw: bytes) -> tuple[bool, str]:
    """Swap the OPEN live database for raw and convert it. The caller holds _lock. Returns (ok, text)."""
    global _restore_failed
    _reset_watch()
    try:
        await db.close_db()
        await asyncio.to_thread(_swap_in, raw)
    except Exception as e:
        log.exception("swap failed before anything changed")
        try:
            await db.init_db()                    # reopen the untouched current database
        except Exception:
            log.exception("could not reopen the database")
        return False, f"Nothing was changed. {str(e)[:150]}"
    try:
        await db.init_db()                        # opens it and converts the old layout
        users, chats, chars = await db.stats()
    except Exception as e:
        log.exception("conversion failed, putting the previous database back")
        await asyncio.to_thread(_put_back)
        try:
            await db.init_db()
        except Exception:
            log.exception("could not reopen the previous database")
        return False, f"Conversion failed, the previous database was put back. {str(e)[:150]}"
    _restore_failed = False
    S.update(restore_state="ok", restore_error="")
    return True, f"{users} users, {chats} groups, {chars} characters."


async def _tell_owner(bot, text: str) -> None:
    try:
        await bot.send_message(OWNER_ID, text, request_timeout=API_TIMEOUT)
    except TelegramAPIError as e:
        log.warning("could not message the owner: %s", e)


async def _find_backup(bot) -> Message | None:
    """The newest backup message: the pinned one, else the last ids saved in the local database (forwarded to
    the owner to read their file id, the forward is deleted again)."""
    pin_err: Exception | None = None
    try:
        pin = await _pinned(bot)
        if _is_backup(pin):
            return pin
    except TelegramAPIError as e:
        pin_err = e
        log.warning("restore: could not read the pinned message: %s", e)
    for mid in reversed(_ids_of(await asyncio.to_thread(_local_get, "backup_msg_ids"))):
        try:
            fwd = await _retry("forward", lambda: bot.forward_message(chat_id=OWNER_ID, from_chat_id=CHAT,
                                                                       message_id=mid, request_timeout=API_TIMEOUT),
                               tries=2)
        except TelegramAPIError as e:
            if _is_transient(e):
                pin_err = pin_err or e
            continue
        try:
            await bot.delete_message(chat_id=OWNER_ID, message_id=fwd.message_id, request_timeout=API_TIMEOUT)
        except TelegramAPIError:
            pass
        if _is_backup(fwd):
            return fwd
    if pin_err is not None and _is_transient(pin_err):
        raise pin_err                             # Telegram unreachable is NOT the same as "no backup exists"
    return None


# ───────────── restore on startup + live restore ─────────────
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
        remote_ts = _msg_ts(msg)
        local_ts = int(_local_get("last_backup_at") or 0)
        if not _local_is_empty() and local_ts >= remote_ts - 120:
            log.info("restore: local database (marker %s) is as new as the backup (%s), keeping it", local_ts, remote_ts)
            return
        raw = await _extract_after_download(bot, msg.document)
        await asyncio.to_thread(_swap_in, raw)
        log.info("restore: loaded %s (%d bytes)", msg.document.file_name, len(raw))
        await _tell_owner(bot, f"<b>Backup restored</b>\n<blockquote>{escape(msg.document.file_name or '')} "
                               f"({len(raw) / 1024:.0f} KB database) was loaded at startup.</blockquote>")
    except Exception as e:
        _restore_failed = True
        transient = _is_transient(e)
        err = str(e)[:200] or type(e).__name__
        S.update(restore_state="retrying" if transient else "failed", restore_error=err)
        log.exception("restore failed, backups paused so the good backup is not overwritten")
        if transient:
            _spawn(_restore_watchdog(bot))
            await _tell_owner(bot, "<b>Restore delayed</b>\n<blockquote>" + escape(err) +
                              "\nTelegram was too slow. The bot keeps retrying in the background and restores "
                              "the backup by itself. Backups stay paused until then. /restore retries now."
                              "</blockquote>")
        else:
            await _tell_owner(bot, "<b>Restore failed</b>\n<blockquote>" + escape(err) +
                              "\nBackups are paused so the good backup is not overwritten. Use /restore to "
                              "retry or /import to load a file.</blockquote>")


async def _extract_after_download(bot, doc) -> bytes:
    raw = await _download(bot, doc)
    return await asyncio.to_thread(_extract_db, raw)


async def _restore_watchdog(bot) -> None:
    """After a network failure at startup: retry until the restore works (1, 2, 4 ... 15 min)."""
    delay = 60
    while _restore_failed:
        await asyncio.sleep(delay)
        if not _restore_failed:
            return
        try:
            ok, text = await _late_restore(bot)
        except Exception as e:
            if _is_transient(e):
                log.warning("restore retry failed again: %s", e)
                delay = min(delay * 2, 900)
                continue
            log.exception("restore retry hit a real error, stopping retries")
            S.update(restore_state="failed", restore_error=str(e)[:200])
            await _tell_owner(bot, "<b>Restore failed</b>\n<blockquote>" + escape(str(e)[:200]) +
                              "\nUse /restore or /import.</blockquote>")
            return
        if ok:
            await _tell_owner(bot, f"<b>Backup restored</b>\n<blockquote>{escape(text)}</blockquote>")
            return
        delay = min(delay * 2, 900)


async def _late_restore(bot, force: bool = False) -> tuple[bool, str]:
    """Restore from the pinned backup while the bot is running. Keeps the local database when it is newer
    (unless force). The replaced database stays as <db>.bak. Raises on network errors."""
    global _restore_failed
    async with _lock:
        msg = await _find_backup(bot)
        if not msg:
            return False, "No backup found in the group."
        remote_ts = _msg_ts(msg)
        raw = await _extract_after_download(bot, msg.document)
        local_ts = int(await db.get_setting("last_backup_at") or 0)
        empty = await asyncio.to_thread(_local_is_empty)
        if not force and not empty and local_ts >= remote_ts - 120:
            _restore_failed = False
            S.update(restore_state="ok", restore_error="")
            return True, "The local database is already as new as the backup, kept it. Backups resumed."
        ok, text = await _apply_raw(raw)
        if ok:
            text = f"{escape(msg.document.file_name or '')}: {text} Previous database kept as .bak. Backups resumed."
        return ok, text


# ───────────── commands (owner and co-owners) ─────────────
ADMINS = F.from_user.id.in_({OWNER_ID, *CO_OWNERS})


@router.message(Command("backup"), ADMINS)
async def backup_cmd(m: Message):
    ok, text = await run_backup(m.bot, manual=True)
    await notice(m, "Backup sent" if ok else "Backup failed", text)


@router.message(Command("restore"), ADMINS)
async def restore_cmd(m: Message):
    """/restore retries loading the pinned backup now. /restore force replaces even a newer local database."""
    force = "force" in (m.text or "").lower().split()
    try:
        ok, text = await _late_restore(m.bot, force=force)
    except Exception as e:
        log.exception("manual restore failed")
        ok, text = False, (str(e)[:150] or type(e).__name__) + (
            " Telegram was too slow, try again in a minute." if _is_transient(e) else "")
    await notice(m, "Restore done" if ok else "Restore failed", text)


@router.message(Command("backupstatus"), ADMINS)
async def status_cmd(m: Message):
    next_in = max(0, int(S["next_run"] - time.time())) if S["next_run"] else 0
    lines = [
        f"Last backup: {_ago(S['last_ok'])}" + (f" · {S['last_size'] / 1024:.0f} KB" if S["last_size"] else ""),
        f"Next run in: {next_in // 60} min" if next_in else "Next run: soon",
        f"Skipped (unchanged): {S['skipped']}",
        f"Failed in a row: {S['fails']}",
        f"Restore: {S['restore_state']}" + (f" ({escape(S['restore_error'][:80])})" if S["restore_error"] else ""),
        f"Backups paused: {'yes' if _restore_failed else 'no'}",
    ]
    if S["last_error"]:
        lines.append("Last error: " + escape(S["last_error"][:150]))
    await notice(m, "Backup status", "\n".join(lines))


# ───────────── /import (owner and co-owners): load an old .db file ─────────────
async def import_db(bot, doc) -> tuple[bool, str]:
    """Replace the live database with an uploaded one and convert it. Returns (ok, text)."""
    if doc.file_size and doc.file_size > BOT_DOWNLOAD_LIMIT:
        return False, "The file is over 20 MB, Telegram does not let bots download it."
    try:
        raw = await _extract_after_download(bot, doc)
    except Exception as e:
        hint = " Telegram was too slow, send it again in a minute." if _is_transient(e) else ""
        return False, f"Could not read the file: {(str(e)[:150] or type(e).__name__)}{hint}"

    async with _lock:                                 # no backup runs while the database is swapped
        ok, text = await _apply_raw(raw)
    if not ok:
        return False, text
    await run_backup(bot, force=True)                 # new backup + pin right away
    return True, text + " Backup sent and pinned."


@router.message(Command("import"), ADMINS)
async def import_cmd(m: Message):
    doc = (m.document or (m.reply_to_message.document if m.reply_to_message else None))
    if not doc:
        return await notice(m, "Import", "Send the old .db (or backup .zip) file with /import as the caption, "
                                         "or reply to the file with /import.")
    ok, text = await import_db(m.bot, doc)
    await notice(m, "Database imported" if ok else "Import failed", text)
