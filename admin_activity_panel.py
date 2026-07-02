# admin_activity_panel.py
import re
import time

from aiogram import Router
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters import Command

from db import (
    ensure_chat_settings,
    get_chat_settings,
    set_chat_settings,
    get_inactive_members,
    clear_alerts_for_chat,
)

router = Router()

BTN_INACTIVITY = "⏳ Неактивность"
BTN_SET_DAYS = "📌 Порог (дни)"
BTN_SET_INTERVAL = "🔁 Интервал (мин)"
BTN_SHOW_INACTIVE = "👀 Неактивные"
BTN_CHECK_NOW = "⚡ Проверить сейчас"
BTN_CLEAR_ALERTS = "🧹 Сбросить алерты"
BTN_BACK = "◀️ Назад"

class InactivityStates(StatesGroup):
    set_days = State()
    set_interval = State()


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SET_DAYS), KeyboardButton(text=BTN_SET_INTERVAL)],
            [KeyboardButton(text=BTN_SHOW_INACTIVE), KeyboardButton(text=BTN_CHECK_NOW)],
            [KeyboardButton(text=BTN_CLEAR_ALERTS)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True
    )


def back_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True
    )


@router.message(Command("activity"))
async def open_activity_panel(message: Message, state: FSMContext):
    """
    Открывает панель. В main.py лучше ограничить права (ADMIN_USER_IDS).
    """
    chat_id = int(message.chat.id)
    ensure_chat_settings(chat_id)

    s = get_chat_settings(chat_id)
    await state.clear()
    await message.reply(
        "⏳ <b>Настройки активности</b>\n\n"
        f"Порог: <code>{s['inactivity_days']} дней</code>\n"
        f"Интервал проверки: <code>{s['check_interval_minutes']} мин</code>\n",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


@router.message(lambda m: m.text == BTN_BACK)
async def close_panel(message: Message, state: FSMContext):
    await state.clear()
    await message.reply("Панель закрыта.", reply_markup=ReplyKeyboardRemove())


@router.message(lambda m: m.text == BTN_SET_DAYS)
async def set_days_prepare(message: Message, state: FSMContext):
    chat_id = int(message.chat.id)
    ensure_chat_settings(chat_id)

    await state.set_state(InactivityStates.set_days)
    await message.reply("Введи порог неактивности (1-3650 дней):", reply_markup=back_kb())


@router.message(InactivityStates.set_days)
async def set_days_finish(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not re.fullmatch(r"\d+", txt):
        return await message.reply("Нужно число дней (пример: 3).", reply_markup=back_kb())

    days = int(txt)
    if not (1 <= days <= 3650):
        return await message.reply("Диапазон: 1-3650.", reply_markup=back_kb())

    chat_id = int(message.chat.id)
    s = get_chat_settings(chat_id)
    set_chat_settings(chat_id, inactivity_days=days, check_interval_minutes=s["check_interval_minutes"])

    await state.clear()
    s2 = get_chat_settings(chat_id)
    await message.reply(f"✅ Порог обновлён: {s2['inactivity_days']} дней", reply_markup=main_kb())


@router.message(lambda m: m.text == BTN_SET_INTERVAL)
async def set_interval_prepare(message: Message, state: FSMContext):
    chat_id = int(message.chat.id)
    ensure_chat_settings(chat_id)

    await state.set_state(InactivityStates.set_interval)
    await message.reply("Введи интервал проверки (1-1440 мин):", reply_markup=back_kb())


@router.message(InactivityStates.set_interval)
async def set_interval_finish(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not re.fullmatch(r"\d+", txt):
        return await message.reply("Нужно число минут (пример: 60).", reply_markup=back_kb())

    minutes = int(txt)
    if not (1 <= minutes <= 1440):
        return await message.reply("Диапазон: 1-1440.", reply_markup=back_kb())

    chat_id = int(message.chat.id)
    s = get_chat_settings(chat_id)
    set_chat_settings(chat_id, inactivity_days=s["inactivity_days"], check_interval_minutes=minutes)

    await state.clear()
    s2 = get_chat_settings(chat_id)
    await message.reply(f"✅ Интервал обновлён: {s2['check_interval_minutes']} мин", reply_markup=main_kb())


@router.message(lambda m: m.text == BTN_SHOW_INACTIVE or m.text == BTN_CHECK_NOW)
async def show_inactive(message: Message):
    chat_id = int(message.chat.id)
    ensure_chat_settings(chat_id)
    s = get_chat_settings(chat_id)

    now_ts = int(time.time())
    rows = get_inactive_members(
        chat_id=chat_id,
        inactivity_days=s["inactivity_days"],
        now_ts=now_ts,
        limit=200,
    )

    if not rows:
        return await message.reply("👀 Сейчас нет неактивных по текущему порогу.", reply_markup=main_kb())

    lines = [f"👥 <b>Неактивные</b> (порог: {s['inactivity_days']} дней)"]
    for r in rows:
        uid = int(r["user_id"])
        uname = r["user_name"] or str(uid)
        last_ts = int(r["last_message_at"])
        age_days = int((now_ts - last_ts) / 86400)
        lines.append(f"• {uname} — {age_days} дней (last: {last_ts})")

    await message.reply("\n".join(lines), reply_markup=main_kb(), parse_mode="HTML")


@router.message(lambda m: m.text == BTN_CLEAR_ALERTS)
async def clear_alerts(message: Message):
    chat_id = int(message.chat.id)
    clear_alerts_for_chat(chat_id)
    await message.reply("🧹 Алерты для этого чата сброшены.", reply_markup=main_kb())