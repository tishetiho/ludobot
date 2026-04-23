import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError

# --- КОНФИГУРАЦИЯ ---
API_TOKEN = "8673476742:AAE4GeCi3x__yVgU3VKdtSYIvqfaTOaraJE"
ADMIN_ID = 12345678  # Ваш ID
OFFICIAL_CHAT_ID = -100123456789  # ID группы
SPONSOR_CHANNEL_ID = -100987654321  # ID канала спонсора

# Настройки по умолчанию
DEFAULT_LIMIT = 5
DEFAULT_WIN_TEXT = "💎 ВЫ ВЫЙГРАЛИ ГЛАВНЫЙ ПРИЗ! 💎"
DEFAULT_LOSE_TEXT = "🎁 Держи утешительный приз за упорство! 🎁"

# --- БАЗА ДАННЫХ ---
DB_PATH = "bot_database.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_premium BOOLEAN,
                fail_count INTEGER DEFAULT 0,
                last_free_spin TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Начальные настройки
        await db.execute("INSERT OR IGNORE INTO settings VALUES ('limit', ?)", (str(DEFAULT_LIMIT),))
        await db.execute("INSERT OR IGNORE INTO settings VALUES ('win_text', ?)", (DEFAULT_WIN_TEXT,))
        await db.execute("INSERT OR IGNORE INTO settings VALUES ('lose_text', ?)", (DEFAULT_LOSE_TEXT,))
        await db.commit()

async def get_setting(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

# --- MIDDLEWARES ---
class RegistrationMiddleware(types.BaseObject):
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.Update,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO users (user_id, is_premium) VALUES (?, ?)",
                    (user.id, user.is_premium)
                )
                await db.commit()
        return await handler(event, data)

# --- FSM STATES ---
class AdminStates(StatesGroup):
    wait_win_text = State()
    wait_lose_text = State()
    wait_limit = State()
    wait_broadcast = State()
    wait_premium_broadcast = State()

# --- ХЕНДЛЕРЫ: ИГРОВАЯ ЛОГИКА (ГРУППА) ---
game_router = Router()
user_cooldowns = {}

@game_router.message(F.chat.id == OFFICIAL_CHAT_ID, F.dice.emoji == "🎰")
async def handle_slots(message: types.Message):
    user_id = message.from_user.id
    
    # Анти-флуд (5 сек)
    if user_id in user_cooldowns and time.time() - user_cooldowns[user_id] < 5:
        return await message.delete()
    user_cooldowns[user_id] = time.time()

    value = message.dice.value
    logging.info(f"User {user_id} rolled {value}")

    async with aiosqlite.connect(DB_PATH) as db:
        if value == 64:  # Комбинация 777
            win_msg = await get_setting("win_text")
            await message.reply(f"🎰 <tg-emoji emoji-id='5445284980000000001'>🔥</tg-emoji> {win_msg}")
            await db.execute("UPDATE users SET fail_count = 0 WHERE user_id = ?", (user_id,))
        else:
            async with db.execute("SELECT fail_count FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                current_fails = row[0] + 1
            
            limit = int(await get_setting("limit"))
            
            if current_fails >= limit:
                lose_msg = await get_setting("lose_text")
                await message.reply(f"🎰 <tg-emoji emoji-id='5445284980000000002'>🎁</tg-emoji> {lose_msg}")
                await db.execute("UPDATE users SET fail_count = 0 WHERE user_id = ?", (user_id,))
            else:
                await db.execute("UPDATE users SET fail_count = ? WHERE user_id = ?", (current_fails, user_id))
        await db.commit()

# --- ХЕНДЛЕРЫ: ЛИЧНЫЕ СООБЩЕНИЯ ---
private_router = Router()
private_router.message.filter(F.chat.type == "private")

def main_menu_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="<tg-emoji emoji-id='5362002344754714153'>ℹ️</tg-emoji> FAQ", callback_data="faq"))
    builder.row(InlineKeyboardButton(text="<tg-emoji emoji-id='5361622359418244951'>👤</tg-emoji> Кабинет", callback_data="profile"))
    builder.row(InlineKeyboardButton(text="<tg-emoji emoji-id='5431466164287618311'>🎰</tg-emoji> Бесплатная прокрутка", callback_data="free_spin"))
    return builder.as_markup()

@private_router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Добро пожаловать в игровой бот!\nИспользуйте меню ниже:",
        reply_markup=main_menu_kb()
    )

@private_router.callback_query(F.data == "faq")
async def faq_callback(call: types.CallbackQuery):
    text = (
        "<tg-emoji emoji-id='5445284980000000003'>📝</tg-emoji> **Правила игры:**\n"
        "1. Играйте в нашем официальном чате.\n"
        "2. Выпало 777 — забирайте главный приз.\n"
        "3. Не везет? Каждая X попытка дает утешительный бонус!\n\n"
        "💰 **Вывод:** Обратитесь к администратору."
    )
    await call.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")

@private_router.callback_query(F.data == "profile")
async def profile_callback(call: types.CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT fail_count, is_premium FROM users WHERE user_id = ?", (call.from_user.id,)) as cursor:
            row = await cursor.fetchone()
            fails, is_prem = row if row else (0, False)
    
    prem_status = "✅ Да" if is_prem else "❌ Нет"
    await call.message.edit_text(
        f"👤 **Ваш профиль**\n\n"
        f"ID: `{call.from_user.id}`\n"
        f"Premium: {prem_status}\n"
        f"Неудачных попыток: {fails}",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )

@private_router.callback_query(F.data == "free_spin")
async def free_spin(call: types.CallbackQuery, bot: Bot):
    # 1. Проверка подписки
    try:
        member = await bot.get_chat_member(SPONSOR_CHANNEL_ID, call.from_user.id)
        if member.status in ["left", "kicked"]:
            return await call.answer("❌ Сначала подпишитесь на канал спонсора!", show_alert=True)
    except:
        return await call.answer("❌ Ошибка проверки подписки.", show_alert=True)

    # 2. Проверка КД 24 часа
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT last_free_spin FROM users WHERE user_id = ?", (call.from_user.id,)) as cursor:
            row = await cursor.fetchone()
            last_spin = row[0]
            
        now = datetime.now()
        if last_spin:
            last_spin_dt = datetime.fromisoformat(last_spin)
            if now < last_spin_dt + timedelta(hours=24):
                wait_time = (last_spin_dt + timedelta(hours=24)) - now
                return await call.answer(f"⏳ Следующая прокрутка через {wait_time.seconds // 3600}ч", show_alert=True)

        # 3. Выдача прокрутки
        await db.execute("UPDATE users SET last_free_spin = ? WHERE user_id = ?", (now.isoformat(), call.from_user.id))
        await db.commit()
        await call.answer("🎰 Вам начислена бесплатная попытка в чате (имитация)!", show_alert=True)

# --- АДМИН-ПАНЕЛЬ ---
admin_router = Router()
admin_router.message.filter(F.from_user.id == ADMIN_ID)

@admin_router.message(Command("adminpan"))
async def admin_menu(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Текст Главного приза", callback_data="edit_win"))
    kb.row(InlineKeyboardButton(text="Текст Утешительного приза", callback_data="edit_lose"))
    kb.row(InlineKeyboardButton(text="Лимит попыток", callback_data="edit_limit"))
    kb.row(InlineKeyboardButton(text="Рассылка всем", callback_data="broadcast_all"))
    kb.row(InlineKeyboardButton(text="Рассылка Premium", callback_data="broadcast_prem"))
    await message.answer("🛠 Админ-панель", reply_markup=kb.as_markup())

@admin_router.callback_query(F.data == "edit_limit")
async def set_limit_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("Введите новое число попыток:")
    await state.set_state(AdminStates.wait_limit)

@admin_router.message(AdminStates.wait_limit)
async def set_limit_finish(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Введите число!")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE settings SET value = ? WHERE key = 'limit'", (message.text,))
        await db.commit()
    await message.answer(f"✅ Лимит изменен на {message.text}")
    await state.clear()

@admin_router.callback_query(F.data.startswith("broadcast"))
async def broadcast_start(call: types.CallbackQuery, state: FSMContext):
    target = "PREMIUM" if "prem" in call.data else "ВСЕМ"
    await call.message.answer(f"Введите текст рассылки для {target}:")
    await state.set_state(AdminStates.wait_premium_broadcast if "prem" in call.data else AdminStates.wait_broadcast)

@admin_router.message(AdminStates.wait_broadcast)
@admin_router.message(AdminStates.wait_premium_broadcast)
async def broadcast_finish(message: types.Message, state: FSMContext, bot: Bot):
    st = await state.get_state()
    query = "SELECT user_id FROM users"
    if st == AdminStates.wait_premium_broadcast:
        query += " WHERE is_premium = 1"
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query) as cursor:
            users = await cursor.fetchall()
    
    count = 0
    for (uid,) in users:
        try:
            await bot.send_message(uid, message.text)
            count += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            pass
        except Exception as e:
            logging.error(f"Error sending to {uid}: {e}")
            
    await message.answer(f"📢 Рассылка завершена. Получили: {count} чел.")
    await state.clear()

# --- ЗАПУСК ---
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    
    bot = Bot(token=API_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    
    # Регистрация мидлварей и роутеров
    dp.update.outer_middleware(RegistrationMiddleware())
    dp.include_routers(admin_router, game_router, private_router)

    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
