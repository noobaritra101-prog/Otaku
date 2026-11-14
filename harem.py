"""Otaku Nexus - /harem (alias /collection): view a user's whole collection, series by series.

- Rich message: picture on top (the user's /fav character, else the first character of the top series),
  no totals line, then one section per series:
      Series name (collected/total)
      ID. Name (sub emoji) - Rarity    (the ID is the number of a numbered list), then a --- divider; the /fav character is highlighted (==text== in rich, bold in the fallback) and gets a ✨ before the name
- 5 series per page, smart Prev / [page/pages] / Next buttons (only with 2+ pages; no Prev on page 1, no Next on the last page), footer "Page x/y", and a View Collection (inline) button. Buttons only work for the
  person who used the command.
- Fallback: photo with an HTML caption, then plain text.
"""
import logging
from collections import defaultdict
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import dbase as db
from config import RARITIES
from drop import collection_btn, edit_card, send_card_rich, sub_emoji
from minigame import notice
from richmsg import esc
from waifu import with_styles

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
