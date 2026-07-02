# inactivity.py
import asyncio
import time
from datetime import datetime

from db import (
    list_chat_ids_with_settings,
    get_chat_settings,
    get_inactive_members,
    should_alert,
    mark_alert,
)

def format_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


async def inactivity_watcher(bot, admin_user_ids: list[int], botlog, sleep_seconds: int = 60):
    """
    Проверяет все чаты из chat_settings и шлёт уведомления admin_user_ids.
    Антиспам: не чаще раза в 24 часа на одного участника в одном чате.
    """
    while True:
        try:
            now_ts = int(time.time())
            chat_ids = list_chat_ids_with_settings()

            for chat_id in chat_ids:
                s = get_chat_settings(chat_id)
                inactivity_days = int(s["inactivity_days"])

                inactive_rows = get_inactive_members(
                    chat_id=chat_id,
                    inactivity_days=inactivity_days,
                    now_ts=now_ts,
                    limit=200,
                )

                if not inactive_rows:
                    continue

                for r in inactive_rows:
                    user_id = int(r["user_id"])
                    user_name = r["user_name"] or str(user_id)
                    last_msg_ts = int(r["last_message_at"])

                    if not should_alert(chat_id, user_id, now_ts, min_repeat_hours=24):
                        continue

                    age_days = int((now_ts - last_msg_ts) / 86400)
                    link = f'<a href="tg://user?id={user_id}">{user_name}</a>'

                    text = (
                        f"⚠️ Неактивен в чате <code>{chat_id}</code>\n"
                        f"Юзер: {link}\n"
                        f"Последнее сообщение: {format_dt(last_msg_ts)}\n"
                        f"Неактивен: {age_days} дней"
                    )

                    for aid in admin_user_ids:
                        try:
                            await bot.send_message(aid, text, parse_mode="HTML")
                        except Exception:
                            pass

                    mark_alert(chat_id, user_id, now_ts)

        except Exception as e:
            await botlog(f"inactivity_watcher error: {e}")

        await asyncio.sleep(sleep_seconds)