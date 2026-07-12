"""
Точка входа. Поднимает:
1. Юзербота (Telethon) — от твоего личного аккаунта.
2. Управляющего бота (aiogram) — интерфейс настроек.
3. Планировщик — фоновая проверка расписаний.

Запуск: python main.py

Авторизация юзербота больше не требует ввода кода в консоли. Если сессия ещё
не авторизована, приложение всё равно запустится: открой управляющего бота
командой /start и выбери «🔐 Авторизация юзербота» — там можно войти по
номеру телефона или отсканировать QR-код.
"""
import asyncio
import logging

import database as db
import userbot
from bot import build_bot_and_dispatcher
from scheduler import scheduler_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("main")


async def main():
    await db.init_db()
    logger.info("База данных готова.")

    await userbot.connect_userbot()

    bot, dp = build_bot_and_dispatcher()
    bot_me = await bot.get_me()
    userbot.set_inline_bot_username(bot_me.username)
    logger.info("Inline-режим будет использовать @%s.", bot_me.username)

    await asyncio.gather(
        dp.start_polling(bot),
        scheduler_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
