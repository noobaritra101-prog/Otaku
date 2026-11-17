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
import io
import json
import logging
import os
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
    except web.HTTPException as e:
        resp = e
    resp.headers.update(CORS_HEADERS)
    return resp


def tg_user_id(request: web.Request) -> int | None:
    """Validate Telegram WebApp initData (X-Init-Data header) and return the user's id, else None."""
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
        return int(json.loads(data["user"])["id"])
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


def _need_user(request: web.Request) -> int:
    uid = tg_user_id(request)
    if uid is None:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Open this from the bot."}), content_type="application/json")
    return uid


async def my_collection(request: web.Request):
    uid = _need_user(request)
    rows = await db.owner_cards(uid)
    cards = [[r["char_id"], r["name"], r["anime"], r["rarity"], r["sub_rarity"] or "", r["qty"],
              KIND.get(r["media_type"], "p")] for r in rows]
    return web.json_response({"cards": cards})


async def me(request: web.Request):
    uid = _need_user(request)
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


async def start(bot) -> web.AppRunner | None:
    """Start the web server inside the bot process. Never raises: the bot must run even if the web part fails."""
    try:
        app = web.Application(middlewares=[cors])
        app["bot"] = bot
        app.add_routes([
            web.get("/", index), web.get("/index.html", index), web.get("/health", health),
            web.get("/logo.png", logo), web.get("/api/overview", overview), web.get("/api/collection", collection),
            web.get("/api/mycollection", my_collection), web.get("/api/me", me),
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
