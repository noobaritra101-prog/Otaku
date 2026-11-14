"""Otaku Nexus - inline mode.

Type @botusername in any chat:
- empty query: every character in the bot, newest first (50 per page, scroll to load more)
- text: filters by name, series, rarity, sub rarity or character ID
- "collection" (the View Collection button): the person's OWN characters, newest bind first; text after it
  filters the same way ("collection makima"). Results are personal (is_personal=True).

Each result carries a "Check binds" button (top 10 owners of that card, shown as a private alert).

Spoiler: Telegram has no spoiler flag for inline results, so when a result is sent the bot receives
chosen_inline_result and edits the inline message media with has_spoiler=True. That needs, in BotFather:
  /setinline          (enable inline mode)
  /setinlinefeedback  (set to 100%)
Without inline feedback everything still works, the image just is not spoilered.
"""
import asyncio
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery, ChosenInlineResult, InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery,
    InlineQueryResultArticle, InlineQueryResultCachedMpeg4Gif, InlineQueryResultCachedPhoto, InlineQueryResultCachedVideo,
    InputMediaAnimation, InputMediaPhoto, InputMediaVideo, InputTextMessageContent,
)

import dbase as db
from drop import COLLECTION_QUERY, name_tag
from waifu import rarity_label, with_styles

router = Router()
log = logging.getLogger("otaku.inline")

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
    text = q.query.strip()
    low = text.lower()
    own = low == COLLECTION_QUERY or low.startswith(COLLECTION_QUERY + " ")
    if own:
        rows = await db.search_collection(q.from_user.id, text[len(COLLECTION_QUERY):], offset, PAGE)
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
                    message_text="<b>My collection is empty</b>", parse_mode="HTML")))
        return out

    try:
        await with_styles(lambda s: q.answer(
            build(s), cache_time=3 if own else CACHE_SECONDS, is_personal=own, next_offset=nxt))
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
