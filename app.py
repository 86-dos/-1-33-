import asyncio
import logging
import sys
import os
from datetime import datetime

import aiosqlite
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramNetworkError

# =========================
# CONFIG
# =========================
API_TOKEN = os.getenv("BOT_TOKEN")

if not API_TOKEN:
    raise ValueError("❌ BOT_TOKEN not found in Secrets")

ADMINS = {6814524171, 7764122495}

DB_PATH = "economy.db"

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

# =========================
# DB INIT
# =========================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                nickname TEXT,
                balance REAL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS usernames (
                username TEXT PRIMARY KEY,
                user_id INTEGER
            )
        """)
        # Таблица логов транзакций
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                from_user INTEGER,
                to_user INTEGER,
                amount REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()
    logger.info("✅ Database initialized")


async def log_transaction(db, type_: str, from_user, to_user, amount: float):
    """Записывает транзакцию в БД и в лог."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO transactions (type, from_user, to_user, amount, created_at) VALUES (?, ?, ?, ?, ?)",
        (type_, from_user, to_user, amount, now)
    )
    logger.info(f"💾 [{type_}] from={from_user} to={to_user} amount={amount:.2f}$ at={now}")


# =========================
# UTILS
# =========================
async def save_user(message: Message):
    if not message or not message.from_user:
        return

    uid = message.from_user.id
    username = message.from_user.username

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,)
        )
        if username:
            await db.execute(
                "INSERT OR REPLACE INTO usernames (username, user_id) VALUES (?, ?)",
                (username.lower(), uid)
            )
        await db.commit()


async def get_user_id(identifier: str):
    if identifier.isdigit():
        return int(identifier)

    clean = identifier.lower().replace("@", "")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM usernames WHERE username = ?", (clean,)
        ) as cur:
            row = await cur.fetchone()

    return row[0] if row else None


# =========================
# BOT
# =========================
dp = Dispatcher()


@dp.message(Command("start"))
async def start(message: Message):
    await save_user(message)
    logger.info(f"👋 /start — user={message.from_user.id}")
    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "🤖 Бот запущен и готов к работе.\n"
        "📖 Используй /help чтобы увидеть все команды."
    )


@dp.message(Command("profile"))
async def profile(message: Message):
    await save_user(message)
    logger.info(f"👤 /profile — user={message.from_user.id}")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT nickname, balance FROM users WHERE user_id = ?",
            (message.from_user.id,)
        ) as cur:
            row = await cur.fetchone()

    nick = row[0] if row and row[0] else "No name"
    bal = row[1] if row else 0

    await message.answer(
        f"👤 <b>Профиль</b>\n\n"
        f"📛 Никнейм: <b>{nick}</b>\n"
        f"💰 Баланс: <b>{bal:.2f}$</b>"
    )


@dp.message(Command("nick"))
async def nick(message: Message):
    await save_user(message)

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("⚠️ Использование: /nick <b>имя</b>")

    new_nick = parts[1].strip()

    if not new_nick:
        return await message.answer("⚠️ Никнейм не может быть пустым")
    if len(new_nick) > 32:
        return await message.answer("⚠️ Никнейм слишком длинный (макс. 32 символа)")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET nickname = ? WHERE user_id = ?",
            (new_nick, message.from_user.id)
        )
        await db.commit()

    logger.info(f"✏️ /nick — user={message.from_user.id} new_nick={new_nick}")
    await message.answer(f"✅ Никнейм обновлён: <b>{new_nick}</b>")


@dp.message(Command("add"))
async def add(message: Message):
    await save_user(message)

    if message.from_user.id not in ADMINS:
        logger.warning(f"🚫 /add — unauthorized user={message.from_user.id}")
        return

    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer("⚠️ Использование: /add @user 100")

    target = await get_user_id(parts[1])
    if target is None:
        return await message.answer("❌ Пользователь не найден")

    try:
        amount = float(parts[2])
    except ValueError:
        return await message.answer("⚠️ Неверная сумма")

    if amount <= 0:
        return await message.answer("⚠️ Сумма должна быть > 0")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (target,)
        )
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, target)
        )
        await log_transaction(db, "ADD", from_user=message.from_user.id, to_user=target, amount=amount)
        await db.commit()

    await message.answer(f"✅ Начислено <b>+{amount:.2f}$</b> пользователю <code>{target}</code>")


@dp.message(Command("take"))
async def take(message: Message):
    await save_user(message)

    if message.from_user.id not in ADMINS:
        logger.warning(f"🚫 /take — unauthorized user={message.from_user.id}")
        return

    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer("⚠️ Использование: /take @user 100")

    target = await get_user_id(parts[1])
    if target is None:
        return await message.answer("❌ Пользователь не найден")

    try:
        amount = float(parts[2])
    except ValueError:
        return await message.answer("⚠️ Неверная сумма")

    if amount <= 0:
        return await message.answer("⚠️ Сумма должна быть > 0")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM users WHERE user_id = ?", (target,)
        ) as cur:
            row = await cur.fetchone()

        bal = row[0] if row else 0

        if bal < amount:
            return await message.answer("❌ У пользователя недостаточно средств")

        await db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (amount, target)
        )
        await log_transaction(db, "TAKE", from_user=target, to_user=message.from_user.id, amount=amount)
        await db.commit()

    await message.answer(f"✅ Снято <b>-{amount:.2f}$</b> у пользователя <code>{target}</code>")


@dp.message(Command("withdraw"))
async def withdraw(message: Message):
    await save_user(message)

    parts = message.text.split()
    if len(parts) < 2:
        return await message.answer("⚠️ Использование: /withdraw 100")

    try:
        amount = float(parts[1])
    except ValueError:
        return await message.answer("⚠️ Неверная сумма")

    if amount <= 0:
        return await message.answer("⚠️ Сумма должна быть > 0")

    uid = message.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM users WHERE user_id = ?", (uid,)
        ) as cur:
            row = await cur.fetchone()

        bal = row[0] if row else 0

        if bal < amount:
            return await message.answer("❌ Недостаточно средств на балансе")

        await db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (amount, uid)
        )
        await log_transaction(db, "WITHDRAW", from_user=uid, to_user=None, amount=amount)
        await db.commit()

    await message.answer(f"💸 Вывод выполнен: <b>-{amount:.2f}$</b>")


@dp.message(Command("pay"))
async def pay(message: Message):
    await save_user(message)

    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer("⚠️ Использование: /pay @user 100")

    target = await get_user_id(parts[1])
    if target is None:
        return await message.answer("❌ Пользователь не найден")

    try:
        amount = float(parts[2])
    except ValueError:
        return await message.answer("⚠️ Неверная сумма")

    if amount <= 0:
        return await message.answer("⚠️ Сумма должна быть > 0")

    sender = message.from_user.id

    if sender == target:
        return await message.answer("⚠️ Нельзя переводить самому себе")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM users WHERE user_id = ?", (sender,)
        ) as cur:
            row = await cur.fetchone()

        bal = row[0] if row else 0

        if bal < amount:
            return await message.answer("❌ Недостаточно средств на балансе")

        try:
            await db.execute("BEGIN")
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (target,)
            )
            await db.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ?",
                (amount, sender)
            )
            await db.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (amount, target)
            )
            await log_transaction(db, "PAY", from_user=sender, to_user=target, amount=amount)
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ [PAY] Transaction failed: {e}")
            return await message.answer("❌ Ошибка транзакции, попробуй ещё раз")

    await message.answer(f"✅ Перевод выполнен: <b>{amount:.2f}$</b> → <code>{target}</code>")


@dp.message(Command("top"))
async def top(message: Message):
    await save_user(message)
    logger.info(f"🏆 /top — user={message.from_user.id}")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT nickname, balance
            FROM users
            ORDER BY balance DESC
            LIMIT 10
        """) as cur:
            rows = await cur.fetchall()

    if not rows:
        return await message.answer("❌ Список пуст")

    medals = ["🥇", "🥈", "🥉"]
    text = "🏆 <b>ТОП ИГРОКОВ</b>\n\n"

    for i, r in enumerate(rows, 1):
        nick = r[0] or "No name"
        bal = r[1]
        medal = medals[i - 1] if i <= 3 else f"{i}."
        text += f"{medal} {nick} — <b>{bal:.2f}$</b>\n"

    await message.answer(text)


@dp.message(Command("history"))
async def history(message: Message):
    await save_user(message)
    uid = message.from_user.id

    # Только админы, только в личке с ботом
    if uid not in ADMINS:
        logger.warning(f"🚫 /history — unauthorized user={uid}")
        return

    if message.chat.type != "private":
        logger.warning(f"🚫 /history — attempted in group by user={uid}")
        return await message.answer("🔒 Эта команда доступна только в личке с ботом")

    logger.info(f"📋 /history — admin={uid}")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT type, from_user, to_user, amount, created_at
            FROM transactions
            ORDER BY id DESC
            LIMIT 20
        """) as cur:
            rows = await cur.fetchall()

    if not rows:
        return await message.answer("📭 История транзакций пуста")

    icons = {
        "PAY": "💸",
        "ADD": "➕",
        "TAKE": "➖",
        "WITHDRAW": "🏧",
    }

    text = "📋 <b>Все транзакции (последние 20)</b>\n\n"
    for r in rows:
        type_, from_u, to_u, amount, created_at = r
        icon = icons.get(type_, "🔄")
        to_str = f"→ <code>{to_u}</code>" if to_u else ""
        text += f"{icon} <b>{type_}</b> | <code>{from_u}</code> {to_str} | <b>{amount:.2f}$</b> | {created_at}\n"

    await message.answer(text)


@dp.message(Command("help"))
async def help_cmd(message: Message):
    logger.info(f"❓ /help — user={message.from_user.id}")
    await message.answer(
        "📖 <b>Список команд</b>\n\n"
        "👤 /profile — твой профиль\n"
        "✏️ /nick &lt;имя&gt; — сменить никнейм\n"
        "💸 /withdraw &lt;сумма&gt; — вывести деньги\n"
        "💳 /pay @user &lt;сумма&gt; — перевести деньги\n"
        "🏆 /top — топ игроков\n\n"
        "🔐 <b>Только для админов (в личке):</b>\n"
        "➕ /add @user &lt;сумма&gt;\n"
        "➖ /take @user &lt;сумма&gt;\n"
        "📋 /history — все транзакции"
    )


@dp.message(F.from_user)
async def track(message: Message):
    await save_user(message)


# =========================
# WEB SERVER
# =========================
async def handle(request):
    return web.Response(text="Bot running")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", handle)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", 7860)
    await site.start()

    logger.info("🌐 Web server started on port 7860")


# =========================
# MAIN
# =========================
async def main():
    await init_db()

    bot = Bot(
        token=API_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    await run_web_server()
    logger.info("🤖 Bot started")

    while True:
        try:
            await dp.start_polling(bot)
        except TelegramNetworkError as e:
            logger.error(f"🌐 Telegram network error: {e}")
            await asyncio.sleep(5)
        except (KeyboardInterrupt, SystemExit):
            logger.info("🛑 Bot stopped")
            break
        except Exception as e:
            logger.error(f"💥 Unexpected error: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
