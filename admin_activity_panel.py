import html
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
    list_known_chats,
    set_chat_settings,
)
from inactivity import build_message_url, check_chat_now, format_dt, send_test_inactivity_alert

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


@router.callback_query(F.data == "act:none")
async def no_chats(call: CallbackQuery):
    await call.answer("Добавьте бота в группу и отправьте там сообщение", show_alert=True)
