"""Mini self-test for PlannerBot.

Run:
  python selftest.py

Does NOT call Telegram. Checks:
- config validation (optional)
- reminder splitter
- parser on simple cases
- sqlite write/read

Note: "через 2 часа" style parsing typically requires OpenAI.
"""

from __future__ import annotations

import time

from config import OPENAI_API_KEY, validate_config
from parser import parse_text, split_into_reminders
from utils import add_reminder, delete_reminder, fetch_pending_reminders, init_db


def _banner(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def main() -> None:
    _banner("1) Config")
    # В проде лучше require_openai=True.
    # В self-test не валим скрипт, если ключа нет.
    validate_config(require_openai=False)
    print("OK: TELEGRAM_BOT_TOKEN and CHANNEL_ID are present.")
    print("OPENAI_API_KEY:", "present" if OPENAI_API_KEY else "missing (AI parsing tests skipped)")

    _banner("2) Splitter")
    voice_like = (
        "Надо через две минуты проверить уведомление, "
        "через пять минут посмотреть почту и через два часа купить хлеб."
    )
    split = split_into_reminders(voice_like)
    print("INPUT:", voice_like)
    print("SPLIT:", split)

    _banner("3) Parser (local rules)")
    user_times = {"morning": "08:00", "day": "13:00", "evening": "18:00", "default": "21:30"}
    for s in ("позвонить папе утром", "в 11:45 встреча", "купить молоко"):
        print("-", s, "->", parse_text(s, user_times=user_times))

    _banner("4) Parser (OpenAI-required examples)")
    if OPENAI_API_KEY:
        for s in ("через 2 часа купить хлеб", "в субботу в 10 утра сдать отчет"):
            print("-", s, "->", parse_text(s, user_times=user_times))
    else:
        print("SKIPPED (no OPENAI_API_KEY)")

    _banner("5) SQLite")
    init_db()
    scheduled_ts = int(time.time()) + 3600
    rid = add_reminder(task="selftest", original="selftest", scheduled_ts=scheduled_ts, user_id=0)
    rows = fetch_pending_reminders(limit=20)
    assert any(r["id"] == rid for r in rows), "Reminder not found in pending list"
    print(f"Inserted reminder id={rid} and found it in pending list.")
    assert delete_reminder(rid), "Failed to delete reminder"
    print("Deleted reminder OK.")

    _banner("DONE")
    print("All checks passed.")


if __name__ == "__main__":
    main()
