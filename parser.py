import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List

import pytz

# openai — опциональная зависимость: локальные правила работают и без неё.
try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore

from config import OPENAI_API_KEY

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


def _now_moscow() -> datetime:
    return datetime.now(MOSCOW_TZ)


def _now_moscow_str() -> str:
    return _now_moscow().strftime("%Y-%m-%d %H:%M:%S")


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _clean_task(text: str) -> str:
    t = text.strip()
    t = re.sub(r"\s+", " ", t)
    t = t.strip(" .,!?:;—-")
    words = t.split()
    if len(words) > 7:
        t = " ".join(words[:7])
    return t


def _normalize_hhmm(value: str, fallback: str) -> str:
    """
    Приводит к формату HH:MM. Если невалидно — возвращает fallback.
    """
    if not value:
        return fallback
    v = value.strip()

    # уже HH:MM
    if re.fullmatch(r"\d{2}:\d{2}", v):
        hh, mm = int(v[:2]), int(v[3:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return v
        return fallback

    # H:MM или HH.MM
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", v)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    return fallback


def _get_times(user_times: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """
    Достаёт пользовательские времена (с дефолтами).
    user_times приходит из utils.get_user_settings(user_id)
    """
    user_times = user_times or {}
    morning = _normalize_hhmm(str(user_times.get("morning_time", "")), "09:00")
    day = _normalize_hhmm(str(user_times.get("day_time", "")), "14:00")
    evening = _normalize_hhmm(str(user_times.get("evening_time", "")), "20:00")
    default = _normalize_hhmm(str(user_times.get("default_time", "")), "20:00")
    return {"morning": morning, "day": day, "evening": evening, "default": default}


def _default_datetime_str(default_time_hhmm: str) -> str:
    """
    По умолчанию: сегодня default_time, но если уже позже — завтра.
    """
    now = _now_moscow()
    hh, mm = int(default_time_hhmm[:2]), int(default_time_hhmm[3:])
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.strftime("%Y-%m-%d %H:%M:%S")


def _apply_time_on_date(base_dt: datetime, hhmm: str) -> datetime:
    hh, mm = int(hhmm[:2]), int(hhmm[3:])
    return base_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _is_day_month_pattern(user_text: str) -> bool:
    """
    Эвристика: "1 января", "12.01", "12/01", "12-01" → день+месяц без года.
    """
    t = user_text.lower()

    months = [
        "январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август",
        "сентябр", "октябр", "ноябр", "декабр"
    ]
    if re.search(r"\b\d{1,2}\s+(" + "|".join(months) + r")", t):
        return True

    if re.search(r"\b\d{1,2}[./-]\d{1,2}\b", t):
        return True

    return False


def _fix_past_datetime(dt_str: str, user_text: str, default_time_hhmm: str) -> str:
    """
    Делает дату будущей, если модель вернула прошедшую.
    - если похоже на день+месяц без года -> +1 год
    - иначе -> безопасный дефолт (сегодня/завтра default_time)
    """
    now = _now_moscow()

    try:
        dt_naive = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        dt_msk = MOSCOW_TZ.localize(dt_naive)
    except Exception:
        return _default_datetime_str(default_time_hhmm)

    if dt_msk > now:
        return dt_str

    if _is_day_month_pattern(user_text):
        try:
            dt_msk2 = dt_msk.replace(year=dt_msk.year + 1)
            if dt_msk2 > now:
                return dt_msk2.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    return _default_datetime_str(default_time_hhmm)


def _looks_like_datetime_text(user_text: str) -> bool:
    """
    Признаки того, что в тексте есть дата/время/относительность.
    """
    t = user_text.lower()

    if re.search(r"\d", t):
        return True

    keywords = [
        "сегодня", "завтра", "послезавтра", "через",
        "утром", "утра", "днём", "днем", "дня", "вечером", "вечера", "ночью",
        "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
        "пн", "вт", "ср", "чт", "пт", "сб", "вс",
        "январ", "феврал", "март", "апрел", "мая", "июн", "июл", "август",
        "сентябр", "октябр", "ноябр", "декабр",
        "пол", "полвосьм", "половин",
    ]
    return any(k in t for k in keywords)


def _try_parse_explicit_time(user_text: str, default_time_hhmm: str) -> Optional[Dict[str, Any]]:
    """
    Локально ловим явное время вида 11:45 или 11.45.
    Ставим сегодня это время, а если уже прошло — завтра.
    """
    t = user_text.strip()
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", t)
    if not m:
        return None

    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None

    now = _now_moscow()
    dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if dt <= now:
        dt = dt + timedelta(days=1)

    task_text = re.sub(r"\bв\s*\d{1,2}[:.]\d{2}\b", "", t, flags=re.IGNORECASE)
    task_text = _clean_task(task_text)

    return {
        "task": task_text if task_text else _clean_task(t),
        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "original": user_text,
        "error": None,
    }


def _try_parse_simple_dayparts(user_text: str, times: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """
    Локально обрабатываем простые случаи:
    - "позвонить папе утром" -> сегодня morning_time (или завтра, если уже прошло)
    - "днём", "вечером"
    Без обращения к OpenAI.
    """
    t = user_text.lower()

    # если есть цифры — не лезем сюда (там лучше explicit/openai)
    if re.search(r"\d", t):
        return None

    now = _now_moscow()
    base = now

    # простая поддержка "завтра/послезавтра"
    if "послезавтра" in t:
        base = now + timedelta(days=2)
    elif "завтра" in t:
        base = now + timedelta(days=1)

    chosen: Optional[str] = None
    if "утром" in t or "утра" in t:
        chosen = times["morning"]
    elif "днём" in t or "днем" in t:
        chosen = times["day"]
    elif "вечером" in t or "вечера" in t:
        chosen = times["evening"]

    if not chosen:
        return None

    dt = _apply_time_on_date(base, chosen)
    if dt <= now:
        dt = dt + timedelta(days=1)

    # чистим task: убираем слова времени
    task_text = re.sub(r"\b(сегодня|завтра|послезавтра)\b", "", user_text, flags=re.IGNORECASE)
    task_text = re.sub(r"\b(утром|утра|днём|днем|вечером|вечера)\b", "", task_text, flags=re.IGNORECASE)
    task_text = _clean_task(task_text)

    return {
        "task": task_text if task_text else _clean_task(user_text),
        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "original": user_text,
        "error": None,
    }


def _build_prompt(user_text: str, times: Dict[str, str]) -> str:
    """
    ⚠️ Важно: это f-string, поэтому все фигурные скобки JSON внутри должны быть {{ }}.
    """
    current_time = _now_moscow_str()

    prompt = f"""
Ты — умный парсер напоминаний. Извлекай из текста СУТЬ задачи и ВРЕМЯ исполнения.

Отвечай ТОЛЬКО в формате JSON без каких-либо пояснений:
{{
  "task": "краткая суть задачи (3-7 слов, без дат/времени)",
  "datetime": "ГГГГ-ММ-ДД ЧЧ:ММ:СС",
  "original": "оригинальный текст"
}}

ПРАВИЛА:
1. Текущее время: {current_time} (Москва)
2. Если время не указано — используй время по умолчанию: {times["default"]}:00
3. Если дата не указана — используй сегодня
4. Все даты должны быть БУДУЩИМИ относительно текущего времени
5. Интерпретируй слова:
   - "завтра" = текущая дата + 1 день
   - "послезавтра" = +2 дня
   - "через N часов/дней" = прибавь указанное время
   - "в [день недели]" = ближайший этот день в будущем
6. Части дня (используй НАСТРОЙКИ пользователя):
   - "утром" = {times["morning"]}:00
   - "днём" = {times["day"]}:00
   - "вечером" = {times["evening"]}:00
7. Разговорные формулировки времени:
   - "в пол 8" / "в полвосьмого" = 19:30:00, если сейчас после 12:00 (Москва), иначе 07:30:00
   - "в пол 8 утра" = 07:30:00
   - "в пол 8 вечера" = 19:30:00
   - "в половину восьмого" = 07:30:00
8. Если не можешь определить дату — верни null в поле datetime

Примеры:
- "купить молоко" → {{"task": "купить молоко", "datetime": "2024-12-12 20:00:00", "original":"купить молоко"}}
- "завтра в 10 утра сдать отчёт" → {{"task": "сдать отчёт", "datetime": "2024-12-13 10:00:00", "original":"завтра в 10 утра сдать отчёт"}}
- "позвонить маме в субботу" → {{"task": "позвонить маме", "datetime": "2024-12-16 20:00:00", "original":"позвонить маме в субботу"}}
- "в пол 8 позвонить папе" → {{"task":"позвонить папе","datetime":"2024-12-12 19:30:00","original":"в пол 8 позвонить папе"}}

Текст пользователя: {user_text}
""".strip()

    return prompt


def _parse_with_openai(user_text: str, times: Dict[str, str]) -> Dict[str, Any]:
    if OpenAI is None:
        return {"error": "Библиотека openai не установлена. Установите зависимости из requirements.txt."}
    if not OPENAI_API_KEY:
        return {"error": "OPENAI_API_KEY не задан в .env"}

    prompt = _build_prompt(user_text, times)

    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Ты отвечаешь строго в JSON."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )

    content = resp.choices[0].message.content or ""
    content = _strip_code_fences(content)

    data = json.loads(content)

    task = data.get("task")
    dt = data.get("datetime")
    original = data.get("original", user_text)

    if not task:
        return {"error": "Не удалось извлечь task из ответа модели."}

    if dt is None or (isinstance(dt, str) and dt.strip().lower() == "null"):
        dt = _default_datetime_str(times["default"])
    else:
        dt = _fix_past_datetime(str(dt), user_text, times["default"])

    return {"task": task, "datetime": dt, "original": original, "error": None}


def parse_text(user_text: str, user_times: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Возвращает:
      {"task": "...", "datetime": "...", "original": "...", "error": None}
    или:
      {"error": "..."}
    """
    try:
        times = _get_times(user_times)

        # 0) Явное время (11:45) — локально и надёжно
        explicit = _try_parse_explicit_time(user_text, times["default"])
        if explicit is not None:
            return explicit

        # 1) Простые "утром/днем/вечером" — тоже локально (с учётом настроек)
        simple_parts = _try_parse_simple_dayparts(user_text, times)
        if simple_parts is not None:
            return simple_parts

        # 2) Если вообще нет признаков даты/времени — ставим default_time локально
        if not _looks_like_datetime_text(user_text):
            return {
                "task": _clean_task(user_text),
                "datetime": _default_datetime_str(times["default"]),
                "original": user_text,
                "error": None,
            }

        # 3) Иначе — OpenAI
        return _parse_with_openai(user_text, times)

    except Exception as e:
        return {"error": str(e)}


# ---------------------------
# MULTI REMINDERS SPLITTER
# ---------------------------

def _simple_split_lines(text: str) -> List[str]:
    """
    Дёшево и сердито: сначала пробуем разрезать по строкам/маркерам без OpenAI.
    """
    t = (text or "").strip()
    if not t:
        return []

    # нормализуем переносы
    t = t.replace("\r\n", "\n").replace("\r", "\n")

    # если явно несколько строк — режем по строкам
    if "\n" in t:
        parts = []
        for line in t.split("\n"):
            line = line.strip()
            line = re.sub(r"^[•\-–—\*\d\)\.]+\s*", "", line)  # убираем маркеры
            if line:
                parts.append(line)
        return parts if len(parts) >= 2 else [t]

    # если в одной строке, но много " ; " — можно попробовать разделить
    if ";" in t:
        parts = [p.strip() for p in t.split(";") if p.strip()]
        return parts if len(parts) >= 2 else [t]

    # Эвристика для одной строки без переносов:
    # часто в голосе несколько напоминаний идут одной строкой.
    # Пример: "через 5 минут ... и через 2 часа ...".
    lower = t.lower()

    # если есть минимум два временных маркера — вероятно это несколько напоминаний
    time_markers = re.findall(
        r"\b(через|завтра|сегодня|послезавтра|утром|дн[её]м|вечером|в\s+\d{1,2}([:.]\d{2})?)\b",
        lower,
    )

    if len(time_markers) >= 2:
        # 1) Сначала пробуем "умное" разбиение по повторяющимся временным маркерам.
        # Это хорошо работает для фраз вида:
        # "через 2 минуты ..., через 5 минут ..., и через 2 часа ..."
        marker_re = re.compile(
            r"\b(через|завтра|сегодня|послезавтра|утром|дн[её]м|вечером)\b|\bв\s+\d{1,2}([:.]\d{2})?\b",
            flags=re.IGNORECASE,
        )
        starts = [m.start() for m in marker_re.finditer(t)]
        if len(starts) >= 2:
            cuts = [0] + starts[1:] + [len(t)]
            parts = []
            for a, b in zip(cuts, cuts[1:]):
                chunk = t[a:b].strip(" \t\n,.;")
                # убираем ведущие связки
                chunk = re.sub(r"^(и|а|потом|затем|а потом)\b\s*", "", chunk, flags=re.IGNORECASE)
                # убираем ведущие общие слова
                chunk = re.sub(r"^(надо|нужно|пожалуйста|план)\b\s*", "", chunk, flags=re.IGNORECASE)
                # убираем хвостовые связки, чтобы не мешали дальнейшему парсингу
                chunk = re.sub(r"\s+(и|потом|затем)\s*$", "", chunk, flags=re.IGNORECASE)
                chunk = chunk.strip(" \t\n,.;")
                if chunk:
                    parts.append(chunk)

            if len(parts) >= 2:
                return parts

        # 2) Если не получилось — пробуем разделители в порядке приоритета
        splitters = [
            r"\s+а\s+потом\s+",
            r"\s+затем\s+",
            r"\s+потом\s+",
            r"\s+и\s+",
        ]
        for sp in splitters:
            parts = [p.strip(" ,.;") for p in re.split(sp, t, flags=re.IGNORECASE) if p.strip()]
            if len(parts) >= 2:
                return parts

    return [t]


def split_into_reminders(text: str, model: str = "gpt-4o-mini") -> dict:
    """
    Делит текст на список отдельных напоминаний.
    Возвращает:
      {"items": ["...", "..."], "error": None}
    или {"items": [], "error": "..."}
    """
    try:
        # 1) сперва пытаемся без модели
        items = _simple_split_lines(text)
        if len(items) >= 2:
            return {"items": items, "error": None}

        # 2) если получилось 1 — можно всё равно вернуть 1
        if len(items) == 1:
            # если строка короткая и без явных разделителей — не тратим токены
            return {"items": items, "error": None}

        # 3) fallback на OpenAI (на всякий)
        if OpenAI is None:
            return {"items": [], "error": "Библиотека openai не установлена. Установите зависимости из requirements.txt."}

        if not OPENAI_API_KEY:
            return {"items": [], "error": "OPENAI_API_KEY не задан в .env"}

        client = OpenAI(api_key=OPENAI_API_KEY)

        prompt = f"""
Разбей пользовательский текст на отдельные напоминания.

Верни ТОЛЬКО JSON (без пояснений):
{{
  "items": ["строка1", "строка2", "..."]
}}

Правила:
- Каждый элемент items — одно напоминание (короткая фраза пользователя)
- Не добавляй ничего от себя
- Если напоминание одно — верни массив из одного элемента
- Если текста недостаточно — верни пустой массив

Текст:
{text}
""".strip()

        r = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Ты аккуратно делишь текст на отдельные напоминания."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=300,
        )

        content = r.choices[0].message.content or ""
        content = _strip_code_fences(content)
        data = json.loads(content)

        items = data.get("items", [])
        if not isinstance(items, list):
            return {"items": [], "error": "Неверный формат items"}

        items = [str(x).strip() for x in items if str(x).strip()]
        return {"items": items, "error": None}

    except Exception as e:
        return {"items": [], "error": str(e)}
