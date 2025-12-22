import os
import sqlite3
import time
from pathlib import Path
from typing import Optional, List

# БД должна жить в постоянном хранилище (volume)
DB_PATH = Path(os.getenv("DB_PATH", "/data/reminders.db")).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    """Единый способ открыть соединение к одной и той же БД."""
    return sqlite3.connect(str(DB_PATH))


def init_db() -> None:
    conn = _connect()
    try:
        cur = conn.cursor()

        # Напоминания
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                task TEXT NOT NULL,
                original TEXT NOT NULL,
                scheduled_ts INTEGER NOT NULL, -- UNIX timestamp (UTC)
                status TEXT NOT NULL DEFAULT 'pending', -- pending | sent | error
                error_text TEXT,
                created_ts INTEGER NOT NULL,
                sent_ts INTEGER
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reminders_status_ts ON reminders(status, scheduled_ts)")

        # Настройки бота (например, channel_id)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_ts INTEGER NOT NULL
            )
            """
        )

        # Пользовательские настройки времени
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                morning_time TEXT DEFAULT '09:00',
                day_time TEXT DEFAULT '14:00',
                evening_time TEXT DEFAULT '20:00',
                default_time TEXT DEFAULT '20:00',
                updated_ts INTEGER NOT NULL
            )
            """
        )

        # --- MIGRATION: add channel_id to user_settings if missing ---
        cur.execute("PRAGMA table_info(user_settings)")
        cols = [r[1] for r in cur.fetchall()]
        if "channel_id" not in cols:
            cur.execute("ALTER TABLE user_settings ADD COLUMN channel_id TEXT")


        conn.commit()
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO settings (key, value, updated_ts)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts
            """,
            (key, value.strip(), int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def get_setting(key: str) -> Optional[str]:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def add_reminder(
    task: str,
    original: str,
    scheduled_ts: int,
    user_id: Optional[int] = None,
) -> int:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO reminders (user_id, task, original, scheduled_ts, status, created_ts)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (user_id, task, original, scheduled_ts, int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def fetch_due_reminders(limit: int = 20) -> List[dict]:
    now_ts = int(time.time())
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM reminders
            WHERE status = 'pending' AND scheduled_ts <= ?
            ORDER BY scheduled_ts ASC
            LIMIT ?
            """,
            (now_ts, limit),
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_pending_reminders(user_id: Optional[int] = None, limit: int = 50) -> List[dict]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, task, original, scheduled_ts, created_ts
            FROM reminders
            WHERE status = 'pending' AND (? IS NULL OR user_id = ?)
            ORDER BY scheduled_ts ASC
            LIMIT ?
            """,
            (user_id, user_id, limit),
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_sent(reminder_id: int) -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE reminders SET status='sent', sent_ts=? WHERE id=?",
            (int(time.time()), reminder_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_error(reminder_id: int, error_text: str) -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE reminders SET status='error', error_text=? WHERE id=?",
            (error_text[:2000], reminder_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_reminder(reminder_id: int) -> bool:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def ensure_user_settings(user_id: int) -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO user_settings (user_id, updated_ts)
            VALUES (?, ?)
            """,
            (user_id, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_settings(user_id: int) -> dict:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return {}
        return dict(row)
    finally:
        conn.close()


def update_user_times(user_id: int, morning: str, day: str, evening: str, default: str) -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_settings (user_id, morning_time, day_time, evening_time, default_time, updated_ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                morning_time=excluded.morning_time,
                day_time=excluded.day_time,
                evening_time=excluded.evening_time,
                default_time=excluded.default_time,
                updated_ts=excluded.updated_ts
            """,
            (user_id, morning, day, evening, default, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def update_user_channel(user_id: int, channel_id: str) -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_settings (user_id, channel_id, updated_ts)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                channel_id=excluded.channel_id,
                updated_ts=excluded.updated_ts
            """,
            (user_id, channel_id.strip(), int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_channel(user_id: int) -> Optional[str]:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT channel_id FROM user_settings WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


def delete_reminder_for_user(reminder_id: int, user_id: int) -> bool:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM reminders WHERE id=? AND user_id=?", (reminder_id, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
