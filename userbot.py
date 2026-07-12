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
import struct
from typing import Awaitable, Callable
import zlib

import qrcode
from telethon import TelegramClient, utils
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
_active_broadcast_lock = asyncio.Lock()
_inline_bot_username: str | None = getattr(config, "INLINE_BOT_USERNAME", None)
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
    Возвращает авторизованные аккаунты-отправители.
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
        })
    return accounts


async def get_active_broadcast_sender_ids() -> list[int]:
    async with _active_broadcast_lock:
        return sorted(_active_broadcast_stop_events.keys())


async def request_broadcast_stop(sender_id: int | None = None) -> list[int]:
    """
    Ставит флаг остановки активной рассылки.
    Если sender_id=None, останавливает все активные рассылки.
    Возвращает список аккаунтов, для которых флаг был выставлен.
    """
    async with _active_broadcast_lock:
        if sender_id is None:
            sender_ids = list(_active_broadcast_stop_events.keys())
        else:
            sender_ids = [int(sender_id)] if int(sender_id) in _active_broadcast_stop_events else []
        for active_sender_id in sender_ids:
            _active_broadcast_stop_events[active_sender_id].set()
        return sorted(sender_ids)


async def _register_broadcast_sender_ids(sender_ids: list[int]) -> dict[int, asyncio.Event]:
    async with _active_broadcast_lock:
        busy_sender_ids = [sender_id for sender_id in sender_ids if sender_id in _active_broadcast_stop_events]
        if busy_sender_ids:
            busy = ", ".join(str(sender_id) for sender_id in busy_sender_ids)
            raise ValueError(f"На аккаунтах уже идёт рассылка: {busy}")
        events = {sender_id: asyncio.Event() for sender_id in sender_ids}
        _active_broadcast_stop_events.update(events)
        return events


async def _unregister_broadcast_sender_ids(sender_ids: list[int]):
    async with _active_broadcast_lock:
        for sender_id in sender_ids:
            _active_broadcast_stop_events.pop(sender_id, None)


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


async def _resolve_required_chat_entity(client: TelegramClient, chat_link_or_id: str, sender_id: int):
    chat_ref = _normalize_chat_ref(chat_link_or_id)
    try:
        entity = await client.get_entity(chat_ref)
    except (ChannelInvalidError, ChannelPrivateError, ValueError) as e:
        raise ValueError(
            f"Аккаунт #{sender_id} не может открыть этот чат. "
            "Проверь ссылку/id и добавь юзербот в чат."
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


async def send_to_employee(
    employee: dict,
    template: dict | str,
    sender_id: int | None = None,
) -> tuple[bool, str | None]:
    """Отправляет один шаблон. Возвращает (успех, текст_ошибки)."""
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
            if post_delay_seconds and index < len(posts) - 1:
                await asyncio.sleep(post_delay_seconds)
        return True, None
    except FloodWaitError as e:
        return False, f"FloodWait: нужно подождать {e.seconds} сек."
    except UserPrivacyRestrictedError:
        return False, "Настройки приватности пользователя запрещают писать первым"
    except PeerFloodError:
        return False, "Telegram временно ограничил отправку сообщений (PeerFlood)"
    except Exception as e:
        return False, str(e)


async def _sleep_or_stopped(delay_seconds: float, stop_event: asyncio.Event) -> bool:
    if delay_seconds <= 0:
        return stop_event.is_set()
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_seconds)
        return True
    except asyncio.TimeoutError:
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
) -> dict:
    """
    Рассылает шаблон списку пользователей с задержкой между отправками.
    Если передано несколько sender_account_ids, отправляет параллельно с этих аккаунтов.
    Если template_id=0/None, ротирует все доступные шаблоны.
    skip_existing_chat задаёт чат, где найденных получателей нужно пропускать.
    Возвращает статистику {'sent': N, 'failed': N, 'skipped': N, 'templates_used': N, 'stopped': bool}.
    progress_callback вызывается после каждой попытки отправки.
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
    if unauthorized_sender_ids:
        senders = ", ".join(f"#{sender_id}" for sender_id in unauthorized_sender_ids)
        raise ValueError(f"Юзербот не авторизован для аккаунтов: {senders}")

    total = len(employees)
    templates, rotation_enabled = await _get_broadcast_templates(template_id)
    delay_min, delay_max = await get_delay_range()

    sent, failed, skipped = 0, 0, 0
    processed = 0
    used_template_ids = set()
    previous_template_id = None
    stopped_sender_ids = set()
    jobs_by_sender = {sender_id: [] for sender_id in sender_account_ids}

    for index, employee in enumerate(employees):
        template = _next_template(templates, index, previous_template_id) if rotation_enabled else templates[0]
        previous_template_id = template["id"]
        sender_id = sender_account_ids[index % len(sender_account_ids)]
        jobs_by_sender[sender_id].append((employee, template))

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

    stop_events = await _register_broadcast_sender_ids(effective_sender_ids)
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
                    "Не удалось отправить пользователю "
                    f"{employee.get('username') or employee.get('telegram_id')}: {error}"
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
                    "skip_chat_title": skip_chat_title,
                })

    async def run_sender(sender_id: int, jobs: list[tuple[dict, dict]]):
        stop_event = stop_events[sender_id]
        token = db.set_current_owner_id(data_owner_id)
        try:
            for index, (employee, template) in enumerate(jobs):
                if stop_event.is_set():
                    stopped_sender_ids.add(sender_id)
                    break

                status = "failed"
                error = None
                if skip_existing_chat:
                    in_chat, check_error = await _is_employee_in_chat(
                        employee,
                        skip_chat_entities[sender_id],
                        sender_id,
                    )
                    if check_error:
                        error = check_error
                    elif in_chat:
                        status = "skipped"
                        error = f"Уже есть в чате: {skip_chat_title or skip_existing_chat}"
                    else:
                        ok, error = await send_to_employee(
                            employee,
                            template,
                            sender_id=sender_id,
                        )
                        status = "sent" if ok else "failed"
                else:
                    ok, error = await send_to_employee(
                        employee,
                        template,
                        sender_id=sender_id,
                    )
                    status = "sent" if ok else "failed"

                await db.add_log(employee["id"], template["id"], status, error)
                await report_progress(sender_id, employee, template, status, error)

                if index < len(jobs) - 1:
                    delay = random.uniform(delay_min, delay_max)
                    if await _sleep_or_stopped(delay, stop_event):
                        stopped_sender_ids.add(sender_id)
                        break
        finally:
            db.reset_current_owner_id(token)

    try:
        await asyncio.gather(*[
            run_sender(sender_id, jobs_by_sender[sender_id])
            for sender_id in effective_sender_ids
        ])
    finally:
        await _unregister_broadcast_sender_ids(effective_sender_ids)

    return {
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "templates_used": len(used_template_ids),
        "stopped": bool(stopped_sender_ids),
        "stopped_accounts": sorted(stopped_sender_ids),
        "senders_used": len(effective_sender_ids),
        "skip_chat_title": skip_chat_title,
    }


async def import_group_members(group_link_or_id: str) -> list[dict]:
    """Возвращает список участников указанной группы/канала в формате для bulk_add_employees."""
    if not await is_authorized():
        raise ValueError("Юзербот не авторизован")

    client = await _get_client()
    entity = await client.get_entity(group_link_or_id)
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
