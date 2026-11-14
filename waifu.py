"""Character management: /upload, /uploaded (rich, toggle List/Series/Rarity/Sub-rarity), /botsadmins, /edit (edit panel + delete), /addadmin, /rmadmin.

Own router: otaku.py only does `import waifu` and `dp.include_router(waifu.router)`.
Every upload is also posted with the picture to the database group (DB_GROUP).
"""
import logging
from collections import Counter
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaAnimation,
    InputMediaPhoto, InputMediaVideo, Message,
)

import dbase as db
from config import OWNER_ID, CO_OWNERS, DB_GROUP, RARITIES, SUB_RARITIES, SUB_RULES, RICH_MESSAGES
from minigame import notice
from richmsg import edit_rich, esc, send_rich

router = Router()
log = logging.getLogger("otaku.waifu")

DB_CHAT = f"@{DB_GROUP}"
TOP = {OWNER_ID, *CO_OWNERS}      # owner + co-owners
NONE_SUB = "None"

# admin id -> (field, char_id, panel chat_id, panel message_id). In memory, cleared on restart.
PENDING: dict[int, tuple[str, int, int, int]] = {}
FIELD_LABEL = {"name": "name", "anime": "series", "file_id": "new picture"}


# ───────────── helpers ─────────────
def media_of(msg: Message | None) -> tuple[str, str] | None:
    """(kind, file_id) of the picture / video / GIF in a message, else None."""
    if not msg:
        return None
    if msg.animation:
        return "animation", msg.animation.file_id
    if msg.video:
        return "video", msg.video.file_id
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    return None


def input_media(kind: str, file_id: str, caption: str, spoiler: bool = False):
    cls = {"video": InputMediaVideo, "animation": InputMediaAnimation}.get(kind, InputMediaPhoto)
    return cls(media=file_id, caption=caption, has_spoiler=spoiler)


async def send_media(target, kind: str, file_id: str, caption: str, spoiler: bool = False, **kw):
    """target is a Message (reply_*) or (bot, chat_id) for send_*. kind: photo | video | animation."""
    if isinstance(target, Message):
        fn = {"video": target.reply_video, "animation": target.reply_animation}.get(kind, target.reply_photo)
    else:
        bot, chat_id = target
        fn = {"video": bot.send_video, "animation": bot.send_animation}.get(kind, bot.send_photo)
        kw["chat_id"] = chat_id
    return await fn(**{kind: file_id}, caption=caption, has_spoiler=spoiler, **kw)


async def allowed(user_id: int | None) -> bool:
    return bool(user_id) and (user_id in TOP or await db.is_admin(user_id))


def rarity_label(name: str) -> str:
    for n, emo, *_ in RARITIES:
        if n == name:
            return f"{emo} {n}"
    return name


def sub_label(name: str | None) -> str:
    if not name:
        return NONE_SUB
    for n, emo, *_ in SUB_RARITIES:
        if n == name:
            return f"{emo} {n}"
    return name


def find_rarity(s: str) -> str | None:
    """'legendary' or its list number (1-9) -> canonical name."""
    s = s.strip().lower()
    if s.isdigit() and 1 <= int(s) <= len(RARITIES):
        return RARITIES[int(s) - 1][0]
    return next((n for n, *_ in RARITIES if n.lower() == s), None)


def find_sub(s: str) -> str | None | bool:
    """'bunny' -> 'Bunny', '-' / 'none' -> None, unknown -> False."""
    s = s.strip().lower()
    if s in ("", "-", "none", "no"):
        return None
    return next((n for n, *_ in SUB_RARITIES if n.lower() == s), False)


def allowed_subs(rarity: str) -> list[str]:
    """Sub rarity names a main rarity may carry (see SUB_RULES in config.py)."""
    names = [n for n, *_ in SUB_RARITIES]
    rule = SUB_RULES.get(rarity)
    return names if rule is None else [n for n in names if n in rule]


def sub_rule_text(rarity: str) -> str:
    subs = allowed_subs(rarity)
    if not subs:
        return f"{rarity} cards can't have a sub-rarity."
    return f"{rarity} cards can only be {', '.join(subs)}."


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(name)}</a>'


def db_caption(c) -> str:
    """Caption posted in the database group: bold labels, plain values, italic artist line."""
    return (
        "<b>#NEWADDED</b>\n\n"
        f"<b>SERIES :</b> {escape(c['anime'])}\n"
        f"<b>NAME :</b> {escape(c['name'])}\n"
        f"<b>RARITY :</b> {rarity_label(c['rarity'])}\n"
        f"<b>SUB-RARITY :</b> {sub_label(c['sub_rarity'])}\n"
        f"<b>ID :</b> {c['char_id']}\n\n"
        f"<i><b>~ Artist 🎨 :</b> {mention(c['added_by'], c['artist_name'] or 'Unknown')}</i>"
    )


def panel_caption(c) -> str:
    return (
        f"<b>Edit waifu</b>  ·  ID <code>{c['char_id']}</code>\n\n"
        f"Name: <b>{escape(c['name'])}</b>\n"
        f"Series: <b>{escape(c['anime'])}</b>\n"
        f"Rarity: <b>{rarity_label(c['rarity'])}</b>\n"
        f"Sub-rarity: <b>{sub_label(c['sub_rarity'])}</b>"
    )


def _b(text: str, data: str, style: str | None, styled: bool) -> InlineKeyboardButton:
    extra = {"style": style} if (style and styled) else {}
    return InlineKeyboardButton(text=text, callback_data=data, **extra)


def panel_kb(cid: int, styled: bool = True) -> InlineKeyboardMarkup:
    b = lambda t, a, s=None: _b(t, f"ew:{a}:{cid}", s, styled)
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("Name", "name"), b("Series", "anime")],
        [b("Rarity", "rar"), b("Sub-rarity", "sub")],
        [b("Picture", "file_id")],
        [b("Delete", "del", "danger"), b("Close", "close")],
    ])


def pick_kb(cid: int, kind: str, styled: bool = True, rarity: str | None = None) -> InlineKeyboardMarkup:
    """Grid of rarities (kind 'rar') or sub rarities (kind 'sub')."""
    if kind == "rar":
        items, cols, pre = [(f"{e} {n}", n) for n, e, *_ in RARITIES], 3, "r"
    else:
        ok = allowed_subs(rarity) if rarity else None
        items, cols, pre = [(f"{e} {n}", n) for n, e, *_ in SUB_RARITIES if ok is None or n in ok], 3, "s"
    btns = [InlineKeyboardButton(text=t, callback_data=f"ew:{pre}:{cid}:{n}") for t, n in items]
    rows = [btns[i:i + cols] for i in range(0, len(btns), cols)]
    if kind == "sub":
        rows.append([InlineKeyboardButton(text="No sub-rarity", callback_data=f"ew:s:{cid}:-")])
    rows.append([_b("Back", f"ew:back:{cid}", "primary", styled)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb(cid: int, styled: bool = True) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _b("Yes, delete", f"ew:delyes:{cid}", "danger", styled),
        _b("No", f"ew:back:{cid}", "success", styled),
    ]])


async def edit_panel(bot, chat_id: int, msg_id: int, caption: str, make_kb) -> None:
    """Edit the panel caption + keyboard; coloured buttons first, plain if Telegram rejects them."""
    for styled in (True, False):
        try:
            await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id,
                                           caption=caption, reply_markup=make_kb(styled))
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
            if styled and "button" in str(e).lower():
                continue
            raise


async def sync_db_post(bot, c) -> None:
    """Keep the post in the database group in line with the card (caption, and picture if changed)."""
    if not c["db_msg_id"]:
        return
    try:
        await bot.edit_message_caption(chat_id=DB_CHAT, message_id=c["db_msg_id"], caption=db_caption(c))
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("db post caption edit failed for %s: %s", c["char_id"], e)


# ───────────── admins ─────────────
@router.message(Command("addadmin"), F.from_user.id.in_(TOP))
async def add_admin_cmd(m: Message):
    r = m.reply_to_message
    if not r or not r.from_user or r.from_user.is_bot:
        return await notice(m, "Usage", "Reply to a user with /addadmin")
    await db.add_admin(r.from_user.id)
    await notice(m, "Admin added", f"{r.from_user.first_name} can upload and edit characters.")


@router.message(Command("rmadmin"), F.from_user.id.in_(TOP))
async def rm_admin_cmd(m: Message):
    r = m.reply_to_message
    if not r or not r.from_user:
        return await notice(m, "Usage", "Reply to an admin with /rmadmin")
    await db.del_admin(r.from_user.id)
    await notice(m, "Admin removed", f"{r.from_user.first_name} can no longer upload.")


# ───────────── /upload ─────────────
USAGE = (
    "Reply to a picture, video or GIF (or send it with this as the caption):\n"
    "/upload Series | Name | Rarity | Sub-rarity\n"
    "Rarity: name or number 1-9. Sub-rarity: name or - for none."
)


@router.message(Command("upload"))
async def upload_cmd(m: Message, command: CommandObject):
    u = m.from_user
    if not await allowed(u.id if u else None):
        return
    found = media_of(m.reply_to_message) or media_of(m)
    if not found:
        return await notice(m, "Usage", USAGE)

    parts = [p.strip() for p in (command.args or "").split("|")]
    if len(parts) < 3 or not parts[0] or not parts[1]:
        return await notice(m, "Usage", USAGE)
    series, name = parts[0], parts[1]
    rarity = find_rarity(parts[2])
    if not rarity:
        return await notice(m, "Unknown rarity", "Use /rarity to see the list, or a number 1-9.")
    sub = find_sub(parts[3]) if len(parts) > 3 else None
    if sub is False:
        return await notice(m, "Unknown sub-rarity", "Use /rarity then Sub rarities, or - for none.")
    if sub and sub not in allowed_subs(rarity):
        return await notice(m, "Sub-rarity not allowed", sub_rule_text(rarity))

    kind, file_id = found
    cid = await db.add_character(name, series, "waifu", rarity, sub, file_id, u.id, u.first_name, kind)
    c = await db.get_character(cid)

    warn = ""
    try:
        post = await send_media((m.bot, DB_CHAT), kind, file_id, db_caption(c), spoiler=True)
        await db.update_character(cid, db_msg_id=post.message_id)
    except Exception as e:
        log.warning("post to %s failed: %s", DB_CHAT, e)
        warn = f" Could not post to {DB_CHAT}, make the bot admin there."
    await notice(m, "Uploaded", f"ID {cid} · {name} · {rarity_label(rarity)} · {sub_label(sub)}.{warn}")


# ───────────── /botsadmins (owner only, rich, HTML fallback) ─────────────
def mention_md(user_id: int, name: str) -> str:
    return f"[{esc(name)}](tg://user?id={user_id})"


async def person_parts(bot, uid: int) -> tuple[str, str]:
    """(html mention, rich markdown mention) for a user id."""
    name = await db.get_user_name(uid)
    if not name:
        try:
            name = (await bot.get_chat(uid)).first_name
        except Exception:
            name = None
    name = name or str(uid)
    return mention(uid, name), mention_md(uid, name)


@router.message(Command("botsadmins", "botsadmin", "botadmins", "botadmin"), F.from_user.id == OWNER_ID)
async def bots_admins_cmd(m: Message):
    bot = m.bot
    owner = await person_parts(bot, OWNER_ID)
    co = [await person_parts(bot, i) for i in CO_OWNERS if i != OWNER_ID]
    ad = [await person_parts(bot, i) for i in await db.list_admins() if i not in TOP]

    def quote_md(items) -> str:
        return "\n".join(f"> {x[1]}" for x in items) if items else "> None"

    def quote_html(items) -> str:
        return "\n".join(x[0] for x in items) if items else "None"

    md_text = (
        "## Bot team\n\n"
        f"### Owner\n{quote_md([owner])}\n\n"
        f"### Co-owners\n{quote_md(co)}\n\n"
        f"### Admins\n{quote_md(ad)}"
    )
    html_text = (
        "<b>Bot team</b>\n\n"
        f"<b>Owner</b>\n<blockquote>{quote_html([owner])}</blockquote>\n"
        f"<b>Co-owners</b>\n<blockquote>{quote_html(co)}</blockquote>\n"
        f"<b>Admins</b>\n<blockquote>{quote_html(ad)}</blockquote>"
    )
    if RICH_MESSAGES:
        try:
            await send_rich(bot, m.chat.id, md_text,
                            message_thread_id=m.message_thread_id if m.is_topic_message else None,
                            reply_to_message_id=m.message_id)
            return
        except Exception as e:
            log.warning("rich /botsadmins failed, using HTML: %s", e)
    await m.reply(html_text)


# ───────────── /uploaded: rich list with List / Series / Rarity / Sub-rarity toggle ─────────────
PAGE = 15          # rows per page in the List view
GROUP_PAGE = 20    # rows per page in the Series view
VIEWS = (("list", "List"), ("series", "Series"), ("rar", "Rarity"), ("sub", "Sub-rarity"))
VIEW_TITLE = {"list": "All characters", "series": "By series", "rar": "By rarity", "sub": "By sub-rarity"}

async def with_styles(call):
    """call(styled) -> awaitable. Coloured buttons first, plain if Telegram rejects them."""
    for styled in (True, False):
        try:
            return await call(styled)
        except TelegramBadRequest as e:
            if styled and "button" in str(e).lower():
                continue
            raise


async def build_view(view: str, page: int) -> dict:
    total = await db.count_characters()
    rows = await db.list_characters(0, max(total, 1))
    if view == "list":
        pages = max(1, -(-total // PAGE))
        page = min(max(page, 1), pages)
        body = []
        for r in rows[(page - 1) * PAGE: page * PAGE]:
            sub = f" · {sub_label(r['sub_rarity'])}" if r["sub_rarity"] else ""
            body.append((str(r["char_id"]), r["name"], r["anime"], f"{rarity_label(r['rarity'])}{sub}"))
        head = ("ID", "Name", "Series", "Rarity")
    elif view == "series":
        counts = Counter(r["anime"] for r in rows)
        items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        pages = max(1, -(-len(items) // GROUP_PAGE))
        page = min(max(page, 1), pages)
        body = [(n, str(c)) for n, c in items[(page - 1) * GROUP_PAGE: page * GROUP_PAGE]]
        head = ("Series", "Characters")
    elif view == "rar":
        counts, pages, page = Counter(r["rarity"] for r in rows), 1, 1
        body = [(f"{e} {n}", str(counts.get(n, 0))) for n, e, *_ in RARITIES]
        head = ("Rarity", "Characters")
    else:
        counts, pages, page = Counter(r["sub_rarity"] for r in rows if r["sub_rarity"]), 1, 1
        body = [(f"{e} {n}", str(counts.get(n, 0))) for n, e, *_ in SUB_RARITIES]
        body.append((NONE_SUB, str(sum(1 for r in rows if not r["sub_rarity"]))))
        head = ("Sub-rarity", "Characters")
    return {"view": view, "page": page, "pages": pages, "total": total, "head": head, "body": body}


def view_md(d: dict) -> str:
    out = ["## Uploaded characters", f"{d['total']} total · {esc(VIEW_TITLE[d['view']])}", ""]
    if not d["body"]:
        out.append("Nothing uploaded yet.")
    else:
        n = len(d["head"])
        aligns = ["---"] * n if d["view"] == "list" else ["---"] * (n - 1) + ["---:"]
        out.append("| " + " | ".join(esc(h) for h in d["head"]) + " |")
        out.append("| " + " | ".join(aligns) + " |")
        out += ["| " + " | ".join(esc(c) for c in row) + " |" for row in d["body"]]
    if d["pages"] > 1:
        out += ["", f"###### Page {d['page']}/{d['pages']}"]
    return "\n".join(out)


def view_html(d: dict) -> str:
    if not d["body"]:
        inner = "Nothing uploaded yet."
    elif d["view"] == "list":
        inner = "\n".join(f"<code>{escape(i)}</code> · <b>{escape(n)}</b> · {escape(s)} · {escape(r)}"
                          for i, n, s, r in d["body"])
    else:
        inner = "\n".join(f"{escape(a)} · <b>{escape(b)}</b>" for a, b in d["body"])
    foot = f"\n<b>Page {d['page']}/{d['pages']}</b>" if d["pages"] > 1 else ""
    return (f"<b>Uploaded characters</b>  ·  {d['total']} total\n<b>{VIEW_TITLE[d['view']]}</b>\n"
            f"<blockquote expandable>{inner}</blockquote>{foot}")


def uploaded_kb(d: dict, styled: bool = True) -> InlineKeyboardMarkup:
    v = d["view"]
    tabs = []
    for key, label in VIEWS:
        on = key == v
        text = f"• {label}" if (on and not styled) else label
        tabs.append(_b(text, f"ul:{key}:1", "primary" if on else None, styled))
    rows = [tabs]
    nav = []
    if d["page"] > 1:
        nav.append(InlineKeyboardButton(text="Prev", callback_data=f"ul:{v}:{d['page'] - 1}"))
    if d["page"] < d["pages"]:
        nav.append(InlineKeyboardButton(text="Next", callback_data=f"ul:{v}:{d['page'] + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_view(m: Message, d: dict) -> None:
    if RICH_MESSAGES:
        try:
            await with_styles(lambda s: send_rich(
                m.bot, m.chat.id, view_md(d), reply_markup=uploaded_kb(d, s),
                message_thread_id=m.message_thread_id if m.is_topic_message else None,
                reply_to_message_id=m.message_id))
            return
        except Exception as e:
            log.warning("rich /uploaded failed, using HTML: %s", e)
    await with_styles(lambda s: m.reply(view_html(d), reply_markup=uploaded_kb(d, s)))


async def edit_view(msg: Message, d: dict) -> None:
    if RICH_MESSAGES:
        try:
            await with_styles(lambda s: edit_rich(
                msg.bot, msg.chat.id, msg.message_id, view_md(d), reply_markup=uploaded_kb(d, s)))
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
            log.warning("rich /uploaded edit failed, using HTML: %s", e)
        except Exception as e:
            log.warning("rich /uploaded edit failed, using HTML: %s", e)
    try:
        await with_styles(lambda s: msg.edit_text(view_html(d), reply_markup=uploaded_kb(d, s)))
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise


@router.message(Command("uploaded", "chars"))
async def uploaded_cmd(m: Message):
    if not await allowed(m.from_user.id if m.from_user else None):
        return
    await send_view(m, await build_view("list", 1))


@router.callback_query(F.data.startswith("ul:"))
async def uploaded_cb(call: CallbackQuery):
    if not await allowed(call.from_user.id):
        return await call.answer("Admins only.", show_alert=True)
    if not call.message:
        return await call.answer()
    parts = call.data.split(":")
    view, page = "list", 1
    try:
        if len(parts) == 2:          # old buttons: ul:<page>
            page = int(parts[1])
        else:
            view, page = parts[1], int(parts[2])
    except (ValueError, IndexError):
        return await call.answer()
    if view not in VIEW_TITLE:
        return await call.answer()
    await edit_view(call.message, await build_view(view, page))
    await call.answer()


# ───────────── /edit panel ─────────────
@router.message(Command("edit"))
async def edit_cmd(m: Message, command: CommandObject):
    if not await allowed(m.from_user.id if m.from_user else None):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        return await notice(m, "Usage", "/edit ID")
    c = await db.get_character(int(arg))
    if not c:
        return await notice(m, "Not found", f"No character with ID {arg}.")
    for styled in (True, False):
        try:
            await send_media(m, c["media_type"], c["file_id"], panel_caption(c), reply_markup=panel_kb(c["char_id"], styled))
            return
        except TelegramBadRequest as e:
            if styled and "button" in str(e).lower():
                continue
            raise


@router.callback_query(F.data.startswith("ew:"))
async def edit_cb(call: CallbackQuery):
    if not await allowed(call.from_user.id):
        return await call.answer("Admins only.", show_alert=True)
    if not call.message:
        return await call.answer()
    try:
        _, act, cid, *rest = call.data.split(":")
        cid = int(cid)
    except ValueError:
        return await call.answer()
    chat_id, msg_id = call.message.chat.id, call.message.message_id
    bot = call.bot

    if act == "close":
        PENDING.pop(call.from_user.id, None)
        try:
            await call.message.delete()
        except Exception:
            pass
        return await call.answer()

    c = await db.get_character(cid)
    if not c:
        await call.answer("This character no longer exists.", show_alert=True)
        try:
            await call.message.delete()
        except Exception:
            pass
        return

    async def show_panel():
        c2 = await db.get_character(cid)
        await edit_panel(bot, chat_id, msg_id, panel_caption(c2), lambda s: panel_kb(cid, s))

    if act == "back":
        PENDING.pop(call.from_user.id, None)
        await show_panel()
        return await call.answer()

    if act in ("name", "anime", "file_id"):
        PENDING[call.from_user.id] = (act, cid, chat_id, msg_id)
        what = "a new picture, video or GIF" if act == "file_id" else f"the new {FIELD_LABEL[act]}"
        await edit_panel(
            bot, chat_id, msg_id,
            f"<b>Edit {FIELD_LABEL[act]}</b>  ·  ID <code>{cid}</code>\n\n"
            f"Send {what} now.\nTap Back or send /cancel to stop.",
            lambda s: InlineKeyboardMarkup(inline_keyboard=[[_b("Back", f"ew:back:{cid}", "primary", s)]]))
        return await call.answer()

    if act == "rar":
        await edit_panel(bot, chat_id, msg_id, "<b>Pick a rarity</b>", lambda s: pick_kb(cid, "rar", s))
        return await call.answer()

    if act == "sub":
        if not allowed_subs(c["rarity"]):
            return await call.answer(sub_rule_text(c["rarity"]), show_alert=True)
        await edit_panel(bot, chat_id, msg_id, "<b>Pick a sub-rarity</b>", lambda s: pick_kb(cid, "sub", s, c["rarity"]))
        return await call.answer()

    if act in ("r", "s") and rest:
        val = ":".join(rest)
        if act == "r":
            if not find_rarity(val):
                return await call.answer()
            fields, note = {"rarity": val}, "Updated"
            if c["sub_rarity"] and c["sub_rarity"] not in allowed_subs(val):
                fields["sub_rarity"], note = None, "Updated, sub-rarity removed"
            await db.update_character(cid, **fields)
        else:
            sub = find_sub(val)
            if sub is False or (sub and sub not in allowed_subs(c["rarity"])):
                return await call.answer(sub_rule_text(c["rarity"]), show_alert=True)
            await db.update_character(cid, sub_rarity=sub)
            note = "Updated"
        await sync_db_post(bot, await db.get_character(cid))
        await show_panel()
        return await call.answer(note)

    if act == "del":
        await edit_panel(
            bot, chat_id, msg_id,
            f"<b>Delete {escape(c['name'])}?</b>\nID {cid}. This also removes it from every "
            "collection and from the database group. It can't be undone.",
            lambda s: confirm_kb(cid, s))
        return await call.answer()

    if act == "delyes":
        await db.delete_character(cid)
        if c["db_msg_id"]:
            try:
                await bot.delete_message(DB_CHAT, c["db_msg_id"])
            except Exception as e:
                log.warning("db post delete failed for %s: %s", cid, e)
        try:
            await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id,
                                           caption=f"<b>Deleted</b>\n{escape(c['name'])} · ID {cid}",
                                           reply_markup=None)
        except Exception:
            pass
        return await call.answer("Deleted")

    await call.answer()


# ───────────── text / picture input for the panel ─────────────
def _waiting(m: Message) -> bool:
    if not m.from_user or m.from_user.id not in PENDING:
        return False
    return not (m.text and m.text.startswith("/"))


@router.message(Command("cancel"), F.from_user.id.func(lambda i: i in PENDING))
async def cancel_cmd(m: Message):
    _, cid, chat_id, msg_id = PENDING.pop(m.from_user.id)
    c = await db.get_character(cid)
    if c:
        try:
            await edit_panel(m.bot, chat_id, msg_id, panel_caption(c), lambda s: panel_kb(cid, s))
        except Exception as e:
            log.warning("panel restore failed: %s", e)
    await notice(m, "Cancelled", "Nothing was changed.")


@router.message(_waiting)
async def pending_input(m: Message):
    field, cid, chat_id, msg_id = PENDING[m.from_user.id]
    if field == "file_id":
        found = media_of(m)
        if not found:
            return await notice(m, "Picture needed", "Send a photo, video or GIF, or /cancel.")
        kind, value = found
    else:
        if not m.text or not m.text.strip():
            return await notice(m, "Text needed", f"Send the new {FIELD_LABEL[field]}, or /cancel.")
        value = m.text.strip()
    PENDING.pop(m.from_user.id, None)

    c = await db.get_character(cid)
    if not c:
        return await notice(m, "Not found", "That character no longer exists.")
    await db.update_character(cid, **{field: value}, **({"media_type": kind} if field == "file_id" else {}))
    c = await db.get_character(cid)

    bot = m.bot
    if field == "file_id":
        try:
            await bot.edit_message_media(
                input_media(kind, value, panel_caption(c)),
                chat_id=chat_id, message_id=msg_id, reply_markup=panel_kb(cid, False))
        except Exception as e:
            log.warning("panel picture swap failed: %s", e)
        if c["db_msg_id"]:
            try:
                await bot.edit_message_media(
                    input_media(kind, value, db_caption(c), spoiler=True),
                    chat_id=DB_CHAT, message_id=c["db_msg_id"])
            except Exception as e:
                log.warning("db post picture swap failed for %s: %s", cid, e)
    else:
        await sync_db_post(bot, c)
        try:
            await edit_panel(bot, chat_id, msg_id, panel_caption(c), lambda s: panel_kb(cid, s))
        except Exception as e:
            log.warning("panel refresh failed: %s", e)
    try:
        await m.delete()          # keep the chat tidy: remove the admin's input message
    except Exception:
        pass
