"""Otaku Nexus - drops, collection actions and inline mode (one file).

Drops
- DropCounter (outer middleware): counts group messages, triggers a drop every N messages.
- Drop message: spoiler card with a random in-character quote (DROP_QUOTES) and the bind command, plain HTML.
- /bind <name>: first correct name wins the card. The result is a rich message (HTML fallback) with a
  View Collection button.
- /setdrop: group admins change the interval or switch drops on / off.
- /dropnow: owner / co-owners, forces a drop (testing).

Collection actions
- /fav ID: choose the favorite shown on top of /harem (Confirm / Cancel).
- /gift ID (reply to a user): give one copy to someone (Confirm / Cancel).

Inline mode (@botusername)
- empty query: every character, newest first (50 per page); text filters by name, series, rarity, sub rarity, ID.
- "collection.<user id>" (the View Collection button): that user's characters; text after it filters the same
  way. Plain "collection" = the asker's own collection (personal).
- Each result has a "Check binds" button (top 10 owners, private alert).
- Spoiler: Telegram has no spoiler flag for inline results, so the bot edits the sent inline message with
  has_spoiler=True after chosen_inline_result. BotFather needs /setinline and /setinlinefeedback (100%).
"""
import asyncio
import inspect
import logging
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from html import escape

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery, ChosenInlineResult, InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery,
    InlineQueryResultArticle, InlineQueryResultCachedMpeg4Gif, InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo, InputMediaAnimation, InputMediaPhoto, InputMediaVideo,
    InputTextMessageContent, Message,
)

import dbase as db
import richmsg
import waifu
from configaration import (
    BIG_RARITIES, CURRENCY_SYMBOL, DROP_ACTIVE_EVENTS, DROP_DUP_NEX, DROP_MAX_EVERY,
    DROP_MIN_EVERY, DROP_MSG_GAP, DROP_PRESETS, DROP_QUOTES, EVENT_SUBS, RARITIES, RICH_MESSAGES,
    SUB_RARITIES,
)
from minigame import notice, topic_id
from richmsg import esc
from waifu import mention, rarity_label, send_media, with_styles

router = Router()
log = logging.getLogger("otaku.drop")

GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
COUNTED = {"text", "photo", "video", "sticker", "animation", "voice", "audio", "document", "video_note"}


# ───────────── per-chat state (in memory, counter flushed to the DB every few messages) ─────────────
@dataclass
class ChatState:
    every: int
    enabled: bool
    count: int
    saved: int = 0
    last_char: int | None = None
    seen: dict[int, float] = field(default_factory=dict)     # user id -> last counted time
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_states: dict[int, ChatState] = {}
_tasks: set[asyncio.Task] = set()


async def state_of(chat) -> ChatState:
    st = _states.get(chat.id)
    if st:
        return st
    await db.upsert_chat(chat.id, chat.title)
    row = await db.get_chat(chat.id)
    st = ChatState(every=row["drop_every"], enabled=bool(row["drop_enabled"]), count=row["msg_count"])
    st.saved = st.count
    return _states.setdefault(chat.id, st)


# ───────────── text helpers ─────────────
def sub_emoji(name: str | None) -> str:
    return next((emo for n, emo, *_ in SUB_RARITIES if n == name), "") if name else ""


def name_tag(c) -> str:
    """'Makima (🎃)': sub rarity shown only as its emoji."""
    emo = sub_emoji(c["sub_rarity"])
    return f"{c['name']} ({emo})" if emo else c["name"]


def blanks(name: str) -> str:
    """'Makima Sama' -> '▢▢▢▢▢ ▢▢▢▢' (spaces and punctuation stay)."""
    return "".join("▢" if ch.isalnum() else ch for ch in name)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def name_matches(guess: str, name: str) -> bool:
    """Full name, or every guessed word (3+ letters) is a word of the name. Order does not matter."""
    g, n = _norm(guess), _norm(name)
    if not g:
        return False
    if g == n:
        return True
    words, parts = g.split(), n.split()
    return all(len(w) >= 3 and w in parts for w in words)


def fmt_speed(sec: int) -> str:
    return f"{sec}s" if sec < 60 else f"{sec // 60}m {sec % 60}s"


# ───────────── drop message (plain HTML, no rich) ─────────────
_SC = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
DROP_RARITY_EMO = {n: e for n, e, *_ in RARITIES}
DROP_EXAMPLE_QUOTE = "I crossed a thousand worlds for this moment !"


def small_caps(text: str) -> str:
    """Title-style small caps: the first letter of every word stays a capital, the rest become small caps
    ('Hello world' -> 'Hᴇʟʟᴏ Wᴏʀʟᴅ'). Anything that is not a letter is kept as it is."""
    out = []
    for word in text.split(" "):
        out.append(word[:1].upper() + "".join(_SC.get(ch, ch) for ch in word[1:].lower()) if word else word)
    return " ".join(out)


def drop_caption(c) -> str:
    """Drop message: bold small-caps title with the rarity emoji, a small-caps quote in a blockquote (random,
    from the example line and the first line of every DROP_QUOTES pair), then the bind line.
    '/bind' and the rarity (name: emoji) are not converted."""
    quote = random.choice([DROP_EXAMPLE_QUOTE] + [h for h, _ in DROP_QUOTES])
    emo = DROP_RARITY_EMO.get(c["rarity"], "")
    return (
        f"<b>{small_caps('A new character')} ({escape(c['rarity'])}: {emo}) {small_caps('has appeared')} !</b>\n\n"
        f"<blockquote>{escape(small_caps(quote))}</blockquote>\n\n"
        f"<b>/bind<i> {small_caps('name and make me yours before i vanish')}</i></b>"
    )


def slipped_caption(c) -> str:
    name = escape(name_tag(c)) if c else "This character"
    return f"<b>{name} slipped away</b>\n<blockquote>Nobody named them in time.</blockquote>"


# ───────────── picking and sending a drop ─────────────
def rarity_order() -> list[str]:
    """All main rarities in a weighted random order (heavier weight tends to come first)."""
    pool = [(n, w) for n, _, w, *_ in RARITIES]
    order = []
    while pool:
        pick = random.choices(pool, weights=[w for _, w in pool])[0]
        order.append(pick[0])
        pool.remove(pick)
    return order


async def pick_drop(last_id: int | None):
    blocked = [s for s in EVENT_SUBS if s not in DROP_ACTIVE_EVENTS]
    for exclude in (last_id, None):          # second pass only matters when the pool has a single card
        for rarity in rarity_order():
            c = await db.pick_character(rarity, exclude, blocked)
            if c:
                return c
    return None


async def send_drop(bot, chat_id: int, thread: int | None, st: ChatState) -> bool:
    async with st.lock:
        st.count = st.saved = 0
        await db.set_chat_count(chat_id, 0)
        c = await pick_drop(st.last_char)
        if not c:
            log.warning("drop skipped in %s: no characters available", chat_id)
            return False
        old = await db.get_drop(chat_id)
        try:
            msg = await send_media(
                (bot, chat_id), c["media_type"], c["file_id"], drop_caption(c),
                spoiler=True, message_thread_id=thread)
        except TelegramAPIError as e:
            log.warning("drop failed in %s (card %s): %s", chat_id, c["char_id"], e)
            return False
        replaced = await db.swap_drop(chat_id, old["char_id"] if old else None, c["char_id"], msg.message_id)
        st.last_char = c["char_id"]
        if old and replaced:
            try:
                await bot.edit_message_caption(
                    chat_id=chat_id, message_id=old["message_id"],
                    caption=slipped_caption(await db.get_character(old["char_id"])))
            except TelegramAPIError as e:
                log.debug("could not mark old drop in %s: %s", chat_id, e)
        return True


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _drop_task(bot, chat_id, thread, st) -> None:
    try:
        await send_drop(bot, chat_id, thread, st)
    except Exception:
        log.exception("drop crashed in %s", chat_id)


# ───────────── message counter ─────────────
class DropCounter(BaseMiddleware):
    """Counts group messages without consuming them, so every other handler still runs."""

    async def __call__(self, handler, event: Message, data):
        try:
            await self._count(event)
        except Exception:
            log.exception("drop counter failed")
        return await handler(event, data)

    async def _count(self, m: Message) -> None:
        if m.chat.type not in GROUPS or not m.from_user or m.from_user.is_bot:
            return
        if m.content_type not in COUNTED or (m.text and m.text.startswith("/")):
            return
        st = await state_of(m.chat)
        if not st.enabled:
            return
        now = time.monotonic()
        if now - st.seen.get(m.from_user.id, -1e9) < DROP_MSG_GAP:
            return
        st.seen[m.from_user.id] = now
        if len(st.seen) > 500:
            st.seen = {m.from_user.id: now}
        st.count += 1
        if st.count >= st.every and not st.lock.locked():
            _spawn(_drop_task(m.bot, m.chat.id, topic_id(m), st))
        elif st.count - st.saved >= 10:
            st.saved = st.count
            await db.set_chat_count(m.chat.id, st.count)


# ───────────── /bind ─────────────
def _footer(qty: int) -> str:
    """Plain-text footer line: new entry vs duplicate."""
    if qty == 1:
        return "Welcome to the family. Keep collecting!"
    bonus = f" +{CURRENCY_SYMBOL} {DROP_DUP_NEX}" if DROP_DUP_NEX > 0 else ""
    return f"Copy x{qty}. Your bond grows stronger!{bonus}"


def result_md(user, c, qty: int, secs: int) -> str:
    who = f"[{esc(user.first_name)}](tg://user?id={user.id})"
    big = c["rarity"] in BIG_RARITIES
    head = f"{'#' if big else '##'} Congratulations, {who}!"
    return (
        f"{head}\n\n"
        f"### You have bound this character\n\n"
        f"---\n\n"
        f"**Name :** {esc(name_tag(c))}\n\n"
        f"**Rarity :** {esc(rarity_label(c['rarity']))}\n\n"
        f"**Series :** {esc(c['anime'])}\n\n"
        f"**ID :** {c['char_id']}\n\n"
        f"**Time Taken :** {fmt_speed(secs)}\n\n"
        f"---\n\n"
        f"###### {esc(_footer(qty))}"
    )


def result_html(user, c, qty: int, secs: int) -> str:
    return (
        f"<b>Congratulations, {mention(user.id, user.first_name)}!</b>\n"
        f"You have bound this character\n"
        f"──────────\n"
        f"<b>Name :</b> {escape(name_tag(c))}\n"
        f"<b>Rarity :</b> {rarity_label(c['rarity'])}\n"
        f"<b>Series :</b> {escape(c['anime'])}\n"
        f"<b>ID :</b> {c['char_id']}\n"
        f"<b>Time Taken :</b> {fmt_speed(secs)}\n"
        f"──────────\n"
        f"{escape(_footer(qty))}"
    )


COLLECTION_QUERY = "collection"        # inline query: "collection.<user id>" lists that user's collection


def collection_btn(uid: int, styled: bool = True) -> InlineKeyboardButton:
    """'View Collection': opens inline mode in the current chat with that user's collection."""
    extra = {"style": "primary"} if styled else {}
    return InlineKeyboardButton(text="View Collection",
                                switch_inline_query_current_chat=f"{COLLECTION_QUERY}.{uid}", **extra)


async def send_result(m: Message, md: str, html: str) -> None:
    """Bind result (rich, HTML fallback) as a reply to the /bind message, with the View Collection button."""
    for rich in ((True, False) if RICH_MESSAGES else (False,)):
        for styled in (True, False):
            kb = InlineKeyboardMarkup(inline_keyboard=[[collection_btn(m.from_user.id, styled)]])
            try:
                if rich:
                    await richmsg.send_rich(m.bot, m.chat.id, md, reply_to_message_id=m.message_id,
                                            message_thread_id=topic_id(m), reply_markup=kb)
                else:
                    await m.reply(html, reply_markup=kb)
                return
            except Exception as e:
                log.warning("bind result failed (rich=%s styled=%s): %s", rich, styled, e)


@router.message(Command("bind"), F.chat.type.in_(GROUPS))
async def bind_cmd(m: Message, command: CommandObject):
    if not m.from_user or m.from_user.is_bot:
        return
    guess = (command.args or "").strip()
    if not guess:
        await notice(m, "Bind", "Send /bind followed by the character's name.")
        return
    d = await db.get_drop(m.chat.id)
    if not d:
        return
    c = await db.get_character(d["char_id"])
    if not c or not name_matches(guess, c["name"]):
        return                                           # wrong guesses stay silent
    if not await db.claim_drop(m.chat.id, c["char_id"]):
        return                                           # someone was faster
    u = m.from_user
    await db.upsert_user(u.id, u.username, u.first_name)
    qty = await db.add_to_collection(u.id, c["char_id"])
    if qty > 1 and DROP_DUP_NEX > 0:
        await db.add_nex(u.id, DROP_DUP_NEX)
    secs = max(0, int(time.time()) - d["dropped_at"])
    await send_result(m, result_md(u, c, qty, secs), result_html(u, c, qty, secs))


# ───────────── /setdrop ─────────────
async def can_control(bot, chat_id: int, user_id: int) -> bool:
    if user_id in waifu.TOP:
        return True
    try:
        mem = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        return False
    return mem.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


def panel_text(st: ChatState) -> str:
    left = max(0, st.every - st.count)
    return (
        "<b>Drop settings</b>\n"
        f"<blockquote>Status: {'On' if st.enabled else 'Off'}\n"
        f"Every: {st.every} messages\n"
        f"Next drop in: {left} messages</blockquote>"
    )


def panel_kb(st: ChatState, styled: bool = True) -> InlineKeyboardMarkup:
    def b(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
        extra = {"style": style} if styled and style else {}
        return InlineKeyboardButton(text=text, callback_data=data, **extra)
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("Drops: On" if st.enabled else "Drops: Off", "sd:t", "success" if st.enabled else "danger")],
        [b(str(n), f"sd:i:{n}", "primary" if n == st.every else None) for n in DROP_PRESETS],
        [b("Close", "sd:x")],
    ])


async def _apply(chat_id: int, st: ChatState, every: int | None = None, enabled: bool | None = None) -> None:
    await db.set_chat_drop(chat_id, every=every, enabled=enabled)
    if every is not None:
        st.every = every
    if enabled is not None:
        st.enabled = enabled


@router.message(Command("setdrop"), F.chat.type.in_(GROUPS))
async def setdrop_cmd(m: Message, command: CommandObject):
    anon_admin = bool(m.sender_chat and m.sender_chat.id == m.chat.id)     # "remain anonymous" admins
    if not anon_admin and not (m.from_user and await can_control(m.bot, m.chat.id, m.from_user.id)):
        await notice(m, "Drop settings", "Only group admins can change drops.")
        return
    st = await state_of(m.chat)
    arg = (command.args or "").strip().lower()
    if not arg:
        await with_styles(lambda s: m.reply(panel_text(st), reply_markup=panel_kb(st, s)))
    elif arg in ("on", "off"):
        await _apply(m.chat.id, st, enabled=arg == "on")
        await notice(m, "Drop settings", f"Drops are now {arg}.")
    elif arg.isdigit():
        n = int(arg)
        if not DROP_MIN_EVERY <= n <= DROP_MAX_EVERY:
            await notice(m, "Drop settings", f"Choose a number from {DROP_MIN_EVERY} to {DROP_MAX_EVERY}.")
            return
        await _apply(m.chat.id, st, every=n)
        await notice(m, "Drop settings", f"A character will drop every {n} messages.")
    else:
        await notice(m, "Drop settings", "Use /setdrop, /setdrop 150, /setdrop on or /setdrop off.")


@router.callback_query(F.data.startswith("sd:"))
async def setdrop_cb(call: CallbackQuery):
    if not call.message:
        return await call.answer()
    chat = call.message.chat
    if not await can_control(call.bot, chat.id, call.from_user.id):
        return await call.answer("Group admins only.", show_alert=True)
    st = await state_of(chat)
    parts = call.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "x":
        try:
            await call.message.delete()
        except TelegramAPIError:
            pass
        return await call.answer()
    if action == "t":
        await _apply(chat.id, st, enabled=not st.enabled)
    elif action == "i" and len(parts) > 2 and parts[2].isdigit():
        n = int(parts[2])
        if DROP_MIN_EVERY <= n <= DROP_MAX_EVERY:
            await _apply(chat.id, st, every=n)
    try:
        await with_styles(lambda s: call.message.edit_text(panel_text(st), reply_markup=panel_kb(st, s)))
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise
    await call.answer()


# ───────────── /dropnow (owner and co-owners, for testing) ─────────────
@router.message(Command("dropnow"), F.chat.type.in_(GROUPS), F.from_user.id.in_(waifu.TOP))
async def dropnow_cmd(m: Message):
    st = await state_of(m.chat)
    if not await send_drop(m.bot, m.chat.id, topic_id(m), st):
        await notice(m, "Drop", "Could not drop. Check that characters exist and the log for details.")


# ───────────── shared: character picture + rich caption ─────────────
# The picture is a media block inside a rich message (same as /start). If rich fails, the picture is sent
# as a normal photo / video / GIF with an HTML caption.
_EDIT_MEDIA = "media" in inspect.signature(richmsg.edit_rich).parameters


def mention_md(uid: int, name: str) -> str:
    return f"[{esc(name)}](tg://user?id={uid})"


def card_media(c) -> tuple[str, str]:
    return c["file_id"], c["media_type"]


def card_line(c) -> str:
    """Plain text 'Name (sub emoji) - 🔮 Rarity' (no brackets when the card has no sub rarity)."""
    return f"{name_tag(c)} - {rarity_label(c['rarity'])}"


def card_text(c) -> str:
    """HTML card block for /fav and /gift (no quote): the line above, then the ID."""
    return f"{escape(card_line(c))}\nID {c['char_id']}"


def card_aside(c) -> str:
    """Rich Markdown card block for /fav and /gift (plain text, no quote any more)."""
    return f"{esc(card_line(c))}\n\nID {c['char_id']}"


async def reply_card(m: Message, c, caption: str, kb_factory) -> None:
    """Fallback: reply with the picture as a normal media message and an HTML caption."""
    send = {"photo": m.reply_photo, "video": m.reply_video,
            "animation": m.reply_animation}.get(c["media_type"], m.reply_photo)
    await with_styles(lambda s: send(c["file_id"], caption=caption, reply_markup=kb_factory(s)))


async def send_card_rich(m: Message, md: str, html: str, c, kb_factory) -> None:
    """Rich message with the picture on top, as a reply. Falls back to photo + HTML caption."""
    if RICH_MESSAGES:
        for styled in (True, False):
            try:
                await richmsg.send_rich(
                    m.bot, m.chat.id, md, media=card_media(c), reply_to_message_id=m.message_id,
                    message_thread_id=topic_id(m), reply_markup=kb_factory(styled))
                return
            except Exception as e:
                log.warning("rich card failed (styled=%s): %s", styled, e)
    await reply_card(m, c, html, kb_factory)


async def _resend(call: CallbackQuery, md: str, c, kb) -> None:
    """Used when richmsg.edit_rich cannot carry a picture: send the new version, delete the old one."""
    msg = call.message
    kw = {"reply_to_message_id": msg.reply_to_message.message_id} if msg.reply_to_message else {}
    await richmsg.send_rich(call.bot, msg.chat.id, md, media=card_media(c),
                            message_thread_id=topic_id(msg), reply_markup=kb, **kw)
    try:
        await msg.delete()
    except TelegramAPIError:
        pass


async def edit_card(call: CallbackQuery, md: str, html: str, c, kb_factory) -> None:
    """Edit a card message (rich with picture) in place. kb_factory(styled) may return None (no buttons).
    If Telegram refuses the in-place edit with the picture, the new version is sent and the old one deleted."""
    msg = call.message
    if RICH_MESSAGES:
        for styled in (True, False):
            kb = kb_factory(styled)
            try:
                if c is not None and not _EDIT_MEDIA:
                    await _resend(call, md, c, kb)
                else:
                    kw = {"media": card_media(c)} if c is not None else {}
                    await richmsg.edit_rich(call.bot, msg.chat.id, msg.message_id, md, reply_markup=kb, **kw)
                return
            except TelegramBadRequest as e:
                if "not modified" in str(e).lower():
                    return
                log.warning("rich card edit failed (styled=%s): %s", styled, e)
            except Exception as e:
                log.warning("rich card edit failed (styled=%s): %s", styled, e)
            if c is not None and _EDIT_MEDIA:
                try:
                    await _resend(call, md, c, kb)
                    return
                except Exception as e:
                    log.warning("rich card resend failed (styled=%s): %s", styled, e)
    try:                                              # normal message (photo caption or text)
        if msg.text:
            await with_styles(lambda s: msg.edit_text(html, reply_markup=kb_factory(s)))
        else:
            await with_styles(lambda s: msg.edit_caption(caption=html, reply_markup=kb_factory(s)))
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("card edit failed: %s", e)


async def finish_card(call: CallbackQuery, c, title: str, md_body: str = "", html_body: str = "") -> None:
    """Replace a confirmation message with a result (same picture, no buttons)."""
    md = f"## {esc(title)}" + (f"\n\n{md_body}" if md_body else "")
    html = f"<b>{escape(title)}</b>" + (f"\n{html_body}" if html_body else "")
    await edit_card(call, md, html, c, lambda s: None)


# ───────────── /fav ─────────────
def fav_kb(uid: int, cid: int, styled: bool = True) -> InlineKeyboardMarkup:
    def b(text, data, style):
        return InlineKeyboardButton(text=text, callback_data=data, **({"style": style} if styled else {}))
    return InlineKeyboardMarkup(inline_keyboard=[[
        b("Confirm", f"fv:y:{uid}:{cid}", "success"),
        b("Cancel", f"fv:n:{uid}:{cid}", "danger"),
    ]])


@router.message(Command("fav"))
async def fav_cmd(m: Message, command: CommandObject):
    u = m.from_user
    if not u:
        return
    arg = (command.args or "").strip()
    if not arg.isascii() or not arg.isdigit():
        return await notice(m, "Favorite", "Use /fav ID. Example: /fav 12")
    cid = int(arg)
    try:
        await db.upsert_user(u.id, u.username, u.first_name)
        c = await db.get_character(cid)
        if not c or not await db.owned_qty(u.id, cid):
            return await notice(m, "Favorite", "You don't own a character with that ID.")
        if await db.get_favorite(u.id) == cid:
            return await notice(m, "Favorite", f"{c['name']} is already your favorite.")
    except Exception:
        log.exception("/fav database error")
        return await notice(m, "Something went wrong", "Try again in a moment.")
    md = f"## Set as your favorite?\n\n{card_aside(c)}"
    html = f"<b>Set as your favorite?</b>\n{card_text(c)}"
    try:
        await send_card_rich(m, md, html, c, lambda s: fav_kb(u.id, cid, s))
    except Exception as e:
        log.warning("/fav picture failed (card %s): %s", cid, e)
        await m.reply(html, reply_markup=fav_kb(u.id, cid, False))


@router.callback_query(F.data.startswith("fv:"))
async def fav_cb(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 4 or not call.message:
        return await call.answer()
    try:
        act, uid, cid = parts[1], int(parts[2]), int(parts[3])
    except ValueError:
        return await call.answer()
    if call.from_user.id != uid:
        return await call.answer("Only the person who used /fav can confirm this.", show_alert=True)
    try:
        c = await db.get_character(cid)
        if act == "n":
            await finish_card(call, c, "Cancelled")
            return await call.answer()
        if not c or not await db.owned_qty(uid, cid):
            await finish_card(call, c, "Not available", esc("You no longer own this character."),
                              "You no longer own this character.")
            return await call.answer()
        await db.set_favorite(uid, cid)
        await finish_card(call, c, "Favorite updated", card_aside(c), card_text(c))
    except Exception:
        log.exception("/fav callback failed")
        return await call.answer("Something went wrong.", show_alert=True)
    await call.answer()


# ───────────── /gift ─────────────
def gift_kb(uid: int, target: int, cid: int, styled: bool = True) -> InlineKeyboardMarkup:
    def b(text, data, style):
        return InlineKeyboardButton(text=text, callback_data=data, **({"style": style} if styled else {}))
    return InlineKeyboardMarkup(inline_keyboard=[[
        b("Confirm", f"gf:y:{uid}:{target}:{cid}", "success"),
        b("Cancel", f"gf:n:{uid}:{target}:{cid}", "danger"),
    ]])


@router.message(Command("gift"))
async def gift_cmd(m: Message, command: CommandObject):
    u = m.from_user
    if not u:
        return
    r = m.reply_to_message
    arg = (command.args or "").strip()
    if not r or not r.from_user or r.forum_topic_created or not arg.isascii() or not arg.isdigit():
        return await notice(m, "Gift", "Reply to someone with /gift ID. Example: /gift 12")
    t = r.from_user
    if t.is_bot:
        return await notice(m, "Gift", "You can't gift a character to a bot.")
    if t.id == u.id:
        return await notice(m, "Gift", "You can't gift a character to yourself.")
    cid = int(arg)
    try:
        await db.upsert_user(u.id, u.username, u.first_name)
        await db.upsert_user(t.id, t.username, t.first_name)
        c = await db.get_character(cid)
        if not c or not await db.owned_qty(u.id, cid):
            return await notice(m, "Gift", "You don't own a character with that ID.")
    except Exception:
        log.exception("/gift database error")
        return await notice(m, "Something went wrong", "Try again in a moment.")
    md = f"## Gift this character?\n\nTo {mention_md(t.id, t.first_name)}\n\n{card_aside(c)}"
    html = (f"<b>Gift this character to {mention(t.id, t.first_name)}?</b>\n"
            f"{card_text(c)}")
    try:
        await send_card_rich(m, md, html, c, lambda s: gift_kb(u.id, t.id, cid, s))
    except Exception as e:
        log.warning("/gift picture failed (card %s): %s", cid, e)
        await m.reply(html, reply_markup=gift_kb(u.id, t.id, cid, False))


@router.callback_query(F.data.startswith("gf:"))
async def gift_cb(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 5 or not call.message:
        return await call.answer()
    try:
        act, uid, tid, cid = parts[1], int(parts[2]), int(parts[3]), int(parts[4])
    except ValueError:
        return await call.answer()
    if call.from_user.id != uid:
        return await call.answer("Only the person who used /gift can confirm this.", show_alert=True)
    c = await db.get_character(cid)
    if act == "n":
        await finish_card(call, c, "Gift cancelled")
        return await call.answer()
    try:
        ok = bool(c) and await db.transfer_character(uid, tid, cid)
    except Exception:
        log.exception("/gift transfer failed")
        return await call.answer("Something went wrong. Nothing was sent.", show_alert=True)
    if not ok:
        await finish_card(call, c, "Gift failed", esc("You no longer own this character."),
                          "You no longer own this character.")
        return await call.answer()
    to_name = await db.get_user_name(tid) or "Collector"
    await finish_card(
        call, c, "Gift sent",
        f"{mention_md(uid, call.from_user.first_name)} gifted **{esc(card_line(c))}** to {mention_md(tid, to_name)}",
        f"{mention(uid, call.from_user.first_name)} gifted {escape(card_line(c))} to {mention(tid, to_name)}")
    await call.answer()


# ───────────── inline mode ─────────────
COLL_RE = re.compile(rf"^{COLLECTION_QUERY}(?:\.(\d+))?(?:\s+(.*))?$", re.I | re.S)
PAGE = 50                      # Telegram allows at most 50 results per answer
CACHE_SECONDS = 30
SPOILER_RETRY_DELAYS = (0.4, 1.2, 2.5)


def caption(c) -> str:
    return (
        "<b>OMG !! Check out This Character !</b>\n\n"
        f"<b>Name :</b> {escape(name_tag(c))}\n"
        f"<b>Rarity : </b>{rarity_label(c['rarity'])}\n"
        f"<b>Global ID :</b> {c['char_id']}"
    )


def binds_kb(c, styled: bool = True) -> InlineKeyboardMarkup:
    extra = {"style": "primary"} if styled else {}
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="Check binds", callback_data=f"bn:{c['char_id']}", **extra)]])


def to_result(c, styled: bool = True):
    rid, cap, kb = str(c["char_id"]), caption(c), binds_kb(c, styled)
    title, desc = name_tag(c), f"{c['anime']} · {c['rarity']}"
    if "qty" in c.keys():                      # own-collection results show the copies owned
        desc += f" · x{c['qty']}"
    kind = c["media_type"]
    if kind == "video":
        return InlineQueryResultCachedVideo(
            id=rid, video_file_id=c["file_id"], title=title, description=desc,
            caption=cap, parse_mode="HTML", reply_markup=kb)
    if kind == "animation":
        return InlineQueryResultCachedMpeg4Gif(
            id=rid, mpeg4_file_id=c["file_id"], title=title,
            caption=cap, parse_mode="HTML", reply_markup=kb)
    return InlineQueryResultCachedPhoto(
        id=rid, photo_file_id=c["file_id"], title=title, description=desc,
        caption=cap, parse_mode="HTML", reply_markup=kb)


def to_input_media(c):
    """Same card as an InputMedia with the spoiler on (used to edit the sent inline message)."""
    kw = dict(media=c["file_id"], caption=caption(c), parse_mode="HTML", has_spoiler=True)
    if c["media_type"] == "video":
        return InputMediaVideo(**kw)
    if c["media_type"] == "animation":
        return InputMediaAnimation(**kw)
    return InputMediaPhoto(**kw)


@router.inline_query()
async def inline_cmd(q: InlineQuery):
    offset = int(q.offset) if q.offset.isdigit() else 0
    mc = COLL_RE.match(q.query.strip())
    own = mc is not None
    if own:
        uid = int(mc.group(1)) if mc.group(1) else q.from_user.id
        rows = await db.search_collection(uid, mc.group(2) or "", offset, PAGE)
    else:
        rows = await db.search_characters(q.query, offset, PAGE)
    nxt = str(offset + PAGE) if len(rows) == PAGE else ""

    def build(styled: bool) -> list:
        out = []
        for c in rows:
            try:
                out.append(to_result(c, styled))
            except Exception:
                log.exception("inline: bad card %s", c["char_id"])
        if own and not out and offset == 0:
            out.append(InlineQueryResultArticle(
                id="empty", title="Nothing here yet",
                description="Bind characters when they drop in a group to fill your collection.",
                input_message_content=InputTextMessageContent(
                    message_text="<b>This collection is empty</b>", parse_mode="HTML")))
        return out

    try:
        await with_styles(lambda s: q.answer(
            build(s), cache_time=3 if own else CACHE_SECONDS, is_personal=own and not (mc and mc.group(1)), next_offset=nxt))
    except TelegramAPIError as e:
        log.warning("inline answer failed (query %r, offset %s): %s", q.query, offset, e)


@router.chosen_inline_result()
async def spoiler_cb(r: ChosenInlineResult, bot: Bot):
    """Put the spoiler on a card that was just sent through inline mode."""
    if not r.inline_message_id or not r.result_id.isdigit():
        return
    c = await db.get_character(int(r.result_id))
    if not c:
        return
    for delay in SPOILER_RETRY_DELAYS:
        await asyncio.sleep(delay)
        try:
            await with_styles(lambda s: bot.edit_message_media(
                media=to_input_media(c), inline_message_id=r.inline_message_id, reply_markup=binds_kb(c, s)))
            return
        except TelegramAPIError as e:
            log.debug("spoiler edit retry for card %s: %s", c["char_id"], e)
    log.warning("could not spoiler inline card %s", c["char_id"])


def holders_text(c, rows) -> str:
    """Telegram alerts hold 200 characters, so names are trimmed and lines added only while they fit."""
    if not rows:
        return "Nobody has bound this character yet."
    text = f"Top binds: {c['name'][:20]}"
    for i, r in enumerate(rows, 1):
        nm = (r["first_name"] or (f"@{r['username']}" if r["username"] else str(r["user_id"]))).replace("\n", " ")
        nm = nm if len(nm) <= 14 else nm[:13] + "…"
        line = f"\n{i}. {nm} x{r['qty']}"
        if len(text) + len(line) > 200:
            break
        text += line
    return text


@router.callback_query(F.data.startswith("bn:"))
async def binds_cb(call: CallbackQuery):
    cid = call.data[3:]
    c = await db.get_character(int(cid)) if cid.isdigit() else None
    if not c:
        return await call.answer("This character no longer exists.", show_alert=True)
    await call.answer(holders_text(c, await db.top_holders(c["char_id"], 10)), show_alert=True)
