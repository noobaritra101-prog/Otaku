"""Ad rewards: stats + live reward tiers + the owner /ads panel (rich message, toggle buttons).

Own router: otaku.py does `import ads` and `dp.include_router(ads.router)`.
webapi.py imports get_cfg() / find_tier() / record() from here, so what you change in /ads applies at once
(no restart) and every ad / reward is counted per UTC day.

A reward TIER is one thing a user can earn by watching ads:
  {"k": "luck", "ads": 2, "daily": 1, "on": True}            DEFAULT: a lucky draw, one of LUCK (Common 40, Uncommon 25, Rare 15)
  {"k": "card:Mythic", "ads": 8, "daily": 1, "on": True}     a free card of that rarity
  {"k": "nex", "ads": 1, "daily": 3, "on": True, "amt": 50}  Nex
Each tier has its own number of ads and its own daily limit per user.

Storage (settings table, no schema change):
  ad_cfg          JSON {"on": bool, "tiers": [tier, ...], "day": "YYYY-MM-DD"}
                  Changes made in /ads are only for that day: when the stored day is not today the settings fall
                  back to the defaults (default_tiers(), rewards on), so everything resets at midnight by itself.
  adst:YYYY-MM-DD JSON {"st": starts, "ads": finished ads, "rw": rewards, "nex": Nex paid, "u": [user ids], "r": {label: n}}
"""
import asyncio
import json
import logging
import time
from datetime import date, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import dbase as db
from configaration import CURRENCY_SYMBOL, OWNER_ID, RARITIES, RICH_MESSAGES, SHOP_UTC_OFFSET_MIN
from richmsg import edit_rich, esc, send_rich

router = Router()
log = logging.getLogger("otaku.ads")

ADS_RANGE, DAILY_RANGE, NEX_RANGE = (1, 20), (1, 10), (10, 1000)
LUCK = (("Common", 40), ("Uncommon", 25), ("Rare", 15))      # relative weights of the lucky draw
RARITY_ORDER = [n for n, *_ in RARITIES]
_lock = asyncio.Lock()


def default_tiers() -> list[dict]:
    return [{"k": "luck", "ads": 2, "daily": 1, "on": True}]


def new_tier(key: str) -> dict:
    if key == "nex":
        return {"k": "nex", "ads": 1, "daily": 1, "on": True, "amt": 50}
    if key == "luck":
        return {"k": "luck", "ads": 2, "daily": 1, "on": True}
    r = key.split(":", 1)[1]
    idx = RARITY_ORDER.index(r) if r in RARITY_ORDER else 3
    return {"k": key, "ads": min(2 + idx, ADS_RANGE[1]), "daily": 1, "on": True}


# ───────────── time ─────────────
def _utc_today() -> date:
    t = time.gmtime(time.time() + SHOP_UTC_OFFSET_MIN * 60)
    return date(t.tm_year, t.tm_mon, t.tm_mday)


# ───────────── tiers ─────────────
def rar_emoji(name: str) -> str:
    return next((e for n, e, *_ in RARITIES if n == name), "•")


def luck_chances() -> list[list]:
    tot = sum(w for _, w in LUCK)
    return [[r, round(w * 100 / tot)] for r, w in LUCK]


def tier_name(t: dict) -> str:
    """Short name without emoji: 'Mythic card' / 'Nex 50' / 'Lucky card'."""
    if t["k"] == "luck":
        return "Lucky card"
    if t["k"] == "nex":
        return f"Nex {t.get('amt', 50)}"
    return f"{t['k'].split(':', 1)[1]} card"


def tier_label(t: dict) -> str:
    if t["k"] == "luck":
        return "Lucky card"
    if t["k"] == "nex":
        return f"{CURRENCY_SYMBOL} Nex {t.get('amt', 50)}"
    r = t["k"].split(":", 1)[1]
    return f"{rar_emoji(r)} {r} card"


def tier_stat_key(t: dict) -> str:
    """Label used in the stats ('Common', 'Mythic', 'Nex'). A lucky draw is counted under the rarity it rolled."""
    return "Nex" if t["k"] == "nex" else t["k"].split(":", 1)[-1]


def find_tier(cfg: dict, key: str | None, only_on: bool = True) -> dict | None:
    for t in cfg["tiers"]:
        if t["k"] == key and (t["on"] or not only_on):
            return t
    return None


def sort_tiers(tiers: list[dict]) -> list[dict]:
    def k(t):
        if t["k"] == "luck":
            return (-1, 0)
        if t["k"] == "nex":
            return (1, 0)
        r = t["k"].split(":", 1)[1]
        return (0, RARITY_ORDER.index(r) if r in RARITY_ORDER else 99)
    return sorted(tiers, key=k)


# ───────────── live settings ─────────────
def _clamp(v, lo_hi, default):
    try:
        return min(max(int(v), lo_hi[0]), lo_hi[1])
    except (TypeError, ValueError):
        return default


async def get_cfg() -> dict:
    raw = await db.get_setting("ad_cfg")
    try:
        c = json.loads(raw) if raw else {}
    except ValueError:
        c = {}
    custom = c.get("day") == _utc_today().isoformat()      # today's changes; anything older has expired
    if not custom:
        c = {}
    tiers = []
    for t in c.get("tiers") or ([] if "tiers" in c else default_tiers()):
        k = str(t.get("k", ""))
        if k not in ("nex", "luck") and not (k.startswith("card:") and k.split(":", 1)[1] in RARITY_ORDER):
            continue
        item = {"k": k, "ads": _clamp(t.get("ads"), ADS_RANGE, 2), "daily": _clamp(t.get("daily"), DAILY_RANGE, 1),
                "on": bool(t.get("on", True))}
        if k == "nex":
            item["amt"] = _clamp(t.get("amt"), NEX_RANGE, 50)
        if not any(x["k"] == k for x in tiers):
            tiers.append(item)
    return {"on": bool(c.get("on", True)), "tiers": sort_tiers(tiers), "custom": custom}


async def save_cfg(cfg: dict) -> None:
    """Save today's changes (they expire at midnight)."""
    cfg = {"on": cfg["on"], "tiers": sort_tiers(cfg["tiers"]), "day": _utc_today().isoformat()}
    await db.set_setting("ad_cfg", json.dumps(cfg))


async def reset_cfg() -> None:
    await db.set_setting("ad_cfg", "")


# ───────────── stats ─────────────
async def record(event: str, uid: int | None = None, label: str | None = None, nex: int = 0) -> None:
    """event: start (pressed Watch), ad (one finished ad), reward (given). Never raises."""
    try:
        key = f"adst:{_utc_today().isoformat()}"
        async with _lock:
            raw = await db.get_setting(key)
            d = json.loads(raw) if raw else {}
            for f in ("st", "ads", "rw", "nex"):
                d.setdefault(f, 0)
            d.setdefault("u", []); d.setdefault("r", {})
            if event == "start":
                d["st"] += 1
            elif event == "ad":
                d["ads"] += 1
            elif event == "reward":
                d["rw"] += 1
                d["nex"] += nex
                if label:
                    d["r"][label] = d["r"].get(label, 0) + 1
            if uid and uid not in d["u"]:
                d["u"].append(uid)
            await db.set_setting(key, json.dumps(d))
    except Exception:
        log.exception("ad stat not saved")


async def day_stats(day: date) -> dict:
    raw = await db.get_setting(f"adst:{day.isoformat()}")
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        d = {}
    return {"st": d.get("st", 0), "ads": d.get("ads", 0), "rw": d.get("rw", 0), "nex": d.get("nex", 0),
            "u": set(d.get("u", [])), "r": d.get("r", {})}


async def range_stats(days: list[date]) -> dict:
    tot = {"st": 0, "ads": 0, "rw": 0, "nex": 0, "u": set(), "r": {}, "best": None, "daily": []}
    for day in days:
        d = await day_stats(day)
        for f in ("st", "ads", "rw", "nex"):
            tot[f] += d[f]
        tot["u"] |= d["u"]
        for r, n in d["r"].items():
            tot["r"][r] = tot["r"].get(r, 0) + n
        tot["daily"].append((day, d["ads"], d["rw"]))
        if d["ads"] and (tot["best"] is None or d["ads"] > tot["best"][1]):
            tot["best"] = (day, d["ads"])
    return tot


def month_days(y: int, m: int) -> list[date]:
    d, out = date(y, m, 1), []
    while d.month == m:
        out.append(d)
        d += timedelta(days=1)
    return out


def shift_month(y: int, m: int, delta: int) -> tuple[int, int]:
    n = y * 12 + (m - 1) + delta
    return n // 12, n % 12 + 1


# ───────────── rendering ─────────────
def stat_lines(s: dict, per_day: bool = False, nd: int = 1) -> list[str]:
    done = round(s["rw"] * 100 / s["st"]) if s["st"] else 0
    out = [f"Started : {s['st']}", f"Ads watched : {s['ads']}", f"Rewards given : {s['rw']}",
           f"Unique users : {len(s['u'])}", f"Completion : {done}%"]
    if s["nex"]:
        out.append(f"Nex paid : {CURRENCY_SYMBOL} {s['nex']}")
    if per_day and nd > 1:
        out.append(f"Ads per day : {s['ads'] / nd:.1f}")
    return out


def rarity_lines(s: dict) -> list[str]:
    if not s["r"]:
        return ["Nothing given yet"]
    def k(x):
        return (1, 0) if x[0] == "Nex" else (0, RARITY_ORDER.index(x[0]) if x[0] in RARITY_ORDER else 99)
    return [f"{CURRENCY_SYMBOL if r == 'Nex' else rar_emoji(r)} {r} : {n}" for r, n in sorted(s["r"].items(), key=k)]


def tier_line(t: dict) -> str:
    return f"{tier_label(t)} · {t['ads']} ads · {t['daily']}/day · {'On' if t['on'] else 'Off'}"


async def build(view: str, arg: str | None = None) -> dict:
    """Returns {"view", "arg", "md", "html", "cfg"}."""
    cfg = await get_cfg()
    t = _utc_today()
    md: list[str] = []
    html: list[str] = []

    def block(title: str, lines: list[str]):
        md.append(f"### {title}")
        md.append("\n".join(f"> {esc(x)}" for x in lines))
        md.append("")
        html.append(f"<b>{title}</b>\n<blockquote>" + "\n".join(x.replace("&", "&amp;").replace("<", "&lt;") for x in lines) + "</blockquote>")

    def head(title: str, sub: str):
        md.extend([f"## {title}", sub, ""])
        html.append(f"<b>{title}</b>\n{sub}\n")

    status = "On" if cfg["on"] else "Paused"
    mode = "Changed for today, resets at midnight" if cfg["custom"] else "Default"
    if view == "today":
        s = await range_stats([t])
        y = await range_stats([t - timedelta(days=1)])
        head("Ad rewards", f"{status} · Today, {t.isoformat()} (UTC)")
        block("Today", stat_lines(s))
        block("Given", rarity_lines(s))
        block("Yesterday", [f"Ads watched : {y['ads']}", f"Rewards given : {y['rw']}", f"Unique users : {len(y['u'])}"])
    elif view == "week":
        days = [t - timedelta(days=i) for i in range(6, -1, -1)]
        s = await range_stats(days)
        head("Ad rewards", f"{status} · Last 7 days")
        block("Total", stat_lines(s, True, 7))
        block("Per day (ads · rewards)", [f"{d.strftime('%a %d %b')} : {a} · {r}" for d, a, r in s["daily"]])
        block("Given", rarity_lines(s))
    elif view == "month":
        y, m = (int(x) for x in arg.split("-")) if arg else (t.year, t.month)
        days = [d for d in month_days(y, m) if d <= t]
        s = await range_stats(days)
        head("Ad rewards", f"{status} · {date(y, m, 1).strftime('%B %Y')}")
        block("Month total", stat_lines(s, True, max(len(days), 1)))
        best = f"{s['best'][0].strftime('%d %b')} ({s['best'][1]} ads)" if s["best"] else "-"
        block("Highlights", [f"Best day : {best}", f"Active days : {sum(1 for _, a, _ in s['daily'] if a)}"])
        block("Given", rarity_lines(s))
        arg = f"{y}-{m:02d}"
    elif view == "tier":
        tr = find_tier(cfg, arg, only_on=False)
        if not tr:
            return await build("cfg")
        head(tier_label(tr), f"{status} · {mode}")
        lines = ["Enabled" if tr["on"] else "Disabled", f"Ads to watch : {tr['ads']}",
                 f"Limit per user : {tr['daily']} per day"]
        if tr["k"] == "nex":
            lines.append(f"Amount : {CURRENCY_SYMBOL} {tr['amt']}")
        block("Settings", lines)
        if tr["k"] == "luck":
            block("Chances (by luck)", [f"{rar_emoji(r)} {r} : {p}%" for r, p in luck_chances()])
    elif view == "add":
        head("Add a reward", "Pick what users can earn. It starts with default numbers you can change.")
    else:  # cfg
        head("Reward settings", f"{status} · {mode}")
        if cfg["tiers"]:
            block("Rewards", [tier_line(x) for x in cfg["tiers"]])
        else:
            block("Rewards", ["None yet. Tap Add reward."])
        note = "Each reward has its own ads and daily limit. Changes apply instantly on the website and last until midnight, then everything goes back to the default."
        md.append(note)
        html.append(note)
    return {"view": view, "arg": arg, "cfg": cfg, "md": "\n".join(md).strip(), "html": "\n".join(html).strip()}


def _b(text: str, data: str, style: str | None, styled: bool) -> InlineKeyboardButton:
    extra = {"style": style} if (style and styled) else {}
    return InlineKeyboardButton(text=text, callback_data=data, **extra)


def kb(d: dict, styled: bool = True) -> InlineKeyboardMarkup:
    v, cfg, arg = d["view"], d["cfg"], d["arg"]
    b = lambda text, data, style=None: _b(text, data, style, styled)
    in_cfg = v in ("cfg", "tier", "add")
    tabs = [b(label, f"ad:v:{key}", "primary" if (key == v or (key == "cfg" and in_cfg)) else None)
            for key, label in (("today", "Today"), ("week", "7 Days"), ("month", "Month"), ("cfg", "Rewards"))]
    rows = [tabs[:3], tabs[3:]]
    if v == "month":
        y, m = (int(x) for x in arg.split("-"))
        py, pm = shift_month(y, m, -1)
        ny, nm = shift_month(y, m, 1)
        nav = [b("Prev", f"ad:v:month:{py}-{pm:02d}")]
        if (ny, nm) <= (_utc_today().year, _utc_today().month):
            nav.append(b("Next", f"ad:v:month:{ny}-{nm:02d}"))
        rows.append(nav)
    elif v == "cfg":
        rows.append([b("Rewards on · tap to pause" if cfg["on"] else "Rewards paused · tap to enable", "ad:s:on",
                       "success" if cfg["on"] else "danger")])
        for x in cfg["tiers"]:
            rows.append([b(f"{tier_label(x)} · {x['ads']} ads" + ("" if x["on"] else " · off"), f"ad:t:{x['k']}")])
        rows.append([b("Add reward", "ad:add", "primary")])
        if cfg["custom"]:
            rows.append([b("Reset to default", "ad:reset", "danger")])
    elif v == "tier":
        k = arg
        tr = find_tier(cfg, k, only_on=False)
        rows.append([b("Disable" if tr["on"] else "Enable", f"ad:ts:{k}:on:0", "danger" if tr["on"] else "success")])
        rows.append([b("-", f"ad:ts:{k}:ads:-1"), b(f"Ads: {tr['ads']}", "ad:noop"), b("+", f"ad:ts:{k}:ads:1")])
        rows.append([b("-", f"ad:ts:{k}:daily:-1"), b(f"Daily: {tr['daily']}", "ad:noop"), b("+", f"ad:ts:{k}:daily:1")])
        if k == "nex":
            rows.append([b("-100", f"ad:ts:{k}:amt:-100"), b("-10", f"ad:ts:{k}:amt:-10"),
                         b("+10", f"ad:ts:{k}:amt:10"), b("+100", f"ad:ts:{k}:amt:100")])
        rows.append([b("Remove", f"ad:ts:{k}:del:0", "danger"), b("Back", "ad:v:cfg")])
    elif v == "add":
        have = {x["k"] for x in cfg["tiers"]}
        opts = (["luck"] if "luck" not in have else []) + [f"card:{n}" for n in RARITY_ORDER if f"card:{n}" not in have]
        if "nex" not in have:
            opts.append("nex")
        row = []
        for o in opts:
            label = ("Lucky card" if o == "luck" else f"{CURRENCY_SYMBOL} Nex" if o == "nex"
                     else f"{rar_emoji(o.split(':', 1)[1])} {o.split(':', 1)[1]}")
            row.append(b(label, f"ad:ta:{o}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([b("Back", "ad:v:cfg")])
    if v in ("today", "week", "month"):
        rows.append([b("Refresh", f"ad:v:{v}" + (f":{arg}" if v == "month" else ""))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def with_styles(call):
    for styled in (True, False):
        try:
            return await call(styled)
        except TelegramBadRequest as e:
            if styled and "button" in str(e).lower():
                continue
            raise


# ───────────── handlers (owner only) ─────────────
@router.message(Command("ads"), F.from_user.id == OWNER_ID)
async def ads_cmd(m: Message):
    d = await build("today")
    if RICH_MESSAGES:
        try:
            await with_styles(lambda s: send_rich(
                m.bot, m.chat.id, d["md"], reply_markup=kb(d, s),
                message_thread_id=m.message_thread_id if m.is_topic_message else None,
                reply_to_message_id=m.message_id))
            return
        except Exception as e:
            log.warning("rich /ads failed, using HTML: %s", e)
    await with_styles(lambda s: m.reply(d["html"], reply_markup=kb(d, s)))


async def show(msg: Message, d: dict) -> None:
    if RICH_MESSAGES:
        try:
            await with_styles(lambda s: edit_rich(msg.bot, msg.chat.id, msg.message_id, d["md"], reply_markup=kb(d, s)))
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
            log.warning("rich /ads edit failed, using HTML: %s", e)
        except Exception as e:
            log.warning("rich /ads edit failed, using HTML: %s", e)
    try:
        await with_styles(lambda s: msg.edit_text(d["html"], reply_markup=kb(d, s)))
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise


@router.callback_query(F.data.startswith("ad:"))
async def ads_cb(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Owner only.", show_alert=True)
    if not call.message:
        return await call.answer()
    p = call.data.split(":", 2)           # ad:<act>:<rest>  (rest may hold a card key like card:Mythic)
    act, rest = p[1] if len(p) > 1 else "", p[2] if len(p) > 2 else ""
    if act == "noop":
        return await call.answer()
    view, arg = "today", None
    if act == "v":
        view, _, arg = rest.partition(":")
        arg = arg or None
    elif act == "s" and rest == "on":
        cfg = await get_cfg()
        cfg["on"] = not cfg["on"]
        await save_cfg(cfg)
        view = "cfg"
    elif act == "add":
        view = "add"
    elif act == "reset":
        await reset_cfg()
        view = "cfg"
    elif act == "ta":                     # add a reward
        cfg = await get_cfg()
        if rest and not find_tier(cfg, rest, only_on=False) and (rest in ("nex", "luck") or rest.split(":", 1)[-1] in RARITY_ORDER):
            cfg["tiers"].append(new_tier(rest))
            await save_cfg(cfg)
        view, arg = "tier", rest
    elif act == "t":                      # open a reward
        view, arg = "tier", rest
    elif act == "ts":                     # change a reward: ad:ts:<key>:<field>:<delta>
        key, field, delta = rest.rsplit(":", 2)
        cfg = await get_cfg()
        tr = find_tier(cfg, key, only_on=False)
        view, arg = "tier", key
        if tr:
            d = int(delta)
            if field == "on":
                tr["on"] = not tr["on"]
            elif field == "ads":
                tr["ads"] = _clamp(tr["ads"] + d, ADS_RANGE, tr["ads"])
            elif field == "daily":
                tr["daily"] = _clamp(tr["daily"] + d, DAILY_RANGE, tr["daily"])
            elif field == "amt" and key == "nex":
                tr["amt"] = _clamp(tr.get("amt", 50) + d, NEX_RANGE, 50)
            elif field == "del":
                cfg["tiers"] = [x for x in cfg["tiers"] if x["k"] != key]
                view, arg = "cfg", None
            await save_cfg(cfg)
    else:
        return await call.answer()
    if view not in ("today", "week", "month", "cfg", "tier", "add"):
        return await call.answer()
    await show(call.message, await build(view, arg))
    await call.answer()
