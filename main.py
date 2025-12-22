import asyncio
import logging
import os
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import CHANNEL_ID as CHANNEL_ID_ENV, TELEGRAM_BOT_TOKEN, validate_config
from parser import parse_text, split_into_reminders
from speech import recognize_audio
from utils import (
    add_reminder,
    delete_reminder,
    ensure_user_settings,
    fetch_due_reminders,
    fetch_pending_reminders,
    get_setting,
    get_user_settings,
    init_db,
    mark_error,
    mark_sent,
    set_setting,
    update_user_times,
    get_user_channel,
    update_user_channel,

)

# --------------------
# Логи
# --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("PlannerBot")

MOSCOW_TZ = pytz.timezone("Europe/Moscow")
UTC = pytz.UTC


# --------------------
# Вспомогательное
# --------------------
def _parse_dt_moscow(dt_str: str) -> datetime:
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    return MOSCOW_TZ.localize(dt)


def _to_utc_ts(dt_msk: datetime) -> int:
    return int(dt_msk.astimezone(UTC).timestamp())


def _is_valid_hhmm(value: str) -> bool:
    # HH:MM (00:00..23:59)
    if not isinstance(value, str):
        return False
    if len(value) != 5 or value[2] != ":":
        return False
    hh, mm = value[:2], value[3:]
    if not (hh.isdigit() and mm.isdigit()):
        return False
    h, m = int(hh), int(mm)
    return 0 <= h <= 23 and 0 <= m <= 59


def _get_channel_id() -> str:
    # Если канал задан через бота — берём из БД, иначе из env
    return get_setting("channel_id") or str(CHANNEL_ID_ENV)
def _normalize_user_times(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Приводим то, что лежит в user_settings, к ключам:
    morning/day/evening/default
    """
    if not raw:
        return {"morning": "09:00", "day": "14:00", "evening": "20:00", "default": "20:00"}

    return {
        "morning": raw.get("morning_time") or raw.get("morning") or "09:00",
        "day": raw.get("day_time") or raw.get("day") or "14:00",
        "evening": raw.get("evening_time") or raw.get("evening") or "20:00",
        "default": raw.get("default_time") or raw.get("default") or "20:00",
    }


async def _check_channel_access(bot, channel_id: str) -> Tuple[bool, str]:
    """
    Проверяем, можем ли мы отправить сообщение в канал.
    """
    try:
        # ТИХАЯ проверка без отправки сообщений в канал.
        # 1) проверяем, что чат существует
        chat = await bot.get_chat(channel_id)

        # 2) проверяем статус бота в канале
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)

        status = getattr(member, "status", None)
        if status not in ("administrator", "creator"):
            return False, "Бот не администратор канала (нет прав публиковать сообщения)."

        # В python-telegram-bot у ChatMemberAdministrator может быть can_post_messages.
        can_post = getattr(member, "can_post_messages", None)
        if can_post is False:
            return False, "У бота нет права «Публиковать сообщения» в канале."

        return True, "OK"
    except Exception as e:
        return False, str(e)


def _split_lines(text: str) -> List[str]:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    return [ln for ln in lines if ln]


# --------------------
# Онбординг
# --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # новый цикл онбординга при каждом /start
    context.user_data.pop("awaiting_times_confirm", None)
    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=InputFile("assets/onboarding.png"),
        caption=(
            "👋 Привет!\n"
            "Я PlannerBot — принимаю напоминания и публикую их в вашем Telegram-канале в нужное время.\n\n"

            "📝 Как это работает:\n"
            "• У вас есть канал, куда будут приходить напоминания\n"
            "• Вы пишите в бот текст напоминания и указываете время, когда нужно его прислать (текстом или голосом)\n"
            "• В нужный момент бот отправит напоминание в ваш канал\n\n"

            "📌 Пример:\n"
            "«Купить продукты послезавтра утром»\n"
            "«1 января покататься на лыжах»\n\n"

            "1️⃣ Для старта настроим бот:\n\n"
            "0) Создайте приватный Telegram-канал (туда будут приходить напоминания)\n"
            "1) Добавьте бота в ваш канал\n"
            "2) Назначьте бота администратором канала\n"
            "3) Дайте право «Публиковать сообщения»\n"
            "4) Привяжите канал к боту:\n"
            "   • перешлите в бот любое сообщение из нужного канала\n\n"

            "После подключения канала продолжим настройку 👇"
        )
    )



async def _send_channel_and_time_intro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Объединённый шаг онбординга после подключения канала.
    Тут же предлагаем настроить время и даём кнопку "Оставляем ✅".
    """
    user_id = update.effective_user.id
    ensure_user_settings(user_id)
    s = _normalize_user_times(get_user_settings(user_id))

    intro = (
        "✅ Канал подключён! Туда будут приходить напоминания. \n\n"
        "⏰ Как бот понимает время в напоминаниях:\n\n"
        "• Если время не указано — напоминание будет запланировано на время по умолчанию\n"
        "  (если это время уже прошло — на завтра)\n\n"
        "• Я понимаю формулировки:\n"
        "  «утром», «днём», «вечером», «завтра», «через 2 часа»,\n"
        "  «в субботу», «в 11:45», «в пол 8»\n\n"
        "👉 Можете настроить, когда для вас «утро / день / вечер».\n\n"
        "Сейчас так:\n"
        f"🌅 Утро:{s['morning']}\n"
        f"🌞 День:{s['day']}\n"
        f"🌙 Вечер:{s['evening']}\n"
        f"⏱ По умолчанию (если время не указано):{s['default']}\n\n"
        "Если хотите изменить — отправьте:\n"
        f"/times {s['morning']} {s['day']} {s['evening']} {s['default']}\n"
        "(утро день вечер дефолт)\n\n"
        "Или оставляем как есть?"
    )

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Оставляем ✅", callback_data="times_keep")]]
    )

    context.user_data["awaiting_times_confirm"] = True
    await update.message.reply_text(intro, reply_markup=keyboard)


async def _send_usage_after_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Работает и для обычного сообщения, и для callback-кнопки
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "✅ Время настроено!\n\n"
        "📝 Как пользоваться ботом:\n\n"
        "• Отправьте текст или голосовое сообщение с напоминанием\n"
        "• Подтвердите результат\n"
        "• После подтверждения напоминание будет отправлено в ваш канал в нужное время\n\n"
        
         "📌 Пример напоминаний:\n"
            "«Не забыть покормить кота»\n"
            "«31 декабря встретить Новый год»\n\n"
            
        "📎 Доступные команды:\n"
        "/times — настройки времени (утро / день / вечер)\n"
        "/list — список активных напоминаний и удаление\n"
    )


# --------------------
# Команды
# --------------------
async def pingchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id = _get_channel_id()
    ok, msg = await _check_channel_access(context.bot, channel_id)
    if ok:
        await update.message.reply_text("✅ Успешно! Я могу писать в канал.")
    else:
        await update.message.reply_text(
            "⚠️ Не могу отправить сообщение в канал.\n\n"
            f"{msg}\n\n"
            "Проверьте:\n"
            "• бот добавлен в канал\n"
            "• бот администратор\n"
            "• есть право «Публиковать сообщения»"
        )


async def setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /setchannel -1001234567890
    if not context.args:
        await update.message.reply_text(
            "Использование:\n"
            "/setchannel -1001234567890\n\n"
            "⚠️ Важно: перед этим добавьте бота в канал и сделайте администратором."
        )
        return

    channel_id = context.args[0].strip()
    if not (channel_id.startswith("-100") and channel_id[1:].isdigit()):
        await update.message.reply_text(
            "Похоже, channel_id неправильный.\n"
            "Он обычно выглядит так: -1001234567890"
        )
        return

    ok, msg = await _check_channel_access(context.bot, channel_id)
    if not ok:
        await update.message.reply_text(
            "⚠️ Не могу подтвердить доступ к этому каналу.\n"
            f"{msg}\n\n"
            "Проверьте:\n"
            "• бот добавлен в канал\n"
            "• бот администратор\n"
            "• есть право «Публиковать сообщения»"
        )
        return

    set_setting("channel_id", channel_id)
    # Всегда продолжаем онбординг после успешного подключения канала.
    await _send_channel_and_time_intro(update, context)


async def times_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id
    ensure_user_settings(user_id)

    # /times без аргументов — показать текущие настройки
    if not context.args:
        s = _normalize_user_times(get_user_settings(user_id))
        await update.message.reply_text(
            "⚙️ Текущие настройки времени:\n\n"
            f"🌅 Утро: {s['morning']}\n"
            f"🌞 День: {s['day']}\n"
            f"🌙 Вечер: {s['evening']}\n"
            f"⏱ По умолчанию: {s['default']}\n\n"
            "Чтобы изменить, отправьте:\n"
            f"/times {s['morning']} {s['day']} {s['evening']} {s['default']}\n"
            "(утро день вечер дефолт)"
        )
        return

    # /times 08:00 13:00 17:00 20:00 — сохранить
    if len(context.args) != 4:
        await update.message.reply_text(
            "Использование:\n"
            "/times 08:00 13:00 17:00 20:00\n"
            "(утро день вечер дефолт)"
        )
        return

    morning, day, evening, default = [a.strip() for a in context.args]

    bad = [t for t in [morning, day, evening, default] if not _is_valid_hhmm(t)]
    if bad:
        await update.message.reply_text(
            "Похоже, время указано неверно.\n"
            "Формат должен быть HH:MM (например 08:00), часы 00..23, минуты 00..59.\n\n"
            "Пример:\n"
            "/times 08:00 13:00 17:00 20:00"
        )
        return

    update_user_times(user_id, morning, day, evening, default)

    # В онбординге — не шлём лишнее подтверждение, а сразу финальный блок
    if context.user_data.get("awaiting_times_confirm", False):
        context.user_data["awaiting_times_confirm"] = False
        await _send_usage_after_times(update, context)
        return

    await update.message.reply_text(
        "✅ Настройки сохранены!\n\n"
        f"🌅 Утро: {morning}\n"
        f"🌞 День: {day}\n"
        f"🌙 Вечер: {evening}\n"
        f"⏱ По умолчанию: {default}"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = fetch_pending_reminders(limit=50)
    if not rows:
        await update.message.reply_text("✅ Нет активных (pending) напоминаний.")
        return

    lines = ["📌 Активные напоминания (pending):"]
    for r in rows:
        dt_utc = datetime.fromtimestamp(r["scheduled_ts"], tz=UTC)
        dt_msk = dt_utc.astimezone(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"#{r['id']} — {dt_msk} — {r['task']}")
    lines.append("\nЧтобы удалить: /delete <id>")

    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3500] + "\n…(обрезано)"
    await update.message.reply_text(text)


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /delete <id>")
        return
    try:
        rid = int(context.args[0])
    except Exception:
        await update.message.reply_text("id должен быть числом. Пример: /delete 12")
        return

    ok = delete_reminder(rid)
    await update.message.reply_text("✅ Удалено." if ok else "Не нашла такое напоминание.")


# --------------------
# Создание напоминаний (1 или пакет)
# --------------------
async def _process_single(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    msg = await update.message.reply_text("🔄 Анализирую...")

    user_id = update.effective_user.id
    ensure_user_settings(user_id)
    user_times = _normalize_user_times(get_user_settings(user_id))

    result = await asyncio.to_thread(parse_text, user_text, user_times)

    if result.get("error"):
        await msg.edit_text(f"Ошибка: {result['error']}")
        return

    task = result.get("task")
    dt_str = result.get("datetime")
    original = result.get("original", user_text)

    if not dt_str:
        await msg.edit_text("Не смогла определить дату/время из текста. Попробуйте переформулировать.")
        return

    context.user_data["pending"] = {"task": task, "datetime": dt_str, "original": original}

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Да", callback_data="confirm_yes"),
            InlineKeyboardButton("❌ Нет", callback_data="confirm_no"),
        ]]
    )

    await msg.edit_text(f"📝 Задача: {task}\n⏰ Дата: {dt_str}\n\nСоздать?", reply_markup=keyboard)


async def _process_batch(update: Update, context: ContextTypes.DEFAULT_TYPE, items: List[str]):
    msg = await update.message.reply_text("🔄 Анализирую список...")

    user_id = update.effective_user.id
    ensure_user_settings(user_id)
    user_times = _normalize_user_times(get_user_settings(user_id))

    parsed: List[Dict[str, Any]] = []
    errors: List[str] = []

    for i, t in enumerate(items, start=1):
        res = await asyncio.to_thread(parse_text, t, user_times)
        if res.get("error") or not res.get("datetime"):
            errors.append(f"{i}) {t} — не смогла понять дату/время")
            continue
        parsed.append(res)

    if errors:
        await msg.edit_text("Не получилось разобрать часть напоминаний:\n\n" + "\n".join(errors))
        return

    preview_lines = []
    for i, r in enumerate(parsed, start=1):
        preview_lines.append(f"{i}) {r.get('task')} — {r.get('datetime')}")
    preview = "\n".join(preview_lines)

    context.user_data["pending_batch_parsed"] = parsed

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Создать все", callback_data="batch_yes"),
            InlineKeyboardButton("❌ Отмена", callback_data="batch_no"),
        ]]
    )

    await msg.edit_text(
        "📝 Я нашла несколько напоминаний:\n\n"
        f"{preview}\n\nСоздать все?",
        reply_markup=keyboard,
    )


async def _process_text_or_batch(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    # Разбиваем по строкам (как просили)
    lines = _split_lines(user_text)
    if len(lines) > 1:
        await _process_batch(update, context, lines)
    else:
        await _process_single(update, context, user_text.strip())


# --------------------
# Хендлеры сообщений
# --------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    # Если это пересылка из канала — используем как setchannel
    fwd = update.message.forward_from_chat
    if fwd and fwd.type == "channel":
        channel_id = str(fwd.id)

        ok, msg = await _check_channel_access(context.bot, channel_id)
        if not ok:
            await update.message.reply_text(
                "⚠️ Я вижу канал, но не могу публиковать туда.\n\n"
                f"{msg}\n\n"
                "Проверьте:\n"
                "• бот добавлен в канал\n"
                "• бот администратор\n"
                "• есть право «Публиковать сообщения»"
            )
            return

        set_setting("channel_id", channel_id)
        # Всегда продолжаем онбординг после успешного подключения канала.
        await _send_channel_and_time_intro(update, context)

        return

    user_text = update.message.text or ""

    # обычный текст — напоминания (1 или пакет)
    await _process_text_or_batch(update, context, user_text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice:
        return

    status = await update.message.reply_text("🎙️ Распознаю голосовое...")

    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "voice.ogg")
            await file.download_to_drive(custom_path=audio_path)

            text = await asyncio.to_thread(recognize_audio, audio_path)

        if not text:
            await status.edit_text("Не удалось распознать речь. Попробуйте ещё раз.")
            return

        # Голос: сначала пробуем разделить в "умном" режиме (если не выйдет — по строкам)
        split = await asyncio.to_thread(split_into_reminders, text)
        if split.get("error"):
            items = _split_lines(text) or [text]
        else:
            items = split.get("items") or _split_lines(text) or [text]

        await status.edit_text(f"✅ Распознано:\n{text}")

        if len(items) > 1:
            await _process_batch(update, context, items)
        else:
            await _process_single(update, context, items[0])

    except Exception as e:
        await status.edit_text(f"Ошибка распознавания: {e}")


# --------------------
# Кнопки
# --------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "times_keep":
        context.user_data["awaiting_times_confirm"] = False
        # без лишних "Ок" — сразу финальный блок
        await query.edit_message_reply_markup(reply_markup=None)
        await _send_usage_after_times(update, context)
        return

    if data in ("batch_no", "batch_yes"):
        if data == "batch_no":
            context.user_data.pop("pending_batch_parsed", None)
            await query.edit_message_text("Ок, отменено ✅")
            return

        parsed = context.user_data.get("pending_batch_parsed") or []
        if not parsed:
            await query.edit_message_text("Нет данных для подтверждения. Отправьте напоминания заново.")
            return

        # создаём все
        created = 0
        for r in parsed:
            dt_str = r["datetime"]
            when_msk = _parse_dt_moscow(dt_str)
            now_msk = datetime.now(MOSCOW_TZ)
            if when_msk <= now_msk:
                # пропускаем прошлое
                continue
            scheduled_ts = _to_utc_ts(when_msk)
            add_reminder(
                task=r["task"],
                original=r.get("original", ""),
                scheduled_ts=scheduled_ts,
                user_id=query.from_user.id,
            )
            created += 1

        context.user_data.pop("pending_batch_parsed", None)
        await query.edit_message_text(f"✅ Готово! Запланировала напоминаний: {created}.")
        return

    # одиночное подтверждение
    pending = context.user_data.get("pending")
    if not pending:
        await query.edit_message_text("Нет данных для подтверждения. Отправьте напоминание заново.")
        return

    if data == "confirm_no":
        context.user_data.pop("pending", None)
        await query.edit_message_text("Ок, отменено ✅")
        return

    if data == "confirm_yes":
        dt_str = pending["datetime"]
        try:
            when_msk = _parse_dt_moscow(dt_str)
        except Exception:
            await query.edit_message_text("Не смогла распарсить дату. Попробуйте снова.")
            return

        now_msk = datetime.now(MOSCOW_TZ)
        if when_msk <= now_msk:
            await query.edit_message_text("Время уже в прошлом. Отправьте напоминание заново.")
            return

        scheduled_ts = _to_utc_ts(when_msk)
        add_reminder(
            task=pending["task"],
            original=pending["original"],
            scheduled_ts=scheduled_ts,
            user_id=query.from_user.id,
        )

        context.user_data.pop("pending", None)
        await query.edit_message_text("✅ Запланировано. Посмотреть список: /list")
        return


# --------------------
# Отправка напоминаний в канал
# --------------------
async def reminders_loop(app: Application, interval_seconds: int = 15):
    while True:
        try:
            due = fetch_due_reminders(limit=20)
            for r in due:
                reminder_id = r["id"]
                try:
                    text = f"⏰ Напоминание: {r['task']}\n\n"
                    await app.bot.send_message(chat_id=_get_channel_id(), text=text)
                    mark_sent(reminder_id)
                except Exception as e:
                    mark_error(reminder_id, str(e))
        except Exception:
            pass

        await asyncio.sleep(interval_seconds)

async def post_init(app: Application):
    loop = asyncio.get_running_loop()
    loop.create_task(reminders_loop(app, interval_seconds=15))

# --------------------
# Ошибки PTB
# --------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Ошибка в обработчике", exc_info=context.error)


def main():
    # Валидируем env-переменные при запуске бота (а не при импорте модулей)
    validate_config(require_openai=True)
    init_db()

    application = (
    Application.builder()
    .token(TELEGRAM_BOT_TOKEN)
    .post_init(post_init)
    .build()
)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("delete", delete_cmd))
    application.add_handler(CommandHandler("pingchannel", pingchannel))
    application.add_handler(CommandHandler("setchannel", setchannel))
    application.add_handler(CommandHandler("times", times_cmd))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(CallbackQueryHandler(on_button))

    application.add_error_handler(on_error)

    application.run_polling()


if __name__ == "__main__":
    main()
