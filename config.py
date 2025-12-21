import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env (если файл есть)
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

# Опционально (на будущее)
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip() or None


def _require(name: str, value: str) -> None:
    if not value:
        raise ValueError(
            f"Переменная окружения {name} не задана. "
            f"Добавьте её в .env (или переменные окружения системы)."
        )


def validate_config(require_openai: bool = True) -> None:
    """Проверяем обязательные переменные окружения.

    Важно: не падаем при импорте модуля, чтобы можно было импортировать
    парсер/утилиты в тестах и интерактивной отладке.
    """
    _require("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    _require("CHANNEL_ID", CHANNEL_ID)
    if require_openai:
        _require("OPENAI_API_KEY", OPENAI_API_KEY)
