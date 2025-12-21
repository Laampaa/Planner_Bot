import os
from openai import OpenAI

from config import OPENAI_API_KEY


def recognize_audio(audio_path: str) -> str:
    """
    Распознаёт речь в аудиофайле и возвращает текст.
    Поддерживаются форматы типа ogg/webm/wav/mp3 (Telegram voice обычно ogg).
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в .env")

    client = OpenAI(api_key=OPENAI_API_KEY)

    with open(audio_path, "rb") as f:
        # Самый бюджетный и качественный вариант под твою задачу:
        # gpt-4o-mini-transcribe (STT)
        resp = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=f,
        )

    # В SDK это обычно resp.text
    text = getattr(resp, "text", None)
    if not text:
        raise RuntimeError("Пустой результат распознавания")
    return text.strip()
