"""Одноразовая выгрузка участников Telegram-чата в CSV через личный аккаунт.

Установка:
    pip install -r requirements-export.txt

Перед запуском создайте API_ID и API_HASH на https://my.telegram.org,
затем задайте переменные окружения или введите значения по запросу.
"""

import asyncio
import csv
import os
import re
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import RPCError


def safe_filename(value: str) -> str:
    value = re.sub(r"[^\w\-. ]+", "_", value, flags=re.UNICODE).strip(" ._")
    return value[:80] or "telegram_chat"


async def main() -> None:
    api_id_raw = os.getenv("TELEGRAM_API_ID") or input("TELEGRAM_API_ID: ").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH") or input("TELEGRAM_API_HASH: ").strip()
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise SystemExit("TELEGRAM_API_ID должен быть числом") from exc
    if not api_hash:
        raise SystemExit("TELEGRAM_API_HASH не указан")

    session_name = os.getenv("TELEGRAM_SESSION", "lorenzo_member_export")
    async with TelegramClient(session_name, api_id, api_hash) as client:
        dialogs = []
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                dialogs.append(dialog)

        if not dialogs:
            raise SystemExit("В аккаунте не найдено доступных групп или каналов")

        print("\nДоступные чаты:")
        for index, dialog in enumerate(dialogs, start=1):
            print(f"{index:>3}. {dialog.name} ({dialog.id})")

        raw = input("\nНомер чата: ").strip()
        try:
            selected = dialogs[int(raw) - 1]
        except (ValueError, IndexError) as exc:
            raise SystemExit("Некорректный номер чата") from exc

        output = Path(f"members_{safe_filename(selected.name)}.csv")
        count = 0
        try:
            with output.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["user_id", "username", "name", "message_count"],
                )
                writer.writeheader()
                async for user in client.iter_participants(selected.entity, aggressive=True):
                    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
                    writer.writerow({
                        "user_id": user.id,
                        "username": user.username or "",
                        "name": name,
                        "message_count": 0,
                    })
                    count += 1
                    if count % 500 == 0:
                        print(f"Выгружено: {count}")
        except RPCError as exc:
            raise SystemExit(f"Telegram не разрешил получить список участников: {exc}") from exc

    print(f"\nГотово. Участников: {count}")
    print(f"Файл: {output.resolve()}")
    print("Отправьте этот CSV боту через: /admin → Активность чатов → нужный чат → Импорт участников.")


if __name__ == "__main__":
    asyncio.run(main())
