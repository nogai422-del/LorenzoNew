import asyncio
import csv
import html
import io
import json
import re
import time
from typing import Awaitable, Callable, Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db import (
    clear_alerts_for_chat,
    ensure_chat_settings,
    get_alert_candidates,
    get_chat_info,
    get_chat_settings,
    import_member,
    list_known_chats,
    set_chat_settings,
)
from inactivity import (
    build_message_url,
    check_chat_now,
    format_dt,
    send_test_inactivity_alert,
    synchronize_chat_members,
)

router = Router()
_ADMIN_CHECKER: Optional[Callable[[int], bool]] = None
_EDIT_CHECKER: Optional[Callable[[int], bool]] = None
_ADMIN_IDS_PROVIDER: Optional[Callable[[], list[int]]] = None
_OWNER_ID_PROVIDER: Optional[Callable[[], int]] = None
_BOTLOG = None


def setup_activity_panel(
    admin_checker: Callable[[int], bool],
    edit_checker: Callable[[int], bool],
    admin_ids_provider: Callable[[], list[int]],
    owner_id_provider: Callable[[], int],
    botlog,
):
    global _ADMIN_CHECKER, _EDIT_CHECKER, _ADMIN_IDS_PROVIDER, _OWNER_ID_PROVIDER, _BOTLOG
    _ADMIN_CHECKER = admin_checker
    _EDIT_CHECKER = edit_checker
    _ADMIN_IDS_PROVIDER = admin_ids_provider
    _OWNER_ID_PROVIDER = owner_id_provider
    _BOTLOG = botlog


def _is_admin(user_id: int) -> bool:
    try:
        return bool(_ADMIN_CHECKER and _ADMIN_CHECKER(int(user_id)))
    except Exception:
        return False


def _can_edit_global(user_id: int) -> bool:
    try:
        return bool(_EDIT_CHECKER and _EDIT_CHECKER(int(user_id)))
    except Exception:
        return False


def _owner_id() -> int:
    try:
        return int(_OWNER_ID_PROVIDER()) if _OWNER_ID_PROVIDER else 0
    except Exception:
        return 0


class ActivityStates(StatesGroup):
    value = State()
    selected_chat = State()
    import_file = State()


def _chat_label(row) -> str:
    title = row["title"] or (f"@{row['username']}" if row["username"] else str(row["chat_id"]))
    return title[:45]


def chats_keyboard() -> InlineKeyboardMarkup:
    rows = list_known_chats()
    buttons = [[InlineKeyboardButton(
        text=("✅ " if int(row["enabled"]) else "⏸ ") + _chat_label(row),
        callback_data=f"act:chat:{int(row['chat_id'])}",
    )] for row in rows[:80]]
    if not buttons:
        buttons = [[InlineKeyboardButton(text="Чаты пока не обнаружены", callback_data="act:none")]]
    buttons.append([InlineKeyboardButton(text="◀️ В главную панель", callback_data="act:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def settings_keyboard(chat_id: int, enabled: bool, can_edit: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_edit:
        rows.extend([
            [InlineKeyboardButton(text="📆 Порог неактивности", callback_data=f"act:set:days:{chat_id}")],
            [InlineKeyboardButton(text="🔄 Интервал проверки", callback_data=f"act:set:interval:{chat_id}")],
            [InlineKeyboardButton(text="🔔 Повтор уведомлений", callback_data=f"act:set:repeat:{chat_id}")],
            [InlineKeyboardButton(text="💬 Минимум сообщений", callback_data=f"act:set:messages:{chat_id}")],
            [InlineKeyboardButton(text="⏸ Выключить" if enabled else "▶️ Включить", callback_data=f"act:toggle:{chat_id}")],
            [InlineKeyboardButton(text="🧹 Сбросить историю алертов", callback_data=f"act:clear:{chat_id}")],
        ])
    rows.extend([
        [InlineKeyboardButton(text="⚡ Реальная проверка", callback_data=f"act:check:{chat_id}"),
         InlineKeyboardButton(text="👥 Кандидаты", callback_data=f"act:list:{chat_id}")],
        [InlineKeyboardButton(text="🔄 Обновить базу участников", callback_data=f"act:sync:{chat_id}")],
        [InlineKeyboardButton(text="📥 Импорт участников", callback_data=f"act:import:{chat_id}")],
        [InlineKeyboardButton(text="🧪 Тест оповещения", callback_data=f"act:test:{chat_id}")],
        [InlineKeyboardButton(text="◀️ К выбору чата", callback_data="act:chats")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _settings_text(chat_id: int, can_edit: bool) -> str:
    s = get_chat_settings(chat_id)
    info = get_chat_info(chat_id) or {}
    title = info.get("title") or (f"@{info['username']}" if info.get("username") else str(chat_id))
    status = "включена" if s["enabled"] else "выключена"
    minimum = str(s["min_message_count"]) if s["min_message_count"] else "отключено"
    access = "изменение разрешено" if can_edit else "только просмотр"
    return (
        f"📊 <b>Активность участников</b>\n\n"
        f"Чат: <b>{html.escape(title)}</b>\n"
        f"Доступ: <b>{access}</b>\n"
        f"Проверка: <b>{status}</b>\n"
        f"Порог отсутствия: <b>{s['inactivity_days']} дн.</b>\n"
        f"Проверять каждые: <b>{s['check_interval_minutes']} мин.</b>\n"
        f"Повторять алерт через: <b>{s['repeat_alert_hours']} ч.</b>\n"
        f"Минимум сообщений: <b>{minimum}</b>\n\n"
        "Настройки защищены: менять их может владелец бота или администратор выбранного Telegram-чата."
    )


async def _can_manage_chat(bot, user_id: int, chat_id: int) -> bool:
    if user_id == _owner_id():
        return True
    if not _is_admin(user_id) or not _can_edit_global(user_id):
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"administrator", "creator"}
    except Exception:
        return False


async def _validate_selected_chat(call: CallbackQuery, state: FSMContext, chat_id: int, require_edit: bool = False) -> bool:
    if not _is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return False
    data = await state.get_data()
    selected = data.get("selected_chat_id")
    if selected is not None and int(selected) != int(chat_id):
        await call.answer("Сессия настроек устарела. Выберите чат заново.", show_alert=True)
        return False
    if require_edit and not await _can_manage_chat(call.bot, call.from_user.id, chat_id):
        await call.answer("Настройки этого чата защищены", show_alert=True)
        return False
    return True


async def _show_settings(target, state: FSMContext, chat_id: int):
    ensure_chat_settings(chat_id)
    await state.update_data(selected_chat_id=int(chat_id))
    s = get_chat_settings(chat_id)
    user_id = target.from_user.id
    can_edit = await _can_manage_chat(target.bot, user_id, chat_id)
    text = _settings_text(chat_id, can_edit)
    kb = settings_keyboard(chat_id, bool(s["enabled"]), can_edit)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(F.text == "📊 Активность чатов")
async def activity_section(message: Message, state: FSMContext):
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Выберите чат для просмотра или настройки активности:", reply_markup=chats_keyboard())


@router.callback_query(F.data == "act:chats")
async def back_to_chats(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await state.clear()
    await call.message.edit_text("Выберите чат:", reply_markup=chats_keyboard())
    await call.answer()


@router.callback_query(F.data == "act:main")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await state.clear()
    await call.message.edit_text("Вернитесь в главную админ-панель с помощью кнопки меню или команды /admin.")
    await call.answer()


@router.callback_query(F.data.startswith("act:chat:"))
async def select_chat(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    chat_id = int(call.data.rsplit(":", 1)[1])
    await state.clear()
    await _show_settings(call, state, chat_id)


@router.callback_query(F.data.startswith("act:toggle:"))
async def toggle_chat(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.rsplit(":", 1)[1])
    if not await _validate_selected_chat(call, state, chat_id, require_edit=True):
        return
    s = get_chat_settings(chat_id)
    set_chat_settings(chat_id, enabled=not bool(s["enabled"]), last_check_at=0)
    await _show_settings(call, state, chat_id)


_SET_PROMPTS = {
    "days": ("inactivity_days", "Введите число дней от 1 до 3650:", 1, 3650),
    "interval": ("check_interval_minutes", "Введите интервал проверки в минутах от 1 до 10080:", 1, 10080),
    "repeat": ("repeat_alert_hours", "Через сколько часов повторять уведомление (1–8760):", 1, 8760),
    "messages": ("min_message_count", "Введите минимум сообщений. 0 отключает эту проверку:", 0, 1_000_000),
}


@router.callback_query(F.data.startswith("act:set:"))
async def prepare_value(call: CallbackQuery, state: FSMContext):
    match = re.fullmatch(r"act:set:(days|interval|repeat|messages):(-?\d+)", call.data or "")
    if not match:
        return await call.answer("Некорректная команда", show_alert=True)
    kind, chat_id_raw = match.groups()
    chat_id = int(chat_id_raw)
    if not await _validate_selected_chat(call, state, chat_id, require_edit=True):
        return
    field, prompt, minimum, maximum = _SET_PROMPTS[kind]
    await state.set_state(ActivityStates.value)
    await state.update_data(selected_chat_id=chat_id, chat_id=chat_id, field=field, minimum=minimum, maximum=maximum)
    await call.message.answer(prompt)
    await call.answer()


@router.message(ActivityStates.value)
async def save_value(message: Message, state: FSMContext):
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    chat_id = int(data["chat_id"])
    if not await _can_manage_chat(message.bot, message.from_user.id, chat_id):
        await state.clear()
        return await message.answer("⛔ Настройки этого чата защищены.")
    text = (message.text or "").strip()
    if not re.fullmatch(r"\d+", text):
        return await message.answer("Нужно отправить целое неотрицательное число.")
    value = int(text)
    if not int(data["minimum"]) <= value <= int(data["maximum"]):
        return await message.answer(f"Допустимый диапазон: {data['minimum']}–{data['maximum']}.")
    set_chat_settings(chat_id, **{data["field"]: value}, last_check_at=0)
    await state.clear()
    await _show_settings(message, state, chat_id)


@router.callback_query(F.data.startswith("act:clear:"))
async def clear_alerts(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.rsplit(":", 1)[1])
    if not await _validate_selected_chat(call, state, chat_id, require_edit=True):
        return
    clear_alerts_for_chat(chat_id)
    await call.answer("История уведомлений сброшена", show_alert=True)


@router.callback_query(F.data.startswith("act:check:"))
async def run_check(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.rsplit(":", 1)[1])
    if not await _validate_selected_chat(call, state, chat_id):
        return
    await call.answer("Проверяю…")
    count = await check_chat_now(
        call.bot, chat_id,
        _ADMIN_IDS_PROVIDER() if _ADMIN_IDS_PROVIDER else [call.from_user.id],
        _BOTLOG, force=True,
    )
    await call.message.answer(f"Проверка завершена. Отправлено уведомлений: {count}.")


@router.callback_query(F.data.startswith("act:sync:"))
async def sync_members(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.rsplit(":", 1)[1])
    if not await _validate_selected_chat(call, state, chat_id, require_edit=True):
        return
    await call.answer("Сверяю участников с Telegram…")
    report = await synchronize_chat_members(call.bot, chat_id, _BOTLOG)
    await call.message.answer(
        "🔄 <b>База участников обновлена</b>\n\n"
        f"Проверено через Telegram: <b>{report['checked']}</b>\n"
        f"Удалено вышедших/забаненных: <b>{report['removed']}</b>\n"
        f"Ошибок проверки: <b>{report['errors']}</b>\n"
        f"Осталось в рабочей базе: <b>{report['active']}</b>",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("act:test:"))
async def test_alert(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.rsplit(":", 1)[1])
    if not await _validate_selected_chat(call, state, chat_id):
        return
    await send_test_inactivity_alert(call.bot, call.from_user.id, chat_id)
    await call.answer("Тестовое оповещение отправлено вам", show_alert=True)


@router.callback_query(F.data.startswith("act:list:"))
async def list_candidates(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.rsplit(":", 1)[1])
    if not await _validate_selected_chat(call, state, chat_id):
        return
    s = get_chat_settings(chat_id)
    rows = get_alert_candidates(chat_id, s["inactivity_days"], s["min_message_count"], int(time.time()), 100)
    if not rows:
        return await call.answer("Кандидатов сейчас нет", show_alert=True)
    info = get_chat_info(chat_id) or {}
    lines = ["👥 <b>Кандидаты на уведомление</b>"]
    for row in rows:
        name = html.escape(row["user_name"] or str(row["user_id"]))
        url = build_message_url(chat_id, info.get("username"), row["last_message_id"])
        last = format_dt(row["last_message_at"])
        if url:
            last = f'<a href="{url}">{last}</a>'
        lines.append(f"• {name} — сообщений: {row['total_message_count']}; последнее: {last}")
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3880] + "…"
    await call.message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    await call.answer()



def _normalise_import_rows(payload: bytes, filename: str) -> list[dict]:
    """Читает CSV/JSON, включая снимок последней активности из Telethon."""
    lower = (filename or "").lower()
    if lower.endswith(".json"):
        data = json.loads(payload.decode("utf-8-sig"))
        if isinstance(data, dict):
            data = data.get("members") or data.get("users") or data.get("data") or []
        if not isinstance(data, list):
            raise ValueError("JSON должен содержать массив участников")
        raw_rows = data
    elif lower.endswith(".csv"):
        text = payload.decode("utf-8-sig")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        raw_rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    else:
        raise ValueError("Поддерживаются только файлы CSV и JSON")

    aliases = {
        "user_id": ("user_id", "id", "telegram_id", "tg_id", "userid"),
        "name": ("name", "full_name", "user_name", "fullname"),
        "username": ("username", "user", "login"),
        "message_count": ("message_count", "messages", "total_message_count", "count"),
        "telegram_last_seen_at": ("telegram_last_seen_at", "last_seen_at", "last_online_at"),
        "telegram_status": ("telegram_status", "last_seen_status", "online_status"),
        "telegram_status_checked_at": ("telegram_status_checked_at", "status_checked_at", "exported_at"),
    }
    result = []
    seen = set()
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        normalized = {str(k).strip().lower(): v for k, v in item.items()}
        values = {}
        for target, names in aliases.items():
            values[target] = next((normalized.get(n) for n in names if normalized.get(n) not in (None, "")), None)
        try:
            user_id = int(str(values["user_id"]).strip())
        except (TypeError, ValueError):
            continue
        if user_id <= 0 or user_id in seen:
            continue
        seen.add(user_id)
        username = str(values["username"] or "").strip().lstrip("@") or None
        name = str(values["name"] or "").strip() or None
        try:
            message_count = max(0, int(values["message_count"] or 0))
        except (TypeError, ValueError):
            message_count = 0
        def _optional_int(value):
            try:
                return int(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None

        result.append({
            "user_id": user_id,
            "name": name,
            "username": username,
            "message_count": message_count,
            "telegram_last_seen_at": _optional_int(values.get("telegram_last_seen_at")),
            "telegram_status": str(values.get("telegram_status") or "").strip() or None,
            "telegram_status_checked_at": _optional_int(values.get("telegram_status_checked_at")),
        })
    return result


@router.callback_query(F.data.startswith("act:import:"))
async def prepare_import(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.rsplit(":", 1)[1])
    if not await _validate_selected_chat(call, state, chat_id, require_edit=True):
        return
    await state.set_state(ActivityStates.import_file)
    await state.update_data(selected_chat_id=chat_id, chat_id=chat_id)
    await call.message.answer(
        "📥 <b>Импорт участников</b>\n\n"
        "Отправьте файл <b>CSV</b> или <b>JSON</b>. Обязательное поле: <code>user_id</code>.\n"
        "Дополнительные поля: <code>name</code>, <code>username</code>, <code>message_count</code>.\n\n"
        "Бот проверит каждый ID через Telegram и добавит только участников выбранного чата. "
        "Вышедшие и забаненные будут пропущены. Для отмены нажмите «◀️ Назад».",
        parse_mode="HTML",
    )
    await call.answer()


@router.message(ActivityStates.import_file, F.document)
async def import_members_file(message: Message, state: FSMContext):
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    chat_id = int(data.get("chat_id", 0))
    if not chat_id or not await _can_manage_chat(message.bot, message.from_user.id, chat_id):
        await state.clear()
        return await message.answer("⛔ Настройки этого чата защищены.")

    document = message.document
    filename = document.file_name or "members.csv"
    if document.file_size and document.file_size > 10 * 1024 * 1024:
        return await message.answer("Файл слишком большой. Максимальный размер — 10 МБ.")
    buffer = io.BytesIO()
    await message.bot.download(document, destination=buffer)
    try:
        rows = _normalise_import_rows(buffer.getvalue(), filename)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return await message.answer(f"Не удалось прочитать файл: {html.escape(str(exc))}")
    if not rows:
        return await message.answer("В файле не найдено ни одного корректного числового user_id.")
    if len(rows) > 10000:
        return await message.answer("За один импорт допускается не более 10 000 уникальных участников.")

    progress = await message.answer(f"Проверяю через Telegram: 0 из {len(rows)}…")
    added = 0
    already_present = 0
    skipped = 0
    errors = 0
    bots = 0
    now_ts = int(time.time())

    from db import get_chat_member_stats
    try:
        from members_panel import import_known_member
    except Exception:
        import_known_member = None

    for index, row in enumerate(rows, start=1):
        user_id = int(row["user_id"])
        try:
            member = await message.bot.get_chat_member(chat_id, user_id)
            status = str(member.status)
            if status in {"left", "kicked", "banned"}:
                skipped += 1
                continue
            user = member.user
            name = row["name"] or user.full_name or str(user_id)
            username = row["username"] or user.username
            existed = get_chat_member_stats(chat_id, user_id) is not None
            import_member(
                chat_id, user_id, name, username, now_ts, row["message_count"],
                telegram_last_seen_at=row.get("telegram_last_seen_at"),
                telegram_status=row.get("telegram_status"),
                telegram_status_checked_at=row.get("telegram_status_checked_at"),
            )
            if import_known_member:
                import_known_member(chat_id, user_id, name, username, user.is_bot, row["message_count"])
            bots += 1 if user.is_bot else 0
            if existed:
                already_present += 1
            else:
                added += 1
        except Exception as exc:
            errors += 1
            if _BOTLOG:
                await _BOTLOG(f"member import chat={chat_id} user={user_id} error: {exc}")
        if index % 50 == 0:
            try:
                await progress.edit_text(f"Проверяю через Telegram: {index} из {len(rows)}…")
            except Exception:
                pass
        # Ограничиваем темп запросов к Bot API.
        await asyncio.sleep(0.035)

    await state.clear()
    await progress.edit_text(
        "📥 <b>Импорт завершён</b>\n\n"
        f"Строк с уникальными ID: <b>{len(rows)}</b>\n"
        f"Добавлено новых: <b>{added}</b>\n"
        f"Уже были, данные обновлены: <b>{already_present}</b>\n"
        f"Не состоят в чате: <b>{skipped}</b>\n"
        f"Ботов в добавленных/обновлённых: <b>{bots}</b>\n"
        f"Ошибок Telegram API: <b>{errors}</b>\n\n"
        "Участники без истории сообщений добавлены с нулевым счётчиком. "
        "Если CSV создан обновлённым Telethon-экспортёром, бот также сохранит доступный статус последнего онлайна. "
        "Ссылка на последнее сообщение появится после их первого нового сообщения.",
        parse_mode="HTML",
    )
    await _show_settings(message, state, chat_id)


@router.message(ActivityStates.import_file)
async def import_requires_document(message: Message):
    await message.answer("Отправьте документ в формате CSV или JSON либо нажмите «◀️ Назад».")


@router.callback_query(F.data == "act:none")
async def no_chats(call: CallbackQuery):
    await call.answer("Добавьте бота в группу и отправьте там сообщение", show_alert=True)
