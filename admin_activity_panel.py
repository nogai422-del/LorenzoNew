import html
import re
import time
from typing import Callable, Optional

from aiogram import F, Router
from aiogram.filters import Command
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
from inactivity import build_message_url, check_chat_now, format_dt

router = Router()
_ADMIN_CHECKER: Optional[Callable[[int], bool]] = None
_ADMIN_IDS_PROVIDER: Optional[Callable[[], list[int]]] = None
_BOTLOG = None


def setup_activity_panel(admin_checker: Callable[[int], bool], admin_ids_provider: Callable[[], list[int]], botlog):
    global _ADMIN_CHECKER, _ADMIN_IDS_PROVIDER, _BOTLOG
    _ADMIN_CHECKER = admin_checker
    _ADMIN_IDS_PROVIDER = admin_ids_provider
    _BOTLOG = botlog


def _is_admin(user_id: int) -> bool:
    try:
        return bool(_ADMIN_CHECKER and _ADMIN_CHECKER(int(user_id)))
    except Exception:
        return False


class ActivityStates(StatesGroup):
    value = State()


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
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def settings_keyboard(chat_id: int, enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📆 Порог неактивности", callback_data=f"act:set:days:{chat_id}")],
        [InlineKeyboardButton(text="🔄 Интервал проверки", callback_data=f"act:set:interval:{chat_id}")],
        [InlineKeyboardButton(text="🔔 Повтор уведомлений", callback_data=f"act:set:repeat:{chat_id}")],
        [InlineKeyboardButton(text="💬 Минимум сообщений", callback_data=f"act:set:messages:{chat_id}")],
        [InlineKeyboardButton(text="⏸ Выключить" if enabled else "▶️ Включить", callback_data=f"act:toggle:{chat_id}")],
        [InlineKeyboardButton(text="⚡ Проверить сейчас", callback_data=f"act:check:{chat_id}"),
         InlineKeyboardButton(text="👥 Показать кандидатов", callback_data=f"act:list:{chat_id}")],
        [InlineKeyboardButton(text="🧹 Сбросить алерты", callback_data=f"act:clear:{chat_id}")],
        [InlineKeyboardButton(text="◀️ К выбору чата", callback_data="act:chats")],
    ])


def _settings_text(chat_id: int) -> str:
    s = get_chat_settings(chat_id)
    info = get_chat_info(chat_id) or {}
    title = info.get("title") or (f"@{info['username']}" if info.get("username") else str(chat_id))
    status = "включена" if s["enabled"] else "выключена"
    minimum = str(s["min_message_count"]) if s["min_message_count"] else "отключено"
    return (
        f"⏳ <b>Проверка активности</b>\n\n"
        f"Чат: <b>{html.escape(title)}</b>\n"
        f"Проверка: <b>{status}</b>\n"
        f"Порог отсутствия: <b>{s['inactivity_days']} дн.</b>\n"
        f"Проверять каждые: <b>{s['check_interval_minutes']} мин.</b>\n"
        f"Повторять алерт через: <b>{s['repeat_alert_hours']} ч.</b>\n"
        f"Минимум сообщений: <b>{minimum}</b>\n\n"
        "Проверка по количеству сообщений начинает действовать после того же периода, "
        "который указан в пороге отсутствия. Значение 0 отключает её."
    )


async def _show_settings(target, chat_id: int):
    ensure_chat_settings(chat_id)
    s = get_chat_settings(chat_id)
    text = _settings_text(chat_id)
    kb = settings_keyboard(chat_id, bool(s["enabled"]))
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("activity"))
async def activity_command(message: Message, state: FSMContext):
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "Выберите чат, для которого нужно настроить проверку активности:",
        reply_markup=chats_keyboard(),
    )


@router.callback_query(F.data == "act:chats")
async def back_to_chats(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await state.clear()
    await call.message.edit_text("Выберите чат:", reply_markup=chats_keyboard())
    await call.answer()


@router.callback_query(F.data.startswith("act:chat:"))
async def select_chat(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    chat_id = int(call.data.rsplit(":", 1)[1])
    await state.clear()
    await _show_settings(call, chat_id)


@router.callback_query(F.data.startswith("act:toggle:"))
async def toggle_chat(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    chat_id = int(call.data.rsplit(":", 1)[1])
    s = get_chat_settings(chat_id)
    set_chat_settings(chat_id, enabled=not bool(s["enabled"]), last_check_at=0)
    await _show_settings(call, chat_id)


_SET_PROMPTS = {
    "days": ("inactivity_days", "Введите число дней от 1 до 3650:", 1, 3650),
    "interval": ("check_interval_minutes", "Введите интервал проверки в минутах от 1 до 10080:", 1, 10080),
    "repeat": ("repeat_alert_hours", "Через сколько часов повторять уведомление (1–8760):", 1, 8760),
    "messages": ("min_message_count", "Введите минимум сообщений. 0 отключает эту проверку:", 0, 1_000_000),
}


@router.callback_query(F.data.startswith("act:set:"))
async def prepare_value(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    match = re.fullmatch(r"act:set:(days|interval|repeat|messages):(-?\d+)", call.data or "")
    if not match:
        return await call.answer("Некорректная команда", show_alert=True)
    kind, chat_id_raw = match.groups()
    field, prompt, minimum, maximum = _SET_PROMPTS[kind]
    await state.set_state(ActivityStates.value)
    await state.update_data(chat_id=int(chat_id_raw), field=field, minimum=minimum, maximum=maximum)
    await call.message.answer(prompt)
    await call.answer()


@router.message(ActivityStates.value)
async def save_value(message: Message, state: FSMContext):
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    text = (message.text or "").strip()
    if not re.fullmatch(r"\d+", text):
        return await message.answer("Нужно отправить целое неотрицательное число.")
    value = int(text)
    if not int(data["minimum"]) <= value <= int(data["maximum"]):
        return await message.answer(f"Допустимый диапазон: {data['minimum']}–{data['maximum']}.")
    chat_id = int(data["chat_id"])
    set_chat_settings(chat_id, **{data["field"]: value}, last_check_at=0)
    await state.clear()
    await _show_settings(message, chat_id)


@router.callback_query(F.data.startswith("act:clear:"))
async def clear_alerts(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    chat_id = int(call.data.rsplit(":", 1)[1])
    clear_alerts_for_chat(chat_id)
    await call.answer("История уведомлений сброшена", show_alert=True)


@router.callback_query(F.data.startswith("act:check:"))
async def run_check(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    chat_id = int(call.data.rsplit(":", 1)[1])
    await call.answer("Проверяю…")
    count = await check_chat_now(
        call.bot,
        chat_id,
        _ADMIN_IDS_PROVIDER() if _ADMIN_IDS_PROVIDER else [call.from_user.id],
        _BOTLOG,
        force=True,
    )
    await call.message.answer(f"Проверка завершена. Отправлено уведомлений: {count}.")


@router.callback_query(F.data.startswith("act:list:"))
async def list_candidates(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    chat_id = int(call.data.rsplit(":", 1)[1])
    s = get_chat_settings(chat_id)
    now_ts = int(time.time())
    rows = get_alert_candidates(chat_id, s["inactivity_days"], s["min_message_count"], now_ts, 100)
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
