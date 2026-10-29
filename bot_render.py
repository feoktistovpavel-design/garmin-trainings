"""
Версия бота для Render (режим webhook). Запуск:  python bot_render.py

Использует telegram_bot.py (обработчики, кнопки) и garmin_workout.py (разбор,
сборка тренировки) без изменений. Все секреты берутся из переменных окружения Render.
"""

import logging
import os
import re

from google import genai
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import telegram_bot as tb

logging.getLogger("tornado.access").setLevel(logging.WARNING)  # в логах не должно быть секретного пути

REQUIRED = [
    "TELEGRAM_BOT_TOKEN",
    "GEMINI_API_KEY",
    "ALLOWED_USER_ID",
    "WEBHOOK_SECRET",
    "GARMIN_EMAIL",
    "GARMIN_PASSWORD",
]


def main():
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"Не заданы переменные окружения: {', '.join(missing)}")

    user_id = os.environ["ALLOWED_USER_ID"]
    if not user_id.isdigit():
        raise SystemExit("ALLOWED_USER_ID должен быть числом.")

    secret = os.environ["WEBHOOK_SECRET"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", secret):
        raise SystemExit("WEBHOOK_SECRET: только латинские буквы, цифры, _ и -, не короче 16 символов.")

    base_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL")
    if not base_url:
        raise SystemExit("Не найден адрес сервиса (RENDER_EXTERNAL_URL).")

    # Настраиваем общие обработчики из telegram_bot.py
    tb.ALLOWED_USER_ID = int(user_id)
    tb.gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    only_me = filters.User(user_id=tb.ALLOWED_USER_ID)

    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", tb.start, filters=only_me))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & only_me, tb.handle_text))
    app.add_handler(MessageHandler(filters.VOICE & only_me, tb.handle_voice))
    app.add_handler(CallbackQueryHandler(tb.on_button))

    port = int(os.environ.get("PORT", 10000))
    print(f"Запускаю webhook на порту {port}")
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=secret,
        webhook_url=f"{base_url.rstrip('/')}/{secret}",
        secret_token=secret,
    )


if __name__ == "__main__":
    main()
