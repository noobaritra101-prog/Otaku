# Otaku Nexus - project notes (claude.md)

Purpose: single source of truth so work can continue from any account or chat.
Rule: update this file on EVERY change (changelog + file map + status).
To continue in a new chat: upload this file plus the .py files and say "continue Otaku Nexus".

## Project
Telegram waifu and husbando collection bot. Character pictures drop in groups, users grab them by name, build a collection, trade and gift.

- Bot name: Otaku Nexus
- Username: @Otakunx_bot
- Main group: @otaku_main
- Database group: @otaku_db (config DB_GROUP; bot must be admin; every upload is posted there with the picture)
- Owner ID: 5716292610
- Co-owners (config CO_OWNERS): 5583111696, 8338154926, 7964904329, 5822885863. In waifu.py they have owner rights (/upload, /edit, /addadmin, /rmadmin). Other owner commands (/setstart, /stats) are still owner only.
- Stack: Python, aiogram 3 (latest), aiosqlite (SQLite, WAL mode)
- Start message: rich message in PM (video / GIF / photo from /setstart as a media block), HTML as automatic fallback.
- Other bot replies: HTML parse mode (bold headings + blockquote)

## Style rules (from owner)
- Use few emojis, keep text clean.
- /start must NOT list commands. It should be a rich intro (headings 1-6, pull quote, table, check list, details, divider, coloured buttons).
- Use Telegram's newer features (blockquote, expandable quote, caption above media, link preview options).
- File names are fixed: otaku.py, dbase.py, configaration.py (spelling intended).
- No env vars; settings live in configaration.py.

## File map
| File | Role |
|---|---|
| otaku.py | Main bot: handlers, rich start message (+ HTML fallback), owner commands, startup |
| richmsg.py | Rich message helper (SendRichMessage method, Markdown escape, media block). Used by /start and /mynex |
| waifu.py | Character management: /upload, /uploaded, /edit panel (edit + delete), /addadmin, /rmadmin; own router, included from otaku.py |
| harem.py | also holds /topplayers and /settplayer (v0.34). /harem (alias /collection) | all | Done (v0.23: rich message, picture on top (the /fav card if still owned, else the first character of the top series), then one section per series: "Series (collected/total)", a numbered list where the number is the character ID (alternating "." / ")" so every item starts its own list): `ID. Name (sub emoji) - Rarity`, xN when 2+ copies, and a `---` divider after every series. No totals line. Footer H6 "Page x/y". 5 series per page, most collected first, characters by ID; smart Prev / page / Next buttons only when 2+ pages (no Prev on page 1, no Next on last page) + View Collection button; callback hm:<viewer>:<target>:<page>, old 5-part buttons still accepted; /harem always opens the command user's own harem (replying to someone changes nothing); fallback photo + HTML caption, then text) |
| /fav ID | all | Done (v0.18: must own the card; shows the card with Confirm / Cancel, only the sender can press; saves users.favorite_id; favorite is cleared if the last copy is gifted away) |
| /gift ID (reply) | all | Done (v0.18: gives ONE copy; no self / bots; shows the card with Confirm / Cancel; ownership re-checked and moved atomically on confirm via dbase.transfer_character) |
| drop.py | One big file: drops (message counter middleware, drop message, /bind, /setdrop, /dropnow), /fav, /gift (Confirm / Cancel), reply_card helper, and inline mode (all characters, collection.<id>, Check binds, spoiler); own router, included from otaku.py. Replaces the old inline.py, fav.py and gift.py (v0.19) |
| minigame.py | /mines, /slot and /whoami games plus their shared reply helpers (notice, rich_reply_to, topic_id, fmt_wait); own router, included from otaku.py |
| shop.py | /shop and /setshop: per-user random daily cards (Rare / Premium / Legendary, + Mythic and Celestial on Sunday), buy with Nex (Confirm step, atomic); own router, included from otaku.py |
| dbase.py | SQLite layer: schema, pragmas, settings, users, chats, stats |
| configaration.py | Token, names, owner ID, drop interval, rarities (name, emoji, weight, note) and sub rarities, flags RICH_MESSAGES / CAPTION_ABOVE_MEDIA |
| claude.md | This file |
| requirements.txt | aiogram, aiosqlite |

## Database tables (dbase.py)
settings(key, value), users, chats(msg_count, drop_every), characters(name, anime, gender, rarity, file_id),
collection(user_id, char_id, qty), drops(chat_id -> current dropped char), admins(user_id).
chats also has drop_enabled (auto-migrated). characters also has sub_rarity, artist_name (uploader first name), db_msg_id (message id of the post in @otaku_db). char_id is AUTOINCREMENT, so IDs keep counting and are never reused after a delete.

## Rarities and drop weight
Main rarities (configaration.RARITIES = name, emoji, weight %, note), only these drop normally:
☘️ Common 40, 🍀 Uncommon 25, 🧿 Rare 15, 💎 Premium 9, 🔮 Legendary 5, 🎗️ Mythic 3, 🎐 Celestial 2, 🦪 Divine 0.8, 🪐 Nexus 0.2.

Sub rarities / versions (configaration.SUB_RARITIES = name, emoji, note), a card has one main rarity plus an optional sub rarity (shown like "🔮 Legendary · 🐰 Bunny"):
🎃 Halloween, 🎬 AMV, 🧹 Maid, 🐰 Bunny, 🎄 Christmas, 💝 Valentine, ☀️ Summer, ❄️ Winter, 🎭 Cosplay, 🎒 School, 👘 Kimono, 🦇 Gothic, 🎤 Idol, 💍 Wedding, 🪄 Magical Girl, 🍑 Erotic, 👙 Swimsuit, 🌸 Spring, 🍁 Autumn, 🌧️ Rainy, 🧛 Vampire, 🏴‍☠️ Pirate, 👶 Chibi, 🥻 Traditional, 🌙 Moon, 🌳 Jungle, 👮 Police, 🎮 Gamer. Owner said "many more" will be added later.
Done in v0.9: characters.sub_rarity column + migration. Planned stats view "Character Count by Rarity" (format: ◈ emoji name - N character(s)).

Sub rarity rules (configaration.SUB_RULES, enforced in waifu.py by allowed_subs): Common and Uncommon cannot have a sub rarity; Nexus can only be AMV; every other rarity can use any sub rarity. /upload rejects a bad combination, the /edit sub-rarity picker only shows allowed ones, and changing the rarity in /edit clears a sub rarity that is no longer allowed.

## Workflow
1. Owner/admins upload characters with /upload (photo + series, name, rarity, sub rarity); file_id stored in DB and the card is posted to @otaku_db.
2. Bot counts messages per group; every N messages (default 100) it drops a weighted-random character with the name hidden.
3. First user to send /bind <name> correctly gets it (v0.10, see Drops section); drop is then locked. Unclaimed drop is replaced on next drop.
4. /harem shows the collection with pagination and filters; /fav, /info, /gift, /trade, /top.
5. Optional later: coins from duplicates, shop, daily reward, wishlist pings.

## Commands status
| Command | Who | Status |
|---|---|---|
| /start | all | Done (PM: rich message + start media block + coloured buttons, HTML fallback; group: same as PM) |
| /mynex | all | Done (rich message with HTML fallback) |
| /daily | all | Done (claim DAILY_AMOUNT Nex every DAILY_COOLDOWN seconds, atomic in dbase.claim_daily, users.last_daily) |
| /slot | all | Done (free 🎰 spin, no bet: Nex reward by match. SLOT_MAX_SPINS (8) spins per SLOT_WINDOW (24h); after the 8th spin a SLOT_LIMIT_COOLDOWN (12h) starts. In-memory state, resets on restart. Result is plain (title, reels, reward), no quote/balance) |
| /mines bet [mines] | all | Done (5x5 rich board edited in place, bet taken up front, cash out unlocks after 3 safe tiles, auto cash out after MINES_TIMEOUT idle) |
| /whoami | all (groups) | Done (v0.32 as /guess, renamed v0.33: spoiler picture, first correct name typed in chat within 30 s wins 5 Nex; see Changelog) |
| /topplayers | all | Done (v0.33, in harem.py since v0.34: top 10 by Nex, Collection or Waifus (most owned characters), toggle buttons, picture / video from /settplayer on top) |
| /settplayer | owner, co-owners | Done (v0.33: reply to a photo, video or GIF to set the /topplayers media) |
| /ngive amount (reply) | all | Done (sends Nex to the replied user; checks balance, no self/bot, asyncio lock, refund if credit fails) |
| /shop | all | Done (v0.30: every user gets their own random cards each day, no shared stock: 1 Rare, 1 Premium, 1 Legendary, plus 1 Mythic and 1 Celestial on Sunday. Rich list with picture on top, tap a card to see it, Buy then Confirm / Cancel, each card once per day. Only the sender can press the buttons; callback sh:<uid>:<m|i|c|b|x>[:<char_id>]) |
| /setshop | owner, co-owners | Done (v0.30: reply to a photo, video or GIF to set the /shop picture as a file_id; SHOP_BANNER URL is only the fallback) |
| /rarity | all | Done (compact rich table of rarities with symbol and drop chance; button toggles to a 3-per-row sub rarity table and Back; only the user who sent the command can toggle; edits the same message; HTML fallback) |
| /setstart | owner | Done (reply to photo, video or GIF) |
| /stats | owner | Done |
| /bind name | all (groups) | Done (v0.10: first correct name wins the live drop; full name or every guessed word of 3+ letters; wrong guesses are silent; result is a rich message with HTML fallback; Divine and Nexus get a bigger heading) |
| /setdrop [n \| on \| off] | group admins, owner, co-owners | Done (v0.10: no argument opens a panel with On/Off, interval buttons and Close; /setdrop 150 sets the interval, min DROP_MIN_EVERY, max DROP_MAX_EVERY; checked live with getChatMember, anonymous admins allowed) |
| /import | owner, co-owners | Done (v0.35: load an old .db or backup .zip, converted to the current layout, new backup pinned) |
| /dropnow | owner, co-owners | Done (forces a drop in this group, for testing) |
| /harem (alias /collection) | all | Done (v0.15: own collection, or reply to a user to see theirs; rich table ID / Character / Series / Qty, 10 per page, newest bind first; Prev / Next + rarity filter buttons (All + emoji of owned rarities), callback hm:<viewer>:<target>:<rarity idx or a>:<page>, only the sender can press; HTML fallback; empty collection shows a notice) |
| /info, /trade, /top | all | Planned |
| /upload | owner/admin | Done (reply to a photo or use it as the photo caption: `/upload Series \| Name \| Rarity \| Sub-rarity`; rarity by name or number 1-9, sub by name or `-`; saves the card and posts it to @otaku_db) |
| /botsadmins (also /botsadmin, /botadmins, /botadmin) | owner only (others silently ignored) | Done (rich message with HTML fallback; lists Owner, Co-owners (CO_OWNERS) and Admins (admins table) as mentions in blockquotes; names from users table, else getChat, else the ID) |
| /uploaded (or /chars) | owner/admin | Done (rich message with HTML fallback; toggle buttons List / Series / Rarity / Sub-rarity edit the same message; List = every character newest first, 15 per page; Series = character count per series, 20 per page; Rarity and Sub-rarity = count per item incl. zeros and 'None'; Prev/Next buttons, callback data ul:<view>:<page> with view list|series|rar|sub; old ul:<page> buttons still work) |
| /edit ID | owner/admin | Done (photo panel: Name, Series, Rarity, Sub-rarity, Picture, Delete with confirm, Close; text and picture edits are typed in after tapping, /cancel to stop; edits and deletes are mirrored in the @otaku_db post) |
| /addadmin, /rmadmin (reply) | owner | Done (admins table; admins can use /upload and /edit) |

## Rich message notes (Bot API 10.1 - 10.3)
- Method: sendRichMessage with rich_message = {markdown | html | blocks}. Declared in richmsg.py as a custom aiogram TelegramMethod because aiogram may not ship it yet.
- Rich Markdown used: # to ###### headings, tables with alignment, task lists (- [x]), > quotes, <aside><cite> pull quote, <details><summary>, ==highlight==, --- divider, media via ![](tg://video?id=lead) + media list.
- Start media (/setstart) is sent as a media block inside the rich message (photo, video, or animation). Media is declared in rich_message.media = [{id, media: InputMedia*}] and referenced in the markdown as ![](tg://video?id=lead) (Bot API 10.2). file_id is accepted.
- send_start order: rich + coloured buttons, rich + plain buttons, HTML + coloured, HTML + plain. Every failure is logged ("rich start failed").
- Coloured inline buttons: InlineKeyboardButton(style="primary" | "success" | "danger") (Bot API 10.3). Needs a recent aiogram; extra fields pass through.
- If Telegram rejects the rich message, otaku.py logs a warning and sends the HTML version. Older clients may show a "not supported" placeholder; set RICH_MESSAGES = False to disable.
- Limits: 32768 chars, 500 blocks, 20 table columns, 50 media.
- Payload checked offline only. Verify /start once on the live bot and check logs for "rich start failed".

## Economy (v0.29)
Income per user per day: /daily 40 Nex (80 with the bot's @username in the bio); /slot 8 spins per 24h, average 6.8 Nex per spin (about 55 a day, hard cap 800); /mines has no income (RTP 0.97); /ngive only moves Nex. Average about 135 Nex a day with the bio bonus (about 95 without), absolute maximum 880.
Slot rewards: jackpot 100, triple 40, two sevens 15, pair 3 (before: 500 / 150 / 50 / 10, daily 200 / 400).
Shop prices (configaration.SHOP_PRICES, v0.29.2, max 20 days of average income): Rare 300, Premium 700, Legendary 1,500, Mythic 2,200, Celestial 2,700 = about 2, 5, 11, 16 and 20 days at 135 Nex a day (up to 28 days without the bio bonus). Divine and Nexus are never sold. Shop day, restock and Sunday follow SHOP_UTC_OFFSET_MIN (330 = IST).

## Currency
Name: Nex, symbol: ⟐ (CURRENCY_NAME / CURRENCY_SYMBOL in configaration.py). Stored in users.nex (old users.coins is auto-renamed on startup). Helpers in dbase.py: get_nex(user_id), add_nex(user_id, amount). Earning/spending is not built yet.

## Drops (drop.py, v0.10)
- Counting: DropCounter is an outer middleware (registered after IgnoreOldMessages in otaku.py), so it never swallows a message. Counts text, photo, video, sticker, GIF, voice, audio, document, video note; skips commands and bots; one user counts once per DROP_MSG_GAP seconds. Counter lives in memory and is flushed to chats.msg_count every 10 messages.
- BotFather privacy mode must be DISABLED (or the bot must be admin), otherwise the bot never sees normal group messages and nothing is counted.
- Picking: weighted random main rarity (RARITIES), then a random card of it, falling back to other rarities if empty; never the same card twice in a row per chat. Event sub rarities (EVENT_SUBS) never drop unless listed in DROP_ACTIVE_EVENTS (for example ["Halloween"]).
- Drop message (v0.27): plain HTML (not rich), the card is sent with a spoiler. Caption: bold title "A Nᴇᴡ Cʜᴀʀᴀᴄᴛᴇʀ (<Rarity name>: <emoji>) Hᴀꜱ Aᴘᴘᴇᴀʀᴇᴅ !", a blockquote with a random quote, then the bold bind line "/bind<i> Nᴀᴍᴇ Aɴᴅ Mᴀᴋᴇ Mᴇ Yᴏᴜʀꜱ Bᴇꜰᴏʀᴇ I Vᴀɴɪꜱʜ</i>". All text uses title-style small caps (drop.small_caps: first letter of each word capital, rest small caps); "/bind" and the rarity (name: emoji) are not converted. The quote is picked at random from DROP_EXAMPLE_QUOTE plus the first line of every DROP_QUOTES pair (the closing line of each pair is no longer used). Sub rarity stays hidden; no series, no name blanks.
- Unclaimed drop: when the next drop arrives the old message is edited to "<name> slipped away". A bound drop message is NOT edited (v0.11).
- Bind result (rich message with HTML fallback, v0.26): "Congratulations, <mention>!" heading, "You have bound this character", divider, Name / Rarity / Series / ID / Time Taken, divider, H6 footer (new entry or duplicate line, plus the DROP_DUP_NEX bonus if set). BIG_RARITIES get a larger heading.
- Claim is atomic (dbase.claim_drop deletes the drops row and checks rowcount), so only the first correct /bind wins.
- Sub rarity display in drop results: emoji only in round brackets, for example "Makima (🎃)" (drop.name_tag). Main rarity is shown with name and emoji.
- Config (configaration.py): DROP_EVERY, DROP_MIN_EVERY (30), DROP_MAX_EVERY (1000), DROP_PRESETS, DROP_MSG_GAP (3), DROP_QUOTES, DROP_DUP_NEX (0 = off), BIG_RARITIES, EVENT_SUBS, DROP_ACTIVE_EVENTS.
- dbase.py additions: get_chat, set_chat_drop, set_chat_count, pick_character, get_drop, swap_drop, claim_drop, add_to_collection.

## Inline mode (now inside drop.py, v0.11, redesigned v0.12)
- BotFather: /setinline (inline mode, placeholder for example "Search characters") and /setinlinefeedback set to 100% (needed for the spoiler).
- Empty query: all characters, newest first, 50 per page (scroll loads the next page through next_offset). Text filters name, series, rarity, sub rarity; a number also matches the character ID.
- Result caption (HTML): "OMG !! Check out This Character !", then Name (sub rarity emoji in round brackets), Rarity (emoji and name), Global ID. No series line. Button "Check binds" (callback bn:<char_id>).
- Spoiler: Telegram has no spoiler flag for inline results, so inline.spoiler_cb listens to chosen_inline_result and edits the sent inline message with InputMedia(has_spoiler=True) (retries at 0.4, 1.2, 2.5 s). The picture shows unspoilered for a moment before the edit. Without inline feedback the card just stays unspoilered.
- Collection view: query "collection.<user id>" (+ optional filter text) returns that user's characters (dbase.search_collection, cache 3 s); plain "collection" = the asker's own (personal). Used by the View Collection button.
- Check binds: private alert with the top 10 owners (most copies first, then earliest bind), as "1. Name xQty". Telegram alerts hold 200 characters, so names are trimmed to 14 characters and lines are dropped if they do not fit.
- Media: photo -> cached photo result, video -> cached video, animation -> cached mpeg4 gif. Results cached for 30 seconds.
- dbase: search_characters(query, offset, limit), top_holders(char_id, limit).

## Changelog
- v0.35: /import (backup.py, owner and co-owners): send an old .db (or a backup .zip) with /import as the caption, or reply to the file with /import. The file is checked (SQLite header, quick_check, bot tables), the live database is closed and kept as <db>.bak, the old file replaces it, db.init_db opens it and converts the old layout (migrations: coins -> nex, missing columns and tables), then a fresh backup is sent and pinned; if the conversion fails the previous database is put back. Backups are paused during the swap and a failed-restore pause is cleared on success. When startup finds no pinned backup and no local database, the owner gets a message to use /import. Limit 20 MB (Telegram download limit).
- v0.34: Files merged. /topplayers and /settplayer moved into harem.py (topplayers.py removed); /whoami moved into minigame.py (guess.py removed; drop.py and waifu.py import minigame, so minigame imports their helpers inside the functions). otaku.py no longer imports or includes guess or topplayers. /topplayers now has three toggle buttons: Nex (richest), Collection (most unique characters) and Waifus (characters owned by the most people, with the owner count). callback tp:<uid>:<nex|col|wai>. dbase: top_waifus. Delete guess.py, topplayers.py and inline.py from the project.
- v0.33: Added /topplayers leaderboard (topplayers.py): rich message with the /settplayer picture / video / GIF on top and a table of the top TOPPLAYERS_COUNT (10) players, toggle buttons Nex (richest, default) and Collection (most unique characters, copies shown too); same message edited in place, only the sender can press (callback tp:<uid>:<nex|col>); fallbacks rich + media, rich, HTML media caption, HTML text, each with coloured then plain buttons. New owner / co-owner command /settplayer (reply to a photo / video / GIF, stored in settings topplayers_file_id / topplayers_type). dbase: top_nex, top_collectors. Config: TOPPLAYERS_COUNT. otaku.py includes topplayers.router. The /guess command is renamed /whoami (guess.py, same rules; config names stay GUESS_*).
- v0.32: Added /guess (guess.py, groups only). Sends a random character picture with a spoiler (drop.pick_drop, so event subs never appear); the first person to type the name in the chat within GUESS_SECONDS (30) wins GUESS_REWARD (5 Nex). Name matching is drop.name_matches; wrong guesses are ignored and pass through to other handlers. One game per chat, GUESS_COOLDOWN (60 s) per chat between games, GUESS_MAX_WINS (10) paid wins per user per 24 h (in memory, max 50 Nex a day). The picture caption is edited to show the answer on a win or timeout. Needs BotFather privacy mode disabled (plain text guesses). New file guess.py (separate from minigame.py because drop.py imports minigame); configaration.py: GUESS_*; otaku.py includes guess.router.
- v0.31: Database backup (backup.py). Every BACKUP_EVERY (15 min) the DB is snapshotted (sqlite backup API), zipped and sent to @otaku_file (config BACKUP_GROUP), pinned, then the previous pinned backup is unpinned and deleted (new one is sent and pinned first). On startup, before db.init_db(), backup.restore reads the group's pinned zip and restores it if it is newer than the local DB (or there is no local DB); if a restore fails, backups are paused so the good backup is never overwritten. New owner / co-owner command /backup (backup now). Bot must be admin in @otaku_file with Pin + Delete rights. Telegram limits: download 20 MB, upload 50 MB. otaku.py: creates the bot, restores, then init_db, starts backup_loop, includes backup.router. New file backup.py; configaration.py: BACKUP_GROUP, BACKUP_EVERY.
- v0.30.1: /shop list restyled: one block per card (name, rarity, price) with --- dividers, no ID / status columns (also removed from the card view), footer "Shop will reset at midnight UTC" (SHOP_UTC_OFFSET_MIN now 0), buttons show only the card name, a bought card's button has no colour, Close is danger (red).
- v0.30: /shop reworked. No shared stock any more: every user gets their own random cards per day (SHOP_SLOTS = Rare 1, Premium 1, Legendary 1; SHOP_SUNDAY_SLOTS = Mythic 1, Celestial 1), unlimited buyers, each card once per day. dbase: tables shop_stock and shop_buys replaced by shop_offers(day, user_id, char_id, rarity, price, bought); shop_* helpers take user_id; shop_buy returns (status, balance). Picture fix: new owner/co-owner command /setshop (reply to photo / video / GIF) stores a file_id in settings (shop_file_id, shop_type), used before the SHOP_BANNER URL (ibb links are often refused by Telegram).
- v0.29.3: Added sub rarities Police 👮 and Gamer 🎮 (28 sub rarities in total). Allowed on every rarity except Common, Uncommon and Nexus, like the others (SUB_RULES unchanged).
- v0.29.2: Shop prices lowered so the most expensive card (Celestial) takes at most 20 days of average income: Rare 1,200 -> 300, Premium 3,000 -> 700, Legendary 7,500 -> 1,500, Mythic 20,000 -> 2,200, Celestial 45,000 -> 2,700.
- v0.29.1: /shop shows a banner picture (configaration.SHOP_BANNER, a direct image URL) as a media block on top of the list; falls back to no picture / HTML photo caption if Telegram rejects it.
- v0.29: Added /shop (shop.py). New tables shop_stock and shop_buys, dbase.shop_* helpers and pick_characters (atomic shop_buy under nex_lock). Config: SHOP_PRICES, SHOP_SLOTS (Rare 3 cards x5, Premium 2 x3, Legendary 1 x2), SHOP_SUNDAY_SLOTS (Mythic 1, Celestial 1, one copy each), SHOP_UTC_OFFSET_MIN. Rewards reduced: DAILY_AMOUNT 200 -> 40, DAILY_BIO_AMOUNT 400 -> 80, slot jackpot 500 -> 100, triple 150 -> 40, two sevens 50 -> 15, pair 10 -> 3. otaku.py imports shop and includes its router. shop.py imports edit_card, send_card_rich, sub_emoji from drop.py and fmt_wait, notice, topic_id from minigame.py.
- v0.28.1: /harem: the highlighted favorite also gets a ✨ before its name (✨ Name (sub emoji) - Rarity) (rich and fallback).
- v0.28: /harem no longer prints the "Favorite : ..." line; the /fav character is highlighted inside its series list instead (==highlight== in the rich message, bold in the HTML fallback). The cover picture is still the /fav card.
- v0.27.1: Drop title shows the rarity name and emoji, for example "(Legendary: 🔮)", in normal font.
- v0.27: Drop message restyled (drop.py drop_caption): small-caps title with the rarity emoji, small-caps quote in a blockquote, small-caps bind line (see Drops section). New helper drop.small_caps, constants DROP_RARITY_EMO and DROP_EXAMPLE_QUOTE. Only the first line of each DROP_QUOTES pair is used now.
- v0.26: Bind result redesigned (drop.py result_md / result_html): heading "Congratulations, <mention>!" (H1 for BIG_RARITIES, else H2), "You have bound this character", divider, Name (sub emoji) / Rarity / Series / ID / Time Taken, divider, H6 footer ("Welcome to the family. Keep collecting!" for a new entry, "Copy xN. Your bond grows stronger!" (+ Nex bonus) for a duplicate). No pull quote, no "Otaku Nexus" cite. CHEERS and _story removed. HTML fallback has the same fields with ────────── dividers.
- v0.25: /harem always shows the command user's own harem; replying to another user no longer opens theirs (harem.py harem_cmd: target = viewer). The hm: callback and the View Collection button are unchanged.
- v0.24: /fav and /gift cards (drop.py) now show `Name (sub emoji) - 🔮 Rarity` plus the ID, with no quote: card_aside no longer uses <aside>, card_text and finish_card no longer use <blockquote> (so "Favorite updated", "Gift sent", "Gift failed" etc. are plain). New helper drop.card_line(c). The "Gift sent" line also shows the rarity: "<user> gifted **Name (🐰) - 🔮 Legendary** to <user>".
- v0.23: /harem layout changes. Every series section now ends with a `---` divider. Character line format is `ID. Name (sub emoji) - rarity emoji Rarity` (for example `12. Makima (🐰) - 🔮 Legendary`; no brackets when there is no sub rarity; ` · xN` still added for 2+ copies). Favorite line uses the same format. Removed the totals blockquote ("N unique · N copies · Page x/y"). Footer is an H6 heading `Page x/y` (also in the HTML fallback caption). Smart nav buttons: shown only with 2+ pages, no Prev on page 1, no Next on the last page, middle `x/y` button is a no-op (hm:noop); View Collection button always shown. harem.py: quality() replaced by card_line() (plain / HTML) and card_line_md() (Rich Markdown); name_tag import removed.
- v0.22: /harem is grouped by series (anime) with collected/total per series, numbered list with the real ID, Name · Rarity · Quality per line; aside pull quote replaced by a normal blockquote; rarity filter buttons removed. dbase: collection_anime_count, collection_anime_page, collection_by_animes (series matched case-insensitively and trimmed). Old collection_page / collection_count / collection_rarities are unused now.
- v0.21: richmsg.py: shared _payload helper; edit_rich(..., media=None, media_below=False) re-sends the media block so edits keep the picture. drop.edit_card falls back to send-new + delete-old if the in-place edit fails.
- v0.20: /harem, /fav and /gift confirmation are rich messages with the character picture as a media block on top (richmsg.send_rich media=(file_id, type)), no table; fallback photo + HTML caption, then text. Shared helpers in drop.py: send_card_rich, edit_card, finish_card, reply_card (fallback), card_text, card_aside, mention_md. richmsg.edit_rich now takes media=(file_id, kind) (and media_below) so the picture stays when a card is edited in place; if Telegram refuses that edit, the new version is sent and the old one deleted. dbase.init_db migrates users.favorite_id (older databases lacked it, which made /harem and /fav answer "Something went wrong").
- v0.19: inline.py, fav.py and gift.py merged into drop.py (one router). harem.py now imports reply_card, name_tag and collection_btn from drop. otaku.py no longer imports or includes inline, fav, gift. Delete inline.py, fav.py, gift.py from the project.
- v0.18: /harem now sends a picture (favorite, else first character) with the list as caption, table removed (no rich message any more). New /fav ID and /gift ID (reply), both with Confirm / Cancel (fav.py, gift.py). View Collection button now opens inline query "collection.<user id>" (bind result: the binder's collection; /harem: the harem owner's collection); plain "collection" still means the asker's own. dbase: owned_qty, get_favorite, set_favorite, transfer_character; collection_page also returns file_id and media_type. harem.reply_card is the shared helper for sending a card with a caption. otaku.py includes fav and gift routers.
- v0.17: "View Collection" button (switch_inline_query_current_chat = "collection") under the bind result and in /harem. Inline mode: query "collection" (optionally followed by a filter) lists the pressing user's own characters, personal results, copies shown as xN; empty collection shows a "Nothing here yet" result. drop.py: send_result (own rich send with the button, replaces rich_reply_to), collection_btn, COLLECTION_QUERY. dbase.search_collection added.
- v0.16: Bind result (rich) no longer has the Rarity / ID / Speed / Copies table; it is now heading + pull quote only.
- v0.15: Added /harem (alias /collection) in new harem.py. dbase.py: collection_count, collection_stats, collection_rarities, collection_page. otaku.py: imports harem and includes its router.
- v0.14: Drop message shows the character's rarity in bold under the quote; /bind name is plain text instead of a code tag.
- v0.13: Drop card is sent with a spoiler. Drop message now a random in-character quote (DROP_QUOTES, 12 included) plus the bind command. Removed the Hint button, its callback and DROP_HINT_COST.
- v0.12: Inline UI redesigned (OMG caption, spoiler via chosen_inline_result, Check binds top 10 button). Sub rarity now shown in round brackets: Makima (🎃). dbase.top_holders added.
- v0.11: Drop message made short and anime-style. Bind result made joyful (rarity cheers). Drop message no longer edited after a bind. Added inline mode (inline.py + dbase.search_characters), registered in otaku.py.
- v0.10: Added drops. New drop.py (counter middleware, plain HTML drop message with hidden name, /bind, /setdrop panel, /dropnow, Hint button). dbase.py: chats.drop_enabled (+ migration) and drop helpers. configaration.py: drop settings. otaku.py: imports drop, registers DropCounter and drop.router. Bind result buttons (Harem / Info) wait for /harem and /info.
- v0.9.11: /upload and /edit now accept video and GIF as well as photos (waifu.py: media_of, media_kind, input_media, send_media). New column characters.media_type ('photo' | 'video' | 'animation', default 'photo', auto-migrated; old cards stay photo). dbase.add_character takes media_type; media_type is in CHAR_FIELDS. Same for the @otaku_db post and the /edit picture swap. For /grab and drops, send cards with waifu.send_media(target, c['media_type'], c['file_id'], caption).
- v0.9.10: Added sub rarity Jungle 🌳 (26 sub rarities in total).
- v0.9.9: Added sub rarity Moon 🌙 (25 sub rarities in total).
- v0.9.8: Removed sub rarity New Year 🎆 (24 sub rarities in total). Cards already saved with it keep the value until edited.
- v0.9.7: Added sub rarity Traditional 🥻 (25 total). Added SUB_RULES in configaration.py: Common and Uncommon have no sub rarity, Nexus is AMV only. waifu.py: allowed_subs / sub_rule_text helpers; /upload validates, /edit sub picker filtered, rarity change clears an invalid sub rarity.
- v0.9.6: /botsadmins is owner only (OWNER_ID); co-owners, admins and everyone else are silently ignored.
- v0.9.5: /botsadmins is now a rich message (headings, quotes with user mention links) with HTML fallback. waifu.py now uses richmsg.send_rich / edit_rich / esc; its own SendRich / EditRich methods from v0.9.4 are removed.
- v0.9.4: /uploaded (alias /chars) is now a rich message with a toggle: List, Series, Rarity, Sub-rarity (same message edited in place, active tab is primary coloured, HTML fallback, RICH_MESSAGES flag respected). Counts are computed in waifu.py from db.list_characters, no dbase.py change. waifu.py now defines its own small SendRich / EditRich methods (sendRichMessage, editMessageText + rich_message) (replaced in v0.9.5).
- v0.9.3: /editwaifu renamed to /edit. Edit panel (caption, delete confirm, deleted notice) no longer uses blockquote. Tapping Name / Series / Picture no longer shows an alert: the panel itself changes to "Send the new ... now" with a Back button (/cancel also restores the panel). Fixed /botsadmins crash: admins table is WITHOUT ROWID, so dbase.list_admins orders by user_id (not rowid).
- v0.9.2: @otaku_db post caption is now fully bold, with the original layout kept (no hashtag series). The picture in the @otaku_db post is sent as a spoiler (has_spoiler=True, also when the picture is swapped from /edit); the caption is not hidden. Added /uploaded (alias /chars) in waifu.py for owner/admin: paginated list of all characters. Added /botsadmins. dbase.py: count_characters(), list_characters(offset, limit), list_admins(), get_user_name(id).
- v0.9.1: Added CO_OWNERS (4 IDs) to configaration.py with owner rights in waifu.py. Removed the husbando option from /upload and the Waifu/Husbando toggle from the edit panel; uploads are always stored as waifu.
- v0.9: Added waifu.py (own router). /upload (admin) saves a card and posts it with the picture to @otaku_db, caption: #NEWADDED, SERIES, NAME, RARITY, SUB-RARITY, ID, "~ Artist 🎨 : mention" (artist = uploader). /edit ID opens one edit panel (name, series, rarity, sub-rarity, picture, type, delete with confirm); callback data ew:<action>:<id>[:<value>]. Changes sync to the @otaku_db post. dbase.py: characters.sub_rarity / artist_name / db_msg_id (auto migration), admins table, is_admin / add_admin / del_admin, add_character / get_character / update_character / delete_character. configaration.py: DB_GROUP. otaku.py: imports waifu and includes its router. Panel pending input is in memory (lost on restart).
- v0.8.8: /rarity made compact (symbol and name in one cell, sub rarities 3 per row) and got a toggle button: Sub rarities / Back, callback data rar:<main|sub>:<user id>, same message edited in place.
- v0.8.7: /slot now allows 8 spins per 24h window; after the 8th spin there is a 12h cooldown (config: SLOT_MAX_SPINS, SLOT_WINDOW, SLOT_LIMIT_COOLDOWN replace SLOT_COOLDOWN). Removed the quote (balance and name) from the slot result, in both rich and HTML versions. Spin is given back if the dice or the reward fails.
- v0.8.6: removed sub rarities Cyberpunk, Samurai, Fantasy, Birthday, Angel, Demon, Gamer; Chibi emoji is now 👶. 24 sub rarities in total.
- v0.8.5: added 16 sub rarities (Erotic 🍑, Swimsuit, Spring, Autumn, Rainy, Birthday, New Year, Angel, Demon, Vampire, Fantasy, Samurai, Pirate, Cyberpunk, Gamer, Chibi). 31 sub rarities in total.
- v0.8.4: added /rarity (otaku.py): rich message with a rarity table (symbol, name, drop chance) and a sub rarity table (symbol, version), read from RARITIES / SUB_RARITIES; HTML fallback.
- v0.8.3: sub rarity emojis changed: Maid 🧹, Valentine 💝.
- v0.8.2: rarities replaced. Main: Common, Uncommon, Rare, Premium, Legendary, Mythic, Celestial, Divine, Nexus (with emojis and new weights). Added SUB_RARITIES (Halloween, AMV, Maid, Bunny and more). RARITIES tuples are now (name, emoji, weight, note); nothing in the .py files read RARITIES yet.
- v0.8.1: common.py merged into minigame.py (helpers notice, rich_reply_to, topic_id, fmt_wait now live there; otaku.py imports fmt_wait from minigame). common.py removed.
- v0.8: minegame.py renamed to minigame.py and /slot moved into it (slot.py removed). /slot is now a free spin with no bet that pays a fixed Nex reward per match (jackpot 500, triple 150, two sevens 50, pair 10), limited by SLOT_COOLDOWN (default 1 hour per user, in memory). fmt_wait moved to common.py.
- v0.7: /daily now 200 Nex (DAILY_AMOUNT), 400 (DAILY_BIO_AMOUNT) when @botusername is in the user's bio (getChat bio; falls back to 200 if Telegram doesn't return it). Added /slot (slot.py) and common.py (shared rich reply helpers; minegame.py now imports them).
- v0.6.1: Text inside <aside> (rich Markdown pull quote) is HTML, not Markdown: use html.escape there, never esc() (it showed literal backslashes like 1\.10x). Mines: unopened tiles are blank (MINES_ICON_HIDDEN, a Braille blank because button text can't be empty); opened safe tiles show 🪎 in success colour.
- v0.6: Mines board is now a rich message edited in place via editMessageText + rich_message (richmsg.edit_rich); no extra result messages, the board itself shows Boom / Cashed out / Timed out. Tiles: hidden 🪎 no colour, opened safe tile 🪎 success, bomb 💣 danger, hit bomb 💥 danger. Cash out button is danger until MINES_MIN_CASHOUT (3) tiles are open, then success; idle timeout with fewer tiles returns the bet. Falls back to an HTML board if rich fails, and to plain buttons if coloured ones are rejected.
- v0.5.1: Mines moved out of otaku.py into minegame.py (own router, own small rich-reply helpers). Shared Nex lock now lives in dbase.nex_lock (used by /ngive and mines).
- v0.5: Added /daily (users.last_daily column + migration, dbase.claim_daily) and /mines (inline 5x5 board, multiplier = MINES_RTP / chance of surviving, bet deducted at start, cash out or auto cash out on idle). Nex symbol is now defined only by CURRENCY_SYMBOL (⟐). /stats, /setstart and notices now go through the rich notice() helper with HTML fallback. The live mines board stays HTML (needs editing); results are rich replies. Games live in memory: a restart mid-game forfeits the bet.
- v0.4.7: richmsg.send_rich now accepts reply_to_message_id (sent as reply_parameters) so rich replies thread to the command.
- v0.4.6: /ngive replies (success and errors) are rich messages with HTML fallback via new reply_rich(m, md, html) helper; success uses a pull quote with the sender's new balance.
- v0.4.5: All command replies now reply to the user's command message (m.reply, reply_photo/video/animation; rich via local send_rich(m, ...) wrapper passing reply_to_message_id). Group /start now sends the same start reply as PM (rich + media + buttons, HTML fallback). richmsg.send_rich needs a reply_to_message_id kwarg (-> reply_parameters) for rich replies to thread.
- v0.4.4: Added /ngive amount (reply to a user) to send Nex. Built on db.get_nex / db.add_nex with a lock; a real atomic db.transfer_nex in dbase.py is a good later upgrade.
- v0.4.3: PM /start is a rich message again, including the /setstart video / GIF / photo as a media block (it had stayed HTML since v0.4, so rich never applied). Falls back to the HTML caption version if Telegram rejects it. CAPTION_ABOVE_MEDIA now puts the media block after the text. richmsg.send_rich got media_below.
- v0.4.2: Group /start is a rich message again (HTML fallback, failures logged with the Telegram error). richmsg.py now passes message_thread_id so rich replies land in the right forum topic; /mynex does the same. PM /start stays HTML.
- v0.4.1: /mynex is a rich message again (HTML fallback). /start is one handler for PM and groups with try/except + logging and a last-resort reply, so it never stays silent in groups. RICH_MESSAGES flag restored.
- v0.4: Converted /start (PM + group) to HTML parse mode, removed rich message code and RICH_MESSAGES flag. Added Nex currency, /mynex command, users.nex column with migration from coins.
- v0.3.1: /start shortened. Removed How it works, Rarities table, What you get and Good to know (rich + HTML fallback). Kept title, pull quote, welcome line, footer, buttons.
- v0.3: /start rebuilt as a native rich message (now short: H1/H2 title, pull quote, welcome line, H6 footer, coloured buttons). Removed commands list from /start. Added richmsg.py, RICH_MESSAGES flag, rarity notes in config. HTML fallback kept.
- v0.2: Renamed files (otaku.py, dbase.py, configaration.py). Fewer emojis. Added /setstart (start photo/video/GIF, shown as rich caption, optional caption above media). Added settings table. Added claude.md.
- v0.1: Base project, SQLite schema with speed pragmas, /start with rich headings and quotes, /stats.

## Next steps
0. Test rich /start on the live bot and fix any schema rejections (see logs).
1. (done v0.9) Character upload + edit panel. Test /upload and /edit on the live bot, check logs for "post to @otaku_db failed".
2. (done v0.10) Group message counter + drop logic. Test on the live bot: disable privacy mode, run /setdrop 30, /dropnow, then /bind.
3. (done v0.10) /bind with name matching.
4. (done v0.15) /harem with pagination. Done v0.18: /fav and /gift. Next: /info <id>, then /trade and /top.
