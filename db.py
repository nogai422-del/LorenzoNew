# db.py
import sqlite3
from typing import Optional

DB_FILE = "bot.db"


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_members (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        user_name TEXT,
        joined_at INTEGER,
        left_at INTEGER,
        last_message_at INTEGER,
        total_message_count INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (chat_id, user_id)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_settings (
        chat_id INTEGER PRIMARY KEY,
        inactivity_days INTEGER NOT NULL DEFAULT 3,
        check_interval_minutes INTEGER NOT NULL DEFAULT 60
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS inactivity_alerts (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        last_alerted_at INTEGER,
        PRIMARY KEY (chat_id, user_id)
    );
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_members_chat_last ON chat_members(chat_id, last_message_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_last ON inactivity_alerts(chat_id, user_id, last_alerted_at);")
    conn.commit()
    conn.close()


def ensure_chat_settings(chat_id: int, inactivity_days: int = 3, check_interval_minutes: int = 60):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM chat_settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO chat_settings(chat_id, inactivity_days, check_interval_minutes) VALUES (?,?,?)",
            (chat_id, inactivity_days, check_interval_minutes),
        )
        conn.commit()
    conn.close()


def get_chat_settings(chat_id: int) -> dict:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT inactivity_days, check_interval_minutes FROM chat_settings WHERE chat_id=?",
        (chat_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"inactivity_days": 3, "check_interval_minutes": 60}
    return {
        "inactivity_days": int(row["inactivity_days"]),
        "check_interval_minutes": int(row["check_interval_minutes"]),
    }


def set_chat_settings(chat_id: int, inactivity_days: int, check_interval_minutes: int):
    inactivity_days = max(1, min(3650, int(inactivity_days)))
    check_interval_minutes = max(1, min(1440, int(check_interval_minutes)))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO chat_settings(chat_id, inactivity_days, check_interval_minutes)
        VALUES (?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
            inactivity_days=excluded.inactivity_days,
            check_interval_minutes=excluded.check_interval_minutes
    """, (chat_id, inactivity_days, check_interval_minutes))
    conn.commit()
    conn.close()


def upsert_message_activity(chat_id: int, user_id: int, user_name: str, now_ts: int):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT joined_at FROM chat_members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = cur.fetchone()

    if row is None:
        joined_at = now_ts
        cur.execute("""
            INSERT INTO chat_members(
                chat_id, user_id, user_name,
                joined_at, left_at,
                last_message_at,
                total_message_count, is_active
            )
            VALUES (?, ?, ?, ?, NULL, ?, 1, 1)
        """, (chat_id, user_id, user_name, joined_at, now_ts))
    else:
        cur.execute("""
            UPDATE chat_members
            SET user_name=?,
                last_message_at=?,
                total_message_count=total_message_count+1,
                is_active=1,
                left_at=NULL
            WHERE chat_id=? AND user_id=?
        """, (user_name, now_ts, chat_id, user_id))

    conn.commit()
    conn.close()


def set_joined(chat_id: int, user_id: int, user_name: str, now_ts: int):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO chat_members(
            chat_id, user_id, user_name,
            joined_at, left_at,
            last_message_at,
            total_message_count, is_active
        )
        VALUES (?, ?, ?, ?, NULL, ?, 0, 1)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            user_name=excluded.user_name,
            joined_at=COALESCE(chat_members.joined_at, excluded.joined_at),
            is_active=1,
            left_at=NULL
    """, (chat_id, user_id, user_name, now_ts, now_ts))

    conn.commit()
    conn.close()


def set_left(chat_id: int, user_id: int, now_ts: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE chat_members
        SET left_at=?,
            is_active=0
        WHERE chat_id=? AND user_id=?
    """, (now_ts, chat_id, user_id))
    conn.commit()
    conn.close()


def get_inactive_members(chat_id: int, inactivity_days: int, now_ts: int, limit: int = 200):
    threshold = now_ts - inactivity_days * 86400
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, user_name, joined_at, last_message_at, total_message_count
        FROM chat_members
        WHERE chat_id=?
          AND is_active=1
          AND last_message_at IS NOT NULL
          AND last_message_at <= ?
        ORDER BY last_message_at ASC
        LIMIT ?
    """, (chat_id, threshold, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def should_alert(chat_id: int, user_id: int, now_ts: int, min_repeat_hours: int = 24) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT last_alerted_at
        FROM inactivity_alerts
        WHERE chat_id=? AND user_id=?
    """, (chat_id, user_id))
    row = cur.fetchone()
    conn.close()

    if row is None:
        return True
    last = int(row["last_alerted_at"])
    return (now_ts - last) >= min_repeat_hours * 3600


def mark_alert(chat_id: int, user_id: int, now_ts: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO inactivity_alerts(chat_id, user_id, last_alerted_at)
        VALUES (?,?,?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET last_alerted_at=excluded.last_alerted_at
    """, (chat_id, user_id, now_ts))
    conn.commit()
    conn.close()


def clear_alerts_for_chat(chat_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM inactivity_alerts WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def list_chat_ids_with_settings() -> list[int]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM chat_settings;")
    rows = cur.fetchall()
    conn.close()
    return [int(r["chat_id"]) for r in rows]


def get_chat_member_stats(chat_id: int, user_id: int) -> Optional[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT joined_at, left_at, last_message_at, total_message_count, is_active, user_name
        FROM chat_members
        WHERE chat_id=? AND user_id=?
    """, (chat_id, user_id))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)