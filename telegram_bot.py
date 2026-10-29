"""
Этап 3: Telegram-бот для создания беговых тренировок в Garmin Connect.

Лежит в одной папке с garmin_workout.py (его функции мы используем).

Установка:  py -m pip install -U python-telegram-bot
Настройка:  рядом с файлами создай файл .env (см. ниже)
Запуск:     py telegram_bot.py

Файл .env (3 строки, без кавычек):
    TELEGRAM_BOT_TOKEN=токен_от_BotFather
    GEMINI_API_KEY=ключ_Gemini
    ALLOWED_USER_ID=твой_числовой_id_в_Telegram
"""

import asyncio
import logging
import os
from pathlib import Path

from google import genai
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from garmin_workout import (
    apply_pace,
    apply_pano,
    build_garmin_workout,
    garmin_login,
    parse_workout,
    summary,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # не засорять лог токеном в URL
log = logging.getLogger("garmin-bot")

gemini_client = None
_garmin = None  # клиент Garmin, чтобы не логиниться на каждую тренировку


# ---------- Настройки из .env ----------

def load_env():
    path = Path(__file__).with_name(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------- Garmin ----------

def get_garmin():
    global _garmin
    if _garmin is None:
        _garmin = garmin_login()
    return _garmin


def create_in_garmin(data):
    """Создаёт тренировку и ставит её в календарь. Выполняется в отдельном потоке."""
    global _garmin
    try:
        client = get_garmin()
        result = client.upload_workout(build_garmin_workout(data))
        client.schedule_workout(result["workoutId"], data["date"])
    except Exception:
        _garmin = None  # в следующий раз войдём заново
        raise


# ---------- Кнопки ----------

KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Создать", callback_data="create"),
            InlineKeyboardButton("✏️ Изменить", callback_data="edit"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]
)


# ---------- Обработчики ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Напиши или надиктуй тренировку, например:\n\n"
        "«Тренировка на завтра: разминка 10 минут, 6 по 1 км в темпе 5:00, "
        "отдых 2 минуты в 70% от ПАНО, заминка 5 минут»\n\n"
        "Я покажу расшифровку, и после твоего подтверждения создам тренировку в Garmin."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    state = context.user_data.setdefault("state", {})
    awaiting = state.get("awaiting")

    if awaiting == "clarify":
        state["request"] += ". " + text
    elif awaiting == "edit":
        state["request"] += ". Исправление: " + text
    else:
        state.clear()
        state["request"] = text
    state["awaiting"] = None

    await process_request(update, state)


async def process_request(update: Update, state: dict):
    msg = await update.message.reply_text("Разбираю...")
    try:
        data = await asyncio.to_thread(parse_workout, gemini_client, state["request"])

        if data.get("question"):
            state["awaiting"] = "clarify"
            await msg.edit_text(f"Уточнение: {data['question']}")
            return

        apply_pace(data)
        apply_pano(data)
        text = summary(data)
    except Exception as e:
        log.exception("Ошибка разбора")
        state.clear()
        await msg.edit_text(f"Не получилось разобрать тренировку: {e}\nПопробуй сформулировать ещё раз.")
        return

    state["data"] = data
    await msg.edit_text(text + "\n\nСоздать?", reply_markup=KEYBOARD)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Голосовые я пока не понимаю. Нажми значок микрофона на клавиатуре телефона "
        "и надиктуй текст, он появится в поле ввода."
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ALLOWED_USER_ID:
        return

    state = context.user_data.setdefault("state", {})
    action = query.data

    if action == "cancel":
        state.clear()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Отменено.")

    elif action == "edit":
        state["awaiting"] = "edit"
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Что изменить? Напиши, например: «разминка 10 минут».")

    elif action == "create":
        data = state.get("data")
        if not data:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Нечего создавать. Пришли описание тренировки.")
            return

        await query.edit_message_reply_markup(reply_markup=None)  # защита от двойного нажатия
        try:
            await asyncio.to_thread(create_in_garmin, data)
        except Exception as e:
            log.exception("Ошибка Garmin")
            await query.message.reply_text(
                f"Не удалось создать тренировку в Garmin: {e}\nНажми «Создать» ещё раз или отмени.",
                reply_markup=KEYBOARD,
            )
            return

        state.clear()
        await query.message.reply_text(f"✅ Готово! Тренировка создана и поставлена на {data['date']}.")


# ---------- Запуск ----------

def main():
    global gemini_client, ALLOWED_USER_ID

    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    user_id = os.environ.get("ALLOWED_USER_ID")

    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", token), ("GEMINI_API_KEY", gemini_key),
                              ("ALLOWED_USER_ID", user_id)] if not v]
    if missing:
        raise SystemExit(f"В файле .env не хватает: {', '.join(missing)}")
    if not user_id.isdigit():
        raise SystemExit("ALLOWED_USER_ID должен быть числом (твой id в Telegram).")

    ALLOWED_USER_ID = int(user_id)
    gemini_client = genai.Client(api_key=gemini_key)

    # Входим в Garmin заранее: если понадобится код двухфакторной защиты,
    # ты введёшь его здесь, в окне терминала, а не во время работы бота.
    print("Вход в Garmin...")
    get_garmin()
    print("Garmin подключён.")

    only_me = filters.User(user_id=ALLOWED_USER_ID)  # чужие сообщения игнорируются

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start, filters=only_me))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & only_me, handle_text))
    app.add_handler(MessageHandler(filters.VOICE & only_me, handle_voice))
    app.add_handler(CallbackQueryHandler(on_button))

    print("Бот запущен. Остановить: Ctrl+C.")
    app.run_polling()


if __name__ == "__main__":
    main()
