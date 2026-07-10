"""Выгрузка участников Telegram-чата в CSV через личный аккаунт.

CSV сохраняется в папку ``exports`` рядом со скриптом и содержит доступный
Telegram-статус пользователя. Точное время последнего онлайна доступно не для
всех участников из-за настроек приватности Telegram.
"""

import asyncio
import csv
import os
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.types import (
    UserStatusEmpty,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

BASE_DIR = Path(__file__).resolve().parent
EXPORT_DIR = BASE_DIR / "exports"


def safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
    return value[:80] or "telegram_chat"


def to_timestamp(value) -> int | None:
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def status_snapshot(user, checked_at: int) -> tuple[str, int | None]:
    status = getattr(user, "status", None)
    if isinstance(status, UserStatusOnline):
        # Пользователь онлайн в момент выгрузки; фиксируем время снимка.
        return "online", checked_at
    if isinstance(status, UserStatusOffline):
        return "offline", to_timestamp(status.was_online)
    if isinstance(status, UserStatusRecently):
        return "recently", None
    if isinstance(status, UserStatusLastWeek):
        return "last_week", None
    if isinstance(status, UserStatusLastMonth):
        return "last_month", None
    if isinstance(status, UserStatusEmpty) or status is None:
        return "hidden", None
    return "unknown", None


async def export_members() -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    api_id_raw = os.getenv("TELEGRAM_API_ID") or input("TELEGRAM_API_ID: ").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH") or input("TELEGRAM_API_HASH: ").strip()
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_API_ID должен быть числом") from exc
    if not api_hash:
        raise RuntimeError("TELEGRAM_API_HASH не указан")

    session_path = BASE_DIR / os.getenv("TELEGRAM_SESSION", "lorenzo_member_export")
    async with TelegramClient(str(session_path), api_id, api_hash) as client:
        dialogs = []
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                dialogs.append(dialog)

        if not dialogs:
            raise RuntimeError("В аккаунте не найдено доступных групп или каналов")

        print("\nДоступные чаты:")
        for index, dialog in enumerate(dialogs, start=1):
            print(f"{index:>3}. {dialog.name} ({dialog.id})")

        raw = input("\nНомер чата: ").strip()
        try:
            selected = dialogs[int(raw) - 1]
        except (ValueError, IndexError) as exc:
            raise RuntimeError("Некорректный номер чата") from exc

        checked_at = int(time.time())
        output = EXPORT_DIR / f"members_{safe_filename(selected.name)}.csv"
        count = 0
        exact_count = 0
        coarse_count = 0

        try:
            with output.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "user_id", "username", "name", "message_count",
                        "telegram_status", "telegram_last_seen_at",
                        "telegram_status_checked_at",
                    ],
                )
                writer.writeheader()
                async for user in client.iter_participants(selected.entity, aggressive=True):
                    name = " ".join(
                        part for part in (user.first_name, user.last_name) if part
                    ).strip()
                    status, last_seen_at = status_snapshot(user, checked_at)
                    if last_seen_at:
                        exact_count += 1
                    elif status in {"recently", "last_week", "last_month"}:
                        coarse_count += 1
                    writer.writerow({
                        "user_id": user.id,
                        "username": user.username or "",
                        "name": name,
                        "message_count": 0,
                        "telegram_status": status,
                        "telegram_last_seen_at": last_seen_at or "",
                        "telegram_status_checked_at": checked_at,
                    })
                    count += 1
                    if count % 100 == 0:
                        print(f"Выгружено участников: {count}")
        except RPCError as exc:
            try:
                output.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError(f"Telegram не разрешил получить список участников: {exc}") from exc

    print("\nВыгрузка завершена.")
    print(f"Участников: {count}")
    print(f"С точным временем онлайна: {exact_count}")
    print(f"С приблизительным статусом: {coarse_count}")
    print(f"CSV-файл: {output}")
    print("Импортируйте файл: /admin → Активность чатов → нужный чат → Импорт участников.")


def main() -> None:
    try:
        asyncio.run(export_members())
    except KeyboardInterrupt:
        print("\nОперация отменена")
    except Exception as exc:
        print(f"\nОШИБКА: {exc}")
        traceback.print_exc()
    finally:
        input("\nНажмите Enter, чтобы закрыть окно...")


if __name__ == "__main__":
    main()
