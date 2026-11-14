"""Otaku Nexus - /shop: buy cards with Nex.

- Every user gets their own random cards each day (no shared stock, any number of people can buy the same
  card): one Rare, one Premium and one Legendary (SHOP_SLOTS), plus on Sunday one Mythic and one Celestial
  (SHOP_SUNDAY_SLOTS). The day rolls over at midnight of SHOP_UTC_OFFSET_MIN. Divine, Nexus, Common, Uncommon
  and event sub rarities (unless the event is active) are never sold.
- Prices come from SHOP_PRICES by main rarity. Each card can be bought once per day.
- Banner: the owner sends /setshop as a reply to a photo / video / GIF (stored as a file_id, always works);
  SHOP_BANNER (a URL) is only the fallback. If the banner is rejected the shop is sent without it.
- Rich message list + buttons. Tap a card to see it with its picture, Buy asks to confirm, the purchase is atomic
  (dbase.shop_buy). Only the person who used /shop can press the buttons.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import dbase as db
import richmsg
from config import (
    CO_OWNERS, CURRENCY_NAME, CURRENCY_SYMBOL, OWNER_ID, DROP_ACTIVE_EVENTS, EVENT_SUBS, RARITIES, RICH_MESSAGES,
    SHOP_BANNER, SHOP_PRICES, SHOP_SLOTS, SHOP_SUNDAY_SLOTS, SHOP_UTC_OFFSET_MIN,
)
from drop import edit_card, send_card_rich, sub_emoji
from minigame import fmt_wait, notice, topic_id
from richmsg import esc

router = Router()
log = logging.getLogger("otaku.shop")

RAR_EMO = {n: e for n, e, *_ in RARITIES}
SYM = CURRENCY_SYMBOL
BLANK = "\u2800"      # Braille blank: keeps empty table header cells valid
_stock_lock = asyncio.Lock()


# ───────────── shop clock and stock ─────────────
def shop_clock() -> tuple[str, bool, int]:
    """(day key, is it Sunday, seconds until the shop restocks) in the shop's own time zone."""
    now = datetime.now(timezone(timedelta(minutes=SHOP_UTC_OFFSET_MIN)))
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return now.strftime("%Y-%m-%d"), now.weekday() == 6, max(1, int((nxt - now).total_seconds()))


async def ensure_stock(day: str, sunday: bool, uid: int) -> None:
    """Pick this user's random cards the first time they open the shop that day."""
    if await db.shop_has_offers(day, uid):
        return
    async with _stock_lock:
        if await db.shop_has_offers(day, uid):
            return
        blocked = [s for s in EVENT_SUBS if s not in DROP_ACTIVE_EVENTS]
        items = []
        for rarity, count in SHOP_SLOTS + (SHOP_SUNDAY_SLOTS if sunday else []):
            price = SHOP_PRICES.get(rarity)
            if not price:
                continue
            for c in await db.pick_characters(rarity, count, blocked):
                items.append((c["char_id"], rarity, price))
        if items:
            await db.shop_add(day, uid, items)
        await db.shop_cleanup(day)


async def banner():
    """(file_id or URL, kind) for the shop picture: /setshop media first, else SHOP_BANNER, else None."""
    fid, kind = await db.get_setting("shop_file_id"), await db.get_setting("shop_type")
    if fid and kind in ("photo", "video", "animation"):
        return fid, kind
    return (SHOP_BANNER, "photo") if SHOP_BANNER else None


# ───────────── text helpers ─────────────
def cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def money(n: int) -> str:
    return f"{n:,} {SYM}"


def name_plain(c) -> str:
    return c["name"] + (f" ({sub_emoji(c['sub_rarity'])})" if c["sub_rarity"] else "")


def name_md(c) -> str:
    return esc(c["name"]) + (f" ({sub_emoji(c['sub_rarity'])})" if c["sub_rarity"] else "")


def rarity_tag(c) -> str:
    return f"{RAR_EMO.get(c['rarity'], '')} {c['rarity']}".strip()


# ───────────── views ─────────────
FOOT = "Shop will reset at midnight UTC"


def main_view(items, balance: int, sunday: bool, secs: int) -> tuple[str, str]:
    """(rich markdown, html fallback) of the shop list: one block per card, divider between them."""
    head = f"## Shop\n\nYour {esc(CURRENCY_NAME)}: **{balance:,}** {SYM}\n\n---\n\n"
    if not items:
        md = head + f"The shelves are empty right now.\n\n---\n\n###### {FOOT}"
        return md, f"<b>Shop</b>\n<blockquote>The shelves are empty right now.</blockquote>\n{FOOT}"
    blocks = [f"**{name_md(c)}**\n\n{rarity_tag(c)} · {c['price']:,} {SYM}"
              for c in items]
    md = head + "\n\n---\n\n".join(blocks) + f"\n\n---\n\n###### {FOOT}"
    lines = "\n──────────\n".join(
        f"<b>{escape(name_plain(c))}</b>\n{rarity_tag(c)} · {c['price']:,} {SYM}"
        for c in items)
    html = (f"<b>Shop</b>\nYour {escape(CURRENCY_NAME)}: <b>{balance:,}</b> {SYM}\n"
            f"<blockquote>{lines}</blockquote>\n{FOOT}")
    return md, html


def main_kb(items, uid: int, styled: bool = True) -> InlineKeyboardMarkup:
    """One button per card with just its name; bought cards get no colour. Close is red."""
    btns = [InlineKeyboardButton(
        text=cut(c["name"], 30), callback_data=f"sh:{uid}:i:{c['char_id']}",
        **({"style": "primary"} if styled and not c["bought"] else {})) for c in items]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="Close", callback_data=f"sh:{uid}:x",
                                      **({"style": "danger"} if styled else {}))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_view(c, balance: int, mode: str) -> tuple[str, str]:
    """mode: info | confirm | done. Returns (rich markdown, html caption)."""
    title = {"info": "Shop", "confirm": "Confirm purchase", "done": "Purchased"}[mode]
    lines = [("Rarity", rarity_tag(c)), ("Series", cut(c["anime"], 40)),
             ("Price", money(c["price"])), (CURRENCY_NAME, money(balance))]
    if mode == "confirm":
        head_md = f"Buy **{name_md(c)}** for **{money(c['price'])}**?"
        head_html = f"Buy <b>{escape(name_plain(c))}</b> for <b>{money(c['price'])}</b>?"
    elif mode == "done":
        head_md = f"**{name_md(c)}** is now in your harem."
        head_html = f"<b>{escape(name_plain(c))}</b> is now in your harem."
    else:
        head_md, head_html = f"**{name_md(c)}**", f"<b>{escape(name_plain(c))}</b>"
    md = (f"## {title}\n\n{head_md}\n\n| {BLANK} | {BLANK} |\n|---|---|\n"
          + "\n".join(f"| {esc(k)} | {esc(v)} |" for k, v in lines))
    html = (f"<b>{title}</b>\n{head_html}\n<blockquote>"
            + "\n".join(f"{escape(k)}: {escape(v)}" for k, v in lines) + "</blockquote>")
    return md, html


def item_kb(uid: int, cid: int, mode: str, can_buy: bool, styled: bool = True) -> InlineKeyboardMarkup:
    def b(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=data, **({"style": style} if styled and style else {}))

    if mode == "confirm":
        rows = [[b("Confirm", f"sh:{uid}:b:{cid}", "success"), b("Cancel", f"sh:{uid}:i:{cid}", "danger")]]
    elif mode == "done":
        rows = [[b("Back to shop", f"sh:{uid}:m", "primary")]]
    else:
        rows = []
        if can_buy:
            rows.append([b("Buy", f"sh:{uid}:c:{cid}", "success")])
        rows.append([b("Back", f"sh:{uid}:m")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ───────────── sending / editing ─────────────
async def load(uid: int):
    day, sunday, secs = shop_clock()
    await ensure_stock(day, sunday, uid)
    return day, sunday, secs, await db.shop_items(day, uid), await db.get_nex(uid)


async def show_main(call: CallbackQuery, uid: int) -> None:
    """Edit the pressed message back into the shop list (a card message is replaced by a new message)."""
    day, sunday, secs, items, bal = await load(uid)
    md, html = main_view(items, bal, sunday, secs)
    BANNER = await banner()
    chat_id, msg_id = call.message.chat.id, call.message.message_id
    for rich in ((True, False) if RICH_MESSAGES else (False,)):
        for styled in (True, False):
            kb = main_kb(items, uid, styled)
            try:
                if rich:
                    await richmsg.edit_rich(call.bot, chat_id, msg_id, md, reply_markup=kb, media=BANNER)
                else:
                    await call.bot.edit_message_text(html, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
                return
            except Exception as e:
                if "not modified" in str(e).lower():
                    return
                log.debug("shop main edit failed (rich=%s styled=%s): %s", rich, styled, e)
    # the message carries a picture and cannot be edited into a text list: send a new one, delete the old
    try:
        await richmsg.send_rich(call.bot, chat_id, md, reply_markup=main_kb(items, uid, False), media=BANNER,
                                message_thread_id=call.message.message_thread_id if call.message.is_topic_message else None)
    except Exception:
        await call.bot.send_message(chat_id, html, reply_markup=main_kb(items, uid, False))
    try:
        await call.message.delete()
    except Exception:
        pass


async def show_item(call: CallbackQuery, c, mode: str, can_buy: bool = True) -> None:
    uid = call.from_user.id
    md, html = item_view(c, await db.get_nex(uid), mode)
    await edit_card(call, md, html, c, lambda s: item_kb(uid, c["char_id"], mode, can_buy, s))


# ───────────── handlers ─────────────
@router.message(Command("shop"))
async def shop_cmd(m: Message):
    u = m.from_user
    if not u:
        return
    try:
        await db.upsert_user(u.id, u.username, u.first_name)
        day, sunday, secs, items, bal = await load(u.id)
    except Exception:
        log.exception("/shop database error")
        return await notice(m, "Something went wrong", "Try again in a moment.")
    md, html = main_view(items, bal, sunday, secs)
    BANNER = await banner()
    # order: rich + banner, rich, HTML photo + banner (caption), HTML text; each with coloured then plain buttons
    combos = ([("rich", BANNER), ("rich", None)] if RICH_MESSAGES else []) + [("html", BANNER), ("html", None)]
    combos = list(dict.fromkeys(combos))                 # no duplicates when there is no banner
    for kind, bn in combos:
        for styled in (True, False):
            kb = main_kb(items, u.id, styled)
            try:
                if kind == "rich":
                    await richmsg.send_rich(m.bot, m.chat.id, md, media=bn, reply_markup=kb,
                                            message_thread_id=topic_id(m), reply_to_message_id=m.message_id)
                elif bn:
                    await {"photo": m.reply_photo, "video": m.reply_video, "animation": m.reply_animation}[bn[1]](
                        bn[0], caption=html, reply_markup=kb)
                else:
                    await m.reply(html, reply_markup=kb)
                return
            except Exception as e:
                log.warning("/shop send failed (%s, banner=%s, styled=%s): %s", kind, bool(bn), styled, e)


@router.callback_query(F.data.startswith("sh:"))
async def shop_cb(call: CallbackQuery):
    parts = call.data.split(":")          # sh:<uid>:<action>[:<char_id>]
    if len(parts) < 3 or not call.message:
        return await call.answer()
    try:
        uid = int(parts[1])
        cid = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        return await call.answer()
    if call.from_user.id != uid:
        return await call.answer("Only the person who used /shop can use these buttons.", show_alert=True)
    act = parts[2]

    try:
        if act == "x":
            await call.answer()
            try:
                await call.message.delete()
            except Exception:
                pass
            return
        if act == "m":
            await show_main(call, uid)
            return await call.answer()

        day, sunday, secs = shop_clock()
        await ensure_stock(day, sunday, uid)
        c = await db.shop_item(day, uid, cid)
        if not c:
            await call.answer("This card is no longer in your shop.", show_alert=True)
            return await show_main(call, uid)

        if act == "i":                       # card details
            await show_item(call, c, "info", can_buy=not c["bought"])
            return await call.answer("You already bought this one today." if c["bought"] else None)
        if act == "c":                       # confirm step
            if c["bought"]:
                return await call.answer("You already bought this one today.", show_alert=True)
            if await db.get_nex(uid) < c["price"]:
                return await call.answer(f"Not enough {CURRENCY_NAME}. You need {c['price']:,} {SYM}.", show_alert=True)
            await show_item(call, c, "confirm")
            return await call.answer()
        if act == "b":                       # buy
            st, bal = await db.shop_buy(uid, day, cid)
            if st == "ok":
                await show_item(call, c, "done")
                return await call.answer("Purchased")
            msg = {
                "gone": "This card is no longer in your shop.",
                "limit": "You already bought this one today.",
                "poor": f"Not enough {CURRENCY_NAME}. You have {bal:,} {SYM}, you need {c['price']:,} {SYM}.",
            }.get(st, "Could not buy this card.")
            await call.answer(msg, show_alert=True)
            return await show_main(call, uid)
        await call.answer()
    except Exception:
        log.exception("/shop callback failed (%s)", call.data)
        await call.answer("Something went wrong.", show_alert=True)


@router.message(Command("setshop"), F.from_user.id.in_({OWNER_ID, *CO_OWNERS}))
async def set_shop(m: Message):
    """Reply to a photo / video / GIF to use it as the /shop picture (stored as a file_id)."""
    r = m.reply_to_message
    if not r:
        return await notice(m, "Usage", "Reply to a photo, video or GIF with /setshop")
    if r.video:
        kind, fid = "video", r.video.file_id
    elif r.animation:
        kind, fid = "animation", r.animation.file_id
    elif r.photo:
        kind, fid = "photo", r.photo[-1].file_id
    else:
        return await notice(m, "Unsupported media", "Reply to a photo, video or GIF.")
    await db.set_setting("shop_file_id", fid)
    await db.set_setting("shop_type", kind)
    await notice(m, "Shop picture updated", f"Type: {kind}. Send /shop to preview.")
