"""
Слой работы с базой данных (SQLite, через aiosqlite — асинхронно,
безопасно для одновременного использования юзерботом и управляющим ботом).

Данные изолированы по owner_id: каждый пользователь панели видит только своих
пользователей, шаблоны, расписания, логи и настройки.
"""
import contextvars
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

import config

DB_PATH = str(
    Path(config.DB_PATH)
    if Path(config.DB_PATH).is_absolute()
    else Path(__file__).resolve().parent / config.DB_PATH
)

_CURRENT_OWNER_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_owner_id",
    default=None,
)


def default_owner_id() -> int:
    return int(config.ADMIN_IDS[0]) if config.ADMIN_IDS else 0


def is_root_admin(user_id: int | None) -> bool:
    return bool(user_id and int(user_id) in config.ADMIN_IDS)


def get_current_owner_id(default_to_root: bool = True) -> int | None:
    owner_id = _CURRENT_OWNER_ID.get()
    if owner_id is not None:
        return int(owner_id)
    return default_owner_id() if default_to_root else None


def set_current_owner_id(owner_id: int):
    return _CURRENT_OWNER_ID.set(int(owner_id))


def reset_current_owner_id(token):
    _CURRENT_OWNER_ID.reset(token)


def normalize_username(username: str | None) -> str | None:
    if not username:
        return None
    cleaned = username.strip().lstrip("@").lower()
    return cleaned or None


SCHEMA = """
CREATE TABLE IF NOT EXISTS access_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    username TEXT,
    username_normalized TEXT,
    role TEXT DEFAULT 'user',
    active INTEGER DEFAULT 1,
    granted_by INTEGER,
    created_at TEXT,
    last_seen_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_access_users_username
ON access_users(username_normalized);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    telegram_id INTEGER,
    username TEXT,
    full_name TEXT,
    group_name TEXT DEFAULT 'Все',
    active INTEGER DEFAULT 1,
    added_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_owner_telegram
ON employees(owner_id, telegram_id)
WHERE telegram_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_owner_username
ON employees(owner_id, username)
WHERE username IS NOT NULL;

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    name TEXT,
    text TEXT,
    payload TEXT,
    created_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_templates_owner_name
ON templates(owner_id, name);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    template_id INTEGER,
    group_name TEXT DEFAULT 'Все',
    time TEXT,
    days TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_schedules_owner_enabled
ON schedules(owner_id, enabled);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    employee_id INTEGER,
    template_id INTEGER,
    status TEXT,
    error TEXT,
    timestamp TEXT
);

CREATE INDEX IF NOT EXISTS idx_logs_owner_id
ON logs(owner_id, id);

CREATE TABLE IF NOT EXISTS broadcast_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    template_id INTEGER,
    group_name TEXT,
    total INTEGER DEFAULT 0,
    sent INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    selected_account_ids TEXT,
    skip_existing_chat TEXT,
    pause_reason TEXT,
    pause_sender_account_id INTEGER,
    paused_at TEXT,
    updated_at TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_broadcast_runs_owner_id
ON broadcast_runs(owner_id, id DESC);

CREATE TABLE IF NOT EXISTS broadcast_run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    position INTEGER,
    employee_id INTEGER,
    telegram_id INTEGER,
    username TEXT,
    full_name TEXT,
    sender_account_id INTEGER,
    template_id INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    timestamp TEXT NOT NULL,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_broadcast_run_items_run_status
ON broadcast_run_items(owner_id, run_id, status, id);

CREATE TABLE IF NOT EXISTS settings (
    owner_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (owner_id, key)
);

CREATE TABLE IF NOT EXISTS sender_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    title TEXT,
    session_name TEXT NOT NULL UNIQUE,
    telegram_user_id INTEGER,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    active INTEGER DEFAULT 1,
    health_status TEXT DEFAULT 'unknown',
    health_error TEXT,
    health_checked_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    last_authorized_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sender_accounts_owner_active
ON sender_accounts(owner_id, active);

CREATE TABLE IF NOT EXISTS inline_payloads (
    token TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inline_payloads_expires
ON inline_payloads(expires_at);

-- Единый реестр контактов: один получатель считается обработанным для всех
-- аккаунтов отправки одного владельца панели.
CREATE TABLE IF NOT EXISTS delivery_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    recipient_key TEXT NOT NULL,
    telegram_id INTEGER,
    username TEXT,
    full_name TEXT,
    sender_account_id INTEGER,
    template_id INTEGER,
    source_kind TEXT DEFAULT 'broadcast',
    source_chat_id INTEGER,
    source_chat_title TEXT,
    status TEXT NOT NULL DEFAULT 'reserved',
    error TEXT,
    reserved_at TEXT NOT NULL,
    sent_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_id, recipient_key)
);

CREATE INDEX IF NOT EXISTS idx_delivery_registry_owner_status
ON delivery_registry(owner_id, status);

CREATE TABLE IF NOT EXISTS monitored_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    sender_account_id INTEGER NOT NULL,
    chat_ref TEXT NOT NULL,
    chat_id INTEGER,
    chat_title TEXT,
    keywords TEXT NOT NULL,
    template_id INTEGER NOT NULL,
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_monitored_chats_sender_enabled
ON monitored_chats(sender_account_id, enabled);

CREATE TABLE IF NOT EXISTS keyword_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    monitored_chat_id INTEGER NOT NULL,
    sender_account_id INTEGER NOT NULL,
    chat_id INTEGER,
    chat_title TEXT,
    message_id INTEGER,
    author_telegram_id INTEGER,
    author_username TEXT,
    matched_keyword TEXT,
    action TEXT,
    details TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(sender_account_id, chat_id, message_id, author_telegram_id)
);
"""


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    return await cursor.fetchone() is not None


async def _columns(db: aiosqlite.Connection, table: str) -> list[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return [row[1] for row in rows]


async def _rename_legacy_table_if_needed(db: aiosqlite.Connection, table: str) -> str | None:
    if not await _table_exists(db, table):
        return None
    columns = await _columns(db, table)
    if "owner_id" in columns:
        return None
    backup = f"{table}_legacy_before_owner"
    await db.execute(f"DROP TABLE IF EXISTS {backup}")
    await db.execute(f"ALTER TABLE {table} RENAME TO {backup}")
    return backup


async def _copy_legacy_data(db: aiosqlite.Connection, backups: dict[str, str | None]):
    owner_id = default_owner_id()

    if backups.get("employees"):
        await db.execute(
            """
            INSERT OR IGNORE INTO employees
                (id, owner_id, telegram_id, username, full_name, group_name, active, added_at)
            SELECT id, ?, telegram_id, username, full_name, group_name, active, added_at
            FROM employees_legacy_before_owner
            """,
            (owner_id,),
        )
        await db.execute("DROP TABLE employees_legacy_before_owner")

    if backups.get("templates"):
        await db.execute(
            """
            INSERT OR IGNORE INTO templates
                (id, owner_id, name, text, payload, created_at)
            SELECT id, ?, name, text, NULL, created_at
            FROM templates_legacy_before_owner
            """,
            (owner_id,),
        )
        await db.execute("DROP TABLE templates_legacy_before_owner")

    if backups.get("schedules"):
        await db.execute(
            """
            INSERT OR IGNORE INTO schedules
                (id, owner_id, template_id, group_name, time, days, enabled, created_at)
            SELECT id, ?, template_id, group_name, time, days, enabled, created_at
            FROM schedules_legacy_before_owner
            """,
            (owner_id,),
        )
        await db.execute("DROP TABLE schedules_legacy_before_owner")

    if backups.get("logs"):
        await db.execute(
            """
            INSERT OR IGNORE INTO logs
                (id, owner_id, employee_id, template_id, status, error, timestamp)
            SELECT id, ?, employee_id, template_id, status, error, timestamp
            FROM logs_legacy_before_owner
            """,
            (owner_id,),
        )
        await db.execute("DROP TABLE logs_legacy_before_owner")

    if backups.get("settings"):
        await db.execute(
            """
            INSERT OR IGNORE INTO settings (owner_id, key, value)
            SELECT ?, key, value
            FROM settings_legacy_before_owner
            """,
            (owner_id,),
        )
        await db.execute("DROP TABLE settings_legacy_before_owner")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        backups = {}
        for table in ("employees", "templates", "schedules", "logs", "settings"):
            backups[table] = await _rename_legacy_table_if_needed(db, table)

        await db.executescript(SCHEMA)
        await _copy_legacy_data(db, backups)
        await _migrate_schema(db)
        await _migrate_legacy_sender_accounts(db)
        await _backfill_delivery_registry(db)
        await db.commit()

    await ensure_root_admins()


async def _migrate_schema(db: aiosqlite.Connection):
    template_columns = await _columns(db, "templates")
    if "payload" not in template_columns:
        await db.execute("ALTER TABLE templates ADD COLUMN payload TEXT")

    sender_columns = await _columns(db, "sender_accounts")
    if "health_status" not in sender_columns:
        await db.execute("ALTER TABLE sender_accounts ADD COLUMN health_status TEXT DEFAULT 'unknown'")
    if "health_error" not in sender_columns:
        await db.execute("ALTER TABLE sender_accounts ADD COLUMN health_error TEXT")
    if "health_checked_at" not in sender_columns:
        await db.execute("ALTER TABLE sender_accounts ADD COLUMN health_checked_at TEXT")

    run_columns = await _columns(db, "broadcast_runs")
    for name, ddl in (
        ("selected_account_ids", "TEXT"),
        ("skip_existing_chat", "TEXT"),
        ("pause_reason", "TEXT"),
        ("pause_sender_account_id", "INTEGER"),
        ("paused_at", "TEXT"),
        ("updated_at", "TEXT"),
    ):
        if name not in run_columns:
            await db.execute(f"ALTER TABLE broadcast_runs ADD COLUMN {name} {ddl}")

    item_columns = await _columns(db, "broadcast_run_items")
    if "position" not in item_columns:
        await db.execute("ALTER TABLE broadcast_run_items ADD COLUMN position INTEGER")
    if "updated_at" not in item_columns:
        await db.execute("ALTER TABLE broadcast_run_items ADD COLUMN updated_at TEXT")
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_broadcast_run_items_run_position "
        "ON broadcast_run_items(owner_id, run_id, position) WHERE position IS NOT NULL"
    )


def _legacy_session_name(owner_id: int) -> str:
    if int(owner_id) == default_owner_id():
        return config.SESSION_NAME
    return f"{config.SESSION_NAME}_{int(owner_id)}"


def _session_file_exists(session_name: str) -> bool:
    path = Path(session_name)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path.exists() or Path(f"{path}.session").exists()



async def _backfill_delivery_registry(db: aiosqlite.Connection):
    """Подтягивает старые успешные логи в новый общий реестр без повторной отправки."""
    if not await _table_exists(db, "delivery_registry"):
        return
    now = datetime.utcnow().isoformat()
    await db.execute(
        """
        INSERT OR IGNORE INTO delivery_registry
            (owner_id, recipient_key, telegram_id, username, full_name,
             sender_account_id, template_id, source_kind, status,
             reserved_at, sent_at, updated_at)
        SELECT
            e.owner_id,
            CASE
                WHEN e.telegram_id IS NOT NULL THEN 'id:' || CAST(e.telegram_id AS TEXT)
                WHEN e.username IS NOT NULL AND TRIM(e.username) <> '' THEN 'username:' || LOWER(LTRIM(e.username, '@'))
                ELSE NULL
            END,
            e.telegram_id,
            LOWER(LTRIM(e.username, '@')),
            e.full_name,
            NULL,
            l.template_id,
            'legacy_log',
            'sent',
            COALESCE(l.timestamp, ?),
            COALESCE(l.timestamp, ?),
            ?
        FROM logs l
        JOIN employees e ON e.id = l.employee_id AND e.owner_id = l.owner_id
        WHERE l.status = 'sent'
          AND (e.telegram_id IS NOT NULL OR (e.username IS NOT NULL AND TRIM(e.username) <> ''))
        """,
        (now, now, now),
    )


async def _migrate_legacy_sender_accounts(db: aiosqlite.Connection):
    now = datetime.utcnow().isoformat()
    owner_ids = set(int(admin_id) for admin_id in config.ADMIN_IDS)
    if await _table_exists(db, "access_users"):
        cursor = await db.execute(
            "SELECT telegram_id FROM access_users WHERE active = 1 AND telegram_id IS NOT NULL"
        )
        rows = await cursor.fetchall()
        owner_ids.update(int(row[0]) for row in rows)

    for owner_id in sorted(owner_ids):
        session_name = _legacy_session_name(owner_id)
        if not _session_file_exists(session_name):
            continue
        await db.execute(
            """
            INSERT OR IGNORE INTO sender_accounts
                (owner_id, title, session_name, active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (owner_id, "Основной аккаунт", session_name, now, now),
        )


# ---------- Доступ к панели ----------

async def ensure_root_admins():
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        for admin_id in config.ADMIN_IDS:
            await db.execute(
                """
                INSERT INTO access_users
                    (telegram_id, role, active, granted_by, created_at, last_seen_at)
                VALUES (?, 'owner', 1, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    role = 'owner',
                    active = 1,
                    last_seen_at = excluded.last_seen_at
                """,
                (admin_id, admin_id, now, now),
            )
        await db.commit()


async def bind_or_check_access(telegram_id: int, username: str | None) -> bool:
    await ensure_root_admins()
    now = datetime.utcnow().isoformat()
    username_norm = normalize_username(username)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        if is_root_admin(telegram_id):
            await db.execute(
                """
                UPDATE access_users
                SET username = ?, username_normalized = ?, active = 1, last_seen_at = ?
                WHERE telegram_id = ?
                """,
                (username, username_norm, now, telegram_id),
            )
            await db.commit()
            return True

        cursor = await db.execute(
            "SELECT * FROM access_users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if row:
            if not row["active"]:
                return False
            await db.execute(
                """
                UPDATE access_users
                SET username = ?, username_normalized = ?, last_seen_at = ?
                WHERE telegram_id = ?
                """,
                (username, username_norm, now, telegram_id),
            )
            await db.commit()
            return True

        if username_norm:
            cursor = await db.execute(
                """
                SELECT * FROM access_users
                WHERE username_normalized = ? AND active = 1
                ORDER BY id DESC
                LIMIT 1
                """,
                (username_norm,),
            )
            row = await cursor.fetchone()
            if row and row["telegram_id"] is None:
                await db.execute(
                    """
                    UPDATE access_users
                    SET telegram_id = ?, username = ?, username_normalized = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (telegram_id, username, username_norm, now, row["id"]),
                )
                await db.commit()
                return True

    return False


async def grant_access_by_username(username: str, granted_by: int) -> dict:
    username_norm = normalize_username(username)
    if not username_norm:
        raise ValueError("Нужно указать username")

    now = datetime.utcnow().isoformat()
    display_username = username.strip().lstrip("@")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM access_users
            WHERE username_normalized = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (username_norm,),
        )
        existing = await cursor.fetchone()
        if existing:
            await db.execute(
                """
                UPDATE access_users
                SET username = ?, username_normalized = ?, active = 1, granted_by = ?
                WHERE id = ?
                """,
                (display_username, username_norm, granted_by, existing["id"]),
            )
            access_id = existing["id"]
        else:
            cursor = await db.execute(
                """
                INSERT INTO access_users
                    (username, username_normalized, role, active, granted_by, created_at)
                VALUES (?, ?, 'user', 1, ?, ?)
                """,
                (display_username, username_norm, granted_by, now),
            )
            access_id = cursor.lastrowid
        await db.commit()

    return {"id": access_id, "username": display_username}


async def get_access_users(include_inactive: bool = True) -> list[dict]:
    query = """
    SELECT id, telegram_id, username, role, active, granted_by, created_at, last_seen_at
    FROM access_users
    """
    params = []
    if not include_inactive:
        query += " WHERE active = 1"
    query += " ORDER BY role DESC, active DESC, id ASC"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def set_access_active(access_id: int, active: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT telegram_id FROM access_users WHERE id = ?", (access_id,))
        row = await cursor.fetchone()
        if row and is_root_admin(row["telegram_id"]):
            return
        await db.execute(
            "UPDATE access_users SET active = ? WHERE id = ?",
            (int(active), access_id),
        )
        await db.commit()


async def get_authorized_owner_ids() -> list[int]:
    ids = set(int(admin_id) for admin_id in config.ADMIN_IDS)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT telegram_id FROM access_users WHERE active = 1 AND telegram_id IS NOT NULL"
        )
        rows = await cursor.fetchall()
        ids.update(int(row[0]) for row in rows)
    return sorted(ids)


# ---------- Аккаунты отправки ----------

async def create_sender_account(owner_id: int | None = None, title: str | None = None) -> dict:
    owner_id = int(owner_id or get_current_owner_id())
    now = datetime.utcnow().isoformat()
    temporary_session_name = f"{config.SESSION_NAME}_new_{secrets.token_hex(8)}"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            INSERT INTO sender_accounts
                (owner_id, title, session_name, active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (owner_id, title, temporary_session_name, now, now),
        )
        sender_id = int(cursor.lastrowid)
        session_name = f"{config.SESSION_NAME}_sender_{sender_id}"
        await db.execute(
            """
            UPDATE sender_accounts
            SET session_name = ?, updated_at = ?
            WHERE id = ?
            """,
            (session_name, now, sender_id),
        )
        await db.commit()

    account = await get_sender_account(sender_id, owner_id=owner_id)
    if not account:
        raise ValueError("Не удалось создать аккаунт отправки")
    return account


async def ensure_legacy_sender_account(owner_id: int | None = None) -> dict | None:
    """
    Подключает старую одиночную Telethon-сессию как аккаунт отправки.
    Это нужно после перехода на несколько sender_accounts: раньше рабочая сессия
    лежала в sessions/userbot.session, а новые аккаунты получают отдельные файлы.
    """
    owner_id = int(owner_id or get_current_owner_id())
    session_name = _legacy_session_name(owner_id)
    if not _session_file_exists(session_name):
        return None

    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, owner_id FROM sender_accounts WHERE session_name = ?",
            (session_name,),
        )
        row = await cursor.fetchone()
        if row:
            if int(row["owner_id"]) != owner_id:
                return None
            sender_id = int(row["id"])
            await db.execute(
                """
                UPDATE sender_accounts
                SET active = 1,
                    title = COALESCE(title, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                ("Основной аккаунт", now, sender_id),
            )
        else:
            cursor = await db.execute(
                """
                INSERT INTO sender_accounts
                    (owner_id, title, session_name, active, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (owner_id, "Основной аккаунт", session_name, now, now),
            )
            sender_id = int(cursor.lastrowid)
        await db.commit()

    return await get_sender_account(sender_id, owner_id=owner_id)


async def get_sender_account(
    sender_id: int,
    owner_id: int | None = None,
    include_inactive: bool = True,
) -> dict | None:
    query = """
    SELECT id, owner_id, title, session_name, telegram_user_id, username,
           first_name, last_name, active, health_status, health_error, health_checked_at,
           created_at, updated_at, last_authorized_at
    FROM sender_accounts
    WHERE id = ?
    """
    params = [int(sender_id)]
    if owner_id is not None:
        query += " AND owner_id = ?"
        params.append(int(owner_id))
    if not include_inactive:
        query += " AND active = 1"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_sender_accounts(
    owner_id: int | None = None,
    sender_ids: list[int] | set[int] | tuple[int, ...] | None = None,
    include_inactive: bool = False,
) -> list[dict]:
    query = """
    SELECT id, owner_id, title, session_name, telegram_user_id, username,
           first_name, last_name, active, health_status, health_error, health_checked_at,
           created_at, updated_at, last_authorized_at
    FROM sender_accounts
    WHERE 1=1
    """
    params = []
    if owner_id is not None:
        query += " AND owner_id = ?"
        params.append(int(owner_id))
    if sender_ids is not None:
        normalized_ids = [int(sender_id) for sender_id in sender_ids]
        if not normalized_ids:
            return []
        placeholders = ",".join("?" for _ in normalized_ids)
        query += f" AND id IN ({placeholders})"
        params.extend(normalized_ids)
    if not include_inactive:
        query += " AND active = 1"
    query += " ORDER BY owner_id ASC, id ASC"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_default_sender_account(owner_id: int | None = None) -> dict | None:
    owner_id = int(owner_id or get_current_owner_id())
    accounts = await get_sender_accounts(owner_id=owner_id, include_inactive=False)
    return accounts[0] if accounts else None


async def update_sender_account_identity(sender_id: int, me):
    now = datetime.utcnow().isoformat()
    username = getattr(me, "username", None)
    first_name = getattr(me, "first_name", None)
    last_name = getattr(me, "last_name", None)
    telegram_user_id = getattr(me, "id", None)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE sender_accounts
            SET telegram_user_id = ?,
                username = ?,
                first_name = ?,
                last_name = ?,
                updated_at = ?,
                last_authorized_at = ?
            WHERE id = ?
            """,
            (
                int(telegram_user_id) if telegram_user_id is not None else None,
                username,
                first_name,
                last_name,
                now,
                now,
                int(sender_id),
            ),
        )
        await db.commit()


async def set_sender_account_active(sender_id: int, active: bool, owner_id: int | None = None):
    query = "UPDATE sender_accounts SET active = ?, updated_at = ? WHERE id = ?"
    params = [int(active), datetime.utcnow().isoformat(), int(sender_id)]
    if owner_id is not None:
        query += " AND owner_id = ?"
        params.append(int(owner_id))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()


async def set_sender_account_health(
    sender_id: int,
    status: str,
    error: str | None = None,
    owner_id: int | None = None,
):
    """Сохраняет техническое состояние аккаунта для панели и failover-логики."""
    allowed = {"unknown", "ok", "unauthorized", "technical_error", "restricted"}
    normalized_status = status if status in allowed else "unknown"
    now = datetime.utcnow().isoformat()
    query = """
        UPDATE sender_accounts
        SET health_status = ?, health_error = ?, health_checked_at = ?, updated_at = ?
        WHERE id = ?
    """
    params: list = [normalized_status, error, now, now, int(sender_id)]
    if owner_id is not None:
        query += " AND owner_id = ?"
        params.append(int(owner_id))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()


# ---------- Пользователи ----------

async def add_employee(username: str = None, telegram_id: int = None,
                       full_name: str = None, group_name: str = "Все"):
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO employees
                (owner_id, telegram_id, username, full_name, group_name, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id,
                telegram_id,
                username.lstrip("@") if username else None,
                full_name,
                group_name,
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()


async def bulk_add_employees(rows: list[dict]):
    """rows: список словарей с ключами username/telegram_id/full_name/group_name"""
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        for row in rows:
            await db.execute(
                """
                INSERT OR IGNORE INTO employees
                    (owner_id, telegram_id, username, full_name, group_name, added_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    row.get("telegram_id"),
                    (row.get("username") or "").lstrip("@") or None,
                    row.get("full_name"),
                    row.get("group_name") or "Все",
                    datetime.utcnow().isoformat(),
                ),
            )
        await db.commit()


async def get_employees(group_name: str = None, active_only: bool = True):
    owner_id = get_current_owner_id()
    query = """
    SELECT id, owner_id, telegram_id, username, full_name, group_name, active
    FROM employees
    WHERE owner_id = ?
    """
    params = [owner_id]
    if group_name and group_name != "Все":
        query += " AND group_name = ?"
        params.append(group_name)
    if active_only:
        query += " AND active = 1"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_groups():
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT DISTINCT group_name FROM employees WHERE owner_id = ?",
            (owner_id,),
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def delete_employee(employee_id: int):
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM employees WHERE id = ? AND owner_id = ?",
            (employee_id, owner_id),
        )
        await db.commit()


async def delete_all_employees() -> int:
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM employees WHERE owner_id = ?",
            (owner_id,),
        )
        row = await cursor.fetchone()
        deleted = int(row[0]) if row else 0
        await db.execute("DELETE FROM employees WHERE owner_id = ?", (owner_id,))
        await db.commit()
    return deleted


async def set_employee_active(employee_id: int, active: bool):
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE employees SET active = ? WHERE id = ? AND owner_id = ?",
            (int(active), employee_id, owner_id),
        )
        await db.commit()


# ---------- Шаблоны сообщений ----------

async def add_template(name: str, text: str, payload: str = None):
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO templates (owner_id, name, text, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, name) DO UPDATE SET
                text = excluded.text,
                payload = excluded.payload,
                created_at = excluded.created_at
            """,
            (owner_id, name, text, payload, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_templates():
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, owner_id, name, text, payload FROM templates WHERE owner_id = ? ORDER BY id DESC",
            (owner_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_template(template_id: int):
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, owner_id, name, text, payload FROM templates WHERE id = ? AND owner_id = ?",
            (template_id, owner_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_template(template_id: int):
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM templates WHERE id = ? AND owner_id = ?",
            (template_id, owner_id),
        )
        await db.commit()


# ---------- Расписания ----------

async def add_schedule(template_id: int, group_name: str, time: str, days: str):
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO schedules
                (owner_id, template_id, group_name, time, days, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (owner_id, template_id, group_name, time, days, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_schedules(enabled_only: bool = False):
    owner_id = get_current_owner_id(default_to_root=False)
    query = "SELECT id, owner_id, template_id, group_name, time, days, enabled FROM schedules WHERE 1=1"
    params = []
    if owner_id is not None:
        query += " AND owner_id = ?"
        params.append(owner_id)
    if enabled_only:
        query += " AND enabled = 1"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def set_schedule_enabled(schedule_id: int, enabled: bool):
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE schedules SET enabled = ? WHERE id = ? AND owner_id = ?",
            (int(enabled), schedule_id, owner_id),
        )
        await db.commit()


async def delete_schedule(schedule_id: int):
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM schedules WHERE id = ? AND owner_id = ?",
            (schedule_id, owner_id),
        )
        await db.commit()


# ---------- Настройки ----------

async def get_setting(key: str, default: str = None) -> str | None:
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM settings WHERE owner_id = ? AND key = ?",
            (owner_id, key),
        )
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str):
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (owner_id, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(owner_id, key) DO UPDATE SET value = excluded.value
            """,
            (owner_id, key, value),
        )
        await db.commit()


# ---------- Inline-режим для отправки через via @bot ----------

async def create_inline_payload(payload: str, ttl_seconds: int = 300) -> str:
    owner_id = get_current_owner_id()
    now = datetime.utcnow()
    expires_at = now + timedelta(seconds=ttl_seconds)
    token = secrets.token_urlsafe(16)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM inline_payloads WHERE expires_at <= ?",
            (now.isoformat(),),
        )
        await db.execute(
            """
            INSERT INTO inline_payloads (token, owner_id, payload, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (token, owner_id, payload, now.isoformat(), expires_at.isoformat()),
        )
        await db.commit()
    return token


async def get_inline_payload(token: str) -> str | None:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM inline_payloads WHERE expires_at <= ?", (now,))
        cursor = await db.execute(
            """
            SELECT payload
            FROM inline_payloads
            WHERE token = ? AND expires_at > ?
            """,
            (token, now),
        )
        row = await cursor.fetchone()
        await db.commit()
    return row[0] if row else None


# ---------- Логи ----------

async def add_log(employee_id: int, template_id: int, status: str, error: str = None):
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO logs
                (owner_id, employee_id, template_id, status, error, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (owner_id, employee_id, template_id, status, error, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_recent_logs(limit: int = 20):
    owner_id = get_current_owner_id()
    query = """
    SELECT logs.id, employees.full_name, employees.username, templates.name as template_name,
           logs.status, logs.error, logs.timestamp
    FROM logs
    LEFT JOIN employees ON logs.employee_id = employees.id AND logs.owner_id = employees.owner_id
    LEFT JOIN templates ON logs.template_id = templates.id AND logs.owner_id = templates.owner_id
    WHERE logs.owner_id = ?
    ORDER BY logs.id DESC
    LIMIT ?
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, (owner_id, limit))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------- Запуски рассылок и детальные результаты ----------

async def create_broadcast_run(
    template_id: int | None,
    group_name: str | None,
    total: int,
    *,
    selected_account_ids: list[int] | None = None,
    skip_existing_chat: str | None = None,
) -> int:
    owner_id = get_current_owner_id()
    now = datetime.utcnow().isoformat()
    account_ids = ",".join(str(int(x)) for x in (selected_account_ids or [])) or None
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO broadcast_runs
                (owner_id, template_id, group_name, total, status, selected_account_ids,
                 skip_existing_chat, updated_at, started_at)
            VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (owner_id, template_id, group_name, int(total), account_ids, skip_existing_chat, now, now),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def add_broadcast_run_item(
    run_id: int,
    employee: dict,
    template_id: int | None,
    sender_account_id: int | None,
    status: str,
    error: str | None = None,
    *,
    position: int | None = None,
):
    """Добавляет элемент запуска. Для checkpoint position должен быть стабильным."""
    owner_id = get_current_owner_id()
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO broadcast_run_items
                (owner_id, run_id, position, employee_id, telegram_id, username, full_name,
                 sender_account_id, template_id, status, error, timestamp, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id, int(run_id), position, employee.get("id"), employee.get("telegram_id"),
                normalize_username(employee.get("username")), employee.get("full_name"),
                sender_account_id, template_id, status, error, now, now,
            ),
        )
        await db.commit()


async def initialize_broadcast_run_items(run_id: int, jobs: list[dict]):
    """Создаёт persistent-очередь до первой отправки, чтобы её можно было восстановить."""
    owner_id = get_current_owner_id()
    now = datetime.utcnow().isoformat()
    rows = []
    for job in jobs:
        employee = job["employee"]
        template = job["template"]
        rows.append((
            owner_id, int(run_id), int(job["position"]), employee.get("id"), employee.get("telegram_id"),
            normalize_username(employee.get("username")), employee.get("full_name"),
            int(job["sender_id"]), int(template["id"]), "pending", None, now, now,
        ))
    if not rows:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """
            INSERT OR IGNORE INTO broadcast_run_items
                (owner_id, run_id, position, employee_id, telegram_id, username, full_name,
                 sender_account_id, template_id, status, error, timestamp, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await db.commit()


async def update_broadcast_run_item(
    run_id: int,
    position: int,
    *,
    sender_account_id: int | None = None,
    status: str | None = None,
    error: str | None = None,
):
    owner_id = get_current_owner_id()
    now = datetime.utcnow().isoformat()
    sets = ["updated_at=?", "timestamp=?"]
    params: list = [now, now]
    if sender_account_id is not None:
        sets.append("sender_account_id=?")
        params.append(int(sender_account_id))
    if status is not None:
        sets.append("status=?")
        params.append(str(status))
    sets.append("error=?")
    params.append(error)
    params.extend([int(run_id), owner_id, int(position)])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE broadcast_run_items SET {', '.join(sets)} WHERE run_id=? AND owner_id=? AND position=?",
            params,
        )
        await db.commit()


async def set_broadcast_run_paused(run_id: int, reason: str, sender_account_id: int | None = None):
    owner_id = get_current_owner_id()
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE broadcast_runs
               SET status='paused', pause_reason=?, pause_sender_account_id=?, paused_at=?, updated_at=?, finished_at=NULL
               WHERE id=? AND owner_id=?""",
            (reason, sender_account_id, now, now, int(run_id), owner_id),
        )
        await db.commit()


async def set_broadcast_run_running(run_id: int):
    owner_id = get_current_owner_id()
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE broadcast_runs
               SET status='running', pause_reason=NULL, pause_sender_account_id=NULL,
                   paused_at=NULL, updated_at=?, finished_at=NULL
               WHERE id=? AND owner_id=?""",
            (now, int(run_id), owner_id),
        )
        await db.commit()


async def refresh_broadcast_run_stats(run_id: int) -> dict:
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT status, COUNT(*) AS count FROM broadcast_run_items
               WHERE run_id=? AND owner_id=? GROUP BY status""",
            (int(run_id), owner_id),
        )
        counts = {row["status"]: int(row["count"]) for row in await cursor.fetchall()}
        sent = counts.get("sent", 0)
        failed = counts.get("failed", 0)
        skipped = counts.get("skipped", 0)
        pending = counts.get("pending", 0) + counts.get("sending", 0)
        now = datetime.utcnow().isoformat()
        await db.execute(
            "UPDATE broadcast_runs SET sent=?, failed=?, skipped=?, updated_at=? WHERE id=? AND owner_id=?",
            (sent, failed, skipped, now, int(run_id), owner_id),
        )
        await db.commit()
        return {"sent": sent, "failed": failed, "skipped": skipped, "pending": pending}


async def finish_broadcast_run(
    run_id: int,
    *,
    sent: int,
    failed: int,
    skipped: int,
    status: str = "completed",
):
    owner_id = get_current_owner_id()
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE broadcast_runs
            SET sent=?, failed=?, skipped=?, status=?, finished_at=?, updated_at=?,
                pause_reason=CASE WHEN ?='paused' THEN pause_reason ELSE NULL END,
                pause_sender_account_id=CASE WHEN ?='paused' THEN pause_sender_account_id ELSE NULL END
            WHERE id=? AND owner_id=?
            """,
            (int(sent), int(failed), int(skipped), status,
             None if status == "paused" else now, now, status, status, int(run_id), owner_id),
        )
        await db.commit()


async def get_broadcast_run(run_id: int) -> dict | None:
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM broadcast_runs WHERE id=? AND owner_id=?",
            (int(run_id), owner_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_broadcast_run_items(run_id: int, statuses: list[str] | None = None) -> list[dict]:
    owner_id = get_current_owner_id()
    params: list = [int(run_id), owner_id]
    where = "run_id=? AND owner_id=?"
    if statuses:
        clean = [str(status) for status in statuses]
        where += f" AND status IN ({','.join('?' for _ in clean)})"
        params.extend(clean)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT * FROM broadcast_run_items WHERE {where} ORDER BY COALESCE(position, id), id",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_recent_broadcast_runs(limit: int = 20) -> list[dict]:
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM broadcast_runs
            WHERE owner_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (owner_id, int(limit)),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def recover_sending_broadcast_items(run_id: int):
    """После рестарта процесса незавершённый sending снова становится pending."""
    owner_id = get_current_owner_id()
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE broadcast_run_items SET status='pending', updated_at=?
               WHERE run_id=? AND owner_id=? AND status='sending'""",
            (now, int(run_id), owner_id),
        )
        await db.commit()


def parse_broadcast_sender_ids(run: dict) -> list[int]:
    raw = run.get("selected_account_ids") or ""
    result = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


# ---------- Единый реестр отправок ----------

def recipient_key(telegram_id: int | None = None, username: str | None = None) -> str | None:
    if telegram_id is not None:
        return f"id:{int(telegram_id)}"
    normalized = normalize_username(username)
    if normalized:
        return f"username:{normalized}"
    return None


async def reserve_delivery(
    *,
    telegram_id: int | None = None,
    username: str | None = None,
    full_name: str | None = None,
    sender_account_id: int | None = None,
    template_id: int | None = None,
    source_kind: str = "broadcast",
    source_chat_id: int | None = None,
    source_chat_title: str | None = None,
) -> bool:
    """Атомарно резервирует получателя. False = ему уже писали/его уже обрабатывают."""
    owner_id = get_current_owner_id()
    normalized = normalize_username(username)
    key = recipient_key(telegram_id, normalized)
    if not key:
        return False
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        conditions = []
        params: list = [owner_id]
        if telegram_id is not None:
            conditions.append("telegram_id = ?")
            params.append(int(telegram_id))
        if normalized:
            conditions.append("username = ?")
            params.append(normalized)
        if conditions:
            cursor = await db.execute(
                f"SELECT 1 FROM delivery_registry WHERE owner_id=? AND ({' OR '.join(conditions)}) LIMIT 1",
                params,
            )
            if await cursor.fetchone() is not None:
                await db.rollback()
                return False
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO delivery_registry
                (owner_id, recipient_key, telegram_id, username, full_name,
                 sender_account_id, template_id, source_kind, source_chat_id,
                 source_chat_title, status, reserved_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
            """,
            (
                owner_id, key, telegram_id, normalized, full_name,
                sender_account_id, template_id, source_kind, source_chat_id,
                source_chat_title, now, now,
            ),
        )
        await db.commit()
        return cursor.rowcount == 1


async def finish_delivery(
    *,
    telegram_id: int | None = None,
    username: str | None = None,
    success: bool,
    error: str | None = None,
):
    owner_id = get_current_owner_id()
    normalized = normalize_username(username)
    conditions = []
    params: list = []
    if telegram_id is not None:
        conditions.append("telegram_id = ?")
        params.append(int(telegram_id))
    if normalized:
        conditions.append("username = ?")
        params.append(normalized)
    if not conditions:
        return
    where = " OR ".join(conditions)
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        if success:
            await db.execute(
                f"""UPDATE delivery_registry
                   SET status='sent', error=NULL, sent_at=?, updated_at=?
                   WHERE owner_id=? AND ({where})""",
                [now, now, owner_id, *params],
            )
        else:
            await db.execute(
                f"DELETE FROM delivery_registry WHERE owner_id=? AND status='reserved' AND ({where})",
                [owner_id, *params],
            )
        await db.commit()


async def has_delivery(telegram_id: int | None = None, username: str | None = None) -> bool:
    owner_id = get_current_owner_id()
    normalized = normalize_username(username)
    conditions = []
    params: list = [owner_id]
    if telegram_id is not None:
        conditions.append("telegram_id = ?")
        params.append(int(telegram_id))
    if normalized:
        conditions.append("username = ?")
        params.append(normalized)
    if not conditions:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT 1 FROM delivery_registry WHERE owner_id=? AND ({' OR '.join(conditions)}) LIMIT 1",
            params,
        )
        return await cursor.fetchone() is not None


async def get_delivery_registry(limit: int = 200) -> list[dict]:
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM delivery_registry WHERE owner_id=?
               ORDER BY COALESCE(sent_at, reserved_at) DESC LIMIT ?""",
            (owner_id, int(limit)),
        )
        return [dict(r) for r in await cursor.fetchall()]


async def find_deliveries(telegram_id: int | None = None, username: str | None = None, limit: int = 20) -> list[dict]:
    """Возвращает записи общего реестра по Telegram ID и/или username текущего владельца."""
    owner_id = get_current_owner_id()
    normalized = normalize_username(username)
    conditions = []
    params: list = [owner_id]
    if telegram_id is not None:
        conditions.append("telegram_id = ?")
        params.append(int(telegram_id))
    if normalized:
        conditions.append("username = ?")
        params.append(normalized)
    if not conditions:
        return []
    params.append(int(limit))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT * FROM delivery_registry
                WHERE owner_id=? AND ({' OR '.join(conditions)})
                ORDER BY COALESCE(sent_at, reserved_at) DESC
                LIMIT ?""",
            params,
        )
        return [dict(r) for r in await cursor.fetchall()]


# ---------- Мониторинг чатов по ключевым словам ----------

async def add_monitored_chat(sender_account_id: int, chat_ref: str, chat_id: int | None,
                             chat_title: str | None, keywords: list[str], template_id: int) -> int:
    owner_id = get_current_owner_id()
    cleaned = [k.strip().lower() for k in keywords if k and k.strip()]
    if not cleaned:
        raise ValueError("Нужно хотя бы одно ключевое слово")
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO monitored_chats
               (owner_id, sender_account_id, chat_ref, chat_id, chat_title, keywords,
                template_id, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (owner_id, int(sender_account_id), chat_ref, chat_id, chat_title,
             "\n".join(cleaned), int(template_id), now, now),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def get_monitored_chats(sender_account_id: int | None = None, enabled_only: bool = False) -> list[dict]:
    owner_id = get_current_owner_id(default_to_root=False)
    query = "SELECT * FROM monitored_chats WHERE 1=1"
    params: list = []
    if owner_id is not None:
        query += " AND owner_id=?"
        params.append(int(owner_id))
    if sender_account_id is not None:
        query += " AND sender_account_id=?"
        params.append(int(sender_account_id))
    if enabled_only:
        query += " AND enabled=1"
    query += " ORDER BY id DESC"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = [dict(r) for r in await cursor.fetchall()]
    for row in rows:
        row["keywords_list"] = [x for x in (row.get("keywords") or "").splitlines() if x]
    return rows


async def delete_monitored_chat(watch_id: int) -> bool:
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM monitored_chats WHERE id=? AND owner_id=?",
            (int(watch_id), owner_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def add_keyword_hit(*, monitored_chat_id: int, sender_account_id: int,
                          chat_id: int | None, chat_title: str | None, message_id: int | None,
                          author_telegram_id: int | None, author_username: str | None,
                          matched_keyword: str | None, action: str, details: str | None = None):
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR IGNORE INTO keyword_hits
               (owner_id, monitored_chat_id, sender_account_id, chat_id, chat_title,
                message_id, author_telegram_id, author_username, matched_keyword,
                action, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_id, int(monitored_chat_id), int(sender_account_id), chat_id, chat_title,
             message_id, author_telegram_id, normalize_username(author_username),
             matched_keyword, action, details, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_keyword_hits(limit: int = 100) -> list[dict]:
    owner_id = get_current_owner_id()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM keyword_hits WHERE owner_id=?
               ORDER BY id DESC LIMIT ?""",
            (owner_id, int(limit)),
        )
        return [dict(r) for r in await cursor.fetchall()]
