"""/restore - copy the old SQLite data into MongoDB (owner only).

Usage:
  /restore                      reads the SQLite file at DB_PATH (default data/otaku_nexus.db)
  /restore  (reply to a .db)    downloads the replied .db / .sqlite file and restores from it
                                (Telegram bots can download files up to 20 MB; bigger files: put the file
                                on the server at DB_PATH instead)

Safe to run more than once: every row is upserted by its key, nothing is added twice,
and nothing already in MongoDB is deleted.
"""
import logging
import os
import tempfile

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from pymongo import ReplaceOne

import dbase
from config import DB_PATH, OWNER_ID

log = logging.getLogger(__name__)
router = Router()

# table -> (mongo collection, key fields used to upsert)
TABLES = {
    "settings": ("settings", ("key",)),
    "users": ("users", ("user_id",)),
    "chats": ("chats", ("chat_id",)),
    "characters": ("characters", ("char_id",)),
    "admins": ("admins", ("user_id",)),
    "collection": ("collection", ("user_id", "char_id")),
    "shop_offers": ("shop_offers", ("day", "user_id", "char_id")),
}
BATCH = 1000


async def _restore_table(src: aiosqlite.Connection, table: str, coll_name: str, keys: tuple) -> int | None:
    async with src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)) as cur:
        if await cur.fetchone() is None:
            return None
    coll = dbase.get_db()[coll_name]
    total, ops = 0, []
    async with src.execute(f"SELECT * FROM {table}") as cur:
        async for row in cur:
            doc = dict(row)
            if table == "users" and "coins" in doc and "nex" not in doc:     # very old databases
                doc["nex"] = doc.pop("coins")
            ops.append(ReplaceOne({k: doc[k] for k in keys}, doc, upsert=True))
            if len(ops) >= BATCH:
                await coll.bulk_write(ops, ordered=False)
                total += len(ops)
                ops = []
    if ops:
        await coll.bulk_write(ops, ordered=False)
        total += len(ops)
    return total


async def restore_from(path: str) -> dict[str, int | None]:
    results: dict[str, int | None] = {}
    async with aiosqlite.connect(path) as src:
        src.row_factory = aiosqlite.Row
        for table, (coll, keys) in TABLES.items():
            results[table] = await _restore_table(src, table, coll, keys)

    # keep the character ID counter ahead of every existing ID (IDs are never reused)
    top = await dbase.get_db().characters.find_one({}, {"char_id": 1, "_id": 0}, sort=[("char_id", -1)])
    seq = top["char_id"] if top else 0
    async with aiosqlite.connect(path) as src:
        try:
            async with src.execute("SELECT seq FROM sqlite_sequence WHERE name='characters'") as cur:
                r = await cur.fetchone()
            if r:
                seq = max(seq, r[0])
        except aiosqlite.Error:
            pass
    await dbase.get_db().counters.update_one({"_id": "characters"}, {"$max": {"seq": seq}}, upsert=True)
    return results


@router.message(Command("restore"))
async def restore_cmd(m: Message) -> None:
    if not m.from_user or m.from_user.id != OWNER_ID:      # owner only, everyone else silently ignored
        return

    tmp_path = None
    path = DB_PATH
    doc = m.reply_to_message.document if m.reply_to_message else None
    try:
        if doc:
            if doc.file_size and doc.file_size > 20 * 1024 * 1024:
                await m.reply("That file is over 20 MB, so a bot can't download it. "
                              f"Put it on the server at <code>{DB_PATH}</code> and send /restore again.",
                              parse_mode="HTML")
                return
            fd, tmp_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            await m.bot.download(doc, destination=tmp_path)
            path = tmp_path
        elif not os.path.isfile(path):
            await m.reply(f"No SQLite file found at <code>{path}</code>.\n"
                          "Reply to a .db file with /restore, or place the file there.", parse_mode="HTML")
            return

        note = await m.reply("Restoring data to MongoDB...")
        results = await restore_from(path)
        lines = ["<b>Restore finished</b>"]
        for table, n in results.items():
            lines.append(f"{table}: {'not in file' if n is None else f'{n} rows'}")
        await note.edit_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        log.exception("restore failed")
        await m.reply(f"Restore failed: <code>{type(e).__name__}: {e}</code>", parse_mode="HTML")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
