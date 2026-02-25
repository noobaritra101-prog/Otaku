import logging
import math
import asyncio
import asyncpg
import re
import hashlib
import time
import sympy as sp
from datetime import datetime
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent
)
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ================= CONFIG =================

TOKEN = "8368723938:AAGRp6Qw0m1hhuXFoCMm_EsweIyhoRAhh84"
DATABASE_URL = "postgresql://axb:h_9dhhH5KF_5c0xumLMziA@axb-bots-12453.jxf.gcp-asia-south1.cockroachlabs.cloud:26257/defaultdb?sslmode=require"
ADMIN_IDS = [5716292610, 7708811819]

logging.basicConfig(level=logging.INFO)

db_pool = None
START_TIME = datetime.now()

# Memory cache to speed up the bot
known_chats = set()

# ================= RATE LIMITER MIDDLEWARE =================

class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 1.5):
        self.limit = limit
        self.users = {}

    async def __call__(self, handler, event: Message, data: dict):
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)
            
        user_id = event.from_user.id
        if user_id in ADMIN_IDS:
            return await handler(event, data)

        current_time = time.time()
        if user_id in self.users:
            time_passed = current_time - self.users[user_id]
            if time_passed < self.limit:
                return 
        
        self.users[user_id] = current_time
        return await handler(event, data)

# ================= DATABASE =================

async def create_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)

async def init_db():
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id BIGINT PRIMARY KEY,
                chat_type TEXT,
                first_name TEXT,
                date_added TEXT
            )
        """)
        rows = await conn.fetch("SELECT chat_id FROM chats")
        for row in rows:
            known_chats.add(row['chat_id'])

async def add_chat(chat_id, chat_type, name):
    if chat_id in known_chats:
        return
    
    known_chats.add(chat_id)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO chats (chat_id, chat_type, first_name, date_added)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id) DO NOTHING
        """, chat_id, chat_type, name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

async def get_stats_data():
    async with db_pool.acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM chats WHERE chat_type = 'private'")
        groups = await conn.fetchval("SELECT COUNT(*) FROM chats WHERE chat_type != 'private'")
        return users, groups

async def get_all_groups():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT chat_id FROM chats WHERE chat_type != 'private'")

async def get_all_users():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT chat_id FROM chats WHERE chat_type = 'private'")

async def get_group_names():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT first_name FROM chats WHERE chat_type != 'private'")

# ================= MATH LOGIC & FILTERS =================

def is_math_expression(text):
    text = text.lower()
    # Added trig functions and 'factorial' to the valid words list
    valid_words = ['sin', 'cos', 'tan', 'sec', 'csc', 'cot', 'log', 'ln', 'sqrt', 'abs', 'round', 'pi', 'e', 'factorial']
    for word in valid_words:
        text = re.sub(r'\b' + word + r'\b', '', text)
        
    text = text.replace('x', '')
    # Added ! to the allowed math symbols
    return bool(re.fullmatch(r'[\d\.\+\-\*\/\^\(\)\s\%\=,\!]+', text))

def safe_calculate(expression):
    expression = expression.replace(",", "")
    expression = expression.replace("^", "**").replace("×", "*").replace("÷", "/")

    if re.fullmatch(r"-?\d+(\.\d+)?", expression.strip()):
        return None

    if len(expression) > 100:
        return None

    # FACTORIAL SAFETY CHECK: Block any factorial larger than 150!
    if "!" in expression:
        factorials = re.findall(r'(\d+)!', expression)
        for num in factorials:
            if int(num) > 150:
                return "⚠️ Number too large for factorial! Keep it under 150."
        
        # Translate 7! into factorial(7) for Python to read
        expression = re.sub(r'(\d+)!', r'factorial(\1)', expression)

    if expression.count("**") > 1:
        return "∞"
    if "**" in expression:
        parts = expression.split("**")
        if any(len(p.strip()) > 3 for p in parts):
            return "∞"

    safe_dict = {k: v for k, v in math.__dict__.items() if not k.startswith("__")}
    safe_dict["abs"] = abs
    safe_dict["round"] = round

    try:
        result = eval(expression, {"__builtins__": {}}, safe_dict)
        return str(result)
    except OverflowError:
        return "∞"
    except:
        return None

def do_calculus(expression, operation):
    if len(expression) > 100:
        return "⚠️ Expression too long! Keep it under 100 characters."
    if expression.count("^") > 3 or expression.count("**") > 3:
        return "⚠️ Too many exponents! My server doesn't have enough RAM for that."

    try:
        x = sp.Symbol('x')
        expr = sp.sympify(expression.replace("^", "**"))
        
        if operation == 'diff':
            result = sp.diff(expr, x)
        elif operation == 'int':
            result = sp.integrate(expr, x)
            
        return str(result).replace("**", "^")
    except Exception:
        return "⚠️ Calculus Error: Check your formatting. Example: sin(x) + x^2"

def solve_equation(expression):
    if len(expression) > 100:
        return None
    if expression.count("^") > 3 or expression.count("**") > 3:
        return "⚠️ Equation too complex! I can't solve that many exponents."

    try:
        expression = expression.replace("^", "**").replace("×", "*").replace("÷", "/")
        if "=" not in expression:
            return None
            
        left_side, right_side = expression.split("=", 1)
        x = sp.Symbol('x')
        
        left_expr = sp.sympify(left_side)
        right_expr = sp.sympify(right_side)
        
        equation = sp.Eq(left_expr, right_expr)
        solutions = sp.solve(equation, x)
        
        if not solutions:
            return "No solution found."
            
        result_str = ", ".join([str(sol).replace("**", "^") for sol in solutions])
        return f"x = {result_str}"
        
    except Exception:
        return None

# ================= COLORED BUTTONS =================

def stats_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Refresh",
                    callback_data="refresh_stats",
                    api_kwargs={"style": "primary"}  # 🔵 Blue
                )
            ]
        ]
    )

def start_keyboard(bot_username):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Add To Group",
                    url=f"https://t.me/{bot_username}?startgroup=true",
                    style="success"   # 🟢 Green
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Support",
                    url="https://t.me/axbsupport",
                    style="primary"   # 🔵 Blue
                ),
                InlineKeyboardButton(
                    text="🏢 HQ",
                    url="https://t.me/your_channel",
                    style="primary"   # 🔵 Blue
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👑 Owner",
                    url="https://t.me/axbowners",
                    style="danger"    # 🔴 Red
                )
            ]
        ]
    )

# ================= HANDLERS =================

async def start_handler(message: Message, bot: Bot):
    asyncio.create_task(add_chat(message.chat.id, message.chat.type, message.chat.title or message.from_user.first_name))

    text = (
        f"Hҽყ {message.from_user.first_name}\n"
        "Wᴇʟᴄσɱᴇ ᴛσ Tʜᴇ Cᴀʟᴄᴜʟᴀᴛᴏʀ Bᴏᴛ\n\n"
        "⚡ Pᴏᴡᴇʀᴇᴅ ʙʏ AｘB Bᴏᴛs"
    )
    me = await bot.get_me()
    await message.answer(text, reply_markup=start_keyboard(me.username))

async def stats_handler(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    users, groups = await get_stats_data()
    uptime = datetime.now() - START_TIME
    uptime_str = str(uptime).split('.')[0]

    text = (
        "📊 <b>System Status</b>\n"
        "──────────────\n"
        f"⏱ Uptime: {uptime_str}\n"
        f"👥 Users: {users}\n"
        f"🌐 Groups: {groups}\n"
        f"🧠 Cached Chats: {len(known_chats)}"
    )
    await message.answer(text, reply_markup=stats_keyboard())

async def refresh_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Not Admin", show_alert=True)
        return

    users, groups = await get_stats_data()
    uptime = datetime.now() - START_TIME
    uptime_str = str(uptime).split('.')[0]

    text = (
        "📊 <b>System Status</b>\n"
        "──────────────\n"
        f"⏱ Uptime: {uptime_str}\n"
        f"👥 Users: {users}\n"
        f"🌐 Groups: {groups}\n"
        f"🧠 Cached Chats: {len(known_chats)}"
    )
    try:
        await callback.message.edit_text(text, reply_markup=stats_keyboard())
        await callback.answer("Updated")
    except:
        await callback.answer("Already up to date!", show_alert=False)

# --- Group Greetings & Info ---

async def group_info(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    groups = await get_group_names()
    if not groups:
        await message.answer("⚠️ <b>No groups found in the database.</b>")
        return

    text = (
        "━━━━━━━━━━━━━━━━\n"
        "📂 <b>AxB Calculator | Group List</b>\n"
        "━━━━━━━━━━━━━━━━\n\n"
    )
    
    for i, row in enumerate(groups, 1):
        name = str(row['first_name']).replace("<", "&lt;").replace(">", "&gt;")
        text += f"<b>{i}.</b> {name}\n"

    if len(text) > 4000:
        text = text[:3900] + "\n\n... <i>(List truncated)</i>"

    text += "\n━━━━━━━━━━━━━━━━"
    await message.answer(text)

async def on_bot_added(message: Message, bot: Bot):
    for new_member in message.new_chat_members:
        if new_member.id == bot.id:
            await add_chat(message.chat.id, message.chat.type, message.chat.title)
            text = (
                "━━━━━━━━━━━━━━━━\n"
                f"👋 <b>Hello, {message.chat.title}!</b>\n"
                "━━━━━━━━━━━━━━━━\n"
                f"Thanks to <a href='tg://user?id={message.from_user.id}'>{message.from_user.first_name}</a> for adding me! ⚡️\n\n"
                "🧮 <b>What I can do:</b>\n"
                "➤ <b>Basic Math:</b> Just type `50 * 12` or `7!`\n"
                "➤ <b>Algebra:</b> Type `x^2 = 16`\n"
                "➤ <b>Calculus:</b> Use `/int sin(x)` or `/diff x^2`\n\n"
                "<i>Just send your math problems directly in the chat!</i>\n"
                "━━━━━━━━━━━━━━━━"
            )
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="💬 Support", url="https://t.me/axbsupport"),
                        InlineKeyboardButton(text="🏢 Updates", url="https://t.me/your_channel")
                    ]
                ]
            )
            await message.answer(text, reply_markup=markup)
            return

# --- Calculus & Algebra Handlers ---

async def integrate_handler(message: Message):
    asyncio.create_task(add_chat(message.chat.id, message.chat.type, message.chat.title or message.from_user.first_name))
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Please provide an expression. Example: `/int sin(x)`")
        return
    result = await asyncio.to_thread(do_calculus, args[1], 'int')
    await message.answer(f"∫({args[1]}) dx\n\n= <b>{result}</b>")

async def differentiate_handler(message: Message):
    asyncio.create_task(add_chat(message.chat.id, message.chat.type, message.chat.title or message.from_user.first_name))
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Please provide an expression. Example: `/diff cos(x)`")
        return
    result = await asyncio.to_thread(do_calculus, args[1], 'diff')
    await message.answer(f"d/dx ({args[1]})\n\n= <b>{result}</b>")

async def solve_handler(message: Message):
    asyncio.create_task(add_chat(message.chat.id, message.chat.type, message.chat.title or message.from_user.first_name))
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Please provide an equation. Example: `/solve 2*x + 10 = 20`")
        return
    result = await asyncio.to_thread(solve_equation, args[1].lower())
    if result:
        await message.answer(f"📝 {args[1]}\n\n👉 <b>{result}</b>")

# --- Broadcast Handlers ---

async def safe_broadcast(bot: Bot, chat_ids: list, from_chat_id: int, message_id: int):
    count = 0
    batch_size = 25
    for i in range(0, len(chat_ids), batch_size):
        batch = chat_ids[i:i + batch_size]
        tasks = [
            bot.copy_message(chat_id=row['chat_id'], from_chat_id=from_chat_id, message_id=message_id)
            for row in batch
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if not isinstance(res, Exception):
                count += 1
        await asyncio.sleep(1)
    return count

async def broadcast_groups(message: Message, bot: Bot):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.reply_to_message:
        await message.reply("⚠️ Reply to a message to broadcast.")
        return

    groups = await get_all_groups()
    msg = await message.answer(f"📢 Broadcasting to {len(groups)} groups at high speed...")
    count = await safe_broadcast(bot, groups, message.chat.id, message.reply_to_message.message_id)
    await msg.edit_text(f"✅ Successfully broadcasted to {count} groups.")

async def broadcast_users(message: Message, bot: Bot):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not message.reply_to_message:
        await message.reply("⚠️ Reply to a message to broadcast.")
        return

    users = await get_all_users()
    msg = await message.answer(f"📢 Broadcasting to {len(users)} users at high speed...")
    count = await safe_broadcast(bot, users, message.chat.id, message.reply_to_message.message_id)
    await msg.edit_text(f"✅ Successfully broadcasted to {count} users.")

# --- Standard Math Handler ---

async def message_handler(message: Message):
    if not message.text:
        return

    if message.text.startswith('/'):
        return

    if not is_math_expression(message.text):
        return

    asyncio.create_task(add_chat(message.chat.id, message.chat.type, message.chat.title or message.from_user.first_name))

    if "=" in message.text and "x" in message.text.lower():
        result = await asyncio.to_thread(solve_equation, message.text.lower())
        if result and not result.startswith("⚠️"):
            await message.answer(f"📝 {message.text}\n\n👉 <b>{result}</b>")
        return

    result = await asyncio.to_thread(safe_calculate, message.text)
    if result:
        await message.answer(f"{message.text} = {result}")

# --- Inline Query Handler ---

async def inline_calculator(inline_query: InlineQuery):
    text = inline_query.query or ""
    if not text:
        return

    result = await asyncio.to_thread(safe_calculate, text)
    if not result:
        return

    result_id = hashlib.md5(text.encode()).hexdigest()
    
    item = InlineQueryResultArticle(
        id=result_id,
        title=f"Result: {result}",
        input_message_content=InputTextMessageContent(
            message_text=f"{text} = {result}",
            parse_mode=ParseMode.HTML
        ),
        description=f"Calculate {text}"
    )
    await inline_query.answer([item], cache_time=1)

# ================= MAIN =================

async def main():
    bot = Bot(
        TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()

    dp.message.middleware(RateLimitMiddleware(limit=1.5))

    await create_pool()
    await init_db()

    dp.message.register(start_handler, Command("start"))
    dp.message.register(stats_handler, Command("stats"))
    
    dp.message.register(integrate_handler, Command("int", "integrate"))
    dp.message.register(differentiate_handler, Command("diff", "differentiate", "dif"))
    dp.message.register(solve_handler, Command("solve"))

    dp.message.register(group_info, Command("groupinfo"))
    dp.message.register(on_bot_added, F.new_chat_members)

    dp.message.register(broadcast_groups, Command("broadcast"))
    dp.message.register(broadcast_users, Command("broad"))

    dp.callback_query.register(refresh_callback, F.data == "refresh_stats")
    
    dp.inline_query.register(inline_calculator)
    dp.message.register(message_handler)

    print("Bot running at lightning speed with Strict Math Filter, RAM protection, and Rate Limiting...⚡")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
