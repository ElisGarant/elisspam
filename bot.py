"""
Управляющий бот на aiogram 3.x.
Через него пользователи управляют своими юзерботами: пользователи, шаблоны,
разовая рассылка, расписания, логи. Доступ выдают root-админы из ADMIN_IDS.
"""
import asyncio
import csv
import functools
import io
import inspect
import logging
import re
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InputTextMessageContent,
    BufferedInputFile,
    InputMediaPhoto,
)
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    SendCodeUnavailableError,
    SessionPasswordNeededError,
    SmsCodeCreateFailedError,
)

import config
import database as db
import template_payload as tpl
import userbot

logger = logging.getLogger("control_bot")

router = Router()


def admin_only(handler):
    signature = inspect.signature(handler)
    accepts_kwargs = any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )
    accepted_kwargs = {
        name
        for name, param in signature.parameters.items()
        if param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }
    first_arg = next(
        (
            name
            for name, param in signature.parameters.items()
            if param.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ),
        None,
    )
    if first_arg:
        accepted_kwargs.discard(first_arg)

    @functools.wraps(handler)
    async def wrapper(event, *args, **kwargs):
        user = event.from_user
        allowed = await db.bind_or_check_access(user.id, user.username)
        if not allowed:
            if isinstance(event, Message):
                await event.answer(
                    "⛔ У тебя нет доступа к этому боту.\n\n"
                    "Попроси администратора выдать доступ на твой @username, "
                    "а потом снова отправь /start."
                )
            else:
                await event.answer("⛔ Нет доступа.", show_alert=True)
            return

        token = db.set_current_owner_id(user.id)
        try:
            if accepts_kwargs:
                return await handler(event, *args, **kwargs)

            handler_kwargs = {
                name: value
                for name, value in kwargs.items()
                if name in accepted_kwargs
            }
            return await handler(event, *args, **handler_kwargs)
        finally:
            db.reset_current_owner_id(token)
    return wrapper


# ---------------- FSM состояния ----------------

class AddEmployee(StatesGroup):
    waiting_data = State()


class ImportGroup(StatesGroup):
    waiting_link = State()


class ImportCSV(StatesGroup):
    waiting_file = State()


class AddEmployeesBulk(StatesGroup):
    waiting_data = State()


class AddTemplate(StatesGroup):
    waiting_name = State()
    waiting_text = State()
    waiting_button_data = State()
    waiting_post_delay = State()


class BroadcastNow(StatesGroup):
    choosing_template = State()
    choosing_group = State()
    choosing_accounts = State()
    choosing_skip_check = State()
    waiting_skip_chat = State()
    confirming = State()


class AddSchedule(StatesGroup):
    choosing_template = State()
    choosing_group = State()
    waiting_time = State()
    waiting_days = State()


class AuthPhone(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class DelaySettings(StatesGroup):
    waiting_range = State()


class AccessAdmin(StatesGroup):
    waiting_username = State()


class AddKeywordWatch(StatesGroup):
    choosing_account = State()
    waiting_chat = State()
    choosing_template = State()
    waiting_keywords = State()


class RegistryCheck(StatesGroup):
    waiting_target = State()


# ---------------- Главное меню ----------------

def main_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="menu_employees")],
        [InlineKeyboardButton(text="📝 Шаблоны сообщений", callback_data="menu_templates")],
        [InlineKeyboardButton(text="📤 Разовая рассылка", callback_data="menu_broadcast")],
        [InlineKeyboardButton(text="⏯ Управление рассылкой", callback_data="bcast_control_menu")],
        [InlineKeyboardButton(text="⏰ Расписание", callback_data="menu_schedule")],
        [InlineKeyboardButton(text="📊 Логи рассылок", callback_data="menu_logs")],
        [InlineKeyboardButton(text="🔎 Автопарсер", callback_data="menu_watches")],
        [InlineKeyboardButton(text="📚 Реестр отправок", callback_data="menu_delivery_registry")],
        [InlineKeyboardButton(text="🔐 Авторизация юзербота", callback_data="menu_auth")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings")],
    ]
    if db.is_root_admin(db.get_current_owner_id(default_to_root=False)):
        rows.append([InlineKeyboardButton(text="🛡 Админ-панель", callback_data="menu_admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"))
@admin_only
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Панель управления юзерботом-рассыльщиком.\n\nВыбери раздел:",
        reply_markup=main_menu_kb()
    )


@router.callback_query(F.data == "menu_main")
@admin_only
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Выбери раздел:", reply_markup=main_menu_kb())
    await callback.answer()


# ---------------- Раздел: Админ-панель доступа ----------------

def access_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выдать доступ по @username", callback_data="access_add")],
        [InlineKeyboardButton(text="👤 Пользователи и доступы", callback_data="access_list")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")],
    ])


def _access_user_line(user: dict) -> str:
    username = f"@{user['username']}" if user.get("username") else "username не привязан"
    telegram_id = user.get("telegram_id") or "ожидает /start"
    role = "root" if user.get("role") == "owner" else "user"
    status = "✅ активен" if user.get("active") else "🚫 выключен"
    return f"#{user['id']} {username} | id: {telegram_id} | {role} | {status}"


async def _send_access_list(callback: CallbackQuery):
    users = await db.get_access_users(include_inactive=True)
    if not users:
        await callback.message.edit_text("Пользователей пока нет.", reply_markup=access_admin_kb())
        return

    lines = [_access_user_line(user) for user in users]
    kb_rows = []
    for user in users:
        if db.is_root_admin(user.get("telegram_id")):
            continue
        action = "🚫 Отключить" if user.get("active") else "✅ Включить"
        label_name = f"@{user['username']}" if user.get("username") else f"#{user['id']}"
        kb_rows.append([
            InlineKeyboardButton(
                text=f"{action} {label_name}",
                callback_data=f"access_toggle_{user['id']}",
            )
        ])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_admin_panel")])

    await callback.message.edit_text(
        "🛡 Пользователи панели\n\n" + "\n".join(lines)[:3500],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


@router.callback_query(F.data == "menu_admin_panel")
@admin_only
async def menu_admin_panel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not db.is_root_admin(callback.from_user.id):
        await callback.answer("Этот раздел доступен только root-админам.", show_alert=True)
        return
    users = await db.get_access_users(include_inactive=True)
    active_count = sum(1 for user in users if user.get("active"))
    pending_count = sum(1 for user in users if user.get("active") and not user.get("telegram_id"))
    await callback.message.edit_text(
        "🛡 Админ-панель доступа\n\n"
        f"Активных доступов: {active_count}\n"
        f"Ожидают первый /start: {pending_count}\n\n"
        "Выдай доступ по username. Когда человек напишет боту /start, "
        "его Telegram ID автоматически привяжется к этому доступу.",
        reply_markup=access_admin_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "access_add")
@admin_only
async def access_add_start(callback: CallbackQuery, state: FSMContext):
    if not db.is_root_admin(callback.from_user.id):
        await callback.answer("Этот раздел доступен только root-админам.", show_alert=True)
        return
    await state.set_state(AccessAdmin.waiting_username)
    await callback.message.edit_text(
        "Отправь username человека, которому нужно выдать доступ.\n\n"
        "Пример: `@ivan_petrov`",
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(AccessAdmin.waiting_username)
@admin_only
async def access_add_finish(message: Message, state: FSMContext):
    if not db.is_root_admin(message.from_user.id):
        await message.answer("⛔ Этот раздел доступен только root-админам.")
        await state.clear()
        return

    try:
        access = await db.grant_access_by_username(message.text.strip(), granted_by=message.from_user.id)
    except ValueError as e:
        await message.answer(f"❌ {e}. Попробуй ещё раз, например `@ivan_petrov`.", parse_mode="Markdown")
        return

    await state.clear()
    await message.answer(
        f"✅ Доступ выдан: @{access['username']}.\n\n"
        "Теперь человек должен написать этому боту /start, после чего его ID привяжется автоматически.",
        reply_markup=access_admin_kb(),
    )


@router.callback_query(F.data == "access_list")
@admin_only
async def access_list(callback: CallbackQuery):
    if not db.is_root_admin(callback.from_user.id):
        await callback.answer("Этот раздел доступен только root-админам.", show_alert=True)
        return
    await _send_access_list(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("access_toggle_"))
@admin_only
async def access_toggle(callback: CallbackQuery):
    if not db.is_root_admin(callback.from_user.id):
        await callback.answer("Этот раздел доступен только root-админам.", show_alert=True)
        return

    access_id = int(callback.data.split("_")[-1])
    users = await db.get_access_users(include_inactive=True)
    current = next((user for user in users if user["id"] == access_id), None)
    if current and not db.is_root_admin(current.get("telegram_id")):
        await db.set_access_active(access_id, not current["active"])
    await callback.answer("Изменено")
    await _send_access_list(callback)


# ---------------- Раздел: Пользователи ----------------

def employees_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить вручную", callback_data="emp_add")],
        [InlineKeyboardButton(text="📋 Добавить списком (текст/.txt)", callback_data="emp_add_bulk")],
        [InlineKeyboardButton(text="📥 Импорт из CSV", callback_data="emp_import_csv")],
        [InlineKeyboardButton(text="👥 Импорт из группы", callback_data="emp_import_group")],
        [InlineKeyboardButton(text="📋 Список пользователей", callback_data="emp_list")],
        [InlineKeyboardButton(text="🧹 Удалить всех пользователей", callback_data="emp_delete_all_confirm")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")],
    ])


@router.callback_query(F.data == "menu_employees")
@admin_only
async def menu_employees(callback: CallbackQuery):
    await callback.message.edit_text("Раздел «Пользователи»:", reply_markup=employees_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "emp_add")
@admin_only
async def emp_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddEmployee.waiting_data)
    await callback.message.edit_text(
        "Отправь данные пользователя в формате:\n\n"
        "`username_или_id, Имя Фамилия`\n\n"
        "Имя можно не указывать.\n"
        "Пример: `ivan_petrov, Иван Петров`",
        parse_mode="Markdown"
    )
    await callback.answer()


@router.message(AddEmployee.waiting_data)
@admin_only
async def emp_add_finish(message: Message, state: FSMContext):
    parts = [p.strip() for p in message.text.split(",")]
    if not parts or not parts[0]:
        await message.answer("Не удалось разобрать данные. Попробуй ещё раз в формате: username, Имя")
        return
    identifier = parts[0]
    full_name = parts[1] if len(parts) > 1 else None
    group_name = "Все"

    telegram_id = int(identifier) if identifier.lstrip("-").isdigit() else None
    username = identifier if telegram_id is None else None

    await db.add_employee(username=username, telegram_id=telegram_id, full_name=full_name, group_name=group_name)
    await state.clear()
    await message.answer(f"✅ Пользователь добавлен: {full_name or identifier}", reply_markup=employees_menu_kb())


# ---------- Добавление списком: текстовым сообщением или .txt-файлом ----------

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def _parse_identifier(raw: str) -> dict | None:
    """Превращает один токен (username или id) в telegram_id/username. None, если пусто."""
    raw = raw.strip().lstrip("@")
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return {"telegram_id": int(raw), "username": None}
    if not USERNAME_RE.fullmatch(raw):
        return None
    return {"telegram_id": None, "username": raw}


def parse_bulk_employee_lines(text: str) -> tuple[list[dict], int]:
    """
    Разбирает текст (из сообщения или .txt-файла) на список пользователей.
    Поддерживаемые форматы, можно свободно смешивать по строкам:
      - просто список: @user1, user2, 123456789 (через пробел, запятую или каждый на своей строке)
      - с именем как при ручном добавлении: `username, Имя Фамилия`
    Возвращает (rows_для_bulk_add_employees, кол-во_нераспознанных_строк).
    """
    rows = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) >= 2:
            ident = _parse_identifier(parts[0])
            if ident is not None and not all(_parse_identifier(part) is not None for part in parts[1:]):
                rows.append({
                    **ident,
                    "full_name": parts[1] or None,
                    "group_name": "Все",
                })
                continue

        tokens = []
        for part in parts:
            tokens.extend(part.split())
        for token in tokens:
            ident = _parse_identifier(token)
            if ident is None:
                skipped += 1
                continue
            rows.append({**ident, "full_name": None, "group_name": "Все"})
    return rows, skipped


@router.callback_query(F.data == "emp_add_bulk")
@admin_only
async def emp_add_bulk_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddEmployeesBulk.waiting_data)
    await callback.message.edit_text(
        "Пришли список пользователей — сообщением или файлом `.txt`.\n\n"
        "Форматы (можно смешивать построчно):\n"
        "• просто username'ы или ID, через пробел/запятую/каждый на новой строке:\n"
        "  `@ivan_petrov, @anna_k, 123456789`\n"
        "• с указанием имени:\n"
        "  `ivan_petrov, Иван Петров`",
        parse_mode="Markdown"
    )
    await callback.answer()


@router.message(AddEmployeesBulk.waiting_data, F.document)
@admin_only
async def emp_add_bulk_file(message: Message, state: FSMContext):
    doc = message.document
    if not doc.file_name.lower().endswith(".txt"):
        await message.answer("Это не .txt-файл. Пришли текстовый файл с расширением .txt, или отправь список сообщением.")
        return

    bot: Bot = message.bot
    file = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file.file_path)
    text_data = file_bytes.read().decode("utf-8-sig", errors="replace")

    rows, skipped = parse_bulk_employee_lines(text_data)
    if not rows:
        await message.answer("Не удалось распознать ни одного пользователя в файле.")
        return

    await db.bulk_add_employees(rows)
    await state.clear()
    note = f"\n⚠️ Не распознано строк: {skipped}" if skipped else ""
    await message.answer(f"✅ Добавлено пользователей: {len(rows)}{note}", reply_markup=employees_menu_kb())


@router.message(AddEmployeesBulk.waiting_data, F.text)
@admin_only
async def emp_add_bulk_text(message: Message, state: FSMContext):
    rows, skipped = parse_bulk_employee_lines(message.text)
    if not rows:
        await message.answer(
            "Не удалось распознать ни одного пользователя. Пришли username'ы/ID через пробел, "
            "запятую или каждый на новой строке — либо файл .txt."
        )
        return

    await db.bulk_add_employees(rows)
    await state.clear()
    note = f"\n⚠️ Не распознано строк: {skipped}" if skipped else ""
    await message.answer(f"✅ Добавлено пользователей: {len(rows)}{note}", reply_markup=employees_menu_kb())


@router.callback_query(F.data == "emp_import_csv")
@admin_only
async def emp_import_csv_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ImportCSV.waiting_file)
    await callback.message.edit_text(
        "Пришли CSV-файл с колонками:\n`username,full_name`\n\n"
        "Вместо username можно указать telegram_id — тогда колонка должна называться `telegram_id`.\n"
        "Первая строка — заголовки.",
        parse_mode="Markdown"
    )
    await callback.answer()


@router.message(ImportCSV.waiting_file, F.document)
@admin_only
async def emp_import_csv_file(message: Message, state: FSMContext):
    doc = message.document
    if not doc.file_name.lower().endswith(".csv"):
        await message.answer("Это не CSV-файл. Пришли файл с расширением .csv")
        return

    bot: Bot = message.bot
    file = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file.file_path)
    text_data = file_bytes.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text_data))

    rows = []
    for row in reader:
        rows.append({
            "telegram_id": int(row["telegram_id"]) if row.get("telegram_id", "").strip().isdigit() else None,
            "username": (row.get("username") or "").strip() or None,
            "full_name": (row.get("full_name") or "").strip() or None,
            "group_name": "Все",
        })

    if not rows:
        await message.answer("Файл пуст или колонки не распознаны.")
        return

    await db.bulk_add_employees(rows)
    await state.clear()
    await message.answer(f"✅ Импортировано пользователей: {len(rows)}", reply_markup=employees_menu_kb())


@router.callback_query(F.data == "emp_import_group")
@admin_only
async def emp_import_group_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ImportGroup.waiting_link)
    await callback.message.edit_text(
        "Пришли ссылку на группу/чат (или @username группы), из которой нужно импортировать участников.\n\n"
        "⚠️ Юзербот должен быть участником этой группы."
    )
    await callback.answer()


@router.message(ImportGroup.waiting_link)
@admin_only
async def emp_import_group_finish(message: Message, state: FSMContext):
    link = message.text.strip()
    status_msg = await message.answer("⏳ Импортирую участников...")
    try:
        members = await userbot.import_group_members(link)
        await db.bulk_add_employees(members)
        await status_msg.edit_text(f"✅ Импортировано пользователей: {len(members)}")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка импорта: {e}")
    await state.clear()
    await message.answer("Раздел «Пользователи»:", reply_markup=employees_menu_kb())


def _employee_handle(employee: dict) -> str:
    if employee.get("username"):
        return f"@{employee['username']}"
    if employee.get("telegram_id"):
        return str(employee["telegram_id"])
    return "нет username/id"


def _employee_button_label(employee: dict) -> str:
    label = employee.get("full_name") or _employee_handle(employee)
    return label[:35] + "..." if len(label) > 35 else label


async def _send_employee_list(callback: CallbackQuery):
    employees = await db.get_employees(active_only=False)
    if not employees:
        await callback.message.edit_text("Список пользователей пуст.", reply_markup=employees_menu_kb())
        return

    lines = []
    for e in employees[:50]:
        name = e["full_name"] or "—"
        handle = _employee_handle(e)
        status = "✅" if e["active"] else "🚫"
        lines.append(f"{status} #{e['id']} {name} ({handle})")

    text = "👥 Пользователи:\n\n" + "\n".join(lines)
    if len(employees) > 50:
        text += f"\n\n...и ещё {len(employees) - 50}"

    kb_rows = [
        [
            InlineKeyboardButton(
                text=f"🗑 #{e['id']} {_employee_button_label(e)}",
                callback_data=f"emp_del_{e['id']}",
            )
        ]
        for e in employees[:50]
    ]
    kb_rows.append([InlineKeyboardButton(text="🧹 Удалить всех пользователей", callback_data="emp_delete_all_confirm")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_employees")])
    await callback.message.edit_text(text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data == "emp_list")
@admin_only
async def emp_list(callback: CallbackQuery):
    await _send_employee_list(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("emp_del_"))
@admin_only
async def emp_delete(callback: CallbackQuery):
    employee_id = int(callback.data.split("_")[-1])
    await db.delete_employee(employee_id)
    await _send_employee_list(callback)
    await callback.answer("Пользователь удалён")


@router.callback_query(F.data == "emp_delete_all_confirm")
@admin_only
async def emp_delete_all_confirm(callback: CallbackQuery):
    employees = await db.get_employees(active_only=False)
    if not employees:
        await callback.message.edit_text("Список пользователей уже пуст.", reply_markup=employees_menu_kb())
        await callback.answer()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🧹 Да, удалить всех ({len(employees)})", callback_data="emp_delete_all_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_employees")],
    ])
    await callback.message.edit_text(
        f"Удалить всех пользователей из твоего списка?\n\n"
        f"Будет удалено: {len(employees)}.\n"
        "Это действие нельзя отменить.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "emp_delete_all_yes")
@admin_only
async def emp_delete_all(callback: CallbackQuery):
    deleted = await db.delete_all_employees()
    await callback.message.edit_text(
        f"✅ Удалено пользователей: {deleted}.",
        reply_markup=employees_menu_kb(),
    )
    await callback.answer("Готово")


# ---------------- Раздел: Шаблоны ----------------

def templates_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить шаблон", callback_data="tpl_add")],
        [InlineKeyboardButton(text="📋 Список шаблонов", callback_data="tpl_list")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")],
    ])


def template_collect_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Добавить кнопку-ссылку", callback_data="tpl_add_url_button")],
        [InlineKeyboardButton(text="⏱ Задержка между постами", callback_data="tpl_set_post_delay")],
        [InlineKeyboardButton(text="✅ Это всё, сохранить", callback_data="tpl_collect_done")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="tpl_collect_cancel")],
    ])


def template_step_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад к шаблону", callback_data="tpl_collect_back")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="tpl_collect_cancel")],
    ])


def _parse_template_button_data(raw: str) -> tuple[str, str]:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Пришли текст кнопки и ссылку.")

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) >= 2:
        text, url = lines[0], lines[1]
    else:
        match = re.match(r"^(.+?)\s*(?:\||->|—|-)\s*(https?://\S+)\s*$", raw)
        if not match:
            raise ValueError("Формат: `Текст кнопки | https://example.com`")
        text, url = match.group(1).strip(), match.group(2).strip()

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Ссылка должна начинаться с http:// или https://")
    if not text:
        raise ValueError("У кнопки должен быть текст.")
    if len(text) > 64:
        raise ValueError("Текст кнопки слишком длинный, максимум 64 символа.")
    return text, url


def _parse_template_post_delay(raw: str) -> float:
    normalized = (raw or "").strip().replace(",", ".")
    if not normalized:
        raise ValueError("Пришли число секунд, например `10`.")
    try:
        seconds = float(normalized)
    except ValueError as e:
        raise ValueError("Задержка должна быть числом секунд, например `10` или `2.5`.") from e
    if seconds < 0:
        raise ValueError("Задержка не может быть отрицательной.")
    if seconds > tpl.MAX_POST_DELAY_SECONDS:
        raise ValueError("Максимальная задержка — 86400 секунд.")
    return seconds


def _preview_file(media: dict) -> BufferedInputFile:
    stream = tpl.media_file(media)
    return BufferedInputFile(stream.getvalue(), filename=stream.name)


def _caption_kwargs(post: dict) -> dict:
    kwargs = tpl.bot_text_kwargs(post, caption=True)
    if not kwargs.get("caption"):
        kwargs.pop("caption", None)
        kwargs.pop("parse_mode", None)
        kwargs.pop("caption_entities", None)
    return kwargs


async def _send_template_preview(anchor: Message, payload: dict):
    for index, post in enumerate(tpl.messages(payload), start=1):
        media = post.get("media") or []
        markup = tpl.bot_reply_markup(post)

        if len(media) > 1:
            album = []
            for media_index, item in enumerate(media):
                kwargs = _caption_kwargs(post) if media_index == 0 else {}
                album.append(InputMediaPhoto(media=_preview_file(item), **kwargs))
            await anchor.answer_media_group(album)
            if markup:
                await anchor.answer(f"Кнопки к посту #{index}:", reply_markup=markup)
            continue

        if len(media) == 1:
            await anchor.answer_photo(
                _preview_file(media[0]),
                reply_markup=markup,
                **_caption_kwargs(post),
            )
            continue

        kwargs = tpl.bot_text_kwargs(post)
        if not kwargs.get("text"):
            kwargs["text"] = f"Кнопки к посту #{index}:"
            kwargs.pop("parse_mode", None)
            kwargs.pop("entities", None)
        await anchor.answer(reply_markup=markup, **kwargs)


@router.callback_query(F.data == "menu_templates")
@admin_only
async def menu_templates(callback: CallbackQuery):
    await callback.message.edit_text("Раздел «Шаблоны сообщений»:", reply_markup=templates_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "template_preview_noop")
@admin_only
async def template_preview_noop(callback: CallbackQuery):
    await callback.answer("Это кнопка предпросмотра.")


def _inline_result_for_post(token: str, post: dict):
    markup = tpl.bot_reply_markup(post)
    title = tpl.post_summary(post, limit=64) or "Пост"
    media = post.get("media") or []

    if len(media) == 1 and media[0].get("file_id"):
        kwargs = tpl.bot_text_kwargs(post, caption=True)
        if not kwargs.get("caption"):
            kwargs.pop("caption", None)
            kwargs.pop("parse_mode", None)
            kwargs.pop("caption_entities", None)
        return InlineQueryResultCachedPhoto(
            id=token,
            photo_file_id=media[0]["file_id"],
            title=title,
            description=title,
            reply_markup=markup,
            **kwargs,
        )

    if media:
        return None

    kwargs = tpl.bot_text_kwargs(post)
    message_text = kwargs.pop("text", "") or title or "Пост"
    if not message_text.strip():
        message_text = "Пост"
        kwargs.pop("parse_mode", None)
        kwargs.pop("entities", None)
    content = InputTextMessageContent(message_text=message_text, **kwargs)
    return InlineQueryResultArticle(
        id=token,
        title=title,
        description=title,
        input_message_content=content,
        reply_markup=markup,
    )


@router.inline_query(F.query.startswith("tpl:"))
async def inline_template_post(inline_query: InlineQuery):
    token = inline_query.query[len("tpl:"):].strip()
    if not token:
        await inline_query.answer([], cache_time=0, is_personal=True)
        return

    raw_payload = await db.get_inline_payload(token)
    payload = tpl.payload_from_json(raw_payload)
    posts = tpl.messages(payload or {})
    result = _inline_result_for_post(token, posts[0]) if posts else None
    await inline_query.answer(
        [result] if result else [],
        cache_time=0,
        is_personal=True,
    )


@router.callback_query(F.data == "tpl_add")
@admin_only
async def tpl_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddTemplate.waiting_name)
    await callback.message.edit_text("Введи короткое название шаблона (например: «Поздравление с праздником»):")
    await callback.answer()


@router.message(AddTemplate.waiting_name)
@admin_only
async def tpl_add_name(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer("Название должно быть текстом. Введи короткое название шаблона:")
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(AddTemplate.waiting_text)
    await state.update_data(posts=[], post_delay_seconds=0)
    await message.answer(
        "Теперь отправь один или несколько готовых постов.\n\n"
        "Можно присылать текст с HTML/форматированием, premium emoji, фото или альбомы из фото. "
        "После каждого поста можно добавить URL-кнопку к последнему посту и задать задержку между постами. "
        "Когда всё отправишь, нажми «Это всё, сохранить».",
        reply_markup=template_collect_kb(),
    )


@router.message(AddTemplate.waiting_text)
@admin_only
async def tpl_add_text(message: Message, state: FSMContext, bot: Bot):
    try:
        post = await tpl.post_from_message(message, bot)
    except ValueError as e:
        await message.answer(f"❌ {e}", reply_markup=template_collect_kb())
        return

    data = await state.get_data()
    posts, added = tpl.upsert_post(data.get("posts") or [], post)
    await state.update_data(posts=posts)

    media_count = sum(len(item.get("media") or []) for item in posts)
    action = "Пост добавлен" if added else "Фото добавлено к альбому"
    await message.answer(
        f"✅ {action}.\n"
        f"Сейчас в шаблоне: постов {len(posts)}, фото {media_count}.\n\n"
        "Можешь добавить кнопку-ссылку к последнему посту, отправить следующий пост или нажать «Это всё, сохранить».",
        reply_markup=template_collect_kb(),
    )


@router.callback_query(AddTemplate.waiting_text, F.data == "tpl_add_url_button")
@admin_only
async def tpl_add_url_button_start(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("posts"):
        await callback.answer("Сначала отправь пост, к которому нужно добавить кнопку.", show_alert=True)
        return

    await state.set_state(AddTemplate.waiting_button_data)
    await callback.message.edit_text(
        "Пришли текст кнопки и ссылку для последнего добавленного поста.\n\n"
        "Формат:\n"
        "`Подробнее | https://example.com`\n\n"
        "Можно также отправить двумя строками: первая строка — текст кнопки, вторая — ссылка.",
        reply_markup=template_step_kb(),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(AddTemplate.waiting_button_data)
@admin_only
async def tpl_add_url_button_finish(message: Message, state: FSMContext):
    try:
        button_text, button_url = _parse_template_button_data(message.text or "")
    except ValueError as e:
        await message.answer(f"❌ {e}", reply_markup=template_step_kb())
        return

    data = await state.get_data()
    posts = data.get("posts") or []
    if not posts:
        await state.set_state(AddTemplate.waiting_text)
        await message.answer("Сначала отправь пост, к которому нужно добавить кнопку.", reply_markup=template_collect_kb())
        return

    tpl.add_url_button(posts[-1], button_text, button_url)
    await state.update_data(posts=posts)
    await state.set_state(AddTemplate.waiting_text)
    await message.answer(
        f"✅ Кнопка «{button_text}» добавлена к последнему посту.\n\n"
        "Можешь отправить следующий пост, добавить ещё кнопку или сохранить шаблон.",
        reply_markup=template_collect_kb(),
    )


@router.callback_query(AddTemplate.waiting_text, F.data == "tpl_set_post_delay")
@admin_only
async def tpl_set_post_delay_start(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current_delay = tpl.format_seconds(data.get("post_delay_seconds") or 0)
    await state.set_state(AddTemplate.waiting_post_delay)
    await callback.message.edit_text(
        "Пришли задержку между постами шаблона в секундах.\n\n"
        f"Сейчас: {current_delay}\n"
        "Например: `10` — первый пост уйдёт сразу, второй через 10 секунд.\n"
        "`0` отключит задержку.",
        reply_markup=template_step_kb(),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(AddTemplate.waiting_post_delay)
@admin_only
async def tpl_set_post_delay_finish(message: Message, state: FSMContext):
    try:
        delay_seconds = _parse_template_post_delay(message.text or "")
    except ValueError as e:
        await message.answer(f"❌ {e}", reply_markup=template_step_kb())
        return

    await state.update_data(post_delay_seconds=delay_seconds)
    await state.set_state(AddTemplate.waiting_text)
    if delay_seconds:
        text = f"✅ Задержка между постами: {tpl.format_seconds(delay_seconds)}."
    else:
        text = "✅ Задержка между постами отключена."
    await message.answer(
        text + "\n\nМожешь продолжать добавлять посты или сохранить шаблон.",
        reply_markup=template_collect_kb(),
    )


@router.callback_query(AddTemplate.waiting_text, F.data == "tpl_collect_cancel")
@admin_only
async def tpl_add_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Добавление шаблона отменено.", reply_markup=templates_menu_kb())
    await callback.answer()


@router.callback_query(AddTemplate.waiting_button_data, F.data == "tpl_collect_cancel")
@router.callback_query(AddTemplate.waiting_post_delay, F.data == "tpl_collect_cancel")
@admin_only
async def tpl_add_cancel_from_step(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Добавление шаблона отменено.", reply_markup=templates_menu_kb())
    await callback.answer()


@router.callback_query(AddTemplate.waiting_button_data, F.data == "tpl_collect_back")
@router.callback_query(AddTemplate.waiting_post_delay, F.data == "tpl_collect_back")
@admin_only
async def tpl_collect_back(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddTemplate.waiting_text)
    data = await state.get_data()
    posts = data.get("posts") or []
    media_count = sum(len(item.get("media") or []) for item in posts)
    delay_seconds = data.get("post_delay_seconds") or 0
    delay_line = f"\nЗадержка между постами: {tpl.format_seconds(delay_seconds)}" if delay_seconds else ""
    await callback.message.edit_text(
        f"Продолжаем собирать шаблон.\n"
        f"Сейчас в шаблоне: постов {len(posts)}, фото {media_count}.{delay_line}\n\n"
        "Можешь отправить следующий пост, добавить кнопку-ссылку или сохранить шаблон.",
        reply_markup=template_collect_kb(),
    )
    await callback.answer()


@router.callback_query(AddTemplate.waiting_text, F.data == "tpl_collect_done")
@admin_only
async def tpl_add_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    posts = data.get("posts") or []
    if not posts:
        await callback.answer("Сначала отправь хотя бы один пост.", show_alert=True)
        return

    payload = tpl.make_payload(posts, post_delay_seconds=data.get("post_delay_seconds") or 0)
    summary = tpl.payload_summary(payload)
    await db.add_template(
        name=data["name"],
        text=summary,
        payload=tpl.payload_to_json(payload),
    )
    await state.clear()

    await callback.message.edit_text(
        f"✅ Шаблон «{data['name']}» сохранён.\n\nПредпросмотр ниже:",
        reply_markup=templates_menu_kb(),
    )
    await callback.answer()
    try:
        await _send_template_preview(callback.message, payload)
    except Exception as e:
        logger.warning("Не удалось отправить предпросмотр шаблона: %s", e)
        await callback.message.answer(
            "⚠️ Шаблон сохранён, но предпросмотр не удалось отправить. "
            f"Краткое содержимое:\n\n{summary}",
            reply_markup=templates_menu_kb(),
        )


@router.callback_query(F.data == "tpl_list")
@admin_only
async def tpl_list(callback: CallbackQuery):
    templates = await db.get_templates()
    if not templates:
        await callback.message.edit_text("Шаблонов пока нет.", reply_markup=templates_menu_kb())
        await callback.answer()
        return

    kb_rows = [[InlineKeyboardButton(text=f"🗑 {t['name']}", callback_data=f"tpl_del_{t['id']}")] for t in templates]
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_templates")])

    text = "📝 Шаблоны:\n\n" + "\n\n".join(f"• {t['name']}:\n{t['text'][:150]}" for t in templates)
    await callback.message.edit_text(text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await callback.answer()


@router.callback_query(F.data.startswith("tpl_del_"))
@admin_only
async def tpl_delete(callback: CallbackQuery):
    template_id = int(callback.data.split("_")[-1])
    await db.delete_template(template_id)
    await callback.answer("Удалено")
    templates = await db.get_templates()
    kb_rows = [[InlineKeyboardButton(text=f"🗑 {t['name']}", callback_data=f"tpl_del_{t['id']}")] for t in templates]
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_templates")])
    text = "📝 Шаблоны:\n\n" + ("\n\n".join(f"• {t['name']}" for t in templates) if templates else "Пусто")
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


# ---------------- Раздел: Разовая рассылка ----------------

def _broadcast_stop_kb() -> InlineKeyboardMarkup:
    # Название функции оставлено для совместимости со старым кодом.
    # В интерфейсе полная остановка заменена на паузу/продолжение.
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏸ Пауза", callback_data="bcast_pause_all"),
            InlineKeyboardButton(text="▶️ Продолжить", callback_data="bcast_resume_all"),
        ],
        [InlineKeyboardButton(text="⏯ Управление аккаунтами", callback_data="bcast_control_menu")],
    ])


def _restriction_pause_kb(run_id: int, sender_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔄 Проверить и продолжить",
            callback_data=f"bcast_check_resume_{int(run_id)}_{int(sender_id)}",
        )],
        [
            InlineKeyboardButton(text="👥 Оставшиеся", callback_data=f"bcast_remaining_{int(run_id)}"),
            InlineKeyboardButton(text="📋 Результаты", callback_data=f"bcast_results_{int(run_id)}"),
        ],
    ])


async def _sender_accounts_for_current_owner() -> list[dict]:
    current_owner_id = db.get_current_owner_id()
    if db.is_root_admin(current_owner_id):
        return await userbot.get_sender_accounts()
    return await userbot.get_sender_accounts(owner_id=current_owner_id)


def _account_label_map(accounts: list[dict]) -> dict[int, str]:
    return {int(account["id"]): account["label"] for account in accounts}


def _filter_selected_account_ids(selected_account_ids: list[int] | None, accounts: list[dict]) -> list[int]:
    available_account_ids = {int(account["id"]) for account in accounts}
    return [
        int(account_id)
        for account_id in (selected_account_ids or [])
        if int(account_id) in available_account_ids
    ]


async def _send_broadcast_account_selector(
    callback: CallbackQuery,
    state: FSMContext,
    selected_account_ids: list[int] | None = None,
):
    accounts = await _sender_accounts_for_current_owner()
    if not accounts:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Авторизация юзербота", callback_data="menu_auth")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")],
        ])
        await callback.message.edit_text(
            "Нет авторизованных аккаунтов для отправки. Сначала авторизуй хотя бы один юзербот-аккаунт.",
            reply_markup=kb,
        )
        await callback.answer()
        return

    data = await state.get_data()
    selected_account_ids = _filter_selected_account_ids(selected_account_ids, accounts)
    if not selected_account_ids:
        current_owner_id = db.get_current_owner_id()
        current_account = next(
            (account for account in accounts if account["owner_id"] == current_owner_id),
            None,
        )
        selected_account_ids = [
            int((current_account or accounts[0])["id"])
        ]

    await state.update_data(selected_account_ids=selected_account_ids)
    await state.set_state(BroadcastNow.choosing_accounts)

    selected_set = set(selected_account_ids)
    kb_rows = []
    for account in accounts:
        account_id = int(account["id"])
        mark = "✅" if account_id in selected_set else "⬜️"
        health_icon, _health_text = _account_health_text(account)
        kb_rows.append([
            InlineKeyboardButton(
                text=f"{mark} {health_icon} {account['label']}",
                callback_data=f"bcast_acc_toggle_{account_id}",
            )
        ])
    if len(accounts) > 1:
        kb_rows.append([InlineKeyboardButton(text="☑️ Выбрать все", callback_data="bcast_acc_all")])
    kb_rows.append([InlineKeyboardButton(text="➡️ Продолжить", callback_data="bcast_acc_next")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_broadcast")])

    group_name = data.get("group_name", "Все")
    selected_labels = [
        account["label"]
        for account in accounts
        if int(account["id"]) in selected_set
    ]
    await callback.message.edit_text(
        "Выбери аккаунты, с которых отправлять рассылку.\n\n"
        f"Группа получателей: {group_name}\n"
        f"Выбрано: {len(selected_account_ids)}\n"
        + "\n".join(f"• {label}" for label in selected_labels),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await callback.answer()


async def _send_broadcast_skip_dialog(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_account_ids = data.get("selected_account_ids") or []
    skip_chat_title = data.get("skip_existing_chat_title")
    skip_line = (
        f"Сейчас включена проверка: {skip_chat_title}"
        if skip_chat_title
        else "Сейчас проверка выключена."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Проверять чат перед отправкой", callback_data="bcast_skip_set")],
        [InlineKeyboardButton(text="➡️ Без проверки", callback_data="bcast_skip_none")],
        [InlineKeyboardButton(text="⬅️ Назад к аккаунтам", callback_data="bcast_skip_back_accounts")],
    ])
    await state.set_state(BroadcastNow.choosing_skip_check)
    await callback.message.edit_text(
        "Фильтр перед рассылкой\n\n"
        "Можно указать чат, группу или канал. Если получатель уже есть там, рассылка пропустит его "
        "и запишет это в логи как «пропущено».\n\n"
        "Важно: каждый выбранный юзербот-аккаунт должен состоять в этом чате и иметь возможность видеть участников.\n\n"
        f"Выбрано аккаунтов: {len(selected_account_ids)}\n"
        f"{skip_line}",
        reply_markup=kb,
    )
    await callback.answer()


async def _build_broadcast_confirm(data: dict, accounts: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    selected_account_ids = _filter_selected_account_ids(data.get("selected_account_ids"), accounts)
    group_name = data["group_name"]
    employees = await db.get_employees(group_name=group_name)
    labels = _account_label_map(accounts)
    account_lines = "\n".join(
        f"• {labels.get(account_id, f'id {account_id}')}"
        for account_id in selected_account_ids
    )

    if data["template_id"] == 0:
        templates = await db.get_templates()
        if not templates:
            raise ValueError("Шаблонов больше нет. Добавь шаблон и запусти рассылку заново.")
        template_title = f"🔀 Ротация всех шаблонов ({len(templates)})"
        preview = "\n\n".join(f"• {t['name']}:\n{t['text'][:120]}" for t in templates[:3])
        if len(templates) > 3:
            preview += f"\n\n...и ещё {len(templates) - 3}"
    else:
        template = await db.get_template(data["template_id"])
        if not template:
            raise ValueError("Шаблон не найден. Выбери заново.")
        template_title = f"«{template['name']}»"
        preview = template["text"][:300]

    skip_chat_title = data.get("skip_existing_chat_title")
    skip_line = (
        f"Фильтр: пропускать тех, кто уже есть в «{skip_chat_title}»"
        if skip_chat_title
        else "Фильтр: без проверки чата"
    )
    auto_switch = await _auto_switch_technical_enabled()
    auto_switch_line = (
        "Тех. failover: ВКЛ — при разлогине/сбое соединения очередь перейдёт на другой выбранный аккаунт"
        if auto_switch
        else "Тех. failover: ВЫКЛ"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="bcast_confirm_yes")],
        [InlineKeyboardButton(text="⬅️ Назад к фильтру", callback_data="bcast_confirm_back_skip")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_main")],
    ])
    text = (
        f"Шаблон: {template_title}\n"
        f"Получателей: {len(employees)} (группа: {group_name})\n"
        f"Аккаунтов отправки: {len(selected_account_ids)}\n"
        f"{account_lines}\n"
        f"{skip_line}\n"
        f"{auto_switch_line}\n\n"
        f"Текст:\n{preview}\n\nОтправляем?"
    )
    return text, kb


async def _show_broadcast_confirm(anchor: Message | CallbackQuery, state: FSMContext, *, edit: bool = True):
    data = await state.get_data()
    accounts = await _sender_accounts_for_current_owner()
    selected_account_ids = _filter_selected_account_ids(data.get("selected_account_ids"), accounts)
    if not selected_account_ids:
        raise ValueError("Выбери хотя бы один аккаунт.")
    await state.update_data(selected_account_ids=selected_account_ids)
    text, kb = await _build_broadcast_confirm(data, accounts)
    await state.set_state(BroadcastNow.confirming)
    if isinstance(anchor, CallbackQuery):
        await anchor.message.edit_text(text, reply_markup=kb)
        await anchor.answer()
    elif edit:
        await anchor.edit_text(text, reply_markup=kb)
    else:
        await anchor.answer(text, reply_markup=kb)


async def _active_broadcast_accounts_for_current_owner() -> list[dict]:
    active_account_ids = await userbot.get_active_broadcast_sender_ids()
    paused_account_ids = set(await userbot.get_paused_broadcast_sender_ids())
    current_owner_id = db.get_current_owner_id()

    accounts = await userbot.get_sender_accounts(sender_ids=active_account_ids, authorized_only=False)
    if not db.is_root_admin(current_owner_id):
        accounts = [account for account in accounts if account["owner_id"] == current_owner_id]
    labels = _account_label_map(accounts)
    result = []
    for account in accounts:
        account_id = int(account["id"])
        health_icon, health_text = _account_health_text(account)
        label = labels.get(account_id, f"id {account_id}")
        result.append({
            "id": account_id,
            "label": f"{health_icon} {label}",
            "paused": account_id in paused_account_ids,
            "health_status": account.get("health_status") or "unknown",
            "health_text": health_text,
        })
    return result


@router.callback_query(F.data == "menu_broadcast")
@admin_only
async def menu_broadcast(callback: CallbackQuery, state: FSMContext):
    templates = await db.get_templates()
    if not templates:
        await callback.message.edit_text("Сначала добавь хотя бы один шаблон.", reply_markup=templates_menu_kb())
        await callback.answer()
        return
    kb_rows = []
    if len(templates) > 1:
        kb_rows.append([InlineKeyboardButton(text="🔀 Ротация всех шаблонов", callback_data="bcast_tpl_0")])
    kb_rows.extend([[InlineKeyboardButton(text=t["name"], callback_data=f"bcast_tpl_{t['id']}")] for t in templates])
    kb_rows.append([InlineKeyboardButton(text="📂 Последние рассылки", callback_data="bcast_recent")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")])
    await state.set_state(BroadcastNow.choosing_template)
    rotation_hint = "\n\nДля чередования текстов выбери «🔀 Ротация всех шаблонов»." if len(templates) > 1 else ""
    await callback.message.edit_text(
        "Выбери шаблон для рассылки:" + rotation_hint,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await callback.answer()



@router.callback_query(F.data == "bcast_recent")
@admin_only
async def broadcast_recent(callback: CallbackQuery):
    runs = await db.get_recent_broadcast_runs(15)
    if not runs:
        await callback.answer("Запусков пока нет.", show_alert=True)
        return
    rows = []
    lines = []
    for run in runs:
        pending = await db.get_broadcast_run_items(int(run["id"]), ["pending", "sending"])
        status = run.get("status") or "unknown"
        icon = "⏸" if status == "paused" else ("✅" if status.startswith("completed") else "▶️")
        lines.append(f"{icon} #{run['id']} — {status}; отправлено {run.get('sent', 0)}, осталось {len(pending)}")
        rows.append([InlineKeyboardButton(text=f"{icon} Рассылка #{run['id']}", callback_data=f"bcast_results_{run['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_broadcast")])
    await callback.message.answer("📂 Последние рассылки\n\n" + "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(BroadcastNow.choosing_template, F.data.startswith("bcast_tpl_"))
@admin_only
async def broadcast_choose_group(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split("_")[-1])
    await state.update_data(template_id=template_id)
    groups = await db.get_groups()
    if "Все" not in groups:
        groups.append("Все")
    kb_rows = [[InlineKeyboardButton(text=g, callback_data=f"bcast_grp_{g}")] for g in groups]
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_broadcast")])
    await state.set_state(BroadcastNow.choosing_group)
    await callback.message.edit_text("Кому отправить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await callback.answer()


@router.callback_query(BroadcastNow.choosing_group, F.data.startswith("bcast_grp_"))
@admin_only
async def broadcast_choose_accounts(callback: CallbackQuery, state: FSMContext):
    group_name = callback.data[len("bcast_grp_"):]
    await state.update_data(group_name=group_name)
    await _send_broadcast_account_selector(callback, state)


@router.callback_query(BroadcastNow.choosing_accounts, F.data.startswith("bcast_acc_toggle_"))
@admin_only
async def broadcast_toggle_account(callback: CallbackQuery, state: FSMContext):
    account_id = int(callback.data.split("_")[-1])
    data = await state.get_data()
    accounts = await _sender_accounts_for_current_owner()
    selected_account_ids = _filter_selected_account_ids(data.get("selected_account_ids"), accounts)
    if account_id in selected_account_ids:
        selected_account_ids.remove(account_id)
    else:
        selected_account_ids.append(account_id)
    await _send_broadcast_account_selector(callback, state, selected_account_ids)


@router.callback_query(BroadcastNow.choosing_accounts, F.data == "bcast_acc_all")
@admin_only
async def broadcast_select_all_accounts(callback: CallbackQuery, state: FSMContext):
    accounts = await _sender_accounts_for_current_owner()
    selected_account_ids = [int(account["id"]) for account in accounts]
    await _send_broadcast_account_selector(callback, state, selected_account_ids)


@router.callback_query(BroadcastNow.choosing_accounts, F.data == "bcast_acc_next")
@admin_only
async def broadcast_choose_skip_check(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    accounts = await _sender_accounts_for_current_owner()
    selected_account_ids = _filter_selected_account_ids(data.get("selected_account_ids"), accounts)
    if not selected_account_ids:
        await callback.answer("Выбери хотя бы один аккаунт.", show_alert=True)
        return
    await state.update_data(selected_account_ids=selected_account_ids)
    await _send_broadcast_skip_dialog(callback, state)


@router.callback_query(BroadcastNow.choosing_skip_check, F.data == "bcast_skip_back_accounts")
@admin_only
async def broadcast_skip_back_accounts(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await _send_broadcast_account_selector(callback, state, data.get("selected_account_ids"))


@router.callback_query(BroadcastNow.confirming, F.data == "bcast_confirm_back_skip")
@admin_only
async def broadcast_confirm_back_skip(callback: CallbackQuery, state: FSMContext):
    await _send_broadcast_skip_dialog(callback, state)


@router.callback_query(BroadcastNow.choosing_skip_check, F.data == "bcast_skip_none")
@admin_only
async def broadcast_skip_none(callback: CallbackQuery, state: FSMContext):
    await state.update_data(skip_existing_chat=None, skip_existing_chat_title=None)
    try:
        await _show_broadcast_confirm(callback, state)
    except ValueError as e:
        await callback.message.edit_text(f"❌ {e}", reply_markup=main_menu_kb())
        await callback.answer()


@router.callback_query(BroadcastNow.choosing_skip_check, F.data == "bcast_skip_set")
@admin_only
async def broadcast_skip_wait_chat(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BroadcastNow.waiting_skip_chat)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Продолжить без проверки", callback_data="bcast_skip_none")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="bcast_skip_dialog")],
    ])
    await callback.message.edit_text(
        "Отправь чат для проверки.\n\n"
        "Подойдёт @username, ссылка вида https://t.me/example или числовой id. "
        "Если получатель уже состоит в этом чате, рассылка пропустит его.\n\n"
        "Проверка идёт через общие группы между юзерботом и получателем, поэтому "
        "работает даже при скрытом списке участников. Все выбранные юзербот-аккаунты "
        "должны сами состоять в этом чате.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(BroadcastNow.waiting_skip_chat, F.data == "bcast_skip_dialog")
@admin_only
async def broadcast_skip_dialog_from_wait(callback: CallbackQuery, state: FSMContext):
    await _send_broadcast_skip_dialog(callback, state)


@router.callback_query(BroadcastNow.waiting_skip_chat, F.data == "bcast_skip_none")
@admin_only
async def broadcast_skip_none_from_wait(callback: CallbackQuery, state: FSMContext):
    await broadcast_skip_none(callback, state)


@router.message(BroadcastNow.waiting_skip_chat)
@admin_only
async def broadcast_skip_chat_received(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пришли чат текстом: @username, ссылку t.me или числовой id.")
        return
    chat_ref = message.text.strip()
    data = await state.get_data()
    accounts = await _sender_accounts_for_current_owner()
    selected_account_ids = _filter_selected_account_ids(data.get("selected_account_ids"), accounts)
    if not selected_account_ids:
        await state.clear()
        await message.answer("❌ Не выбран ни один аккаунт для рассылки.", reply_markup=main_menu_kb())
        return

    status = await message.answer("🔎 Проверяю доступ выбранных аккаунтов к чату...")
    try:
        skip_info = await userbot.validate_membership_skip_chat(chat_ref, selected_account_ids)
    except ValueError as e:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Продолжить без проверки", callback_data="bcast_skip_none")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="bcast_skip_dialog")],
        ])
        await status.edit_text(
            f"❌ Не удалось включить проверку:\n{e}\n\n"
            "Проверь, что ссылка верная и каждый выбранный юзербот-аккаунт состоит в этом чате.",
            reply_markup=kb,
        )
        return

    await state.update_data(
        skip_existing_chat=skip_info["chat"],
        skip_existing_chat_title=skip_info["title"],
    )
    try:
        await _show_broadcast_confirm(status, state, edit=True)
    except ValueError as e:
        await state.clear()
        await status.edit_text(f"❌ {e}", reply_markup=main_menu_kb())


@router.callback_query(BroadcastNow.confirming, F.data == "bcast_confirm_yes")
@admin_only
async def broadcast_execute(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    employees = await db.get_employees(group_name=data["group_name"])
    total = len(employees)
    accounts = await _sender_accounts_for_current_owner()
    selected_account_ids = _filter_selected_account_ids(data.get("selected_account_ids"), accounts)
    if not selected_account_ids:
        await state.clear()
        await callback.message.edit_text("❌ Не выбран ни один аккаунт для рассылки.", reply_markup=main_menu_kb())
        await callback.answer()
        return
    account_labels = _account_label_map(accounts)
    skip_chat_title = data.get("skip_existing_chat_title")
    skip_line = f"Фильтр: {skip_chat_title}\n" if skip_chat_title else ""
    await callback.message.edit_text(
        "⏳ Рассылка запущена\n\n"
        f"Аккаунтов отправки: {len(selected_account_ids)}\n"
        f"{skip_line}"
        f"Прогресс: 0/{total} (0%)\n"
        "✅ Успешно: 0\n"
        "⏭ Пропущено: 0\n"
        "❌ Ошибок: 0\n"
        "📊 Скоринг доставки: 0%",
        reply_markup=_broadcast_stop_kb(),
    )
    await callback.answer()

    async def update_broadcast_progress(progress: dict):
        processed = progress["processed"]
        sent = progress["sent"]
        failed = progress["failed"]
        skipped = progress.get("skipped", 0)
        percent = round(processed * 100 / total) if total else 100
        attempts = sent + failed
        score = round(sent * 100 / attempts) if attempts else 0
        current = progress.get("current") or {}
        current_label = current.get("full_name") or _employee_handle(current)
        status = progress.get("status")
        if status == "skipped":
            last_status = "⏭ пропущено"
        elif progress.get("ok"):
            last_status = "✅ отправлено"
        else:
            last_status = "❌ ошибка"
        sender_id = progress.get("sender_id") or progress.get("sender_account_id")
        sender_label = account_labels.get(sender_id, f"id {sender_id}") if sender_id else "неизвестно"
        error = progress.get("error")
        error_line = f"\nПоследняя ошибка: {error[:120]}" if error else ""
        paused_accounts = progress.get("paused_accounts") or []
        pause_line = (
            "\n⏸ Рассылка поставлена на паузу. Если Telegram ограничил отправку, "
            "сначала дождись снятия ограничения, затем нажми «Продолжить»."
            if paused_accounts else ""
        )
        technical_disabled = progress.get("technical_disabled_accounts") or []
        failover_count = int(progress.get("failover_count") or 0)
        failover_line = ""
        if technical_disabled:
            disabled_labels = ", ".join(
                account_labels.get(account_id, f"id {account_id}")
                for account_id in technical_disabled
            )
            failover_line = (
                f"\n⚠️ Технически исключены: {disabled_labels}"
                + (f"\n🔁 Переназначено заданий: {failover_count}" if failover_count else "")
            )
        rotation_line = (
            f"\n🔀 Шаблонов использовано: {progress['templates_used']}"
            if data.get("template_id") == 0
            else ""
        )
        restriction_sender_id = progress.get("restriction_sender_id")
        progress_kb = (
            _restriction_pause_kb(int(progress.get("run_id")), int(restriction_sender_id))
            if progress.get("status") == "paused_restriction" and progress.get("run_id") and restriction_sender_id
            else _broadcast_stop_kb()
        )
        progress_title = (
            "⏸ Рассылка приостановлена\n\n"
            if progress.get("status") == "paused_restriction"
            else "⏳ Рассылка идёт\n\n"
        )
        try:
            await callback.message.edit_text(
                progress_title
                + f"Аккаунтов отправки: {len(selected_account_ids)}\n"
                f"{skip_line}"
                f"Прогресс: {processed}/{total} ({percent}%)\n"
                f"✅ Успешно: {sent}\n"
                f"⏭ Пропущено: {skipped}\n"
                f"❌ Ошибок: {failed}\n"
                f"📊 Скоринг доставки: {score}%\n"
                f"Аккаунт: {sender_label}\n"
                f"Последний: {current_label} — {last_status}"
                f"{rotation_line}"
                f"{error_line}"
                f"{failover_line}"
                f"{pause_line}",
                reply_markup=progress_kb,
            )
        except Exception as e:
            logger.debug("Не удалось обновить live-скоринг рассылки: %s", e)

    try:
        result = await userbot.broadcast(
            data["template_id"],
            employees,
            progress_callback=update_broadcast_progress,
            sender_account_ids=selected_account_ids,
            skip_existing_chat=data.get("skip_existing_chat"),
            group_name=data.get("group_name"),
        )
    except ValueError as e:
        await state.clear()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(f"❌ {e}", reply_markup=main_menu_kb())
        return

    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    attempts = result["sent"] + result["failed"]
    final_score = round(result["sent"] * 100 / attempts) if attempts else 0
    rotation_note = f"\nШаблонов использовано: {result['templates_used']}" if data.get("template_id") == 0 else ""
    skip_note = ""
    if result.get("skip_chat_title"):
        skip_note = f"\nФильтр: {result['skip_chat_title']}"
    stopped_accounts = result.get("stopped_accounts") or []
    stopped_note = ""
    if stopped_accounts:
        stopped_labels = ", ".join(account_labels.get(account_id, f"id {account_id}") for account_id in stopped_accounts)
        stopped_note = f"\nОстановлено на аккаунтах: {stopped_labels}"
    technical_disabled = result.get("technical_disabled_accounts") or []
    technical_note = ""
    if technical_disabled:
        disabled_labels = ", ".join(
            account_labels.get(account_id, f"id {account_id}")
            for account_id in technical_disabled
        )
        technical_note = f"\n⚠️ Технически исключены: {disabled_labels}"
        if result.get("failover_count"):
            technical_note += f"\n🔁 Переназначено заданий: {result['failover_count']}"
    status_title = "⏹ Рассылка остановлена" if result.get("stopped") else "✅ Готово!"
    processed = result["sent"] + result["failed"] + result.get("skipped", 0)
    processed_note = f"\nОбработано: {processed}/{total}" if result.get("stopped") else ""
    result_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📋 Результаты этой рассылки",
            callback_data=f"bcast_results_{result['run_id']}",
        )],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu_main")],
    ])
    await callback.message.answer(
        f"{status_title}\nОтправлено: {result['sent']}\nПропущено: {result.get('skipped', 0)}\nОшибок: {result['failed']}\n"
        f"Скоринг доставки: {final_score}%"
        f"{processed_note}"
        f"{rotation_note}"
        f"{skip_note}"
        f"{technical_note}"
        f"{stopped_note}",
        reply_markup=result_kb
    )


@router.callback_query(F.data == "bcast_control_menu")
@admin_only
async def broadcast_control_menu(callback: CallbackQuery):
    accounts = await _active_broadcast_accounts_for_current_owner()
    if not accounts:
        await callback.answer("Активных рассылок нет.", show_alert=True)
        return

    kb_rows = [
        [
            InlineKeyboardButton(text="⏸ Пауза для всех", callback_data="bcast_pause_all"),
            InlineKeyboardButton(text="▶️ Продолжить все", callback_data="bcast_resume_all"),
        ]
    ]
    for account in accounts:
        if account.get("paused"):
            text = f"▶️ {account['label']}"
            data = f"bcast_resume_{account['id']}"
        else:
            text = f"⏸ {account['label']}"
            data = f"bcast_pause_{account['id']}"
        kb_rows.append([InlineKeyboardButton(text=text, callback_data=data)])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")])

    status_lines = [
        f"{'⏸' if account.get('paused') else '▶️'} {account['label']}"
        for account in accounts
    ]
    await callback.message.answer(
        "⏯ Управление активной рассылкой\n\n"
        "Пауза сохраняет текущую очередь. После «Продолжить» рассылка идёт с того же места.\n\n"
        + "\n".join(status_lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await callback.answer()


@router.callback_query(F.data == "bcast_pause_all")
@admin_only
async def broadcast_pause_all(callback: CallbackQuery):
    accounts = await _active_broadcast_accounts_for_current_owner()
    changed = []
    for account in accounts:
        changed.extend(await userbot.request_broadcast_pause(account["id"]))
    if not changed:
        await callback.answer("Активных рассылок нет или они уже на паузе.", show_alert=True)
        return
    await callback.answer("⏸ Рассылка поставлена на паузу.", show_alert=True)


@router.callback_query(F.data == "bcast_resume_all")
@admin_only
async def broadcast_resume_all(callback: CallbackQuery):
    accounts = await _active_broadcast_accounts_for_current_owner()
    changed = []
    for account in accounts:
        changed.extend(await userbot.request_broadcast_resume(account["id"]))
    if not changed:
        await callback.answer("Нет рассылок на паузе.", show_alert=True)
        return
    await callback.answer("▶️ Рассылка продолжена.", show_alert=True)


@router.callback_query(F.data.startswith("bcast_pause_") & (F.data != "bcast_pause_all"))
@admin_only
async def broadcast_pause_account(callback: CallbackQuery):
    try:
        account_id = int(callback.data.rsplit("_", 1)[-1])
    except ValueError:
        await callback.answer()
        return
    accounts = await _active_broadcast_accounts_for_current_owner()
    if account_id not in {a["id"] for a in accounts}:
        await callback.answer("Эта рассылка уже не активна или недоступна.", show_alert=True)
        return
    changed = await userbot.request_broadcast_pause(account_id)
    await callback.answer("⏸ Пауза включена." if changed else "Уже на паузе.", show_alert=True)


@router.callback_query(F.data.startswith("bcast_resume_") & (F.data != "bcast_resume_all"))
@admin_only
async def broadcast_resume_account(callback: CallbackQuery):
    try:
        account_id = int(callback.data.rsplit("_", 1)[-1])
    except ValueError:
        await callback.answer()
        return
    accounts = await _active_broadcast_accounts_for_current_owner()
    if account_id not in {a["id"] for a in accounts}:
        await callback.answer("Эта рассылка уже не активна или недоступна.", show_alert=True)
        return
    changed = await userbot.request_broadcast_resume(account_id)
    await callback.answer("▶️ Рассылка продолжена." if changed else "Этот аккаунт не на паузе.", show_alert=True)


def _broadcast_result_recipient(item: dict) -> str:
    username = item.get("username")
    full_name = item.get("full_name")
    telegram_id = item.get("telegram_id")
    if username:
        handle = f"@{username.lstrip('@')}"
        return f"{full_name} ({handle})" if full_name else handle
    if telegram_id:
        return f"{full_name} (id {telegram_id})" if full_name else f"id {telegram_id}"
    return full_name or "неизвестный пользователь"


async def _show_broadcast_result_list(callback: CallbackQuery, run_id: int, sent_only: bool):
    run = await db.get_broadcast_run(run_id)
    if not run:
        await callback.answer("Отчёт не найден.", show_alert=True)
        return
    statuses = ["sent"] if sent_only else ["failed", "skipped"]
    items = await db.get_broadcast_run_items(run_id, statuses)
    title = "✅ Кому отправлено" if sent_only else "❌ Кому не отправлено"
    lines = []
    for item in items[:60]:
        line = f"• {_broadcast_result_recipient(item)}"
        if not sent_only:
            status_label = "пропущено" if item.get("status") == "skipped" else "ошибка"
            reason = (item.get("error") or "причина не указана").replace("\n", " ")
            line += f" — {status_label}: {reason[:140]}"
        lines.append(line)
    if len(items) > 60:
        lines.append(f"\n…ещё {len(items) - 60}. Полный список можно скачать CSV из отчёта.")
    text = f"{title} — рассылка #{run_id}\nВсего: {len(items)}\n\n" + ("\n".join(lines) if lines else "Список пуст.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К отчёту", callback_data=f"bcast_results_{run_id}")],
    ])
    await callback.message.answer(text[:3900], reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("bcast_results_"))
@admin_only
async def broadcast_results(callback: CallbackQuery):
    try:
        run_id = int(callback.data.rsplit("_", 1)[-1])
    except ValueError:
        await callback.answer()
        return
    run = await db.get_broadcast_run(run_id)
    if not run:
        await callback.answer("Отчёт не найден.", show_alert=True)
        return
    live_stats = await db.refresh_broadcast_run_stats(run_id)
    run.update({key: live_stats[key] for key in ("sent", "failed", "skipped")})
    not_sent = int(run.get("failed") or 0) + int(run.get("skipped") or 0)
    pending_items = await db.get_broadcast_run_items(run_id, ["pending", "sending"])
    kb_rows = [
        [InlineKeyboardButton(text=f"✅ Отправлено ({run.get('sent', 0)})", callback_data=f"bcast_result_sent_{run_id}")],
        [InlineKeyboardButton(text=f"❌ Не отправлено ({not_sent})", callback_data=f"bcast_result_unsent_{run_id}")],
    ]
    if pending_items:
        kb_rows.append([InlineKeyboardButton(text=f"👥 Оставшиеся ({len(pending_items)})", callback_data=f"bcast_remaining_{run_id}")])
        if (run.get("status") == "paused" and run.get("pause_sender_account_id")
                and str(run.get("pause_reason") or "").startswith("restriction:")):
            kb_rows.append([InlineKeyboardButton(
                text="🔄 Проверить и продолжить",
                callback_data=f"bcast_check_resume_{run_id}_{int(run['pause_sender_account_id'])}",
            )])
        elif run.get("status") == "paused":
            kb_rows.append([InlineKeyboardButton(
                text="▶️ Продолжить с checkpoint",
                callback_data=f"bcast_resume_checkpoint_{run_id}",
            )])
    kb_rows.extend([
        [InlineKeyboardButton(text="📥 Скачать полный CSV", callback_data=f"bcast_result_csv_{run_id}")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu_main")],
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await callback.message.answer(
        f"📋 Результаты рассылки #{run_id}\n\n"
        f"Всего: {run.get('total', 0)}\n"
        f"✅ Отправлено: {run.get('sent', 0)}\n"
        f"⏭ Пропущено: {run.get('skipped', 0)}\n"
        f"❌ Ошибок: {run.get('failed', 0)}\n"
        f"Статус: {run.get('status', 'unknown')}",
        reply_markup=kb,
    )
    await callback.answer()



@router.callback_query(F.data.startswith("bcast_remaining_"))
@admin_only
async def broadcast_remaining(callback: CallbackQuery):
    try:
        run_id = int(callback.data.rsplit("_", 1)[-1])
    except ValueError:
        await callback.answer()
        return
    run = await db.get_broadcast_run(run_id)
    if not run:
        await callback.answer("Рассылка не найдена.", show_alert=True)
        return
    items = await db.get_broadcast_run_items(run_id, ["pending", "sending"])
    lines = [f"• {_broadcast_result_recipient(item)}" for item in items[:60]]
    if len(items) > 60:
        lines.append(f"…ещё {len(items) - 60}")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["position", "full_name", "username", "telegram_id", "sender_account_id", "template_id", "status", "error"])
    for item in items:
        writer.writerow([
            item.get("position"), item.get("full_name"), item.get("username"), item.get("telegram_id"),
            item.get("sender_account_id"), item.get("template_id"), item.get("status"), item.get("error"),
        ])
    kb_rows = [[InlineKeyboardButton(text="⬅️ К отчёту", callback_data=f"bcast_results_{run_id}")]]
    if (run.get("status") == "paused" and run.get("pause_sender_account_id")
            and str(run.get("pause_reason") or "").startswith("restriction:")):
        kb_rows.insert(0, [InlineKeyboardButton(
            text="🔄 Проверить и продолжить",
            callback_data=f"bcast_check_resume_{run_id}_{int(run['pause_sender_account_id'])}",
        )])
    elif run.get("status") == "paused":
        kb_rows.insert(0, [InlineKeyboardButton(
            text="▶️ Продолжить с checkpoint",
            callback_data=f"bcast_resume_checkpoint_{run_id}",
        )])
    await callback.message.answer(
        f"👥 Оставшиеся — рассылка #{run_id}\nВсего: {len(items)}\n\n" + ("\n".join(lines) if lines else "Список пуст."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    if items:
        await callback.message.answer_document(
            BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename=f"broadcast_{run_id}_remaining.csv"),
            caption=f"👥 Оставшиеся пользователи рассылки #{run_id}",
        )
    await callback.answer()



@router.callback_query(F.data.startswith("bcast_resume_checkpoint_"))
@admin_only
async def broadcast_resume_checkpoint(callback: CallbackQuery):
    try:
        run_id = int(callback.data.rsplit("_", 1)[-1])
    except ValueError:
        await callback.answer()
        return
    run = await db.get_broadcast_run(run_id)
    if not run:
        await callback.answer("Рассылка не найдена.", show_alert=True)
        return
    if str(run.get("pause_reason") or "").startswith("restriction:"):
        await callback.answer("Сначала используй «Проверить и продолжить».", show_alert=True)
        return
    active_ids = await userbot.get_active_broadcast_sender_ids()
    run_sender_ids = set(db.parse_broadcast_sender_ids(run))
    active_for_run = [sender_id for sender_id in active_ids if sender_id in run_sender_ids]
    if active_for_run:
        changed = []
        for sender_id in active_for_run:
            changed.extend(await userbot.request_broadcast_resume(sender_id))
        if changed:
            await db.set_broadcast_run_running(run_id)
            await callback.answer("▶️ Продолжено с текущего места.", show_alert=True)
            return
    await callback.answer("▶️ Восстанавливаю очередь...")
    try:
        result = await userbot.resume_broadcast_run(run_id)
    except ValueError as exc:
        await callback.message.answer(f"⚠️ Не удалось продолжить: {exc}")
        return
    await callback.message.answer(
        f"✅ Обработка checkpoint завершена.\n"
        f"Отправлено: {result.get('sent', 0)}\nОшибок: {result.get('failed', 0)}\n"
        f"Осталось: {result.get('pending', 0)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Результаты", callback_data=f"bcast_results_{run_id}")]
        ]),
    )


@router.callback_query(F.data.startswith("bcast_check_resume_"))
@admin_only
async def broadcast_check_and_resume(callback: CallbackQuery):
    parts = callback.data.split("_")
    try:
        run_id = int(parts[-2])
        sender_id = int(parts[-1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    run = await db.get_broadcast_run(run_id)
    if not run:
        await callback.answer("Рассылка не найдена.", show_alert=True)
        return
    allowed_sender_ids = set(db.parse_broadcast_sender_ids(run))
    if sender_id not in allowed_sender_ids and sender_id != run.get("pause_sender_account_id"):
        await callback.answer("Этот аккаунт не относится к данной рассылке.", show_alert=True)
        return
    if run.get("status") not in {"paused", "running"}:
        await callback.answer("Эта рассылка уже завершена.", show_alert=True)
        return
    await callback.answer("🔄 Проверяю статус аккаунта...")
    try:
        result = await userbot.check_spambot_status(sender_id)
    except Exception as exc:
        await callback.message.answer(f"⚠️ Не удалось проверить SpamBot: {type(exc).__name__}: {exc}")
        return
    restricted = result.get("restricted")
    until = result.get("until")
    if restricted is True:
        until_text = until.strftime("%d.%m.%Y %H:%M UTC") if until else "срок не определён"
        await db.set_broadcast_run_paused(run_id, f"restriction:Ограничение до {until_text}", sender_id)
        await callback.message.answer(
            f"🚫 Ограничение ещё действует.\n\nДо: {until_text}\nРассылка остаётся на паузе.",
            reply_markup=_restriction_pause_kb(run_id, sender_id),
        )
        return
    if restricted is None:
        await callback.message.answer(
            "⚠️ SpamBot не дал однозначного ответа. Рассылка остаётся на паузе.\n\n"
            + (result.get("text") or ""),
            reply_markup=_restriction_pause_kb(run_id, sender_id),
        )
        return

    active_run_id = await userbot.get_restriction_run_for_sender(sender_id)
    if active_run_id == run_id:
        resumed_ids = await userbot.release_restriction_pause(run_id)
        await callback.message.answer(
            "✅ Ограничение больше не обнаружено.\n"
            "▶️ Продолжаю сохранённую очередь с текущего места.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Результаты", callback_data=f"bcast_results_{run_id}")]
            ]),
        )
        return

    # После рестарта процесса активных Event уже нет — восстанавливаем pending из SQLite.
    await callback.message.answer("✅ Ограничение больше не обнаружено. Восстанавливаю сохранённую очередь…")
    try:
        result_run = await userbot.resume_broadcast_run(run_id)
    except ValueError as exc:
        await callback.message.answer(f"⚠️ Не удалось продолжить: {exc}")
        return
    await callback.message.answer(
        f"✅ Продолжение завершено/запущено.\n"
        f"Отправлено всего: {result_run.get('sent', 0)}\n"
        f"Ошибок: {result_run.get('failed', 0)}\n"
        f"Осталось: {result_run.get('pending', 0)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Результаты", callback_data=f"bcast_results_{run_id}")]
        ]),
    )


@router.callback_query(F.data.startswith("bcast_result_sent_"))
@admin_only
async def broadcast_result_sent(callback: CallbackQuery):
    try:
        run_id = int(callback.data.rsplit("_", 1)[-1])
    except ValueError:
        await callback.answer()
        return
    await _show_broadcast_result_list(callback, run_id, True)


@router.callback_query(F.data.startswith("bcast_result_unsent_"))
@admin_only
async def broadcast_result_unsent(callback: CallbackQuery):
    try:
        run_id = int(callback.data.rsplit("_", 1)[-1])
    except ValueError:
        await callback.answer()
        return
    await _show_broadcast_result_list(callback, run_id, False)


@router.callback_query(F.data.startswith("bcast_result_csv_"))
@admin_only
async def broadcast_result_csv(callback: CallbackQuery):
    try:
        run_id = int(callback.data.rsplit("_", 1)[-1])
    except ValueError:
        await callback.answer()
        return
    run = await db.get_broadcast_run(run_id)
    if not run:
        await callback.answer("Отчёт не найден.", show_alert=True)
        return
    items = await db.get_broadcast_run_items(run_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "status", "full_name", "username", "telegram_id", "sender_account_id",
        "template_id", "error", "timestamp",
    ])
    for item in items:
        writer.writerow([
            item.get("status"), item.get("full_name"), item.get("username"),
            item.get("telegram_id"), item.get("sender_account_id"), item.get("template_id"),
            item.get("error"), item.get("timestamp"),
        ])
    payload = output.getvalue().encode("utf-8-sig")
    await callback.message.answer_document(
        BufferedInputFile(payload, filename=f"broadcast_{run_id}_results.csv"),
        caption=f"📋 Полный отчёт рассылки #{run_id}",
    )
    await callback.answer()


# ---------------- Раздел: Расписание ----------------

def schedule_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить расписание", callback_data="sched_add")],
        [InlineKeyboardButton(text="📋 Список расписаний", callback_data="sched_list")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")],
    ])


@router.callback_query(F.data == "menu_schedule")
@admin_only
async def menu_schedule(callback: CallbackQuery):
    await callback.message.edit_text("Раздел «Расписание»:", reply_markup=schedule_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "sched_add")
@admin_only
async def sched_add_start(callback: CallbackQuery, state: FSMContext):
    templates = await db.get_templates()
    if not templates:
        await callback.message.edit_text("Сначала добавь хотя бы один шаблон.", reply_markup=templates_menu_kb())
        await callback.answer()
        return
    kb_rows = []
    if len(templates) > 1:
        kb_rows.append([InlineKeyboardButton(text="🔀 Ротация всех шаблонов", callback_data="sched_tpl_0")])
    kb_rows.extend([[InlineKeyboardButton(text=t["name"], callback_data=f"sched_tpl_{t['id']}")] for t in templates])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_schedule")])
    await state.set_state(AddSchedule.choosing_template)
    rotation_hint = "\n\nДля автоматического чередования текстов выбери «🔀 Ротация всех шаблонов»." if len(templates) > 1 else ""
    await callback.message.edit_text(
        "Выбери шаблон для расписания:" + rotation_hint,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await callback.answer()


@router.callback_query(AddSchedule.choosing_template, F.data.startswith("sched_tpl_"))
@admin_only
async def sched_choose_group(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split("_")[-1])
    await state.update_data(template_id=template_id)
    groups = await db.get_groups()
    if "Все" not in groups:
        groups.append("Все")
    kb_rows = [[InlineKeyboardButton(text=g, callback_data=f"sched_grp_{g}")] for g in groups]
    await state.set_state(AddSchedule.choosing_group)
    await callback.message.edit_text("Кому отправлять по расписанию?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await callback.answer()


@router.callback_query(AddSchedule.choosing_group, F.data.startswith("sched_grp_"))
@admin_only
async def sched_wait_time(callback: CallbackQuery, state: FSMContext):
    group_name = callback.data[len("sched_grp_"):]
    await state.update_data(group_name=group_name)
    await state.set_state(AddSchedule.waiting_time)
    await callback.message.edit_text("Во сколько отправлять? Формат ЧЧ:ММ (например 09:00), время по часовому поясу " + config.TIMEZONE)
    await callback.answer()


@router.message(AddSchedule.waiting_time)
@admin_only
async def sched_wait_days(message: Message, state: FSMContext):
    time_str = message.text.strip()
    if len(time_str) != 5 or time_str[2] != ":":
        await message.answer("Неверный формат. Пример: 09:00")
        return
    await state.update_data(time=time_str)
    await state.set_state(AddSchedule.waiting_days)
    await message.answer(
        "В какие дни отправлять? Напиши через запятую сокращения: mon,tue,wed,thu,fri,sat,sun\n"
        "Или напиши `*` — каждый день.",
        parse_mode="Markdown"
    )


@router.message(AddSchedule.waiting_days)
@admin_only
async def sched_finish(message: Message, state: FSMContext):
    days = message.text.strip().lower()
    data = await state.get_data()
    await db.add_schedule(template_id=data["template_id"], group_name=data["group_name"],
                           time=data["time"], days=days)
    await state.clear()
    template_note = "Шаблон: ротация всех шаблонов.\n" if data["template_id"] == 0 else ""
    await message.answer(
        f"✅ Расписание создано: {data['time']} ({days}), группа «{data['group_name']}».\n\n"
        f"{template_note}"
        f"⚠️ Чтобы новое расписание вступило в силу, планировщик подхватит его автоматически "
        f"(обновляется при следующем цикле).",
        reply_markup=schedule_menu_kb()
    )


@router.callback_query(F.data == "sched_list")
@admin_only
async def sched_list(callback: CallbackQuery):
    schedules = await db.get_schedules()
    if not schedules:
        await callback.message.edit_text("Расписаний пока нет.", reply_markup=schedule_menu_kb())
        await callback.answer()
        return

    kb_rows = []
    lines = []
    for s in schedules:
        if s["template_id"] == 0:
            tpl_name = "🔀 Ротация всех шаблонов"
        else:
            template = await db.get_template(s["template_id"])
            tpl_name = template["name"] if template else "?"
        status = "✅ вкл" if s["enabled"] else "🚫 выкл"
        lines.append(f"#{s['id']} {s['time']} [{s['days']}] → {s['group_name']} / «{tpl_name}» ({status})")
        kb_rows.append([
            InlineKeyboardButton(text=f"🔁 Вкл/Выкл #{s['id']}", callback_data=f"sched_toggle_{s['id']}"),
            InlineKeyboardButton(text=f"🗑 #{s['id']}", callback_data=f"sched_del_{s['id']}"),
        ])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_schedule")])
    await callback.message.edit_text("⏰ Расписания:\n\n" + "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await callback.answer()


@router.callback_query(F.data.startswith("sched_toggle_"))
@admin_only
async def sched_toggle(callback: CallbackQuery):
    schedule_id = int(callback.data.split("_")[-1])
    schedules = await db.get_schedules()
    current = next((s for s in schedules if s["id"] == schedule_id), None)
    if current:
        await db.set_schedule_enabled(schedule_id, not current["enabled"])
    await callback.answer("Изменено")
    await sched_list(callback)


@router.callback_query(F.data.startswith("sched_del_"))
@admin_only
async def sched_delete(callback: CallbackQuery):
    schedule_id = int(callback.data.split("_")[-1])
    await db.delete_schedule(schedule_id)
    await callback.answer("Удалено")
    await sched_list(callback)


# ---------------- Раздел: Логи ----------------

@router.callback_query(F.data == "menu_logs")
@admin_only
async def menu_logs(callback: CallbackQuery):
    logs = await db.get_recent_logs(limit=20)
    if not logs:
        await callback.message.edit_text("Логов пока нет.", reply_markup=main_menu_kb())
        await callback.answer()
        return
    lines = []
    for l in logs:
        if l["status"] == "sent":
            icon = "✅"
        elif l["status"] == "skipped":
            icon = "⏭"
        else:
            icon = "❌"
        name = l["full_name"] or l["username"] or "?"
        lines.append(f"{icon} {name} — «{l['template_name']}» — {l['timestamp'][:16]}" + (f" ({l['error']})" if l["error"] else ""))
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")]])
    await callback.message.edit_text("📊 Последние отправки:\n\n" + "\n".join(lines)[:4000], reply_markup=kb)
    await callback.answer()


# ---------------- Раздел: Авторизация юзербота ----------------

async def _auth_accounts_for_current_owner() -> list[dict]:
    await userbot.ensure_legacy_sender_account(owner_id=db.get_current_owner_id())
    return await userbot.get_sender_accounts(
        owner_id=db.get_current_owner_id(),
        authorized_only=False,
    )


async def _auto_switch_technical_enabled() -> bool:
    raw = await db.get_setting("auto_switch_technical_accounts", "1")
    return str(raw).strip().lower() not in {"0", "false", "off", "no"}


def _account_health_text(account: dict) -> tuple[str, str]:
    if not account.get("authorized"):
        return "🔐", "не авторизован"
    status = account.get("health_status") or "unknown"
    if status == "ok":
        return "✅", "работает"
    if status == "technical_error":
        return "⚠️", "техническая ошибка"
    if status == "restricted":
        return "🚫", "ограничение отправки"
    if status == "unauthorized":
        return "🔐", "не авторизован"
    return "◻️", "не проверен"


async def _render_auth_menu(callback: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    accounts = await _auth_accounts_for_current_owner()
    auto_switch = await _auto_switch_technical_enabled()
    if accounts:
        lines = ["Аккаунты отправки:"]
        for account in accounts:
            icon, status_text = _account_health_text(account)
            line = f"{icon} {account['label']} — {status_text}"
            error = account.get("health_error")
            if error and (account.get("health_status") or "unknown") in {"technical_error", "restricted"}:
                line += f"\n   ↳ {str(error).replace(chr(10), ' ')[:120]}"
            lines.append(line)
    else:
        lines = ["Пока нет добавленных аккаунтов отправки."]

    switch_label = "ВКЛ ✅" if auto_switch else "ВЫКЛ ⛔"
    kb_rows = [
        [InlineKeyboardButton(text="➕ Добавить аккаунт отправки", callback_data="auth_add")],
        [InlineKeyboardButton(text="🩺 Проверить аккаунты", callback_data="auth_check_accounts")],
        [InlineKeyboardButton(
            text=f"🔁 Автопереключение при тех. сбоях: {switch_label}",
            callback_data="auth_toggle_auto_switch",
        )],
    ]
    for account in accounts:
        if not account.get("authorized"):
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"🔐 Авторизовать {account['label']}",
                    callback_data=f"auth_use_{account['id']}",
                )
            ])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")])

    text = (
        "🔐 Аккаунты юзерботов\n\n"
        + "\n".join(lines)
        + "\n\n🔁 Автопереключение используется только при технической недоступности "
          "сессии/соединения. При PeerFlood, FloodWait или ограничении отправки "
          "рассылка ставится на паузу."
    )
    await callback.message.edit_text(text[:3900], reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


def _auth_methods_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 По номеру телефона", callback_data="auth_phone")],
        [InlineKeyboardButton(text="🔳 По QR-коду", callback_data="auth_qr")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_auth")],
    ])


async def _get_auth_sender_id(state: FSMContext) -> int:
    data = await state.get_data()
    sender_id = data.get("auth_sender_id")
    if sender_id:
        return int(sender_id)
    account = await userbot.create_sender_account()
    await state.update_data(auth_sender_id=account["id"])
    return int(account["id"])


async def _require_auth_sender_id(state: FSMContext) -> int:
    data = await state.get_data()
    sender_id = data.get("auth_sender_id")
    if not sender_id:
        raise ValueError("Не выбран аккаунт отправки. Начни авторизацию заново.")
    return int(sender_id)


def _auth_success_name(me) -> str:
    if not me:
        return "неизвестно"
    name = " ".join(filter(None, [getattr(me, "first_name", None), getattr(me, "last_name", None)])).strip()
    username = getattr(me, "username", None)
    if name and username:
        return f"{name} (@{username})"
    return name or (f"@{username}" if username else "неизвестно")


@router.callback_query(F.data == "menu_auth")
@admin_only
async def menu_auth(callback: CallbackQuery, state: FSMContext):
    await _render_auth_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "auth_check_accounts")
@admin_only
async def auth_check_accounts(callback: CallbackQuery, state: FSMContext):
    await callback.answer("🩺 Проверяю сессии...")
    await userbot.check_sender_accounts(owner_id=db.get_current_owner_id())
    await _render_auth_menu(callback, state)


@router.callback_query(F.data == "auth_toggle_auto_switch")
@admin_only
async def auth_toggle_auto_switch(callback: CallbackQuery, state: FSMContext):
    enabled = await _auto_switch_technical_enabled()
    await db.set_setting("auto_switch_technical_accounts", "0" if enabled else "1")
    await _render_auth_menu(callback, state)
    await callback.answer(
        "Автопереключение при технических сбоях выключено." if enabled
        else "Автопереключение при технических сбоях включено.",
        show_alert=True,
    )


@router.callback_query(F.data == "auth_add")
@admin_only
async def auth_add_account(callback: CallbackQuery, state: FSMContext):
    account = await userbot.create_sender_account()
    await state.clear()
    await state.update_data(auth_sender_id=account["id"])
    await callback.message.edit_text(
        f"Новый аккаунт отправки #{account['id']} создан.\n\nВыбери способ входа:",
        reply_markup=_auth_methods_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("auth_use_"))
@admin_only
async def auth_use_existing_account(callback: CallbackQuery, state: FSMContext):
    try:
        sender_id = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer()
        return
    account = await db.get_sender_account(sender_id, owner_id=db.get_current_owner_id(), include_inactive=False)
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await state.clear()
    await state.update_data(auth_sender_id=sender_id)
    await callback.message.edit_text(
        f"Авторизация аккаунта отправки #{sender_id}.\n\nВыбери способ входа:",
        reply_markup=_auth_methods_kb(),
    )
    await callback.answer()


# --- Вход по номеру телефона ---

def auth_code_kb(can_resend: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if can_resend:
        rows.append([InlineKeyboardButton(text="🔁 Отправить код ещё раз", callback_data="auth_resend_code")])
    rows.extend([
        [InlineKeyboardButton(text="🔳 Войти по QR-коду", callback_data="auth_qr")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_auth")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_auth_error(error: Exception) -> str:
    if isinstance(error, FloodWaitError):
        return f"Telegram временно ограничил запросы. Попробуй через {error.seconds} сек."
    if isinstance(error, PhoneNumberFloodError):
        return "По этому номеру слишком много запросов кода. Подожди и попробуй позже."
    if isinstance(error, PhoneNumberInvalidError):
        return "Telegram не принял номер. Проверь формат: номер должен начинаться с + и кода страны."
    if isinstance(error, PhoneNumberBannedError):
        return "Этот номер заблокирован в Telegram и не может быть авторизован."
    if isinstance(error, SendCodeUnavailableError):
        return (
            "Telegram уже использовал доступные способы отправки для этого номера. "
            "Проверь код из первого сообщения/приложения Telegram или войди по QR-коду."
        )
    if isinstance(error, SmsCodeCreateFailedError):
        return "Telegram не смог создать SMS-код. Попробуй повторно или используй QR-код."
    if isinstance(error, PhoneCodeInvalidError):
        return "Код неверный. Проверь сообщение от Telegram и введи код ещё раз."
    if isinstance(error, PhoneCodeExpiredError):
        return "Код истёк. Нажми «Отправить код ещё раз» и введи новый код."
    return str(error) or error.__class__.__name__


def _format_code_sent_text(phone: str, sent_code: dict, resent: bool = False) -> str:
    delivery = sent_code.get("delivery") or "через Telegram"
    prefix = "Код запрошен повторно" if resent else "Код запрошен"
    lines = [
        f"✅ {prefix}. Telegram отправляет его {delivery}.",
        "",
        f"Проверь аккаунт с номером {phone}: обычно код приходит в чат Telegram / системное уведомление, а не в этого управляющего бота.",
        "Введи код сюда одним сообщением.",
    ]
    timeout = sent_code.get("timeout")
    if timeout:
        lines.append(f"\nПовторная отправка обычно доступна через {timeout} сек.")
    next_delivery = sent_code.get("next_delivery")
    if next_delivery:
        lines.append(f"Следующий вариант доставки: {next_delivery}.")
    else:
        lines.append(
            "Telegram не сообщил следующий способ доставки, поэтому повторную отправку сейчас лучше не нажимать."
        )
    lines.append("\nЕсли код так и не пришёл, войди по QR-коду или попробуй этот номер позже.")
    return "\n".join(lines)


@router.callback_query(F.data == "auth_phone")
@admin_only
async def auth_phone_start(callback: CallbackQuery, state: FSMContext):
    sender_id = await _get_auth_sender_id(state)
    await state.set_state(AuthPhone.waiting_phone)
    await callback.message.edit_text(
        f"Аккаунт отправки #{sender_id}.\n\n"
        "Введи номер телефона юзербот-аккаунта в международном формате, например +491234567890:"
    )
    await callback.answer()


@router.message(AuthPhone.waiting_phone)
@admin_only
async def auth_phone_request_code(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Отправь номер телефона текстом, например +491234567890:")
        return
    phone = message.text.strip()
    status = await message.answer("⏳ Отправляю код подтверждения...")
    sender_id = await _get_auth_sender_id(state)
    try:
        sent_code = await userbot.auth_send_code(phone, sender_id=sender_id)
    except Exception as e:
        logger.exception("Не удалось отправить код авторизации для sender_id=%s", sender_id)
        await status.edit_text(f"❌ Не удалось отправить код: {_format_auth_error(e)}\n\nПопробуй ещё раз, отправь номер телефона:")
        return
    phone = sent_code.get("phone") or phone
    await state.update_data(
        auth_sender_id=sender_id,
        phone=phone,
        phone_code_hash=sent_code["phone_code_hash"],
        can_resend_code=sent_code.get("can_resend", False),
    )
    await state.set_state(AuthPhone.waiting_code)
    await status.edit_text(
        f"Аккаунт отправки #{sender_id}.\n\n" + _format_code_sent_text(phone, sent_code),
        reply_markup=auth_code_kb(sent_code.get("can_resend", False)),
    )


@router.callback_query(AuthPhone.waiting_code, F.data == "auth_resend_code")
@admin_only
async def auth_phone_resend_code(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    if not phone:
        await state.clear()
        await callback.message.edit_text(
            "Не нашёл номер из предыдущего шага. Начни авторизацию заново.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 По номеру телефона", callback_data="auth_phone")],
                [InlineKeyboardButton(text="🔳 По QR-коду", callback_data="auth_qr")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")],
            ]),
        )
        await callback.answer()
        return
    try:
        sender_id = await _require_auth_sender_id(state)
    except ValueError as e:
        await state.clear()
        await callback.message.edit_text(f"❌ {e}", reply_markup=main_menu_kb())
        await callback.answer()
        return

    if not data.get("can_resend_code"):
        await callback.message.edit_reply_markup(reply_markup=auth_code_kb(False))
        await callback.answer(
            "Telegram не дал следующий способ доставки. Проверь первый код или войди по QR-коду.",
            show_alert=True,
        )
        return

    await callback.answer("Запрашиваю код повторно...")
    try:
        sent_code = await userbot.auth_send_code(phone, sender_id=sender_id, resend=True, force_sms=True)
    except Exception as e:
        logger.exception("Не удалось повторно отправить код авторизации для sender_id=%s", sender_id)
        if isinstance(e, SendCodeUnavailableError):
            await state.update_data(can_resend_code=False)
        await callback.message.edit_text(
            f"❌ Не удалось повторно отправить код: {_format_auth_error(e)}\n\n"
            "Проверь первый код или войди по QR-коду.",
            reply_markup=auth_code_kb(False),
        )
        return

    phone = sent_code.get("phone") or phone
    await state.update_data(
        phone=phone,
        phone_code_hash=sent_code["phone_code_hash"],
        can_resend_code=sent_code.get("can_resend", False),
    )
    await callback.message.edit_text(
        f"Аккаунт отправки #{sender_id}.\n\n" + _format_code_sent_text(phone, sent_code, resent=True),
        reply_markup=auth_code_kb(sent_code.get("can_resend", False)),
    )


@router.message(AuthPhone.waiting_code)
@admin_only
async def auth_phone_sign_in(message: Message, state: FSMContext):
    if not message.text:
        data = await state.get_data()
        await message.answer(
            "Введи код текстом из сообщения Telegram.",
            reply_markup=auth_code_kb(data.get("can_resend_code", False)),
        )
        return
    data = await state.get_data()
    code = message.text.strip().replace(" ", "")
    try:
        sender_id = await _require_auth_sender_id(state)
    except ValueError as e:
        await state.clear()
        await message.answer(f"❌ {e}", reply_markup=main_menu_kb())
        return
    try:
        await userbot.auth_sign_in_code(
            data["phone"],
            code,
            data["phone_code_hash"],
            sender_id=sender_id,
        )
    except SessionPasswordNeededError:
        await state.set_state(AuthPhone.waiting_password)
        await message.answer("На аккаунте включена двухфакторная аутентификация (2FA). Введи облачный пароль:")
        return
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        await message.answer(
            f"❌ {_format_auth_error(e)}",
            reply_markup=auth_code_kb(data.get("can_resend_code", False)),
        )
        return
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка входа: {_format_auth_error(e)}\n\nНачни заново: /start → 🔐 Авторизация юзербота.")
        return
    await state.clear()
    me = await userbot.get_me_safe(sender_id)
    name = _auth_success_name(me)
    await message.answer(f"✅ Аккаунт отправки #{sender_id} авторизован: {name}", reply_markup=main_menu_kb())


@router.message(AuthPhone.waiting_password)
@admin_only
async def auth_password_sign_in(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Введи облачный пароль текстом.")
        return
    password = message.text.strip()
    try:
        sender_id = await _require_auth_sender_id(state)
    except ValueError as e:
        await state.clear()
        await message.answer(f"❌ {e}", reply_markup=main_menu_kb())
        return
    try:
        await userbot.auth_sign_in_password(password, sender_id=sender_id)
    except Exception as e:
        await message.answer(f"❌ Ошибка входа по паролю: {_format_auth_error(e)}\n\nПопробуй ввести пароль ещё раз, или начни заново: /start")
        return
    await state.clear()
    me = await userbot.get_me_safe(sender_id)
    name = _auth_success_name(me)
    await message.answer(f"✅ Аккаунт отправки #{sender_id} авторизован: {name}", reply_markup=main_menu_kb())


# --- Вход по QR-коду ---

@router.callback_query(F.data == "auth_qr")
@admin_only
async def auth_qr_flow(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    sender_id = await _get_auth_sender_id(state)
    try:
        qr_login, png = await userbot.auth_qr_start(sender_id=sender_id)
    except Exception as e:
        await callback.message.edit_text(f"❌ Не удалось создать QR-код: {e}")
        return

    photo_msg = await callback.message.answer_photo(
        BufferedInputFile(png, filename="qr.png"),
        caption=(
            f"Аккаунт отправки #{sender_id}.\n\n"
            "Отсканируй этот QR в приложении Telegram (аккаунт, который будет юзерботом):\n"
            "Настройки → Устройства → Подключить устройство.\n\n"
            "Код действует около 30 секунд — буду обновлять его автоматически, пока не отсканируешь."
        ),
    )

    # Ждём подтверждения. Пока код не отсканирован — периодически перегенерируем его,
    # т.к. QR-логин Telethon истекает примерно раз в 30 секунд.
    while True:
        try:
            await qr_login.wait(timeout=30)
            break  # успешный вход
        except SessionPasswordNeededError:
            await state.set_state(AuthPhone.waiting_password)
            await photo_msg.reply(
                f"QR отсканирован для аккаунта отправки #{sender_id}. "
                "На аккаунте включена двухфакторная аутентификация (2FA). "
                "Введи облачный пароль:"
            )
            return
        except asyncio.TimeoutError:
            try:
                new_png = await userbot.auth_qr_recreate(qr_login)
            except Exception as e:
                await photo_msg.reply(f"❌ Ошибка обновления QR-кода: {e}")
                return
            try:
                await photo_msg.edit_media(
                    InputMediaPhoto(
                        media=BufferedInputFile(new_png, filename="qr.png"),
                        caption=f"Аккаунт отправки #{sender_id}.\n\nQR обновлён — отсканируй заново:",
                    )
                )
            except Exception:
                pass  # если не удалось отредактировать (например, сообщение слишком старое) — просто продолжаем ждать
        except Exception as e:
            await photo_msg.reply(f"❌ Ошибка авторизации по QR: {e}")
            return

    await state.clear()
    try:
        me = await userbot.refresh_sender_account_identity(sender_id)
    except Exception:
        me = await userbot.get_me_safe(sender_id)
    name = _auth_success_name(me)
    await photo_msg.reply(f"✅ Аккаунт отправки #{sender_id} авторизован: {name}", reply_markup=main_menu_kb())


# ---------------- Раздел: Настройки ----------------

MIN_SAFE_DELAY = 1.5  # ниже этого предупреждаем о риске flood-limit


def settings_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Изменить задержку между сообщениями", callback_data="settings_delay")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")],
    ])


@router.callback_query(F.data == "menu_settings")
@admin_only
async def menu_settings(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    delay_min, delay_max = await userbot.get_delay_range()
    text = (
        "⚙️ Настройки рассылки\n\n"
        f"⏱ Текущая задержка между сообщениями: от {delay_min:g} до {delay_max:g} сек "
        "(перед каждой отправкой берётся случайное число из этого интервала)."
    )
    await callback.message.edit_text(text, reply_markup=settings_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "settings_delay")
@admin_only
async def settings_delay_start(callback: CallbackQuery, state: FSMContext):
    delay_min, delay_max = await userbot.get_delay_range()
    await state.set_state(DelaySettings.waiting_range)
    await callback.message.edit_text(
        f"Текущий интервал: от {delay_min:g} до {delay_max:g} сек.\n\n"
        "Введи новый интервал в формате `мин-макс` (в секундах), например:\n"
        "`2-5` — случайная задержка от 2 до 5 секунд перед каждым сообщением.\n\n"
        f"⚠️ Не рекомендуется ставить минимум ниже {MIN_SAFE_DELAY:g} сек — "
        "слишком частая отправка новым людям повышает риск flood-wait/ограничений Telegram.",
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(DelaySettings.waiting_range)
@admin_only
async def settings_delay_save(message: Message, state: FSMContext):
    raw = message.text.strip().replace(" ", "")
    parts = raw.replace(",", "-").split("-")
    if len(parts) != 2:
        await message.answer(
            "❌ Не понял формат. Введи так: `мин-макс`, например `2-5`.",
            parse_mode="Markdown",
        )
        return
    try:
        delay_min = float(parts[0].replace(",", "."))
        delay_max = float(parts[1].replace(",", "."))
    except ValueError:
        await message.answer(
            "❌ Не понял числа. Введи так: `мин-макс`, например `2-5`.",
            parse_mode="Markdown",
        )
        return

    if delay_min <= 0 or delay_max <= 0:
        await message.answer("❌ Значения должны быть положительными. Попробуй ещё раз:")
        return
    if delay_max < delay_min:
        delay_min, delay_max = delay_max, delay_min

    await userbot.set_delay_range(delay_min, delay_max)
    await state.clear()

    warning = ""
    if delay_min < MIN_SAFE_DELAY:
        warning = (
            f"\n\n⚠️ Учти: минимум {delay_min:g} сек ниже рекомендуемых {MIN_SAFE_DELAY:g} сек — "
            "повышен риск flood-wait/ограничений от Telegram при рассылке новым людям."
        )

    await message.answer(
        f"✅ Новая задержка сохранена: от {delay_min:g} до {delay_max:g} сек.{warning}",
        reply_markup=main_menu_kb(),
    )


def build_bot_and_dispatcher() -> tuple[Bot, Dispatcher]:
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp


# ---------------- Автопарсер и единый реестр ----------------

def _watch_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить правило", callback_data="watch_add_start")],
        [InlineKeyboardButton(text="📋 Правила", callback_data="watch_list_btn")],
        [InlineKeyboardButton(text="🧾 Найденные совпадения", callback_data="watch_hits")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu_main")],
    ])


def _watch_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_watches")],
    ])


async def _watches_text() -> str:
    rows = await db.get_monitored_chats()
    if not rows:
        body = "Пока нет выбранных чатов."
    else:
        lines = []
        for row in rows[:20]:
            kws = ", ".join(row.get("keywords_list") or [])
            state = "✅" if row.get("enabled") else "⏸"
            lines.append(
                f"{state} #{row['id']} · аккаунт #{row['sender_account_id']}\n"
                f"Чат: {row.get('chat_title') or row.get('chat_ref')}\n"
                f"Ключи: {kws}\nШаблон: #{row['template_id']}"
            )
        body = "\n\n".join(lines)
    return (
        "🔎 Автопарсер по ключевым словам\n\n"
        f"{body}\n\n"
        "Управление — кнопками ниже. Автоответ в ЛС выполняется только если пользователь "
        "раньше уже сам писал выбранному аккаунту; иначе совпадение только сохраняется в журнал."
    )


@router.callback_query(F.data == "menu_watches")
@admin_only
async def menu_watches(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(await _watches_text(), reply_markup=_watch_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "watch_add_start")
@admin_only
async def watch_add_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    accounts = await _sender_accounts_for_current_owner()
    if not accounts:
        await callback.message.edit_text(
            "Нет авторизованных аккаунтов. Сначала авторизуй юзербот.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔐 Авторизация", callback_data="menu_auth")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_watches")],
            ]),
        )
        await callback.answer()
        return

    rows = [
        [InlineKeyboardButton(text=account["label"], callback_data=f"watch_acc_{account['id']}")]
        for account in accounts
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_watches")])
    await state.set_state(AddKeywordWatch.choosing_account)
    await callback.message.edit_text(
        "1/4. Выбери аккаунт, который будет следить за чатом:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(AddKeywordWatch.choosing_account, F.data.startswith("watch_acc_"))
@admin_only
async def watch_choose_account(callback: CallbackQuery, state: FSMContext):
    try:
        sender_id = int(callback.data.rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        await callback.answer("Некорректный аккаунт.", show_alert=True)
        return
    accounts = await _sender_accounts_for_current_owner()
    account = next((a for a in accounts if int(a["id"]) == sender_id), None)
    if not account:
        await callback.answer("Аккаунт недоступен.", show_alert=True)
        return
    await state.update_data(sender_id=sender_id, sender_label=account["label"])
    await state.set_state(AddKeywordWatch.waiting_chat)
    await callback.message.edit_text(
        "2/4. Отправь ссылку, @username или ID чата/группы/канала, за которым нужно следить.\n\n"
        f"Аккаунт: {account['label']}",
        reply_markup=_watch_cancel_kb(),
    )
    await callback.answer()


@router.message(AddKeywordWatch.waiting_chat)
@admin_only
async def watch_receive_chat(message: Message, state: FSMContext):
    chat_ref = (message.text or "").strip()
    if not chat_ref:
        await message.answer("Укажи ссылку, @username или ID чата.", reply_markup=_watch_cancel_kb())
        return
    templates = await db.get_templates()
    if not templates:
        await state.clear()
        await message.answer("Сначала создай хотя бы один шаблон сообщения.", reply_markup=templates_menu_kb())
        return
    await state.update_data(chat_ref=chat_ref)
    await state.set_state(AddKeywordWatch.choosing_template)
    rows = [
        [InlineKeyboardButton(text=t["name"], callback_data=f"watch_tpl_{t['id']}")]
        for t in templates
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="menu_watches")])
    await message.answer(
        "3/4. Выбери шаблон для автоматического ответа:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(AddKeywordWatch.choosing_template, F.data.startswith("watch_tpl_"))
@admin_only
async def watch_choose_template(callback: CallbackQuery, state: FSMContext):
    try:
        template_id = int(callback.data.rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        await callback.answer("Некорректный шаблон.", show_alert=True)
        return
    template = await db.get_template(template_id)
    if not template:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    await state.update_data(template_id=template_id, template_name=template["name"])
    await state.set_state(AddKeywordWatch.waiting_keywords)
    await callback.message.edit_text(
        "4/4. Введи ключевые слова или фразы через запятую.\n"
        "Например: ремонт, нужна смета, ищу мастера",
        reply_markup=_watch_cancel_kb(),
    )
    await callback.answer()


@router.message(AddKeywordWatch.waiting_keywords)
@admin_only
async def watch_finish_add(message: Message, state: FSMContext):
    keywords = [x.strip() for x in (message.text or "").split(",") if x.strip()]
    if not keywords:
        await message.answer("Нужно хотя бы одно ключевое слово.", reply_markup=_watch_cancel_kb())
        return
    data = await state.get_data()
    try:
        watch = await userbot.add_keyword_watch(
            int(data["sender_id"]), data["chat_ref"], keywords, int(data["template_id"])
        )
    except Exception as e:
        logger.warning("Не удалось добавить правило автопарсера: %s", e)
        await message.answer(
            f"❌ Не удалось добавить правило: {e}",
            reply_markup=_watch_menu_kb(),
        )
        await state.clear()
        return
    await state.clear()
    await message.answer(
        f"✅ Правило #{watch['id']} добавлено.\n\n"
        f"Чат: {watch['title']}\n"
        f"Аккаунт: {data.get('sender_label', '#' + str(data['sender_id']))}\n"
        f"Шаблон: {data.get('template_name', '#' + str(data['template_id']))}\n"
        f"Ключи: {', '.join(watch['keywords'])}",
        reply_markup=_watch_menu_kb(),
    )


async def _render_watch_list(callback: CallbackQuery):
    rows = await db.get_monitored_chats()
    if not rows:
        await callback.message.edit_text("Правил пока нет.", reply_markup=_watch_menu_kb())
        return
    text_lines = []
    kb_rows = []
    for row in rows[:30]:
        title = row.get("chat_title") or row.get("chat_ref") or str(row.get("chat_id"))
        kws = ", ".join(row.get("keywords_list") or [])
        text_lines.append(
            f"#{row['id']} · {title}\n"
            f"Аккаунт #{row['sender_account_id']} · шаблон #{row['template_id']}\n"
            f"Ключи: {kws}"
        )
        kb_rows.append([
            InlineKeyboardButton(text=f"🗑 Удалить #{row['id']} · {title[:24]}", callback_data=f"watch_del_{row['id']}")
        ])
    kb_rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="watch_add_start")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_watches")])
    await callback.message.edit_text(
        "📋 Правила автопарсера\n\n" + "\n\n".join(text_lines[:30]),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


@router.callback_query(F.data == "watch_list_btn")
@admin_only
async def watch_list_button(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _render_watch_list(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("watch_del_"))
@admin_only
async def watch_delete_button(callback: CallbackQuery, state: FSMContext):
    try:
        watch_id = int(callback.data.rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    deleted = await db.delete_monitored_chat(watch_id)
    await state.clear()
    await _render_watch_list(callback)
    await callback.answer("Удалено" if deleted else "Правило не найдено", show_alert=not deleted)


def _keyword_hit_label(row: dict) -> str:
    who = f"@{row['author_username']}" if row.get("author_username") else f"id {row.get('author_telegram_id') or '—'}"
    chat = row.get("chat_title") or f"chat {row.get('chat_id') or '—'}"
    action_map = {
        "sent": "✅ отправлено",
        "send_failed": "❌ ошибка",
        "candidate_no_consent": "🟡 найдено, без авто-DM",
        "skipped_already_contacted": "↩️ уже был в реестре",
    }
    action = action_map.get(row.get("action"), row.get("action") or "—")
    when = (row.get("created_at") or "—")[:19]
    return f"{who} · {chat}\nКлюч: {row.get('matched_keyword') or '—'} · {action} · {when}"


@router.callback_query(F.data == "watch_hits")
@admin_only
async def watch_hits(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await db.get_keyword_hits(limit=50)
    text = "🧾 Последние совпадения автопарсера\n\n"
    text += "\n\n".join(_keyword_hit_label(row) for row in rows) if rows else "Совпадений пока нет."
    kb_rows = []
    seen_ids = set()
    for row in rows:
        user_id = row.get("author_telegram_id")
        if user_id is None or int(user_id) in seen_ids:
            continue
        seen_ids.add(int(user_id))
        label = f"@{row['author_username']}" if row.get("author_username") else f"id {user_id}"
        kb_rows.append([
            InlineKeyboardButton(text=f"🔍 Проверить {label[:28]}", callback_data=f"registry_uid_{int(user_id)}")
        ])
        if len(kb_rows) >= 8:
            break
    kb_rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="watch_hits")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_watches")])
    await callback.message.edit_text(
        text[:3900],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await callback.answer()


# Старые команды оставлены как совместимый резервный способ управления.
@router.message(Command("watch_list"))
@admin_only
async def cmd_watch_list(message: Message):
    await message.answer(await _watches_text(), reply_markup=_watch_menu_kb())


@router.message(Command("watch_add"))
@admin_only
async def cmd_watch_add(message: Message):
    await message.answer("Добавление правил теперь доступно кнопкой «🔎 Автопарсер → ➕ Добавить правило».", reply_markup=_watch_menu_kb())


@router.message(Command("watch_del"))
@admin_only
async def cmd_watch_del(message: Message):
    await message.answer("Удаление правил теперь доступно кнопкой «🔎 Автопарсер → 📋 Правила».", reply_markup=_watch_menu_kb())


def _registry_row_label(row: dict) -> str:
    target = f"@{row['username']}" if row.get("username") else f"id {row.get('telegram_id')}"
    source = row.get("source_chat_title") or row.get("source_kind") or "—"
    sender = f"#{row['sender_account_id']}" if row.get("sender_account_id") else "—"
    when = row.get("sent_at") or row.get("reserved_at") or "—"
    return f"{target} · аккаунт {sender}\nИсточник: {source} · {row.get('status')} · {when[:19]}"


def _registry_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Проверить пользователя", callback_data="registry_check_start")],
        [InlineKeyboardButton(text="🔄 Обновить реестр", callback_data="menu_delivery_registry")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu_main")],
    ])


@router.callback_query(F.data == "menu_delivery_registry")
@admin_only
async def menu_delivery_registry(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await db.get_delivery_registry(limit=50)
    text = "📚 Единый реестр отправок\n\n"
    text += "\n\n".join(_registry_row_label(row) for row in rows) if rows else "Реестр пока пуст."
    text += "\n\nКнопка ниже проверяет пользователя сразу по всем аккаунтам владельца."
    await callback.message.edit_text(text[:3900], reply_markup=_registry_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "registry_check_start")
@admin_only
async def registry_check_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RegistryCheck.waiting_target)
    await callback.message.edit_text(
        "Отправь @username или Telegram ID пользователя, которого нужно проверить в общем реестре:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_delivery_registry")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("registry_uid_"))
@admin_only
async def registry_check_by_button(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        telegram_id = int(callback.data[len("registry_uid_"):])
    except (TypeError, ValueError):
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return
    rows = await db.find_deliveries(telegram_id=telegram_id, limit=20)
    if not rows:
        text = f"➖ Пользователя id {telegram_id} нет в общем реестре отправок."
    else:
        text = "✅ Этому пользователю уже отправляли сообщение.\n\n" + "\n\n".join(
            _registry_row_label(row) for row in rows
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К совпадениям", callback_data="watch_hits")],
        [InlineKeyboardButton(text="📚 Реестр", callback_data="menu_delivery_registry")],
    ])
    await callback.message.edit_text(text[:3900], reply_markup=kb)
    await callback.answer()


@router.message(RegistryCheck.waiting_target)
@admin_only
async def registry_check_finish(message: Message, state: FSMContext):
    target = (message.text or "").strip()
    if not target:
        await message.answer("Укажи @username или Telegram ID.")
        return
    telegram_id = int(target) if target.lstrip("-").isdigit() else None
    username = None if telegram_id is not None else target.lstrip("@").strip()
    if telegram_id is None and not username:
        await message.answer("Не удалось распознать пользователя. Укажи @username или числовой Telegram ID.")
        return
    rows = await db.find_deliveries(telegram_id=telegram_id, username=username, limit=20)
    await state.clear()
    if not rows:
        text = "➖ В общем реестре отправок этого пользователя нет."
    else:
        text = "✅ Этому пользователю уже отправляли сообщение.\n\n" + "\n\n".join(
            _registry_row_label(row) for row in rows
        )
    await message.answer(text[:3900], reply_markup=_registry_menu_kb())


@router.message(Command("sent_registry"))
@admin_only
async def cmd_sent_registry(message: Message):
    rows = await db.get_delivery_registry(limit=50)
    text = "📚 Единый реестр отправок\n\n"
    text += "\n\n".join(_registry_row_label(row) for row in rows) if rows else "Реестр пока пуст."
    await message.answer(text[:3900], reply_markup=_registry_menu_kb())


@router.message(Command("sent_check"))
@admin_only
async def cmd_sent_check(message: Message):
    await message.answer("Проверка теперь доступна кнопкой «📚 Реестр отправок → 🔍 Проверить пользователя».", reply_markup=_registry_menu_kb())
