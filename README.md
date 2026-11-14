<div align="center">

# 🪐 Otaku Nexus

**A Telegram bot for collecting waifus & husbandos.**
Characters drop in group chats, you bind them, build a harem, earn **Nex ⟐**, and spend it in the shop.

![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)
![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white)
![SQLite](https://img.shields.io/badge/database-SQLite-003B57?logo=sqlite&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

</div>

---

## ✨ Features

- 🎴 **Character drops** – a card appears every N messages in a group; first to `/bind` it wins
- 💎 **9 main rarities** (Common → Nexus) plus 28 sub-rarities (Maid, Bunny, Halloween, …)
- 📚 **Harem** – browse your collection series by series with pagination and a favourite card
- 🛒 **Daily shop** – every user gets their own random Rare / Premium / Legendary cards, plus Mythic & Celestial on Sundays
- 🎰 **Mini games** – `/mines` (5×5 board, cash-out) and `/slot` (free 🎰 spins)
- 💰 **Economy** – `/daily`, `/ngive`, `/mynex` with an atomic, lock-protected Nex ledger
- 🛠 **Admin tools** – upload, edit and delete characters, manage bot admins, mirror uploads to a database group
- 🖼 **Rich messages** (Bot API 10.1+) with automatic HTML fallback

## 🗂 Project structure

```
otaku-nexus/
├── otaku.py      # entry point: dispatcher, start, daily, stats, middlewares
├── config.py     # settings, rarities, prices (secrets come from .env)
├── dbase.py      # SQLite schema + async data layer
├── richmsg.py    # sendRichMessage / editMessageText helpers
├── waifu.py      # upload, edit, admins
├── harem.py      # /harem collection viewer
├── shop.py       # /shop daily store
├── minigame.py   # /mines and /slot
├── drop.py       # drops + /bind  (add this file)
├── .env.example
├── requirements.txt
└── README.md
```

## 🚀 Getting started

```bash
git clone https://github.com/<your-username>/otaku-nexus.git
cd otaku-nexus

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # then edit .env with your token and IDs
python otaku.py
```

Create the bot with [@BotFather](https://t.me/BotFather). For the database group, add the bot as an **admin**.

## ⚙️ Configuration

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Token from BotFather |
| `OWNER_ID` | ✅ | Telegram user ID of the owner |
| `CO_OWNERS` | | Comma-separated IDs with owner rights |
| `BOT_USERNAME` | | Bot username without `@` (used for the daily bio bonus) |
| `MAIN_GROUP` / `DB_GROUP` | | Public group and database group usernames |
| `DB_PATH` | | SQLite file path (default `data/otaku_nexus.db`) |

Gameplay values (rarities, prices, rewards, drop quotes) live in `config.py`.

## 🤖 Commands

| Command | Who | Description |
|---|---|---|
| `/start` | all | Welcome message |
| `/daily` | all | Claim Nex (double with the bot in your bio) |
| `/mynex` | all | Balance and profile |
| `/ngive` | all | Send Nex to another user |
| `/harem` · `/collection` | all | Your collection |
| `/shop` | all | Daily card shop |
| `/mines <bet> [mines]` | all | Mines game |
| `/slot` | all | Free slot spin |
| `/rarity` | all | Rarity table |
| `/stats` | admin | Bot statistics |
| `/upload` | admin | Add a character |
| `/uploaded` · `/chars` | admin | List uploaded characters |
| `/edit` | admin | Edit or delete a character |
| `/addadmin` · `/rmadmin` · `/botsadmins` | owner | Manage admins |
| `/setstart` · `/setshop` | owner | Set start / shop media |

## 🔐 Security

- Never commit `.env` or your database file (both are in `.gitignore`).
- If a token was ever pushed or shared, revoke it in @BotFather (`/revoke`).

## 📄 License

MIT – see [LICENSE](LICENSE).
