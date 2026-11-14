import asyncio
import logging
import time
from html import escape

from aiogram import BaseMiddleware, Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message,
)

import dbase as db
import inspect

import backup
import drop
import harem
import minigame
import richmsg
import shop
import waifu
from minigame import fmt_wait
from richmsg import esc
from configaration import (
    BOT_TOKEN, BOT_NAME, BOT_USERNAME, MAIN_GROUP, OWNER_ID, DROP_EVERY,
    RICH_MESSAGES, CAPTION_ABOVE_MEDIA, CURRENCY_NAME, CURRENCY_SYMBOL,
    DAILY_AMOUNT, DAILY_BIO_AMOUNT, DAILY_COOLDOWN, RARITIES, SUB_RARITIES,
)

router = Router()
log = logging.getLogger("otaku")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

_RICH_CAN_REPLY = "reply_to_message_id" in inspect.signature(richmsg.send_rich).parameters


async def send_rich(m: Message, md: str, **kw):
    """Rich message sent as a reply to the user's command message."""
    if _RICH_CAN_REPLY:
        kw["reply_to_message_id"] = m.message_id
    else:
        log.warning("richmsg.send_rich has no reply_to_message_id; rich reply is not threaded")
    return await richmsg.send_rich(m.bot, m.chat.id, md, **kw)

MAX_CMD_AGE = 60        # seconds: commands older than this are ignored


class IgnoreOldMessages(BaseMiddleware):
    """Skip messages that were sent while the bot was offline / are too old."""
    async def __call__(self, handler, event: Message, data):
        if time.time() - event.date.timestamp() > MAX_CMD_AGE:
            return None
        return await handler(event, data)


# ───────────── /start (rich message + HTML fallback) ─────────────
def start_kb(styled: bool = True) -> InlineKeyboardMarkup:
    # `style` (primary / success / danger) = coloured buttons, Bot API 10.3
    extra_a = {"style": "primary"} if styled else {}
    extra_b = {"style": "success"} if styled else {}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Add me to your group", **extra_a,
                              url=f"https://t.me/{BOT_USERNAME}?startgroup=true")],
        [InlineKeyboardButton(text="Main Group", **extra_b,
                              url=f"https://t.me/{MAIN_GROUP}")],
    ])


def start_md(name: str) -> str:
    """Rich Markdown start text (title, welcome line, pull quote, divider, footer)."""
    return (
        f"# Welcome to {esc(BOT_NAME)}\n\n"
        f"Hey **{esc(name)}**, ready to build your collection?\n\n"
        "<aside>Collect waifus and husbandos. Characters drop in groups, "
        f"the fastest one to grab keeps them.<cite>{escape(BOT_NAME)}</cite></aside>\n\n"
        "---\n\n"
        "###### Add me to a group and let the drops begin"
    )


def start_html(name: str) -> str:
    return (
        f"<b>Welcome to {BOT_NAME}</b>\n"
        f"Hey <b>{escape(name)}</b>, ready to build your collection?\n"
        "<blockquote>Collect waifus and husbandos. Characters drop in groups, "
        "the fastest one to grab keeps them.</blockquote>"
    )


async def _start_media() -> tuple[str, str] | None:
    fid = await db.get_setting("start_file_id")
    kind = await db.get_setting("start_type")
    if fid and kind in ("photo", "video", "animation"):
        return fid, kind
    return None


async def _send_start_rich(m: Message, kb: InlineKeyboardMarkup) -> None:
    """Rich message /start. The /setstart media (video / GIF / photo) is a media block inside it."""
    await send_rich(
        m, start_md(m.from_user.first_name),
        media=await _start_media(),
        reply_markup=kb,
        message_thread_id=topic_id(m),
        media_below=CAPTION_ABOVE_MEDIA,
    )


async def _send_start_html(m: Message, kb: InlineKeyboardMarkup) -> None:
    """HTML fallback (media as caption, or plain text if there is no media / media fails)."""
    text = start_html(m.from_user.first_name)
    media = await _start_media()
    if media:
        fid, kind = media
        send = {"photo": m.reply_photo, "video": m.reply_video,
                "animation": m.reply_animation}[kind]
        try:
            await send(fid, caption=text, reply_markup=kb,
                       show_caption_above_media=CAPTION_ABOVE_MEDIA)
            return
        except TelegramBadRequest as e:
            if "button" in str(e).lower():
                raise            # let the caller retry with plain buttons
            log.warning("start media failed, falling back to text: %s", e)
    await m.reply(text, reply_markup=kb, link_preview_options=NO_PREVIEW)


def _button_error(e: Exception) -> bool:
    return "button" in str(e).lower()


async def send_start(m: Message) -> None:
    """Order: rich + coloured buttons, rich + plain buttons, HTML + coloured, HTML + plain."""
    for styled in (True, False):
        kb = start_kb(styled=styled)
        if RICH_MESSAGES:
            try:
                await _send_start_rich(m, kb)
                return
            except Exception as e:
                if styled and _button_error(e):
                    log.warning("coloured buttons rejected (rich start), retrying plain: %s", e)
                    continue
                log.warning("rich start failed, using HTML fallback: %s", e)
        try:
            await _send_start_html(m, kb)
            return
        except TelegramBadRequest as e:
            if styled and _button_error(e):
                log.warning("coloured buttons rejected (HTML start), retrying plain: %s", e)
                continue
            raise


GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


def topic_id(m: Message) -> int | None:
    """Forum topic id (None outside topics), so replies land in the same topic."""
    return m.message_thread_id if m.is_topic_message else None


async def reply_rich(m: Message, md: str, html: str) -> None:
    """Rich message reply with HTML fallback."""
    if RICH_MESSAGES:
        try:
            await send_rich(m, md, message_thread_id=topic_id(m))
            return
        except Exception as e:
            log.warning("rich reply failed in chat %s, using HTML fallback: %s", m.chat.id, e)
    await m.reply(html)


async def notice(m: Message, title: str, text: str) -> None:
    """Short rich notice (heading + quote) with HTML fallback."""
    await reply_rich(
        m, f"### {esc(title)}\n\n> {esc(text)}",
        f"<b>{escape(title)}</b>\n<blockquote>{escape(text)}</blockquote>")


async def group_start(m: Message) -> None:
    """Groups get the exact same /start reply as PM (after registering the chat)."""
    if m.chat.type in GROUP_TYPES:
        try:
            await db.upsert_chat(m.chat.id, m.chat.title)
        except Exception:
            log.exception("upsert_chat failed (continuing)")
    if m.from_user:
        try:
            await db.upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
        except Exception:
            log.exception("upsert_user failed (continuing)")
    await send_start(m)


@router.message(CommandStart())
async def start_cmd(m: Message):
    try:
        if m.chat.type == ChatType.PRIVATE:
            u = m.from_user
            await db.upsert_user(u.id, u.username, u.first_name)
            await send_start(m)
        else:
            await group_start(m)
    except Exception:
        log.exception("/start failed in chat %s (%s)", m.chat.id, m.chat.type)
        try:                                  # last resort so the command never stays silent
            await m.reply(f"<b>{BOT_NAME}</b>\nWelcome. Open me in PM to get started.")
        except Exception:
            log.exception("/start last-resort reply failed")


# ───────────── currency ─────────────
@router.message(Command("mynex"))
async def my_nex(m: Message):
    u = m.from_user
    if not u:
        return
    try:
        await db.upsert_user(u.id, u.username, u.first_name)
        amount = await db.get_nex(u.id)
    except Exception:
        log.exception("/mynex database error")
        return await notice(m, "Something went wrong", "Try again in a moment.")

    if RICH_MESSAGES:
        md = (
            f"## Your {CURRENCY_NAME} : **{amount}** {CURRENCY_SYMBOL}\n\n"
            f"<aside>{escape(u.first_name)}<cite>{escape(BOT_NAME)} wallet</cite></aside>"
        )
        try:
            await send_rich(m, md, message_thread_id=topic_id(m))
            return
        except Exception as e:
            log.warning("rich /mynex failed, using HTML fallback: %s", e)

    await m.reply(
        f"<b>Your {CURRENCY_NAME} : {amount}</b> {CURRENCY_SYMBOL}\n"
        f"<blockquote>{escape(u.first_name)} · {BOT_NAME} wallet</blockquote>"
    )


_nex_lock = db.nex_lock         # shared with minigame.py so balances can't race


def mention(u) -> str:
    return f'<a href="tg://user?id={u.id}">{escape(u.first_name)}</a>'


def mention_md(u) -> str:
    return f"[{esc(u.first_name)}](tg://user?id={u.id})"


@router.message(Command("ngive"))
async def n_give(m: Message, command: CommandObject):
    """/ngive <amount> (as a reply) - send Nex to the replied user."""
    u = m.from_user
    if not u:
        return
    r = m.reply_to_message
    arg = (command.args or "").strip().split()

    async def fail(title: str, text: str):
        await reply_rich(
            m, f"### {esc(title)}\n\n> {esc(text)}",
            f"<b>{escape(title)}</b>\n<blockquote>{escape(text)}</blockquote>")

    if not r or not r.from_user or r.forum_topic_created or len(arg) != 1:
        return await fail("Usage", "Reply to someone with /ngive amount. Example: /ngive 100")
    if not arg[0].isascii() or not arg[0].isdigit() or int(arg[0]) <= 0:
        return await fail("Invalid amount", "Use a whole number above 0.")

    amount = int(arg[0])
    target = r.from_user
    if target.is_bot:
        return await fail("Not allowed", f"You can't send {CURRENCY_NAME} to a bot.")
    if target.id == u.id:
        return await fail("Not allowed", f"You can't send {CURRENCY_NAME} to yourself.")

    await db.upsert_user(u.id, u.username, u.first_name)
    await db.upsert_user(target.id, target.username, target.first_name)

    async with _nex_lock:
        balance = await db.get_nex(u.id)
        if balance < amount:
            return await fail(
                f"Not enough {CURRENCY_NAME}",
                f"You have {balance} {CURRENCY_SYMBOL}, you tried to send {amount} {CURRENCY_SYMBOL}.")
        await db.add_nex(u.id, -amount)
        try:
            await db.add_nex(target.id, amount)
        except Exception:
            log.exception("/ngive credit failed, refunding %s to %s", amount, u.id)
            await db.add_nex(u.id, amount)
            return await fail("Transfer failed", "Nothing was taken. Try again.")
        left = balance - amount

    md = (
        f"## {esc(CURRENCY_NAME)} sent\n\n"
        f"{mention_md(u)} sent **{amount}** {CURRENCY_SYMBOL} to {mention_md(target)}\n\n"
        f"<aside>Balance: {left} {CURRENCY_SYMBOL}<cite>{escape(BOT_NAME)} wallet</cite></aside>"
    )
    html = (
        f"<b>{CURRENCY_NAME} sent</b>\n"
        f"<blockquote>{mention(u)} sent <b>{amount}</b> {CURRENCY_SYMBOL} to {mention(target)}\n"
        f"Your balance: <b>{left}</b> {CURRENCY_SYMBOL}</blockquote>"
    )
    await reply_rich(m, md, html)


# ───────────── /daily ─────────────
async def bio_has_bot(bot, user_id: int) -> tuple[bool, str]:
    """(is @botusername in the user's bio?, bot username). Falls back to (False, ...) on any error."""
    uname = ""
    try:
        uname = (await bot.me()).username or ""
        bio = (await bot.get_chat(user_id)).bio or ""
        return bool(uname) and f"@{uname}".lower() in bio.lower(), uname
    except Exception as e:
        log.debug("bio check failed for %s: %s", user_id, e)
        return False, uname


@router.message(Command("daily"))
async def daily_cmd(m: Message):
    u = m.from_user
    if not u:
        return
    try:
        await db.upsert_user(u.id, u.username, u.first_name)
        bonus, uname = await bio_has_bot(m.bot, u.id)
        amount = DAILY_BIO_AMOUNT if bonus else DAILY_AMOUNT
        ok, balance, left = await db.claim_daily(u.id, amount, DAILY_COOLDOWN)
    except Exception:
        log.exception("/daily database error")
        return await notice(m, "Something went wrong", "Try again in a moment.")

    if not ok:
        return await notice(m, "Already claimed", f"Come back in {fmt_wait(left)} for your next daily {CURRENCY_NAME}.")

    sym = CURRENCY_SYMBOL
    if bonus:
        extra, extra_h = "Bio bonus applied", "Bio bonus applied"
    elif uname:
        extra = f"Put @{uname} in your bio to claim {DAILY_BIO_AMOUNT} {sym} daily"
        extra_h = escape(extra)
    else:
        extra = extra_h = ""
    md = (
        f"## Daily {esc(CURRENCY_NAME)} claimed\n\n"
        f"**{amount}** {sym} added to your wallet\n\n"
        + (f"{esc(extra)}\n\n" if extra else "")
        + f"<aside>Balance: {balance} {sym}<cite>{escape(u.first_name)}</cite></aside>"
    )
    html = (
        f"<b>Daily {CURRENCY_NAME} claimed</b>\n"
        f"<blockquote><b>{amount}</b> {sym} added to your wallet\n"
        + (f"{extra_h}\n" if extra_h else "")
        + f"Balance: <b>{balance}</b> {sym}</blockquote>"
    )
    await reply_rich(m, md, html)


# ───────────── owner commands ─────────────
@router.message(Command("setstart"), F.from_user.id == OWNER_ID)
async def set_start(m: Message):
    r = m.reply_to_message
    if not r:
        return await notice(m, "Usage", "Reply to a photo, video or GIF with /setstart")

    if r.video:
        kind, fid = "video", r.video.file_id
    elif r.animation:
        kind, fid = "animation", r.animation.file_id
    elif r.photo:
        kind, fid = "photo", r.photo[-1].file_id
    else:
        return await notice(m, "Unsupported media", "Reply to a photo, video or GIF.")

    await db.set_setting("start_file_id", fid)
    await db.set_setting("start_type", kind)
    await notice(m, "Start media updated", f"Type: {kind}. Send /start to preview.")


@router.message(Command("stats"), F.from_user.id == OWNER_ID)
async def owner_stats(m: Message):
    users, chats, chars = await db.stats()
    md = (
        "## Bot stats\n\n"
        "| Stat | Count |\n|---|---:|\n"
        f"| Users | {users} |\n| Groups | {chats} |\n| Characters | {chars} |"
    )
    html = (
        "<b>Bot stats</b>\n"
        f"<blockquote>Users: <b>{users}</b>\nGroups: <b>{chats}</b>\nCharacters: <b>{chars}</b></blockquote>"
    )
    await reply_rich(m, md, html)


# ───────────── /rarity (toggle: rarities <-> sub rarities) ─────────────
BLANK = "\u2800"               # Braille blank, keeps empty table cells valid
SUB_COLS = 3                    # sub rarities are laid out in a compact grid, 3 per row


def _pct(w) -> str:
    return f"{w:g}%"


def rarity_view(kind: str) -> tuple[str, str]:
    """(rich markdown, html fallback) for 'main' or 'sub'."""
    if kind == "sub":
        cells = [f"{emo} {esc(name)}" for name, emo, _ in SUB_RARITIES]
        plain = [f"{emo} {escape(name)}" for name, emo, _ in SUB_RARITIES]
        rows = [cells[i:i + SUB_COLS] for i in range(0, len(cells), SUB_COLS)]
        body = "\n".join("| " + " | ".join(r + [BLANK] * (SUB_COLS - len(r))) + " |" for r in rows)
        md = f"## Sub rarities\n\n| Version | {BLANK} | {BLANK} |\n|---|---|---|\n{body}"
        lines = "\n".join(" · ".join(plain[i:i + SUB_COLS]) for i in range(0, len(plain), SUB_COLS))
        return md, f"<b>Sub rarities</b>\n<blockquote>{lines}</blockquote>"
    rows = "\n".join(f"| {emo} {esc(name)} | {_pct(w)} |" for name, emo, w, _ in RARITIES)
    md = f"## Rarities\n\n| Rarity | Chance |\n|---|---:|\n{rows}"
    lines = "\n".join(f"{emo} {escape(name)} - {_pct(w)}" for name, emo, w, _ in RARITIES)
    return md, f"<b>Rarities</b>\n<blockquote>{lines}</blockquote>"


def rarity_kb(kind: str, uid: int, styled: bool = True) -> InlineKeyboardMarkup:
    to, text = ("main", "Back") if kind == "sub" else ("sub", "Sub rarities")
    extra = {"style": "primary"} if styled else {}
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=f"rar:{to}:{uid}", **extra)]])


@router.message(Command("rarity"))
async def rarity_cmd(m: Message):
    uid = m.from_user.id if m.from_user else 0
    md, html = rarity_view("main")
    # order: rich + coloured button, rich + plain, HTML + coloured, HTML + plain
    for rich in ((True, False) if RICH_MESSAGES else (False,)):
        for styled in (True, False):
            kb = rarity_kb("main", uid, styled)
            try:
                if rich:
                    await send_rich(m, md, message_thread_id=topic_id(m), reply_markup=kb)
                else:
                    await m.reply(html, reply_markup=kb)
                return
            except Exception as e:
                log.warning("/rarity send failed (rich=%s styled=%s): %s", rich, styled, e)


@router.callback_query(F.data.startswith("rar:"))
async def rarity_cb(call: CallbackQuery):
    try:
        _, kind, uid = call.data.split(":")
        uid = int(uid)
    except ValueError:
        return await call.answer()
    if kind not in ("main", "sub") or not call.message:
        return await call.answer()
    if call.from_user.id != uid:
        return await call.answer("Only the person who used /rarity can switch this.", show_alert=True)

    md, html = rarity_view(kind)
    chat_id, msg_id = call.message.chat.id, call.message.message_id
    done = False
    for rich in ((True, False) if RICH_MESSAGES else (False,)):
        for styled in (True, False):
            kb = rarity_kb(kind, uid, styled)
            try:
                if rich:
                    await richmsg.edit_rich(call.bot, chat_id, msg_id, md, reply_markup=kb)
                else:
                    await call.bot.edit_message_text(html, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
                done = True
            except TelegramBadRequest as e:
                if "not modified" in str(e).lower():
                    done = True
                else:
                    log.warning("/rarity edit failed (rich=%s styled=%s): %s", rich, styled, e)
            except Exception as e:
                log.warning("/rarity edit failed (rich=%s styled=%s): %s", rich, styled, e)
            if done:
                break
        if done:
            break
    await call.answer()


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await backup.restore(bot)          # load the pinned backup BEFORE the database is opened
    await db.init_db()
    dp = Dispatcher()
    dp.message.outer_middleware(IgnoreOldMessages())
    dp.message.outer_middleware(drop.DropCounter())
    dp.include_router(router)
    dp.include_router(minigame.router)
    dp.include_router(shop.router)
    dp.include_router(waifu.router)
    dp.include_router(drop.router)
    dp.include_router(harem.router)
    dp.include_router(backup.router)
    backup_task = None
    try:
        await bot.delete_webhook(drop_pending_updates=True)   # also clears old queued commands
        me = await bot.get_me()
        log.info("Started as @%s", me.username)
        if me.username and me.username.lower() != BOT_USERNAME.lower():
            log.warning("BOT_USERNAME (%s) does not match the token's bot (@%s)", BOT_USERNAME, me.username)
        backup_task = asyncio.create_task(backup.backup_loop(bot))
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        if backup_task:
            backup_task.cancel()
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
