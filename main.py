import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time

from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    WebAppInfo,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ChatMemberUpdated
)


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain.com")

DB_PATH = "telegramm_chat.sqlite3"
PORT = int(os.getenv("PORT", "8000"))

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

PERMISSIONS = {
    "manage_roles",
    "manage_members",
    "manage_permissions",
    "mention_roles"
}

ADMIN_PERMISSIONS = sorted(PERMISSIONS)


# ============================================================
# DATABASE
# ============================================================

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS members (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY(chat_id, user_id),
            FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            color TEXT NOT NULL,
            position INTEGER NOT NULL,
            permissions TEXT NOT NULL DEFAULT '[]',
            protected INTEGER NOT NULL DEFAULT 0,
            UNIQUE(chat_id, name),
            FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_roles (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            PRIMARY KEY(chat_id, user_id, role_id),
            FOREIGN KEY(chat_id, user_id)
                REFERENCES members(chat_id, user_id) ON DELETE CASCADE,
            FOREIGN KEY(role_id)
                REFERENCES roles(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_messages_chat
        ON messages(chat_id, id);
        """)


# ============================================================
# USER HELPERS
# ============================================================

def save_user(user):
    uid = int(user.id)

    with db() as conn:
        conn.execute("""
            INSERT INTO users(id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (
            uid,
            user.username,
            user.first_name or "Пользователь"
        ))


def save_user_dict(user):
    uid = int(user["id"])

    with db() as conn:
        conn.execute("""
            INSERT INTO users(id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (
            uid,
            user.get("username"),
            user.get("first_name") or "Пользователь"
        ))


# ============================================================
# GROUP INITIALIZATION
# ============================================================

async def register_group(chat):
    """
    Автоматически определяет создателя Telegram-группы.
    Для получения списка администраторов бот должен быть
    администратором группы.
    """

    if chat.type not in ("group", "supergroup"):
        return

    administrators = await bot.get_chat_administrators(chat.id)

    creator = next(
        (
            admin.user
            for admin in administrators
            if admin.status == "creator"
        ),
        None
    )

    if not creator:
        raise RuntimeError(
            f"Не удалось определить владельца группы {chat.id}"
        )

    owner_id = creator.id

    save_user(creator)

    title = chat.title or "Telegram Group"

    with db() as conn:

        conn.execute("""
            INSERT INTO chats(id, title, owner_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                owner_id=excluded.owner_id
        """, (
            chat.id,
            title,
            owner_id,
            datetime.now(timezone.utc).isoformat()
        ))

        defaults = [
            (
                "Владелец",
                "#e0b84c",
                10000,
                ["*"],
                1
            ),
            (
                "Администратор",
                "#e05252",
                9000,
                ADMIN_PERMISSIONS,
                1
            ),
            (
                "Участник",
                "#9aa0aa",
                0,
                [],
                1
            )
        ]

        for name, color, position, permissions, protected in defaults:
            conn.execute("""
                INSERT OR IGNORE INTO roles
                (chat_id, name, color, position, permissions, protected)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                chat.id,
                name,
                color,
                position,
                json.dumps(permissions),
                protected
            ))

        owner_role = conn.execute("""
            SELECT id FROM roles
            WHERE chat_id=? AND name='Владелец'
        """, (chat.id,)).fetchone()["id"]

        admin_role = conn.execute("""
            SELECT id FROM roles
            WHERE chat_id=? AND name='Администратор'
        """, (chat.id,)).fetchone()["id"]

        member_role = conn.execute("""
            SELECT id FROM roles
            WHERE chat_id=? AND name='Участник'
        """, (chat.id,)).fetchone()["id"]

        conn.execute("""
            INSERT OR IGNORE INTO members(chat_id, user_id)
            VALUES (?, ?)
        """, (chat.id, owner_id))

        # Если владелец группы изменился, обновляем его роль.
        conn.execute("""
            DELETE FROM user_roles
            WHERE chat_id=? AND role_id=?
        """, (chat.id, owner_role))

        conn.execute("""
            INSERT OR IGNORE INTO user_roles(chat_id, user_id, role_id)
            VALUES (?, ?, ?)
        """, (chat.id, owner_id, owner_role))

        for admin in administrators:
            uid = admin.user.id

            save_user(admin.user)

            conn.execute("""
                INSERT OR IGNORE INTO members(chat_id, user_id)
                VALUES (?, ?)
            """, (chat.id, uid))

            if admin.status == "administrator":
                conn.execute("""
                    INSERT OR IGNORE INTO user_roles
                    (chat_id, user_id, role_id)
                    VALUES (?, ?, ?)
                """, (chat.id, uid, admin_role))

        # Создатель и администраторы не получают роль участника.
        conn.execute("""
            DELETE FROM user_roles
            WHERE chat_id=? AND role_id=?
            AND user_id IN (
                SELECT user_id FROM members
                WHERE chat_id=?
            )
            AND user_id IN (
                SELECT owner_id FROM chats WHERE id=?
            )
        """, (chat.id, member_role, chat.id, chat.id))


async def sync_member(chat_id, user):
    """
    Регистрирует участника, если бот получил обновление о нём.
    """

    save_user(user)

    with db() as conn:

        group = conn.execute(
            "SELECT id FROM chats WHERE id=?",
            (chat_id,)
        ).fetchone()

        if not group:
            return

        conn.execute("""
            INSERT OR IGNORE INTO members(chat_id, user_id)
            VALUES (?, ?)
        """, (chat_id, user.id))

        member_role = conn.execute("""
            SELECT id FROM roles
            WHERE chat_id=? AND name='Участник'
        """, (chat_id,)).fetchone()

        if member_role:
            conn.execute("""
                INSERT OR IGNORE INTO user_roles
                (chat_id, user_id, role_id)
                VALUES (?, ?, ?)
            """, (chat_id, user.id, member_role["id"]))


# ============================================================
# TELEGRAM EVENTS
# ============================================================

@dp.my_chat_member()
async def bot_added(event: ChatMemberUpdated):

    chat = event.chat

    if chat.type not in ("group", "supergroup"):
        return

    if event.new_chat_member.status in ("administrator", "member"):

        try:
            await register_group(chat)

            await bot.send_message(
                chat.id,
                "TELEGRAMM CHAT подключён!\n\n"
                "Владелец группы определён автоматически.\n"
                "Для управления откройте бота в личных сообщениях."
            )

        except Exception as error:
            print("Ошибка регистрации группы:", error)


@dp.chat_member()
async def member_updated(event: ChatMemberUpdated):

    if event.chat.type not in ("group", "supergroup"):
        return

    try:
        await sync_member(event.chat.id, event.new_chat_member.user)

    except Exception as error:
        print("Ошибка регистрации участника:", error)


@dp.message(F.chat.type.in_(["group", "supergroup"]))
async def group_message(message: Message):

    if not message.from_user or message.from_user.is_bot:
        return

    try:
        await sync_member(message.chat.id, message.from_user)
    except Exception:
        pass


@dp.message(Command("setup"))
async def setup_group(message: Message):

    if message.chat.type not in ("group", "supergroup"):
        return

    try:
        await register_group(message.chat)

        await message.answer(
            "TELEGRAMM CHAT успешно настроен!\n"
            "Владелец группы определён автоматически."
        )

    except Exception as error:
        await message.answer(
            "Не удалось настроить группу.\n"
            "Убедитесь, что бот является администратором."
        )

        print(error)


# ============================================================
# TELEGRAM AUTHENTICATION
# ============================================================

def validate_init_data(init_data):

    if not init_data:
        raise web.HTTPUnauthorized(
            text="Откройте приложение через Telegram"
        )

    values = dict(
        parse_qsl(init_data, keep_blank_values=True)
    )

    received_hash = values.pop("hash", None)

    if not received_hash:
        raise web.HTTPUnauthorized(
            text="Нет подписи Telegram"
        )

    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError:
        raise web.HTTPUnauthorized(
            text="Неверная дата авторизации"
        )

    if abs(time.time() - auth_date) > 86400:
        raise web.HTTPUnauthorized(
            text="Данные авторизации устарели"
        )

    data_check = "\n".join(
        f"{key}={value}"
        for key, value in sorted(values.items())
    )

    secret = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256
    ).digest()

    expected = hmac.new(
        secret,
        data_check.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise web.HTTPUnauthorized(
            text="Ошибка авторизации Telegram"
        )

    try:
        user = json.loads(values.get("user", "{}"))
    except json.JSONDecodeError:
        raise web.HTTPUnauthorized(
            text="Ошибка данных пользователя"
        )

    if not user.get("id"):
        raise web.HTTPUnauthorized(
            text="Пользователь не найден"
        )

    return user


def get_identity(request):
    init_data = request.headers.get(
        "X-Telegram-Init-Data",
        ""
    )

    user = validate_init_data(init_data)

    save_user_dict(user)

    return int(user["id"])


# ============================================================
# GROUP ACCESS
# ============================================================

async def get_group_context(request):

    uid = get_identity(request)

    try:
        chat_id = int(
            request.headers.get("X-Chat-Id", "0")
        )
    except ValueError:
        raise web.HTTPBadRequest(
            text="Неверный ID группы"
        )

    with db() as conn:
        group = conn.execute("""
            SELECT * FROM chats WHERE id=?
        """, (chat_id,)).fetchone()

        if not group:
            raise web.HTTPNotFound(
                text="Группа не зарегистрирована"
            )

    # Проверяем, что пользователь всё ещё состоит в группе.
    try:
        member = await bot.get_chat_member(chat_id, uid)

        if member.status in ("left", "kicked"):
            raise web.HTTPForbidden(
                text="Вы не состоите в этой группе"
            )

    except web.HTTPException:
        raise

    except Exception:
        raise web.HTTPForbidden(
            text="Не удалось проверить участие в группе"
        )

    return uid, chat_id


# ============================================================
# PERMISSIONS
# ============================================================

def get_permissions(conn, chat_id, uid):

    rows = conn.execute("""
        SELECT r.permissions
        FROM roles r
        JOIN user_roles ur ON ur.role_id=r.id
        WHERE ur.chat_id=? AND ur.user_id=?
    """, (chat_id, uid)).fetchall()

    permissions = set()

    for row in rows:
        permissions.update(
            json.loads(row["permissions"])
        )

    return permissions


def has_permission(conn, chat_id, uid, permission):

    group = conn.execute("""
        SELECT owner_id FROM chats WHERE id=?
    """, (chat_id,)).fetchone()

    if group and group["owner_id"] == uid:
        return True

    permissions = get_permissions(
        conn,
        chat_id,
        uid
    )

    return (
        "*" in permissions
        or permission in permissions
    )


def require_permission(conn, chat_id, uid, permission):

    if not has_permission(
        conn,
        chat_id,
        uid,
        permission
    ):
        raise web.HTTPForbidden(
            text="Недостаточно прав"
        )


def highest_position(conn, chat_id, uid):

    row = conn.execute("""
        SELECT MAX(r.position) AS position
        FROM roles r
        JOIN user_roles ur ON ur.role_id=r.id
        WHERE ur.chat_id=? AND ur.user_id=?
    """, (chat_id, uid)).fetchone()

    return row["position"] or 0


def get_role(conn, chat_id, role_id):

    role = conn.execute("""
        SELECT * FROM roles
        WHERE chat_id=? AND id=?
    """, (chat_id, role_id)).fetchone()

    if not role:
        raise web.HTTPNotFound(
            text="Роль не найдена"
        )

    return role


def check_role_hierarchy(conn, chat_id, uid, role):

    group = conn.execute("""
        SELECT owner_id FROM chats WHERE id=?
    """, (chat_id,)).fetchone()

    if group["owner_id"] == uid:
        return

    if role["protected"]:
        raise web.HTTPForbidden(
            text="Стартовые роли может менять только владелец"
        )

    if role["position"] >= highest_position(
        conn,
        chat_id,
        uid
    ):
        raise web.HTTPForbidden(
            text="Недостаточный уровень иерархии"
        )


def serialize_role(role):

    return {
        "id": role["id"],
        "name": role["name"],
        "color": role["color"],
        "position": role["position"],
        "permissions": json.loads(role["permissions"]),
        "protected": bool(role["protected"])
    }


# ============================================================
# GROUP LIST
# ============================================================

async def api_groups(request):

    uid = get_identity(request)

    with db() as conn:

        groups = conn.execute("""
            SELECT c.id, c.title
            FROM chats c
            JOIN members m ON m.chat_id=c.id
            WHERE m.user_id=?
            ORDER BY c.title
        """, (uid,)).fetchall()

    return web.json_response([
        {
            "id": group["id"],
            "title": group["title"]
        }
        for group in groups
    ])


# ============================================================
# GROUP STATE
# ============================================================

async def api_state(request):

    uid, chat_id = await get_group_context(request)

    with db() as conn:

        group = conn.execute("""
            SELECT * FROM chats WHERE id=?
        """, (chat_id,)).fetchone()

        roles = conn.execute("""
            SELECT * FROM roles
            WHERE chat_id=?
            ORDER BY position DESC, id
        """, (chat_id,)).fetchall()

        users = conn.execute("""
            SELECT u.id, u.username, u.first_name
            FROM users u
            JOIN members m ON m.user_id=u.id
            WHERE m.chat_id=?
            ORDER BY u.first_name
        """, (chat_id,)).fetchall()

        memberships = {}

        for row in conn.execute("""
            SELECT user_id, role_id
            FROM user_roles
            WHERE chat_id=?
        """, (chat_id,)):
            memberships.setdefault(
                str(row["user_id"]), []
            ).append(row["role_id"])

        messages = conn.execute("""
            SELECT
                m.id,
                m.user_id,
                m.text,
                m.created_at,
                u.first_name
            FROM messages m
            LEFT JOIN users u ON u.id=m.user_id
            WHERE m.chat_id=?
            ORDER BY m.id DESC
            LIMIT 100
        """, (chat_id,)).fetchall()

        permissions = get_permissions(
            conn,
            chat_id,
            uid
        )

        return web.json_response({

            "group": {
                "id": group["id"],
                "title": group["title"],
                "owner_id": group["owner_id"]
            },

            "me": {
                "id": uid,
                "is_owner": group["owner_id"] == uid,
                "permissions": sorted(permissions),
                "highest_position": highest_position(
                    conn,
                    chat_id,
                    uid
                )
            },

            "roles": [
                serialize_role(role)
                for role in roles
            ],

            "users": [
                {
                    "id": user["id"],
                    "username": user["username"],
                    "first_name": user["first_name"],
                    "role_ids": memberships.get(
                        str(user["id"]), []
                    )
                }
                for user in users
            ],

            "messages": [
                {
                    "id": message["id"],
                    "user_id": message["user_id"],
                    "name": message["first_name"] or "Пользователь",
                    "text": message["text"],
                    "created_at": message["created_at"]
                }
                for message in reversed(messages)
            ]

        })


# ============================================================
# MESSAGE API
# ============================================================

def extract_mentions(text):

    return re.findall(
        r"@\[([^\]]+)\]",
        text
    )


async def api_send_message(request):

    uid, chat_id = await get_group_context(request)

    data = await request.json()

    text = str(data.get("text", "")).strip()

    if not text:
        raise web.HTTPBadRequest(
            text="Пустое сообщение"
        )

    if len(text) > 2000:
        raise web.HTTPBadRequest(
            text="Сообщение слишком длинное"
        )

    mentions = extract_mentions(text)

    with db() as conn:

        if mentions:
            require_permission(
                conn,
                chat_id,
                uid,
                "mention_roles"
            )

        user = conn.execute("""
            SELECT first_name FROM users WHERE id=?
        """, (uid,)).fetchone()

        created_at = datetime.now(
            timezone.utc
        ).isoformat()

        cursor = conn.execute("""
            INSERT INTO messages
            (chat_id, user_id, text, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            chat_id,
            uid,
            text,
            created_at
        ))

        message_id = cursor.lastrowid

        targets = set()

        for role_name in mentions:

            role = conn.execute("""
                SELECT id FROM roles
                WHERE chat_id=? AND lower(name)=lower(?)
            """, (chat_id, role_name)).fetchone()

            if not role:
                continue

            members = conn.execute("""
                SELECT user_id FROM user_roles
                WHERE chat_id=? AND role_id=?
            """, (chat_id, role["id"])).fetchall()

            for member in members:
                if member["user_id"] != uid:
                    targets.add(member["user_id"])

    for target_id in targets:
        try:
            await bot.send_message(
                target_id,
                f"🔔 Вас упомянули в группе TELEGRAMM CHAT:\n\n{text}"
            )
        except Exception:
            pass

    return web.json_response({
        "id": message_id,
        "user_id": uid,
        "name": user["first_name"],
        "text": text,
        "created_at": created_at
    })


# ============================================================
# ROLE DATA VALIDATION
# ============================================================

def validate_role_data(data):

    name = str(data.get("name", "")).strip()
    color = str(data.get("color", "#5865f2"))
    permissions = data.get("permissions", [])

    try:
        position = int(data.get("position", 1))
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(
            text="Неверная позиция роли"
        )

    if len(name) < 2 or len(name) > 32:
        raise web.HTTPBadRequest(
            text="Название должно содержать от 2 до 32 символов"
        )

    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise web.HTTPBadRequest(
            text="Неверный цвет"
        )

    if not 1 <= position <= 8999:
        raise web.HTTPBadRequest(
            text="Неверная позиция"
        )

    if not isinstance(permissions, list):
        raise web.HTTPBadRequest(
            text="Неверный список разрешений"
        )

    if not set(permissions).issubset(PERMISSIONS):
        raise web.HTTPBadRequest(
            text="Неизвестное разрешение"
        )

    return name, color, position, permissions


# ============================================================
# CREATE ROLE
# ============================================================

async def api_create_role(request):

    uid, chat_id = await get_group_context(request)

    data = await request.json()

    name, color, position, permissions = validate_role_data(data)

    with db() as conn:

        require_permission(
            conn,
            chat_id,
            uid,
            "manage_roles"
        )

        if position >= highest_position(conn, chat_id, uid):
            raise web.HTTPForbidden(
                text="Нельзя создать роль на своём или более высоком уровне"
            )

        if not set(permissions).issubset(
            get_permissions(conn, chat_id, uid)
        ):
            raise web.HTTPForbidden(
                text="Нельзя выдавать права, которых у вас нет"
            )

        try:

            conn.execute("""
                INSERT INTO roles
                (chat_id, name, color, position, permissions)
                VALUES (?, ?, ?, ?, ?)
            """, (
                chat_id,
                name,
                color,
                position,
                json.dumps(permissions)
            ))

        except sqlite3.IntegrityError:
            raise web.HTTPConflict(
                text="Роль с таким названием уже существует"
            )

    return await api_state(request)


# ============================================================
# EDIT ROLE
# ============================================================

async def api_edit_role(request):

    uid, chat_id = await get_group_context(request)

    role_id = int(request.match_info["role_id"])

    data = await request.json()

    name, color, position, permissions = validate_role_data(data)

    with db() as conn:

        require_permission(
            conn,
            chat_id,
            uid,
            "manage_roles"
        )

        role = get_role(conn, chat_id, role_id)

        check_role_hierarchy(
            conn,
            chat_id,
            uid,
            role
        )

        if position >= highest_position(conn, chat_id, uid):
            raise web.HTTPForbidden(
                text="Нельзя переместить роль выше своего уровня"
            )

        if not set(permissions).issubset(
            get_permissions(conn, chat_id, uid)
        ):
            raise web.HTTPForbidden(
                text="Нельзя выдавать права, которых у вас нет"
            )

        try:

            conn.execute("""
                UPDATE roles
                SET name=?, color=?, position=?, permissions=?
                WHERE chat_id=? AND id=?
            """, (
                name,
                color,
                position,
                json.dumps(permissions),
                chat_id,
                role_id
            ))

        except sqlite3.IntegrityError:
            raise web.HTTPConflict(
                text="Роль с таким названием уже существует"
            )

    return await api_state(request)


# ============================================================
# DELETE ROLE
# ============================================================

async def api_delete_role(request):

    uid, chat_id = await get_group_context(request)

    role_id = int(request.match_info["role_id"])

    with db() as conn:

        require_permission(
            conn,
            chat_id,
            uid,
            "manage_roles"
        )

        role = get_role(conn, chat_id, role_id)

        check_role_hierarchy(
            conn,
            chat_id,
            uid,
            role
        )

        if role["protected"]:
            raise web.HTTPForbidden(
                text="Стартовые роли нельзя удалить"
            )

        conn.execute("""
            DELETE FROM roles
            WHERE chat_id=? AND id=?
        """, (chat_id, role_id))

    return await api_state(request)


# ============================================================
# ASSIGN ROLE
# ============================================================

async def api_assign_role(request):

    uid, chat_id = await get_group_context(request)

    data = await request.json()

    try:
        target_id = int(data["user_id"])
        role_id = int(data["role_id"])
    except (KeyError, ValueError, TypeError):
        raise web.HTTPBadRequest(
            text="Неверные данные"
        )

    with db() as conn:

        require_permission(
            conn,
            chat_id,
            uid,
            "manage_members"
        )

        role = get_role(conn, chat_id, role_id)

        check_role_hierarchy(
            conn,
            chat_id,
            uid,
            role
        )

        target = conn.execute("""
            SELECT user_id FROM members
            WHERE chat_id=? AND user_id=?
        """, (chat_id, target_id)).fetchone()

        if not target:
            raise web.HTTPNotFound(
                text="Участник не найден"
            )

        group = conn.execute("""
            SELECT owner_id FROM chats WHERE id=?
        """, (chat_id,)).fetchone()

        if role["name"] == "Владелец":
            raise web.HTTPForbidden(
                text="Роль владельца нельзя выдавать вручную"
            )

        if target_id == group["owner_id"]:
            raise web.HTTPForbidden(
                text="Нельзя менять роли владельца"
            )

        if highest_position(conn, chat_id, target_id) >= highest_position(
            conn,
            chat_id,
            uid
        ):
            raise web.HTTPForbidden(
                text="Недостаточный уровень доступа"
            )

        conn.execute("""
            INSERT OR IGNORE INTO user_roles
            (chat_id, user_id, role_id)
            VALUES (?, ?, ?)
        """, (
            chat_id,
            target_id,
            role_id
        ))

    return await api_state(request)


# ============================================================
# REMOVE ROLE
# ============================================================

async def api_remove_role(request):

    uid, chat_id = await get_group_context(request)

    try:
        target_id = int(request.query["user_id"])
        role_id = int(request.query["role_id"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(
            text="Неверные параметры"
        )

    with db() as conn:

        require_permission(
            conn,
            chat_id,
            uid,
            "manage_members"
        )

        role = get_role(conn, chat_id, role_id)

        check_role_hierarchy(
            conn,
            chat_id,
            uid,
            role
        )

        if role["protected"]:
            raise web.HTTPForbidden(
                text="Стартовые роли нельзя снимать"
            )

        group = conn.execute("""
            SELECT owner_id FROM chats WHERE id=?
        """, (chat_id,)).fetchone()

        if target_id == group["owner_id"]:
            raise web.HTTPForbidden(
                text="Нельзя менять роли владельца"
            )

        if highest_position(conn, chat_id, target_id) >= highest_position(
            conn,
            chat_id,
            uid
        ):
            raise web.HTTPForbidden(
                text="Недостаточный уровень доступа"
            )

        conn.execute("""
            DELETE FROM user_roles
            WHERE chat_id=? AND user_id=? AND role_id=?
        """, (
            chat_id,
            target_id,
            role_id
        ))

    return await api_state(request)


# ============================================================
# MINI APP INTERFACE
# ============================================================

HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>TELEGRAMM CHAT</title>

<script src="https://telegram.org/js/telegram-web-app.js"></script>

<style>

:root {
    --bg:#313338;
    --panel:#2b2d31;
    --side:#1e1f22;
    --text:#f2f3f5;
    --muted:#949ba4;
    --accent:#5865f2;
}

* {
    box-sizing:border-box;
}

body {
    margin:0;
    background:var(--bg);
    color:var(--text);
    font-family:Arial,sans-serif;
}

.app {
    height:100dvh;
    display:grid;
    grid-template-columns:230px 1fr 210px;
    overflow:hidden;
}

.left {
    background:var(--side);
    padding:14px;
    overflow:auto;
}

.brand {
    font-weight:bold;
    font-size:17px;
    margin-bottom:20px;
}

.section {
    color:var(--muted);
    font-size:11px;
    font-weight:bold;
    margin:18px 0 8px;
}

.center {
    display:flex;
    flex-direction:column;
    min-width:0;
}

.top {
    height:52px;
    display:flex;
    align-items:center;
    padding:0 18px;
    border-bottom:1px solid #25262a;
    font-weight:bold;
}

.messages {
    flex:1;
    overflow:auto;
    padding:18px;
}

.msg {
    display:flex;
    gap:12px;
    margin-bottom:18px;
}

.avatar {
    width:36px;
    height:36px;
    border-radius:50%;
    background:#5865f2;
    display:grid;
    place-items:center;
    flex-shrink:0;
    font-weight:bold;
}

.name {
    font-weight:bold;
}

.time {
    color:var(--muted);
    font-size:11px;
    margin-left:8px;
}

.content {
    margin-top:5px;
    white-space:pre-wrap;
    overflow-wrap:anywhere;
}

.composer {
    padding:12px 18px;
}

.composebox {
    display:flex;
    gap:8px;
    padding:8px;
    background:#383a40;
    border-radius:8px;
}

.composebox input {
    flex:1;
    min-width:0;
    background:transparent;
    color:white;
    border:0;
    outline:0;
    padding:8px;
}

.btn {
    background:var(--accent);
    color:white;
    border:0;
    border-radius:7px;
    padding:9px 13px;
    cursor:pointer;
}

.right {
    background:var(--panel);
    padding:14px;
    overflow:auto;
}

.member {
    padding:8px 4px;
}

.member small {
    display:block;
    margin-top:4px;
}

.tool {
    background:#404249;
    color:white;
    border:0;
    border-radius:6px;
    padding:8px;
    margin:3px;
    cursor:pointer;
}

.modal {
    display:none;
    position:fixed;
    inset:0;
    background:#0009;
    align-items:center;
    justify-content:center;
    padding:15px;
}

.dialog {
    width:min(440px,100%);
    max-height:85vh;
    overflow:auto;
    background:#2b2d31;
    border-radius:12px;
    padding:18px;
}

.dialog input:not([type=checkbox]):not([type=color]),
.dialog select {
    width:100%;
    padding:9px;
    margin:6px 0 12px;
    color:white;
    background:#1e1f22;
    border:1px solid #555;
    border-radius:6px;
}

.dialog input[type=color] {
    display:block;
    width:100%;
    height:40px;
}

.perm {
    display:flex;
    gap:8px;
    margin:10px 0;
}

.group-option {
    display:block;
    width:100%;
    padding:12px;
    margin:8px 0;
    text-align:left;
    background:#404249;
    border:0;
    border-radius:8px;
    color:white;
    cursor:pointer;
}

@media(max-width:760px) {
    .app {
        grid-template-columns:66px 1fr;
    }

    .left {
        padding:12px 7px;
    }

    .brand {
        font-size:0;
    }

    .brand:after {
        content:"TC";
        font-size:17px;
    }

    .right {
        display:none;
    }
}

</style>
</head>

<body>

<div class="app">

    <aside class="left">

        <div class="brand">TELEGRAMM CHAT</div>

        <div class="section">МОИ ГРУППЫ</div>

        <div id="groups"></div>

        <div class="section">УПРАВЛЕНИЕ</div>

        <button class="tool" onclick="openRole()">
            ＋ Роль
        </button>

        <button class="tool" onclick="openMembers()">
            Участники
        </button>

        <div class="section">ВАШИ РОЛИ</div>

        <div id="myroles"></div>

    </aside>


    <main class="center">

        <header class="top" id="groupTitle">
            Выберите группу
        </header>

        <section class="messages" id="messages"></section>

        <footer class="composer">

            <div class="composebox">

                <input
                    id="message"
                    placeholder="Написать сообщение..."
                    maxlength="2000"
                >

                <button class="btn" onclick="sendMessage()">
                    ➤
                </button>

            </div>

        </footer>

    </main>


    <aside class="right">

        <div class="section">
            УЧАСТНИКИ — <span id="count">0</span>
        </div>

        <div id="members"></div>

    </aside>

</div>


<div class="modal" id="modal">
    <div class="dialog" id="dialog"></div>
</div>


<script>

const tg = window.Telegram.WebApp;

tg.ready();
tg.expand();

let groups = [];
let state = null;
let uid = null;
let selectedGroup = null;

const $ = id => document.getElementById(id);


function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&":"&amp;",
        "<":"&lt;",
        ">":"&gt;",
        '"':"&quot;",
        "'":"&#39;"
    })[char]);
}


function headers() {
    return {
        "Content-Type":"application/json",
        "X-Telegram-Init-Data":tg.initData,
        "X-Chat-Id":String(selectedGroup)
    };
}


async function api(path, options={}) {

    const response = await fetch(path, {
        ...options,
        headers:{
            ...headers(),
            ...(options.headers || {})
        }
    });

    const data = await response.json();

    if (!response.ok) {
        throw new Error(data.detail || "Ошибка");
    }

    return data;
}


function has(permission) {
    return state.me.is_owner ||
        state.me.permissions.includes("*") ||
        state.me.permissions.includes(permission);
}


function getRoles(ids) {
    return ids
        .map(id => state.roles.find(role => role.id === id))
        .filter(Boolean);
}


// ============================================================
// GROUP SELECTOR
// ============================================================

async function loadGroups() {

    const response = await fetch("/api/groups", {
        headers:{
            "X-Telegram-Init-Data":tg.initData
        }
    });

    groups = await response.json();

    if (!groups.length) {
        $("groups").innerHTML =
            "<p>Вы пока не состоите ни в одной подключённой группе.</p>";
        return;
    }

    $("groups").innerHTML = groups.map(group => `
        <button
            class="group-option"
            onclick="selectGroup(${group.id})"
        >
            ${esc(group.title)}
        </button>
    `).join("");

    if (groups.length === 1) {
        selectGroup(groups[0].id);
    }
}


async function selectGroup(id) {

    selectedGroup = id;

    await refresh();
}


// ============================================================
// RENDER
// ============================================================

function render() {

    if (!state) return;

    $("groupTitle").textContent =
        "#　" + state.group.title;

    $("count").textContent =
        state.users.length;

    const me = state.users.find(user => user.id === uid);

    $("myroles").innerHTML =
        getRoles(me?.role_ids || []).map(role => `
            <div style="padding:5px;color:${role.color}">
                ● ${esc(role.name)}
            </div>
        `).join("");


    $("members").innerHTML = state.users.map(user => {

        return `
            <div class="member">

                <b>${esc(user.first_name)}</b>

                ${getRoles(user.role_ids).map(role => `
                    <small style="color:${role.color}">
                        ● ${esc(role.name)}
                    </small>
                `).join("")}

            </div>
        `;

    }).join("");


    $("messages").innerHTML = state.messages.map(message => `

        <div class="msg">

            <div class="avatar">
                ${esc(message.name.slice(0,1).toUpperCase())}
            </div>

            <div>

                <div>

                    <span class="name">
                        ${esc(message.name)}
                    </span>

                    <span class="time">
                        ${new Date(message.created_at).toLocaleTimeString([], {
                            hour:"2-digit",
                            minute:"2-digit"
                        })}
                    </span>

                </div>

                <div class="content">
                    ${formatMessage(message.text)}
                </div>

            </div>

        </div>

    `).join("");

    $("messages").scrollTop =
        $("messages").scrollHeight;
}


function formatMessage(text) {

    return esc(text).replace(
        /@\[([^\]]+)\]/g,
        (_, name) => {

            const role = state.roles.find(
                item => item.name.toLowerCase() === name.toLowerCase()
            );

            return `
                <b style="color:${role?.color || "#aeb5ff"}">
                    @${esc(name)}
                </b>
            `;
        }
    );
}


// ============================================================
// MESSAGES
// ============================================================

async function sendMessage() {

    if (!selectedGroup) {
        return alert("Сначала выберите группу");
    }

    const input = $("message");
    const text = input.value.trim();

    if (!text) return;

    try {

        await api("/api/messages", {
            method:"POST",
            body:JSON.stringify({text})
        });

        input.value = "";

        await refresh();

    } catch(error) {
        alert(error.message);
    }
}


$("message").addEventListener("keydown", event => {

    if (event.key === "Enter") {
        event.preventDefault();
        sendMessage();
    }

});


// ============================================================
// MODALS
// ============================================================

function show(html) {
    $("dialog").innerHTML = html;
    $("modal").style.display = "flex";
}

function closeModal() {
    $("modal").style.display = "none";
}


// ============================================================
// ROLE MANAGEMENT
// ============================================================

function openRole(role=null) {

    if (!has("manage_roles")) {
        return alert("Недостаточно прав");
    }

    const permissions = [
        ["manage_roles","Управление ролями"],
        ["manage_members","Управление участниками"],
        ["manage_permissions","Настройка прав"],
        ["mention_roles","Упоминание ролей"]
    ];

    show(`

        <h3>${role ? "Изменить роль" : "Создать роль"}</h3>

        <label>Название</label>

        <input
            id="rname"
            maxlength="32"
            value="${esc(role?.name || "")}"
        >

        <label>Цвет</label>

        <input
            id="rcolor"
            type="color"
            value="${role?.color || "#5865f2"}"
        >

        <label>Позиция в иерархии</label>

        <input
            id="rposition"
            type="number"
            min="1"
            max="8999"
            value="${role?.position || 10}"
        >

        <div class="section">ПРАВА</div>

        ${permissions.map(([permission,label]) => `

            <label class="perm">

                <input
                    type="checkbox"
                    value="${permission}"
                    ${role?.permissions?.includes(permission) ? "checked" : ""}
                    ${!has("manage_permissions") ? "disabled" : ""}
                >

                ${label}

            </label>

        `).join("")}

        <button class="btn" onclick="saveRole(${role?.id || 0})">
            Сохранить
        </button>

        <button class="tool" onclick="closeModal()">
            Отмена
        </button>

        ${role && !role.protected ? `
            <button class="tool" onclick="deleteRole(${role.id})">
                Удалить
            </button>
        ` : ""}

    `);
}


async function saveRole(id) {

    const permissions = [
        ...document.querySelectorAll(
            "#dialog input[type=checkbox]:checked"
        )
    ].map(input => input.value);

    const data = {
        name:$("rname").value.trim(),
        color:$("rcolor").value,
        position:Number($("rposition").value),
        permissions
    };

    try {

        state = await api(
            id ? `/api/roles/${id}` : "/api/roles",
            {
                method:id ? "PUT" : "POST",
                body:JSON.stringify(data)
            }
        );

        closeModal();
        render();

    } catch(error) {
        alert(error.message);
    }
}


async function deleteRole(id) {

    if (!confirm("Удалить роль?")) return;

    try {

        state = await api(`/api/roles/${id}`, {
            method:"DELETE"
        });

        closeModal();
        render();

    } catch(error) {
        alert(error.message);
    }
}


// ============================================================
// MEMBER MANAGEMENT
// ============================================================

function openMembers() {

    if (!has("manage_members")) {
        return alert("Недостаточно прав");
    }

    show(`

        <h3>Управление участниками</h3>

        ${state.users.map(user => `

            <div style="padding:10px 0;border-bottom:1px solid #444">

                <b>${esc(user.first_name)}</b>

                <div>

                    ${state.roles
                        .filter(role => role.name !== "Владелец")
                        .map(role => `

                            <button
                                class="tool"
                                style="color:${role.color}"
                                onclick="toggleRole(
                                    ${user.id},
                                    ${role.id},
                                    ${user.role_ids.includes(role.id)}
                                )"
                            >

                                ${user.role_ids.includes(role.id) ? "−" : "+"}
                                ${esc(role.name)}

                            </button>

                        `).join("")}

                </div>

            </div>

        `).join("")}

        <button class="tool" onclick="closeModal()">
            Закрыть
        </button>

    `);
}


async function toggleRole(userId, roleId, assigned) {

    try {

        if (assigned) {

            state = await api(
                `/api/roles/assign?user_id=${userId}&role_id=${roleId}`,
                {method:"DELETE"}
            );

        } else {

            state = await api("/api/roles/assign", {
                method:"POST",
                body:JSON.stringify({
                    user_id:userId,
                    role_id:roleId
                })
            });

        }

        openMembers();
        render();

    } catch(error) {
        alert(error.message);
    }
}


// ============================================================
// REFRESH
// ============================================================

async function refresh() {

    if (!selectedGroup) return;

    try {

        state = await api("/api/state");

        render();

    } catch(error) {
        console.error(error);
    }
}


// ============================================================
// START
// ============================================================

async function boot() {

    if (!tg.initData) {
        document.body.innerHTML =
            "<div style='padding:24px'>Откройте приложение через Telegram.</div>";
        return;
    }

    try {

        const response = await fetch("/api/auth", {
            method:"POST",
            headers:{
                "Content-Type":"application/json"
            },
            body:JSON.stringify({
                init_data:tg.initData
            })
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || "Ошибка авторизации");
        }

        uid = data.me.id;

        await loadGroups();

        setInterval(refresh, 3000);

    } catch(error) {

        document.body.innerHTML =
            `<div style="padding:24px">${esc(error.message)}</div>`;

    }
}

boot();

</script>

</body>
</html>
"""


# ============================================================
# WEB SERVER
# ============================================================

async def index(request):

    return web.Response(
        text=HTML,
        content_type="text/html"
    )


def create_web_app():

    app = web.Application()

    app.router.add_get("/", index)

    app.router.add_post("/api/auth", api_groups)
    app.router.add_get("/api/groups", api_groups)
    app.router.add_get("/api/state", api_state)

    app.router.add_post("/api/messages", api_send_message)

    app.router.add_post("/api/roles", api_create_role)
    app.router.add_put("/api/roles/{role_id}", api_edit_role)
    app.router.add_delete("/api/roles/{role_id}", api_delete_role)

    app.router.add_post("/api/roles/assign", api_assign_role)
    app.router.add_delete("/api/roles/assign", api_remove_role)

    return app


# ============================================================
# BOT START
# ============================================================

@dp.message(CommandStart())
async def start(message: Message):

    if message.chat.type != "private":
        await message.answer(
            "Откройте бота в личных сообщениях, чтобы пользоваться TELEGRAMM CHAT."
        )
        return

    keyboard = ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(
                text="Открыть TELEGRAMM CHAT",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        ]],
        resize_keyboard=True
    )

    await message.answer(
        "Добро пожаловать в TELEGRAMM CHAT!\n\n"
        "Выберите группу, чтобы открыть её чат и управление ролями.",
        reply_markup=keyboard
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    if BOT_TOKEN == "YOUR_BOT_TOKEN":
        raise RuntimeError("Укажите BOT_TOKEN")

    init_db()

    app = create_web_app()

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT
    )

    await site.start()

    print(f"TELEGRAMM CHAT запущен: порт {PORT}")

    try:

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )

    finally:

        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
