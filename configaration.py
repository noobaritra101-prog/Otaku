# Otaku Nexus - configuration
BOT_TOKEN = "8607412360:AAEIX6pf3Yd9S0J5Dc_OWtk4PU-BW8aDOTg"
BOT_NAME = "Otaku Nexus"
BOT_USERNAME = "Otakunx_bot"
MAIN_GROUP = "otaku_main"          # without @
NEWS_CHANNEL = "otaku_newz"        # news channel (without @), button on /start
DB_GROUP = "otaku_db"              # database group: every upload is posted here with the picture (bot must be admin)
OWNER_ID = 5716292610
CO_OWNERS = [5583111696, 8338154926, 7964904329, 5822885863]   # same rights as the owner in waifu.py

DB_PATH = "otaku_nexus.db"
BACKUP_GROUP = "otaku_file"        # backup group (without @): bot must be admin with Pin + Delete rights
BACKUP_EVERY = 15 * 60             # seconds between database backups
DROP_EVERY = 110                   # messages per drop (default; each group can change it with /setdrop)

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

# Guess game (/guess) - spoiler picture, type the name in the chat
GUESS_SECONDS = 30                 # time to guess
GUESS_REWARD = 5                   # Nex for the first correct guess
GUESS_DAILY_CAP = 10               # paid wins per user per day (UTC), max 50 Nex a day; wins over the cap pay nothing and are not counted

# Leaderboard (/topplayers)
TOPPLAYERS_COUNT = 10             # players shown

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
DROP_MIN_EVERY = 110               # lowest interval /setdrop accepts (anti-spam)
DROP_MAX_EVERY = 500               # highest interval /setdrop accepts
DROP_STEP = 10                     # how much the minus / plus buttons in the /setdrop panel change the interval
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
