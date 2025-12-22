import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List

import pytz
from openai import OpenAI

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
    t = (text or "").strip()
    t = re.sub(r"\s+", " ", t)
    t = t.strip(" .,!?:;—-")
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
    Поддерживаем оба формата ключей:
      - morning/day/evening/default
      - morning_time/day_time/evening_time/default_time
    """
    user_times = user_times or {}
    morning = _normalize_hhmm(str(user_times.get("morning", user_times.get("morning_time", ""))), "09:00")
    day = _normalize_hhmm(str(user_times.get("day", user_times.get("day_time", ""))), "14:00")
    evening = _normalize_hhmm(str(user_times.get("evening", user_times.get("evening_time", ""))), "20:00")
    default = _normalize_hhmm(str(user_times.get("default", user_times.get("default_time", ""))), "20:00")
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


# ---------------------------
# EXPLICIT DATE+TIME (local)
# ---------------------------

_EXPLICIT_DT_RE = re.compile(
    r"""(?ix)
    \b
    (?P<day>\d{1,2})[./-](?P<month>\d{1,2})
    (?:[./-](?P<year>\d{2,4}))?
    (?:\s*(?:года?|г\.)\s*)?
    [\s,]+
    (?:в|во)?\s*
    (?P<hour>\d{1,2})[:.](?P<minute>\d{2})
    \b
    """,
)


_DATE_TOKEN_RE = re.compile(
    r"""(?x)
    \b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b
    """
)

_DAYPART_WORDS_RE = re.compile(r"\b(утром|утра|дн[её]м|днем|вечером|вечера|ночью)\b", re.IGNORECASE)


def _normalize_year_2or4(y: Optional[str]) -> int:
    """
    Нормализация двухзначного года: 00-69 -> 2000-2069, 70-99 -> 1970-1999.
    Если год не указан — используем текущий.
    """
    now_year = _now_moscow().year
    if not y:
        return now_year
    yy = int(y)
    if yy < 100:
        return 2000 + yy if yy <= 69 else 1900 + yy
    return yy


def _try_parse_explicit_datetime(user_text: str) -> Optional[Dict[str, Any]]:
    """
    Локально ловим явную дату+время (например: 21.12.25 14:48, 01-01-2026 00:15).
    ВАЖНО: текст задачи НЕ обрезаем — вырезаем только сам фрагмент даты/времени.
    """
    t = (user_text or "").strip()
    m = _EXPLICIT_DT_RE.search(t)
    if not m:
        return None

    day = int(m.group("day"))
    month = int(m.group("month"))
    year = _normalize_year_2or4(m.group("year"))
    hh = int(m.group("hour"))
    mm = int(m.group("minute"))

    if not (1 <= day <= 31 and 1 <= month <= 12 and 0 <= hh <= 23 and 0 <= mm <= 59):
        return None

    try:
        dt = MOSCOW_TZ.localize(datetime(year, month, day, hh, mm, 0))
    except Exception:
        return None

    # сохраняем текст ДО и ПОСЛЕ даты
    before = t[:m.start()].strip()
    after = t[m.end():].strip()

    combined = f"{before} {after}".strip()
    combined = re.sub(r"\s+", " ", combined)
    combined = re.sub(r"\s+,", ",", combined)
    combined = re.sub(r",\s+", ", ", combined)
    combined = combined.strip(" ,")

    task_text = _clean_task(combined) if combined else _clean_task(t)

    return {
        "task": task_text,
        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "original": user_text,
        "error": None,
    }


def _try_parse_explicit_time(user_text: str, default_time_hhmm: str) -> Optional[Dict[str, Any]]:
    """
    Локально ловим явное время вида 11:45 или 11.45.
    ✅ Учитываем слова "сегодня/завтра/послезавтра".
    ✅ Учитываем "вечером/ночью" => PM (21:30).
    """
    t = user_text.strip()
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", t)
    if not m:
        return None

    # Защита: не путать дату вида 01.01.26 с временем 01:01
    for dm in _DATE_TOKEN_RE.finditer(t):
        ds, de = dm.span()
        ms, me = m.span()
        if not (me <= ds or ms >= de):
            return None

    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None

    now = _now_moscow()
    tl = t.lower()

    # ✅ День (сегодня/завтра/послезавтра)
    base = now
    if "послезавтра" in tl:
        base = now + timedelta(days=2)
    elif "завтра" in tl:
        base = now + timedelta(days=1)

    # ✅ PM-логика для "вечером / ночью"
    if hh < 12 and re.search(r"\b(вечером|вечера|ночью)\b", tl):
        hh += 12

    dt = base.replace(hour=hh, minute=mm, second=0, microsecond=0)

    # если всё равно получилось в прошлом — переносим на завтра
    if dt <= now:
        dt = dt + timedelta(days=1)

    task_text = re.sub(r"\bв\s*\d{1,2}[:.]\d{2}\b", "", t, flags=re.IGNORECASE)
    task_text = _DAYPART_WORDS_RE.sub("", task_text)  # ✅ убираем "вечера/вечером/утром..."
    task_text = _clean_task(task_text)


    return {
        "task": task_text if task_text else _clean_task(t),
        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "original": user_text,
        "error": None,
    }

def _try_parse_space_time(user_text: str) -> Optional[Dict[str, Any]]:
    """
    Ловим время вида: 'в 9 30', '9 30 вечером', 'в 21 05'.
    """
    t = (user_text or "").strip()
    tl = t.lower()

    m = re.search(r"\bв?\s*(\d{1,2})\s+(\d{2})\b", tl)
    if not m:
        return None

    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None

    now = _now_moscow()

    # дата: сегодня / завтра / послезавтра
    base = now
    if "послезавтра" in tl:
        base = now + timedelta(days=2)
    elif "завтра" in tl:
        base = now + timedelta(days=1)

    # вечер / ночь => PM
    if hh < 12 and re.search(r"\b(вечером|вечера|ночью)\b", tl):
        hh += 12

    dt = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if dt <= now:
        dt = dt + timedelta(days=1)

    # вырезаем 'в 9 30 вечера'
    task_text = (t[:m.start()] + " " + t[m.end():]).strip()
    task_text = _DAYPART_WORDS_RE.sub("", task_text)
    task_text = _clean_task(task_text)


    return {
        "task": task_text if task_text else _clean_task(t),
        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "original": user_text,
        "error": None,
    }


_HOUR_WORDS = {
    "один": 1, "одна": 1,
    "два": 2, "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
}

_MINUTE_WORDS = {
    "ноль": 0,
    "пять": 5,
    "десять": 10,
    "пятнадцать": 15,
    "двадцать": 20,
    "двадцать пять": 25,
    "тридцать": 30,
    "сорок": 40,
    "сорок пять": 45,
    "пятьдесят": 50,
    "пятьдесят пять": 55,
}

_SPOKEN_TIME_RE = re.compile(
    r"""(?ix)
    \bв\s+
    (?P<hour>одиннадцать|двенадцать|десять|девять|восемь|семь|шесть|пять|четыре|три|два|две|один|одна)
    (?:\s+
      (?P<minute>
        ноль\s+пять|ноль\s+десять|ноль\s+пятнадцать|
        двадцать\s+пять|сорок\s+пять|пятьдесят\s+пять|
        пятнадцать|двадцать|тридцать|сорок|пятьдесят|
        пять|десять
      )
    )?
    \b
    """
)

def _try_parse_spoken_time(user_text: str) -> Optional[Dict[str, Any]]:
    """
    Локально ловим время словами: "в девять тридцать", "в десять", "в девять тридцать вечером".
    """
    t = (user_text or "").strip()
    tl = t.lower()

    m = _SPOKEN_TIME_RE.search(tl)
    if not m:
        return None

    hour_word = m.group("hour")
    minute_word = (m.group("minute") or "").strip()

    hh = _HOUR_WORDS.get(hour_word)
    if hh is None:
        return None

    mm = 0
    if minute_word:
        minute_word = re.sub(r"\s+", " ", minute_word)
        mm = _MINUTE_WORDS.get(minute_word, None)
        if mm is None:
            return None

    now = _now_moscow()

    # дата: сегодня/завтра/послезавтра
    base = now
    if "послезавтра" in tl:
        base = now + timedelta(days=2)
    elif "завтра" in tl:
        base = now + timedelta(days=1)

    # вечер/ночь => PM: если час < 12, добавляем 12
    if hh < 12 and re.search(r"\b(вечером|вечера|ночью)\b", tl):
        hh += 12

    dt = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if dt <= now:
        dt = dt + timedelta(days=1)

    # убираем кусок "в девять тридцать" из задачи
    task_text = (t[:m.start()] + " " + t[m.end():]).strip()
    task_text = _DAYPART_WORDS_RE.sub("", task_text)
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

def _try_parse_relative_day_only(user_text: str, default_time_hhmm: str) -> Optional[Dict[str, Any]]:
    """
    Локально обрабатываем "сегодня/завтра/послезавтра" без явного времени и без "утром/днем/вечером".
    Ставим default_time.
    """
    t = (user_text or "").strip()
    tl = t.lower()

    if "послезавтра" not in tl and "завтра" not in tl and "сегодня" not in tl:
        return None

    # если в тексте есть явное время или цифры/дата — пусть это решают другие правила
    if re.search(r"\b\d{1,2}[:.]\d{2}\b", tl):
        return None
    if re.search(r"\b\d{1,2}[./-]\d{1,2}\b", tl):
        return None
    if re.search(r"\b(утром|утра|дн[её]м|днем|вечером|вечера)\b", tl):
        return None

    now = _now_moscow()
    base = now
    if "послезавтра" in tl:
        base = now + timedelta(days=2)
    elif "завтра" in tl:
        base = now + timedelta(days=1)
    # "сегодня" -> base = now

    hh, mm = int(default_time_hhmm[:2]), int(default_time_hhmm[3:])
    dt = base.replace(hour=hh, minute=mm, second=0, microsecond=0)

    # если вдруг "сегодня", а default уже прошел — переносим на завтра
    if dt <= now:
        dt = dt + timedelta(days=1)

    task_text = re.sub(r"\b(сегодня|завтра|послезавтра)\b", "", t, flags=re.IGNORECASE)
    task_text = _clean_task(task_text)

    return {
        "task": task_text if task_text else _clean_task(t),
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

        # 0) Явная дата+время (21.12.25 14:48) — локально и надёжно
        explicit_dt = _try_parse_explicit_datetime(user_text)
        if explicit_dt is not None:
            return explicit_dt

        # 1) Явное время (11:45) — локально и надёжно
        explicit = _try_parse_explicit_time(user_text, times["default"])
        if explicit is not None:
            return explicit
        
        # 2) numeric time with space (9 30)
        space_time = _try_parse_space_time(user_text)
        if space_time is not None:
            return space_time
        
        # 3) девять тридцать
        spoken = _try_parse_spoken_time(user_text)
        if spoken is not None:
            return spoken

        # 4) Простые "утром/днем/вечером" — тоже локально (с учётом настроек)
        relative_day_only = _try_parse_relative_day_only(user_text, times["default"])
        if relative_day_only is not None:
            return relative_day_only
       
        simple_parts = _try_parse_simple_dayparts(user_text, times)
        if simple_parts is not None:
            return simple_parts

        # 5) Если вообще нет признаков даты/времени — ставим default_time локально
        if not _looks_like_datetime_text(user_text):
            return {
                "task": _clean_task(user_text),
                "datetime": _default_datetime_str(times["default"]),
                "original": user_text,
                "error": None,
            }

        # 6) Иначе — OpenAI
        return _parse_with_openai(user_text, times)

    except Exception as e:
        return {"error": str(e)}


# ---------------------------
# MULTI REMINDERS SPLITTER
# ---------------------------

def _simple_split_lines(text: str) -> List[str]:
    """
    Дёшево и сердито: сначала пробуем разрезать без OpenAI.

    Важно для голосовых:
    - поддерживаем случаи вида: "через 2 минуты ..., через 5 минут ... и через 2 часа ..."
    - поддерживаем "22.12.25 ... . вечером ... . и послезавтра ..."
    """
    t = (text or "").strip()
    if not t:
        return []

    # нормализуем переносы
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\s+", " ", t).strip()

    # 1) если явно несколько строк — режем по строкам
    if "\n" in t:
        parts: List[str] = []
        for line in t.split("\n"):
            line = line.strip()
            line = re.sub(r"^[•\-–—\*\d\)\.]+\s*", "", line)  # убираем маркеры
            if line:
                parts.append(line)
        return parts if len(parts) >= 2 else [t]

    # 2) точка с запятой — почти всегда разделитель
    if ";" in t:
        parts = [p.strip() for p in t.split(";") if p.strip()]
        return parts if len(parts) >= 2 else [t]

    # 3) если есть несколько "временных якорей", пробуем расставить переносы.
    # Сначала ловим "полную" дату+время как ОДИН якорь, чтобы не считать дату и время отдельно
    explicit_dt_anchor = r"""
        \b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?
        (?:\s*(?:года?|г\.)\s*)?
        [\s,]+
        (?:в|во)?\s*
        \d{1,2}[:.]\d{2}\b
    """

    anchor_next = rf"(?:{explicit_dt_anchor}|через\b|сегодня\b|завтра\b|послезавтра\b|утром\b|утра\b|дн[её]м\b|днем\b|вечером\b|вечера\b|\d{{1,2}}[./-]\d{{1,2}}(?:[./-]\d{{2,4}})?\b|\d{{1,2}}[:.]\d{{2}}\b)"

    anchors = re.findall(anchor_next, t, flags=re.IGNORECASE | re.VERBOSE)


    if len(anchors) >= 2:
        # ✅ Разделяем по окончаниям предложений, если дальше начинается новый "якорь"
        t2 = re.sub(rf"[.!?]\s*(?={anchor_next})", "\n", t, flags=re.IGNORECASE)

        # запятая перед новым напоминанием
        t2 = re.sub(rf",\s*(?={anchor_next})", "\n", t2, flags=re.IGNORECASE)

        # "и/а" перед новым напоминанием
        t2 = re.sub(rf"\s+(?:и|а)\s+(?={anchor_next})", "\n", t2, flags=re.IGNORECASE)

        # "потом/затем/а потом" перед новым напоминанием
        t2 = re.sub(rf"\s+(?:а\s+потом|потом|затем)\s+(?={anchor_next})", "\n", t2, flags=re.IGNORECASE)

        parts = [p.strip(" ,") for p in t2.split("\n") if p.strip(" ,")]

        # лёгкая чистка префиксов
        cleaned: List[str] = []
        for p in parts:
            p = re.sub(r"^(?:нужно|надо|пожалуйста)\s+", "", p, flags=re.IGNORECASE).strip()
            if p:
                cleaned.append(p)

        return cleaned if len(cleaned) >= 2 else [t]

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
            return {"items": items, "error": None}

        # 3) fallback на OpenAI (на всякий)
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
