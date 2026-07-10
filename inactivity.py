import asyncio
import html
import time
from datetime import datetime

from db import (
    get_alert_candidates,
    get_chat_info,
    get_chat_settings,
    list_chat_ids_with_settings,
    mark_alert,
    mark_chat_checked,
    record_chat,
    remove_member,
    should_alert,
)


def format_dt(ts: int | None) -> str:
    if not ts:
        return "сообщений ещё не было"
    return datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y %H:%M")


def build_message_url(chat_id: int, chat_username: str | None, message_id: int | None) -> str | None:
    if not message_id:
        return None
    if chat_username:
        return f"https://t.me/{chat_username}/{int(message_id)}"
    raw = str(chat_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{int(message_id)}"
    return None


async def _is_still_member(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status not in {"left", "kicked"}
    except Exception:
        # Ошибка API не должна удалять живого участника.
        return True


async def check_chat_now(bot, chat_id: int, admin_user_ids: list[int], botlog, force: bool = False) -> int:
    now_ts = int(time.time())
    settings = get_chat_settings(chat_id)
    if not settings["enabled"] and not force:
        return 0

    if not force:
        due_at = int(settings["last_check_at"]) + int(settings["check_interval_minutes"]) * 60
        if now_ts < due_at:
            return 0

    try:
        chat = await bot.get_chat(chat_id)
        chat_title = chat.title or chat.username or str(chat_id)
        chat_username = getattr(chat, "username", None)
        record_chat(chat_id, chat.title, chat_username, chat.type, now_ts)
    except Exception:
        info = get_chat_info(chat_id) or {}
        chat_title = info.get("title") or (f"@{info['username']}" if info.get("username") else str(chat_id))
        chat_username = info.get("username")

    rows = get_alert_candidates(
        chat_id=chat_id,
        inactivity_days=settings["inactivity_days"],
        min_message_count=settings["min_message_count"],
        now_ts=now_ts,
    )
    sent = 0
    threshold = now_ts - int(settings["inactivity_days"]) * 86400

    for row in rows:
        user_id = int(row["user_id"])
        if not await _is_still_member(bot, chat_id, user_id):
            remove_member(chat_id, user_id)
            continue

        last_activity = int(row["last_message_at"] or row["joined_at"] or 0)
        inactive = bool(last_activity and last_activity <= threshold)
        low_messages = bool(
            int(settings["min_message_count"]) > 0
            and int(row["total_message_count"] or 0) < int(settings["min_message_count"])
            and int(row["joined_at"] or 0) <= threshold
        )
        alert_types = []
        if inactive:
            alert_types.append("inactivity")
        if low_messages:
            alert_types.append("message_count")
        if not alert_types:
            continue

        due_types = [t for t in alert_types if should_alert(
            chat_id, user_id, t, now_ts, settings["repeat_alert_hours"]
        )]
        if not due_types:
            continue

        name = html.escape(row["user_name"] or str(user_id))
        user_link = f'<a href="tg://user?id={user_id}">{name}</a>'
        reasons = []
        if "inactivity" in due_types:
            days = max(0, (now_ts - last_activity) // 86400)
            reasons.append(f"нет сообщений: <b>{days} дн.</b>")
        if "message_count" in due_types:
            reasons.append(
                f"сообщений: <b>{int(row['total_message_count'] or 0)}</b> "
                f"из требуемых <b>{int(settings['min_message_count'])}</b>"
            )

        msg_url = build_message_url(chat_id, chat_username, row["last_message_id"])
        last_line = format_dt(row["last_message_at"])
        if msg_url:
            last_line = f'<a href="{msg_url}">{last_line}</a>'

        text = (
            f"⚠️ <b>Проверка активности</b>\n"
            f"Чат: <b>{html.escape(chat_title)}</b>\n"
            f"Участник: {user_link}\n"
            f"Причина: {'; '.join(reasons)}\n"
            f"Последнее сообщение: {last_line}"
        )

        delivered = False
        for admin_id in admin_user_ids:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML", disable_web_page_preview=True)
                delivered = True
            except Exception as exc:
                await botlog(f"Cannot send inactivity alert to {admin_id}: {exc}")

        if delivered:
            for alert_type in due_types:
                mark_alert(chat_id, user_id, alert_type, now_ts)
            sent += 1

    mark_chat_checked(chat_id, now_ts)
    return sent


async def inactivity_watcher(bot, admin_user_ids: list[int], botlog, sleep_seconds: int = 30):
    """Лёгкий цикл; фактическая частота каждой группы берётся из её настроек."""
    while True:
        try:
            for chat_id in list_chat_ids_with_settings():
                try:
                    await check_chat_now(bot, chat_id, admin_user_ids, botlog)
                except Exception as exc:
                    await botlog(f"inactivity check chat={chat_id} error: {exc}")
        except Exception as exc:
            await botlog(f"inactivity_watcher error: {exc}")
        await asyncio.sleep(max(10, int(sleep_seconds)))


async def send_test_inactivity_alert(bot, recipient_id: int, chat_id: int) -> None:
    """Отправляет безопасный тест только нажавшему админу, не меняя историю алертов."""
    info = get_chat_info(chat_id) or {}
    chat_title = info.get("title") or (f"@{info['username']}" if info.get("username") else str(chat_id))
    text = (
        "🧪 <b>Тестовое оповещение о неактиве</b>\n"
        f"Чат: <b>{html.escape(chat_title)}</b>\n"
        "Участник: <a href=\"tg://user?id=1\">Тестовый участник</a>\n"
        "Причина: нет сообщений: <b>7 дн.</b>\n"
        "Последнее сообщение: тестовая ссылка не создаётся\n\n"
        "✅ Канал доставки оповещений работает. Это сообщение не записано в историю алертов."
    )
    await bot.send_message(recipient_id, text, parse_mode="HTML", disable_web_page_preview=True)
