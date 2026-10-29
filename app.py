"""
Мини-проверка: пускает ли Garmin вход с сервера Render.
Открываешь в браузере  https://ТВОЙ-СЕРВИС.onrender.com/test?key=ТВОЙ_TEST_KEY
и видишь результат текстом. Больше ничего не делает.
"""

import hmac
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from garminconnect import Garmin

PORT = int(os.environ.get("PORT", 10000))
TEST_KEY = os.environ.get("TEST_KEY", "")


def run_login_test():
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        return "Не заданы переменные GARMIN_EMAIL и GARMIN_PASSWORD."

    try:
        client = Garmin(email, password)
        client.login()
    except Exception as e:
        return f"ОШИБКА ВХОДА: {type(e).__name__}: {e}"

    result = "ВХОД В GARMIN ВЫПОЛНЕН."
    try:
        result += f" Имя в профиле: {client.get_full_name()}"
    except Exception as e:
        result += f" (имя получить не удалось: {type(e).__name__}: {e})"
    return result


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/test":
            key = parse_qs(url.query).get("key", [""])[0]
            if not TEST_KEY or not hmac.compare_digest(key, TEST_KEY):
                self._send(403, "Неверный ключ.")
                return
            self._send(200, run_login_test())
        else:
            self._send(200, "Сервис работает. Для проверки открой /test?key=...")

    def log_message(self, format, *args):
        pass  # не пишем адреса запросов (в них ключ) в лог


if __name__ == "__main__":
    print(f"Слушаю порт {PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
