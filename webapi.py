"""Otaku Nexus - website and read-only API, served by the bot process itself (same Railway service).

  GET /                    the website (web/index.html)
  GET /api/overview        stats, rarities, shop and game settings (so the site always matches configaration.py)
  GET /api/collection      the owner's cards: [id, name, series, rarity, sub rarity, copies, media p|v|a]
  GET /api/img/<char id>   the card's picture / video, streamed from Telegram (only for cards the owner owns,
                           the bot token never leaves the server)
  GET /logo.png            the logo, fetched once from LOGO_URL and cached
  GET /health              "ok"

Port: the PORT environment variable (Railway sets it), default 8080. Everything is read-only and public data
(no user ids besides the owner, no balances). CORS is open so the page can also be hosted elsewhere.
"""
import hmac
import hashlib
import asyncio
import io
import json
import logging
import math
import os
import secrets
import time
from collections import OrderedDict
from urllib.parse import parse_qsl

from aiohttp import ClientSession, ClientTimeout, web

import dbase as db
from configaration import (
    BOT_TOKEN, CURRENCY_NAME, CURRENCY_SYMBOL, DAILY_AMOUNT, DAILY_BIO_AMOUNT, DROP_MAX_EVERY, DROP_MIN_EVERY, GUESS_DAILY_CAP,
    GUESS_REWARD, GUESS_SECONDS, MINES_DEFAULT, MINES_MAX_BET, MINES_MIN_BET, MINES_MIN_CASHOUT, MINES_RTP,
    MINES_TIMEOUT, OWNER_ID, RARITIES, SHOP_PRICES, SHOP_SLOTS, SHOP_SUNDAY_SLOTS, SHOP_UTC_OFFSET_MIN,
    SLOT_MAX_SPINS, SLOT_REWARD_JACKPOT, SLOT_REWARD_PAIR, SLOT_REWARD_TRIPLE, SLOT_REWARD_TWO_SEVENS, SUB_RARITIES,
)

log = logging.getLogger("otaku.web")

LOGO_URL = "https://i.ibb.co/5gbmM7LJ/file-0000000010188246861284ef1e5f1f15.png"
WEB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
MIME = {"p": "image/jpeg", "v": "video/mp4", "a": "video/mp4"}
KIND = {"photo": "p", "video": "v", "animation": "a"}

_img: "OrderedDict[int, tuple[bytes, str]]" = OrderedDict()      # char id -> (bytes, mime), newest last
_logo: tuple[bytes, str] | None = None


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Init-Data",
    "Access-Control-Max-Age": "86400",
}


@web.middleware
async def cors(request: web.Request, handler):
    # The mini app lives on another origin (Netlify) and sends the X-Init-Data header, so the browser first
    # sends an OPTIONS preflight. Without answering it (and allowing that header) every call is "Failed to fetch".
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=CORS_HEADERS)
    try:
        resp = await handler(request)
    except ApiError as e:
        resp = web.json_response({"error": e.msg}, status=e.status)
    except web.HTTPException as e:
        resp = e
    except Exception:
        log.exception("web handler failed: %s", request.path)
        resp = web.json_response({"error": "Something went wrong."}, status=500)
    resp.headers.update(CORS_HEADERS)
    return resp


def tg_user(request: web.Request) -> dict | None:
    """Validate Telegram WebApp initData (X-Init-Data header) and return the Telegram user dict, else None."""
    raw = request.headers.get("X-Init-Data", "")
    if not raw:
        return None
    try:
        data = dict(parse_qsl(raw, keep_blank_values=True))
        got = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(data.get("auth_date", "0")) > 86400:
            return None
        return json.loads(data["user"])
    except Exception:
        return None


async def index(request: web.Request):
    if not os.path.exists(WEB_FILE):
        return web.Response(text="Website file missing: put index.html in the web folder next to webapi.py.", status=404)
    return web.FileResponse(WEB_FILE, headers={"Cache-Control": "no-cache"})


async def health(request: web.Request):
    return web.Response(text="ok")


async def overview(request: web.Request):
    users, chats, chars = await db.stats()
    return web.json_response({
        "currency": {"name": CURRENCY_NAME, "symbol": CURRENCY_SYMBOL},
        "stats": {"players": users, "groups": chats, "characters": chars},
        "rarities": [[n, e, w] for n, e, w, *_ in RARITIES],
        "subCount": len(SUB_RARITIES),
        "shop": {
            "daily": [[r, SHOP_PRICES.get(r, 0)] for r, _ in SHOP_SLOTS],
            "sunday": [[r, SHOP_PRICES.get(r, 0)] for r, _ in SHOP_SUNDAY_SLOTS],
            "utcOffsetMin": SHOP_UTC_OFFSET_MIN,
        },
        "games": {
            "mines": {"minBet": MINES_MIN_BET, "maxBet": MINES_MAX_BET, "default": MINES_DEFAULT, "rtp": MINES_RTP,
                      "timeout": MINES_TIMEOUT, "minCashout": MINES_MIN_CASHOUT},
            "slot": {"spins": SLOT_MAX_SPINS, "jackpot": SLOT_REWARD_JACKPOT, "triple": SLOT_REWARD_TRIPLE,
                     "twoSevens": SLOT_REWARD_TWO_SEVENS, "pair": SLOT_REWARD_PAIR},
            "whoami": {"seconds": GUESS_SECONDS, "reward": GUESS_REWARD, "dailyCap": GUESS_DAILY_CAP},
            "daily": {"amount": DAILY_AMOUNT, "bio": DAILY_BIO_AMOUNT},
        },
        "drop": {"min": DROP_MIN_EVERY, "max": DROP_MAX_EVERY},
        "owner": {"name": await db.get_user_name(OWNER_ID) or "Owner"},
    })


async def collection(request: web.Request):
    rows = await db.owner_cards(OWNER_ID)
    cards = [[r["char_id"], r["name"], r["anime"], r["rarity"], r["sub_rarity"] or "", r["qty"],
              KIND.get(r["media_type"], "p")] for r in rows]
    return web.json_response({"owner": await db.get_user_name(OWNER_ID) or "Owner", "cards": cards})


async def _need_user(request: web.Request) -> int:
    u = tg_user(request)
    if not u:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Open this from the bot."}), content_type="application/json")
    uid = int(u["id"])
    try:
        await db.upsert_user(uid, u.get("username"), u.get("first_name") or "Player")
    except Exception:
        log.exception("upsert_user failed")
    return uid


async def _body(request: web.Request) -> dict:
    try:
        d = await request.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        self.status, self.msg = status, msg


async def my_collection(request: web.Request):
    uid = await _need_user(request)
    rows = await db.owner_cards(uid)
    cards = [[r["char_id"], r["name"], r["anime"], r["rarity"], r["sub_rarity"] or "", r["qty"],
              KIND.get(r["media_type"], "p")] for r in rows]
    return web.json_response({"cards": cards})


async def me(request: web.Request):
    uid = await _need_user(request)
    return web.json_response({"nex": await db.get_nex(uid)})


async def picture(request: web.Request):
    try:
        cid = int(request.match_info["cid"])
    except ValueError:
        raise web.HTTPNotFound()
    hit = _img.get(cid)
    if not hit:
        # <img>/<video> tags cannot send the X-Init-Data header, so pictures are served for any character
        c = await db.get_character(cid)
        if not c:
            raise web.HTTPNotFound()
        buf = io.BytesIO()
        try:
            await request.app["bot"].download(c["file_id"], destination=buf)
        except Exception as e:
            log.warning("picture %s failed: %s", cid, e)
            raise web.HTTPBadGateway()
        hit = (buf.getvalue(), MIME[KIND.get(c["media_type"], "p")])
        _img[cid] = hit
        while len(_img) > 150:
            _img.popitem(last=False)
    return web.Response(body=hit[0], content_type=hit[1], headers={"Cache-Control": "public, max-age=86400"})


async def logo(request: web.Request):
    global _logo
    if not _logo:
        try:
            async with ClientSession(timeout=ClientTimeout(total=15)) as s, s.get(LOGO_URL) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                _logo = (await r.read(), r.headers.get("Content-Type", "image/png"))
        except Exception as e:
            log.warning("logo fetch failed: %s", e)
            raise web.HTTPNotFound()
    return web.Response(body=_logo[0], content_type=_logo[1].split(";")[0], headers={"Cache-Control": "public, max-age=86400"})


# ───────────── shop ─────────────
def _shop_item(r) -> dict:
    return {"id": r["char_id"], "name": r["name"], "series": r["anime"], "rarity": r["rarity"],
            "sub": r["sub_rarity"] or "", "price": r["price"], "bought": bool(r["bought"]),
            "m": KIND.get(r["media_type"], "p")}


async def shop_list(request: web.Request):
    uid = await _need_user(request)
    import shop as shop_mod                      # lazy: shop.py pulls in the whole bot
    day, sunday, secs = shop_mod.shop_clock()
    await shop_mod.ensure_stock(day, sunday, uid)
    items = await db.shop_items(day, uid)
    return web.json_response({"items": [_shop_item(r) for r in items], "secs": secs, "sunday": sunday,
                              "balance": await db.get_nex(uid)})


async def shop_buy(request: web.Request):
    uid = await _need_user(request)
    try:
        cid = int((await _body(request)).get("id"))
    except (TypeError, ValueError):
        raise ApiError(400, "Bad request.")
    import shop as shop_mod
    day, sunday, secs = shop_mod.shop_clock()
    await shop_mod.ensure_stock(day, sunday, uid)
    item = await db.shop_item(day, uid, cid)
    st, bal = await db.shop_buy(uid, day, cid)
    price = item["price"] if item else 0
    msg = {"ok": "Purchased.", "gone": "This card is no longer in your shop.",
           "limit": "You already bought this one today.",
           "poor": f"Not enough {CURRENCY_NAME}. You have {bal:,}, you need {price:,}."}.get(st, "Could not buy this card.")
    return web.json_response({"status": st, "balance": bal, "message": msg})


# ───────────── mines (the web version of /mines; the hidden board never leaves the server) ─────────────
_mlocks: dict[int, asyncio.Lock] = {}


def _mkey(uid: int) -> str:
    return f"webmines:{uid}"


def mines_mult(n: int, k: int) -> float:
    """Same formula as minigame.mines_mult: multiplier after k safe tiles with n mines (1.0 before the first tile)."""
    if k <= 0:
        return 1.0
    p = math.comb(25 - n, k) / math.comb(25, k)
    return math.floor(MINES_RTP / p * 100) / 100


def _busy_map() -> dict:
    """minigame's one-game-per-user map, shared so /mines in a chat and the web game exclude each other."""
    try:
        import minigame
        return minigame._mines_by_user
    except Exception:
        return _local_busy


_local_busy: dict[int, str] = {}


def _mgame(g: dict) -> dict:
    k, n = len(g["picked"]), g["mines"]
    mult = mines_mult(n, k)
    return {"bet": g["bet"], "mines": n, "picked": g["picked"], "mult": mult, "value": int(g["bet"] * mult),
            "next": mines_mult(n, k + 1) if k + 1 <= 25 - n else 0, "minCashout": MINES_MIN_CASHOUT}


async def _mload(uid: int) -> dict | None:
    raw = await db.get_setting(_mkey(uid))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def _msave(uid: int, g: dict | None) -> None:
    await db.set_setting(_mkey(uid), json.dumps(g) if g else "")


async def _mend(uid: int, g: dict, result: str, hit: int | None = None) -> dict:
    """Finish a game: pay out (cash / clear / timeout), reveal the mines, forget the game."""
    k = len(g["picked"])
    mult = mines_mult(g["mines"], k)
    if result == "boom":
        payout = 0
    elif result == "timeout" and k < MINES_MIN_CASHOUT:
        payout = g["bet"]                       # idle before cash out unlocked: bet returned (same as /mines)
    else:
        payout = int(g["bet"] * mult)
    async with db.nex_lock:
        await _msave(uid, None)
        _busy_map().pop(uid, None)
        bal = await db.add_nex(uid, payout) if payout else await db.get_nex(uid)
    return {"over": True, "result": result, "bet": g["bet"], "payout": payout, "mult": mult,
            "picked": g["picked"], "mines": g["board"], "hit": hit, "balance": bal}


async def _mcurrent(uid: int) -> tuple[dict | None, dict | None]:
    """(running game, result if it just timed out)."""
    g = await _mload(uid)
    if g and time.time() - g["t"] > MINES_TIMEOUT:
        return None, await _mend(uid, g, "timeout")
    return g, None


async def mines_state(request: web.Request):
    uid = await _need_user(request)
    async with _mlocks.setdefault(uid, asyncio.Lock()):
        g, ended = await _mcurrent(uid)
        out = {"balance": await db.get_nex(uid)}
        busy = _busy_map().get(uid)
        if ended:
            out["ended"] = ended
        elif g:
            _busy_map()[uid] = busy or "web"
            out["game"] = _mgame(g)
        elif busy:
            out["busyInChat"] = True
        return web.json_response(out)


async def mines_start(request: web.Request):
    uid = await _need_user(request)
    d = await _body(request)
    try:
        bet, n = int(d.get("bet")), int(d.get("mines"))
    except (TypeError, ValueError):
        raise ApiError(400, "Bad request.")
    if not MINES_MIN_BET <= bet <= MINES_MAX_BET:
        raise ApiError(400, f"Bet must be {MINES_MIN_BET:,} to {MINES_MAX_BET:,} Nex.")
    if not 1 <= n <= 24:
        raise ApiError(400, "Mines must be 1 to 24.")
    async with _mlocks.setdefault(uid, asyncio.Lock()):
        g, _ = await _mcurrent(uid)
        if g:
            raise ApiError(409, "You already have a game running.")
        async with db.nex_lock:
            if uid in _busy_map():
                raise ApiError(409, "Finish your /mines game in the chat first.")
            if await db.get_nex(uid) < bet:
                raise ApiError(400, "Not enough Nex.")
            bal = await db.add_nex(uid, -bet)
            _busy_map()[uid] = "web"
            board = secrets.SystemRandom().sample(range(25), n)
            g = {"bet": bet, "mines": n, "board": board, "picked": [], "t": time.time()}
            await _msave(uid, g)
        return web.json_response({"game": _mgame(g), "balance": bal})


async def mines_open(request: web.Request):
    uid = await _need_user(request)
    try:
        i = int((await _body(request)).get("i"))
    except (TypeError, ValueError):
        raise ApiError(400, "Bad request.")
    async with _mlocks.setdefault(uid, asyncio.Lock()):
        g, ended = await _mcurrent(uid)
        if ended:
            return web.json_response(ended)
        if not g:
            raise ApiError(404, "No game running.")
        if not 0 <= i < 25 or i in g["picked"]:
            raise ApiError(400, "Pick a closed tile.")
        if i in g["board"]:
            return web.json_response(await _mend(uid, g, "boom", hit=i))
        g["picked"].append(i)
        g["t"] = time.time()
        if len(g["picked"]) >= 25 - g["mines"]:
            return web.json_response(await _mend(uid, g, "clear"))
        await _msave(uid, g)
        return web.json_response({"game": _mgame(g), "balance": await db.get_nex(uid)})


async def mines_cash(request: web.Request):
    uid = await _need_user(request)
    async with _mlocks.setdefault(uid, asyncio.Lock()):
        g, ended = await _mcurrent(uid)
        if ended:
            return web.json_response(ended)
        if not g:
            raise ApiError(404, "No game running.")
        if len(g["picked"]) < MINES_MIN_CASHOUT:
            raise ApiError(400, f"Open {MINES_MIN_CASHOUT - len(g['picked'])} more tiles first.")
        return web.json_response(await _mend(uid, g, "cash"))


# ───────────── reward ads (Adsgram block int-61157): watch 2 ads, get a random card ─────────────
AD_NEED = 2                     # ads per reward (the page plays them back to back)
AD_DAILY_CAP = 5                # rewards per user per shop day
ADSGRAM_SECRET = "3eac2290823c10126530d87a93fbd26c"   # the same value goes in the Adsgram reward URL as &secret=...
AD_TOKEN_TTL = 900
AD_REWARD = (("Common", 40), ("Uncommon", 25), ("Rare", 15))   # relative weights, shown to the user as percentages
_adlocks: dict[int, asyncio.Lock] = {}


def _ad_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + SHOP_UTC_OFFSET_MIN * 60))


def _ad_chances() -> list:
    tot = sum(w for _, w in AD_REWARD)
    return [[r, round(w * 100 / tot)] for r, w in AD_REWARD]


async def _adload(uid: int) -> dict:
    raw = await db.get_setting(f"adrw:{uid}")
    try:
        s = json.loads(raw) if raw else {}
    except ValueError:
        s = {}
    if s.get("day") != _ad_day():
        s = {"day": _ad_day(), "n": 0}
    return s


async def _adsave(uid: int, s: dict) -> None:
    await db.set_setting(f"adrw:{uid}", json.dumps(s))


AD_BLOCKED_SUBS: tuple = ()     # sub rarities (e.g. event cards) that must never be given as a free reward


async def _ad_pick_card(rarity: str):
    """A random card of the rolled rarity; if that rarity has none, try the other reward rarities."""
    order = [rarity] + [r for r, _ in AD_REWARD if r != rarity]
    for r in order:
        c = await db.pick_character(r, blocked_subs=AD_BLOCKED_SUBS)
        if c:
            return c
    return None


async def _ad_give(uid: int, char_id: int) -> None:
    await db.add_to_collection(uid, char_id)


async def adreward_state(request: web.Request):
    uid = await _need_user(request)
    s = await _adload(uid)
    return web.json_response({"left": max(0, AD_DAILY_CAP - s["n"]), "cap": AD_DAILY_CAP, "need": AD_NEED,
                              "chances": _ad_chances()})


async def adreward_start(request: web.Request):
    uid = await _need_user(request)
    async with _adlocks.setdefault(uid, asyncio.Lock()):
        s = await _adload(uid)
        if s["n"] >= AD_DAILY_CAP:
            raise ApiError(429, "No ad rewards left today. Come back tomorrow.")
        s["tok"], s["t"], s["seen"] = secrets.token_urlsafe(12), time.time(), 0
        await _adsave(uid, s)
        return web.json_response({"token": s["tok"]})


async def adsgram_reward(request: web.Request):
    """Adsgram server callback, one call per finished ad:  /adsgram/reward?userid=[userId]&secret=...
    It only counts the ad for a reward session the user started in the app; the card is given by /api/adreward/claim."""
    if ADSGRAM_SECRET and not hmac.compare_digest(request.query.get("secret", ""), ADSGRAM_SECRET):
        raise web.HTTPForbidden(text="forbidden")
    try:
        uid = int(request.query.get("userid", ""))
    except ValueError:
        raise web.HTTPBadRequest(text="bad userid")
    async with _adlocks.setdefault(uid, asyncio.Lock()):
        s = await _adload(uid)
        if s.get("tok"):
            s["seen"] = s.get("seen", 0) + 1
            await _adsave(uid, s)
    return web.Response(text="ok")                 # always 200 so Adsgram does not retry


async def adreward_claim(request: web.Request):
    uid = await _need_user(request)
    tok = str((await _body(request)).get("token") or "")
    async with _adlocks.setdefault(uid, asyncio.Lock()):
        s = await _adload(uid)
        want = s.get("tok")
        if not want or not hmac.compare_digest(want, tok):
            raise ApiError(400, "Start again: press Watch 2 ads.")
        if time.time() - s.get("t", 0) > AD_TOKEN_TTL:
            s.pop("tok", None); await _adsave(uid, s)
            raise ApiError(400, "That took too long. Start again.")
        if s.get("seen", 0) < AD_NEED:             # Adsgram's callbacks can arrive a moment after the ad closes
            return web.json_response({"pending": True, "seen": s.get("seen", 0)})
        if s["n"] >= AD_DAILY_CAP:
            raise ApiError(429, "No ad rewards left today. Come back tomorrow.")
        rarity = secrets.SystemRandom().choices([r for r, _ in AD_REWARD], [w for _, w in AD_REWARD])[0]
        c = await _ad_pick_card(rarity)
        if not c:
            raise ApiError(503, "No card is available right now. Try again later.")
        for k in ("tok", "t", "seen"):             # a session pays once
            s.pop(k, None)
        await _adsave(uid, s)
        await _ad_give(uid, c["char_id"])
        s["n"] += 1
        await _adsave(uid, s)
        return web.json_response({"card": {"id": c["char_id"], "name": c["name"], "series": c["anime"], "rarity": c["rarity"],
                                           "sub": c["sub_rarity"] or "", "m": KIND.get(c["media_type"], "p")},
                                  "left": AD_DAILY_CAP - s["n"]})

async def start(bot) -> web.AppRunner | None:
    """Start the web server inside the bot process. Never raises: the bot must run even if the web part fails."""
    try:
        app = web.Application(middlewares=[cors])
        app["bot"] = bot
        app.add_routes([
            web.get("/", index), web.get("/index.html", index), web.get("/health", health),
            web.get("/logo.png", logo), web.get("/api/overview", overview), web.get("/api/collection", collection),
            web.get("/api/mycollection", my_collection), web.get("/api/me", me),
            web.get("/api/shop", shop_list), web.post("/api/shop/buy", shop_buy),
            web.get("/api/adreward", adreward_state), web.post("/api/adreward/start", adreward_start),
            web.post("/api/adreward/claim", adreward_claim), web.get("/adsgram/reward", adsgram_reward),
            web.get("/api/mines/state", mines_state), web.post("/api/mines/start", mines_start),
            web.post("/api/mines/open", mines_open), web.post("/api/mines/cash", mines_cash),
            web.get("/api/img/{cid}", picture),
        ])
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", "8080"))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        log.info("website listening on port %d", port)
        return runner
    except Exception:
        log.exception("website failed to start (the bot keeps running)")
        return None
