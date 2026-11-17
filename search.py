"""Otaku Nexus - /waifuverse (browse every series and character) and /search (find a character by name or ID).

/waifuverse: rich message (no table) with toggle buttons [Anime] [Characters].
  - Anime: series A to Z, numbered quote list with the character count, 10 per page. Number buttons open the
    characters of that series (with a Back button).
  - Characters: every character by ID, 15 per page, `ID. Name (sub emoji) - Rarity` in a quote.
  - Smart Prev / page / Next buttons (only with 2+ pages). The same message is edited; only the person who used
    the command can press the buttons. Callbacks: wv:<uid>:<a|c>:<page>, wv:<uid>:s:<series index>:<page>.

/search <name | ID | series words>: every word has to appear in the name or the series; a number also matches the ID.
  - 0 results: a notice. 1 result: the character card (picture on top). 2+ results: a numbered list (10 per
    page) with number buttons; pressing one sends that card as a reply and keeps the list.
  - Callbacks: sr:<uid>:<token>:<page> (page), sp:<uid>:<token>:<char id> (open). The query text is kept in memory
    under the token (resets on restart: the buttons then say to search again).

Card = picture, name, series, rarity, sub rarity, type, ID, artist, owners / copies and the top collectors.
Rich message first, then picture + HTML caption. Own router: otaku.py includes search.router.
"""
import logging
from collections import OrderedDict
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters,
)

import dbase as db
import richmsg
from configaration import RICH_MESSAGES
from drop import name_tag, sub_emoji
from minigame import notice, topic_id
from richmsg import esc
from waifu import rarity_label, send_media, sub_label, with_styles

router = Router()
log = logging.getLogger("otaku.search")

ANIME_PER_PAGE = 10
CHARS_PER_PAGE = 15
SEARCH_PER_PAGE = 10


def cut(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


def btn(text: str, data: str, style: str | None = None, styled: bool = True) -> InlineKeyboardButton:
    extra = {"style": style} if (styled and style) else {}
    return InlineKeyboardButton(text=text, callback_data=data, **extra)


def nav_row(page: int, pages: int, prefix: str) -> list[InlineKeyboardButton]:
    """Prev only after page 1, Next only before the last page, middle button is a no-op."""
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn("Prev", f"{prefix}:{page - 1}"))
    row.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(btn("Next", f"{prefix}:{page + 1}"))
    return row


def char_line(c) -> str:
    return f"{name_tag(c)} - {rarity_label(c['rarity'])}"


TICK, BOX = "✅", "⬜"


def char_lines(rows, owned: set[int]) -> tuple[list[str], list[str]]:
    """Checklist of characters. Rich: a real task list (`- [x]` owned, `- [ ]` not yet) with the ID at the end.
    HTML fallback: numbered by ID with ✅ / ⬜."""
    md, html = [], []
    for c in rows:
        have = c["char_id"] in owned
        md.append(f"- [{'x' if have else ' '}] {esc(char_line(c))} · ID {c['char_id']}")
        html.append(f"{c['char_id']}. {TICK if have else BOX} {escape(char_line(c))}")
    return md, html


# ═════════════ /waifuverse ═════════════
async def verse_view(uid: int, view: str, page: int, idx: int = 0):
    """Returns (md, html, kb_factory)."""
    if view == "a":
        total = await db.verse_anime_count()
        pages = max(1, (total + ANIME_PER_PAGE - 1) // ANIME_PER_PAGE)
        page = min(max(page, 0), pages - 1)
        rows = await db.verse_anime_page(page * ANIME_PER_PAGE, ANIME_PER_PAGE, uid)
        chars = await db.verse_chars_count()
        lines_md, lines_h = [], []
        for i, r in enumerate(rows, page * ANIME_PER_PAGE + 1):
            tail = f"{r['owned']}/{r['n']} owned"
            lines_md.append(f"> {i}. **{esc(cut(r['anime'], 40))}** - {tail}")
            lines_h.append(f"{i}. <b>{escape(cut(r['anime'], 40))}</b> - {tail}")
        if not lines_md:
            lines_md, lines_h = ["> Nothing here yet."], ["Nothing here yet."]
        head = f"{total} series · {chars} characters"
        md = f"# Waifuverse : ==Anime==\n\n{head}\n\n---\n\n" + "\n".join(lines_md) + f"\n\n---\n\n###### Page {page + 1}/{pages}"
        html = (f"<b>Waifuverse : Anime</b>\n{head}\n<blockquote expandable>" + "\n".join(lines_h)
                + f"</blockquote>\nPage {page + 1}/{pages}")

        def kb(styled: bool = True) -> InlineKeyboardMarkup:
            rows_kb = [toggle_row(uid, "a", styled)]
            nums = [btn(str(i), f"wv:{uid}:s:{i - 1}:{page}", None, styled)
                    for i in range(page * ANIME_PER_PAGE + 1, page * ANIME_PER_PAGE + len(rows) + 1)]
            rows_kb += [nums[i:i + 5] for i in range(0, len(nums), 5)]
            nav = nav_row(page, pages, f"wv:{uid}:a")
            if nav:
                rows_kb.append(nav)
            return InlineKeyboardMarkup(inline_keyboard=rows_kb)
        return md, html, kb

    if view == "c":
        total = await db.verse_chars_count()
        pages = max(1, (total + CHARS_PER_PAGE - 1) // CHARS_PER_PAGE)
        page = min(max(page, 0), pages - 1)
        rows = await db.verse_chars_page(None, page * CHARS_PER_PAGE, CHARS_PER_PAGE)
        lines_md, lines_h = char_lines(rows, await db.owned_among(uid, [c["char_id"] for c in rows]))
        if not lines_md:
            lines_md, lines_h = ["> Nothing here yet."], ["Nothing here yet."]
        md = (f"# Waifuverse : ==Characters==\n\n{total} characters\n\n---\n\n" + "\n".join(lines_md)
              + f"\n\n---\n\n###### Page {page + 1}/{pages}")
        html = (f"<b>Waifuverse : Characters</b>\n{total} characters\n<blockquote expandable>"
                + "\n".join(lines_h) + f"</blockquote>\nPage {page + 1}/{pages}")

        def kb(styled: bool = True) -> InlineKeyboardMarkup:
            rows_kb = [toggle_row(uid, "c", styled)]
            nav = nav_row(page, pages, f"wv:{uid}:c")
            if nav:
                rows_kb.append(nav)
            return InlineKeyboardMarkup(inline_keyboard=rows_kb)
        return md, html, kb

    raise ValueError(view)


async def series_view(uid: int, idx: int, back_page: int, page: int):
    """Characters of one series. Returns (md, html, kb_factory) or None when the series is gone."""
    row = await db.verse_anime_page(idx, 1, uid)
    if not row:
        return None
    akey, name = row[0]["akey"], row[0]["anime"]
    total = await db.verse_chars_count(akey)
    pages = max(1, (total + CHARS_PER_PAGE - 1) // CHARS_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    rows = await db.verse_chars_page(akey, page * CHARS_PER_PAGE, CHARS_PER_PAGE)
    owned = await db.owned_among(uid, [c["char_id"] for c in rows])
    have = row[0]["owned"] if "owned" in row[0].keys() else 0
    lines_md, lines_h = char_lines(rows, owned)
    md = (f"# Series : =={esc(cut(name, 40))}==\n\n{have}/{total} owned\n\n---\n\n"
          + "\n".join(lines_md) + f"\n\n---\n\n###### Page {page + 1}/{pages}")
    html = (f"<b>Series : {escape(cut(name, 40))}</b>\n{have}/{total} owned\n"
            f"<blockquote expandable>" + "\n".join(lines_h) + f"</blockquote>\nPage {page + 1}/{pages}")

    def kb(styled: bool = True) -> InlineKeyboardMarkup:
        rows_kb = [toggle_row(uid, "a", styled)]
        nav = nav_row(page, pages, f"wv:{uid}:s:{idx}:{back_page}")
        if nav:
            rows_kb.append(nav)
        rows_kb.append([btn("Back", f"wv:{uid}:a:{back_page}")])
        return InlineKeyboardMarkup(inline_keyboard=rows_kb)
    return md, html, kb


def toggle_row(uid: int, view: str, styled: bool) -> list[InlineKeyboardButton]:
    row = []
    for key, label in (("a", "Anime"), ("c", "Characters")):
        on = key == view
        text = f"• {label}" if (on and not styled) else label
        row.append(btn(text, f"wv:{uid}:{key}:0", "primary" if on else None, styled))
    return row


@router.message(Command("waifuverse"))
async def verse_cmd(m: Message):
    u = m.from_user
    if not u:
        return
    try:
        await db.upsert_user(u.id, u.username, u.first_name)
        md, html, kb = await verse_view(u.id, "a", 0)
    except Exception:
        log.exception("/waifuverse failed")
        return await notice(m, "Something went wrong", "Try again in a moment.")
    await send_text(m, md, html, kb)


async def send_text(m: Message, md: str, html: str, kb) -> None:
    """Rich reply, then HTML fallback; coloured buttons first, plain after."""
    if RICH_MESSAGES:
        for styled in (True, False):
            try:
                await richmsg.send_rich(m.bot, m.chat.id, md, reply_markup=kb(styled),
                                        message_thread_id=topic_id(m), reply_to_message_id=m.message_id)
                return
            except Exception as e:
                log.warning("rich send failed (styled=%s): %s", styled, e)
    await with_styles(lambda s: m.reply(html, reply_markup=kb(s)))


async def edit_text(call: CallbackQuery, md: str, html: str, kb) -> None:
    msg = call.message
    if RICH_MESSAGES:
        for styled in (True, False):
            try:
                await richmsg.edit_rich(call.bot, msg.chat.id, msg.message_id, md, reply_markup=kb(styled))
                return
            except Exception as e:
                if "not modified" in str(e).lower():
                    return
                log.debug("rich edit failed (styled=%s): %s", styled, e)
    try:
        await with_styles(lambda s: msg.edit_text(html, reply_markup=kb(s)))
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.warning("edit failed: %s", e)


@router.callback_query(F.data == "noop")
async def noop_cb(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("wv:"))
async def verse_cb(call: CallbackQuery):
    p = (call.data or "").split(":")
    if len(p) < 4 or not call.message:
        return await call.answer()
    try:
        uid = int(p[1])
        if call.from_user.id != uid:
            return await call.answer("Only the person who used /waifuverse can use these buttons.", show_alert=True)
        if p[2] in ("a", "c") and len(p) == 4:
            res = await verse_view(uid, p[2], int(p[3]))
        elif p[2] == "s" and len(p) == 5:
            res = await series_view(uid, int(p[3]), int(p[4]), 0)
        elif p[2] == "s" and len(p) == 6:
            res = await series_view(uid, int(p[3]), int(p[4]), int(p[5]))
        else:
            return await call.answer()
    except ValueError:
        return await call.answer()
    except Exception:
        log.exception("/waifuverse callback failed")
        return await call.answer("Something went wrong.", show_alert=True)
    if not res:
        return await call.answer("This series is gone.", show_alert=True)
    md, html, kb = res
    await edit_text(call, md, html, kb)
    await call.answer()


# ═════════════ /search ═════════════
_queries: "OrderedDict[str, str]" = OrderedDict()      # token -> query text (in memory)
_counter = 0


def remember(query: str) -> str:
    global _counter
    for tok, q in _queries.items():
        if q == query:
            return tok
    _counter += 1
    tok = format(_counter, "x")
    _queries[tok] = query
    while len(_queries) > 300:
        _queries.popitem(last=False)
    return tok


async def card_view(c) -> tuple[str, str]:
    """(rich markdown, html caption) of one character."""
    owners, copies = await db.card_totals(c["char_id"])
    holders = await db.top_holders(c["char_id"], 3) if owners else []
    sub = c["sub_rarity"]
    rows = [("Series", c["anime"]), ("Rarity", rarity_label(c["rarity"]))]
    if sub:
        rows.append(("Sub rarity", sub_label(sub)))
    rows.append(("ID", str(c["char_id"])))
    rows.append(("Owned by", f"{owners} player{'s' if owners != 1 else ''} · {copies} cop{'ies' if copies != 1 else 'y'}"))

    md = f"# {esc(cut(c['name'], 60))}\n\n---\n\n" + "\n\n".join(f"**{k} :** {esc(v)}" for k, v in rows)
    html = f"<b>{escape(cut(c['name'], 60))}</b>\n" + "\n".join(f"<b>{k} :</b> {escape(v)}" for k, v in rows)
    if holders:
        def who(h):
            return cut(h["first_name"] or (f"@{h['username']}" if h["username"] else str(h["user_id"])), 20)
        md += "\n\n---\n\n###### Top collectors\n\n" + "\n".join(
            f"{i}. **{esc(who(h))}** - x{h['qty']}" for i, h in enumerate(holders, 1))
        html += "\n\n<b>Top collectors</b>\n" + "\n".join(
            f"{i}. <b>{escape(who(h))}</b> - x{h['qty']}" for i, h in enumerate(holders, 1))
    return md, html


async def send_card(bot, chat_id: int, c, thread: int | None, reply_to: int | None) -> None:
    md, html = await card_view(c)
    media = (c["file_id"], c["media_type"])
    if RICH_MESSAGES:
        try:
            await richmsg.send_rich(bot, chat_id, md, media=media, message_thread_id=thread,
                                    reply_to_message_id=reply_to)
            return
        except Exception as e:
            log.warning("rich card failed (char %s): %s", c["char_id"], e)
    reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    await send_media((bot, chat_id), c["media_type"], c["file_id"], html,
                     message_thread_id=thread, reply_parameters=reply)


async def search_view(uid: int, tok: str, query: str, page: int):
    """Returns (md, html, kb_factory) for the result list."""
    total = await db.search_count(query)
    pages = max(1, (total + SEARCH_PER_PAGE - 1) // SEARCH_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    rows = await db.search_page(query, page * SEARCH_PER_PAGE, SEARCH_PER_PAGE)
    lines_md, lines_h = [], []
    owned = await db.owned_among(uid, [c["char_id"] for c in rows])
    for i, c in enumerate(rows, page * SEARCH_PER_PAGE + 1):
        mark = TICK if c["char_id"] in owned else BOX
        box = "x" if c["char_id"] in owned else " "
        lines_md.append(f"- [{box}] {i} · **{esc(name_tag(c))}** - {esc(cut(c['anime'], 30))} · {esc(rarity_label(c['rarity']))} · ID {c['char_id']}")
        lines_h.append(f"{i}. {mark} <b>{escape(name_tag(c))}</b> - {escape(cut(c['anime'], 30))} · {escape(rarity_label(c['rarity']))} · ID {c['char_id']}")
    shown = cut(query, 30)
    md = (f"# Search : =={esc(shown)}==\n\n{total} result{'s' if total != 1 else ''}\n\n---\n\n"
          + "\n".join(lines_md) + f"\n\n---\n\n###### Page {page + 1}/{pages}")
    html = (f"<b>Search : {escape(shown)}</b>\n{total} result{'s' if total != 1 else ''}\n"
            + "\n".join(lines_h) + f"\nPage {page + 1}/{pages}")

    def kb(styled: bool = True) -> InlineKeyboardMarkup:
        nums = [btn(str(i), f"sp:{uid}:{tok}:{c['char_id']}", None, styled)
                for i, c in enumerate(rows, page * SEARCH_PER_PAGE + 1)]
        rows_kb = [nums[i:i + 5] for i in range(0, len(nums), 5)]
        nav = nav_row(page, pages, f"sr:{uid}:{tok}")
        if nav:
            rows_kb.append(nav)
        return InlineKeyboardMarkup(inline_keyboard=rows_kb)
    return md, html, kb


@router.message(Command("search"))
async def search_cmd(m: Message, command: CommandObject):
    u = m.from_user
    if not u:
        return
    query = (command.args or "").strip()
    if not query and m.reply_to_message and m.reply_to_message.text:
        query = m.reply_to_message.text.strip()
    if not query:
        return await notice(m, "Search", "Use /search name, series or ID. Example: /search rem or /search 12")
    query = query[:60]
    try:
        await db.upsert_user(u.id, u.username, u.first_name)
        total = await db.search_count(query)
        if total == 0:
            return await notice(m, "Nothing found", f"No character matches {query}.")
        if total == 1:
            c = (await db.search_page(query, 0, 1))[0]
            return await send_card(m.bot, m.chat.id, c, topic_id(m), m.message_id)
        md, html, kb = await search_view(u.id, remember(query), query, 0)
    except Exception:
        log.exception("/search failed")
        return await notice(m, "Something went wrong", "Try again in a moment.")
    await send_text(m, md, html, kb)


@router.callback_query(F.data.startswith("sr:"))
async def search_page_cb(call: CallbackQuery):
    p = (call.data or "").split(":")
    if len(p) != 4 or not call.message:
        return await call.answer()
    try:
        uid, tok, page = int(p[1]), p[2], int(p[3])
    except ValueError:
        return await call.answer()
    if call.from_user.id != uid:
        return await call.answer("Only the person who used /search can use these buttons.", show_alert=True)
    query = _queries.get(tok)
    if not query:
        return await call.answer("This search expired, use /search again.", show_alert=True)
    try:
        md, html, kb = await search_view(uid, tok, query, page)
    except Exception:
        log.exception("/search page failed")
        return await call.answer("Something went wrong.", show_alert=True)
    await edit_text(call, md, html, kb)
    await call.answer()


@router.callback_query(F.data.startswith("sp:"))
async def search_pick_cb(call: CallbackQuery):
    p = (call.data or "").split(":")
    if len(p) != 4 or not call.message:
        return await call.answer()
    try:
        uid, cid = int(p[1]), int(p[3])
    except ValueError:
        return await call.answer()
    if call.from_user.id != uid:
        return await call.answer("Only the person who used /search can use these buttons.", show_alert=True)
    try:
        c = await db.get_character(cid)
        if not c:
            return await call.answer("This character was removed.", show_alert=True)
        msg = call.message
        origin = msg.reply_to_message.message_id if msg.reply_to_message else None
        await send_card(call.bot, msg.chat.id, c, topic_id(msg), origin)
    except Exception:
        log.exception("/search open failed")
        return await call.answer("Something went wrong.", show_alert=True)
    try:                                              # the list is not needed any more
        await msg.delete()
    except Exception as e:
        log.debug("could not delete the search list: %s", e)
    await call.answer()
