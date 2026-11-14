# Otaku Nexus - configuration
# Secrets and IDs come from environment variables (.env file), never from the code.
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:      # python-dotenv is optional; plain environment variables still work
    pass


def _ids(name: str) -> list[int]:
    return [int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x]


BOT_TOKEN = os.environ["BOT_TOKEN"]            # required, from @BotFather
BOT_NAME = os.getenv("BOT_NAME", "Otaku Nexus")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")   # without @
MAIN_GROUP = os.getenv("MAIN_GROUP", "")       # without @
DB_GROUP = os.getenv("DB_GROUP", "")           # every upload is posted here with the picture (bot must be admin)
OWNER_ID = int(os.environ["OWNER_ID"])         # required
CO_OWNERS = _ids("CO_OWNERS")                  # comma separated; same rights as the owner in waifu.py

# MongoDB (the live database). The URI holds the password, so it lives in .env only.
MONGO_URI = os.environ["MONGO_URI"]            # required, e.g. mongodb+srv://user:password@cluster0.xxx.mongodb.net/
MONGO_DB = os.getenv("MONGO_DB", "otaku_nexus")

# Old SQLite file: only used by the /restore command to copy previous data into MongoDB.
DB_PATH = os.getenv("DB_PATH", "data/otaku_nexus.db")
DROP_EVERY = 100                   # messages per drop (default; each group can change it with /setdrop)

RICH_MESSAGES = True               # native rich messages (Bot API 10.1+) for /mynex, auto-fallback to HTML
CAPTION_ABOVE_MEDIA = False        # put the start text above the start media

# Currency
CURRENCY_NAME = "Nex"
CURRENCY_SYMBOL = "⟐"

# Daily reward (/daily)
DAILY_AMOUNT = 40                  # Nex per claim (was 200)
DAILY_BIO_AMOUNT = 80              # Nex per claim when the bot's @username is in the user's bio (was 400)
DAILY_COOLDOWN = 24 * 60 * 60      # seconds between claims

# Slot machine (/slot) - free spin with Telegram's 🎰 dice, no bet, pays a Nex reward by match
SLOT_MAX_SPINS = 8                 # free spins allowed per window
SLOT_WINDOW = 24 * 60 * 60         # seconds: spins are counted per user in this window (resets after it)
SLOT_LIMIT_COOLDOWN = 12 * 60 * 60 # seconds of cooldown once SLOT_MAX_SPINS spins are used
SLOT_REWARD_JACKPOT = 100          # 7 7 7 (was 500)
SLOT_REWARD_TRIPLE = 40            # BAR BAR BAR / 🍇🍇🍇 / 🍋🍋🍋 (was 150)
SLOT_REWARD_TWO_SEVENS = 15        # exactly two 7s (was 50)
SLOT_REWARD_PAIR = 3                # any other pair (was 10)
SLOT_REWARD_NONE = 0               # no match

# Mines game (/mines bet [mines])
MINES_MIN_BET = 10
MINES_MAX_BET = 50000
MINES_DEFAULT = 3                  # mines on the 5x5 board when not given (1-24)
MINES_RTP = 0.97                   # payout fairness, 0.97 = 3% house edge
MINES_TIMEOUT = 300                # idle seconds before auto cash out
MINES_MIN_CASHOUT = 3              # safe tiles to open before cash out unlocks

# Mines tile icons
MINES_ICON_HIDDEN = "\u2800"       # unopened tile: blank (Braille blank, buttons can't be empty)
MINES_ICON_TILE = "🪎"             # opened safe tile
MINES_ICON_BOMB = "💣"             # other mines shown when the game ends
MINES_ICON_BLAST = "💥"            # the mine that was hit

# Main rarities: (name, emoji, drop weight %, note). Weights add up to 100.
# Only the main rarities take part in normal drops.
RARITIES = [
    ("Common", "☘️", 40, "Everyday drops"),
    ("Uncommon", "🍀", 25, "A little luck"),
    ("Rare", "🧿", 15, "Worth showing off"),
    ("Premium", "💎", 9, "Refined and sought after"),
    ("Legendary", "🔮", 5, "Serious bragging rights"),
    ("Mythic", "🎗️", 3, "Almost never seen"),
    ("Celestial", "🎐", 2, "Touched by the heavens"),
    ("Divine", "🦪", 0.8, "A once in a season find"),
    ("Nexus", "🪐", 0.2, "The rarest of all"),
]

# Sub rarities (versions): (name, emoji, note). A card has one main rarity and
# optionally one sub rarity, so a Maid card can be Common or Legendary.
# Seasonal ones are meant for events; add new ones here whenever needed.
SUB_RARITIES = [
    ("Halloween", "🎃", "Halloween event"),
    ("AMV", "🎬", "AMV edition"),
    ("Maid", "🧹", "Maid outfit"),
    ("Bunny", "🐰", "Bunny outfit"),
    ("Christmas", "🎄", "Christmas event"),
    ("Valentine", "💝", "Valentine event"),
    ("Summer", "☀️", "Summer event"),
    ("Winter", "❄️", "Winter event"),
    ("Cosplay", "🎭", "Cosplay"),
    ("School", "🎒", "School uniform"),
    ("Kimono", "👘", "Kimono"),
    ("Gothic", "🦇", "Gothic style"),
    ("Idol", "🎤", "Idol stage outfit"),
    ("Wedding", "💍", "Wedding"),
    ("Magical Girl", "🪄", "Magical girl"),
    ("Erotic", "🍑", "Erotic"),
    ("Swimsuit", "👙", "Swimsuit"),
    ("Spring", "🌸", "Spring event"),
    ("Autumn", "🍁", "Autumn event"),
    ("Rainy", "🌧️", "Rainy day"),
    ("Vampire", "🧛", "Vampire"),
    ("Pirate", "🏴‍☠️", "Pirate"),
    ("Chibi", "👶", "Chibi"),
    ("Traditional", "🥻", "Traditional outfit"),
    ("Moon", "🌙", "Moon"),
    ("Jungle", "🌳", "Jungle"),
    ("Police", "👮", "Police uniform"),
    ("Gamer", "🎮", "Gamer"),
]

# Which sub rarities a main rarity may carry (rarity name -> allowed sub names).
# An empty list means no sub rarity at all. Rarities not listed allow every sub rarity.
SUB_RULES = {
    "Common": [],
    "Uncommon": [],
    "Nexus": ["AMV"],
}

# Drops (drop.py)
DROP_MIN_EVERY = 30                # lowest interval /setdrop accepts (anti-spam)
DROP_MAX_EVERY = 1000              # highest interval /setdrop accepts
DROP_PRESETS = [50, 100, 150, 200, 300]   # interval buttons in the /setdrop panel
DROP_MSG_GAP = 3                   # seconds: one user counts once per this gap (stops spam-farming drops)
# Drop messages: (bold in-character line, closing line). {cmd} becomes "/bind name". Add as many as you like.
DROP_QUOTES = [
    ("Huff ! Came all the ways crossing just for you !",

     "{cmd} to bind me before anyone does"),

    ("Finally found you ! Did you miss me ?",

     "{cmd} and take me home before they notice"),

    ("A summoning circle ? Well, that works too !",

     "{cmd} to claim me quickly, my time here is short"),

    ("Hmm ? You can see me ? Then say my name !",

     "{cmd} before someone else beats you to it"),

    ("I crossed a thousand worlds for this moment !",

     "{cmd} and make me yours before I vanish"),

    ("Are you my new partner ? Prove it !",

     "{cmd} to prove it, and be the first"),

    ("Nobody told me the party started without me !",

     "{cmd} to bring me in before I leave"),

    ("Don't just stand there, I'm right here !",

     "{cmd} and I'm all yours"),

    ("The Nexus brought me here. Maybe it's fate !",

     "{cmd} to seal the bond before anyone else"),

    ("Ugh, the portal was so crowded. I'm exhausted !",

     "{cmd} and let me rest in your collection"),

    ("Plot twist : I'm only here for today !",

     "{cmd} before someone else claims me"),

    ("Eh ? Did you just blink ? I'm right here !",

     "{cmd} before I wander off"),
]
DROP_DUP_NEX = 0                   # Nex paid when a bind is a duplicate (0 = off)
BIG_RARITIES = ["Divine", "Nexus"] # get a bigger heading when bound

# Event sub rarities never drop unless listed in DROP_ACTIVE_EVENTS, e.g. ["Halloween"] during the event.
EVENT_SUBS = ["Halloween", "Christmas", "Valentine", "Summer", "Winter", "Spring", "Autumn"]
DROP_ACTIVE_EVENTS = []


# ───────────── Shop (shop.py) ─────────────
# Earning per user per day (after the reduced rewards above):
#   /daily      40 Nex, or 80 with the bot's @username in the bio
#   /slot       8 spins per 24h, average 6.8 Nex per spin = about 55 Nex (hard cap 800 if every spin is a jackpot)
#   /mines      no income: payout is MINES_RTP (0.97), so it loses Nex on average
#   => about 135 Nex per day on average with the bio bonus (about 95 without), 880 absolute maximum.
# Prices keep the rarity order and nothing takes longer than 20 days of average income (135 Nex a day with the bio bonus):
#   Rare ~2 days, Premium ~5, Legendary ~11, Mythic ~16, Celestial 20 (without the bio bonus, about 95 a day: up to 28 days).
SHOP_PRICES = {
    "Rare": 300,
    "Premium": 700,
    "Legendary": 1500,
    "Mythic": 2200,
    "Celestial": 2700,
}
# Every user gets their own random card per rarity each day (no shared stock, unlimited buyers).
# (rarity, cards per day). A user can buy each of their cards once per day.
SHOP_SLOTS = [("Rare", 1), ("Premium", 1), ("Legendary", 1)]
SHOP_SUNDAY_SLOTS = [("Mythic", 1), ("Celestial", 1)]   # only on Sunday, gone when Sunday ends
SHOP_UTC_OFFSET_MIN = 0            # the shop day (reset + "Sunday") follows this UTC offset: 0 = UTC (midnight UTC), 330 = India (IST)
SHOP_BANNER = "https://i.ibb.co/3yrzHKTg/file-000000009cec82119ae43142c8e19852.png"   # picture on top of /shop ("" = none)
