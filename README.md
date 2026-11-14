# Otaku Nexus

A Telegram bot for collecting anime characters. Characters drop in group chats, members claim them, build a collection, earn an in-game currency (Nex), and spend it in a daily shop or on mini games.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0)
![Database](https://img.shields.io/badge/database-SQLite-003B57)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Table of Contents

1. [Overview](#overview)
2. [Features](#features)
3. [Architecture](#architecture)
4. [Requirements](#requirements)
5. [Installation](#installation)
6. [Configuration](#configuration)
7. [Commands](#commands)
8. [Game Systems](#game-systems)
9. [Database](#database)
10. [Deployment](#deployment)
11. [Security](#security)
12. [Contributing](#contributing)
13. [License](#license)

---

## Overview

Otaku Nexus is an asynchronous bot built on [aiogram 3](https://docs.aiogram.dev/) and SQLite (via `aiosqlite`). It is designed for group chats: activity in a group triggers character drops, and members compete to claim them. Outside of drops, the bot provides an economy, a daily shop, collection browsing, and administrative tools for managing the character catalogue.

Messages are sent as native Telegram rich messages (Bot API 10.1+) when available, with an automatic fallback to HTML formatting.

## Features

**Collection**
- Characters drop automatically after a configurable number of messages in a group.
- Nine main rarities, from Common to Nexus, with weighted drop rates.
- Twenty-eight optional sub-rarities (outfits and seasonal events) that combine with a main rarity.
- A paginated collection view grouped by series, with a favourite character.

**Economy**
- Daily reward, with a higher payout when the bot's username is in the user's bio.
- Peer-to-peer transfers with `/ngive`.
- All balance changes are serialised with a shared lock to prevent race conditions.

**Shop**
- Each user receives their own random daily offers, so stock is never shared or exhausted.
- Additional high-rarity offers appear on Sundays.
- Purchases are atomic at the database level.

**Mini games**
- `/mines`: a 5x5 board with a rising multiplier and manual cash out.
- `/slot`: free spins using Telegram's slot machine dice, with a daily spin limit.

**Administration**
- Upload, edit, and delete characters directly from Telegram.
- Every upload is mirrored with its media to a dedicated database group.
- Owner and co-owner roles, plus a delegated admin list.
- Per-group drop interval and on/off control.

## Architecture

```
otaku-nexus/
├── otaku.py          Entry point: dispatcher, middleware, start, daily, stats, transfers
├── config.py         Settings, rarities, rewards, prices (secrets loaded from environment)
├── dbase.py          SQLite schema and async data access layer
├── richmsg.py        Rich message methods (sendRichMessage, editMessageText) and helpers
├── drop.py           Drop counter middleware, character drops, claiming
├── waifu.py          Character upload, edit, delete, and admin management
├── harem.py          Collection viewer
├── shop.py           Daily shop
├── minigame.py       Mines and slot games
├── requirements.txt  Python dependencies
├── .env.example      Template for environment variables
├── .gitignore
├── LICENSE
└── README.md
```

Each feature module owns its own `aiogram.Router`. `otaku.py` imports the modules and registers their routers on a single `Dispatcher`, so a feature can be added or removed without touching the others.

## Requirements

- Python 3.10 or newer
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Telegram group to act as the character database (the bot must be an administrator there)

## Installation

```bash
git clone https://github.com/<your-username>/otaku-nexus.git
cd otaku-nexus

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env             # Windows: copy .env.example .env
# edit .env with your values

python otaku.py
```

The SQLite database and its `data/` directory are created automatically on first start.

## Configuration

### Environment variables

Set these in `.env` or in the process environment.

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Bot token issued by BotFather |
| `OWNER_ID` | Yes | Telegram user ID of the owner |
| `CO_OWNERS` | No | Comma-separated user IDs with owner-level rights on character management |
| `BOT_NAME` | No | Display name (default: `Otaku Nexus`) |
| `BOT_USERNAME` | No | Bot username without `@`; used for the daily bio bonus |
| `MAIN_GROUP` | No | Username of the main community group, without `@` |
| `DB_GROUP` | No | Username of the database group, without `@` |
| `DB_PATH` | No | SQLite file path (default: `data/otaku_nexus.db`) |

### Gameplay settings

Gameplay values are plain constants in `config.py`:

| Area | Settings |
|---|---|
| Currency | `CURRENCY_NAME`, `CURRENCY_SYMBOL` |
| Daily reward | `DAILY_AMOUNT`, `DAILY_BIO_AMOUNT`, `DAILY_COOLDOWN` |
| Slot | `SLOT_MAX_SPINS`, `SLOT_WINDOW`, `SLOT_LIMIT_COOLDOWN`, `SLOT_REWARD_*` |
| Mines | `MINES_MIN_BET`, `MINES_MAX_BET`, `MINES_DEFAULT`, `MINES_RTP`, `MINES_TIMEOUT`, `MINES_MIN_CASHOUT` |
| Rarities | `RARITIES`, `SUB_RARITIES`, `SUB_RULES` |
| Drops | `DROP_EVERY`, `DROP_MIN_EVERY`, `DROP_MAX_EVERY`, `DROP_PRESETS`, `DROP_MSG_GAP`, `DROP_QUOTES`, `DROP_ACTIVE_EVENTS` |
| Shop | `SHOP_PRICES`, `SHOP_SLOTS`, `SHOP_SUNDAY_SLOTS`, `SHOP_UTC_OFFSET_MIN`, `SHOP_BANNER` |

## Commands

### General

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/mynex` | Show your Nex balance and profile |
| `/daily` | Claim the daily Nex reward |
| `/ngive <amount>` | Reply to a user to send them Nex |
| `/harem`, `/collection` | Browse your collection by series |
| `/shop` | Open your daily shop |
| `/mines <bet> [mines]` | Start a Mines game |
| `/slot` | Spin the free slot machine |
| `/rarity` | List rarities and drop rates |

### Administration

| Command | Access | Description |
|---|---|---|
| `/upload` | Admins | Add a character (reply to or attach media, then series, name, rarity) |
| `/uploaded`, `/chars` | Admins | Browse uploaded characters by list, series, rarity, or sub-rarity |
| `/edit <id>` | Admins | Edit name, series, picture, or rarity, or delete a character |
| `/cancel` | Admins | Cancel a pending edit |
| `/addadmin`, `/rmadmin` | Owner, co-owners | Add or remove a bot admin |
| `/botsadmins` | Owner | List bot admins |
| `/setstart` | Owner | Set the start message media |
| `/setshop` | Owner, co-owners | Set the shop banner (reply to media) |
| `/stats` | Owner | Show bot statistics |

Group drop controls (such as `/setdrop`) and claiming are implemented in `drop.py`.

## Game Systems

### Rarities

Main rarities and their drop weights (percent, totalling 100):

| Rarity | Weight |
|---|---|
| Common | 40 |
| Uncommon | 25 |
| Rare | 15 |
| Premium | 9 |
| Legendary | 5 |
| Mythic | 3 |
| Celestial | 2 |
| Divine | 0.8 |
| Nexus | 0.2 |

A character has one main rarity and optionally one sub-rarity. `SUB_RULES` restricts which sub-rarities each main rarity may carry. Event sub-rarities do not drop unless listed in `DROP_ACTIVE_EVENTS`.

### Drops

Each group keeps a message counter. When it reaches the group's interval, a random character is dropped and can be claimed once. A per-user gap (`DROP_MSG_GAP`) stops a single user from farming drops by spamming.

### Shop

Every user gets their own offers each day: one Rare, one Premium, and one Legendary, plus one Mythic and one Celestial on Sundays. Prices are set per rarity in `SHOP_PRICES`, and each offer can be bought once per day. The day rolls over at midnight in the offset given by `SHOP_UTC_OFFSET_MIN`. Divine, Nexus, Common, Uncommon, and inactive event cards are never sold.

### Mines

A 5x5 board with 1 to 24 mines. Each safe tile raises the multiplier, calculated from the true probability scaled by `MINES_RTP` (default 0.97, a 3% house edge). Cash out unlocks after `MINES_MIN_CASHOUT` safe tiles, and idle games cash out automatically after `MINES_TIMEOUT` seconds.

### Slot

Free spins with a daily limit. Rewards depend on the match: jackpot, triple, two sevens, or pair. After the limit is reached the player waits for a cooldown.

## Database

SQLite in WAL mode, with these tables:

| Table | Purpose |
|---|---|
| `settings` | Key-value bot settings |
| `users` | Profiles, Nex balance, favourite, last daily claim |
| `chats` | Group registration, message counters, drop interval |
| `characters` | Character catalogue |
| `admins` | Delegated bot admins |
| `collection` | Characters owned by each user |
| `shop_offers` | Per-user daily shop offers |
| `drops` | Active and past drops |

The database file contains user data. Do not commit it, and back it up regularly.

## Deployment

For continuous operation, run the bot under a process manager. Example `systemd` unit:

```ini
[Unit]
Description=Otaku Nexus bot
After=network-online.target

[Service]
WorkingDirectory=/opt/otaku-nexus
EnvironmentFile=/opt/otaku-nexus/.env
ExecStart=/opt/otaku-nexus/.venv/bin/python otaku.py
Restart=always
User=otaku

[Install]
WantedBy=multi-user.target
```

The bot uses long polling, so no public URL or open port is required.

## Security

- Never commit `.env` or the SQLite database; both are listed in `.gitignore`.
- If a bot token is ever exposed, revoke it immediately in BotFather with `/revoke`.
- Only grant admin rights to trusted users, since admins can add, edit, and delete characters.
- Restrict file permissions on `.env` on shared servers.

## Contributing

Contributions are welcome.

1. Fork the repository and create a feature branch.
2. Keep changes focused and match the existing code style.
3. Test the change against a development bot token.
4. Open a pull request describing the change and the reason for it.

## License

Released under the MIT License. See [LICENSE](LICENSE).
