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

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup

from aiohttp import web


# ============================================================
# TELEGRAMM CHAT
# Python 3.11
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "123456789"))
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}

WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain.com")
DB_PATH = "telegramm_chat.sqlite3"
WEB_PORT = int(os.getenv("PORT", "8000"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

PERMISSIONS = {
    "manage_roles",
    "manage_members",
    "manage_permissions",
    "mention_roles",
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
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            color TEXT NOT NULL,
            position INTEGER NOT NULL,
            permissions TEXT NOT NULL DEFAULT '[]',
            protected INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS user_roles (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            PRIMARY KEY(user_id, role_id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)

        roles = [
            ("Владелец", "#e0b84c", 10000, ["*"], 1),
            ("Администратор", "#e05252", 9000, ADMIN_PERMISSIONS, 1),
            ("Участник", "#9aa0aa", 0, [], 1),
        ]

        for name, color, position, permissions, protected in roles:
            conn.execute("""
                INSERT OR IGNORE INTO roles
                (name, color, position, permissions, protected)
                VALUES (?, ?, ?, ?, ?)
            """, (
                name,
                color,
                position,
                json.dumps(permissions),
                protected
            ))

        owner_role = conn.execute(
            "SELECT id FROM roles WHERE name='Владелец'"
        ).fetchone()["id"]

        admin_role = conn.execute(
            "SELECT id FROM roles WHERE name='Администратор'"
        ).fetchone()["id"]

        now = datetime.now(timezone.utc).isoformat()

        conn.execute("""
            INSERT OR IGNORE INTO users
            (id, username, first_name, last_seen)
            VALUES (?, ?, ?, ?)
        """, (OWNER_ID, None, "Владелец", now))

        conn.execute("""
            INSERT OR IGNORE INTO user_roles (user_id, role_id)
            VALUES (?, ?)
        """, (OWNER_ID, owner_role))

        for admin_id in ADMIN_IDS:
            conn.execute("""
                INSERT OR IGNORE INTO users
                (id, username, first_name, last_seen)
                VALUES (?, ?, ?, ?)
            """, (admin_id, None, "Администратор", now))

            conn.execute("""
                INSERT OR IGNORE INTO user_roles (user_id, role_id)
                VALUES (?, ?)
            """, (admin_id, admin_role))


# ============================================================
# TELEGRAM AUTHENTICATION
# ============================================================

def validate_init_data(init_data: str):
    if not init_data:
        raise web.HTTPUnauthorized(text="Откройте приложение через Telegram")

    values = dict(parse_qsl(init_data, keep_blank_values=True))

    received_hash = values.pop("hash", None)

    if not received_hash:
        raise web.HTTPUnauthorized(text="Нет подписи Telegram")

    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError:
        raise web.HTTPUnauthorized(text="Неверная дата авторизации")

    if abs(time.time() - auth_date) > 86400:
        raise web.HTTPUnauthorized(text="Данные авторизации устарели")

    data_check = "\n".join(
        f"{key}={value}"
        for key, value in sorted(values.items())
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise web.HTTPUnauthorized(text="Ошибка проверки Telegram")

    try:
        user = json.loads(values.get("user", "{}"))
    except json.JSONDecodeError:
        raise web.HTTPUnauthorized(text="Ошибка данных пользователя")

    if not user.get("id"):
        raise web.HTTPUnauthorized(text="Пользователь не найден")

    return user


def get_identity(request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return validate_init_data(init_data)


# ============================================================
# USERS AND ROLES
# ============================================================

def ensure_user(user):
    uid = int(user["id"])

    with db() as conn:
        now = datetime.now(timezone.utc).isoformat()

        conn.execute("""
            INSERT INTO users (id, username, first_name, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_seen=excluded.last_seen
        """, (
            uid,
            user.get("username"),
            user.get("first_name") or "Пользователь",
            now
        ))

        if uid == OWNER_ID:
            role = conn.execute(
                "SELECT id FROM roles WHERE name='Владелец'"
            ).fetchone()

        elif uid in ADMIN_IDS:
            role = conn.execute(
                "SELECT id FROM roles WHERE name='Администратор'"
            ).fetchone()

        else:
            role = conn.execute(
                "SELECT id FROM roles WHERE name='Участник'"
            ).fetchone()

        if role:
            conn.execute("""
                INSERT OR IGNORE INTO user_roles (user_id, role_id)
                VALUES (?, ?)
            """, (uid, role["id"]))


def get_permissions(conn, uid):
    rows = conn.execute("""
        SELECT r.permissions
        FROM roles r
        JOIN user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = ?
    """, (uid,)).fetchall()

    permissions = set()

    for row in rows:
        permissions.update(json.loads(row["permissions"]))

    return permissions


def has_permission(conn, uid, permission):
    if uid == OWNER_ID:
        return True

    permissions = get_permissions(conn, uid)

    return "*" in permissions or permission in permissions


def require_permission(conn, uid, permission):
    if not has_permission(conn, uid, permission):
        raise web.HTTPForbidden(text="Недостаточно прав")


def highest_position(conn, uid):
    row = conn.execute("""
        SELECT MAX(r.position) AS position
        FROM roles r
        JOIN user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = ?
    """, (uid,)).fetchone()

    return row["position"] or 0


def get_role(conn, role_id):
    role = conn.execute(
        "SELECT * FROM roles WHERE id = ?",
        (role_id,)
    ).fetchone()

    if not role:
        raise web.HTTPNotFound(text="Роль не найдена")

    return role


def check_role_hierarchy(conn, uid, role):
    if uid == OWNER_ID:
        return

    if role["protected"]:
        raise web.HTTPForbidden(
            text="Стартовую роль может менять только владелец"
        )

    if role["position"] >= highest_position(conn, uid):
        raise web.HTTPForbidden(
            text="Нельзя управлять ролью равного или более высокого уровня"
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
# CHAT STATE
# ============================================================

def get_state(uid):
    with db() as conn:
        roles = conn.execute("""
            SELECT * FROM roles
            ORDER BY position DESC, id ASC
        """).fetchall()

        users = conn.execute("""
            SELECT id, username, first_name
            FROM users
            ORDER BY first_name
        """).fetchall()

        memberships = {}

        for row in conn.execute(
            "SELECT user_id, role_id FROM user_roles"
        ):
            memberships.setdefault(str(row["user_id"]), []).append(
                row["role_id"]
            )

        messages = conn.execute("""
            SELECT
                m.id,
                m.user_id,
                m.text,
                m.created_at,
                u.first_name,
                u.username
            FROM messages m
            LEFT JOIN users u ON u.id = m.user_id
            ORDER BY m.id DESC
            LIMIT 100
        """).fetchall()

        messages = list(reversed(messages))

        permissions = get_permissions(conn, uid)

        return {
            "me": {
                "id": uid,
                "is_owner": uid == OWNER_ID,
                "permissions": sorted(permissions),
                "highest_position": highest_position(conn, uid)
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
                    "role_ids": memberships.get(str(user["id"]), [])
                }
                for user in users
            ],

            "messages": [
                {
                    "id": message["id"],
                    "user_id": message["user_id"],
                    "name": (
                        message["first_name"]
                        or message["username"]
                        or "Пользователь"
                    ),
                    "text": message["text"],
                    "created_at": message["created_at"]
                }
                for message in messages
            ]
        }


# ============================================================
# REQUEST HELPERS
# ============================================================

async def read_json(request):
    try:
        return await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Неверный JSON")


def authenticated_user(request):
    user = get_identity(request)
    ensure_user(user)
    return int(user["id"])


# ============================================================
# AUTH API
# ============================================================

async def api_auth(request):
    data = await read_json(request)

    user = validate_init_data(data.get("init_data", ""))

    ensure_user(user)

    return web.json_response(get_state(int(user["id"])))


async def api_state(request):
    uid = authenticated_user(request)

    return web.json_response(get_state(uid))


# ============================================================
# MESSAGES API
# ============================================================

def extract_role_mentions(text):
    return re.findall(r"@\[([^\]]+)\]", text)


async def api_send_message(request):
    uid = authenticated_user(request)

    data = await read_json(request)

    text = str(data.get("text", "")).strip()

    if not text:
        raise web.HTTPBadRequest(text="Сообщение пустое")

    if len(text) > 2000:
        raise web.HTTPBadRequest(text="Сообщение слишком длинное")

    mentions = extract_role_mentions(text)

    with db() as conn:
        if mentions:
            require_permission(conn, uid, "mention_roles")

        user = conn.execute(
            "SELECT first_name FROM users WHERE id = ?",
            (uid,)
        ).fetchone()

        if not user:
            raise web.HTTPUnauthorized(text="Пользователь не найден")

        created_at = datetime.now(timezone.utc).isoformat()

        cursor = conn.execute("""
            INSERT INTO messages (user_id, text, created_at)
            VALUES (?, ?, ?)
        """, (uid, text, created_at))

        message_id = cursor.lastrowid

        targets = set()

        for role_name in mentions:
            role = conn.execute("""
                SELECT id FROM roles
                WHERE lower(name) = lower(?)
            """, (role_name,)).fetchone()

            if not role:
                continue

            members = conn.execute("""
                SELECT user_id
                FROM user_roles
                WHERE role_id = ?
            """, (role["id"],)).fetchall()

            for member in members:
                if member["user_id"] != uid:
                    targets.add(member["user_id"])

    # Telegram bot notifications for role mentions.
    for target_id in targets:
        try:
            await bot.send_message(
                target_id,
                f"🔔 Вас упомянули в TELEGRAMM CHAT:\n\n{text}"
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
# ROLE CREATION AND EDITING
# ============================================================

def validate_role_data(data):
    name = str(data.get("name", "")).strip()
    color = str(data.get("color", "#5865f2"))
    position = data.get("position", 1)
    permissions = data.get("permissions", [])

    if len(name) < 2 or len(name) > 32:
        raise web.HTTPBadRequest(text="Название роли: от 2 до 32 символов")

    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise web.HTTPBadRequest(text="Неверный цвет")

    try:
        position = int(position)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Неверная позиция роли")

    if position < 1 or position > 8999:
        raise web.HTTPBadRequest(text="Позиция должна быть от 1 до 8999")

    if not isinstance(permissions, list):
        raise web.HTTPBadRequest(text="Неверный список прав")

    if not set(permissions).issubset(PERMISSIONS):
        raise web.HTTPBadRequest(text="Неизвестное разрешение")

    return name, color, position, permissions


async def api_create_role(request):
    uid = authenticated_user(request)
    data = await read_json(request)

    name, color, position, permissions = validate_role_data(data)

    with db() as conn:
        require_permission(conn, uid, "manage_roles")

        if uid != OWNER_ID:
            if position >= highest_position(conn, uid):
                raise web.HTTPForbidden(
                    text="Нельзя создать роль на своём или более высоком уровне"
                )

            if not set(permissions).issubset(get_permissions(conn, uid)):
                raise web.HTTPForbidden(
                    text="Нельзя выдавать права, которых у вас нет"
                )

        try:
            conn.execute("""
                INSERT INTO roles
                (name, color, position, permissions)
                VALUES (?, ?, ?, ?)
            """, (
                name,
                color,
                position,
                json.dumps(permissions)
            ))
        except sqlite3.IntegrityError:
            raise web.HTTPConflict(
                text="Роль с таким названием уже существует"
            )

    return web.json_response(get_state(uid))


async def api_edit_role(request):
    uid = authenticated_user(request)
    role_id = int(request.match_info["role_id"])

    data = await read_json(request)

    name, color, position, permissions = validate_role_data(data)

    with db() as conn:
        require_permission(conn, uid, "manage_roles")

        role = get_role(conn, role_id)

        check_role_hierarchy(conn, uid, role)

        if uid != OWNER_ID:
            if position >= highest_position(conn, uid):
                raise web.HTTPForbidden(
                    text="Нельзя переместить роль на свой или более высокий уровень"
                )

            if not set(permissions).issubset(get_permissions(conn, uid)):
                raise web.HTTPForbidden(
                    text="Нельзя выдавать права, которых у вас нет"
                )

        try:
            conn.execute("""
                UPDATE roles
                SET name = ?,
                    color = ?,
                    position = ?,
                    permissions = ?
                WHERE id = ?
            """, (
                name,
                color,
                position,
                json.dumps(permissions),
                role_id
            ))
        except sqlite3.IntegrityError:
            raise web.HTTPConflict(
                text="Роль с таким названием уже существует"
            )

    return web.json_response(get_state(uid))


async def api_delete_role(request):
    uid = authenticated_user(request)
    role_id = int(request.match_info["role_id"])

    with db() as conn:
        require_permission(conn, uid, "manage_roles")

        role = get_role(conn, role_id)

        check_role_hierarchy(conn, uid, role)

        if role["protected"]:
            raise web.HTTPForbidden(
                text="Стартовые роли нельзя удалить"
            )

        conn.execute(
            "DELETE FROM roles WHERE id = ?",
            (role_id,)
        )

    return web.json_response(get_state(uid))


# ============================================================
# ROLE ASSIGNMENT
# ============================================================

async def api_assign_role(request):
    uid = authenticated_user(request)
    data = await read_json(request)

    try:
        target_id = int(data["user_id"])
        role_id = int(data["role_id"])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPBadRequest(text="Неверные данные")

    with db() as conn:
        require_permission(conn, uid, "manage_members")

        role = get_role(conn, role_id)

        check_role_hierarchy(conn, uid, role)

        target = conn.execute(
            "SELECT id FROM users WHERE id = ?",
            (target_id,)
        ).fetchone()

        if not target:
            raise web.HTTPNotFound(text="Участник не найден")

        if role["name"] == "Владелец" and target_id != OWNER_ID:
            raise web.HTTPForbidden(
                text="Роль владельца нельзя выдавать другим участникам"
            )

        if target_id == OWNER_ID and uid != OWNER_ID:
            raise web.HTTPForbidden(
                text="Нельзя менять роли владельца"
            )

        if uid != OWNER_ID:
            if highest_position(conn, target_id) >= highest_position(conn, uid):
                raise web.HTTPForbidden(
                    text="Нельзя менять роли участника равного или более высокого уровня"
                )

        conn.execute("""
            INSERT OR IGNORE INTO user_roles (user_id, role_id)
            VALUES (?, ?)
        """, (target_id, role_id))

    return web.json_response(get_state(uid))


async def api_remove_role(request):
    uid = authenticated_user(request)

    try:
        target_id = int(request.query["user_id"])
        role_id = int(request.query["role_id"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="Неверные параметры")

    with db() as conn:
        require_permission(conn, uid, "manage_members")

        role = get_role(conn, role_id)

        check_role_hierarchy(conn, uid, role)

        if role["name"] == "Владелец":
            raise web.HTTPForbidden(
                text="Роль владельца нельзя снять"
            )

        if role["protected"] and uid != OWNER_ID:
            raise web.HTTPForbidden(
                text="Стартовые роли может снимать только владелец"
            )

        if target_id == OWNER_ID and uid != OWNER_ID:
            raise web.HTTPForbidden(
                text="Нельзя менять роли владельца"
            )

        if uid != OWNER_ID:
            if highest_position(conn, target_id) >= highest_position(conn, uid):
                raise web.HTTPForbidden(
                    text="Недостаточный уровень доступа"
                )

        conn.execute("""
            DELETE FROM user_roles
            WHERE user_id = ? AND role_id = ?
        """, (target_id, role_id))

    return web.json_response(get_state(uid))


# ============================================================
# TELEGRAM BOT
# ============================================================

@dp.message(CommandStart())
async def start(message: Message):
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Открыть TELEGRAMM CHAT",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            ]
        ],
        resize_keyboard=True
    )

    await message.answer(
        "Добро пожаловать в TELEGRAMM CHAT!\n\n"
        "Нажми кнопку ниже, чтобы открыть чат.",
        reply_markup=keyboard
    )


# ============================================================
# WEB INTERFACE
# ============================================================

HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">

<title>TELEGRAMM CHAT</title>

<script src="https://telegram.org/js/telegram-web-app.js"></script>

<style>
:root {
    --bg: #313338;
    --panel: #2b2d31;
    --side: #1e1f22;
    --text: #f2f3f5;
    --muted: #949ba4;
    --accent: #5865f2;
}

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: Arial, sans-serif;
}

.app {
    height: 100dvh;
    display: grid;
    grid-template-columns: 230px 1fr 210px;
    overflow: hidden;
}

.left {
    background: var(--side);
    padding: 14px;
    overflow-y: auto;
}

.brand {
    font-size: 17px;
    font-weight: 800;
    margin: 5px 0 25px;
}

.section {
    color: var(--muted);
    font-size: 11px;
    font-weight: bold;
    margin: 18px 0 8px;
}

.channel {
    background: #3f4147;
    padding: 10px;
    border-radius: 6px;
}

.center {
    display: flex;
    flex-direction: column;
    min-width: 0;
}

.top {
    height: 52px;
    display: flex;
    align-items: center;
    padding: 0 18px;
    font-weight: bold;
    border-bottom: 1px solid #25262a;
}

.messages {
    flex: 1;
    overflow-y: auto;
    padding: 18px;
}

.msg {
    display: flex;
    gap: 12px;
    margin-bottom: 18px;
}

.avatar {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    background: #5865f2;
    display: grid;
    place-items: center;
    font-weight: bold;
    flex-shrink: 0;
}

.name {
    font-weight: bold;
}

.time {
    color: var(--muted);
    font-size: 11px;
    margin-left: 8px;
}

.content {
    margin-top: 5px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    line-height: 1.45;
}

.composer {
    padding: 12px 18px 18px;
}

.composebox {
    display: flex;
    gap: 8px;
    background: #383a40;
    border-radius: 9px;
    padding: 8px;
}

.composebox input {
    flex: 1;
    min-width: 0;
    background: transparent;
    border: 0;
    color: white;
    outline: none;
    padding: 7px;
}

.btn {
    background: var(--accent);
    color: white;
    border: 0;
    border-radius: 7px;
    padding: 8px 13px;
    cursor: pointer;
    font-weight: bold;
}

.right {
    background: var(--panel);
    padding: 14px;
    overflow-y: auto;
}

.member {
    padding: 8px 5px;
}

.member small {
    display: block;
    margin-top: 4px;
}

.role {
    font-size: 11px;
    font-weight: bold;
}

.tools {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
}

.tool {
    background: #404249;
    color: white;
    border: 0;
    border-radius: 6px;
    padding: 8px;
    cursor: pointer;
}

.modal {
    position: fixed;
    inset: 0;
    background: #0009;
    display: none;
    align-items: center;
    justify-content: center;
    padding: 18px;
}

.dialog {
    background: #2b2d31;
    border-radius: 12px;
    padding: 18px;
    width: min(440px, 100%);
    max-height: 85vh;
    overflow-y: auto;
}

.dialog input:not([type="checkbox"]) {
    width: 100%;
    background: #1e1f22;
    color: white;
    border: 1px solid #555;
    border-radius: 6px;
    padding: 9px;
    margin: 6px 0 12px;
}

.perm {
    display: flex;
    gap: 8px;
    align-items: center;
    margin: 10px 0;
}

@media(max-width:760px) {
    .app {
        grid-template-columns: 66px 1fr;
    }

    .left {
        padding: 12px 7px;
    }

    .brand {
        font-size: 0;
    }

    .brand:after {
        content: "TC";
        font-size: 17px;
    }

    .right {
        display: none;
    }
}
</style>
</head>

<body>

<div class="app">

    <aside class="left">

        <div class="brand">TELEGRAMM CHAT</div>

        <div class="section">ТЕКСТОВЫЕ КАНАЛЫ</div>

        <div class="channel">
            #　общий-чат
        </div>

        <div class="section">УПРАВЛЕНИЕ</div>

        <div class="tools">
            <button class="tool" onclick="openRole()">
                ＋ Роль
            </button>

            <button class="tool" onclick="openMembers()">
                Участники
            </button>
        </div>

        <div class="section">ВАШИ РОЛИ</div>

        <div id="myroles"></div>

    </aside>


    <main class="center">

        <header class="top">
            #　общий-чат
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

let state = null;
let uid = null;

const $ = id => document.getElementById(id);

function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    })[char]);
}

function headers() {
    return {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": tg.initData
    };
}

async function api(path, options = {}) {

    const response = await fetch(path, {
        ...options,
        headers: {
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

function hasPermission(permission) {

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
// RENDER
// ============================================================

function render() {

    const me = state.users.find(user => user.id === uid);

    $("count").textContent = state.users.length;

    $("myroles").innerHTML = getRoles(me?.role_ids || [])
        .map(role => `
            <div style="padding:5px;color:${role.color}">
                ● ${escapeHTML(role.name)}
            </div>
        `).join("");


    $("members").innerHTML = state.users.map(user => {

        const roles = getRoles(user.role_ids);

        return `
            <div class="member">

                <b>${escapeHTML(user.first_name)}</b>

                ${roles.map(role => `
                    <small style="color:${role.color}">
                        ● ${escapeHTML(role.name)}
                    </small>
                `).join("")}

            </div>
        `;

    }).join("");


    $("messages").innerHTML = state.messages.map(message => {

        return `
            <div class="msg">

                <div class="avatar">
                    ${escapeHTML(message.name.slice(0, 1).toUpperCase())}
                </div>

                <div>

                    <div>
                        <span class="name">
                            ${escapeHTML(message.name)}
                        </span>

                        <span class="time">
                            ${new Date(message.created_at).toLocaleTimeString([], {
                                hour: "2-digit",
                                minute: "2-digit"
                            })}
                        </span>
                    </div>

                    <div class="content">
                        ${formatMessage(message.text)}
                    </div>

                </div>

            </div>
        `;

    }).join("");

    $("messages").scrollTop = $("messages").scrollHeight;
}


function formatMessage(text) {

    let result = escapeHTML(text);

    result = result.replace(/@\[([^\]]+)\]/g, (_, name) => {

        const role = state.roles.find(
            item => item.name.toLowerCase() === name.toLowerCase()
        );

        const color = role?.color || "#aeb5ff";

        return `
            <b style="color:${color}">
                @${escapeHTML(name)}
            </b>
        `;
    });

    return result;
}


// ============================================================
// MESSAGES
// ============================================================

async function sendMessage() {

    const input = $("message");
    const text = input.value.trim();

    if (!text) return;

    try {

        await api("/api/messages", {
            method: "POST",
            body: JSON.stringify({ text })
        });

        input.value = "";

        await refresh();

    } catch (error) {
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

function closeModal() {
    $("modal").style.display = "none";
}

function showModal(html) {
    $("dialog").innerHTML = html;
    $("modal").style.display = "flex";
}


// ============================================================
// ROLE MANAGEMENT
// ============================================================

function openRole(role = null) {

    if (!hasPermission("manage_roles")) {
        return alert("Недостаточно прав");
    }

    const permissions = [
        ["manage_roles", "Управление ролями"],
        ["manage_members", "Управление участниками"],
        ["manage_permissions", "Настройка прав"],
        ["mention_roles", "Упоминание ролей"]
    ];

    showModal(`

        <h3>${role ? "Изменить роль" : "Создать роль"}</h3>

        <label>Название</label>

        <input
            id="roleName"
            maxlength="32"
            value="${escapeHTML(role?.name || "")}"
        >

        <label>Цвет</label>

        <input
            id="roleColor"
            type="color"
            value="${role?.color || "#5865f2"}"
        >

        <label>Позиция в иерархии</label>

        <input
            id="rolePosition"
            type="number"
            min="1"
            max="8999"
            value="${role?.position || 10}"
        >

        <div class="section">ПРАВА РОЛИ</div>

        ${permissions.map(([permission, label]) => `

            <label class="perm">

                <input
                    type="checkbox"
                    value="${permission}"
                    ${role?.permissions?.includes(permission) ? "checked" : ""}
                    ${!hasPermission("manage_permissions") ? "disabled" : ""}
                >

                ${label}

            </label>

        `).join("")}

        <div class="tools">

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

        </div>

    `);
}


async function saveRole(roleId) {

    const permissions = [
        ...document.querySelectorAll(
            "#dialog input[type=checkbox]:checked"
        )
    ].map(input => input.value);

    const data = {
        name: $("roleName").value.trim(),
        color: $("roleColor").value,
        position: Number($("rolePosition").value),
        permissions
    };

    try {

        state = await api(
            roleId ? `/api/roles/${roleId}` : "/api/roles",
            {
                method: roleId ? "PUT" : "POST",
                body: JSON.stringify(data)
            }
        );

        closeModal();
        render();

    } catch (error) {
        alert(error.message);
    }
}


async function deleteRole(roleId) {

    if (!confirm("Удалить эту роль?")) return;

    try {

        state = await api(`/api/roles/${roleId}`, {
            method: "DELETE"
        });

        closeModal();
        render();

    } catch (error) {
        alert(error.message);
    }
}


// ============================================================
// MEMBER MANAGEMENT
// ============================================================

function openMembers() {

    if (!hasPermission("manage_members")) {
        return alert("Недостаточно прав");
    }

    showModal(`

        <h3>Управление участниками</h3>

        ${state.users.map(user => {

            const roles = state.roles.filter(role =>
                role.name !== "Владелец" &&
                (state.me.is_owner || !role.protected)
            );

            return `

                <div style="padding:10px 0;border-bottom:1px solid #444">

                    <b>${escapeHTML(user.first_name)}</b>

                    <div>

                        ${roles.map(role => `

                            <button
                                class="tool"
                                style="margin:4px;color:${role.color}"
                                onclick="toggleRole(
                                    ${user.id},
                                    ${role.id},
                                    ${user.role_ids.includes(role.id)}
                                )"
                            >

                                ${user.role_ids.includes(role.id) ? "−" : "+"}
                                ${escapeHTML(role.name)}

                            </button>

                        `).join("")}

                    </div>

                </div>

            `;

        }).join("")}

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
                { method: "DELETE" }
            );

        } else {

            state = await api("/api/roles/assign", {
                method: "POST",
                body: JSON.stringify({
                    user_id: userId,
                    role_id: roleId
                })
            });

        }

        openMembers();
        render();

    } catch (error) {
        alert(error.message);
    }
}


// ============================================================
// REFRESH
// ============================================================

async function refresh() {

    try {
        state = await api("/api/state");
        render();
    } catch (error) {
        console.error(error);
    }
}


// ============================================================
// START
// ============================================================

async function boot() {

    if (!tg.initData) {

        document.body.innerHTML = `
            <div style="padding:24px">
                Откройте приложение через Telegram.
            </div>
        `;

        return;
    }

    try {

        const response = await fetch("/api/auth", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                init_data: tg.initData
            })
        });

        state = await response.json();

        if (!response.ok) {
            throw new Error(state.detail || "Ошибка авторизации");
        }

        uid = state.me.id;

        render();

        setInterval(refresh, 3000);

    } catch (error) {

        document.body.innerHTML = `
            <div style="padding:24px">
                ${escapeHTML(error.message)}
            </div>
        `;

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

    app.router.add_post("/api/auth", api_auth)
    app.router.add_get("/api/state", api_state)

    app.router.add_post("/api/messages", api_send_message)

    app.router.add_post("/api/roles", api_create_role)
    app.router.add_put("/api/roles/{role_id}", api_edit_role)
    app.router.add_delete("/api/roles/{role_id}", api_delete_role)

    app.router.add_post("/api/roles/assign", api_assign_role)
    app.router.add_delete("/api/roles/assign", api_remove_role)

    return app


# ============================================================
# MAIN
# ============================================================

async def main():

    if BOT_TOKEN == "YOUR_BOT_TOKEN":
        raise RuntimeError("Укажите BOT_TOKEN")

    if OWNER_ID == 123456789:
        raise RuntimeError("Укажите OWNER_ID")

    init_db()

    runner = web.AppRunner(create_web_app())

    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=WEB_PORT
    )

    await site.start()

    print(f"TELEGRAMM CHAT запущен на порту {WEB_PORT}")

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
