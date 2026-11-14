"""Otaku Nexus - MongoDB data layer (motor).

Same public functions as the old SQLite version, so the other modules keep working.
Rows are returned as plain dicts (row["name"] works as before).
"""
import asyncio
import re
import time

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from config import MONGO_DB, MONGO_URI

_client: AsyncIOMotorClient | None = None
_db = None
nex_lock = asyncio.Lock()    # serialises Nex transfers / bets across modules
_xfer_lock = asyncio.Lock()

NO_ID = {"_id": 0}
DEFAULT_DROP_EVERY = 100


def get_db():
    """The Motor database (used by restore.py)."""
    return _db


async def init_db() -> None:
    global _client, _db
    _client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=15000)
    _db = _client[MONGO_DB]
    await _client.admin.command("ping")
    await _db.users.create_index("user_id", unique=True)
    await _db.chats.create_index("chat_id", unique=True)
    await _db.characters.create_index("char_id", unique=True)
    await _db.characters.create_index("rarity")
    await _db.admins.create_index("user_id", unique=True)
    await _db.collection.create_index([("user_id", ASCENDING), ("char_id", ASCENDING)], unique=True)
    await _db.collection.create_index("char_id")
    await _db.shop_offers.create_index(
        [("day", ASCENDING), ("user_id", ASCENDING), ("char_id", ASCENDING)], unique=True)
    await _db.drops.create_index("chat_id", unique=True)


async def close_db() -> None:
    if _client:
        _client.close()


def _rx(q: str) -> re.Pattern:
    return re.compile(re.escape(q), re.IGNORECASE)


# ---------- settings ----------
async def get_setting(key: str) -> str | None:
    r = await _db.settings.find_one({"key": key})
    return r["value"] if r else None


async def set_setting(key: str, value: str) -> None:
    await _db.settings.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)


# ---------- users / chats ----------
async def upsert_user(user_id: int, username: str | None, first_name: str) -> None:
    await _db.users.update_one(
        {"user_id": user_id},
        {"$set": {"username": username, "first_name": first_name},
         "$setOnInsert": {"nex": 0, "favorite_id": None, "last_daily": 0, "joined_at": int(time.time())}},
        upsert=True)


async def upsert_chat(chat_id: int, title: str | None) -> None:
    await _db.chats.update_one(
        {"chat_id": chat_id},
        {"$set": {"title": title},
         "$setOnInsert": {"msg_count": 0, "drop_every": DEFAULT_DROP_EVERY, "drop_enabled": 1,
                          "added_at": int(time.time())}},
        upsert=True)


async def stats() -> tuple[int, int, int]:
    return (await _db.users.count_documents({}),
            await _db.chats.count_documents({}),
            await _db.characters.count_documents({}))


# ---------- currency (Nex) ----------
async def get_nex(user_id: int) -> int:
    r = await _db.users.find_one({"user_id": user_id}, {"nex": 1, "_id": 0})
    return r["nex"] if r else 0


async def add_nex(user_id: int, amount: int) -> int:
    """Add (or subtract with a negative amount) Nex. Balance never drops below 0. Returns new balance."""
    await _db.users.update_one(
        {"user_id": user_id},
        [{"$set": {"nex": {"$max": [0, {"$add": [{"$ifNull": ["$nex", 0]}, amount]}]}}}])
    return await get_nex(user_id)


async def claim_daily(user_id: int, amount: int, cooldown: int) -> tuple[bool, int, int]:
    """Atomic daily claim. Returns (claimed, balance, seconds_left). The user row must exist."""
    now = int(time.time())
    res = await _db.users.update_one(
        {"user_id": user_id, "last_daily": {"$lte": now - cooldown}},
        {"$inc": {"nex": amount}, "$set": {"last_daily": now}})
    if res.modified_count:
        return True, await get_nex(user_id), 0
    r = await _db.users.find_one({"user_id": user_id}, {"last_daily": 1, "_id": 0})
    left = max(1, (r["last_daily"] if r else 0) + cooldown - now)
    return False, await get_nex(user_id), left


# ---------- admins ----------
async def is_admin(user_id: int) -> bool:
    return await _db.admins.find_one({"user_id": user_id}) is not None


async def list_admins() -> list[int]:
    return [r["user_id"] async for r in _db.admins.find({}, NO_ID).sort("user_id", ASCENDING)]


async def get_user_name(user_id: int) -> str | None:
    r = await _db.users.find_one({"user_id": user_id}, {"first_name": 1, "_id": 0})
    return r["first_name"] if r else None


async def add_admin(user_id: int) -> None:
    await _db.admins.update_one({"user_id": user_id}, {"$setOnInsert": {"user_id": user_id}}, upsert=True)


async def del_admin(user_id: int) -> None:
    await _db.admins.delete_one({"user_id": user_id})


# ---------- characters ----------
CHAR_FIELDS = {"name", "anime", "gender", "rarity", "sub_rarity", "file_id", "db_msg_id", "media_type"}


async def _next_char_id() -> int:
    r = await _db.counters.find_one_and_update(
        {"_id": "characters"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
    return r["seq"]


async def add_character(name: str, anime: str, gender: str, rarity: str, sub_rarity: str | None,
                        file_id: str, added_by: int, artist_name: str,
                        media_type: str = "photo") -> int:
    """Insert a card and return its ID (IDs keep counting, never reused)."""
    char_id = await _next_char_id()
    await _db.characters.insert_one({
        "char_id": char_id, "name": name, "anime": anime, "gender": gender, "rarity": rarity,
        "sub_rarity": sub_rarity, "file_id": file_id, "added_by": added_by,
        "added_at": int(time.time()), "artist_name": artist_name, "db_msg_id": None,
        "media_type": media_type})
    return char_id


async def get_character(char_id: int):
    return await _db.characters.find_one({"char_id": char_id}, NO_ID)


async def update_character(char_id: int, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in CHAR_FIELDS}
    if not fields:
        return
    await _db.characters.update_one({"char_id": char_id}, {"$set": fields})


async def count_characters() -> int:
    return await _db.characters.count_documents({})


async def list_characters(offset: int = 0, limit: int = 15):
    """Newest first (highest ID first), for the /uploaded list."""
    cur = (_db.characters.find({}, {"_id": 0, "char_id": 1, "name": 1, "anime": 1, "rarity": 1, "sub_rarity": 1})
           .sort("char_id", DESCENDING).skip(offset).limit(limit))
    return await cur.to_list(length=limit)


async def delete_character(char_id: int) -> None:
    await _db.collection.delete_many({"char_id": char_id})
    await _db.drops.delete_many({"char_id": char_id})
    await _db.shop_offers.delete_many({"char_id": char_id})
    await _db.characters.delete_one({"char_id": char_id})


# ---------- drops ----------
async def get_chat(chat_id: int):
    return await _db.chats.find_one({"chat_id": chat_id}, NO_ID)


async def set_chat_drop(chat_id: int, every: int | None = None, enabled: bool | None = None) -> None:
    """Change a group's drop interval and / or on-off switch (creates the chat row if missing)."""
    sets: dict = {}
    if every is not None:
        sets["drop_every"] = every
    if enabled is not None:
        sets["drop_enabled"] = int(enabled)
    defaults = {"title": None, "msg_count": 0, "drop_every": DEFAULT_DROP_EVERY, "drop_enabled": 1,
                "added_at": int(time.time())}
    on_insert = {k: v for k, v in defaults.items() if k not in sets}
    update: dict = {"$setOnInsert": on_insert}
    if sets:
        update["$set"] = sets
    await _db.chats.update_one({"chat_id": chat_id}, update, upsert=True)


async def set_chat_count(chat_id: int, count: int) -> None:
    await _db.chats.update_one({"chat_id": chat_id}, {"$set": {"msg_count": count}})


def _pick_match(rarity: str, blocked_subs=(), exclude_id: int | None = None) -> dict:
    m: dict = {"rarity": rarity}
    if blocked_subs:
        m["sub_rarity"] = {"$nin": list(blocked_subs)}      # null / missing sub rarity also passes
    if exclude_id:
        m["char_id"] = {"$ne": exclude_id}
    return m


async def pick_character(rarity: str, exclude_id: int | None = None, blocked_subs=()):
    """One random card of a main rarity, skipping blocked sub rarities (event cards) and one char_id."""
    rows = await _db.characters.aggregate([
        {"$match": _pick_match(rarity, blocked_subs, exclude_id)},
        {"$sample": {"size": 1}}, {"$project": NO_ID}]).to_list(length=1)
    return rows[0] if rows else None


async def get_drop(chat_id: int):
    return await _db.drops.find_one({"chat_id": chat_id}, NO_ID)


async def swap_drop(chat_id: int, old_char_id: int | None, char_id: int, message_id: int) -> bool:
    """Make a new card the live drop. Returns True if the old drop was still unclaimed (so it ran away)."""
    replaced = False
    if old_char_id is not None:
        res = await _db.drops.delete_one({"chat_id": chat_id, "char_id": old_char_id})
        replaced = res.deleted_count == 1
    await _db.drops.replace_one(
        {"chat_id": chat_id},
        {"chat_id": chat_id, "char_id": char_id, "message_id": message_id, "dropped_at": int(time.time())},
        upsert=True)
    return replaced


async def claim_drop(chat_id: int, char_id: int) -> bool:
    """Atomic: only the first caller gets True, the drop row is gone for everyone else."""
    res = await _db.drops.delete_one({"chat_id": chat_id, "char_id": char_id})
    return res.deleted_count == 1


async def add_to_collection(user_id: int, char_id: int) -> int:
    """Add one copy to a user's collection. Returns the new quantity (1 = new entry)."""
    r = await _db.collection.find_one_and_update(
        {"user_id": user_id, "char_id": char_id},
        {"$inc": {"qty": 1}, "$setOnInsert": {"first_at": int(time.time())}},
        upsert=True, return_document=ReturnDocument.AFTER)
    return r["qty"]


def _text_filter(query: str, prefix: str = "") -> dict | None:
    q = query.strip()
    if not q:
        return None
    rx = _rx(q)
    cond = [{f"{prefix}{c}": rx} for c in ("name", "anime", "rarity", "sub_rarity")]
    if q.isdigit():
        cond.append({f"{prefix}char_id": int(q)})
    return {"$or": cond}


async def search_characters(query: str = "", offset: int = 0, limit: int = 50):
    """Inline mode: all cards (newest first), or filtered by name / series / rarity / sub rarity / ID."""
    flt = _text_filter(query) or {}
    cur = _db.characters.find(flt, NO_ID).sort("char_id", DESCENDING).skip(offset).limit(limit)
    return await cur.to_list(length=limit)


async def top_holders(char_id: int, limit: int = 10):
    """Users who own a card: most copies first, then whoever bound it earliest."""
    return await _db.collection.aggregate([
        {"$match": {"char_id": char_id}},
        {"$sort": {"qty": -1, "first_at": 1}},
        {"$lookup": {"from": "users", "localField": "user_id", "foreignField": "user_id", "as": "u"}},
        {"$unwind": "$u"},
        {"$limit": limit},
        {"$project": {"_id": 0, "user_id": 1, "username": "$u.username",
                      "first_name": "$u.first_name", "qty": 1}},
    ]).to_list(length=limit)


# ---------- harem (collection view) ----------
def _owned(user_id: int) -> list[dict]:
    """collection joined with characters (inner join): each doc is {user_id, char_id, qty, first_at, ch: {...}}."""
    return [
        {"$match": {"user_id": user_id}},
        {"$lookup": {"from": "characters", "localField": "char_id", "foreignField": "char_id", "as": "ch"}},
        {"$unwind": "$ch"},
    ]


_FLAT = {"_id": 0, "char_id": "$ch.char_id", "name": "$ch.name", "anime": "$ch.anime", "rarity": "$ch.rarity",
         "sub_rarity": "$ch.sub_rarity", "file_id": "$ch.file_id", "media_type": "$ch.media_type", "qty": 1}


async def collection_count(user_id: int, rarity: str | None = None) -> int:
    pipe = _owned(user_id)
    if rarity:
        pipe.append({"$match": {"ch.rarity": rarity}})
    pipe.append({"$count": "n"})
    rows = await _db.collection.aggregate(pipe).to_list(length=1)
    return rows[0]["n"] if rows else 0


async def collection_stats(user_id: int) -> tuple[int, int]:
    """(unique characters, total copies)."""
    rows = await _db.collection.aggregate(
        _owned(user_id) + [{"$group": {"_id": None, "n": {"$sum": 1}, "copies": {"$sum": "$qty"}}}]
    ).to_list(length=1)
    return (rows[0]["n"], rows[0]["copies"]) if rows else (0, 0)


async def collection_rarities(user_id: int) -> set[str]:
    rows = await _db.collection.aggregate(
        _owned(user_id) + [{"$group": {"_id": "$ch.rarity"}}]).to_list(length=None)
    return {r["_id"] for r in rows}


async def collection_page(user_id: int, rarity: str | None = None, offset: int = 0, limit: int = 10):
    """A user's characters, most recently bound first."""
    pipe = _owned(user_id)
    if rarity:
        pipe.append({"$match": {"ch.rarity": rarity}})
    pipe += [{"$sort": {"first_at": -1, "char_id": -1}}, {"$skip": offset}, {"$limit": limit},
             {"$project": _FLAT}]
    return await _db.collection.aggregate(pipe).to_list(length=limit)


async def search_collection(user_id: int, query: str = "", offset: int = 0, limit: int = 50):
    """Inline mode: one user's own characters (most recently bound first), optional text filter, with qty."""
    pipe = _owned(user_id)
    flt = _text_filter(query, "ch.")
    if flt:
        pipe.append({"$match": flt})
    pipe += [{"$sort": {"first_at": -1, "char_id": -1}}, {"$skip": offset}, {"$limit": limit},
             {"$replaceRoot": {"newRoot": {"$mergeObjects": ["$ch", {"qty": "$qty"}]}}},
             {"$project": NO_ID}]
    return await _db.collection.aggregate(pipe).to_list(length=limit)


# ---------- favorite / gift ----------
async def owned_qty(user_id: int, char_id: int) -> int:
    r = await _db.collection.find_one({"user_id": user_id, "char_id": char_id}, {"qty": 1, "_id": 0})
    return r["qty"] if r else 0


async def get_favorite(user_id: int) -> int | None:
    r = await _db.users.find_one({"user_id": user_id}, {"favorite_id": 1, "_id": 0})
    return r.get("favorite_id") if r else None


async def set_favorite(user_id: int, char_id: int | None) -> None:
    await _db.users.update_one({"user_id": user_id}, {"$set": {"favorite_id": char_id}})


async def transfer_character(from_id: int, to_id: int, char_id: int) -> bool:
    """Move one copy from one user to another. False if the sender no longer owns it.
    If the sender gave away their last copy of their favorite, the favorite is cleared."""
    async with _xfer_lock:
        res = await _db.collection.update_one(
            {"user_id": from_id, "char_id": char_id, "qty": {"$gte": 1}}, {"$inc": {"qty": -1}})
        if res.modified_count != 1:
            return False
        try:
            await _db.collection.delete_one({"user_id": from_id, "char_id": char_id, "qty": {"$lte": 0}})
            await _db.collection.update_one(
                {"user_id": to_id, "char_id": char_id},
                {"$inc": {"qty": 1}, "$setOnInsert": {"first_at": int(time.time())}}, upsert=True)
            if await owned_qty(from_id, char_id) == 0:
                await _db.users.update_one(
                    {"user_id": from_id, "favorite_id": char_id}, {"$set": {"favorite_id": None}})
            return True
        except Exception:
            # give the copy back to the sender so nothing is lost
            await _db.collection.update_one(
                {"user_id": from_id, "char_id": char_id},
                {"$inc": {"qty": 1}, "$setOnInsert": {"first_at": int(time.time())}}, upsert=True)
            raise


# ---------- harem: anime-wise view ----------
_AKEY = {"$toLower": {"$trim": {"input": "$ch.anime"}}}


async def collection_anime_count(user_id: int) -> int:
    """Number of different series the user owns at least one character of."""
    rows = await _db.collection.aggregate(
        _owned(user_id) + [{"$group": {"_id": _AKEY}}, {"$count": "n"}]).to_list(length=1)
    return rows[0]["n"] if rows else 0


async def collection_anime_page(user_id: int, offset: int = 0, limit: int = 5):
    """Series of a user's collection: most collected first. Rows: akey, anime, collected, total."""
    rows = await _db.collection.aggregate(_owned(user_id) + [
        {"$group": {"_id": _AKEY, "anime": {"$min": "$ch.anime"}, "collected": {"$sum": 1}}},
        {"$sort": {"collected": -1, "_id": 1}}, {"$skip": offset}, {"$limit": limit},
    ]).to_list(length=limit)
    if not rows:
        return []
    keys = [r["_id"] for r in rows]
    totals = await _db.characters.aggregate([
        {"$addFields": {"akey": {"$toLower": {"$trim": {"input": "$anime"}}}}},
        {"$match": {"akey": {"$in": keys}}},
        {"$group": {"_id": "$akey", "total": {"$sum": 1}}},
    ]).to_list(length=None)
    tmap = {t["_id"]: t["total"] for t in totals}
    return [{"akey": r["_id"], "anime": r["anime"], "collected": r["collected"],
             "total": tmap.get(r["_id"], r["collected"])} for r in rows]


async def collection_by_animes(user_id: int, keys: list[str]):
    """A user's characters inside the given series keys (akey), ordered by series then ID."""
    if not keys:
        return []
    pipe = _owned(user_id) + [
        {"$addFields": {"akey": _AKEY}},
        {"$match": {"akey": {"$in": keys}}},
        {"$sort": {"akey": 1, "char_id": 1}},
        {"$project": {**_FLAT, "akey": 1}},
    ]
    return await _db.collection.aggregate(pipe).to_list(length=None)


# ---------- shop ----------
async def pick_characters(rarity: str, count: int, blocked_subs=()):
    """`count` different random cards of a main rarity, skipping blocked (event) sub rarities."""
    return await _db.characters.aggregate([
        {"$match": _pick_match(rarity, blocked_subs)},
        {"$sample": {"size": count}}, {"$project": NO_ID}]).to_list(length=count)


async def shop_has_offers(day: str, user_id: int) -> bool:
    return await _db.shop_offers.find_one({"day": day, "user_id": user_id}) is not None


async def shop_add(day: str, user_id: int, items: list[tuple[int, str, int]]) -> None:
    """items = (char_id, rarity, price): this user's cards for the day."""
    for seq, (char_id, rarity, price) in enumerate(items):
        await _db.shop_offers.update_one(
            {"day": day, "user_id": user_id, "char_id": char_id},
            {"$setOnInsert": {"rarity": rarity, "price": price, "bought": 0, "seq": seq}},
            upsert=True)


def _offer_pipe(day: str, user_id: int, char_id: int | None = None) -> list[dict]:
    m: dict = {"day": day, "user_id": user_id}
    if char_id is not None:
        m["char_id"] = char_id
    return [
        {"$match": m},
        {"$sort": {"seq": 1}},
        {"$lookup": {"from": "characters", "localField": "char_id", "foreignField": "char_id", "as": "ch"}},
        {"$unwind": "$ch"},       # deleted characters are skipped
        {"$replaceRoot": {"newRoot": {"$mergeObjects": ["$ch", {"price": "$price", "bought": "$bought"}]}}},
        {"$project": NO_ID},
    ]


async def shop_items(day: str, user_id: int):
    """The user's cards for the day with price and bought flag (deleted characters are skipped)."""
    return await _db.shop_offers.aggregate(_offer_pipe(day, user_id)).to_list(length=None)


async def shop_item(day: str, user_id: int, char_id: int):
    rows = await _db.shop_offers.aggregate(_offer_pipe(day, user_id, char_id)).to_list(length=1)
    return rows[0] if rows else None


async def shop_buy(user_id: int, day: str, char_id: int) -> tuple[str, int]:
    """Buy the user's own offer. Returns (status, balance). status: ok, gone, limit, poor.
    Takes the shared Nex lock; every step is a guarded update and is undone if a later step fails."""
    async with nex_lock:
        item = await shop_item(day, user_id, char_id)
        if not item:
            return "gone", await get_nex(user_id)
        price, bal = item["price"], await get_nex(user_id)
        if item["bought"]:
            return "limit", bal
        if bal < price:
            return "poor", bal
        flag = await _db.shop_offers.update_one(
            {"day": day, "user_id": user_id, "char_id": char_id, "bought": 0}, {"$set": {"bought": 1}})
        if flag.modified_count != 1:
            return "limit", bal
        pay = await _db.users.update_one(
            {"user_id": user_id, "nex": {"$gte": price}}, {"$inc": {"nex": -price}})
        if pay.modified_count != 1:
            await _db.shop_offers.update_one(
                {"day": day, "user_id": user_id, "char_id": char_id}, {"$set": {"bought": 0}})
            return "poor", await get_nex(user_id)
        try:
            await add_to_collection(user_id, char_id)
        except Exception:
            await _db.shop_offers.update_one(
                {"day": day, "user_id": user_id, "char_id": char_id}, {"$set": {"bought": 0}})
            await _db.users.update_one({"user_id": user_id}, {"$inc": {"nex": price}})
            raise
        return "ok", await get_nex(user_id)


async def shop_cleanup(keep_day: str) -> None:
    """Old days are not needed any more."""
    await _db.shop_offers.delete_many({"day": {"$lt": keep_day}})
