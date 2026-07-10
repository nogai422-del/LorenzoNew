import sqlite3
from typing import Optional

DB_FILE = "bot.db"

DEFAULT_SETTINGS = {
    "inactivity_days": 3,
    "check_interval_minutes": 60,
    "repeat_alert_hours": 24,
    "min_message_count": 0,
    "enabled": 1,
    "last_check_at": 0,
}


def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table: str, name: str, sql_type_and_default: str):
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type_and_default}")


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                chat_type TEXT,
                last_seen_at INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_members (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                username TEXT,
                joined_at INTEGER,
                left_at INTEGER,
                last_message_at INTEGER,
                last_message_id INTEGER,
                telegram_last_seen_at INTEGER,
                telegram_status TEXT,
                telegram_status_checked_at INTEGER,
                total_message_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                inactivity_days INTEGER NOT NULL DEFAULT 3,
                check_interval_minutes INTEGER NOT NULL DEFAULT 60,
                repeat_alert_hours INTEGER NOT NULL DEFAULT 24,
                min_message_count INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_check_at INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inactivity_alerts (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                alert_type TEXT NOT NULL DEFAULT 'inactivity',
                last_alerted_at INTEGER,
                PRIMARY KEY (chat_id, user_id, alert_type)
            )
        """)

        # Миграции старой базы.
        _add_column(conn, "chat_members", "username", "TEXT")
        _add_column(conn, "chat_members", "last_message_id", "INTEGER")
        _add_column(conn, "chat_members", "telegram_last_seen_at", "INTEGER")
        _add_column(conn, "chat_members", "telegram_status", "TEXT")
        _add_column(conn, "chat_members", "telegram_status_checked_at", "INTEGER")
        _add_column(conn, "chat_settings", "repeat_alert_hours", "INTEGER NOT NULL DEFAULT 24")
        _add_column(conn, "chat_settings", "min_message_count", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "chat_settings", "enabled", "INTEGER NOT NULL DEFAULT 1")
        _add_column(conn, "chat_settings", "last_check_at", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "inactivity_alerts", "alert_type", "TEXT NOT NULL DEFAULT 'inactivity'")

        # В старой версии первичный ключ был только (chat_id, user_id).
        # Пересоздаём таблицу, чтобы отдельно хранить алерты отсутствия и малого числа сообщений.
        alert_pk = [row["name"] for row in conn.execute("PRAGMA table_info(inactivity_alerts)") if row["pk"]]
        if alert_pk != ["chat_id", "user_id", "alert_type"]:
            conn.execute("""
                CREATE TABLE inactivity_alerts_new (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    alert_type TEXT NOT NULL DEFAULT 'inactivity',
                    last_alerted_at INTEGER,
                    PRIMARY KEY (chat_id, user_id, alert_type)
                )
            """)
            conn.execute("""
                INSERT OR REPLACE INTO inactivity_alerts_new(chat_id, user_id, alert_type, last_alerted_at)
                SELECT chat_id, user_id, COALESCE(alert_type, 'inactivity'), last_alerted_at
                FROM inactivity_alerts
            """)
            conn.execute("DROP TABLE inactivity_alerts")
            conn.execute("ALTER TABLE inactivity_alerts_new RENAME TO inactivity_alerts")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_members_chat_active_last ON chat_members(chat_id, is_active, last_message_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_last ON inactivity_alerts(chat_id, user_id, last_alerted_at)")
        conn.commit()


def record_chat(chat_id: int, title: str | None, username: str | None, chat_type: str | None, now_ts: int):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO chats(chat_id, title, username, chat_type, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=COALESCE(excluded.title, chats.title),
                username=COALESCE(excluded.username, chats.username),
                chat_type=COALESCE(excluded.chat_type, chats.chat_type),
                last_seen_at=excluded.last_seen_at
        """, (int(chat_id), title, username, chat_type, int(now_ts)))
        conn.commit()


def list_known_chats() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("""
            SELECT c.chat_id, c.title, c.username, c.chat_type, c.last_seen_at,
                   COALESCE(s.enabled, 1) AS enabled
            FROM chats c
            LEFT JOIN chat_settings s ON s.chat_id=c.chat_id
            ORDER BY c.last_seen_at DESC, c.title COLLATE NOCASE
        """).fetchall()


def get_chat_info(chat_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chats WHERE chat_id=?", (int(chat_id),)).fetchone()
    return dict(row) if row else None


def ensure_chat_settings(chat_id: int):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO chat_settings(chat_id) VALUES (?)", (int(chat_id),))
        conn.commit()


def get_chat_settings(chat_id: int) -> dict:
    ensure_chat_settings(chat_id)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_settings WHERE chat_id=?", (int(chat_id),)).fetchone()
    result = dict(DEFAULT_SETTINGS)
    if row:
        result.update(dict(row))
    result["chat_id"] = int(chat_id)
    return result


def set_chat_settings(chat_id: int, **changes):
    current = get_chat_settings(chat_id)
    values = {
        "inactivity_days": max(1, min(3650, int(changes.get("inactivity_days", current["inactivity_days"])))),
        "check_interval_minutes": max(1, min(10080, int(changes.get("check_interval_minutes", current["check_interval_minutes"])))),
        "repeat_alert_hours": max(1, min(8760, int(changes.get("repeat_alert_hours", current["repeat_alert_hours"])))),
        "min_message_count": max(0, min(1_000_000, int(changes.get("min_message_count", current["min_message_count"])))),
        "enabled": 1 if bool(changes.get("enabled", current["enabled"])) else 0,
        "last_check_at": max(0, int(changes.get("last_check_at", current["last_check_at"]))),
    }
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO chat_settings(
                chat_id, inactivity_days, check_interval_minutes, repeat_alert_hours,
                min_message_count, enabled, last_check_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                inactivity_days=excluded.inactivity_days,
                check_interval_minutes=excluded.check_interval_minutes,
                repeat_alert_hours=excluded.repeat_alert_hours,
                min_message_count=excluded.min_message_count,
                enabled=excluded.enabled,
                last_check_at=excluded.last_check_at
        """, (int(chat_id), values["inactivity_days"], values["check_interval_minutes"],
              values["repeat_alert_hours"], values["min_message_count"], values["enabled"],
              values["last_check_at"]))
        conn.commit()


def mark_chat_checked(chat_id: int, now_ts: int):
    set_chat_settings(chat_id, last_check_at=now_ts)


def upsert_message_activity(chat_id: int, user_id: int, user_name: str, now_ts: int,
                            message_id: int | None = None, username: str | None = None):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO chat_members(
                chat_id, user_id, user_name, username, joined_at, left_at,
                last_message_at, last_message_id, total_message_count, is_active
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 1, 1)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                user_name=excluded.user_name,
                username=excluded.username,
                last_message_at=excluded.last_message_at,
                last_message_id=excluded.last_message_id,
                total_message_count=chat_members.total_message_count+1,
                is_active=1,
                left_at=NULL
        """, (int(chat_id), int(user_id), user_name, username, int(now_ts), int(now_ts), message_id))
        # Новая активность должна позволить новое оповещение только после повторного достижения порога.
        conn.execute("DELETE FROM inactivity_alerts WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
        conn.commit()



def import_member(chat_id: int, user_id: int, user_name: str | None, username: str | None,
                  joined_at: int, message_count: int = 0,
                  telegram_last_seen_at: int | None = None, telegram_status: str | None = None,
                  telegram_status_checked_at: int | None = None):
    """Добавляет подтверждённого участника из внешнего списка без искусственного сообщения."""
    joined_at = max(0, int(joined_at))
    message_count = max(0, int(message_count or 0))
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO chat_members(
                chat_id, user_id, user_name, username, joined_at, left_at,
                last_message_at, last_message_id, telegram_last_seen_at, telegram_status,
                telegram_status_checked_at, total_message_count, is_active
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                user_name=COALESCE(excluded.user_name, chat_members.user_name),
                username=COALESCE(excluded.username, chat_members.username),
                joined_at=COALESCE(chat_members.joined_at, excluded.joined_at),
                telegram_last_seen_at=CASE
                    WHEN excluded.telegram_last_seen_at IS NULL THEN chat_members.telegram_last_seen_at
                    ELSE MAX(COALESCE(chat_members.telegram_last_seen_at, 0), excluded.telegram_last_seen_at)
                END,
                telegram_status=COALESCE(excluded.telegram_status, chat_members.telegram_status),
                telegram_status_checked_at=COALESCE(excluded.telegram_status_checked_at, chat_members.telegram_status_checked_at),
                total_message_count=MAX(chat_members.total_message_count, excluded.total_message_count),
                left_at=NULL,
                is_active=1
        """, (int(chat_id), int(user_id), user_name, username, joined_at,
              int(telegram_last_seen_at) if telegram_last_seen_at else None,
              telegram_status,
              int(telegram_status_checked_at) if telegram_status_checked_at else None,
              message_count))
        conn.commit()

def set_joined(chat_id: int, user_id: int, user_name: str, now_ts: int, username: str | None = None):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO chat_members(
                chat_id, user_id, user_name, username, joined_at, left_at,
                last_message_at, last_message_id, total_message_count, is_active
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 0, 1)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                user_name=excluded.user_name,
                username=excluded.username,
                joined_at=excluded.joined_at,
                left_at=NULL,
                is_active=1
        """, (int(chat_id), int(user_id), user_name, username, int(now_ts)))
        conn.execute("DELETE FROM inactivity_alerts WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
        conn.commit()


def remove_member(chat_id: int, user_id: int):
    """Полностью удаляет ушедшего/кикнутого/забаненного из рабочей базы."""
    with get_conn() as conn:
        conn.execute("DELETE FROM inactivity_alerts WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
        conn.execute("DELETE FROM chat_members WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
        conn.commit()


def set_left(chat_id: int, user_id: int, now_ts: int):
    # Сохраняем сигнатуру старой функции, но больше не держим бывших участников.
    remove_member(chat_id, user_id)



def list_active_members(chat_id: int, limit: int = 100000):
    """Возвращает всех известных активных участников для сверки с Telegram."""
    with get_conn() as conn:
        return conn.execute("""
            SELECT user_id, user_name, username, joined_at, last_message_at,
                   last_message_id, telegram_last_seen_at, telegram_status,
                   telegram_status_checked_at, total_message_count
            FROM chat_members
            WHERE chat_id=? AND is_active=1
            ORDER BY user_id
            LIMIT ?
        """, (int(chat_id), int(limit))).fetchall()


def count_active_members(chat_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM chat_members WHERE chat_id=? AND is_active=1",
            (int(chat_id),),
        ).fetchone()
    return int(row["count"] or 0)

def get_alert_candidates(chat_id: int, inactivity_days: int, min_message_count: int,
                         now_ts: int, limit: int = 500):
    threshold = int(now_ts) - int(inactivity_days) * 86400
    with get_conn() as conn:
        return conn.execute("""
            SELECT user_id, user_name, username, joined_at, last_message_at,
                   last_message_id, telegram_last_seen_at, telegram_status,
                   telegram_status_checked_at, total_message_count
            FROM chat_members
            WHERE chat_id=? AND is_active=1
              AND (
                    (MAX(COALESCE(last_message_at, 0), COALESCE(telegram_last_seen_at, 0), COALESCE(joined_at, 0)) > 0
                     AND MAX(COALESCE(last_message_at, 0), COALESCE(telegram_last_seen_at, 0), COALESCE(joined_at, 0)) <= ?)
                 OR (? > 0 AND total_message_count < ? AND COALESCE(joined_at, 0) <= ?)
              )
            ORDER BY MAX(COALESCE(last_message_at, 0), COALESCE(telegram_last_seen_at, 0), COALESCE(joined_at, 0)) ASC
            LIMIT ?
        """, (int(chat_id), threshold, int(min_message_count), int(min_message_count), threshold, int(limit))).fetchall()


def get_inactive_members(chat_id: int, inactivity_days: int, now_ts: int, limit: int = 200):
    return get_alert_candidates(chat_id, inactivity_days, 0, now_ts, limit)


def should_alert(chat_id: int, user_id: int, alert_type: str, now_ts: int, repeat_hours: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT last_alerted_at FROM inactivity_alerts
            WHERE chat_id=? AND user_id=? AND alert_type=?
        """, (int(chat_id), int(user_id), alert_type)).fetchone()
    return row is None or int(now_ts) - int(row["last_alerted_at"] or 0) >= int(repeat_hours) * 3600


def mark_alert(chat_id: int, user_id: int, alert_type: str, now_ts: int):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO inactivity_alerts(chat_id, user_id, alert_type, last_alerted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id, alert_type)
            DO UPDATE SET last_alerted_at=excluded.last_alerted_at
        """, (int(chat_id), int(user_id), alert_type, int(now_ts)))
        conn.commit()


def clear_alerts_for_chat(chat_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM inactivity_alerts WHERE chat_id=?", (int(chat_id),))
        conn.commit()


def list_chat_ids_with_settings() -> list[int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT chat_id FROM chat_settings ORDER BY chat_id").fetchall()
    return [int(r["chat_id"]) for r in rows]


def get_chat_member_stats(chat_id: int, user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_members WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id))).fetchone()
    return dict(row) if row else None
