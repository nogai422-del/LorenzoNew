import os
import csv
import html
import sqlite3
import time
from io import StringIO
from datetime import datetime
from typing import Callable, Optional

from aiogram import Router, F, BaseMiddleware, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)

router = Router()

DB_FILE = os.getenv("MEMBERS_PANEL_DB", "members_panel.sqlite3")

_ADMIN_CHECKER: Optional[Callable[[int], bool]] = None


# =========================
# SETUP
# =========================
def setup_members_panel(is_admin_checker: Callable[[int], bool]):
    """
    Вызывается из main.py:
        setup_members_panel(is_admin)
    """
    global _ADMIN_CHECKER
    _ADMIN_CHECKER = is_admin_checker
    init_members_db()


def _is_admin(user_id: int) -> bool:
    if _ADMIN_CHECKER is None:
        return False
    try:
        return bool(_ADMIN_CHECKER(int(user_id)))
    except Exception:
        return False


# =========================
# DB
# =========================
def _connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_members_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS known_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                type TEXT,
                first_seen_ts INTEGER,
                last_seen_ts INTEGER
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS known_members (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT,
                username TEXT,
                is_bot INTEGER DEFAULT 0,
                first_seen_ts INTEGER,
                last_seen_ts INTEGER,
                joined_at INTEGER,
                left_at INTEGER,
                message_count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )

        conn.commit()


def _now_ts() -> int:
    return int(time.time())


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"


def record_chat(chat_id: int, title: str | None, username: str | None, chat_type: str | None):
    now = _now_ts()

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO known_chats(chat_id, title, username, type, first_seen_ts, last_seen_ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                type = excluded.type,
                last_seen_ts = excluded.last_seen_ts
            """,
            (int(chat_id), title, username, chat_type, now, now),
        )
        conn.commit()


def record_member_activity(
    chat_id: int,
    user_id: int,
    name: str | None,
    username: str | None,
    is_bot: bool,
):
    now = _now_ts()

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO known_members(
                chat_id, user_id, name, username, is_bot,
                first_seen_ts, last_seen_ts, joined_at, left_at, message_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                name = excluded.name,
                username = excluded.username,
                is_bot = excluded.is_bot,
                last_seen_ts = excluded.last_seen_ts,
                left_at = NULL,
                message_count = message_count + 1
            """,
            (
                int(chat_id),
                int(user_id),
                name,
                username,
                1 if is_bot else 0,
                now,
                now,
            ),
        )
        conn.commit()


def record_member_join(
    chat_id: int,
    user_id: int,
    name: str | None,
    username: str | None,
    is_bot: bool,
):
    now = _now_ts()

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO known_members(
                chat_id, user_id, name, username, is_bot,
                first_seen_ts, last_seen_ts, joined_at, left_at, message_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                name = excluded.name,
                username = excluded.username,
                is_bot = excluded.is_bot,
                last_seen_ts = excluded.last_seen_ts,
                joined_at = excluded.joined_at,
                left_at = NULL
            """,
            (
                int(chat_id),
                int(user_id),
                name,
                username,
                1 if is_bot else 0,
                now,
                now,
                now,
            ),
        )
        conn.commit()


def record_member_left(chat_id: int, user_id: int):
    now = _now_ts()

    with _connect() as conn:
        conn.execute(
            """
            UPDATE known_members
            SET left_at = ?, last_seen_ts = ?
            WHERE chat_id = ? AND user_id = ?
            """,
            (now, now, int(chat_id), int(user_id)),
        )
        conn.commit()


def list_internal_chats() -> list[sqlite3.Row]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT chat_id, title, username, type, first_seen_ts, last_seen_ts
            FROM known_chats
            ORDER BY last_seen_ts DESC
            """
        ).fetchall()
    return rows


def count_known_members(chat_id: int, include_left: bool = True) -> int:
    with _connect() as conn:
        if include_left:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM known_members WHERE chat_id = ?",
                (int(chat_id),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM known_members WHERE chat_id = ? AND left_at IS NULL",
                (int(chat_id),),
            ).fetchone()

    return int(row["c"] or 0)


def list_known_members(chat_id: int, limit: int = 10, offset: int = 0) -> list[sqlite3.Row]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                chat_id,
                user_id,
                name,
                username,
                is_bot,
                first_seen_ts,
                last_seen_ts,
                joined_at,
                left_at,
                message_count
            FROM known_members
            WHERE chat_id = ?
            ORDER BY
                CASE WHEN left_at IS NULL THEN 0 ELSE 1 END,
                last_seen_ts DESC
            LIMIT ? OFFSET ?
            """,
            (int(chat_id), int(limit), int(offset)),
        ).fetchall()

    return rows


def list_all_known_members(chat_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                chat_id,
                user_id,
                name,
                username,
                is_bot,
                first_seen_ts,
                last_seen_ts,
                joined_at,
                left_at,
                message_count
            FROM known_members
            WHERE chat_id = ?
            ORDER BY
                CASE WHEN left_at IS NULL THEN 0 ELSE 1 END,
                last_seen_ts DESC
            """,
            (int(chat_id),),
        ).fetchall()

    return rows


# =========================
# OPTIONAL SYNC WITH YOUR db.py
# =========================
def _extract_chat_id(item):
    try:
        if isinstance(item, dict):
            return int(item.get("chat_id"))
        if isinstance(item, (list, tuple)):
            return int(item[0])
        return int(item)
    except Exception:
        return None


def list_external_chat_ids_from_main_db() -> list[int]:
    """
    Если в твоём db.py есть list_chat_ids_with_settings(),
    модуль попробует подхватить группы оттуда.
    """
    try:
        from db import list_chat_ids_with_settings

        raw = list_chat_ids_with_settings()
        result = []

        for item in raw:
            chat_id = _extract_chat_id(item)
            if chat_id:
                result.append(chat_id)

        return sorted(set(result))
    except Exception:
        return []


# =========================
# MIDDLEWARE
# =========================
class MembersTrackMiddleware(BaseMiddleware):
    """
    Подключается в main.py:
        dp.message.outer_middleware(MembersTrackMiddleware())

    Он молча собирает:
    - группы, где бот видит сообщения;
    - пользователей, которые пишут;
    - вступивших;
    - вышедших.
    """

    async def __call__(self, handler, event, data):
        try:
            if isinstance(event, Message):
                chat = event.chat

                if chat and chat.type in ("group", "supergroup"):
                    record_chat(
                        chat_id=int(chat.id),
                        title=chat.title,
                        username=getattr(chat, "username", None),
                        chat_type=chat.type,
                    )

                    if event.from_user and not event.from_user.is_bot:
                        record_member_activity(
                            chat_id=int(chat.id),
                            user_id=int(event.from_user.id),
                            name=event.from_user.full_name,
                            username=event.from_user.username,
                            is_bot=event.from_user.is_bot,
                        )

                    if event.new_chat_members:
                        for user in event.new_chat_members:
                            record_member_join(
                                chat_id=int(chat.id),
                                user_id=int(user.id),
                                name=user.full_name,
                                username=user.username,
                                is_bot=user.is_bot,
                            )

                    if event.left_chat_member:
                        record_member_left(
                            chat_id=int(chat.id),
                            user_id=int(event.left_chat_member.id),
                        )

        except Exception:
            pass

        return await handler(event, data)


# =========================
# UI HELPERS
# =========================
def _chat_label_from_row(row) -> str:
    title = None

    try:
        title = row["title"]
    except Exception:
        pass

    if not title:
        try:
            username = row["username"]
            if username:
                title = f"@{username}"
        except Exception:
            pass

    if not title:
        try:
            title = str(row["chat_id"])
        except Exception:
            title = "Unknown chat"

    if len(title) > 45:
        title = title[:42] + "..."

    return title


async def get_chat_title_safe(bot: Bot, chat_id: int) -> str:
    # сначала пробуем Telegram API
    try:
        chat = await bot.get_chat(chat_id)

        title = chat.title or chat.username or str(chat_id)

        record_chat(
            chat_id=int(chat.id),
            title=chat.title,
            username=getattr(chat, "username", None),
            chat_type=chat.type,
        )

        return title
    except Exception:
        pass

    # потом локальную базу
    with _connect() as conn:
        row = conn.execute(
            "SELECT title, username FROM known_chats WHERE chat_id = ?",
            (int(chat_id),),
        ).fetchone()

    if row:
        if row["title"]:
            return row["title"]
        if row["username"]:
            return f"@{row['username']}"

    return str(chat_id)


async def build_chats_kb(bot: Bot) -> InlineKeyboardMarkup:
    buttons = []

    seen = set()

    # 1. Группы из локальной базы members_panel.sqlite3
    for row in list_internal_chats():
        chat_id = int(row["chat_id"])
        seen.add(chat_id)

        label = _chat_label_from_row(row)

        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"👥 {label}",
                    callback_data=f"mp_chat:{chat_id}",
                )
            ]
        )

    # 2. Группы из твоей основной db.py, если доступны
    for chat_id in list_external_chat_ids_from_main_db():
        if chat_id in seen:
            continue

        seen.add(chat_id)

        title = await get_chat_title_safe(bot, chat_id)
        if len(title) > 45:
            title = title[:42] + "..."

        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"👥 {title}",
                    callback_data=f"mp_chat:{chat_id}",
                )
            ]
        )

    if not buttons:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Пока нет известных групп",
                    callback_data="mp_no_chats",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def chat_actions_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Количество участников",
                    callback_data=f"mp_count:{chat_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👀 Известные участники",
                    callback_data=f"mp_known:{chat_id}:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📤 Экспорт CSV",
                    callback_data=f"mp_csv:{chat_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Выбрать другую группу",
                    callback_data="mp_back_to_chats",
                )
            ],
        ]
    )


def known_members_page_kb(chat_id: int, page: int, total: int, per_page: int) -> InlineKeyboardMarkup:
    buttons = []

    nav = []

    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"mp_known:{chat_id}:{page - 1}",
            )
        )

    if (page + 1) * per_page < total:
        nav.append(
            InlineKeyboardButton(
                text="Вперёд ➡️",
                callback_data=f"mp_known:{chat_id}:{page + 1}",
            )
        )

    if nav:
        buttons.append(nav)

    buttons.append(
        [
            InlineKeyboardButton(
                text="📤 Экспорт CSV",
                callback_data=f"mp_csv:{chat_id}",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                text="◀️ Назад к группе",
                callback_data=f"mp_chat:{chat_id}",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def member_line(row: sqlite3.Row, number: int) -> str:
    user_id = int(row["user_id"])
    name = html.escape(row["name"] or str(user_id))
    username = row["username"]
    message_count = int(row["message_count"] or 0)

    status = "🚪 вышел" if row["left_at"] else "✅ в группе/известен"
    bot_mark = " 🤖" if row["is_bot"] else ""

    username_part = f" @{html.escape(username)}" if username else ""

    return (
        f"{number}. <a href=\"tg://user?id={user_id}\">{name}</a>{bot_mark}\n"
        f"   ID: <code>{user_id}</code>{username_part}\n"
        f"   Статус: <b>{status}</b>\n"
        f"   Сообщений: <b>{message_count}</b>\n"
        f"   Последняя активность: <code>{_fmt_ts(row['last_seen_ts'])}</code>"
    )


# =========================
# OPEN MENU
# =========================
@router.message(Command("members"))
@router.message(F.text == "👥 Участники")
async def open_members_panel(message: Message, bot: Bot):
    if not message.from_user or not _is_admin(message.from_user.id):
        if message.chat.type == "private":
            return await message.answer("⛔ Нет доступа.")
        return

    # Если случайно нажали/написали в группе — ничего не показываем в группе
    if message.chat.type != "private":
        try:
            await message.delete()
        except Exception:
            pass

        try:
            await bot.send_message(
                message.from_user.id,
                "🔒 Раздел «Участники» доступен только в личном чате с ботом.\n\n"
                "Открой личку и нажми /admin или /members."
            )
        except Exception:
            pass

        return

    kb = await build_chats_kb(bot)

    await message.answer(
        "👥 <b>Участники</b>\n\n"
        "Выбери группу, по которой нужно посмотреть данные.\n\n"
        "В группу бот ничего не отправит.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "mp_no_chats")
async def no_chats_cb(call: CallbackQuery):
    if not call.from_user or not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    await call.answer(
        "Пока нет известных групп. "
        "Бот должен увидеть хотя бы одно сообщение/вступление в группе.",
        show_alert=True,
    )


@router.callback_query(F.data == "mp_back_to_chats")
async def back_to_chats_cb(call: CallbackQuery, bot: Bot):
    if not call.from_user or not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    kb = await build_chats_kb(bot)

    await call.message.edit_text(
        "👥 <b>Участники</b>\n\n"
        "Выбери группу:",
        reply_markup=kb,
    )

    await call.answer()


# =========================
# SELECT CHAT
# =========================
@router.callback_query(F.data.startswith("mp_chat:"))
async def select_chat_cb(call: CallbackQuery, bot: Bot):
    if not call.from_user or not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    try:
        chat_id = int(call.data.split(":", 1)[1])
    except Exception:
        return await call.answer("Ошибка chat_id", show_alert=True)

    title = await get_chat_title_safe(bot, chat_id)

    known_total = count_known_members(chat_id, include_left=True)
    known_active = count_known_members(chat_id, include_left=False)

    await call.message.edit_text(
        f"👥 <b>Группа:</b> {html.escape(title)}\n"
        f"ID: <code>{chat_id}</code>\n\n"
        f"Известных участников в базе: <b>{known_total}</b>\n"
        f"Без отметки выхода: <b>{known_active}</b>\n\n"
        f"Что показать?",
        reply_markup=chat_actions_kb(chat_id),
    )

    await call.answer()


# =========================
# REAL MEMBER COUNT
# =========================
@router.callback_query(F.data.startswith("mp_count:"))
async def member_count_cb(call: CallbackQuery, bot: Bot):
    if not call.from_user or not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    try:
        chat_id = int(call.data.split(":", 1)[1])
    except Exception:
        return await call.answer("Ошибка chat_id", show_alert=True)

    title = await get_chat_title_safe(bot, chat_id)

    try:
        count = await bot.get_chat_member_count(chat_id)

        await call.message.answer(
            f"📊 <b>Количество участников</b>\n\n"
            f"Группа: <b>{html.escape(title)}</b>\n"
            f"ID: <code>{chat_id}</code>\n\n"
            f"👥 Участников сейчас: <b>{count}</b>"
        )
    except Exception as e:
        await call.message.answer(
            f"❌ Не удалось получить количество участников.\n\n"
            f"Группа: <b>{html.escape(title)}</b>\n"
            f"ID: <code>{chat_id}</code>\n\n"
            f"<code>{html.escape(repr(e))}</code>"
        )

    await call.answer()


# =========================
# KNOWN MEMBERS LIST
# =========================
@router.callback_query(F.data.startswith("mp_known:"))
async def known_members_cb(call: CallbackQuery, bot: Bot):
    if not call.from_user or not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    try:
        _, chat_id_raw, page_raw = call.data.split(":")
        chat_id = int(chat_id_raw)
        page = max(0, int(page_raw))
    except Exception:
        return await call.answer("Ошибка данных", show_alert=True)

    per_page = 10
    offset = page * per_page

    title = await get_chat_title_safe(bot, chat_id)
    total = count_known_members(chat_id, include_left=True)
    rows = list_known_members(chat_id, limit=per_page, offset=offset)

    if total == 0:
        await call.message.edit_text(
            f"👀 <b>Известные участники</b>\n\n"
            f"Группа: <b>{html.escape(title)}</b>\n"
            f"ID: <code>{chat_id}</code>\n\n"
            f"Пока нет известных участников.\n\n"
            f"Бот начнёт собирать их, когда увидит сообщения, входы или выходы.",
            reply_markup=chat_actions_kb(chat_id),
        )
        return await call.answer()

    lines = []

    for idx, row in enumerate(rows, start=offset + 1):
        lines.append(member_line(row, idx))

    text = (
        f"👀 <b>Известные участники</b>\n\n"
        f"Группа: <b>{html.escape(title)}</b>\n"
        f"ID: <code>{chat_id}</code>\n\n"
        f"Всего известных: <b>{total}</b>\n"
        f"Страница: <b>{page + 1}</b>\n\n"
        + "\n\n".join(lines)
    )

    if len(text) > 3900:
        text = text[:3850] + "\n\n…"

    await call.message.edit_text(
        text,
        reply_markup=known_members_page_kb(chat_id, page, total, per_page),
        disable_web_page_preview=True,
    )

    await call.answer()


# =========================
# CSV EXPORT
# =========================
@router.callback_query(F.data.startswith("mp_csv:"))
async def export_csv_cb(call: CallbackQuery, bot: Bot):
    if not call.from_user or not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    try:
        chat_id = int(call.data.split(":", 1)[1])
    except Exception:
        return await call.answer("Ошибка chat_id", show_alert=True)

    title = await get_chat_title_safe(bot, chat_id)
    rows = list_all_known_members(chat_id)

    if not rows:
        await call.answer("Нет данных для экспорта.", show_alert=True)
        return

    output = StringIO()
    writer = csv.writer(output, delimiter=";")

    writer.writerow(
        [
            "chat_id",
            "chat_title",
            "user_id",
            "name",
            "username",
            "is_bot",
            "status",
            "first_seen",
            "last_seen",
            "joined_at",
            "left_at",
            "message_count",
        ]
    )

    for row in rows:
        status = "left" if row["left_at"] else "known_or_active"

        writer.writerow(
            [
                row["chat_id"],
                title,
                row["user_id"],
                row["name"] or "",
                row["username"] or "",
                int(row["is_bot"] or 0),
                status,
                _fmt_ts(row["first_seen_ts"]),
                _fmt_ts(row["last_seen_ts"]),
                _fmt_ts(row["joined_at"]),
                _fmt_ts(row["left_at"]),
                int(row["message_count"] or 0),
            ]
        )

    data = output.getvalue().encode("utf-8-sig")

    safe_name = str(chat_id).replace("-", "m")
    filename = f"known_members_{safe_name}.csv"

    file = BufferedInputFile(data, filename=filename)

    await call.message.answer_document(
        file,
        caption=(
            f"📤 <b>Экспорт известных участников</b>\n\n"
            f"Группа: <b>{html.escape(title)}</b>\n"
            f"ID: <code>{chat_id}</code>\n"
            f"Строк: <b>{len(rows)}</b>"
        ),
    )

    await call.answer("CSV сформирован.")