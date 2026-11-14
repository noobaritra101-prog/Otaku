"""Otaku Nexus - /harem (alias /collection): view a user's whole collection, series by series.

- Rich message: picture on top (the user's /fav character, else the first character of the top series),
  no totals line, then one section per series:
      Series name (collected/total)
      ID. Name (sub emoji) - Rarity    (the ID is the number of a numbered list), then a --- divider; the /fav character is highlighted (==text== in rich, bold in the fallback) and gets a ✨ before the name
- 5 series per page, smart Prev / [page/pages] / Next buttons (only with 2+ pages; no Prev on page 1, no Next on the last page), footer "Page x/y", and a View Collection (inline) button. Buttons only work for the
  person who used the command.
- Fallback: photo with an HTML caption, then plain text.

Also here: /topplayers leaderboard and /settplayer (set its video / GIF / picture).
- Rich message with the /settplayer media on top (a media block), a table of the top TOPPLAYERS_COUNT and three
  toggle buttons: Nex (richest players), Collection (most unique characters) and Waifus (characters owned by the
  most people). The same message is edited. Only the person who used /topplayers can press the buttons.
- Fallbacks: rich + media, rich, HTML media caption, HTML text; each with coloured then plain buttons.
- /settplayer (owner and co-owners): reply to a photo / video / GIF, stored as a file_id in settings.
"""
import logging
from collections import defaultdict
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters,
)

import dbase as db
import richmsg
from configaration import (
    CO_OWNERS, CURRENCY_NAME, CURRENCY_SYMBOL, OWNER_ID, RARITIES, RICH_MESSAGES, TOPPLAYERS_COUNT,
)
from drop import collection_btn, edit_card, send_card_rich, sub_emoji
from minigame import notice, topic_id
from richmsg import esc
from waifu import send_media, with_styles

router = Router()
log = logging.getLogger("otaku.harem")

SERIES_PER_PAGE = 5
CAPTION_MAX = 1000                  # Telegram caption limit is 1024 (fallback only)
RAR_EMO = {n: e for n, e, *_ in RARITIES}


def cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def card_line(c) -> str:
    """Plain text: 'Name (sub emoji) - rarity emoji Rarity'. The brackets are left out when there is no sub rarity."""
    name = c["name"] + (f" ({sub_emoji(c['sub_rarity'])})" if c["sub_rarity"] else "")
    return f"{name} - {RAR_EMO.get(c['rarity'], '')} {c['rarity']}".replace("-  ", "- ").strip()


def card_line_md(c) -> str:
    """Same as card_line, escaped for Rich Markdown (only the user text is escaped)."""
    sub = f" ({sub_emoji(c['sub_rarity'])})" if c["sub_rarity"] else ""
    return f"{esc(c['name'])}{sub} - {RAR_EMO.get(c['rarity'], '')} {esc(c['rarity'])}".strip()


# ───────────── view ─────────────
async def cover(uid: int):
    """(character row for the picture, is_favorite): the /fav card if still owned, else the first character
    of the top series."""
    fid = await db.get_favorite(uid)
    if fid and await db.owned_qty(uid, fid):
        c = await db.get_character(fid)
        if c:
            return c, True
    top = await db.collection_anime_page(uid, 0, 1)
    rows = await db.collection_by_animes(uid, [top[0]["akey"]]) if top else []
    return (rows[0], False) if rows else (None, False)


async def build(target_id: int, page: int):
    """Returns (rich md, html caption, state, cover card) or None when the collection is empty."""
    series = await db.collection_anime_count(target_id)
    if series == 0:
        return None
    pages = (series + SERIES_PER_PAGE - 1) // SERIES_PER_PAGE
    page = min(max(page, 0), pages - 1)
    groups = await db.collection_anime_page(target_id, page * SERIES_PER_PAGE, SERIES_PER_PAGE)
    by = defaultdict(list)
    for c in await db.collection_by_animes(target_id, [g["akey"] for g in groups]):
        by[c["akey"]].append(c)
    unique, copies = await db.collection_stats(target_id)
    name = await db.get_user_name(target_id) or "Collector"
    card, is_fav = await cover(target_id)

    footer = f"Page {page + 1}/{pages}"
    md = f"## {esc(cut(name, 30))}'s Harem\n"
    html = f"<b>{escape(cut(name, 30))}'s Harem</b>\n"
    fav_id = card["char_id"] if is_fav and card else None   # the favorite is highlighted in the list
    html_lines: list[str] = []

    for g in groups:
        title = f"{g['anime']} ({g['collected']}/{g['total']})"
        lines = []
        html_lines.append(f"<b>{escape(cut(title, 40))}</b>")
        for n, c in enumerate(by[g["akey"]]):
            # alternating "." / ")" starts a new numbered list per item, so each number stays the real ID
            delim = "." if n % 2 == 0 else ")"
            tail = f" · x{c['qty']}" if c["qty"] > 1 else ""
            hot = c["char_id"] == fav_id
            lines.append(f"{c['char_id']}{delim} " + (f"==✨ {card_line_md(c)}==" if hot else card_line_md(c)) + tail)
            html_lines.append(f"{c['char_id']}. " + (f"<b>✨ {escape(card_line(c))}</b>" if hot else escape(card_line(c))) + escape(tail))
        md += f"\n#### {esc(g['anime'])} ({g['collected']}/{g['total']})\n\n" + "\n".join(lines) + "\n\n---\n"

    md += f"\n###### {footer}\n"
    kept = []
    for line in html_lines:                         # fallback caption must fit Telegram's caption limit
        if len(html) + sum(len(x) + 1 for x in kept) + len(line) + len(footer) + 40 > CAPTION_MAX:
            break
        kept.append(line)
    html += f"<blockquote expandable>{chr(10).join(kept)}</blockquote>\n{footer}"
    return md, html, {"page": page, "pages": pages}, card


def keyboard(viewer: int, target: int, st: dict, styled: bool = True) -> InlineKeyboardMarkup:
    def b(text: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=data)

    page, pages = st["page"], st["pages"]
    rows = []
    if pages > 1:                                   # smart buttons: Prev only after page 1, Next only before the last
        nav = []
        if page > 0:
            nav.append(b("Prev", f"hm:{viewer}:{target}:{page - 1}"))
        nav.append(b(f"{page + 1}/{pages}", "hm:noop"))
        if page < pages - 1:
            nav.append(b("Next", f"hm:{viewer}:{target}:{page + 1}"))
        rows.append(nav)
    rows.append([collection_btn(target, styled)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ───────────── handlers ─────────────
@router.message(Command("harem", "collection"))
async def harem_cmd(m: Message):
    viewer = m.from_user
    if not viewer:
        return
    target = viewer                                     # replying to someone does not matter: always your own harem
    try:
        await db.upsert_user(viewer.id, viewer.username, viewer.first_name)
        res = await build(target.id, 0)
    except Exception:
        log.exception("/harem database error")
        return await notice(m, "Something went wrong", "Try again in a moment.")
    if not res:
        return await notice(m, "Your harem is empty", "Bind characters when they drop in a group to fill it.")
    md, html, st, card = res
    kbf = lambda s: keyboard(viewer.id, target.id, st, s)
    try:
        await send_card_rich(m, md, html, card, kbf)
    except Exception as e:                              # picture rejected -> plain text harem
        log.warning("/harem picture failed (card %s): %s", card["char_id"], e)
        await with_styles(lambda s: m.reply(html, reply_markup=kbf(s)))


@router.callback_query(F.data.startswith("hm:"))
async def harem_cb(call: CallbackQuery):
    parts = call.data.split(":")
    # hm:<viewer>:<target>:<page>  (old buttons had a rarity part: hm:<viewer>:<target>:<rar>:<page>)
    if len(parts) not in (4, 5) or not call.message:        # also covers hm:noop
        return await call.answer()
    try:
        viewer, target, page = int(parts[1]), int(parts[2]), int(parts[-1])
    except ValueError:
        return await call.answer()
    if call.from_user.id != viewer:
        return await call.answer("Only the person who used /harem can use these buttons.", show_alert=True)
    try:
        res = await build(target, page)
    except Exception:
        log.exception("/harem callback database error")
        return await call.answer("Something went wrong.", show_alert=True)
    if not res:
        return await call.answer("This harem is empty.", show_alert=True)
    md, html, st, card = res
    await edit_card(call, md, html, card, lambda s: keyboard(viewer, target, st, s))
    await call.answer()


# ═════════════ /topplayers leaderboard ═════════════
SYM = CURRENCY_SYMBOL
VIEWS = (("nex", CURRENCY_NAME), ("col", "Collection"), ("wai", "Waifus"))
VIEW_TITLE = dict(VIEWS)


# ───────────── data ─────────────
def cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def card_name(c) -> str:
    return c["name"] + (f" ({sub_emoji(c['sub_rarity'])})" if c["sub_rarity"] else "")


def name_of(r) -> str:
    return cut((r["first_name"] or (f"@{r['username']}" if r["username"] else str(r["user_id"]))).replace("\n", " "), 20)


async def banner() -> tuple[str, str] | None:
    fid, kind = await db.get_setting("topplayers_file_id"), await db.get_setting("topplayers_type")
    return (fid, kind) if fid and kind in ("photo", "video", "animation") else None


def board_view(view: str, rows) -> tuple[str, str]:
    """(rich markdown, html fallback)."""
    label = VIEW_TITLE[view]
    if view == "nex":
        head, aligns = ("#", "Player", CURRENCY_NAME), "|---:|---|---:|"
        body = [(str(i), name_of(r), f"{r['nex']:,} {SYM}") for i, r in enumerate(rows, 1)]
    elif view == "wai":
        head, aligns = ("#", "Waifu", "Rarity", "Owners"), "|---:|---|---|---:|"
        body = [(str(i), cut(card_name(r), 24), f"{RAR_EMO.get(r['rarity'], '')} {r['rarity']}".strip(),
                 f"{r['owners']:,}") for i, r in enumerate(rows, 1)]
    else:
        head, aligns = ("#", "Player", "Unique", "Copies"), "|---:|---|---:|---:|"
        body = [(str(i), name_of(r), f"{r['uniq']:,}", f"{r['copies']:,}") for i, r in enumerate(rows, 1)]

    md = f"## Top players\n\nBy {esc(label)}\n\n"
    if body:
        md += "| " + " | ".join(esc(h) for h in head) + " |\n" + aligns + "\n"
        md += "\n".join("| " + " | ".join(esc(c) for c in row) + " |" for row in body)
        inner = "\n".join(f"{row[0]}. <b>{escape(row[1])}</b> · " + " · ".join(escape(c) for c in row[2:])
                          for row in body)
    else:
        md += "Nobody here yet."
        inner = "Nobody here yet."
    html = f"<b>Top players</b>\n<b>By {escape(label)}</b>\n<blockquote>{inner}</blockquote>"
    return md, html


def board_kb(uid: int, view: str, styled: bool = True) -> InlineKeyboardMarkup:
    tabs = []
    for key, label in VIEWS:
        on = key == view
        text = f"• {label}" if (on and not styled) else label
        extra = {"style": "primary"} if (on and styled) else {}
        tabs.append(InlineKeyboardButton(text=text, callback_data=f"tp:{uid}:{key}", **extra))
    return InlineKeyboardMarkup(inline_keyboard=[tabs])


async def load(view: str):
    fetch = {"nex": db.top_nex, "col": db.top_collectors, "wai": db.top_waifus}[view]
    rows = await fetch(TOPPLAYERS_COUNT)
    return board_view(view, rows)


# ───────────── sending ─────────────
async def send_board(bot, chat_id: int, uid: int, view: str,
                     thread: int | None = None, reply_to: int | None = None) -> None:
    md, html = await load(view)
    media = await banner()
    reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    # order: rich + media, rich, HTML media caption, HTML text; each with coloured then plain buttons
    combos = ([("rich", media), ("rich", None)] if RICH_MESSAGES else []) + [("html", media), ("html", None)]
    combos = list(dict.fromkeys(combos))
    for kind, bn in combos:
        for styled in (True, False):
            kb = board_kb(uid, view, styled)
            try:
                if kind == "rich":
                    await richmsg.send_rich(bot, chat_id, md, media=bn, reply_markup=kb,
                                            message_thread_id=thread, reply_to_message_id=reply_to)
                elif bn:
                    await send_media((bot, chat_id), bn[1], bn[0], html, reply_markup=kb,
                                     message_thread_id=thread, reply_parameters=reply)
                else:
                    await bot.send_message(chat_id, html, reply_markup=kb,
                                           message_thread_id=thread, reply_parameters=reply)
                return
            except Exception as e:
                log.warning("/topplayers send failed (%s, media=%s, styled=%s): %s", kind, bool(bn), styled, e)
    raise RuntimeError("all /topplayers send attempts failed")


@router.message(Command("topplayers"))
async def top_cmd(m: Message):
    u = m.from_user
    if not u:
        return
    try:
        await db.upsert_user(u.id, u.username, u.first_name)
        await send_board(m.bot, m.chat.id, u.id, "nex", topic_id(m), m.message_id)
    except Exception:
        log.exception("/topplayers failed")
        await notice(m, "Something went wrong", "Try again in a moment.")


@router.callback_query(F.data.startswith("tp:"))
async def top_cb(call: CallbackQuery):
    parts = (call.data or "").split(":")
    if len(parts) != 3 or not call.message:
        return await call.answer()
    try:
        uid = int(parts[1])
    except ValueError:
        return await call.answer()
    view = parts[2]
    if view not in VIEW_TITLE:
        return await call.answer()
    if call.from_user.id != uid:
        return await call.answer("Only the person who used /topplayers can use these buttons.", show_alert=True)

    try:
        md, html = await load(view)
    except Exception:
        log.exception("/topplayers callback database error")
        return await call.answer("Something went wrong.", show_alert=True)
    media = await banner()
    msg = call.message
    chat_id, msg_id = msg.chat.id, msg.message_id
    for rich in ((True, False) if RICH_MESSAGES else (False,)):
        for styled in (True, False):
            kb = board_kb(uid, view, styled)
            try:
                if rich:
                    await richmsg.edit_rich(call.bot, chat_id, msg_id, md, reply_markup=kb, media=media)
                elif msg.text:
                    await call.bot.edit_message_text(html, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
                else:
                    await call.bot.edit_message_caption(chat_id=chat_id, message_id=msg_id,
                                                        caption=html, reply_markup=kb)
                return await call.answer()
            except Exception as e:
                if "not modified" in str(e).lower():
                    return await call.answer()
                log.debug("/topplayers edit failed (rich=%s styled=%s): %s", rich, styled, e)
    # the message cannot be edited in place: send a fresh one and delete the old one
    try:
        await send_board(call.bot, chat_id, uid, view, msg.message_thread_id if msg.is_topic_message else None)
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception:
        log.exception("/topplayers resend failed")
    await call.answer()


# ───────────── /settplayer (owner and co-owners) ─────────────
@router.message(Command("settplayer"), F.from_user.id.in_({OWNER_ID, *CO_OWNERS}))
async def set_tplayer(m: Message):
    """Reply to a photo / video / GIF to use it on /topplayers (stored as a file_id)."""
    r = m.reply_to_message
    if not r:
        return await notice(m, "Usage", "Reply to a photo, video or GIF with /settplayer")
    if r.video:
        kind, fid = "video", r.video.file_id
    elif r.animation:
        kind, fid = "animation", r.animation.file_id
    elif r.photo:
        kind, fid = "photo", r.photo[-1].file_id
    else:
        return await notice(m, "Unsupported media", "Reply to a photo, video or GIF.")
    await db.set_setting("topplayers_file_id", fid)
    await db.set_setting("topplayers_type", kind)
    await notice(m, "Top players media updated", f"Type: {kind}. Send /topplayers to preview.")
