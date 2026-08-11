"""
Юзербот на Telethon: каждый пользователь панели авторизует свой личный
Telegram-аккаунт и работает только со своими данными.

Авторизация происходит через управляющего бота (bot.py):
раздел «🔐 Авторизация юзербота» — вход по номеру телефона или по QR-коду.

ВАЖНО: рассылка от личного аккаунта — это не официальный Bot API, а обычный
клиент Telegram. Telegram может ограничивать (flood-wait) слишком частую
отправку новых сообщений, особенно людям, которые не в контактах.
"""
import asyncio
import binascii
import logging
import os
import random
import re
import struct
from datetime import datetime, timezone, timedelta
from typing import Awaitable, Callable
import zlib

import qrcode
from telethon import TelegramClient, utils, events
from telethon.errors import (
    ChannelInvalidError,
    ChannelParicipantMissingError,
    ChannelPrivateError,
    FloodWaitError,
    ParticipantIdInvalidError,
    PeerFloodError,
    RPCError,
    SessionPasswordNeededError,
    UserNotParticipantError,
    UserPrivacyRestrictedError,
)
from telethon.tl import functions, types

import config
import database as db
import template_payload as tpl

logger = logging.getLogger("userbot")

_clients: dict[int, TelegramClient] = {}
_active_broadcast_stop_events: dict[int, asyncio.Event] = {}
_active_broadcast_pause_events: dict[int, asyncio.Event] = {}
_active_broadcast_lock = asyncio.Lock()
_restricted_run_by_sender: dict[int, int] = {}
_restriction_sender_for_run: dict[int, int] = {}
_resume_run_locks: dict[int, asyncio.Lock] = {}
_inline_bot_username: str | None = getattr(config, "INLINE_BOT_USERNAME", None)
_keyword_handlers_registered: set[int] = set()
ProgressCallback = Callable[[dict], Awaitable[None]]


def set_inline_bot_username(username: str | None):
    global _inline_bot_username
    _inline_bot_username = username.strip().lstrip("@") if username else None


def _get_inline_bot_username() -> str | None:
    username = _inline_bot_username or getattr(config, "INLINE_BOT_USERNAME", None)
    return username.strip().lstrip("@") if username else None


def _ensure_session_dir(session_name: str):
    session_dir = os.path.dirname(session_name)
    if session_dir:
        os.makedirs(session_dir, exist_ok=True)


async def create_sender_account() -> dict:
    return await db.create_sender_account()


async def ensure_legacy_sender_account(owner_id: int | None = None) -> dict | None:
    return await db.ensure_legacy_sender_account(owner_id)


async def _ensure_sender_account(sender_id: int | None = None) -> dict:
    if sender_id is not None:
        account = await db.get_sender_account(int(sender_id), include_inactive=False)
        if not account:
            raise ValueError(f"Аккаунт отправки #{sender_id} не найден")
        return account

    owner_id = db.get_current_owner_id() or db.default_owner_id()
    account = await db.get_default_sender_account(owner_id)
    if account:
        return account
    return await db.create_sender_account(owner_id)


async def _get_client(sender_id: int | None = None) -> TelegramClient:
    account = await _ensure_sender_account(sender_id)
    sender_id = int(account["id"])

    client = _clients.get(sender_id)
    if client is None:
        session_name = account["session_name"]
        _ensure_session_dir(session_name)
        client = TelegramClient(session_name, config.API_ID, config.API_HASH)
        _clients[sender_id] = client

    if not client.is_connected():
        await client.connect()
    return client


async def connect_userbot():
    """
    Подключает Telethon-клиенты для всех заведённых аккаунтов отправки.
    Если конкретная сессия ещё не авторизована — пользователь авторизует её позже
    через раздел «🔐 Авторизация юзербота».
    """
    accounts = await db.get_sender_accounts(include_inactive=False)
    for account in accounts:
        sender_id = int(account["id"])
        client = await _get_client(sender_id)
        if await client.is_user_authorized():
            me = await client.get_me()
            await db.update_sender_account_identity(sender_id, me)
            logger.info(
                f"Юзербот sender_id={sender_id} owner_id={account['owner_id']} авторизован: "
                f"{me.first_name} (@{me.username})"
            )
            _register_keyword_handler(client, sender_id, int(account["owner_id"]))
        else:
            logger.warning(
                f"Юзербот sender_id={sender_id} owner_id={account['owner_id']} НЕ авторизован. "
                "Авторизация выполняется через управляющего бота."
            )
    return _clients


async def is_authorized(sender_id: int | None = None) -> bool:
    client = await _get_client(sender_id)
    return await client.is_user_authorized()


async def get_me_safe(sender_id: int | None = None):
    try:
        client = await _get_client(sender_id)
        return await client.get_me()
    except Exception:
        return None


async def refresh_sender_account_identity(sender_id: int):
    client = await _get_client(sender_id)
    me = await client.get_me()
    await db.update_sender_account_identity(sender_id, me)
    account = await db.get_sender_account(int(sender_id), include_inactive=True)
    if account:
        _register_keyword_handler(client, int(sender_id), int(account["owner_id"]))
    return me


def _format_sender_label(account: dict, me=None) -> str:
    if me:
        name = " ".join(filter(None, [getattr(me, "first_name", None), getattr(me, "last_name", None)])).strip()
        if not name:
            name = f"id {getattr(me, 'id', account['id'])}"
        username = getattr(me, "username", None)
        identity = f"{name} (@{username})" if username else name
    else:
        name = " ".join(filter(None, [account.get("first_name"), account.get("last_name")])).strip()
        username = account.get("username")
        if name and username:
            identity = f"{name} (@{username})"
        elif name:
            identity = name
        elif username:
            identity = f"@{username}"
        elif account.get("title"):
            identity = account["title"]
        else:
            identity = "не авторизован"
    return f"#{account['id']} {identity}"


async def get_sender_accounts(
    sender_ids: list[int] | set[int] | None = None,
    owner_id: int | None = None,
    authorized_only: bool = True,
) -> list[dict]:
    """
    Возвращает аккаунты-отправители и их последнее известное техническое состояние.
    sender_id здесь — id строки sender_accounts, к которой привязан файл сессии Telethon.
    """
    configured_accounts = await db.get_sender_accounts(
        owner_id=owner_id,
        sender_ids=sender_ids,
        include_inactive=False,
    )
    accounts = []
    for account in configured_accounts:
        sender_id = int(account["id"])
        authorized = False
        me = None
        try:
            authorized = await is_authorized(sender_id)
            if authorized:
                me = await get_me_safe(sender_id)
                if me:
                    await db.update_sender_account_identity(sender_id, me)
        except Exception as e:
            logger.debug("Не удалось проверить аккаунт sender_id=%s: %s", sender_id, e)
        if authorized_only and not authorized:
            continue
        accounts.append({
            "id": sender_id,
            "sender_id": sender_id,
            "owner_id": int(account["owner_id"]),
            "label": _format_sender_label(account, me),
            "authorized": authorized,
            "session_name": account["session_name"],
            "username": getattr(me, "username", None) if me else account.get("username"),
            "telegram_user_id": getattr(me, "id", None) if me else account.get("telegram_user_id"),
            "is_root_owner": db.is_root_admin(account["owner_id"]),
            "health_status": account.get("health_status") or "unknown",
            "health_error": account.get("health_error"),
            "health_checked_at": account.get("health_checked_at"),
        })
    return accounts


async def check_sender_account(sender_id: int) -> dict:
    """
    Проверяет сессию без тестовой рассылки: доступность клиента, авторизацию и get_me().
    Ограничение на отправку (restricted) намеренно не снимается этой проверкой, потому что
    безопасно определить его исчезновение без реальной отправки нельзя.
    """
    account = await db.get_sender_account(int(sender_id), include_inactive=False)
    if not account:
        return {"id": int(sender_id), "status": "technical_error", "authorized": False, "error": "Аккаунт не найден"}
    try:
        client = await _get_client(int(sender_id))
        authorized = await client.is_user_authorized()
        if not authorized:
            error = "Сессия не авторизована"
            await db.set_sender_account_health(int(sender_id), "unauthorized", error)
            return {"id": int(sender_id), "status": "unauthorized", "authorized": False, "error": error}
        me = await client.get_me()
        if me:
            await db.update_sender_account_identity(int(sender_id), me)
        previous_status = account.get("health_status") or "unknown"
        if previous_status == "restricted":
            # get_me подтверждает работоспособность сессии, но не отсутствие ограничения на исходящие сообщения.
            return {
                "id": int(sender_id),
                "status": "restricted",
                "authorized": True,
                "error": account.get("health_error") or "Ранее обнаружено ограничение отправки",
            }
        await db.set_sender_account_health(int(sender_id), "ok", None)
        return {"id": int(sender_id), "status": "ok", "authorized": True, "error": None}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        await db.set_sender_account_health(int(sender_id), "technical_error", error)
        return {"id": int(sender_id), "status": "technical_error", "authorized": False, "error": error}


async def check_sender_accounts(owner_id: int | None = None) -> list[dict]:
    configured = await db.get_sender_accounts(owner_id=owner_id, include_inactive=False)
    results = []
    for account in configured:
        result = await check_sender_account(int(account["id"]))
        result["label"] = _format_sender_label(account)
        results.append(result)
    return results


SPAMBOT_USERNAME = "SpamBot"
_SPAMBOT_LIMIT_RE = re.compile(
    r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4},\s+\d{1,2}:\d{2}\s+UTC)",
    re.IGNORECASE,
)


def _parse_spambot_limit(text: str) -> datetime | None:
    match = _SPAMBOT_LIMIT_RE.search(text or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%d %b %Y, %H:%M UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _spambot_says_clear(text: str) -> bool:
    low = (text or "").lower()
    phrases = (
        "никаких ограничений",
        "ограничений на вашем аккаунте нет",
        "no limits are currently applied",
        "free from any restrictions",
        "no restrictions are currently applied",
    )
    return any(phrase in low for phrase in phrases)


async def _spambot_query_once(client: TelegramClient, timeout_seconds: int = 12) -> str | None:
    entity = await client.get_entity(SPAMBOT_USERNAME)
    before = await client.get_messages(entity, limit=1)
    before_id = int(before[0].id) if before else 0
    sent = await client.send_message(entity, "/start")
    min_id = max(before_id, int(getattr(sent, "id", 0)))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(3, int(timeout_seconds))
    while loop.time() < deadline:
        await asyncio.sleep(1)
        messages = await client.get_messages(entity, limit=6)
        for msg in messages:
            if int(getattr(msg, "id", 0)) <= min_id or bool(getattr(msg, "out", False)):
                continue
            text = getattr(msg, "message", None) or ""
            if text.strip():
                return text
    return None


async def check_spambot_status(sender_id: int) -> dict:
    """Проверяет официальный SpamBot. Повторный запрос делается только для уже истёкшей даты."""
    sender_id = int(sender_id)
    client = await _get_client(sender_id)
    if not await client.is_user_authorized():
        error = "Сессия не авторизована"
        await db.set_sender_account_health(sender_id, "unauthorized", error)
        return {"restricted": None, "until": None, "text": error}

    text = await _spambot_query_once(client)
    if not text:
        return {"restricted": None, "until": None, "text": "SpamBot не ответил на запрос статуса"}

    until = _parse_spambot_limit(text)
    now = datetime.now(timezone.utc)
    if until and until > now:
        await db.set_sender_account_health(sender_id, "restricted", f"Ограничение до {until:%d.%m.%Y %H:%M UTC}")
        return {"restricted": True, "until": until, "text": text}

    if until and until <= now:
        # Дата из ответа уже истекла: один раз обновляем статус. Сам по себе
        # истёкший timestamp НЕ считаем подтверждением снятия ограничения.
        await asyncio.sleep(1.5)
        refreshed = await _spambot_query_once(client)
        if not refreshed:
            return {
                "restricted": None,
                "until": until,
                "text": "Срок ограничения истёк, но SpamBot не подтвердил новый статус",
            }
        text = refreshed
        until = _parse_spambot_limit(text)
        now = datetime.now(timezone.utc)
        if until and until > now:
            await db.set_sender_account_health(sender_id, "restricted", f"Ограничение до {until:%d.%m.%Y %H:%M UTC}")
            return {"restricted": True, "until": until, "text": text}

    if _spambot_says_clear(text):
        await db.set_sender_account_health(sender_id, "ok", None)
        return {"restricted": False, "until": until, "text": text}

    # Если SpamBot снова прислал только уже истёкшую дату, не делаем вывод,
    # что ограничение снято: ждём однозначного ответа при следующей проверке.
    return {"restricted": None, "until": until, "text": text}


async def get_restriction_run_for_sender(sender_id: int) -> int | None:
    return _restricted_run_by_sender.get(int(sender_id))


async def release_restriction_pause(run_id: int) -> list[int]:
    """Снимает только внутреннюю паузу после отдельной успешной проверки статуса."""
    run_id = int(run_id)
    async with _active_broadcast_lock:
        sender_ids = [sender_id for sender_id, value in _restricted_run_by_sender.items() if value == run_id]
        for sender_id in sender_ids:
            _restricted_run_by_sender.pop(sender_id, None)
            pause_event = _active_broadcast_pause_events.get(sender_id)
            if pause_event:
                pause_event.clear()
        _restriction_sender_for_run.pop(run_id, None)
    await db.set_broadcast_run_running(run_id)
    return sorted(sender_ids)


async def get_active_broadcast_sender_ids() -> list[int]:
    async with _active_broadcast_lock:
        return sorted(_active_broadcast_stop_events.keys())


async def get_paused_broadcast_sender_ids() -> list[int]:
    async with _active_broadcast_lock:
        return sorted(
            sender_id
            for sender_id, pause_event in _active_broadcast_pause_events.items()
            if pause_event.is_set()
        )


async def request_broadcast_pause(sender_id: int | None = None) -> list[int]:
    """Ставит активную рассылку на паузу, не теряя очередь."""
    async with _active_broadcast_lock:
        if sender_id is None:
            sender_ids = list(_active_broadcast_pause_events.keys())
        else:
            sender_ids = [int(sender_id)] if int(sender_id) in _active_broadcast_pause_events else []
        for active_sender_id in sender_ids:
            _active_broadcast_pause_events[active_sender_id].set()
        return sorted(sender_ids)


async def request_broadcast_resume(sender_id: int | None = None) -> list[int]:
    """Продолжает ранее поставленную на паузу рассылку с того же места."""
    async with _active_broadcast_lock:
        if sender_id is None:
            sender_ids = list(_active_broadcast_pause_events.keys())
        else:
            sender_ids = [int(sender_id)] if int(sender_id) in _active_broadcast_pause_events else []
        resumed = []
        for active_sender_id in sender_ids:
            if active_sender_id in _restricted_run_by_sender:
                continue
            pause_event = _active_broadcast_pause_events[active_sender_id]
            if pause_event.is_set():
                pause_event.clear()
                resumed.append(active_sender_id)
        return sorted(resumed)


async def request_broadcast_stop(sender_id: int | None = None) -> list[int]:
    """Внутренняя аварийная остановка. В обычном интерфейсе используется пауза/продолжение."""
    async with _active_broadcast_lock:
        if sender_id is None:
            sender_ids = list(_active_broadcast_stop_events.keys())
        else:
            sender_ids = [int(sender_id)] if int(sender_id) in _active_broadcast_stop_events else []
        for active_sender_id in sender_ids:
            _active_broadcast_stop_events[active_sender_id].set()
            pause_event = _active_broadcast_pause_events.get(active_sender_id)
            if pause_event:
                pause_event.clear()
        return sorted(sender_ids)


async def _register_broadcast_sender_ids(sender_ids: list[int]) -> tuple[dict[int, asyncio.Event], dict[int, asyncio.Event]]:
    async with _active_broadcast_lock:
        busy_sender_ids = [sender_id for sender_id in sender_ids if sender_id in _active_broadcast_stop_events]
        if busy_sender_ids:
            busy = ", ".join(str(sender_id) for sender_id in busy_sender_ids)
            raise ValueError(f"На аккаунтах уже идёт рассылка: {busy}")
        stop_events = {sender_id: asyncio.Event() for sender_id in sender_ids}
        pause_events = {sender_id: asyncio.Event() for sender_id in sender_ids}
        _active_broadcast_stop_events.update(stop_events)
        _active_broadcast_pause_events.update(pause_events)
        return stop_events, pause_events


async def _unregister_broadcast_sender_ids(sender_ids: list[int]):
    async with _active_broadcast_lock:
        for sender_id in sender_ids:
            _active_broadcast_stop_events.pop(sender_id, None)
            _active_broadcast_pause_events.pop(sender_id, None)
            run_id = _restricted_run_by_sender.pop(sender_id, None)
            if run_id is not None and not any(value == run_id for value in _restricted_run_by_sender.values()):
                _restriction_sender_for_run.pop(run_id, None)


# ---------- Авторизация по номеру телефона ----------

def _sent_code_delivery_label(code_type) -> str:
    if isinstance(code_type, types.auth.SentCodeTypeApp):
        return "в приложение Telegram на этом аккаунте"
    if isinstance(code_type, (
        types.auth.SentCodeTypeSms,
        types.auth.SentCodeTypeFirebaseSms,
        types.auth.SentCodeTypeFragmentSms,
        types.auth.SentCodeTypeSmsPhrase,
        types.auth.SentCodeTypeSmsWord,
    )):
        return "по SMS"
    if isinstance(code_type, types.auth.SentCodeTypeCall):
        return "телефонным звонком"
    if isinstance(code_type, types.auth.SentCodeTypeFlashCall):
        return "flash-звонком"
    if isinstance(code_type, types.auth.SentCodeTypeMissedCall):
        return "через пропущенный звонок"
    if isinstance(code_type, types.auth.SentCodeTypeEmailCode):
        return "на привязанную почту"
    if isinstance(code_type, types.auth.SentCodeTypeSetUpEmailRequired):
        return "после настройки почты в Telegram"
    return "через Telegram"


def _sent_code_info(result) -> dict:
    code_type = getattr(result, "type", None)
    next_type = getattr(result, "next_type", None)
    return {
        "phone_code_hash": result.phone_code_hash,
        "delivery": _sent_code_delivery_label(code_type),
        "next_delivery": _sent_code_delivery_label(next_type) if next_type else None,
        "can_resend": next_type is not None,
        "timeout": getattr(result, "timeout", None),
    }


def _normalize_auth_phone(phone: str) -> str:
    normalized = utils.parse_phone(phone)
    if not normalized:
        raise ValueError("Не удалось прочитать номер телефона. Введи номер в международном формате, например +491234567890.")
    return normalized


async def auth_send_code(
    phone: str,
    sender_id: int | None = None,
    resend: bool = False,
    force_sms: bool = False,
) -> dict:
    """Отправляет код подтверждения и возвращает данные для продолжения входа.

    При первом запросе Telegram обычно присылает код в приложение Telegram на этом же
    номере (если он там залогинен), а не по SMS. Чтобы гарантированно получить SMS,
    повторный запрос (resend) должен идти с force_sms=True.
    """
    client = await _get_client(sender_id)
    normalized_phone = _normalize_auth_phone(phone)
    try:
        logger.info(
            "Запрашиваю код авторизации sender_id=%s phone=+%s resend=%s force_sms=%s",
            sender_id,
            normalized_phone,
            resend,
            force_sms,
        )
        result = await client.send_code_request(normalized_phone, force_sms=force_sms)
    except Exception:
        logger.exception(
            "Не удалось запросить код авторизации sender_id=%s phone=+%s resend=%s",
            sender_id,
            normalized_phone,
            resend,
        )
        raise

    info = _sent_code_info(result)
    info["phone"] = f"+{normalized_phone}"
    logger.info(
        "Код авторизации запрошен sender_id=%s phone=+%s delivery=%s next_delivery=%s timeout=%s",
        sender_id,
        normalized_phone,
        info.get("delivery"),
        info.get("next_delivery"),
        info.get("timeout"),
    )
    return info


async def auth_sign_in_code(phone: str, code: str, phone_code_hash: str, sender_id: int | None = None):
    """Вход по коду. Может выбросить SessionPasswordNeededError, если включена 2FA."""
    client = await _get_client(sender_id)
    await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    if sender_id is not None:
        await refresh_sender_account_identity(sender_id)


async def auth_sign_in_password(password: str, sender_id: int | None = None):
    """Второй шаг входа при включённой облачной (2FA) защите."""
    client = await _get_client(sender_id)
    await client.sign_in(password=password)
    if sender_id is not None:
        await refresh_sender_account_identity(sender_id)


# ---------- Авторизация по QR-коду ----------

def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = binascii.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def _qr_png_bytes(url: str) -> bytes:
    box_size = 10
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)

    matrix = qr.get_matrix()
    width = len(matrix[0]) * box_size
    height = len(matrix) * box_size
    raw_rows = []
    for row in matrix:
        expanded_row = bytearray()
        for module in row:
            expanded_row.extend([0 if module else 255] * box_size)
        expanded_row = bytes(expanded_row)
        for _ in range(box_size):
            raw_rows.append(b"\x00" + expanded_row)

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    png = [
        b"\x89PNG\r\n\x1a\n",
        _png_chunk(b"IHDR", header),
        _png_chunk(b"IDAT", zlib.compress(b"".join(raw_rows))),
        _png_chunk(b"IEND", b""),
    ]
    return b"".join(png)


async def auth_qr_start(sender_id: int | None = None):
    """Создаёт QR-логин. Возвращает (qr_login, png_bytes)."""
    client = await _get_client(sender_id)
    qr_login = await client.qr_login()
    return qr_login, _qr_png_bytes(qr_login.url)


async def auth_qr_recreate(qr_login) -> bytes:
    """Перегенерирует истёкший QR-код. Возвращает новые png_bytes."""
    await qr_login.recreate()
    return _qr_png_bytes(qr_login.url)


# ---------- Настройка задержки между сообщениями ----------

SETTING_DELAY_MIN = "send_delay_min_seconds"
SETTING_DELAY_MAX = "send_delay_max_seconds"


async def get_delay_range() -> tuple[float, float]:
    """
    Возвращает (min, max) задержку в секундах между отправкой сообщений.
    Берётся из БД текущего пользователя панели, иначе — из config.py.
    """
    raw_min = await db.get_setting(SETTING_DELAY_MIN)
    raw_max = await db.get_setting(SETTING_DELAY_MAX)
    delay_min = float(raw_min) if raw_min is not None else float(config.SEND_DELAY_MIN_SECONDS)
    delay_max = float(raw_max) if raw_max is not None else float(config.SEND_DELAY_MAX_SECONDS)
    if delay_max < delay_min:
        delay_min, delay_max = delay_max, delay_min
    return delay_min, delay_max


async def set_delay_range(delay_min: float, delay_max: float):
    """Сохраняет новый диапазон задержки в БД текущего пользователя."""
    if delay_max < delay_min:
        delay_min, delay_max = delay_max, delay_min
    await db.set_setting(SETTING_DELAY_MIN, str(delay_min))
    await db.set_setting(SETTING_DELAY_MAX, str(delay_max))


async def resolve_target(employee: dict):
    """Возвращает entity для отправки: приоритет telegram_id, иначе username."""
    if employee.get("telegram_id"):
        return employee["telegram_id"]
    if employee.get("username"):
        return employee["username"]
    raise ValueError("У пользователя не указан ни telegram_id, ни username")


def _normalize_chat_ref(chat_link_or_id: str):
    raw = (chat_link_or_id or "").strip()
    if not raw:
        raise ValueError("Нужно указать ссылку, @username или id чата")
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _entity_title(entity, fallback: str) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return fallback


def _chat_access_error_text(exc: Exception) -> str:
    """Делает RPC-ошибки invite-ссылок понятными и безопасными для интерфейса."""
    name = type(exc).__name__
    text = str(exc or "").lower()
    if name in {"InviteHashExpiredError", "InviteHashInvalidError"} or (
        "checkchatinviterequest" in text and ("expired" in text or "not valid" in text or "invalid" in text)
    ):
        return "Ссылка-приглашение истекла или недействительна"
    if name in {"InviteRequestSentError"}:
        return "Для входа в чат требуется одобрение заявки"
    if name in {"ChannelPrivateError", "ChatAdminRequiredError"}:
        return "Аккаунт не имеет доступа к этому чату"
    if name in {"UserNotParticipantError", "ChannelParicipantMissingError"}:
        return "Аккаунт должен состоять в этом чате"
    return f"{name}: {exc}"


async def _find_dialog_entity_by_peer_id(client: TelegramClient, chat_id: int):
    """Ищет уже доступный чат в диалогах по сохранённому peer id."""
    try:
        async for dialog in client.iter_dialogs():
            entity = getattr(dialog, "entity", None)
            if entity is not None and int(utils.get_peer_id(entity)) == int(chat_id):
                return entity
    except Exception:
        logger.debug("Не удалось перебрать диалоги для chat_id=%s", chat_id, exc_info=True)
    return None


async def _resolve_chat_entity(
    client: TelegramClient,
    chat_link_or_id: str,
    *,
    fallback_chat_id: int | None = None,
):
    """
    Разрешает чат без автоматического вступления. Для сохранённого правила сначала
    использует chat_id/диалоги, поэтому истёкшая старая invite-ссылка не мешает
    работе, если аккаунт уже состоит в чате.
    """
    if fallback_chat_id is not None:
        try:
            return await client.get_entity(int(fallback_chat_id))
        except Exception:
            entity = await _find_dialog_entity_by_peer_id(client, int(fallback_chat_id))
            if entity is not None:
                return entity

    chat_ref = _normalize_chat_ref(chat_link_or_id)
    try:
        return await client.get_entity(chat_ref)
    except (ChannelInvalidError, ChannelPrivateError, ValueError, RPCError) as exc:
        raise ValueError(_chat_access_error_text(exc)) from exc


async def _resolve_required_chat_entity(client: TelegramClient, chat_link_or_id: str, sender_id: int):
    try:
        entity = await _resolve_chat_entity(client, chat_link_or_id)
    except ValueError as e:
        raise ValueError(
            f"Аккаунт #{sender_id} не может открыть этот чат: {e}. "
            "Проверь ссылку/id и убедись, что аккаунт уже состоит в чате."
        ) from e

    try:
        me = await client.get_me()
        await client.get_permissions(entity, me)
    except (UserNotParticipantError, ChannelParicipantMissingError, ParticipantIdInvalidError) as e:
        raise ValueError(f"Аккаунт #{sender_id} должен состоять в этом чате.") from e
    except ChannelPrivateError as e:
        raise ValueError(f"Аккаунт #{sender_id} не имеет доступа к этому чату.") from e
    except RPCError as e:
        raise ValueError(f"Не удалось проверить доступ аккаунта #{sender_id} к чату: {e}") from e

    return entity


async def validate_membership_skip_chat(chat_link_or_id: str, sender_ids: list[int]) -> dict:
    """
    Проверяет, что все выбранные аккаунты могут открыть чат.
    Сами получатели потом проверяются через общие группы, поэтому права
    администратора и открытый список участников не нужны.
    """
    sender_ids = list(dict.fromkeys(int(sender_id) for sender_id in sender_ids))
    if not sender_ids:
        raise ValueError("Не выбран ни один аккаунт для проверки")

    titles_by_sender_id = {}
    for sender_id in sender_ids:
        client = await _get_client(sender_id)
        if not await client.is_user_authorized():
            raise ValueError(f"Юзербот не авторизован для аккаунта #{sender_id}")
        entity = await _resolve_required_chat_entity(client, chat_link_or_id, sender_id)
        titles_by_sender_id[sender_id] = _entity_title(entity, str(chat_link_or_id).strip())

    first_title = next(iter(titles_by_sender_id.values()), str(chat_link_or_id).strip())
    return {
        "chat": str(chat_link_or_id).strip(),
        "title": first_title,
        "titles_by_sender_id": titles_by_sender_id,
    }


async def _is_employee_in_chat(
    employee: dict,
    chat_entity,
    sender_id: int,
) -> tuple[bool, str | None]:
    try:
        client = await _get_client(sender_id)
        target = await resolve_target(employee)
        user_entity = await client.get_entity(target)
        if await _has_chat_in_common_chats(client, user_entity, chat_entity):
            return True, None
        return False, None
    except (UserNotParticipantError, ChannelParicipantMissingError, ParticipantIdInvalidError):
        return False, None
    except Exception as e:
        return False, f"Не удалось проверить наличие в чате: {e}"


def _peer_ids_for_match(entity) -> set[int]:
    ids = set()
    try:
        ids.add(int(utils.get_peer_id(entity)))
    except Exception:
        pass
    raw_id = getattr(entity, "id", None)
    if raw_id is not None:
        raw_id = int(raw_id)
        ids.update({raw_id, -raw_id})
    return ids


async def _has_chat_in_common_chats(client: TelegramClient, user_entity, chat_entity) -> bool:
    target_peer_ids = _peer_ids_for_match(chat_entity)
    if not target_peer_ids:
        raise ValueError("Не удалось определить id чата для проверки")

    input_user = await client.get_input_entity(user_entity)
    max_id = 0
    pages_checked = 0
    while pages_checked < 20:
        result = await client(functions.messages.GetCommonChatsRequest(
            user_id=input_user,
            max_id=max_id,
            limit=100,
        ))
        common_chats = getattr(result, "chats", None) or []
        if any(_peer_ids_for_match(common_chat) & target_peer_ids for common_chat in common_chats):
            return True
        if len(common_chats) < 100:
            return False

        chat_ids = [
            int(getattr(common_chat, "id"))
            for common_chat in common_chats
            if getattr(common_chat, "id", None) is not None
        ]
        if not chat_ids:
            return False
        next_max_id = min(chat_ids)
        if next_max_id == max_id:
            return False
        max_id = next_max_id
        pages_checked += 1

    return False


async def _send_template_post(client: TelegramClient, entity, post: dict, can_send_buttons: bool):
    media = post.get("media") or []
    buttons = tpl.telethon_buttons(post) if can_send_buttons else None
    append_button_links = bool(post.get("buttons")) and not can_send_buttons
    html = tpl.html_for_send(post, append_button_links=append_button_links)

    if len(media) > 1:
        files = [tpl.media_file(item) for item in media]
        captions = [html] + [""] * (len(files) - 1)
        await client.send_file(
            entity,
            files,
            caption=captions,
            parse_mode=tpl.TelegramHTML,
            force_document=False,
        )
        if buttons:
            await client.send_message(entity, "Кнопки:", buttons=buttons)
        return

    if len(media) == 1:
        await client.send_file(
            entity,
            tpl.media_file(media[0]),
            caption=html or None,
            parse_mode=tpl.TelegramHTML,
            buttons=buttons,
            force_document=False,
        )
        return

    if html.strip():
        await client.send_message(
            entity,
            html,
            parse_mode=tpl.TelegramHTML,
            buttons=buttons,
        )
    elif buttons:
        await client.send_message(entity, "Кнопки:", buttons=buttons)


def _can_send_post_via_inline(post: dict) -> bool:
    if not post.get("buttons"):
        return False
    media = post.get("media") or []
    if len(media) > 1:
        return False
    if len(media) == 1 and not media[0].get("file_id"):
        return False
    return True


async def _send_template_post_via_inline(client: TelegramClient, entity, post: dict) -> bool:
    bot_username = _get_inline_bot_username()
    if not bot_username or not _can_send_post_via_inline(post):
        return False

    payload = tpl.make_payload([post])
    token = await db.create_inline_payload(tpl.payload_to_json(payload))
    try:
        results = await client.inline_query(bot_username, f"tpl:{token}", entity=entity)
        if not results:
            return False
        await results[0].click(entity)
        return True
    except Exception as e:
        logger.debug("Inline-отправка через @%s не удалась: %s", bot_username, e)
        return False



async def list_available_watch_chats(sender_id: int, *, limit: int = 200) -> list[dict]:
    """Возвращает группы/каналы, уже доступные выбранному аккаунту.

    Это основной безопасный способ выбрать приватный чат, если старая invite-ссылка
    уже истекла: Telegram не позволяет восстановить chat_id из просроченного invite hash,
    зато уже доступный чат можно взять напрямую из списка диалогов аккаунта.
    """
    account = await db.get_sender_account(int(sender_id), include_inactive=False)
    if not account:
        raise ValueError("Аккаунт отправки не найден")
    owner_id = db.get_current_owner_id()
    if int(account["owner_id"]) != int(owner_id) and not db.is_root_admin(owner_id):
        raise ValueError("Нет доступа к этому аккаунту")
    if not await is_authorized(sender_id):
        raise ValueError("Аккаунт не авторизован")

    client = await _get_client(sender_id)
    rows: list[dict] = []
    seen: set[int] = set()
    try:
        async for dialog in client.iter_dialogs(limit=max(1, min(int(limit), 500))):
            if not (getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False)):
                continue
            entity = getattr(dialog, "entity", None)
            if entity is None:
                continue
            try:
                chat_id = int(utils.get_peer_id(entity))
            except Exception:
                continue
            if chat_id in seen:
                continue
            seen.add(chat_id)
            title = _entity_title(entity, str(getattr(dialog, "name", None) or chat_id))
            username = getattr(entity, "username", None)
            ref = f"@{username}" if username else str(chat_id)
            rows.append({
                "chat_id": chat_id,
                "title": title,
                "ref": ref,
                "username": username,
            })
    except Exception as exc:
        raise ValueError(f"Не удалось получить список чатов аккаунта: {exc}") from exc

    rows.sort(key=lambda x: (str(x.get("title") or "").casefold(), int(x["chat_id"])))
    return rows


async def resolve_keyword_watch_chats(sender_id: int, chat_refs: list[str]) -> dict:
    """Проверяет список чатов выбранным аккаунтом без записи правил в БД."""
    account = await db.get_sender_account(int(sender_id), include_inactive=False)
    if not account:
        raise ValueError("Аккаунт отправки не найден")
    owner_id = db.get_current_owner_id()
    if int(account["owner_id"]) != int(owner_id) and not db.is_root_admin(owner_id):
        raise ValueError("Нет доступа к этому аккаунту")
    if not await is_authorized(sender_id):
        raise ValueError("Аккаунт не авторизован")

    client = await _get_client(sender_id)
    resolved: list[dict] = []
    errors: list[dict] = []
    seen_chat_ids: set[int] = set()
    for raw_ref in chat_refs:
        ref = str(raw_ref or "").strip()
        if not ref:
            continue
        try:
            entity = await _resolve_chat_entity(client, ref)
            chat_id = int(utils.get_peer_id(entity))
            if chat_id in seen_chat_ids:
                continue
            seen_chat_ids.add(chat_id)
            resolved.append({
                "ref": ref,
                "chat_id": chat_id,
                "title": _entity_title(entity, ref),
            })
        except Exception as exc:
            error_text = str(exc) if isinstance(exc, ValueError) else _chat_access_error_text(exc)
            errors.append({"ref": ref, "error": error_text})
    return {"resolved": resolved, "errors": errors}


async def add_keyword_watches(
    sender_id: int,
    chat_refs: list[str],
    keywords: list[str],
    template_id: int,
    *,
    resolved_chats: list[dict] | None = None,
) -> dict:
    """Массово добавляет правила для нескольких чатов с общим набором ключей.

    Если ``resolved_chats`` передан после предпросмотра, повторно invite-ссылки
    не открываются. Это важно для одноразовых/успевших истечь приватных ссылок:
    после успешного preview правило сохраняется по уже известному chat_id.
    """
    account = await db.get_sender_account(int(sender_id), include_inactive=False)
    if not account:
        raise ValueError("Аккаунт отправки не найден")
    owner_id = db.get_current_owner_id()
    if int(account["owner_id"]) != int(owner_id) and not db.is_root_admin(owner_id):
        raise ValueError("Нет доступа к этому аккаунту")
    if not await is_authorized(sender_id):
        raise ValueError("Аккаунт не авторизован")
    template = await db.get_template(int(template_id))
    if not template:
        raise ValueError("Шаблон не найден")

    cleaned_keywords: list[str] = []
    seen_keywords: set[str] = set()
    for keyword in keywords:
        value = str(keyword or "").strip().lower()
        if not value or value in seen_keywords:
            continue
        seen_keywords.add(value)
        cleaned_keywords.append(value)
    if not cleaned_keywords:
        raise ValueError("Нужно хотя бы одно ключевое слово")

    if resolved_chats is None:
        preview = await resolve_keyword_watch_chats(sender_id, chat_refs)
    else:
        preview = {"resolved": [], "errors": []}
        seen_ids: set[int] = set()
        for item in resolved_chats:
            try:
                chat_id = int(item["chat_id"])
                ref = str(item.get("ref") or chat_id).strip()
                title = str(item.get("title") or ref).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if chat_id in seen_ids:
                continue
            seen_ids.add(chat_id)
            preview["resolved"].append({"ref": ref, "chat_id": chat_id, "title": title})

    existing = await db.get_monitored_chats(sender_account_id=int(sender_id))
    existing_keys = {
        (
            int(row.get("chat_id") or 0),
            int(row.get("template_id") or 0),
            frozenset(row.get("keywords_list") or []),
        )
        for row in existing
    }

    added: list[dict] = []
    skipped_existing: list[dict] = []
    for item in preview["resolved"]:
        key = (int(item["chat_id"]), int(template_id), frozenset(cleaned_keywords))
        if key in existing_keys:
            skipped_existing.append(item)
            continue
        watch_id = await db.add_monitored_chat(
            int(sender_id), item["ref"], int(item["chat_id"]), item["title"], cleaned_keywords, int(template_id)
        )
        added.append({**item, "id": watch_id})
        existing_keys.add(key)

    client = await _get_client(sender_id)
    _register_keyword_handler(client, int(sender_id), int(account["owner_id"]))
    return {
        "added": added,
        "skipped_existing": skipped_existing,
        "errors": preview["errors"],
        "keywords": cleaned_keywords,
        "template_id": int(template_id),
    }


async def add_keyword_watch(sender_id: int, chat_ref: str, keywords: list[str], template_id: int) -> dict:
    """Совместимый одиночный вариант поверх массового добавления."""
    result = await add_keyword_watches(sender_id, [chat_ref], keywords, template_id)
    if result["added"]:
        item = result["added"][0]
        return {
            "id": item["id"],
            "chat_id": item["chat_id"],
            "title": item["title"],
            "keywords": result["keywords"],
            "template_id": int(template_id),
        }
    if result["skipped_existing"]:
        raise ValueError("Такое правило уже существует")
    if result["errors"]:
        raise ValueError(result["errors"][0]["error"])
    raise ValueError("Не удалось добавить правило")


async def scan_keyword_watch_history(watch_id: int, *, limit: int | None = None, days: int | None = None) -> dict:
    """Сканирует историю одного правила. Только журналирует совпадения, без отправки сообщений."""
    watches = await db.get_monitored_chats(enabled_only=False)
    watch = next((row for row in watches if int(row["id"]) == int(watch_id)), None)
    if not watch:
        raise ValueError("Правило не найдено")

    sender_id = int(watch["sender_account_id"])
    account = await db.get_sender_account(sender_id, include_inactive=False)
    if not account:
        raise ValueError("Аккаунт отправки не найден")
    owner_id = db.get_current_owner_id()
    if int(account["owner_id"]) != int(owner_id) and not db.is_root_admin(owner_id):
        raise ValueError("Нет доступа к этому аккаунту")
    if not await is_authorized(sender_id):
        raise ValueError("Аккаунт не авторизован")

    client = await _get_client(sender_id)
    entity = await _resolve_chat_entity(
        client,
        watch["chat_ref"],
        fallback_chat_id=int(watch["chat_id"]) if watch.get("chat_id") is not None else None,
    )
    me = await client.get_me()
    chat_id = int(utils.get_peer_id(entity))
    title = _entity_title(entity, watch.get("chat_title") or watch.get("chat_ref") or str(chat_id))
    keywords = [kw.lower() for kw in (watch.get("keywords_list") or []) if kw]
    if not keywords:
        raise ValueError("У правила нет ключевых слов")

    scan_limit = int(limit) if limit is not None else 10000
    if scan_limit <= 0:
        raise ValueError("Лимит должен быть положительным")
    scan_limit = min(scan_limit, 10000)
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days)) if days is not None else None

    stats = {"scanned": 0, "matched": 0, "added": 0, "errors": 0, "title": title}
    async for message in client.iter_messages(entity, limit=scan_limit):
        msg_date = getattr(message, "date", None)
        if cutoff is not None and msg_date is not None:
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if msg_date < cutoff:
                break

        stats["scanned"] += 1
        text = (getattr(message, "message", None) or getattr(message, "raw_text", None) or "").lower()
        if not text:
            continue
        matched = next((kw for kw in keywords if kw in text), None)
        if not matched:
            continue
        stats["matched"] += 1

        try:
            author = await message.get_sender()
            if not isinstance(author, types.User) or getattr(author, "bot", False):
                continue
            author_id = getattr(author, "id", None)
            if not author_id or int(author_id) == int(me.id):
                continue
            username = getattr(author, "username", None)
            already = await db.has_delivery(telegram_id=int(author_id), username=username)
            inserted = await db.add_keyword_hit(
                monitored_chat_id=int(watch["id"]),
                sender_account_id=sender_id,
                chat_id=chat_id,
                chat_title=title,
                message_id=getattr(message, "id", None),
                author_telegram_id=int(author_id),
                author_username=username,
                matched_keyword=matched,
                action="history_already_contacted" if already else "history_candidate",
                details="Исторический скан: сообщение только сохранено в журнал, авто-DM отключён",
            )
            if inserted:
                stats["added"] += 1
        except Exception as exc:
            stats["errors"] += 1
            logger.debug("Не удалось обработать историческое сообщение %s/%s: %s", chat_id, getattr(message, "id", None), exc)

    return stats


async def _has_prior_private_interaction(client: TelegramClient, user_entity) -> bool:
    """Безопасный gate: автоответ в ЛС только если человек раньше сам писал этому аккаунту в ЛС."""
    try:
        messages = await client.get_messages(user_entity, limit=50)
    except Exception:
        return False
    return any(not getattr(message, "out", False) for message in messages)


async def _process_keyword_event(event, sender_id: int, owner_id: int):
    token = db.set_current_owner_id(owner_id)
    try:
        if getattr(event, "out", False):
            return
        watches = await db.get_monitored_chats(sender_account_id=sender_id, enabled_only=True)
        if not watches:
            return
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        matching_watches = [w for w in watches if int(w.get("chat_id") or 0) == chat_id]
        if not matching_watches:
            return
        text = (getattr(event, "raw_text", None) or "").lower()
        if not text:
            return
        author = await event.get_sender()
        if not author or getattr(author, "bot", False):
            return
        author_id = getattr(author, "id", None)
        author_username = getattr(author, "username", None)
        if not author_id:
            return
        client = await _get_client(sender_id)
        me = await client.get_me()
        if int(author_id) == int(me.id):
            return
        chat = await event.get_chat()
        chat_title = _entity_title(chat, str(chat_id))

        for watch in matching_watches:
            matched = next((kw for kw in watch.get("keywords_list", []) if kw.lower() in text), None)
            if not matched:
                continue

            # Не отправляем повторно, даже если раньше писал другой аккаунт владельца.
            if await db.has_delivery(telegram_id=int(author_id), username=author_username):
                await db.add_keyword_hit(
                    monitored_chat_id=watch["id"], sender_account_id=sender_id,
                    chat_id=chat_id, chat_title=chat_title, message_id=getattr(event, "id", None),
                    author_telegram_id=int(author_id), author_username=author_username,
                    matched_keyword=matched, action="skipped_already_contacted",
                    details="Пользователь уже есть в общем реестре отправок",
                )
                continue

            # Не инициируем холодное ЛС: человек должен ранее сам написать аккаунту в приватном чате.
            if not await _has_prior_private_interaction(client, author):
                await db.add_keyword_hit(
                    monitored_chat_id=watch["id"], sender_account_id=sender_id,
                    chat_id=chat_id, chat_title=chat_title, message_id=getattr(event, "id", None),
                    author_telegram_id=int(author_id), author_username=author_username,
                    matched_keyword=matched, action="candidate_no_consent",
                    details="Ключ найден, но авто-DM не отправлен: нет предыдущего входящего ЛС",
                )
                continue

            template = await db.get_template(int(watch["template_id"]))
            if not template:
                continue
            reserved = await db.reserve_delivery(
                telegram_id=int(author_id), username=author_username,
                full_name=" ".join(filter(None, [getattr(author, "first_name", None), getattr(author, "last_name", None)])) or None,
                sender_account_id=sender_id, template_id=int(watch["template_id"]),
                source_kind="keyword_watch", source_chat_id=chat_id, source_chat_title=chat_title,
            )
            if not reserved:
                continue
            employee = {
                "telegram_id": int(author_id), "username": author_username,
                "full_name": " ".join(filter(None, [getattr(author, "first_name", None), getattr(author, "last_name", None)])) or None,
            }
            ok, error = await send_to_employee(employee, template, sender_id=sender_id)
            await db.finish_delivery(telegram_id=int(author_id), username=author_username, success=ok, error=error)
            await db.add_keyword_hit(
                monitored_chat_id=watch["id"], sender_account_id=sender_id,
                chat_id=chat_id, chat_title=chat_title, message_id=getattr(event, "id", None),
                author_telegram_id=int(author_id), author_username=author_username,
                matched_keyword=matched, action="sent" if ok else "send_failed", details=error,
            )
            break
    except Exception as e:
        logger.exception("Ошибка обработчика ключевых слов sender_id=%s: %s", sender_id, e)
    finally:
        db.reset_current_owner_id(token)


def _register_keyword_handler(client: TelegramClient, sender_id: int, owner_id: int):
    if sender_id in _keyword_handlers_registered:
        return

    async def handler(event):
        await _process_keyword_event(event, sender_id, owner_id)

    client.add_event_handler(handler, events.NewMessage(incoming=True))
    _keyword_handlers_registered.add(sender_id)


def _classify_send_exception(exc: Exception) -> tuple[str, str]:
    """Возвращает (kind, human_error), где kind: restriction/technical/recipient."""
    if isinstance(exc, FloodWaitError):
        return "restriction", f"FloodWait: нужно подождать {exc.seconds} сек."
    if isinstance(exc, PeerFloodError):
        return "restriction", "Telegram временно ограничил отправку сообщений (PeerFlood)"
    if isinstance(exc, UserPrivacyRestrictedError):
        return "recipient", "Настройки приватности пользователя запрещают писать первым"
    if isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError)):
        return "technical", f"{type(exc).__name__}: {exc}"

    name = type(exc).__name__
    technical_names = {
        "AuthKeyError", "AuthKeyDuplicatedError", "AuthKeyUnregisteredError",
        "SessionExpiredError", "SessionRevokedError", "UnauthorizedError",
        "UserDeactivatedError", "UserDeactivatedBanError", "ServerError",
        "TimedOutError", "RpcCallFailError",
    }
    restriction_names = {
        "UserRestrictedError", "UserBannedInChannelError", "ChatWriteForbiddenError",
    }
    if name in technical_names:
        return "technical", f"{name}: {exc}"
    if name in restriction_names:
        return "restriction", f"{name}: {exc}"
    return "recipient", str(exc)


async def send_to_employee_detailed(
    employee: dict,
    template: dict | str,
    sender_id: int | None = None,
) -> tuple[bool, str | None, str | None]:
    """Отправляет один шаблон и возвращает (успех, ошибка, тип_ошибки)."""
    posts_sent = 0
    try:
        client = await _get_client(sender_id)
        target = await resolve_target(employee)
        entity = await client.get_entity(target)
        payload = tpl.template_payload(template) if isinstance(template, dict) else tpl.legacy_payload(template)
        can_send_buttons = await client.is_bot()
        posts = tpl.messages(payload)
        post_delay_seconds = tpl.post_delay_seconds(payload)
        for index, post in enumerate(posts):
            sent_inline = False
            if not can_send_buttons:
                sent_inline = await _send_template_post_via_inline(client, entity, post)
            if not sent_inline:
                await _send_template_post(client, entity, post, can_send_buttons)
            posts_sent += 1
            if post_delay_seconds and index < len(posts) - 1:
                await asyncio.sleep(post_delay_seconds)
        if sender_id is not None:
            account = await db.get_sender_account(int(sender_id), include_inactive=True)
            if account and (account.get("health_status") or "unknown") != "ok":
                await db.set_sender_account_health(int(sender_id), "ok", None)
        return True, None, None
    except Exception as exc:
        kind, error = _classify_send_exception(exc)
        if posts_sent and kind in {"technical", "restriction"}:
            kind = "technical_partial" if kind == "technical" else "restriction_partial"
            error = (
                f"Сбой после отправки части шаблона ({posts_sent} сообщ.). "
                f"Автоповтор этого получателя отключён во избежание дубля. {error}"
            )
        if sender_id is not None:
            if kind in {"technical", "technical_partial"}:
                await db.set_sender_account_health(int(sender_id), "technical_error", error)
            elif kind in {"restriction", "restriction_partial"}:
                await db.set_sender_account_health(int(sender_id), "restricted", error)
        return False, error, kind


async def send_to_employee(
    employee: dict,
    template: dict | str,
    sender_id: int | None = None,
) -> tuple[bool, str | None]:
    """Совместимый интерфейс для одиночной отправки."""
    ok, error, _kind = await send_to_employee_detailed(employee, template, sender_id=sender_id)
    return ok, error


async def _wait_while_paused(stop_event: asyncio.Event, pause_event: asyncio.Event) -> bool:
    while pause_event.is_set():
        if stop_event.is_set():
            return True
        await asyncio.sleep(0.5)
    return stop_event.is_set()


async def _sleep_with_controls(
    delay_seconds: float,
    stop_event: asyncio.Event,
    pause_event: asyncio.Event,
) -> bool:
    """Ждёт задержку, не расходуя её во время паузы. True = аварийная остановка."""
    if await _wait_while_paused(stop_event, pause_event):
        return True
    remaining = max(0.0, float(delay_seconds))
    loop = asyncio.get_running_loop()
    while remaining > 0:
        if stop_event.is_set():
            return True
        if pause_event.is_set():
            if await _wait_while_paused(stop_event, pause_event):
                return True
            continue
        slice_seconds = min(0.5, remaining)
        started = loop.time()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=slice_seconds)
            return True
        except asyncio.TimeoutError:
            remaining -= max(0.0, loop.time() - started)
    return stop_event.is_set()


async def _get_broadcast_templates(template_id: int | None) -> tuple[list[dict], bool]:
    """
    Возвращает список шаблонов для рассылки.
    template_id=0/None означает ротацию всех доступных шаблонов текущего пользователя.
    """
    if template_id:
        template = await db.get_template(template_id)
        if not template:
            raise ValueError("Шаблон не найден")
        return [template], False

    templates = await db.get_templates()
    if not templates:
        raise ValueError("Нет шаблонов для ротации")
    random.shuffle(templates)
    return templates, True


def _next_template(templates: list[dict], index: int, previous_template_id: int | None) -> dict:
    """Берёт следующий шаблон из перемешанной пачки и перемешивает пачку на новом круге."""
    if index > 0 and index % len(templates) == 0:
        random.shuffle(templates)
        if len(templates) > 1 and templates[0]["id"] == previous_template_id:
            templates.append(templates.pop(0))
    return templates[index % len(templates)]


async def broadcast(
    template_id: int | None,
    employees: list[dict],
    progress_callback: ProgressCallback | None = None,
    sender_account_ids: list[int] | None = None,
    sender_owner_ids: list[int] | None = None,
    skip_existing_chat: str | None = None,
    group_name: str | None = None,
) -> dict:
    """
    Рассылает шаблон списку пользователей с задержкой между отправками.

    При технической недоступности сессии (разлогин, обрыв соединения, сбой ключа
    авторизации и т.п.) оставшаяся очередь этого аккаунта может быть автоматически
    переназначена на другой выбранный исправный аккаунт. Ограничения Telegram на
    отправку (PeerFlood/FloodWait/restricted) НЕ считаются техническим failover:
    при них вся рассылка ставится на паузу.
    """
    data_owner_id = db.get_current_owner_id()
    if data_owner_id is None:
        data_owner_id = db.default_owner_id()

    if sender_account_ids is None and sender_owner_ids is not None:
        sender_account_ids = []
        for owner_id in sender_owner_ids:
            owner_accounts = await get_sender_accounts(owner_id=int(owner_id), authorized_only=True)
            sender_account_ids.extend(account["id"] for account in owner_accounts)

    if sender_account_ids is None:
        owner_accounts = await get_sender_accounts(owner_id=data_owner_id, authorized_only=True)
        sender_account_ids = [account["id"] for account in owner_accounts]
        if not sender_account_ids:
            default_account = await _ensure_sender_account()
            sender_account_ids = [int(default_account["id"])]

    sender_account_ids = list(dict.fromkeys(int(sender_id) for sender_id in sender_account_ids))
    if not sender_account_ids:
        raise ValueError("Не выбран ни один аккаунт для рассылки")

    configured_accounts = await db.get_sender_accounts(
        sender_ids=sender_account_ids,
        include_inactive=False,
    )
    configured_by_id = {int(account["id"]): account for account in configured_accounts}
    missing_sender_ids = [
        sender_id for sender_id in sender_account_ids
        if sender_id not in configured_by_id
    ]
    if missing_sender_ids:
        missing = ", ".join(f"#{sender_id}" for sender_id in missing_sender_ids)
        raise ValueError(f"Аккаунты отправки не найдены: {missing}")

    inaccessible_sender_ids = [
        sender_id
        for sender_id, account in configured_by_id.items()
        if int(account["owner_id"]) != data_owner_id and not db.is_root_admin(data_owner_id)
    ]
    if inaccessible_sender_ids:
        denied = ", ".join(f"#{sender_id}" for sender_id in inaccessible_sender_ids)
        raise ValueError(f"Нет доступа к аккаунтам отправки: {denied}")

    unauthorized_sender_ids = []
    for sender_id in sender_account_ids:
        if not await is_authorized(sender_id):
            unauthorized_sender_ids.append(sender_id)
            await db.set_sender_account_health(sender_id, "unauthorized", "Сессия не авторизована")
    if unauthorized_sender_ids:
        senders = ", ".join(f"#{sender_id}" for sender_id in unauthorized_sender_ids)
        raise ValueError(f"Юзербот не авторизован для аккаунтов: {senders}")

    total = len(employees)
    templates, rotation_enabled = await _get_broadcast_templates(template_id)
    delay_min, delay_max = await get_delay_range()
    auto_switch_raw = await db.get_setting("auto_switch_technical_accounts", "1")
    auto_switch_technical = str(auto_switch_raw).strip().lower() not in {"0", "false", "off", "no"}

    sent, failed, skipped = 0, 0, 0
    processed = 0
    used_template_ids = set()
    previous_template_id = None
    stopped_sender_ids: set[int] = set()
    technical_disabled_sender_ids: set[int] = set()
    failover_jobs: list[dict] = []
    failover_count = 0
    failover_lock = asyncio.Lock()

    jobs_by_sender: dict[int, list[dict]] = {sender_id: [] for sender_id in sender_account_ids}
    all_jobs: list[dict] = []
    for index, employee in enumerate(employees):
        template = _next_template(templates, index, previous_template_id) if rotation_enabled else templates[0]
        previous_template_id = template["id"]
        sender_id = sender_account_ids[index % len(sender_account_ids)]
        job = {
            "employee": employee,
            "template": template,
            "position": index,
            "sender_id": sender_id,
            "attempted_sender_ids": set(),
        }
        jobs_by_sender[sender_id].append(job)
        all_jobs.append(job)

    effective_sender_ids = [
        sender_id
        for sender_id, jobs in jobs_by_sender.items()
        if jobs
    ]
    if not effective_sender_ids:
        return {
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "templates_used": 0,
            "stopped": False,
            "stopped_accounts": [],
            "technical_disabled_accounts": [],
            "failover_count": 0,
            "auto_switch_technical": auto_switch_technical,
            "senders_used": 0,
        }

    skip_chat_entities = {}
    skip_chat_title = None
    if skip_existing_chat:
        skip_info = await validate_membership_skip_chat(skip_existing_chat, effective_sender_ids)
        skip_chat_title = skip_info["title"]
        for sender_id in effective_sender_ids:
            client = await _get_client(sender_id)
            skip_chat_entities[sender_id] = await _resolve_required_chat_entity(
                client,
                skip_existing_chat,
                sender_id,
            )

    stop_events, pause_events = await _register_broadcast_sender_ids(effective_sender_ids)
    run_id = await db.create_broadcast_run(
        template_id, group_name, total,
        selected_account_ids=effective_sender_ids,
        skip_existing_chat=skip_existing_chat,
    )
    await db.initialize_broadcast_run_items(run_id, all_jobs)
    progress_lock = asyncio.Lock()

    async def report_progress(
        sender_id: int,
        employee: dict,
        template: dict,
        status: str,
        error: str | None,
    ):
        nonlocal sent, failed, skipped, processed
        async with progress_lock:
            used_template_ids.add(template["id"])
            processed += 1
            if status == "sent":
                sent += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
                logger.warning(
                    "Не удалось отправить пользователю %s: %s",
                    employee.get("username") or employee.get("telegram_id"),
                    error,
                )
            if progress_callback:
                await progress_callback({
                    "total": total,
                    "processed": processed,
                    "sent": sent,
                    "failed": failed,
                    "skipped": skipped,
                    "templates_used": len(used_template_ids),
                    "current": employee,
                    "ok": status == "sent",
                    "status": status,
                    "error": error,
                    "sender_id": sender_id,
                    "sender_account_id": sender_id,
                    "sender_owner_id": int(configured_by_id[sender_id]["owner_id"]),
                    "stopped_accounts": sorted(stopped_sender_ids),
                    "paused_accounts": sorted(
                        active_sender_id
                        for active_sender_id, pause_event in pause_events.items()
                        if pause_event.is_set()
                    ),
                    "technical_disabled_accounts": sorted(technical_disabled_sender_ids),
                    "failover_count": failover_count,
                    "auto_switch_technical": auto_switch_technical,
                    "run_id": run_id,
                    "skip_chat_title": skip_chat_title,
                })

    async def record_final(
        sender_id: int,
        job: dict,
        status: str,
        error: str | None,
    ):
        employee = job["employee"]
        template = job["template"]
        await db.add_log(employee.get("id"), template["id"], status, error)
        await db.update_broadcast_run_item(
            run_id, int(job["position"]), sender_account_id=sender_id, status=status, error=error
        )
        await db.refresh_broadcast_run_stats(run_id)
        await report_progress(sender_id, employee, template, status, error)

    async def process_job(sender_id: int, job: dict) -> tuple[str, str | None, str | None]:
        employee = job["employee"]
        template = job["template"]
        status = "failed"
        error = None
        error_kind = None

        if await db.has_delivery(employee.get("telegram_id"), employee.get("username")):
            return "skipped", "Уже было отправлено с одного из аккаунтов", None

        if skip_existing_chat:
            in_chat, check_error = await _is_employee_in_chat(
                employee,
                skip_chat_entities[sender_id],
                sender_id,
            )
            if check_error:
                return "failed", check_error, None
            if in_chat:
                return "skipped", f"Уже есть в чате: {skip_chat_title or skip_existing_chat}", None

        reserved = await db.reserve_delivery(
            telegram_id=employee.get("telegram_id"),
            username=employee.get("username"),
            full_name=employee.get("full_name"),
            sender_account_id=sender_id,
            template_id=template.get("id"),
            source_kind="broadcast",
        )
        if not reserved:
            return "skipped", "Уже было отправлено/зарезервировано другим аккаунтом", None

        ok, error, error_kind = await send_to_employee_detailed(
            employee,
            template,
            sender_id=sender_id,
        )
        await db.finish_delivery(
            telegram_id=employee.get("telegram_id"),
            username=employee.get("username"),
            # Если часть шаблона уже ушла, считаем получателя обработанным
            # в общем реестре, чтобы будущая рассылка не продублировала начало.
            success=ok or error_kind in {"technical_partial", "restriction_partial"},
            error=error,
        )
        status = "sent" if ok else "failed"
        return status, error, error_kind

    async def queue_for_failover(sender_id: int, jobs: list[dict], start_index: int):
        nonlocal failover_count
        async with failover_lock:
            for job in jobs[start_index:]:
                moved = {
                    "employee": job["employee"],
                    "template": job["template"],
                    "position": job["position"],
                    "sender_id": job.get("sender_id", sender_id),
                    "attempted_sender_ids": set(job.get("attempted_sender_ids") or set()) | {sender_id},
                }
                failover_jobs.append(moved)
                failover_count += 1

    async def mark_remaining_failed(sender_id: int, jobs: list[dict], start_index: int, reason: str):
        for job in jobs[start_index:]:
            await record_final(sender_id, job, "failed", reason)

    async def run_sender(sender_id: int, jobs: list[dict]):
        stop_event = stop_events[sender_id]
        pause_event = pause_events[sender_id]
        token = db.set_current_owner_id(data_owner_id)
        index = 0
        try:
            while index < len(jobs):
                if await _wait_while_paused(stop_event, pause_event):
                    stopped_sender_ids.add(sender_id)
                    break

                job = jobs[index]
                employee = job["employee"]
                template = job["template"]
                await db.update_broadcast_run_item(
                    run_id, int(job["position"]), sender_account_id=sender_id, status="sending", error=None
                )
                status, error, error_kind = await process_job(sender_id, job)

                if status == "failed" and error_kind == "technical":
                    await db.update_broadcast_run_item(
                        run_id, int(job["position"]), sender_account_id=sender_id, status="pending", error=error
                    )
                    technical_disabled_sender_ids.add(sender_id)
                    if auto_switch_technical:
                        await queue_for_failover(sender_id, jobs, index)
                    else:
                        await record_final(sender_id, job, status, error)
                        await mark_remaining_failed(
                            sender_id,
                            jobs,
                            index + 1,
                            "Аккаунт технически недоступен; автопереключение выключено",
                        )
                    break

                if status == "failed" and error_kind == "restriction":
                    # Ограниченный получатель остаётся pending и будет повторён после успешной проверки.
                    await db.update_broadcast_run_item(
                        run_id, int(job["position"]), sender_account_id=sender_id, status="pending", error=error
                    )
                    await db.set_broadcast_run_paused(run_id, f"restriction:{error or 'restriction'}", sender_id)
                    async with _active_broadcast_lock:
                        _restriction_sender_for_run[run_id] = sender_id
                        for active_sender_id in effective_sender_ids:
                            _restricted_run_by_sender[active_sender_id] = run_id
                    for active_pause_event in pause_events.values():
                        active_pause_event.set()
                    if progress_callback:
                        await progress_callback({
                            "total": total,
                            "processed": processed,
                            "sent": sent,
                            "failed": failed,
                            "skipped": skipped,
                            "templates_used": len(used_template_ids),
                            "current": employee,
                            "ok": False,
                            "status": "paused_restriction",
                            "error": error,
                            "sender_id": sender_id,
                            "sender_account_id": sender_id,
                            "sender_owner_id": int(configured_by_id[sender_id]["owner_id"]),
                            "stopped_accounts": sorted(stopped_sender_ids),
                            "paused_accounts": sorted(effective_sender_ids),
                            "technical_disabled_accounts": sorted(technical_disabled_sender_ids),
                            "failover_count": failover_count,
                            "auto_switch_technical": auto_switch_technical,
                            "run_id": run_id,
                            "restriction_sender_id": sender_id,
                            "pause_reason": error,
                            "skip_chat_title": skip_chat_title,
                        })
                    # Ждём именно разрешённого снятия внутренней паузы; затем повторяем текущего адресата.
                    if await _wait_while_paused(stop_event, pause_event):
                        stopped_sender_ids.add(sender_id)
                        break
                    continue

                if status == "failed" and error_kind == "restriction_partial":
                    # Часть шаблона уже отправлена: текущего адресата не повторяем,
                    # но всю рассылку ставим на паузу до подтверждения снятия ограничения.
                    await record_final(sender_id, job, status, error)
                    await db.set_broadcast_run_paused(run_id, f"restriction:{error or 'restriction_partial'}", sender_id)
                    async with _active_broadcast_lock:
                        _restriction_sender_for_run[run_id] = sender_id
                        for active_sender_id in effective_sender_ids:
                            _restricted_run_by_sender[active_sender_id] = run_id
                    for active_pause_event in pause_events.values():
                        active_pause_event.set()
                    if progress_callback:
                        await progress_callback({
                            "total": total,
                            "processed": processed,
                            "sent": sent,
                            "failed": failed,
                            "skipped": skipped,
                            "templates_used": len(used_template_ids),
                            "current": employee,
                            "ok": False,
                            "status": "paused_restriction",
                            "error": error,
                            "sender_id": sender_id,
                            "sender_account_id": sender_id,
                            "sender_owner_id": int(configured_by_id[sender_id]["owner_id"]),
                            "stopped_accounts": sorted(stopped_sender_ids),
                            "paused_accounts": sorted(effective_sender_ids),
                            "technical_disabled_accounts": sorted(technical_disabled_sender_ids),
                            "failover_count": failover_count,
                            "auto_switch_technical": auto_switch_technical,
                            "run_id": run_id,
                            "restriction_sender_id": sender_id,
                            "pause_reason": error,
                            "skip_chat_title": skip_chat_title,
                        })
                    if await _wait_while_paused(stop_event, pause_event):
                        stopped_sender_ids.add(sender_id)
                        break
                    index += 1
                    continue

                if status == "failed" and error_kind == "technical_partial":
                    await record_final(sender_id, job, status, error)
                    await db.set_broadcast_run_paused(run_id, f"technical_partial:{error or 'technical_partial'}", sender_id)
                    for active_pause_event in pause_events.values():
                        active_pause_event.set()
                    if await _wait_while_paused(stop_event, pause_event):
                        stopped_sender_ids.add(sender_id)
                        break
                    index += 1
                    continue

                await record_final(sender_id, job, status, error)
                index += 1

                if index < len(jobs):
                    delay = random.uniform(delay_min, delay_max)
                    if await _sleep_with_controls(delay, stop_event, pause_event):
                        stopped_sender_ids.add(sender_id)
                        break
        finally:
            db.reset_current_owner_id(token)

    async def finalize_jobs_without_sender(jobs: list[dict], reason: str):
        for job in jobs:
            attempted = sorted(job.get("attempted_sender_ids") or [])
            fallback_sender = attempted[-1] if attempted else effective_sender_ids[0]
            await record_final(fallback_sender, job, "failed", reason)

    run_status = "completed"
    try:
        await asyncio.gather(*[
            run_sender(sender_id, jobs_by_sender[sender_id])
            for sender_id in effective_sender_ids
        ])

        # Обрабатываем технический failover волнами. Аккаунт, на котором произошёл
        # технический сбой, больше не получает задания в этом запуске.
        while failover_jobs and auto_switch_technical:
            async with failover_lock:
                pending_jobs = list(failover_jobs)
                failover_jobs.clear()

            healthy_sender_ids = [
                sender_id
                for sender_id in effective_sender_ids
                if sender_id not in technical_disabled_sender_ids
                and sender_id not in stopped_sender_ids
            ]
            if not healthy_sender_ids:
                await finalize_jobs_without_sender(
                    pending_jobs,
                    "Нет доступного исправного аккаунта для автоматического переключения",
                )
                break

            assignments: dict[int, list[dict]] = {sender_id: [] for sender_id in healthy_sender_ids}
            unassigned: list[dict] = []
            cursor = 0
            for job in pending_jobs:
                attempted = set(job.get("attempted_sender_ids") or set())
                eligible = [sender_id for sender_id in healthy_sender_ids if sender_id not in attempted]
                if not eligible:
                    unassigned.append(job)
                    continue
                sender_id = eligible[cursor % len(eligible)]
                cursor += 1
                assignments[sender_id].append(job)

            if unassigned:
                await finalize_jobs_without_sender(
                    unassigned,
                    "Все выбранные аккаунты уже оказались технически недоступны для этого задания",
                )

            active_assignments = [
                (sender_id, jobs)
                for sender_id, jobs in assignments.items()
                if jobs
            ]
            if not active_assignments:
                break
            await asyncio.gather(*[
                run_sender(sender_id, jobs)
                for sender_id, jobs in active_assignments
            ])

        if stopped_sender_ids:
            run_status = "stopped"
        elif technical_disabled_sender_ids and failed:
            run_status = "completed_with_errors"
    except Exception:
        run_status = "error"
        raise
    finally:
        current_run = await db.get_broadcast_run(run_id)
        if current_run and current_run.get("status") == "paused":
            run_status = "paused"
        await db.finish_broadcast_run(
            run_id,
            sent=sent,
            failed=failed,
            skipped=skipped,
            status=run_status,
        )
        await _unregister_broadcast_sender_ids(effective_sender_ids)

    return {
        "run_id": run_id,
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "templates_used": len(used_template_ids),
        "stopped": bool(stopped_sender_ids),
        "stopped_accounts": sorted(stopped_sender_ids),
        "technical_disabled_accounts": sorted(technical_disabled_sender_ids),
        "failover_count": failover_count,
        "auto_switch_technical": auto_switch_technical,
        "senders_used": len(effective_sender_ids),
        "skip_chat_title": skip_chat_title,
    }


async def resume_broadcast_run(
    run_id: int,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    """Восстанавливает pending-очередь после перезапуска процесса/паузы."""
    run_id = int(run_id)
    lock = _resume_run_locks.setdefault(run_id, asyncio.Lock())
    if lock.locked():
        raise ValueError("Эта рассылка уже продолжает работу")
    async with lock:
        run = await db.get_broadcast_run(run_id)
        if not run:
            raise ValueError("Рассылка не найдена")
        if run.get("status") in {"completed", "completed_with_errors"}:
            raise ValueError("Эта рассылка уже завершена")
        await db.recover_sending_broadcast_items(run_id)
        items = await db.get_broadcast_run_items(run_id, ["pending"])
        if not items:
            stats = await db.refresh_broadcast_run_stats(run_id)
            await db.finish_broadcast_run(run_id, sent=stats["sent"], failed=stats["failed"], skipped=stats["skipped"])
            return {"run_id": run_id, **stats, "stopped": False, "senders_used": 0}

        selected_sender_ids = db.parse_broadcast_sender_ids(run)
        if not selected_sender_ids:
            selected_sender_ids = list(dict.fromkeys(int(item["sender_account_id"]) for item in items if item.get("sender_account_id")))
        if not selected_sender_ids:
            raise ValueError("У запуска не сохранены аккаунты отправки")

        authorized = []
        for sender_id in selected_sender_ids:
            if await is_authorized(sender_id):
                authorized.append(sender_id)
        if not authorized:
            raise ValueError("Нет авторизованных аккаунтов для продолжения")

        await db.set_broadcast_run_running(run_id)
        stop_events, pause_events = await _register_broadcast_sender_ids(authorized)
        delay_min, delay_max = await get_delay_range()
        sent_now = failed_now = skipped_now = 0
        stopped = False
        try:
            for offset, item in enumerate(items):
                sender_id = int(item.get("sender_account_id") or authorized[offset % len(authorized)])
                if sender_id not in authorized:
                    sender_id = authorized[offset % len(authorized)]
                stop_event = stop_events[sender_id]
                pause_event = pause_events[sender_id]
                if await _wait_while_paused(stop_event, pause_event):
                    stopped = True
                    break
                employee = {
                    "id": item.get("employee_id"), "telegram_id": item.get("telegram_id"),
                    "username": item.get("username"), "full_name": item.get("full_name"),
                }
                template = await db.get_template(int(item["template_id"])) if item.get("template_id") else None
                if not template:
                    await db.update_broadcast_run_item(run_id, int(item["position"]), sender_account_id=sender_id, status="failed", error="Шаблон удалён")
                    failed_now += 1
                    continue
                await db.update_broadcast_run_item(run_id, int(item["position"]), sender_account_id=sender_id, status="sending", error=None)
                if await db.has_delivery(employee.get("telegram_id"), employee.get("username")):
                    await db.update_broadcast_run_item(run_id, int(item["position"]), sender_account_id=sender_id, status="skipped", error="Уже было отправлено с одного из аккаунтов")
                    skipped_now += 1
                    continue
                reserved = await db.reserve_delivery(
                    telegram_id=employee.get("telegram_id"), username=employee.get("username"),
                    full_name=employee.get("full_name"), sender_account_id=sender_id,
                    template_id=template.get("id"), source_kind="broadcast_resume",
                )
                if not reserved:
                    await db.update_broadcast_run_item(run_id, int(item["position"]), sender_account_id=sender_id, status="skipped", error="Уже было отправлено/зарезервировано")
                    skipped_now += 1
                    continue
                ok, error, kind = await send_to_employee_detailed(employee, template, sender_id=sender_id)
                await db.finish_delivery(
                    telegram_id=employee.get("telegram_id"), username=employee.get("username"),
                    success=ok or kind in {"technical_partial", "restriction_partial"}, error=error,
                )
                if ok:
                    status = "sent"; sent_now += 1
                elif kind in {"restriction", "restriction_partial"}:
                    if kind == "restriction_partial":
                        # Не повторяем адресата, которому уже ушла часть шаблона.
                        await db.update_broadcast_run_item(run_id, int(item["position"]), sender_account_id=sender_id, status="failed", error=error)
                        failed_now += 1
                    else:
                        await db.update_broadcast_run_item(run_id, int(item["position"]), sender_account_id=sender_id, status="pending", error=error)
                    await db.set_broadcast_run_paused(run_id, f"restriction:{error or 'restriction'}", sender_id)
                    async with _active_broadcast_lock:
                        _restriction_sender_for_run[run_id] = sender_id
                        for active_sender_id in authorized:
                            _restricted_run_by_sender[active_sender_id] = run_id
                    for event in pause_events.values():
                        event.set()
                    stopped = True
                    break
                else:
                    status = "failed"; failed_now += 1
                await db.update_broadcast_run_item(run_id, int(item["position"]), sender_account_id=sender_id, status=status, error=error)
                if progress_callback:
                    stats = await db.refresh_broadcast_run_stats(run_id)
                    await progress_callback({"run_id": run_id, "total": int(run.get("total") or 0), **stats, "current": employee, "status": status, "error": error, "sender_account_id": sender_id})
                if offset < len(items) - 1:
                    await _sleep_with_controls(random.uniform(delay_min, delay_max), stop_event, pause_event)
        finally:
            await _unregister_broadcast_sender_ids(authorized)

        stats = await db.refresh_broadcast_run_stats(run_id)
        latest = await db.get_broadcast_run(run_id)
        if latest and latest.get("status") != "paused" and not stopped:
            await db.finish_broadcast_run(run_id, sent=stats["sent"], failed=stats["failed"], skipped=stats["skipped"], status="completed_with_errors" if stats["failed"] else "completed")
        return {"run_id": run_id, **stats, "stopped": stopped, "senders_used": len(authorized)}

async def import_group_members(group_link_or_id: str) -> list[dict]:
    """Возвращает список участников указанной группы/канала в формате для bulk_add_employees."""
    if not await is_authorized():
        raise ValueError("Юзербот не авторизован")

    client = await _get_client()
    try:
        entity = await _resolve_chat_entity(client, group_link_or_id)
    except ValueError as exc:
        raise ValueError(f"Не удалось открыть группу: {exc}") from exc
    participants = await client.get_participants(entity)
    result = []
    for p in participants:
        if p.bot:
            continue
        full_name = " ".join(filter(None, [p.first_name, p.last_name])) or None
        result.append({
            "telegram_id": p.id,
            "username": p.username,
            "full_name": full_name,
            "group_name": "Все",
        })
    return result
