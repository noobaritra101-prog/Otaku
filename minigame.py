"""Mini games: /mines (5x5 board, one rich message edited in place), /slot (free 🎰 spin) and /whoami
(guess the spoiler character).

Self-contained module: otaku.py only does `import minigame` and `dp.include_router(minigame.router)`.
Shares the Nex lock (db.nex_lock) with /ngive so balances can't race.
"""
import asyncio
import inspect
import logging
import math
import secrets
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters,
)

import dbase as db
import richmsg
from richmsg import esc
from configaration import (
    RICH_MESSAGES, CURRENCY_NAME, CURRENCY_SYMBOL,
    MINES_MIN_BET, MINES_MAX_BET, MINES_DEFAULT, MINES_RTP, MINES_TIMEOUT, MINES_MIN_CASHOUT,
    MINES_ICON_HIDDEN, MINES_ICON_TILE, MINES_ICON_BOMB, MINES_ICON_BLAST,
    SLOT_MAX_SPINS, SLOT_WINDOW, SLOT_LIMIT_COOLDOWN, SLOT_REWARD_JACKPOT, SLOT_REWARD_TRIPLE, SLOT_REWARD_TWO_SEVENS,
    SLOT_REWARD_PAIR, SLOT_REWARD_NONE,
    GUESS_DAILY_CAP, GUESS_REWARD, GUESS_SECONDS,
)

router = Router()
log = logging.getLogger("otaku.minigame")


# ═════════════ shared helpers ═════════════
_RICH_CAN_REPLY = "reply_to_message_id" in inspect.signature(richmsg.send_rich).parameters


def topic_id(m: Message) -> int | None:
    return m.message_thread_id if m.is_topic_message else None


async def rich_reply_to(bot, chat_id: int, msg_id: int, thread: int | None, md: str, html: str) -> None:
    """Rich message replying to a given message id, HTML fallback."""
    if RICH_MESSAGES:
        try:
            kw = {"reply_to_message_id": msg_id} if _RICH_CAN_REPLY else {}
            await richmsg.send_rich(bot, chat_id, md, message_thread_id=thread, **kw)
            return
        except Exception as e:
            log.warning("rich reply failed in chat %s, using HTML fallback: %s", chat_id, e)
    await bot.send_message(
        chat_id, html, message_thread_id=thread,
        reply_parameters=ReplyParameters(message_id=msg_id, allow_sending_without_reply=True))


async def notice(m: Message, title: str, text: str) -> None:
    """Short rich notice (heading + quote) replying to the command."""
    await rich_reply_to(
        m.bot, m.chat.id, m.message_id, topic_id(m),
        f"### {esc(title)}\n\n> {esc(text)}",
        f"<b>{escape(title)}</b>\n<blockquote>{escape(text)}</blockquote>")


def fmt_wait(sec: int) -> str:
    """Seconds -> '2h 5m' / '12m'."""
    h, rem = divmod(int(sec), 3600)
    mi = rem // 60
    return f"{h}h {mi}m" if h else f"{max(mi, 1)}m"


# ═════════════ /mines ═════════════
MINES_CELLS = 25            # 5 x 5 board
MINES_COLS = 5
_styled = True              # coloured buttons; switched off if Telegram rejects them


@dataclass
class MinesGame:
    gid: str
    user_id: int
    name: str
    chat_id: int
    thread: int | None
    bet: int
    mines: set[int]
    msg_id: int = 0
    rich: bool = True                      # board is a rich message (else HTML fallback)
    hit: int | None = None                 # the mine that was clicked
    picked: set[int] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last: float = field(default_factory=time.time)
    done: bool = False
    task: asyncio.Task | None = None


_mines_games: dict[str, MinesGame] = {}     # game id -> game
_mines_by_user: dict[int, str] = {}         # one game per user
_rng = secrets.SystemRandom()


def mines_mult(mines: int, picks: int) -> float:
    """Fair multiplier for `picks` safe tiles, scaled by MINES_RTP, floored to 2 decimals."""
    if picks <= 0:
        return 1.0
    p = math.comb(MINES_CELLS - mines, picks) / math.comb(MINES_CELLS, picks)
    return math.floor(MINES_RTP / p * 100) / 100


def _btn(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    # `style` (primary / success / danger) = coloured buttons, Bot API 10.3
    extra = {"style": style} if (style and _styled) else {}
    return InlineKeyboardButton(text=text, callback_data=data, **extra)


def mines_kb(g: MinesGame, over: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for r in range(MINES_CELLS // MINES_COLS):
        row = []
        for c in range(MINES_COLS):
            i = r * MINES_COLS + c
            if over and i == g.hit:
                b = _btn(MINES_ICON_BLAST, f"mn:{g.gid}:{i}", "danger")
            elif over and i in g.mines:
                b = _btn(MINES_ICON_BOMB, f"mn:{g.gid}:{i}", "danger")
            elif i in g.picked:
                b = _btn(MINES_ICON_TILE, f"mn:{g.gid}:{i}", "success")     # opened safe tile
            else:
                b = _btn(MINES_ICON_HIDDEN, f"mn:{g.gid}:{i}")              # unopened: blank, no colour
            row.append(b)
        rows.append(row)
    if not over:
        k = len(g.picked)
        if k >= MINES_MIN_CASHOUT:
            payout = int(g.bet * mines_mult(len(g.mines), k))
            rows.append([_btn(f"Cash out {payout} {CURRENCY_SYMBOL}", f"mn:{g.gid}:c", "success")])
        else:
            rows.append([_btn(f"Cash out ({k}/{MINES_MIN_CASHOUT})", f"mn:{g.gid}:c", "danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def board_md(g: MinesGame, final: tuple[str, str, int] | None = None) -> str:
    """Rich Markdown board. final = (title, line, balance) once the game is over."""
    n, k = len(g.mines), len(g.picked)
    sym = CURRENCY_SYMBOL
    info = f"Bet **{g.bet}** {sym} · Mines **{n}**"
    if final:
        title, line, balance = final
        return (f"## {esc(title)}\n\n{esc(line)}\n\n{info}\n\n"
                f"<aside>Balance: {balance} {sym}<cite>{escape(g.name)} · Mines</cite></aside>")
    mult = f"{mines_mult(n, k):.2f}x"
    if k < MINES_CELLS - n:
        side = f"Multiplier: {mult} · Next tile: {mines_mult(n, k + 1):.2f}x"
    else:
        side = f"Multiplier: {mult}"
    hint = ""
    if k < MINES_MIN_CASHOUT:
        left = MINES_MIN_CASHOUT - k
        hint = f"\n\n###### Open {left} more tile{'s' if left != 1 else ''} to unlock cash out"
    return f"## Mines\n\n{info}\n\n<aside>{side}<cite>{escape(g.name)}</cite></aside>{hint}"


def board_html(g: MinesGame, final: tuple[str, str, int] | None = None) -> str:
    """HTML fallback board."""
    n, k = len(g.mines), len(g.picked)
    sym = CURRENCY_SYMBOL
    lines = [f"Bet: <b>{g.bet}</b> {sym} · Mines: <b>{n}</b>"]
    if final:
        title, line, balance = final
        lines = [escape(line)] + lines + [f"Balance: <b>{balance}</b> {sym}"]
        head = f"<b>{escape(title)}</b> · {escape(g.name)}"
    else:
        lines.append(f"Multiplier: <b>{mines_mult(n, k):.2f}x</b>")
        if k < MINES_CELLS - n:
            lines.append(f"Next tile: <b>{mines_mult(n, k + 1):.2f}x</b>")
        if k < MINES_MIN_CASHOUT:
            lines.append(f"Open {MINES_MIN_CASHOUT - k} more to unlock cash out")
        head = f"<b>Mines</b> · {escape(g.name)}"
    return head + "\n<blockquote>" + "\n".join(lines) + "</blockquote>"


async def _try(fn):
    """Run a send/edit; if Telegram rejects coloured buttons, drop colours and retry once."""
    global _styled
    try:
        return await fn()
    except TelegramBadRequest as e:
        if _styled and "button" in str(e).lower():
            log.warning("coloured buttons rejected, switching them off: %s", e)
            _styled = False
            return await fn()
        raise


async def _send_board(m: Message, g: MinesGame) -> Message:
    if RICH_MESSAGES:
        kw = {"reply_to_message_id": m.message_id} if _RICH_CAN_REPLY else {}
        try:
            sent = await _try(lambda: richmsg.send_rich(
                m.bot, m.chat.id, board_md(g), reply_markup=mines_kb(g),
                message_thread_id=g.thread, **kw))
            g.rich = True
            return sent
        except Exception as e:
            log.warning("rich mines board failed, using HTML board: %s", e)
    g.rich = False
    return await _try(lambda: m.reply(board_html(g), reply_markup=mines_kb(g)))


async def _mines_edit(bot, g: MinesGame, final: tuple[str, str, int] | None = None) -> None:
    """Edit the board message in place (no new messages)."""
    over = final is not None

    async def go():
        kb = mines_kb(g, over)
        if g.rich:
            return await richmsg.edit_rich(bot, g.chat_id, g.msg_id, board_md(g, final), reply_markup=kb)
        return await bot.edit_message_text(
            board_html(g, final), chat_id=g.chat_id, message_id=g.msg_id, reply_markup=kb)

    try:
        await _try(go)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("mines board edit failed: %s", e)
    except Exception:
        log.exception("mines board edit failed")


async def _mines_finish(bot, g: MinesGame, kind: str, cleared: bool = False,
                        timeout: bool = False) -> str | None:
    """End a game (caller holds g.lock) by editing the board. kind: 'cash' or 'boom'.
    Returns an error text or None."""
    g.done = True
    n, k = len(g.mines), len(g.picked)
    mult = mines_mult(n, k)
    sym = CURRENCY_SYMBOL
    if kind == "cash":
        refund = timeout and k < MINES_MIN_CASHOUT and not cleared
        payout = g.bet if refund else int(g.bet * mult)
        try:
            async with db.nex_lock:
                balance = await db.add_nex(g.user_id, payout)
        except Exception:
            log.exception("mines payout failed for %s", g.user_id)
            g.done = False
            return "Payout failed, try again."
        if refund:
            title, line = "Timed out", f"{g.bet} {sym} returned, cash out needs {MINES_MIN_CASHOUT} tiles"
        else:
            title = "Board cleared" if cleared else ("Timed out, cashed out" if timeout else "Cashed out")
            line = f"{payout} {sym} at {mult:.2f}x"
    else:
        balance = await db.get_nex(g.user_id)
        title, line = "Boom", f"You hit a mine and lost {g.bet} {sym}"

    _mines_games.pop(g.gid, None)
    _mines_by_user.pop(g.user_id, None)
    await _mines_edit(bot, g, final=(title, line, balance))
    return None


async def _mines_watch(bot, g: MinesGame) -> None:
    """Auto cash out (or return the bet) when a game sits idle."""
    while True:
        await asyncio.sleep(30)
        if g.done:
            return
        if time.time() - g.last >= MINES_TIMEOUT:
            async with g.lock:
                if not g.done:
                    await _mines_finish(bot, g, "cash", timeout=True)
            if g.done:
                return


@router.message(Command("mines"))
async def mines_cmd(m: Message, command: CommandObject):
    u = m.from_user
    if not u:
        return
    usage = (f"/mines bet [mines] . Example: /mines 100 3. Bet {MINES_MIN_BET} to {MINES_MAX_BET}, "
             f"mines 1 to {MINES_CELLS - 1} (default {MINES_DEFAULT}).")
    args = (command.args or "").split()
    if not args or len(args) > 2 or not all(a.isascii() and a.isdigit() for a in args):
        return await notice(m, "Mines", usage)
    bet = int(args[0])
    mines = int(args[1]) if len(args) == 2 else MINES_DEFAULT
    if not MINES_MIN_BET <= bet <= MINES_MAX_BET:
        return await notice(m, "Invalid bet", f"Bet between {MINES_MIN_BET} and {MINES_MAX_BET} {CURRENCY_SYMBOL}.")
    if not 1 <= mines <= MINES_CELLS - 1:
        return await notice(m, "Invalid mines", f"Choose 1 to {MINES_CELLS - 1} mines.")

    await db.upsert_user(u.id, u.username, u.first_name)
    gid = secrets.token_hex(4)
    async with db.nex_lock:
        busy = u.id in _mines_by_user
        balance = await db.get_nex(u.id)
        ok = not busy and balance >= bet
        if ok:
            await db.add_nex(u.id, -bet)
            _mines_by_user[u.id] = gid
    if busy:
        return await notice(m, "Game in progress", "Finish your current Mines game first.")
    if not ok:
        return await notice(m, f"Not enough {CURRENCY_NAME}", f"You have {balance} {CURRENCY_SYMBOL}.")

    g = MinesGame(gid=gid, user_id=u.id, name=u.first_name, chat_id=m.chat.id, thread=topic_id(m),
                  bet=bet, mines=set(_rng.sample(range(MINES_CELLS), mines)))
    _mines_games[gid] = g
    try:
        sent = await _send_board(m, g)
    except Exception:
        log.exception("mines board send failed, refunding")
        _mines_games.pop(gid, None)
        _mines_by_user.pop(u.id, None)
        await db.add_nex(u.id, bet)
        return
    g.msg_id = sent.message_id
    g.task = asyncio.create_task(_mines_watch(m.bot, g))


@router.callback_query(F.data.startswith("mn:"))
async def mines_cb(call: CallbackQuery):
    parts = (call.data or "").split(":")
    if len(parts) != 3:
        return await call.answer()
    _, gid, act = parts
    g = _mines_games.get(gid)
    if not g or g.done:
        return await call.answer("This game has ended.")
    if call.from_user.id != g.user_id:
        return await call.answer("This is not your game.", show_alert=True)

    async with g.lock:
        if g.done:
            return await call.answer("This game has ended.")
        g.last = time.time()
        if act == "c":
            k = len(g.picked)
            if k < MINES_MIN_CASHOUT:
                left = MINES_MIN_CASHOUT - k
                return await call.answer(f"Open {left} more tile{'s' if left != 1 else ''} to cash out.")
            err = await _mines_finish(call.bot, g, "cash")
            return await call.answer(err, show_alert=bool(err))
        if not act.isdigit() or int(act) >= MINES_CELLS:
            return await call.answer()
        i = int(act)
        if i in g.picked:
            return await call.answer()
        if i in g.mines:
            g.hit = i
            await _mines_finish(call.bot, g, "boom")
            return await call.answer()
        g.picked.add(i)
        if len(g.picked) == MINES_CELLS - len(g.mines):
            err = await _mines_finish(call.bot, g, "cash", cleared=True)
            return await call.answer(err, show_alert=bool(err))
        await _mines_edit(call.bot, g)
        await call.answer()


# ═════════════ /slot ═════════════
# Free spin, no bet. Dice value 1..64 = three reels, 2 bits each: 0 BAR, 1 grapes, 2 lemon, 3 seven.
# The result is decoded the moment Telegram returns the dice, so the reward and result message
# go out immediately without waiting for the spin animation.
SLOT_SYMBOLS = ["BAR", "🍇", "🍋", "7️⃣"]
SLOT_SEVEN = 3
# user id -> [window start, spins used in window, blocked until]  (in memory, resets on restart)
_slot_state: dict[int, list[float]] = {}


def slot_decode(value: int) -> list[int]:
    v = value - 1
    return [(v >> (2 * i)) & 3 for i in range(3)]


def slot_outcome(reels: list[int]) -> tuple[str, int]:
    """(title, reward) for three reels."""
    sevens = reels.count(SLOT_SEVEN)
    if sevens == 3:
        return "Jackpot", SLOT_REWARD_JACKPOT
    if len(set(reels)) == 1:
        return "Triple match", SLOT_REWARD_TRIPLE
    if sevens == 2:
        return "Two sevens", SLOT_REWARD_TWO_SEVENS
    if len(set(reels)) == 2:
        return "Pair", SLOT_REWARD_PAIR
    return "No match", SLOT_REWARD_NONE


@router.message(Command("slot"))
async def slot_cmd(m: Message):
    u = m.from_user
    if not u:
        return
    now = time.time()
    prev = _slot_state.get(u.id)
    st = list(prev) if prev else [now, 0, 0.0]          # [window start, spins used, blocked until]
    if st[2] > now:                                      # all spins used, 12h cooldown running
        return await notice(m, "Slot on cooldown", f"Spin again in {fmt_wait(st[2] - now)}.")
    if st[2] or now - st[0] >= SLOT_WINDOW:              # cooldown over or window expired: fresh spins
        st = [now, 0, 0.0]
    st[1] += 1
    if st[1] >= SLOT_MAX_SPINS:                          # last spin of the window: start the cooldown
        st[2] = now + SLOT_LIMIT_COOLDOWN
    _slot_state[u.id] = st                               # set before the spin so spamming can't stack spins

    def undo():                                          # give the spin back if it could not be completed
        if prev:
            _slot_state[u.id] = prev
        else:
            _slot_state.pop(u.id, None)

    await db.upsert_user(u.id, u.username, u.first_name)
    try:
        dice = await m.reply_dice(emoji="🎰")
        reels = slot_decode(dice.dice.value)
    except Exception:
        log.exception("slot dice failed")
        undo()
        return await notice(m, "Something went wrong", "Try again in a moment.")

    title, reward = slot_outcome(reels)
    sym = CURRENCY_SYMBOL
    try:
        async with db.nex_lock:
            if reward:
                await db.add_nex(u.id, reward)
    except Exception:
        log.exception("slot reward failed for %s", u.id)
        undo()
        return await notice(m, "Reward failed", "Your spin was not paid, try again.")

    line = " · ".join(SLOT_SYMBOLS[r] for r in reels)
    got = f"Reward **{reward}** {sym}" if reward else "No reward this time"
    got_h = f"Reward <b>{reward}</b> {sym}" if reward else "No reward this time"
    md = f"## {esc(title)}\n\n{line}\n\n{got}"
    html = f"<b>{escape(title)}</b>\n{line}\n{got_h}"
    await rich_reply_to(m.bot, m.chat.id, dice.message_id, topic_id(m), md, html)


# ═════════════ /whoami ═════════════
# The bot sends a random character picture WITH A SPOILER (a normal picture message, so the spoiler is certain).
# Whoever types the character's name first in the chat within GUESS_SECONDS wins GUESS_REWARD Nex. Wrong guesses
# are ignored silently (they pass on to other handlers). One game per chat at a time, no cooldown between games.
# The picture message is NOT edited afterwards: the result is a separate rich reply (heading, quote, time, dividers).
# Daily cap: GUESS_DAILY_CAP paid wins per user per UTC day, kept in the database (whoami_wins); wins over the cap
# pay nothing and are not counted. Stats for /topwho (harem.py): whoami_wins (correct guesses), whoami_stats
# (finished games for the starter and the winner, fastest correct guess).
# Guesses are plain messages, so BotFather privacy mode must be disabled (same as drops).
# drop.py and waifu.py import this module, so their helpers are imported inside the functions (no import cycle).
GROUP_CHATS = {ChatType.GROUP, ChatType.SUPERGROUP}
LINE = "──────────"


@dataclass
class GuessGame:
    chat_id: int
    thread: int | None
    msg_id: int
    char: object
    starter_id: int
    started: float = field(default_factory=time.time)
    done: bool = False
    task: asyncio.Task | None = None


_guess_games: dict[int, GuessGame] = {}     # chat id -> running game
_guess_busy: set[int] = set()               # chats where a game is being started right now
_guess_last: dict[int, int] = {}            # chat id -> last card used (no immediate repeat)


async def _guess_timeout(bot, g: GuessGame) -> None:
    from drop import name_tag
    from waifu import rarity_label
    await asyncio.sleep(GUESS_SECONDS)
    if g.done:
        return
    g.done = True
    _guess_games.pop(g.chat_id, None)
    c = g.char
    n, tail = name_tag(c), f"{c['anime']} - {rarity_label(c['rarity'])}"
    md = (f"## Time's up\n\n---\n\n> Nobody guessed it.\n>\n> It was **{esc(n)}**\n>\n> {esc(tail)}\n\n"
          f"**Time Limit :** {GUESS_SECONDS}s\n\n---\n\n###### Better luck next time")
    html = (f"<b>Time's up</b>\n{LINE}\n<blockquote>Nobody guessed it.\nIt was <b>{escape(n)}</b>\n"
            f"{escape(tail)}</blockquote>\n<b>Time Limit :</b> {GUESS_SECONDS}s\n{LINE}\nBetter luck next time")
    await rich_reply_to(bot, g.chat_id, g.msg_id, g.thread, md, html)


@router.message(Command("whoami"), F.chat.type.in_(GROUP_CHATS))
async def whoami_cmd(m: Message):
    from drop import pick_drop
    from waifu import send_media
    if not m.from_user or m.from_user.is_bot:
        return
    cid = m.chat.id
    if cid in _guess_games or cid in _guess_busy:
        return await notice(m, "Who am I", "A game is already running here.")

    _guess_busy.add(cid)
    try:
        c = await pick_drop(_guess_last.get(cid))
        if not c:
            return await notice(m, "Who am I", "No characters available yet.")
        caption = (
            f"<b>Guess the character !</b>\n{LINE}\n"
            f"<blockquote>Type the name in the chat.\n"
            f"Time : {GUESS_SECONDS}s\n"
            f"Reward : {GUESS_REWARD} {CURRENCY_SYMBOL}</blockquote>")
        try:
            msg = await send_media(m, c["media_type"], c["file_id"], caption, spoiler=True)
        except TelegramAPIError as e:
            log.warning("whoami send failed in %s (card %s): %s", cid, c["char_id"], e)
            return await notice(m, "Something went wrong", "Try again in a moment.")
        g = GuessGame(chat_id=cid, thread=topic_id(m), msg_id=msg.message_id, char=c, starter_id=m.from_user.id)
        _guess_games[cid] = g
        _guess_last[cid] = c["char_id"]
        g.task = asyncio.create_task(_guess_timeout(m.bot, g))
    finally:
        _guess_busy.discard(cid)


async def _is_guess(m: Message) -> bool:
    """True only for a correct name while a game runs in this chat, so everything else passes through."""
    from drop import name_matches
    g = _guess_games.get(m.chat.id)
    if not g or g.done or not m.from_user or m.from_user.is_bot:
        return False
    if not m.text or m.text.startswith("/") or len(m.text) > 80:
        return False
    return name_matches(m.text, g.char["name"])


@router.message(F.chat.type.in_(GROUP_CHATS), _is_guess)
async def whoami_win(m: Message):
    from drop import name_tag
    from waifu import mention, rarity_label
    g = _guess_games.get(m.chat.id)
    if not g or g.done:
        return
    g.done = True                                   # first correct guess wins (no await before this)
    _guess_games.pop(m.chat.id, None)
    if g.task:
        g.task.cancel()
    u, c = m.from_user, g.char
    secs = max(0.1, round(time.time() - g.started, 2))
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sym = CURRENCY_SYMBOL
    paid, today = False, GUESS_DAILY_CAP
    try:
        await db.upsert_user(u.id, u.username, u.first_name)
        paid, today = await db.whoami_win_add(u.id, day, GUESS_DAILY_CAP)
        if paid:
            async with db.nex_lock:
                await db.add_nex(u.id, GUESS_REWARD)
            await db.whoami_best(u.id, secs)
        for pid in {u.id, g.starter_id}:            # a finished game counts for the starter and the winner
            await db.whoami_game_add(pid)
    except Exception:
        log.exception("whoami stats / reward failed for %s", u.id)
        paid = False

    n, tail = name_tag(c), f"{c['anime']} - {rarity_label(c['rarity'])}"
    who_md = f"[{esc(u.first_name)}](tg://user?id={u.id})"
    if paid:
        rew_md, rew_h = f"{GUESS_REWARD} {sym}", f"{GUESS_REWARD} {sym}"
        foot = f"Today {today}/{GUESS_DAILY_CAP} wins"
    else:
        rew_md = rew_h = "None, daily limit reached"
        foot = f"Daily limit of {GUESS_DAILY_CAP} wins reached, come back tomorrow"
    md = (f"## Correct, {who_md}!\n\n### You guessed the character\n\n---\n\n"
          f"> **{esc(n)}**\n>\n> {esc(tail)}\n\n"
          f"**Time Taken :** {secs:.2f}s\n\n**Reward :** {esc(rew_md)}\n\n---\n\n###### {esc(foot)}")
    html = (f"<b>Correct, {mention(u.id, u.first_name)}!</b>\nYou guessed the character\n{LINE}\n"
            f"<blockquote><b>{escape(n)}</b>\n{escape(tail)}</blockquote>\n"
            f"<b>Time Taken :</b> {secs:.2f}s\n<b>Reward :</b> {escape(rew_h)}\n{LINE}\n{escape(foot)}")
    await rich_reply_to(m.bot, m.chat.id, m.message_id, g.thread, md, html)
