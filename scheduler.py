"""
Простой планировщик: раз в минуту сверяет текущее время/день с таблицей schedules
и запускает рассылку, если совпало. Чтобы не отправить дважды в одну и ту же минуту,
хранит в памяти, какие расписания уже сработали в текущую минуту.
"""
import asyncio
import logging
from datetime import datetime
import pytz

import config
import database as db
import userbot

logger = logging.getLogger("scheduler")

DAY_MAP = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


async def scheduler_loop():
    tz = pytz.timezone(config.TIMEZONE)
    fired_keys = set()  # (schedule_id, "YYYY-MM-DD HH:MM")
    last_seen_minute = None

    while True:
        try:
            now = datetime.now(tz)
            current_time = now.strftime("%H:%M")
            current_day = DAY_MAP[now.weekday()]
            minute_key = now.strftime("%Y-%m-%d %H:%M")
            if minute_key != last_seen_minute:
                fired_keys.clear()
                last_seen_minute = minute_key

            schedules = await db.get_schedules(enabled_only=True)
            for s in schedules:
                days_allowed = [d.strip() for d in s["days"].split(",")] if s["days"] != "*" else list(DAY_MAP.values())
                fire_key = (s["id"], minute_key)
                if s["time"] == current_time and current_day in days_allowed and fire_key not in fired_keys:
                    fired_keys.add(fire_key)
                    token = db.set_current_owner_id(s["owner_id"])
                    try:
                        logger.info(
                            f"Запуск расписания #{s['id']} owner_id={s['owner_id']} "
                            f"({s['time']}, группа {s['group_name']})"
                        )
                        employees = await db.get_employees(group_name=s["group_name"])
                        if employees:
                            try:
                                await userbot.broadcast(s["template_id"], employees)
                            except ValueError as e:
                                logger.warning(f"Расписание #{s['id']} пропущено: {e}")
                    finally:
                        db.reset_current_owner_id(token)
        except Exception as e:
            logger.exception(f"Ошибка в цикле планировщика: {e}")

        await asyncio.sleep(30)
